"""Hardware-independent tests for the BlueZ coexistence probe."""

from __future__ import annotations


import ctypes
import socket
import struct
import unittest

from dataclasses import fields


from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree


from airpods_hr.address import BluetoothAddress
from airpods_hr.bluez_coexistence import BlueZCompatibilityRegistration, BlueZCoexistenceState, CoexistenceCategory, CoexistenceFailure, CoexistenceHRStreamObservation, CoexistencePhase, DBusNextBlueZCoexistenceClient, KernelL2CAPLocalRXObservation, KernelL2CAPTransport, _AAP_LOCAL_RX_IMTU, _L2CAP_IMTU_OFFSET, _L2CAP_OPTIONS_SIZE, _LINUX_L2CAP_OPTIONS, _LINUX_SOL_L2CAP, _NativeL2CAPOptions


from airpods_hr.discovery import AirPodsCandidate
from airpods_hr.heart_rate_session import DEFAULT_CONTROL_SUMMARY_LIMIT


from airpods_hr.protocol import AAP_PSM, HEART_RATE_MARKER, HeartRateCommand
from airpods_hr.sdp import (
    USBAdapterIdentity,
    build_bluez_sdp_service_records,
)


ALL_COMPATIBILITY_UUIDS = frozenset(
    record.uuid for record in build_bluez_sdp_service_records(
        USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)
    )
)


LOCAL_ADAPTER_ADDRESS = "00:11:22:33:44:55"
REMOTE_AIRPODS_ADDRESS = "AA:BB:CC:DD:EE:FF"


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
    ) -> None:
        self.frames = list(frames or [])
        self.connect_error = connect_error
        self.security_error = security_error
        self.l2cap_getsockopt_results = list(l2cap_getsockopt_results or [])
        self.l2cap_setsockopt_error = l2cap_setsockopt_error
        self.l2cap_options = l2cap_options()
        self.local_endpoint = local_endpoint
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


class FakeSocketModule:
    AF_BLUETOOTH = socket.AF_BLUETOOTH
    SOCK_SEQPACKET = socket.SOCK_SEQPACKET
    BTPROTO_L2CAP = socket.BTPROTO_L2CAP
    SOL_BLUETOOTH = socket.SOL_BLUETOOTH
    BT_SECURITY = socket.BT_SECURITY
    BT_SECURITY_MEDIUM = socket.BT_SECURITY_MEDIUM
    SOL_L2CAP = _LINUX_SOL_L2CAP
    L2CAP_OPTIONS = _LINUX_L2CAP_OPTIONS


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
        first_profile = profile_client.register_profile.await_args_list[0].args[1]
        first_profile.Release()
        first_profile.Release()
        self.assertTrue(first_profile.released)
        await registration.unregister()
        self.assertEqual(profile_client.unregister_profile.await_count, 4)
        await registration.unregister()
        self.assertEqual(profile_client.unregister_profile.await_count, 4)

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
            socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP
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
        self.assertEqual(security[1:3], (socket.SOL_BLUETOOTH, socket.BT_SECURITY))
        self.assertEqual(security[3], bytes((socket.BT_SECURITY_MEDIUM, 0)))
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
            AF_BLUETOOTH=socket.AF_BLUETOOTH,
            SOCK_SEQPACKET=socket.SOCK_SEQPACKET,
            BTPROTO_L2CAP=socket.BTPROTO_L2CAP,
            SOL_BLUETOOTH=socket.SOL_BLUETOOTH,
            BT_SECURITY=socket.BT_SECURITY,
            BT_SECURITY_MEDIUM=socket.BT_SECURITY_MEDIUM,
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
            AF_BLUETOOTH=socket.AF_BLUETOOTH,
            SOCK_SEQPACKET=socket.SOCK_SEQPACKET,
            BTPROTO_L2CAP=socket.BTPROTO_L2CAP,
            SOL_BLUETOOTH=socket.SOL_BLUETOOTH,
            BT_SECURITY=socket.BT_SECURITY,
            BT_SECURITY_MEDIUM=socket.BT_SECURITY_MEDIUM,
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


