"""Hardware-independent tests for the pre-authentication diagnostics."""

from __future__ import annotations

import asyncio
import importlib.metadata
import inspect
import unittest
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bumble import hci
from bumble.device import Device
from bumble.host import Host
from pyee import EventEmitter

from airpods_hr.pre_auth_diagnostics import (
    PRE_AUTH_DELAY_SECONDS,
    SUPPORTED_BUMBLE_VERSION,
    BumblePreAuthCompatibilityError,
    BumbleRemoteDiscovery,
    PreAuthSequenceError,
    PreAuthSequenceMode,
    PreAuthSequenceStrategy,
    RemoteDiscoveryResult,
    validate_bumble_pre_auth_api,
)


class FakeDevice:
    def __init__(
        self,
        events: list[str],
        *,
        supported: str = "success",
        extended: str = "success",
        name: str = "success",
    ) -> None:
        self.events = events
        self.supported = supported
        self.extended = extended
        self.name = name
        self.host = EventEmitter()
        self.connection: object | None = None

    async def send_command(self, command: object) -> object:
        assert self.connection is not None
        connection = self.connection
        if isinstance(
            command, hci.HCI_Read_Remote_Supported_Features_Command
        ):
            assert command.connection_handle == connection.handle
            self.events.append("supported_request")
            if self.supported == "command_error":
                raise RuntimeError("synthetic command rejection")
            if self.supported == "rejected":
                self.events.append("supported_rejected")
                return self._command_status(
                    command, hci.HCI_UNKNOWN_HCI_COMMAND_ERROR
                )
            self.events.append("supported_accepted")
            if self.supported == "timeout":
                return self._command_status(command)
            handle = (
                connection.handle + 1
                if self.supported == "wrong_handle"
                else connection.handle
            )
            status = 1 if self.supported == "failure" else hci.HCI_SUCCESS
            asyncio.get_running_loop().call_soon(
                self._emit_features,
                "supported_response",
                handle,
                status,
                0x0123456789ABCDEF,
                0,
                0,
            )
            return self._command_status(command)
        if isinstance(
            command, hci.HCI_Read_Remote_Extended_Features_Command
        ):
            assert command.connection_handle == connection.handle
            assert command.page_number == 1
            self.events.append("extended_request")
            if self.extended == "command_error":
                raise RuntimeError("synthetic command rejection")
            if self.extended == "rejected":
                self.events.append("extended_rejected")
                return self._command_status(
                    command, hci.HCI_UNKNOWN_HCI_COMMAND_ERROR
                )
            self.events.append("extended_accepted")
            if self.extended == "timeout":
                return self._command_status(command)
            handle = (
                connection.handle + 1
                if self.extended == "wrong_handle"
                else connection.handle
            )
            page = 0 if self.extended == "wrong_page" else 1
            status = 1 if self.extended == "failure" else hci.HCI_SUCCESS
            asyncio.get_running_loop().call_soon(
                self._emit_features,
                "extended_response",
                handle,
                status,
                0xFEDCBA9876543210,
                page,
                2,
            )
            return self._command_status(command)
        if isinstance(command, hci.HCI_Remote_Name_Request_Command):
            assert command.bd_addr == connection.peer_address
            assert (
                command.page_scan_repetition_mode
                == hci.HCI_Remote_Name_Request_Command.R2
            )
            assert command.reserved == 0
            assert command.clock_offset == 0
            self.events.append("name_request")
            if self.name == "command_error":
                raise RuntimeError("synthetic command rejection")
            if self.name == "rejected":
                self.events.append("name_rejected")
                return self._command_status(
                    command, hci.HCI_UNKNOWN_HCI_COMMAND_ERROR
                )
            self.events.append("name_accepted")
            if self.name == "timeout":
                return self._command_status(command)
            address = (
                hci.Address(bytes(range(1, 7)))
                if self.name == "wrong_address"
                else connection.peer_address
            )
            asyncio.get_running_loop().call_soon(
                self._emit_name,
                address,
                self.name == "failure",
            )
            return self._command_status(command)
        raise AssertionError("unexpected HCI command")

    @staticmethod
    def _command_status(
        command: object,
        status: int = hci.HCI_COMMAND_STATUS_PENDING,
    ) -> hci.HCI_Command_Status_Event:
        return hci.HCI_Command_Status_Event(
            status=status,
            num_hci_command_packets=1,
            command_opcode=command.op_code,
        )

    def _emit_features(
        self,
        event: str,
        handle: int,
        status: int,
        features: int,
        page: int,
        maximum_page: int,
    ) -> None:
        self.events.append(event)
        self.host.emit(
            "classic_remote_features",
            handle,
            status,
            features,
            page,
            maximum_page,
        )

    def _emit_name(self, address: object, failed: bool) -> None:
        self.events.append("name_response")
        if failed:
            self.host.emit("remote_name_failure", address, 1)
        else:
            self.host.emit("remote_name", address, b"PRIVATE NAME")


