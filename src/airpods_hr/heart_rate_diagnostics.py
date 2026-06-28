"""Evidence-preserving diagnostics for canonically parsed HR reports."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic_ns
from typing import IO, Any, Protocol

from airpods_hr import _airpods_aap_core as _native
from airpods_hr.heartrate import HeartRateReport


DIAGNOSTIC_SCHEMA_VERSION = _native.diagnostic_schema_version()


def _report_fields(report: HeartRateReport) -> list[int]:
    return [report.bpm, report.aux, report.sequence, report.field_5,
            report.timestamp_ticks, report.flags]


def _state_error(error: ValueError) -> DiagnosticStateError:
    return DiagnosticStateError(str(error))


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
        return _native.diagnostic_sample_event_values(
            self.host_monotonic_ns, self.elapsed_ms,
            _report_fields(self.report), self.report.raw_report,
        )

    def format_human(self) -> str:
        return _native.diagnostic_sample_human_values(
            self.host_monotonic_ns, self.elapsed_ms,
            _report_fields(self.report), self.report.raw_report,
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
        self._native_state = _native._DiagnosticRecorderState()
        self._closed = False

    @property
    def sample_events_emitted(self) -> int:
        return self._native_state.sample_events_emitted

    @property
    def session_started(self) -> bool:
        return self._native_state.session_started

    @property
    def session_stopped(self) -> bool:
        return self._native_state.session_stopped

    def start_session(self) -> None:
        try:
            self._native_state.check_start()
        except ValueError as error:
            raise _state_error(error) from error
        reference_ns = self._monotonic_clock_ns()
        wall_clock = self._utc_clock().astimezone(timezone.utc)
        try:
            token, event = self._native_state.plan_start(
                reference_ns, wall_clock.isoformat().replace("+00:00", "Z")
            )
        except ValueError as error:
            raise _state_error(error) from error
        self._write_event(event)
        self._native_state.commit_start(token)

    def record_sample(self, report: HeartRateReport) -> DiagnosticSample:
        self._require_active_session()
        try:
            _native.diagnostic_validate_raw(report.raw_report)
        except ValueError as error:
            raise _state_error(error) from error
        observed_ns = self._monotonic_clock_ns()
        try:
            token, event = self._native_state.plan_sample(
                observed_ns, _report_fields(report), report.raw_report
            )
        except ValueError as error:
            raise _state_error(error) from error
        self._write_event(event)
        self._native_state.commit_sample(token)
        sample = DiagnosticSample(observed_ns, event["elapsed_ms"], report)
        return sample

    def stop_session(self, termination_reason: str) -> None:
        self._require_active_session()
        stopped_ns = self._monotonic_clock_ns()
        try:
            token, event = self._native_state.plan_stop(
                stopped_ns, termination_reason
            )
        except ValueError as error:
            raise _state_error(error) from error
        self._write_event(event)
        self._native_state.commit_stop(token)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._sink is not None:
            self._sink.close()

    def _require_active_session(self) -> int:
        try:
            return self._native_state.check_active()
        except ValueError as error:
            raise _state_error(error) from error

    def _write_event(self, event: Mapping[str, Any]) -> None:
        if self._sink is not None:
            self._sink.write_event(event)


def create_diagnostic_recorder(
    output_path: str | Path | None,
) -> HeartRateDiagnosticRecorder:
    """Create a recorder, opening optional output before Bluetooth access."""

    sink = JsonlDiagnosticSink.open(output_path) if output_path is not None else None
    return HeartRateDiagnosticRecorder(sink)
