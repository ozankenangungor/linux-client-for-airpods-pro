"""Reference-probe-only AAP local receive-MTU wire diagnostics."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from importlib import metadata
from bumble import l2cap

from airpods_hr.aap import HandshakeObservation
from airpods_hr.bumble_compat import SUPPORTED_BUMBLE_VERSION
from airpods_hr.protocol import AAP_PSM


L2CAP_CLASSIC_DEFAULT_MTU = 672
AAP_POST_ACK_TYPE_17_LENGTH_LIMIT = 8
_MISSING = object()
_STOCK_SEND_CONFIGURE_REQUEST = l2cap.ClassicChannel.send_configure_request
_STOCK_SEND_CONTROL_FRAME = l2cap.ChannelManager.send_control_frame
_STOCK_CONFIGURE_RESPONSE_HANDLER = (
    l2cap.ChannelManager.on_l2cap_configure_response
)


class AAPLocalRXProfile(StrEnum):
    """Host-originated AAP Configure Request profile for the reference probe."""

    PROVEN = "proven"
    KERNEL_DEFAULT = "kernel-default"


class AAPLocalRXDiagnosticError(RuntimeError):
    """Raised when the local-RX wire experiment cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class _ConfigurationOption:
    option_type: int
    value: bytes
    encoded: bytes


@dataclass(frozen=True, slots=True)
class AAPLocalRXConfigurationObservation:
    """Allowlisted metadata for the host request and peer response."""

    mode: AAPLocalRXProfile
    request_observed: bool = False
    request_option_types: tuple[int, ...] = ()
    request_mtu: int | None = None
    request_flags: int | None = None
    request_identifier: int | None = None
    request_destination_cid: int | None = None
    internal_receive_mtu: int | None = None
    peer_response_observed: bool = False
    peer_response_result: int | None = None
    peer_response_option_types: tuple[int, ...] = ()
    peer_response_mtu: int | None = None


@dataclass(frozen=True, slots=True)
class AAPPostACKShapeObservation:
    """Bounded frame-length metadata derived from canonical safe summaries."""

    first_type_0x002b_length: int | None
    type_0x0017_frame_lengths: tuple[int, ...]
    max_post_ack_frame_length: int | None
    summaries_considered: int

    @classmethod
    def from_handshake(
        cls, observation: HandshakeObservation
    ) -> AAPPostACKShapeObservation:
        first_type_2b: int | None = None
        type_17_lengths: list[int] = []
        maximum: int | None = None
        for summary in observation.post_ack_frame_summaries:
            maximum = (
                summary.length
                if maximum is None
                else max(maximum, summary.length)
            )
            if first_type_2b is None and summary.header_u16_4_5 == 0x002B:
                first_type_2b = summary.length
            if (
                summary.header_u16_4_5 == 0x0017
                and len(type_17_lengths) < AAP_POST_ACK_TYPE_17_LENGTH_LIMIT
            ):
                type_17_lengths.append(summary.length)
        return cls(
            first_type_0x002b_length=first_type_2b,
            type_0x0017_frame_lengths=tuple(type_17_lengths),
            max_post_ack_frame_length=maximum,
            summaries_considered=len(observation.post_ack_frame_summaries),
        )


def _parameters(function: object) -> tuple[str, ...]:
    try:
        return tuple(inspect.signature(function).parameters)
    except (TypeError, ValueError) as error:
        raise AAPLocalRXDiagnosticError(
            "Bumble local-RX diagnostic signatures cannot be inspected"
        ) from error


def validate_bumble_local_rx_api(
    *, installed_version: str | None = None
) -> None:
    """Require the exact Bumble 0.0.234 configuration seam reviewed here."""

    version = installed_version
    if version is None:
        try:
            version = metadata.version("bumble")
        except metadata.PackageNotFoundError as error:
            raise AAPLocalRXDiagnosticError(
                "Bumble is unavailable for local-RX diagnostics"
            ) from error
    if version != SUPPORTED_BUMBLE_VERSION:
        raise AAPLocalRXDiagnosticError(
            f"unsupported Bumble version {version!r} for local-RX diagnostics; "
            f"expected {SUPPORTED_BUMBLE_VERSION}"
        )
    if (
        l2cap.ClassicChannel.send_configure_request
        is not _STOCK_SEND_CONFIGURE_REQUEST
        or l2cap.ChannelManager.send_control_frame
        is not _STOCK_SEND_CONTROL_FRAME
        or l2cap.ChannelManager.on_l2cap_configure_response
        is not _STOCK_CONFIGURE_RESPONSE_HANDLER
        or _parameters(_STOCK_SEND_CONFIGURE_REQUEST) != ("self",)
        or _parameters(_STOCK_SEND_CONTROL_FRAME)
        != ("self", "connection", "cid", "control_frame")
        or _parameters(_STOCK_CONFIGURE_RESPONSE_HANDLER)
        != ("self", "connection", "cid", "response")
    ):
        raise AAPLocalRXDiagnosticError(
            "Bumble local-RX configuration API differs from 0.0.234"
        )


