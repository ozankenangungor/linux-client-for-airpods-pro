"""Real C consumer -> frozen Rust SDK -> real hubd, with a fake session only."""

from __future__ import annotations

import asyncio
from collections import Counter
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from typing import Any

from airpods_hr._hubd.server import AirPodsHubDaemon, DaemonState
from tests.test_rust_client_hubd_integration import (
    FakeSensorSession, FakeSessionFactory, ROOT, TEST_TIMEOUT, report,
)

HEADER = ROOT / "crates/airpods-client-c/include/airpods_client.h"
LIBDIR = ROOT / "target/debug"

# Reviewed application ABI, independent of the Rust implementation/header parser.
SYMBOLS = {
    "airpods_client_c_abi_version", "airpods_client_protocol_version",
    "airpods_client_connect", "airpods_client_connect_to", "airpods_client_ping",
    "airpods_client_hello", "airpods_client_status", "airpods_client_hr_subscribe",
    "airpods_client_hr_next", "airpods_client_hr_unsubscribe", "airpods_client_close",
    "airpods_client_free", "airpods_error_kind", "airpods_error_message",
    "airpods_error_daemon_code", "airpods_error_free", "airpods_hello_service",
    "airpods_hello_experimental", "airpods_hello_free", "airpods_status_state",
    "airpods_status_subscriber_count", "airpods_status_unknown_state", "airpods_status_free",
}


def command(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True,
                            timeout=120, check=False, **kwargs)
    if result.returncode:
        raise AssertionError(f"C SDK gate failed: {argv!r}\n{result.stderr[-4000:]}")
    return result


