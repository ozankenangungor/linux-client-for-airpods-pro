"""Hardware-independent tests for the installed hub daemon runner."""

from __future__ import annotations

import ast
import asyncio
import logging
import math
import os
import shutil
import signal
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from airpods_hr._hubd import main as hubd_main
from airpods_hr._hubd.main import (
    EXIT_CONFIGURATION,
    EXIT_FAILURE,
    AsyncioSignalRegistrar,
    build_parser,
    main,
    run_daemon,
)
from airpods_hr._hubd.production import (
    DEFAULT_DAEMON_OPERATION_TIMEOUT,
    ProductionHubConfig,
    create_production_hub,
)
from airpods_hr._hubd.server import (
    AirPodsHubDaemon,
    DaemonState,
)
from airpods_hr.heartrate import HeartRateReport
from airpods_hr.service_installer import installed_python, render_unit
from tools.probe_hubd_runner import run_probe as run_runner_probe


ROOT = Path(__file__).resolve().parents[1]


class FakeSignalRegistrar:
    def __init__(self) -> None:
        self.callbacks: dict[signal.Signals, Any] = {}
        self.added: list[signal.Signals] = []
        self.removed: list[signal.Signals] = []

    def add(self, signum: signal.Signals, callback: Any) -> None:
        self.callbacks[signum] = callback
        self.added.append(signum)

    def remove(self, signum: signal.Signals) -> None:
        self.callbacks.pop(signum, None)
        self.removed.append(signum)

    def fire(self, signum: signal.Signals) -> None:
        self.callbacks[signum]()


class FakeDaemon:
    def __init__(self, *, start_error: BaseException | None = None) -> None:
        self.start_error = start_error
        self.start_calls = 0
        self.shutdown_calls = 0
        self.started = asyncio.Event()
        self.state = DaemonState.STOPPED

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            self.state = DaemonState.FAILED
            raise self.start_error
        self.state = DaemonState.READY
        self.started.set()

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.state = DaemonState.STOPPED


class GatedStartDaemon(FakeDaemon):
    def __init__(self) -> None:
        super().__init__()
        self.start_entered = asyncio.Event()
        self.cancelled = False

    async def start(self) -> None:
        self.start_calls += 1
        self.state = DaemonState.STARTING
        self.start_entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled = True
            self.state = DaemonState.FAILED
            raise


class FakeHubBuilder:
    def __init__(self, daemon: FakeDaemon) -> None:
        self.daemon = daemon
        self.calls = 0
        self.kwargs: dict[str, Any] = {}

    def __call__(self, _path: Path, **kwargs: Any) -> Any:
        self.calls += 1
        self.kwargs = kwargs
        return SimpleNamespace(
            daemon=self.daemon,
            factory=SimpleNamespace(calls=0, session=None),
        )


