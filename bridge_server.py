import argparse
import asyncio
import json
import logging
import os
import ssl
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
from websockets.datastructures import Headers
from websockets.asyncio.client import ClientConnection, connect as ws_connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

AZURE_OPENAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"].strip()
AZURE_OPENAI_API_KEY = os.environ["AZURE_OPENAI_API_KEY"].strip()
AZURE_OPENAI_DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"].strip()
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview").strip()

REALTIME_VOICE = os.environ.get("REALTIME_VOICE", "cedar").strip() or "cedar"
REALTIME_INSTRUCTIONS = (
    os.environ.get(
        "REALTIME_INSTRUCTIONS",
        "You are a friendly phone agent. Answer naturally, keep responses concise, and ask "
        "clarifying questions when needed. Only offer extension numbers when the caller "
        "explicitly asks for them. Do not volunteer people's names until you have narrowed "
        "the probable matches to two or fewer. If the caller's information still leaves more "
        "than two probable matches, ask for more information to narrow the choice. Queue names "
        "are less sensitive and may be shared when appropriate.",
    ).strip()
    or (
        "You are a friendly phone agent. Answer naturally and keep responses concise. Only "
        "offer extension numbers when explicitly asked. Do not volunteer people's names until "
        "the probable matches are two or fewer."
    )
)
BRIDGE_BIND_HOST = os.environ.get("BRIDGE_BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
BRIDGE_BIND_PORT = int(os.environ.get("BRIDGE_BIND_PORT", "8765"))
PHONE_DIRECTORY_MCP_URL = (
    os.environ.get("PHONE_DIRECTORY_MCP_URL", "https://pbs-common-mcp.azurewebsites.net/mcp").strip()
)
PHONE_DIRECTORY_MCP_PROTOCOL_VERSION = (
    os.environ.get("PHONE_DIRECTORY_MCP_PROTOCOL_VERSION", "2025-03-26").strip() or "2025-03-26"
)


def _directory_lookup_is_configured() -> bool:
    return bool(PHONE_DIRECTORY_MCP_URL)


def _mcp_request_headers(*, session_id: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    return headers


def _mcp_initialize_session() -> str:
    response = requests.post(
        PHONE_DIRECTORY_MCP_URL,
        headers=_mcp_request_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PHONE_DIRECTORY_MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "acs-realtime-bridge",
                    "version": "1.0",
                },
            },
        },
        timeout=30,
    )
    response.raise_for_status()
    session_id = response.headers.get("mcp-session-id")
    if not session_id:
        raise RuntimeError("MCP server did not return an mcp-session-id header.")

    initialized_response = requests.post(
        PHONE_DIRECTORY_MCP_URL,
        headers=_mcp_request_headers(session_id=session_id),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=30,
    )
    initialized_response.raise_for_status()
    return session_id


def _mcp_extract_result(payload: dict[str, Any]) -> Any:
    if "error" in payload:
        raise RuntimeError(f"MCP request failed: {payload['error']}")

    result = payload.get("result", {})
    if result.get("isError"):
        raise RuntimeError(f"MCP tool returned isError=true: {result}")

    structured_content = result.get("structuredContent")
    if structured_content is not None:
        if isinstance(structured_content, dict) and set(structured_content) == {"result"}:
            return structured_content["result"]
        return structured_content

    content = result.get("content", [])
    if not content:
        return result

    if len(content) == 1 and content[0].get("type") == "text":
        text = content[0].get("text", "")
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}

    return {"content": content}


def _mcp_list_tools() -> list[dict[str, Any]]:
    session_id = _mcp_initialize_session()
    response = requests.post(
        PHONE_DIRECTORY_MCP_URL,
        headers=_mcp_request_headers(session_id=session_id),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(f"MCP tools/list failed: {payload['error']}")
    result = payload.get("result", {})
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise RuntimeError("MCP tools/list response did not include a tools array.")
    return tools


def _mcp_call_tool(name: str, arguments: dict[str, Any]) -> Any:
    session_id = _mcp_initialize_session()
    response = requests.post(
        PHONE_DIRECTORY_MCP_URL,
        headers=_mcp_request_headers(session_id=session_id),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments,
            },
        },
        timeout=30,
    )
    response.raise_for_status()
    return _mcp_extract_result(response.json())


