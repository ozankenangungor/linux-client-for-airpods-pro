"""PTY checks for the Rust top dashboard against real daemons and fake sensors."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
from pathlib import Path
import pty
import re
import signal
import struct
import subprocess
import tempfile
import termios
import unittest

from tests.test_airpodsctl_hubd_integration import CountingHubDaemon
from tests.test_rust_client_hubd_integration import FakeSensorSession, FakeSessionFactory, report

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 10.0
_BINARY: Path | None = None


def binary() -> Path:
    global _BINARY
    if _BINARY is None:
        built = subprocess.run(
            ["cargo", "build", "--locked", "-p", "airpodsctl"],
            cwd=ROOT, capture_output=True, check=False, timeout=120,
        )
        if built.returncode:
            raise AssertionError(built.stderr[-4000:].decode(errors="replace"))
        _BINARY = ROOT / "target/debug/airpodsctl"
        if not _BINARY.is_file():
            raise AssertionError("airpodsctl binary was not produced")
    return _BINARY


class AirpodsctlTopIntegrationTests(unittest.IsolatedAsyncioTestCase):
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
        self.master: int | None = None
        self.output = bytearray()
        self.baseline: list[object] | None = None
        await self.daemon_a.start()

    async def asyncTearDown(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), 2)
        if self.master is not None:
            os.close(self.master)
        await self.daemon_b.shutdown()
        await self.daemon_a.shutdown()
        self.temp.cleanup()

    async def start_top(self) -> asyncio.subprocess.Process:
        master, slave = pty.openpty()
        self.master = master
        os.set_blocking(master, False)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        self.baseline = termios.tcgetattr(master)
        try:
            self.process = await asyncio.create_subprocess_exec(
                "setsid", "--ctty", os.fspath(self.executable),
                "top", "--socket", os.fspath(self.socket),
                stdin=slave, stdout=slave, stderr=asyncio.subprocess.PIPE,
            )
        finally:
            os.close(slave)
        return self.process

    async def wait_output(self, marker: bytes, *, after: int = 0) -> None:
        assert self.master is not None
        end = asyncio.get_running_loop().time() + TIMEOUT
        while True:
            if marker.startswith(b"\x1b"):
                found = marker in self.output[after:]
            else:
                plain = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", self.output[after:])
                found = re.sub(rb"\s+", b"", marker) in re.sub(rb"\s+", b"", plain)
            if found:
                return
            if asyncio.get_running_loop().time() >= end:
                self.fail(f"PTY did not render {marker!r}; tail={self.output[-500:]!r}")
            try:
                chunk = os.read(self.master, 16384)
            except BlockingIOError:
                chunk = b""
            except OSError as error:
                if error.errno == errno.EIO:
                    chunk = b""
                else:
                    raise
            if chunk:
                self.output.extend(chunk)
                self.assertLess(len(self.output), 256_000)
            else:
                await asyncio.sleep(0.01)

    async def subscribers(self, daemon: CountingHubDaemon, expected: int) -> None:
        async def wait() -> None:
            while daemon.subscriber_count != expected:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), TIMEOUT)

    async def finish(self, expected: int) -> None:
        assert self.process is not None and self.master is not None
        end = asyncio.get_running_loop().time() + TIMEOUT
        while self.process.returncode is None and asyncio.get_running_loop().time() < end:
            try:
                chunk = os.read(self.master, 16384)
            except BlockingIOError:
                chunk = b""
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
                chunk = b""
            self.output.extend(chunk)
            await asyncio.sleep(0.01)
        if self.process.returncode is None:
            self.fail(f"top stayed alive after input; tail={self.output[-300:]!r}")
        await asyncio.wait_for(self.process.wait(), 1)
        self.assertEqual(self.process.returncode, expected)
        await self.wait_output(b"\x1b[?1049l")
        self.assertEqual(termios.tcgetattr(self.master), self.baseline)
        assert self.process.stderr is not None
        self.assertEqual(await self.process.stderr.read(), b"")

    async def test_sample_and_q_restore_terminal_and_unsubscribe(self) -> None:
        await self.start_top()
        await self.subscribers(self.daemon_a, 1)
        self.sensor_a.inject(report(169, field_5=1, sequence=1))
        await self.wait_output(b"169 BPM (left)")
        assert self.master is not None
        os.write(self.master, b"q")
        await self.finish(0)
        await self.subscribers(self.daemon_a, 0)
        self.assertEqual(self.daemon_a.unsubscribe_requests, 1)
        self.assertEqual(self.sensor_a.stop_calls, 1)

    async def test_raw_ctrl_c_restores_terminal_and_unsubscribes(self) -> None:
        await self.start_top()
        await self.subscribers(self.daemon_a, 1)
        self.sensor_a.inject(report(72, field_5=2, sequence=1))
        await self.wait_output(b"72 BPM (right)")
        assert self.master is not None
        os.write(self.master, b"\x03")
        await self.finish(130)
        await self.subscribers(self.daemon_a, 0)
        self.assertEqual(self.daemon_a.unsubscribe_requests, 1)

    async def test_escape_key_quits_cleanly(self) -> None:
        await self.start_top()
        await self.subscribers(self.daemon_a, 1)
        self.sensor_a.inject(report(72, field_5=2, sequence=1))
        await self.wait_output(b"72 BPM (right)")
        assert self.master is not None
        os.write(self.master, b"\x1b")
        await self.finish(0)
        self.assertEqual(self.daemon_a.unsubscribe_requests, 1)

    async def test_restart_uses_resilient_stream_and_preserves_samples(self) -> None:
        await self.start_top()
        await self.subscribers(self.daemon_a, 1)
        self.sensor_a.inject(report(169, field_5=1, sequence=1))
        await self.wait_output(b"169 BPM (left)")
        await self.daemon_a.shutdown()
        await self.wait_output(b"reconnecting attempt 1")
        await self.daemon_b.start()
        await self.subscribers(self.daemon_b, 1)
        # Ratatui writes only changed cells after the reconnect notice.
        await self.wait_output(b"er 1 attempt")
        for sequence, (bpm, side) in enumerate([(88, 2), (88, 2), (74, 37)], 1):
            self.sensor_b.inject(report(bpm, field_5=side, sequence=sequence))
        await self.wait_output(b"74 BPM (unknown(37))")
        self.assertEqual(self.sensor_b.reports_returned, 3)
        assert self.master is not None
        os.write(self.master, b"q")
        await self.finish(0)
        await self.subscribers(self.daemon_b, 0)
        self.assertEqual(self.daemon_b.subscribe_requests, 1)
        self.assertEqual(self.daemon_b.unsubscribe_requests, 1)
        self.assertEqual(self.sensor_b.reports_returned, 3)
        self.assertEqual(self.factory_a.calls, 1)
        self.assertEqual(self.factory_b.calls, 1)

    async def test_resize_redraws_without_resubscription(self) -> None:
        process = await self.start_top()
        await self.subscribers(self.daemon_a, 1)
        self.sensor_a.inject(report(71, field_5=2, sequence=1))
        await self.wait_output(b"71 BPM (right)")
        assert self.master is not None
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 6, 30, 0, 0))
        await self.wait_output(b"terminal too small")
        self.assertIsNone(process.returncode)
        self.assertEqual(self.daemon_a.subscribe_requests, 1)
        self.assertEqual(self.sensor_a.start_calls, 1)
        before_resize = len(self.output)
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        await self.wait_output(b"\x1b[24;1Hq", after=before_resize)
        self.sensor_a.inject(report(72, field_5=2, sequence=2))
        await self.wait_output(b"72 BPM (right)")
        os.write(self.master, b"q")
        await self.finish(0)
        self.assertEqual(self.daemon_a.subscribe_requests, 1)

    async def test_sigterm_restores_terminal_and_unsubscribes(self) -> None:
        process = await self.start_top()
        await self.subscribers(self.daemon_a, 1)
        self.sensor_a.inject(report(72, field_5=2, sequence=1))
        await self.wait_output(b"72 BPM (right)")
        process.send_signal(signal.SIGTERM)
        await self.finish(143)
        await self.subscribers(self.daemon_a, 0)
        self.assertEqual(self.daemon_a.unsubscribe_requests, 1)

    async def test_sigint_restores_terminal_and_unsubscribes(self) -> None:
        process = await self.start_top()
        await self.subscribers(self.daemon_a, 1)
        self.sensor_a.inject(report(72, field_5=2, sequence=1))
        await self.wait_output(b"72 BPM (right)")
        process.send_signal(signal.SIGINT)
        await self.finish(130)
        await self.subscribers(self.daemon_a, 0)
        self.assertEqual(self.daemon_a.unsubscribe_requests, 1)

    async def test_json_and_non_tty_fail_before_connect_or_escape_output(self) -> None:
        for args, expected in [
            (["--json", "top"], b"--json cannot be used with top"),
            (["top", "--json"], b"--json cannot be used with top"),
            (["top"], b"top requires terminal stdin and stdout"),
        ]:
            process = await asyncio.create_subprocess_exec(
                os.fspath(self.executable), *args, "--socket", os.fspath(self.socket),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), TIMEOUT)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(stdout, b"")
            self.assertIn(expected, stderr)
            self.assertNotIn(b"\x1b", stderr)
            self.assertEqual(self.daemon_a.subscribe_requests, 0)


if __name__ == "__main__":
    unittest.main()
