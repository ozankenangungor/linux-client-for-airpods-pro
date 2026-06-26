"""Hardware-independent lifecycle and real Unix-socket tests for hubd."""

from __future__ import annotations

import asyncio
import errno
import json
import multiprocessing
import os
import socket
import stat
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import airpods_hr
import airpods_hr._hubd.server as hubd_server
from airpods_hr._hubd.protocol import (
    MAX_FRAME_SIZE,
    OUTBOUND_QUEUE_SIZE,
    PROTOCOL_VERSION,
)
from airpods_hr._hubd.server import (
    AirPodsHubDaemon,
    DaemonAlreadyRunningError,
    DaemonState,
    SessionOperationError,
    UnsafeSocketPathError,
    socket_path_from_environment,
)
from airpods_hr.heartrate import HeartRateReport
from tools.probe_hubd import main as probe_main


def report(bpm: int = 80, *, field_5: int = 1, sequence: int = 1) -> HeartRateReport:
    return HeartRateReport(
        bpm=bpm,
        aux=20,
        sequence=sequence,
        field_5=field_5,
        timestamp_ticks=100,
        flags=0x1000,
    )


class FakeSession:
    def __init__(self) -> None:
        self.open_calls = 0
        self.start_calls = 0
        self.receive_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self.open_error: BaseException | None = None
        self.start_error: BaseException | None = None
        self.stop_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.open_gate: asyncio.Event | None = None
        self.start_gate: asyncio.Event | None = None
        self.stop_gate: asyncio.Event | None = None
        self.reports: asyncio.Queue[HeartRateReport | BaseException] = asyncio.Queue()
        self.events: list[str] = []

    async def open(self) -> None:
        self.open_calls += 1
        self.events.append("open")
        if self.open_gate is not None:
            await self.open_gate.wait()
        if self.open_error is not None:
            raise self.open_error

    async def start(self) -> None:
        self.start_calls += 1
        self.events.append("start")
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.start_error is not None:
            raise self.start_error

    async def receive_report(self) -> HeartRateReport:
        self.receive_calls += 1
        value = await self.reports.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def stop(self) -> None:
        self.stop_calls += 1
        self.events.append("stop")
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        if self.stop_error is not None:
            raise self.stop_error

    async def close(self) -> None:
        self.close_calls += 1
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error

    def inject(self, value: HeartRateReport | BaseException) -> None:
        self.reports.put_nowait(value)


class FakeFactory:
    def __init__(self, session: FakeSession | None = None) -> None:
        self.session = session or FakeSession()
        self.calls = 0

    def __call__(self) -> FakeSession:
        self.calls += 1
        return self.session


class LazyFactory:
    def __init__(self, open_gate: asyncio.Event | None = None) -> None:
        self.open_gate = open_gate
        self.calls = 0
        self.sessions: list[FakeSession] = []

    @property
    def open_calls(self) -> int:
        return sum(session.open_calls for session in self.sessions)

    def __call__(self) -> FakeSession:
        self.calls += 1
        session = FakeSession()
        session.open_gate = self.open_gate
        self.sessions.append(session)
        return session


class ProcessSession:
    def __init__(self, open_entered: Any, open_release: Any) -> None:
        self.open_entered = open_entered
        self.open_release = open_release
        self.open_calls = 0
        self.close_calls = 0

    async def open(self) -> None:
        self.open_calls += 1
        self.open_entered.set()
        released = await asyncio.to_thread(self.open_release.wait, 10.0)
        if not released:
            raise TimeoutError("process test open gate timed out")

    async def start(self) -> None:
        pass

    async def receive_report(self) -> HeartRateReport:
        await asyncio.Future()
        raise AssertionError("unreachable")

    async def stop(self) -> None:
        pass

    async def close(self) -> None:
        self.close_calls += 1


class ProcessFactory:
    def __init__(self, open_entered: Any, open_release: Any) -> None:
        self.open_entered = open_entered
        self.open_release = open_release
        self.calls = 0
        self.sessions: list[ProcessSession] = []

    @property
    def open_calls(self) -> int:
        return sum(session.open_calls for session in self.sessions)

    @property
    def close_calls(self) -> int:
        return sum(session.close_calls for session in self.sessions)

    def __call__(self) -> ProcessSession:
        self.calls += 1
        session = ProcessSession(self.open_entered, self.open_release)
        self.sessions.append(session)
        return session


class BindPausedDaemon(AirPodsHubDaemon):
    def __init__(self, *args: Any, bound: Any, release_bind: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._bound_event = bound
        self._release_bind = release_bind

    def _listener_bound(self) -> None:
        self._bound_event.set()
        if not self._release_bind.wait(10.0):
            raise TimeoutError("process test bind gate timed out")


def run_process_daemon(
    socket_path: str,
    open_entered: Any,
    open_release: Any,
    shutdown_requested: Any,
    messages: Any,
    bound: Any | None = None,
    release_bind: Any | None = None,
) -> None:
    async def run() -> None:
        factory = ProcessFactory(open_entered, open_release)
        if bound is None:
            daemon = AirPodsHubDaemon(factory, socket_path)
        else:
            daemon = BindPausedDaemon(
                factory,
                socket_path,
                bound=bound,
                release_bind=release_bind,
            )
        try:
            await daemon.start()
        except BaseException as error:
            failed_state = daemon.state.value
            await daemon.shutdown()
            messages.put(
                {
                    "phase": "start_failed",
                    "error": type(error).__name__,
                    "state": failed_state,
                    "factory_calls": factory.calls,
                    "open_calls": factory.open_calls,
                }
            )
            return

        messages.put(
            {
                "phase": "ready",
                "state": daemon.state.value,
                "factory_calls": factory.calls,
                "open_calls": factory.open_calls,
                "lock_file_exists": daemon._lock_path.exists(),
            }
        )
        requested = await asyncio.to_thread(shutdown_requested.wait, 10.0)
        if not requested:
            raise TimeoutError("process test shutdown gate timed out")
        await daemon.shutdown()
        messages.put(
            {
                "phase": "stopped",
                "state": daemon.state.value,
                "close_calls": factory.close_calls,
                "lock_file_exists": daemon._lock_path.exists(),
                "socket_exists": daemon.socket_path.exists(),
            }
        )

    asyncio.run(run())


class JsonClient:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, path: Path) -> JsonClient:
        return cls(*(await asyncio.open_unix_connection(path)))

    async def request(self, operation: str, **fields: Any) -> dict[str, Any]:
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

    async def raw(self, value: bytes) -> dict[str, Any]:
        self.writer.write(value)
        await self.writer.drain()
        return await self.read()

    async def read(self, timeout: float = 1.0) -> dict[str, Any]:
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        if not line:
            raise EOFError("server closed connection")
        return json.loads(line)

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


class HubDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temp.name) / "hubd.sock"
        self.factory = FakeFactory()
        self.daemon = AirPodsHubDaemon(self.factory, self.socket_path)
        self.clients: list[JsonClient] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        await self.daemon.shutdown()
        self.temp.cleanup()

    async def start(self) -> None:
        await self.daemon.start()

    async def client(self) -> JsonClient:
        client = await JsonClient.connect(self.socket_path)
        self.clients.append(client)
        await self.wait_for(lambda: len(self.daemon._clients) == len(self.clients))
        return client

    async def wait_for(self, predicate, timeout: float = 1.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0)

    async def process_message(self, messages: Any) -> dict[str, Any]:
        return await asyncio.wait_for(
            asyncio.to_thread(messages.get, True, 10.0),
            timeout=12.0,
        )

    async def join_process(self, process: Any) -> None:
        await asyncio.to_thread(process.join, 10.0)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 0)

    async def subscribe(self, client: JsonClient) -> dict[str, Any]:
        return await client.request("subscribe", stream="heart_rate")

    async def unsubscribe(self, client: JsonClient) -> dict[str, Any]:
        return await client.request("unsubscribe", stream="heart_rate")

    async def test_initial_startup_and_zero_subscribers(self) -> None:
        self.assertEqual(self.daemon.state, DaemonState.STOPPED)
        self.assertIsNone(self.daemon.session)
        await self.start()
        self.assertEqual(self.factory.calls, 1)
        self.assertIs(self.daemon.session, self.factory.session)
        self.assertEqual(self.factory.session.open_calls, 1)
        self.assertEqual(self.factory.session.start_calls, 0)
        self.assertEqual(self.daemon.state, DaemonState.READY)

    async def test_first_and_second_subscribers_share_one_reader(self) -> None:
        await self.start()
        first, second = await self.client(), await self.client()
        first_reply = await self.subscribe(first)
        reader_task = self.daemon._reader_task
        second_reply = await self.subscribe(second)
        self.assertFalse(first_reply["already_subscribed"])
        self.assertFalse(second_reply["already_subscribed"])
        self.assertEqual(self.factory.session.start_calls, 1)
        self.assertEqual(self.daemon.state, DaemonState.STREAMING)
        self.assertIs(self.daemon._reader_task, reader_task)
        self.assertTrue(self.daemon.report_reader_active)

    async def test_report_fanout_preserves_bpm_duplicates_and_sides(self) -> None:
        await self.start()
        first, second = await self.client(), await self.client()
        await self.subscribe(first)
        await self.subscribe(second)
        for sample in (
            report(169, field_5=1),
            report(169, field_5=1),
            report(91, field_5=2),
            report(72, field_5=37),
        ):
            self.factory.session.inject(sample)
        first_events = [await first.read() for _ in range(4)]
        second_events = [await second.read() for _ in range(4)]
        self.assertEqual(first_events, second_events)
        self.assertEqual([item["bpm"] for item in first_events[:2]], [169, 169])
        self.assertEqual(first_events[0]["source_side"], "left")
        self.assertEqual(first_events[2]["source_side"], "right")
        self.assertEqual(first_events[3]["source_side"], "unknown")
        self.assertEqual(first_events[3]["source_side_raw"], 37)
        self.assertNotIn("aux", first_events[0])
        self.assertNotIn("flags", first_events[0])
        self.assertNotIn("raw_report", first_events[0])

    async def test_unsubscribed_client_receives_no_events(self) -> None:
        await self.start()
        subscribed, observer = await self.client(), await self.client()
        await self.subscribe(subscribed)
        self.factory.session.inject(report())
        self.assertEqual((await subscribed.read())["event"], "heart_rate")
        with self.assertRaises(TimeoutError):
            await observer.read(timeout=0.02)

    async def test_unsubscribe_only_stops_on_last_and_keeps_session_open(self) -> None:
        await self.start()
        first, second = await self.client(), await self.client()
        await self.subscribe(first)
        await self.subscribe(second)
        await self.unsubscribe(first)
        self.assertEqual(self.factory.session.stop_calls, 0)
        self.assertEqual(self.daemon.state, DaemonState.STREAMING)
        await self.unsubscribe(second)
        self.assertEqual(self.factory.session.stop_calls, 1)
        self.assertEqual(self.factory.session.close_calls, 0)
        self.assertEqual(self.daemon.state, DaemonState.READY)
        self.assertFalse(self.daemon.report_reader_active)

    async def test_resubscribe_uses_same_session_and_starts_again(self) -> None:
        await self.start()
        client = await self.client()
        owned_session = self.daemon.session
        await self.subscribe(client)
        await self.unsubscribe(client)
        await self.subscribe(client)
        self.assertIs(self.daemon.session, owned_session)
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(self.factory.session.open_calls, 1)
        self.assertEqual(self.factory.session.start_calls, 2)

    async def test_duplicate_subscription_operations_are_idempotent(self) -> None:
        await self.start()
        client = await self.client()
        await self.subscribe(client)
        duplicate = await self.subscribe(client)
        self.assertTrue(duplicate["already_subscribed"])
        self.assertEqual(self.factory.session.start_calls, 1)
        await self.unsubscribe(client)
        duplicate = await self.unsubscribe(client)
        self.assertTrue(duplicate["already_unsubscribed"])
        self.assertEqual(self.factory.session.stop_calls, 1)

    async def test_disconnect_removes_subscription_and_stops_last(self) -> None:
        await self.start()
        first, second = await self.client(), await self.client()
        await self.subscribe(first)
        await self.subscribe(second)
        await first.close()
        self.clients.remove(first)
        await self.wait_for(lambda: self.daemon.subscriber_count == 1)
        self.assertEqual(self.factory.session.stop_calls, 0)
        await second.close()
        self.clients.remove(second)
        await self.wait_for(lambda: self.daemon.state is DaemonState.READY)
        self.assertEqual(self.factory.session.stop_calls, 1)

    async def test_concurrent_final_disconnects_call_stop_once(self) -> None:
        await self.start()
        first, second = await self.client(), await self.client()
        await self.subscribe(first)
        await self.subscribe(second)
        self.clients.clear()
        await asyncio.gather(first.close(), second.close())
        await self.wait_for(lambda: self.daemon.state is DaemonState.READY)
        self.assertEqual(self.daemon.subscriber_count, 0)
        self.assertEqual(self.factory.session.stop_calls, 1)

    async def test_two_concurrent_first_subscriptions_call_start_once(self) -> None:
        await self.start()
        first, second = await self.client(), await self.client()
        gate = self.factory.session.start_gate = asyncio.Event()
        first_task = asyncio.create_task(self.subscribe(first))
        await self.wait_for(lambda: self.factory.session.start_calls == 1)
        second_task = asyncio.create_task(self.subscribe(second))
        await asyncio.sleep(0)
        self.assertEqual(self.factory.session.start_calls, 1)
        gate.set()
        await asyncio.gather(first_task, second_task)
        self.assertEqual(self.factory.session.start_calls, 1)
        self.assertEqual(self.daemon.subscriber_count, 2)

    async def test_concurrent_final_unsubscribes_call_stop_once(self) -> None:
        await self.start()
        first, second = await self.client(), await self.client()
        await self.subscribe(first)
        await self.subscribe(second)
        gate = self.factory.session.stop_gate = asyncio.Event()
        tasks = [
            asyncio.create_task(self.unsubscribe(first)),
            asyncio.create_task(self.unsubscribe(second)),
        ]
        await self.wait_for(lambda: self.factory.session.stop_calls == 1)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(self.factory.session.stop_calls, 1)
        self.assertEqual(self.daemon.state, DaemonState.READY)

    async def test_subscribe_during_stopping_is_serialized(self) -> None:
        await self.start()
        first, second = await self.client(), await self.client()
        await self.subscribe(first)
        gate = self.factory.session.stop_gate = asyncio.Event()
        stop_task = asyncio.create_task(self.unsubscribe(first))
        await self.wait_for(lambda: self.daemon.state is DaemonState.STOPPING_HR)
        subscribe_task = asyncio.create_task(self.subscribe(second))
        await asyncio.sleep(0)
        self.assertEqual(self.factory.session.start_calls, 1)
        gate.set()
        await asyncio.gather(stop_task, subscribe_task)
        self.assertEqual(self.factory.session.stop_calls, 1)
        self.assertEqual(self.factory.session.start_calls, 2)
        self.assertEqual(self.daemon.state, DaemonState.STREAMING)

    async def test_start_failure_has_no_subscription_or_reader(self) -> None:
        self.factory.session.start_error = RuntimeError("private failure")
        await self.start()
        client = await self.client()
        reply = await self.subscribe(client)
        if reply["error"]["code"] == "service_failed":
            reply = await client.read()
        self.assertEqual(reply["error"]["code"], "session_start_failed")
        self.assertEqual(self.daemon.subscriber_count, 0)
        self.assertFalse(self.daemon.report_reader_active)
        self.assertEqual(self.daemon.state, DaemonState.FAILED)

    async def test_receive_failure_is_terminal_and_notifies_clients(self) -> None:
        await self.start()
        client = await self.client()
        await self.subscribe(client)
        self.factory.session.inject(RuntimeError("secret packet detail"))
        reply = await client.read()
        await self.wait_for(lambda: self.daemon.state is DaemonState.FAILED)
        self.assertEqual(reply["error"]["code"], "service_failed")
        self.assertNotIn("secret", json.dumps(reply))
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(self.factory.session.open_calls, 1)
        self.assertEqual(self.factory.session.start_calls, 1)
        self.assertEqual(self.daemon.subscriber_count, 0)

    async def test_shutdown_from_receive_failure_attempts_stop_then_close(self) -> None:
        await self.start()
        client = await self.client()
        await self.subscribe(client)
        self.factory.session.inject(RuntimeError("receive failed"))
        await client.read()
        await self.wait_for(lambda: self.daemon.state is DaemonState.FAILED)
        await self.daemon.shutdown()
        self.assertEqual(
            self.factory.session.events,
            ["open", "start", "stop", "close"],
        )
        self.assertFalse(self.daemon.report_reader_active)

    async def test_stop_failure_does_not_claim_ready(self) -> None:
        await self.start()
        client = await self.client()
        await self.subscribe(client)
        self.factory.session.stop_error = RuntimeError("stop failed")
        first = await self.unsubscribe(client)
        if first["error"]["code"] == "service_failed":
            first = await client.read()
        self.assertEqual(first["error"]["code"], "session_stop_failed")
        self.assertEqual(self.daemon.state, DaemonState.FAILED)

    async def test_shutdown_ready_closes_once_and_is_idempotent(self) -> None:
        await self.start()
        await self.daemon.shutdown()
        await self.daemon.shutdown()
        self.assertEqual(self.factory.session.stop_calls, 0)
        self.assertEqual(self.factory.session.close_calls, 1)
        self.assertEqual(self.factory.session.events, ["open", "close"])
        self.assertEqual(self.daemon.state, DaemonState.STOPPED)

    async def test_close_failure_retains_lock_until_shutdown_retry(self) -> None:
        await self.start()
        self.factory.session.close_error = RuntimeError("close failed")
        await self.daemon.shutdown()
        self.assertEqual(self.daemon.state, DaemonState.FAILED)
        self.assertIsNotNone(self.daemon._lock_fd)
        second_factory = LazyFactory()
        second_daemon = AirPodsHubDaemon(second_factory, self.socket_path)
        with self.assertRaises(DaemonAlreadyRunningError):
            await second_daemon.start()
        self.assertEqual(second_factory.calls, 0)
        await second_daemon.shutdown()

        self.factory.session.close_error = None
        await self.daemon.shutdown()
        self.assertEqual(self.factory.session.close_calls, 2)
        self.assertEqual(self.daemon.state, DaemonState.STOPPED)
        self.assertIsNone(self.daemon._lock_fd)

    async def test_shutdown_streaming_stops_and_leaves_no_reader(self) -> None:
        await self.start()
        client = await self.client()
        await self.subscribe(client)
        await self.daemon.shutdown()
        self.assertEqual(
            self.factory.session.events, ["open", "start", "stop", "close"]
        )
        self.assertFalse(self.daemon.report_reader_active)
        self.assertTrue(self.daemon._reader_task is None)
        self.assertFalse(self.daemon._handler_tasks)

    async def test_hello_ping_status_and_independent_subscriptions(self) -> None:
        await self.start()
        first, second = await self.client(), await self.client()
        hello = await first.request("hello")
        ping = await first.request("ping")
        await self.subscribe(first)
        status = await second.request("status")
        self.assertTrue(hello["experimental"])
        self.assertTrue(ping["pong"])
        self.assertEqual(status["subscriber_count"], 1)
        self.assertEqual(status["state"], "streaming")
        self.assertEqual(self.daemon.subscriber_count, 1)

    async def test_bad_frames_receive_safe_errors(self) -> None:
        await self.start()
        client = await self.client()
        cases = (
            (b"not-json\n", "invalid_json"),
            (b"[]\n", "invalid_request"),
            (b'{"protocol_version":true,"operation":"ping"}\n', "unsupported_version"),
            (b'{"protocol_version":99,"operation":"ping"}\n', "unsupported_version"),
            (b'{"protocol_version":1}\n', "invalid_operation"),
            (b'{"protocol_version":1,"operation":"erase"}\n', "unknown_operation"),
        )
        for frame, code in cases:
            self.assertEqual((await client.raw(frame))["error"]["code"], code)

    async def test_oversized_frame_is_rejected_and_connection_closed(self) -> None:
        await self.start()
        client = await self.client()
        client.writer.write(b"{" + b"x" * (MAX_FRAME_SIZE + 1) + b"\n")
        await client.writer.drain()
        line = await asyncio.wait_for(client.reader.readline(), 1.0)
        if line:
            self.assertEqual(json.loads(line)["error"]["code"], "frame_too_large")
        self.assertEqual(await asyncio.wait_for(client.reader.readline(), 1.0), b"")

    async def test_socket_is_user_only_and_removed_on_shutdown(self) -> None:
        await self.start()
        socket_stat = self.socket_path.stat()
        self.assertTrue(stat.S_ISSOCK(socket_stat.st_mode))
        self.assertEqual(stat.S_IMODE(socket_stat.st_mode), 0o600)
        lock_stat = self.daemon._lock_path.stat()
        self.assertTrue(stat.S_ISREG(lock_stat.st_mode))
        self.assertEqual(lock_stat.st_uid, os.geteuid())
        self.assertEqual(stat.S_IMODE(lock_stat.st_mode), 0o600)
        self.assertIsNotNone(self.daemon._lock_fd)
        await self.daemon.shutdown()
        self.assertFalse(self.socket_path.exists())
        self.assertTrue(self.daemon._lock_path.exists())
        self.assertIsNone(self.daemon._lock_fd)

    async def test_process_lock_precedes_socket_checks_ready_and_streaming(
        self,
    ) -> None:
        await self.start()
        self.assertEqual(self.daemon.subscriber_count, 0)
        self.assertEqual(self.daemon.state, DaemonState.READY)

        ready_factory = LazyFactory()
        ready_daemon = AirPodsHubDaemon(ready_factory, self.socket_path)
        with patch.object(
            hubd_server,
            "_remove_safe_stale_socket",
            side_effect=AssertionError("socket check must not run"),
        ) as socket_check:
            with self.assertRaises(DaemonAlreadyRunningError):
                await ready_daemon.start()
        socket_check.assert_not_awaited()
        self.assertEqual(ready_factory.calls, 0)
        await ready_daemon.shutdown()

        client = await self.client()
        await self.subscribe(client)
        self.assertEqual(self.daemon.state, DaemonState.STREAMING)
        streaming_factory = LazyFactory()
        streaming_daemon = AirPodsHubDaemon(streaming_factory, self.socket_path)
        with patch.object(
            hubd_server,
            "_remove_safe_stale_socket",
            side_effect=AssertionError("socket check must not run"),
        ) as socket_check:
            with self.assertRaises(DaemonAlreadyRunningError):
                await streaming_daemon.start()
        socket_check.assert_not_awaited()
        self.assertEqual(streaming_factory.calls, 0)
        await streaming_daemon.shutdown()

    async def test_active_daemon_socket_cannot_be_stolen(self) -> None:
        await self.start()
        first = await self.client()
        self.assertTrue((await first.request("ping"))["pong"])
        original = self.socket_path.lstat()

        second_factory = LazyFactory()
        second_daemon = AirPodsHubDaemon(second_factory, self.socket_path)
        try:
            with self.assertRaises(DaemonAlreadyRunningError):
                await second_daemon.start()
            self.assertEqual(second_daemon.state, DaemonState.FAILED)
            self.assertEqual(second_factory.calls, 0)
            self.assertEqual(second_factory.open_calls, 0)
            self.assertFalse(second_factory.sessions)
            current = self.socket_path.lstat()
            self.assertEqual(
                (current.st_dev, current.st_ino),
                (original.st_dev, original.st_ino),
            )

            later = await self.client()
            self.assertTrue((await later.request("ping"))["pong"])
            self.assertEqual(self.daemon.state, DaemonState.READY)
        finally:
            await second_daemon.shutdown()

    async def test_active_listener_without_process_lock_is_not_removed(self) -> None:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen()
        original = self.socket_path.lstat()
        factory = LazyFactory()
        daemon = AirPodsHubDaemon(factory, self.socket_path)
        try:
            with self.assertRaises(DaemonAlreadyRunningError):
                await daemon.start()
            self.assertEqual(factory.calls, 0)
            current = self.socket_path.lstat()
            self.assertEqual(
                (current.st_dev, current.st_ino),
                (original.st_dev, original.st_ino),
            )
        finally:
            await daemon.shutdown()
            listener.close()

    async def test_starting_daemon_owns_listener_before_session_open(self) -> None:
        open_gate = self.factory.session.open_gate = asyncio.Event()
        first_start = asyncio.create_task(self.daemon.start())
        await self.wait_for(lambda: self.factory.session.open_calls == 1)
        self.assertEqual(self.daemon.state, DaemonState.STARTING)
        self.assertTrue(stat.S_ISSOCK(self.socket_path.lstat().st_mode))

        second_factory = LazyFactory()
        second_daemon = AirPodsHubDaemon(second_factory, self.socket_path)
        try:
            with self.assertRaises(DaemonAlreadyRunningError):
                await second_daemon.start()
            self.assertEqual(second_factory.calls, 0)
            self.assertEqual(second_factory.open_calls, 0)
            self.assertFalse(second_factory.sessions)
            self.assertEqual(self.daemon.state, DaemonState.STARTING)

            open_gate.set()
            await first_start
            self.assertEqual(self.daemon.state, DaemonState.READY)
            client = await self.client()
            self.assertTrue((await client.request("ping"))["pong"])
            self.assertEqual(
                self.factory.calls + second_factory.calls,
                1,
            )
            self.assertEqual(
                self.factory.session.open_calls
                + second_factory.open_calls,
                1,
            )
            await client.close()
            self.clients.remove(client)
            await self.daemon.shutdown()
            self.assertFalse(self.socket_path.exists())
        finally:
            open_gate.set()
            await asyncio.gather(first_start, return_exceptions=True)
            await second_daemon.shutdown()

    async def test_simultaneous_prebind_race_has_one_session_owner(self) -> None:
        open_gate = asyncio.Event()
        first_factory = LazyFactory(open_gate)
        second_factory = LazyFactory(open_gate)
        first_daemon = AirPodsHubDaemon(first_factory, self.socket_path)
        second_daemon = AirPodsHubDaemon(second_factory, self.socket_path)
        both_at_boundary = asyncio.Event()
        arrivals = 0

        async def synchronized_prebind() -> None:
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_at_boundary.set()
            await both_at_boundary.wait()

        try:
            with patch.object(
                AirPodsHubDaemon,
                "_before_process_lock",
                side_effect=synchronized_prebind,
            ):
                starts = [
                    asyncio.create_task(first_daemon.start()),
                    asyncio.create_task(second_daemon.start()),
                ]
                await self.wait_for(
                    lambda: first_factory.open_calls + second_factory.open_calls == 1
                )
                await self.wait_for(lambda: any(task.done() for task in starts))
                self.assertEqual(
                    first_factory.calls + second_factory.calls, 1
                )
                self.assertEqual(
                    len(first_factory.sessions) + len(second_factory.sessions), 1
                )
                self.assertEqual(
                    first_factory.open_calls + second_factory.open_calls, 1
                )
                self.assertEqual(
                    sum(
                        daemon.state is DaemonState.STARTING
                        for daemon in (first_daemon, second_daemon)
                    ),
                    1,
                )
                open_gate.set()
                results = await asyncio.gather(*starts, return_exceptions=True)

            self.assertEqual(sum(result is None for result in results), 1)
            self.assertEqual(
                sum(
                    isinstance(result, DaemonAlreadyRunningError)
                    for result in results
                ),
                1,
            )
            winners = [
                daemon
                for daemon in (first_daemon, second_daemon)
                if daemon.state is DaemonState.READY
            ]
            self.assertEqual(len(winners), 1)
            client = await JsonClient.connect(self.socket_path)
            try:
                self.assertTrue((await client.request("ping"))["pong"])
            finally:
                await client.close()
        finally:
            open_gate.set()
            await first_daemon.shutdown()
            await second_daemon.shutdown()
        self.assertFalse(self.socket_path.exists())

    async def test_cross_process_lock_exclusion_and_reuse(self) -> None:
        context = multiprocessing.get_context("spawn")
        lock_path = self.socket_path.with_suffix(".lock")
        processes: list[Any] = []
        release_events: list[Any] = []
        queues: list[Any] = []
        try:
            a_opened = context.Event()
            a_release_open = context.Event()
            a_shutdown = context.Event()
            a_messages = context.Queue()
            release_events.extend((a_release_open, a_shutdown))
            queues.append(a_messages)
            process_a = context.Process(
                target=run_process_daemon,
                args=(
                    str(self.socket_path),
                    a_opened,
                    a_release_open,
                    a_shutdown,
                    a_messages,
                ),
            )
            processes.append(process_a)
            process_a.start()
            self.assertTrue(await asyncio.to_thread(a_opened.wait, 10.0))
            self.assertTrue(stat.S_ISSOCK(self.socket_path.lstat().st_mode))
            self.assertTrue(stat.S_ISREG(lock_path.lstat().st_mode))

            b_opened = context.Event()
            b_release_open = context.Event()
            b_release_open.set()
            b_shutdown = context.Event()
            b_messages = context.Queue()
            queues.append(b_messages)
            process_b = context.Process(
                target=run_process_daemon,
                args=(
                    str(self.socket_path),
                    b_opened,
                    b_release_open,
                    b_shutdown,
                    b_messages,
                ),
            )
            processes.append(process_b)
            process_b.start()
            rejected = await self.process_message(b_messages)
            self.assertEqual(rejected["phase"], "start_failed")
            self.assertEqual(rejected["error"], "DaemonAlreadyRunningError")
            self.assertEqual(rejected["factory_calls"], 0)
            self.assertEqual(rejected["open_calls"], 0)
            await self.join_process(process_b)

            a_release_open.set()
            ready_a = await self.process_message(a_messages)
            self.assertEqual(ready_a["phase"], "ready")
            self.assertEqual(ready_a["state"], "ready")
            self.assertEqual(ready_a["factory_calls"], 1)
            self.assertEqual(ready_a["open_calls"], 1)
            client_a = await JsonClient.connect(self.socket_path)
            try:
                self.assertTrue((await client_a.request("ping"))["pong"])
            finally:
                await client_a.close()
            a_shutdown.set()
            stopped_a = await self.process_message(a_messages)
            self.assertEqual(stopped_a["phase"], "stopped")
            self.assertEqual(stopped_a["close_calls"], 1)
            self.assertTrue(stopped_a["lock_file_exists"])
            self.assertFalse(stopped_a["socket_exists"])
            await self.join_process(process_a)

            c_opened = context.Event()
            c_release_open = context.Event()
            c_release_open.set()
            c_shutdown = context.Event()
            c_messages = context.Queue()
            release_events.append(c_shutdown)
            queues.append(c_messages)
            process_c = context.Process(
                target=run_process_daemon,
                args=(
                    str(self.socket_path),
                    c_opened,
                    c_release_open,
                    c_shutdown,
                    c_messages,
                ),
            )
            processes.append(process_c)
            process_c.start()
            ready_c = await self.process_message(c_messages)
            self.assertEqual(ready_c["phase"], "ready")
            self.assertEqual(ready_c["factory_calls"], 1)
            self.assertEqual(ready_c["open_calls"], 1)
            client_c = await JsonClient.connect(self.socket_path)
            try:
                self.assertTrue((await client_c.request("ping"))["pong"])
            finally:
                await client_c.close()
            c_shutdown.set()
            stopped_c = await self.process_message(c_messages)
            self.assertEqual(stopped_c["phase"], "stopped")
            self.assertEqual(stopped_c["close_calls"], 1)
            self.assertTrue(lock_path.exists())
            self.assertFalse(self.socket_path.exists())
            await self.join_process(process_c)
        finally:
            for event in release_events:
                event.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                await asyncio.to_thread(process.join, 5.0)
            for messages in queues:
                messages.close()
                messages.join_thread()

    async def test_cross_process_lock_covers_bind_listen_window(self) -> None:
        context = multiprocessing.get_context("spawn")
        processes: list[Any] = []
        release_events: list[Any] = []
        queues: list[Any] = []
        try:
            a_opened = context.Event()
            a_release_open = context.Event()
            a_release_open.set()
            a_shutdown = context.Event()
            a_bound = context.Event()
            a_release_bind = context.Event()
            a_messages = context.Queue()
            release_events.extend((a_shutdown, a_release_bind))
            queues.append(a_messages)
            process_a = context.Process(
                target=run_process_daemon,
                args=(
                    str(self.socket_path),
                    a_opened,
                    a_release_open,
                    a_shutdown,
                    a_messages,
                    a_bound,
                    a_release_bind,
                ),
            )
            processes.append(process_a)
            process_a.start()
            self.assertTrue(await asyncio.to_thread(a_bound.wait, 10.0))
            original = self.socket_path.lstat()

            b_opened = context.Event()
            b_release_open = context.Event()
            b_release_open.set()
            b_shutdown = context.Event()
            b_messages = context.Queue()
            queues.append(b_messages)
            process_b = context.Process(
                target=run_process_daemon,
                args=(
                    str(self.socket_path),
                    b_opened,
                    b_release_open,
                    b_shutdown,
                    b_messages,
                ),
            )
            processes.append(process_b)
            process_b.start()
            rejected = await self.process_message(b_messages)
            self.assertEqual(rejected["error"], "DaemonAlreadyRunningError")
            self.assertEqual(rejected["factory_calls"], 0)
            self.assertEqual(rejected["open_calls"], 0)
            current = self.socket_path.lstat()
            self.assertEqual(
                (current.st_dev, current.st_ino),
                (original.st_dev, original.st_ino),
            )
            await self.join_process(process_b)

            a_release_bind.set()
            ready = await self.process_message(a_messages)
            self.assertEqual(ready["phase"], "ready")
            self.assertEqual(ready["factory_calls"], 1)
            self.assertEqual(ready["open_calls"], 1)
            client = await JsonClient.connect(self.socket_path)
            try:
                self.assertTrue((await client.request("ping"))["pong"])
            finally:
                await client.close()
            a_shutdown.set()
            stopped = await self.process_message(a_messages)
            self.assertEqual(stopped["phase"], "stopped")
            self.assertFalse(stopped["socket_exists"])
            await self.join_process(process_a)
        finally:
            for event in release_events:
                event.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                await asyncio.to_thread(process.join, 5.0)
            for messages in queues:
                messages.close()
                messages.join_thread()

    async def test_asyncio_receives_only_prebound_owned_socket(self) -> None:
        original = asyncio.start_unix_server
        with patch.object(
            asyncio, "start_unix_server", wraps=original
        ) as start_server:
            await self.start()
        kwargs = start_server.await_args.kwargs
        self.assertIsInstance(kwargs["sock"], socket.socket)
        self.assertNotIn("path", kwargs)
        self.assertFalse(kwargs["cleanup_socket"])

    async def test_asyncio_handoff_failure_removes_owned_listener(self) -> None:
        with patch.object(
            asyncio,
            "start_unix_server",
            side_effect=RuntimeError("handoff failed"),
        ):
            with self.assertRaises(SessionOperationError):
                await self.daemon.start()
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(self.factory.session.open_calls, 0)
        self.assertEqual(self.daemon.state, DaemonState.FAILED)
        self.assertIsNone(self.daemon._server)
        self.assertFalse(self.socket_path.exists())
        await self.daemon.shutdown()
        self.assertEqual(self.factory.session.close_calls, 1)

    async def test_owned_stale_socket_is_safely_replaced(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.socket_path))
        stale.close()
        await self.start()
        self.assertEqual(self.daemon.state, DaemonState.READY)
        self.assertTrue(stat.S_ISSOCK(self.socket_path.lstat().st_mode))
        client = await self.client()
        self.assertTrue((await client.request("ping"))["pong"])
        await self.daemon.shutdown()
        self.assertFalse(self.socket_path.exists())

    async def test_shutdown_disables_new_accepts_before_stop_finishes(self) -> None:
        await self.start()
        client = await self.client()
        await self.subscribe(client)
        stop_gate = self.factory.session.stop_gate = asyncio.Event()
        shutdown_task = asyncio.create_task(self.daemon.shutdown())
        await self.wait_for(lambda: self.factory.session.stop_calls == 1)
        self.assertEqual(self.daemon.state, DaemonState.SHUTTING_DOWN)
        self.assertIsNone(self.daemon._server)
        self.assertIsNotNone(self.daemon._lock_fd)

        second_factory = LazyFactory()
        second_daemon = AirPodsHubDaemon(second_factory, self.socket_path)
        with self.assertRaises(DaemonAlreadyRunningError):
            await second_daemon.start()
        self.assertEqual(second_factory.calls, 0)
        await second_daemon.shutdown()

        with self.assertRaises(OSError):
            await asyncio.open_unix_connection(self.socket_path)

        stop_gate.set()
        await shutdown_task
        self.assertFalse(self.daemon._clients)
        self.assertFalse(self.daemon._handler_tasks)
        self.assertFalse(self.daemon._background_tasks)
        self.assertFalse(self.daemon.report_reader_active)
        self.assertIsNone(self.daemon._reader_task)
        self.assertEqual(self.factory.session.close_calls, 1)
        self.assertFalse(self.socket_path.exists())
        self.assertIsNone(self.daemon._lock_fd)
        self.assertTrue(self.daemon._lock_path.exists())

    async def test_shutdown_does_not_remove_replacement_regular_file(self) -> None:
        await self.start()
        self.socket_path.unlink()
        self.socket_path.write_text("replacement")
        await self.daemon.shutdown()
        self.assertEqual(self.socket_path.read_text(), "replacement")

    async def test_shutdown_does_not_remove_replacement_unix_socket(self) -> None:
        await self.start()
        self.socket_path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(self.socket_path))
        try:
            await self.daemon.shutdown()
            self.assertTrue(stat.S_ISSOCK(self.socket_path.lstat().st_mode))
        finally:
            replacement.close()
            self.socket_path.unlink(missing_ok=True)

    async def test_uid_mismatch_is_rejected(self) -> None:
        self.daemon = AirPodsHubDaemon(
            self.factory,
            self.socket_path,
            peer_uid_provider=lambda _sock: os.geteuid() + 1,
        )
        await self.start()
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        writer.write(b'{"protocol_version":1,"operation":"ping"}\n')
        await writer.drain()
        try:
            closed = await asyncio.wait_for(reader.readline(), 1.0)
        except ConnectionResetError:
            closed = b""
        self.assertEqual(closed, b"")
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass
        self.assertEqual(self.daemon.subscriber_count, 0)

    async def test_slow_client_disconnect_does_not_block_fast_client(self) -> None:
        await self.start()
        slow = await self.client()
        slow_server_client = next(iter(self.daemon._clients))
        fast = await self.client()
        await self.subscribe(slow)
        await self.subscribe(fast)

        slow_server_client.closing = True
        assert slow_server_client.writer_task is not None
        slow_server_client.writer_task.cancel()
        await asyncio.gather(slow_server_client.writer_task, return_exceptions=True)
        slow_server_client.writer_task = None
        slow_server_client.closing = False
        for _ in range(OUTBOUND_QUEUE_SIZE):
            slow_server_client.outbound.put_nowait(b"occupied\n")

        self.factory.session.inject(report(88))
        fast_event = await fast.read()
        self.assertEqual(fast_event["bpm"], 88)
        await self.wait_for(lambda: slow_server_client not in self.daemon._clients)
        self.assertEqual(self.daemon.subscriber_count, 1)
        self.assertEqual(self.daemon.state, DaemonState.STREAMING)
        self.assertEqual(self.factory.session.stop_calls, 0)

    async def test_startup_failure_enters_failed_and_can_clean_up(self) -> None:
        self.factory.session.open_error = RuntimeError("open failed")
        with self.assertRaises(SessionOperationError):
            await self.daemon.start()
        self.assertEqual(self.daemon.state, DaemonState.FAILED)
        self.assertEqual(self.factory.calls, 1)
        self.assertIsNone(self.daemon._server)
        self.assertFalse(self.socket_path.exists())
        self.assertIsNotNone(self.daemon._lock_fd)
        second_factory = LazyFactory()
        second_daemon = AirPodsHubDaemon(second_factory, self.socket_path)
        with self.assertRaises(DaemonAlreadyRunningError):
            await second_daemon.start()
        self.assertEqual(second_factory.calls, 0)
        await second_daemon.shutdown()
        await self.daemon.shutdown()
        self.assertEqual(self.factory.session.close_calls, 1)
        self.assertIsNone(self.daemon._lock_fd)


