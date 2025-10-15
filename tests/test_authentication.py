"""Hardware-independent tests for the Classic authentication-only session."""

from __future__ import annotations

import logging
import unittest
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from bumble.core import PhysicalTransport
from bumble.device import Device
from bumble.hci import Address
from airpods_hr.address import BluetoothAddress
from airpods_hr.authentication import BumbleClassicRuntimeFactory, CapturingHCITransportBackend, ControllerHandoffTransport
from airpods_hr.bluetooth import AdapterState, ControllerHandoff
from airpods_hr.bumble_keys import InMemoryBumbleKeyStore
from airpods_hr.classic_diagnostics import RuntimeNameProfile, runtime_name_for_profile
from airpods_hr.logging_safety import BUMBLE_SECRET_SAFE_LEVEL, BUMBLE_SENSITIVE_LOGGERS, harden_bumble_logging
from airpods_hr.pairing import BluetoothLinkKey, ClassicPairingCredentials


def synthetic_address(start: int) -> BluetoothAddress:
    return BluetoothAddress.parse(
        ":".join(f"{start + offset:02X}" for offset in range(6))
    )



def synthetic_credentials() -> ClassicPairingCredentials:
    return ClassicPairingCredentials(
        link_key=BluetoothLinkKey(bytes(range(16))),
        link_key_type=8,
        authenticated=True,
        pin_length=0,
    )



class FakeBlueZAdapterBackend:
    def __init__(self) -> None:
        self.state = AdapterState(name="hci0", index=0, powered=False)

    async def ensure_available(self) -> None:
        return None

    async def get_adapter(self, adapter_name: str) -> AdapterState:
        if adapter_name != self.state.name:
            raise AssertionError("unexpected synthetic adapter")
        return self.state

    async def set_powered(self, adapter_name: str, powered: bool) -> None:
        self.state = AdapterState(name=adapter_name, index=0, powered=powered)



class FakeHCITransportBackend:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.transport = object()

    async def ensure_available(self) -> None:
        return None

    @asynccontextmanager
    async def acquire(self, adapter_index: int):
        self.assert_adapter_index(adapter_index)
        self.events.append("transport_acquire")
        try:
            yield self.transport
        finally:
            self.events.append("transport_release")

    @staticmethod
    def assert_adapter_index(adapter_index: int) -> None:
        if adapter_index != 0:
            raise AssertionError("unexpected synthetic adapter index")



class BumbleRuntimeFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_factory_uses_classic_only_bredr_configuration(self) -> None:
        raw_connection = SimpleNamespace(
            authenticated=False,
            encryption=0,
            authenticate=AsyncMock(),
            encrypt=AsyncMock(),
            disconnect=AsyncMock(),
        )
        device = SimpleNamespace(
            keystore=None,
            power_on=AsyncMock(),
            power_off=AsyncMock(),
            connect=AsyncMock(return_value=raw_connection),
            send_sync_command=AsyncMock(),
        )
        transport = SimpleNamespace(source=object(), sink=object())
        peer = synthetic_address(40)
        store = InMemoryBumbleKeyStore({peer: synthetic_credentials()})

        with patch.object(
            Device,
            "from_config_with_hci",
            return_value=device,
        ) as create_device:
            async with BumbleClassicRuntimeFactory().open(
                transport, store
            ) as runtime:
                await runtime.connect(peer)

        config = create_device.call_args.args[0]
        self.assertTrue(config.classic_enabled)
        self.assertFalse(config.le_enabled)
        self.assertEqual(
            config.name,
            runtime_name_for_profile(RuntimeNameProfile.PROJECT_DEFAULT),
        )
        self.assertIs(device.keystore, store)
        connected_address = device.connect.call_args.args[0]
        self.assertIsInstance(connected_address, Address)
        self.assertTrue(connected_address.is_public)
        self.assertEqual(
            device.connect.call_args.kwargs["transport"],
            PhysicalTransport.BR_EDR,
        )
        self.assertEqual(device.connect.call_args.kwargs["timeout"], 20.0)
        device.power_on.assert_awaited_once()
        device.power_off.assert_awaited_once()
        device.send_sync_command.assert_not_awaited()

    async def test_factory_legacy_name_profile_is_opt_in(self) -> None:
        device = SimpleNamespace(
            keystore=None,
            power_on=AsyncMock(),
            power_off=AsyncMock(),
        )
        transport = SimpleNamespace(source=object(), sink=object())
        store = InMemoryBumbleKeyStore({})

        with patch.object(
            Device, "from_config_with_hci", return_value=device
        ) as create_device:
            async with BumbleClassicRuntimeFactory(
                runtime_name_profile=RuntimeNameProfile.LEGACY_POC
            ).open(transport, store):
                pass

        config = create_device.call_args.args[0]
        self.assertEqual(
            config.name,
            runtime_name_for_profile(RuntimeNameProfile.LEGACY_POC),
        )
        self.assertTrue(config.classic_enabled)
        self.assertFalse(config.le_enabled)

    async def test_accepted_handoff_exposes_and_releases_active_transport(
        self,
    ) -> None:
        events: list[str] = []
        delegate = FakeHCITransportBackend(events)
        captured = CapturingHCITransportBackend(delegate)
        controller = ControllerHandoff(FakeBlueZAdapterBackend(), captured)
        handoff = ControllerHandoffTransport(controller, captured)

        async with handoff.acquire("hci0") as active:
            self.assertIs(active, delegate.transport)
            self.assertIs(captured.require_active_transport(), active)

        self.assertEqual(events, ["transport_acquire", "transport_release"])
        with self.assertRaises(RuntimeError):
            captured.require_active_transport()



class BumbleLoggingSafetyTests(unittest.TestCase):
    def test_sensitive_logger_levels_are_hardened_before_authentication(self) -> None:
        logger_names = set(BUMBLE_SENSITIVE_LOGGERS)
        logger_names.update(
            name
            for name in logging.Logger.manager.loggerDict
            if name == "bumble" or name.startswith("bumble.")
        )
        saved = {
            name: (
                logging.getLogger(name).level,
                logging.getLogger(name).disabled,
            )
            for name in logger_names
        }
        logger = logging.getLogger("bumble.host")
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            harden_bumble_logging()
            logger.debug(bytes(range(16)).hex())

            for name in BUMBLE_SENSITIVE_LOGGERS:
                self.assertEqual(
                    logging.getLogger(name).level,
                    BUMBLE_SECRET_SAFE_LEVEL,
                )
                self.assertTrue(logging.getLogger(name).disabled)
            self.assertEqual(stream.getvalue(), "")
        finally:
            logger.removeHandler(handler)
            for name, (level, disabled) in saved.items():
                logging.getLogger(name).setLevel(level)
                logging.getLogger(name).disabled = disabled

