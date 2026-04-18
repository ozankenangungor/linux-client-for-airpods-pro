"""Hardware-independent tests for the private persistent production core."""

from __future__ import annotations

import ast
import asyncio
import errno
import hashlib
import unittest
from contextlib import asynccontextmanager, redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from dbus_next.errors import DBusError

from airpods_hr.aap import (
    AAP_HANDSHAKE_ACK,
    AAPDescriptorObservationTimeoutError,
    AAPHandshakeResult,
    AAPHandshakeSession,
    DescriptorEvidence,
    HandshakeObservation,
)
from airpods_hr.address import BluetoothAddress
from airpods_hr.bluez_coexistence import (
    BlueZCoexistenceState,
    CoexistenceCategory,
    CoexistenceFailure,
    CoexistencePhase,
    KernelL2CAPLocalRXObservation,
)
from airpods_hr.discovery import AirPodsCandidate
from airpods_hr.heart_rate_session import (
    CONNECT4_ACK,
    HeartRateMonitorActivationSession,
    HeartRateMonitorSessionResult,
    HeartRateProgress,
)
from airpods_hr.heartrate import HeartRateReport, parse_heart_rate_packet
from airpods_hr.production_session import (
    InternalProductionSession,
    ProductionSessionCategory,
    ProductionSessionCounters,
    ProductionSessionError,
    ProductionSessionState,
    ProductionSessionStateError,
)
from airpods_hr.protocol import (
    HEART_RATE_MARKER,
    HEART_RATE_REPORT_ID,
    HEART_RATE_REPORT_SIZE,
    HeartRateCommand,
)
from tools.probe_production_session import build_parser, main, run_probe


LOCAL_ADDRESS = "00:11:22:33:44:55"
REMOTE_ADDRESS = "AA:BB:CC:DD:EE:FF"


def candidate() -> AirPodsCandidate:
    return AirPodsCandidate(
        display_name="Test AirPods",
        adapter_name="hci0",
        adapter_path="/org/bluez/hci0",
        adapter_address=BluetoothAddress.parse(LOCAL_ADDRESS),
        address=BluetoothAddress.parse(REMOTE_ADDRESS),
        object_path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        adapter_modalias="usb:v1234p5678d9ABC",
    )


def state(*, connected: bool = True, powered: bool = True):
    return BlueZCoexistenceState(candidate(), powered, connected, frozenset())


def raw_report(
    bpm: int,
    sequence: int,
    *,
    field_5: int = 1,
    timestamp_ticks: int = 100,
    flags: int = 0x1000,
) -> bytes:
    report = bytearray(HEART_RATE_REPORT_SIZE)
    report[0] = HEART_RATE_REPORT_ID
    report[1] = bpm
    report[2] = 20
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


def activation_frames(reports: list[bytes]) -> list[bytes]:
    return [
        service_ack(0x0E),
        CONNECT4_ACK,
        service_ack(0x13),
        *reports,
    ]


async def stop_with_ack(
    session: InternalProductionSession, transport: FakeTransport
) -> None:
    transport.add_frames([b"wake stream receiver", service_ack(0x13)])
    await session.stop()


