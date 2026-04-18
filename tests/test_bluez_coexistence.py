"""Hardware-independent tests for the BlueZ coexistence probe."""

from __future__ import annotations

import ast
import asyncio
import ctypes
import struct
import unittest
from contextlib import asynccontextmanager, redirect_stderr
from dataclasses import fields
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from xml.etree import ElementTree

from dbus_next.errors import DBusError

from airpods_hr.aap import (
    AAP_FRAME_SUMMARY_LIMIT,
    AAP_HANDSHAKE_ACK,
    AAP_HANDSHAKE_REQUEST,
    AAPDescriptorObservationTimeoutError,
    AAPFrameSummary,
    AAPHandshakeSession,
    AAPHandshakeTimeoutError,
    AAPProgress,
    AAPType2BFrameSummary,
    DescriptorEvidence,
    HandshakeObservation,
)
from airpods_hr.address import BluetoothAddress
from airpods_hr.bluez_coexistence import (
    BlueZCompatibilityRegistration,
    BlueZCoexistenceSession,
    BlueZCoexistenceState,
    CoexistenceCategory,
    CoexistenceFailure,
    CoexistenceHRStreamObservation,
    CoexistenceHRTimeoutDiagnostics,
    CoexistencePhase,
    DBusNextBlueZCoexistenceClient,
    KernelL2CAPLocalRXObservation,
    KernelL2CAPTransport,
    _AAP_LOCAL_RX_IMTU,
    _L2CAP_IMTU_OFFSET,
    _L2CAP_OPTIONS_SIZE,
    _LINUX_L2CAP_OPTIONS,
    _LINUX_SOL_L2CAP,
    _NativeL2CAPOptions,
)
from airpods_hr.discovery import AirPodsCandidate
from airpods_hr.heart_rate_session import (
    CONNECT4_ACK,
    DEFAULT_CONTROL_SUMMARY_LIMIT,
    ControlFrameSummary,
    HeartRateActivationSession,
    HeartRateProgress,
)
from airpods_hr.heartrate import HeartRateReport
from airpods_hr.protocol import AAP_PSM, HEART_RATE_MARKER, HeartRateCommand
from airpods_hr.sdp import (
    USBAdapterIdentity,
    build_bluez_sdp_service_records,
)
from tools.probe_bluez_coexistence import (
    _heart_rate_progress,
    build_parser,
    main,
    run_live_probe,
    run_probe,
)


ALL_COMPATIBILITY_UUIDS = frozenset(
    record.uuid for record in build_bluez_sdp_service_records(
        USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)
    )
)


LOCAL_ADAPTER_ADDRESS = "00:11:22:33:44:55"
REMOTE_AIRPODS_ADDRESS = "AA:BB:CC:DD:EE:FF"

# Deterministic Linux-shaped values used only to verify injected constant
# plumbing. The fake transport must not depend on the host Python socket build.
TEST_AF_BLUETOOTH = 31
TEST_SOCK_SEQPACKET = 5
TEST_BTPROTO_L2CAP = 0
TEST_SOL_BLUETOOTH = 274
TEST_BT_SECURITY = 4
TEST_BT_SECURITY_MEDIUM = 2


def l2cap_options(
    *,
    omtu: int = 0,
    imtu: int = 672,
    flush_to: int = 0xFFFF,
    mode: int = 0,
    fcs: int = 1,
    max_tx: int = 3,
    txwin_size: int = 63,
) -> bytes:
    return struct.pack(
        "@HHHBBBxH", omtu, imtu, flush_to, mode, fcs, max_tx, txwin_size
    )


def candidate(
    *, adapter_modalias: str | None = "usb:v1234p5678d9ABC"
) -> AirPodsCandidate:
    return AirPodsCandidate(
        display_name="Test AirPods",
        adapter_name="hci0",
        adapter_path="/org/bluez/hci0",
        adapter_address=BluetoothAddress.parse(LOCAL_ADAPTER_ADDRESS),
        address=BluetoothAddress.parse(REMOTE_AIRPODS_ADDRESS),
        object_path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        adapter_modalias=adapter_modalias,
    )


def state(*, connected: bool = True, powered: bool = True):
    return BlueZCoexistenceState(
        candidate(), powered, connected, ALL_COMPATIBILITY_UUIDS
    )


def service_ack(service_id: int) -> bytes:
    suffix = bytes.fromhex("10 01 4a 02 08") + bytes((service_id,))
    payload = b"\x08\x7f" + suffix
    return (
        bytes.fromhex("04 00 04 00 17 00 00 00 10 00")
        + len(payload).to_bytes(2, "little")
        + payload
    )


def heart_rate_packet(bpm: int, sequence: int) -> bytes:
    report = (
        bytes((1, bpm, 9))
        + sequence.to_bytes(2, "little")
        + bytes((7,))
        + (1_000_000_000 * sequence).to_bytes(8, "little")
        + (3).to_bytes(4, "little")
    )
    return b"prefix" + HEART_RATE_MARKER + report


def successful_frames(sample_count: int = 5) -> list[bytes]:
    return [
        AAP_HANDSHAKE_ACK,
        b"AccessoryService HeartRateService",
        service_ack(0x0E),
        CONNECT4_ACK,
        service_ack(0x13),
        *(heart_rate_packet(index * 10, index) for index in range(sample_count)),
        service_ack(0x13),
    ]


def descriptor_timeout_observation(
    *, ack_observed: bool = True, receive_frames_dropped: int = 1
) -> HandshakeObservation:
    private_frame = b"\x00\x00\x04\x00\x10\x00PRIVATE_DESCRIPTOR_STRING"
    type_2b_frame = bytearray(51)
    type_2b_frame[2:4] = (4).to_bytes(2, "little")
    type_2b_frame[4:6] = (0x002B).to_bytes(2, "little")
    type_2b_frame[6] = 3
    type_2b_frame[7:9] = (34).to_bytes(2, "little")
    type_2b_frame[31:34] = bytes((7, 0x34, 0x12))
    type_2b_frame[48:51] = bytes((7, 0x34, 0x12))
    return HandshakeObservation(
        ack_observed=ack_observed,
        evidence=DescriptorEvidence(
            sensor_framework=True,
            heart_rate_service=False,
            heart_rate=True,
            heartrate_access=False,
        ),
        pre_ack_frame_count=2,
        post_ack_frame_count=3,
        receive_frames_dropped=receive_frames_dropped,
        pre_ack_frame_summaries=(AAPFrameSummary.from_frame(private_frame),),
        post_ack_frame_summaries=(
            AAPFrameSummary.from_frame(bytes(type_2b_frame)),
        ),
    )


def activation_frames(sample_count: int = 5) -> list[bytes]:
    return [
        service_ack(0x0E),
        CONNECT4_ACK,
        service_ack(0x13),
        *(heart_rate_packet(index * 10, index) for index in range(sample_count)),
        service_ack(0x13),
    ]


class FakeSocket:
    def __init__(
        self,
        frames: list[bytes | BaseException] | None = None,
        *,
        connect_error: BaseException | None = None,
        security_error: BaseException | None = None,
        l2cap_getsockopt_results: list[bytes | BaseException] | None = None,
        l2cap_setsockopt_error: BaseException | None = None,
        local_endpoint: tuple[object, ...] | None = None,
        close_results: list[BaseException | None] | None = None,
    ) -> None:
        self.frames = list(frames or [])
        self.connect_error = connect_error
        self.security_error = security_error
        self.l2cap_getsockopt_results = list(l2cap_getsockopt_results or [])
        self.l2cap_setsockopt_error = l2cap_setsockopt_error
        self.l2cap_options = l2cap_options()
        self.local_endpoint = local_endpoint
        self.close_results = list(close_results or [])
        self.bound_endpoint: tuple[str, int] | None = None
        self.events: list[object] = []
        self.sent: list[bytes] = []
        self.close_calls = 0

    def bind(self, endpoint: tuple[str, int]) -> None:
        self.bound_endpoint = endpoint
        self.events.append(("bind", endpoint))

    def setsockopt(self, level: int, option: int, value: bytes) -> None:
        if level == _LINUX_SOL_L2CAP and option == _LINUX_L2CAP_OPTIONS:
            self.events.append(("l2cap_setsockopt", level, option, value))
            if self.l2cap_setsockopt_error is not None:
                raise self.l2cap_setsockopt_error
            self.l2cap_options = bytes(value)
            return
        self.events.append(("security", level, option, value))
        if self.security_error is not None:
            raise self.security_error

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        self.events.append(("l2cap_getsockopt", level, option, size))
        if self.l2cap_getsockopt_results:
            result = self.l2cap_getsockopt_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return self.l2cap_options

    def settimeout(self, timeout: float | None) -> None:
        self.events.append(("timeout", timeout))

    def connect(self, target: tuple[str, int]) -> None:
        self.events.append(("connect", target))
        if self.connect_error is not None:
            raise self.connect_error

    def getsockname(self) -> tuple[object, ...]:
        self.events.append("getsockname")
        if self.local_endpoint is not None:
            return self.local_endpoint
        assert self.bound_endpoint is not None
        return self.bound_endpoint

    def send(self, payload: bytes) -> int:
        self.sent.append(payload)
        return len(payload)

    def recv(self, size: int) -> bytes:
        self.events.append(("recv", size))
        if not self.frames:
            raise TimeoutError
        frame = self.frames.pop(0)
        if isinstance(frame, BaseException):
            raise frame
        return frame

    def close(self) -> None:
        self.close_calls += 1
        self.events.append("close")
        if self.close_results:
            result = self.close_results.pop(0)
            if isinstance(result, BaseException):
                raise result


class FakeSocketModule:
    AF_BLUETOOTH = TEST_AF_BLUETOOTH
    SOCK_SEQPACKET = TEST_SOCK_SEQPACKET
    BTPROTO_L2CAP = TEST_BTPROTO_L2CAP
    SOL_BLUETOOTH = TEST_SOL_BLUETOOTH
    BT_SECURITY = TEST_BT_SECURITY
    BT_SECURITY_MEDIUM = TEST_BT_SECURITY_MEDIUM
    SOL_L2CAP = _LINUX_SOL_L2CAP
    L2CAP_OPTIONS = _LINUX_L2CAP_OPTIONS


