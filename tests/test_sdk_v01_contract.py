"""External Python contract for the supported airpods_client v0.1 surface."""

from __future__ import annotations

import asyncio
from dataclasses import fields
import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from tests.client_sdk_test_support import (
    PYTHON_CLIENT_ROOT,
    PYTHON_CLIENT_SRC as _CLIENT_SRC,
    ROOT,
)

import airpods_client
from airpods_client import (
    AirPodsClient,
    AirPodsClientError,
    ConnectionClosed,
    ConnectionFailed,
    DaemonError,
    DaemonState,
    EventBufferFull,
    FrameTooLarge,
    HeartRateSample,
    HeartRateSubscription,
    Hello,
    InvalidMessage,
    ProtocolVersionError,
    SourceSide,
    Status,
    SubscriptionActive,
    XdgRuntimeDirMissing,
)


PUBLIC_NAMES = [
    "AirPodsClient",
    "AirPodsClientError",
    "ConnectionClosed",
    "ConnectionFailed",
    "DaemonError",
    "DaemonState",
    "EventBufferFull",
    "FrameTooLarge",
    "HeartRateSample",
    "HeartRateSubscription",
    "Hello",
    "InvalidMessage",
    "ProtocolVersionError",
    "SourceSide",
    "Status",
    "SubscriptionActive",
    "XdgRuntimeDirMissing",
]


def frame(**values: object) -> bytes:
    return json.dumps(
        {"protocol_version": 1, **values},
        separators=(",", ":"),
        sort_keys=True,
    ).encode() + b"\n"


class PythonV01ApiContractTests(unittest.IsolatedAsyncioTestCase):
    def test_standalone_distribution_metadata_and_single_source_copy(self) -> None:
        metadata = tomllib.loads(
            (PYTHON_CLIENT_ROOT / "pyproject.toml").read_text()
        )["project"]
        self.assertEqual(metadata["name"], "airpods-client")
        self.assertEqual(metadata["version"], "0.1.0")
        self.assertEqual(metadata["dependencies"], [])
        self.assertFalse((ROOT / "src/airpods_client").exists())
        self.assertTrue((_CLIENT_SRC / "airpods_client/client.py").is_file())

    def test_exact_root_exports_and_public_model_fields(self) -> None:
        self.assertEqual(airpods_client.__all__, PUBLIC_NAMES)
        self.assertTrue(
            all(getattr(airpods_client, name) is not None for name in PUBLIC_NAMES)
        )
        for private in (
            "_EventRoute",
            "_SubscriptionPhase",
            "MAX_FRAME_SIZE",
            "PROTOCOL_VERSION",
        ):
            self.assertNotIn(private, airpods_client.__all__)
            self.assertFalse(hasattr(airpods_client, private))

        self.assertEqual(
            [field.name for field in fields(Hello)],
            ["service", "experimental"],
        )
        self.assertEqual(
            [field.name for field in fields(Status)],
            ["state", "subscriber_count"],
        )
        self.assertEqual(
            [field.name for field in fields(HeartRateSample)],
            ["bpm", "source_side", "source_side_raw"],
        )
        self.assertTrue(issubclass(ConnectionFailed, AirPodsClientError))
        self.assertTrue(issubclass(ConnectionClosed, AirPodsClientError))
        self.assertTrue(issubclass(FrameTooLarge, AirPodsClientError))
        self.assertTrue(issubclass(InvalidMessage, AirPodsClientError))
        self.assertTrue(issubclass(ProtocolVersionError, AirPodsClientError))
        self.assertTrue(issubclass(DaemonError, AirPodsClientError))
        self.assertTrue(issubclass(SubscriptionActive, AirPodsClientError))
        self.assertTrue(issubclass(EventBufferFull, AirPodsClientError))
        self.assertTrue(issubclass(XdgRuntimeDirMissing, AirPodsClientError))

    async def test_frozen_async_usage_uses_only_root_exports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "hubd.sock"

            async def handle(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                try:
                    expected = ["hello", "ping", "status", "subscribe"]
                    for operation in expected:
                        request = json.loads(await reader.readline())
                        self.assertEqual(request["operation"], operation)
                        if operation == "hello":
                            reply = frame(
                                ok=True,
                                operation="hello",
                                service="airpods-hubd",
                                experimental=True,
                            )
                        elif operation == "ping":
                            reply = frame(ok=True, operation="ping", pong=True)
                        elif operation == "status":
                            reply = frame(
                                ok=True,
                                operation="status",
                                state="ready",
                                subscriber_count=0,
                            )
                        else:
                            reply = frame(
                                ok=True,
                                operation="subscribe",
                                stream="heart_rate",
                                subscribed=True,
                                already_subscribed=False,
                            ) + frame(
                                event="heart_rate",
                                bpm=169,
                                source_side="unknown",
                                source_side_raw=37,
                            )
                        writer.write(reply)
                        await writer.drain()
                    request = json.loads(await reader.readline())
                    self.assertEqual(request["operation"], "unsubscribe")
                    writer.write(
                        frame(
                            ok=True,
                            operation="unsubscribe",
                            stream="heart_rate",
                            subscribed=False,
                            already_unsubscribed=False,
                        )
                    )
                    await writer.drain()
                    await reader.read()
                finally:
                    writer.close()
                    await writer.wait_closed()

            handler_tasks: list[asyncio.Task[None]] = []

            def accepted(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
            ) -> None:
                handler_tasks.append(asyncio.create_task(handle(reader, writer)))

            server = await asyncio.start_unix_server(accepted, socket_path)
            try:
                async with await AirPodsClient.connect_to(socket_path) as client:
                    hello = await client.hello()
                    self.assertEqual(hello, Hello("airpods-hubd", True))
                    await client.ping()
                    self.assertEqual(
                        await client.status(),
                        Status(DaemonState.READY, 0),
                    )
                    async with await client.subscribe_heart_rate() as subscription:
                        self.assertIsInstance(subscription, HeartRateSubscription)
                        sample = await subscription.__anext__()
                        self.assertEqual(
                            sample,
                            HeartRateSample(169, SourceSide.UNKNOWN, 37),
                        )
            finally:
                server.close()
                await server.wait_closed()
                if handler_tasks:
                    results = await asyncio.gather(
                        *handler_tasks,
                        return_exceptions=True,
                    )
                    errors = [
                        result
                        for result in results
                        if isinstance(result, BaseException)
                    ]
                    if errors:
                        raise errors[0]


if __name__ == "__main__":
    unittest.main()
