"""Hardware-independent tests for the continuous-monitor validation probe."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from airpods_hr.heart_rate_session import (
    HeartRateMonitorResult,
    HeartRateMonitorSessionResult,
    HeartRateProgress,
    HeartRateSessionError,
)
from airpods_hr.heartrate import HeartRateReport
from tools.probe_heart_rate_monitor import (
    DEFAULT_VALIDATION_SAMPLE_TARGET,
    DEFAULT_VALIDATION_WINDOW_SECONDS,
    MonitorProbeOutcome,
    _WatchdogState,
    _heart_rate_progress,
    _run_session_with_watchdog,
    _validation_watchdog,
    build_parser,
    main,
    run_probe,
)


def monitor_result(samples_observed: int) -> HeartRateMonitorResult:
    return HeartRateMonitorResult(
        display_name="Synthetic AirPods",
        heart_rate=HeartRateMonitorSessionResult(
            samples_observed=samples_observed,
            stop_acknowledged=True,
            application_payloads_sent=10,
            control_frames_observed=4,
            non_hr_frames=0,
            malformed_hr_frames=0,
        ),
        replacement_key_reported=False,
        sdp_diagnostics=SimpleNamespace(),
    )


def outcome(samples_observed: int, *, expired: bool) -> MonitorProbeOutcome:
    return MonitorProbeOutcome(monitor_result(samples_observed), expired)


class MonitorProbeParserTests(unittest.TestCase):
    def test_defaults_are_dry_run_and_bounded(self) -> None:
        args = build_parser().parse_args([])

        self.assertFalse(args.execute)
        self.assertEqual(args.samples, DEFAULT_VALIDATION_SAMPLE_TARGET)
        self.assertEqual(
            args.monitor_window, DEFAULT_VALIDATION_WINDOW_SECONDS
        )

    def test_sample_bounds_are_enforced(self) -> None:
        parser = build_parser()
        for value in ("0", "21"):
            with self.subTest(value=value), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parser.parse_args(["--samples", value])
                self.assertEqual(raised.exception.code, 2)
        self.assertEqual(parser.parse_args(["--samples", "1"]).samples, 1)
        self.assertEqual(parser.parse_args(["--samples", "20"]).samples, 20)

    def test_monitor_window_bounds_are_enforced(self) -> None:
        parser = build_parser()
        for value in ("2.99", "30.01"):
            with self.subTest(value=value), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    parser.parse_args(["--monitor-window", value])
                self.assertEqual(raised.exception.code, 2)
        self.assertEqual(
            parser.parse_args(["--monitor-window", "3"]).monitor_window,
            3,
        )
        self.assertEqual(
            parser.parse_args(["--monitor-window", "30"]).monitor_window,
            30,
        )

    def test_main_default_invocation_is_dry_run(self) -> None:
        stream = StringIO()

        status = main([], stream=stream)

        self.assertEqual(status, 0)
        self.assertIn(
            "DRY RUN: no Bluetooth state will be changed.", stream.getvalue()
        )


class MonitorProbeDryRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_never_calls_live_runner_or_backends(self) -> None:
        output: list[str] = []
        live_runner = AsyncMock()
        with (
            patch(
                "tools.probe_heart_rate_monitor.DBusNextManagedObjectsBackend"
            ) as discovery_backend,
            patch(
                "tools.probe_heart_rate_monitor.DBusNextBlueZBackend"
            ) as bluez_backend,
        ):
            status = await run_probe(
                execute=False,
                output=output.append,
                live_runner=live_runner,
            )

        self.assertEqual(status, 0)
        live_runner.assert_not_awaited()
        discovery_backend.assert_not_called()
        bluez_backend.assert_not_called()

    async def test_dry_run_prints_target_window_and_safety_plan(self) -> None:
        output: list[str] = []

        status = await run_probe(
            execute=False,
            sample_target=12,
            monitor_window=18,
            output=output.append,
        )

        self.assertEqual(status, 0)
        self.assertIn("Continuous monitor core validation", output)
        self.assertIn("Validation sample target: 12", output)
        self.assertIn("Post-start safety window: 18 seconds", output)
        rendered = "\n".join(output)
        self.assertIn("STOP_HR then HR_OFF", rendered)
        self.assertIn("restore BlueZ", rendered)
        self.assertIn("no reconnect", rendered)
        self.assertIn("no opcode 0x44", rendered)


class MonitorProbeProgressTests(unittest.TestCase):
    def test_samples_are_counted_printed_and_set_stop_at_target(self) -> None:
        output: list[str] = []
        stop_event = asyncio.Event()
        start_acknowledged = asyncio.Event()
        progress = _heart_rate_progress(
            output.append,
            3,
            stop_event,
            start_acknowledged,
        )
        reports = (
            HeartRateReport(70, 1, 10, 2, 100, 3),
            HeartRateReport(71, 4, 11, 5, 101, 6),
            HeartRateReport(72, 7, 12, 8, 102, 9),
        )

        progress(HeartRateProgress.SAMPLE, reports[0])
        progress(HeartRateProgress.SAMPLE, reports[1])
        self.assertFalse(stop_event.is_set())
        progress(HeartRateProgress.SAMPLE, reports[2])

        self.assertTrue(stop_event.is_set())
        self.assertEqual(
            output,
            [
                "Heart rate 1: 70 bpm",
                "Heart rate 2: 71 bpm",
                "Heart rate 3: 72 bpm",
            ],
        )
        rendered = "\n".join(output)
        for private_value in ("100", "101", "102"):
            self.assertNotIn(private_value, rendered)

    def test_start_acknowledged_releases_watchdog_gate(self) -> None:
        output: list[str] = []
        stop_event = asyncio.Event()
        start_acknowledged = asyncio.Event()
        progress = _heart_rate_progress(
            output.append,
            1,
            stop_event,
            start_acknowledged,
        )

        progress(HeartRateProgress.START_ACKNOWLEDGED, None)

        self.assertTrue(start_acknowledged.is_set())
        self.assertEqual(output, ["Heart-rate start acknowledgement: OK"])


class ValidationWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchdog_waits_for_start_ack_before_window(self) -> None:
        start_acknowledged = asyncio.Event()
        stop_event = asyncio.Event()
        wait_started = asyncio.Event()
        state = _WatchdogState()

        async def record_wait(awaitable, timeout):
            self.assertEqual(timeout, 15)
            wait_started.set()
            return await awaitable

        task = asyncio.create_task(
            _validation_watchdog(
                start_acknowledged,
                stop_event,
                15,
                state,
                wait_for=record_wait,
            )
        )
        await asyncio.sleep(0)
        self.assertFalse(wait_started.is_set())
        start_acknowledged.set()
        await wait_started.wait()
        stop_event.set()
        await task

        self.assertFalse(state.expired)

    async def test_watchdog_expiration_sets_event_without_cancelling_monitor(
        self,
    ) -> None:
        start_acknowledged = asyncio.Event()
        start_acknowledged.set()
        stop_event = asyncio.Event()
        state = _WatchdogState()

        async def expire(awaitable, timeout):
            self.assertEqual(timeout, 15)
            awaitable.close()
            raise TimeoutError

        monitor_task = asyncio.create_task(stop_event.wait())
        await _validation_watchdog(
            start_acknowledged,
            stop_event,
            15,
            state,
            wait_for=expire,
        )
        await monitor_task

        self.assertTrue(state.expired)
        self.assertTrue(stop_event.is_set())
        self.assertFalse(monitor_task.cancelled())

    async def test_target_completion_cancels_and_awaits_watchdog(self) -> None:
        created_tasks: list[asyncio.Task] = []
        original_create_task = asyncio.create_task

        def track_task(coro):
            task = original_create_task(coro)
            created_tasks.append(task)
            return task

        class ImmediateSession:
            async def run(self, stop_event):
                stop_event.set()
                return monitor_result(8)

        with patch(
            "tools.probe_heart_rate_monitor.asyncio.create_task",
            side_effect=track_task,
        ):
            result = await _run_session_with_watchdog(
                ImmediateSession(),
                asyncio.Event(),
                asyncio.Event(),
                15,
                _WatchdogState(),
            )

        self.assertEqual(result.heart_rate.samples_observed, 8)
        self.assertEqual(len(created_tasks), 1)
        self.assertTrue(created_tasks[0].done())
        self.assertTrue(created_tasks[0].cancelled())
        self.assertNotIn(created_tasks[0], asyncio.all_tasks())

    async def test_cancellation_propagates_after_watchdog_is_awaited(self) -> None:
        started = asyncio.Event()

        class BlockingSession:
            async def run(self, stop_event):
                del stop_event
                started.set()
                await asyncio.Future()
                raise AssertionError("unreachable")

        task = asyncio.create_task(
            _run_session_with_watchdog(
                BlockingSession(),
                asyncio.Event(),
                asyncio.Event(),
                15,
                _WatchdogState(),
            )
        )
        await started.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task


class MonitorProbeResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_reached_returns_pass(self) -> None:
        output: list[str] = []
        runner = AsyncMock(return_value=outcome(8, expired=False))

        status = await run_probe(
            execute=True,
            output=output.append,
            live_runner=runner,
        )

        self.assertEqual(status, 0)
        self.assertEqual(
            output[-1], "PASS: 8 continuous heart-rate samples observed."
        )
        called_output, called_target, called_window = runner.call_args.args
        self.assertTrue(callable(called_output))
        self.assertEqual((called_target, called_window), (8, 15.0))

    async def test_watchdog_before_target_returns_partial(self) -> None:
        output: list[str] = []

        status = await run_probe(
            execute=True,
            sample_target=8,
            live_runner=AsyncMock(return_value=outcome(3, expired=True)),
            output=output.append,
        )

        self.assertEqual(status, 2)
        self.assertEqual(
            output[-1],
            "PARTIAL: 3 of 8 continuous heart-rate samples observed before "
            "the validation window ended.",
        )

    async def test_zero_sample_watchdog_is_partial(self) -> None:
        output: list[str] = []

        status = await run_probe(
            execute=True,
            live_runner=AsyncMock(return_value=outcome(0, expired=True)),
            output=output.append,
        )

        self.assertEqual(status, 2)
        self.assertIn("PARTIAL: 0 of 8", output[-1])

    async def test_programmatic_cancellation_propagates(self) -> None:
        runner = AsyncMock(side_effect=asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            await run_probe(execute=True, live_runner=runner)

    async def test_safe_failure_mapping_returns_one(self) -> None:
        output: list[str] = []
        runner = AsyncMock(side_effect=HeartRateSessionError("private detail"))

        status = await run_probe(
            execute=True,
            live_runner=runner,
            output=output.append,
        )

        self.assertEqual(status, 1)
        self.assertEqual(
            output,
            ["FAIL: heart-rate monitor failed; cleanup was attempted."],
        )
        self.assertNotIn("private detail", "\n".join(output))


if __name__ == "__main__":
    unittest.main()
