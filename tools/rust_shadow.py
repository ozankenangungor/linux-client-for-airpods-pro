"""Explicit, hardware-independent Python/Rust parser comparison helper."""

from dataclasses import dataclass
from enum import StrEnum

import _airpods_aap_core as _rust_core

from airpods_hr.heartrate import (
    HeartRateMarkerNotFoundError,
    HeartRateReportIDError,
    HeartRateReportTruncatedError,
    parse_heart_rate_packet as parse_python,
)


class ShadowFailureCategory(StrEnum):
    MARKER_NOT_FOUND = "marker_not_found"
    TRUNCATED_REPORT = "truncated_report"
    INVALID_REPORT_ID = "invalid_report_id"


class ShadowParityMismatchError(RuntimeError):
    """Raised without packet contents when the two parser results differ."""


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    failure_category: ShadowFailureCategory | None

    @property
    def succeeded(self) -> bool:
        return self.failure_category is None


_FIELDS = (
    "bpm",
    "aux",
    "sequence",
    "field_5",
    "timestamp_ticks",
    "flags",
    "raw_report",
)

_PYTHON_FAILURES = {
    HeartRateMarkerNotFoundError: ShadowFailureCategory.MARKER_NOT_FOUND,
    HeartRateReportTruncatedError: ShadowFailureCategory.TRUNCATED_REPORT,
    HeartRateReportIDError: ShadowFailureCategory.INVALID_REPORT_ID,
}

_RUST_FAILURES = {
    _rust_core.MarkerNotFoundError: ShadowFailureCategory.MARKER_NOT_FOUND,
    _rust_core.TruncatedReportError: ShadowFailureCategory.TRUNCATED_REPORT,
    _rust_core.InvalidReportIdError: ShadowFailureCategory.INVALID_REPORT_ID,
}


def compare_heart_rate_packet(packet: bytes) -> ShadowComparison:
    """Run both parsers and compare without logging packet or report bytes."""

    if not isinstance(packet, bytes):
        raise TypeError("packet must be a bytes object")

    try:
        python_result = parse_python(packet)
        python_failure = None
    except tuple(_PYTHON_FAILURES) as error:
        python_result = None
        python_failure = _PYTHON_FAILURES[type(error)]

    try:
        rust_result = _rust_core.parse_heart_rate_packet(packet)
        rust_failure = None
    except tuple(_RUST_FAILURES) as error:
        rust_result = None
        rust_failure = _RUST_FAILURES[type(error)]

    if python_failure != rust_failure:
        raise ShadowParityMismatchError("Python/Rust parser failure categories differ")
    if python_failure is not None:
        return ShadowComparison(failure_category=python_failure)

    if python_result is None or rust_result is None:
        raise ShadowParityMismatchError("Python/Rust parser outcomes differ")
    python_fields = tuple(getattr(python_result, field) for field in _FIELDS)
    rust_fields = tuple(getattr(rust_result, field) for field in _FIELDS)
    if python_fields != rust_fields:
        raise ShadowParityMismatchError("Python/Rust parser fields differ")

    expected_side = {
        1: ("left", None),
        2: ("right", None),
    }.get(python_result.field_5, ("unknown", python_result.field_5))
    if rust_result.source_side() != expected_side:
        raise ShadowParityMismatchError("Python/Rust source-side derivation differs")

    return ShadowComparison(failure_category=None)