def _mcp_tool_to_realtime_tool(tool: dict[str, Any]) -> dict[str, Any]:
    name = str(tool.get("name") or "").strip()
    if not name:
        raise ValueError("MCP tool is missing a name.")

    parameters = tool.get("inputSchema")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}, "additionalProperties": True}

    return {
        "type": "function",
        "name": name,
        "description": str(tool.get("description") or f"MCP tool: {name}"),
        "parameters": parameters,
    }


def _validate_config() -> None:
    if AZURE_OPENAI_API_VERSION != "2025-04-01-preview":
        raise RuntimeError(
            "AZURE_OPENAI_API_VERSION must be 2025-04-01-preview for the Azure OpenAI realtime websocket bridge."
        )


def _normalize_azure_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    scheme = parsed.scheme or "https"
    host = parsed.netloc or parsed.path
    if not host:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT is invalid.")
    return f"{scheme}://{host}/"


def _realtime_ws_url() -> str:
    parsed = urlparse(_normalize_azure_endpoint(AZURE_OPENAI_ENDPOINT))
    return f"wss://{parsed.netloc}/openai/realtime?{urlencode({'api-version': AZURE_OPENAI_API_VERSION, 'deployment': AZURE_OPENAI_DEPLOYMENT})}"


class RealtimeCallBridge:
    def __init__(
        self,
        *,
        acs_websocket: ServerConnection,
        call_connection_id: str,
        correlation_id: str,
    ) -> None:
        self._acs_websocket = acs_websocket
        self._call_connection_id = call_connection_id
        self._correlation_id = correlation_id
        self._available_mcp_tool_names: set[str] = set()
        self._participant_raw_id: str | None = None
        self._assistant_audio_active = False
        self._outbound_audio_chunks = 0
        self._initial_greeting_requested = False
        self._pending_response_reason: str | None = None
        self._active_response_reason: str | None = None
        self._response_retry_counts: dict[str, int] = {}
        self._response_requested_after_tool_output = False

    def _should_ignore_speech_interrupt(self) -> bool:
        return (
            self._assistant_audio_active
            and self._active_response_reason == "initial greeting"
            and self._outbound_audio_chunks == 0
        )

    async def run(self) -> None:
        realtime_tools = await self._get_realtime_tools()
        async with ws_connect(
            _realtime_ws_url(),
            additional_headers={"api-key": AZURE_OPENAI_API_KEY},
            ssl=ssl.create_default_context(),
            open_timeout=20,
            ping_interval=20,
            max_size=None,
        ) as realtime_ws:
            await realtime_ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "instructions": REALTIME_INSTRUCTIONS,
                            "voice": REALTIME_VOICE,
                            "turn_detection": {
                                "type": "server_vad",
                                "create_response": False,
                                "interrupt_response": True,
                                "silence_duration_ms": 500,
                            },
                            "input_audio_format": "pcm16",
                            "output_audio_format": "pcm16",
                            "tool_choice": "auto",
                            "tools": realtime_tools,
                        },
                    }
                )
            )

            await asyncio.gather(
                self._forward_acs_audio_to_realtime(realtime_ws),
                self._forward_realtime_audio_to_acs(realtime_ws),
            )

    async def _forward_acs_audio_to_realtime(self, realtime_ws: ClientConnection) -> None:
        async for message in self._acs_websocket:
            if not isinstance(message, str):
                logging.warning("Ignoring non-text ACS frame for callConnectionId=%s", self._call_connection_id)
                continue

            packet = json.loads(message)
            kind = packet.get("kind")

            if kind == "AudioMetadata":
                metadata = packet.get("audioMetadata", {})
                logging.info(
                    "ACS audio metadata for callConnectionId=%s: encoding=%s sampleRate=%s channels=%s",
                    self._call_connection_id,
                    metadata.get("encoding"),
                    metadata.get("sampleRate"),
                    metadata.get("channels"),
                )
                continue

            if kind != "AudioData":
                logging.debug(
                    "Ignoring ACS packet kind=%s for callConnectionId=%s",
                    kind,
                    self._call_connection_id,
                )
                continue

            audio_data = packet.get("audioData", {})
            participant_raw_id = audio_data.get("participantRawID")
            if participant_raw_id and self._participant_raw_id is None:
                self._participant_raw_id = participant_raw_id
                logging.info(
                    "Using ACS participantRawID=%s for callConnectionId=%s",
                    participant_raw_id,
                    self._call_connection_id,
                )

            if (
                self._participant_raw_id
                and participant_raw_id
                and participant_raw_id != self._participant_raw_id
            ):
                continue

            chunk = audio_data.get("data")
            if chunk:
                await realtime_ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": chunk}))

    async def _forward_realtime_audio_to_acs(self, realtime_ws: ClientConnection) -> None:
        async for raw_event in realtime_ws:
            if not isinstance(raw_event, str):
                continue

            event = json.loads(raw_event)
            event_type = event.get("type", "")

            if event_type == "session.created":
                logging.info(
                    "Realtime session created for callConnectionId=%s correlationId=%s",
                    self._call_connection_id,
                    self._correlation_id,
                )
                continue

            if event_type == "session.updated":
                logging.info("Realtime session updated for callConnectionId=%s", self._call_connection_id)
                if not self._initial_greeting_requested:
                    await self._request_realtime_audio_response(realtime_ws, reason="initial greeting")
                    self._initial_greeting_requested = True
                continue

            if event_type == "input_audio_buffer.speech_started":
                logging.info("Realtime detected speech start for callConnectionId=%s", self._call_connection_id)
                if self._should_ignore_speech_interrupt():
                    logging.info(
                        "Ignoring speech interrupt before first greeting audio for callConnectionId=%s",
                        self._call_connection_id,
                    )
                    continue
                if self._assistant_audio_active:
                    await realtime_ws.send(json.dumps({"type": "response.cancel"}))
                    logging.info(
                        "Cancelled active realtime response for callConnectionId=%s due to caller speech",
                        self._call_connection_id,
                    )
                    await self._send_stop_audio_to_acs()
                continue

            if event_type == "input_audio_buffer.speech_stopped":
                logging.info("Realtime detected speech stop for callConnectionId=%s", self._call_connection_id)
                continue

            if event_type == "input_audio_buffer.committed":
                logging.info("Realtime committed caller audio for callConnectionId=%s", self._call_connection_id)
                await self._request_realtime_audio_response(realtime_ws)
                continue

            if event_type == "response.created":
                self._outbound_audio_chunks = 0
                self._active_response_reason = self._pending_response_reason
                self._pending_response_reason = None
                response_id = event.get("response", {}).get("id")
                logging.info(
                    "Realtime response created for callConnectionId=%s responseId=%s",
                    self._call_connection_id,
                    response_id,
                )
                continue

            if event_type in {"response.output_audio.delta", "response.audio.delta"}:
                self._assistant_audio_active = True
                self._outbound_audio_chunks += 1
                delta = event.get("delta", "")
                await self._send_audio_to_acs(delta)
                continue

            if event_type == "response.done":
                self._assistant_audio_active = False
                response = event.get("response", {})
                status = response.get("status")
                status_details = response.get("status_details")
                logging.info(
                    "Realtime response done for callConnectionId=%s status=%s outboundChunks=%s reason=%s statusDetails=%s",
                    self._call_connection_id,
                    status,
                    self._outbound_audio_chunks,
                    self._active_response_reason,
                    status_details,
                )
                if (
                    status == "failed"
                    and self._outbound_audio_chunks == 0
                    and self._active_response_reason
                    and self._response_retry_counts.get(self._active_response_reason, 0) < 1
                ):
                    self._response_retry_counts[self._active_response_reason] = (
                        self._response_retry_counts.get(self._active_response_reason, 0) + 1
                    )
                    logging.info(
                        "Retrying realtime response for callConnectionId=%s reason=%s",
                        self._call_connection_id,
                        self._active_response_reason,
                    )
                    await self._request_realtime_audio_response(
                        realtime_ws,
                        reason=self._active_response_reason,
                    )
                elif self._response_requested_after_tool_output:
                    self._response_requested_after_tool_output = False
                    await self._request_realtime_audio_response(realtime_ws, reason="tool output")
                self._active_response_reason = None
                continue

            if event_type == "response.function_call_arguments.done":
                await self._handle_function_call(realtime_ws, event)
                continue

            if event_type == "error":
                logging.error("Realtime error for callConnectionId=%s: %s", self._call_connection_id, event.get("error"))
                continue

            if event_type in {
                "response.output_audio_transcript.delta",
                "response.output_audio_transcript.done",
                "response.audio_transcript.delta",
                "response.audio_transcript.done",
                "response.text.delta",
                "conversation.item.input_audio_transcription.completed",
            }:
                if event_type == "conversation.item.input_audio_transcription.completed":
                    transcript = event.get("transcript")
                    logging.debug(
                        "Realtime transcript completed for callConnectionId=%s transcript=%r",
                        self._call_connection_id,
                        transcript,
                    )
                continue

            logging.debug(
                "Unhandled realtime event type=%s for callConnectionId=%s",
                event_type,
                self._call_connection_id,
            )

    async def _get_realtime_tools(self) -> list[dict[str, Any]]:
        if not _directory_lookup_is_configured():
            self._available_mcp_tool_names = set()
            return []

        try:
            mcp_tools = await asyncio.to_thread(_mcp_list_tools)
        except Exception:  # noqa: BLE001
            logging.exception(
                "Failed to list MCP tools for callConnectionId=%s",
                self._call_connection_id,
            )
            self._available_mcp_tool_names = set()
            return []

        realtime_tools: list[dict[str, Any]] = []
        available_names: set[str] = set()
        for tool in mcp_tools:
            try:
                realtime_tool = _mcp_tool_to_realtime_tool(tool)
            except Exception:  # noqa: BLE001
                logging.exception(
                    "Failed to convert MCP tool definition for callConnectionId=%s tool=%r",
                    self._call_connection_id,
                    tool,
                )
                continue
            realtime_tools.append(realtime_tool)
            available_names.add(realtime_tool["name"])

        self._available_mcp_tool_names = available_names
        logging.info(
            "Loaded %s MCP tools for callConnectionId=%s: %s",
            len(realtime_tools),
            self._call_connection_id,
            sorted(available_names),
        )
        return realtime_tools

    async def _handle_function_call(self, realtime_ws: ClientConnection, event: dict[str, Any]) -> None:
        name = event.get("name")
        call_id = event.get("call_id")
        arguments_text = event.get("arguments", "{}")

        logging.info(
            "Realtime function call for callConnectionId=%s name=%s callId=%s",
            self._call_connection_id,
            name,
            call_id,
        )

        if not call_id:
            logging.warning(
                "Ignoring function call without call_id for callConnectionId=%s name=%s",
                self._call_connection_id,
                name,
            )
            return

        if name not in self._available_mcp_tool_names:
            await self._send_function_call_output(
                realtime_ws,
                call_id=call_id,
                payload={"ok": False, "error": f"Unsupported tool: {name}"},
            )
            self._response_requested_after_tool_output = True
            return

        try:
            arguments = json.loads(arguments_text or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object.")
        except Exception as exc:  # noqa: BLE001
            await self._send_function_call_output(
                realtime_ws,
                call_id=call_id,
                payload={"ok": False, "error": f"Invalid tool arguments: {exc}"},
            )
            self._response_requested_after_tool_output = True
            return

        result = await self._call_mcp_tool(name, arguments)
        await self._send_function_call_output(realtime_ws, call_id=call_id, payload=result)
        self._response_requested_after_tool_output = True

    async def _send_function_call_output(
        self,
        realtime_ws: ClientConnection,
        *,
        call_id: str,
        payload: dict[str, Any],
    ) -> None:
        await realtime_ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(payload),
                    },
                }
            )
        )

    async def _call_mcp_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not _directory_lookup_is_configured():
            return {"ok": False, "error": "MCP tools are not configured on the bridge."}

        try:
            payload = await asyncio.to_thread(_mcp_call_tool, name, arguments)
        except Exception as exc:  # noqa: BLE001
            logging.exception(
                "MCP tool call failed for callConnectionId=%s tool=%s",
                self._call_connection_id,
                name,
            )
            return {
                "ok": False,
                "error": f"MCP tool call failed for {name}.",
                "details": str(exc),
                "tool": name,
            }

        logging.info(
            "MCP tool call complete for callConnectionId=%s tool=%s",
            self._call_connection_id,
            name,
        )
        if isinstance(payload, dict):
            return {"ok": True, **payload}
        return {"ok": True, "result": payload}

    async def _send_audio_to_acs(self, audio_b64: str) -> None:
        if not audio_b64:
            return
        payload = {
            "Kind": "AudioData",
            "AudioData": {"Data": audio_b64},
            "StopAudio": None,
        }
        await self._acs_websocket.send(json.dumps(payload))

    async def _send_stop_audio_to_acs(self) -> None:
        logging.info("Sending StopAudio to ACS for callConnectionId=%s", self._call_connection_id)
        payload = {
            "Kind": "StopAudio",
            "AudioData": None,
            "StopAudio": {},
        }
        await self._acs_websocket.send(json.dumps(payload))
        self._assistant_audio_active = False

    async def _request_realtime_audio_response(
        self,
        realtime_ws: ClientConnection,
        *,
        reason: str = "caller audio",
    ) -> None:
        self._pending_response_reason = reason
        await realtime_ws.send(
            json.dumps({"type": "response.create", "response": {"modalities": ["audio", "text"]}})
        )
        logging.info(
            "Requested realtime audio response for callConnectionId=%s reason=%s",
            self._call_connection_id,
            reason,
        )


