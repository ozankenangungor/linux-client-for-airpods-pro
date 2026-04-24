"""Public Python models and errors for the authoritative Rust heart-rate parser."""

from dataclasses import dataclass, field

import airpods_hr._airpods_aap_core as _rust_core


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

    try:
        report = _rust_core.parse_heart_rate_packet(packet)
    except _rust_core.MarkerNotFoundError as error:
        raise HeartRateMarkerNotFoundError(str(error)) from None
    except _rust_core.TruncatedReportError as error:
        raise HeartRateReportTruncatedError(str(error)) from None
    except _rust_core.InvalidReportIdError as error:
        raise HeartRateReportIDError(str(error)) from None

    return HeartRateReport(
        bpm=report.bpm,
        aux=report.aux,
        sequence=report.sequence,
        field_5=report.field_5,
        timestamp_ticks=report.timestamp_ticks,
        flags=report.flags,
        raw_report=report.raw_report,
    )
