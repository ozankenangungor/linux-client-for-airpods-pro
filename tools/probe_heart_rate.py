#!/usr/bin/env python3.14
"""Safe-by-default bounded probe for the proven AAP heart-rate sequence."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TextIO

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
    DEFAULT_SAMPLE_TARGET,
    DEFAULT_STREAM_TIMEOUT,
    ControlFrameSummary,
    HeartRateActivationSession,
    HeartRateBootstrapAckTimeoutError,
    HeartRateCompletion,
    HeartRateConnectAckTimeoutError,
    HeartRateNoSamplesError,
    HeartRateProbeResult,
    HeartRateProbeSession,
    HeartRateProgress,
    HeartRateSessionError,
    HeartRateStartAckTimeoutError,
)
from airpods_hr.heartrate import HeartRateReport
from airpods_hr.pairing import BlueZPairingStore, PairingStoreError
from airpods_hr.sdp import SDPCompatibilityError


LiveRunner = Callable[
    [Callable[[str], None], int, float], Awaitable[HeartRateProbeResult]
]


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
        description="Run a bounded, opt-in AirPods heart-rate experiment."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the state-changing heart-rate experiment",
    )
    parser.add_argument(
        "--samples",
        type=_bounded_int(1, 10),
        default=DEFAULT_SAMPLE_TARGET,
        help="valid sample target (default: 5; range: 1-10)",
    )
    parser.add_argument(
        "--stream-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_STREAM_TIMEOUT,
        help="bounded stream window in seconds (default: 12; range: 1-30)",
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


def _heart_rate_progress(output: Callable[[str], None]):
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
        elif event is HeartRateProgress.SAMPLE and report is not None:
            sample_index += 1
            output(f"Heart rate {sample_index}: {report.bpm} bpm")
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


def _u16_or_unavailable(value: int | None) -> str:
    return "unavailable" if value is None else f"0x{value:04x}"


def _u8_or_unavailable(value: int | None) -> str:
    return "unavailable" if value is None else f"0x{value:02x}"


def _u8_tuple_or_unavailable(value: tuple[int, ...] | None) -> str:
    if value is None:
        return "unavailable"
    if not value:
        return "()"
    rendered = ", ".join(f"0x{item:02x}" for item in value)
    if len(value) == 1:
        rendered += ","
    return f"({rendered})"


def _u8_pair_or_unavailable(first: int | None, second: int | None) -> str:
    if first is None or second is None:
        return "unavailable"
    return f"(0x{first:02x}, 0x{second:02x})"


def _int_tuple_or_unavailable(value: tuple[int, ...] | None) -> str:
    if value is None:
        return "unavailable"
    if not value:
        return "()"
    rendered = ", ".join(str(item) for item in value)
    if len(value) == 1:
        rendered += ","
    return f"({rendered})"


def _print_control_frame_summary(
    output: Callable[[str], None],
    index: int,
    summary: ControlFrameSummary,
) -> None:
    output(f"Control frame {index}:")
    output(f"  length={summary.length}")
    output(f"  header_u16_2_3={_u16_or_unavailable(summary.header_u16_2_3)}")
    output(f"  header_u16_4_5={_u16_or_unavailable(summary.header_u16_4_5)}")
    output(f"  word_u16_8_9={_u16_or_unavailable(summary.word_u16_8_9)}")
    output(f"  word_u16_10_11={_u16_or_unavailable(summary.word_u16_10_11)}")
    output(
        "  outer service envelope="
        f"{_yes_no_unavailable(summary.outer_service_envelope_match)}"
    )
    output(
        "  fixed word 0x0010="
        f"{_yes_no_unavailable(summary.fixed_word_10_00_match)}"
    )
    output(
        "  trailing length consistent="
        f"{_yes_no_unavailable(summary.trailing_length_consistent)}"
    )
    output(
        "  tag 0x08 at offset 12="
        f"{_yes_no_unavailable(summary.tag_08_at_offset_12)}"
    )
    output(
        "  service-0x0e ACK suffix="
        f"{_yes_no_unavailable(summary.service_ack_suffix_0e)}"
    )
    output(
        "  service-0x13 ACK suffix="
        f"{_yes_no_unavailable(summary.service_ack_suffix_13)}"
    )
    output(
        "  candidate identifier terminated="
        f"{_yes_no_unavailable(summary.candidate_identifier_terminated)}"
    )
    identifier_octets = summary.candidate_identifier_octets
    output(
        "  candidate identifier octets="
        f"{identifier_octets if identifier_octets is not None else 'unavailable'}"
    )
    output(
        "  candidate identifier canonical="
        f"{_yes_no_unavailable(summary.candidate_identifier_canonical)}"
    )
    output(
        "  current 1/2-byte canonical identifier="
        f"{_yes_no_unavailable(summary.identifier_is_current_canonical_1_or_2)}"
    )
    post_identifier_length = summary.post_identifier_length
    rendered_post_identifier_length = (
        post_identifier_length
        if post_identifier_length is not None
        else "unavailable"
    )
    output(
        "  post-identifier length="
        f"{rendered_post_identifier_length}"
    )
    rendered_prefix = _u8_pair_or_unavailable(
        summary.post_identifier_prefix_octet_0,
        summary.post_identifier_prefix_octet_1,
    )
    output(
        "  post-identifier prefix octets="
        f"{rendered_prefix}"
    )
    output(
        "  post-identifier starts 10 01="
        f"{_yes_no_unavailable(summary.post_identifier_starts_10_01)}"
    )
    output(
        "  post-identifier prefix is observed="
        f"{_yes_no_unavailable(summary.post_identifier_prefix_is_observed)}"
    )
    output(
        "  post-identifier field tag="
        f"{_u8_or_unavailable(summary.post_identifier_field_tag)}"
    )
    output(
        "  post-identifier field parameter="
        f"{_u8_or_unavailable(summary.post_identifier_field_parameter)}"
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
        "  remainder bootstrap-0x10 shape="
        f"{_yes_no_unavailable(summary.remainder_is_bootstrap_10_shape)}"
    )
    output(
        "  remainder bootstrap-0x11/0x12/0x13 shape="
        f"{_yes_no_unavailable(summary.remainder_is_bootstrap_11_12_13_shape)}"
    )
    output(
        "  terminal tag 0x08="
        f"{_yes_no_unavailable(summary.terminal_tag_08)}"
    )
    output(
        "  terminal value="
        f"{_u8_or_unavailable(summary.terminal_value)}"
    )
    group_count = summary.observed_62_02_08_group_count
    output(
        "  observed 62-02-08 group count="
        f"{group_count if group_count is not None else 'unavailable'}"
    )
    output(
        "  observed 62-02-08 terminal values="
        f"{_u8_tuple_or_unavailable(summary.observed_62_02_08_terminal_values)}"
    )
    output(
        "  observed 62-02-08 group offsets="
        f"{_int_tuple_or_unavailable(summary.observed_62_02_08_group_offsets)}"
    )
    output(
        "  bootstrap tail 0x10 suffix present="
        f"{_yes_no_unavailable(summary.bootstrap_tail_10_suffix_present)}"
    )
    output(
        "  bootstrap tail 0x11/0x12/0x13 suffix present="
        f"{_yes_no_unavailable(summary.bootstrap_tail_11_12_13_suffix_present)}"
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
    relative = summary.relative_to_stop_head_seconds
    output(
        "  after STOP_HEAD="
        f"{f'+{relative:.3f}s' if relative is not None else 'unavailable'}"
    )


async def run_live_probe(
    output: Callable[[str], None],
    sample_target: int = DEFAULT_SAMPLE_TARGET,
    stream_timeout: float = DEFAULT_STREAM_TIMEOUT,
) -> HeartRateProbeResult:
    output(f"Runtime name profile: {RuntimeNameProfile.PROJECT_DEFAULT.value}")
    output("Classic host-state snapshot: disabled")
    discovery_backend = DBusNextManagedObjectsBackend()
    bluez_backend = DBusNextBlueZBackend()
    try:
        await discovery_backend.connect()
        await bluez_backend.connect()
        handoff, transport = create_controller_handoff_transport(bluez_backend)
        await transport.ensure_available()
        secure_session = ClassicAuthenticationSession(
            BlueZDeviceDiscovery(discovery_backend),
            BlueZPairingStore(),
            handoff,
            BumbleClassicRuntimeFactory(),
            progress=_authentication_progress(output),
        )
        session = HeartRateProbeSession(
            secure_session,
            AAPChannelSession(progress=_channel_progress(output)),
            AAPHandshakeSession(progress=_aap_progress(output)),
            HeartRateActivationSession(
                sample_target=sample_target,
                stream_timeout=stream_timeout,
                progress=_heart_rate_progress(output),
            ),
            sdp_installed=lambda: output(
                "SDP compatibility records: installed"
            ),
            bluez_restored=lambda: output("BlueZ restoration: OK"),
        )
        result = await session.run()
        return result
    finally:
        discovery_backend.close()
        bluez_backend.close()


async def run_probe(
    *,
    execute: bool,
    sample_target: int = DEFAULT_SAMPLE_TARGET,
    stream_timeout: float = DEFAULT_STREAM_TIMEOUT,
    output: Callable[[str], None] = print,
    live_runner: LiveRunner = run_live_probe,
) -> int:
    if not execute:
        output("DRY RUN: no Bluetooth state will be changed.")
        output(f"Runtime name profile: {RuntimeNameProfile.PROJECT_DEFAULT.value}")
        output("Classic host-state snapshot: disabled")
        output(f"Valid heart-rate sample target: {sample_target}")
        output(f"Maximum stream observation: {stream_timeout:g} seconds")
        output("Planned operations:")
        output("  1. Discover one paired AirPods candidate.")
        output("  2. Load its existing local Classic credentials.")
        output("  3. Hand the powered-down controller from BlueZ to Bumble.")
        output("  4. Install four SDP records and establish Classic security.")
        output("  5. Open AAP PSM 0x1001 and complete the known handshake.")
        output("  6. Send the seven proven HR activation commands in order.")
        output("  7. Observe a bounded number of parsed BPM samples.")
        output("  8. Send STOP_HR then HR_OFF and restore BlueZ.")
        output("  9. Send no workout opcode 0x44 packet.")
        output("Use --execute only after reviewing the experiment.")
        return 0

    try:
        result = await live_runner(output, sample_target, stream_timeout)
        count = len(result.heart_rate.samples)
        if result.heart_rate.completion is HeartRateCompletion.TARGET_REACHED:
            output(f"PASS: {count} heart-rate samples observed.")
            return 0
        output(
            f"PARTIAL: {count} of {result.heart_rate.requested_samples} "
            "heart-rate samples observed before the bounded deadline."
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
    except HeartRateNoSamplesError as error:
        output("FAIL: no valid heart-rate sample was observed.")
        output(f"Control frames observed: {error.control_frames_observed}")
        output(f"Non-HR frames observed: {error.non_hr_frames}")
        output(f"Malformed HR frames observed: {error.malformed_hr_frames}")
    except HeartRateSessionError:
        output("FAIL: heart-rate session failed; cleanup was attempted.")
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
        output("FAIL: unexpected heart-rate probe error.")
    return 1


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit = print if stream is None else lambda message: print(message, file=stream)
    try:
        return asyncio.run(
            run_probe(
                execute=args.execute,
                sample_target=args.samples,
                stream_timeout=args.stream_timeout,
                output=emit,
            )
        )
    except KeyboardInterrupt:
        emit("FAIL: interrupted; HR stop and restoration were attempted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
