import json
import logging
import os
from typing import Any

import azure.functions as func
from azure.communication.callautomation import (
    AudioFormat,
    CallAutomationClient,
    MediaStreamingAudioChannelType,
    MediaStreamingContentType,
    MediaStreamingOptions,
    StreamingTransportType,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

ACS_CONNECTION_STRING = os.environ["ACS_CONNECTION_STRING"]
CALLBACK_URL = os.environ["CALLBACK_URL"]
ACS_MEDIA_STREAMING_URL = os.environ.get("ACS_MEDIA_STREAMING_URL", "").strip()


def _json_response(payload: dict[str, Any], status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload),
        mimetype="application/json",
        status_code=status_code,
    )


def _require_media_streaming_url() -> str:
    if not ACS_MEDIA_STREAMING_URL:
        raise RuntimeError("ACS_MEDIA_STREAMING_URL is required for bidirectional ACS media streaming.")
    if not ACS_MEDIA_STREAMING_URL.startswith(("ws://", "wss://")):
        raise RuntimeError("ACS_MEDIA_STREAMING_URL must be a ws:// or wss:// URL.")
    return ACS_MEDIA_STREAMING_URL


def _media_streaming_options() -> MediaStreamingOptions:
    return MediaStreamingOptions(
        transport_url=_require_media_streaming_url(),
        transport_type=StreamingTransportType.WEBSOCKET,
        content_type=MediaStreamingContentType.AUDIO,
        audio_channel_type=MediaStreamingAudioChannelType.UNMIXED,
        start_media_streaming=True,
        enable_bidirectional=True,
        audio_format=AudioFormat.PCM24_K_MONO,
    )


@app.route(route="incoming_call")
def incoming_call(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("incoming_call invoked")

    try:
        body = req.get_json()
    except ValueError:
        body = {}

    logging.info("Request body: %s", json.dumps(body))

    events = body if isinstance(body, list) else [body] if isinstance(body, dict) else []
    client = CallAutomationClient.from_connection_string(ACS_CONNECTION_STRING)

    for event in events:
        event_type = event.get("eventType") or event.get("type")
        logging.info("Received event_type=%s", event_type)

        if event_type == "Microsoft.EventGrid.SubscriptionValidationEvent":
            validation_code = event["data"]["validationCode"]
            return _json_response({"validationResponse": validation_code})

        if event_type == "Microsoft.Communication.IncomingCall":
            try:
                client.answer_call(
                    incoming_call_context=event["data"]["incomingCallContext"],
                    callback_url=CALLBACK_URL,
                    media_streaming=_media_streaming_options(),
                )
            except Exception as exc:
                logging.exception("Failed to answer incoming call: %s", exc)
                return _json_response({"status": "error", "message": str(exc)}, status_code=500)

            logging.info(
                "Answer request sent with ACS media streaming for transportUrl=%s",
                ACS_MEDIA_STREAMING_URL,
            )
            return _json_response({"status": "answer_requested", "mode": "external_realtime_bridge"})

        if event_type == "Microsoft.Communication.CallConnected":
            return _json_response({"status": "media_streaming_requested"})

    return _json_response({"status": "ok"})
