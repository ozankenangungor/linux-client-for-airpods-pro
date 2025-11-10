"""Hardware-independent tests for the continuous-monitor validation probe."""

from __future__ import annotations

import asyncio
import unittest


from types import SimpleNamespace
from unittest.mock import patch

from airpods_hr.heart_rate_session import HeartRateMonitorResult, HeartRateMonitorSessionResult, HeartRateProgress


from airpods_hr.heartrate import HeartRateReport
from tools.probe_heart_rate_monitor import _WatchdogState, _heart_rate_progress, _run_session_with_watchdog, _validation_watchdog


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