class ObservedHubDaemon(AirPodsHubDaemon):
    """Count real requests while delegating all protocol/session behavior."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.requests: Counter[str] = Counter()

    async def _dispatch(self, client: Any, request: dict[str, Any]) -> dict[str, Any]:
        self.requests[request["operation"]] += 1
        return await super()._dispatch(client, request)


class CBuildTests(unittest.TestCase):
    def test_header_c11(self) -> None:
        command(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-x", "c", "-fsyntax-only", os.fspath(HEADER)])

    def test_header_cpp17(self) -> None:
        command(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                 "-x", "c++", "-fsyntax-only", os.fspath(HEADER)])

    def test_exact_symbols_and_both_libraries(self) -> None:
        command(["cargo", "build", "-p", "airpods-client-c", "--locked"])
        self.assertTrue((LIBDIR / "libairpods_client_c.a").is_file())
        output = command(["nm", "-D", "--defined-only",
                          os.fspath(LIBDIR / "libairpods_client_c.so")]).stdout
        found = {line.split()[-1] for line in output.splitlines()
                 if line.split()[-1].startswith("airpods_")}
        self.assertEqual(found, SYMBOLS)


class CClientHubdIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        command(["cargo", "build", "-p", "airpods-client-c", "--locked"])
        cls.build_dir = tempfile.TemporaryDirectory(prefix="airpods-c-probe-")
        cls.probe = Path(cls.build_dir.name) / "c-probe"
        try:
            command(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic",
                     "-I", os.fspath(HEADER.parent), os.fspath(ROOT / "tests/c_ffi_probe.c"),
                     "-L", os.fspath(LIBDIR), "-lairpods_client_c",
                     "-o", os.fspath(cls.probe)])
        except BaseException:
            cls.build_dir.cleanup()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build_dir.cleanup()

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="airpods-c-ipc-")
        self.socket = Path(self.temp.name) / "hubd.sock"
        self.session = FakeSensorSession()
        self.factory = FakeSessionFactory(self.session)
        self.daemon = ObservedHubDaemon(self.factory, self.socket)
        self.processes: list[asyncio.subprocess.Process] = []
        # Privacy/error tests do not start even a fake daemon.

    async def asyncTearDown(self) -> None:
        for process in self.processes:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        await self.daemon.shutdown()
        self.temp.cleanup()

    async def launch(self, mode: str, path: Path | None = None,
                     environment: dict[str, str] | None = None) -> asyncio.subprocess.Process:
        env = dict(os.environ if environment is None else environment)
        env["LD_LIBRARY_PATH"] = os.fspath(LIBDIR)
        process = await asyncio.create_subprocess_exec(
            os.fspath(self.probe), mode, os.fspath(path or self.socket), env=env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=4096,
        )
        self.processes.append(process)
        return process

    async def line(self, process: asyncio.subprocess.Process, expected: str) -> None:
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), TEST_TIMEOUT)
        self.assertEqual(line.decode().strip(), expected)

    async def finish(self, process: asyncio.subprocess.Process) -> str:
        stdout, stderr = await asyncio.wait_for(process.communicate(), TEST_TIMEOUT)
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        self.assertEqual(stderr, b"")
        self.assertNotIn(b"\x1b", stdout)
        self.assertNotIn(b"Traceback", stdout)
        self.assertNotIn(b"panicked", stdout)
        return stdout.decode()

    async def cleaned(self, unsubscribes: int) -> None:
        await asyncio.wait_for(self.session.stop_observed.wait(), TEST_TIMEOUT)
        self.assertEqual(self.daemon.subscriber_count, 0)
        self.assertEqual(self.daemon.state, DaemonState.READY)
        self.assertEqual(self.daemon.requests["subscribe"], 1)
        self.assertEqual(self.daemon.requests["unsubscribe"], unsubscribes)
        self.assertEqual(self.factory.calls, 1)
        self.assertEqual(self.session.open_calls, 1)
        self.assertEqual(self.session.start_calls, 1)
        self.assertEqual(self.session.stop_calls, 1)
        self.assertEqual(self.session.close_calls, 0)

    async def test_order_duplicates_unknown_and_timeout_preservation(self) -> None:
        await self.daemon.start()
        process = await self.launch("scenario")
        await self.line(process, "TIMEOUTS")
        self.session.inject(report(169, field_5=1, sequence=1))
        await self.line(process, "SAMPLE 169 1 0 0")
        await self.line(process, "FIRST")
        self.session.inject(report(88, field_5=2, sequence=2))
        self.session.inject(report(88, field_5=2, sequence=3))
        self.session.inject(report(74, field_5=37, sequence=4))
        for sample in ["SAMPLE 88 2 0 0", "SAMPLE 88 2 0 0", "SAMPLE 74 0 37 0"]:
            await self.line(process, sample)
        await self.line(process, "DONE")
        self.assertEqual(await self.finish(process), "")
        await self.cleaned(1)

    async def test_close_active_confirms_unsubscribe(self) -> None:
        await self.daemon.start()
        process = await self.launch("close-active")
        await self.line(process, "READY")
        assert process.stdin is not None
        process.stdin.write(b"\n")
        await process.stdin.drain()
        await self.line(process, "DONE")
        self.assertEqual(await self.finish(process), "")
        await self.cleaned(1)

    async def test_free_active_disconnect_cleanup(self) -> None:
        await self.daemon.start()
        process = await self.launch("free-active")
        await self.line(process, "READY")
        assert process.stdin is not None
        process.stdin.write(b"\n")
        await process.stdin.drain()
        await self.line(process, "DONE")
        self.assertEqual(await self.finish(process), "")
        await self.cleaned(0)

    async def test_default_connect_masks_private_path(self) -> None:
        private = Path(self.temp.name) / "distinctive-private-owner-runtime-11-4"
        private.mkdir()
        env = dict(os.environ, XDG_RUNTIME_DIR=os.fspath(private))
        process = await self.launch("default-error", environment=env)
        message = await self.finish(process)
        self.assertIn("could not connect to default airpods-hubd socket:", message)
        self.assertNotIn(os.fspath(private), message)
        self.assertNotIn("distinctive-private-owner", message)
        self.assertNotIn("airpods-hubd.sock", message)
        self.assertEqual(self.factory.calls, 0)

    async def test_explicit_missing_path_retained(self) -> None:
        path = Path(self.temp.name) / "distinctive-explicit-missing.sock"
        process = await self.launch("explicit-error", path)
        message = await self.finish(process)
        self.assertIn(os.fspath(path), message)
        self.assertEqual(self.factory.calls, 0)

    async def test_missing_runtime_is_typed(self) -> None:
        env = dict(os.environ)
        env.pop("XDG_RUNTIME_DIR", None)
        process = await self.launch("xdg-error", environment=env)
        self.assertIn("XDG_RUNTIME_DIR is required", await self.finish(process))
        self.assertEqual(self.factory.calls, 0)


if __name__ == "__main__":
    unittest.main()
