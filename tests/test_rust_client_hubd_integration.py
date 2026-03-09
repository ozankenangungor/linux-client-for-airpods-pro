"""Rust client integration against the real Python hub daemon and a fake sensor."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import Any

from airpods_hr._hubd.server import AirPodsHubDaemon, DaemonState
from airpods_hr.heartrate import HeartRateReport


ROOT = Path(__file__).resolve().parents[1]
PROBE_SOURCE = (
    ROOT / "crates/airpods-client/examples/integration_probe.rs"
)
TEST_TIMEOUT = 10.0
_PROBE_BINARY: Path | None = None


def build_probe() -> Path:
    """Build the repository probe once per Python test process."""

    global _PROBE_BINARY
    if _PROBE_BINARY is not None:
        return _PROBE_BINARY
    build = subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "-p",
            "airpods-client",
            "--example",
            "integration_probe",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if build.returncode != 0:
        stderr = build.stderr.decode("utf-8", errors="replace")[-4000:]
        raise AssertionError(f"Rust integration probe build failed: {stderr}")
    binary = ROOT / "target/debug/examples/integration_probe"
    if not binary.is_file():
        raise AssertionError("Rust integration probe binary was not produced")
    _PROBE_BINARY = binary
    return binary


def report(bpm: int, *, field_5: int, sequence: int) -> HeartRateReport:
    return HeartRateReport(
        bpm=bpm,
        aux=20,
        sequence=sequence,
        field_5=field_5,
        timestamp_ticks=100 + sequence,
        flags=0x1000,
    )


class FakeSensorSession:
    def __init__(self) -> None:
        self.open_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self.reports_returned = 0
        self.reports: asyncio.Queue[HeartRateReport] = asyncio.Queue()
        self.stop_observed = asyncio.Event()

    async def open(self) -> None:
        self.open_calls += 1

    async def start(self) -> None:
        self.start_calls += 1

    async def receive_report(self) -> HeartRateReport:
        value = await self.reports.get()
        self.reports_returned += 1
        return value

    async def stop(self) -> None:
        self.stop_calls += 1
        self.stop_observed.set()

    async def close(self) -> None:
        self.close_calls += 1

    def inject(self, value: HeartRateReport) -> None:
        self.reports.put_nowait(value)


class FakeSessionFactory:
    def __init__(self, session: FakeSensorSession) -> None:
        self.session = session
        self.calls = 0

    def __call__(self) -> FakeSensorSession:
        self.calls += 1
        return self.session


class BarrierHubDaemon(AirPodsHubDaemon):
    """The real daemon with test-only scheduling barriers around real dispatch."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._block_next_status = False
        self.status_entered = asyncio.Event()
        self.status_release = asyncio.Event()
        self.heart_rate_enqueued = asyncio.Event()

    def arm_status_interleave(self) -> None:
        self._block_next_status = True
        self.status_entered.clear()
        self.status_release.clear()
        self.heart_rate_enqueued.clear()

    async def _dispatch(self, client: Any, request: dict[str, Any]) -> dict[str, Any]:
        if self._block_next_status and request.get("operation") == "status":
            self._block_next_status = False
            self.status_entered.set()
            await self.status_release.wait()
        return await super()._dispatch(client, request)

    def _enqueue(self, client: Any, message: dict[str, Any]) -> bool:
        accepted = super()._enqueue(client, message)
        if accepted and message.get("event") == "heart_rate":
            self.heart_rate_enqueued.set()
        return accepted


class RustClientHubdIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        self.processes: list[asyncio.subprocess.Process] = []
        await self.daemon.start()
        self.assertEqual(self.daemon.state, DaemonState.READY)
        self.assertTrue(self.socket_path.is_socket())

    async def asyncTearDown(self) -> None:
        for process in self.processes:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        await self.daemon.shutdown()
        self.temp.cleanup()

    async def start_probe(self, mode: str) -> asyncio.subprocess.Process:
        process = await asyncio.create_subprocess_exec(
            os.fspath(self.probe_binary),
            mode,
            os.fspath(self.socket_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=4096,
        )
        self.processes.append(process)
        return process

    async def read_phase(
        self, process: asyncio.subprocess.Process, expected: str
    ) -> dict[str, Any]:
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), TEST_TIMEOUT)
        if not line:
            assert process.stderr is not None
            stderr = await asyncio.wait_for(process.stderr.read(4096), TEST_TIMEOUT)
            self.fail(
                "probe exited before phase "
                f"{expected}: {stderr.decode('utf-8', errors='replace')}"
            )
        self.assertLessEqual(len(line), 4096)
        message = json.loads(line)
        self.assertIsInstance(message, dict)
        self.assertEqual(message.get("phase"), expected)
        return message

    async def send_go(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdin is not None
        process.stdin.write(b"go\n")
        await asyncio.wait_for(process.stdin.drain(), TEST_TIMEOUT)

    async def finish_probe(self, process: asyncio.subprocess.Process) -> None:
        await asyncio.wait_for(process.wait(), TEST_TIMEOUT)
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = await asyncio.wait_for(process.stdout.read(4096), TEST_TIMEOUT)
        stderr = await asyncio.wait_for(process.stderr.read(4096), TEST_TIMEOUT)
        self.assertEqual(stdout, b"")
        self.assertLessEqual(len(stderr), 4096)
        self.assertEqual(
            process.returncode,
            0,
            stderr.decode("utf-8", errors="replace"),
        )

    async def shutdown_and_assert_single_session(self) -> None:
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(self.session.open_calls, 1)
        self.assertEqual(self.session.close_calls, 0)
        await self.daemon.shutdown()
        self.assertEqual(self.session.close_calls, 1)
        self.assertEqual(self.daemon.state, DaemonState.STOPPED)
        self.assertFalse(self.socket_path.exists())

    async def test_basic_protocol_events_and_interleaving(self) -> None:
        process = await self.start_probe("basic")
        await self.read_phase(process, "subscribed")
        self.assertEqual(self.session.start_calls, 1)
        self.assertEqual(self.daemon.subscriber_count, 1)

        await self.read_phase(process, "interleave_ready")
        self.daemon.arm_status_interleave()
        await self.send_go(process)
        await asyncio.wait_for(self.daemon.status_entered.wait(), TEST_TIMEOUT)
        self.session.inject(report(169, field_5=1, sequence=1))
        await asyncio.wait_for(
            self.daemon.heart_rate_enqueued.wait(), TEST_TIMEOUT
        )
        self.daemon.status_release.set()

        await self.read_phase(process, "remaining_events_ready")
        self.session.inject(report(88, field_5=2, sequence=2))
        self.session.inject(report(88, field_5=2, sequence=3))
        self.session.inject(report(73, field_5=37, sequence=4))
        summary = await self.read_phase(process, "pass")
        self.assertEqual(summary["scenario"], "basic")
        self.assertEqual(summary["event_count"], 4)
        self.assertEqual(summary["bpm"], [169, 88, 88, 73])
        await self.finish_probe(process)

        self.assertEqual(self.session.reports_returned, 4)
        self.assertEqual(self.session.start_calls, 1)
        self.assertEqual(self.session.stop_calls, 1)
        self.assertEqual(self.daemon.subscriber_count, 0)
        self.assertEqual(self.daemon.state, DaemonState.READY)
        await self.shutdown_and_assert_single_session()

    async def test_two_rust_clients_share_one_real_daemon_reader(self) -> None:
        process = await self.start_probe("two-clients")
        await self.read_phase(process, "client_a_subscribed")
        await self.read_phase(process, "both_subscribed")
        self.assertEqual(self.session.start_calls, 1)
        self.assertEqual(self.daemon.subscriber_count, 2)

        await self.read_phase(process, "shared_events_ready")
        self.session.inject(report(101, field_5=1, sequence=1))
        self.session.inject(report(102, field_5=2, sequence=2))
        await self.read_phase(process, "client_a_unsubscribed")
        self.assertEqual(self.daemon.subscriber_count, 1)
        self.assertEqual(self.session.stop_calls, 0)

        await self.read_phase(process, "client_b_event_ready")
        self.session.inject(report(83, field_5=37, sequence=3))
        summary = await self.read_phase(process, "pass")
        self.assertEqual(summary["scenario"], "two-clients")
        await self.finish_probe(process)

        self.assertEqual(self.session.reports_returned, 3)
        self.assertEqual(self.session.start_calls, 1)
        self.assertEqual(self.session.stop_calls, 1)
        self.assertEqual(self.daemon.subscriber_count, 0)
        self.assertEqual(self.daemon.state, DaemonState.READY)
        await self.shutdown_and_assert_single_session()

    async def test_drop_cleanup_allows_same_connection_resubscribe(self) -> None:
        process = await self.start_probe("drop-resubscribe")
        await self.read_phase(process, "first_subscribed")
        self.assertEqual(self.session.start_calls, 1)
        self.assertEqual(self.session.stop_calls, 0)
        await self.send_go(process)

        await self.read_phase(process, "replacement_subscribed")
        self.assertEqual(self.session.start_calls, 2)
        self.assertEqual(self.session.stop_calls, 1)
        self.assertEqual(self.daemon.subscriber_count, 1)
        await self.read_phase(process, "replacement_event_ready")
        self.session.inject(report(90, field_5=1, sequence=1))
        summary = await self.read_phase(process, "pass")
        self.assertEqual(summary["scenario"], "drop-resubscribe")
        await self.finish_probe(process)

        self.assertEqual(self.session.start_calls, 2)
        self.assertEqual(self.session.stop_calls, 2)
        self.assertEqual(self.daemon.subscriber_count, 0)
        self.assertEqual(self.daemon.state, DaemonState.READY)
        await self.shutdown_and_assert_single_session()

    async def test_active_rust_client_disconnect_stops_but_keeps_session_open(
        self,
    ) -> None:
        process = await self.start_probe("disconnect")
        await self.read_phase(process, "subscribed")
        self.assertEqual(self.session.start_calls, 1)
        await asyncio.wait_for(self.session.stop_observed.wait(), TEST_TIMEOUT)
        summary = await self.read_phase(process, "pass")
        self.assertEqual(summary["scenario"], "disconnect")
        await self.finish_probe(process)

        self.assertEqual(self.session.stop_calls, 1)
        self.assertEqual(self.session.close_calls, 0)
        self.assertEqual(self.daemon.subscriber_count, 0)
        self.assertEqual(self.daemon.state, DaemonState.READY)
        await self.shutdown_and_assert_single_session()


class IntegrationHarnessSafetyTests(unittest.TestCase):
    def test_probe_uses_only_public_client_api_boundary(self) -> None:
        source = PROBE_SOURCE.read_text()
        self.assertIn("use airpods_client::", source)
        self.assertNotIn("airpods_client::private", source)
        self.assertNotIn("Command::new", source)
        self.assertNotIn("system" + "ctl", source)

    def test_harness_has_no_production_or_bluetooth_path(self) -> None:
        source = Path(__file__).read_text()
        for forbidden in (
            "Production" + "HeartRateSession",
            "create_" + "production_session",
            "AF_" + "BLUETOOTH",
            "BTPROTO_" + "L2CAP",
            "system" + "ctl",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
