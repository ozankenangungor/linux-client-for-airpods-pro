"""Hardware-independent CLI tests for device diagnostics."""

from __future__ import annotations


import signal

import unittest


from typing import Any, Mapping


from airpods_hr.bluetooth import AdapterRestoreError

from airpods_hr.heart_rate_diagnostics import DiagnosticWriteError, HeartRateDiagnosticRecorder


from airpods_hr.heart_rate_session import HeartRateProgress
from airpods_hr.heartrate import HeartRateReport, parse_heart_rate_packet
from airpods_hr.monitor_cli import create_heart_rate_progress, run_composed_monitor, run_live_monitor, run_monitor_command


from airpods_hr.protocol import HEART_RATE_MARKER


RAW_REPORT = bytes.fromhex(
    "01 48 a5 34 12 5a 08 07 06 05 04 03 02 01 ef cd ab 89"
)


def parsed_report() -> HeartRateReport:
    return parse_heart_rate_packet(b"\x08\x01" + HEART_RATE_MARKER + RAW_REPORT)


class FakeSignalRegistrar:
    def __init__(self) -> None:
        self.callbacks: dict[signal.Signals, object] = {}

    def add(self, signum: signal.Signals, callback) -> None:
        self.callbacks[signum] = callback

    def remove(self, signum: signal.Signals) -> None:
        self.callbacks.pop(signum, None)


class FakeBackend:
    def __init__(self) -> None:
        self.close_count = 0

    async def connect(self) -> None:
        return None

    def close(self) -> None:
        self.close_count += 1


class FailingSink:
    def __init__(self, successful_writes: int) -> None:
        self.successful_writes = successful_writes
        self.write_count = 0
        self.close_count = 0

    def write_event(self, event: Mapping[str, Any]) -> None:
        del event
        if self.write_count >= self.successful_writes:
            raise DiagnosticWriteError("synthetic private failure")
        self.write_count += 1

    def close(self) -> None:
        self.close_count += 1


class DiagnosticFailureLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_write_failure_unwinds_and_closes_every_owner(self) -> None:
        sink = FailingSink(successful_writes=1)
        discovery = FakeBackend()
        bluez = FakeBackend()
        monitor_cleanup = False

        def recorder_factory(output_path):
            del output_path
            return HeartRateDiagnosticRecorder(sink)

        class Session:
            def __init__(self, lifecycle, stdout, stderr) -> None:
                self._progress = create_heart_rate_progress(
                    lifecycle, stdout, stderr
                )

            async def run(self, stop_event) -> None:
                nonlocal monitor_cleanup
                del stop_event
                try:
                    self._progress(HeartRateProgress.SAMPLE, parsed_report())
                finally:
                    monitor_cleanup = True

        async def session_factory(
            discovery_backend,
            bluez_backend,
            lifecycle,
            stdout,
            stderr,
        ):
            del discovery_backend, bluez_backend
            return Session(lifecycle, stdout, stderr)

        def operation(lifecycle, stdout, stderr):
            return run_composed_monitor(
                lifecycle,
                stdout,
                stderr,
                discovery_backend_factory=lambda: discovery,
                bluez_backend_factory=lambda: bluez,
                session_factory=session_factory,
            )

        async def live_runner(
            stdout,
            stderr,
            *,
            diagnostic_recorder=None,
        ):
            return await run_live_monitor(
                stdout,
                stderr,
                diagnostic_recorder=diagnostic_recorder,
                registrar=FakeSignalRegistrar(),
                operation_factory=operation,
            )

        stderr: list[str] = []
        status = await run_monitor_command(
            dry_run=False,
            diagnostic=True,
            stdout=lambda message: None,
            stderr=stderr.append,
            live_runner=live_runner,
            recorder_factory=recorder_factory,
        )

        self.assertEqual(status, 1)
        self.assertTrue(monitor_cleanup)
        self.assertEqual(discovery.close_count, 1)
        self.assertEqual(bluez.close_count, 1)
        self.assertEqual(sink.close_count, 1)
        self.assertEqual(
            stderr[-1], "Error: diagnostic capture failed; cleanup was attempted."
        )
        self.assertNotIn("synthetic private failure", "\n".join(stderr))

    async def test_signal_exit_is_recorded_without_lifecycle_change(self) -> None:
        events: list[dict[str, Any]] = []

        class Sink:
            def write_event(self, event: Mapping[str, Any]) -> None:
                events.append(dict(event))

            def close(self) -> None:
                return None

        async def live_runner(stdout, stderr, *, diagnostic_recorder=None):
            del stdout, stderr, diagnostic_recorder
            return 130

        status = await run_monitor_command(
            dry_run=False,
            diagnostic=True,
            stdout=lambda message: None,
            stderr=lambda message: None,
            live_runner=live_runner,
            recorder_factory=lambda output: HeartRateDiagnosticRecorder(
                Sink()
            ),
        )

        self.assertEqual(status, 130)
        self.assertEqual(events[-1]["event"], "session_stop")
        self.assertEqual(events[-1]["termination_reason"], "sigint")

    async def test_restore_error_remains_authoritative_over_finalization(self) -> None:
        sink = FailingSink(successful_writes=1)

        async def live_runner(stdout, stderr, *, diagnostic_recorder=None):
            del stdout, stderr, diagnostic_recorder
            raise AdapterRestoreError("private restoration details")

        stderr: list[str] = []
        status = await run_monitor_command(
            dry_run=False,
            diagnostic=True,
            stdout=lambda message: None,
            stderr=stderr.append,
            live_runner=live_runner,
            recorder_factory=lambda output: HeartRateDiagnosticRecorder(sink),
        )

        self.assertEqual(status, 1)
        self.assertEqual(stderr, ["Error: BlueZ adapter restoration failed."])
        self.assertEqual(sink.close_count, 1)


