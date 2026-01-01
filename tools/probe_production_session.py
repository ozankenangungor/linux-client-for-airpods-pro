#!/usr/bin/env python3.14
"""Safe-by-default private probe for the persistent production session core."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TextIO

from airpods_hr.production_session import (
    DEFAULT_REPORT_TIMEOUT,
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    InternalProductionSession,
    ProductionSessionCounters,
    create_production_session,
)


DEFAULT_CYCLES = 3
DEFAULT_SAMPLES_PER_CYCLE = 5
DEFAULT_RESTART_DELAY = 5.0


SessionFactory = Callable[..., InternalProductionSession]
Sleeper = Callable[[float], Awaitable[None]]


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
            "Exercise repeated canonical HR activations on one persistent "
            "BlueZ/kernel AAP channel."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the opt-in Bluetooth session test",
    )
    parser.add_argument(
        "--cycles",
        type=_bounded_int(1, 10),
        default=DEFAULT_CYCLES,
    )
    parser.add_argument(
        "--samples-per-cycle",
        type=_bounded_int(1, 100),
        default=DEFAULT_SAMPLES_PER_CYCLE,
    )
    parser.add_argument(
        "--restart-delay",
        type=_bounded_float(0.0, 30.0),
        default=DEFAULT_RESTART_DELAY,
    )
    parser.add_argument(
        "--descriptor-timeout",
        type=_bounded_float(1.0, 30.0),
        default=30.0,
    )
    parser.add_argument(
        "--report-timeout",
        type=_bounded_float(0.1, 30.0),
        default=DEFAULT_REPORT_TIMEOUT,
    )
    parser.add_argument(
        "--start-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_START_TIMEOUT,
    )
    parser.add_argument(
        "--stop-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_STOP_TIMEOUT,
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include safe exception class details on failure",
    )
    return parser


def _print_counters(
    counters: ProductionSessionCounters,
    output: Callable[[str], None],
) -> None:
    output("PERSISTENT SESSION COUNTERS")
    output(f"  transport_opens={counters.transport_opens}")
    output(f"  descriptor_handshakes={counters.descriptor_handshakes}")
    output(f"  hr_activations={counters.hr_activations}")
    output(f"  hr_stops={counters.hr_stops}")
    output(f"  reports_received={counters.reports_received}")


async def run_probe(
    *,
    execute: bool,
    cycles: int = DEFAULT_CYCLES,
    samples_per_cycle: int = DEFAULT_SAMPLES_PER_CYCLE,
    restart_delay: float = DEFAULT_RESTART_DELAY,
    descriptor_timeout: float = 30.0,
    report_timeout: float = DEFAULT_REPORT_TIMEOUT,
    start_timeout: float = DEFAULT_START_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    verbose: bool = False,
    output: Callable[[str], None] = print,
    session_factory: SessionFactory = create_production_session,
    sleep: Sleeper = asyncio.sleep,
) -> int:
    if not execute:
        output("DRY RUN: no Bluetooth or BlueZ state will be changed.")
        output("transport=bluez-kernel-coexistence")
        output("kernel_local_rx_imtu=2048")
        output("descriptor_policy=canonical-required")
        output("AAP channels=1; descriptor handshakes=1")
        output(
            f"cycles={cycles}; samples_per_cycle={samples_per_cycle}; "
            f"restart_delay={restart_delay:g}s"
        )
        output(f"AAP descriptor timeout={descriptor_timeout:g}s")
        return 0

    session = session_factory(
        descriptor_timeout=descriptor_timeout,
        start_timeout=start_timeout,
        stop_timeout=stop_timeout,
        output=output,
    )
    failure: BaseException | None = None
    try:
        await session.open()
        for cycle_index in range(1, cycles + 1):
            output(f"CYCLE {cycle_index}: START")
            await session.start()
            for sample_index in range(1, samples_per_cycle + 1):
                report = await session.receive_report(timeout=report_timeout)
                output(
                    f"C{cycle_index} S{sample_index:02d} bpm={report.bpm} "
                    f"seq={report.sequence} field_5={report.field_5} "
                    f"flags=0x{report.flags:08x}"
                )
            await session.stop()
            output(f"CYCLE {cycle_index}: STOP complete")
            if cycle_index < cycles:
                await sleep(restart_delay)
    except BaseException as error:
        failure = error
    finally:
        try:
            await session.close()
        except BaseException as error:
            if failure is None:
                failure = error
            else:
                failure.add_note("persistent session close also failed")

    _print_counters(session.counters, output)
    if failure is not None:
        if isinstance(failure, asyncio.CancelledError):
            raise failure
        category = getattr(failure, "category", "session_failed")
        category_text = getattr(category, "value", str(category))
        phase = getattr(failure, "phase", "session")
        output(f"PERSISTENT SESSION FAIL at {phase}: {category_text}")
        if verbose:
            output(f"Safe detail: {type(failure).__name__}")
        return 1

    counters = session.counters
    expected_reports = cycles * samples_per_cycle
    if (
        counters.transport_opens != 1
        or counters.descriptor_handshakes != 1
        or counters.hr_activations != cycles
        or counters.hr_stops != cycles
        or counters.reports_received != expected_reports
    ):
        output("PERSISTENT SESSION FAIL at counters: lifecycle_mismatch")
        return 1
    output("PERSISTENT SESSION PASS")
    return 0


def main(
    argv: Sequence[str] | None = None, *, stream: TextIO | None = None
) -> int:
    args = build_parser().parse_args(argv)

    def emit(message: str) -> None:
        print(message, file=stream)

    try:
        return asyncio.run(
            run_probe(
                execute=args.execute,
                cycles=args.cycles,
                samples_per_cycle=args.samples_per_cycle,
                restart_delay=args.restart_delay,
                descriptor_timeout=args.descriptor_timeout,
                report_timeout=args.report_timeout,
                start_timeout=args.start_timeout,
                stop_timeout=args.stop_timeout,
                verbose=args.verbose,
                output=emit,
            )
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
