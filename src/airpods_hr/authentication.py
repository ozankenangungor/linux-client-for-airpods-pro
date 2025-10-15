"""Safe orchestration for a Classic authentication-only Bumble session."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager

from enum import StrEnum
from typing import Protocol

from airpods_hr.address import BluetoothAddress
from airpods_hr.bluetooth import (
    BumbleHCITransportBackend,
    ControllerHandoff,
    HCITransportBackend,
)
from airpods_hr.bumble_keys import InMemoryBumbleKeyStore
from airpods_hr.classic_diagnostics import (
    ClassicHostStateObserver,
    ClassicHostStateSnapshot,
    RuntimeNameProfile,
    runtime_name_for_profile,
)


class ClassicAuthenticationError(RuntimeError):
    """Base error for an authentication-only session."""


class BumbleDeviceLifecycleError(ClassicAuthenticationError):
    """Raised when the temporary Bumble Device cannot start or stop."""


class AuthenticationProgress(StrEnum):
    """Non-secret progress events emitted by the orchestration layer."""

    DEVICE_SELECTED = "device_selected"
    CONNECTED = "connected"
    AUTHENTICATED = "authenticated"
    ENCRYPTED = "encrypted"
    DISCONNECTED = "disconnected"
    REPLACEMENT_KEY_REPORTED = "replacement_key_reported"


ProgressCallback = Callable[[AuthenticationProgress, str | None], None]


class ClassicConnection(Protocol):
    @property
    def authenticated(self) -> bool: ...

    @property
    def encrypted(self) -> bool: ...

    async def authenticate(self) -> None: ...

    async def encrypt(self) -> None: ...

    async def disconnect(self) -> None: ...

    @property
    def l2cap_channel_manager(self) -> object: ...

    async def create_l2cap_channel(self, spec: object) -> object: ...


class SDPDiagnosticsObserver(Protocol):
    def observe(self, device: object) -> AbstractContextManager[None]: ...


class ClassicRuntime(Protocol):
    async def connect(self, peer_address: BluetoothAddress) -> ClassicConnection: ...

    def temporary_sdp_records(
        self, records: object
    ) -> AbstractContextManager[None]: ...

    def observe_sdp(
        self, observer: SDPDiagnosticsObserver
    ) -> AbstractContextManager[None]: ...


class CapturingHCITransportBackend:
    """Expose the active transport while preserving the accepted backend."""

    def __init__(self, backend: HCITransportBackend) -> None:
        self._backend = backend
        self._active_transport: object | None = None

    async def ensure_available(self) -> None:
        await self._backend.ensure_available()

    @asynccontextmanager
    async def acquire(self, adapter_index: int) -> AsyncIterator[object]:
        async with self._backend.acquire(adapter_index) as active_transport:
            if self._active_transport is not None:
                raise RuntimeError("an HCI transport is already active")
            self._active_transport = active_transport
            try:
                yield active_transport
            finally:
                self._active_transport = None

    def require_active_transport(self) -> object:
        if self._active_transport is None:
            raise RuntimeError("the HCI transport is not active")
        return self._active_transport


class ControllerHandoffTransport:
    """Adapt ControllerHandoff into a context that yields its transport."""

    def __init__(
        self,
        controller_handoff: ControllerHandoff,
        transport_backend: CapturingHCITransportBackend,
    ) -> None:
        self._controller_handoff = controller_handoff
        self._transport_backend = transport_backend

    @asynccontextmanager
    async def acquire(self, adapter_name: str) -> AsyncIterator[object]:
        async with self._controller_handoff.handoff(adapter_name):
            yield self._transport_backend.require_active_transport()


class BumbleClassicConnection:
    """Small adapter over Bumble's BR/EDR Connection API."""

    def __init__(
        self,
        connection: object,
        *,
        security_timeout: float,
        disconnect_timeout: float,
    ) -> None:
        self._connection = connection
        self._security_timeout = security_timeout
        self._disconnect_timeout = disconnect_timeout

    @property
    def authenticated(self) -> bool:
        return bool(self._connection.authenticated)

    @property
    def encrypted(self) -> bool:
        return bool(self._connection.encryption)

    async def authenticate(self) -> None:
        await asyncio.wait_for(
            self._connection.authenticate(), timeout=self._security_timeout
        )

    async def encrypt(self) -> None:
        await asyncio.wait_for(
            self._connection.encrypt(), timeout=self._security_timeout
        )

    async def disconnect(self) -> None:
        await asyncio.wait_for(
            self._connection.disconnect(), timeout=self._disconnect_timeout
        )

    @property
    def l2cap_channel_manager(self) -> object:
        return self._connection.device.l2cap_channel_manager

    async def create_l2cap_channel(self, spec: object) -> object:
        return await self._connection.create_l2cap_channel(spec)

