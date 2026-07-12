"""Isolated Linux Bluetooth controller handoff support.

The orchestration code depends on small BlueZ and HCI transport protocols so
its restoration behavior can be tested without Bluetooth hardware or root.
Real backends import their optional dependencies only when used.
"""

from __future__ import annotations

import asyncio
import errno
import airpods_hr._airpods_aap_core as _rust_core
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncContextManager, Protocol, TypeVar


class HandoffError(RuntimeError):
    """Base error for controller handoff failures."""


class OptionalDependencyError(HandoffError):
    """Raised when a real backend's optional dependency is unavailable."""


class BlueZUnavailableError(HandoffError):
    """Raised when the BlueZ service cannot be reached over D-Bus."""


class BlueZOperationError(HandoffError):
    """Raised when a BlueZ D-Bus operation fails."""


class AdapterNotFoundError(HandoffError):
    """Raised when BlueZ does not currently expose the requested adapter."""


class AdapterStateTimeoutError(HandoffError):
    """Raised when an adapter does not reach a requested state in time."""


class AdapterRestoreError(HandoffError):
    """Raised when the adapter's original Powered state cannot be restored."""


class AdapterReappearanceTimeoutError(AdapterRestoreError):
    """Raised when an adapter does not reappear in BlueZ after handoff."""


class ControllerPermissionError(HandoffError):
    """Raised when HCI user-channel access is denied."""


class TransportAcquisitionError(HandoffError):
    """Raised when Bumble cannot acquire the HCI user-channel transport."""


@dataclass(frozen=True, slots=True)
class AdapterState:
    """Non-sensitive adapter state required to restore a handoff."""

    name: str
    index: int
    powered: bool


class BlueZAdapterBackend(Protocol):
    """BlueZ operations used by the controller handoff orchestrator."""

    async def ensure_available(self) -> None:
        """Verify that org.bluez is available."""

    async def get_adapter(self, adapter_name: str) -> AdapterState:
        """Rediscover and return current adapter state."""

    async def set_powered(self, adapter_name: str, powered: bool) -> None:
        """Set org.bluez.Adapter1.Powered."""


class HCITransportBackend(Protocol):
    """Exclusive HCI transport acquisition used by the orchestrator."""

    async def ensure_available(self) -> None:
        """Verify that the transport implementation can be loaded."""

    def acquire(self, adapter_index: int) -> AsyncContextManager[object]:
        """Acquire and always release an HCI user-channel transport."""


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
Result = TypeVar("Result")


