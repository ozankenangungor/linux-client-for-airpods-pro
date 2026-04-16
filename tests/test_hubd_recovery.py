"""Deterministic automatic-recovery tests for the private hub daemon."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from airpods_hr._hubd.protocol import PROTOCOL_VERSION
from airpods_hr._hubd.server import AirPodsHubDaemon, DaemonState
from airpods_hr.heartrate import HeartRateReport, parse_heart_rate_packet
from airpods_hr.protocol import (
    HEART_RATE_MARKER,
    HEART_RATE_REPORT_ID,
    HEART_RATE_REPORT_SIZE,
)


class RecoverableSessionFailure(RuntimeError):
    pass


class TerminalSessionFailure(RuntimeError):
    pass


class ScriptedSession:
    def __init__(
        self,
        *,
        open_error: BaseException | None = None,
        start_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.open_error = open_error
        self.start_error = start_error
        self.close_error = close_error
        self.reports: asyncio.Queue[HeartRateReport | BaseException] = asyncio.Queue()
        self.events: list[str] = []
        self.open_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self.opened = False
        self.factory: ScriptedFactory | None = None

    async def open(self) -> None:
        self.events.append("open")
        self.open_calls += 1
        if self.open_error is not None:
            raise self.open_error
        self.opened = True
        assert self.factory is not None
        self.factory.active_sessions += 1
        self.factory.maximum_active_sessions = max(
            self.factory.maximum_active_sessions,
            self.factory.active_sessions,
        )

    async def start(self) -> None:
        self.events.append("start")
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error

    async def receive_report(self) -> HeartRateReport:
        value = await self.reports.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def stop(self) -> None:
        self.events.append("stop")
        self.stop_calls += 1

    async def close(self) -> None:
        self.events.append("close")
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
        if self.opened:
            self.opened = False
            assert self.factory is not None
            self.factory.active_sessions -= 1

    def inject(self, value: HeartRateReport | BaseException) -> None:
        self.reports.put_nowait(value)


class ScriptedFactory:
    def __init__(self, sessions: list[ScriptedSession]) -> None:
        self.sessions = sessions
        self.calls = 0
        self.active_sessions = 0
        self.maximum_active_sessions = 0

    def __call__(self) -> ScriptedSession:
        session = self.sessions[self.calls]
        self.calls += 1
        session.factory = self
        return session


class ImmediateSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


class BlockingSleeper(ImmediateSleeper):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class JsonClient:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, path: Path) -> JsonClient:
        return cls(*(await asyncio.open_unix_connection(path)))

    async def request(self, operation: str, **fields: object) -> dict[str, Any]:
        self.writer.write(
            json.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "operation": operation,
                    **fields,
                }
            ).encode()
            + b"\n"
        )
        await self.writer.drain()
        return await self.read()

    async def read(self) -> dict[str, Any]:
        frame = await asyncio.wait_for(self.reader.readline(), timeout=1)
        if not frame:
            raise EOFError("daemon closed the client connection")
        return json.loads(frame)

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


def canonical_report(bpm: int = 87) -> HeartRateReport:
    report = bytearray(HEART_RATE_REPORT_SIZE)
    report[0] = HEART_RATE_REPORT_ID
    report[1] = bpm
    report[2] = 20
    report[3:5] = (7).to_bytes(2, "little")
    report[5] = 2
    report[6:14] = (100).to_bytes(8, "little")
    report[14:18] = (0x1000).to_bytes(4, "little")
    return parse_heart_rate_packet(HEART_RATE_MARKER + bytes(report))


class HubRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temp.name) / "hubd.sock"
        self.clients: list[JsonClient] = []
        self.daemon: AirPodsHubDaemon | None = None

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        if self.daemon is not None:
            await self.daemon.shutdown()
        self.temp.cleanup()

    def make_daemon(
        self,
        sessions: list[ScriptedSession],
        sleeper: ImmediateSleeper,
    ) -> tuple[AirPodsHubDaemon, ScriptedFactory]:
        factory = ScriptedFactory(sessions)
        daemon = AirPodsHubDaemon(
            factory,
            self.socket_path,
            session_error_is_recoverable=lambda error: isinstance(
                error, RecoverableSessionFailure
            ),
            recovery_sleep=sleeper,
        )
        self.daemon = daemon
        return daemon, factory

    async def client(self) -> JsonClient:
        client = await JsonClient.connect(self.socket_path)
        self.clients.append(client)
        return client

    async def subscribe(self, client: JsonClient) -> dict[str, Any]:
        return await client.request("subscribe", stream="heart_rate")

    async def wait_for(self, predicate: Any) -> None:
        async with asyncio.timeout(1):
            while not predicate():
                await asyncio.sleep(0)

    async def start_streaming(
        self, sessions: list[ScriptedSession], sleeper: ImmediateSleeper
    ) -> tuple[AirPodsHubDaemon, ScriptedFactory, JsonClient]:
        daemon, factory = self.make_daemon(sessions, sleeper)
        await daemon.start()
        client = await self.client()
        reply = await self.subscribe(client)
        self.assertTrue(reply["ok"])
        return daemon, factory, client

    async def test_failure_then_success_retries_initial_establishment(self) -> None:
        sleeper = ImmediateSleeper()
        failed = ScriptedSession(open_error=RecoverableSessionFailure("gone"))
        restored = ScriptedSession()
        daemon, factory = self.make_daemon([failed, restored], sleeper)

        await daemon.start()

        self.assertEqual(factory.calls, 2)
        self.assertEqual(sleeper.delays, [1.0])
        self.assertEqual(failed.events, ["open", "close"])
        self.assertEqual(restored.events, ["open"])
        self.assertEqual(daemon.state, DaemonState.READY)

    async def test_repeated_recoverable_failures_use_bounded_order(self) -> None:
        sleeper = ImmediateSleeper()
        initial = ScriptedSession()
        failures = [
            ScriptedSession(open_error=RecoverableSessionFailure("gone"))
            for _ in range(3)
        ]
        restored = ScriptedSession()
        daemon, factory, _client = await self.start_streaming(
            [initial, *failures, restored], sleeper
        )

        initial.inject(RecoverableSessionFailure("peer closed"))
        await self.wait_for(lambda: daemon.session is restored)

        self.assertEqual(factory.calls, 5)
        self.assertEqual(sleeper.delays, [1.0, 2.0, 5.0, 10.0])
        self.assertEqual(daemon.state, DaemonState.STREAMING)

    async def test_backoff_caps_at_ten_seconds(self) -> None:
        sleeper = ImmediateSleeper()
        initial = ScriptedSession()
        failures = [
            ScriptedSession(open_error=RecoverableSessionFailure("gone"))
            for _ in range(5)
        ]
        restored = ScriptedSession()
        daemon, _factory, _client = await self.start_streaming(
            [initial, *failures, restored], sleeper
        )

        initial.inject(RecoverableSessionFailure("peer closed"))
        await self.wait_for(lambda: daemon.session is restored)

        self.assertEqual(sleeper.delays, [1.0, 2.0, 5.0, 10.0, 10.0, 10.0])

    async def test_success_resets_backoff_for_later_disconnect(self) -> None:
        sleeper = ImmediateSleeper()
        initial, first_restore, second_restore = (
            ScriptedSession(),
            ScriptedSession(),
            ScriptedSession(),
        )
        daemon, _factory, _client = await self.start_streaming(
            [initial, first_restore, second_restore], sleeper
        )

        initial.inject(RecoverableSessionFailure("first loss"))
        await self.wait_for(lambda: daemon.session is first_restore)
        first_restore.inject(RecoverableSessionFailure("second loss"))
        await self.wait_for(lambda: daemon.session is second_restore)

        self.assertEqual(sleeper.delays, [1.0, 1.0])

    async def test_shutdown_during_backoff_cancels_retry(self) -> None:
        sleeper = BlockingSleeper()
        initial, unused = ScriptedSession(), ScriptedSession()
        daemon, factory, _client = await self.start_streaming(
            [initial, unused], sleeper
        )

        initial.inject(RecoverableSessionFailure("peer closed"))
        await asyncio.wait_for(sleeper.entered.wait(), timeout=1)
        await daemon.shutdown()

        self.assertTrue(sleeper.cancelled)
        self.assertEqual(factory.calls, 1)
        self.assertFalse(daemon.recovery_active)
        self.assertEqual(daemon.state, DaemonState.STOPPED)

    async def test_duplicate_failure_signals_create_one_recovery_loop(self) -> None:
        sleeper = BlockingSleeper()
        initial, restored = ScriptedSession(), ScriptedSession()
        daemon, factory, _client = await self.start_streaming(
            [initial, restored], sleeper
        )

        initial.inject(RecoverableSessionFailure("peer closed"))
        await asyncio.wait_for(sleeper.entered.wait(), timeout=1)
        await asyncio.gather(
            daemon._receive_failed(RecoverableSessionFailure("duplicate one")),
            daemon._receive_failed(RecoverableSessionFailure("duplicate two")),
        )
        self.assertEqual(factory.calls, 1)
        sleeper.release.set()
        await self.wait_for(lambda: daemon.session is restored)

        self.assertEqual(factory.calls, 2)
        self.assertEqual(sleeper.delays, [1.0])
        self.assertEqual(factory.maximum_active_sessions, 1)

    async def test_terminal_failure_does_not_schedule_retry(self) -> None:
        sleeper = ImmediateSleeper()
        initial = ScriptedSession()
        daemon, factory, client = await self.start_streaming([initial], sleeper)

        initial.inject(TerminalSessionFailure("invariant"))
        reply = await client.read()
        await self.wait_for(lambda: daemon.state is DaemonState.FAILED)

        self.assertEqual(reply["error"]["code"], "service_failed")
        self.assertEqual(factory.calls, 1)
        self.assertEqual(sleeper.delays, [])
        self.assertFalse(daemon.recovery_active)

    async def test_unverified_cleanup_failure_prevents_replacement(self) -> None:
        sleeper = ImmediateSleeper()
        initial = ScriptedSession(close_error=RuntimeError("cleanup failed"))
        unused = ScriptedSession()
        daemon, factory, client = await self.start_streaming(
            [initial, unused], sleeper
        )

        initial.inject(RecoverableSessionFailure("peer closed"))
        reply = await client.read()
        await self.wait_for(lambda: daemon.state is DaemonState.FAILED)

        self.assertEqual(reply["error"]["code"], "service_failed")
        self.assertIs(daemon.session, initial)
        self.assertEqual(factory.calls, 1)
        self.assertEqual(sleeper.delays, [])
        initial.close_error = None

    async def test_recovered_session_uses_canonical_sample_pipeline(self) -> None:
        sleeper = ImmediateSleeper()
        initial, restored = ScriptedSession(), ScriptedSession()
        daemon, _factory, client = await self.start_streaming(
            [initial, restored], sleeper
        )

        initial.inject(RecoverableSessionFailure("peer closed"))
        await self.wait_for(lambda: daemon.session is restored)
        restored.inject(canonical_report(87))
        event = await client.read()

        self.assertEqual(event["event"], "heart_rate")
        self.assertEqual(event["bpm"], 87)
        self.assertEqual(event["source_side"], "right")
        self.assertEqual(daemon.subscriber_count, 1)

    async def test_new_client_can_inspect_daemon_during_recovery(self) -> None:
        sleeper = BlockingSleeper()
        initial, restored = ScriptedSession(), ScriptedSession()
        daemon, _factory, subscribed = await self.start_streaming(
            [initial, restored], sleeper
        )

        initial.inject(RecoverableSessionFailure("peer closed"))
        await asyncio.wait_for(sleeper.entered.wait(), timeout=1)
        observer = await self.client()
        status = await observer.request("status")
        unavailable = await observer.request("subscribe", stream="heart_rate")
        ping = await observer.request("ping")

        self.assertEqual(status["state"], "starting")
        self.assertEqual(status["subscriber_count"], 1)
        self.assertEqual(unavailable["error"]["code"], "service_unavailable")
        self.assertTrue(ping["pong"])
        sleeper.release.set()
        await self.wait_for(lambda: daemon.session is restored)
        restored.inject(canonical_report(91))
        self.assertEqual((await subscribed.read())["bpm"], 91)

    async def test_repeated_recovery_and_shutdown_release_every_session(self) -> None:
        sleeper = ImmediateSleeper()
        sessions = [ScriptedSession() for _ in range(3)]
        daemon, factory, _client = await self.start_streaming(sessions, sleeper)

        sessions[0].inject(RecoverableSessionFailure("first loss"))
        await self.wait_for(lambda: daemon.session is sessions[1])
        sessions[1].inject(RecoverableSessionFailure("second loss"))
        await self.wait_for(lambda: daemon.session is sessions[2])
        await daemon.shutdown()

        self.assertEqual(factory.maximum_active_sessions, 1)
        self.assertEqual(factory.active_sessions, 0)
        self.assertEqual([session.close_calls for session in sessions], [1, 1, 1])
        self.assertEqual([session.stop_calls for session in sessions], [1, 1, 1])
        self.assertFalse(daemon.report_reader_active)
        self.assertFalse(daemon.recovery_active)
        self.assertEqual(daemon.state, DaemonState.STOPPED)


if __name__ == "__main__":
    unittest.main()
