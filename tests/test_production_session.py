"""Hardware-independent tests for the private persistent production core."""

from __future__ import annotations

import ast
import asyncio
import copy
import errno
import hashlib
import subprocess
import unittest
from contextlib import asynccontextmanager, redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, patch

from dbus_next.errors import DBusError

from airpods_hr import production_session as production_module
from airpods_hr.aap import (
    AAP_HANDSHAKE_ACK,
    AAPDescriptorObservationTimeoutError,
    AAPHandshakeResult,
    AAPHandshakeSession,
    AAPHandshakeTimeoutError,
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


native_core = production_module._native

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
        close_error: BaseException | None = None,
        snapshot_error_at: int | None = None,
    ) -> None:
        self.events = events
        self.preflight_error = preflight_error
        self.close_error = close_error
        self.snapshot_error_at = snapshot_error_at
        self.close_calls = 0
        self.snapshot_calls = 0
        self._cleanup_complete = True

    @property
    def cleanup_complete(self) -> bool:
        return self._cleanup_complete

    async def connect(self) -> None:
        self.events.append("client_connect")
        self._cleanup_complete = False

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
        if self.snapshot_calls == self.snapshot_error_at:
            raise TimeoutError("synthetic cleanup checkpoint failure")
        return state()

    def close(self) -> None:
        self.events.append("client_close")
        self.close_calls += 1
        if self.close_error is not None:
            self._cleanup_complete = False
            raise self.close_error
        self._cleanup_complete = True