class FakeClient:
    def __init__(
        self,
        events: list[str],
        *,
        preflight_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.preflight_error = preflight_error
        self.close_calls = 0
        self.snapshot_calls = 0

    async def connect(self) -> None:
        self.events.append("client_connect")

    async def preflight(self, *, require_connected: bool = True):
        self.events.append("preflight")
        self.require_connected = require_connected
        if self.preflight_error is not None:
            raise self.preflight_error
        return state()

    async def snapshot(self, selected):
        self.events.append("snapshot")
        self.snapshot_calls += 1
        self.selected = selected
        return state()

    def close(self) -> None:
        self.events.append("client_close")
        self.close_calls += 1


class FakeRegistration:
    def __init__(
        self,
        events: list[str],
        *,
        register_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.register_error = register_error
        self.register_calls = 0
        self.unregister_calls = 0
        self.registered_count = 0

    async def register(self, selected_state) -> None:
        self.events.append("register")
        self.register_calls += 1
        self.selected_state = selected_state
        if self.register_error is not None:
            raise self.register_error

    async def unregister(self) -> None:
        self.events.append("unregister")
        self.unregister_calls += 1


class FakeTransport:
    def __init__(
        self,
        events: list[str],
        frames: list[bytes] | None = None,
        *,
        open_error: BaseException | None = None,
        imtu: int = 2048,
    ) -> None:
        self.events = events
        self.frames: asyncio.Queue[bytes] = asyncio.Queue()
        for frame in frames or []:
            self.frames.put_nowait(frame)
        self.open_error = open_error
        self.application_payloads_sent = 0
        self.dropped_frames = 0
        self.pending_receive_frames = 0
        self.commands: list[HeartRateCommand] = []
        self.open_calls: list[tuple[str, str]] = []
        self.close_calls = 0
        self.collect_entries = 0
        self.collect_exits = 0
        self.local_rx_observation = KernelL2CAPLocalRXObservation(
            target_imtu=2048,
            options_source="linux-uapi-fallback",
            before_imtu=672,
            after_imtu=imtu,
            preserved_omtu=True,
            preserved_flush_to=True,
            preserved_mode=True,
            preserved_fcs=True,
            preserved_max_tx=True,
            preserved_txwin_size=True,
            verified=imtu == 2048,
        )

    async def open(self, local_address: str, remote_address: str) -> None:
        self.events.append("transport_open")
        self.open_calls.append((local_address, remote_address))
        if self.open_error is not None:
            raise self.open_error

    @asynccontextmanager
    async def collect(self):
        self.events.append("collect_enter")
        self.collect_entries += 1
        try:
            yield self
        finally:
            self.events.append("collect_exit")
            self.collect_exits += 1

    def send_handshake_request(self) -> None:
        self.application_payloads_sent += 1

    def send_heart_rate_command(self, command: HeartRateCommand) -> None:
        self.commands.append(command)
        self.application_payloads_sent += 1

    async def receive(self, timeout: float) -> bytes:
        try:
            return await asyncio.wait_for(self.frames.get(), timeout=timeout)
        except TimeoutError:
            raise TimeoutError from None

    def add_frames(self, frames: list[bytes]) -> None:
        for frame in frames:
            self.frames.put_nowait(frame)

    def close(self) -> None:
        self.events.append("transport_close")
        self.close_calls += 1


class FakeHandshake:
    def __init__(
        self,
        events: list[str],
        *,
        error: BaseException | None = None,
        descriptor_complete: bool = True,
    ) -> None:
        self.events = events
        self.error = error
        self.calls = 0
        self.descriptor_complete = descriptor_complete

    async def run_collected(self, transport) -> AAPHandshakeResult:
        self.events.append("handshake")
        self.calls += 1
        if self.error is not None:
            raise self.error
        transport.application_payloads_sent += 1
        evidence = DescriptorEvidence(
            sensor_framework=self.descriptor_complete,
            heart_rate_service=self.descriptor_complete,
            heart_rate=self.descriptor_complete,
            heartrate_access=self.descriptor_complete,
        )
        return AAPHandshakeResult(
            observation=HandshakeObservation(True, evidence),
            application_payloads_sent=1,
            handshake_sent_at=0.0,
        )


def make_session(
    *,
    frames: list[bytes] | None = None,
    events: list[str] | None = None,
    client=None,
    registration=None,
    transport=None,
    handshake=None,
    monitor_factory=None,
    start_timeout: float = 1.0,
    stop_timeout: float = 1.0,
):
    event_log = events if events is not None else []
    client = client or FakeClient(event_log)
    registration = registration or FakeRegistration(event_log)
    transport = transport or FakeTransport(event_log, frames)
    handshake_session = handshake or FakeHandshake(event_log)
    session = InternalProductionSession(
        client,
        registration,
        transport,
        handshake_session,
        start_timeout=start_timeout,
        stop_timeout=stop_timeout,
        monitor_factory=monitor_factory,
        output=lambda message: None,
    )
    return session, client, registration, transport, handshake_session, event_log


def dbus_coexistence_failure(
    error_name: str,
    *,
    category: CoexistenceCategory = CoexistenceCategory.PREFLIGHT_FAILED,
    phase: CoexistencePhase = CoexistencePhase.PREFLIGHT,
) -> CoexistenceFailure:
    failure = CoexistenceFailure(category, phase)
    failure.__cause__ = DBusError(error_name, "synthetic test failure")
    return failure


class ProductionSessionStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_state_and_invalid_operations(self) -> None:
        session, *_ = make_session()
        self.assertIs(session.state, ProductionSessionState.CLOSED)
        with self.assertRaises(ProductionSessionStateError):
            await session.start()
        with self.assertRaises(ProductionSessionStateError):
            await session.receive_report()
        with self.assertRaises(ProductionSessionStateError):
            await session.stop()
        await session.close()
        self.assertIs(session.state, ProductionSessionState.CLOSED)

    async def test_open_order_once_and_descriptor_gated_ready(self) -> None:
        session, client, registration, transport, handshake, events = make_session()
        await session.open()

        self.assertIs(session.state, ProductionSessionState.READY)
        self.assertEqual(transport.open_calls, [(LOCAL_ADDRESS, REMOTE_ADDRESS)])
        self.assertEqual(handshake.calls, 1)
        self.assertEqual(session.counters.transport_opens, 1)
        self.assertEqual(session.counters.descriptor_handshakes, 1)
        self.assertLess(events.index("preflight"), events.index("register"))
        self.assertLess(events.index("register"), events.index("transport_open"))
        self.assertLess(events.index("transport_open"), events.index("handshake"))
        self.assertEqual(transport.collect_entries, 1)
        await session.close()
        self.assertEqual(transport.collect_exits, 1)
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(client.close_calls, 1)
        await session.close()
        self.assertEqual(transport.close_calls, 1)

    async def test_open_remains_opening_until_descriptor_completion(self) -> None:
        events: list[str] = []
        descriptor_gate = asyncio.Event()

        class GatedHandshake(FakeHandshake):
            async def run_collected(self, transport):
                await descriptor_gate.wait()
                return await super().run_collected(transport)

        session, _, _, _, _, _ = make_session(
            events=events,
            handshake=GatedHandshake(events),
        )
        opening = asyncio.create_task(session.open())
        await asyncio.sleep(0)
        self.assertIs(session.state, ProductionSessionState.OPENING)
        descriptor_gate.set()
        await opening
        self.assertIs(session.state, ProductionSessionState.READY)
        await session.close()

    async def test_incomplete_descriptor_result_fails_and_cleans(self) -> None:
        events: list[str] = []
        handshake = FakeHandshake(events, descriptor_complete=False)
        session, client, registration, transport, _, _ = make_session(
            events=events, handshake=handshake
        )
        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()
        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.DESCRIPTOR_HANDSHAKE_FAILED,
        )
        self.assertIs(session.state, ProductionSessionState.FAILED)
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(client.close_calls, 1)
        await session.close()
        self.assertIs(session.state, ProductionSessionState.CLOSED)
        self.assertEqual(transport.close_calls, 1)

    async def test_descriptor_timeout_has_no_ack_only_or_reconnect(self) -> None:
        events: list[str] = []
        observation = HandshakeObservation(
            ack_observed=True,
            evidence=DescriptorEvidence(sensor_framework=True),
            post_ack_frame_count=3,
        )
        handshake = FakeHandshake(
            events, error=AAPDescriptorObservationTimeoutError(observation)
        )
        session, client, _, transport, _, _ = make_session(
            events=events, handshake=handshake
        )
        with self.assertRaises(ProductionSessionError):
            await session.open()
        self.assertEqual(handshake.calls, 1)
        self.assertEqual(transport.commands, [])
        self.assertNotIn("device_connect", events)
        self.assertNotIn("device_disconnect", events)
        self.assertEqual(client.close_calls, 1)

    async def test_unverified_local_rx_imtu_fails_before_handshake(self) -> None:
        events: list[str] = []
        client = FakeClient(events)
        registration = FakeRegistration(events)
        transport = FakeTransport(events, imtu=672)
        handshake = FakeHandshake(events)
        session = InternalProductionSession(
            client,
            registration,
            transport,
            handshake,
            output=lambda message: None,
        )
        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()
        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.TRANSPORT_FAILED,
        )
        self.assertEqual(len(transport.open_calls), 1)
        self.assertEqual(handshake.calls, 0)
        self.assertEqual(transport.close_calls, 1)
        self.assertFalse(raised.exception.recoverable)

    async def test_known_disconnected_preflight_is_recoverable(self) -> None:
        events: list[str] = []
        disconnected = CoexistenceFailure(
            CoexistenceCategory.AIRPODS_NOT_CONNECTED,
            CoexistencePhase.PREFLIGHT,
        )
        session, _, _, _, _, _ = make_session(
            events=events,
            client=FakeClient(events, preflight_error=disconnected),
        )

        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()

        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.PREFLIGHT_FAILED,
        )
        self.assertTrue(raised.exception.recoverable)

    async def test_transient_dbus_no_reply_is_recoverable(self) -> None:
        events: list[str] = []
        session, _, _, _, _, _ = make_session(
            events=events,
            client=FakeClient(
                events,
                preflight_error=dbus_coexistence_failure(
                    "org.freedesktop.DBus.Error.NoReply"
                ),
            ),
        )

        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()

        self.assertTrue(raised.exception.recoverable)

    async def test_bluez_invalid_arguments_is_terminal(self) -> None:
        events: list[str] = []
        session, _, _, _, _, _ = make_session(
            events=events,
            registration=FakeRegistration(
                events,
                register_error=dbus_coexistence_failure(
                    "org.bluez.Error.InvalidArguments",
                    category=CoexistenceCategory.PROFILE_REGISTRATION_FAILED,
                    phase=CoexistencePhase.PROFILE_REGISTRATION,
                ),
            ),
        )

        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()

        self.assertFalse(raised.exception.recoverable)

    async def test_bluez_not_supported_is_terminal(self) -> None:
        events: list[str] = []
        session, _, _, _, _, _ = make_session(
            events=events,
            registration=FakeRegistration(
                events,
                register_error=dbus_coexistence_failure(
                    "org.bluez.Error.NotSupported",
                    category=CoexistenceCategory.PROFILE_REGISTRATION_FAILED,
                    phase=CoexistencePhase.PROFILE_REGISTRATION,
                ),
            ),
        )

        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()

        self.assertFalse(raised.exception.recoverable)

    async def test_unknown_dbus_error_is_terminal(self) -> None:
        events: list[str] = []
        session, _, _, _, _, _ = make_session(
            events=events,
            client=FakeClient(
                events,
                preflight_error=dbus_coexistence_failure(
                    "com.example.AirPods.Error.TemporarilyMysterious"
                ),
            ),
        )

        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()

        self.assertFalse(raised.exception.recoverable)

    async def test_programmer_error_during_open_is_terminal(self) -> None:
        events: list[str] = []
        session, _, _, _, _, _ = make_session(
            events=events,
            transport=FakeTransport(
                events, open_error=TypeError("unexpected implementation bug")
            ),
        )

        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()

        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.TRANSPORT_FAILED,
        )
        self.assertEqual(raised.exception.detail, "TypeError")
        self.assertFalse(raised.exception.recoverable)

    async def test_unsupported_kernel_bind_semantics_are_terminal(self) -> None:
        events: list[str] = []
        platform_error = CoexistenceFailure(
            CoexistenceCategory.L2CAP_BIND_FAILED,
            CoexistencePhase.L2CAP_CONNECTION,
        )
        platform_error.__cause__ = OSError(
            errno.EINVAL, "unsupported L2CAP bind semantics"
        )
        session, _, _, _, _, _ = make_session(
            events=events,
            transport=FakeTransport(events, open_error=platform_error),
        )

        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()

        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.TRANSPORT_FAILED,
        )
        self.assertFalse(raised.exception.recoverable)

    async def test_wrapped_cancellation_remains_cancellation(self) -> None:
        events: list[str] = []
        cancellation = asyncio.CancelledError()
        wrapped = CoexistenceFailure(
            CoexistenceCategory.PROFILE_REGISTRATION_FAILED,
            CoexistencePhase.PROFILE_REGISTRATION,
        )
        wrapped.__cause__ = cancellation
        session, _, _, _, _, _ = make_session(
            events=events,
            registration=FakeRegistration(events, register_error=wrapped),
        )

        with self.assertRaises(asyncio.CancelledError) as raised:
            await session.open()

        self.assertIs(raised.exception, cancellation)
        self.assertIs(session.state, ProductionSessionState.FAILED)

    async def test_partial_open_failures_close_owned_resources(self) -> None:
        for failure_phase in ("registration", "transport"):
            with self.subTest(failure_phase=failure_phase):
                events: list[str] = []
                client = FakeClient(events)
                registration = FakeRegistration(
                    events,
                    register_error=(
                        RuntimeError("register")
                        if failure_phase == "registration"
                        else None
                    ),
                )
                transport = FakeTransport(
                    events,
                    open_error=(
                        RuntimeError("open")
                        if failure_phase == "transport"
                        else None
                    ),
                )
                session = InternalProductionSession(
                    client,
                    registration,
                    transport,
                    FakeHandshake(events),
                    output=lambda message: None,
                )
                with self.assertRaises(ProductionSessionError):
                    await session.open()
                self.assertIs(session.state, ProductionSessionState.FAILED)
                self.assertEqual(client.close_calls, 1)
                self.assertEqual(registration.unregister_calls, 1)
                if failure_phase == "transport":
                    self.assertEqual(transport.close_calls, 1)
                    self.assertEqual(
                        transport.open_calls,
                        [(LOCAL_ADDRESS, REMOTE_ADDRESS)],
                    )
                else:
                    self.assertEqual(transport.close_calls, 0)

    async def test_activation_failure_fails_closed_without_reopening(self) -> None:
        class FailedMonitor:
            async def run_collected(self, transport, handshake, stop_event):
                del transport, handshake, stop_event
                raise RuntimeError("activation failed")

        session, client, registration, transport, handshake, _ = make_session(
            monitor_factory=lambda progress: FailedMonitor()
        )
        await session.open()
        with self.assertRaises(ProductionSessionError) as raised:
            await session.start()
        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.ACTIVATION_FAILED,
        )
        self.assertIs(session.state, ProductionSessionState.FAILED)
        self.assertEqual(len(transport.open_calls), 1)
        self.assertEqual(handshake.calls, 1)
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(client.close_calls, 1)


class ProductionSessionStreamingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def monitor_factory(progress):
        return HeartRateMonitorActivationSession(
            progress=progress,
            minimum_bootstrap_seconds=0,
            receive_poll_interval=0.01,
        )

    async def test_first_169_and_duplicates_are_returned_unchanged_in_order(
        self,
    ) -> None:
        first = parsed_report(169, 0)
        duplicate = parsed_report(169, 0)
        session, _, _, transport, _, _ = make_session(
            frames=activation_frames(
                [
                    b"prefix" + HEART_RATE_MARKER + first.raw_report,
                    b"prefix" + HEART_RATE_MARKER + duplicate.raw_report,
                ]
            ),
            monitor_factory=self.monitor_factory,
        )
        await session.open()
        await session.start()
        self.assertIs(session.state, ProductionSessionState.STREAMING)
        observed_first = await session.receive_report(timeout=1)
        observed_second = await session.receive_report(timeout=1)
        self.assertIsInstance(observed_first, HeartRateReport)
        self.assertEqual(observed_first, first)
        self.assertEqual(observed_second, duplicate)
        self.assertEqual(observed_first.bpm, 169)
        self.assertEqual(observed_first.raw_report, first.raw_report)
        await stop_with_ack(session, transport)
        self.assertIs(session.state, ProductionSessionState.READY)
        await session.close()

    async def test_start_remains_starting_until_activation_ack(self) -> None:
        activation_gate = asyncio.Event()

        class GatedMonitor:
            def __init__(self, progress) -> None:
                self.progress = progress

            async def run_collected(self, transport, handshake, stop_event):
                del transport, handshake
                await activation_gate.wait()
                self.progress(HeartRateProgress.START_ACKNOWLEDGED, None)
                await stop_event.wait()
                self.progress(HeartRateProgress.STOP_ACKNOWLEDGED, None)
                self.progress(HeartRateProgress.HR_OFF_SENT, None)
                return HeartRateMonitorSessionResult(
                    samples_observed=0,
                    stop_acknowledged=True,
                    application_payloads_sent=10,
                    control_frames_observed=0,
                    non_hr_frames=0,
                    malformed_hr_frames=0,
                )

        session, _, _, _, _, _ = make_session(
            monitor_factory=GatedMonitor
        )
        await session.open()
        starting = asyncio.create_task(session.start())
        await asyncio.sleep(0)
        self.assertIs(session.state, ProductionSessionState.STARTING)
        activation_gate.set()
        await starting
        self.assertIs(session.state, ProductionSessionState.STREAMING)
        await session.stop()
        self.assertIs(session.state, ProductionSessionState.READY)
        await session.close()

    async def test_concurrent_report_consumer_is_rejected_deterministically(
        self,
    ) -> None:
        session, _, _, transport, _, _ = make_session(
            frames=activation_frames([]),
            monitor_factory=self.monitor_factory,
        )
        await session.open()
        await session.start()
        first_consumer = asyncio.create_task(session.receive_report(timeout=1))
        await asyncio.sleep(0)
        with self.assertRaises(ProductionSessionError) as raised:
            await session.receive_report(timeout=1)
        self.assertEqual(
            raised.exception.category, ProductionSessionCategory.INVALID_STATE
        )
        first_consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first_consumer
        transport.add_frames([heart_rate_packet(70, 1)])
        self.assertEqual((await session.receive_report(timeout=1)).bpm, 70)
        await stop_with_ack(session, transport)
        await session.close()

    async def test_three_cycles_share_transport_and_handshake(self) -> None:
        session, _, registration, transport, _, _ = make_session(
            frames=[AAP_HANDSHAKE_ACK, descriptor_frame()],
            handshake=AAPHandshakeSession(),
            monitor_factory=self.monitor_factory,
        )
        await session.open()
        samples: list[HeartRateReport] = []
        for cycle in range(3):
            reports = [
                heart_rate_packet(169 if index == 0 else 80 + index, index)
                for index in range(5)
            ]
            transport.add_frames(activation_frames(reports))
            await session.start()
            for _ in range(5):
                samples.append(await session.receive_report(timeout=1))
            await stop_with_ack(session, transport)
            self.assertIs(session.state, ProductionSessionState.READY)

        self.assertEqual(len(samples), 15)
        self.assertEqual([samples[i].bpm for i in (0, 5, 10)], [169, 169, 169])
        self.assertEqual(session.counters.transport_opens, 1)
        self.assertEqual(session.counters.descriptor_handshakes, 1)
        self.assertEqual(session.counters.hr_activations, 3)
        self.assertEqual(session.counters.hr_stops, 3)
        self.assertEqual(session.counters.reports_received, 15)
        self.assertEqual(len(transport.open_calls), 1)
        self.assertEqual(transport.collect_entries, 1)
        self.assertEqual(transport.commands, list(HeartRateCommand) * 3)
        self.assertEqual(transport.close_calls, 0)
        self.assertEqual(registration.unregister_calls, 0)
        await session.close()
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)

    async def test_close_from_streaming_stops_before_transport_and_is_idempotent(
        self,
    ) -> None:
        events: list[str] = []
        session, _, _, transport, _, events = make_session(
            events=events,
            frames=activation_frames([heart_rate_packet(70, 0)]),
            monitor_factory=self.monitor_factory,
        )
        await session.open()
        await session.start()
        await session.receive_report(timeout=1)
        transport.add_frames([b"wake stream receiver", service_ack(0x13)])
        await session.close()
        self.assertIs(session.state, ProductionSessionState.CLOSED)
        self.assertEqual(session.counters.hr_stops, 1)
        self.assertEqual(
            transport.commands[-2:],
            [HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF],
        )
        self.assertLess(
            events.index("collect_exit"), events.index("transport_close")
        )
        self.assertEqual(transport.close_calls, 1)
        await session.close()
        self.assertEqual(transport.close_calls, 1)

    async def test_cancelled_receive_leaves_no_competing_consumer(self) -> None:
        session, _, _, transport, _, _ = make_session(
            frames=activation_frames([]),
            monitor_factory=self.monitor_factory,
        )
        await session.open()
        await session.start()
        pending = asyncio.create_task(session.receive_report(timeout=5))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        transport.add_frames([heart_rate_packet(90, 0)])
        report = await session.receive_report(timeout=1)
        self.assertEqual(report.bpm, 90)
        await stop_with_ack(session, transport)
        await session.close()

    async def test_unexpected_receive_bug_is_terminal(self) -> None:
        fail = asyncio.Event()

        class BuggyMonitor:
            def __init__(self, progress) -> None:
                self.progress = progress

            async def run_collected(self, transport, handshake, stop_event):
                del transport, handshake, stop_event
                self.progress(HeartRateProgress.START_ACKNOWLEDGED, None)
                await fail.wait()
                raise TypeError("unexpected report-processing bug")

        session, _, _, _, _, _ = make_session(monitor_factory=BuggyMonitor)
        await session.open()
        await session.start()
        fail.set()

        with self.assertRaises(ProductionSessionError) as raised:
            await session.receive_report(timeout=1)

        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.RECEIVE_FAILED,
        )
        self.assertEqual(raised.exception.detail, "TypeError")
        self.assertFalse(raised.exception.recoverable)
        await session.close()


class ProductionProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_probe_defaults_and_bounds(self) -> None:
        args = build_parser().parse_args([])
        self.assertFalse(args.execute)
        self.assertEqual(args.cycles, 3)
        self.assertEqual(args.samples_per_cycle, 5)
        self.assertEqual(args.restart_delay, 5)
        self.assertEqual(args.descriptor_timeout, 30)
        for option, value in (
            ("--cycles", "0"),
            ("--samples-per-cycle", "0"),
            ("--restart-delay", "31"),
            ("--descriptor-timeout", "0"),
        ):
            with self.subTest(option=option), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args([option, value])

    async def test_dry_run_never_constructs_session(self) -> None:
        factory = unittest.mock.Mock()
        output: list[str] = []
        status = await run_probe(
            execute=False,
            output=output.append,
            session_factory=factory,
        )
        self.assertEqual(status, 0)
        factory.assert_not_called()
        rendered = "\n".join(output)
        self.assertIn("AAP channels=1; descriptor handshakes=1", rendered)
        self.assertIn("cycles=3; samples_per_cycle=5", rendered)
        self.assertIn("kernel_local_rx_imtu=2048", rendered)

    async def test_execute_reports_three_cycles_on_one_session(self) -> None:
        class FakeProbeSession:
            def __init__(self) -> None:
                self.opens = 0
                self.activations = 0
                self.stops = 0
                self.reports = 0
                self.close_calls = 0

            @property
            def counters(self) -> ProductionSessionCounters:
                return ProductionSessionCounters(
                    transport_opens=self.opens,
                    descriptor_handshakes=self.opens,
                    hr_activations=self.activations,
                    hr_stops=self.stops,
                    reports_received=self.reports,
                )

            async def open(self) -> None:
                self.opens += 1

            async def start(self) -> None:
                self.activations += 1

            async def receive_report(self, timeout: float) -> HeartRateReport:
                del timeout
                self.reports += 1
                return parsed_report(169, (self.reports - 1) % 5)

            async def stop(self) -> None:
                self.stops += 1

            async def close(self) -> None:
                self.close_calls += 1

        fake = FakeProbeSession()
        factory = unittest.mock.Mock(return_value=fake)
        sleep = AsyncMock()
        output: list[str] = []
        status = await run_probe(
            execute=True,
            cycles=3,
            samples_per_cycle=5,
            restart_delay=5,
            output=output.append,
            session_factory=factory,
            sleep=sleep,
        )
        self.assertEqual(status, 0)
        self.assertEqual(fake.opens, 1)
        self.assertEqual(fake.activations, 3)
        self.assertEqual(fake.stops, 3)
        self.assertEqual(fake.reports, 15)
        self.assertEqual(fake.close_calls, 1)
        self.assertEqual(sleep.await_count, 2)
        self.assertIn("PERSISTENT SESSION PASS", output)

    def test_main_default_is_deterministic_dry_run(self) -> None:
        first = StringIO()
        second = StringIO()
        self.assertEqual(main([], stream=first), 0)
        self.assertEqual(main([], stream=second), 0)
        self.assertEqual(first.getvalue(), second.getvalue())
        self.assertIn("DRY RUN", first.getvalue())


