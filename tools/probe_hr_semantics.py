#!/usr/bin/env python3.14
"""Safe-by-default private heart-rate semantics capture tool."""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from airpods_hr.aap import AAPHandshakeSession
from airpods_hr.bluez_coexistence import (
    DEFAULT_DBUS_TIMEOUT,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_L2CAP_CONNECT_TIMEOUT,
    BlueZCompatibilityRegistration,
    CoexistenceFailure,
    DBusNextBlueZCoexistenceClient,
    KernelL2CAPTransport,
)
from airpods_hr.heart_rate_diagnostics import JsonlDiagnosticSink
from airpods_hr.hr_semantics import (
    DEFAULT_BASELINE_SAMPLES,
    DEFAULT_CYCLE_TIMEOUT,
    DEFAULT_RESTART_DELAY,
    DEFAULT_SAMPLES_PER_CYCLE,
    HRSemanticsFailure,
    HRSemanticsRecorder,
    HRSemanticsResult,
    HRSemanticsScenario,
    HRSemanticsSession,
)


LiveRunner = Callable[..., Awaitable[HRSemanticsResult]]


def _bounded_int(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"value must be between {minimum} and {maximum}"
            )
        return parsed

    return parse


def _bounded_float(minimum: float, maximum: float) -> Callable[[str], float]:
    def parse(value: str) -> float:
        parsed = float(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"value must be between {minimum:g} and {maximum:g}"
            )
        return parsed

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture neutral evidence from canonical AirPods HR reports over "
            "the BlueZ/kernel coexistence path."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the opt-in Bluetooth capture",
    )
    parser.add_argument(
        "--scenario",
        choices=tuple(scenario.value for scenario in HRSemanticsScenario),
        default=HRSemanticsScenario.BASELINE.value,
    )
    parser.add_argument(
        "--samples",
        type=_bounded_int(1, 300),
        default=DEFAULT_BASELINE_SAMPLES,
        help="reports in the baseline activation (default: 30)",
    )
    parser.add_argument(
        "--samples-per-cycle",
        type=_bounded_int(1, 100),
        default=DEFAULT_SAMPLES_PER_CYCLE,
        help="reports per activation-restart cycle (default: 10)",
    )
    parser.add_argument(
        "--restart-delay",
        type=_bounded_float(0.0, 30.0),
        default=DEFAULT_RESTART_DELAY,
        help="seconds between same-channel activation cycles (default: 5)",
    )
    parser.add_argument(
        "--descriptor-timeout",
        type=_bounded_float(1.0, 30.0),
        default=30.0,
        help="canonical descriptor observation window (default: 30)",
    )
    parser.add_argument(
        "--cycle-timeout",
        type=_bounded_float(5.0, 300.0),
        default=DEFAULT_CYCLE_TIMEOUT,
        help="maximum seconds for each activation cycle (default: 60)",
    )
    parser.add_argument(
        "--dbus-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_DBUS_TIMEOUT,
    )
    parser.add_argument(
        "--connect-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_L2CAP_CONNECT_TIMEOUT,
    )
    parser.add_argument(
        "--handshake-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_HANDSHAKE_TIMEOUT,
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="new JSONL path (default: timestamped file under /tmp)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include safe failure class/category details",
    )
    return parser


def _default_output_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return Path(f"/tmp/airpods-hr-semantics-{timestamp}-{os.getpid()}.jsonl")


async def run_live_capture(
    output: Callable[[str], None],
    recorder: HRSemanticsRecorder,
    scenario: HRSemanticsScenario,
    samples: int,
    samples_per_cycle: int,
    restart_delay: float,
    descriptor_timeout: float,
    cycle_timeout: float,
    dbus_timeout: float,
    connect_timeout: float,
    handshake_timeout: float,
) -> HRSemanticsResult:
    client = DBusNextBlueZCoexistenceClient()
    registration = BlueZCompatibilityRegistration(
        client, operation_timeout=dbus_timeout
    )
    transport = KernelL2CAPTransport(connect_timeout=connect_timeout)
    session = HRSemanticsSession(
        client,
        registration,
        transport,
        AAPHandshakeSession(
            ack_timeout=handshake_timeout,
            descriptor_timeout=descriptor_timeout,
        ),
        recorder,
        scenario=scenario,
        requested_samples=(
            samples if scenario is HRSemanticsScenario.BASELINE else None
        ),
        requested_samples_per_cycle=(
            samples_per_cycle
            if scenario is HRSemanticsScenario.ACTIVATION_RESTART
            else None
        ),
        restart_delay=restart_delay,
        cycle_timeout=cycle_timeout,
        dbus_timeout=dbus_timeout,
        output=output,
    )
    return await session.run()


