"""Bounded activation and observation of the proven AAP heart-rate stream."""

from __future__ import annotations


from collections.abc import Awaitable, Callable

from enum import StrEnum


from airpods_hr.heartrate import HeartRateReport


from airpods_hr.protocol import HEART_RATE_SERVICE_ID


MINIMUM_BOOTSTRAP_SECONDS = 1.5
DEFAULT_SAMPLE_TARGET = 5
DEFAULT_STREAM_TIMEOUT = 12.0
DEFAULT_CONTROL_ACK_TIMEOUT = 3.0
DEFAULT_STOP_ACK_TIMEOUT = 2.0
DEFAULT_CONTROL_SUMMARY_LIMIT = 8

CONNECT4_ACK = bytes.fromhex(
    "01 00 04 00 85 00 01 00 03 00 00 00 00 00 00 00 00 00"
)

_SERVICE_ACK_PREFIX = bytes.fromhex("04 00 04 00 17 00 00 00")
_SERVICE_ACK_SUFFIX_PREFIX = bytes.fromhex("10 01 4a 02 08")
_SUPPORTED_ACK_SERVICES = frozenset((0x0E, HEART_RATE_SERVICE_ID))
_OBSERVED_CONTROL_PREFIXES = (b"\x10\x01", b"\x10\x03")
_SERVICE_ACK_REMAINDER_PREFIX = bytes.fromhex("4a 02 08")
_BOOTSTRAP_REMAINDER_10 = bytes.fromhex("62 02 08 10")
_BOOTSTRAP_REMAINDER_11_12_13 = bytes.fromhex(
    "62 02 08 11 62 02 08 12 62 02 08 13"
)
_BOOTSTRAP_TAIL_10 = _OBSERVED_CONTROL_PREFIXES[0] + _BOOTSTRAP_REMAINDER_10
_BOOTSTRAP_TAIL_11_12_13 = (
    _OBSERVED_CONTROL_PREFIXES[0] + _BOOTSTRAP_REMAINDER_11_12_13
)


class HeartRateProgress(StrEnum):
    BOOTSTRAP_COMPLETE = "bootstrap_complete"
    STOP_HEAD_ACKNOWLEDGED = "stop_head_acknowledged"
    CONTROL_CHANNELS_READY = "control_channels_ready"
    START_ACKNOWLEDGED = "start_acknowledged"
    SAMPLE = "sample"
    STOP_ACKNOWLEDGED = "stop_acknowledged"
    STOP_ACK_MISSING = "stop_ack_missing"
    HR_OFF_SENT = "hr_off_sent"


Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
HeartRateProgressCallback = Callable[
    [HeartRateProgress, HeartRateReport | None], None
]


def _is_canonical_one_or_two_byte_varint(value: bytes) -> bool:
    if len(value) == 1:
        return value[0] < 0x80
    if len(value) == 2:
        return value[0] >= 0x80 and value[1] < 0x80 and value[1] != 0
    return False


def _has_observed_control_body(frame: bytes, remainder: bytes) -> bool:
    for identifier_octets in (1, 2):
        identifier_end = 13 + identifier_octets
        if not _is_canonical_one_or_two_byte_varint(frame[13:identifier_end]):
            continue
        if any(
            frame[identifier_end:] == prefix + remainder
            for prefix in _OBSERVED_CONTROL_PREFIXES
        ):
            return True
    return False


def is_observed_service_ack(frame: bytes, service_id: int) -> bool:
    """Recognize the observed ACK shape while leaving its identifier variable."""

    if service_id not in _SUPPORTED_ACK_SERVICES or not isinstance(frame, bytes):
        return False
    if len(frame) < 20 or not frame.startswith(_SERVICE_ACK_PREFIX):
        return False

    if frame[8:10] != b"\x10\x00":
        return False
    trailing_length = int.from_bytes(frame[10:12], "little")
    if trailing_length != len(frame) - 12:
        return False
    if frame[12] != 0x08:
        return False
    remainder = _SERVICE_ACK_REMAINDER_PREFIX + bytes((service_id,))
    return _has_observed_control_body(frame, remainder)


def is_connect4_ack(frame: bytes) -> bool:
    """Match the exact connect acknowledgement observed in proven runs."""

    return isinstance(frame, bytes) and frame == CONNECT4_ACK


