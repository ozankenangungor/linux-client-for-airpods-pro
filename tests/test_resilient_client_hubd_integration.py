"""Opt-in Rust reconnect against real hub daemon instances and fake sensors."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest

from airpods_hr._hubd.server import DaemonState
from tests.test_airpodsctl_hubd_integration import CountingHubDaemon, binary
from tests.test_rust_client_hubd_integration import FakeSensorSession, FakeSessionFactory, report

TIMEOUT = 10.0


class ResilientHubdIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.executable = binary()

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.socket = Path(self.temp.name) / "hubd.sock"
        self.sensor_a = FakeSensorSession()
        self.factory_a = FakeSessionFactory(self.sensor_a)
        self.daemon_a = CountingHubDaemon(self.factory_a, self.socket)
        self.sensor_b = FakeSensorSession()
        self.factory_b = FakeSessionFactory(self.sensor_b)
        self.daemon_b = CountingHubDaemon(self.factory_b, self.socket)
        self.process: asyncio.subprocess.Process | None = None
        await self.daemon_a.start()

    async def asyncTearDown(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), 2)
        await self.daemon_b.shutdown()
        await self.daemon_a.shutdown()
        self.temp.cleanup()

    async def start_watch(self, *args: str) -> asyncio.subprocess.Process:
        self.process = await asyncio.create_subprocess_exec(
            os.fspath(self.executable), "--json", "watch", "--reconnect", *args,
            "--socket", os.fspath(self.socket),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        return self.process

    async def line(self, process: asyncio.subprocess.Process) -> dict[str, object]:
        assert process.stdout is not None
        data = await asyncio.wait_for(process.stdout.readline(), TIMEOUT)
        self.assertTrue(data, "CLI ended before expected JSON event")
        return json.loads(data)

    async def subscribers(self, daemon: CountingHubDaemon, expected: int) -> None:
        async def wait() -> None:
            while daemon.subscriber_count != expected:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), TIMEOUT)

    async def test_restart_preserves_samples_and_confirms_final_unsubscribe(self) -> None:
        process = await self.start_watch("--count", "4")
        await self.subscribers(self.daemon_a, 1)
        self.assertEqual(self.daemon_a.subscribe_requests, 1)
        self.sensor_a.inject(report(169, field_5=1, sequence=1))
        self.assertEqual(await self.line(process), {"bpm":169,"source_side":"left"})
        await self.daemon_a.shutdown()
        self.assertIsNone(process.returncode)
        self.assertEqual(await self.line(process), {"event":"reconnecting","attempt":1,"delay_ms":1000})
        await self.daemon_b.start()
        await self.subscribers(self.daemon_b, 1)
        self.assertEqual(self.daemon_b.subscribe_requests, 1)
        self.assertEqual(len(self.daemon_b._clients), 1)
        self.assertEqual(await self.line(process), {"event":"reconnected","attempts":1})
        for sequence, (bpm, side) in enumerate([(88,2),(88,2),(74,37)], 1):
            self.sensor_b.inject(report(bpm, field_5=side, sequence=sequence))
        self.assertEqual(await self.line(process), {"bpm":88,"source_side":"right"})
        self.assertEqual(await self.line(process), {"bpm":88,"source_side":"right"})
        self.assertEqual(await self.line(process), {"bpm":74,"source_side":"unknown","source_side_raw":37})
        await asyncio.wait_for(process.wait(), TIMEOUT)
        self.assertEqual(process.returncode, 0)
        await self.subscribers(self.daemon_b, 0)
        self.assertEqual(self.daemon_b.unsubscribe_requests, 1)
        self.assertEqual(self.daemon_b.state, DaemonState.READY)
        self.assertEqual(self.sensor_b.stop_calls, 1)
        self.assertEqual(self.sensor_b.close_calls, 0)
        self.assertEqual(self.factory_a.calls, 1)
        self.assertEqual(self.factory_b.calls, 1)

    async def test_sigint_during_backoff_prevents_reconnect(self) -> None:
        process = await self.start_watch()
        await self.subscribers(self.daemon_a, 1)
        await self.daemon_a.shutdown()
        self.assertEqual(await self.line(process), {"event":"reconnecting","attempt":1,"delay_ms":1000})
        process.send_signal(signal.SIGINT)
        await asyncio.wait_for(process.wait(), TIMEOUT)
        self.assertEqual(process.returncode, 130)
        await self.daemon_b.start()
        await asyncio.sleep(1.2)
        self.assertEqual(self.daemon_b.subscriber_count, 0)
        self.assertEqual(self.daemon_b.subscribe_requests, 0)
        self.assertEqual(len(self.daemon_b._clients), 0)
        self.assertEqual(self.factory_b.calls, 1)

    async def test_active_sigint_confirms_unsubscribe(self) -> None:
        process = await self.start_watch()
        await self.subscribers(self.daemon_a, 1)
        process.send_signal(signal.SIGINT)
        await asyncio.wait_for(process.wait(), TIMEOUT)
        self.assertEqual(process.returncode, 130)
        await self.subscribers(self.daemon_a, 0)
        self.assertEqual(self.daemon_a.unsubscribe_requests, 1)
        self.assertEqual(self.sensor_a.stop_calls, 1)

    async def test_human_lifecycle_uses_stderr_and_samples_use_stdout(self) -> None:
        process = await asyncio.create_subprocess_exec(
            os.fspath(self.executable), "watch", "--reconnect", "--count", "2",
            "--socket", os.fspath(self.socket),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self.process = process
        await self.subscribers(self.daemon_a, 1)
        self.sensor_a.inject(report(169, field_5=1, sequence=1))
        assert process.stdout is not None and process.stderr is not None
        self.assertEqual(await asyncio.wait_for(process.stdout.readline(), TIMEOUT), b"169 bpm (left)\n")
        await self.daemon_a.shutdown()
        self.assertEqual(await asyncio.wait_for(process.stderr.readline(), TIMEOUT), b"reconnecting to airpods-hubd: attempt 1 in 1s\n")
        await self.daemon_b.start()
        await self.subscribers(self.daemon_b, 1)
        self.assertEqual(await asyncio.wait_for(process.stderr.readline(), TIMEOUT), b"reconnected to airpods-hubd after 1 attempt\n")
        self.sensor_b.inject(report(74, field_5=37, sequence=1))
        self.assertEqual(await asyncio.wait_for(process.stdout.readline(), TIMEOUT), b"74 bpm (unknown(37))\n")
        await asyncio.wait_for(process.wait(), TIMEOUT)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(self.daemon_b.unsubscribe_requests, 1)
