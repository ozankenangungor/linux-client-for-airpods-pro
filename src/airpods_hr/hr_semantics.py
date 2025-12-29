"""Private telemetry evidence capture over the proven BlueZ coexistence path."""

from __future__ import annotations


from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from statistics import median
from time import monotonic_ns
from typing import Any


from airpods_hr.heart_rate_diagnostics import DiagnosticSink
from airpods_hr.heart_rate_session import HeartRateMonitorActivationSession, HeartRateProgress


from airpods_hr.heartrate import HeartRateReport, parse_heart_rate_packet
from airpods_hr.protocol import HEART_RATE_MARKER, HEART_RATE_REPORT_ID, HEART_RATE_REPORT_SIZE


SEMANTICS_SCHEMA_VERSION = 1
DEFAULT_BASELINE_SAMPLES = 30
DEFAULT_SAMPLES_PER_CYCLE = 10
DEFAULT_RESTART_DELAY = 5.0
DEFAULT_CYCLE_TIMEOUT = 60.0


class HRSemanticsScenario(StrEnum):
    BASELINE = "baseline"
    ACTIVATION_RESTART = "activation-restart"


@dataclass(frozen=True, slots=True)
class ParserStructure:
    canonical_report_length: int
    sequence_width_bits: int
    timestamp_width_bits: int
    flags_width_bits: int


def _derive_parser_structure() -> ParserStructure:
    """Derive field widths by exercising the frozen canonical parser."""

    field_octets = {"sequence": 0, "timestamp_ticks": 0, "flags": 0}
    for offset in range(1, HEART_RATE_REPORT_SIZE):
        raw = bytearray(HEART_RATE_REPORT_SIZE)
        raw[0] = HEART_RATE_REPORT_ID
        raw[offset] = 0xFF
        report = parse_heart_rate_packet(HEART_RATE_MARKER + bytes(raw))
        for field_name in field_octets:
            if getattr(report, field_name) != 0:
                field_octets[field_name] += 1
    return ParserStructure(
        canonical_report_length=HEART_RATE_REPORT_SIZE,
        sequence_width_bits=field_octets["sequence"] * 8,
        timestamp_width_bits=field_octets["timestamp_ticks"] * 8,
        flags_width_bits=field_octets["flags"] * 8,
    )


PARSER_STRUCTURE = _derive_parser_structure()


@dataclass(frozen=True, slots=True)
class HRSemanticsSampleRecord:
    """One canonical report plus mechanically derived timing and deltas."""

    schema_version: int
    scenario: str
    cycle_index: int
    sample_index_within_cycle: int
    sample_index_global: int
    receive_monotonic_ns: int
    delta_from_previous_report_ms: float | None
    milliseconds_since_activation_ack: float
    bpm: int
    aux: int
    sequence: int
    field_5: int
    timestamp_ticks: int
    flags_decimal: int
    flags_hex: str
    flags_bits_set: tuple[int, ...]
    sequence_delta_modulo: int | None
    timestamp_delta_modulo: int | None
    duplicate_parsed_report: bool
    raw_report_bytes: int
    raw_report_hex: str

    def as_event(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "record_type": "sample",
            "scenario": self.scenario,
            "cycle_index": self.cycle_index,
            "sample_index_within_cycle": self.sample_index_within_cycle,
            "sample_index_global": self.sample_index_global,
            "receive_monotonic_ns": self.receive_monotonic_ns,
            "delta_from_previous_report_ms": (
                self.delta_from_previous_report_ms
            ),
            "milliseconds_since_activation_ack": (
                self.milliseconds_since_activation_ack
            ),
            "bpm": self.bpm,
            "aux": self.aux,
            "sequence": self.sequence,
            "field_5": self.field_5,
            "timestamp_ticks": self.timestamp_ticks,
            "flags_decimal": self.flags_decimal,
            "flags_hex": self.flags_hex,
            "flags_bits_set": list(self.flags_bits_set),
            "sequence_delta_modulo": self.sequence_delta_modulo,
            "timestamp_delta_modulo": self.timestamp_delta_modulo,
            "duplicate_parsed_report": self.duplicate_parsed_report,
            "raw_report_bytes": self.raw_report_bytes,
            "raw_report_hex": self.raw_report_hex,
        }


@dataclass(slots=True)
class _CycleCapture:
    cycle_index: int
    attempted: bool = False
    activation_ack_ns: int | None = None
    complete: bool = False


