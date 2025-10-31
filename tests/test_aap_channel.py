"""Hardware-independent tests for the signaling-only AAP channel session."""

from __future__ import annotations

import asyncio
import inspect
import traceback
import unittest
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bumble import l2cap

from airpods_hr.aap_channel import (
    AAPChannelCloseError,
    AAPChannelOpenError,
    AAPChannelOpenTimeoutError,
    AAPChannelSession,
    AAPChannelStateError,
    AAPL2CAPProbeSession,
)
from airpods_hr.authentication import (
    AuthenticatedClassicContext,
    BumbleClassicConnection,
)
from airpods_hr.protocol import AAP_PSM
from tools.probe_aap_l2cap import build_parser, run_probe


class CompatibilityRecorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.active = False

    @contextmanager
    def __call__(self, manager):
        del manager
        if self.active:
            raise AssertionError("compatibility already active")
        self.active = True
        self.events.append("compat_enter")
        try:
            yield
        finally:
            self.events.append("compat_exit")
            self.active = False


class FakeRawChannel:
    def __init__(
        self,
        events: list[str],
        compatibility: CompatibilityRecorder,
        *,
        local_mtu: int = 2048,
        peer_mtu: int = 3001,
        mode=l2cap.TransmissionMode.BASIC,
        state=l2cap.ClassicChannel.State.OPEN,
        psm: int = AAP_PSM,
        close_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.compatibility = compatibility
        self.mtu = local_mtu
        self.peer_mtu = peer_mtu
        self.mode = mode
        self.state = state
        self.psm = psm
        self.close_error = close_error
        self.application_send_count = 0

    async def disconnect(self) -> None:
        if not self.compatibility.active:
            raise AssertionError("compatibility exited before channel close")
        self.events.append("channel_close")
        if self.close_error is not None:
            raise self.close_error

    def write(self, payload: bytes) -> None:
        del payload
        self.application_send_count += 1

    def send_pdu(self, payload: bytes) -> None:
        del payload
        self.application_send_count += 1


class FakeConnection:
    def __init__(
        self,
        events: list[str],
        compatibility: CompatibilityRecorder,
        *,
        channel: FakeRawChannel | None = None,
        open_error: BaseException | None = None,
    ) -> None:
        self.events = events
        self.compatibility = compatibility
        self.channel = channel or FakeRawChannel(events, compatibility)
        self.open_error = open_error
        self.l2cap_channel_manager = object()
        self.spec = None
        self.sdp_install_count = 0

    async def create_l2cap_channel(self, spec):
        if not self.compatibility.active:
            raise AssertionError("compatibility was not active before creation")
        self.events.append("channel_create")
        self.spec = spec
        if self.open_error is not None:
            raise self.open_error
        return self.channel


class DeterministicWaitFor:
    def __init__(self, *, fail_call: int, error: BaseException) -> None:
        self.calls = 0
        self.fail_call = fail_call
        self.error = error

    async def __call__(self, awaitable, timeout: float):
        del timeout
        self.calls += 1
        if self.calls == self.fail_call:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise self.error
        return await awaitable


class FakeSecureSession:
    def __init__(self, events: list[str], connection: FakeConnection) -> None:
        self.events = events
        self.connection = connection

    @asynccontextmanager
    async def open(self):
        self.events.extend(
            [
                "discovery",
                "credentials",
                "logging",
                "handoff",
                "transport_acquire",
                "device_on",
                "bredr_connect",
                "authenticate",
                "encrypt",
            ]
        )
        context = AuthenticatedClassicContext(
            display_name="Synthetic AirPods",
            connection=self.connection,
            _replacement_key_state=SimpleNamespace(reported=False),
        )
        try:
            yield context
        finally:
            self.events.extend(
                ["bredr_disconnect", "device_off", "transport_release", "restore"]
            )


class AAPChannelSessionTests(unittest.IsolatedAsyncioTestCase):
    def make_components(self, **channel_options):
        events: list[str] = []
        compatibility = CompatibilityRecorder(events)
        channel = FakeRawChannel(events, compatibility, **channel_options)
        connection = FakeConnection(events, compatibility, channel=channel)
        session = AAPChannelSession(compatibility=compatibility)
        return session, connection, channel, compatibility, events

    async def test_compatibility_covers_creation_lifetime_and_close(self) -> None:
        session, connection, _, compatibility, events = self.make_components()

        async with session.open(connection):
            self.assertTrue(compatibility.active)
            events.append("held")

        self.assertFalse(compatibility.active)
        self.assertEqual(
            events,
            ["compat_enter", "channel_create", "held", "channel_close", "compat_exit"],
        )

    async def test_psm_is_exactly_aap_and_mode_is_basic(self) -> None:
        session, connection, _, _, _ = self.make_components()

        async with session.open(connection) as channel:
            self.assertEqual(channel.psm, AAP_PSM)
            self.assertEqual(channel.mode, "Basic")

        self.assertIsInstance(connection.spec, l2cap.ClassicChannelSpec)
        self.assertEqual(connection.spec.psm, AAP_PSM)
        self.assertEqual(connection.spec.mode, l2cap.TransmissionMode.BASIC)

    async def test_open_exposes_observed_mtus_without_forcing_peer_value(self) -> None:
        session, connection, _, _, _ = self.make_components(
            local_mtu=1900, peer_mtu=2765
        )

        async with session.open(connection) as channel:
            self.assertEqual(channel.local_mtu, 1900)
            self.assertEqual(channel.peer_mtu, 2765)

    async def test_non_open_channel_is_rejected_and_cleanup_attempted(self) -> None:
        session, connection, _, _, events = self.make_components(
            state=l2cap.ClassicChannel.State.WAIT_CONFIG_RSP
        )

        with self.assertRaises(AAPChannelStateError):
            async with session.open(connection):
                pass

        self.assertEqual(events[-2:], ["channel_close", "compat_exit"])

    async def test_non_basic_channel_is_rejected(self) -> None:
        session, connection, _, _, _ = self.make_components(
            mode=l2cap.TransmissionMode.ENHANCED_RETRANSMISSION
        )

        with self.assertRaisesRegex(AAPChannelStateError, "Basic mode"):
            async with session.open(connection):
                pass

    async def test_open_failure_restores_compatibility(self) -> None:
        events: list[str] = []
        compatibility = CompatibilityRecorder(events)
        connection = FakeConnection(
            events, compatibility, open_error=RuntimeError("synthetic failure")
        )
        session = AAPChannelSession(compatibility=compatibility)

        with self.assertRaises(AAPChannelOpenError):
            async with session.open(connection):
                pass

        self.assertFalse(compatibility.active)
        self.assertEqual(events, ["compat_enter", "channel_create", "compat_exit"])

    async def test_open_timeout_is_deterministic_and_cleans_up(self) -> None:
        session, connection, _, compatibility, events = self.make_components()
        session = AAPChannelSession(
            compatibility=compatibility,
            wait_for=DeterministicWaitFor(fail_call=1, error=TimeoutError()),
        )

        with self.assertRaises(AAPChannelOpenTimeoutError):
            async with session.open(connection):
                pass

        self.assertEqual(events, ["compat_enter", "compat_exit"])

    async def test_cancellation_during_open_restores_compatibility(self) -> None:
        events: list[str] = []
        compatibility = CompatibilityRecorder(events)
        connection = FakeConnection(
            events, compatibility, open_error=asyncio.CancelledError()
        )

        with self.assertRaises(asyncio.CancelledError):
            async with AAPChannelSession(compatibility=compatibility).open(connection):
                pass

        self.assertEqual(events[-1], "compat_exit")

    async def test_cancellation_while_held_closes_channel(self) -> None:
        session, connection, _, compatibility, events = self.make_components()

        with self.assertRaises(asyncio.CancelledError):
            async with session.open(connection):
                raise asyncio.CancelledError()

        self.assertFalse(compatibility.active)
        self.assertEqual(events[-2:], ["channel_close", "compat_exit"])

    async def test_close_failure_still_restores_compatibility(self) -> None:
        session, connection, _, compatibility, events = self.make_components(
            close_error=RuntimeError("synthetic close failure")
        )

        with self.assertRaises(AAPChannelCloseError):
            async with session.open(connection):
                pass

        self.assertFalse(compatibility.active)
        self.assertEqual(events[-2:], ["channel_close", "compat_exit"])

    async def test_primary_error_is_not_replaced_by_close_failure(self) -> None:
        session, connection, _, _, _ = self.make_components(
            close_error=RuntimeError("secondary")
        )

        with self.assertRaisesRegex(RuntimeError, "primary") as caught:
            async with session.open(connection):
                raise RuntimeError("primary")

        self.assertTrue(any("close failed" in note for note in caught.exception.__notes__))

    async def test_close_timeout_is_bounded(self) -> None:
        session, connection, _, compatibility, _ = self.make_components()
        session = AAPChannelSession(
            compatibility=compatibility,
            wait_for=DeterministicWaitFor(fail_call=2, error=TimeoutError()),
        )

        with self.assertRaisesRegex(AAPChannelCloseError, "timed out"):
            async with session.open(connection):
                pass

    async def test_session_sends_zero_application_payload(self) -> None:
        session, connection, raw_channel, _, _ = self.make_components()

        async with session.open(connection) as channel:
            self.assertFalse(hasattr(channel, "write"))
            self.assertFalse(hasattr(channel, "send_pdu"))

        self.assertEqual(raw_channel.application_send_count, 0)
        self.assertEqual(connection.sdp_install_count, 0)

    async def test_bumble_connection_adapter_uses_owning_device_manager(self) -> None:
        manager = object()
        expected_channel = object()
        raw_connection = SimpleNamespace(
            device=SimpleNamespace(l2cap_channel_manager=manager),
            create_l2cap_channel=AsyncMock(return_value=expected_channel),
        )
        connection = BumbleClassicConnection(
            raw_connection,
            security_timeout=1.0,
            disconnect_timeout=1.0,
        )
        spec = l2cap.ClassicChannelSpec(
            psm=AAP_PSM, mode=l2cap.TransmissionMode.BASIC
        )

        created = await connection.create_l2cap_channel(spec)

        self.assertIs(connection.l2cap_channel_manager, manager)
        self.assertIs(created, expected_channel)
        raw_connection.create_l2cap_channel.assert_awaited_once_with(spec)


class AAPL2CAPOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def make_probe_session(self, **channel_options):
        events: list[str] = []
        compatibility = CompatibilityRecorder(events)
        raw_channel = FakeRawChannel(events, compatibility, **channel_options)
        connection = FakeConnection(
            events, compatibility, channel=raw_channel
        )
        secure = FakeSecureSession(events, connection)
        channel_session = AAPChannelSession(compatibility=compatibility)
        return (
            AAPL2CAPProbeSession(secure, channel_session),
            raw_channel,
            connection,
            events,
        )

    async def test_channel_close_precedes_bredr_and_controller_cleanup(self) -> None:
        session, _, _, events = self.make_probe_session()

        result = await session.run()

        self.assertLess(events.index("channel_close"), events.index("compat_exit"))
        self.assertLess(events.index("compat_exit"), events.index("bredr_disconnect"))
        self.assertLess(events.index("bredr_disconnect"), events.index("device_off"))
        self.assertLess(events.index("device_off"), events.index("transport_release"))
        self.assertLess(events.index("transport_release"), events.index("restore"))
        self.assertFalse(result.application_payload_sent)

    async def test_l2cap_failure_unwinds_all_outer_resources(self) -> None:
        events: list[str] = []
        compatibility = CompatibilityRecorder(events)
        connection = FakeConnection(
            events, compatibility, open_error=RuntimeError("negotiation failed")
        )
        secure = FakeSecureSession(events, connection)
        session = AAPL2CAPProbeSession(
            secure, AAPChannelSession(compatibility=compatibility)
        )

        with self.assertRaises(AAPChannelOpenError):
            await session.run()

        self.assertEqual(
            events[-5:],
            ["compat_exit", "bredr_disconnect", "device_off", "transport_release", "restore"],
        )

    async def test_l2cap_timeout_unwinds_all_outer_resources(self) -> None:
        session, _, connection, events = self.make_probe_session()
        secure = FakeSecureSession(events, connection)
        channel_session = AAPChannelSession(
            compatibility=connection.compatibility,
            wait_for=DeterministicWaitFor(fail_call=1, error=TimeoutError()),
        )

        with self.assertRaises(AAPChannelOpenTimeoutError):
            await AAPL2CAPProbeSession(secure, channel_session).run()

        self.assertEqual(
            events[-5:],
            ["compat_exit", "bredr_disconnect", "device_off", "transport_release", "restore"],
        )

    async def test_cancellation_during_l2cap_open_unwinds_everything(self) -> None:
        events: list[str] = []
        compatibility = CompatibilityRecorder(events)
        connection = FakeConnection(
            events, compatibility, open_error=asyncio.CancelledError()
        )
        secure = FakeSecureSession(events, connection)

        with self.assertRaises(asyncio.CancelledError):
            await AAPL2CAPProbeSession(
                secure, AAPChannelSession(compatibility=compatibility)
            ).run()

        self.assertEqual(
            events[-5:],
            ["compat_exit", "bredr_disconnect", "device_off", "transport_release", "restore"],
        )

    async def test_cancellation_while_channel_held_unwinds_everything(self) -> None:
        session, _, connection, events = self.make_probe_session()

        class CancellingChannelSession(AAPChannelSession):
            @asynccontextmanager
            async def open(self, selected_connection):
                async with super().open(selected_connection) as channel:
                    raise asyncio.CancelledError()
                    yield channel

        secure = FakeSecureSession(events, connection)
        session = AAPL2CAPProbeSession(
            secure,
            CancellingChannelSession(compatibility=connection.compatibility),
        )
        with self.assertRaises(asyncio.CancelledError):
            await session.run()

        self.assertEqual(
            events[-4:],
            ["bredr_disconnect", "device_off", "transport_release", "restore"],
        )

    async def test_channel_close_failure_does_not_leak_outer_resources(self) -> None:
        session, _, _, events = self.make_probe_session(
            close_error=RuntimeError("synthetic close failure")
        )

        with self.assertRaises(AAPChannelCloseError):
            await session.run()

        self.assertEqual(
            events[-5:],
            ["compat_exit", "bredr_disconnect", "device_off", "transport_release", "restore"],
        )

    async def test_probe_session_never_sends_or_installs_sdp(self) -> None:
        session, raw_channel, connection, _ = self.make_probe_session(peer_mtu=2582)

        result = await session.run()

        self.assertEqual(raw_channel.application_send_count, 0)
        self.assertEqual(connection.sdp_install_count, 0)
        self.assertEqual(result.peer_mtu, 2582)


class AAPL2CAPProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_is_default_and_does_not_create_backend(self) -> None:
        runner = AsyncMock()
        output: list[str] = []

        result = await run_probe(execute=False, output=output.append, live_runner=runner)

        self.assertEqual(result, 0)
        runner.assert_not_awaited()
        self.assertTrue(output[0].startswith("DRY RUN"))
        self.assertIn("Send no AAP application payload", "\n".join(output))

    async def test_execute_is_the_only_live_gate(self) -> None:
        runner = AsyncMock()

        self.assertFalse(build_parser().parse_args([]).execute)
        self.assertTrue(build_parser().parse_args(["--execute"]).execute)
        self.assertEqual(await run_probe(execute=True, live_runner=runner), 0)
        runner.assert_awaited_once()

    async def test_probe_failure_output_does_not_expose_synthetic_key(self) -> None:
        secret = bytes(reversed(range(16))).hex()

        async def fail(output) -> None:
            del output
            raise AAPChannelOpenError(secret)

        output: list[str] = []
        result = await run_probe(execute=True, output=output.append, live_runner=fail)

        self.assertEqual(result, 1)
        self.assertNotIn(secret, "\n".join(output))

    async def test_channel_exception_traceback_does_not_expose_cause_text(self) -> None:
        secret = bytes(range(16)).hex()
        events: list[str] = []
        compatibility = CompatibilityRecorder(events)
        connection = FakeConnection(
            events, compatibility, open_error=RuntimeError(secret)
        )

        with self.assertRaises(AAPChannelOpenError) as caught:
            async with AAPChannelSession(compatibility=compatibility).open(connection):
                pass

        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(secret, rendered)


if __name__ == "__main__":
    unittest.main()
