"""Private telemetry evidence capture over the proven BlueZ coexistence path."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic_ns
from typing import Any

from airpods_hr.aap import AAPHandshakeResult, AAPHandshakeSession
from airpods_hr import _airpods_aap_core as _rust_core
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
from airpods_hr.heartrate import HeartRateReport
from airpods_hr.protocol import (
    HeartRateCommand,
)


SEMANTICS_SCHEMA_VERSION = _rust_core.semantics_schema_version()
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


PARSER_STRUCTURE = ParserStructure(*_rust_core.semantics_parser_structure())

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
            "record_type": _rust_core.SEMANTICS_SAMPLE_RECORD_TYPE,
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


class HRSemanticsRecorder:
    """Python clock and sink adapter over the authoritative native recorder."""

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
        self._clock_ns = monotonic_clock_ns
        self._state = _rust_core.HRSemanticsState(
            scenario.value,
            requested_samples,
            requested_samples_per_cycle,
            restart_delay_seconds,
        )
        self._records: list[HRSemanticsSampleRecord] = []

    @property
    def records(self) -> tuple[HRSemanticsSampleRecord, ...]:
        return tuple(self._records)

    @property
    def header_written(self) -> bool:
        return self._state.header_written

    def write_header(
        self, *, descriptor_complete: bool, local_rx_imtu: int | None
    ) -> None:
        event = self._state.prepare_header(descriptor_complete, local_rx_imtu)
        self._sink.write_event(event)
        self._state.commit_header()

    def mark_cycle_attempted(self, cycle_index: int) -> None:
        self._state.mark_cycle_attempted(cycle_index)

    def begin_cycle(self, cycle_index: int) -> None:
        self._state.validate_begin_cycle(cycle_index)
        self._state.begin_cycle(cycle_index, self._clock_ns())

    def record_sample(self, report: HeartRateReport) -> HRSemanticsSampleRecord:
        self._state.validate_sample()
        self._state.validate_raw_report(report.raw_report)
        fields = self._state.prepare_sample(
            self._clock_ns(),
            (report.bpm, report.aux, report.sequence, report.field_5,
             report.timestamp_ticks, report.flags),
            report.raw_report,
        )
        fields["flags_bits_set"] = tuple(fields["flags_bits_set"])
        record = HRSemanticsSampleRecord(**fields)
        self._sink.write_event(record.as_event())
        self._state.commit_sample()
        self._records.append(record)
        return record

    def complete_cycle(self, cycle_index: int) -> None:
        self._state.complete_cycle(cycle_index)

    def abandon_cycle(self, cycle_index: int) -> None:
        self._state.abandon_cycle(cycle_index)

    def write_summary(
        self, *, status: str, failure_category: str | None
    ) -> Mapping[str, Any]:
        summary = self._state.prepare_summary(status, failure_category)
        self._sink.write_event(summary)
        self._state.commit_summary()
        return summary

    def close(self) -> None:
        self._sink.close()


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
        self._cycle_plan = tuple(_rust_core.semantics_cycle_plan(
            scenario.value, requested_samples, requested_samples_per_cycle
        ))
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
        return self._cycle_plan

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