class FakeClient:
    def __init__(
        self,
        preflight_state: BlueZCoexistenceState | None = None,
        snapshots: list[BlueZCoexistenceState | BaseException] | None = None,
    ) -> None:
        self.preflight_state = preflight_state or state()
        self.snapshots = list(snapshots or [state()] * 6)
        self.connect_calls = 0
        self.close_calls = 0
        self.snapshot_calls = 0
        self.preflight_require_connected: bool | None = None

    async def connect(self) -> None:
        self.connect_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    async def preflight(
        self, *, require_connected: bool = True
    ) -> BlueZCoexistenceState:
        self.preflight_require_connected = require_connected
        if require_connected and not self.preflight_state.device_connected:
            raise CoexistenceFailure(
                CoexistenceCategory.AIRPODS_NOT_CONNECTED,
                CoexistencePhase.PREFLIGHT,
            )
        return self.preflight_state

    async def snapshot(self, selected: AirPodsCandidate) -> BlueZCoexistenceState:
        self.snapshot_calls += 1
        self.assert_candidate = selected
        item = self.snapshots.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeRegistration:
    def __init__(
        self,
        *,
        register_error: BaseException | None = None,
        registered_count: int = 0,
    ) -> None:
        self.register_error = register_error
        self._registered_count = registered_count
        self.register_calls = 0
        self.unregister_calls = 0

    @property
    def registered_count(self) -> int:
        return self._registered_count

    async def register(self, selected_state: BlueZCoexistenceState) -> None:
        self.register_calls += 1
        self.selected_state = selected_state
        if self.register_error is not None:
            raise self.register_error

    async def unregister(self) -> None:
        self.unregister_calls += 1


class FailingHandshake:
    async def run_collected(self, transport: object) -> None:
        del transport
        raise RuntimeError("synthetic private detail")


class DescriptorTimeoutHandshake:
    def __init__(self, observation: HandshakeObservation) -> None:
        self.observation = observation

    async def run_collected(self, transport: object) -> None:
        transport.send_handshake_request()
        raise AAPDescriptorObservationTimeoutError(self.observation)


class MissingACKHandshake:
    async def run_collected(self, transport: object) -> None:
        transport.send_handshake_request()
        raise AAPHandshakeTimeoutError("exact ACK absent")


class UnusedHeartRate:
    async def run_collected(self, transport: object, handshake: object) -> None:
        del transport, handshake
        raise AssertionError("HR phase should not run")