def _decode_options(data: bytes) -> tuple[_ConfigurationOption, ...]:
    options: list[_ConfigurationOption] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 2:
            raise AAPLocalRXDiagnosticError(
                "host Configure Request options are malformed"
            )
        length = data[offset + 1]
        end = offset + 2 + length
        if end > len(data):
            raise AAPLocalRXDiagnosticError(
                "host Configure Request options are malformed"
            )
        options.append(
            _ConfigurationOption(
                option_type=data[offset],
                value=data[offset + 2 : end],
                encoded=data[offset:end],
            )
        )
        offset = end
    return tuple(options)


def _mtu(options: tuple[_ConfigurationOption, ...]) -> int | None:
    matches = [
        option
        for option in options
        if option.option_type == l2cap.L2CAP_Configure_Request.ParameterType.MTU
    ]
    if not matches:
        return None
    if len(matches) != 1 or len(matches[0].value) != 2:
        raise AAPLocalRXDiagnosticError("Configure MTU option is malformed")
    return int.from_bytes(matches[0].value, "little")


class AAPLocalRXDiagnosticStrategy:
    """Observe or rewrite one initial host-originated AAP Configure Request."""

    def __init__(self, mode: AAPLocalRXProfile) -> None:
        validate_bumble_local_rx_api()
        self.mode = AAPLocalRXProfile(mode)
        self._observation = AAPLocalRXConfigurationObservation(self.mode)
        self._request_identifier: int | None = None

    @property
    def observation(self) -> AAPLocalRXConfigurationObservation:
        return self._observation

    @contextmanager
    def __call__(self, manager: object) -> Iterator[None]:
        previous_send = manager.__dict__.get("send_control_frame", _MISSING)
        previous_response = manager.__dict__.get(
            "on_l2cap_configure_response", _MISSING
        )
        installed_send = manager.send_control_frame
        installed_response = manager.on_l2cap_configure_response

        def send_control_frame(
            connection: object,
            cid: int,
            frame: l2cap.L2CAP_Control_Frame,
        ) -> None:
            outgoing = frame
            channel = self._find_outgoing_aap_channel(
                manager, connection, frame
            )
            if channel is not None and not self._observation.request_observed:
                assert isinstance(frame, l2cap.L2CAP_Configure_Request)
                original_options = _decode_options(frame.options)
                original_mtu = _mtu(original_options)
                if original_mtu is None:
                    raise AAPLocalRXDiagnosticError(
                        "reviewed Bumble AAP request did not contain MTU"
                    )
                if self.mode is AAPLocalRXProfile.KERNEL_DEFAULT:
                    rewritten_options = tuple(
                        option
                        for option in original_options
                        if option.option_type
                        != l2cap.L2CAP_Configure_Request.ParameterType.MTU
                    )
                    outgoing = l2cap.L2CAP_Configure_Request(
                        identifier=frame.identifier,
                        destination_cid=frame.destination_cid,
                        flags=frame.flags,
                        options=b"".join(
                            option.encoded for option in rewritten_options
                        ),
                    )
                emitted_options = _decode_options(outgoing.options)
                self._request_identifier = outgoing.identifier
                self._observation = AAPLocalRXConfigurationObservation(
                    mode=self.mode,
                    request_observed=True,
                    request_option_types=tuple(
                        option.option_type for option in emitted_options
                    ),
                    request_mtu=_mtu(emitted_options),
                    request_flags=outgoing.flags,
                    request_identifier=outgoing.identifier,
                    request_destination_cid=outgoing.destination_cid,
                    internal_receive_mtu=int(channel.mtu),
                )
            installed_send(connection, cid, outgoing)

        def on_configure_response(
            connection: object,
            cid: int,
            response: l2cap.L2CAP_Configure_Response,
        ) -> None:
            if self._is_matching_peer_response(
                manager, connection, response
            ):
                options = _decode_options(response.options)
                self._observation = replace(
                    self._observation,
                    peer_response_observed=True,
                    peer_response_result=int(response.result),
                    peer_response_option_types=tuple(
                        option.option_type for option in options
                    ),
                    peer_response_mtu=_mtu(options),
                )
            installed_response(connection, cid, response)

        manager.send_control_frame = send_control_frame
        manager.on_l2cap_configure_response = on_configure_response
        try:
            yield
        finally:
            if previous_send is _MISSING:
                del manager.send_control_frame
            else:
                manager.send_control_frame = previous_send
            if previous_response is _MISSING:
                del manager.on_l2cap_configure_response
            else:
                manager.on_l2cap_configure_response = previous_response

    @staticmethod
    def _find_outgoing_aap_channel(
        manager: object,
        connection: object,
        frame: l2cap.L2CAP_Control_Frame,
    ) -> object | None:
        if not isinstance(frame, l2cap.L2CAP_Configure_Request):
            return None
        channels = manager.channels.get(connection.handle, {})
        for channel in channels.values():
            if (
                channel.psm == AAP_PSM
                and channel.destination_cid == frame.destination_cid
            ):
                return channel
        return None

    def _is_matching_peer_response(
        self,
        manager: object,
        connection: object,
        response: l2cap.L2CAP_Configure_Response,
    ) -> bool:
        if (
            not self._observation.request_observed
            or response.identifier != self._request_identifier
        ):
            return False
        channel = manager.find_channel(connection.handle, response.source_cid)
        return channel is not None and channel.psm == AAP_PSM