class HRSemanticsRecorder:
    """Flush one independently valid JSON object for every capture record."""

    def __init__(
        self,
        sink: DiagnosticSink,
        *,
        scenario: HRSemanticsScenario,
        requested_samples: int | None,
        requested_samples_per_cycle: int | None,
        restart_delay_seconds: float,
        monotonic_clock_ns: Callable[[], int] = monotonic_ns,
    ) -> None:
        self._sink = sink
        self._scenario = scenario
        self._requested_samples = requested_samples
        self._requested_samples_per_cycle = requested_samples_per_cycle
        self._restart_delay_seconds = restart_delay_seconds
        self._clock_ns = monotonic_clock_ns
        cycle_count = 1 if scenario is HRSemanticsScenario.BASELINE else 2
        self._cycles = {
            index: _CycleCapture(index) for index in range(1, cycle_count + 1)
        }
        self._records: list[HRSemanticsSampleRecord] = []
        self._header_written = False
        self._summary_written = False
        self._active_cycle: int | None = None

    @property
    def records(self) -> tuple[HRSemanticsSampleRecord, ...]:
        return tuple(self._records)

    @property
    def header_written(self) -> bool:
        return self._header_written

    def write_header(
        self, *, descriptor_complete: bool, local_rx_imtu: int | None
    ) -> None:
        if self._header_written:
            raise RuntimeError("semantics capture header is already written")
        self._sink.write_event(
            {
                "schema_version": SEMANTICS_SCHEMA_VERSION,
                "record_type": "capture_header",
                "scenario": self._scenario.value,
                "requested_samples": self._requested_samples,
                "requested_samples_per_cycle": (
                    self._requested_samples_per_cycle
                ),
                "restart_delay_seconds": self._restart_delay_seconds,
                "descriptor_complete": descriptor_complete,
                "transport": "bluez-kernel-coexistence",
                "local_rx_imtu": local_rx_imtu,
                "canonical_report_length": (
                    PARSER_STRUCTURE.canonical_report_length
                ),
                "sequence_width_bits": PARSER_STRUCTURE.sequence_width_bits,
                "timestamp_width_bits": (
                    PARSER_STRUCTURE.timestamp_width_bits
                ),
                "flags_width_bits": PARSER_STRUCTURE.flags_width_bits,
                "raw_report_hex_included": True,
            }
        )
        self._header_written = True

    def mark_cycle_attempted(self, cycle_index: int) -> None:
        cycle = self._cycle(cycle_index)
        if cycle.attempted:
            raise RuntimeError("HR semantics cycle is single-use")
        cycle.attempted = True

    def begin_cycle(self, cycle_index: int) -> None:
        if not self._header_written:
            raise RuntimeError("semantics capture header is not written")
        cycle = self._cycle(cycle_index)
        if not cycle.attempted or cycle.activation_ack_ns is not None:
            raise RuntimeError("unexpected HR activation acknowledgement")
        if self._active_cycle is not None:
            raise RuntimeError("another HR semantics cycle is active")
        cycle.activation_ack_ns = self._clock_ns()
        self._active_cycle = cycle_index

    def record_sample(self, report: HeartRateReport) -> HRSemanticsSampleRecord:
        if self._active_cycle is None:
            raise RuntimeError("HR sample arrived before activation acknowledgement")
        if (
            not isinstance(report.raw_report, bytes)
            or len(report.raw_report) != HEART_RATE_REPORT_SIZE
        ):
            raise RuntimeError("canonical report does not retain 18 raw bytes")
        cycle = self._cycle(self._active_cycle)
        assert cycle.activation_ack_ns is not None
        received_ns = self._clock_ns()
        previous = self._records[-1] if self._records else None
        cycle_sample_count = sum(
            record.cycle_index == cycle.cycle_index for record in self._records
        )
        sequence_modulus = 1 << PARSER_STRUCTURE.sequence_width_bits
        timestamp_modulus = 1 << PARSER_STRUCTURE.timestamp_width_bits
        flags_width = PARSER_STRUCTURE.flags_width_bits // 4
        record = HRSemanticsSampleRecord(
            schema_version=SEMANTICS_SCHEMA_VERSION,
            scenario=self._scenario.value,
            cycle_index=cycle.cycle_index,
            sample_index_within_cycle=cycle_sample_count + 1,
            sample_index_global=len(self._records) + 1,
            receive_monotonic_ns=received_ns,
            delta_from_previous_report_ms=(
                (received_ns - previous.receive_monotonic_ns) / 1_000_000
                if previous is not None
                else None
            ),
            milliseconds_since_activation_ack=(
                received_ns - cycle.activation_ack_ns
            )
            / 1_000_000,
            bpm=report.bpm,
            aux=report.aux,
            sequence=report.sequence,
            field_5=report.field_5,
            timestamp_ticks=report.timestamp_ticks,
            flags_decimal=report.flags,
            flags_hex=f"0x{report.flags:0{flags_width}x}",
            flags_bits_set=tuple(
                bit
                for bit in range(PARSER_STRUCTURE.flags_width_bits)
                if report.flags & (1 << bit)
            ),
            sequence_delta_modulo=(
                (report.sequence - previous.sequence) % sequence_modulus
                if previous is not None
                else None
            ),
            timestamp_delta_modulo=(
                (report.timestamp_ticks - previous.timestamp_ticks)
                % timestamp_modulus
                if previous is not None
                else None
            ),
            duplicate_parsed_report=(
                self._same_parsed_report(report, previous)
                if previous is not None
                else False
            ),
            raw_report_bytes=len(report.raw_report),
            raw_report_hex=report.raw_report.hex(),
        )
        self._sink.write_event(record.as_event())
        self._records.append(record)
        return record

    def complete_cycle(self, cycle_index: int) -> None:
        cycle = self._cycle(cycle_index)
        if self._active_cycle != cycle_index:
            raise RuntimeError("completed HR semantics cycle is not active")
        cycle.complete = True
        self._active_cycle = None

    def abandon_cycle(self, cycle_index: int) -> None:
        if self._active_cycle == cycle_index:
            self._active_cycle = None

    def write_summary(
        self, *, status: str, failure_category: str | None
    ) -> Mapping[str, Any]:
        if not self._header_written:
            raise RuntimeError("semantics capture header is not written")
        if self._summary_written:
            raise RuntimeError("semantics capture summary is already written")
        cycle_records = {
            index: [r for r in self._records if r.cycle_index == index]
            for index in self._cycles
        }
        intervals = [
            record.delta_from_previous_report_ms
            for record in self._records
            if record.delta_from_previous_report_ms is not None
        ]
        summary: dict[str, Any] = {
            "schema_version": SEMANTICS_SCHEMA_VERSION,
            "record_type": "capture_summary",
            "scenario": self._scenario.value,
            "status": status,
            "failure_category": failure_category,
            "canonical_reports_received": len(self._records),
            "cycles_completed": sum(
                cycle.complete for cycle in self._cycles.values()
            ),
            "first_bpm_per_cycle": [
                records[0].bpm if records else None
                for records in cycle_records.values()
            ],
            "last_bpm_per_cycle": [
                records[-1].bpm if records else None
                for records in cycle_records.values()
            ],
            "unique_bpm_values": sorted({r.bpm for r in self._records}),
            "cycle_summaries": [
                self._cycle_summary(index, records)
                for index, records in cycle_records.items()
            ],
            "sequence_reset_observed_between_cycles": (
                self._reset_observation(cycle_records, "sequence")
            ),
            "timestamp_reset_observed_between_cycles": (
                self._reset_observation(cycle_records, "timestamp_ticks")
            ),
            "flags_bit_positions_observed": sorted(
                {bit for r in self._records for bit in r.flags_bits_set}
            ),
            "receive_interval_ms": {
                "count": len(intervals),
                "min": min(intervals) if intervals else None,
                "median": median(intervals) if intervals else None,
                "max": max(intervals) if intervals else None,
            },
        }
        self._sink.write_event(summary)
        self._summary_written = True
        return summary

    def close(self) -> None:
        self._sink.close()

    def _cycle(self, cycle_index: int) -> _CycleCapture:
        try:
            return self._cycles[cycle_index]
        except KeyError as error:
            raise ValueError("cycle index is outside this scenario") from error

    def _cycle_summary(
        self, cycle_index: int, records: list[HRSemanticsSampleRecord]
    ) -> dict[str, Any]:
        cycle = self._cycles[cycle_index]
        return {
            "cycle_index": cycle_index,
            "attempted": cycle.attempted,
            "activation_ack_observed": cycle.activation_ack_ns is not None,
            "complete": cycle.complete,
            "reports_received": len(records),
            "sequence_first": records[0].sequence if records else None,
            "sequence_last": records[-1].sequence if records else None,
            "timestamp_first": records[0].timestamp_ticks if records else None,
            "timestamp_last": records[-1].timestamp_ticks if records else None,
            "field_5_unique_values": sorted({r.field_5 for r in records}),
            "flags_unique_hex_values": sorted(
                {r.flags_hex for r in records}
            ),
        }

    def _reset_observation(
        self,
        cycle_records: Mapping[int, list[HRSemanticsSampleRecord]],
        field_name: str,
    ) -> str:
        if self._scenario is not HRSemanticsScenario.ACTIVATION_RESTART:
            return "unknown"
        first_cycle = cycle_records[1]
        second_cycle = cycle_records[2]
        if not first_cycle or not second_cycle:
            return "unknown"
        previous = getattr(first_cycle[-1], field_name)
        current = getattr(second_cycle[0], field_name)
        if current == 0 and previous != 0:
            return "yes"
        if current >= previous:
            return "no"
        return "unknown"

    @staticmethod
    def _same_parsed_report(
        report: HeartRateReport, previous: HRSemanticsSampleRecord
    ) -> bool:
        return (
            report.bpm == previous.bpm
            and report.aux == previous.aux
            and report.sequence == previous.sequence
            and report.field_5 == previous.field_5
            and report.timestamp_ticks == previous.timestamp_ticks
            and report.flags == previous.flags_decimal
            and report.raw_report.hex() == previous.raw_report_hex
        )


MonitorFactory = Callable[
    [Callable[[HeartRateProgress, HeartRateReport | None], None]],
    HeartRateMonitorActivationSession,
]


