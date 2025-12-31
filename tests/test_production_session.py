"""Hardware-independent tests for the private persistent production core."""

from __future__ import annotations


import asyncio

import unittest
from contextlib import asynccontextmanager


from airpods_hr.aap import AAPDescriptorObservationTimeoutError, AAPHandshakeResult, DescriptorEvidence, HandshakeObservation


from airpods_hr.address import BluetoothAddress
from airpods_hr.bluez_coexistence import (
    BlueZCoexistenceState,
    KernelL2CAPLocalRXObservation,
)
from airpods_hr.discovery import AirPodsCandidate


from airpods_hr.production_session import InternalProductionSession, ProductionSessionCategory, ProductionSessionError, ProductionSessionState


from airpods_hr.protocol import HeartRateCommand


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
    handshake=None,
    monitor_factory=None,
    start_timeout: float = 1.0,
    stop_timeout: float = 1.0,
):
    event_log = events if events is not None else []
    client = FakeClient(event_log)
    registration = FakeRegistration(event_log)
    transport = FakeTransport(event_log, frames)
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


class ProductionSessionStateTests(unittest.IsolatedAsyncioTestCase):


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


