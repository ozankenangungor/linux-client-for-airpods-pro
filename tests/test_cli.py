"""Hardware-independent tests for the packaged monitor command."""

from __future__ import annotations

import asyncio
import signal
import unittest

from unittest.mock import AsyncMock

from airpods_hr.aap import AAPHandshakeError
from airpods_hr.aap_channel import AAPChannelError
from airpods_hr.authentication import ClassicAuthenticationError
from airpods_hr.bluetooth import AdapterRestoreError, HandoffError

from airpods_hr.discovery import (
    MultipleAirPodsCandidatesError,
    NoAirPodsCandidatesError,
)
from airpods_hr.heart_rate_session import HeartRateProgress, HeartRateSessionError
from airpods_hr.heartrate import HeartRateReport
from airpods_hr.monitor_cli import (
    MonitorLifecycle,
    create_heart_rate_progress,
    run_composed_monitor,
    run_live_monitor,
    run_monitor_command,
)
from airpods_hr.pairing import PairingStoreError
from airpods_hr.sdp import SDPCompatibilityError


class FakeSignalRegistrar:
    def __init__(self) -> None:
        self.callbacks: dict[signal.Signals, object] = {}
        self.added: list[signal.Signals] = []
        self.removed: list[signal.Signals] = []

    def add(self, signum: signal.Signals, callback) -> None:
        self.added.append(signum)
        self.callbacks[signum] = callback

    def remove(self, signum: signal.Signals) -> None:
        self.removed.append(signum)
        self.callbacks.pop(signum, None)

    def fire(self, signum: signal.Signals) -> None:
        callback = self.callbacks[signum]
        callback()


class FakeBackend:
    def __init__(self) -> None:
        self.connect_count = 0
        self.close_count = 0

    async def connect(self) -> None:
        self.connect_count += 1

    def close(self) -> None:
        self.close_count += 1


def report(bpm: int = 72) -> HeartRateReport:
    return HeartRateReport(
        bpm=bpm,
        aux=0,
        sequence=0,
        field_5=0,
        timestamp_ticks=0,
        flags=0,
    )


class ProductProgressTests(unittest.TestCase):
    def test_sample_writes_only_bpm_to_stdout(self) -> None:
        lifecycle = MonitorLifecycle()
        stdout: list[str] = []
        stderr: list[str] = []
        progress = create_heart_rate_progress(lifecycle, stdout.append, stderr.append)

        progress(HeartRateProgress.SAMPLE, report(81))

        self.assertEqual(stdout, ["Heart rate: 81 bpm"])
        self.assertEqual(stderr, [])

    def test_start_status_writes_only_to_stderr(self) -> None:
        lifecycle = MonitorLifecycle()
        stdout: list[str] = []
        stderr: list[str] = []
        progress = create_heart_rate_progress(lifecycle, stdout.append, stderr.append)

        progress(HeartRateProgress.START_ACKNOWLEDGED, None)

        self.assertTrue(lifecycle.start_acknowledged)
        self.assertEqual(stdout, [])
        self.assertEqual(
            stderr,
            ["Heart-rate monitoring started.", "Press Ctrl+C to stop."],
        )


class SignalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def run_before_start_signal(self, signum: signal.Signals) -> tuple:
        registrar = FakeSignalRegistrar()
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        lifecycle_box: list[MonitorLifecycle] = []

        async def operation(lifecycle, stdout, stderr):
            del stdout, stderr
            lifecycle_box.append(lifecycle)
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        task = asyncio.create_task(
            run_live_monitor(
                lambda message: None,
                lambda message: None,
                registrar=registrar,
                operation_factory=operation,
            )
        )
        await entered.wait()
        registrar.fire(signum)
        status = await task
        return status, registrar, lifecycle_box[0], cancelled

    async def test_first_sigint_before_start_cancels_and_returns_130(self) -> None:
        status, registrar, lifecycle, cancelled = await self.run_before_start_signal(
            signal.SIGINT
        )

        self.assertEqual(status, 130)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(lifecycle.stop_event.is_set())
        self.assertEqual(
            registrar.removed, [signal.SIGTERM, signal.SIGINT]
        )

    async def test_first_sigterm_before_start_cancels_and_returns_143(self) -> None:
        status, _, lifecycle, cancelled = await self.run_before_start_signal(
            signal.SIGTERM
        )

        self.assertEqual(status, 143)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(lifecycle.stop_event.is_set())

    async def run_after_start_signal(self, signum: signal.Signals) -> tuple:
        registrar = FakeSignalRegistrar()
        started = asyncio.Event()
        stop_seen = asyncio.Event()
        release = asyncio.Event()
        cancelled = False
        lifecycle_box: list[MonitorLifecycle] = []

        async def operation(lifecycle, stdout, stderr):
            nonlocal cancelled
            del stdout, stderr
            lifecycle_box.append(lifecycle)
            lifecycle.mark_start_acknowledged()
            started.set()
            try:
                await lifecycle.stop_event.wait()
                stop_seen.set()
                await release.wait()
                lifecycle.mark_bluez_restored()
            except asyncio.CancelledError:
                cancelled = True
                raise

        task = asyncio.create_task(
            run_live_monitor(
                lambda message: None,
                lambda message: None,
                registrar=registrar,
                operation_factory=operation,
            )
        )
        await started.wait()
        registrar.fire(signum)
        await stop_seen.wait()
        lifecycle = lifecycle_box[0]
        initially_cancelling = lifecycle.active_task.cancelling()
        release.set()
        status = await task
        return status, lifecycle, cancelled, initially_cancelling

    async def test_first_sigint_after_start_sets_event_and_returns_130(self) -> None:
        status, lifecycle, cancelled, initially_cancelling = (
            await self.run_after_start_signal(signal.SIGINT)
        )

        self.assertEqual(status, 130)
        self.assertTrue(lifecycle.stop_event.is_set())
        self.assertFalse(cancelled)
        self.assertEqual(initially_cancelling, 0)

    async def test_first_sigterm_after_start_sets_event_and_returns_143(self) -> None:
        status, lifecycle, cancelled, initially_cancelling = (
            await self.run_after_start_signal(signal.SIGTERM)
        )

        self.assertEqual(status, 143)
        self.assertTrue(lifecycle.stop_event.is_set())
        self.assertFalse(cancelled)
        self.assertEqual(initially_cancelling, 0)

    async def test_second_signal_cancels_active_cleanup_once(self) -> None:
        registrar = FakeSignalRegistrar()
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_attempts = 0
        cancellation_observed = asyncio.Event()

        async def operation(lifecycle, stdout, stderr):
            nonlocal cleanup_attempts
            del stdout, stderr
            lifecycle.mark_start_acknowledged()
            started.set()
            await lifecycle.stop_event.wait()
            cleanup_attempts += 1
            cleanup_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_observed.set()
                raise

        task = asyncio.create_task(
            run_live_monitor(
                lambda message: None,
                lambda message: None,
                registrar=registrar,
                operation_factory=operation,
            )
        )
        await started.wait()
        registrar.fire(signal.SIGINT)
        await cleanup_started.wait()
        registrar.fire(signal.SIGTERM)
        status = await task

        self.assertEqual(status, 130)
        self.assertTrue(cancellation_observed.is_set())
        self.assertEqual(cleanup_attempts, 1)

    async def test_signal_after_operation_is_done_is_harmless(self) -> None:
        lifecycle = MonitorLifecycle()

        async def completed() -> None:
            return None

        task = asyncio.create_task(completed())
        await task
        lifecycle.active_task = task

        lifecycle.request_shutdown(signal.SIGINT)

        self.assertFalse(lifecycle.shutdown_requested)
        self.assertIsNone(lifecycle.first_signal)

    async def test_programmatic_normal_stop_returns_zero(self) -> None:
        registrar = FakeSignalRegistrar()
        stdout: list[str] = []
        stderr: list[str] = []

        async def operation(lifecycle, stdout, stderr):
            del stdout, stderr
            lifecycle.stop_event.set()
            lifecycle.mark_bluez_restored()

        status = await run_live_monitor(
            stdout.append,
            stderr.append,
            registrar=registrar,
            operation_factory=operation,
        )

        self.assertEqual(status, 0)
        self.assertEqual(stdout, [])
        self.assertEqual(
            stderr,
            [
                "Heart-rate monitoring stopped.",
                "Bluetooth ownership and BlueZ state restored.",
            ],
        )

    async def test_programmatic_cancellation_propagates(self) -> None:
        registrar = FakeSignalRegistrar()
        entered = asyncio.Event()

        async def operation(lifecycle, stdout, stderr):
            del lifecycle, stdout, stderr
            entered.set()
            await asyncio.Future()

        task = asyncio.create_task(
            run_live_monitor(
                lambda message: None,
                lambda message: None,
                registrar=registrar,
                operation_factory=operation,
            )
        )
        await entered.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_handlers_are_scoped_and_no_tasks_remain(self) -> None:
        registrar = FakeSignalRegistrar()
        current = asyncio.current_task()
        before = {task for task in asyncio.all_tasks() if task is not current}

        async def operation(lifecycle, stdout, stderr):
            del lifecycle, stdout, stderr

        self.assertEqual(
            await run_live_monitor(
                lambda message: None,
                lambda message: None,
                registrar=registrar,
                operation_factory=operation,
            ),
            0,
        )

        after = {task for task in asyncio.all_tasks() if task is not current}
        self.assertEqual(registrar.added, [signal.SIGINT, signal.SIGTERM])
        self.assertEqual(registrar.removed, [signal.SIGTERM, signal.SIGINT])
        self.assertEqual(registrar.callbacks, {})
        self.assertEqual(after, before)


class BackendLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def run_with_backends(self, session) -> tuple[FakeBackend, FakeBackend]:
        discovery = FakeBackend()
        bluez = FakeBackend()

        async def session_factory(*args):
            del args
            return session

        await run_composed_monitor(
            MonitorLifecycle(),
            lambda message: None,
            lambda message: None,
            discovery_backend_factory=lambda: discovery,
            bluez_backend_factory=lambda: bluez,
            session_factory=session_factory,
        )
        return discovery, bluez

    async def test_backends_close_once_after_success(self) -> None:
        class Session:
            async def run(self, stop_event):
                stop_event.set()

        discovery, bluez = await self.run_with_backends(Session())

        self.assertEqual((discovery.connect_count, discovery.close_count), (1, 1))
        self.assertEqual((bluez.connect_count, bluez.close_count), (1, 1))

    async def test_backends_close_once_after_protocol_failure(self) -> None:
        discovery = FakeBackend()
        bluez = FakeBackend()

        class Session:
            async def run(self, stop_event):
                del stop_event
                raise AAPHandshakeError("synthetic")

        async def session_factory(*args):
            del args
            return Session()

        with self.assertRaises(AAPHandshakeError):
            await run_composed_monitor(
                MonitorLifecycle(),
                lambda message: None,
                lambda message: None,
                discovery_backend_factory=lambda: discovery,
                bluez_backend_factory=lambda: bluez,
                session_factory=session_factory,
            )

        self.assertEqual(discovery.close_count, 1)
        self.assertEqual(bluez.close_count, 1)

    async def test_backends_close_once_after_signal_cancellation(self) -> None:
        discovery = FakeBackend()
        bluez = FakeBackend()
        registrar = FakeSignalRegistrar()
        entered = asyncio.Event()

        class Session:
            async def run(self, stop_event):
                del stop_event
                entered.set()
                await asyncio.Future()

        async def session_factory(*args):
            del args
            return Session()

        def operation(lifecycle, stdout, stderr):
            return run_composed_monitor(
                lifecycle,
                stdout,
                stderr,
                discovery_backend_factory=lambda: discovery,
                bluez_backend_factory=lambda: bluez,
                session_factory=session_factory,
            )

        task = asyncio.create_task(
            run_live_monitor(
                lambda message: None,
                lambda message: None,
                registrar=registrar,
                operation_factory=operation,
            )
        )
        await entered.wait()
        registrar.fire(signal.SIGINT)

        self.assertEqual(await task, 130)
        self.assertEqual(discovery.close_count, 1)
        self.assertEqual(bluez.close_count, 1)

    async def test_backends_close_once_after_sigterm_graceful_stop(self) -> None:
        discovery = FakeBackend()
        bluez = FakeBackend()
        registrar = FakeSignalRegistrar()
        started = asyncio.Event()

        class Session:
            async def run(self, stop_event):
                started.set()
                await stop_event.wait()

        async def session_factory(
            discovery_backend,
            bluez_backend,
            lifecycle,
            stdout,
            stderr,
        ):
            del discovery_backend, bluez_backend, stdout, stderr
            lifecycle.mark_start_acknowledged()
            return Session()

        def operation(lifecycle, stdout, stderr):
            return run_composed_monitor(
                lifecycle,
                stdout,
                stderr,
                discovery_backend_factory=lambda: discovery,
                bluez_backend_factory=lambda: bluez,
                session_factory=session_factory,
            )

        task = asyncio.create_task(
            run_live_monitor(
                lambda message: None,
                lambda message: None,
                registrar=registrar,
                operation_factory=operation,
            )
        )
        await started.wait()
        registrar.fire(signal.SIGTERM)

        self.assertEqual(await task, 143)
        self.assertEqual(discovery.close_count, 1)
        self.assertEqual(bluez.close_count, 1)


