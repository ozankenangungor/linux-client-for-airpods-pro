#!/usr/bin/env python3.14
"""Safe-by-default bounded validation probe for the continuous HR core."""

from __future__ import annotations


import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


from airpods_hr.heart_rate_session import HeartRateMonitorResult, HeartRateProgress


from airpods_hr.heartrate import HeartRateReport


DEFAULT_VALIDATION_SAMPLE_TARGET = 8
DEFAULT_VALIDATION_WINDOW_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class MonitorProbeOutcome:
    result: HeartRateMonitorResult
    watchdog_expired: bool


@dataclass(slots=True)
class _WatchdogState:
    expired: bool = False


class _MonitorSession(Protocol):
    async def run(self, stop_event: asyncio.Event) -> HeartRateMonitorResult: ...


LiveRunner = Callable[
    [Callable[[str], None], int, float], Awaitable[MonitorProbeOutcome]
]
WaitFor = Callable[[Awaitable[bool], float], Awaitable[bool]]


def _heart_rate_progress(
    output: Callable[[str], None],
    sample_target: int,
    stop_event: asyncio.Event,
    start_acknowledged: asyncio.Event,
):
    sample_index = 0

    def emit(event: HeartRateProgress, report: HeartRateReport | None) -> None:
        nonlocal sample_index
        if event is HeartRateProgress.BOOTSTRAP_COMPLETE:
            output("HR bootstrap window: complete")
        elif event is HeartRateProgress.STOP_HEAD_ACKNOWLEDGED:
            output("STOP_HEAD acknowledgement: OK")
        elif event is HeartRateProgress.CONTROL_CHANNELS_READY:
            output("AAP control channels: OK")
        elif event is HeartRateProgress.START_ACKNOWLEDGED:
            output("Heart-rate start acknowledgement: OK")
            start_acknowledged.set()
        elif event is HeartRateProgress.SAMPLE and report is not None:
            sample_index += 1
            output(f"Heart rate {sample_index}: {report.bpm} bpm")
            if sample_index >= sample_target:
                stop_event.set()
        elif event is HeartRateProgress.STOP_ACKNOWLEDGED:
            output("Heart-rate stop acknowledgement: OK")
        elif event is HeartRateProgress.STOP_ACK_MISSING:
            output("Heart-rate stop acknowledgement: not observed")
        elif event is HeartRateProgress.HR_OFF_SENT:
            output("HR_OFF: sent")

    return emit


async def _validation_watchdog(
    start_acknowledged: asyncio.Event,
    stop_event: asyncio.Event,
    monitor_window: float,
    state: _WatchdogState,
    *,
    wait_for: WaitFor = asyncio.wait_for,
) -> None:
    await start_acknowledged.wait()
    try:
        await wait_for(stop_event.wait(), monitor_window)
    except TimeoutError:
        state.expired = True
        stop_event.set()


async def _run_session_with_watchdog(
    session: _MonitorSession,
    stop_event: asyncio.Event,
    start_acknowledged: asyncio.Event,
    monitor_window: float,
    state: _WatchdogState,
) -> HeartRateMonitorResult:
    watchdog = asyncio.create_task(
        _validation_watchdog(
            start_acknowledged,
            stop_event,
            monitor_window,
            state,
        )
    )
    try:
        return await session.run(stop_event)
    finally:
        if not watchdog.done():
            watchdog.cancel()
        try:
            await watchdog
        except asyncio.CancelledError:
            pass