class CoexistenceDryRunTests(unittest.IsolatedAsyncioTestCase):
    def test_parser_defaults_are_safe_and_bounded(self) -> None:
        args = build_parser().parse_args([])
        self.assertFalse(args.execute)
        self.assertEqual(args.samples, 5)
        self.assertEqual(args.connect_timeout, 10)
        self.assertEqual(args.descriptor_timeout, 3)
        self.assertFalse(args.audit_sdp)
        self.assertFalse(args.experimental_ack_only_hr)
        self.assertFalse(args.experimental_fresh_bluez_acl)
        experimental = build_parser().parse_args(
            ["--experimental-ack-only-hr"]
        )
        self.assertTrue(experimental.experimental_ack_only_hr)
        fresh = build_parser().parse_args(
            ["--experimental-fresh-bluez-acl"]
        )
        self.assertTrue(fresh.experimental_fresh_bluez_acl)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "--experimental-ack-only-hr",
                    "--experimental-fresh-bluez-acl",
                ]
            )
        audit = build_parser().parse_args(["--audit-sdp"])
        self.assertTrue(audit.audit_sdp)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["--execute", "--audit-sdp"])
        for value in ("0", "2", "11"):
            with self.subTest(value=value), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args(["--samples", value])
        for value in ("0", "0.9", "31"):
            with self.subTest(value=value), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args(["--descriptor-timeout", value])

    def test_main_default_is_deterministic_dry_run(self) -> None:
        first = StringIO()
        second = StringIO()
        with patch(
            "tools.probe_bluez_coexistence.DBusNextBlueZCoexistenceClient"
        ) as bluez, patch(
            "tools.probe_bluez_coexistence.KernelL2CAPTransport"
        ) as l2cap:
            self.assertEqual(main([], stream=first), 0)
            self.assertEqual(main([], stream=second), 0)
        bluez.assert_not_called()
        l2cap.assert_not_called()
        self.assertEqual(first.getvalue(), second.getvalue())
        self.assertIn("no Bluetooth or BlueZ state", first.getvalue())

    async def test_dry_run_never_calls_live_runner(self) -> None:
        runner = AsyncMock()
        output: list[str] = []
        status = await run_probe(
            execute=False, output=output.append, live_runner=runner
        )
        self.assertEqual(status, 0)
        runner.assert_not_awaited()
        self.assertTrue(
            any(message.startswith("Controller handoff: no") for message in output)
        )
        self.assertTrue(
            any("local RX MTU 2048" in message for message in output)
        )

    async def test_dry_run_prints_configured_descriptor_timeout(self) -> None:
        output: list[str] = []
        status = await run_probe(
            execute=False,
            handshake_timeout=9,
            descriptor_timeout=17,
            output=output.append,
        )
        self.assertEqual(status, 0)
        self.assertTrue(
            any(
                "AAP ACK=9s, AAP descriptor=17s" in message
                for message in output
            )
        )

    async def test_dry_run_reports_explicit_experimental_mode(self) -> None:
        output: list[str] = []
        status = await run_probe(
            execute=False,
            experimental_ack_only_hr=True,
            output=output.append,
        )
        self.assertEqual(status, 0)
        self.assertIn("Experimental ACK-only HR: enabled.", output)

    async def test_dry_run_reports_fresh_acl_requirements_without_live_work(
        self,
    ) -> None:
        runner = AsyncMock()
        output: list[str] = []
        status = await run_probe(
            execute=False,
            experimental_fresh_bluez_acl=True,
            output=output.append,
            live_runner=runner,
        )
        self.assertEqual(status, 0)
        runner.assert_not_awaited()
        self.assertIn("Experimental fresh BlueZ ACL: enabled.", output)
        rendered = "\n".join(output)
        self.assertIn("Device1.Connected=false", rendered)
        self.assertIn(
            "does not call BlueZ Connect, Disconnect, or ConnectProfile",
            rendered,
        )

    async def test_custom_descriptor_timeout_reaches_canonical_session(
        self,
    ) -> None:
        expected_result = object()
        with patch(
            "tools.probe_bluez_coexistence.AAPHandshakeSession"
        ) as handshake_class, patch(
            "tools.probe_bluez_coexistence.BlueZCoexistenceSession"
        ) as coexistence_class:
            coexistence_class.return_value.run = AsyncMock(
                return_value=expected_result
            )
            result = await run_live_probe(
                lambda message: None,
                5,
                5,
                10,
                7,
                19,
                12,
                False,
                False,
            )
        self.assertIs(result, expected_result)
        self.assertEqual(handshake_class.call_args.kwargs["ack_timeout"], 7)
        self.assertEqual(
            handshake_class.call_args.kwargs["descriptor_timeout"], 19
        )
        self.assertFalse(
            coexistence_class.call_args.kwargs[
                "experimental_fresh_bluez_acl"
            ]
        )

    async def test_fresh_acl_thirty_second_timeout_reaches_canonical_session(
        self,
    ) -> None:
        expected_result = object()
        with patch(
            "tools.probe_bluez_coexistence.AAPHandshakeSession"
        ) as handshake_class, patch(
            "tools.probe_bluez_coexistence.BlueZCoexistenceSession"
        ) as coexistence_class:
            coexistence_class.return_value.run = AsyncMock(
                return_value=expected_result
            )
            result = await run_live_probe(
                lambda message: None,
                5,
                5,
                10,
                5,
                30,
                12,
                False,
                True,
            )
        self.assertIs(result, expected_result)
        self.assertEqual(
            handshake_class.call_args.kwargs["descriptor_timeout"], 30
        )
        self.assertTrue(
            coexistence_class.call_args.kwargs[
                "experimental_fresh_bluez_acl"
            ]
        )
        self.assertIsNotNone(
            coexistence_class.call_args.kwargs["aap_ack_event"]
        )

    async def test_failure_has_phase_category_and_no_fallback(self) -> None:
        runner = AsyncMock(
            side_effect=CoexistenceFailure(
                CoexistenceCategory.L2CAP_CONNECT_FAILED,
                CoexistencePhase.L2CAP_CONNECTION,
                "errno EACCES (13)",
            )
        )
        output: list[str] = []
        status = await run_probe(
            execute=True,
            verbose=True,
            output=output.append,
            live_runner=runner,
        )
        self.assertEqual(status, 1)
        self.assertEqual(
            output,
            [
                "COEXISTENCE FAIL at l2cap_connection: l2cap_connect_failed",
                "Safe detail: errno EACCES (13)",
            ],
        )

    async def test_local_rx_failure_prints_safe_metadata_when_verbose(
        self,
    ) -> None:
        observation = KernelL2CAPLocalRXObservation(
            target_imtu=2048,
            options_source="linux-uapi-fallback",
            before_imtu=672,
            after_imtu=672,
            preserved_omtu=True,
            preserved_flush_to=True,
            preserved_mode=True,
            preserved_fcs=True,
            preserved_max_tx=True,
            preserved_txwin_size=True,
            verified=False,
        )
        runner = AsyncMock(
            side_effect=CoexistenceFailure(
                CoexistenceCategory.L2CAP_LOCAL_RX_MTU_FAILED,
                CoexistencePhase.L2CAP_CONNECTION,
                "errno ENOPROTOOPT (92)",
                l2cap_local_rx_observation=observation,
            )
        )
        output: list[str] = []
        status = await run_probe(
            execute=True,
            verbose=True,
            output=output.append,
            live_runner=runner,
        )
        rendered = "\n".join(output)
        self.assertEqual(status, 1)
        self.assertIn("l2cap_local_rx_mtu_failed", rendered)
        self.assertIn("target_imtu=2048", rendered)
        self.assertIn("before_imtu=672", rendered)
        self.assertIn("after_imtu=672", rendered)
        self.assertIn("verified=no", rendered)
        self.assertIn("Safe detail: errno ENOPROTOOPT (92)", rendered)

    def test_hr_progress_prints_uninterpreted_safe_fields(self) -> None:
        output: list[str] = []
        activation_event = asyncio.Event()
        transport = SimpleNamespace(arm_hr_stream_observation=Mock())
        progress = _heart_rate_progress(
            output.append, activation_event, transport
        )
        progress(HeartRateProgress.START_ACKNOWLEDGED, None)
        progress(
            HeartRateProgress.SAMPLE,
            HeartRateReport(
                bpm=0,
                aux=9,
                sequence=7,
                field_5=0,
                timestamp_ticks=123,
                flags=5,
                raw_report=bytes(18),
            ),
        )
        self.assertTrue(activation_event.is_set())
        transport.arm_hr_stream_observation.assert_called_once_with()
        self.assertIn(
            "HR sample 1: bpm=0, sequence=7, field_5=0, flags=5, "
            "raw_report_bytes=18",
            output,
        )

    async def test_descriptor_timeout_verbose_output_is_safe_and_structured(
        self,
    ) -> None:
        observation = descriptor_timeout_observation()
        runner = AsyncMock(
            side_effect=CoexistenceFailure(
                CoexistenceCategory.AAP_DESCRIPTOR_TIMEOUT,
                CoexistencePhase.AAP_HANDSHAKE,
                "PRIVATE_DESCRIPTOR_STRING",
                handshake_observation=observation,
            )
        )
        output: list[str] = []
        status = await run_probe(
            execute=True,
            verbose=True,
            output=output.append,
            live_runner=runner,
        )
        rendered = "\n".join(output)
        self.assertEqual(status, 1)
        self.assertIsInstance(
            observation.post_ack_frame_summaries[0].type_2b_summary,
            AAPType2BFrameSummary,
        )
        self.assertIn(
            "COEXISTENCE FAIL at aap_handshake: aap_descriptor_timeout",
            rendered,
        )
        for expected in (
            "exact_ack_observed=yes",
            "pre_ack_frames=2",
            "post_ack_frames=3",
            "receive_frames_dropped=1",
            "sensor_framework=yes",
            "heart_rate_service=no",
            "heart_rate=yes",
            "heartrate_access=no",
            "header_u16_2_3=0x0004",
            "header_u16_4_5=0x002B",
            "Type-0x002B structural summary:",
            "record_count_17=2",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("PRIVATE_DESCRIPTOR_STRING", rendered)
        self.assertNotIn(
            b"PRIVATE_DESCRIPTOR_STRING".hex(), rendered.lower()
        )

    async def test_descriptor_timeout_frame_summaries_are_canonically_bounded(
        self,
    ) -> None:
        summary = AAPFrameSummary.from_frame(b"\x00\x01\x02\x03\x04\x05")
        observation = HandshakeObservation(
            ack_observed=True,
            evidence=DescriptorEvidence(),
            pre_ack_frame_count=AAP_FRAME_SUMMARY_LIMIT + 10,
            post_ack_frame_count=1,
            pre_ack_frame_summaries=(summary,)
            * (AAP_FRAME_SUMMARY_LIMIT + 10),
            post_ack_frame_summaries=(summary,),
        )
        runner = AsyncMock(
            side_effect=CoexistenceFailure(
                CoexistenceCategory.AAP_DESCRIPTOR_TIMEOUT,
                CoexistencePhase.AAP_HANDSHAKE,
                handshake_observation=observation,
            )
        )
        output: list[str] = []
        await run_probe(
            execute=True,
            verbose=True,
            output=output.append,
            live_runner=runner,
        )
        summary_lines = [
            line
            for line in output
            if line.startswith(("  pre_ack_frame_", "  post_ack_frame_"))
        ]
        self.assertEqual(len(summary_lines), AAP_FRAME_SUMMARY_LIMIT)
        self.assertFalse(any("post_ack_frame_" in line for line in summary_lines))


class BlueZStateTests(unittest.IsolatedAsyncioTestCase):
    def managed_objects(self, *, connected: bool, powered: bool = True):
        selected = candidate()
        return {
            selected.adapter_path: {
                "org.bluez.Adapter1": {
                    "Address": SimpleNamespace(value="00:11:22:33:44:55"),
                    "Powered": SimpleNamespace(value=powered),
                    "Modalias": SimpleNamespace(value="usb:v1234p5678d9ABC"),
                    "UUIDs": SimpleNamespace(value=list(ALL_COMPATIBILITY_UUIDS)),
                }
            },
            selected.object_path: {
                "org.bluez.Device1": {
                    "Address": SimpleNamespace(value="AA:BB:CC:DD:EE:FF"),
                    "Adapter": SimpleNamespace(value=selected.adapter_path),
                    "Name": SimpleNamespace(value="AirPods Pro"),
                    "Alias": SimpleNamespace(value="Test AirPods"),
                    "Paired": SimpleNamespace(value=True),
                    "Connected": SimpleNamespace(value=connected),
                }
            },
        }

    async def test_connected_powered_preflight_is_accepted(self) -> None:
        client = DBusNextBlueZCoexistenceClient()
        client.get_managed_objects = AsyncMock(
            return_value=self.managed_objects(connected=True)
        )
        result = await client.preflight()
        self.assertTrue(result.device_connected)
        self.assertTrue(result.adapter_powered)
        self.assertEqual(result.adapter_uuids, ALL_COMPATIBILITY_UUIDS)

    async def test_disconnected_preflight_is_rejected(self) -> None:
        client = DBusNextBlueZCoexistenceClient()
        client.get_managed_objects = AsyncMock(
            return_value=self.managed_objects(connected=False)
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await client.preflight()
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.AIRPODS_NOT_CONNECTED,
        )

    async def test_fresh_acl_preflight_accepts_paired_disconnected_device(
        self,
    ) -> None:
        client = DBusNextBlueZCoexistenceClient()
        client.get_managed_objects = AsyncMock(
            return_value=self.managed_objects(connected=False)
        )
        result = await client.preflight(require_connected=False)
        self.assertFalse(result.device_connected)
        self.assertTrue(result.adapter_powered)

    async def test_unpowered_preflight_is_rejected(self) -> None:
        client = DBusNextBlueZCoexistenceClient()
        client.get_managed_objects = AsyncMock(
            return_value=self.managed_objects(connected=True, powered=False)
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await client.preflight()
        self.assertEqual(
            raised.exception.category, CoexistenceCategory.PREFLIGHT_FAILED
        )

    async def test_connect_rollback_failure_remains_unproven(self) -> None:
        bus = SimpleNamespace(
            connect=AsyncMock(),
            disconnect=Mock(side_effect=[RuntimeError("disconnect failed"), None]),
        )
        bus.connect.return_value = bus
        client = DBusNextBlueZCoexistenceClient()
        client.get_managed_objects = AsyncMock(
            side_effect=TimeoutError("managed objects unavailable")
        )

        with patch("dbus_next.aio.MessageBus", return_value=bus):
            with self.assertRaises(CoexistenceFailure) as raised:
                await client.connect()

        self.assertEqual(
            raised.exception.category, CoexistenceCategory.BLUEZ_NOT_AVAILABLE
        )
        self.assertFalse(client.cleanup_complete)
        client.close()
        self.assertFalse(client.cleanup_complete)
        self.assertEqual(bus.disconnect.call_count, 2)

    async def test_profile_export_rollback_failure_preserves_primary(self) -> None:
        manager = SimpleNamespace(
            call_register_profile=AsyncMock(
                side_effect=TimeoutError("registration reply timed out")
            )
        )
        bus = SimpleNamespace(
            export=Mock(),
            unexport=Mock(side_effect=RuntimeError("unexport failed")),
        )
        client = DBusNextBlueZCoexistenceClient()
        client._bus = bus
        client._profile_manager = manager
        client._variant = lambda _signature, value: value
        record = build_bluez_sdp_service_records(
            USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)
        )[0]

        with self.assertRaises(TimeoutError) as raised:
            await client.register_profile("/test/profile", SimpleNamespace(), record)

        self.assertIn("profile export rollback", " ".join(raised.exception.__notes__))
        self.assertFalse(client.cleanup_complete)

    async def test_unregister_does_not_exist_is_positive_release(self) -> None:
        manager = SimpleNamespace(
            call_unregister_profile=AsyncMock(
                side_effect=DBusError(
                    "org.bluez.Error.DoesNotExist", "already removed"
                )
            )
        )
        bus = SimpleNamespace(unexport=Mock())
        client = DBusNextBlueZCoexistenceClient()
        client._bus = bus
        client._profile_manager = manager
        profile = SimpleNamespace()

        await client.unregister_profile("/test/profile", profile)

        bus.unexport.assert_called_once_with("/test/profile", profile)


class ProfileLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_records_register_and_unregister_once_each(self) -> None:
        profile_client = SimpleNamespace(
            register_profile=AsyncMock(), unregister_profile=AsyncMock()
        )
        registration = BlueZCompatibilityRegistration(profile_client)
        missing_state = BlueZCoexistenceState(candidate(), True, True, frozenset())
        await registration.register(missing_state)
        self.assertEqual(profile_client.register_profile.await_count, 4)
        self.assertEqual(registration.registered_count, 4)
        self.assertFalse(registration.cleanup_complete)
        first_profile = profile_client.register_profile.await_args_list[0].args[1]
        first_profile.Release()
        first_profile.Release()
        self.assertTrue(first_profile.released)
        await registration.unregister()
        self.assertEqual(profile_client.unregister_profile.await_count, 4)
        self.assertTrue(registration.cleanup_complete)
        await registration.unregister()
        self.assertEqual(profile_client.unregister_profile.await_count, 4)
        self.assertTrue(registration.cleanup_complete)

    async def test_existing_adapter_identity_needs_no_duplicate_profile(self) -> None:
        profile_client = SimpleNamespace(
            register_profile=AsyncMock(), unregister_profile=AsyncMock()
        )
        registration = BlueZCompatibilityRegistration(profile_client)
        await registration.register(state())
        profile_client.register_profile.assert_not_awaited()
        await registration.unregister()
        profile_client.unregister_profile.assert_not_awaited()

    async def test_complete_uuid_set_does_not_require_adapter_modalias(self) -> None:
        profile_client = SimpleNamespace(
            register_profile=AsyncMock(), unregister_profile=AsyncMock()
        )
        registration = BlueZCompatibilityRegistration(profile_client)
        no_identity_state = BlueZCoexistenceState(
            candidate(adapter_modalias=None),
            True,
            True,
            ALL_COMPATIBILITY_UUIDS,
        )
        await registration.register(no_identity_state)
        self.assertEqual(registration.registered_count, 0)
        profile_client.register_profile.assert_not_awaited()
        await registration.unregister()
        profile_client.unregister_profile.assert_not_awaited()

    async def test_partial_registration_failure_cleans_prior_profile(self) -> None:
        profile_client = SimpleNamespace(
            register_profile=AsyncMock(
                side_effect=[None, PermissionError(13, "private path")]
            ),
            unregister_profile=AsyncMock(),
        )
        registration = BlueZCompatibilityRegistration(profile_client)
        missing_state = BlueZCoexistenceState(candidate(), True, True, frozenset())
        with self.assertRaises(CoexistenceFailure) as raised:
            await registration.register(missing_state)
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.PROFILE_REGISTRATION_FAILED,
        )
        profile_client.unregister_profile.assert_awaited_once()
        self.assertTrue(registration.cleanup_complete)

    async def test_partial_registration_rollback_failure_remains_unproven(
        self,
    ) -> None:
        profile_client = SimpleNamespace(
            register_profile=AsyncMock(
                side_effect=[None, TimeoutError("second registration failed")]
            ),
            unregister_profile=AsyncMock(
                side_effect=[RuntimeError("rollback unregister failed"), None]
            ),
        )
        registration = BlueZCompatibilityRegistration(profile_client)
        missing_state = BlueZCoexistenceState(candidate(), True, True, frozenset())

        with self.assertRaises(CoexistenceFailure) as raised:
            await registration.register(missing_state)

        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.PROFILE_REGISTRATION_FAILED,
        )
        self.assertFalse(registration.cleanup_complete)
        await registration.unregister()
        self.assertFalse(registration.cleanup_complete)
        self.assertEqual(profile_client.unregister_profile.await_count, 2)

    async def test_registration_cancellation_preserves_cleanup_uncertainty(
        self,
    ) -> None:
        cancellation = asyncio.CancelledError()
        profile_client = SimpleNamespace(
            register_profile=AsyncMock(side_effect=[None, cancellation]),
            unregister_profile=AsyncMock(
                side_effect=[RuntimeError("rollback unregister failed"), None]
            ),
        )
        registration = BlueZCompatibilityRegistration(profile_client)
        missing_state = BlueZCoexistenceState(candidate(), True, True, frozenset())

        with self.assertRaises(asyncio.CancelledError) as raised:
            await registration.register(missing_state)

        self.assertIs(raised.exception, cancellation)
        self.assertFalse(registration.cleanup_complete)
        await registration.unregister()
        self.assertFalse(registration.cleanup_complete)

    def test_bluez_xml_records_are_well_formed_and_canonical(self) -> None:
        records = build_bluez_sdp_service_records(
            USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)
        )
        self.assertEqual(len(records), 4)
        roots = [ElementTree.fromstring(record.service_record) for record in records]
        self.assertTrue(all(root.tag == "record" for root in roots))
        joined = "".join(record.service_record for record in records)
        values = (
            "0x1234",
            "0x5678",
            "0x9abc",
            "0x0d",
            "0x0019",
            "0x0103",
            "0x0106",
        )
        for value in values:
            self.assertIn(value, joined)


