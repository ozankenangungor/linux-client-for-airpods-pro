"""User-facing continuous monitor command and Unix signal lifecycle."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from airpods_hr.aap import AAPHandshakeError, AAPHandshakeSession
from airpods_hr.aap_channel import AAPChannelError, AAPChannelSession
from airpods_hr.authentication import (
    BumbleClassicRuntimeFactory,
    ClassicAuthenticationError,
    ClassicAuthenticationSession,
    create_controller_handoff_transport,
)
from airpods_hr.bluetooth import (
    AdapterRestoreError,
    DBusNextBlueZBackend,
    HandoffError,
)
from airpods_hr.discovery import (
    BlueZDeviceDiscovery,
    DBusNextManagedObjectsBackend,
    MultipleAirPodsCandidatesError,
    NoAirPodsCandidatesError,
)
from airpods_hr.heart_rate_session import (
    HeartRateMonitorActivationSession,
    HeartRateMonitorResult,
    HeartRateMonitorSession,
    HeartRateProgress,
    HeartRateSessionError,
)
from airpods_hr.heart_rate_diagnostics import (
    DiagnosticOutputOpenError,
    HeartRateDiagnosticError,
    HeartRateDiagnosticRecorder,
    create_diagnostic_recorder,
)
from airpods_hr.heartrate import HeartRateReport
from airpods_hr.pairing import BlueZPairingStore, PairingStoreError
from airpods_hr.sdp import SDPCompatibilityError


Output = Callable[[str], None]


class MonitorSession(Protocol):
    async def run(self, stop_event: asyncio.Event) -> HeartRateMonitorResult: ...


class SignalRegistrar(Protocol):
    def add(self, signum: signal.Signals, callback: Callable[[], None]) -> None: ...

    def remove(self, signum: signal.Signals) -> None: ...


class ConnectedBackend(Protocol):
    async def connect(self) -> None: ...

    def close(self) -> None: ...


SessionFactory = Callable[
    [
        DBusNextManagedObjectsBackend,
        DBusNextBlueZBackend,
        "MonitorLifecycle",
        Output,
        Output,
    ],
    Awaitable[MonitorSession],
]
OperationFactory = Callable[["MonitorLifecycle", Output, Output], Awaitable[None]]
RecorderFactory = Callable[[str | Path | None], HeartRateDiagnosticRecorder]


class LiveRunner(Protocol):
    async def __call__(
        self,
        stdout: Output,
        stderr: Output,
        *,
        diagnostic_recorder: HeartRateDiagnosticRecorder | None = None,
    ) -> int: ...


class MonitorBackendCleanupError(RuntimeError):
    """Raised when a D-Bus backend cannot be closed after a monitor run."""


class AsyncioSignalRegistrar:
    """Register callbacks on one running asyncio event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add(self, signum: signal.Signals, callback: Callable[[], None]) -> None:
        self._loop.add_signal_handler(signum, callback)

    def remove(self, signum: signal.Signals) -> None:
        self._loop.remove_signal_handler(signum)


@dataclass(slots=True)
class MonitorLifecycle:
    """CLI-owned coordination state without protocol responsibilities."""

    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    start_acknowledged: bool = False
    shutdown_requested: bool = False
    first_signal: signal.Signals | None = None
    active_task: asyncio.Task[None] | None = None
    bluez_restored: bool = False
    diagnostic_recorder: HeartRateDiagnosticRecorder | None = None

    def mark_start_acknowledged(self) -> None:
        self.start_acknowledged = True

    def mark_bluez_restored(self) -> None:
        self.bluez_restored = True

    def request_shutdown(self, signum: signal.Signals) -> None:
        """Request graceful stop first, then task cancellation on repetition."""

        task = self.active_task
        if task is None or task.done():
            return
        if not self.shutdown_requested:
            self.shutdown_requested = True
            self.first_signal = signum
            if self.start_acknowledged:
                self.stop_event.set()
            else:
                task.cancel()
            return
        task.cancel()

    @property
    def signal_exit_code(self) -> int | None:
        if self.first_signal is None:
            return None
        return 128 + int(self.first_signal)


