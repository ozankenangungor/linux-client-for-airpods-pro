"""Reference-probe-only AAP local receive-MTU wire diagnostics."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata

from bumble import l2cap

from airpods_hr import _airpods_aap_core as _native

from airpods_hr.aap import HandshakeObservation
from airpods_hr.bumble_compat import SUPPORTED_BUMBLE_VERSION
from airpods_hr.protocol import AAP_PSM


L2CAP_CLASSIC_DEFAULT_MTU = 672
AAP_POST_ACK_TYPE_17_LENGTH_LIMIT = _native.diagnostic_post_ack_type_17_limit()
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
        first_type_2b, type_17_lengths, maximum, considered = _native.diagnostic_post_ack_shape(
            [
                (summary.header_u16_4_5, summary.length)
                for summary in observation.post_ack_frame_summaries
            ]
        )
        return cls(
            first_type_0x002b_length=first_type_2b,
            type_0x0017_frame_lengths=tuple(type_17_lengths),
            max_post_ack_frame_length=maximum,
            summaries_considered=considered,
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


class AAPLocalRXDiagnosticStrategy:
    """Observe or rewrite one initial host-originated AAP Configure Request."""

    def __init__(self, mode: AAPLocalRXProfile) -> None:
        validate_bumble_local_rx_api()
        self.mode = AAPLocalRXProfile(mode)
        self._native_state = _native._LocalRXState(
            self.mode is AAPLocalRXProfile.KERNEL_DEFAULT
        )

    @property
    def observation(self) -> AAPLocalRXConfigurationObservation:
        facts = self._native_state.snapshot()
        facts["mode"] = AAPLocalRXProfile(facts["mode"])
        facts["request_option_types"] = tuple(facts["request_option_types"])
        facts["peer_response_option_types"] = tuple(
            facts["peer_response_option_types"]
        )
        return AAPLocalRXConfigurationObservation(**facts)

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
            if channel is not None and not self._native_state.request_observed:
                assert isinstance(frame, l2cap.L2CAP_Configure_Request)
                try:
                    rewritten_bytes, _, _, original_mtu = _native.diagnostic_local_rx_rewrite(
                        self.mode is AAPLocalRXProfile.KERNEL_DEFAULT,
                        frame.options,
                    )
                except ValueError as error:
                    raise AAPLocalRXDiagnosticError(str(error)) from error
                if original_mtu is None:
                    raise AAPLocalRXDiagnosticError(
                        "reviewed Bumble AAP request did not contain MTU"
                    )
                if self.mode is AAPLocalRXProfile.KERNEL_DEFAULT:
                    outgoing = l2cap.L2CAP_Configure_Request(
                        identifier=frame.identifier,
                        destination_cid=frame.destination_cid,
                        flags=frame.flags,
                        options=rewritten_bytes,
                    )
                self._native_state.observe_request(
                    self.mode is AAPLocalRXProfile.KERNEL_DEFAULT,
                    frame.options,
                    outgoing.flags,
                    outgoing.identifier,
                    outgoing.destination_cid,
                    int(channel.mtu),
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
                try:
                    self._native_state.observe_response(
                        int(response.result), response.options
                    )
                except ValueError as error:
                    raise AAPLocalRXDiagnosticError(str(error)) from error
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
        if not self._native_state.identifier_matches(response.identifier):
            return False
        channel = manager.find_channel(connection.handle, response.source_cid)
        return self._native_state.response_matches(
            response.identifier,
            channel.psm if channel is not None else None,
            AAP_PSM,
        )
