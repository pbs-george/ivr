import argparse
import asyncio
import base64
import json
import os
import ssl
from pathlib import Path
from urllib.parse import urlencode, urlparse

from websockets.asyncio.client import connect as ws_connect


INPUT_SAMPLE_RATE = 24000
OUTPUT_SAMPLE_RATE = 24000
DEFAULT_RECORD_SECONDS = 5.0
DEFAULT_VOICE = "cedar"
DEFAULT_INSTRUCTIONS = (
    "You are a friendly voice assistant. Reply briefly, naturally, and in a conversational tone."
)


def load_local_settings() -> None:
    settings_path = Path(__file__).with_name("local.settings.json")
    if not settings_path.exists():
        return

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        values = data.get("Values", {})
        for key, value in values.items():
            if isinstance(value, str) and value and not os.getenv(key):
                os.environ[key] = value
    except Exception as exc:
        print(f"WARN: Failed to load local.settings.json: {exc}")


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(f"ERROR: Missing required environment variable: {name}")
        raise SystemExit(2)
    return value


def get_deployment_name(cli_value: str | None) -> str:
    candidates = [
        cli_value,
        os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip(),
        os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "").strip(),
    ]
    for value in candidates:
        if value:
            return value

    print("ERROR: Missing deployment name.")
    print("Set AZURE_OPENAI_DEPLOYMENT or pass --deployment.")
    raise SystemExit(2)


def normalize_azure_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    scheme = parsed.scheme or "https"
    host = parsed.netloc or parsed.path
    if not host:
        return endpoint
    return f"{scheme}://{host}/"


def describe_endpoint(endpoint: str) -> None:
    host = (urlparse(endpoint).hostname or "").lower()
    if host.endswith(".cognitiveservices.azure.com"):
        print("Endpoint format looks correct for Azure OpenAI.")
        return
    if host.endswith(".openai.azure.com"):
        print("Endpoint format looks correct for Azure OpenAI.")
        return
    print("WARN: Endpoint host is not a standard Azure OpenAI endpoint.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Azure OpenAI realtime mic/speaker chat")
    parser.add_argument(
        "--deployment",
        help="Azure OpenAI deployment name. Defaults to AZURE_OPENAI_DEPLOYMENT.",
    )
    parser.add_argument(
        "--record-seconds",
        type=float,
        default=DEFAULT_RECORD_SECONDS,
        help=f"Seconds to record per turn (default: {DEFAULT_RECORD_SECONDS})",
    )
    parser.add_argument(
        "--voice",
        default=DEFAULT_VOICE,
        help=f"Realtime voice to use (default: {DEFAULT_VOICE})",
    )
    parser.add_argument(
        "--instructions",
        default=DEFAULT_INSTRUCTIONS,
        help="System instructions for the assistant.",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List detected audio devices and exit.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Connect to the realtime deployment and exit without recording audio.",
    )
    parser.add_argument(
        "--input-device",
        type=int,
        default=None,
        help="Input device index from --list-devices output.",
    )
    parser.add_argument(
        "--output-device",
        type=int,
        default=None,
        help="Output device index from --list-devices output.",
    )
    return parser.parse_args()


def _require_audio_modules():
    try:
        import numpy as np  # type: ignore
    except Exception as exc:
        print("ERROR: Failed to import numpy.")
        print(f"Details: {exc}")
        print("Install with: python -m pip install numpy")
        raise SystemExit(2)

    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:
        print("ERROR: Failed to import sounddevice.")
        print(f"Details: {exc}")
        print("Install with: python -m pip install sounddevice")
        raise SystemExit(2)

    return np, sd


def _list_audio_devices(sd) -> list[dict]:
    devices = list(sd.query_devices())
    print("Detected audio devices:")
    for idx, dev in enumerate(devices):
        in_ch = int(dev.get("max_input_channels", 0))
        out_ch = int(dev.get("max_output_channels", 0))
        print(f"- [{idx}] in={in_ch} out={out_ch} name={dev.get('name', 'unknown')}")
    if not devices:
        print("No audio devices were detected.")
    return devices


def _configure_audio_devices(sd, input_device: int | None, output_device: int | None) -> tuple[int, int]:
    devices = list(sd.query_devices())
    if not devices:
        raise RuntimeError("No audio devices detected.")

    input_index = input_device
    output_index = output_device

    if input_index is None:
        input_index = next((i for i, dev in enumerate(devices) if int(dev.get("max_input_channels", 0)) > 0), None)
    if output_index is None:
        output_index = next((i for i, dev in enumerate(devices) if int(dev.get("max_output_channels", 0)) > 0), None)

    if input_index is None:
        raise RuntimeError("No microphone-capable audio device found.")
    if output_index is None:
        raise RuntimeError("No speaker-capable audio device found.")

    sd.default.device = (input_index, output_index)
    print(f"Using input device [{input_index}] {devices[input_index].get('name', 'unknown')}")
    print(f"Using output device [{output_index}] {devices[output_index].get('name', 'unknown')}")
    return input_index, output_index


