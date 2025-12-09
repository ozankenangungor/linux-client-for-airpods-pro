"""Private AAP L2CAP configuration-response diagnostics.

The proven compatibility context remains the authority for accepting the
reviewed AirPods FLUSH_TIMEOUT request.  This module adds probe-only passive
observation and an explicit experiment that changes only the successful wire
response to the MTU-only shape emitted by the Linux kernel.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterator

from bumble import l2cap

from airpods_hr.bumble_compat import aap_flush_timeout_compatibility
from airpods_hr.protocol import AAP_PSM


_HANDLER_ATTRIBUTE = "on_l2cap_configure_request"
_MISSING = object()


class AAPConfigureResponseMode(StrEnum):
    """Wire-response policy available only to the reference diagnostic."""

    PROVEN = "proven"
    KERNEL_MTU_ONLY = "kernel-mtu-only"


@dataclass(frozen=True, slots=True)
class _ConfigurationOption:
    option_type: int
    value: bytes
    encoded: bytes


@dataclass(frozen=True, slots=True)
class AAPL2CAPConfigurationObservation:
    """Allowlisted, immutable summary of one AAP configuration exchange."""

    response_mode: AAPConfigureResponseMode
    peer_option_types: tuple[int, ...]
    peer_mtu: int | None
    peer_flush_timeout: int | None
    peer_rfc_present: bool
    peer_rfc_mode: int | None
    response_result: int | None
    response_option_types: tuple[int, ...]
    response_mtu: int | None
    response_flush_timeout: int | None
    response_rfc_present: bool
    response_rfc_mode: int | None


def _decode_options(data: bytes) -> tuple[_ConfigurationOption, ...] | None:
    options: list[_ConfigurationOption] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 2:
            return None
        length = data[offset + 1]
        end = offset + 2 + length
        if end > len(data):
            return None
        options.append(
            _ConfigurationOption(
                option_type=data[offset],
                value=data[offset + 2 : end],
                encoded=data[offset:end],
            )
        )
        offset = end
    return tuple(options)


def _value_u16(
    options: tuple[_ConfigurationOption, ...], option_type: int
) -> int | None:
    matching = [option for option in options if option.option_type == option_type]
    if len(matching) != 1 or len(matching[0].value) != 2:
        return None
    return int.from_bytes(matching[0].value, "little")


def _rfc_mode(options: tuple[_ConfigurationOption, ...]) -> int | None:
    matching = [option for option in options if option.option_type == 0x04]
    if len(matching) != 1 or len(matching[0].value) != 9:
        return None
    return matching[0].value[0]


def _summarize_exchange(
    mode: AAPConfigureResponseMode,
    request_options: tuple[_ConfigurationOption, ...],
    response: l2cap.L2CAP_Configure_Response | None,
) -> AAPL2CAPConfigurationObservation:
    response_options = (
        _decode_options(response.options) if response is not None else ()
    )
    if response_options is None:
        response_options = ()
    return AAPL2CAPConfigurationObservation(
        response_mode=mode,
        peer_option_types=tuple(
            option.option_type for option in request_options
        ),
        peer_mtu=_value_u16(request_options, 0x01),
        peer_flush_timeout=_value_u16(request_options, 0x02),
        peer_rfc_present=any(
            option.option_type == 0x04 for option in request_options
        ),
        peer_rfc_mode=_rfc_mode(request_options),
        response_result=(int(response.result) if response is not None else None),
        response_option_types=tuple(
            option.option_type for option in response_options
        ),
        response_mtu=_value_u16(response_options, 0x01),
        response_flush_timeout=_value_u16(response_options, 0x02),
        response_rfc_present=any(
            option.option_type == 0x04 for option in response_options
        ),
        response_rfc_mode=_rfc_mode(response_options),
    )


def _reviewed_request_mtu(
    options: tuple[_ConfigurationOption, ...],
) -> _ConfigurationOption | None:
    allowed_types = {0x01, 0x02, 0x04}
    if any(option.option_type not in allowed_types for option in options):
        return None
    mtu_options = [option for option in options if option.option_type == 0x01]
    flush_options = [option for option in options if option.option_type == 0x02]
    rfc_options = [option for option in options if option.option_type == 0x04]
    if (
        len(mtu_options) != 1
        or len(mtu_options[0].value) != 2
        or len(flush_options) != 1
        or len(flush_options[0].value) != 2
        or len(rfc_options) > 1
    ):
        return None
    if rfc_options and (
        len(rfc_options[0].value) != 9
        or rfc_options[0].value[0] != int(l2cap.TransmissionMode.BASIC)
    ):
        return None
    return mtu_options[0]


class AAPConfigurationDiagnosticStrategy:
    """AAPChannelSession compatibility factory with bounded observation."""

    def __init__(self, mode: AAPConfigureResponseMode) -> None:
        self.mode = AAPConfigureResponseMode(mode)
        self._observation: AAPL2CAPConfigurationObservation | None = None

    @property
    def observation(self) -> AAPL2CAPConfigurationObservation | None:
        return self._observation

    @contextmanager
    def __call__(self, manager: object) -> Iterator[None]:
        self._observation = None
        with aap_flush_timeout_compatibility(manager):
            if not isinstance(manager, l2cap.ChannelManager):
                raise TypeError("expected a Bumble ChannelManager")
            installed_handler = manager.on_l2cap_configure_request
            previous_handler = manager.__dict__.get(
                _HANDLER_ATTRIBUTE, _MISSING
            )

            def wrapper(connection: object, cid: int, request: object) -> None:
                if not isinstance(request, l2cap.L2CAP_Configure_Request):
                    installed_handler(connection, cid, request)
                    return
                self._handle_request(
                    manager,
                    installed_handler,
                    connection,
                    cid,
                    request,
                )

            setattr(manager, _HANDLER_ATTRIBUTE, wrapper)
            try:
                yield
            finally:
                if previous_handler is _MISSING:
                    delattr(manager, _HANDLER_ATTRIBUTE)
                else:
                    setattr(manager, _HANDLER_ATTRIBUTE, previous_handler)

    def _handle_request(
        self,
        manager: l2cap.ChannelManager,
        installed_handler: object,
        connection: object,
        cid: int,
        request: l2cap.L2CAP_Configure_Request,
    ) -> None:
        channel = manager.find_channel(
            connection.handle, request.destination_cid  # type: ignore[attr-defined]
        )
        if channel is None or channel.psm != AAP_PSM:
            installed_handler(connection, cid, request)  # type: ignore[operator]
            return

        decoded = _decode_options(request.options)
        mtu_option = (
            _reviewed_request_mtu(decoded)
            if decoded is not None and request.flags == 0
            else None
        )
        if self.mode is AAPConfigureResponseMode.KERNEL_MTU_ONLY and (
            decoded is None or mtu_option is None
        ):
            response = l2cap.L2CAP_Configure_Response(
                identifier=request.identifier,
                source_cid=channel.destination_cid,
                flags=0,
                result=(
                    l2cap.L2CAP_Configure_Response.Result.FAILURE_UNKNOWN_OPTIONS
                ),
                options=b"",
            )
            self._observation = _summarize_exchange(
                self.mode, decoded or (), response
            )
            channel.send_control_frame(response)
            return

        request_options = decoded or ()
        previous_send = channel.__dict__.get("send_control_frame", _MISSING)
        original_send = channel.send_control_frame

        def send_control_frame(frame: l2cap.L2CAP_Control_Frame) -> None:
            response = frame
            if (
                isinstance(frame, l2cap.L2CAP_Configure_Response)
                and frame.identifier == request.identifier
            ):
                if (
                    self.mode is AAPConfigureResponseMode.KERNEL_MTU_ONLY
                    and frame.result
                    == l2cap.L2CAP_Configure_Response.Result.SUCCESS
                ):
                    assert mtu_option is not None
                    response = l2cap.L2CAP_Configure_Response(
                        identifier=frame.identifier,
                        source_cid=frame.source_cid,
                        flags=frame.flags,
                        result=frame.result,
                        options=mtu_option.encoded,
                    )
                self._observation = _summarize_exchange(
                    self.mode, request_options, response
                )
            original_send(response)

        channel.send_control_frame = send_control_frame
        try:
            installed_handler(connection, cid, request)  # type: ignore[operator]
        finally:
            if previous_send is _MISSING:
                del channel.send_control_frame
            else:
                channel.send_control_frame = (  # type: ignore[method-assign]
                    previous_send
                )