class SocketPathSafetyTests(unittest.TestCase):
    def test_missing_xdg_runtime_directory_has_no_fallback(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(UnsafeSocketPathError, "XDG_RUNTIME_DIR"):
                socket_path_from_environment()

    def test_regular_file_at_socket_path_is_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hubd.sock"
            path.write_text("keep me")
            factory = FakeFactory()
            daemon = AirPodsHubDaemon(factory, path)
            with self.assertRaises(UnsafeSocketPathError):
                asyncio.run(daemon.start())
            self.assertEqual(path.read_text(), "keep me")
            self.assertEqual(factory.calls, 0)
            self.assertIsNone(daemon._lock_fd)

    def test_lock_file_symlink_is_rejected_without_socket_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "hubd.sock"
            lock_path = socket_path.with_suffix(".lock")
            target = Path(directory) / "target"
            target.write_text("keep me")
            lock_path.symlink_to(target)
            factory = FakeFactory()
            daemon = AirPodsHubDaemon(factory, socket_path)
            with self.assertRaises(UnsafeSocketPathError):
                asyncio.run(daemon.start())
            self.assertEqual(factory.calls, 0)
            self.assertFalse(socket_path.exists())
            self.assertTrue(lock_path.is_symlink())
            self.assertEqual(target.read_text(), "keep me")

    def test_directory_at_lock_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "hubd.sock"
            lock_path = socket_path.with_suffix(".lock")
            lock_path.mkdir()
            factory = FakeFactory()
            daemon = AirPodsHubDaemon(factory, socket_path)
            with self.assertRaises(UnsafeSocketPathError):
                asyncio.run(daemon.start())
            self.assertEqual(factory.calls, 0)
            self.assertTrue(lock_path.is_dir())
            self.assertFalse(socket_path.exists())

    def test_fifo_at_lock_path_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "hubd.sock"
            lock_path = socket_path.with_suffix(".lock")
            os.mkfifo(lock_path, mode=0o600)
            factory = FakeFactory()
            daemon = AirPodsHubDaemon(factory, socket_path)
            with self.assertRaises(UnsafeSocketPathError):
                asyncio.run(daemon.start())
            self.assertEqual(factory.calls, 0)
            self.assertTrue(stat.S_ISFIFO(lock_path.lstat().st_mode))
            self.assertFalse(socket_path.exists())

    def test_foreign_owned_lock_stat_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "hubd.sock"
            daemon = AirPodsHubDaemon(FakeFactory(), socket_path)
            real_fstat = os.fstat

            def foreign_fstat(lock_fd: int) -> Any:
                actual = real_fstat(lock_fd)
                return SimpleNamespace(
                    st_mode=actual.st_mode,
                    st_uid=os.geteuid() + 1,
                )

            with patch.object(
                hubd_server.os, "fstat", side_effect=foreign_fstat
            ):
                with self.assertRaises(UnsafeSocketPathError):
                    daemon._acquire_process_lock()
            self.assertIsNone(daemon._lock_fd)
            self.assertFalse(socket_path.exists())

    def test_foreign_owned_socket_is_never_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hubd.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))
            stale.close()
            with patch.object(
                hubd_server.os, "geteuid", return_value=os.geteuid() + 1
            ):
                with self.assertRaises(UnsafeSocketPathError):
                    asyncio.run(hubd_server._remove_safe_stale_socket(path))
            self.assertTrue(stat.S_ISSOCK(path.lstat().st_mode))

    def test_path_replacement_during_stale_probe_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hubd.sock"
            moved = Path(directory) / "original.sock"
            original = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            original.bind(str(path))
            replacements: list[socket.socket] = []

            def replace_path(_path: Path) -> None:
                path.rename(moved)
                replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                replacement.bind(str(path))
                replacements.append(replacement)

            factory = FakeFactory()
            daemon = AirPodsHubDaemon(factory, path)
            try:
                with patch.object(
                    hubd_server,
                    "_probe_existing_socket",
                    side_effect=replace_path,
                ):
                    with self.assertRaisesRegex(UnsafeSocketPathError, "changed"):
                        asyncio.run(daemon.start())
                self.assertEqual(factory.calls, 0)
                self.assertTrue(stat.S_ISSOCK(path.lstat().st_mode))
                self.assertTrue(stat.S_ISSOCK(moved.lstat().st_mode))
            finally:
                original.close()
                for replacement in replacements:
                    replacement.close()

    def test_ambiguous_socket_probe_error_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hubd.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))
            stale.close()

            class AmbiguousProbe:
                def settimeout(self, _timeout: float) -> None:
                    pass

                def connect(self, _path: str) -> None:
                    raise PermissionError(errno.EACCES, "ambiguous")

                def close(self) -> None:
                    pass

            async def attempt_removal() -> None:
                with patch.object(
                    hubd_server.socket, "socket", return_value=AmbiguousProbe()
                ):
                    await hubd_server._remove_safe_stale_socket(path)

            with self.assertRaisesRegex(UnsafeSocketPathError, "proven stale"):
                asyncio.run(attempt_removal())
            self.assertTrue(stat.S_ISSOCK(path.lstat().st_mode))

    def test_symlinked_runtime_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer)
            target = root / "target"
            target.mkdir(mode=0o700)
            link = root / "runtime"
            link.symlink_to(target, target_is_directory=True)
            daemon = AirPodsHubDaemon(FakeFactory(), link / "hubd.sock")
            with self.assertRaisesRegex(UnsafeSocketPathError, "canonical"):
                asyncio.run(daemon.start())

    def test_public_package_exports_remain_unchanged(self) -> None:
        self.assertEqual(
            airpods_hr.__all__,
            [
                "HeartRateMarkerNotFoundError",
                "HeartRateParseError",
                "HeartRateReport",
                "HeartRateReportIDError",
                "HeartRateReportTruncatedError",
                "parse_heart_rate_packet",
            ],
        )
        self.assertFalse(any("hub" in name.lower() for name in airpods_hr.__all__))

    def test_bluetooth_production_modules_do_not_import_hubd(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "airpods_hr"
        production_files = [
            path for path in root.glob("*.py") if path.name != "service_installer.py"
        ]
        for path in production_files:
            self.assertNotIn("airpods_hr._hubd", path.read_text(), path.name)

    def test_only_python_policy_and_analysis_adapters_import_native_bridge(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src"
        native_importers = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if "_airpods_aap_core" in path.read_text()
        }
        self.assertEqual(
            native_importers,
            {
                "airpods_hr/heartrate.py",
                "airpods_hr/hr_semantics.py",
                "airpods_hr/aap.py",
                "airpods_hr/heart_rate_session.py",
                "airpods_hr/production_session.py",
                "airpods_hr/sdp.py",
                "airpods_hr/sdp_diagnostics.py",
                "airpods_hr/reference_sdp_footprint.py",
                "airpods_hr/pre_auth_diagnostics.py",
                "airpods_hr/pre_aap_diagnostics.py",
                "airpods_hr/aap_config_diagnostics.py",
                "airpods_hr/aap_local_rx_diagnostics.py",
                "airpods_hr/classic_diagnostics.py",
                "airpods_hr/bluez_sdp_audit.py",
                "airpods_hr/_hubd/protocol.py",
                "airpods_hr/_hubd/server.py",
            },
        )




class HubProbeTests(unittest.TestCase):
    def test_private_probe_proves_one_session_fanout(self) -> None:
        stream = StringIO()
        self.assertEqual(probe_main(["--samples", "3"], stream=stream), 0)
        output = stream.getvalue()
        for expected in (
            "session_objects_created=1",
            "session_opens=1",
            "session_starts=1",
            "reports_read=3",
            "client_a_reports=3",
            "client_b_reports=3",
            "session_stops=1",
            "session_closes=1",
            "HUBD PRIVATE PROBE PASS",
        ):
            self.assertIn(expected, output)
