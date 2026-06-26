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

from airpods_hr import _airpods_aap_core as _native
from airpods_hr.bumble_compat import aap_flush_timeout_compatibility
from airpods_hr.protocol import AAP_PSM


_HANDLER_ATTRIBUTE = "on_l2cap_configure_request"
_MISSING = object()


class AAPConfigureResponseMode(StrEnum):
    """Wire-response policy available only to the reference diagnostic."""

    PROVEN = "proven"
    KERNEL_MTU_ONLY = "kernel-mtu-only"


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


def _summarize_exchange(
    mode: AAPConfigureResponseMode,
    request_options: bytes,
    response: l2cap.L2CAP_Configure_Response | None,
) -> AAPL2CAPConfigurationObservation:
    facts = _native.diagnostic_config_observation(
        mode.value,
        request_options,
        int(response.result) if response is not None else None,
        response.options if response is not None else None,
    )
    facts["peer_option_types"] = tuple(facts["peer_option_types"])
    facts["response_option_types"] = tuple(facts["response_option_types"])
    return AAPL2CAPConfigurationObservation(**facts)


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

        fail, mtu_encoded = _native.diagnostic_config_plan(
            self.mode is AAPConfigureResponseMode.KERNEL_MTU_ONLY,
            request.options,
            request.flags,
        )
        if fail:
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
                self.mode, request.options, response
            )
            channel.send_control_frame(response)
            return

        previous_send = channel.__dict__.get("send_control_frame", _MISSING)
        original_send = channel.send_control_frame

        def send_control_frame(frame: l2cap.L2CAP_Control_Frame) -> None:
            response = frame
            if isinstance(frame, l2cap.L2CAP_Configure_Response):
                matching, rewrite_options = _native.diagnostic_config_rewrite(
                    self.mode is AAPConfigureResponseMode.KERNEL_MTU_ONLY,
                    request.identifier,
                    frame.identifier,
                    frame.result == l2cap.L2CAP_Configure_Response.Result.SUCCESS,
                    mtu_encoded,
                )
                if matching and rewrite_options is not None:
                    response = l2cap.L2CAP_Configure_Response(
                        identifier=frame.identifier,
                        source_cid=frame.source_cid,
                        flags=frame.flags,
                        result=frame.result,
                        options=rewrite_options,
                    )
                if matching:
                    self._observation = _summarize_exchange(
                        self.mode, request.options, response
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
