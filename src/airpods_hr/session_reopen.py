"""Private same-ACL, new-AAP-channel characterization support.

This module composes the frozen production session core without changing its lifecycle.
It is deliberately absent from :mod:`airpods_hr` exports.
"""

from __future__ import annotations

import asyncio


from dataclasses import dataclass

from typing import Any


from airpods_hr.bluez_coexistence import DEFAULT_DBUS_TIMEOUT, DBusNextBlueZCoexistenceClient


@dataclass(frozen=True, slots=True)
class BlueZReopenCheckpoint:
    label: str
    bluez_reachable: bool
    adapter_powered: bool
    device_connected: bool

    @property
    def invariant_holds(self) -> bool:
        return (
            self.bluez_reachable
            and self.adapter_powered
            and self.device_connected
        )


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