def create_heart_rate_progress(
    lifecycle: MonitorLifecycle,
    stdout: Output,
    stderr: Output,
) -> Callable[[HeartRateProgress, HeartRateReport | None], None]:
    """Map allowlisted monitor progress to product output streams."""

    def emit(event: HeartRateProgress, report: HeartRateReport | None) -> None:
        if event is HeartRateProgress.START_ACKNOWLEDGED:
            lifecycle.mark_start_acknowledged()
            stderr("Heart-rate monitoring started.")
            stderr("Press Ctrl+C to stop.")
        elif event is HeartRateProgress.SAMPLE and report is not None:
            if lifecycle.diagnostic_recorder is None:
                stdout(f"Heart rate: {report.bpm} bpm")
            else:
                sample = lifecycle.diagnostic_recorder.record_sample(report)
                stdout(sample.format_human())

    return emit


async def create_monitor_session(
    discovery_backend: DBusNextManagedObjectsBackend,
    bluez_backend: DBusNextBlueZBackend,
    lifecycle: MonitorLifecycle,
    stdout: Output,
    stderr: Output,
) -> MonitorSession:
    """Compose the existing production monitor infrastructure."""

    handoff, transport = create_controller_handoff_transport(bluez_backend)
    await transport.ensure_available()
    secure_session = ClassicAuthenticationSession(
        BlueZDeviceDiscovery(discovery_backend),
        BlueZPairingStore(),
        handoff,
        BumbleClassicRuntimeFactory(),
    )
    return HeartRateMonitorSession(
        secure_session,
        AAPChannelSession(),
        AAPHandshakeSession(),
        HeartRateMonitorActivationSession(
            progress=create_heart_rate_progress(lifecycle, stdout, stderr)
        ),
        bluez_restored=lifecycle.mark_bluez_restored,
    )


async def run_composed_monitor(
    lifecycle: MonitorLifecycle,
    stdout: Output,
    stderr: Output,
    *,
    discovery_backend_factory: Callable[[], ConnectedBackend] | None = None,
    bluez_backend_factory: Callable[[], ConnectedBackend] | None = None,
    session_factory: SessionFactory | None = None,
) -> None:
    """Run one composed monitor and close each constructed backend once."""

    discovery_factory = discovery_backend_factory or DBusNextManagedObjectsBackend
    bluez_factory = bluez_backend_factory or DBusNextBlueZBackend
    build_session = session_factory or create_monitor_session
    discovery_backend: ConnectedBackend | None = None
    bluez_backend: ConnectedBackend | None = None
    primary_error: BaseException | None = None
    try:
        stderr("Connecting to AirPods...")
        discovery_backend = discovery_factory()
        bluez_backend = bluez_factory()
        await discovery_backend.connect()
        await bluez_backend.connect()
        session = await build_session(
            discovery_backend,
            bluez_backend,
            lifecycle,
            stdout,
            stderr,
        )
        await session.run(lifecycle.stop_event)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_errors: list[Exception] = []
        for backend in (discovery_backend, bluez_backend):
            if backend is not None:
                try:
                    backend.close()
                except Exception as error:
                    cleanup_errors.append(error)
        if cleanup_errors:
            if primary_error is not None:
                primary_error.add_note("a D-Bus backend also failed to close")
            else:
                raise MonitorBackendCleanupError(
                    "a D-Bus backend failed to close"
                ) from cleanup_errors[0]


async def run_with_signal_handlers(
    operation: Awaitable[None],
    lifecycle: MonitorLifecycle,
    registrar: SignalRegistrar,
) -> int:
    """Run one operation with scoped SIGINT and SIGTERM callbacks."""

    task = asyncio.create_task(operation)
    lifecycle.active_task = task
    installed: list[signal.Signals] = []
    try:
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                registrar.add(
                    signum,
                    lambda signum=signum: lifecycle.request_shutdown(signum),
                )
                installed.append(signum)
        except BaseException:
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            raise

        try:
            await task
        except asyncio.CancelledError:
            if lifecycle.first_signal is None:
                raise
        return lifecycle.signal_exit_code or 0
    finally:
        for signum in reversed(installed):
            registrar.remove(signum)
        lifecycle.active_task = None


async def run_live_monitor(
    stdout: Output,
    stderr: Output,
    *,
    diagnostic_recorder: HeartRateDiagnosticRecorder | None = None,
    registrar: SignalRegistrar | None = None,
    operation_factory: OperationFactory | None = None,
) -> int:
    """Run the product monitor under its complete signal lifecycle."""

    lifecycle = MonitorLifecycle(diagnostic_recorder=diagnostic_recorder)
    signal_registrar = registrar or AsyncioSignalRegistrar(
        asyncio.get_running_loop()
    )
    create_operation = operation_factory or (
        lambda state, sample_output, status_output: run_composed_monitor(
            state, sample_output, status_output
        )
    )
    operation = create_operation(lifecycle, stdout, stderr)
    exit_code = await run_with_signal_handlers(
        operation,
        lifecycle,
        signal_registrar,
    )
    stderr("Heart-rate monitoring stopped.")
    if lifecycle.bluez_restored:
        stderr("Bluetooth ownership and BlueZ state restored.")
    elif lifecycle.shutdown_requested and not lifecycle.start_acknowledged:
        stderr("Shutdown completed before heart-rate monitoring started.")
    return exit_code