class SafeErrorMappingTests(unittest.IsolatedAsyncioTestCase):
    async def assert_safe_failure(self, error: Exception, expected: str) -> None:
        stdout: list[str] = []
        stderr: list[str] = []

        async def fail(sample_output, status_output):
            del sample_output, status_output
            raise error

        status = await run_monitor_command(
            dry_run=False,
            stdout=stdout.append,
            stderr=stderr.append,
            live_runner=fail,
        )

        self.assertEqual(status, 1)
        self.assertEqual(stdout, [])
        self.assertIn(expected, stderr)

    async def test_session_failure_is_safe(self) -> None:
        await self.assert_safe_failure(
            HeartRateSessionError("private details"),
            "Error: the heart-rate session failed; cleanup was attempted.",
        )

    async def test_all_known_failure_categories_have_safe_messages(self) -> None:
        cases = (
            (
                NoAirPodsCandidatesError("private"),
                "Error: no paired AirPods candidate was found.",
            ),
            (
                MultipleAirPodsCandidatesError([]),
                "Error: multiple paired AirPods candidates require selection.",
            ),
            (
                PairingStoreError("private"),
                "Error: existing local Classic credentials could not be loaded.",
            ),
            (
                SDPCompatibilityError("private"),
                "Error: the local SDP compatibility profile could not be prepared.",
            ),
            (
                AAPHandshakeError("private"),
                "Error: the AAP handshake or descriptor phase failed.",
            ),
            (AAPChannelError("private"), "Error: the AAP channel failed."),
            (
                HeartRateSessionError("private"),
                "Error: the heart-rate session failed; cleanup was attempted.",
            ),
            (
                AdapterRestoreError("private"),
                "Error: BlueZ adapter restoration failed.",
            ),
            (
                HandoffError("private"),
                "Error: controller handoff failed; cleanup was attempted.",
            ),
            (
                ClassicAuthenticationError("private"),
                "Error: the Classic security session failed.",
            ),
        )
        for error, message in cases:
            with self.subTest(error_type=type(error).__name__):
                await self.assert_safe_failure(error, message)

    async def test_adapter_restore_error_never_reports_success(self) -> None:
        stderr: list[str] = []

        async def fail(sample_output, status_output):
            del sample_output, status_output
            raise AdapterRestoreError("private details")

        status = await run_monitor_command(
            dry_run=False,
            stdout=lambda message: None,
            stderr=stderr.append,
            live_runner=fail,
        )

        self.assertEqual(status, 1)
        self.assertEqual(stderr, ["Error: BlueZ adapter restoration failed."])
        self.assertNotIn("restored", "\n".join(stderr).lower())

    async def test_unexpected_exception_text_is_not_leaked(self) -> None:
        await self.assert_safe_failure(
            RuntimeError("private identifier and credential"),
            "Error: an unexpected monitor failure occurred.",
        )

    async def test_dry_run_never_calls_live_runner(self) -> None:
        runner = AsyncMock()
        stderr: list[str] = []

        status = await run_monitor_command(
            dry_run=True,
            stdout=lambda message: None,
            stderr=stderr.append,
            live_runner=runner,
        )

        self.assertEqual(status, 0)
        runner.assert_not_awaited()
        self.assertIn("DRY RUN: no Bluetooth state will be changed.", stderr)


