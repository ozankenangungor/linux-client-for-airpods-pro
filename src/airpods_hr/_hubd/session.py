"""Injected session boundary for the hardware-independent daemon core."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from airpods_hr.heartrate import HeartRateReport


class SensorSession(Protocol):
    """The minimal lifecycle expected from one persistent sensor session."""

    async def open(self) -> None: ...

    async def start(self) -> None: ...

    async def receive_report(self) -> HeartRateReport: ...

    async def stop(self) -> None: ...

    async def close(self) -> None: ...


SessionFactory = Callable[[], SensorSession]
