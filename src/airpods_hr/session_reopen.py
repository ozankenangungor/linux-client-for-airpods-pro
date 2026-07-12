"""Private same-ACL, new-AAP-channel characterization support.

This module composes the frozen production session core without changing its lifecycle.
It is deliberately absent from :mod:`airpods_hr` exports.
"""

from __future__ import annotations

import asyncio
import airpods_hr._airpods_aap_core as _rust_core
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from airpods_hr.aap import (
    AAPDescriptorObservationTimeoutError,
    AAPHandshakeResult,
    AAPHandshakeSession,
    AAPHandshakeTimeoutError,
    HandshakeObservation,
)
from airpods_hr.bluez_coexistence import (
    DEFAULT_DBUS_TIMEOUT,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_L2CAP_CONNECT_TIMEOUT,
    BlueZCompatibilityRegistration,
    CoexistenceTransport,
    DBusNextBlueZCoexistenceClient,
    KernelL2CAPTransport,
)
from airpods_hr.production_session import (
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    InternalProductionSession,
)


class SessionReopenResultCategory(StrEnum):
    BOTH_SESSIONS_PASS = "BOTH_SESSIONS_PASS"
    SESSION_2_EXACT_ACK_DESCRIPTOR_TIMEOUT = (
        "SESSION_2_EXACT_ACK_DESCRIPTOR_TIMEOUT"
    )
    SESSION_2_AAP_ACK_FAILURE = "SESSION_2_AAP_ACK_FAILURE"
    SESSION_2_TRANSPORT_FAILURE = "SESSION_2_TRANSPORT_FAILURE"
    BLUEZ_STATE_CHANGED = "BLUEZ_STATE_CHANGED"
    OTHER_FAILURE = "OTHER_FAILURE"


class Session1Mode(StrEnum):
    HR_CYCLE = "hr-cycle"
    DESCRIPTOR_ONLY = "descriptor-only"


@dataclass(frozen=True, slots=True)
class BlueZReopenCheckpoint:
    label: str
    bluez_reachable: bool
    adapter_powered: bool
    device_connected: bool

    @property
    def invariant_holds(self) -> bool:
        return _rust_core.runtime_reopen_checkpoint_holds(
            self.bluez_reachable, self.adapter_powered, self.device_connected
        )


@dataclass(frozen=True, slots=True)
class SessionReopenCounters:
    session_1_mode: Session1Mode
    session_objects_created: int
    transport_opens: int
    transport_closes: int
    descriptor_handshakes_attempted: int
    descriptor_handshakes_completed: int
    exact_aap_acks: int
    hr_activations: int
    hr_stops: int
    hr_activations_session_1: int
    hr_stops_session_1: int
    reports_received_session_1: int
    reports_received_session_2: int


@dataclass(frozen=True, slots=True)
class SessionReopenResult:
    category: SessionReopenResultCategory
    counters: SessionReopenCounters
    checkpoints: tuple[BlueZReopenCheckpoint, ...]
    session_2_handshake_observation: HandshakeObservation | None = None
    failure_type: str | None = None


class ReopenSession(Protocol):
    @property
    def counters(self) -> Any: ...

    async def open(self) -> None: ...

    async def start(self) -> None: ...

    async def receive_report(self, timeout: float) -> Any: ...

    async def stop(self) -> None: ...

    async def close(self) -> None: ...


class ReopenCheckpointObserver(Protocol):
    async def open(self) -> BlueZReopenCheckpoint: ...

    async def checkpoint(self, label: str) -> BlueZReopenCheckpoint: ...

    def close(self) -> None: ...