class ProductionStaticSafetyTests(unittest.TestCase):
    def test_frozen_protocol_parser_transport_monitor_and_semantics_hashes(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        expected = {
            "src/airpods_hr/protocol.py": (
                "b4d1daea0582841e48ba9efc3a8a7d4d74bba9b69cdbf54d3767b8bb45afecca"
            ),
            "src/airpods_hr/heartrate.py": (
                "df0ddb9824146c7ab23eb30c2548aaa9ec7e8dc26461d76aaf92f2c19c3dc045"
            ),
            "src/airpods_hr/bluez_coexistence.py": (
                "55824df2b95d698e60e972d52c500c7ef3e5901cb75793663bd6d9401e26305d"
            ),
            "src/airpods_hr/monitor_cli.py": (
                "41332f411af2e89374b42047a2e009aef035ca2e8bce74440d0d9d891d7aded4"
            ),
            "src/airpods_hr/hr_semantics.py": (
                "f6004987032f02c5b2a7c59590a4e3e2edb5ef85788f8259f2e0b9499f5bc4ba"
            ),
            "tools/probe_hr_semantics.py": (
                "4e0fa4c54f3b29d376284e882225a0194f07fa79b8be5c2dec44d7bf73057e21"
            ),
            "src/airpods_hr/__init__.py": (
                "b50576f701568dd5d63190568c47427d6d2b65c02596a1608dbdb87f3afea35f"
            ),
        }
        for relative, digest in expected.items():
            self.assertEqual(
                hashlib.sha256((root / relative).read_bytes()).hexdigest(), digest
            )

    def test_private_core_has_no_handoff_pairing_or_fallback_dependencies(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        sources = (
            root / "src/airpods_hr/production_session.py",
            root / "tools/probe_production_session.py",
        )
        forbidden_modules = {
            "airpods_hr.authentication",
            "airpods_hr.bluetooth",
            "airpods_hr.bumble_keys",
            "airpods_hr.pairing",
            "airpods_hr.reference_sdp_footprint",
        }
        rendered = "".join(source.read_text() for source in sources)
        for source in sources:
            tree = ast.parse(source.read_text(encoding="utf-8"))
            imports = {
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            }
            self.assertTrue(forbidden_modules.isdisjoint(imports))
        for forbidden in (
            "ControllerHandoff",
            "HCI_CHANNEL_USER",
            "/var/lib/bluetooth",
            "experimental_ack_only_hr",
        ):
            self.assertNotIn(forbidden, rendered)


if __name__ == "__main__":
    unittest.main()
