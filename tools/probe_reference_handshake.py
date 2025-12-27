"""Private diagnostic for the existing controller-handoff AAP handshake."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Protocol
from airpods_hr.aap import AAP_FRAME_SUMMARY_LIMIT, AAPHandshakeProbeSession, AAPHandshakeSession, AAPProgress, AAPType2BFrameSummary, HandshakeObservation
from airpods_hr.aap_channel import AAPChannel, AAPChannelProgress, AAPChannelSession
from airpods_hr.aap_config_diagnostics import AAPConfigurationDiagnosticStrategy, AAPConfigureResponseMode, AAPL2CAPConfigurationObservation
from airpods_hr.aap_local_rx_diagnostics import AAPLocalRXConfigurationObservation, AAPLocalRXDiagnosticStrategy, AAPLocalRXProfile, AAPPostACKShapeObservation, L2CAP_CLASSIC_DEFAULT_MTU
from airpods_hr.authentication import AuthenticationProgress, BumbleClassicRuntimeFactory, ClassicAuthenticationSession, create_controller_handoff_transport
from airpods_hr.bluetooth import DBusNextBlueZBackend
from airpods_hr.discovery import BlueZDeviceDiscovery, DBusNextManagedObjectsBackend
from airpods_hr.pairing import BlueZPairingStore
from airpods_hr.pre_aap_diagnostics import PreAAPAAPChannelSession, PreAAPSequenceMode, PreAAPSequenceObservation, PreAAPSequenceSecureSession, PreAAPSequenceStrategy
from airpods_hr.pre_auth_diagnostics import PreAuthSequenceMode, PreAuthSequenceObservation, PreAuthSequenceStrategy
from airpods_hr.sdp import REQUIRED_SDP_COMPATIBILITY_RECORDS
from airpods_hr.reference_sdp_footprint import BLUEZ_LIKE_EXTRA_SERVICE_SPECS, ReferenceSDPFootprint, ReferenceSDPFootprintSecureSession, ReferenceSDPFootprintStrategy, ReferenceSDPQuerySnapshot


LiveRunner = Callable[
    [
        Callable[[str], None],
        float,
        AAPConfigureResponseMode,
        ReferenceSDPFootprint,
        PreAAPSequenceMode,
        PreAuthSequenceMode,
        AAPLocalRXProfile,
    ],
    Awaitable[HandshakeObservation],
]



DEFAULT_ACK_TIMEOUT = 5.0



DEFAULT_DESCRIPTOR_TIMEOUT = 3.0



class HandoffTransport(Protocol):
    def acquire(self, adapter_name: str) -> Any: ...



class _ReportingHandoffTransport:
    """Report the existing handoff boundary without changing its behavior."""

    def __init__(
        self,
        delegate: HandoffTransport,
        output: Callable[[str], None],
    ) -> None:
        self._delegate = delegate
        self._output = output

    @asynccontextmanager
    async def acquire(self, adapter_name: str) -> AsyncIterator[object]:
        self._output("PHASE 1 — controller_handoff")
        try:
            async with self._delegate.acquire(adapter_name) as transport:
                self._output("Controller handoff: succeeded")
                yield transport
        finally:
            self._output("Controller handoff cleanup: attempted")



def _bounded_float(minimum: float, maximum: float) -> Callable[[str], float]:
    def parse(value: str) -> float:
        parsed = float(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"value must be between {minimum:g} and {maximum:g}"
            )
        return parsed

    return parse



def _yes_no(value: bool) -> str:
    return "yes" if value else "no"



def _optional_integer(value: int | None) -> str:
    return "unavailable" if value is None else str(value)



def _optional_boolean(value: bool | None) -> str:
    return "unavailable" if value is None else _yes_no(value)



def _optional_hex(value: int | None, width: int) -> str:
    return "unavailable" if value is None else f"0x{value:0{width}X}"



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
    observation: HandshakeObservation,
) -> None:
    emitted = 0
    phases = (
        ("pre_ack", observation.pre_ack_frame_summaries),
        ("post_ack", observation.post_ack_frame_summaries),
    )
    for label, summaries in phases:
        for index, summary in enumerate(summaries, 1):
            if emitted >= AAP_FRAME_SUMMARY_LIMIT:
                return
            output(f"  {label}_frame_{index}:")
            output(f"    length={summary.length}")
            output(
                "    header_u16_2_3="
                f"{_optional_hex(summary.header_u16_2_3, 4)}"
            )
            output(
                "    header_u16_4_5="
                f"{_optional_hex(summary.header_u16_4_5, 4)}"
            )
            if summary.type_2b_summary is not None:
                _print_type_2b_summary(output, summary.type_2b_summary)
            emitted += 1



def _print_observation_fields(
    output: Callable[[str], None], observation: HandshakeObservation
) -> None:
    evidence = observation.evidence
    output(f"  exact_ack_observed={_yes_no(observation.ack_observed)}")
    output(f"  pre_ack_frames={observation.pre_ack_frame_count}")
    output(f"  post_ack_frames={observation.post_ack_frame_count}")
    output(f"  receive_frames_dropped={observation.receive_frames_dropped}")
    output(f"  sensor_framework={_yes_no(evidence.sensor_framework)}")
    output(f"  heart_rate_service={_yes_no(evidence.heart_rate_service)}")
    output(f"  heart_rate={_yes_no(evidence.heart_rate)}")
    output(f"  heartrate_access={_yes_no(evidence.heartrate_access)}")



def _print_descriptor_timeout_diagnostics(
    output: Callable[[str], None], observation: HandshakeObservation
) -> None:
    output("Reference AAP descriptor timeout diagnostics:")
    _print_observation_fields(output, observation)
    _print_frame_summaries(output, observation)



def _print_ack_timeout_diagnostics(
    output: Callable[[str], None], observation: HandshakeObservation
) -> None:
    output("Reference AAP ACK timeout diagnostics:")
    _print_observation_fields(output, observation)
    _print_frame_summaries(output, observation)



def _print_reference_summary(
    output: Callable[[str], None],
    observation: HandshakeObservation,
    *,
    descriptor_complete: bool,
) -> None:
    output("REFERENCE HANDSHAKE SUMMARY")
    _print_observation_fields(output, observation)
    output(f"  descriptor_complete={_yes_no(descriptor_complete)}")



def _format_option_types(option_types: tuple[int, ...]) -> str:
    if not option_types:
        return "none"
    return ",".join(f"0x{option_type:02X}" for option_type in option_types)



def _format_config_value(value: int | None) -> str:
    return "not-observed" if value is None else str(value)



def _format_rfc_mode(value: int | None) -> str:
    if value is None:
        return "not-observed"
    return "Basic" if value == 0 else str(value)



def _print_l2cap_configuration_summary(
    output: Callable[[str], None],
    mode: AAPConfigureResponseMode,
    observation: AAPL2CAPConfigurationObservation | None,
) -> None:
    output("AAP L2CAP CONFIGURATION SUMMARY")
    output(f"  response_mode={mode.value}")
    if observation is None:
        output("  peer_option_types=not-observed")
        output("  peer_mtu=not-observed")
        output("  peer_flush_timeout=not-observed")
        output("  peer_rfc_present=not-observed")
        output("  peer_rfc_mode=not-observed")
        output("  response_result=not-observed")
        output("  response_option_types=not-observed")
        output("  response_mtu=not-observed")
        output("  response_flush_timeout_present=not-observed")
        output("  response_flush_timeout=not-observed")
        output("  response_rfc_present=not-observed")
        output("  response_rfc_mode=not-observed")
        return
    output(
        "  peer_option_types="
        f"{_format_option_types(observation.peer_option_types)}"
    )
    output(f"  peer_mtu={_format_config_value(observation.peer_mtu)}")
    output(
        "  peer_flush_timeout="
        f"{_format_config_value(observation.peer_flush_timeout)}"
    )
    output(f"  peer_rfc_present={_yes_no(observation.peer_rfc_present)}")
    output(f"  peer_rfc_mode={_format_rfc_mode(observation.peer_rfc_mode)}")
    response_result = (
        "not-observed"
        if observation.response_result is None
        else (
            "success"
            if observation.response_result == 0
            else str(observation.response_result)
        )
    )
    output(f"  response_result={response_result}")
    output(
        "  response_option_types="
        f"{_format_option_types(observation.response_option_types)}"
    )
    output(
        "  response_mtu="
        f"{_format_config_value(observation.response_mtu)}"
    )
    output(
        "  response_flush_timeout_present="
        f"{_yes_no(observation.response_flush_timeout is not None)}"
    )
    output(
        "  response_flush_timeout="
        f"{_format_config_value(observation.response_flush_timeout)}"
    )
    output(
        "  response_rfc_present="
        f"{_yes_no(observation.response_rfc_present)}"
    )
    output(
        "  response_rfc_mode="
        f"{_format_rfc_mode(observation.response_rfc_mode)}"
    )



def _print_local_rx_configuration_summary(
    output: Callable[[str], None],
    observation: AAPLocalRXConfigurationObservation,
) -> None:
    output("AAP LOCAL RX CONFIGURATION SUMMARY")
    output(f"  mode={observation.mode.value}")
    output(f"  request_observed={_yes_no(observation.request_observed)}")
    request_option_types = (
        _format_option_types(observation.request_option_types)
        if observation.request_observed
        else "not-observed"
    )
    output(
        "  request_option_types="
        f"{request_option_types}"
    )
    request_mtu = (
        f"default-{L2CAP_CLASSIC_DEFAULT_MTU}/not-explicit"
        if observation.request_observed and observation.request_mtu is None
        else (
            str(observation.request_mtu)
            if observation.request_mtu is not None
            else "not-observed"
        )
    )
    output(f"  request_mtu={request_mtu}")
    output(
        f"  request_flags={_optional_hex(observation.request_flags, 4)}"
    )
    output(
        "  internal_receive_mtu="
        f"{_format_config_value(observation.internal_receive_mtu)}"
    )
    output(
        "  peer_response_observed="
        f"{_yes_no(observation.peer_response_observed)}"
    )
    response_result = (
        "not-observed"
        if observation.peer_response_result is None
        else (
            "success"
            if observation.peer_response_result == 0
            else str(observation.peer_response_result)
        )
    )
    output(f"  peer_response_result={response_result}")
    response_option_types = (
        _format_option_types(observation.peer_response_option_types)
        if observation.peer_response_observed
        else "not-observed"
    )
    output(
        "  peer_response_option_types="
        f"{response_option_types}"
    )
    output(
        "  peer_response_mtu="
        f"{_format_config_value(observation.peer_response_mtu)}"
    )



def _print_post_ack_shape_summary(
    output: Callable[[str], None], observation: HandshakeObservation
) -> None:
    shape = AAPPostACKShapeObservation.from_handshake(observation)
    lengths = (
        "none"
        if not shape.type_0x0017_frame_lengths
        else ",".join(str(length) for length in shape.type_0x0017_frame_lengths)
    )
    output("AAP POST-ACK SHAPE SUMMARY")
    output(
        "  first_type_0x002b_length="
        f"{_format_config_value(shape.first_type_0x002b_length)}"
    )
    output(f"  type_0x0017_frame_lengths={lengths}")
    output(
        "  max_post_ack_frame_length="
        f"{_format_config_value(shape.max_post_ack_frame_length)}"
    )



class _ReferenceAAPCompatibility:
    """Compose independent request- and response-direction diagnostics."""

    def __init__(
        self,
        response: AAPConfigurationDiagnosticStrategy,
        local_rx: AAPLocalRXDiagnosticStrategy,
    ) -> None:
        self.response = response
        self.local_rx = local_rx

    @contextmanager
    def __call__(self, manager: object) -> Iterator[None]:
        with self.response(manager), self.local_rx(manager):
            yield



def _format_uuid16s(values: tuple[int, ...]) -> str:
    return "none" if not values else ",".join(f"0x{value:04X}" for value in values)



def _format_attribute_ranges(values: tuple[tuple[int, int], ...]) -> str:
    if not values:
        return "none"
    return ",".join(
        f"0x{start:04X}" if start == end else f"0x{start:04X}{end:04X}"
        for start, end in values
    )



def _print_sdp_query_summary(
    output: Callable[[str], None], snapshot: ReferenceSDPQuerySnapshot
) -> None:
    output("REFERENCE SDP QUERY SUMMARY")
    output(f"  sdp_footprint={snapshot.footprint.value}")
    output(f"  service_search_attribute_requests={snapshot.requests_observed}")
    query = snapshot.l2cap_full_attribute_query()
    output(
        "  l2cap_full_attribute_search_observed="
        f"{_yes_no(query is not None)}"
    )
    if query is None:
        output("  search_uuids=not-observed")
        output("  attribute_ranges=not-observed")
        output("  max_attribute_bytes=not-observed")
        output("  continuation_used=not-observed")
        output("  matching_record_count=not-observed")
        output("  response_bytes=not-observed")
        return
    output(f"  search_uuids={_format_uuid16s(query.search_uuids)}")
    output(
        "  attribute_ranges="
        f"{_format_attribute_ranges(query.attribute_ranges)}"
    )
    output(f"  max_attribute_bytes={query.maximum_attribute_byte_count}")
    output(f"  continuation_used={_yes_no(query.continuation_used)}")
    output(f"  matching_record_count={query.matching_record_count}")
    output(f"  response_bytes={query.total_response_bytes}")



def _format_information_mask(value: int | None, width: int) -> str:
    return "not-observed" if value is None else f"0x{value:0{width}X}"



def _print_pre_aap_sequence_summary(
    output: Callable[[str], None], observation: PreAAPSequenceObservation
) -> None:
    output("PRE-AAP SEQUENCE SUMMARY")
    output(f"  mode={observation.mode.value}")
    output(f"  delay_ms={observation.delay_ms}")
    output(
        "  extended_features_request_sent="
        f"{_yes_no(observation.extended_features_request_sent)}"
    )
    output(
        "  extended_features_response_observed="
        f"{_yes_no(observation.extended_features_response_observed)}"
    )
    output(
        "  extended_features_result="
        f"{observation.extended_features_result.value}"
    )
    output(
        "  extended_features_mask="
        f"{_format_information_mask(observation.extended_features_mask, 8)}"
    )
    output(
        "  fixed_channels_request_sent="
        f"{_yes_no(observation.fixed_channels_request_sent)}"
    )
    output(
        "  fixed_channels_response_observed="
        f"{_yes_no(observation.fixed_channels_response_observed)}"
    )
    output(
        "  fixed_channels_result="
        f"{observation.fixed_channels_result.value}"
    )
    output(
        "  fixed_channels_mask="
        f"{_format_information_mask(observation.fixed_channels_mask, 16)}"
    )
    output(f"  aap_open_attempted={_yes_no(observation.aap_open_attempted)}")



def _format_remote_discovery_value(
    value: int | None, width: int, *, applicable: bool
) -> str:
    if not applicable:
        return "not-applicable"
    return "not-observed" if value is None else f"0x{value:0{width}X}"



def _format_remote_discovery_integer(
    value: int | None, *, applicable: bool
) -> str:
    if not applicable:
        return "not-applicable"
    return "not-observed" if value is None else str(value)



def _print_pre_auth_sequence_summary(
    output: Callable[[str], None], observation: PreAuthSequenceObservation
) -> None:
    output("PRE-AUTH SEQUENCE SUMMARY")
    output(f"  mode={observation.mode.value}")
    output(f"  delay_ms={observation.delay_ms}")
    output(
        "  remote_supported_features_request_sent="
        f"{_yes_no(observation.remote_supported_features_request_sent)}"
    )
    output(
        "  remote_supported_features_command_accepted="
        f"{_yes_no(observation.remote_supported_features_command_accepted)}"
    )
    output(
        "  remote_supported_features_response_observed="
        f"{_yes_no(observation.remote_supported_features_response_observed)}"
    )
    output(
        "  remote_supported_features_result="
        f"{observation.remote_supported_features_result.value}"
    )
    output(
        "  remote_supported_features_mask="
        + _format_remote_discovery_value(
            observation.remote_supported_features_mask,
            16,
            applicable=observation.remote_supported_features_request_sent,
        )
    )
    output(
        "  remote_extended_features_request_sent="
        f"{_yes_no(observation.remote_extended_features_request_sent)}"
    )
    output(
        "  remote_extended_features_command_accepted="
        f"{_yes_no(observation.remote_extended_features_command_accepted)}"
    )
    output(
        "  remote_extended_features_page="
        + _format_remote_discovery_integer(
            observation.remote_extended_features_page,
            applicable=observation.remote_extended_features_request_sent,
        )
    )
    output(
        "  remote_extended_features_response_observed="
        f"{_yes_no(observation.remote_extended_features_response_observed)}"
    )
    output(
        "  remote_extended_features_result="
        f"{observation.remote_extended_features_result.value}"
    )
    output(
        "  remote_extended_features_max_page="
        + _format_remote_discovery_integer(
            observation.remote_extended_features_max_page,
            applicable=observation.remote_extended_features_request_sent,
        )
    )
    output(
        "  remote_extended_features_mask="
        + _format_remote_discovery_value(
            observation.remote_extended_features_mask,
            16,
            applicable=observation.remote_extended_features_request_sent,
        )
    )
    output(
        "  remote_name_request_sent="
        f"{_yes_no(observation.remote_name_request_sent)}"
    )
    output(
        "  remote_name_command_accepted="
        f"{_yes_no(observation.remote_name_command_accepted)}"
    )
    output(
        "  remote_name_response_observed="
        f"{_yes_no(observation.remote_name_response_observed)}"
    )
    output(f"  remote_name_result={observation.remote_name_result.value}")
    output(
        "  authentication_attempted="
        f"{_yes_no(observation.authentication_attempted)}"
    )



def _authentication_progress(output: Callable[[str], None]):
    phase_started = False

    def emit(event: AuthenticationProgress, detail: str | None) -> None:
        nonlocal phase_started
        del detail
        if event is AuthenticationProgress.DEVICE_SELECTED:
            output("Reference preflight: paired AirPods selected")
        elif event is AuthenticationProgress.CONNECTED:
            if not phase_started:
                output("PHASE 3 — authentication_encryption")
                phase_started = True
            output("BR/EDR connection: succeeded")
        elif event is AuthenticationProgress.AUTHENTICATED:
            output("BR/EDR authentication: succeeded")
        elif event is AuthenticationProgress.ENCRYPTED:
            output("BR/EDR encryption: established")
        elif event is AuthenticationProgress.DISCONNECTED:
            output("Reference BR/EDR disconnect: complete")
        elif event is AuthenticationProgress.REPLACEMENT_KEY_REPORTED:
            output("Controller reported replacement-key activity: yes")

    return emit



def _channel_progress(output: Callable[[str], None]):
    def emit(event: AAPChannelProgress, channel: AAPChannel | None) -> None:
        if event is AAPChannelProgress.OPENED and channel is not None:
            output("PHASE 4 — l2cap_connection")
            output("Reference AAP PSM 0x1001: opened")
        elif event is AAPChannelProgress.CLOSED:
            output("Reference AAP L2CAP close: complete")

    return emit



def _aap_progress(
    output: Callable[[str], None],
    footprint: ReferenceSDPFootprint = ReferenceSDPFootprint.PROVEN,
):
    footprint = ReferenceSDPFootprint(footprint)

    def emit(event: AAPProgress) -> None:
        if event is AAPProgress.SDP_INSTALLED:
            output("PHASE 2 — reference_sdp_identity")
            output(
                "Known-good reference SDP records installed: "
                f"{len(REQUIRED_SDP_COMPATIBILITY_RECORDS)}/"
                f"{len(REQUIRED_SDP_COMPATIBILITY_RECORDS)}"
            )
            output(
                "Reference SDP services: "
                + ", ".join(REQUIRED_SDP_COMPATIBILITY_RECORDS)
            )
            output(f"Reference SDP footprint: {footprint.value}")
            if footprint is ReferenceSDPFootprint.BLUEZ_LIKE:
                output(
                    "Experimental BlueZ-like extra SDP records installed: "
                    f"{len(BLUEZ_LIKE_EXTRA_SERVICE_SPECS)}"
                )
        elif event is AAPProgress.HANDSHAKE_SENT:
            output("PHASE 5 — aap_handshake")
            output("Canonical AAP handshake request: sent")
        elif event is AAPProgress.ACK_OBSERVED:
            output("Exact canonical AAP ACK: observed")
        elif event is AAPProgress.DESCRIPTORS_OBSERVED:
            output("Canonical descriptor evidence: complete")

    return emit



async def run_live_probe(
    output: Callable[[str], None],
    descriptor_timeout: float = DEFAULT_DESCRIPTOR_TIMEOUT,
    response_mode: AAPConfigureResponseMode = AAPConfigureResponseMode.PROVEN,
    sdp_footprint: ReferenceSDPFootprint = ReferenceSDPFootprint.PROVEN,
    pre_aap_sequence: PreAAPSequenceMode = PreAAPSequenceMode.PROVEN,
    pre_auth_sequence: PreAuthSequenceMode = PreAuthSequenceMode.PROVEN,
    local_rx_profile: AAPLocalRXProfile = AAPLocalRXProfile.PROVEN,
) -> HandshakeObservation:
    response_mode = AAPConfigureResponseMode(response_mode)
    sdp_footprint = ReferenceSDPFootprint(sdp_footprint)
    pre_aap_sequence = PreAAPSequenceMode(pre_aap_sequence)
    pre_auth_sequence = PreAuthSequenceMode(pre_auth_sequence)
    local_rx_profile = AAPLocalRXProfile(local_rx_profile)
    output("PHASE 0 — reference_preflight")
    discovery_backend = DBusNextManagedObjectsBackend()
    bluez_backend = DBusNextBlueZBackend()
    configuration = AAPConfigurationDiagnosticStrategy(response_mode)
    local_rx = AAPLocalRXDiagnosticStrategy(local_rx_profile)
    footprint_strategy = ReferenceSDPFootprintStrategy(sdp_footprint)
    sequence_strategy = PreAAPSequenceStrategy(pre_aap_sequence)
    pre_auth_strategy = PreAuthSequenceStrategy(pre_auth_sequence)
    primary_error: BaseException | None = None
    try:
        await discovery_backend.connect()
        await bluez_backend.connect()
        handoff, transport = create_controller_handoff_transport(bluez_backend)
        await transport.ensure_available()
        reporting_handoff = _ReportingHandoffTransport(handoff, output)
        secure_session = ClassicAuthenticationSession(
            BlueZDeviceDiscovery(discovery_backend),
            BlueZPairingStore(),
            reporting_handoff,
            BumbleClassicRuntimeFactory(),
            progress=_authentication_progress(output),
            pre_authentication=pre_auth_strategy.before_authentication,
        )
        progress = _aap_progress(output, sdp_footprint)
        footprint_session = ReferenceSDPFootprintSecureSession(
            secure_session, footprint_strategy
        )
        sequence_session = PreAAPSequenceSecureSession(
            footprint_session, sequence_strategy
        )
        channel_session = AAPChannelSession(
            compatibility=_ReferenceAAPCompatibility(configuration, local_rx),
            progress=_channel_progress(output),
        )
        session = AAPHandshakeProbeSession(
            sequence_session,
            PreAAPAAPChannelSession(channel_session, sequence_strategy),
            AAPHandshakeSession(
                descriptor_timeout=descriptor_timeout,
                progress=progress,
            ),
            progress=progress,
        )
        result = await session.run()
        output("Controller ownership and BlueZ restoration: complete")
        return result.observation
    except BaseException as error:
        primary_error = error
        raise
    finally:
        output("PHASE 6 — cleanup")
        _print_l2cap_configuration_summary(
            output, response_mode, configuration.observation
        )
        _print_local_rx_configuration_summary(output, local_rx.observation)
        _print_sdp_query_summary(
            output, footprint_strategy.diagnostics.snapshot()
        )
        _print_pre_aap_sequence_summary(output, sequence_strategy.observation)
        _print_pre_auth_sequence_summary(output, pre_auth_strategy.observation)
        cleanup_errors: list[BaseException] = []
        for backend in (discovery_backend, bluez_backend):
            try:
                backend.close()
            except BaseException as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            if primary_error is not None:
                primary_error.add_note(
                    "reference D-Bus cleanup also reported a failure"
                )
            else:
                raise RuntimeError("reference D-Bus cleanup failed") from None
        else:
            output("Reference D-Bus resources: closed")