class QuietLogger(logging.Logger):
    def __init__(self) -> None:
        super().__init__("test-hubd-runner")
        self.stream = StringIO()
        handler = logging.StreamHandler(self.stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self.addHandler(handler)
        self.setLevel(logging.DEBUG)
        self.propagate = False


class RunnerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.path = Path("/private/runtime/airpods-hubd.sock")
        self.config = ProductionHubConfig()

    async def test_normal_fake_lifecycle_exits_zero_and_shuts_down_once(
        self,
    ) -> None:
        daemon = FakeDaemon()
        builder = FakeHubBuilder(daemon)
        registrar = FakeSignalRegistrar()
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_daemon(
                self.path,
                self.config,
                shutdown_event=stop,
                registrar=registrar,
                hub_builder=builder,
                logger=QuietLogger(),
            )
        )
        await daemon.started.wait()
        stop.set()
        self.assertEqual(await task, 0)
        self.assertEqual(builder.calls, 1)
        self.assertEqual(daemon.start_calls, 1)
        self.assertEqual(daemon.shutdown_calls, 1)
        self.assertEqual(daemon.state, DaemonState.STOPPED)
        self.assertEqual(registrar.added, [signal.SIGINT, signal.SIGTERM])
        self.assertEqual(registrar.removed, [signal.SIGTERM, signal.SIGINT])

    async def test_default_builder_is_the_accepted_production_composition(
        self,
    ) -> None:
        daemon = FakeDaemon()
        builder = FakeHubBuilder(daemon)
        stop = asyncio.Event()
        with patch.object(hubd_main, "create_production_hub", builder):
            task = asyncio.create_task(
                run_daemon(
                    self.path,
                    self.config,
                    shutdown_event=stop,
                    registrar=FakeSignalRegistrar(),
                    logger=QuietLogger(),
                )
            )
            await daemon.started.wait()
            stop.set()
            self.assertEqual(await task, 0)
        self.assertEqual(builder.calls, 1)
        self.assertIs(builder.kwargs["config"], self.config)

    async def _signal_after_ready(self, signum: signal.Signals) -> tuple[int, Any]:
        daemon = FakeDaemon()
        builder = FakeHubBuilder(daemon)
        registrar = FakeSignalRegistrar()
        task = asyncio.create_task(
            run_daemon(
                self.path,
                self.config,
                registrar=registrar,
                hub_builder=builder,
                logger=QuietLogger(),
            )
        )
        await daemon.started.wait()
        registrar.fire(signum)
        status = await task
        return status, (daemon, builder, registrar)

    async def test_sigint_requests_one_graceful_shutdown_and_exits_130(self) -> None:
        status, (daemon, builder, _registrar) = await self._signal_after_ready(
            signal.SIGINT
        )
        self.assertEqual(status, 130)
        self.assertEqual(builder.calls, 1)
        self.assertEqual(daemon.start_calls, 1)
        self.assertEqual(daemon.shutdown_calls, 1)

    async def test_sigterm_requests_one_graceful_shutdown_and_exits_143(self) -> None:
        status, (daemon, builder, _registrar) = await self._signal_after_ready(
            signal.SIGTERM
        )
        self.assertEqual(status, 143)
        self.assertEqual(builder.calls, 1)
        self.assertEqual(daemon.start_calls, 1)
        self.assertEqual(daemon.shutdown_calls, 1)

    async def test_signal_during_startup_cancels_then_cleans_up_once(self) -> None:
        daemon = GatedStartDaemon()
        builder = FakeHubBuilder(daemon)
        registrar = FakeSignalRegistrar()
        task = asyncio.create_task(
            run_daemon(
                self.path,
                self.config,
                registrar=registrar,
                hub_builder=builder,
                logger=QuietLogger(),
            )
        )
        await daemon.start_entered.wait()
        registrar.fire(signal.SIGTERM)
        self.assertEqual(await task, 143)
        self.assertTrue(daemon.cancelled)
        self.assertEqual(builder.calls, 1)
        self.assertEqual(daemon.start_calls, 1)
        self.assertEqual(daemon.shutdown_calls, 1)

    async def test_logging_contains_lifecycle_without_sensor_data(self) -> None:
        daemon = FakeDaemon()
        stop = asyncio.Event()
        stop.set()
        logger = QuietLogger()
        status = await run_daemon(
            self.path,
            self.config,
            shutdown_event=stop,
            registrar=FakeSignalRegistrar(),
            hub_builder=FakeHubBuilder(daemon),
            logger=logger,
        )
        self.assertEqual(status, 0)
        rendered = logger.stream.getvalue()
        for expected in ("daemon starting", "daemon ready", "daemon stopped"):
            self.assertIn(expected, rendered)
        for forbidden in (
            "bpm=",
            "raw report",
            "LinkKey",
            "encryption material",
            "AAP packet",
        ):
            self.assertNotIn(forbidden, rendered)

    async def test_fake_runner_probe_uses_real_ipc_and_cleans_up(self) -> None:
        output: list[str] = []
        self.assertEqual(await run_runner_probe(output=output.append), 0)
        self.assertIn("runner_exit=0", output)
        self.assertIn("factory_calls=1", output)
        self.assertIn("session_opens=1", output)
        self.assertIn("ping_ok=true", output)
        self.assertIn("status_state=ready", output)
        self.assertIn("session_closes=1", output)
        self.assertIn("socket_removed=true", output)
        self.assertIn("lock_released=true", output)
        self.assertIn("HUBD RUNNER FAKE PROBE PASS", output)


class RunnerOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_runner_fails_before_second_session_construction(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "airpods-hubd.sock"
            first_session = MinimalSession()
            first_builder = CountingSessionBuilder(first_session)
            first_hub = create_production_hub(path, builder=first_builder)
            await first_hub.daemon.start()
            self.assertEqual(first_builder.calls, 1)

            second_session = MinimalSession()
            second_builder = CountingSessionBuilder(second_session)

            def build_second(path: Path, **kwargs: Any) -> Any:
                return create_production_hub(
                    path,
                    config=kwargs["config"],
                    output=kwargs["output"],
                    builder=second_builder,
                )

            try:
                logger = QuietLogger()
                status = await run_daemon(
                    path,
                    ProductionHubConfig(),
                    registrar=FakeSignalRegistrar(),
                    hub_builder=build_second,
                    logger=logger,
                )
                self.assertEqual(status, EXIT_FAILURE)
                self.assertIn(
                    "daemon failed: already_running", logger.stream.getvalue()
                )
                self.assertEqual(second_builder.calls, 0)
                self.assertEqual(second_session.open_calls, 0)
                self.assertEqual(first_hub.daemon.state, DaemonState.READY)
            finally:
                await first_hub.daemon.shutdown()


class MinimalSession:
    def __init__(self) -> None:
        self.open_calls = 0
        self.close_calls = 0
        self.reports: asyncio.Queue[HeartRateReport] = asyncio.Queue()

    async def open(self) -> None:
        self.open_calls += 1

    async def start(self) -> None:
        pass

    async def receive_report(self) -> HeartRateReport:
        return await self.reports.get()

    async def stop(self) -> None:
        pass

    async def close(self) -> None:
        self.close_calls += 1


class CountingSessionBuilder:
    def __init__(self, session: MinimalSession) -> None:
        self.session = session
        self.calls = 0

    def __call__(self, **_kwargs: Any) -> MinimalSession:
        self.calls += 1
        return self.session


class RunnerCliTests(unittest.TestCase):
    def test_help_exits_without_production_or_async_access(self) -> None:
        stdout = StringIO()
        with (
            patch.object(hubd_main, "create_production_hub") as create_hub,
            patch.object(hubd_main.asyncio, "run") as asyncio_run,
            redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        create_hub.assert_not_called()
        asyncio_run.assert_not_called()

    def test_invalid_configuration_exits_before_runtime_or_production(self) -> None:
        for value in ("1", "nan", "inf"):
            with self.subTest(value=value):
                with (
                    patch.object(
                        hubd_main, "create_production_hub"
                    ) as create_hub,
                    patch.object(
                        hubd_main, "run_daemon", new_callable=AsyncMock
                    ) as run,
                    patch.object(
                        hubd_main, "socket_path_from_environment"
                    ) as socket_path,
                    patch.object(
                        hubd_main,
                        "_configure_logging",
                        return_value=QuietLogger(),
                    ),
                ):
                    status = main(["--operation-timeout", value])
            self.assertEqual(status, EXIT_CONFIGURATION)
            create_hub.assert_not_called()
            run.assert_not_awaited()
            socket_path.assert_not_called()

    def test_default_socket_comes_from_xdg_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "airpods-hubd.sock"
            run = AsyncMock(return_value=0)
            with (
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}),
                patch.object(hubd_main, "run_daemon", run),
            ):
                self.assertEqual(main([]), 0)
        self.assertEqual(run.await_args.args[0], expected)

    def test_parser_exposes_only_safe_operator_options(self) -> None:
        parser = build_parser()
        option_strings = {
            option
            for action in parser._actions
            for option in action.option_strings
        }
        self.assertEqual(
            option_strings,
            {
                "-h",
                "--help",
                "--socket-path",
                "--operation-timeout",
                "--verbose",
            },
        )


class RunnerStaticSafetyTests(unittest.TestCase):
    def test_runner_is_composition_only_with_no_retry_or_sensor_logging(self) -> None:
        source_path = ROOT / "src/airpods_hr/_hubd/main.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn("airpods_hr.bluez_coexistence", imports)
        self.assertNotIn("airpods_hr.production_session", imports)
        for forbidden in (
            "KernelL2CAP",
            "AAPHandshake",
            "HeartRateCommand",
            "receive_report",
            "while True",
            "reconnect",
            "power cycle",
            "bpm",
        ):
            self.assertNotIn(forbidden, source)

    def test_console_entrypoint_is_installed_metadata_only(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(
            project["project"]["scripts"]["airpods-hubd"],
            "airpods_hr._hubd.main:main",
        )
        import airpods_hr

        self.assertFalse(hasattr(airpods_hr, "run_daemon"))
        self.assertEqual(hubd_main.__all__, [])

    def test_systemd_unit_is_nonaggressive_user_service(self) -> None:
        unit = render_unit(installed_python())
        self.assertIn("ExecStart=", unit)
        self.assertIn(" -m airpods_hr._hubd.main", unit)
        self.assertNotIn("Environment=PATH", unit)
        self.assertNotIn("/usr/bin/env", unit)
        self.assertIn("Restart=no", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("UMask=0077", unit)
        self.assertNotIn("User=root", unit)
        self.assertNotIn("Restart=always", unit)
        for forbidden in ("bluetoothctl", "reconnect", "reset", "power cycle"):
            self.assertNotIn(forbidden, unit.lower())

    def test_systemd_stop_timeout_covers_two_daemon_cleanup_windows(self) -> None:
        unit = render_unit(installed_python())
        timeout_line = next(
            line for line in unit.splitlines() if line.startswith("TimeoutStopSec=")
        )
        timeout = float(timeout_line.partition("=")[2])
        shutdown_margin = 30.0
        self.assertTrue(math.isfinite(timeout))
        self.assertGreater(timeout, 2 * DEFAULT_DAEMON_OPERATION_TIMEOUT)
        self.assertGreaterEqual(
            timeout,
            2 * DEFAULT_DAEMON_OPERATION_TIMEOUT + shutdown_margin,
        )

    def test_systemd_unit_verifies_when_systemd_analyze_is_available(self) -> None:
        analyzer = shutil.which("systemd-analyze")
        if analyzer is None:
            self.skipTest("systemd-analyze is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            unit_path = Path(directory) / "airpods-hubd.service"
            unit_path.write_text(render_unit(installed_python()), encoding="utf-8")
            result = subprocess.run(
                [analyzer, "verify", "--user", str(unit_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_asyncio_signal_registrar_delegates_to_loop(self) -> None:
        loop = Mock()
        registrar = AsyncioSignalRegistrar(loop)
        callback = Mock()
        registrar.add(signal.SIGTERM, callback)
        registrar.remove(signal.SIGTERM)
        loop.add_signal_handler.assert_called_once_with(signal.SIGTERM, callback)
        loop.remove_signal_handler.assert_called_once_with(signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
