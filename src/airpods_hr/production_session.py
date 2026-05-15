"""Private persistent BlueZ/kernel heart-rate session core.

This module is deliberately not exported from :mod:`airpods_hr`.  It validates a long-lived AAP lifecycle before any public API is fixed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any

from dbus_next.errors import DBusError

from airpods_hr import _airpods_aap_core as _native

from airpods_hr.aap import (
    AAPDescriptorObservationTimeoutError,
    AAPHandshakeResult,
    AAPHandshakeSession,
    AAPHandshakeTimeoutError,
)
from airpods_hr.bluez_coexistence import (
    DEFAULT_DBUS_TIMEOUT,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_L2CAP_CONNECT_TIMEOUT,
    BlueZCoexistenceState,
    BlueZCompatibilityRegistration,
    BlueZStateClient,
    CoexistenceCategory,
    CoexistenceFailure,
    CoexistenceTransport,
    CompatibilityRegistration,
    DBusNextBlueZCoexistenceClient,
    KernelL2CAPTransport,
)
from airpods_hr.discovery import (
    DeviceDiscoveryUnavailableError,
    NoAirPodsCandidatesError,
)
from airpods_hr.heart_rate_session import (
    HeartRateBootstrapAckTimeoutError,
    HeartRateConnectAckTimeoutError,
    HeartRateMonitorActivationSession,
    HeartRateMonitorSessionResult,
    HeartRateNoSamplesError,
    HeartRateProgress,
    HeartRateStartAckTimeoutError,
)
from airpods_hr.heartrate import HeartRateReport
from airpods_hr.protocol import HeartRateCommand


DEFAULT_REPORT_TIMEOUT = 5.0
DEFAULT_START_TIMEOUT = 15.0
DEFAULT_STOP_TIMEOUT = 5.0
EXPECTED_LOCAL_RX_IMTU = 2048
_DESCRIPTOR_HANDSHAKE_PHASE = "descriptor_handshake"


class ProductionSessionState(StrEnum):
    CLOSED = "closed"
    OPENING = "opening"
    READY = "ready"
    STARTING = "starting"
    STREAMING = "streaming"
    STOPPING = "stopping"
    FAILED = "failed"


# Numeric identities are private FFI keys, not another lifecycle policy table.
_NATIVE_STATES = tuple(ProductionSessionState)


class _ProductionOperation(IntEnum):
    OPEN = 0
    START = 1
    RECEIVE_REPORT = 2
    STOP = 3
    CLOSE = 4


class _ProductionEvent(IntEnum):
    OPEN_BEGIN = 0
    OPEN_SUCCEEDED = 1
    OPERATION_FAILED = 2
    START_BEGIN = 3
    START_SUCCEEDED = 4
    RECEIVE_ACTIVATION_FAILED = 5
    STOP_BEGIN = 6
    STOP_SUCCEEDED = 7
    CLOSE_FINALIZED = 8


class ProductionSessionCategory(StrEnum):
    INVALID_STATE = "invalid_state"
    PREFLIGHT_FAILED = "preflight_failed"
    REGISTRATION_FAILED = "registration_failed"
    TRANSPORT_FAILED = "transport_failed"
    AAP_ACK_TIMEOUT = "aap_ack_timeout"
    AAP_DESCRIPTOR_TIMEOUT = "aap_descriptor_timeout"
    DESCRIPTOR_HANDSHAKE_FAILED = "descriptor_handshake_failed"
    ACTIVATION_FAILED = "activation_failed"
    RECEIVE_FAILED = "receive_failed"
    STOP_FAILED = "stop_failed"
    CLEANUP_FAILED = "cleanup_failed"


class ProductionSessionError(RuntimeError):
    """Typed internal failure containing no private Bluetooth material."""

    def __init__(
        self,
        category: ProductionSessionCategory,
        phase: str,
        detail: str | None = None,
        *,
        recoverable: bool = False,
    ) -> None:
        self.category = category
        self.phase = phase
        self.detail = detail
        self.recoverable = recoverable
        message = f"{category.value} at {phase}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class ProductionSessionStateError(ProductionSessionError):
    def __init__(self, operation: str, state: ProductionSessionState) -> None:
        super().__init__(
            ProductionSessionCategory.INVALID_STATE,
            operation,
            f"state={state.value}",
        )


# Private FFI identities follow the existing CoexistenceCategory declaration order.
_NATIVE_COEXISTENCE_CATEGORIES = tuple(CoexistenceCategory)

_RECOVERABLE_SESSION_EXCEPTIONS = (
    AAPHandshakeTimeoutError,
    AAPDescriptorObservationTimeoutError,
    HeartRateBootstrapAckTimeoutError,
    HeartRateConnectAckTimeoutError,
    HeartRateStartAckTimeoutError,
    HeartRateNoSamplesError,
    NoAirPodsCandidatesError,
    DeviceDiscoveryUnavailableError,
    ConnectionError,
    TimeoutError,
)


def _nested_control_flow(error: Exception) -> BaseException | None:
    """Find control flow hidden by a dependency wrapper, without cycling."""

    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if not isinstance(current, Exception):
            return current
        current = current.__cause__ or current.__context__
    return None


def _is_recoverable_session_error(error: BaseException) -> bool:
    """Classify only explicit, expected operational session failures."""

    if isinstance(error, ProductionSessionError):
        return error.recoverable
    if isinstance(error, _RECOVERABLE_SESSION_EXCEPTIONS):
        return True
    if not isinstance(error, CoexistenceFailure):
        return False
    # Ask the core before inspecting the cause: terminal categories historically
    # short-circuit without traversing even a nested coexistence failure.
    try:
        category_id = _NATIVE_COEXISTENCE_CATEGORIES.index(error.category)
    except ValueError:
        # No neutral identity exists; the parent also treated unknown categories
        # as terminal without inspecting their cause.
        return False
    if not _native.classify_coexistence_recovery(category_id, 0, False, None, None):
        return False

    cause = error.__cause__
    if cause is None:
        return True
    if isinstance(cause, CoexistenceFailure):
        cause_kind, nested_recoverable = 1, _is_recoverable_session_error(cause)
        errno_value, dbus_name = None, None
    elif isinstance(cause, _RECOVERABLE_SESSION_EXCEPTIONS):
        cause_kind, nested_recoverable, errno_value, dbus_name = 2, False, None, None
    elif isinstance(cause, OSError):
        cause_kind, nested_recoverable, dbus_name = 3, False, None
        errno_value = cause.errno
        if isinstance(errno_value, int) and not isinstance(errno_value, bool):
            errno_value = int(errno_value)
        if type(errno_value) is not int or not -(1 << 31) <= errno_value < (1 << 31):
            errno_value = None
    elif isinstance(cause, DBusError):
        cause_kind, nested_recoverable, errno_value = 4, False, None
        dbus_name = cause.type if isinstance(cause.type, str) else None
    else:
        cause_kind, nested_recoverable, errno_value, dbus_name = 5, False, None, None
    return _native.classify_coexistence_recovery(
        category_id, cause_kind, nested_recoverable, errno_value, dbus_name
    )


def _translate_session_error(
    category: ProductionSessionCategory,
    phase: str,
    error: Exception,
) -> ProductionSessionError:
    return ProductionSessionError(
        category,
        phase,
        type(error).__name__,
        recoverable=_is_recoverable_session_error(error),
    )


@dataclass(frozen=True, slots=True)
class ProductionSessionCounters:
    transport_opens: int
    descriptor_handshakes: int
    hr_activations: int
    hr_stops: int
    reports_received: int


class _ActivationTransportView:
    """Rebase the canonical per-activation payload counter on one channel."""

    def __init__(self, transport: CoexistenceTransport) -> None:
        self._transport = transport
        self._starting_payload_count = transport.application_payloads_sent

    @property
    def application_payloads_sent(self) -> int:
        return 1 + (
            self._transport.application_payloads_sent
            - self._starting_payload_count
        )

    @property
    def pending_receive_frames(self) -> int:
        return self._transport.pending_receive_frames

    def send_heart_rate_command(self, command: HeartRateCommand) -> None:
        self._transport.send_heart_rate_command(command)

    async def receive(self, timeout: float) -> bytes:
        return await self._transport.receive(timeout)


MonitorFactory = Callable[
    [Callable[[HeartRateProgress, HeartRateReport | None], None]],
    HeartRateMonitorActivationSession,
]


class InternalProductionSession:
    """One AAP connection with repeatable canonical HR activation cycles."""

    def __init__(
        self,
        client: BlueZStateClient,
        registration: CompatibilityRegistration,
        transport: CoexistenceTransport,
        handshake: AAPHandshakeSession,
        *,
        dbus_timeout: float = DEFAULT_DBUS_TIMEOUT,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        stop_timeout: float = DEFAULT_STOP_TIMEOUT,
        monitor_factory: MonitorFactory | None = None,
        output: Callable[[str], None] = print,
    ) -> None:
        if min(dbus_timeout, start_timeout, stop_timeout) <= 0:
            raise ValueError("production session timeouts must be positive")
        self._client = client
        self._registration = registration
        self._transport = transport
        self._handshake_session = handshake
        self._dbus_timeout = dbus_timeout
        self._start_timeout = start_timeout
        self._stop_timeout = stop_timeout
        self._monitor_factory = monitor_factory or self._make_monitor
        self._output = output

        self.state = ProductionSessionState.CLOSED
        self._lifecycle_lock = asyncio.Lock()
        self._receive_in_progress = False
        self._open_attempted = False
        self._client_connected = False
        self._registration_owned = False
        self._transport_owned = False
        self._collection_context: AbstractAsyncContextManager[Any] | None = None
        self._collection_entered = False
        # Some release APIs become no-ops after raising, so later retries cannot
        # retroactively prove that the first release completed.
        self._release_unproven = False
        self._initial_state: BlueZCoexistenceState | None = None
        self._handshake: AAPHandshakeResult | None = None
        self._activation_task: asyncio.Task[HeartRateMonitorSessionResult] | None = None
        self._activation_stop: asyncio.Event | None = None
        self._activation_started: asyncio.Future[None] | None = None
        self._reports: asyncio.Queue[HeartRateReport] | None = None
        self._transport_opens = 0
        self._descriptor_handshakes = 0
        self._hr_activations = 0
        self._hr_stops = 0
        self._reports_received = 0

    @property
    def counters(self) -> ProductionSessionCounters:
        return ProductionSessionCounters(
            transport_opens=self._transport_opens,
            descriptor_handshakes=self._descriptor_handshakes,
            hr_activations=self._hr_activations,
            hr_stops=self._hr_stops,
            reports_received=self._reports_received,
        )

    @property
    def cleanup_complete(self) -> bool:
        """Return whether every potentially owned resource is proven released."""

        return (
            not self._release_unproven
            and self._activation_task is None
            and not self._collection_entered
            and self._collection_context is None
            and not self._transport_owned
            and not self._registration_owned
            and not self._client_connected
            and self._client.cleanup_complete
            and self._registration.cleanup_complete
            and self._transport.cleanup_complete
        )

    def _require_operation(self, operation: str) -> None:
        try:
            _native.production_operation(
                _NATIVE_STATES.index(self.state),
                _ProductionOperation[operation.upper()].value,
            )
        except ValueError as error:
            if error.args == (9,):
                raise ProductionSessionStateError(operation, self.state) from error
            raise ProductionSessionError(
                ProductionSessionCategory.INVALID_STATE,
                operation,
                "native lifecycle operation failed",
            ) from error

    def _advance(
        self, event: _ProductionEvent, *, cleanup_complete: bool = False
    ) -> None:
        try:
            next_state = _native.production_transition(
                _NATIVE_STATES.index(self.state), event.value, cleanup_complete
            )
            self.state = _NATIVE_STATES[next_state]
        except (ValueError, IndexError, TypeError) as error:
            raise ProductionSessionError(
                ProductionSessionCategory.INVALID_STATE,
                "lifecycle",
                "native lifecycle transition failed",
            ) from error

    async def open(self) -> None:
        async with self._lifecycle_lock:
            self._require_operation("open")
            if self._open_attempted:
                raise ProductionSessionError(
                    ProductionSessionCategory.INVALID_STATE,
                    "open",
                    "session objects are single-use after close or open failure",
                )
            self._open_attempted = True
            self._advance(_ProductionEvent.OPEN_BEGIN)
            phase = "bluez_connect"
            try:
                self._output("OPEN SESSION: BlueZ preflight")
                self._client_connected = True
                await asyncio.wait_for(
                    self._client.connect(), timeout=self._dbus_timeout
                )
                phase = "preflight"
                state = await asyncio.wait_for(
                    self._client.preflight(require_connected=True),
                    timeout=self._dbus_timeout,
                )
                self._initial_state = state
                self._require_connected(state, phase)

                phase = "compatibility_registration"
                self._registration_owned = True
                await self._registration.register(state)
                await self._checkpoint("after_profile_registration")

                phase = "transport_open"
                self._transport_owned = True
                await self._transport.open(
                    str(state.candidate.adapter_address),
                    str(state.candidate.address),
                )
                self._transport_opens += 1
                local_rx = self._transport.local_rx_observation
                if (
                    not local_rx.verified
                    or local_rx.after_imtu != EXPECTED_LOCAL_RX_IMTU
                ):
                    raise ProductionSessionError(
                        ProductionSessionCategory.TRANSPORT_FAILED,
                        "local_rx_imtu",
                        "kernel local receive MTU verification failed",
                    )
                await self._checkpoint("after_transport_open")

                phase = "transport_collection"
                self._collection_context = self._transport.collect()
                await self._collection_context.__aenter__()
                self._collection_entered = True

                phase = _DESCRIPTOR_HANDSHAKE_PHASE
                handshake = await self._handshake_session.run_collected(
                    self._transport
                )
                if not handshake.evidence.required:
                    raise ProductionSessionError(
                        ProductionSessionCategory.DESCRIPTOR_HANDSHAKE_FAILED,
                        phase,
                        "canonical descriptor evidence is incomplete",
                    )
                self._handshake = handshake
                self._descriptor_handshakes += 1
                await self._checkpoint("after_descriptor_handshake")
            except asyncio.CancelledError as error:
                await self._record_failed_operation(error)
                raise
            except Exception as error:
                await self._record_failed_operation(error)
                control_flow = _nested_control_flow(error)
                if control_flow is not None:
                    raise control_flow
                if isinstance(error, ProductionSessionError):
                    raise
                category = self._open_category(phase)
                if (
                    phase == _DESCRIPTOR_HANDSHAKE_PHASE
                    and isinstance(error, AAPHandshakeTimeoutError)
                ):
                    category = ProductionSessionCategory.AAP_ACK_TIMEOUT
                elif (
                    phase == _DESCRIPTOR_HANDSHAKE_PHASE
                    and isinstance(error, AAPDescriptorObservationTimeoutError)
                    and error.observation.ack_observed is True
                ):
                    category = ProductionSessionCategory.AAP_DESCRIPTOR_TIMEOUT
                raise _translate_session_error(category, phase, error) from error

            self._advance(_ProductionEvent.OPEN_SUCCEEDED)
            self._output("SESSION READY: descriptor handshake complete")

    async def start(self) -> None:
        async with self._lifecycle_lock:
            self._require_operation("start")
            assert self._handshake is not None
            self._advance(_ProductionEvent.START_BEGIN)
            loop = asyncio.get_running_loop()
            self._activation_stop = asyncio.Event()
            self._activation_started = loop.create_future()
            self._reports = asyncio.Queue()

            def progress(
                event: HeartRateProgress, report: HeartRateReport | None
            ) -> None:
                if event is HeartRateProgress.START_ACKNOWLEDGED:
                    if not self._activation_started.done():
                        self._activation_started.set_result(None)
                elif event is HeartRateProgress.SAMPLE and report is not None:
                    assert self._reports is not None
                    self._reports.put_nowait(report)

            monitor = self._monitor_factory(progress)
            transport_view = _ActivationTransportView(self._transport)
            self._activation_task = asyncio.create_task(
                monitor.run_collected(
                    transport_view,
                    self._handshake,
                    self._activation_stop,
                ),
                name="airpods-hr-production-activation",
            )
            try:
                await self._wait_for_activation_start()
            except asyncio.CancelledError as error:
                await self._record_failed_operation(error)
                raise
            except Exception as error:
                await self._record_failed_operation(error)
                control_flow = _nested_control_flow(error)
                if control_flow is not None:
                    raise control_flow
                if isinstance(error, ProductionSessionError):
                    raise
                raise _translate_session_error(
                    ProductionSessionCategory.ACTIVATION_FAILED,
                    "start",
                    error,
                ) from error
            self._hr_activations += 1
            self._advance(_ProductionEvent.START_SUCCEEDED)

    async def receive_report(
        self, timeout: float = DEFAULT_REPORT_TIMEOUT
    ) -> HeartRateReport:
        if timeout <= 0:
            raise ValueError("report timeout must be positive")
        self._require_operation("receive_report")
        if self._receive_in_progress:
            raise ProductionSessionError(
                ProductionSessionCategory.INVALID_STATE,
                "receive_report",
                "another report consumer is active",
            )
        assert self._reports is not None
        assert self._activation_task is not None
        self._receive_in_progress = True
        get_task = asyncio.create_task(self._reports.get())
        try:
            done, _ = await asyncio.wait(
                {get_task, self._activation_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if get_task in done:
                report = get_task.result()
                self._reports_received += 1
                return report
            if self._activation_task in done:
                try:
                    self._activation_task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    control_flow = _nested_control_flow(error)
                    if control_flow is not None:
                        raise control_flow
                    self._advance(_ProductionEvent.RECEIVE_ACTIVATION_FAILED)
                    raise _translate_session_error(
                        ProductionSessionCategory.RECEIVE_FAILED,
                        "receive_report",
                        error,
                    ) from error
                raise ProductionSessionError(
                    ProductionSessionCategory.RECEIVE_FAILED,
                    "receive_report",
                    "activation ended before a report arrived",
                    recoverable=True,
                )
            raise ProductionSessionError(
                ProductionSessionCategory.RECEIVE_FAILED,
                "receive_report",
                "no heart-rate report arrived before timeout",
                recoverable=True,
            )
        finally:
            if not get_task.done():
                get_task.cancel()
                try:
                    await get_task
                except asyncio.CancelledError:
                    pass
            self._receive_in_progress = False

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._require_operation("stop")
            try:
                await self._stop_locked()
            except asyncio.CancelledError as error:
                await self._record_failed_operation(error)
                raise
            except Exception as error:
                await self._record_failed_operation(error)
                control_flow = _nested_control_flow(error)
                if control_flow is not None:
                    raise control_flow
                if isinstance(error, ProductionSessionError):
                    raise
                raise _translate_session_error(
                    ProductionSessionCategory.STOP_FAILED,
                    "stop",
                    error,
                ) from error

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._require_operation("close")
            if self.state is ProductionSessionState.CLOSED:
                return
            errors: list[BaseException] = []
            if self.state is ProductionSessionState.STREAMING:
                try:
                    await self._stop_locked()
                except BaseException as error:
                    errors.append(error)
            elif self._activation_task is not None:
                try:
                    await self._abort_activation()
                except BaseException as error:
                    errors.append(error)
            errors.extend(await self._cleanup_resources())
            self._advance(
                _ProductionEvent.CLOSE_FINALIZED,
                cleanup_complete=self.cleanup_complete,
            )
            cancellation = next(
                (
                    error
                    for error in errors
                    if isinstance(error, asyncio.CancelledError)
                ),
                None,
            )
            if cancellation is not None:
                raise cancellation
            if errors:
                raise ProductionSessionError(
                    ProductionSessionCategory.CLEANUP_FAILED,
                    "close",
                    type(errors[-1]).__name__,
                ) from errors[-1]
            if not self.cleanup_complete:
                raise ProductionSessionError(
                    ProductionSessionCategory.CLEANUP_FAILED,
                    "close",
                    "resource release remains unproven",
                )

    async def _wait_for_activation_start(self) -> None:
        assert self._activation_started is not None
        assert self._activation_task is not None
        done, _ = await asyncio.wait(
            {self._activation_started, self._activation_task},
            timeout=self._start_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise TimeoutError("HR activation acknowledgement timed out")
        if self._activation_task in done:
            self._activation_task.result()
            raise RuntimeError("HR activation ended before streaming")
        self._activation_started.result()
        if self._activation_task.done():
            self._activation_task.result()

    async def _stop_locked(self) -> None:
        assert self._activation_stop is not None
        assert self._activation_task is not None
        self._advance(_ProductionEvent.STOP_BEGIN)
        self._activation_stop.set()
        try:
            result = await asyncio.wait_for(
                asyncio.shield(self._activation_task),
                timeout=self._stop_timeout,
            )
        except TimeoutError as error:
            await self._cancel_activation_task()
            raise ProductionSessionError(
                ProductionSessionCategory.STOP_FAILED,
                "stop",
                "canonical HR cleanup timed out",
                recoverable=True,
            ) from error
        if not result.stop_acknowledged:
            raise ProductionSessionError(
                ProductionSessionCategory.STOP_FAILED,
                "stop",
                "canonical STOP_HR acknowledgement was not observed",
                recoverable=True,
            )
        self._hr_stops += 1
        self._clear_activation()
        self._advance(_ProductionEvent.STOP_SUCCEEDED)

    async def _abort_activation(self) -> None:
        if self._activation_stop is not None:
            self._activation_stop.set()
        if self._activation_task is not None and not self._activation_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._activation_task),
                    timeout=self._stop_timeout,
                )
            except asyncio.CancelledError:
                await self._cancel_activation_task()
                raise
            except BaseException:
                await self._cancel_activation_task()
        if self._activation_task is not None and not self._activation_task.done():
            raise RuntimeError("activation task cleanup did not complete")
        if self._activation_task is not None and self._activation_task.done():
            try:
                result = self._activation_task.result()
                if result.stop_acknowledged:
                    self._hr_stops += 1
            except BaseException:
                pass
        self._clear_activation()

    async def _cancel_activation_task(self) -> None:
        assert self._activation_task is not None
        self._activation_task.cancel()
        try:
            await asyncio.wait_for(
                self._activation_task,
                timeout=self._stop_timeout,
            )
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except BaseException:
            pass

    def _clear_activation(self) -> None:
        self._activation_task = None
        self._activation_stop = None
        self._activation_started = None
        self._reports = None

    async def _cleanup_resources(self) -> list[BaseException]:
        errors: list[BaseException] = []
        release_uncertain = False
        if self._activation_task is not None:
            try:
                await self._abort_activation()
            except BaseException as error:
                errors.append(error)
                release_uncertain = True
        if self._collection_entered and self._collection_context is not None:
            try:
                await self._collection_context.__aexit__(None, None, None)
            except BaseException as error:
                errors.append(error)
                release_uncertain = True
            else:
                self._collection_entered = False
                self._collection_context = None
        elif not self._collection_entered:
            self._collection_context = None
        if self._transport_owned:
            try:
                self._transport.close()
            except BaseException as error:
                errors.append(error)
                release_uncertain = True
            else:
                if self._transport.cleanup_complete:
                    self._transport_owned = False
                else:
                    errors.append(
                        RuntimeError("transport release remains unproven")
                    )
                    release_uncertain = True
        if self._registration_owned:
            try:
                await self._registration.unregister()
            except BaseException as error:
                errors.append(error)
                release_uncertain = True
            else:
                if self._registration.cleanup_complete:
                    self._registration_owned = False
                else:
                    errors.append(
                        RuntimeError("registration release remains unproven")
                    )
                    release_uncertain = True
        if self._client_connected and self._initial_state is not None:
            try:
                await self._checkpoint("after_cleanup")
            except BaseException as error:
                errors.append(error)
        if self._client_connected:
            try:
                self._client.close()
            except BaseException as error:
                errors.append(error)
                release_uncertain = True
            else:
                if self._client.cleanup_complete:
                    self._client_connected = False
                else:
                    errors.append(
                        RuntimeError("BlueZ client release remains unproven")
                    )
                    release_uncertain = True
        if release_uncertain:
            self._release_unproven = True
        return errors

    async def _record_failed_operation(self, error: BaseException) -> None:
        cleanup_errors = await self._cleanup_resources()
        self._advance(_ProductionEvent.OPERATION_FAILED)
        self._annotate_cleanup(error, cleanup_errors)
        cancellation = next(
            (
                cleanup_error
                for cleanup_error in cleanup_errors
                if isinstance(cleanup_error, asyncio.CancelledError)
            ),
            None,
        )
        if cancellation is not None:
            raise cancellation

    async def _checkpoint(self, phase: str) -> BlueZCoexistenceState:
        assert self._initial_state is not None
        current = await asyncio.wait_for(
            self._client.snapshot(self._initial_state.candidate),
            timeout=self._dbus_timeout,
        )
        self._require_connected(current, phase)
        self._output(
            f"{phase}: BlueZ reachable=yes, adapter powered=yes, "
            "Device1.Connected=true"
        )
        return current

    @staticmethod
    def _require_connected(state: BlueZCoexistenceState, phase: str) -> None:
        if not state.adapter_powered or not state.device_connected:
            raise ProductionSessionError(
                ProductionSessionCategory.PREFLIGHT_FAILED,
                phase,
                "BlueZ adapter or device connection invariant failed",
                recoverable=True,
            )

    @staticmethod
    def _open_category(phase: str) -> ProductionSessionCategory:
        if phase in {"bluez_connect", "preflight"}:
            return ProductionSessionCategory.PREFLIGHT_FAILED
        if phase == "compatibility_registration":
            return ProductionSessionCategory.REGISTRATION_FAILED
        if phase in {"transport_open", "transport_collection"}:
            return ProductionSessionCategory.TRANSPORT_FAILED
        return ProductionSessionCategory.DESCRIPTOR_HANDSHAKE_FAILED

    @staticmethod
    def _annotate_cleanup(
        primary: BaseException, cleanup_errors: list[BaseException]
    ) -> None:
        if cleanup_errors:
            primary.add_note("production session cleanup also reported an error")

    @staticmethod
    def _make_monitor(
        progress: Callable[[HeartRateProgress, HeartRateReport | None], None],
    ) -> HeartRateMonitorActivationSession:
        return HeartRateMonitorActivationSession(progress=progress)


def create_production_session(
    *,
    descriptor_timeout: float = 30.0,
    dbus_timeout: float = DEFAULT_DBUS_TIMEOUT,
    connect_timeout: float = DEFAULT_L2CAP_CONNECT_TIMEOUT,
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    start_timeout: float = DEFAULT_START_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    output: Callable[[str], None] = print,
) -> InternalProductionSession:
    """Compose the private core from the proven production components."""

    client = DBusNextBlueZCoexistenceClient()
    registration = BlueZCompatibilityRegistration(
        client, operation_timeout=dbus_timeout
    )
    transport = KernelL2CAPTransport(connect_timeout=connect_timeout)
    handshake = AAPHandshakeSession(
        ack_timeout=handshake_timeout,
        descriptor_timeout=descriptor_timeout,
    )
    return InternalProductionSession(
        client,
        registration,
        transport,
        handshake,
        dbus_timeout=dbus_timeout,
        start_timeout=start_timeout,
        stop_timeout=stop_timeout,
        output=output,
    )
