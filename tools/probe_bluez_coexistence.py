#!/usr/bin/env python3.14
"""Safe-by-default BlueZ coexistence feasibility probe."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TextIO

from airpods_hr.aap import (
    AAP_FRAME_SUMMARY_LIMIT,
    AAPFrameSummary,
    AAPHandshakeSession,
    AAPProgress,
    AAPType2BFrameSummary,
    HandshakeObservation,
)
from airpods_hr.bluez_coexistence import (
    DEFAULT_DBUS_TIMEOUT,
    DEFAULT_DESCRIPTOR_TIMEOUT,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_L2CAP_CONNECT_TIMEOUT,
    BlueZCompatibilityRegistration,
    BlueZCoexistenceSession,
    CoexistenceFailure,
    CoexistenceResult,
    DBusNextBlueZCoexistenceClient,
    KernelL2CAPLocalRXObservation,
    KernelL2CAPTransport,
)
from airpods_hr.bluez_sdp_audit import (
    BlueZSDPAuditResult,
    SDPComparisonStatus,
    audit_bluez_sdp_identity,
)
from airpods_hr.heart_rate_session import (
    ControlFrameSummary,
    DEFAULT_CONTROL_SUMMARY_LIMIT,
    DEFAULT_SAMPLE_TARGET,
    DEFAULT_STREAM_TIMEOUT,
    HeartRateActivationSession,
    HeartRateProgress,
)
from airpods_hr.heartrate import HeartRateReport


LiveRunner = Callable[
    [Callable[[str], None], int, float, float, float, float, float, bool, bool],
    Awaitable[CoexistenceResult],
]
AuditRunner = Callable[
    [Callable[[str], None], float], Awaitable[BlueZSDPAuditResult]
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
        description=(
            "Test AAP heart rate through a kernel L2CAP socket while BlueZ "
            "retains controller ownership."
        )
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--execute",
        action="store_true",
        help="perform the opt-in coexistence experiment",
    )
    action.add_argument(
        "--audit-sdp",
        action="store_true",
        help="read BlueZ local SDP evidence without registering profiles",
    )
    parser.add_argument(
        "--samples",
        type=_bounded_int(3, 10),
        default=DEFAULT_SAMPLE_TARGET,
        help="canonical HR sample target (default: 5; range: 3-10)",
    )
    parser.add_argument(
        "--dbus-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_DBUS_TIMEOUT,
        help="timeout for each BlueZ D-Bus operation (default: 5)",
    )
    parser.add_argument(
        "--connect-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_L2CAP_CONNECT_TIMEOUT,
        help="kernel L2CAP connect timeout (default: 10)",
    )
    parser.add_argument(
        "--handshake-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_HANDSHAKE_TIMEOUT,
        help="AAP ACK timeout (default: 5)",
    )
    parser.add_argument(
        "--descriptor-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_DESCRIPTOR_TIMEOUT,
        help="post-ACK descriptor window in seconds (default: 3)",
    )
    parser.add_argument(
        "--hr-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_STREAM_TIMEOUT,
        help="bounded HR sample collection window (default: 12)",
    )
    experimental = parser.add_mutually_exclusive_group()
    experimental.add_argument(
        "--experimental-ack-only-hr",
        action="store_true",
        help=(
            "after an exact ACK and zero-drop descriptor timeout only, run "
            "the canonical HR sequence as a probe-only experiment"
        ),
    )
    experimental.add_argument(
        "--experimental-fresh-bluez-acl",
        action="store_true",
        help=(
            "require a paired but disconnected AirPods candidate, then let "
            "the kernel L2CAP connect establish a fresh BlueZ-managed ACL"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include safe errno, D-Bus error name, or exception category",
    )
    return parser


def _aap_progress(
    output: Callable[[str], None], ack_event: asyncio.Event | None = None
):
    def emit(event: AAPProgress) -> None:
        if event is AAPProgress.HANDSHAKE_SENT:
            output("AAP handshake request: sent")
        elif event is AAPProgress.ACK_OBSERVED:
            if ack_event is not None:
                ack_event.set()
            output("AAP handshake ACK: observed")
        elif event is AAPProgress.DESCRIPTORS_OBSERVED:
            output("AAP descriptor evidence: sensor framework and HR service")

    return emit


def _heart_rate_progress(
    output: Callable[[str], None],
    activation_event: asyncio.Event,
    transport: KernelL2CAPTransport,
):
    sample_index = 0

    def emit(event: HeartRateProgress, report: HeartRateReport | None) -> None:
        nonlocal sample_index
        if event is HeartRateProgress.START_ACKNOWLEDGED:
            transport.arm_hr_stream_observation()
            activation_event.set()
            output("Heart-rate activation ACK: observed")
        elif event is HeartRateProgress.SAMPLE and report is not None:
            sample_index += 1
            output(
                f"HR sample {sample_index}: bpm={report.bpm}, "
                f"sequence={report.sequence}, field_5={report.field_5}, "
                f"flags={report.flags}, raw_report_bytes={len(report.raw_report)}"
            )
        elif event is HeartRateProgress.STOP_ACKNOWLEDGED:
            output("Heart-rate stop ACK: observed")
        elif event is HeartRateProgress.STOP_ACK_MISSING:
            output("Heart-rate stop ACK: not observed within cleanup timeout")
        elif event is HeartRateProgress.HR_OFF_SENT:
            output("HR_OFF: sent")

    return emit


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _optional_integer(value: int | None) -> str:
    return "unavailable" if value is None else str(value)


def _optional_boolean(value: bool | None) -> str:
    return "unavailable" if value is None else _yes_no(value)


def _optional_hex(value: int | None, width: int) -> str:
    return "unavailable" if value is None else f"0x{value:0{width}X}"


def _print_kernel_local_rx_summary(
    output: Callable[[str], None],
    observation: KernelL2CAPLocalRXObservation,
) -> None:
    output("KERNEL L2CAP LOCAL RX SUMMARY")
    output(f"  target_imtu={observation.target_imtu}")
    output(f"  options_source={observation.options_source}")
    output(f"  before_imtu={_optional_integer(observation.before_imtu)}")
    output(f"  after_imtu={_optional_integer(observation.after_imtu)}")
    for field_name in (
        "omtu",
        "flush_to",
        "mode",
        "fcs",
        "max_tx",
        "txwin_size",
    ):
        preserved = getattr(observation, f"preserved_{field_name}")
        output(f"  preserved_{field_name}={_optional_boolean(preserved)}")
    output(f"  verified={_yes_no(observation.verified)}")


def _print_type_2b_summary(
    output: Callable[[str], None], summary: AAPType2BFrameSummary
) -> None:
    output("    Type-0x002B structural summary:")
    output(f"      frame_length={summary.frame_length}")
    output(f"      header_u8_6={_optional_hex(summary.header_u8_6, 2)}")
    output(
        "      declared_body_length_u16_7_8="
        f"{_optional_integer(summary.declared_body_length_u16_7_8)}"
    )
    output(
        "      actual_body_length_after_offset_17="
        f"{_optional_integer(summary.actual_body_length_after_offset_17)}"
    )
    output(
        "      declared_body_length_consistent="
        f"{_optional_boolean(summary.declared_body_length_consistent)}"
    )
    output(
        "      body_aligned_to_17_bytes="
        f"{_optional_boolean(summary.body_aligned_to_17_bytes)}"
    )
    output(
        "      record_count_17="
        f"{_optional_integer(summary.record_count_17)}"
    )
    output(
        "      record_suffix_distinct_count="
        f"{_optional_integer(summary.record_suffix_distinct_count)}"
    )
    for index, suffix in enumerate(summary.record_suffix_histogram, 1):
        output(
            f"      suffix_pair_{index}: "
            f"field_u8=0x{suffix.suffix_field_u8:02X}, "
            f"field_u16=0x{suffix.suffix_field_u16:04X}, "
            f"count={suffix.count}"
        )
    output(
        "      unit_bytes_8_13_uniform="
        f"{_optional_boolean(summary.unit_bytes_8_13_uniform)}"
    )


def _print_frame_summaries(
    output: Callable[[str], None],
    label: str,
    summaries: tuple[AAPFrameSummary, ...],
    limit: int,
) -> int:
    emitted = 0
    for index, summary in enumerate(summaries[:limit], 1):
        output(
            f"  {label}_frame_{index}: length={summary.length}, "
            f"header_u16_2_3={_optional_hex(summary.header_u16_2_3, 4)}, "
            f"header_u16_4_5={_optional_hex(summary.header_u16_4_5, 4)}"
        )
        if summary.type_2b_summary is not None:
            _print_type_2b_summary(output, summary.type_2b_summary)
        emitted += 1
    return emitted


def _print_descriptor_timeout_diagnostics(
    output: Callable[[str], None], observation: HandshakeObservation
) -> None:
    evidence = observation.evidence
    output("AAP descriptor timeout diagnostics:")
    output(f"  exact_ack_observed={_yes_no(observation.ack_observed)}")
    output(f"  pre_ack_frames={observation.pre_ack_frame_count}")
    output(f"  post_ack_frames={observation.post_ack_frame_count}")
    output(f"  receive_frames_dropped={observation.receive_frames_dropped}")
    output(f"  sensor_framework={_yes_no(evidence.sensor_framework)}")
    output(f"  heart_rate_service={_yes_no(evidence.heart_rate_service)}")
    output(f"  heart_rate={_yes_no(evidence.heart_rate)}")
    output(f"  heartrate_access={_yes_no(evidence.heartrate_access)}")
    emitted = _print_frame_summaries(
        output,
        "pre_ack",
        observation.pre_ack_frame_summaries,
        AAP_FRAME_SUMMARY_LIMIT,
    )
    _print_frame_summaries(
        output,
        "post_ack",
        observation.post_ack_frame_summaries,
        AAP_FRAME_SUMMARY_LIMIT - emitted,
    )


def _print_control_frame_summary(
    output: Callable[[str], None],
    index: int,
    summary: ControlFrameSummary,
) -> None:
    output(f"  stream_frame_{index}:")
    output(f"    length={summary.length}")
    output(
        "    header_u16_2_3="
        f"{_optional_hex(summary.header_u16_2_3, 4)}"
    )
    output(
        "    header_u16_4_5="
        f"{_optional_hex(summary.header_u16_4_5, 4)}"
    )
    output(
        "    heart_rate_marker_present="
        f"{_yes_no(summary.heart_rate_marker_present)}"
    )
    output(
        "    outer_service_envelope_match="
        f"{_yes_no(summary.outer_service_envelope_match)}"
    )
    output(
        "    service_ack_suffix_0e="
        f"{_yes_no(summary.service_ack_suffix_0e)}"
    )
    output(
        "    service_ack_suffix_13="
        f"{_yes_no(summary.service_ack_suffix_13)}"
    )
    output(
        "    candidate_identifier_canonical="
        f"{_optional_boolean(summary.candidate_identifier_canonical)}"
    )
    output(
        "    trailing_length_consistent="
        f"{_optional_boolean(summary.trailing_length_consistent)}"
    )


def _print_hr_timeout_diagnostics(
    output: Callable[[str], None], error: CoexistenceFailure
) -> None:
    diagnostics = error.hr_timeout_diagnostics
    if diagnostics is None:
        return
    observation = diagnostics.stream_observation
    output("HR stream timeout diagnostics:")
    output(
        "  stream_observation_armed="
        f"{_yes_no(observation.observation_armed)}"
    )
    output(
        "  stream_observation_cleanly_disarmed="
        f"{_yes_no(observation.observation_cleanly_disarmed)}"
    )
    output(f"  post_start_frames={observation.frames_observed}")
    output(f"  frames_with_hr_marker={observation.frames_with_hr_marker}")
    output(f"  frames_without_hr_marker={observation.frames_without_hr_marker}")
    output(
        "  canonical_non_hr_frames="
        f"{diagnostics.canonical_non_hr_frames}"
    )
    output(
        "  canonical_malformed_hr_frames="
        f"{diagnostics.canonical_malformed_hr_frames}"
    )
    output(
        "  control_frames_observed="
        f"{diagnostics.control_frames_observed}"
    )
    output(
        "  receive_frames_dropped="
        f"{observation.receive_frames_dropped}"
    )
    output(
        "  frame_count_corresponds="
        f"{_yes_no(diagnostics.frame_count_corresponds)}"
    )
    for index, summary in enumerate(
        observation.frame_summaries[:DEFAULT_CONTROL_SUMMARY_LIMIT], 1
    ):
        _print_control_frame_summary(output, index, summary)


async def run_live_probe(
    output: Callable[[str], None],
    samples: int,
    dbus_timeout: float,
    connect_timeout: float,
    handshake_timeout: float,
    descriptor_timeout: float,
    hr_timeout: float,
    experimental_ack_only_hr: bool,
    experimental_fresh_bluez_acl: bool = False,
) -> CoexistenceResult:
    client = DBusNextBlueZCoexistenceClient()
    registration = BlueZCompatibilityRegistration(
        client, operation_timeout=dbus_timeout
    )
    transport = KernelL2CAPTransport(connect_timeout=connect_timeout)
    aap_ack_event = asyncio.Event()
    activation_event = asyncio.Event()
    session = BlueZCoexistenceSession(
        client,
        registration,
        transport,
        AAPHandshakeSession(
            ack_timeout=handshake_timeout,
            descriptor_timeout=descriptor_timeout,
            progress=_aap_progress(output, aap_ack_event),
        ),
        HeartRateActivationSession(
            sample_target=samples,
            stream_timeout=hr_timeout,
            progress=_heart_rate_progress(output, activation_event, transport),
        ),
        aap_ack_event=aap_ack_event,
        hr_activation_event=activation_event,
        experimental_ack_only_hr=experimental_ack_only_hr,
        experimental_fresh_bluez_acl=experimental_fresh_bluez_acl,
        dbus_timeout=dbus_timeout,
        output=output,
    )
    return await session.run()


async def run_live_sdp_audit(
    output: Callable[[str], None], dbus_timeout: float
) -> BlueZSDPAuditResult:
    del output
    client = DBusNextBlueZCoexistenceClient()
    try:
        await asyncio.wait_for(client.connect(), timeout=dbus_timeout)
        state = await asyncio.wait_for(client.preflight(), timeout=dbus_timeout)
        return audit_bluez_sdp_identity(state)
    finally:
        client.close()


def _print_sdp_audit(
    output: Callable[[str], None], result: BlueZSDPAuditResult
) -> None:
    def format_value(name: str, value: int | None) -> str:
        if value is None:
            return "unknown"
        if name == "rfcomm_channel":
            return str(value)
        return f"0x{value:04X}"

    output("BLUEZ SDP AUDIT: read-only local identity comparison")
    output("Adapter1.UUIDs evidence: service-class coverage only")
    attribute_source = (
        "structured local record inspector"
        if result.detailed_attribute_inspection_available
        else "unknown/not-observable through BlueZ D-Bus"
    )
    output(f"Detailed SDP attribute source: {attribute_source}")
    for record in result.records:
        output(f"  {record.name}:")
        output(
            "    service_class_uuid="
            f"{'present' if record.service_class_uuid_present else 'absent'}"
        )
        for attribute in record.attributes:
            observed = (
                "not-observable"
                if attribute.status is SDPComparisonStatus.NOT_OBSERVABLE
                else (
                    "absent"
                    if attribute.observed_value is None
                    else format_value(attribute.name, attribute.observed_value)
                )
            )
            output(
                f"    {attribute.name}: "
                f"expected={format_value(attribute.name, attribute.expected_value)}, "
                f"observed={observed}, comparison={attribute.status.value}"
            )
    output(
        "Full local SDP record equivalence="
        f"{result.full_record_equivalence.value}"
    )
    if result.full_record_equivalence is SDPComparisonStatus.NOT_OBSERVABLE:
        output("UUID coverage alone does not establish SDP record equivalence.")
    output("BLUEZ SDP AUDIT COMPLETE")


async def run_probe(
    *,
    execute: bool,
    audit_sdp: bool = False,
    samples: int = DEFAULT_SAMPLE_TARGET,
    dbus_timeout: float = DEFAULT_DBUS_TIMEOUT,
    connect_timeout: float = DEFAULT_L2CAP_CONNECT_TIMEOUT,
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    descriptor_timeout: float = DEFAULT_DESCRIPTOR_TIMEOUT,
    hr_timeout: float = DEFAULT_STREAM_TIMEOUT,
    experimental_ack_only_hr: bool = False,
    experimental_fresh_bluez_acl: bool = False,
    verbose: bool = False,
    output: Callable[[str], None] = print,
    live_runner: LiveRunner = run_live_probe,
    audit_runner: AuditRunner = run_live_sdp_audit,
) -> int:
    if audit_sdp:
        try:
            audit = await audit_runner(output, dbus_timeout)
        except CoexistenceFailure as error:
            output(f"BLUEZ SDP AUDIT FAIL: {error.category.value}")
            if verbose and error.detail:
                output(f"Safe detail: {error.detail}")
            return 1
        except Exception as error:
            output("BLUEZ SDP AUDIT FAIL: inspection_unavailable")
            if verbose:
                output(f"Safe detail: {type(error).__name__}")
            return 1
        _print_sdp_audit(output, audit)
        return 0

    if not execute:
        output("DRY RUN: no Bluetooth or BlueZ state will be changed.")
        output("Planned coexistence operations:")
        output("  0. Read BlueZ adapter and paired AirPods connection state.")
        output("  1. Temporarily register only missing known SDP identity records.")
        output(
            "  2. Set Classic L2CAP local RX MTU 2048, then request "
            "medium-security PSM 0x1001."
        )
        output("  3. Reuse the canonical bounded AAP handshake.")
        output(f"  4. Reuse canonical HR activation and collect {samples} reports.")
        output("  5. Stop HR and close only probe-owned socket/profile resources.")
        output("Controller handoff: no; LinkKey access: no; Bumble fallback: no.")
        output(
            "Experimental ACK-only HR: "
            f"{'enabled' if experimental_ack_only_hr else 'disabled'}."
        )
        output(
            "Experimental fresh BlueZ ACL: "
            f"{'enabled' if experimental_fresh_bluez_acl else 'disabled'}."
        )
        if experimental_fresh_bluez_acl:
            output(
                "Fresh-ACL preflight requires paired AirPods with "
                "Device1.Connected=false; the probe does not call BlueZ "
                "Connect, Disconnect, or ConnectProfile."
            )
        output(
            "Timeouts: "
            f"D-Bus={dbus_timeout:g}s, L2CAP={connect_timeout:g}s, "
            f"AAP ACK={handshake_timeout:g}s, "
            f"AAP descriptor={descriptor_timeout:g}s, HR={hr_timeout:g}s."
        )
        return 0

    try:
        result = await live_runner(
            output,
            samples,
            dbus_timeout,
            connect_timeout,
            handshake_timeout,
            descriptor_timeout,
            hr_timeout,
            experimental_ack_only_hr,
            experimental_fresh_bluez_acl,
        )
    except asyncio.CancelledError:
        raise
    except CoexistenceFailure as error:
        output(
            f"COEXISTENCE FAIL at {error.phase.value}: {error.category.value}"
        )
        if error.experimental_ack_only_hr_attempted:
            output("Descriptor handshake: incomplete")
            output("Exact AAP ACK: proven")
            output("Experimental HR activation: fail")
        if verbose:
            if error.l2cap_local_rx_observation is not None:
                _print_kernel_local_rx_summary(
                    output, error.l2cap_local_rx_observation
                )
            _print_hr_timeout_diagnostics(output, error)
            if error.handshake_observation is not None:
                _print_descriptor_timeout_diagnostics(
                    output, error.handshake_observation
                )
            elif error.detail:
                output(f"Safe detail: {error.detail}")
        return 1
    except Exception as error:
        output("COEXISTENCE FAIL at unknown: unexpected_probe_error")
        if verbose:
            output(f"Safe detail: {type(error).__name__}")
        return 1

    output(
        f"Canonical HR reports received: {len(result.heart_rate.samples)}/"
        f"{result.heart_rate.requested_samples}"
    )
    output(
        "Descriptor handshake: "
        f"{'complete' if result.descriptor_handshake_complete else 'incomplete'}"
    )
    output(
        "Exact AAP ACK: "
        f"{'proven' if result.handshake_observation.ack_observed else 'not proven'}"
    )
    if experimental_fresh_bluez_acl:
        output("Experimental fresh BlueZ ACL: pass")
        output("COEXISTENCE FRESH BLUEZ ACL EXPERIMENT PASS")
    elif result.experimental_ack_only_hr_used:
        output("Experimental HR activation: pass")
        output("COEXISTENCE HR EXPERIMENT PASS")
    else:
        output("COEXISTENCE PASS")
    return 0


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit = print if stream is None else lambda message: print(message, file=stream)
    try:
        return asyncio.run(
            run_probe(
                execute=args.execute,
                audit_sdp=args.audit_sdp,
                samples=args.samples,
                dbus_timeout=args.dbus_timeout,
                connect_timeout=args.connect_timeout,
                handshake_timeout=args.handshake_timeout,
                descriptor_timeout=args.descriptor_timeout,
                hr_timeout=args.hr_timeout,
                experimental_ack_only_hr=args.experimental_ack_only_hr,
                experimental_fresh_bluez_acl=(
                    args.experimental_fresh_bluez_acl
                ),
                verbose=args.verbose,
                output=emit,
            )
        )
    except KeyboardInterrupt:
        emit("COEXISTENCE FAIL at interrupted: cleanup was attempted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
