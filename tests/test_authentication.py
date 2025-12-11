"""Hardware-independent tests for the Classic authentication-only session."""

from __future__ import annotations

import asyncio
import logging
import traceback
import unittest
from contextlib import asynccontextmanager, contextmanager
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bumble.core import PhysicalTransport
from bumble.device import Device
from bumble.hci import Address

from airpods_hr.address import BluetoothAddress
from airpods_hr.authentication import (
    BumbleClassicRuntimeFactory,
    CapturingHCITransportBackend,
    ClassicAuthenticationFailedError,
    ClassicAuthenticationSession,
    ClassicConnectionError,
    ClassicEncryptionFailedError,
    ControllerHandoffTransport,
)
from airpods_hr.bluetooth import AdapterState, ControllerHandoff
from airpods_hr.bumble_keys import InMemoryBumbleKeyStore
from airpods_hr.classic_diagnostics import (
    RuntimeNameProfile,
    runtime_name_for_profile,
)
from airpods_hr.discovery import (
    AirPodsCandidate,
    MultipleAirPodsCandidatesError,
    NoAirPodsCandidatesError,
)
from airpods_hr.logging_safety import (
    BUMBLE_SECRET_SAFE_LEVEL,
    BUMBLE_SENSITIVE_LOGGERS,
    harden_bumble_logging,
)
from airpods_hr.pairing import (
    BluetoothLinkKey,
    ClassicPairingCredentials,
    LinkKeySectionMissingError,
)
from airpods_hr.sdp import AdapterIdentityError, SDPCompatibilityProfile
from tools.probe_classic_auth import build_parser, run_probe


def synthetic_address(start: int) -> BluetoothAddress:
    return BluetoothAddress.parse(
        ":".join(f"{start + offset:02X}" for offset in range(6))
    )


def candidate(
    index: int = 0,
    *,
    adapter_modalias: str | None = "usb:v1234p5678d9ABC",
) -> AirPodsCandidate:
    return AirPodsCandidate(
        display_name=f"Synthetic AirPods {index + 1}",
        adapter_name="hci0",
        adapter_path="/org/bluez/hci0",
        adapter_address=synthetic_address(index),
        address=synthetic_address(index + 20),
        object_path=f"/org/bluez/hci0/dev_synthetic_{index}",
        adapter_modalias=adapter_modalias,
    )


def synthetic_credentials() -> ClassicPairingCredentials:
    return ClassicPairingCredentials(
        link_key=BluetoothLinkKey(bytes(range(16))),
        link_key_type=8,
        authenticated=True,
        pin_length=0,
    )


class FakeDiscovery:
    def __init__(self, events: list[str], candidates) -> None:
        self.events = events
        self.candidates = candidates

    async def discover_candidates(self):
        self.events.append("discovery")
        return self.candidates


class FakePairingStore:
    def __init__(
        self,
        events: list[str],
        *,
        error: Exception | None = None,
    ) -> None:
        self.events = events
        self.error = error

    def load_classic_credentials(self, adapter_address, device_address):
        del adapter_address, device_address
        self.events.append("credentials")
        if self.error is not None:
            raise self.error
        return synthetic_credentials()


class FakeHandoff:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @asynccontextmanager
    async def acquire(self, adapter_name: str):
        del adapter_name
        self.events.append("handoff")
        try:
            yield object()
        finally:
            self.events.append("release")
            self.events.append("restore")


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