def _record_pcm16(np, sd, seconds: float) -> bytes:
    frames = max(1, int(seconds * INPUT_SAMPLE_RATE))
    print(f"Recording for {seconds:.1f}s. Speak now...")
    recording = sd.rec(frames, samplerate=INPUT_SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    print("Recording complete.")
    return np.asarray(recording, dtype=np.int16).tobytes()


def _play_pcm16(np, sd, pcm_bytes: bytes) -> None:
    if not pcm_bytes:
        print("WARN: The model returned no audio.")
        return

    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    if samples.size == 0:
        print("WARN: No playable PCM samples were returned.")
        return

    print("Playing assistant audio...")
    sd.play(samples, samplerate=OUTPUT_SAMPLE_RATE)
    sd.wait()


def _format_realtime_error(event) -> str:
    error = event.get("error")
    if error is None:
        return "Unknown realtime error"

    parts = [error.get("message", "Unknown realtime error")]
    code = error.get("code")
    if code:
        parts.append(f"code={code}")
    param = error.get("param")
    if param:
        parts.append(f"param={param}")
    return " | ".join(parts)


async def _wait_for_session_updated(websocket) -> None:
    while True:
        event = json.loads(await websocket.recv())
        event_type = event.get("type", "")

        if event_type == "session.updated":
            return
        if event_type == "error":
            raise RuntimeError(_format_realtime_error(event))


async def _collect_response(websocket) -> tuple[bytes, str]:
    audio_buffer = bytearray()
    transcript_parts: list[str] = []

    while True:
        event = json.loads(await websocket.recv())
        event_type = event.get("type", "")

        if event_type == "response.audio.delta":
            delta = event.get("delta", "")
            if delta:
                audio_buffer.extend(base64.b64decode(delta))
            continue

        if event_type in {"response.audio_transcript.delta", "response.text.delta"}:
            delta = event.get("delta", "")
            if delta:
                transcript_parts.append(delta)
            continue

        if event_type == "response.done":
            response = event.get("response", {})
            status = response.get("status")
            if status and status != "completed":
                raise RuntimeError(f"Response finished with status: {status}")
            return bytes(audio_buffer), "".join(transcript_parts).strip()

        if event_type == "error":
            raise RuntimeError(_format_realtime_error(event))


async def run_voice_chat(
    endpoint: str,
    api_key: str,
    deployment: str,
    api_version: str,
    record_seconds: float,
    voice: str,
    instructions: str,
    check_only: bool,
    list_devices_only: bool,
    input_device: int | None,
    output_device: int | None,
) -> int:
    if list_devices_only:
        _, sd = _require_audio_modules()
        _list_audio_devices(sd)
        return 0

    np = sd = None
    if not check_only:
        np, sd = _require_audio_modules()
        try:
            _configure_audio_devices(sd, input_device=input_device, output_device=output_device)
        except Exception as exc:
            print(f"ERROR: {exc}")
            print("Run with --list-devices to inspect available devices.")
            return 2

    parsed = urlparse(endpoint)
    ws_url = f"wss://{parsed.netloc}/openai/realtime?{urlencode({'api-version': api_version, 'deployment': deployment})}"

    async with ws_connect(
        ws_url,
        additional_headers={"api-key": api_key},
        ssl=ssl.create_default_context(),
        open_timeout=20,
    ) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "instructions": instructions,
                        "voice": voice,
                        "turn_detection": {"type": "none"},
                        "input_audio_format": "pcm16",
                        "output_audio_format": "pcm16",
                    },
                }
            )
        )
        await _wait_for_session_updated(websocket)

        print("Connected to Azure OpenAI Realtime.")
        print(f"Deployment: {deployment}")
        print(f"Voice: {voice}")

        if check_only:
            print("Realtime session is ready.")
            return 0

        print("Press Enter to record a turn, or type q then Enter to quit.")

        while True:
            user_command = await asyncio.to_thread(input, "\nEnter=record, q=quit > ")
            if user_command.strip().lower() in {"q", "quit", "exit"}:
                print("Closing voice chat.")
                return 0

            pcm_bytes = await asyncio.to_thread(_record_pcm16, np, sd, record_seconds)
            await websocket.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm_bytes).decode("ascii"),
                    }
                )
            )
            await websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await websocket.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {"modalities": ["audio", "text"]},
                    }
                )
            )

            print("Waiting for assistant response...")
            response_audio, response_transcript = await _collect_response(websocket)
            if response_transcript:
                print("Assistant:")
                print(response_transcript)

            await asyncio.to_thread(_play_pcm16, np, sd, response_audio)


def main() -> int:
    args = parse_args()
    load_local_settings()

    endpoint_raw = require_env("AZURE_OPENAI_ENDPOINT")
    api_key = require_env("AZURE_OPENAI_API_KEY")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-01-preview").strip()
    deployment = get_deployment_name(args.deployment)
    endpoint = normalize_azure_endpoint(endpoint_raw)

    print("Azure OpenAI realtime voice chat")
    print(f"Endpoint: {endpoint}")
    print(f"Deployment: {deployment}")
    print(f"API version: {api_version}")
    if endpoint != endpoint_raw:
        print(f"INFO: Normalized endpoint from: {endpoint_raw}")
    describe_endpoint(endpoint)

    try:
        return asyncio.run(
            run_voice_chat(
                endpoint=endpoint,
                api_key=api_key,
                deployment=deployment,
                api_version=api_version,
                record_seconds=args.record_seconds,
                voice=args.voice,
                instructions=args.instructions,
                check_only=args.check_only,
                list_devices_only=args.list_devices,
                input_device=args.input_device,
                output_device=args.output_device,
            )
        )
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