class BumbleClassicRuntime:
    """Authentication-only operations on one powered Bumble Device."""

    def __init__(
        self,
        device: object,
        *,
        connect_timeout: float,
        security_timeout: float,
        disconnect_timeout: float,
        host_state_snapshot: ClassicHostStateSnapshot | None = None,
    ) -> None:
        self._device = device
        self._connect_timeout = connect_timeout
        self._security_timeout = security_timeout
        self._disconnect_timeout = disconnect_timeout
        self.host_state_snapshot = host_state_snapshot

    async def connect(self, peer_address: BluetoothAddress) -> ClassicConnection:
        from bumble.core import PhysicalTransport
        from bumble.hci import Address

        bumble_address = Address.from_string_for_transport(
            str(peer_address), PhysicalTransport.BR_EDR
        )
        connection = await self._device.connect(
            bumble_address,
            transport=PhysicalTransport.BR_EDR,
            timeout=self._connect_timeout,
        )
        return BumbleClassicConnection(
            connection,
            security_timeout=self._security_timeout,
            disconnect_timeout=self._disconnect_timeout,
        )

    @contextmanager
    def temporary_sdp_records(self, records: object):
        """Install records only on this owned temporary Bumble Device."""

        previous = self._device.sdp_service_records
        self._device.sdp_service_records = records
        primary_error: BaseException | None = None
        try:
            yield
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                self._device.sdp_service_records = previous
            except BaseException:
                if primary_error is not None:
                    primary_error.add_note(
                        "temporary SDP records also failed to restore during cleanup"
                    )
                else:
                    raise

    def observe_sdp(
        self, observer: SDPDiagnosticsObserver
    ) -> AbstractContextManager[None]:
        """Attach a narrow observer to this owned temporary Bumble Device."""

        return observer.observe(self._device)


class BumbleClassicRuntimeFactory:
    """Create and clean up the temporary Classic-only Bumble Device."""

    def __init__(
        self,
        *,
        connect_timeout: float = 20.0,
        security_timeout: float = 20.0,
        disconnect_timeout: float = 5.0,
        power_timeout: float = 10.0,
        runtime_name_profile: RuntimeNameProfile = RuntimeNameProfile.PROJECT_DEFAULT,
        host_state_observer: ClassicHostStateObserver | None = None,
    ) -> None:
        if min(
            connect_timeout,
            security_timeout,
            disconnect_timeout,
            power_timeout,
        ) <= 0:
            raise ValueError("Bumble operation timeouts must be positive")
        self._connect_timeout = connect_timeout
        self._security_timeout = security_timeout
        self._disconnect_timeout = disconnect_timeout
        self._power_timeout = power_timeout
        self._runtime_name_profile = RuntimeNameProfile(runtime_name_profile)
        self._host_state_observer = host_state_observer

    @asynccontextmanager
    async def open(
        self,
        active_transport: object,
        keystore: InMemoryBumbleKeyStore,
    ) -> AsyncIterator[ClassicRuntime]:
        from bumble.device import Device, DeviceConfiguration

        config = DeviceConfiguration(
            name=runtime_name_for_profile(self._runtime_name_profile),
            classic_enabled=True,
            le_enabled=False,
        )
        device = Device.from_config_with_hci(
            config,
            active_transport.source,
            active_transport.sink,
        )
        device.keystore = keystore
        primary_error: BaseException | None = None
        try:
            try:
                await asyncio.wait_for(
                    device.power_on(), timeout=self._power_timeout
                )
            except Exception:
                raise BumbleDeviceLifecycleError(
                    "the temporary Bumble Device could not be powered on"
                ) from None

            host_state_snapshot = None
            if self._host_state_observer is not None:
                host_state_snapshot = await self._host_state_observer.capture(
                    device, self._runtime_name_profile
                )

            yield BumbleClassicRuntime(
                device,
                connect_timeout=self._connect_timeout,
                security_timeout=self._security_timeout,
                disconnect_timeout=self._disconnect_timeout,
                host_state_snapshot=host_state_snapshot,
            )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                await asyncio.wait_for(
                    device.power_off(), timeout=self._power_timeout
                )
            except BaseException as cleanup_error:
                if primary_error is not None:
                    primary_error.add_note(
                        "the temporary Bumble Device also failed to power off "
                        "during cleanup"
                    )
                elif isinstance(cleanup_error, asyncio.CancelledError):
                    raise
                else:
                    raise BumbleDeviceLifecycleError(
                        "the temporary Bumble Device could not be powered off"
                    ) from None


def create_controller_handoff_transport(
    bluez_backend: object,
) -> tuple[ControllerHandoffTransport, CapturingHCITransportBackend]:
    """Compose the accepted handoff with its existing Bumble transport."""

    captured = CapturingHCITransportBackend(BumbleHCITransportBackend())
    controller = ControllerHandoff(bluez_backend, captured)
    return ControllerHandoffTransport(controller, captured), captured
