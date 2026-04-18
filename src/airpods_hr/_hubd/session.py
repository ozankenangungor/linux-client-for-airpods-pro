"""Injected session boundary for the hardware-independent daemon core."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from airpods_hr._connection_epoch import (
    ConnectionEpochRefreshError,
    ConnectionEpochRefreshOutcome,
    ConnectionEpochRefreshStage,
)
from airpods_hr.heartrate import HeartRateReport


class SensorSession(Protocol):
    """The minimal lifecycle expected from one persistent sensor session."""

    async def open(self) -> None: ...

    async def start(self) -> None: ...

    async def receive_report(self) -> HeartRateReport: ...

    async def stop(self) -> None: ...

    async def close(self) -> None: ...


SessionFactory = Callable[[], SensorSession]
SessionErrorClassifier = Callable[[BaseException], bool]
SessionCleanupVerifier = Callable[[SensorSession, BaseException], bool]
RecoverySleeper = Callable[[float], Awaitable[None]]


class ConnectionEpochRefresher(Protocol):
    """Perform one finite target-device connection-epoch replacement."""

    async def refresh(self) -> ConnectionEpochRefreshOutcome: ...


EpochRefreshEligibility = Callable[[BaseException], bool]