async def _handle_acs_connection(websocket: ServerConnection) -> None:
    call_connection_id = websocket.request.headers.get("x-ms-call-connection-id", "unknown")
    correlation_id = websocket.request.headers.get("x-ms-call-correlation-id", "unknown")
    logging.info(
        "ACS media websocket connected: callConnectionId=%s correlationId=%s path=%s",
        call_connection_id,
        correlation_id,
        websocket.request.path,
    )

    handler = RealtimeCallBridge(
        acs_websocket=websocket,
        call_connection_id=call_connection_id,
        correlation_id=correlation_id,
    )
    try:
        await handler.run()
    finally:
        logging.info("ACS media websocket closed for callConnectionId=%s", call_connection_id)


def _health_response(status_code: int, reason: str, body: bytes) -> Response:
    headers = Headers()
    headers["Content-Type"] = "text/plain; charset=utf-8"
    headers["Content-Length"] = str(len(body))
    headers["Connection"] = "close"
    return Response(status_code, reason, headers, body)


async def _process_request(_connection: ServerConnection, request: Request) -> Response | None:
    upgrade = request.headers.get("Upgrade", "")
    if upgrade.lower() == "websocket":
        return None

    if request.path in {"/", "/health", "/healthz", "/ready"}:
        return _health_response(200, "OK", b"ok\n")

    return _health_response(426, "Upgrade Required", b"websocket upgrade required\n")