class FakeConnection:
    def __init__(self, device: FakeDevice, events: list[str]) -> None:
        self.events = events
        self.authenticated = False
        self.encrypted = False
        self._connection = SimpleNamespace(
            device=device,
            handle=0x0123,
            peer_address=hci.Address(bytes(range(6))),
        )
        device.connection = self._connection

    async def authenticate(self) -> None:
        self.events.append("authenticate")
        self.authenticated = True

    async def encrypt(self) -> None:
        self.events.append("encrypt")
        self.encrypted = True

    async def disconnect(self) -> None:
        self.events.append("disconnect")


def fixture(
    *,
    supported: str = "success",
    extended: str = "success",
    name: str = "success",
) -> tuple[list[str], FakeDevice, FakeConnection]:
    events: list[str] = []
    device = FakeDevice(
        events, supported=supported, extended=extended, name=name
    )
    return events, device, FakeConnection(device, events)


class BumbleAPIContractTests(unittest.TestCase):
    def test_installed_bumble_0_0_234_contract_is_supported(self) -> None:
        self.assertEqual(
            importlib.metadata.version("bumble"), SUPPORTED_BUMBLE_VERSION
        )
        self.assertTrue(hasattr(Device, "send_command"))
        self.assertTrue(hasattr(Device, "send_async_command"))
        validate_bumble_pre_auth_api()

    def test_implementation_uses_verified_send_command_api(self) -> None:
        source = Path(
            inspect.getsourcefile(BumbleRemoteDiscovery) or ""
        ).read_text(encoding="utf-8")
        self.assertIn("device.send_command(command)", source)
        self.assertNotIn("device.send_async_command", source)
        self.assertTrue(hasattr(FakeDevice, "send_command"))
        self.assertFalse(hasattr(FakeDevice, "send_async_command"))

    def test_host_event_names_and_handler_signatures_match_reviewed_source(
        self,
    ) -> None:
        handlers = (
            (
                Host.on_hci_read_remote_supported_features_complete_event,
                ("classic_remote_features",),
            ),
            (
                Host.on_hci_read_remote_extended_features_complete_event,
                ("classic_remote_features",),
            ),
            (
                Host.on_hci_remote_name_request_complete_event,
                ("remote_name", "remote_name_failure"),
            ),
        )
        for handler, event_names in handlers:
            with self.subTest(handler=handler.__name__):
                self.assertEqual(
                    tuple(inspect.signature(handler).parameters),
                    ("self", "event"),
                )
                source = inspect.getsource(handler)
                for event_name in event_names:
                    self.assertIn(repr(event_name), source)

    def test_actual_host_emitters_use_reviewed_callback_argument_order(
        self,
    ) -> None:
        host = Host()
        features: list[tuple[object, ...]] = []
        names: list[tuple[object, ...]] = []
        failures: list[tuple[object, ...]] = []
        host.on("classic_remote_features", lambda *args: features.append(args))
        host.on("remote_name", lambda *args: names.append(args))
        host.on("remote_name_failure", lambda *args: failures.append(args))
        handle = 0x0123
        address = hci.Address(bytes(range(6)))

        host.on_hci_read_remote_supported_features_complete_event(
            hci.HCI_Read_Remote_Supported_Features_Complete_Event(
                status=hci.HCI_SUCCESS,
                connection_handle=handle,
                lmp_features=bytes.fromhex("0102030405060708"),
            )
        )
        host.on_hci_read_remote_extended_features_complete_event(
            hci.HCI_Read_Remote_Extended_Features_Complete_Event(
                status=hci.HCI_SUCCESS,
                connection_handle=handle,
                page_number=1,
                maximum_page_number=2,
                extended_lmp_features=bytes.fromhex("1020304050607080"),
            )
        )
        host.on_hci_remote_name_request_complete_event(
            hci.HCI_Remote_Name_Request_Complete_Event(
                status=hci.HCI_SUCCESS,
                bd_addr=address,
                remote_name=b"TEST\0",
            )
        )
        host.on_hci_remote_name_request_complete_event(
            hci.HCI_Remote_Name_Request_Complete_Event(
                status=hci.HCI_UNKNOWN_HCI_COMMAND_ERROR,
                bd_addr=address,
                remote_name=b"",
            )
        )

        self.assertEqual(
            features,
            [
                (handle, hci.HCI_SUCCESS, 0x0807060504030201, 0, 0),
                (handle, hci.HCI_SUCCESS, 0x8070605040302010, 1, 2),
            ],
        )
        self.assertEqual(names, [(address, b"TEST")])
        self.assertEqual(
            failures, [(address, hci.HCI_UNKNOWN_HCI_COMMAND_ERROR)]
        )

    def test_unreviewed_bumble_version_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            BumblePreAuthCompatibilityError, "unsupported Bumble version"
        ):
            validate_bumble_pre_auth_api(installed_version="0.0.235")

    def test_replaced_send_command_method_fails_closed(self) -> None:
        async def replacement(device, command, check_result=False):
            del device, command, check_result

        with patch.object(Device, "send_command", replacement):
            with self.assertRaisesRegex(
                BumblePreAuthCompatibilityError, "handlers were replaced"
            ):
                validate_bumble_pre_auth_api(
                    installed_version=SUPPORTED_BUMBLE_VERSION
                )


class PreAuthModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_proven_runtime_delegates_authentication_immediately(self) -> None:
        events, _, connection = fixture()
        sleep = AsyncMock()
        discovery = AsyncMock()
        strategy = PreAuthSequenceStrategy(
            PreAuthSequenceMode.PROVEN,
            sleep=sleep,
            remote_discovery=discovery,
        )
        await strategy.before_authentication(connection)
        await connection.authenticate()
        self.assertEqual(events, ["authenticate"])
        sleep.assert_not_awaited()
        discovery.run.assert_not_awaited()
        self.assertTrue(strategy.observation.authentication_attempted)

    async def test_proven_mode_adds_no_delay_or_discovery(self) -> None:
        sleep = AsyncMock()
        discovery = AsyncMock()
        strategy = PreAuthSequenceStrategy(
            PreAuthSequenceMode.PROVEN,
            sleep=sleep,
            remote_discovery=discovery,
        )
        await strategy.run(object())
        sleep.assert_not_awaited()
        discovery.run.assert_not_awaited()
        selected = strategy.observation
        self.assertEqual(selected.delay_ms, 0)
        self.assertFalse(selected.remote_supported_features_request_sent)

    async def test_delay_only_precedes_authentication_without_discovery(self) -> None:
        events, _, connection = fixture()

        async def sleep(delay: float) -> None:
            self.assertEqual(delay, PRE_AUTH_DELAY_SECONDS)
            events.append("delay")

        discovery = AsyncMock()
        strategy = PreAuthSequenceStrategy(
            PreAuthSequenceMode.DELAY_ONLY,
            sleep=sleep,
            remote_discovery=discovery,
        )
        await strategy.before_authentication(connection)
        await connection.authenticate()
        self.assertEqual(events, ["delay", "authenticate"])
        discovery.run.assert_not_awaited()
        self.assertEqual(strategy.observation.delay_ms, 85)
        self.assertTrue(strategy.observation.authentication_attempted)

    async def test_bluez_discovery_order_precedes_authentication(self) -> None:
        events, device, connection = fixture()
        strategy = PreAuthSequenceStrategy(
            PreAuthSequenceMode.BLUEZ_DISCOVERY,
            remote_discovery=BumbleRemoteDiscovery(operation_timeout=0.1),
        )
        await strategy.before_authentication(connection)
        await connection.authenticate()
        self.assertEqual(
            events,
            [
                "supported_request",
                "supported_accepted",
                "supported_response",
                "extended_request",
                "extended_accepted",
                "extended_response",
                "name_request",
                "name_accepted",
                "name_response",
                "authenticate",
            ],
        )
        selected = strategy.observation
        self.assertEqual(
            selected.remote_supported_features_mask, 0x0123456789ABCDEF
        )
        self.assertTrue(selected.remote_supported_features_command_accepted)
        self.assertEqual(selected.remote_extended_features_page, 1)
        self.assertTrue(selected.remote_extended_features_command_accepted)
        self.assertEqual(selected.remote_extended_features_max_page, 2)
        self.assertEqual(
            selected.remote_extended_features_mask, 0xFEDCBA9876543210
        )
        self.assertTrue(selected.remote_name_response_observed)
        self.assertTrue(selected.remote_name_command_accepted)
        self.assertTrue(selected.authentication_attempted)
        for event in (
            "classic_remote_features",
            "remote_name",
            "remote_name_failure",
        ):
            self.assertEqual(device.host.listeners(event), [])

    async def test_supported_features_failure_prevents_authentication(self) -> None:
        await self._assert_failure_prevents_authentication(
            supported="failure",
            expected_result=RemoteDiscoveryResult.OTHER,
        )

    async def test_supported_command_rejection_stops_sequence_immediately(
        self,
    ) -> None:
        events, device, connection = fixture(supported="rejected")
        strategy = self._strategy()
        with self.assertRaises(PreAuthSequenceError):
            await strategy.before_authentication(connection)
        self.assertEqual(events, ["supported_request", "supported_rejected"])
        self.assertFalse(
            strategy.observation.remote_supported_features_command_accepted
        )
        self.assertFalse(strategy.observation.authentication_attempted)
        self._assert_watchers_empty(device)

    async def test_extended_command_rejection_stops_before_remote_name(
        self,
    ) -> None:
        events, device, connection = fixture(extended="rejected")
        strategy = self._strategy()
        with self.assertRaises(PreAuthSequenceError):
            await strategy.before_authentication(connection)
        self.assertEqual(
            events,
            [
                "supported_request",
                "supported_accepted",
                "supported_response",
                "extended_request",
                "extended_rejected",
            ],
        )
        self.assertFalse(
            strategy.observation.remote_extended_features_command_accepted
        )
        self.assertFalse(strategy.observation.authentication_attempted)
        self._assert_watchers_empty(device)

    async def test_remote_name_command_rejection_prevents_authentication(
        self,
    ) -> None:
        events, device, connection = fixture(name="rejected")
        strategy = self._strategy()
        with self.assertRaises(PreAuthSequenceError):
            await strategy.before_authentication(connection)
        self.assertEqual(
            events[-2:], ["name_request", "name_rejected"]
        )
        self.assertNotIn("authenticate", events)
        self.assertFalse(strategy.observation.remote_name_command_accepted)
        self.assertFalse(strategy.observation.authentication_attempted)
        self._assert_watchers_empty(device)

    async def test_extended_features_timeout_prevents_authentication(self) -> None:
        await self._assert_failure_prevents_authentication(
            extended="timeout",
            expected_result=RemoteDiscoveryResult.TIMEOUT,
        )

    async def test_extended_command_error_restores_watchers(self) -> None:
        await self._assert_failure_prevents_authentication(
            extended="command_error",
            expected_result=RemoteDiscoveryResult.OTHER,
        )

    async def test_wrong_extended_page_fails_closed(self) -> None:
        events, _, connection = fixture(extended="wrong_page")
        strategy = self._strategy()
        with self.assertRaises(PreAuthSequenceError):
            await strategy.run(connection)
        self.assertNotIn("authenticate", events)
        selected = strategy.observation
        self.assertTrue(selected.remote_extended_features_response_observed)
        self.assertEqual(
            selected.remote_extended_features_result,
            RemoteDiscoveryResult.OTHER,
        )

    async def test_remote_name_failure_prevents_authentication(self) -> None:
        await self._assert_failure_prevents_authentication(
            name="failure",
            expected_result=RemoteDiscoveryResult.OTHER,
        )

    async def test_remote_name_timeout_prevents_authentication(self) -> None:
        await self._assert_failure_prevents_authentication(
            name="timeout",
            expected_result=RemoteDiscoveryResult.TIMEOUT,
        )

    async def test_wrong_feature_handle_is_not_accepted(self) -> None:
        await self._assert_failure_prevents_authentication(
            supported="wrong_handle",
            expected_result=RemoteDiscoveryResult.TIMEOUT,
        )

    async def test_wrong_remote_name_address_is_not_accepted(self) -> None:
        await self._assert_failure_prevents_authentication(
            name="wrong_address",
            expected_result=RemoteDiscoveryResult.TIMEOUT,
        )

    async def test_cancellation_restores_all_event_watchers(self) -> None:
        events, device, connection = fixture(supported="timeout")
        strategy = PreAuthSequenceStrategy(
            PreAuthSequenceMode.BLUEZ_DISCOVERY,
            remote_discovery=BumbleRemoteDiscovery(operation_timeout=10),
        )
        task = asyncio.create_task(strategy.run(connection))
        while "supported_request" not in events:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(device.host.listeners("classic_remote_features"), [])

    def _strategy(self) -> PreAuthSequenceStrategy:
        return PreAuthSequenceStrategy(
            PreAuthSequenceMode.BLUEZ_DISCOVERY,
            remote_discovery=BumbleRemoteDiscovery(operation_timeout=0.001),
        )

    def _assert_watchers_empty(self, device: FakeDevice) -> None:
        for event in (
            "classic_remote_features",
            "remote_name",
            "remote_name_failure",
        ):
            self.assertEqual(device.host.listeners(event), [])

    async def _assert_failure_prevents_authentication(
        self,
        *,
        supported: str = "success",
        extended: str = "success",
        name: str = "success",
        expected_result: RemoteDiscoveryResult,
    ) -> None:
        events, device, connection = fixture(
            supported=supported, extended=extended, name=name
        )
        strategy = self._strategy()
        with self.assertRaises(PreAuthSequenceError):
            await strategy.before_authentication(connection)
            await connection.authenticate()
        self.assertNotIn("authenticate", events)
        self.assertFalse(strategy.observation.authentication_attempted)
        if supported != "success":
            actual = strategy.observation.remote_supported_features_result
        elif extended != "success":
            actual = strategy.observation.remote_extended_features_result
        else:
            actual = strategy.observation.remote_name_result
        self.assertEqual(actual, expected_result)
        self._assert_watchers_empty(device)


class PreAuthSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_observation_retains_decoded_metadata_only(self) -> None:
        selected = PreAuthSequenceStrategy(
            PreAuthSequenceMode.PROVEN
        ).observation
        field_names = {field.name for field in fields(selected)}
        self.assertFalse(
            field_names & {"raw", "payload", "packet", "frame", "data", "name"}
        )
        self.assertNotIn("PRIVATE NAME", repr(selected))

    def test_experiment_is_absent_from_frozen_paths(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "src/airpods_hr/monitor_cli.py",
            "src/airpods_hr/authentication.py",
            "src/airpods_hr/protocol.py",
            "tools/probe_bluez_coexistence.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("PreAuthSequenceStrategy", source)
            self.assertNotIn("bluez-discovery", source)


if __name__ == "__main__":
    unittest.main()
