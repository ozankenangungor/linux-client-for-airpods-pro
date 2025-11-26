"""Evidence-preserving diagnostics for canonically parsed HR reports."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic_ns
from typing import IO, Any, Protocol

from airpods_hr.heartrate import HeartRateReport
from airpods_hr.protocol import HEART_RATE_REPORT_SIZE


DIAGNOSTIC_SCHEMA_VERSION = 1


class HeartRateDiagnosticError(RuntimeError):
    """Base error for diagnostic capture failures."""


class DiagnosticOutputOpenError(HeartRateDiagnosticError):
    """Raised when the requested JSONL output cannot be opened safely."""


class DiagnosticWriteError(HeartRateDiagnosticError):
    """Raised when a complete diagnostic event cannot be persisted."""


class DiagnosticCloseError(HeartRateDiagnosticError):
    """Raised when final flushing or closure of a diagnostic sink fails."""


class DiagnosticStateError(HeartRateDiagnosticError):
    """Raised when recorder lifecycle methods are called out of order."""


class DiagnosticSink(Protocol):
    def write_event(self, event: Mapping[str, Any]) -> None: ...

    def close(self) -> None: ...


class JsonlDiagnosticSink:
    """Write deterministic UTF-8 JSON Lines without overwriting evidence."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._closed = False

    @classmethod
    def open(cls, path: str | Path) -> JsonlDiagnosticSink:
        try:
            stream = Path(path).open("x", encoding="utf-8", newline="\n")
        except (OSError, ValueError) as error:
            raise DiagnosticOutputOpenError(
                "diagnostic output could not be opened"
            ) from error
        return cls(stream)

    def write_event(self, event: Mapping[str, Any]) -> None:
        if self._closed:
            raise DiagnosticWriteError("diagnostic output is closed")
        try:
            line = json.dumps(
                event,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            self._stream.write(line + "\n")
            self._stream.flush()
        except (OSError, TypeError, ValueError) as error:
            raise DiagnosticWriteError(
                "diagnostic event could not be written"
            ) from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.close()
        except (OSError, ValueError) as error:
            raise DiagnosticCloseError(
                "diagnostic output could not be closed"
            ) from error


@dataclass(frozen=True, slots=True)
class DiagnosticSample:
    """Safe timing and canonical report data for one parsed sample."""

    host_monotonic_ns: int
    elapsed_ms: int
    report: HeartRateReport

    def as_event(self) -> dict[str, int | str]:
        report = self.report
        return {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "event": "heart_rate_sample",
            "host_monotonic_ns": self.host_monotonic_ns,
            "elapsed_ms": self.elapsed_ms,
            "bpm": report.bpm,
            "aux": report.aux,
            "sequence": report.sequence,
            "field_5": report.field_5,
            "timestamp_ticks": report.timestamp_ticks,
            "flags": report.flags,
            "raw_report_hex": report.raw_report.hex(),
        }

    def format_human(self) -> str:
        event = self.as_event()
        return (
            "Heart rate diagnostic: "
            f"host_monotonic_ns={event['host_monotonic_ns']} "
            f"elapsed_ms={event['elapsed_ms']} "
            f"bpm={event['bpm']} "
            f"aux={event['aux']} "
            f"sequence={event['sequence']} "
            f"field_5={event['field_5']} "
            f"timestamp_ticks={event['timestamp_ticks']} "
            f"flags={event['flags']} "
            f"raw_report_hex={event['raw_report_hex']}"
        )


class HeartRateDiagnosticRecorder:
    """Record bounded per-event metadata without retaining sample history."""

    def __init__(
        self,
        sink: DiagnosticSink | None = None,
        *,
        monotonic_clock_ns: Callable[[], int] = monotonic_ns,
        utc_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sink = sink
        self._monotonic_clock_ns = monotonic_clock_ns
        self._utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))
        self._session_reference_ns: int | None = None
        self._sample_events_emitted = 0
        self._stopped = False
        self._closed = False

    @property
    def sample_events_emitted(self) -> int:
        return self._sample_events_emitted

    @property
    def session_started(self) -> bool:
        return self._session_reference_ns is not None

    @property
    def session_stopped(self) -> bool:
        return self._stopped

    def start_session(self) -> None:
        if self._session_reference_ns is not None:
            raise DiagnosticStateError("diagnostic session is already started")
        reference_ns = self._monotonic_clock_ns()
        wall_clock = self._utc_clock().astimezone(timezone.utc)
        event: dict[str, int | str] = {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "event": "session_start",
            "host_monotonic_reference_ns": reference_ns,
            "wall_clock_utc": wall_clock.isoformat().replace("+00:00", "Z"),
        }
        self._write_event(event)
        self._session_reference_ns = reference_ns

    def record_sample(self, report: HeartRateReport) -> DiagnosticSample:
        reference_ns = self._require_active_session()
        if not isinstance(report.raw_report, bytes) or len(report.raw_report) != (
            HEART_RATE_REPORT_SIZE
        ):
            raise DiagnosticStateError(
                "parsed report does not retain the validated 18-byte payload"
            )
        observed_ns = self._monotonic_clock_ns()
        sample = DiagnosticSample(
            host_monotonic_ns=observed_ns,
            elapsed_ms=(observed_ns - reference_ns) // 1_000_000,
            report=report,
        )
        self._write_event(sample.as_event())
        self._sample_events_emitted += 1
        return sample

    def stop_session(self, termination_reason: str) -> None:
        reference_ns = self._require_active_session()
        stopped_ns = self._monotonic_clock_ns()
        event: dict[str, int | str] = {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "event": "session_stop",
            "host_monotonic_ns": stopped_ns,
            "elapsed_ms": (stopped_ns - reference_ns) // 1_000_000,
            "heart_rate_samples_emitted": self._sample_events_emitted,
            "termination_reason": termination_reason,
        }
        self._write_event(event)
        self._stopped = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._sink is not None:
            self._sink.close()

    def _require_active_session(self) -> int:
        if self._session_reference_ns is None or self._stopped:
            raise DiagnosticStateError("diagnostic session is not active")
        return self._session_reference_ns

    def _write_event(self, event: Mapping[str, Any]) -> None:
        if self._sink is not None:
            self._sink.write_event(event)


def create_diagnostic_recorder(
    output_path: str | Path | None,
) -> HeartRateDiagnosticRecorder:
    """Create a recorder, opening optional output before Bluetooth access."""

    sink = JsonlDiagnosticSink.open(output_path) if output_path is not None else None
    return HeartRateDiagnosticRecorder(sink)