async def run_server() -> None:
    _validate_config()
    if _directory_lookup_is_configured():
        logging.info(
            "MCP tool discovery enabled using server %s",
            PHONE_DIRECTORY_MCP_URL,
        )
    else:
        logging.info(
            "Directory lookup tool disabled because PHONE_DIRECTORY_MCP_URL is not configured."
        )
    async with serve(
        _handle_acs_connection,
        BRIDGE_BIND_HOST,
        BRIDGE_BIND_PORT,
        max_size=None,
        ping_interval=20,
        process_request=_process_request,
    ):
        logging.info("Bridge server listening on ws://%s:%s", BRIDGE_BIND_HOST, BRIDGE_BIND_PORT)
        await asyncio.Future()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ACS to Azure OpenAI realtime websocket bridge")
    parser.add_argument("--host", default=BRIDGE_BIND_HOST, help=f"Bind host (default: {BRIDGE_BIND_HOST})")
    parser.add_argument("--port", type=int, default=BRIDGE_BIND_PORT, help=f"Bind port (default: {BRIDGE_BIND_PORT})")
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.CRITICAL)

    global BRIDGE_BIND_HOST, BRIDGE_BIND_PORT
    BRIDGE_BIND_HOST = args.host
    BRIDGE_BIND_PORT = args.port

    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logging.info("Bridge server interrupted.")
        return 130
    except Exception as exc:
        logging.exception("Bridge server failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