class FakeRegistration:
    def __init__(
        self,
        events: list[str],
        *,
        register_error: BaseException | None = None,
        unregister_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.register_error = register_error
        self.unregister_error = unregister_error
        self.register_calls = 0
        self.unregister_calls = 0
        self.registered_count = 0
        self._cleanup_complete = True

    @property
    def cleanup_complete(self) -> bool:
        return self._cleanup_complete

    async def register(self, selected_state) -> None:
        self.events.append("register")
        self.register_calls += 1
        self.selected_state = selected_state
        self._cleanup_complete = False
        if self.register_error is not None:
            raise self.register_error

    async def unregister(self) -> None:
        self.events.append("unregister")
        self.unregister_calls += 1
        if self.unregister_error is not None:
            self._cleanup_complete = False
            raise self.unregister_error
        self._cleanup_complete = True


class FakeTransport:
    def __init__(
        self,
        events: list[str],
        frames: list[bytes] | None = None,
        *,
        open_error: BaseException | None = None,
        collection_exit_error: BaseException | None = None,
        close_error: BaseException | None = None,
        imtu: int = 2048,
    ) -> None:
        self.events = events
        self.frames: asyncio.Queue[bytes] = asyncio.Queue()
        for frame in frames or []:
            self.frames.put_nowait(frame)
        self.open_error = open_error
        self.collection_exit_error = collection_exit_error
        self.close_error = close_error
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
        self._cleanup_complete = True

    @property
    def cleanup_complete(self) -> bool:
        return self._cleanup_complete

    async def open(self, local_address: str, remote_address: str) -> None:
        self.events.append("transport_open")
        self.open_calls.append((local_address, remote_address))
        self._cleanup_complete = False
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
            if self.collection_exit_error is not None:
                raise self.collection_exit_error

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
        if self.close_error is not None:
            self._cleanup_complete = False
            raise self.close_error
        self._cleanup_complete = True


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
    async def test_cleanup_failure_never_claims_complete_release(self) -> None:
        cases = (
            "collection_exit",
            "transport_close",
            "registration_unregister",
            "client_close",
        )
        for boundary in cases:
            with self.subTest(boundary=boundary):
                events: list[str] = []
                failure = RuntimeError(f"{boundary} failed")
                client = FakeClient(
                    events,
                    close_error=(failure if boundary == "client_close" else None),
                )
                registration = FakeRegistration(
                    events,
                    unregister_error=(
                        failure
                        if boundary == "registration_unregister"
                        else None
                    ),
                )
                transport = FakeTransport(
                    events,
                    collection_exit_error=(
                        failure if boundary == "collection_exit" else None
                    ),
                    close_error=(
                        failure if boundary == "transport_close" else None
                    ),
                )
                session, *_ = make_session(
                    events=events,
                    client=client,
                    registration=registration,
                    transport=transport,
                )
                await session.open()

                with self.assertRaises(ProductionSessionError) as raised:
                    await session.close()

                self.assertEqual(
                    raised.exception.category,
                    ProductionSessionCategory.CLEANUP_FAILED,
                )
                self.assertIs(session.state, ProductionSessionState.FAILED)
                self.assertFalse(session.cleanup_complete)
                self.assertEqual(transport.collect_exits, 1)
                self.assertEqual(transport.close_calls, 1)
                self.assertEqual(registration.unregister_calls, 1)
                self.assertEqual(client.close_calls, 1)
                if boundary == "collection_exit":
                    self.assertTrue(session._collection_entered)
                    self.assertIsNotNone(session._collection_context)
                elif boundary == "transport_close":
                    self.assertTrue(session._transport_owned)
                elif boundary == "registration_unregister":
                    self.assertTrue(session._registration_owned)
                else:
                    self.assertTrue(session._client_connected)

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

    async def test_cleanup_checkpoint_error_does_not_erase_release_proof(
        self,
    ) -> None:
        events: list[str] = []
        client = FakeClient(events, snapshot_error_at=4)
        session, _, registration, transport, _, _ = make_session(
            events=events, client=client
        )
        await session.open()

        with self.assertRaises(ProductionSessionError) as raised:
            await session.close()

        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.CLEANUP_FAILED,
        )
        self.assertIs(session.state, ProductionSessionState.CLOSED)
        self.assertTrue(session.cleanup_complete)
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(client.close_calls, 1)

    async def test_cleanup_cancellation_remains_control_flow(self) -> None:
        events: list[str] = []
        cancellation = asyncio.CancelledError()
        transport = FakeTransport(events, close_error=cancellation)
        session, client, registration, _, _, _ = make_session(
            events=events, transport=transport
        )
        await session.open()

        with self.assertRaises(asyncio.CancelledError) as raised:
            await session.close()

        self.assertIs(raised.exception, cancellation)
        self.assertIs(session.state, ProductionSessionState.FAILED)
        self.assertFalse(session.cleanup_complete)
        self.assertTrue(session._transport_owned)
        self.assertEqual(registration.unregister_calls, 1)
        self.assertEqual(client.close_calls, 1)

    async def test_cleanup_retry_cannot_retroactively_prove_release(self) -> None:
        events: list[str] = []
        transport = FakeTransport(
            events, close_error=RuntimeError("transport close failed")
        )
        session, *_ = make_session(events=events, transport=transport)
        await session.open()

        with self.assertRaises(ProductionSessionError):
            await session.close()
        transport.close_error = None
        with self.assertRaises(ProductionSessionError) as retried:
            await session.close()

        self.assertEqual(
            retried.exception.category,
            ProductionSessionCategory.CLEANUP_FAILED,
        )
        self.assertIs(session.state, ProductionSessionState.FAILED)
        self.assertFalse(session.cleanup_complete)
        self.assertTrue(session._release_unproven)
        self.assertEqual(transport.close_calls, 2)

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
        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()
        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.AAP_DESCRIPTOR_TIMEOUT,
        )
        self.assertTrue(raised.exception.recoverable)
        self.assertEqual(handshake.calls, 1)
        self.assertEqual(transport.commands, [])
        self.assertNotIn("device_connect", events)
        self.assertNotIn("device_disconnect", events)
        self.assertEqual(client.close_calls, 1)

    async def test_aap_ack_timeout_has_specific_recoverable_category(self) -> None:
        events: list[str] = []
        handshake = FakeHandshake(
            events, error=AAPHandshakeTimeoutError("exact ACK absent")
        )
        session, *_ = make_session(events=events, handshake=handshake)

        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()

        self.assertEqual(
            raised.exception.category, ProductionSessionCategory.AAP_ACK_TIMEOUT
        )
        self.assertTrue(raised.exception.recoverable)
        self.assertIsInstance(
            raised.exception.__cause__, AAPHandshakeTimeoutError
        )

    async def test_aap_timeout_type_outside_handshake_keeps_phase_category(
        self,
    ) -> None:
        events: list[str] = []
        session, *_ = make_session(
            events=events,
            client=FakeClient(
                events,
                preflight_error=AAPHandshakeTimeoutError("synthetic preflight"),
            ),
        )

        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()

        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.PREFLIGHT_FAILED,
        )
        self.assertTrue(raised.exception.recoverable)

    async def test_descriptor_timeout_without_ack_is_not_epoch_eligible(
        self,
    ) -> None:
        events: list[str] = []
        observation = HandshakeObservation(
            ack_observed=False,
            evidence=DescriptorEvidence(sensor_framework=True),
        )
        handshake = FakeHandshake(
            events, error=AAPDescriptorObservationTimeoutError(observation)
        )
        session, *_ = make_session(events=events, handshake=handshake)

        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()

        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.DESCRIPTOR_HANDSHAKE_FAILED,
        )
        self.assertTrue(raised.exception.recoverable)

    async def test_generic_descriptor_failure_keeps_generic_category(self) -> None:
        events: list[str] = []
        handshake = FakeHandshake(
            events, error=RuntimeError("synthetic implementation failure")
        )
        session, *_ = make_session(events=events, handshake=handshake)

        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()

        self.assertEqual(
            raised.exception.category,
            ProductionSessionCategory.DESCRIPTOR_HANDSHAKE_FAILED,
        )
        self.assertFalse(raised.exception.recoverable)

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


