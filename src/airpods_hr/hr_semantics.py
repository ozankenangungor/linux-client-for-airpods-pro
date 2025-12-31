"""Private telemetry evidence capture over the proven BlueZ coexistence path."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from statistics import median
from time import monotonic_ns
from typing import Any

from airpods_hr.aap import AAPHandshakeResult, AAPHandshakeSession
from airpods_hr.bluez_coexistence import (
    BlueZCoexistenceState,
    BlueZStateClient,
    CoexistenceFailure,
    CoexistenceTransport,
    CompatibilityRegistration,
)
from airpods_hr.heart_rate_diagnostics import DiagnosticSink
from airpods_hr.heart_rate_session import (
    HeartRateMonitorActivationSession,
    HeartRateMonitorSessionResult,
    HeartRateProgress,
)
from airpods_hr.heartrate import HeartRateReport, parse_heart_rate_packet
from airpods_hr.protocol import (
    HEART_RATE_MARKER,
    HEART_RATE_REPORT_ID,
    HEART_RATE_REPORT_SIZE,
    HeartRateCommand,
)


SEMANTICS_SCHEMA_VERSION = 1
DEFAULT_BASELINE_SAMPLES = 30
DEFAULT_SAMPLES_PER_CYCLE = 10
DEFAULT_RESTART_DELAY = 5.0
DEFAULT_CYCLE_TIMEOUT = 60.0


class HRSemanticsScenario(StrEnum):
    BASELINE = "baseline"
    ACTIVATION_RESTART = "activation-restart"


class HRSemanticsCategory(StrEnum):
    PREFLIGHT_FAILED = "preflight_failed"
    BLUEZ_CONNECTION_LOST = "bluez_connection_lost"
    HANDSHAKE_FAILED = "handshake_failed"
    CYCLE_FAILED = "cycle_failed"
    CYCLE_TIMEOUT = "cycle_timeout"
    STOP_ACK_MISSING = "stop_ack_missing"
    CLEANUP_FAILED = "cleanup_failed"


class HRSemanticsFailure(RuntimeError):
    """Safe phase failure without private device material."""

    def __init__(self, category: HRSemanticsCategory, phase: str) -> None:
        self.category = category
        self.phase = phase
        super().__init__(f"{category.value} at {phase}")


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


class _CycleRelativeTransport:
    """Present one canonical activation's payload count on a shared channel."""

    def __init__(self, transport: CoexistenceTransport) -> None:
        self._transport = transport
        self._starting_payload_count = transport.application_payloads_sent

    @property
    def application_payloads_sent(self) -> int:
        return 1 + (
            self._transport.application_payloads_sent
            - self._starting_payload_count
        )

    @property
    def pending_receive_frames(self) -> int:
        return self._transport.pending_receive_frames

    def send_heart_rate_command(self, command: HeartRateCommand) -> None:
        self._transport.send_heart_rate_command(command)

    async def receive(self, timeout: float) -> bytes:
        return await self._transport.receive(timeout)


MonitorFactory = Callable[
    [Callable[[HeartRateProgress, HeartRateReport | None], None]],
    HeartRateMonitorActivationSession,
]


@dataclass(frozen=True, slots=True)
class HRSemanticsResult:
    scenario: HRSemanticsScenario
    reports_received: int
    cycles_completed: int
    aap_connections_opened: int
    descriptor_handshakes_completed: int


