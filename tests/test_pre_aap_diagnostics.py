"""Hardware-independent tests for the pre-AAP reference diagnostics."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from bumble import l2cap

from airpods_hr.pre_aap_diagnostics import (
    PRE_AAP_DELAY_SECONDS,
    BumbleInformationExchange,
    InformationResponseResult,
    PreAAPAAPChannelSession,
    PreAAPSequenceError,
    PreAAPSequenceMode,
    PreAAPSequenceSecureSession,
    PreAAPSequenceStrategy,
)


class FakeManager:
    def __init__(
        self,
        responses: dict[int, tuple[int, bytes]] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.events: list[tuple[str, int]] = []
        self._identifier = 0

    def next_identifier(self, connection: object) -> int:
        del connection
        self._identifier += 1
        return self._identifier

    def send_control_frame(
        self, connection: object, cid: int, request: object
    ) -> None:
        assert isinstance(request, l2cap.L2CAP_Information_Request)
        info_type = int(request.info_type)
        self.events.append(("request", info_type))
        response_spec = self.responses.get(info_type)
        if response_spec is None:
            return
        result, data = response_spec
        response = l2cap.L2CAP_Information_Response(
            identifier=request.identifier,
            info_type=info_type,
            result=result,
            data=data,
        )
        self.events.append(("response", info_type))
        self.on_l2cap_information_response(connection, cid, response)


class FailingSendManager(FakeManager):
    def send_control_frame(
        self, connection: object, cid: int, request: object
    ) -> None:
        del connection, cid, request
        raise RuntimeError("synthetic send failure")


def fake_connection(manager: FakeManager) -> object:
    raw = SimpleNamespace(device=SimpleNamespace(l2cap_channel_manager=manager))
    return SimpleNamespace(l2cap_channel_manager=manager, _connection=raw)


def successful_manager() -> FakeManager:
    return FakeManager(
        {
            0x0002: (
                int(l2cap.L2CAP_Information_Response.Result.SUCCESS),
                bytes.fromhex("80000000"),
            ),
            0x0003: (
                int(l2cap.L2CAP_Information_Response.Result.SUCCESS),
                bytes.fromhex("0200000000000000"),
            ),
        }
    )


class FakeSecureSession:
    def __init__(self, connection: object) -> None:
        self.connection = connection
        self.events: list[str] = []

    @asynccontextmanager
    async def open(self, *, pre_connect_profile: object | None = None):
        del pre_connect_profile
        self.events.append("encrypted_context_entered")
        try:
            yield SimpleNamespace(connection=self.connection)
        finally:
            self.events.append("encrypted_context_exited")


class FakeChannelSession:
    def __init__(self) -> None:
        self.events: list[str] = []

    @asynccontextmanager
    async def open_protocol(self, connection: object, factory: object):
        del connection, factory
        self.events.append("aap_open")
        yield object()


class PreAAPModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_proven_mode_adds_no_delay_or_information_exchange(self) -> None:
        sleep = AsyncMock()
        exchange = AsyncMock()
        strategy = PreAAPSequenceStrategy(
            PreAAPSequenceMode.PROVEN,
            sleep=sleep,
            information_exchange=exchange,
        )
        await strategy.run(object())
        sleep.assert_not_awaited()
        exchange.run.assert_not_awaited()
        selected = strategy.observation
        self.assertEqual(selected.delay_ms, 0)
        self.assertEqual(
            selected.extended_features_result,
            InformationResponseResult.NOT_APPLICABLE,
        )

    async def test_delay_only_waits_without_information_requests(self) -> None:
        sleep = AsyncMock()
        exchange = AsyncMock()
        strategy = PreAAPSequenceStrategy(
            PreAAPSequenceMode.DELAY_ONLY,
            sleep=sleep,
            information_exchange=exchange,
        )
        await strategy.run(object())
        sleep.assert_awaited_once_with(PRE_AAP_DELAY_SECONDS)
        exchange.run.assert_not_awaited()
        self.assertEqual(strategy.observation.delay_ms, 20)
        self.assertFalse(strategy.observation.extended_features_request_sent)

    async def test_bluez_sequence_sends_and_waits_in_order(self) -> None:
        manager = successful_manager()
        strategy = PreAAPSequenceStrategy(
            PreAAPSequenceMode.BLUEZ_L2CAP_INFO,
            information_exchange=BumbleInformationExchange(
                response_timeout=0.1
            ),
        )
        await strategy.run(fake_connection(manager))
        self.assertEqual(
            manager.events,
            [
                ("request", 0x0002),
                ("response", 0x0002),
                ("request", 0x0003),
                ("response", 0x0003),
            ],
        )
        selected = strategy.observation
        self.assertTrue(selected.extended_features_request_sent)
        self.assertTrue(selected.extended_features_response_observed)
        self.assertEqual(
            selected.extended_features_result,
            InformationResponseResult.SUCCESS,
        )
        self.assertEqual(selected.extended_features_mask, 0x80)
        self.assertTrue(selected.fixed_channels_request_sent)
        self.assertTrue(selected.fixed_channels_response_observed)
        self.assertEqual(selected.fixed_channels_mask, 0x02)

    async def test_timeout_stops_before_second_request_and_restores_hook(self) -> None:
        manager = FakeManager()
        existing = Mock()
        manager.on_l2cap_information_response = existing
        strategy = PreAAPSequenceStrategy(
            PreAAPSequenceMode.BLUEZ_L2CAP_INFO,
            information_exchange=BumbleInformationExchange(
                response_timeout=0.001
            ),
        )
        with self.assertRaises(PreAAPSequenceError):
            await strategy.run(fake_connection(manager))
        self.assertEqual(manager.events, [("request", 0x0002)])
        self.assertIs(manager.on_l2cap_information_response, existing)
        selected = strategy.observation
        self.assertEqual(
            selected.extended_features_result,
            InformationResponseResult.TIMEOUT,
        )
        self.assertFalse(selected.fixed_channels_request_sent)

    async def test_success_restores_absent_hook(self) -> None:
        manager = successful_manager()
        strategy = PreAAPSequenceStrategy(
            PreAAPSequenceMode.BLUEZ_L2CAP_INFO,
            information_exchange=BumbleInformationExchange(
                response_timeout=0.1
            ),
        )
        self.assertNotIn("on_l2cap_information_response", manager.__dict__)
        await strategy.run(fake_connection(manager))
        self.assertNotIn("on_l2cap_information_response", manager.__dict__)

    async def test_send_exception_restores_previous_hook(self) -> None:
        manager = FailingSendManager()
        previous = Mock()
        manager.on_l2cap_information_response = previous
        strategy = PreAAPSequenceStrategy(
            PreAAPSequenceMode.BLUEZ_L2CAP_INFO,
            information_exchange=BumbleInformationExchange(
                response_timeout=0.1
            ),
        )
        with self.assertRaises(PreAAPSequenceError):
            await strategy.run(fake_connection(manager))
        self.assertIs(manager.on_l2cap_information_response, previous)
        self.assertEqual(
            strategy.observation.extended_features_result,
            InformationResponseResult.OTHER,
        )

    async def test_not_supported_response_stops_before_aap(self) -> None:
        manager = FakeManager(
            {
                0x0002: (
                    int(
                        l2cap.L2CAP_Information_Response.Result.NOT_SUPPORTED
                    ),
                    b"",
                )
            }
        )
        strategy = PreAAPSequenceStrategy(
            PreAAPSequenceMode.BLUEZ_L2CAP_INFO,
            information_exchange=BumbleInformationExchange(
                response_timeout=0.1
            ),
        )
        with self.assertRaises(PreAAPSequenceError):
            await strategy.run(fake_connection(manager))
        selected = strategy.observation
        self.assertTrue(selected.extended_features_response_observed)
        self.assertEqual(
            selected.extended_features_result,
            InformationResponseResult.NOT_SUPPORTED,
        )
        self.assertFalse(selected.aap_open_attempted)

    async def test_malformed_success_response_fails_closed(self) -> None:
        manager = FakeManager(
            {
                0x0002: (
                    int(l2cap.L2CAP_Information_Response.Result.SUCCESS),
                    b"BAD",
                )
            }
        )
        strategy = PreAAPSequenceStrategy(
            PreAAPSequenceMode.BLUEZ_L2CAP_INFO,
            information_exchange=BumbleInformationExchange(
                response_timeout=0.1
            ),
        )
        with self.assertRaises(PreAAPSequenceError):
            await strategy.run(fake_connection(manager))
        self.assertEqual(
            strategy.observation.extended_features_result,
            InformationResponseResult.OTHER,
        )

    async def test_secure_wrapper_finishes_sequence_before_aap_open(self) -> None:
        manager = successful_manager()
        strategy = PreAAPSequenceStrategy(
            PreAAPSequenceMode.BLUEZ_L2CAP_INFO,
            information_exchange=BumbleInformationExchange(
                response_timeout=0.1
            ),
        )
        secure = PreAAPSequenceSecureSession(
            FakeSecureSession(fake_connection(manager)), strategy
        )
        channel_delegate = FakeChannelSession()
        channel = PreAAPAAPChannelSession(channel_delegate, strategy)
        async with secure.open() as context:
            self.assertTrue(strategy.observation.fixed_channels_response_observed)
            self.assertFalse(strategy.observation.aap_open_attempted)
            async with channel.open_protocol(context.connection, object()):
                self.assertTrue(strategy.observation.aap_open_attempted)
        self.assertEqual(channel_delegate.events, ["aap_open"])

    async def test_pre_aap_failure_never_enters_aap_session(self) -> None:
        manager = FakeManager()
        strategy = PreAAPSequenceStrategy(
            PreAAPSequenceMode.BLUEZ_L2CAP_INFO,
            information_exchange=BumbleInformationExchange(
                response_timeout=0.001
            ),
        )
        secure = PreAAPSequenceSecureSession(
            FakeSecureSession(fake_connection(manager)), strategy
        )
        channel = FakeChannelSession()
        with self.assertRaises(PreAAPSequenceError):
            async with secure.open() as context:
                async with channel.open_protocol(context.connection, object()):
                    pass
        self.assertEqual(channel.events, [])
        self.assertFalse(strategy.observation.aap_open_attempted)


class PreAAPDiagnosticSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_observation_has_no_raw_signaling_payload_field(self) -> None:
        field_names = {field.name for field in fields(
            PreAAPSequenceStrategy(PreAAPSequenceMode.PROVEN).observation
        )}
        self.assertFalse(
            field_names & {"raw", "payload", "packet", "frame", "data"}
        )

    def test_experiment_is_absent_from_production_paths(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "src/airpods_hr/monitor_cli.py",
            "src/airpods_hr/aap_channel.py",
            "src/airpods_hr/authentication.py",
            "tools/probe_bluez_coexistence.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("PreAAPSequence", source)
            self.assertNotIn("bluez-l2cap-info", source)


if __name__ == "__main__":
    unittest.main()