# Differential expectations from the pre-10.5 Python guards and assignments in
# 280204e76019b2d8afceebdbe752f6d9372c298a (not from the Rust tables).
LIFECYCLE_STATES = tuple(ProductionSessionState)
LIFECYCLE_OPERATIONS = ("open", "start", "receive_report", "stop", "close")
LIFECYCLE_EVENTS = (
    "OPEN_BEGIN",
    "OPEN_SUCCEEDED",
    "OPERATION_FAILED",
    "START_BEGIN",
    "START_SUCCEEDED",
    "RECEIVE_ACTIVATION_FAILED",
    "STOP_BEGIN",
    "STOP_SUCCEEDED",
    "CLOSE_FINALIZED",
)
PARENT_LEGAL_OPERATIONS = {
    "closed": frozenset({"open", "close"}),
    "opening": frozenset({"close"}),
    "ready": frozenset({"start", "close"}),
    "starting": frozenset({"close"}),
    "streaming": frozenset({"receive_report", "stop", "close"}),
    "stopping": frozenset({"close"}),
    "failed": frozenset({"close"}),
}
# _record_failed_operation assigned FAILED unconditionally in the parent, but
# only OPENING, STARTING and STOPPING reach it through public operations;
# FAILED is also possible if an unlocked receive fails during stop cleanup.
# receive_report's failure assignment was unconditional (even after a racing
# stop/close). close() returned early from CLOSED without finalizing.
PARENT_TRANSITIONS = {
    ("closed", "OPEN_BEGIN"): "opening",
    ("opening", "OPEN_SUCCEEDED"): "ready",
    ("opening", "OPERATION_FAILED"): "failed",
    ("starting", "OPERATION_FAILED"): "failed",
    ("stopping", "OPERATION_FAILED"): "failed",
    ("failed", "OPERATION_FAILED"): "failed",
    ("ready", "START_BEGIN"): "starting",
    ("starting", "START_SUCCEEDED"): "streaming",
    ("streaming", "STOP_BEGIN"): "stopping",
    ("stopping", "STOP_SUCCEEDED"): "ready",
}