class KernelL2CAPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_bind_security_connect_and_route_order(self) -> None:
        fake = FakeSocket(local_endpoint=(LOCAL_ADAPTER_ADDRESS.lower(), 0))
        factory = Mock(return_value=fake)
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule, socket_factory=factory
        )
        await transport.open(LOCAL_ADAPTER_ADDRESS, REMOTE_AIRPODS_ADDRESS)
        factory.assert_called_once_with(
            FakeSocketModule.AF_BLUETOOTH,
            FakeSocketModule.SOCK_SEQPACKET,
            FakeSocketModule.BTPROTO_L2CAP,
        )
        bind_index = next(
            index for index, event in enumerate(fake.events)
            if isinstance(event, tuple) and event[0] == "bind"
        )
        security_index = next(
            index for index, event in enumerate(fake.events)
            if isinstance(event, tuple) and event[0] == "security"
        )
        options_read_indexes = [
            index
            for index, event in enumerate(fake.events)
            if isinstance(event, tuple) and event[0] == "l2cap_getsockopt"
        ]
        options_set_index = next(
            index
            for index, event in enumerate(fake.events)
            if isinstance(event, tuple) and event[0] == "l2cap_setsockopt"
        )
        connect_index = next(
            index for index, event in enumerate(fake.events)
            if isinstance(event, tuple) and event[0] == "connect"
        )
        route_index = fake.events.index("getsockname")
        self.assertEqual(
            fake.events[bind_index],
            ("bind", (LOCAL_ADAPTER_ADDRESS, 0)),
        )
        self.assertLess(bind_index, security_index)
        self.assertLess(security_index, options_read_indexes[0])
        self.assertLess(options_read_indexes[0], options_set_index)
        self.assertLess(options_set_index, options_read_indexes[1])
        self.assertLess(options_read_indexes[1], connect_index)
        self.assertLess(connect_index, route_index)
        security = fake.events[security_index]
        self.assertEqual(
            security[1:3],
            (FakeSocketModule.SOL_BLUETOOTH, FakeSocketModule.BT_SECURITY),
        )
        self.assertEqual(
            security[3], bytes((FakeSocketModule.BT_SECURITY_MEDIUM, 0))
        )
        self.assertEqual(
            fake.events[connect_index],
            ("connect", (REMOTE_AIRPODS_ADDRESS, AAP_PSM)),
        )
        self.assertEqual(len(options_read_indexes), 2)
        self.assertEqual(
            fake.events[options_read_indexes[0]][1:],
            (_LINUX_SOL_L2CAP, _LINUX_L2CAP_OPTIONS, _L2CAP_OPTIONS_SIZE),
        )
        configured = fake.events[options_set_index][3]
        before = l2cap_options()
        self.assertEqual(
            configured[:_L2CAP_IMTU_OFFSET], before[:_L2CAP_IMTU_OFFSET]
        )
        self.assertEqual(
            configured[_L2CAP_IMTU_OFFSET + 2 :],
            before[_L2CAP_IMTU_OFFSET + 2 :],
        )
        self.assertEqual(
            struct.unpack_from("@H", configured, _L2CAP_IMTU_OFFSET)[0],
            _AAP_LOCAL_RX_IMTU,
        )
        observation = transport.local_rx_observation
        self.assertEqual(observation.target_imtu, 2048)
        self.assertEqual(observation.before_imtu, 672)
        self.assertEqual(observation.after_imtu, 2048)
        self.assertEqual(observation.options_source, "python-socket")
        self.assertTrue(observation.verified)
        self.assertTrue(observation.preserved_omtu)
        self.assertTrue(observation.preserved_flush_to)
        self.assertTrue(observation.preserved_mode)
        self.assertTrue(observation.preserved_fcs)
        self.assertTrue(observation.preserved_max_tx)
        self.assertTrue(observation.preserved_txwin_size)
        transport.close()
        transport.close()
        self.assertEqual(fake.close_calls, 1)
        self.assertTrue(transport.cleanup_complete)

    def test_native_l2cap_options_layout_matches_linux_abi(self) -> None:
        self.assertEqual(ctypes.sizeof(_NativeL2CAPOptions), 12)
        self.assertEqual(_L2CAP_OPTIONS_SIZE, 12)
        self.assertEqual(
            {
                name: getattr(_NativeL2CAPOptions, name).offset
                for name, _ in _NativeL2CAPOptions._fields_
            },
            {
                "omtu": 0,
                "imtu": 2,
                "flush_to": 4,
                "mode": 6,
                "fcs": 7,
                "max_tx": 8,
                "txwin_size": 10,
            },
        )
        self.assertFalse(
            {"raw", "payload", "bytes"}.intersection(
                field.name for field in fields(KernelL2CAPLocalRXObservation)
            )
        )

    async def test_python_constant_gap_uses_verified_linux_uapi_fallback(
        self,
    ) -> None:
        module = SimpleNamespace(
            AF_BLUETOOTH=FakeSocketModule.AF_BLUETOOTH,
            SOCK_SEQPACKET=FakeSocketModule.SOCK_SEQPACKET,
            BTPROTO_L2CAP=FakeSocketModule.BTPROTO_L2CAP,
            SOL_BLUETOOTH=FakeSocketModule.SOL_BLUETOOTH,
            BT_SECURITY=FakeSocketModule.BT_SECURITY,
            BT_SECURITY_MEDIUM=FakeSocketModule.BT_SECURITY_MEDIUM,
            SOL_L2CAP=_LINUX_SOL_L2CAP,
        )
        fake = FakeSocket()
        transport = KernelL2CAPTransport(
            socket_module=module, socket_factory=Mock(return_value=fake)
        )
        await transport.open(LOCAL_ADAPTER_ADDRESS, REMOTE_AIRPODS_ADDRESS)
        self.assertEqual(
            transport.local_rx_observation.options_source,
            "linux-uapi-fallback",
        )
        transport.close()

    async def test_local_rx_option_failures_close_and_prevent_connect(
        self,
    ) -> None:
        cases = {
            "getsockopt": FakeSocket(
                l2cap_getsockopt_results=[OSError(92, "private")]
            ),
            "setsockopt": FakeSocket(
                l2cap_setsockopt_error=OSError(22, "private")
            ),
            "malformed": FakeSocket(l2cap_getsockopt_results=[b"short"]),
            "imtu_mismatch": FakeSocket(
                l2cap_getsockopt_results=[
                    l2cap_options(),
                    l2cap_options(imtu=672),
                ]
            ),
            "preserved_field_mismatch": FakeSocket(
                l2cap_getsockopt_results=[
                    l2cap_options(),
                    l2cap_options(imtu=2048, flush_to=30),
                ]
            ),
        }
        for name, fake in cases.items():
            with self.subTest(name=name):
                transport = KernelL2CAPTransport(
                    socket_module=FakeSocketModule,
                    socket_factory=Mock(return_value=fake),
                )
                with self.assertRaises(CoexistenceFailure) as raised:
                    await transport.open(
                        LOCAL_ADAPTER_ADDRESS, REMOTE_AIRPODS_ADDRESS
                    )
                self.assertEqual(
                    raised.exception.category,
                    CoexistenceCategory.L2CAP_LOCAL_RX_MTU_FAILED,
                )
                self.assertEqual(fake.close_calls, 1)
                self.assertFalse(
                    any(
                        isinstance(event, tuple) and event[0] == "connect"
                        for event in fake.events
                    )
                )
                transport.close()
                self.assertEqual(fake.close_calls, 1)

    async def test_conflicting_exposed_l2cap_constant_fails_closed(self) -> None:
        module = SimpleNamespace(
            AF_BLUETOOTH=FakeSocketModule.AF_BLUETOOTH,
            SOCK_SEQPACKET=FakeSocketModule.SOCK_SEQPACKET,
            BTPROTO_L2CAP=FakeSocketModule.BTPROTO_L2CAP,
            SOL_BLUETOOTH=FakeSocketModule.SOL_BLUETOOTH,
            BT_SECURITY=FakeSocketModule.BT_SECURITY,
            BT_SECURITY_MEDIUM=FakeSocketModule.BT_SECURITY_MEDIUM,
            SOL_L2CAP=99,
            L2CAP_OPTIONS=_LINUX_L2CAP_OPTIONS,
        )
        fake = FakeSocket()
        transport = KernelL2CAPTransport(
            socket_module=module, socket_factory=Mock(return_value=fake)
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await transport.open(
                LOCAL_ADAPTER_ADDRESS, REMOTE_AIRPODS_ADDRESS
            )
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.L2CAP_LOCAL_RX_MTU_FAILED,
        )
        self.assertEqual(fake.close_calls, 1)
        self.assertFalse(any(event[0] == "connect" for event in fake.events))

    async def test_stream_observation_uses_canonical_receive_path_and_bounds_summaries(
        self,
    ) -> None:
        start_ack = service_ack(0x13)
        marker_frame = b"prefix" + HEART_RATE_MARKER + b"truncated"
        non_marker_frame = b"bounded-control-frame"
        extra_frames = [bytes((index,)) for index in range(10)]
        stop_ack = service_ack(0x13)
        observed_frames = [marker_frame, non_marker_frame, *extra_frames]
        fake = FakeSocket([start_ack, *observed_frames, stop_ack])
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake),
        )
        await transport.open(LOCAL_ADAPTER_ADDRESS, REMOTE_AIRPODS_ADDRESS)
        async with transport.collect():
            transport.send_handshake_request()
            self.assertEqual(await transport.receive(1), start_ack)
            before_arm = transport.hr_stream_observation
            self.assertFalse(before_arm.observation_armed)
            self.assertEqual(before_arm.frames_observed, 0)

            transport.arm_hr_stream_observation()
            for frame in observed_frames:
                self.assertEqual(await transport.receive(1), frame)

            transport.send_heart_rate_command(HeartRateCommand.STOP_HR)
            after_disarm = transport.hr_stream_observation
            self.assertTrue(after_disarm.observation_armed)
            self.assertTrue(after_disarm.observation_cleanly_disarmed)
            self.assertEqual(after_disarm.frames_observed, len(observed_frames))
            self.assertEqual(after_disarm.frames_with_hr_marker, 1)
            self.assertEqual(
                after_disarm.frames_without_hr_marker,
                len(observed_frames) - 1,
            )
            self.assertEqual(
                len(after_disarm.frame_summaries),
                DEFAULT_CONTROL_SUMMARY_LIMIT,
            )
            self.assertTrue(
                after_disarm.frame_summaries[0].heart_rate_marker_present
            )
            self.assertEqual(await transport.receive(1), stop_ack)

        final = transport.hr_stream_observation
        self.assertEqual(final, after_disarm)
        recv_events = [event for event in fake.events if event[0] == "recv"]
        self.assertEqual(len(recv_events), len(observed_frames) + 2)
        self.assertFalse(
            {"frame", "payload", "raw", "bytes"}.intersection(
                field.name for field in fields(CoexistenceHRStreamObservation)
            )
        )
        self.assertNotIn(marker_frame.hex(), repr(final))
        transport.close()

    async def test_stream_observation_is_single_use_and_summary_limit_is_bounded(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            KernelL2CAPTransport(stream_summary_limit=0)
        with self.assertRaises(ValueError):
            KernelL2CAPTransport(
                stream_summary_limit=DEFAULT_CONTROL_SUMMARY_LIMIT + 1
            )
        transport = KernelL2CAPTransport()
        transport.arm_hr_stream_observation()
        with self.assertRaises(RuntimeError):
            transport.arm_hr_stream_observation()

    async def test_security_failure_is_categorized_and_closed(self) -> None:
        fake = FakeSocket(security_error=PermissionError(13, "secret detail"))
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule, socket_factory=Mock(return_value=fake)
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await transport.open(
                LOCAL_ADAPTER_ADDRESS, REMOTE_AIRPODS_ADDRESS
            )
        self.assertEqual(
            raised.exception.category, CoexistenceCategory.L2CAP_SECURITY_FAILED
        )
        self.assertEqual(fake.close_calls, 1)

    async def test_connect_failure_is_categorized_and_closed(self) -> None:
        fake = FakeSocket(connect_error=PermissionError(13, "secret detail"))
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule, socket_factory=Mock(return_value=fake)
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await transport.open(
                LOCAL_ADAPTER_ADDRESS, REMOTE_AIRPODS_ADDRESS
            )
        self.assertEqual(
            raised.exception.category, CoexistenceCategory.L2CAP_CONNECT_FAILED
        )
        self.assertEqual(raised.exception.detail, "errno EACCES (13)")
        self.assertEqual(fake.close_calls, 1)

    async def test_open_rollback_close_failure_remains_unproven(self) -> None:
        fake = FakeSocket(
            connect_error=ConnectionError("peer unavailable"),
            close_results=[RuntimeError("socket close failed"), None],
        )
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake),
        )

        with self.assertRaises(CoexistenceFailure) as raised:
            await transport.open(
                LOCAL_ADAPTER_ADDRESS, REMOTE_AIRPODS_ADDRESS
            )

        self.assertEqual(
            raised.exception.category, CoexistenceCategory.L2CAP_CONNECT_FAILED
        )
        self.assertFalse(transport.cleanup_complete)
        transport.close()
        self.assertFalse(transport.cleanup_complete)
        self.assertEqual(fake.close_calls, 2)

    async def test_open_cancellation_preserves_cleanup_uncertainty(self) -> None:
        cancellation = asyncio.CancelledError()
        fake = FakeSocket(
            connect_error=cancellation,
            close_results=[RuntimeError("socket close failed"), None],
        )
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake),
        )

        with self.assertRaises(asyncio.CancelledError) as raised:
            await transport.open(
                LOCAL_ADAPTER_ADDRESS, REMOTE_AIRPODS_ADDRESS
            )

        self.assertIs(raised.exception, cancellation)
        self.assertFalse(transport.cleanup_complete)
        transport.close()
        self.assertFalse(transport.cleanup_complete)

    async def test_host_missing_bluetooth_api_is_socket_failure(self) -> None:
        transport = KernelL2CAPTransport(
            socket_module=SimpleNamespace(), socket_factory=Mock()
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await transport.open(
                LOCAL_ADAPTER_ADDRESS, REMOTE_AIRPODS_ADDRESS
            )
        self.assertEqual(
            raised.exception.category, CoexistenceCategory.L2CAP_SOCKET_FAILED
        )

    async def test_route_mismatch_is_categorized_and_closed_once(self) -> None:
        fake = FakeSocket(local_endpoint=("66:77:88:99:AA:BB", 0))
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake),
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await transport.open(
                LOCAL_ADAPTER_ADDRESS, REMOTE_AIRPODS_ADDRESS
            )
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.L2CAP_ROUTE_MISMATCH,
        )
        self.assertEqual(
            raised.exception.phase, CoexistencePhase.L2CAP_CONNECTION
        )
        self.assertEqual(fake.close_calls, 1)
        transport.close()
        self.assertEqual(fake.close_calls, 1)


class CoexistenceOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def experimental_session(
        self,
        *,
        observation: HandshakeObservation | None = None,
        frames: list[bytes | BaseException] | None = None,
        client: FakeClient | None = None,
        heart_rate: object | None = None,
        handshake: object | None = None,
    ):
        activation_event = asyncio.Event()
        registration = FakeRegistration()
        fake_socket = FakeSocket(
            frames if frames is not None else activation_frames()
        )
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake_socket),
        )

        def progress(
            event: HeartRateProgress, report: HeartRateReport | None
        ) -> None:
            del report
            if event is HeartRateProgress.START_ACKNOWLEDGED:
                transport.arm_hr_stream_observation()
                activation_event.set()

        selected_heart_rate = heart_rate or HeartRateActivationSession(
            sample_target=5,
            minimum_bootstrap_seconds=0,
            progress=progress,
        )
        selected_handshake = handshake or DescriptorTimeoutHandshake(
            observation
            or descriptor_timeout_observation(receive_frames_dropped=0)
        )
        output: list[str] = []
        session = BlueZCoexistenceSession(
            client or FakeClient(),
            registration,
            transport,
            selected_handshake,
            selected_heart_rate,
            hr_activation_event=activation_event,
            experimental_ack_only_hr=True,
            output=output.append,
        )
        return session, registration, fake_socket, selected_heart_rate, output

    async def successful_session(
        self,
        *,
        client: FakeClient | None = None,
        registration: FakeRegistration | None = None,
        frames: list[bytes] | None = None,
    ):
        selected_client = client or FakeClient()
        selected_registration = registration or FakeRegistration()
        fake_socket = FakeSocket(frames or successful_frames())
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake_socket),
        )
        reports: list[HeartRateReport] = []
        output: list[str] = []

        def progress(event: object, report: HeartRateReport | None) -> None:
            if event is HeartRateProgress.START_ACKNOWLEDGED:
                transport.arm_hr_stream_observation()
            if report is not None:
                reports.append(report)

        session = BlueZCoexistenceSession(
            selected_client,
            selected_registration,
            transport,
            AAPHandshakeSession(),
            HeartRateActivationSession(
                sample_target=5,
                minimum_bootstrap_seconds=0,
                progress=progress,
            ),
            output=output.append,
        )
        result = await session.run()
        return (
            result,
            selected_client,
            selected_registration,
            fake_socket,
            reports,
            output,
        )

    async def test_real_canonical_sessions_collect_exactly_five_reports(self) -> None:
        result, client, registration, fake_socket, reports, output = (
            await self.successful_session()
        )
        self.assertEqual(len(result.heart_rate.samples), 5)
        self.assertEqual(len(reports), 5)
        self.assertTrue(all(isinstance(item, HeartRateReport) for item in reports))
        self.assertEqual([item.bpm for item in reports], [0, 10, 20, 30, 40])
        self.assertTrue(all(len(item.raw_report) == 18 for item in reports))
        self.assertTrue(result.hr_stream_observation.observation_armed)
        self.assertTrue(
            result.hr_stream_observation.observation_cleanly_disarmed
        )
        self.assertEqual(result.hr_stream_observation.frames_observed, 5)
        self.assertEqual(result.hr_stream_observation.frames_with_hr_marker, 5)
        self.assertEqual(fake_socket.sent[0], AAP_HANDSHAKE_REQUEST)
        self.assertEqual(
            fake_socket.sent[1:],
            [command.payload for command in HeartRateCommand],
        )
        self.assertIn(
            ("bind", (LOCAL_ADAPTER_ADDRESS, 0)), fake_socket.events
        )
        self.assertIn(
            ("connect", (REMOTE_AIRPODS_ADDRESS, AAP_PSM)),
            fake_socket.events,
        )
        self.assertEqual(registration.register_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(client.close_calls, 1)
        self.assertEqual(
            [phase for phase, connected in result.checkpoints],
            list(CoexistencePhase),
        )
        self.assertTrue(all(connected for _, connected in result.checkpoints))
        self.assertIn(
            "Kernel L2CAP local adapter: selected BlueZ adapter confirmed",
            output,
        )
        self.assertIn("KERNEL L2CAP LOCAL RX SUMMARY", output)
        self.assertIn("  target_imtu=2048", output)
        self.assertIn("  before_imtu=672", output)
        self.assertIn("  after_imtu=2048", output)
        self.assertIn("  preserved_omtu=yes", output)
        self.assertIn("  preserved_flush_to=yes", output)
        self.assertIn("  preserved_mode=yes", output)
        self.assertIn("  preserved_fcs=yes", output)
        self.assertIn("  preserved_max_tx=yes", output)
        self.assertIn("  preserved_txwin_size=yes", output)
        self.assertIn("  verified=yes", output)
        self.assertEqual(result.local_rx_observation.after_imtu, 2048)
        self.assertIn(
            "Required compatibility UUID classes already present: 4/4",
            output,
        )
        self.assertIn("Temporary BlueZ profiles registered: 0", output)
        phase_one_output = "\n".join(
            line
            for line in output
            if "compatibility UUID" in line or "profiles registered" in line
        )
        self.assertNotIn(LOCAL_ADAPTER_ADDRESS, phase_one_output)
        self.assertNotIn(REMOTE_AIRPODS_ADDRESS, phase_one_output)

    async def test_fresh_acl_refuses_connected_device_before_l2cap_or_aap(
        self,
    ) -> None:
        client = FakeClient(preflight_state=state(), snapshots=[state()])
        registration = FakeRegistration()
        factory = Mock(return_value=FakeSocket())
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule, socket_factory=factory
        )
        handshake = SimpleNamespace(run_collected=AsyncMock())
        heart_rate = SimpleNamespace(run_collected=AsyncMock())
        output: list[str] = []
        session = BlueZCoexistenceSession(
            client,
            registration,
            transport,
            handshake,
            heart_rate,
            experimental_fresh_bluez_acl=True,
            output=output.append,
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.FRESH_ACL_REQUIRES_DISCONNECTED_DEVICE,
        )
        self.assertEqual(raised.exception.phase, CoexistencePhase.PREFLIGHT)
        self.assertIs(client.preflight_require_connected, False)
        self.assertEqual(registration.register_calls, 0)
        factory.assert_not_called()
        handshake.run_collected.assert_not_awaited()
        heart_rate.run_collected.assert_not_awaited()
        self.assertIn("initial_link_state=connected", output)

    async def test_fresh_acl_disconnected_preflight_uses_canonical_path(
        self,
    ) -> None:
        disconnected = state(connected=False)
        connected = state()
        client = FakeClient(
            preflight_state=disconnected,
            snapshots=[
                disconnected,
                connected,
                connected,
                connected,
                connected,
                connected,
                connected,
            ],
        )
        registration = FakeRegistration()
        fake_socket = FakeSocket(successful_frames())
        factory = Mock(return_value=fake_socket)
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule, socket_factory=factory
        )
        ack_event = asyncio.Event()

        def aap_progress(event: AAPProgress) -> None:
            if event is AAPProgress.ACK_OBSERVED:
                ack_event.set()

        def heart_rate_progress(
            event: HeartRateProgress, report: HeartRateReport | None
        ) -> None:
            del report
            if event is HeartRateProgress.START_ACKNOWLEDGED:
                transport.arm_hr_stream_observation()

        output: list[str] = []
        session = BlueZCoexistenceSession(
            client,
            registration,
            transport,
            AAPHandshakeSession(progress=aap_progress),
            HeartRateActivationSession(
                sample_target=5,
                minimum_bootstrap_seconds=0,
                progress=heart_rate_progress,
            ),
            aap_ack_event=ack_event,
            experimental_fresh_bluez_acl=True,
            output=output.append,
        )
        result = await session.run()

        self.assertIs(client.preflight_require_connected, False)
        self.assertEqual(len(result.heart_rate.samples), 5)
        self.assertTrue(result.descriptor_handshake_complete)
        self.assertFalse(result.experimental_ack_only_hr_used)
        self.assertEqual(fake_socket.bound_endpoint, (LOCAL_ADAPTER_ADDRESS, 0))
        self.assertIn(
            ("connect", (REMOTE_AIRPODS_ADDRESS, AAP_PSM)),
            fake_socket.events,
        )
        event_names = [
            event[0] if isinstance(event, tuple) else event
            for event in fake_socket.events
        ]
        self.assertLess(event_names.index("bind"), event_names.index("security"))
        self.assertLess(
            event_names.index("security"),
            event_names.index("l2cap_getsockopt"),
        )
        self.assertLess(
            event_names.index("l2cap_getsockopt"),
            event_names.index("l2cap_setsockopt"),
        )
        self.assertLess(
            event_names.index("l2cap_setsockopt"), event_names.index("connect")
        )
        self.assertLess(
            event_names.index("connect"), event_names.index("getsockname")
        )
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(result.local_rx_observation.after_imtu, 2048)
        self.assertTrue(result.local_rx_observation.verified)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(
            sum(
                phase is CoexistencePhase.AAP_HANDSHAKE
                for phase, _ in result.checkpoints
            ),
            2,
        )
        rendered = "\n".join(output)
        self.assertIn("initial_link_state=disconnected", rendered)
        self.assertIn("fresh_kernel_l2cap_connect=success", rendered)
        self.assertIn("bluez_connected_after_l2cap=yes", rendered)
        self.assertIn(
            "after_exact_aap_ack: Device1.Connected=true", rendered
        )
        self.assertIn("descriptor_complete=yes", rendered)
        self.assertIn(
            "after_descriptor_phase: Device1.Connected=true", rendered
        )

        probe_output: list[str] = []
        runner = AsyncMock(return_value=result)
        status = await run_probe(
            execute=True,
            experimental_fresh_bluez_acl=True,
            output=probe_output.append,
            live_runner=runner,
        )
        self.assertEqual(status, 0)
        self.assertIs(runner.await_args.args[-2], False)
        self.assertIs(runner.await_args.args[-1], True)
        self.assertEqual(
            probe_output[-1],
            "COEXISTENCE FRESH BLUEZ ACL EXPERIMENT PASS",
        )

    async def test_fresh_acl_descriptor_timeout_stops_before_hr(self) -> None:
        disconnected = state(connected=False)
        connected = state()
        client = FakeClient(
            preflight_state=disconnected,
            snapshots=[
                disconnected,
                connected,
                connected,
                connected,
                connected,
            ],
        )
        registration = FakeRegistration()
        fake_socket = FakeSocket([AAP_HANDSHAKE_ACK, TimeoutError()])
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake_socket),
        )
        ack_event = asyncio.Event()

        def aap_progress(event: AAPProgress) -> None:
            if event is AAPProgress.ACK_OBSERVED:
                ack_event.set()

        heart_rate = SimpleNamespace(run_collected=AsyncMock())
        output: list[str] = []
        session = BlueZCoexistenceSession(
            client,
            registration,
            transport,
            AAPHandshakeSession(progress=aap_progress),
            heart_rate,
            aap_ack_event=ack_event,
            experimental_fresh_bluez_acl=True,
            output=output.append,
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.AAP_DESCRIPTOR_TIMEOUT,
        )
        heart_rate.run_collected.assert_not_awaited()
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        rendered = "\n".join(output)
        self.assertIn("exact_ack_observed=yes", rendered)
        self.assertIn("descriptor_complete=no", rendered)
        self.assertIn(
            "after_descriptor_phase: Device1.Connected=true", rendered
        )

    def test_ack_only_and_fresh_acl_session_modes_are_exclusive(self) -> None:
        with self.assertRaises(ValueError):
            BlueZCoexistenceSession(
                FakeClient(),
                FakeRegistration(),
                SimpleNamespace(),
                FailingHandshake(),
                UnusedHeartRate(),
                experimental_ack_only_hr=True,
                experimental_fresh_bluez_acl=True,
            )

    async def test_connection_loss_is_detected_at_phase_and_cleanup_rechecks(
        self,
    ) -> None:
        client = FakeClient(
            snapshots=[state(), state(connected=False), state(connected=False)]
        )
        registration = FakeRegistration()
        fake_socket = FakeSocket()
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake_socket),
        )
        session = BlueZCoexistenceSession(
            client,
            registration,
            transport,
            AAPHandshakeSession(),
            HeartRateActivationSession(minimum_bootstrap_seconds=0),
            output=lambda message: None,
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category, CoexistenceCategory.BLUEZ_CONNECTION_LOST
        )
        self.assertEqual(
            raised.exception.phase, CoexistencePhase.L2CAP_CONNECTION
        )
        self.assertEqual(client.snapshot_calls, 3)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(fake_socket.close_calls, 1)

    async def test_profile_failure_skips_later_phases_but_runs_cleanup(self) -> None:
        registration = FakeRegistration(
            register_error=CoexistenceFailure(
                CoexistenceCategory.PROFILE_REGISTRATION_FAILED,
                CoexistencePhase.PROFILE_REGISTRATION,
            )
        )
        fake_socket = FakeSocket()
        factory = Mock(return_value=fake_socket)
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule, socket_factory=factory
        )
        session = BlueZCoexistenceSession(
            FakeClient(snapshots=[state()]),
            registration,
            transport,
            AAPHandshakeSession(),
            HeartRateActivationSession(minimum_bootstrap_seconds=0),
            output=lambda message: None,
        )
        with self.assertRaises(CoexistenceFailure):
            await session.run()
        factory.assert_not_called()
        self.assertEqual(registration.unregister_calls, 1)

    async def test_handshake_failure_closes_socket_and_profile(self) -> None:
        registration = FakeRegistration()
        fake_socket = FakeSocket()
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake_socket),
        )
        session = BlueZCoexistenceSession(
            FakeClient(snapshots=[state(), state(), state()]),
            registration,
            transport,
            FailingHandshake(),
            UnusedHeartRate(),
            output=lambda message: None,
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category, CoexistenceCategory.AAP_HANDSHAKE_FAILED
        )
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)

    async def test_descriptor_timeout_retains_observation_and_skips_hr(
        self,
    ) -> None:
        observation = descriptor_timeout_observation()
        registration = FakeRegistration()
        fake_socket = FakeSocket()
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake_socket),
        )
        heart_rate = SimpleNamespace(run_collected=AsyncMock())
        session = BlueZCoexistenceSession(
            FakeClient(snapshots=[state(), state(), state()]),
            registration,
            transport,
            DescriptorTimeoutHandshake(observation),
            heart_rate,
            output=lambda message: None,
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.AAP_DESCRIPTOR_TIMEOUT,
        )
        self.assertEqual(
            raised.exception.phase, CoexistencePhase.AAP_HANDSHAKE
        )
        self.assertIs(raised.exception.handshake_observation, observation)
        heart_rate.run_collected.assert_not_awaited()
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)

    async def test_experimental_exact_ack_timeout_runs_canonical_hr(self) -> None:
        observation = descriptor_timeout_observation(receive_frames_dropped=0)
        session, registration, fake_socket, _, output = self.experimental_session(
            observation=observation
        )
        result = await session.run()
        self.assertFalse(result.descriptor_handshake_complete)
        self.assertTrue(result.experimental_ack_only_hr_used)
        self.assertIs(result.handshake_observation, observation)
        self.assertFalse(result.handshake_observation.evidence.required)
        self.assertEqual(len(result.heart_rate.samples), 5)
        self.assertEqual(result.heart_rate.requested_samples, 5)
        self.assertEqual(
            [report.bpm for report in result.heart_rate.samples],
            [0, 10, 20, 30, 40],
        )
        self.assertEqual(
            fake_socket.sent,
            [AAP_HANDSHAKE_REQUEST]
            + [command.payload for command in HeartRateCommand],
        )
        self.assertIn(
            "EXPERIMENTAL: exact AAP ACK observed but descriptor evidence "
            "timed out.",
            output,
        )
        self.assertIn(
            "EXPERIMENTAL: proceeding to canonical HR activation for "
            "coexistence feasibility testing only.",
            output,
        )
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(
            [phase for phase, connected in result.checkpoints],
            list(CoexistencePhase),
        )
        self.assertTrue(all(connected for _, connected in result.checkpoints))
        probe_output: list[str] = []
        runner = AsyncMock(return_value=result)
        status = await run_probe(
            execute=True,
            experimental_ack_only_hr=True,
            output=probe_output.append,
            live_runner=runner,
        )
        self.assertEqual(status, 0)
        self.assertIs(runner.await_args.args[-2], True)
        self.assertIs(runner.await_args.args[-1], False)
        self.assertIn("Descriptor handshake: incomplete", probe_output)
        self.assertIn("Exact AAP ACK: proven", probe_output)
        self.assertIn("Experimental HR activation: pass", probe_output)
        self.assertEqual(probe_output[-1], "COEXISTENCE HR EXPERIMENT PASS")

    async def test_experimental_mode_rejects_missing_exact_ack(self) -> None:
        heart_rate = SimpleNamespace(run_collected=AsyncMock())
        session, registration, fake_socket, _, _ = self.experimental_session(
            handshake=MissingACKHandshake(), heart_rate=heart_rate
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category, CoexistenceCategory.AAP_HANDSHAKE_FAILED
        )
        heart_rate.run_collected.assert_not_awaited()
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)

    async def test_experimental_mode_rejects_generic_handshake_failure(
        self,
    ) -> None:
        heart_rate = SimpleNamespace(run_collected=AsyncMock())
        session, _, _, _, _ = self.experimental_session(
            handshake=FailingHandshake(), heart_rate=heart_rate
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category, CoexistenceCategory.AAP_HANDSHAKE_FAILED
        )
        heart_rate.run_collected.assert_not_awaited()

    async def test_experimental_mode_rejects_receive_frame_drops(self) -> None:
        observation = descriptor_timeout_observation(receive_frames_dropped=1)
        heart_rate = SimpleNamespace(run_collected=AsyncMock())
        session, registration, fake_socket, _, _ = self.experimental_session(
            observation=observation, heart_rate=heart_rate
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.AAP_DESCRIPTOR_TIMEOUT,
        )
        self.assertIs(raised.exception.handshake_observation, observation)
        heart_rate.run_collected.assert_not_awaited()
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)

    async def test_descriptor_timeout_without_exact_ack_cannot_continue(
        self,
    ) -> None:
        observation = descriptor_timeout_observation(
            ack_observed=False, receive_frames_dropped=0
        )
        heart_rate = SimpleNamespace(run_collected=AsyncMock())
        session, _, _, _, _ = self.experimental_session(
            observation=observation, heart_rate=heart_rate
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.AAP_DESCRIPTOR_TIMEOUT,
        )
        heart_rate.run_collected.assert_not_awaited()

    async def test_connection_loss_after_exact_ack_blocks_experiment(self) -> None:
        client = FakeClient(
            snapshots=[
                state(),
                state(),
                state(connected=False),
                state(connected=False),
            ]
        )
        heart_rate = SimpleNamespace(run_collected=AsyncMock())
        session, registration, fake_socket, _, _ = self.experimental_session(
            client=client, heart_rate=heart_rate
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category, CoexistenceCategory.BLUEZ_CONNECTION_LOST
        )
        self.assertEqual(
            raised.exception.phase, CoexistencePhase.AAP_HANDSHAKE
        )
        heart_rate.run_collected.assert_not_awaited()
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)

    async def test_experimental_hr_failure_still_cleans_resources(self) -> None:
        session, registration, fake_socket, _, _ = self.experimental_session(
            frames=[]
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.HR_ACTIVATION_FAILED,
        )
        self.assertTrue(raised.exception.experimental_ack_only_hr_attempted)
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        output: list[str] = []
        status = await run_probe(
            execute=True,
            experimental_ack_only_hr=True,
            output=output.append,
            live_runner=AsyncMock(side_effect=raised.exception),
        )
        self.assertEqual(status, 1)
        self.assertIn("Descriptor handshake: incomplete", output)
        self.assertIn("Exact AAP ACK: proven", output)
        self.assertIn("Experimental HR activation: fail", output)

    async def test_zero_frame_stream_timeout_preserves_safe_diagnostics(
        self,
    ) -> None:
        frames: list[bytes | BaseException] = [
            service_ack(0x0E),
            CONNECT4_ACK,
            service_ack(0x13),
            TimeoutError(),
            service_ack(0x13),
        ]
        session, registration, fake_socket, _, _ = self.experimental_session(
            frames=frames
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        failure = raised.exception
        self.assertEqual(failure.category, CoexistenceCategory.HR_TIMEOUT)
        diagnostics = failure.hr_timeout_diagnostics
        self.assertIsNotNone(diagnostics)
        assert diagnostics is not None
        observation = diagnostics.stream_observation
        self.assertTrue(observation.observation_armed)
        self.assertTrue(observation.observation_cleanly_disarmed)
        self.assertEqual(observation.frames_observed, 0)
        self.assertEqual(observation.frames_with_hr_marker, 0)
        self.assertEqual(observation.frames_without_hr_marker, 0)
        self.assertEqual(diagnostics.canonical_non_hr_frames, 0)
        self.assertEqual(diagnostics.canonical_malformed_hr_frames, 0)
        self.assertEqual(diagnostics.control_frames_observed, 3)
        self.assertTrue(diagnostics.frame_count_corresponds)
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(fake_socket.frames, [])

        output: list[str] = []
        status = await run_probe(
            execute=True,
            experimental_ack_only_hr=True,
            verbose=True,
            output=output.append,
            live_runner=AsyncMock(side_effect=failure),
        )
        rendered = "\n".join(output)
        self.assertEqual(status, 1)
        self.assertIn("HR stream timeout diagnostics:", rendered)
        self.assertIn("post_start_frames=0", rendered)
        self.assertIn("canonical_non_hr_frames=0", rendered)
        self.assertIn("canonical_malformed_hr_frames=0", rendered)
        self.assertIn("frame_count_corresponds=yes", rendered)

    async def test_non_hr_and_malformed_frames_are_observed_not_retained(
        self,
    ) -> None:
        private_non_hr = b"PRIVATE_STREAM_STRING"
        malformed_hr = b"prefix" + HEART_RATE_MARKER + b"short"
        frames: list[bytes | BaseException] = [
            service_ack(0x0E),
            CONNECT4_ACK,
            service_ack(0x13),
            private_non_hr,
            malformed_hr,
            TimeoutError(),
            service_ack(0x13),
        ]
        session, registration, fake_socket, _, _ = self.experimental_session(
            frames=frames
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        failure = raised.exception
        diagnostics = failure.hr_timeout_diagnostics
        self.assertIsNotNone(diagnostics)
        assert diagnostics is not None
        observation = diagnostics.stream_observation
        self.assertEqual(observation.frames_observed, 2)
        self.assertEqual(observation.frames_with_hr_marker, 1)
        self.assertEqual(observation.frames_without_hr_marker, 1)
        self.assertEqual(diagnostics.canonical_non_hr_frames, 1)
        self.assertEqual(diagnostics.canonical_malformed_hr_frames, 1)
        self.assertTrue(diagnostics.frame_count_corresponds)
        self.assertTrue(
            all(
                isinstance(item, ControlFrameSummary)
                for item in observation.frame_summaries
            )
        )
        self.assertFalse(
            any(
                summary.service_ack_suffix_13
                for summary in observation.frame_summaries
            )
        )
        self.assertEqual(fake_socket.frames, [])
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)

        output: list[str] = []
        await run_probe(
            execute=True,
            experimental_ack_only_hr=True,
            verbose=True,
            output=output.append,
            live_runner=AsyncMock(side_effect=failure),
        )
        rendered = "\n".join(output)
        self.assertIn("post_start_frames=2", rendered)
        self.assertIn("frames_with_hr_marker=1", rendered)
        self.assertIn("frames_without_hr_marker=1", rendered)
        self.assertIn("canonical_non_hr_frames=1", rendered)
        self.assertIn("canonical_malformed_hr_frames=1", rendered)
        self.assertIn("heart_rate_marker_present=yes", rendered)
        self.assertIn("heart_rate_marker_present=no", rendered)
        self.assertNotIn(private_non_hr.decode(), rendered)
        self.assertNotIn(private_non_hr.hex(), rendered.lower())
        self.assertNotIn(malformed_hr.hex(), rendered.lower())

    async def test_timeout_diagnostics_report_frame_count_discrepancy(self) -> None:
        summary = ControlFrameSummary.from_frame(b"safe-summary-only")
        observation = CoexistenceHRStreamObservation(
            frames_observed=2,
            frames_with_hr_marker=0,
            frames_without_hr_marker=2,
            frame_summaries=(summary,) * (DEFAULT_CONTROL_SUMMARY_LIMIT + 3),
            observation_armed=True,
            observation_cleanly_disarmed=True,
            receive_frames_dropped=0,
        )
        diagnostics = CoexistenceHRTimeoutDiagnostics(
            stream_observation=observation,
            canonical_non_hr_frames=1,
            canonical_malformed_hr_frames=0,
            control_frames_observed=3,
            frame_count_corresponds=False,
        )
        failure = CoexistenceFailure(
            CoexistenceCategory.HR_TIMEOUT,
            CoexistencePhase.HR_RECEPTION,
            hr_timeout_diagnostics=diagnostics,
        )
        output: list[str] = []
        await run_probe(
            execute=True,
            verbose=True,
            output=output.append,
            live_runner=AsyncMock(side_effect=failure),
        )
        self.assertIn("  frame_count_corresponds=no", output)
        self.assertEqual(
            sum(line.startswith("  stream_frame_") for line in output),
            DEFAULT_CONTROL_SUMMARY_LIMIT,
        )

    async def test_route_mismatch_never_enters_aap_or_hr(self) -> None:
        registration = FakeRegistration()
        fake_socket = FakeSocket(
            local_endpoint=("66:77:88:99:AA:BB", 0)
        )
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake_socket),
        )
        handshake = SimpleNamespace(run_collected=AsyncMock())
        heart_rate = SimpleNamespace(run_collected=AsyncMock())
        session = BlueZCoexistenceSession(
            FakeClient(snapshots=[state(), state()]),
            registration,
            transport,
            handshake,
            heart_rate,
            output=lambda message: None,
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.L2CAP_ROUTE_MISMATCH,
        )
        handshake.run_collected.assert_not_awaited()
        heart_rate.run_collected.assert_not_awaited()
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)

    async def test_fewer_than_requested_samples_is_hr_timeout(self) -> None:
        client = FakeClient()
        registration = FakeRegistration()
        fake_socket = FakeSocket(successful_frames(sample_count=2))
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake_socket),
        )
        session = BlueZCoexistenceSession(
            client,
            registration,
            transport,
            AAPHandshakeSession(),
            HeartRateActivationSession(
                sample_target=5, minimum_bootstrap_seconds=0
            ),
            output=lambda message: None,
        )
        # Move the stop ACK behind the two samples; receive timeout then triggers
        # canonical HR cleanup and makes the partial outcome a probe failure.
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(raised.exception.category, CoexistenceCategory.HR_TIMEOUT)
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertIn(HeartRateCommand.STOP_HR.payload, fake_socket.sent)
        self.assertIn(HeartRateCommand.HR_OFF.payload, fake_socket.sent)

    async def test_cleanup_state_failure_is_categorized_as_cleanup(self) -> None:
        client = FakeClient(
            snapshots=[
                state(),
                state(),
                state(),
                state(),
                state(),
                RuntimeError("private D-Bus detail"),
            ]
        )
        registration = FakeRegistration()
        fake_socket = FakeSocket(successful_frames())
        transport = KernelL2CAPTransport(
            socket_module=FakeSocketModule,
            socket_factory=Mock(return_value=fake_socket),
        )
        session = BlueZCoexistenceSession(
            client,
            registration,
            transport,
            AAPHandshakeSession(),
            HeartRateActivationSession(
                sample_target=5, minimum_bootstrap_seconds=0
            ),
            output=lambda message: None,
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await session.run()
        self.assertEqual(raised.exception.category, CoexistenceCategory.CLEANUP_FAILED)
        self.assertEqual(raised.exception.phase, CoexistencePhase.CLEANUP)
        self.assertEqual(fake_socket.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)

    async def test_cancellation_unwinds_probe_owned_resources(self) -> None:
        import asyncio

        entered = asyncio.Event()

        class BlockingHandshake:
            async def run_collected(self, transport: object) -> None:
                del transport
                entered.set()
                await asyncio.Event().wait()

        class CancellationTransport:
            def __init__(self) -> None:
                self.close_calls = 0
                self.local_rx_observation = KernelL2CAPLocalRXObservation(
                    target_imtu=2048,
                    options_source="python-socket",
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

            async def open(
                self, local_address: str, remote_address: str
            ) -> None:
                self.local_address = local_address
                self.remote_address = remote_address

            @asynccontextmanager
            async def collect(self):
                yield self

            def close(self) -> None:
                self.close_calls += 1

        client = FakeClient(snapshots=[state(), state(), state()])
        registration = FakeRegistration()
        transport = CancellationTransport()
        session = BlueZCoexistenceSession(
            client,
            registration,
            transport,
            BlockingHandshake(),
            UnusedHeartRate(),
            output=lambda message: None,
        )
        task = asyncio.create_task(session.run())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(client.close_calls, 1)


class StaticSafetyTests(unittest.TestCase):
    def test_coexistence_sources_have_no_handoff_or_pairing_imports(self) -> None:
        root = Path(__file__).resolve().parents[1]
        sources = [
            root / "src/airpods_hr/bluez_coexistence.py",
            root / "src/airpods_hr/bluez_sdp_audit.py",
            root / "tools/probe_bluez_coexistence.py",
        ]
        forbidden_modules = {
            "airpods_hr.authentication",
            "airpods_hr.bluetooth",
            "airpods_hr.bumble_keys",
            "airpods_hr.monitor_cli",
            "airpods_hr.pairing",
        }
        forbidden_names = {
            "ControllerHandoff",
            "HCI_CHANNEL_USER",
            "BlueZPairingStore",
            "BumbleClassicRuntimeFactory",
            "HeartRateMonitorSession",
        }
        forbidden_dbus_calls = {
            "call_connect",
            "call_disconnect",
            "call_connect_profile",
            "call_disconnect_profile",
        }
        for source in sources:
            tree = ast.parse(source.read_text(encoding="utf-8"))
            imported_modules: set[str] = set()
            imported_names: set[str] = set()
            called_attributes: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported_modules.add(node.module or "")
                    imported_names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.Call) and isinstance(
                    node.func, ast.Attribute
                ):
                    called_attributes.add(node.func.attr)
            self.assertTrue(forbidden_modules.isdisjoint(imported_modules), source)
            self.assertTrue(forbidden_names.isdisjoint(imported_names), source)
            self.assertTrue(
                forbidden_dbus_calls.isdisjoint(called_attributes), source
            )

    def test_protocol_source_is_not_modified_for_coexistence(self) -> None:
        root = Path(__file__).resolve().parents[1]
        protocol_source = root / "src/airpods_hr/protocol.py"
        import hashlib

        self.assertEqual(
            hashlib.sha256(protocol_source.read_bytes()).hexdigest(),
            "b4d1daea0582841e48ba9efc3a8a7d4d74bba9b69cdbf54d3767b8bb45afecca",
        )

    def test_classic_local_rx_fix_does_not_use_le_or_fallback_paths(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/airpods_hr/bluez_coexistence.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("BT_RCVMTU", source)
        self.assertNotIn("ControllerHandoff", source)
        self.assertNotIn("HCI_CHANNEL_USER", source)
        self.assertNotIn("BlueZPairingStore", source)


if __name__ == "__main__":
    unittest.main()
