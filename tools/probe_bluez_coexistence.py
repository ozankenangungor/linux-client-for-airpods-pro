"""Safe-by-default BlueZ coexistence feasibility probe."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TextIO
from airpods_hr.aap import AAP_FRAME_SUMMARY_LIMIT, AAPFrameSummary, AAPHandshakeSession, AAPProgress, AAPType2BFrameSummary, HandshakeObservation
from airpods_hr.bluez_coexistence import DEFAULT_DBUS_TIMEOUT, DEFAULT_DESCRIPTOR_TIMEOUT, DEFAULT_HANDSHAKE_TIMEOUT, DEFAULT_L2CAP_CONNECT_TIMEOUT, BlueZCompatibilityRegistration, BlueZCoexistenceSession, CoexistenceFailure, CoexistenceResult, DBusNextBlueZCoexistenceClient, KernelL2CAPLocalRXObservation, KernelL2CAPTransport
from airpods_hr.bluez_sdp_audit import BlueZSDPAuditResult, SDPComparisonStatus, audit_bluez_sdp_identity
from airpods_hr.heart_rate_session import ControlFrameSummary, DEFAULT_CONTROL_SUMMARY_LIMIT, DEFAULT_SAMPLE_TARGET, DEFAULT_STREAM_TIMEOUT, HeartRateActivationSession, HeartRateProgress
from airpods_hr.heartrate import HeartRateReport


LiveRunner = Callable[
    [Callable[[str], None], int, float, float, float, float, float, bool, bool],
    Awaitable[CoexistenceResult],
]



AuditRunner = Callable[
    [Callable[[str], None], float], Awaitable[BlueZSDPAuditResult]
]



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

