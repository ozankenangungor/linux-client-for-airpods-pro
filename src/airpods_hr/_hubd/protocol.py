"""Bounded experimental JSONL protocol for the private local daemon."""

from __future__ import annotations

import json
from typing import Any

from airpods_hr import _airpods_aap_core as _native
from airpods_hr.heartrate import HeartRateReport


(
    PROTOCOL_VERSION,
    MAX_FRAME_SIZE,
    OUTBOUND_QUEUE_SIZE,
    HEART_RATE_STREAM,
    _operations,
    _states,
) = _native.hub_constants()
SUPPORTED_OPERATIONS = frozenset(_operations)


class RequestError(ValueError):
    """A safe request error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def decode_request(frame: bytes) -> dict[str, Any]:
    """Decode and validate one size-bounded UTF-8 JSON object."""

    try:
        return _native.hub_decode_request(frame)
    except _native.HubRequestError as error:
        raise RequestError(error.code, error.message) from None


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
    return _native.hub_heart_rate_event(report.bpm, report.field_5)


def encode_message(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":"), sort_keys=True).encode() + b"\n"