class ProductionLifecycleNativeTests(unittest.TestCase):
    """Exercise every native identity through the Python FFI, not a mock."""

    def test_parent_operation_table_and_session_adapter_for_all_35_pairs(self) -> None:
        self.assertEqual(len(LIFECYCLE_STATES), 7)
        self.assertEqual(len(LIFECYCLE_OPERATIONS), 5)
        self.assertEqual(set(PARENT_LEGAL_OPERATIONS), {state.value for state in LIFECYCLE_STATES})
        self.assertEqual(tuple(production_module._NATIVE_STATES), LIFECYCLE_STATES)
        self.assertEqual(
            tuple(operation.value for operation in production_module._ProductionOperation),
            tuple(range(5)),
        )
        for state_id, session_state in enumerate(LIFECYCLE_STATES):
            for operation_id, operation in enumerate(LIFECYCLE_OPERATIONS):
                with self.subTest(state=session_state, operation=operation):
                    session, *_ = make_session()
                    session.state = session_state
                    legal = operation in PARENT_LEGAL_OPERATIONS[session_state.value]
                    if legal:
                        self.assertEqual(
                            native_core.production_operation(state_id, operation_id),
                            state_id,
                        )
                        with patch.object(
                            native_core,
                            "production_operation",
                            wraps=native_core.production_operation,
                        ) as called:
                            session._require_operation(operation)
                        called.assert_called_once_with(state_id, operation_id)
                    else:
                        with self.assertRaises(ValueError) as native_error:
                            native_core.production_operation(state_id, operation_id)
                        self.assertEqual(native_error.exception.args, (9,))
                        with (
                            patch.object(
                                native_core,
                                "production_operation",
                                wraps=native_core.production_operation,
                            ) as called,
                            self.assertRaises(ProductionSessionStateError) as raised,
                        ):
                            session._require_operation(operation)
                        called.assert_called_once_with(state_id, operation_id)
                        self.assertEqual(raised.exception.category, ProductionSessionCategory.INVALID_STATE)
                        self.assertEqual(raised.exception.phase, operation)
                        self.assertEqual(raised.exception.detail, f"state={session_state.value}")
                        self.assertIsInstance(raised.exception.__cause__, ValueError)
                        self.assertEqual(getattr(raised.exception.__cause__, "args", None), (9,))
                    self.assertIs(session.state, session_state)

    def test_parent_event_table_and_session_adapter_for_all_126_cases(self) -> None:
        self.assertEqual(len(LIFECYCLE_EVENTS), 9)
        self.assertEqual(
            tuple(event.name for event in production_module._ProductionEvent),
            LIFECYCLE_EVENTS,
        )
        self.assertEqual(
            tuple(event.value for event in production_module._ProductionEvent),
            tuple(range(9)),
        )
        for state_id, session_state in enumerate(LIFECYCLE_STATES):
            for event_id, event_name in enumerate(LIFECYCLE_EVENTS):
                for cleanup_complete in (False, True):
                    with self.subTest(state=session_state, event=event_name, cleanup=cleanup_complete):
                        session, *_ = make_session()
                        session.state = session_state
                        if event_name == "RECEIVE_ACTIVATION_FAILED":
                            next_state = "failed"
                        elif event_name == "CLOSE_FINALIZED" and state_id != 0:
                            next_state = "closed" if cleanup_complete else "failed"
                        else:
                            next_state = PARENT_TRANSITIONS.get((session_state.value, event_name))
                        if next_state is None:
                            with self.assertRaises(ValueError) as native_error:
                                native_core.production_transition(state_id, event_id, cleanup_complete)
                            self.assertEqual(native_error.exception.args, (10,))
                            with (
                                patch.object(
                                    native_core,
                                    "production_transition",
                                    wraps=native_core.production_transition,
                                ) as called,
                                self.assertRaises(ProductionSessionError) as raised,
                            ):
                                session._advance(
                                    production_module._ProductionEvent(event_id),
                                    cleanup_complete=cleanup_complete,
                                )
                            called.assert_called_once_with(state_id, event_id, cleanup_complete)
                            self.assertEqual(raised.exception.category, ProductionSessionCategory.INVALID_STATE)
                            self.assertEqual(raised.exception.phase, "lifecycle")
                            self.assertEqual(raised.exception.detail, "native lifecycle transition failed")
                            self.assertEqual(getattr(raised.exception.__cause__, "args", None), (10,))
                            self.assertIs(session.state, session_state)
                        else:
                            expected = ProductionSessionState(next_state)
                            self.assertEqual(
                                native_core.production_transition(state_id, event_id, cleanup_complete),
                                LIFECYCLE_STATES.index(expected),
                            )
                            with patch.object(
                                native_core,
                                "production_transition",
                                wraps=native_core.production_transition,
                            ) as called:
                                session._advance(
                                    production_module._ProductionEvent(event_id),
                                    cleanup_complete=cleanup_complete,
                                )
                            called.assert_called_once_with(state_id, event_id, cleanup_complete)
                            self.assertIs(session.state, expected)

    def test_unknown_native_identities_and_python_ffi_types(self) -> None:
        for identity in range(256):
            if identity >= 7:
                with self.subTest(kind="state", identity=identity):
                    with self.assertRaises(ValueError) as raised:
                        native_core.production_operation(identity, 4)
                    self.assertEqual(raised.exception.args, (7,))
                    with self.assertRaises(ValueError) as raised:
                        native_core.production_transition(identity, 5, False)
                    self.assertEqual(raised.exception.args, (7,))
            if identity >= 5:
                with self.subTest(kind="operation", identity=identity):
                    with self.assertRaises(ValueError) as raised:
                        native_core.production_operation(0, identity)
                    self.assertEqual(raised.exception.args, (7,))
            if identity >= 9:
                with self.subTest(kind="event", identity=identity):
                    with self.assertRaises(ValueError) as raised:
                        native_core.production_transition(0, identity, False)
                    self.assertEqual(raised.exception.args, (7,))
        for invalid in (True, False, -1, 256, None, 0.0, "0", b"0"):
            with self.subTest(invalid=invalid):
                for function, args in (
                    (native_core.production_operation, (invalid, 4)),
                    (native_core.production_operation, (0, invalid)),
                    (native_core.production_transition, (invalid, 5, False)),
                    (native_core.production_transition, (0, invalid, False)),
                ):
                    with self.assertRaises(ValueError) as raised:
                        function(*args)
                    self.assertEqual(raised.exception.args, ("invalid transition identity",))
        for invalid in (0, 1, None, "true", 1.0):
            with self.subTest(cleanup_complete=invalid):
                with self.assertRaises(ValueError) as raised:
                    native_core.production_transition(0, 0, invalid)
                self.assertEqual(raised.exception.args, ("invalid cleanup_complete flag",))

    def test_adapter_rejects_unknown_state_native_errors_and_bad_return(self) -> None:
        session, *_ = make_session()
        session.state = "unknown"
        with self.assertRaises(ProductionSessionError) as raised:
            session._require_operation("open")
        self.assertEqual(raised.exception.detail, "native lifecycle operation failed")
        with self.assertRaises(ProductionSessionError) as raised:
            session._advance(production_module._ProductionEvent.OPEN_BEGIN)
        self.assertEqual(raised.exception.detail, "native lifecycle transition failed")
        session.state = ProductionSessionState.CLOSED
        with (
            patch.object(native_core, "production_transition", return_value=255),
            self.assertRaises(ProductionSessionError) as raised,
        ):
            session._advance(production_module._ProductionEvent.OPEN_BEGIN)
        self.assertIsInstance(raised.exception.__cause__, IndexError)
        self.assertIs(session.state, ProductionSessionState.CLOSED)


class ProductionLifecycleRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_from_closed_is_a_noop_without_finalization(self) -> None:
        session, client, registration, transport, _, _ = make_session()
        with patch.object(
            native_core,
            "production_transition",
            wraps=native_core.production_transition,
        ) as called:
            await session.close()
        called.assert_not_called()
        self.assertIs(session.state, ProductionSessionState.CLOSED)
        self.assertEqual(
            (client.close_calls, registration.unregister_calls, transport.close_calls),
            (0, 0, 0),
        )

    async def test_single_use_after_close_and_after_failed_open(self) -> None:
        session, client, _, transport, handshake, _ = make_session()
        await session.open()
        await session.close()
        with self.assertRaises(ProductionSessionError) as raised:
            await session.open()
        self.assertEqual(raised.exception.category, ProductionSessionCategory.INVALID_STATE)
        self.assertEqual(raised.exception.phase, "open")
        self.assertEqual(raised.exception.detail, "session objects are single-use after close or open failure")
        self.assertIs(session.state, ProductionSessionState.CLOSED)
        self.assertEqual((client.close_calls, len(transport.open_calls), handshake.calls), (1, 1, 1))

        events: list[str] = []
        failed, failed_client, _, _, _, _ = make_session(
            events=events, client=FakeClient(events, preflight_error=TimeoutError())
        )
        with self.assertRaises(ProductionSessionError):
            await failed.open()
        await failed.close()
        with self.assertRaises(ProductionSessionError) as raised:
            await failed.open()
        self.assertEqual(raised.exception.detail, "session objects are single-use after close or open failure")
        self.assertEqual(events.count("client_connect"), 1)
        self.assertEqual(failed_client.close_calls, 1)

    async def test_start_timeout_cleans_and_keeps_activation_count_zero(self) -> None:
        class NoStartAck:
            async def run_collected(self, transport, handshake, stop_event):
                del transport, handshake
                await stop_event.wait()
                return SimpleNamespace(stop_acknowledged=False)

        session, client, registration, transport, _, _ = make_session(
            monitor_factory=lambda progress: NoStartAck(), start_timeout=0.01,
        )
        await session.open()
        with self.assertRaises(ProductionSessionError) as raised:
            await session.start()
        self.assertEqual(raised.exception.category, ProductionSessionCategory.ACTIVATION_FAILED)
        self.assertEqual(raised.exception.detail, "TimeoutError")
        self.assertTrue(raised.exception.recoverable)
        self.assertIs(session.state, ProductionSessionState.FAILED)
        self.assertEqual(session.counters.hr_activations, 0)
        self.assertIsNone(session._activation_task)
        self.assertEqual((transport.close_calls, registration.unregister_calls, client.close_calls), (1, 1, 1))
        await session.close()
        self.assertIs(session.state, ProductionSessionState.CLOSED)

    async def test_activation_ends_before_start_ack_and_fails_closed(self) -> None:
        class EarlyEnd:
            async def run_collected(self, transport, handshake, stop_event):
                del transport, handshake, stop_event
                return SimpleNamespace(stop_acknowledged=False)

        session, client, registration, transport, _, _ = make_session(
            monitor_factory=lambda progress: EarlyEnd()
        )
        await session.open()
        with self.assertRaises(ProductionSessionError) as raised:
            await session.start()
        self.assertEqual(raised.exception.category, ProductionSessionCategory.ACTIVATION_FAILED)
        self.assertEqual(raised.exception.detail, "RuntimeError")
        self.assertFalse(raised.exception.recoverable)
        self.assertIs(session.state, ProductionSessionState.FAILED)
        self.assertEqual(session.counters.hr_activations, 0)
        self.assertEqual((transport.close_calls, registration.unregister_calls, client.close_calls), (1, 1, 1))

    async def test_receive_timeout_keeps_streaming_and_ended_activation_does_not_fail_state(self) -> None:
        finish = asyncio.Event()

        class EndsAfterStartAck:
            def __init__(self, progress):
                self.progress = progress

            async def run_collected(self, transport, handshake, stop_event):
                del transport, handshake, stop_event
                self.progress(HeartRateProgress.START_ACKNOWLEDGED, None)
                await finish.wait()
                return SimpleNamespace(stop_acknowledged=True)

        session, _, _, _, _, _ = make_session(monitor_factory=EndsAfterStartAck)
        await session.open()
        await session.start()
        with self.assertRaises(ProductionSessionError) as timed_out:
            await session.receive_report(timeout=0.01)
        self.assertEqual(timed_out.exception.category, ProductionSessionCategory.RECEIVE_FAILED)
        self.assertEqual(timed_out.exception.detail, "no heart-rate report arrived before timeout")
        self.assertTrue(timed_out.exception.recoverable)
        self.assertIs(session.state, ProductionSessionState.STREAMING)
        self.assertEqual(session.counters.reports_received, 0)
        self.assertFalse(session._receive_in_progress)

        finish.set()
        with self.assertRaises(ProductionSessionError) as ended:
            await session.receive_report(timeout=1)
        self.assertEqual(ended.exception.detail, "activation ended before a report arrived")
        self.assertTrue(ended.exception.recoverable)
        self.assertIs(session.state, ProductionSessionState.STREAMING)
        self.assertEqual(session.counters.reports_received, 0)
        await session.stop()
        self.assertIs(session.state, ProductionSessionState.READY)
        await session.close()

    async def test_receive_failure_racing_stop_cleanup_keeps_original_stop_error(self) -> None:
        class FailsOnStop:
            def __init__(self, progress):
                self.progress = progress

            async def run_collected(self, transport, handshake, stop_event):
                del transport, handshake
                self.progress(HeartRateProgress.START_ACKNOWLEDGED, None)
                await stop_event.wait()
                raise RuntimeError("activation failed during stop")

        session, *_ = make_session(monitor_factory=FailsOnStop)
        await session.open()
        await session.start()
        cleanup_pending = asyncio.Event()
        allow_cleanup = asyncio.Event()
        original_cleanup = session._cleanup_resources

        async def wait_before_cleanup():
            cleanup_pending.set()
            await allow_cleanup.wait()
            return await original_cleanup()

        with patch.object(session, "_cleanup_resources", side_effect=wait_before_cleanup):
            report_task = asyncio.create_task(session.receive_report(timeout=1))
            await asyncio.sleep(0)
            stop_task = asyncio.create_task(session.stop())
            await asyncio.wait_for(cleanup_pending.wait(), timeout=1)
            with self.assertRaises(ProductionSessionError) as report_error:
                await asyncio.wait_for(report_task, timeout=1)
            self.assertEqual(report_error.exception.category, ProductionSessionCategory.RECEIVE_FAILED)
            self.assertIs(session.state, ProductionSessionState.FAILED)
            allow_cleanup.set()
            with self.assertRaises(ProductionSessionError) as stop_error:
                await asyncio.wait_for(stop_task, timeout=1)
        self.assertEqual(stop_error.exception.category, ProductionSessionCategory.STOP_FAILED)
        self.assertIs(session.state, ProductionSessionState.FAILED)
        await session.close()
        self.assertIs(session.state, ProductionSessionState.CLOSED)

    async def test_missing_stop_ack_retains_recoverable_stop_error(self) -> None:
        class NoStopAck:
            def __init__(self, progress):
                self.progress = progress

            async def run_collected(self, transport, handshake, stop_event):
                del transport, handshake
                self.progress(HeartRateProgress.START_ACKNOWLEDGED, None)
                await stop_event.wait()
                return SimpleNamespace(stop_acknowledged=False)

        session, client, registration, transport, _, _ = make_session(
            monitor_factory=NoStopAck
        )
        await session.open()
        await session.start()
        with self.assertRaises(ProductionSessionError) as raised:
            await session.stop()
        self.assertEqual(raised.exception.category, ProductionSessionCategory.STOP_FAILED)
        self.assertEqual(raised.exception.detail, "canonical STOP_HR acknowledgement was not observed")
        self.assertTrue(raised.exception.recoverable)
        self.assertIs(session.state, ProductionSessionState.FAILED)
        self.assertEqual((session.counters.hr_activations, session.counters.hr_stops), (1, 0))
        self.assertEqual((transport.close_calls, registration.unregister_calls, client.close_calls), (1, 1, 1))
        await session.close()
        self.assertIs(session.state, ProductionSessionState.CLOSED)

    async def test_stop_timeout_preserves_nested_cancellation_and_releases_resources(self) -> None:
        class NeverStops:
            def __init__(self, progress):
                self.progress = progress

            async def run_collected(self, transport, handshake, stop_event):
                del transport, handshake, stop_event
                self.progress(HeartRateProgress.START_ACKNOWLEDGED, None)
                await asyncio.Event().wait()

        session, client, registration, transport, _, _ = make_session(
            monitor_factory=NeverStops, stop_timeout=0.01,
        )
        await session.open()
        await session.start()
        # asyncio.wait_for(shield(...)) embeds cancellation in the timeout's
        # context; the parent's _nested_control_flow deliberately propagates it.
        with self.assertRaises(asyncio.CancelledError):
            await session.stop()
        self.assertIs(session.state, ProductionSessionState.FAILED)
        self.assertEqual((session.counters.hr_activations, session.counters.hr_stops), (1, 0))
        self.assertIsNone(session._activation_task)
        self.assertEqual((transport.close_calls, registration.unregister_calls, client.close_calls), (1, 1, 1))
        await session.close()
        self.assertIs(session.state, ProductionSessionState.CLOSED)


