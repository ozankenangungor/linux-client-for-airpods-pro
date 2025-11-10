#!/usr/bin/env python3.14
"""Safe-by-default bounded validation probe for the continuous HR core."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, TextIO

from airpods_hr.aap import AAPHandshakeError, AAPHandshakeSession, AAPProgress
from airpods_hr.aap_channel import (
    AAPChannel,
    AAPChannelError,
    AAPChannelProgress,
    AAPChannelSession,
)
from airpods_hr.authentication import (
    AuthenticationProgress,
    BumbleClassicRuntimeFactory,
    ClassicAuthenticationError,
    ClassicAuthenticationSession,
    create_controller_handoff_transport,
)
from airpods_hr.bluetooth import (
    AdapterRestoreError,
    DBusNextBlueZBackend,
    HandoffError,
)
from airpods_hr.classic_diagnostics import RuntimeNameProfile
from airpods_hr.discovery import (
    BlueZDeviceDiscovery,
    DBusNextManagedObjectsBackend,
    MultipleAirPodsCandidatesError,
    NoAirPodsCandidatesError,
)
from airpods_hr.heart_rate_session import (
    ControlFrameSummary,
    HeartRateBootstrapAckTimeoutError,
    HeartRateConnectAckTimeoutError,
    HeartRateMonitorActivationSession,
    HeartRateMonitorResult,
    HeartRateMonitorSession,
    HeartRateProgress,
    HeartRateSessionError,
    HeartRateStartAckTimeoutError,
)
from airpods_hr.heartrate import HeartRateReport
from airpods_hr.pairing import BlueZPairingStore, PairingStoreError
from airpods_hr.sdp import SDPCompatibilityError


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
        description="Validate the continuous AirPods heart-rate monitor core."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the state-changing continuous-monitor validation",
    )
    parser.add_argument(
        "--samples",
        type=_bounded_int(1, 20),
        default=DEFAULT_VALIDATION_SAMPLE_TARGET,
        help="validation sample target (default: 8; range: 1-20)",
    )
    parser.add_argument(
        "--monitor-window",
        type=_bounded_float(3.0, 30.0),
        default=DEFAULT_VALIDATION_WINDOW_SECONDS,
        help="post-start safety window in seconds (default: 15; range: 3-30)",
    )
    return parser


def _authentication_progress(output: Callable[[str], None]):
    def emit(event: AuthenticationProgress, detail: str | None) -> None:
        if event is AuthenticationProgress.DEVICE_SELECTED:
            output(f"Device: {detail or 'AirPods'}")
        elif event is AuthenticationProgress.CONNECTED:
            output("BR/EDR connection: OK")
        elif event is AuthenticationProgress.AUTHENTICATED:
            output("Authentication: OK")
        elif event is AuthenticationProgress.ENCRYPTED:
            output("Encryption: OK")
        elif event is AuthenticationProgress.DISCONNECTED:
            output("Disconnect: OK")
        elif event is AuthenticationProgress.REPLACEMENT_KEY_REPORTED:
            output("Controller reported a new/replacement link key.")

    return emit


def _channel_progress(output: Callable[[str], None]):
    def emit(event: AAPChannelProgress, channel: AAPChannel | None) -> None:
        if event is AAPChannelProgress.OPENED:
            output("AAP L2CAP channel: OPEN")
        elif event is AAPChannelProgress.CLOSED:
            output("AAP close: OK")

    return emit


def _aap_progress(output: Callable[[str], None]):
    def emit(event: AAPProgress) -> None:
        if event is AAPProgress.HANDSHAKE_SENT:
            output("AAP handshake request: sent")
        elif event is AAPProgress.ACK_OBSERVED:
            output("AAP handshake ACK: OK")
        elif event is AAPProgress.DESCRIPTORS_OBSERVED:
            output("Sensor framework descriptor: observed")
            output("HeartRateService: observed")

    return emit


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


def _yes_no_unavailable(value: bool | None) -> str:
    if value is None:
        return "unavailable"
    return "yes" if value else "no"


def _print_control_frame_summary(
    output: Callable[[str], None],
    index: int,
    summary: ControlFrameSummary,
) -> None:
    """Print bounded structural metadata without raw bytes or identifiers."""

    identifier_octets = (
        str(summary.candidate_identifier_octets)
        if summary.candidate_identifier_octets is not None
        else "unavailable"
    )
    elapsed = (
        f"+{summary.relative_to_stop_head_seconds:.3f}s"
        if summary.relative_to_stop_head_seconds is not None
        else "unavailable"
    )
    output(f"Control frame {index}:")
    output(f"  length={summary.length}")
    output(
        "  outer service envelope="
        f"{_yes_no_unavailable(summary.outer_service_envelope_match)}"
    )
    output(
        "  trailing length consistent="
        f"{_yes_no_unavailable(summary.trailing_length_consistent)}"
    )
    output(f"  candidate identifier octets={identifier_octets}")
    output(
        "  current 1/2-byte canonical identifier="
        f"{_yes_no_unavailable(summary.identifier_is_current_canonical_1_or_2)}"
    )
    output(
        "  post-identifier prefix is observed="
        f"{_yes_no_unavailable(summary.post_identifier_prefix_is_observed)}"
    )
    output(
        "  remainder ACK-0x0e shape="
        f"{_yes_no_unavailable(summary.remainder_is_ack_0e_shape)}"
    )
    output(
        "  remainder ACK-0x13 shape="
        f"{_yes_no_unavailable(summary.remainder_is_ack_13_shape)}"
    )
    output(
        "  bootstrap tail 0x10="
        f"{_yes_no_unavailable(summary.bootstrap_tail_10)}"
    )
    output(
        "  bootstrap tail 0x11/0x12/0x13="
        f"{_yes_no_unavailable(summary.bootstrap_tail_11_12_13)}"
    )
    output(
        "  HR marker="
        f"{_yes_no_unavailable(summary.heart_rate_marker_present)}"
    )
    output(f"  after STOP_HEAD={elapsed}")


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


async def run_live_probe(
    output: Callable[[str], None],
    sample_target: int = DEFAULT_VALIDATION_SAMPLE_TARGET,
    monitor_window: float = DEFAULT_VALIDATION_WINDOW_SECONDS,
) -> MonitorProbeOutcome:
    output(f"Runtime name profile: {RuntimeNameProfile.PROJECT_DEFAULT.value}")
    output("Classic host-state snapshot: disabled")
    discovery_backend = DBusNextManagedObjectsBackend()
    bluez_backend = DBusNextBlueZBackend()
    try:
        await discovery_backend.connect()
        await bluez_backend.connect()
        handoff, transport = create_controller_handoff_transport(bluez_backend)
        await transport.ensure_available()
        stop_event = asyncio.Event()
        start_acknowledged = asyncio.Event()
        watchdog_state = _WatchdogState()
        secure_session = ClassicAuthenticationSession(
            BlueZDeviceDiscovery(discovery_backend),
            BlueZPairingStore(),
            handoff,
            BumbleClassicRuntimeFactory(),
            progress=_authentication_progress(output),
        )
        session = HeartRateMonitorSession(
            secure_session,
            AAPChannelSession(progress=_channel_progress(output)),
            AAPHandshakeSession(progress=_aap_progress(output)),
            HeartRateMonitorActivationSession(
                progress=_heart_rate_progress(
                    output,
                    sample_target,
                    stop_event,
                    start_acknowledged,
                )
            ),
            sdp_installed=lambda: output(
                "SDP compatibility records: installed"
            ),
            bluez_restored=lambda: output("BlueZ restoration: OK"),
        )
        result = await _run_session_with_watchdog(
            session,
            stop_event,
            start_acknowledged,
            monitor_window,
            watchdog_state,
        )
        return MonitorProbeOutcome(result, watchdog_state.expired)
    finally:
        discovery_backend.close()
        bluez_backend.close()


async def run_probe(
    *,
    execute: bool,
    sample_target: int = DEFAULT_VALIDATION_SAMPLE_TARGET,
    monitor_window: float = DEFAULT_VALIDATION_WINDOW_SECONDS,
    output: Callable[[str], None] = print,
    live_runner: LiveRunner = run_live_probe,
) -> int:
    if not execute:
        output("DRY RUN: no Bluetooth state will be changed.")
        output("Continuous monitor core validation")
        output(f"Validation sample target: {sample_target}")
        output(f"Post-start safety window: {monitor_window:g} seconds")
        output("Planned operations:")
        output("  1. Reuse the reviewed Classic, SDP, AAP, and HR setup path.")
        output("  2. Start the safety window only after START_HR acknowledgement.")
        output("  3. Stop through the monitor event at the target or window limit.")
        output("  4. Send STOP_HR then HR_OFF and restore BlueZ.")
        output("  5. Perform no reconnect and send no opcode 0x44 packet.")
        output("Use --execute only after reviewing the experiment.")
        return 0

    try:
        outcome = await live_runner(output, sample_target, monitor_window)
        count = outcome.result.heart_rate.samples_observed
        if count >= sample_target:
            output(f"PASS: {count} continuous heart-rate samples observed.")
            return 0
        if outcome.watchdog_expired:
            output(
                f"PARTIAL: {count} of {sample_target} continuous heart-rate "
                "samples observed before the validation window ended."
            )
        else:
            output(
                f"PARTIAL: {count} of {sample_target} continuous heart-rate "
                "samples observed before validation stopped."
            )
        return 2
    except NoAirPodsCandidatesError:
        output("FAIL: no paired AirPods candidate was found.")
    except MultipleAirPodsCandidatesError:
        output("FAIL: multiple paired AirPods candidates require selection.")
    except PairingStoreError:
        output("FAIL: existing local Classic credentials could not be loaded.")
    except SDPCompatibilityError:
        output("FAIL: local adapter PnP identity is unavailable or malformed.")
    except HeartRateBootstrapAckTimeoutError as error:
        output("FAIL: STOP_HEAD acknowledgement was not observed.")
        output(f"Application payloads sent: {error.application_payloads_sent}")
        output(
            "Frames queued before STOP_HEAD: "
            f"{error.frames_queued_before_stop_head}"
        )
        output(f"Control frames observed: {error.frames_observed}")
        for index, summary in enumerate(error.summaries, start=1):
            _print_control_frame_summary(output, index, summary)
    except HeartRateConnectAckTimeoutError as error:
        output("FAIL: AAP control-channel acknowledgement was not observed.")
        output(f"Control frames observed: {error.frames_observed}")
    except HeartRateStartAckTimeoutError as error:
        output("FAIL: heart-rate start acknowledgement was not observed.")
        output(f"Control frames observed: {error.frames_observed}")
    except HeartRateSessionError:
        output("FAIL: heart-rate monitor failed; cleanup was attempted.")
    except (AAPHandshakeError, AAPChannelError):
        output("FAIL: AAP session failed; cleanup was attempted.")
    except AdapterRestoreError:
        output("FAIL: BlueZ adapter restoration reported an error.")
    except HandoffError:
        output("FAIL: controller handoff failed; cleanup was attempted.")
    except ClassicAuthenticationError:
        output("FAIL: Classic security session failed; cleanup was attempted.")
    except asyncio.CancelledError:
        raise
    except Exception:
        output("FAIL: unexpected continuous-monitor probe error.")
    return 1


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit = print if stream is None else lambda message: print(message, file=stream)
    try:
        return asyncio.run(
            run_probe(
                execute=args.execute,
                sample_target=args.samples,
                monitor_window=args.monitor_window,
                output=emit,
            )
        )
    except KeyboardInterrupt:
        emit("FAIL: interrupted; cleanup and restoration were attempted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
