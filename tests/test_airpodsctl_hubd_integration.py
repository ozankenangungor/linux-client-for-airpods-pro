"""Exercise the airpodsctl binary against the real hub daemon and a fake sensor."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from typing import Any

from airpods_hr._hubd.server import AirPodsHubDaemon, DaemonState
from tests.test_rust_client_hubd_integration import (
    FakeSensorSession,
    FakeSessionFactory,
    report,
)


ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 10.0
_BINARY: Path | None = None


def binary() -> Path:
    global _BINARY
    if _BINARY is None:
        built = subprocess.run(
            ["cargo", "build", "--locked", "-p", "airpodsctl"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            timeout=120,
        )
        if built.returncode:
            raise AssertionError(
                f"airpodsctl build failed: {built.stderr[-4000:].decode(errors='replace')}"
            )
        _BINARY = ROOT / "target/debug/airpodsctl"
        if not _BINARY.is_file():
            raise AssertionError("airpodsctl binary was not produced")
    return _BINARY


class CountingHubDaemon(AirPodsHubDaemon):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.subscribe_requests = 0
        self.unsubscribe_requests = 0

    async def _dispatch(self, client: Any, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("operation") == "subscribe":
            self.subscribe_requests += 1
        if request.get("operation") == "unsubscribe":
            self.unsubscribe_requests += 1
        return await super()._dispatch(client, request)


class AirpodsctlHubdIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.executable = binary()

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.socket = Path(self.temp.name) / "hubd.sock"
        self.sensor = FakeSensorSession()
        self.factory = FakeSessionFactory(self.sensor)
        self.daemon = CountingHubDaemon(self.factory, self.socket)
        self.processes: list[asyncio.subprocess.Process] = []
        await self.daemon.start()
        self.assertEqual(self.daemon.state, DaemonState.READY)

    async def asyncTearDown(self) -> None:
        for process in self.processes:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.wait(), 2)
        await self.daemon.shutdown()
        self.temp.cleanup()

    async def start_cli(self, *args: str) -> asyncio.subprocess.Process:
        process = await asyncio.create_subprocess_exec(
            os.fspath(self.executable),
            *args,
            "--socket",
            os.fspath(self.socket),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4096,
        )
        self.processes.append(process)
        return process

    async def finish(self, process: asyncio.subprocess.Process, code: int = 0) -> tuple[bytes, bytes]:
        stdout, stderr = await asyncio.wait_for(process.communicate(), TIMEOUT)
        self.assertLessEqual(len(stdout), 4096)
        self.assertLessEqual(len(stderr), 4096)
        self.assertEqual(process.returncode, code, stderr.decode(errors="replace"))
        return stdout, stderr

    async def wait_subscribers(self, expected: int) -> None:
        async def wait() -> None:
            while self.daemon.subscriber_count != expected:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), TIMEOUT)

    def assert_one_open_session(self) -> None:
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(self.sensor.open_calls, 1)
        self.assertEqual(self.sensor.close_calls, 0)
        self.assertEqual(self.daemon.state, DaemonState.READY)

    async def test_hello_ping_status(self) -> None:
        hello, error = await self.finish(await self.start_cli("hello"))
        self.assertEqual(hello, b"service: airpods-hubd\nexperimental: true\n")
        self.assertEqual(error, b"")
        pong, _ = await self.finish(await self.start_cli("ping", "--json"))
        self.assertEqual(json.loads(pong), {"pong": True})
        status, _ = await self.finish(await self.start_cli("--json", "status"))
        self.assertEqual(json.loads(status), {"state": "ready", "subscriber_count": 0})
        self.assert_one_open_session()

    async def test_watch_count_preserves_samples_and_unsubscribes(self) -> None:
        process = await self.start_cli("watch", "--count", "4")
        await self.wait_subscribers(1)
        self.assertEqual(self.sensor.start_calls, 1)
        for sequence, (bpm, side) in enumerate(
            [(169, 1), (169, 1), (72, 2), (74, 37)], 1
        ):
            self.sensor.inject(report(bpm, field_5=side, sequence=sequence))
        stdout, stderr = await self.finish(process)
        self.assertEqual(stderr, b"")
        self.assertEqual(
            stdout.splitlines(),
            [b"169 bpm (left)", b"169 bpm (left)", b"72 bpm (right)", b"74 bpm (unknown(37))"],
        )
        await self.wait_subscribers(0)
        self.assertEqual(self.sensor.reports_returned, 4)
        self.assertEqual(self.sensor.start_calls, 1)
        self.assertEqual(self.sensor.stop_calls, 1)
        self.assertEqual(self.daemon.subscribe_requests, 1)
        self.assertEqual(self.daemon.unsubscribe_requests, 1)
        self.assert_one_open_session()

    async def test_json_watch_keeps_unknown_source_byte(self) -> None:
        process = await self.start_cli("--json", "watch", "--count", "2")
        await self.wait_subscribers(1)
        self.sensor.inject(report(169, field_5=1, sequence=1))
        self.sensor.inject(report(74, field_5=37, sequence=2))
        stdout, stderr = await self.finish(process)
        self.assertEqual(stderr, b"")
        lines = stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            [json.loads(line) for line in lines],
            [
                {"bpm": 169, "source_side": "left"},
                {"bpm": 74, "source_side": "unknown", "source_side_raw": 37},
            ],
        )
        self.assertTrue(all(b"\n" not in line and b" " not in line for line in lines))
        await self.wait_subscribers(0)
        self.assertEqual(self.sensor.stop_calls, 1)
        self.assertEqual(self.daemon.subscribe_requests, 1)
        self.assertEqual(self.daemon.unsubscribe_requests, 1)
        self.assert_one_open_session()

    async def test_active_ctrl_c_confirms_cleanup(self) -> None:
        process = await self.start_cli("watch")
        await self.wait_subscribers(1)
        self.sensor.inject(report(83, field_5=2, sequence=1))
        assert process.stdout is not None
        first = await asyncio.wait_for(process.stdout.readline(), TIMEOUT)
        self.assertEqual(first, b"83 bpm (right)\n")
        process.send_signal(signal.SIGINT)
        stdout, stderr = await self.finish(process, 130)
        self.assertEqual(stdout, b"")
        self.assertEqual(stderr, b"")
        await self.wait_subscribers(0)
        await asyncio.wait_for(self.sensor.stop_observed.wait(), TIMEOUT)
        self.assertEqual(self.sensor.stop_calls, 1)
        self.assertEqual(self.daemon.subscribe_requests, 1)
        self.assertEqual(self.daemon.unsubscribe_requests, 1)
        self.assert_one_open_session()

    async def test_absent_socket_exits_one_without_traceback(self) -> None:
        missing = Path(self.temp.name) / "missing.sock"
        process = await asyncio.create_subprocess_exec(
            os.fspath(self.executable),
            "status",
            "--socket",
            os.fspath(missing),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.processes.append(process)
        stdout, stderr = await self.finish(process, 1)
        self.assertEqual(stdout, b"")
        self.assertIn(b"could not connect", stderr)
        self.assertNotIn(b"panicked", stderr)

    async def test_help_and_version_need_no_daemon(self) -> None:
        for option, marker in [("--help", b"Usage:"), ("--version", b"0.1.0")]:
            process = await asyncio.create_subprocess_exec(
                os.fspath(self.executable),
                option,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self.processes.append(process)
            stdout, stderr = await self.finish(process)
            self.assertIn(marker, stdout)
            self.assertEqual(stderr, b"")


if __name__ == "__main__":
    unittest.main()