def assert_only_task_10_6_policy_changed(test: unittest.TestCase) -> None:
    """Freeze every non-policy node against the exact Iteration 10.6 parent."""
    root = Path(__file__).resolve().parents[1]
    path = "src/airpods_hr/production_session.py"
    parent = ast.parse(subprocess.run(
        ["git", "--no-pager", "show", f"ea54f6376b43c26d99e7fecad2987721e609edf4:{path}"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout)
    current = ast.parse((root / path).read_text(encoding="utf-8"))

    removed_policy = {
        "_RECOVERABLE_COEXISTENCE_CATEGORIES",
        "_RECOVERABLE_OS_ERRNOS",
        "_TRANSIENT_DBUS_ERROR_NAMES",
        "_is_transient_dbus_error",
    }
    changed_policy = "_is_recoverable_session_error"
    new_policy = "_NATIVE_COEXISTENCE_CATEGORIES"

    def name(node: ast.stmt) -> str | None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                return target.id
        return None

    def split(tree: ast.Module, allowed: set[str]) -> tuple[dict[str, ast.stmt], list[ast.stmt]]:
        selected = [node for node in tree.body if name(node) in allowed]
        test.assertEqual(len(selected), len(allowed))
        test.assertEqual({name(node) for node in selected}, allowed)
        return ({name(node): node for node in selected},
                [node for node in tree.body if name(node) not in allowed])

    old_policy, parent_rest = split(parent, removed_policy | {changed_policy})
    current_policy, current_rest = split(current, {new_policy, changed_policy})
    test.assertIsInstance(old_policy[changed_policy], ast.FunctionDef)
    test.assertIsInstance(current_policy[changed_policy], ast.FunctionDef)
    test.assertEqual(
        ast.dump(current_policy[new_policy], include_attributes=False),
        ast.dump(ast.parse("_NATIVE_COEXISTENCE_CATEGORIES = tuple(CoexistenceCategory)").body[0],
                 include_attributes=False),
    )
    # The removed policy definitions and errno import must not reappear elsewhere.
    test.assertFalse(removed_policy & {name(node) for node in current_rest})
    test.assertNotIn(new_policy, {name(node) for node in parent_rest})
    errno_import = ast.dump(ast.parse("import errno").body[0], include_attributes=False)
    test.assertEqual(
        sum(ast.dump(node, include_attributes=False) == errno_import for node in parent_rest), 1
    )
    test.assertNotIn(errno_import, [ast.dump(node, include_attributes=False) for node in current_rest])
    parent_rest = [node for node in parent_rest
                   if ast.dump(node, include_attributes=False) != errno_import]
    test.assertEqual(
        ast.dump(ast.Module(body=current_rest, type_ignores=[]), include_attributes=False),
        ast.dump(ast.Module(body=parent_rest, type_ignores=[]), include_attributes=False),
    )


class ProductionLifecycleParentASTTests(unittest.TestCase):
    """Freeze parent lifecycle work other than the precise Iteration 10.5 substitutions."""

    PARENT = "280204e76019b2d8afceebdbe752f6d9372c298a"
    # Each old statement is taken from the parent's AST, not from its line
    # numbers. Rewriting the *expected* tree prevents a broad normalization
    # from hiding changed awaits, exception handling, cleanup or counters.
    SUBSTITUTIONS: ClassVar[dict[str, tuple[tuple[str, str], ...]]] = {
        "open": (
            (
                'if self.state is not ProductionSessionState.CLOSED:\n'
                '    raise ProductionSessionStateError("open", self.state)',
                'self._require_operation("open")',
            ),
            ('self.state = ProductionSessionState.OPENING',
             'self._advance(_ProductionEvent.OPEN_BEGIN)'),
            ('self.state = ProductionSessionState.READY',
             'self._advance(_ProductionEvent.OPEN_SUCCEEDED)'),
        ),
        "start": (
            (
                'if self.state is not ProductionSessionState.READY:\n'
                '    raise ProductionSessionStateError("start", self.state)',
                'self._require_operation("start")',
            ),
            ('self.state = ProductionSessionState.STARTING',
             'self._advance(_ProductionEvent.START_BEGIN)'),
            ('self.state = ProductionSessionState.STREAMING',
             'self._advance(_ProductionEvent.START_SUCCEEDED)'),
        ),
        "receive_report": (
            (
                'if self.state is not ProductionSessionState.STREAMING:\n'
                '    raise ProductionSessionStateError("receive_report", self.state)',
                'self._require_operation("receive_report")',
            ),
            ('self.state = ProductionSessionState.FAILED',
             'self._advance(_ProductionEvent.RECEIVE_ACTIVATION_FAILED)'),
        ),
        "stop": (
            (
                'if self.state is not ProductionSessionState.STREAMING:\n'
                '    raise ProductionSessionStateError("stop", self.state)',
                'self._require_operation("stop")',
            ),
        ),
        "close": (
            (
                'if self.state is ProductionSessionState.CLOSED:\n    return',
                'self._require_operation("close")\n'
                'if self.state is ProductionSessionState.CLOSED:\n    return',
            ),
            (
                'self.state = ProductionSessionState.CLOSED '
                'if self.cleanup_complete else ProductionSessionState.FAILED',
                'self._advance(_ProductionEvent.CLOSE_FINALIZED, '
                'cleanup_complete=self.cleanup_complete)',
            ),
        ),
        "_stop_locked": (
            ('self.state = ProductionSessionState.STOPPING',
             'self._advance(_ProductionEvent.STOP_BEGIN)'),
            ('self.state = ProductionSessionState.READY',
             'self._advance(_ProductionEvent.STOP_SUCCEEDED)'),
        ),
        "_record_failed_operation": (
            ('self.state = ProductionSessionState.FAILED',
             'self._advance(_ProductionEvent.OPERATION_FAILED)'),
        ),
    }
    # These four methods are deliberately not transformed: their full AST is
    # compared to the parent along with __init__, cleanup_complete and all
    # other methods. No new await/cancellation/resource/counter paths allowed.
    UNCHANGED_LIFECYCLE_METHODS: ClassVar[set[str]] = {
        "_wait_for_activation_start",
        "_abort_activation",
        "_cancel_activation_task",
        "_cleanup_resources",
    }
    ADAPTERS: ClassVar[tuple[str, str]] = (
        (
            'def _require_operation(self, operation: str) -> None:\n'
        '    try:\n'
        '        _native.production_operation(\n'
        '            _NATIVE_STATES.index(self.state),\n'
        '            _ProductionOperation[operation.upper()].value,\n'
        '        )\n'
        '    except ValueError as error:\n'
        '        if error.args == (9,):\n'
        '            raise ProductionSessionStateError(operation, self.state) from error\n'
        '        raise ProductionSessionError(\n'
        '            ProductionSessionCategory.INVALID_STATE,\n'
        '            operation,\n'
        '            "native lifecycle operation failed",\n'
            '        ) from error\n'
        ),
        (
            'def _advance(self, event: _ProductionEvent, *, cleanup_complete: bool = False) -> None:\n'
        '    try:\n'
        '        next_state = _native.production_transition(\n'
        '            _NATIVE_STATES.index(self.state), event.value, cleanup_complete\n'
        '        )\n'
        '        self.state = _NATIVE_STATES[next_state]\n'
        '    except (ValueError, IndexError, TypeError) as error:\n'
        '        raise ProductionSessionError(\n'
        '            ProductionSessionCategory.INVALID_STATE,\n'
        '            "lifecycle",\n'
        '            "native lifecycle transition failed",\n'
            '        ) from error\n'
        ),
    )

    @staticmethod
    def _dump(node: ast.AST) -> str:
        return ast.dump(node, include_attributes=False)

    def test_only_whitelisted_parent_ast_guards_and_assignments_changed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        parent_source = subprocess.run(
            ["git", "--no-pager", "show", f"{self.PARENT}:src/airpods_hr/production_session.py"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout
        parent = ast.parse(parent_source)
        current = ast.parse(
            (root / "src/airpods_hr/production_session.py").read_text(encoding="utf-8")
        )
        parent_class = next(
            n for n in parent.body
            if isinstance(n, ast.ClassDef) and n.name == "InternalProductionSession"
        )
        current_class = next(
            n for n in current.body
            if isinstance(n, ast.ClassDef) and n.name == "InternalProductionSession"
        )
        method_types = (ast.FunctionDef, ast.AsyncFunctionDef)
        parent_methods = [n for n in parent_class.body if isinstance(n, method_types)]
        current_methods = [n for n in current_class.body if isinstance(n, method_types)]
        self.assertEqual(
            [self._dump(n) for n in current_class.body if not isinstance(n, method_types)],
            [self._dump(n) for n in parent_class.body if not isinstance(n, method_types)],
        )
        self.assertTrue(self.UNCHANGED_LIFECYCLE_METHODS.isdisjoint(self.SUBSTITUTIONS))
        self.assertTrue(self.UNCHANGED_LIFECYCLE_METHODS <= {n.name for n in parent_methods})
        self.assertEqual(
            set(self.SUBSTITUTIONS),
            {"open", "start", "receive_report", "stop", "close",
             "_stop_locked", "_record_failed_operation"},
        )
        adapters = {"_require_operation", "_advance"}
        self.assertEqual(
            [n.name for n in current_methods if n.name not in adapters],
            [n.name for n in parent_methods],
        )
        self.assertEqual(
            [n.name for n in current_methods if n.name in adapters],
            ["_require_operation", "_advance"],
        )
        for source, name in zip(self.ADAPTERS, ("_require_operation", "_advance"), strict=True):
            actual = next(n for n in current_methods if n.name == name)
            self.assertEqual(self._dump(actual), self._dump(ast.parse(source).body[0]), name)

        class ReplaceOnlyListedStatements(ast.NodeTransformer):
            def __init__(self, substitutions):
                self.replacements = {
                    ast.dump(ast.parse(before).body[0]): ast.parse(after).body
                    for before, after in substitutions
                }
                self.seen = {key: 0 for key in self.replacements}

            def visit(self, node):
                if isinstance(node, ast.stmt):
                    key = ast.dump(node)
                    if key in self.replacements:
                        self.seen[key] += 1
                        return copy.deepcopy(self.replacements[key])
                return super().visit(node)

        current_by_name = {n.name: n for n in current_methods}
        for old in parent_methods:
            expected = copy.deepcopy(old)
            if old.name in self.SUBSTITUTIONS:
                transform = ReplaceOnlyListedStatements(self.SUBSTITUTIONS[old.name])
                expected = transform.visit(expected)
                self.assertTrue(
                    all(count == 1 for count in transform.seen.values()),
                    (old.name, transform.seen),
                )
            self.assertEqual(self._dump(current_by_name[old.name]), self._dump(expected), old.name)

        # The newer parent freezes class metadata and all unrelated top-level code.
        assert_only_task_10_6_policy_changed(self)


class ProductionStaticSafetyTests(unittest.TestCase):
    def test_frozen_protocol_transport_monitor_and_semantics_hashes(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        expected = {
            "src/airpods_hr/protocol.py": (
                "b4d1daea0582841e48ba9efc3a8a7d4d74bba9b69cdbf54d3767b8bb45afecca"
            ),
            # Iteration 10.1 parser compatibility is covered by golden and native tests.
            "src/airpods_hr/bluez_coexistence.py": (
                "d0e666932d485a9f4ccc64f7d2c946fd1d14af2f6e9d8c1c8ceb263f0c52da25"
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
