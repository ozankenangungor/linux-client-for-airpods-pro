#!/usr/bin/env python3.14
"""Safe-by-default characterization of a new AAP channel on one BlueZ ACL."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TextIO

from airpods_hr.aap import (
    AAPDescriptorObservationTimeoutError,
    AAPHandshakeTimeoutError,
)
from airpods_hr.production_session import (
    DEFAULT_REPORT_TIMEOUT,
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    ProductionSessionCategory,
)
from airpods_hr.session_reopen import (
    BlueZReopenCheckpoint,
    BlueZReopenCheckpointObserver,
    ReopenSessionBundle,
    SessionReopenCounters,
    SessionReopenResult,
    SessionReopenResultCategory,
    create_reopen_session_bundle,
)


DEFAULT_SAMPLES_PER_SESSION = 5
DEFAULT_REOPEN_DELAY = 5.0
DEFAULT_DESCRIPTOR_TIMEOUT = 30.0

BundleFactory = Callable[..., ReopenSessionBundle]
ObserverFactory = Callable[[], Any]
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
            "Open two independent production AAP sessions while preserving "
            "one existing BlueZ connection."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the opt-in Bluetooth lifecycle characterization",
    )
    parser.add_argument(
        "--samples-per-session",
        type=_bounded_int(1, 100),
        default=DEFAULT_SAMPLES_PER_SESSION,
    )
    parser.add_argument(
        "--reopen-delay",
        type=_bounded_float(0.0, 30.0),
        default=DEFAULT_REOPEN_DELAY,
    )
    parser.add_argument(
        "--descriptor-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_DESCRIPTOR_TIMEOUT,
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
        help="print bounded safe handshake evidence",
    )
    return parser


def _checkpoint_line(checkpoint: BlueZReopenCheckpoint) -> str:
    yes_no = lambda value: "yes" if value else "no"
    return (
        f"{checkpoint.label}: BlueZ reachable="
        f"{yes_no(checkpoint.bluez_reachable)}, adapter powered="
        f"{yes_no(checkpoint.adapter_powered)}, Device1.Connected="
        f"{yes_no(checkpoint.device_connected)}"
    )


async def _exercise_session(
    bundle: ReopenSessionBundle,
    *,
    session_index: int,
    samples: int,
    report_timeout: float,
    output: Callable[[str], None],
) -> None:
    session = bundle.session
    await session.open()
    local_rx = bundle.transport.local_rx_observation
    output(
        f"SESSION {session_index}: kernel local RX imtu "
        f"before={local_rx.before_imtu} after={local_rx.after_imtu} "
        f"verified={'yes' if local_rx.verified else 'no'}"
    )
    output(f"SESSION {session_index}: selected adapter route verified=yes")
    output(f"SESSION {session_index}: descriptor handshake complete")
    await session.start()
    for sample_index in range(1, samples + 1):
        report = await session.receive_report(timeout=report_timeout)
        output(
            f"SESSION {session_index} S{sample_index:02d} "
            f"bpm={report.bpm} seq={report.sequence} "
            f"field_5={report.field_5} flags=0x{report.flags:08x}"
        )
    await session.stop()
    output(f"SESSION {session_index}: STOP complete")


def _safe_handshake_observation(bundle: ReopenSessionBundle | None) -> Any:
    return getattr(bundle.handshake, "observation", None) if bundle else None


def _classify_session_2_failure(
    error: BaseException,
    bundle: ReopenSessionBundle,
) -> SessionReopenResultCategory:
    handshake_error = getattr(bundle.handshake, "error", None)
    observation = _safe_handshake_observation(bundle)
    if (
        isinstance(handshake_error, AAPDescriptorObservationTimeoutError)
        and observation is not None
        and observation.ack_observed
    ):
        return SessionReopenResultCategory.SESSION_2_EXACT_ACK_DESCRIPTOR_TIMEOUT
    if isinstance(handshake_error, AAPHandshakeTimeoutError) and (
        observation is None or not observation.ack_observed
    ):
        return SessionReopenResultCategory.SESSION_2_AAP_ACK_FAILURE
    category = getattr(error, "category", None)
    if category is ProductionSessionCategory.PREFLIGHT_FAILED:
        return SessionReopenResultCategory.BLUEZ_STATE_CHANGED
    if category is ProductionSessionCategory.TRANSPORT_FAILED:
        return SessionReopenResultCategory.SESSION_2_TRANSPORT_FAILURE
    return SessionReopenResultCategory.OTHER_FAILURE


def _aggregate(
    bundles: list[ReopenSessionBundle],
    checkpoints: list[BlueZReopenCheckpoint],
    category: SessionReopenResultCategory,
    failure: BaseException | None,
) -> SessionReopenResult:
    production_counters = [bundle.session.counters for bundle in bundles]
    counters = SessionReopenCounters(
        session_objects_created=len(bundles),
        transport_opens=sum(item.transport_opens for item in production_counters),
        transport_closes=sum(
            int(getattr(bundle.transport, "close_calls", 0)) for bundle in bundles
        ),
        descriptor_handshakes_attempted=sum(
            int(getattr(bundle.handshake, "attempts", 0)) for bundle in bundles
        ),
        descriptor_handshakes_completed=sum(
            int(getattr(bundle.handshake, "completed", 0)) for bundle in bundles
        ),
        exact_aap_acks=sum(
            int(
                observation is not None and observation.ack_observed
            )
            for observation in (
                _safe_handshake_observation(bundle) for bundle in bundles
            )
        ),
        hr_activations=sum(item.hr_activations for item in production_counters),
        hr_stops=sum(item.hr_stops for item in production_counters),
        reports_received_session_1=(
            production_counters[0].reports_received if production_counters else 0
        ),
        reports_received_session_2=(
            production_counters[1].reports_received
            if len(production_counters) > 1
            else 0
        ),
    )
    return SessionReopenResult(
        category=category,
        counters=counters,
        checkpoints=tuple(checkpoints),
        session_2_handshake_observation=(
            _safe_handshake_observation(bundles[1]) if len(bundles) > 1 else None
        ),
        failure_type=type(failure).__name__ if failure is not None else None,
    )


def _print_result(
    result: SessionReopenResult,
    *,
    verbose: bool,
    output: Callable[[str], None],
) -> None:
    counters = result.counters
    output("SESSION REOPEN COUNTERS")
    for name in SessionReopenCounters.__dataclass_fields__:
        output(f"  {name}={getattr(counters, name)}")
    observation = result.session_2_handshake_observation
    if (
        verbose
        and result.category
        is SessionReopenResultCategory.SESSION_2_EXACT_ACK_DESCRIPTOR_TIMEOUT
        and observation is not None
    ):
        evidence = observation.evidence
        output("SESSION 2 AAP descriptor timeout diagnostics:")
        output(f"  exact_ack_observed={'yes' if observation.ack_observed else 'no'}")
        output(f"  post_ack_frames={observation.post_ack_frame_count}")
        output(f"  receive_frames_dropped={observation.receive_frames_dropped}")
        output(f"  sensor_framework={'yes' if evidence.sensor_framework else 'no'}")
        output(
            f"  heart_rate_service={'yes' if evidence.heart_rate_service else 'no'}"
        )
        output(f"  heart_rate={'yes' if evidence.heart_rate else 'no'}")
        output(f"  heartrate_access={'yes' if evidence.heartrate_access else 'no'}")
    output(f"SESSION REOPEN RESULT: {result.category.value}")
    if verbose and result.failure_type:
        output(f"Safe detail: {result.failure_type}")


async def run_probe(
    *,
    execute: bool,
    samples_per_session: int = DEFAULT_SAMPLES_PER_SESSION,
    reopen_delay: float = DEFAULT_REOPEN_DELAY,
    descriptor_timeout: float = DEFAULT_DESCRIPTOR_TIMEOUT,
    report_timeout: float = DEFAULT_REPORT_TIMEOUT,
    start_timeout: float = DEFAULT_START_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    verbose: bool = False,
    output: Callable[[str], None] = print,
    bundle_factory: BundleFactory = create_reopen_session_bundle,
    observer_factory: ObserverFactory = BlueZReopenCheckpointObserver,
    sleep: Sleeper = asyncio.sleep,
) -> tuple[int, SessionReopenResult | None]:
    if not execute:
        output("DRY RUN: no Bluetooth or BlueZ state will be changed.")
        output("sessions=2; AAP channels=2; descriptor state shared=no")
        output("descriptor_policy=canonical-required; ACK-only continuation=no")
        output("automatic BlueZ reconnect=no; Bumble fallback=no")
        output("kernel_local_rx_imtu=2048")
        output(
            f"samples_per_session={samples_per_session}; "
            f"reopen_delay={reopen_delay:g}s; "
            f"descriptor_timeout={descriptor_timeout:g}s"
        )
        return 0, None

    observer = observer_factory()
    bundles: list[ReopenSessionBundle] = []
    checkpoints: list[BlueZReopenCheckpoint] = []
    failure: BaseException | None = None
    category = SessionReopenResultCategory.OTHER_FAILURE
    try:
        try:
            initial = await observer.open()
        except BaseException:
            category = SessionReopenResultCategory.BLUEZ_STATE_CHANGED
            raise
        checkpoints.append(initial)
        output(_checkpoint_line(initial))
        if not initial.invariant_holds:
            category = SessionReopenResultCategory.BLUEZ_STATE_CHANGED
            raise RuntimeError("initial BlueZ state invariant failed")

        first = bundle_factory(
            descriptor_timeout=descriptor_timeout,
            start_timeout=start_timeout,
            stop_timeout=stop_timeout,
            output=output,
        )
        bundles.append(first)
        first_error: BaseException | None = None
        try:
            await _exercise_session(
                first,
                session_index=1,
                samples=samples_per_session,
                report_timeout=report_timeout,
                output=output,
            )
        except BaseException as error:
            first_error = error
            if (
                getattr(error, "category", None)
                is ProductionSessionCategory.PREFLIGHT_FAILED
            ):
                category = SessionReopenResultCategory.BLUEZ_STATE_CHANGED
            raise
        finally:
            try:
                await first.session.close()
            except BaseException as close_error:
                if first_error is None:
                    raise
                first_error.add_note("session 1 close also failed")

        try:
            after_first = await observer.checkpoint("after_session_1_close")
        except BaseException:
            category = SessionReopenResultCategory.BLUEZ_STATE_CHANGED
            raise
        checkpoints.append(after_first)
        output(_checkpoint_line(after_first))
        if not after_first.invariant_holds:
            category = SessionReopenResultCategory.BLUEZ_STATE_CHANGED
            raise RuntimeError("BlueZ state changed after session 1")

        await sleep(reopen_delay)
        try:
            before_second = await observer.checkpoint("before_session_2_open")
        except BaseException:
            category = SessionReopenResultCategory.BLUEZ_STATE_CHANGED
            raise
        checkpoints.append(before_second)
        output(_checkpoint_line(before_second))
        if not before_second.invariant_holds:
            category = SessionReopenResultCategory.BLUEZ_STATE_CHANGED
            raise RuntimeError("BlueZ state changed before session 2")

        second = bundle_factory(
            descriptor_timeout=descriptor_timeout,
            start_timeout=start_timeout,
            stop_timeout=stop_timeout,
            output=output,
        )
        bundles.append(second)
        second_error: BaseException | None = None
        try:
            await _exercise_session(
                second,
                session_index=2,
                samples=samples_per_session,
                report_timeout=report_timeout,
                output=output,
            )
        except BaseException as error:
            second_error = error
            category = _classify_session_2_failure(error, second)
            raise
        finally:
            try:
                await second.session.close()
            except BaseException as close_error:
                if second_error is None:
                    raise
                second_error.add_note("session 2 close also failed")

        category = SessionReopenResultCategory.BOTH_SESSIONS_PASS
    except BaseException as error:
        failure = error
    finally:
        observer.close()

    if isinstance(failure, asyncio.CancelledError):
        raise failure
    result = _aggregate(bundles, checkpoints, category, failure)
    _print_result(result, verbose=verbose, output=output)
    return (
        0 if category is SessionReopenResultCategory.BOTH_SESSIONS_PASS else 1,
        result,
    )


def main(
    argv: Sequence[str] | None = None, *, stream: TextIO | None = None
) -> int:
    args = build_parser().parse_args(argv)

    def emit(message: str) -> None:
        print(message, file=stream)

    try:
        status, _ = asyncio.run(
            run_probe(
                execute=args.execute,
                samples_per_session=args.samples_per_session,
                reopen_delay=args.reopen_delay,
                descriptor_timeout=args.descriptor_timeout,
                report_timeout=args.report_timeout,
                start_timeout=args.start_timeout,
                stop_timeout=args.stop_timeout,
                verbose=args.verbose,
                output=emit,
            )
        )
        return status
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
