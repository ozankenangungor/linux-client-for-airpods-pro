"""One Rust client and one Python client sharing the real fake-backed hubd."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from typing import Any

from airpods_client import AirPodsClient, DaemonState, SourceSide
from airpods_hr._hubd.server import DaemonState as HubDaemonState
from tests.test_rust_client_hubd_integration import (
    BarrierHubDaemon,
    FakeSensorSession,
    FakeSessionFactory,
    TEST_TIMEOUT,
    build_probe,
    report,
)


class MixedClientHubdIntegrationTests(unittest.IsolatedAsyncioTestCase):
    probe_binary: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.probe_binary = build_probe()

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temp.name) / "airpods-hubd.sock"
        self.session = FakeSensorSession()
        self.factory = FakeSessionFactory(self.session)
        self.daemon = BarrierHubDaemon(self.factory, self.socket_path)
        self.process: asyncio.subprocess.Process | None = None
        self.python_client: AirPodsClient | None = None
        await self.daemon.start()

    async def asyncTearDown(self) -> None:
        if self.python_client is not None:
            await self.python_client.close()
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2.0)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        await self.daemon.shutdown()
        self.temp.cleanup()

    async def read_phase(self, expected: str) -> dict[str, Any]:
        assert self.process is not None
        assert self.process.stdout is not None
        line = await asyncio.wait_for(
            self.process.stdout.readline(), TEST_TIMEOUT
        )
        if not line:
            assert self.process.stderr is not None
            stderr = await asyncio.wait_for(
                self.process.stderr.read(4096), TEST_TIMEOUT
            )
            self.fail(
                f"Rust probe exited before {expected}: "
                + stderr.decode("utf-8", errors="replace")
            )
        self.assertLessEqual(len(line), 4096)
        value = json.loads(line)
        self.assertEqual(value.get("phase"), expected)
        return value

    async def test_rust_and_python_share_one_real_daemon_session(self) -> None:
        self.python_client = await AirPodsClient.connect_to(self.socket_path)
        python_subscription = (
            await self.python_client.subscribe_heart_rate()
        )
        self.assertEqual(self.session.start_calls, 1)

        self.process = await asyncio.create_subprocess_exec(
            os.fspath(self.probe_binary),
            "mixed",
            os.fspath(self.socket_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4096,
        )
        await self.read_phase("rust_subscribed")
        await self.read_phase("shared_events_ready")
        self.assertEqual(self.daemon.subscriber_count, 2)
        self.assertEqual(self.session.start_calls, 1)

        self.session.inject(report(169, field_5=1, sequence=1))
        self.session.inject(report(88, field_5=2, sequence=2))
        python_samples = [
            await python_subscription.__anext__(),
            await python_subscription.__anext__(),
        ]
        rust_summary = await self.read_phase("rust_unsubscribed")
        self.assertEqual(rust_summary["bpm"], [169, 88])
        self.assertEqual([sample.bpm for sample in python_samples], [169, 88])
        self.assertEqual(
            [sample.source_side for sample in python_samples],
            [SourceSide.LEFT, SourceSide.RIGHT],
        )
        self.assertEqual(self.daemon.subscriber_count, 1)
        self.assertEqual(self.session.stop_calls, 0)

        self.session.inject(report(73, field_5=37, sequence=3))
        remaining = await python_subscription.__anext__()
        self.assertEqual(
            (remaining.bpm, remaining.source_side, remaining.source_side_raw),
            (73, SourceSide.UNKNOWN, 37),
        )
        await python_subscription.unsubscribe()
        self.assertEqual(self.daemon.subscriber_count, 0)
        self.assertEqual(self.session.stop_calls, 1)
        self.assertEqual(self.daemon.state, HubDaemonState.READY)

        assert self.process.stdin is not None
        self.process.stdin.write(b"go\n")
        await asyncio.wait_for(self.process.stdin.drain(), TEST_TIMEOUT)
        final = await self.read_phase("pass")
        self.assertEqual(final["scenario"], "mixed")
        await asyncio.wait_for(self.process.wait(), TEST_TIMEOUT)
        assert self.process.stderr is not None
        stderr = await asyncio.wait_for(
            self.process.stderr.read(4096), TEST_TIMEOUT
        )
        self.assertEqual(
            self.process.returncode,
            0,
            stderr.decode("utf-8", errors="replace"),
        )

        status = await self.python_client.status()
        self.assertEqual(status.state, DaemonState.READY)
        self.assertEqual(status.subscriber_count, 0)
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(self.session.open_calls, 1)
        self.assertEqual(self.session.start_calls, 1)
        self.assertEqual(self.session.stop_calls, 1)
        self.assertEqual(self.session.close_calls, 0)
        await self.daemon.shutdown()
        self.assertEqual(self.session.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
