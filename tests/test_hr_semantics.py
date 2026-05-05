"""Hardware-independent tests for telemetry semantics capture."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import tempfile
import unittest
from contextlib import asynccontextmanager, redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from airpods_hr.aap import AAP_HANDSHAKE_ACK, AAPHandshakeSession
from airpods_hr.address import BluetoothAddress
from airpods_hr.bluez_coexistence import (
    BlueZCoexistenceState,
    KernelL2CAPLocalRXObservation,
)
from airpods_hr.discovery import AirPodsCandidate
from airpods_hr.heart_rate_diagnostics import JsonlDiagnosticSink
from airpods_hr.heart_rate_session import (
    CONNECT4_ACK,
    HeartRateMonitorActivationSession,
    HeartRateMonitorSessionResult,
    HeartRateProgress,
)
from airpods_hr.heartrate import HeartRateReport, parse_heart_rate_packet
from airpods_hr.hr_semantics import (
    PARSER_STRUCTURE,
    HRSemanticsRecorder,
    HRSemanticsScenario,
    HRSemanticsSession,
)
from airpods_hr.protocol import (
    HEART_RATE_MARKER,
    HEART_RATE_REPORT_ID,
    HEART_RATE_REPORT_SIZE,
    HeartRateCommand,
)
from tools.probe_hr_semantics import build_parser, main, run_probe


LOCAL_ADDRESS = "00:11:22:33:44:55"
REMOTE_ADDRESS = "AA:BB:CC:DD:EE:FF"


class MemorySink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.close_calls = 0

    def write_event(self, event) -> None:
        self.events.append(dict(event))

    def close(self) -> None:
        self.close_calls += 1


class ClockNS:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def __call__(self) -> int:
        return self.values.pop(0)


def raw_report(
    bpm: int,
    sequence: int,
    *,
    aux: int = 2,
    field_5: int = 3,
    timestamp_ticks: int = 100,
    flags: int = 0,
) -> bytes:
    report = bytearray(HEART_RATE_REPORT_SIZE)
    report[0] = HEART_RATE_REPORT_ID
    report[1] = bpm
    report[2] = aux
    report[3:5] = sequence.to_bytes(2, "little")
    report[5] = field_5
    report[6:14] = timestamp_ticks.to_bytes(8, "little")
    report[14:18] = flags.to_bytes(4, "little")
    return bytes(report)


def parsed_report(*args, **kwargs) -> HeartRateReport:
    return parse_heart_rate_packet(
        HEART_RATE_MARKER + raw_report(*args, **kwargs)
    )


def heart_rate_packet(*args, **kwargs) -> bytes:
    return b"outer" + HEART_RATE_MARKER + raw_report(*args, **kwargs)


def service_ack(service_id: int) -> bytes:
    return bytes.fromhex(
        "04 00 04 00 17 00 00 00 10 00 08 00 08 01 10 01 4a 02 08"
    ) + bytes((service_id,))


def descriptor_frame() -> bytes:
    return (
        b"AccessoryService devmotion6 MaxReportSize ReportDescriptor "
        b"HeartRateService HeartRate com.apple.hid.heartrate-access"
    )


def candidate() -> AirPodsCandidate:
    return AirPodsCandidate(
        address=BluetoothAddress.parse(REMOTE_ADDRESS),
        display_name="AirPods Pro",
        object_path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        adapter_path="/org/bluez/hci0",
        adapter_address=BluetoothAddress.parse(LOCAL_ADDRESS),
        adapter_name="hci0",
        adapter_modalias="usb:v1234p5678d0001",
    )


def state() -> BlueZCoexistenceState:
    return BlueZCoexistenceState(candidate(), True, True, frozenset())


class FakeClient:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.preflight_calls = 0
        self.snapshot_calls = 0
        self.close_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def preflight(self, *, require_connected: bool = True):
        self.preflight_calls += 1
        self.require_connected = require_connected
        return state()

    async def snapshot(self, selected):
        self.snapshot_calls += 1
        self.selected = selected
        return state()

    def close(self) -> None:
        self.close_calls += 1


class FakeRegistration:
    def __init__(self) -> None:
        self.register_calls = 0
        self.unregister_calls = 0
        self.registered_count = 0

    async def register(self, selected_state) -> None:
        self.register_calls += 1
        self.selected_state = selected_state

    async def unregister(self) -> None:
        self.unregister_calls += 1


class FakeTransport:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = list(frames)
        self.application_payloads_sent = 0
        self.pending_receive_frames = 0
        self.dropped_frames = 0
        self.commands: list[HeartRateCommand] = []
        self.open_calls: list[tuple[str, str]] = []
        self.collect_entries = 0
        self.close_calls = 0
        self.local_rx_observation = KernelL2CAPLocalRXObservation(
            target_imtu=2048,
            options_source="linux-uapi-fallback",
            before_imtu=672,
            after_imtu=2048,
            preserved_omtu=True,
            preserved_flush_to=True,
            preserved_mode=True,
            preserved_fcs=True,
            preserved_max_tx=True,
            preserved_txwin_size=True,
            verified=True,
        )

    async def open(self, local_address: str, remote_address: str) -> None:
        self.open_calls.append((local_address, remote_address))

    @asynccontextmanager
    async def collect(self):
        self.collect_entries += 1
        yield self

    def send_handshake_request(self) -> None:
        self.application_payloads_sent += 1

    def send_heart_rate_command(self, command: HeartRateCommand) -> None:
        self.commands.append(command)
        self.application_payloads_sent += 1

    async def receive(self, timeout: float) -> bytes:
        del timeout
        if not self.frames:
            raise TimeoutError
        return self.frames.pop(0)

    def close(self) -> None:
        self.close_calls += 1


def cycle_frames(reports: list[bytes]) -> list[bytes]:
    return [
        service_ack(0x0E),
        CONNECT4_ACK,
        service_ack(0x13),
        *reports,
        service_ack(0x13),
    ]


def make_recorder(
    sink: MemorySink,
    *,
    scenario: HRSemanticsScenario,
    clock: ClockNS | None = None,
) -> HRSemanticsRecorder:
    return HRSemanticsRecorder(
        sink,
        scenario=scenario,
        requested_samples=(
            30 if scenario is HRSemanticsScenario.BASELINE else None
        ),
        requested_samples_per_cycle=(
            10
            if scenario is HRSemanticsScenario.ACTIVATION_RESTART
            else None
        ),
        restart_delay_seconds=5,
        monotonic_clock_ns=clock or ClockNS([0] * 100),
    )


class SemanticsRecorderTests(unittest.TestCase):
    def test_parser_widths_are_derived_from_frozen_parser(self) -> None:
        self.assertEqual(PARSER_STRUCTURE.canonical_report_length, 18)
        self.assertEqual(PARSER_STRUCTURE.sequence_width_bits, 16)
        self.assertEqual(PARSER_STRUCTURE.timestamp_width_bits, 64)
        self.assertEqual(PARSER_STRUCTURE.flags_width_bits, 32)

    def test_records_exact_fields_timing_flags_and_modulo_deltas(self) -> None:
        sink = MemorySink()
        recorder = make_recorder(
            sink,
            scenario=HRSemanticsScenario.BASELINE,
            clock=ClockNS([1_000_000_000, 1_250_000_000, 1_750_000_000]),
        )
        recorder.write_header(descriptor_complete=True, local_rx_imtu=2048)
        recorder.mark_cycle_attempted(1)
        recorder.begin_cycle(1)
        first = recorder.record_sample(
            parsed_report(
                169,
                0xFFFF,
                aux=7,
                field_5=9,
                timestamp_ticks=(1 << 64) - 1,
                flags=0x81800000,
            )
        )
        second = recorder.record_sample(
            parsed_report(
                42,
                0,
                aux=8,
                field_5=10,
                timestamp_ticks=1,
                flags=3,
            )
        )

        self.assertEqual([r.bpm for r in recorder.records], [169, 42])
        self.assertEqual(first.sample_index_within_cycle, 1)
        self.assertEqual(first.sample_index_global, 1)
        self.assertIsNone(first.delta_from_previous_report_ms)
        self.assertEqual(first.milliseconds_since_activation_ack, 250)
        self.assertEqual(first.flags_decimal, 0x81800000)
        self.assertEqual(first.flags_hex, "0x81800000")
        self.assertEqual(first.flags_bits_set, (23, 24, 31))
        self.assertEqual(first.raw_report_bytes, 18)
        self.assertEqual(first.raw_report_hex, first.raw_report_hex.lower())
        self.assertEqual(second.delta_from_previous_report_ms, 500)
        self.assertEqual(second.milliseconds_since_activation_ack, 750)
        self.assertEqual(second.sequence_delta_modulo, 1)
        self.assertEqual(second.timestamp_delta_modulo, 2)
        self.assertEqual(second.aux, 8)
        self.assertEqual(second.field_5, 10)
        self.assertEqual(second.flags_bits_set, (0, 1))

    def test_duplicate_reports_remain_ordered_and_unfiltered(self) -> None:
        sink = MemorySink()
        recorder = make_recorder(
            sink,
            scenario=HRSemanticsScenario.BASELINE,
            clock=ClockNS([0, 1_000_000, 2_000_000]),
        )
        recorder.write_header(descriptor_complete=True, local_rx_imtu=2048)
        recorder.mark_cycle_attempted(1)
        recorder.begin_cycle(1)
        report = parsed_report(169, 0, field_5=2)
        first = recorder.record_sample(report)
        second = recorder.record_sample(report)
        self.assertFalse(first.duplicate_parsed_report)
        self.assertTrue(second.duplicate_parsed_report)
        self.assertEqual(len(recorder.records), 2)
        self.assertEqual([record.bpm for record in recorder.records], [169, 169])

    def test_header_sample_and_summary_are_safe_independent_json_lines(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "semantics.jsonl"
            recorder = HRSemanticsRecorder(
                JsonlDiagnosticSink.open(path),
                scenario=HRSemanticsScenario.BASELINE,
                requested_samples=1,
                requested_samples_per_cycle=None,
                restart_delay_seconds=5,
                monotonic_clock_ns=ClockNS([0, 1_000_000]),
            )
            recorder.write_header(descriptor_complete=True, local_rx_imtu=2048)
            recorder.mark_cycle_attempted(1)
            recorder.begin_cycle(1)
            recorder.record_sample(parsed_report(169, 0))
            recorder.complete_cycle(1)
            summary = recorder.write_summary(
                status="complete", failure_category=None
            )
            recorder.close()

            events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(
            [event["record_type"] for event in events],
            ["capture_header", "sample", "capture_summary"],
        )
        self.assertEqual(events[0]["local_rx_imtu"], 2048)
        self.assertTrue(events[0]["descriptor_complete"])
        self.assertEqual(events[1]["raw_report_bytes"], 18)
        self.assertEqual(summary["first_bpm_per_cycle"], [169])
        self.assertEqual(summary["last_bpm_per_cycle"], [169])
        serialized = json.dumps(events)
        for forbidden in (LOCAL_ADDRESS, REMOTE_ADDRESS, "LinkKey", "address"):
            self.assertNotIn(forbidden, serialized)

    def test_restart_summary_reports_only_mechanical_cycle_evidence(self) -> None:
        sink = MemorySink()
        recorder = make_recorder(
            sink,
            scenario=HRSemanticsScenario.ACTIVATION_RESTART,
            clock=ClockNS([0, 1, 2, 3]),
        )
        recorder.write_header(descriptor_complete=True, local_rx_imtu=2048)
        recorder.mark_cycle_attempted(1)
        recorder.begin_cycle(1)
        recorder.record_sample(parsed_report(80, 7, field_5=3, flags=4))
        recorder.complete_cycle(1)
        recorder.mark_cycle_attempted(2)
        recorder.begin_cycle(2)
        recorder.record_sample(parsed_report(90, 0, field_5=5, flags=8))
        recorder.complete_cycle(2)
        summary = recorder.write_summary(
            status="complete", failure_category=None
        )
        self.assertEqual(summary["cycles_completed"], 2)
        self.assertEqual(summary["first_bpm_per_cycle"], [80, 90])
        self.assertEqual(
            summary["sequence_reset_observed_between_cycles"], "yes"
        )
        self.assertEqual(
            summary["cycle_summaries"][0]["field_5_unique_values"], [3]
        )
        self.assertEqual(
            summary["cycle_summaries"][1]["flags_unique_hex_values"],
            ["0x00000008"],
        )
        self.assertEqual(summary["flags_bit_positions_observed"], [2, 3])


class SemanticsOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_baseline_records_all_thirty_samples_in_one_cycle(self) -> None:
        reports = [heart_rate_packet(169 if i == 0 else 70 + i, i) for i in range(30)]
        transport = FakeTransport(
            [AAP_HANDSHAKE_ACK, descriptor_frame(), *cycle_frames(reports)]
        )
        sink = MemorySink()
        recorder = HRSemanticsRecorder(
            sink,
            scenario=HRSemanticsScenario.BASELINE,
            requested_samples=30,
            requested_samples_per_cycle=None,
            restart_delay_seconds=5,
        )
        client = FakeClient()
        registration = FakeRegistration()
        session = HRSemanticsSession(
            client,
            registration,
            transport,
            AAPHandshakeSession(),
            recorder,
            scenario=HRSemanticsScenario.BASELINE,
            requested_samples=30,
            requested_samples_per_cycle=None,
            monitor_factory=lambda progress: HeartRateMonitorActivationSession(
                progress=progress,
                minimum_bootstrap_seconds=0,
                receive_poll_interval=0.01,
            ),
            output=lambda message: None,
        )
        result = await session.run()

        self.assertEqual(result.reports_received, 30)
        self.assertEqual(result.cycles_completed, 1)
        self.assertEqual(result.aap_connections_opened, 1)
        self.assertEqual(result.descriptor_handshakes_completed, 1)
        self.assertEqual(len(recorder.records), 30)
        self.assertEqual(recorder.records[0].bpm, 169)
        self.assertEqual(
            [record.sequence for record in recorder.records], list(range(30))
        )
        self.assertEqual(transport.open_calls, [(LOCAL_ADDRESS, REMOTE_ADDRESS)])
        self.assertEqual(transport.collect_entries, 1)
        self.assertEqual(transport.application_payloads_sent, 10)
        self.assertEqual(transport.commands, list(HeartRateCommand))
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(client.close_calls, 1)

    async def test_restart_uses_one_channel_one_handshake_and_two_cycles(
        self,
    ) -> None:
        first = [heart_rate_packet(169, i) for i in range(3)]
        second = [heart_rate_packet(169, i) for i in range(3)]
        transport = FakeTransport(
            [
                AAP_HANDSHAKE_ACK,
                descriptor_frame(),
                *cycle_frames(first),
                *cycle_frames(second),
            ]
        )
        sink = MemorySink()
        recorder = HRSemanticsRecorder(
            sink,
            scenario=HRSemanticsScenario.ACTIVATION_RESTART,
            requested_samples=None,
            requested_samples_per_cycle=3,
            restart_delay_seconds=5,
        )
        sleeps: list[float] = []

        async def sleep(delay: float) -> None:
            sleeps.append(delay)

        client = FakeClient()
        registration = FakeRegistration()
        session = HRSemanticsSession(
            client,
            registration,
            transport,
            AAPHandshakeSession(),
            recorder,
            scenario=HRSemanticsScenario.ACTIVATION_RESTART,
            requested_samples=None,
            requested_samples_per_cycle=3,
            restart_delay=5,
            sleep=sleep,
            monitor_factory=lambda progress: HeartRateMonitorActivationSession(
                progress=progress,
                minimum_bootstrap_seconds=0,
                receive_poll_interval=0.01,
            ),
            output=lambda message: None,
        )
        result = await session.run()

        self.assertEqual(result.reports_received, 6)
        self.assertEqual(result.cycles_completed, 2)
        self.assertEqual(result.aap_connections_opened, 1)
        self.assertEqual(result.descriptor_handshakes_completed, 1)
        self.assertEqual(transport.open_calls, [(LOCAL_ADDRESS, REMOTE_ADDRESS)])
        self.assertEqual(transport.collect_entries, 1)
        self.assertEqual(sleeps, [5])
        self.assertEqual(
            [record.cycle_index for record in recorder.records],
            [1, 1, 1, 2, 2, 2],
        )
        self.assertEqual(
            [record.sample_index_within_cycle for record in recorder.records],
            [1, 2, 3, 1, 2, 3],
        )
        self.assertEqual(
            [record.sample_index_global for record in recorder.records],
            [1, 2, 3, 4, 5, 6],
        )
        expected_cycle = list(HeartRateCommand)
        self.assertEqual(
            transport.commands, expected_cycle + expected_cycle
        )
        self.assertEqual(transport.application_payloads_sent, 19)
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(client.close_calls, 1)
        first_stop = transport.commands.index(HeartRateCommand.STOP_HR)
        second_start = transport.commands.index(
            HeartRateCommand.START_HR, len(expected_cycle)
        )
        self.assertLess(first_stop, second_start)

    async def test_second_cycle_failure_preserves_first_and_partial_second(
        self,
    ) -> None:
        sink = MemorySink()
        recorder = HRSemanticsRecorder(
            sink,
            scenario=HRSemanticsScenario.ACTIVATION_RESTART,
            requested_samples=None,
            requested_samples_per_cycle=2,
            restart_delay_seconds=0,
            monotonic_clock_ns=ClockNS([0, 1, 2, 3, 4]),
        )
        calls = 0

        class Monitor:
            def __init__(self, progress) -> None:
                self.progress = progress

            async def run_collected(self, transport, handshake, stop_event):
                nonlocal calls
                del transport, handshake, stop_event
                calls += 1
                self.progress(HeartRateProgress.START_ACKNOWLEDGED, None)
                self.progress(
                    HeartRateProgress.SAMPLE,
                    parsed_report(169, calls),
                )
                if calls == 2:
                    raise RuntimeError("synthetic later failure")
                self.progress(
                    HeartRateProgress.SAMPLE,
                    parsed_report(80, 2),
                )
                self.progress(HeartRateProgress.STOP_ACKNOWLEDGED, None)
                self.progress(HeartRateProgress.HR_OFF_SENT, None)
                return HeartRateMonitorSessionResult(
                    samples_observed=2,
                    stop_acknowledged=True,
                    application_payloads_sent=10,
                    control_frames_observed=0,
                    non_hr_frames=0,
                    malformed_hr_frames=0,
                )

        transport = FakeTransport([AAP_HANDSHAKE_ACK, descriptor_frame()])
        client = FakeClient()
        registration = FakeRegistration()
        session = HRSemanticsSession(
            client,
            registration,
            transport,
            AAPHandshakeSession(),
            recorder,
            scenario=HRSemanticsScenario.ACTIVATION_RESTART,
            requested_samples=None,
            requested_samples_per_cycle=2,
            restart_delay=0,
            sleep=AsyncMock(),
            monitor_factory=Monitor,
            output=lambda message: None,
        )
        with self.assertRaises(RuntimeError):
            await session.run()

        self.assertEqual([r.cycle_index for r in recorder.records], [1, 1, 2])
        summary = sink.events[-1]
        self.assertEqual(summary["record_type"], "capture_summary")
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["cycles_completed"], 1)
        self.assertEqual(summary["canonical_reports_received"], 3)
        self.assertTrue(summary["cycle_summaries"][0]["complete"])
        self.assertFalse(summary["cycle_summaries"][1]["complete"])
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(client.close_calls, 1)


class SemanticsProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_parser_defaults_and_bounds(self) -> None:
        args = build_parser().parse_args([])
        self.assertFalse(args.execute)
        self.assertEqual(args.scenario, "baseline")
        self.assertEqual(args.samples, 30)
        self.assertEqual(args.samples_per_cycle, 10)
        self.assertEqual(args.restart_delay, 5)
        self.assertEqual(args.descriptor_timeout, 30)
        restart = build_parser().parse_args(
            ["--scenario", "activation-restart"]
        )
        self.assertEqual(restart.scenario, "activation-restart")
        for option, value in (
            ("--samples", "0"),
            ("--samples", "301"),
            ("--samples-per-cycle", "0"),
            ("--restart-delay", "31"),
            ("--descriptor-timeout", "0"),
        ):
            with self.subTest(option=option), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args([option, value])

    async def test_dry_run_creates_no_backend_or_file(self) -> None:
        runner = AsyncMock()
        output: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "dry-run.jsonl"
            status = await run_probe(
                execute=False,
                scenario=HRSemanticsScenario.ACTIVATION_RESTART,
                output_path=target,
                output=output.append,
                live_runner=runner,
            )
            self.assertFalse(target.exists())
        self.assertEqual(status, 0)
        runner.assert_not_awaited()
        rendered = "\n".join(output)
        self.assertIn("AAP channels=1; descriptor handshakes=1", rendered)
        self.assertIn("reports_per_cycle=10", rendered)
        self.assertIn("kernel_local_rx_imtu=2048", rendered)

    def test_main_default_is_deterministic_and_non_live(self) -> None:
        first = StringIO()
        second = StringIO()
        self.assertEqual(main([], stream=first), 0)
        self.assertEqual(main([], stream=second), 0)
        self.assertEqual(first.getvalue(), second.getvalue())
        self.assertIn("DRY RUN", first.getvalue())


class SemanticsStaticSafetyTests(unittest.TestCase):
    def test_frozen_protocol_transport_and_monitor_hashes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        expected = {
            "src/airpods_hr/protocol.py": (
                "b4d1daea0582841e48ba9efc3a8a7d4d74bba9b69cdbf54d3767b8bb45afecca"
            ),
            "src/airpods_hr/monitor_cli.py": (
                "41332f411af2e89374b42047a2e009aef035ca2e8bce74440d0d9d891d7aded4"
            ),
            "src/airpods_hr/bluez_coexistence.py": (
                "d0e666932d485a9f4ccc64f7d2c946fd1d14af2f6e9d8c1c8ceb263f0c52da25"
            ),
            "src/airpods_hr/aap_config_diagnostics.py": (
                "a5db6e50e14cc08f78bc0218f7c7ae7411739cfa644c9203e6f1f1c4a7a1970d"
            ),
            "src/airpods_hr/aap_local_rx_diagnostics.py": (
                "40b5f39c3737eec4cd132804b2dfbfe57c7c2180265b15d7987139a876830d1e"
            ),
            "src/airpods_hr/pre_aap_diagnostics.py": (
                "0b37874aa7136a4ef754974d873d064ad92027c01a4f263efc4dd214c9479782"
            ),
            "src/airpods_hr/pre_auth_diagnostics.py": (
                "c0331641dad21cd9ace2e0708e8586c50863b4cc9c5a5b2bf1792661e6ec0d4c"
            ),
            "src/airpods_hr/reference_sdp_footprint.py": (
                "3dab4655a72d2e97877691cb3a0d1b73d2d16ba35c02d2f98b4218cd68447325"
            ),
            "tools/probe_reference_handshake.py": (
                "3928019cb5bd8948935d80aa6bd37e7ec6f109811160464d8f214004f717cb66"
            ),
        }
        for relative, digest in expected.items():
            self.assertEqual(
                hashlib.sha256((root / relative).read_bytes()).hexdigest(),
                digest,
            )

    def test_heart_rate_session_orchestration_remains_frozen(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (root / "src/airpods_hr/heart_rate_session.py").read_text()
        tail = "class HeartRateActivationSession:" + source.split(
            "class HeartRateActivationSession:", 1
        )[1]
        self.assertEqual(
            hashlib.sha256(tail.encode()).hexdigest(),
            "d67ee87d07dcb84988f056afe5eeeefafbfdbd124808cb281ce89fb7c9d2aae9",
        )

    def test_semantics_path_has_no_bumble_handoff_or_pairing_dependency(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        sources = (
            root / "src/airpods_hr/hr_semantics.py",
            root / "tools/probe_hr_semantics.py",
        )
        forbidden_modules = {
            "airpods_hr.authentication",
            "airpods_hr.bluetooth",
            "airpods_hr.bumble_keys",
            "airpods_hr.pairing",
            "airpods_hr.reference_sdp_footprint",
        }
        for source in sources:
            tree = ast.parse(source.read_text(encoding="utf-8"))
            imports = {
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            }
            self.assertTrue(forbidden_modules.isdisjoint(imports))
        self.assertNotIn("ControllerHandoff", "".join(s.read_text() for s in sources))
        self.assertNotIn("HCI_CHANNEL_USER", "".join(s.read_text() for s in sources))


if __name__ == "__main__":
    unittest.main()
