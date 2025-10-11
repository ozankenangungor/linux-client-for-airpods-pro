"""Isolated Linux Bluetooth controller handoff support.

The orchestration code depends on small BlueZ and HCI transport protocols so
its restoration behavior can be tested without Bluetooth hardware or root.
Real backends import their optional dependencies only when used.
"""

from __future__ import annotations

import asyncio


import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncContextManager, Protocol, TypeVar


class HandoffError(RuntimeError):
    """Base error for controller handoff failures."""


class AdapterNotFoundError(HandoffError):
    """Raised when BlueZ does not currently expose the requested adapter."""


class AdapterStateTimeoutError(HandoffError):
    """Raised when an adapter does not reach a requested state in time."""


class AdapterRestoreError(HandoffError):
    """Raised when the adapter's original Powered state cannot be restored."""


class AdapterReappearanceTimeoutError(AdapterRestoreError):
    """Raised when an adapter does not reappear in BlueZ after handoff."""


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
        if state_timeout <= 0 or restore_timeout <= 0 or poll_interval <= 0:
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

            await self._sleep(min(self._poll_interval, deadline - now))

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
            await self._sleep(min(self._poll_interval, deadline - now))

        if not saw_adapter:
            error = AdapterReappearanceTimeoutError(
                f"adapter {original.name} did not reappear in BlueZ "
                f"within {self._restore_timeout:g} seconds"
            )
            if last_read_error is not None:
                raise error from last_read_error
            raise error

        if last_set_error is not None:
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


