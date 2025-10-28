"""Hardware-independent tests for the signaling-only AAP channel session."""

from __future__ import annotations

import asyncio
import inspect

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bumble import l2cap

from airpods_hr.aap_channel import AAPChannelCloseError, AAPChannelOpenError, AAPChannelOpenTimeoutError, AAPChannelSession, AAPChannelStateError


from airpods_hr.authentication import BumbleClassicConnection


from airpods_hr.protocol import AAP_PSM


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


