"""Hardware-independent tests for the installed hub daemon runner."""

from __future__ import annotations


import asyncio
import logging


import signal

import tempfile

import unittest

from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from airpods_hr._hubd import main as hubd_main
from airpods_hr._hubd.main import EXIT_FAILURE, run_daemon


from airpods_hr._hubd.production import ProductionHubConfig, create_production_hub
from airpods_hr._hubd.server import DaemonState


from airpods_hr.heartrate import HeartRateReport


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


