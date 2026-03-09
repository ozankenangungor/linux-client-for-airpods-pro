"""Hardware-independent protocol and lifecycle tests for airpods_client."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from typing import Any, Awaitable, Callable

from airpods_client import (
    AirPodsClient,
    ConnectionClosed,
    ConnectionFailed,
    DaemonError,
    DaemonState,
    FrameTooLarge,
    InvalidMessage,
    ProtocolVersionError,
    SourceSide,
    SubscriptionActive,
    XdgRuntimeDirMissing,
)


Handler = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]


def message(**fields: object) -> bytes:
    return json.dumps(
        {"protocol_version": 1, **fields},
        separators=(",", ":"),
        sort_keys=True,
    ).encode() + b"\n"


def success(operation: str, **fields: object) -> bytes:
    return message(ok=True, operation=operation, **fields)


def subscription_response(operation: str, subscribed: bool) -> bytes:
    idempotence = (
        {"already_subscribed": False}
        if subscribed
        else {"already_unsubscribed": False}
    )
    return success(
        operation,
        stream="heart_rate",
        subscribed=subscribed,
        **idempotence,
    )


def event(bpm: int, side: str, raw: int | None = None) -> bytes:
    fields: dict[str, object] = {
        "event": "heart_rate",
        "bpm": bpm,
        "source_side": side,
    }
    if raw is not None:
        fields["source_side_raw"] = raw
    return message(**fields)


async def read_operation(reader: asyncio.StreamReader, expected: str) -> None:
    frame = await reader.readline()
    request = json.loads(frame)
    if request.get("operation") != expected:
        raise AssertionError(
            f"expected {expected}, received {request.get('operation')}"
        )
    if request.get("protocol_version") != 1:
        raise AssertionError("client did not send protocol version 1")


class PackageBoundaryTests(unittest.TestCase):
    def test_package_is_stdlib_only_and_has_no_autostart_or_bluetooth_path(
        self,
    ) -> None:
        package = Path(__file__).parents[1] / "src/airpods_client"
        source = "\n".join(
            path.read_text() for path in sorted(package.glob("*.py"))
        )
        for forbidden in (
            "airpods_hr",
            "bumble",
            "dbus_next",
            "production_session",
            "bluez_coexistence",
            "AF_" + "BLUETOOTH",
            "system" + "ctl",
            "create_subprocess",
        ):
            self.assertNotIn(forbidden, source)

    def test_public_models_match_protocol_v1_semantics(self) -> None:
        self.assertEqual(DaemonState.READY.value, "ready")
        self.assertEqual(DaemonState.STREAMING.value, "streaming")
        self.assertEqual(SourceSide.LEFT.value, "left")
        self.assertEqual(SourceSide.RIGHT.value, "right")
        self.assertEqual(SourceSide.UNKNOWN.value, "unknown")


class PythonClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temp.name) / "test.sock"
        self.servers: list[asyncio.AbstractServer] = []
        self.server_tasks: list[asyncio.Task[None]] = []
        self.clients: list[AirPodsClient] = []

    async def asyncTearDown(self) -> None:
        for client in self.clients:
            await client.close()
        for server in self.servers:
            server.close()
            await server.wait_closed()
        tasks = tuple(self.server_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            ]
            if errors:
                raise errors[0]
        self.temp.cleanup()

    async def connect(self, handler: Handler) -> AirPodsClient:
        def accepted(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            async def run_handler() -> None:
                try:
                    await handler(reader, writer)
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except (ConnectionError, OSError):
                        pass

            task = asyncio.create_task(run_handler())
            self.server_tasks.append(task)

        server = await asyncio.start_unix_server(accepted, self.socket_path)
        self.servers.append(server)
        client = await AirPodsClient.connect_to(self.socket_path)
        self.clients.append(client)
        return client

    async def test_default_socket_and_explicit_path_hello_ping(self) -> None:
        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await read_operation(reader, "hello")
            writer.write(
                success(
                    "hello",
                    service="airpods-hubd",
                    experimental=True,
                )
            )
            await writer.drain()
            await read_operation(reader, "ping")
            writer.write(success("ping", pong=True))
            await writer.drain()
            await reader.read()

        runtime_socket = Path(self.temp.name) / "airpods-hubd.sock"
        self.socket_path = runtime_socket
        with mock.patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": self.temp.name}, clear=False
        ):
            client = await self.connect(handler)
            self.assertEqual(AirPodsClient.default_socket_path(), runtime_socket)
        hello = await client.hello()
        self.assertEqual(hello.service, "airpods-hubd")
        self.assertTrue(hello.experimental)
        await client.ping()

    async def test_missing_runtime_directory_fails_clearly(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(XdgRuntimeDirMissing):
                await AirPodsClient.connect()

    async def test_absent_explicit_daemon_is_a_connection_error(self) -> None:
        with self.assertRaises(ConnectionFailed):
            await AirPodsClient.connect_to(self.socket_path)

    async def test_ready_and_streaming_status(self) -> None:
        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            for state, count in (("ready", 0), ("streaming", 2)):
                await read_operation(reader, "status")
                writer.write(
                    success("status", state=state, subscriber_count=count)
                )
                await writer.drain()
            await reader.read()

        client = await self.connect(handler)
        ready = await client.status()
        streaming = await client.status()
        self.assertEqual(ready.state, DaemonState.READY)
        self.assertEqual(ready.subscriber_count, 0)
        self.assertEqual(streaming.state, DaemonState.STREAMING)
        self.assertEqual(streaming.subscriber_count, 2)

    async def test_events_interleave_and_preserve_order_duplicates_and_sides(
        self,
    ) -> None:
        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await read_operation(reader, "subscribe")
            writer.write(event(169, "left"))
            writer.write(subscription_response("subscribe", True))
            writer.write(event(88, "right"))
            writer.write(event(88, "right"))
            writer.write(event(73, "unknown", 37))
            await writer.drain()
            await read_operation(reader, "status")
            writer.write(
                event(72, "left")
                + success("status", state="streaming", subscriber_count=1)
            )
            await writer.drain()
            await read_operation(reader, "unsubscribe")
            writer.write(subscription_response("unsubscribe", False))
            await writer.drain()
            await reader.read()

        client = await self.connect(handler)
        subscription = await client.subscribe_heart_rate()
        first = [await subscription.__anext__() for _ in range(4)]
        status_task = asyncio.create_task(client.status())
        fifth = await subscription.__anext__()
        status = await status_task
        self.assertEqual([sample.bpm for sample in first], [169, 88, 88, 73])
        self.assertEqual(
            [sample.source_side for sample in first],
            [
                SourceSide.LEFT,
                SourceSide.RIGHT,
                SourceSide.RIGHT,
                SourceSide.UNKNOWN,
            ],
        )
        self.assertEqual(first[3].source_side_raw, 37)
        self.assertEqual((fifth.bpm, fifth.source_side), (72, SourceSide.LEFT))
        self.assertEqual(status.state, DaemonState.STREAMING)
        await subscription.unsubscribe()
        self.assertIsNone(await subscription.next())

    async def test_second_live_subscription_is_rejected(self) -> None:
        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await read_operation(reader, "subscribe")
            writer.write(subscription_response("subscribe", True))
            await writer.drain()
            await read_operation(reader, "unsubscribe")
            writer.write(subscription_response("unsubscribe", False))
            await writer.drain()
            await reader.read()

        client = await self.connect(handler)
        subscription = await client.subscribe_heart_rate()
        with self.assertRaises(SubscriptionActive):
            await client.subscribe_heart_rate()
        await subscription.close()

    async def assert_reader_error(
        self,
        frame: bytes,
        expected: type[BaseException],
    ) -> BaseException:
        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await reader.readline()
            writer.write(frame)
            await writer.drain()
            await reader.read()

        client = await self.connect(handler)
        with self.assertRaises(expected) as caught:
            await client.status()
        return caught.exception

    async def test_malformed_non_object_and_unexpected_messages(self) -> None:
        cases: list[tuple[bytes, type[BaseException]]] = [
            (b"{not-json}\n", InvalidMessage),
            (b"\xff\n", InvalidMessage),
            (b"[]\n", InvalidMessage),
            (message(operation="status"), InvalidMessage),
            (success("ping", pong=True), InvalidMessage),
        ]
        for index, (frame, expected) in enumerate(cases):
            with self.subTest(index=index):
                await self.assert_reader_error(frame, expected)
            self.socket_path = Path(self.temp.name) / f"test-{index}.sock"

    async def test_oversized_frame_is_rejected(self) -> None:
        await self.assert_reader_error(b"{" + b"x" * 4096 + b"}\n", FrameTooLarge)

    async def test_protocol_mismatch_is_rejected(self) -> None:
        error = await self.assert_reader_error(
            b'{"protocol_version":2,"ok":true}\n',
            ProtocolVersionError,
        )
        self.assertEqual(error.received, 2)

    async def test_daemon_error_is_typed(self) -> None:
        frame = message(
            ok=False,
            error={"code": "service_unavailable", "message": "unavailable"},
        )
        error = await self.assert_reader_error(frame, DaemonError)
        self.assertEqual(error.code, "service_unavailable")
        self.assertEqual(error.message, "unavailable")

    async def test_connection_close_fails_request_and_subscription(self) -> None:
        close_now = asyncio.Event()

        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await read_operation(reader, "subscribe")
            writer.write(subscription_response("subscribe", True))
            await writer.drain()
            await close_now.wait()

        client = await self.connect(handler)
        subscription = await client.subscribe_heart_rate()
        waiting = asyncio.create_task(subscription.__anext__())
        close_now.set()
        with self.assertRaises(ConnectionClosed):
            await waiting
        with self.assertRaises(ConnectionClosed):
            await client.status()

    async def test_cancelled_request_cannot_steal_later_response(self) -> None:
        first_seen = asyncio.Event()
        check_started = asyncio.Event()
        release_first = asyncio.Event()
        request_b_arrived_early = False

        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            nonlocal request_b_arrived_early
            await read_operation(reader, "ping")
            first_seen.set()
            second_read = asyncio.create_task(reader.readline())
            release_wait = asyncio.create_task(release_first.wait())
            check_started.set()
            done, _ = await asyncio.wait(
                {second_read, release_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            request_b_arrived_early = second_read in done
            await release_wait
            writer.write(success("ping", pong=True))
            await writer.drain()
            second = json.loads(await second_read)
            if second.get("operation") != "status":
                raise AssertionError("request B was not status")
            writer.write(
                success("status", state="ready", subscriber_count=0)
            )
            await writer.drain()
            await reader.read()

        client = await self.connect(handler)
        request_a = asyncio.create_task(client.ping())
        await first_seen.wait()
        request_a.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request_a
        request_b = asyncio.create_task(client.status())
        await check_started.wait()
        self.assertFalse(request_b.done())
        release_first.set()
        status = await request_b
        self.assertFalse(request_b_arrived_early)
        self.assertEqual(status.state, DaemonState.READY)

    async def test_cancelled_subscribe_cleans_before_replacement(self) -> None:
        first_seen = asyncio.Event()
        cleanup_seen = asyncio.Event()
        release_first = asyncio.Event()
        release_cleanup = asyncio.Event()
        wire: list[str] = []

        async def operation(reader: asyncio.StreamReader) -> str:
            request = json.loads(await reader.readline())
            value = request["operation"]
            wire.append(value)
            return value

        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            self.assertEqual(await operation(reader), "subscribe")
            first_seen.set()
            await release_first.wait()
            writer.write(event(40, "left"))
            writer.write(subscription_response("subscribe", True))
            await writer.drain()
            self.assertEqual(await operation(reader), "unsubscribe")
            cleanup_seen.set()
            await release_cleanup.wait()
            writer.write(event(41, "right"))
            writer.write(subscription_response("unsubscribe", False))
            await writer.drain()
            self.assertEqual(await operation(reader), "subscribe")
            writer.write(subscription_response("subscribe", True))
            writer.write(event(99, "unknown", 37))
            await writer.drain()
            self.assertEqual(await operation(reader), "unsubscribe")
            writer.write(subscription_response("unsubscribe", False))
            await writer.drain()
            await reader.read()

        client = await self.connect(handler)
        cancelled = asyncio.create_task(client.subscribe_heart_rate())
        await first_seen.wait()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        replacement_task = asyncio.create_task(client.subscribe_heart_rate())
        release_first.set()
        await cleanup_seen.wait()
        self.assertFalse(replacement_task.done())
        release_cleanup.set()
        replacement = await replacement_task
        sample = await replacement.__anext__()
        self.assertEqual((sample.bpm, sample.source_side_raw), (99, 37))
        await replacement.close()
        self.assertEqual(
            wire,
            ["subscribe", "unsubscribe", "subscribe", "unsubscribe"],
        )

    async def test_cancelled_unsubscribe_finishes_before_replacement(self) -> None:
        unsubscribe_seen = asyncio.Event()
        release_unsubscribe = asyncio.Event()
        wire: list[str] = []

        async def operation(reader: asyncio.StreamReader) -> str:
            request = json.loads(await reader.readline())
            value = request["operation"]
            wire.append(value)
            return value

        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            self.assertEqual(await operation(reader), "subscribe")
            writer.write(subscription_response("subscribe", True))
            await writer.drain()
            self.assertEqual(await operation(reader), "unsubscribe")
            unsubscribe_seen.set()
            await release_unsubscribe.wait()
            writer.write(subscription_response("unsubscribe", False))
            await writer.drain()
            self.assertEqual(await operation(reader), "subscribe")
            writer.write(subscription_response("subscribe", True))
            writer.write(event(77, "left"))
            await writer.drain()
            self.assertEqual(await operation(reader), "unsubscribe")
            writer.write(subscription_response("unsubscribe", False))
            await writer.drain()
            await reader.read()

        client = await self.connect(handler)
        subscription = await client.subscribe_heart_rate()
        close_caller = asyncio.create_task(subscription.close())
        await unsubscribe_seen.wait()
        close_caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await close_caller
        replacement_task = asyncio.create_task(client.subscribe_heart_rate())
        self.assertFalse(replacement_task.done())
        release_unsubscribe.set()
        replacement = await replacement_task
        self.assertEqual((await replacement.__anext__()).bpm, 77)
        await replacement.close()
        self.assertEqual(
            wire,
            ["subscribe", "unsubscribe", "subscribe", "unsubscribe"],
        )

    async def test_subscription_close_is_idempotent(self) -> None:
        unsubscribe_calls = 0

        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            nonlocal unsubscribe_calls
            await read_operation(reader, "subscribe")
            writer.write(subscription_response("subscribe", True))
            await writer.drain()
            await read_operation(reader, "unsubscribe")
            unsubscribe_calls += 1
            writer.write(subscription_response("unsubscribe", False))
            await writer.drain()
            await reader.read()

        client = await self.connect(handler)
        subscription = await client.subscribe_heart_rate()
        iterator_started = asyncio.Event()

        async def wait_for_sample() -> object:
            iterator_started.set()
            return await subscription.__anext__()

        waiting = asyncio.create_task(wait_for_sample())
        await iterator_started.wait()
        await asyncio.gather(subscription.close(), subscription.close())
        with self.assertRaises(StopAsyncIteration):
            await waiting
        await subscription.close()
        self.assertEqual(unsubscribe_calls, 1)

    async def test_client_close_with_active_subscription_is_idempotent(
        self,
    ) -> None:
        disconnected = asyncio.Event()

        async def handler(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            await read_operation(reader, "subscribe")
            writer.write(subscription_response("subscribe", True))
            await writer.drain()
            await reader.read()
            disconnected.set()

        client = await self.connect(handler)
        subscription = await client.subscribe_heart_rate()
        await asyncio.gather(client.close(), client.close())
        await disconnected.wait()
        with self.assertRaises(ConnectionClosed):
            await subscription.__anext__()


if __name__ == "__main__":
    unittest.main()