class FakeConnection:
    def __init__(
        self,
        events: list[str],
        *,
        authentication_error: BaseException | None = None,
        encryption_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.authentication_error = authentication_error
        self.encryption_error = encryption_error
        self.authenticated = False
        self.encrypted = False
        self.runtime = None

    def assert_sdp_profile(self) -> None:
        if self.runtime is not None and self.runtime.expect_sdp:
            if not isinstance(self.runtime.sdp_records, dict):
                raise AssertionError("SDP profile is not active")
            if len(self.runtime.sdp_records) != 4:
                raise AssertionError("the four-record SDP profile is not active")

    async def authenticate(self) -> None:
        self.assert_sdp_profile()
        self.events.append("authenticate")
        if self.authentication_error is not None:
            raise self.authentication_error
        self.authenticated = True

    async def encrypt(self) -> None:
        self.assert_sdp_profile()
        self.events.append("encrypt")
        if self.encryption_error is not None:
            raise self.encryption_error
        self.encrypted = True

    async def disconnect(self) -> None:
        self.events.append("disconnect")


class FakeRuntime:
    def __init__(
        self,
        events: list[str],
        connection: FakeConnection,
        *,
        connection_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.connection = connection
        self.connection_error = connection_error
        self.aap_open_count = 0
        self.sdp_install_count = 0
        self.expect_sdp = False
        self.expect_sdp_diagnostics = False
        self.sdp_diagnostics_active = False
        self.original_sdp_records = {7: []}
        self.sdp_records = self.original_sdp_records
        self.connection.runtime = self

    async def connect(self, peer_address: BluetoothAddress):
        del peer_address
        if self.expect_sdp:
            if not isinstance(self.sdp_records, dict) or len(self.sdp_records) != 4:
                raise AssertionError("SDP profile was not active before connect")
        if self.expect_sdp_diagnostics and not self.sdp_diagnostics_active:
            raise AssertionError("SDP diagnostics were not active before connect")
        self.events.append("connect")
        if self.connection_error is not None:
            raise self.connection_error
        return self.connection

    @contextmanager
    def temporary_sdp_records(self, records):
        previous = self.sdp_records
        self.sdp_records = records
        self.sdp_install_count += 1
        self.events.append("sdp_install")
        try:
            yield
        finally:
            self.sdp_records = previous
            self.events.append("sdp_restore")

    @contextmanager
    def observe_sdp(self, observer):
        del observer
        self.sdp_diagnostics_active = True
        self.events.append("sdp_diagnostics_install")
        try:
            yield
        finally:
            self.sdp_diagnostics_active = False
            self.events.append("sdp_diagnostics_restore")


class FakeRuntimeFactory:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime

    @asynccontextmanager
    async def open(self, active_transport, keystore):
        del active_transport, keystore
        yield self.runtime


class ClassicAuthenticationSessionTests(unittest.IsolatedAsyncioTestCase):
    def make_session(
        self,
        *,
        candidates=None,
        pairing_error: Exception | None = None,
        connection_error: Exception | None = None,
        authentication_error: BaseException | None = None,
        encryption_error: BaseException | None = None,
        progress=None,
    ):
        events: list[str] = []
        selected = [candidate()] if candidates is None else candidates
        connection = FakeConnection(
            events,
            authentication_error=authentication_error,
            encryption_error=encryption_error,
        )
        runtime = FakeRuntime(
            events,
            connection,
            connection_error=connection_error,
        )
        session = ClassicAuthenticationSession(
            FakeDiscovery(events, selected),
            FakePairingStore(events, error=pairing_error),
            FakeHandoff(events),
            FakeRuntimeFactory(runtime),
            progress=progress,
            logging_hardener=lambda: events.append("logging"),
        )
        return session, events, runtime

    async def test_exact_orchestration_order(self) -> None:
        session, events, runtime = self.make_session()

        result = await session.run()

        self.assertEqual(
            events,
            [
                "discovery",
                "credentials",
                "logging",
                "handoff",
                "connect",
                "authenticate",
                "encrypt",
                "disconnect",
                "release",
                "restore",
            ],
        )
        self.assertEqual(result.display_name, "Synthetic AirPods 1")
        self.assertFalse(result.replacement_key_reported)
        self.assertEqual(runtime.aap_open_count, 0)
        self.assertEqual(runtime.sdp_install_count, 0)

    async def test_optional_pre_authentication_hook_runs_at_exact_boundary(
        self,
    ) -> None:
        events: list[str] = []
        connection = FakeConnection(events)
        runtime = FakeRuntime(events, connection)

        async def pre_authentication(selected_connection) -> None:
            self.assertIs(selected_connection, connection)
            events.append("pre_authentication")

        session = ClassicAuthenticationSession(
            FakeDiscovery(events, [candidate()]),
            FakePairingStore(events),
            FakeHandoff(events),
            FakeRuntimeFactory(runtime),
            logging_hardener=lambda: events.append("logging"),
            pre_authentication=pre_authentication,
        )
        await session.run()
        self.assertLess(
            events.index("connect"), events.index("pre_authentication")
        )
        self.assertLess(
            events.index("pre_authentication"), events.index("authenticate")
        )

    async def test_pre_authentication_failure_propagates_and_cleans_up(
        self,
    ) -> None:
        class SyntheticPreAuthenticationError(RuntimeError):
            pass

        events: list[str] = []
        connection = FakeConnection(events)
        runtime = FakeRuntime(events, connection)

        async def pre_authentication(selected_connection) -> None:
            self.assertIs(selected_connection, connection)
            events.append("pre_authentication")
            raise SyntheticPreAuthenticationError

        session = ClassicAuthenticationSession(
            FakeDiscovery(events, [candidate()]),
            FakePairingStore(events),
            FakeHandoff(events),
            FakeRuntimeFactory(runtime),
            logging_hardener=lambda: events.append("logging"),
            pre_authentication=pre_authentication,
        )
        with self.assertRaises(SyntheticPreAuthenticationError):
            await session.run()
        self.assertNotIn("authenticate", events)
        self.assertEqual(events[-3:], ["disconnect", "release", "restore"])

    async def test_zero_candidates_fails_before_handoff(self) -> None:
        session, events, _ = self.make_session(candidates=[])

        with self.assertRaises(NoAirPodsCandidatesError):
            await session.run()

        self.assertEqual(events, ["discovery"])

    async def test_multiple_candidates_fail_before_handoff(self) -> None:
        session, events, _ = self.make_session(
            candidates=[candidate(0), candidate(1)]
        )

        with self.assertRaises(MultipleAirPodsCandidatesError):
            await session.run()

        self.assertEqual(events, ["discovery"])

    async def test_missing_credentials_fails_before_handoff(self) -> None:
        session, events, _ = self.make_session(
            pairing_error=LinkKeySectionMissingError("synthetic missing key")
        )

        with self.assertRaises(LinkKeySectionMissingError):
            await session.run()

        self.assertEqual(events, ["discovery", "credentials"])

    async def test_connection_failure_releases_and_restores(self) -> None:
        secret = bytes(reversed(range(16))).hex()
        session, events, _ = self.make_session(
            connection_error=RuntimeError(secret)
        )

        with self.assertRaises(ClassicConnectionError) as caught:
            await session.run()

        self.assertEqual(events[-2:], ["release", "restore"])
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(secret, rendered)

    async def test_authentication_failure_disconnects_and_restores(self) -> None:
        session, events, _ = self.make_session(
            authentication_error=RuntimeError("synthetic auth failure")
        )

        with self.assertRaises(ClassicAuthenticationFailedError):
            await session.run()

        self.assertEqual(events[-3:], ["disconnect", "release", "restore"])

    async def test_encryption_failure_disconnects_and_restores(self) -> None:
        session, events, _ = self.make_session(
            encryption_error=RuntimeError("synthetic encryption failure")
        )

        with self.assertRaises(ClassicEncryptionFailedError):
            await session.run()

        self.assertEqual(events[-3:], ["disconnect", "release", "restore"])

    async def test_cancellation_during_authentication_cleans_up(self) -> None:
        session, events, _ = self.make_session(
            authentication_error=asyncio.CancelledError()
        )

        with self.assertRaises(asyncio.CancelledError):
            await session.run()

        self.assertEqual(events[-3:], ["disconnect", "release", "restore"])

    async def test_cancellation_during_encryption_cleans_up(self) -> None:
        session, events, _ = self.make_session(
            encryption_error=asyncio.CancelledError()
        )

        with self.assertRaises(asyncio.CancelledError):
            await session.run()

        self.assertEqual(events[-3:], ["disconnect", "release", "restore"])

    async def test_sdp_profile_is_active_before_connect_and_through_nested_work(self) -> None:
        events: list[str] = []
        selected = [candidate()]
        connection = FakeConnection(events)
        runtime = FakeRuntime(events, connection)
        runtime.expect_sdp = True
        runtime.expect_sdp_diagnostics = True
        session = ClassicAuthenticationSession(
            FakeDiscovery(events, selected),
            FakePairingStore(events),
            FakeHandoff(events),
            FakeRuntimeFactory(runtime),
            progress=lambda event, detail: events.append(event.value),
            logging_hardener=lambda: events.append("logging"),
        )
        class TrackingSDPProfile(SDPCompatibilityProfile):
            def prepare(self, selected_candidate):
                events.append("pnp_prepare")
                return super().prepare(selected_candidate)

        profile = TrackingSDPProfile(
            installed_callback=lambda: events.append("sdp_installed"),
            diagnostics=object(),
        )

        async with session.open(pre_connect_profile=profile):
            self.assertEqual(len(runtime.sdp_records), 4)
            events.append("nested_aap")

        self.assertIs(runtime.sdp_records, runtime.original_sdp_records)
        self.assertEqual(runtime.sdp_install_count, 1)
        self.assertEqual(
            events,
            [
                "discovery",
                "device_selected",
                "credentials",
                "pnp_prepare",
                "logging",
                "handoff",
                "sdp_install",
                "sdp_diagnostics_install",
                "sdp_installed",
                "connect",
                "connected",
                "authenticate",
                "authenticated",
                "encrypt",
                "encrypted",
                "nested_aap",
                "sdp_diagnostics_restore",
                "sdp_restore",
                "disconnect",
                "disconnected",
                "release",
                "restore",
            ],
        )

    async def test_missing_modalias_fails_before_handoff(self) -> None:
        session, events, _ = self.make_session(
            candidates=[candidate(adapter_modalias=None)]
        )

        with self.assertRaises(AdapterIdentityError):
            async with session.open(
                pre_connect_profile=SDPCompatibilityProfile()
            ):
                pass

        self.assertEqual(events, ["discovery", "credentials"])

    async def test_connection_failure_restores_pre_connect_profile(self) -> None:
        session, events, runtime = self.make_session(
            connection_error=RuntimeError("synthetic connection failure")
        )
        runtime.expect_sdp = True

        with self.assertRaises(ClassicConnectionError):
            async with session.open(
                pre_connect_profile=SDPCompatibilityProfile()
            ):
                pass

        self.assertIs(runtime.sdp_records, runtime.original_sdp_records)
        self.assertEqual(events[-3:], ["sdp_restore", "release", "restore"])

    async def test_authentication_failure_restores_profile_before_disconnect(self) -> None:
        session, events, runtime = self.make_session(
            authentication_error=RuntimeError("synthetic auth failure")
        )
        runtime.expect_sdp = True

        with self.assertRaises(ClassicAuthenticationFailedError):
            async with session.open(
                pre_connect_profile=SDPCompatibilityProfile()
            ):
                pass

        self.assertIs(runtime.sdp_records, runtime.original_sdp_records)
        self.assertEqual(
            events[-4:],
            ["sdp_restore", "disconnect", "release", "restore"],
        )

    async def test_encryption_failure_restores_profile_before_disconnect(self) -> None:
        session, events, runtime = self.make_session(
            encryption_error=RuntimeError("synthetic encryption failure")
        )
        runtime.expect_sdp = True

        with self.assertRaises(ClassicEncryptionFailedError):
            async with session.open(
                pre_connect_profile=SDPCompatibilityProfile()
            ):
                pass

        self.assertIs(runtime.sdp_records, runtime.original_sdp_records)
        self.assertEqual(
            events[-4:],
            ["sdp_restore", "disconnect", "release", "restore"],
        )

    async def test_cancellation_restores_profile_before_disconnect(self) -> None:
        session, events, runtime = self.make_session(
            authentication_error=asyncio.CancelledError()
        )
        runtime.expect_sdp = True

        with self.assertRaises(asyncio.CancelledError):
            async with session.open(
                pre_connect_profile=SDPCompatibilityProfile()
            ):
                pass

        self.assertIs(runtime.sdp_records, runtime.original_sdp_records)
        self.assertEqual(
            events[-4:],
            ["sdp_restore", "disconnect", "release", "restore"],
        )


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


class ClassicAuthenticationProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_is_dry_run_by_default(self) -> None:
        runner = AsyncMock()
        output: list[str] = []

        result = await run_probe(
            execute=False,
            output=output.append,
            live_runner=runner,
        )

        self.assertEqual(result, 0)
        runner.assert_not_awaited()
        self.assertTrue(output[0].startswith("DRY RUN"))

    async def test_execute_is_the_only_live_gate(self) -> None:
        runner = AsyncMock()

        self.assertFalse(build_parser().parse_args([]).execute)
        self.assertTrue(build_parser().parse_args(["--execute"]).execute)
        self.assertEqual(
            await run_probe(execute=True, live_runner=runner),
            0,
        )
        runner.assert_awaited_once()

    async def test_failure_output_does_not_include_synthetic_key(self) -> None:
        secret = bytes(reversed(range(16))).hex()

        async def fail(output) -> None:
            del output
            raise ClassicConnectionError(secret)

        output: list[str] = []
        result = await run_probe(
            execute=True,
            output=output.append,
            live_runner=fail,
        )

        self.assertEqual(result, 1)
        self.assertNotIn(secret, "\n".join(output))


if __name__ == "__main__":
    unittest.main()
