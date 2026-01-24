"""Bounded experimental JSONL protocol for the private local daemon."""

from __future__ import annotations

import json
from typing import Any

from airpods_hr.heartrate import HeartRateReport


PROTOCOL_VERSION = 1
MAX_FRAME_SIZE = 4096
OUTBOUND_QUEUE_SIZE = 16
HEART_RATE_STREAM = "heart_rate"
SUPPORTED_OPERATIONS = frozenset(
    {"hello", "status", "subscribe", "unsubscribe", "ping"}
)


class RequestError(ValueError):
    """A safe request error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def decode_request(frame: bytes) -> dict[str, Any]:
    """Decode and validate one size-bounded UTF-8 JSON object."""

    if len(frame) > MAX_FRAME_SIZE:
        raise RequestError("frame_too_large", "request frame exceeds limit")
    try:
        value = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RequestError(
            "invalid_json", "request must be valid UTF-8 JSON"
        ) from None
    if not isinstance(value, dict):
        raise RequestError("invalid_request", "request must be a JSON object")
    version = value.get("protocol_version")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise RequestError(
            "unsupported_version", "unsupported experimental protocol version"
        )
    operation = value.get("operation")
    if not isinstance(operation, str) or not operation:
        raise RequestError(
            "invalid_operation", "operation must be a non-empty string"
        )
    if operation not in SUPPORTED_OPERATIONS:
        raise RequestError("unknown_operation", "operation is not supported")
    if operation in {"subscribe", "unsubscribe"}:
        stream = value.get("stream")
        if stream != HEART_RATE_STREAM:
            raise RequestError("invalid_stream", "stream must be heart_rate")
    return value


def response(operation: str, **fields: Any) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "ok": True,
        "operation": operation,
        **fields,
    }


def error_response(error: RequestError | str) -> dict[str, Any]:
    if isinstance(error, RequestError):
        code = error.code
        message = error.message
    else:
        code = error
        message = "sensor service is unavailable"
    return {
        "protocol_version": PROTOCOL_VERSION,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def heart_rate_event(report: HeartRateReport) -> dict[str, Any]:
    source_side = {1: "left", 2: "right"}.get(report.field_5, "unknown")
    event: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "event": HEART_RATE_STREAM,
        "bpm": report.bpm,
        "source_side": source_side,
    }
    if source_side == "unknown":
        event["source_side_raw"] = report.field_5
    return event


def encode_message(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":"), sort_keys=True).encode() + b"\n"
