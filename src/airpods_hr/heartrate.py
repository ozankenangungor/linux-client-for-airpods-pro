"""Hardware-independent parsing for observed AAP heart-rate reports."""

from dataclasses import dataclass, field

from airpods_hr.protocol import (
    HEART_RATE_MARKER,
    HEART_RATE_REPORT_ID,
    HEART_RATE_REPORT_SIZE,
)


class HeartRateParseError(ValueError):
    """Base class for malformed or unsupported heart-rate packets."""


class HeartRateMarkerNotFoundError(HeartRateParseError):
    """Raised when an outer AAP packet has no heart-rate report marker."""


class HeartRateReportTruncatedError(HeartRateParseError):
    """Raised when the marker is present but the full report is not."""


class HeartRateReportIDError(HeartRateParseError):
    """Raised when the embedded report has an unexpected report ID."""


@dataclass(frozen=True, slots=True)
class HeartRateReport:
    """Decoded fields from an observed 18-byte heart-rate report.

    ``aux`` and ``field_5`` have unknown semantics. ``timestamp_ticks`` is a
    monotonically increasing 64-bit value in observed traffic; its official
    unit is unconfirmed. The meanings of individual ``flags`` bits are also
    unknown. ``raw_report`` preserves the exact validated 18-byte report when
    the model is produced by :func:`parse_heart_rate_packet`.
    """

    bpm: int
    aux: int
    sequence: int
    field_5: int
    timestamp_ticks: int
    flags: int
    raw_report: bytes = field(default=b"", repr=False)


def parse_heart_rate_packet(packet: bytes) -> HeartRateReport:
    """Find and decode one heart-rate report in an outer AAP packet.

    The report is located by its verified marker rather than an outer packet
    offset because an earlier protobuf varint can change the outer packet size.

    Raises:
        TypeError: If ``packet`` is not a bytes object.
        HeartRateMarkerNotFoundError: If the marker is absent.
        HeartRateReportTruncatedError: If fewer than 18 bytes follow the marker.
        HeartRateReportIDError: If the report ID is not the verified value.
    """

    if not isinstance(packet, bytes):
        raise TypeError("packet must be a bytes object")

    marker_offset = packet.find(HEART_RATE_MARKER)
    if marker_offset < 0:
        raise HeartRateMarkerNotFoundError("heart-rate marker not found")

    report_offset = marker_offset + len(HEART_RATE_MARKER)
    report = packet[report_offset : report_offset + HEART_RATE_REPORT_SIZE]
    if len(report) != HEART_RATE_REPORT_SIZE:
        raise HeartRateReportTruncatedError(
            f"heart-rate report is truncated: expected "
            f"{HEART_RATE_REPORT_SIZE} bytes, found {len(report)}"
        )

    if report[0] != HEART_RATE_REPORT_ID:
        raise HeartRateReportIDError(
            f"unexpected heart-rate report ID: 0x{report[0]:02x}"
        )

    return HeartRateReport(
        bpm=report[1],
        aux=report[2],
        sequence=int.from_bytes(report[3:5], "little"),
        field_5=report[5],
        timestamp_ticks=int.from_bytes(report[6:14], "little"),
        flags=int.from_bytes(report[14:18], "little"),
        raw_report=report,
    )