class ControllerHandoff:
    """Temporarily transfer one powered-down controller from BlueZ to HCI."""

    def __init__(
        self,
        bluez: BlueZAdapterBackend,
        transport: HCITransportBackend,
        *,
        state_timeout: float = 5.0,
        restore_timeout: float = 10.0,
        poll_interval: float = 0.1,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        if not _rust_core.runtime_positive_timeouts(
            (state_timeout, restore_timeout, poll_interval)
        ):
            raise ValueError("timeouts and poll_interval must be positive")
        self._bluez = bluez
        self._transport = transport
        self._state_timeout = state_timeout
        self._restore_timeout = restore_timeout
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock

    async def inspect(self, adapter_name: str) -> AdapterState:
        """Read the adapter state without changing it."""

        return await self._bluez.get_adapter(adapter_name)

    @asynccontextmanager
    async def handoff(
        self, adapter: str | AdapterState
    ) -> AsyncIterator[AdapterState]:
        """Power down, acquire, release, rediscover, and restore an adapter.

        Cleanup is attempted for ordinary exceptions and asyncio cancellation.
        If cleanup cannot restore the original Powered state, a dedicated
        :class:`AdapterRestoreError` replaces the active error and chains it as
        context so restoration failure is never hidden.
        """

        original = (
            await self.inspect(adapter) if isinstance(adapter, str) else adapter
        )

        try:
            if original.powered:
                # A failed setter may still have changed state, so restoration
                # is already required before this call is made.
                await self._bluez.set_powered(original.name, False)

            await self._wait_for_powered(
                original.name,
                False,
                timeout=self._state_timeout,
            )

            async with self._transport.acquire(original.index):
                yield original
        finally:
            await self._restore(original)

    async def _wait_for_powered(
        self,
        adapter_name: str,
        expected: bool,
        *,
        timeout: float,
    ) -> AdapterState:
        deadline = self._clock() + timeout

        while True:
            try:
                state = await self._bluez.get_adapter(adapter_name)
            except AdapterNotFoundError:
                state = None

            if state is not None and state.powered is expected:
                return state

            now = self._clock()
            if now >= deadline:
                state_name = "powered" if expected else "powered off"
                raise AdapterStateTimeoutError(
                    f"adapter {adapter_name} did not become {state_name} "
                    f"within {timeout:g} seconds"
                )

            await self._sleep(_rust_core.runtime_poll_delay(self._poll_interval, deadline, now))

    async def _restore(self, original: AdapterState) -> None:
        deadline = self._clock() + self._restore_timeout
        saw_adapter = False
        last_read_error: Exception | None = None
        last_set_error: Exception | None = None

        while self._clock() < deadline:
            try:
                current = await self._before_deadline(
                    lambda: self._bluez.get_adapter(original.name),
                    deadline,
                )
            except Exception as error:
                last_read_error = error
            else:
                last_read_error = None
                saw_adapter = True
                if current.powered is original.powered:
                    return

                try:
                    await self._before_deadline(
                        lambda: self._bluez.set_powered(
                            original.name, original.powered
                        ),
                        deadline,
                    )
                except Exception as error:
                    # A D-Bus error does not prove that the requested state was
                    # not applied. Always re-read before deciding to retry.
                    last_set_error = error
                else:
                    last_set_error = None

                try:
                    observed = await self._before_deadline(
                        lambda: self._bluez.get_adapter(original.name),
                        deadline,
                    )
                except Exception as error:
                    last_read_error = error
                else:
                    last_read_error = None
                    saw_adapter = True
                    if observed.powered is original.powered:
                        return

            now = self._clock()
            if now >= deadline:
                break
            await self._sleep(_rust_core.runtime_poll_delay(self._poll_interval, deadline, now))

        restoration = _rust_core.runtime_restoration(
            saw_adapter, last_set_error is not None
        )
        if restoration == 0:
            error = AdapterReappearanceTimeoutError(
                f"adapter {original.name} did not reappear in BlueZ "
                f"within {self._restore_timeout:g} seconds"
            )
            if last_read_error is not None:
                raise error from last_read_error
            raise error

        if restoration == 1:
            error = AdapterRestoreError(
                f"adapter {original.name} reappeared, but Powered="
                f"{original.powered} was not observed within "
                f"{self._restore_timeout:g} seconds; the last D-Bus Set failed"
            )
            raise error from last_set_error

        error = AdapterRestoreError(
            f"adapter {original.name} reappeared, but Powered="
            f"{original.powered} was not observed within "
            f"{self._restore_timeout:g} seconds"
        )
        if last_read_error is not None:
            raise error from last_read_error
        raise error

    async def _before_deadline(
        self,
        operation: Callable[[], Awaitable[Result]],
        deadline: float,
    ) -> Result:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise TimeoutError("adapter restoration deadline reached")
        return await asyncio.wait_for(operation(), timeout=remaining)


class DBusNextBlueZBackend:
    """BlueZ Adapter1 backend implemented with the pure-Python dbus-next API.

    Every adapter operation starts from ObjectManager data. In particular,
    restoration does not retain or reuse an adapter proxy that existed before
    the HCI user channel was acquired.
    """

    BLUEZ_SERVICE = "org.bluez"
    OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
    PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
    ADAPTER_INTERFACE = "org.bluez.Adapter1"

    def __init__(self) -> None:
        self._bus: Any | None = None
        self._variant_type: Any | None = None

    async def connect(self) -> None:
        if self._bus is not None:
            return

        try:
            from dbus_next import BusType, Variant
            from dbus_next.aio import MessageBus
        except ModuleNotFoundError as error:
            raise OptionalDependencyError(
                "dbus-next is required for the live controller handoff probe"
            ) from error

        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self._variant_type = Variant
        except Exception as error:
            raise BlueZUnavailableError(
                "could not connect to the system D-Bus"
            ) from error

    def close(self) -> None:
        """Disconnect from D-Bus without changing adapter state."""

        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def ensure_available(self) -> None:
        await self._get_managed_objects()

    async def get_adapter(self, adapter_name: str) -> AdapterState:
        adapter_path, properties = await self._find_adapter(adapter_name)
        del adapter_path
        powered = properties.get("Powered")
        if powered is None:
            raise BlueZOperationError(
                f"BlueZ adapter {adapter_name} has no Powered property"
            )
        return AdapterState(
            name=adapter_name,
            index=self._adapter_index(adapter_name),
            powered=bool(powered.value),
        )

    async def set_powered(self, adapter_name: str, powered: bool) -> None:
        adapter_path, _ = await self._find_adapter(adapter_name)
        bus = self._require_bus()

        try:
            introspection = await bus.introspect(self.BLUEZ_SERVICE, adapter_path)
            proxy = bus.get_proxy_object(
                self.BLUEZ_SERVICE, adapter_path, introspection
            )
            properties = proxy.get_interface(self.PROPERTIES_INTERFACE)
            await properties.call_set(
                self.ADAPTER_INTERFACE,
                "Powered",
                self._variant_type("b", powered),
            )
        except Exception as error:
            raise BlueZOperationError(
                f"could not set {adapter_name} Powered={powered}"
            ) from error

    async def _find_adapter(
        self, adapter_name: str
    ) -> tuple[str, dict[str, Any]]:
        self._adapter_index(adapter_name)
        managed_objects = await self._get_managed_objects()

        for object_path, interfaces in managed_objects.items():
            if object_path.rsplit("/", 1)[-1] != adapter_name:
                continue
            adapter_properties = interfaces.get(self.ADAPTER_INTERFACE)
            if adapter_properties is not None:
                return object_path, adapter_properties

        raise AdapterNotFoundError(
            f"BlueZ adapter {adapter_name} is not currently available"
        )

    async def _get_managed_objects(self) -> dict[str, Any]:
        bus = self._require_bus()
        try:
            introspection = await bus.introspect(self.BLUEZ_SERVICE, "/")
            proxy = bus.get_proxy_object(self.BLUEZ_SERVICE, "/", introspection)
            manager = proxy.get_interface(self.OBJECT_MANAGER_INTERFACE)
            return await manager.call_get_managed_objects()
        except Exception as error:
            raise BlueZUnavailableError(
                "org.bluez is not available on the system bus"
            ) from error

    def _require_bus(self) -> Any:
        if self._bus is None:
            raise BlueZUnavailableError("the system D-Bus backend is not connected")
        return self._bus

    @classmethod
    def _adapter_index(cls, adapter_name: str) -> int:
        digits = _rust_core.runtime_adapter_digits(adapter_name)
        if digits is None:
            raise ValueError("adapter must use the form hci<index>")
        return int(digits)


class BumbleHCITransportBackend:
    """Acquire only Bumble's Linux hci-socket transport, without a Device."""

    def __init__(self) -> None:
        self._open_transport: Any | None = None

    async def ensure_available(self) -> None:
        if self._open_transport is not None:
            return
        try:
            from bumble.transport import open_transport
        except ModuleNotFoundError as error:
            raise OptionalDependencyError(
                "Bumble is required for the live controller handoff probe"
            ) from error
        self._open_transport = open_transport

    @asynccontextmanager
    async def acquire(self, adapter_index: int) -> AsyncIterator[object]:
        await self.ensure_available()
        transport_spec = f"hci-socket:{adapter_index}"

        try:
            transport = await self._open_transport(transport_spec)
        except PermissionError as error:
            raise ControllerPermissionError(
                "permission denied opening the HCI user channel; "
                "run the reviewed probe with appropriate privileges"
            ) from error
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EPERM):
                raise ControllerPermissionError(
                    "permission denied opening the HCI user channel; "
                    "run the reviewed probe with appropriate privileges"
                ) from error
            raise TransportAcquisitionError(
                f"could not open {transport_spec}"
            ) from error
        except Exception as error:
            raise TransportAcquisitionError(
                f"could not open {transport_spec}"
            ) from error

        async with transport as active_transport:
            yield active_transport