class _ObservedHandshakeSession:
    """Record canonical safe observations without changing acceptance."""

    def __init__(self, delegate: AAPHandshakeSession) -> None:
        self._delegate = delegate
        self._counts = _rust_core.ReopenObservationCounters()
        self.observation: HandshakeObservation | None = None
        self.error: BaseException | None = None

    @property
    def attempts(self) -> int:
        return self._counts.attempts

    @property
    def completed(self) -> int:
        return self._counts.completed

    async def run_collected(
        self, transport: CoexistenceTransport
    ) -> AAPHandshakeResult:
        self._counts.handshake_attempt()
        try:
            result = await self._delegate.run_collected(transport)
        except (
            AAPDescriptorObservationTimeoutError,
            AAPHandshakeTimeoutError,
        ) as error:
            self.error = error
            self.observation = error.observation
            raise
        except BaseException as error:
            self.error = error
            raise
        self._counts.handshake_complete()
        self.observation = result.observation
        return result


class _TrackedTransport:
    """Count ownership operations while delegating the proven transport."""

    def __init__(self, delegate: CoexistenceTransport) -> None:
        self._delegate = delegate
        self._counts = _rust_core.ReopenObservationCounters()

    @property
    def open_calls(self) -> int:
        return self._counts.open_calls

    @property
    def close_calls(self) -> int:
        return self._counts.close_calls

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def open(self, local_address: str, remote_address: str) -> None:
        await self._delegate.open(local_address, remote_address)
        self._counts.transport_open()

    def close(self) -> None:
        self._counts.transport_close()
        self._delegate.close()

    def collect(self) -> AbstractAsyncContextManager[Any]:
        return self._delegate.collect()

    def send_handshake_request(self) -> None:
        self._delegate.send_handshake_request()

    def send_heart_rate_command(self, command: Any) -> None:
        self._delegate.send_heart_rate_command(command)

    async def receive(self, timeout: float) -> bytes:
        return await self._delegate.receive(timeout)


@dataclass(slots=True)
class ReopenSessionBundle:
    session: ReopenSession
    transport: Any
    handshake: Any


class BlueZReopenCheckpointObserver:
    """Observe one selected BlueZ device without changing its state."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        timeout: float = DEFAULT_DBUS_TIMEOUT,
    ) -> None:
        self._client = client or DBusNextBlueZCoexistenceClient()
        self._timeout = timeout
        self._candidate: Any | None = None

    async def open(self) -> BlueZReopenCheckpoint:
        await asyncio.wait_for(self._client.connect(), timeout=self._timeout)
        state = await asyncio.wait_for(
            self._client.preflight(require_connected=True), timeout=self._timeout
        )
        self._candidate = state.candidate
        return self._from_state("before_session_1", state)

    async def checkpoint(self, label: str) -> BlueZReopenCheckpoint:
        if self._candidate is None:
            raise RuntimeError("checkpoint observer is not open")
        state = await asyncio.wait_for(
            self._client.snapshot(self._candidate), timeout=self._timeout
        )
        return self._from_state(label, state)

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _from_state(label: str, state: Any) -> BlueZReopenCheckpoint:
        return BlueZReopenCheckpoint(
            label=label,
            bluez_reachable=True,
            adapter_powered=bool(state.adapter_powered),
            device_connected=bool(state.device_connected),
        )


def create_reopen_session_bundle(
    *,
    descriptor_timeout: float = 30.0,
    dbus_timeout: float = DEFAULT_DBUS_TIMEOUT,
    connect_timeout: float = DEFAULT_L2CAP_CONNECT_TIMEOUT,
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    start_timeout: float = DEFAULT_START_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    output: Callable[[str], None] = print,
) -> ReopenSessionBundle:
    """Create one entirely fresh production-session dependency graph."""

    client = DBusNextBlueZCoexistenceClient()
    registration = BlueZCompatibilityRegistration(
        client, operation_timeout=dbus_timeout
    )
    transport = _TrackedTransport(
        KernelL2CAPTransport(connect_timeout=connect_timeout)
    )
    handshake = _ObservedHandshakeSession(
        AAPHandshakeSession(
            ack_timeout=handshake_timeout,
            descriptor_timeout=descriptor_timeout,
        )
    )
    session = InternalProductionSession(
        client,
        registration,
        transport,
        handshake,
        dbus_timeout=dbus_timeout,
        start_timeout=start_timeout,
        stop_timeout=stop_timeout,
        output=output,
    )
    return ReopenSessionBundle(session, transport, handshake)
