#!/usr/bin/env python3.14
"""Private diagnostic for the existing controller-handoff AAP handshake."""

from __future__ import annotations


from collections.abc import Awaitable, Callable


from airpods_hr.aap import HandshakeObservation


from airpods_hr.aap_config_diagnostics import AAPConfigureResponseMode, AAPL2CAPConfigurationObservation


from airpods_hr.aap_local_rx_diagnostics import AAPLocalRXConfigurationObservation, AAPLocalRXProfile, AAPPostACKShapeObservation, L2CAP_CLASSIC_DEFAULT_MTU


from airpods_hr.pre_aap_diagnostics import PreAAPSequenceMode


from airpods_hr.pre_auth_diagnostics import PreAuthSequenceMode


from airpods_hr.reference_sdp_footprint import ReferenceSDPFootprint


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


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _optional_hex(value: int | None, width: int) -> str:
    return "unavailable" if value is None else f"0x{value:0{width}X}"


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


