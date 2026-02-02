"""Hardware-independent tests for the private production hubd composition."""

from __future__ import annotations

import ast
import asyncio
import errno
import fcntl
import hashlib
import json
import os
import socket
import stat
import tempfile
import tomllib
import unittest
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from airpods_hr._hubd import production as hubd_production
from airpods_hr._hubd.production import (
    DEFAULT_DAEMON_OPERATION_TIMEOUT,
    ProductionHubConfig,
    ProductionSessionFactory,
    create_production_hub,
    minimum_daemon_operation_timeout,
)
from airpods_hr._hubd.protocol import PROTOCOL_VERSION
from airpods_hr._hubd.server import DaemonState
from airpods_hr.heartrate import HeartRateReport
from airpods_hr.production_session import (
    DEFAULT_REPORT_TIMEOUT,
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    ProductionSessionCounters,
)
from tools import probe_hubd_production
from tools.probe_hubd_production import (
    CLIENT_TIMEOUT_MARGIN,
    DEFAULT_CLIENT_TIMEOUT,
    ProbeClient,
    ProbeFailure,
    build_parser,
    main,
    minimum_client_timeout,
    run_probe,
)


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SESSION_SHA256 = (
    "f4141c6372c9bda65b4aca1b09c40e2f024ec8fe1b75964e8cc5f26c2156371e"
)
PACKAGE_INIT_SHA256 = (
    "b50576f701568dd5d63190568c47427d6d2b65c02596a1608dbdb87f3afea35f"
)


def report(index: int) -> HeartRateReport:
    return HeartRateReport(
        bpm=169 if index == 0 else 80 + index,
        aux=20,
        sequence=index,
        field_5=1 if index % 2 == 0 else 2,
        timestamp_ticks=100 + index,
        flags=0x1000,
    )


class FakeProductionSession:
    def __init__(self, *, open_error: BaseException | None = None) -> None:
        self.open_error = open_error
        self.open_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self.reports_received = 0
        self.reports: asyncio.Queue[HeartRateReport] = asyncio.Queue()

    @property
    def counters(self) -> ProductionSessionCounters:
        return ProductionSessionCounters(
            transport_opens=self.open_calls,
            descriptor_handshakes=self.open_calls,
            hr_activations=self.start_calls,
            hr_stops=self.stop_calls,
            reports_received=self.reports_received,
        )

    async def open(self) -> None:
        self.open_calls += 1
        if self.open_error is not None:
            raise self.open_error

    async def start(self) -> None:
        self.start_calls += 1

    async def receive_report(self) -> HeartRateReport:
        value = await self.reports.get()
        self.reports_received += 1
        return value

    async def stop(self) -> None:
        self.stop_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


class FakeBuilder:
    def __init__(self, session: FakeProductionSession) -> None:
        self.session = session
        self.calls = 0
        self.kwargs: dict[str, Any] = {}

    def __call__(self, **kwargs: Any) -> FakeProductionSession:
        self.calls += 1
        self.kwargs = kwargs
        return self.session


def process_lock_is_held(lock_path: Path) -> bool:
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return True
            raise
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


class ProductionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_real_clients_reuse_one_production_style_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "hubd-production.sock"
            lock_path = socket_path.with_suffix(".lock")
            session = FakeProductionSession()
            builder = FakeBuilder(session)
            lock_during_restart: list[bool] = []

            async def inject_cycle(cycle: int, active: Any) -> None:
                self.assertIs(active, session)
                self.assertTrue(process_lock_is_held(lock_path))
                for index in range(3):
                    session.reports.put_nowait(report(index))

            async def restart_wait(delay: float) -> None:
                self.assertEqual(delay, 0)
                lock_during_restart.append(process_lock_is_held(lock_path))

            output: list[str] = []
            status = await run_probe(
                execute=True,
                socket_path=socket_path,
                sample_target=2,
                restart_delay=0,
                client_timeout=20,
                output=output.append,
                session_builder=builder,
                sleep=restart_wait,
                cycle_ready=inject_cycle,
            )

            self.assertEqual(status, 0)
            self.assertEqual(builder.calls, 1)
            self.assertEqual(session.open_calls, 1)
            self.assertEqual(session.start_calls, 2)
            self.assertEqual(session.stop_calls, 2)
            self.assertEqual(session.close_calls, 1)
            self.assertGreaterEqual(session.reports_received, 4)
            self.assertEqual(lock_during_restart, [True])
            self.assertFalse(process_lock_is_held(lock_path))
            self.assertFalse(socket_path.exists())
            self.assertTrue(lock_path.exists())
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            self.assertIn("factory_calls=1", output)
            self.assertIn("client_event_counts=2,2,2", output)
            self.assertIn("HUBD PRODUCTION PROBE PASS", output)
            self.assertTrue(any("bpm=169" in line for line in output))

    async def test_failure_does_not_retry_or_construct_another_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "hubd-production.sock"
            session = FakeProductionSession(open_error=RuntimeError("open failed"))
            builder = FakeBuilder(session)
            output: list[str] = []
            status = await run_probe(
                execute=True,
                socket_path=socket_path,
                restart_delay=0,
                client_timeout=20,
                output=output.append,
                session_builder=builder,
            )
            self.assertEqual(status, 1)
            self.assertEqual(builder.calls, 1)
            self.assertEqual(session.open_calls, 1)
            self.assertEqual(session.close_calls, 1)
            self.assertFalse(socket_path.exists())
            self.assertIn("factory_calls=1", output)
            self.assertIn("  transport_opens=1", output)
            self.assertTrue(any("PROBE FAIL category=" in line for line in output))

    async def test_keyboard_interrupt_runs_bounded_owned_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "hubd-production.sock"
            session = FakeProductionSession()
            builder = FakeBuilder(session)

            async def interrupt(_cycle: int, _session: Any) -> None:
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                await run_probe(
                    execute=True,
                    socket_path=socket_path,
                    sample_target=1,
                    restart_delay=0,
                    client_timeout=20,
                    output=lambda _line: None,
                    session_builder=builder,
                    cycle_ready=interrupt,
                )
            self.assertEqual(builder.calls, 1)
            self.assertEqual(session.open_calls, 1)
            self.assertEqual(session.start_calls, 1)
            self.assertEqual(session.stop_calls, 1)
            self.assertEqual(session.close_calls, 1)
            self.assertFalse(socket_path.exists())
            self.assertFalse(process_lock_is_held(socket_path.with_suffix(".lock")))

    async def test_direct_composition_constructs_and_opens_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "hubd-production.sock"
            session = FakeProductionSession()
            builder = FakeBuilder(session)
            hub = create_production_hub(
                socket_path,
                config=ProductionHubConfig(),
                output=lambda _line: None,
                builder=builder,
            )
            await hub.daemon.start()
            self.assertEqual(hub.daemon.state, DaemonState.READY)
            self.assertIs(hub.daemon.session, session)
            self.assertIs(hub.factory.session, session)
            self.assertEqual(hub.factory.calls, 1)
            self.assertEqual(builder.calls, 1)
            self.assertEqual(session.open_calls, 1)
            self.assertTrue(process_lock_is_held(socket_path.with_suffix(".lock")))
            await hub.daemon.shutdown()
            self.assertEqual(session.close_calls, 1)


class ProbeClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_response_interleaving_preserves_bpm_169(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "client.sock"

            async def handler(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                await reader.readline()
                event = {
                    "protocol_version": PROTOCOL_VERSION,
                    "event": "heart_rate",
                    "bpm": 169,
                    "source_side": "left",
                }
                response = {
                    "protocol_version": PROTOCOL_VERSION,
                    "ok": True,
                    "operation": "subscribe",
                }
                writer.write(json.dumps(event).encode() + b"\n")
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handler, path=path)
            client = await ProbeClient.connect(path, timeout=1)
            try:
                reply = await client.request("subscribe", stream="heart_rate")
                self.assertTrue(reply["ok"])
                events = await client.heart_rate_events(1)
                self.assertEqual(events[0]["bpm"], 169)
            finally:
                await client.close()
                server.close()
                await server.wait_closed()

    async def test_read_timeout_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeout.sock"
            release = asyncio.Event()

            async def handler(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                await reader.readline()
                await release.wait()
                writer.close()
                await writer.wait_closed()

            server = await asyncio.start_unix_server(handler, path=path)
            client = await ProbeClient.connect(path, timeout=0.02)
            try:
                with self.assertRaisesRegex(ProbeFailure, "ipc_read_timeout"):
                    await client.request("ping")
            finally:
                release.set()
                await client.close()
                server.close()
                await server.wait_closed()


class ProductionFactoryTests(unittest.TestCase):
    def test_factory_delegates_to_existing_production_builder_once(self) -> None:
        config = ProductionHubConfig()
        session = Mock()
        with patch.object(
            hubd_production,
            "create_production_session",
            return_value=session,
        ) as builder:
            factory = ProductionSessionFactory(
                config, output=lambda _line: None
            )
            self.assertIs(factory(), session)
            with self.assertRaisesRegex(RuntimeError, "single-use"):
                factory()
        builder.assert_called_once_with(
            descriptor_timeout=30.0,
            dbus_timeout=5.0,
            connect_timeout=10.0,
            handshake_timeout=5.0,
            start_timeout=15.0,
            stop_timeout=5.0,
            output=unittest.mock.ANY,
        )
        self.assertEqual(factory.calls, 1)
        self.assertIs(factory.session, session)

    def test_outer_timeout_covers_complete_production_windows(self) -> None:
        minimum = minimum_daemon_operation_timeout(
            descriptor_timeout=30,
            dbus_timeout=5,
            connect_timeout=10,
            handshake_timeout=5,
            start_timeout=15,
            stop_timeout=5,
        )
        self.assertEqual(minimum, 130)
        self.assertEqual(DEFAULT_DAEMON_OPERATION_TIMEOUT, 150)
        with self.assertRaisesRegex(ValueError, "production open window"):
            ProductionHubConfig(daemon_operation_timeout=129)
        with self.assertRaisesRegex(ValueError, "production open window"):
            ProductionHubConfig(
                start_timeout=150,
                daemon_operation_timeout=150,
            )


class ProductionProbeDryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_and_exact_minimum_client_timeouts_are_accepted(
        self,
    ) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(args.client_timeout, 30)
        self.assertEqual(DEFAULT_CLIENT_TIMEOUT, 30)
        self.assertGreater(DEFAULT_CLIENT_TIMEOUT, DEFAULT_START_TIMEOUT)
        self.assertGreater(DEFAULT_CLIENT_TIMEOUT, DEFAULT_STOP_TIMEOUT)
        self.assertGreater(DEFAULT_CLIENT_TIMEOUT, DEFAULT_REPORT_TIMEOUT)
        minimum = minimum_client_timeout(
            start_timeout=DEFAULT_START_TIMEOUT,
            stop_timeout=DEFAULT_STOP_TIMEOUT,
        )
        self.assertEqual(CLIENT_TIMEOUT_MARGIN, 5)
        self.assertEqual(minimum, 20)
        self.assertEqual(
            await run_probe(
                execute=False,
                client_timeout=minimum,
                output=lambda _line: None,
            ),
            0,
        )

    async def test_incompatible_client_timeout_precedes_all_owned_access(
        self,
    ) -> None:
        cases = (
            {"client_timeout": 19.999},
            {"start_timeout": 30, "client_timeout": 15},
            {"stop_timeout": 30, "client_timeout": 15},
            {"start_timeout": 1, "stop_timeout": 1, "client_timeout": 1},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                builder = Mock()
                output: list[str] = []
                with (
                    patch.object(
                        probe_hubd_production,
                        "_probe_socket_path",
                        side_effect=AssertionError("socket must not be resolved"),
                    ) as resolve_socket,
                    patch.object(
                        probe_hubd_production,
                        "create_production_hub",
                        side_effect=AssertionError("hub must not be created"),
                    ) as create_hub,
                    patch.object(
                        probe_hubd_production.asyncio,
                        "open_unix_connection",
                        side_effect=AssertionError("IPC must not be opened"),
                    ) as connect,
                ):
                    status = await run_probe(
                        execute=True,
                        output=output.append,
                        session_builder=builder,
                        **arguments,
                    )
                self.assertEqual(status, 2)
                builder.assert_not_called()
                resolve_socket.assert_not_called()
                create_hub.assert_not_called()
                connect.assert_not_called()
                self.assertEqual(
                    output,
                    [
                        "HUBD PRODUCTION PROBE FAIL "
                        "category=invalid_client_timeout_configuration"
                    ],
                )

    async def test_dry_run_never_resolves_socket_or_constructs_session(
        self,
    ) -> None:
        builder = Mock()
        output: list[str] = []
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                probe_hubd_production,
                "create_production_hub",
                side_effect=AssertionError("production hub must not be created"),
            ) as create_hub,
            patch.object(
                probe_hubd_production.asyncio,
                "open_unix_connection",
                side_effect=AssertionError("IPC must not be opened"),
            ) as connect,
        ):
            status = await run_probe(
                execute=False,
                output=output.append,
                session_builder=builder,
            )
        self.assertEqual(status, 0)
        builder.assert_not_called()
        create_hub.assert_not_called()
        connect.assert_not_called()
        self.assertIn(
            "DRY RUN: no Bluetooth, BlueZ, or production session access.", output
        )
        self.assertIn("probe_performs_disconnect_or_reconnect=no", output)

    async def test_execute_without_safe_runtime_fails_before_factory(self) -> None:
        builder = Mock()
        output: list[str] = []
        with patch.dict(os.environ, {}, clear=True):
            status = await run_probe(
                execute=True,
                output=output.append,
                session_builder=builder,
            )
        self.assertEqual(status, 1)
        builder.assert_not_called()
        self.assertIn("factory_calls=0", output)
        self.assertIn(
            "HUBD PRODUCTION PROBE FAIL "
            "category=safe_runtime_directory_unavailable",
            output,
        )

    def test_default_socket_is_inside_xdg_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
                selected = probe_hubd_production._probe_socket_path(None)
        self.assertEqual(
            selected,
            Path(directory) / "airpods-hubd-probe.sock",
        )

    async def test_incompatible_outer_timeout_is_rejected_before_factory(
        self,
    ) -> None:
        builder = Mock()
        output: list[str] = []
        status = await run_probe(
            execute=True,
            daemon_operation_timeout=129,
            output=output.append,
            session_builder=builder,
        )
        self.assertEqual(status, 2)
        builder.assert_not_called()
        self.assertIn(
            "HUBD PRODUCTION PROBE FAIL category=invalid_timeout_configuration",
            output,
        )

    def test_main_defaults_to_deterministic_dry_run(self) -> None:
        first = StringIO()
        second = StringIO()
        self.assertEqual(main([], stream=first), 0)
        self.assertEqual(main([], stream=second), 0)
        self.assertEqual(first.getvalue(), second.getvalue())
        self.assertIn("future execution requires explicit --execute", first.getvalue())

    def test_main_rejects_incompatible_cli_timeout_combinations(self) -> None:
        cases = (
            ["--execute", "--start-timeout", "30", "--client-timeout", "15"],
            ["--execute", "--stop-timeout", "30", "--client-timeout", "15"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                stream = StringIO()
                with patch.object(
                    probe_hubd_production,
                    "create_production_hub",
                    side_effect=AssertionError("hub must not be created"),
                ) as create_hub:
                    self.assertEqual(main(arguments, stream=stream), 2)
                create_hub.assert_not_called()
                self.assertIn(
                    "invalid_client_timeout_configuration",
                    stream.getvalue(),
                )


class ProductionIntegrationStaticSafetyTests(unittest.TestCase):
    def test_integration_and_package_exports_remain_private(self) -> None:
        import airpods_hr

        self.assertEqual(hubd_production.__all__, [])
        self.assertFalse(hasattr(airpods_hr, "create_production_hub"))
        private_init = ROOT / "src/airpods_hr/_hubd/__init__.py"
        self.assertNotIn("production", private_init.read_text())
        self.assertEqual(
            hashlib.sha256(
                (ROOT / "src/airpods_hr/__init__.py").read_bytes()
            ).hexdigest(),
            PACKAGE_INIT_SHA256,
        )

    def test_production_session_is_frozen(self) -> None:
        digest = hashlib.sha256(
            (ROOT / "src/airpods_hr/production_session.py").read_bytes()
        ).hexdigest()
        self.assertEqual(digest, PRODUCTION_SESSION_SHA256)

    def test_integration_duplicates_no_transport_or_protocol_logic(self) -> None:
        integration = ROOT / "src/airpods_hr/_hubd/production.py"
        tree = ast.parse(integration.read_text(encoding="utf-8"))
        imports = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn("airpods_hr.bluez_coexistence", imports)
        source = integration.read_text(encoding="utf-8")
        for forbidden in (
            "KernelL2CAPTransport",
            "BlueZCompatibilityRegistration",
            "AAPHandshakeSession",
            "HeartRateCommand",
            "_airpods_aap_core",
        ):
            self.assertNotIn(forbidden, source)

    def test_no_public_daemon_script_or_systemd_unit_was_added(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(
            project["project"]["scripts"],
            {"airpods-hr": "airpods_hr.cli:main"},
        )
        self.assertFalse(list(ROOT.rglob("*.service")))
        self.assertNotIn(
            "_airpods_aap_core",
            (ROOT / "src/airpods_hr/_hubd/production.py").read_text()
            + (ROOT / "tools/probe_hubd_production.py").read_text(),
        )


if __name__ == "__main__":
    unittest.main()