async def run_probe(
    *,
    execute: bool,
    scenario: HRSemanticsScenario,
    samples: int = DEFAULT_BASELINE_SAMPLES,
    samples_per_cycle: int = DEFAULT_SAMPLES_PER_CYCLE,
    restart_delay: float = DEFAULT_RESTART_DELAY,
    descriptor_timeout: float = 30.0,
    cycle_timeout: float = DEFAULT_CYCLE_TIMEOUT,
    dbus_timeout: float = DEFAULT_DBUS_TIMEOUT,
    connect_timeout: float = DEFAULT_L2CAP_CONNECT_TIMEOUT,
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    output_path: Path | None = None,
    verbose: bool = False,
    output: Callable[[str], None] = print,
    live_runner: LiveRunner = run_live_capture,
) -> int:
    if not execute:
        target = output_path or Path(
            "/tmp/airpods-hr-semantics-<timestamp>-<pid>.jsonl"
        )
        output("DRY RUN: no Bluetooth or BlueZ state will be changed.")
        output(f"scenario={scenario.value}")
        if scenario is HRSemanticsScenario.BASELINE:
            output(f"cycles=1, reports_per_cycle={samples}")
        else:
            output(
                f"cycles=2, reports_per_cycle={samples_per_cycle}, "
                f"restart_delay={restart_delay:g}s"
            )
            output("AAP channels=1; descriptor handshakes=1")
        output("descriptor_policy=canonical-required")
        output("kernel_local_rx_imtu=2048")
        output(f"output={target}")
        return 0

    target = output_path or _default_output_path()
    try:
        sink = JsonlDiagnosticSink.open(target)
    except Exception as error:
        output("HR SEMANTICS CAPTURE FAIL at output_open")
        if verbose:
            output(f"Safe detail: {type(error).__name__}")
        output(f"output={target}")
        return 1

    recorder = HRSemanticsRecorder(
        sink,
        scenario=scenario,
        requested_samples=(
            samples if scenario is HRSemanticsScenario.BASELINE else None
        ),
        requested_samples_per_cycle=(
            samples_per_cycle
            if scenario is HRSemanticsScenario.ACTIVATION_RESTART
            else None
        ),
        restart_delay_seconds=restart_delay,
    )
    try:
        result = await live_runner(
            output,
            recorder,
            scenario,
            samples,
            samples_per_cycle,
            restart_delay,
            descriptor_timeout,
            cycle_timeout,
            dbus_timeout,
            connect_timeout,
            handshake_timeout,
        )
    except asyncio.CancelledError:
        raise
    except (HRSemanticsFailure, CoexistenceFailure) as error:
        phase = getattr(error, "phase", "capture")
        category = getattr(error, "category", type(error).__name__)
        category_text = getattr(category, "value", str(category))
        output(f"HR SEMANTICS CAPTURE FAIL at {phase}: {category_text}")
        if verbose:
            output(f"Safe detail: {type(error).__name__}")
        return 1
    except Exception as error:
        output("HR SEMANTICS CAPTURE FAIL at unknown: capture_failed")
        if verbose:
            output(f"Safe detail: {type(error).__name__}")
        return 1
    else:
        output(
            f"Canonical reports captured: {result.reports_received}; "
            f"cycles completed: {result.cycles_completed}"
        )
        output("HR SEMANTICS CAPTURE COMPLETE")
        return 0
    finally:
        try:
            recorder.close()
        finally:
            output(f"output={target}")


def main(
    argv: Sequence[str] | None = None, *, stream: TextIO | None = None
) -> int:
    args = build_parser().parse_args(argv)
    destination = stream

    def emit(message: str) -> None:
        print(message, file=destination)

    try:
        return asyncio.run(
            run_probe(
                execute=args.execute,
                scenario=HRSemanticsScenario(args.scenario),
                samples=args.samples,
                samples_per_cycle=args.samples_per_cycle,
                restart_delay=args.restart_delay,
                descriptor_timeout=args.descriptor_timeout,
                cycle_timeout=args.cycle_timeout,
                dbus_timeout=args.dbus_timeout,
                connect_timeout=args.connect_timeout,
                handshake_timeout=args.handshake_timeout,
                output_path=args.output,
                verbose=args.verbose,
                output=emit,
            )
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
