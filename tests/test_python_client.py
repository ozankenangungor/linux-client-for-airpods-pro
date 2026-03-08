"""Hardware-independent protocol and lifecycle tests for airpods_client."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from typing import Awaitable, Callable

from airpods_client import AirPodsClient, ConnectionFailed, DaemonError, DaemonState, FrameTooLarge, InvalidMessage, ProtocolVersionError, SourceSide, XdgRuntimeDirMissing


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