def _termination_reason(exit_code: int) -> str:
    if exit_code == 0:
        return "completed"
    if exit_code == 130:
        return "sigint"
    if exit_code == 143:
        return "sigterm"
    return "failure"


def _exception_termination_reason(error: BaseException) -> str:
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, HeartRateDiagnosticError):
        return "diagnostic_error"
    return "failure"


async def _run_diagnostic_monitor(
    stdout: Output,
    stderr: Output,
    output_path: str | Path | None,
    live_runner: LiveRunner,
    recorder_factory: RecorderFactory,
) -> int:
    recorder = recorder_factory(output_path)
    primary_error: BaseException | None = None
    try:
        recorder.start_session()
        try:
            exit_code = await live_runner(
                stdout,
                stderr,
                diagnostic_recorder=recorder,
            )
        except BaseException as error:
            try:
                recorder.stop_session(_exception_termination_reason(error))
            except HeartRateDiagnosticError:
                error.add_note("diagnostic session finalization also failed")
            raise
        recorder.stop_session(_termination_reason(exit_code))
        return exit_code
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            recorder.close()
        except HeartRateDiagnosticError:
            if primary_error is not None:
                primary_error.add_note("diagnostic output closure also failed")
            else:
                raise


async def run_monitor_command(
    *,
    dry_run: bool,
    diagnostic: bool = False,
    output_path: str | Path | None = None,
    stdout: Output,
    stderr: Output,
    live_runner: LiveRunner = run_live_monitor,
    recorder_factory: RecorderFactory = create_diagnostic_recorder,
) -> int:
    """Run a dry plan or map one live monitor outcome to a safe exit code."""

    if dry_run:
        stderr("DRY RUN: no Bluetooth state will be changed.")
        stderr("Planned operations:")
        stderr("  1. Discover one paired AirPods candidate.")
        stderr("  2. Read its existing local Classic credentials in memory.")
        stderr("  3. Hand the controller from BlueZ to Bumble temporarily.")
        stderr("  4. Connect securely and start continuous heart-rate monitoring.")
        stderr("  5. Stop on SIGINT or SIGTERM and restore BlueZ ownership.")
        if diagnostic:
            destination = (
                "JSONL file" if output_path is not None else "standard output"
            )
            stderr(f"Diagnostic evidence destination: {destination}.")
        stderr("No reconnect, retry, or arbitrary protocol command is used.")
        return 0

    try:
        if diagnostic:
            return await _run_diagnostic_monitor(
                stdout,
                stderr,
                output_path,
                live_runner,
                recorder_factory,
            )
        return await live_runner(stdout, stderr)
    except DiagnosticOutputOpenError:
        stderr("Error: the diagnostic output file could not be opened.")
    except HeartRateDiagnosticError:
        stderr("Error: diagnostic capture failed; cleanup was attempted.")
    except NoAirPodsCandidatesError:
        stderr("Error: no paired AirPods candidate was found.")
    except MultipleAirPodsCandidatesError:
        stderr("Error: multiple paired AirPods candidates require selection.")
    except PairingStoreError:
        stderr("Error: existing local Classic credentials could not be loaded.")
    except SDPCompatibilityError:
        stderr("Error: the local SDP compatibility profile could not be prepared.")
    except AAPHandshakeError:
        stderr("Error: the AAP handshake or descriptor phase failed.")
    except AAPChannelError:
        stderr("Error: the AAP channel failed.")
    except HeartRateSessionError:
        stderr("Error: the heart-rate session failed; cleanup was attempted.")
    except AdapterRestoreError:
        stderr("Error: BlueZ adapter restoration failed.")
    except HandoffError:
        stderr("Error: controller handoff failed; cleanup was attempted.")
    except ClassicAuthenticationError:
        stderr("Error: the Classic security session failed.")
    except asyncio.CancelledError:
        raise
    except Exception:
        stderr("Error: an unexpected monitor failure occurred.")
    return 1