class HRSemanticsSession:
    """Run one or two canonical HR activations on one coexistence channel."""

    def __init__(
        self,
        client: BlueZStateClient,
        registration: CompatibilityRegistration,
        transport: CoexistenceTransport,
        handshake: AAPHandshakeSession,
        recorder: HRSemanticsRecorder,
        *,
        scenario: HRSemanticsScenario,
        requested_samples: int | None,
        requested_samples_per_cycle: int | None,
        restart_delay: float = DEFAULT_RESTART_DELAY,
        cycle_timeout: float = DEFAULT_CYCLE_TIMEOUT,
        dbus_timeout: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monitor_factory: MonitorFactory | None = None,
        output: Callable[[str], None] = print,
    ) -> None:
        if restart_delay < 0 or cycle_timeout <= 0 or dbus_timeout <= 0:
            raise ValueError("semantics timeouts must be positive or zero")
        if scenario is HRSemanticsScenario.BASELINE:
            if requested_samples is None or requested_samples_per_cycle is not None:
                raise ValueError("baseline requires requested_samples only")
            if requested_samples <= 0:
                raise ValueError("baseline sample count must be positive")
        elif (
            requested_samples is not None
            or requested_samples_per_cycle is None
        ):
            raise ValueError("activation-restart requires samples per cycle only")
        elif requested_samples_per_cycle <= 0:
            raise ValueError("restart sample count must be positive")
        self._client = client
        self._registration = registration
        self._transport = transport
        self._handshake = handshake
        self._recorder = recorder
        self._scenario = scenario
        self._requested_samples = requested_samples
        self._requested_samples_per_cycle = requested_samples_per_cycle
        self._restart_delay = restart_delay
        self._cycle_timeout = cycle_timeout
        self._dbus_timeout = dbus_timeout
        self._sleep = sleep
        self._monitor_factory = monitor_factory or self._make_monitor
        self._output = output

    async def run(self) -> HRSemanticsResult:
        state: BlueZCoexistenceState | None = None
        primary_error: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        descriptor_complete = False
        aap_connections_opened = 0
        descriptor_handshakes_completed = 0
        try:
            self._output("PHASE 0 — semantics_preflight")
            await asyncio.wait_for(
                self._client.connect(), timeout=self._dbus_timeout
            )
            state = await asyncio.wait_for(
                self._client.preflight(require_connected=True),
                timeout=self._dbus_timeout,
            )
            self._assert_connected(state, "preflight")
            self._output(
                "preflight: BlueZ reachable=yes, adapter powered=yes, "
                "Device1.Connected=true"
            )

            self._output("PHASE 1 — compatibility_registration")
            await self._registration.register(state)
            await self._checkpoint(state, "after_profile_registration")

            self._output("PHASE 2 — l2cap_connection")
            await self._transport.open(
                str(state.candidate.adapter_address),
                str(state.candidate.address),
            )
            aap_connections_opened = 1
            local_rx = self._transport.local_rx_observation
            if local_rx.after_imtu != 2048 or not local_rx.verified:
                raise HRSemanticsFailure(
                    HRSemanticsCategory.PREFLIGHT_FAILED,
                    "l2cap_local_rx_verification",
                )
            self._output(
                "Kernel L2CAP local RX: imtu=2048 verified; selected route "
                "confirmed"
            )
            await self._checkpoint(state, "after_l2cap_connect")

            async with self._transport.collect():
                self._output("PHASE 3 — descriptor_handshake")
                handshake = await self._handshake.run_collected(self._transport)
                descriptor_complete = handshake.evidence.required
                if not descriptor_complete:
                    raise HRSemanticsFailure(
                        HRSemanticsCategory.HANDSHAKE_FAILED,
                        "descriptor_handshake",
                    )
                descriptor_handshakes_completed = 1
                self._recorder.write_header(
                    descriptor_complete=True,
                    local_rx_imtu=local_rx.after_imtu,
                )
                await self._checkpoint(state, "after_descriptor_handshake")

                cycle_targets = self._cycle_targets()
                for cycle_index, target in enumerate(cycle_targets, 1):
                    if cycle_index > 1:
                        self._output(
                            f"Restart delay: {self._restart_delay:g} seconds"
                        )
                        await self._sleep(self._restart_delay)
                    await self._run_cycle(
                        state,
                        handshake,
                        cycle_index=cycle_index,
                        target=target,
                    )
        except BaseException as error:
            primary_error = error

        self._output("PHASE 5 — semantics_cleanup")
        try:
            self._transport.close()
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            await self._registration.unregister()
        except BaseException as error:
            cleanup_errors.append(error)
        if state is not None:
            try:
                await self._checkpoint(state, "after_cleanup")
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            self._client.close()
        except BaseException as error:
            cleanup_errors.append(error)

        if not self._recorder.header_written:
            local_rx_imtu = self._transport.local_rx_observation.after_imtu
            self._recorder.write_header(
                descriptor_complete=descriptor_complete,
                local_rx_imtu=local_rx_imtu,
            )
        effective_error = primary_error or (
            HRSemanticsFailure(
                HRSemanticsCategory.CLEANUP_FAILED, "semantics_cleanup"
            )
            if cleanup_errors
            else None
        )
        self._recorder.write_summary(
            status="failed" if effective_error is not None else "complete",
            failure_category=self._failure_category(effective_error),
        )
        if effective_error is not None:
            if primary_error is not None and cleanup_errors:
                primary_error.add_note("semantics cleanup also failed")
            raise effective_error

        return HRSemanticsResult(
            scenario=self._scenario,
            reports_received=len(self._recorder.records),
            cycles_completed=len(self._cycle_targets()),
            aap_connections_opened=aap_connections_opened,
            descriptor_handshakes_completed=descriptor_handshakes_completed,
        )

    async def _run_cycle(
        self,
        state: BlueZCoexistenceState,
        handshake: AAPHandshakeResult,
        *,
        cycle_index: int,
        target: int,
    ) -> HeartRateMonitorSessionResult:
        self._output(f"PHASE 4 — hr_cycle_{cycle_index}")
        self._recorder.mark_cycle_attempted(cycle_index)
        stop_event = asyncio.Event()

        def progress(
            event: HeartRateProgress, report: HeartRateReport | None
        ) -> None:
            if event is HeartRateProgress.START_ACKNOWLEDGED:
                self._recorder.begin_cycle(cycle_index)
                self._output(f"Cycle {cycle_index}: activation ACK observed")
            elif event is HeartRateProgress.SAMPLE and report is not None:
                record = self._recorder.record_sample(report)
                delta = (
                    "n/a"
                    if record.delta_from_previous_report_ms is None
                    else f"{record.delta_from_previous_report_ms:g}ms"
                )
                self._output(
                    f"C{cycle_index} S{record.sample_index_within_cycle:02d} "
                    f"bpm={record.bpm} seq={record.sequence} "
                    f"field_5={record.field_5} ts={record.timestamp_ticks} "
                    f"flags={record.flags_hex} dt={delta}"
                )
                if record.sample_index_within_cycle >= target:
                    stop_event.set()
            elif event is HeartRateProgress.STOP_ACKNOWLEDGED:
                self._output(f"Cycle {cycle_index}: stop ACK observed")
            elif event is HeartRateProgress.HR_OFF_SENT:
                self._output(f"Cycle {cycle_index}: HR_OFF sent")

        monitor = self._monitor_factory(progress)
        relative_transport = _CycleRelativeTransport(self._transport)
        try:
            result = await asyncio.wait_for(
                monitor.run_collected(
                    relative_transport,
                    handshake,
                    stop_event,
                ),
                timeout=self._cycle_timeout,
            )
            if result.samples_observed != target:
                raise HRSemanticsFailure(
                    HRSemanticsCategory.CYCLE_FAILED,
                    f"hr_cycle_{cycle_index}",
                )
            if not result.stop_acknowledged:
                raise HRSemanticsFailure(
                    HRSemanticsCategory.STOP_ACK_MISSING,
                    f"hr_cycle_{cycle_index}",
                )
            self._recorder.complete_cycle(cycle_index)
            await self._checkpoint(state, f"after_hr_cycle_{cycle_index}")
            return result
        except TimeoutError as error:
            self._recorder.abandon_cycle(cycle_index)
            raise HRSemanticsFailure(
                HRSemanticsCategory.CYCLE_TIMEOUT,
                f"hr_cycle_{cycle_index}",
            ) from error
        except BaseException:
            self._recorder.abandon_cycle(cycle_index)
            raise

    async def _checkpoint(
        self, initial: BlueZCoexistenceState, label: str
    ) -> BlueZCoexistenceState:
        current = await asyncio.wait_for(
            self._client.snapshot(initial.candidate), timeout=self._dbus_timeout
        )
        self._assert_connected(current, label)
        self._output(
            f"{label}: BlueZ reachable=yes, adapter powered=yes, "
            "Device1.Connected=true"
        )
        return current

    @staticmethod
    def _assert_connected(state: BlueZCoexistenceState, phase: str) -> None:
        if not state.adapter_powered or not state.device_connected:
            raise HRSemanticsFailure(
                HRSemanticsCategory.BLUEZ_CONNECTION_LOST, phase
            )

    def _cycle_targets(self) -> tuple[int, ...]:
        if self._scenario is HRSemanticsScenario.BASELINE:
            assert self._requested_samples is not None
            return (self._requested_samples,)
        assert self._requested_samples_per_cycle is not None
        return (
            self._requested_samples_per_cycle,
            self._requested_samples_per_cycle,
        )

    @staticmethod
    def _failure_category(error: BaseException | None) -> str | None:
        if isinstance(error, HRSemanticsFailure):
            return error.category.value
        if isinstance(error, CoexistenceFailure):
            return error.category.value
        return type(error).__name__ if error is not None else None

    @staticmethod
    def _make_monitor(
        progress: Callable[[HeartRateProgress, HeartRateReport | None], None],
    ) -> HeartRateMonitorActivationSession:
        return HeartRateMonitorActivationSession(progress=progress)
