"""Secret-safe, instance-scoped diagnostics for Bumble's SDP server."""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import metadata
from time import monotonic
from typing import Callable, Iterator

from bumble import l2cap, sdp
from airpods_hr import _airpods_aap_core as _sdp_core


SUPPORTED_BUMBLE_VERSION = "0.0.234"
_MISSING = object()

_STOCK_SDP_ON_CONNECTION = sdp.Server.on_connection
_STOCK_SDP_ON_PDU = sdp.Server.on_pdu
_STOCK_L2CAP_CONNECTION_REQUEST = (
    l2cap.ChannelManager.on_l2cap_connection_request
)


class SDPDiagnosticsError(RuntimeError):
    """Raised when the reviewed Bumble diagnostic boundary is unavailable."""


class ProtocolTimelineKind(StrEnum):
    """Allowlisted events that contain no packet or peer data."""

    HANDSHAKE_SENT = "handshake_sent"
    ACK_OBSERVED = "ack_observed"
    FIRST_POST_ACK_FRAME = "first_post_ack_frame"
    FIRST_357_BYTE_FRAME = "first_357_byte_frame"
    PSM_1_REQUEST = "psm_1_request"
    PSM_3_REQUEST = "psm_3_request"
    PSM_23_REQUEST = "psm_23_request"
    PSM_25_REQUEST = "psm_25_request"
    OTHER_PSM_REQUEST = "other_psm_request"


@dataclass(frozen=True, slots=True)
class ProtocolTimelineEvent:
    kind: ProtocolTimelineKind
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class ProtocolTimelineSnapshot:
    events: tuple[ProtocolTimelineEvent, ...] = ()
    total_events: int = 0

    def first(self, kind: ProtocolTimelineKind) -> ProtocolTimelineEvent | None:
        return next((event for event in self.events if event.kind is kind), None)


class SafeProtocolTimeline:
    """Bounded monotonic event history with an allowlisted vocabulary."""

    def __init__(
        self,
        *,
        limit: int = 64,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if limit <= 0:
            raise ValueError("protocol timeline limit must be positive")
        self._clock = clock
        self._origin = clock()
        self._state = _sdp_core.SDPTimelineState(limit)

    def record(self, kind: ProtocolTimelineKind) -> None:
        kind = ProtocolTimelineKind(kind)
        elapsed = (
            self._clock() - self._origin if not self._state.full() else 0.0
        )
        self._state.record(kind.value, elapsed)

    def snapshot(self) -> ProtocolTimelineSnapshot:
        events, total = self._state.snapshot()
        return ProtocolTimelineSnapshot(
            events=tuple(
                ProtocolTimelineEvent(ProtocolTimelineKind(kind), elapsed)
                for kind, elapsed in events
            ),
            total_events=total,
        )


@dataclass(frozen=True, slots=True)
class SDPDiagnosticsSnapshot:
    """Counters and booleans that contain no packet or peer identity data."""

    sdp_connection_observed: bool = False
    sdp_requests_observed: int = 0
    pnp_information_queries: int = 0
    handsfree_audio_gateway_queries: int = 0
    audio_source_queries: int = 0
    avrcp_target_queries: int = 0
    other_queries: int = 0
    pnp_information_match_served: bool = False
    handsfree_audio_gateway_match_served: bool = False
    audio_source_match_served: bool = False
    avrcp_target_match_served: bool = False
    psm_1_requests: int = 0
    psm_3_requests: int = 0
    psm_23_requests: int = 0
    psm_25_requests: int = 0
    other_psm_requests: int = 0
    timeline: ProtocolTimelineSnapshot = field(
        default_factory=ProtocolTimelineSnapshot
    )


_KNOWN_SERVICES = _sdp_core.sdp_known_services()


def _parameters(function: object) -> tuple[str, ...]:
    try:
        return tuple(inspect.signature(function).parameters)
    except (TypeError, ValueError) as error:
        raise SDPDiagnosticsError(
            "Bumble SDP diagnostic method signatures could not be inspected"
        ) from error


def validate_bumble_sdp_diagnostics_api(
    *, installed_version: str | None = None
) -> None:
    """Require the exact Bumble internals reviewed for this observer."""

    version = installed_version
    if version is None:
        try:
            version = metadata.version("bumble")
        except metadata.PackageNotFoundError as error:
            raise SDPDiagnosticsError(
                "Bumble is unavailable for SDP diagnostics"
            ) from error
    if version != SUPPORTED_BUMBLE_VERSION:
        raise SDPDiagnosticsError(
            f"unsupported Bumble version {version!r} for SDP diagnostics; "
            f"expected {SUPPORTED_BUMBLE_VERSION}"
        )
    if (
        sdp.Server.on_connection is not _STOCK_SDP_ON_CONNECTION
        or sdp.Server.on_pdu is not _STOCK_SDP_ON_PDU
        or l2cap.ChannelManager.on_l2cap_connection_request
        is not _STOCK_L2CAP_CONNECTION_REQUEST
    ):
        raise SDPDiagnosticsError(
            "Bumble SDP/L2CAP diagnostic methods differ from 0.0.234"
        )
    if (
        _parameters(_STOCK_SDP_ON_CONNECTION) != ("self", "channel")
        or _parameters(_STOCK_SDP_ON_PDU) != ("self", "pdu")
        or _parameters(_STOCK_L2CAP_CONNECTION_REQUEST)
        != ("self", "connection", "cid", "request")
    ):
        raise SDPDiagnosticsError(
            "Bumble SDP/L2CAP diagnostic signatures differ from 0.0.234"
        )


class BumbleSDPDiagnostics:
    """Observe one temporary Device without changing SDP response semantics."""

    def __init__(self, *, timeline: SafeProtocolTimeline | None = None) -> None:
        validate_bumble_sdp_diagnostics_api()
        self._timeline = timeline or SafeProtocolTimeline()
        self._active = False
        self._reset()

    def _reset(self) -> None:
        self._state = _sdp_core.SDPDiagnosticState()

    def snapshot(self) -> SDPDiagnosticsSnapshot:
        connected, requests, queries, matches, psm = self._state.snapshot()
        return SDPDiagnosticsSnapshot(
            sdp_connection_observed=connected,
            sdp_requests_observed=requests,
            pnp_information_queries=queries[0],
            handsfree_audio_gateway_queries=queries[1],
            audio_source_queries=queries[2],
            avrcp_target_queries=queries[3],
            other_queries=queries[4],
            pnp_information_match_served=matches[0],
            handsfree_audio_gateway_match_served=matches[1],
            audio_source_match_served=matches[2],
            avrcp_target_match_served=matches[3],
            psm_1_requests=psm[0],
            psm_3_requests=psm[1],
            psm_23_requests=psm[2],
            psm_25_requests=psm[3],
            other_psm_requests=psm[4],
            timeline=self._timeline.snapshot(),
        )

    @staticmethod
    def _classify_pattern(pattern: sdp.DataElement) -> tuple[str, ...]:
        try:
            values = tuple(element.value for element in pattern.value)
        except (AttributeError, TypeError):
            return ()
        return tuple(
            _sdp_core.sdp_classify_service_uuid_bytes(
                [getattr(value, "uuid_bytes", b"") for value in values]
            )
        )


    def _classify_request(
        self, request: sdp.SDP_PDU, server: sdp.Server
    ) -> tuple[str, ...]:
        self._state.observe_request()
        matched: tuple[str, ...] = ()
        if isinstance(request, sdp.SDP_ServiceSearchRequest):
            names = self._classify_pattern(request.service_search_pattern)
            matching_services = server.match_services(
                request.service_search_pattern
            )
            matched = tuple(
                name
                for name, _service_uuid, handle in _KNOWN_SERVICES
                if name in names and handle in matching_services
            )
        elif isinstance(request, sdp.SDP_ServiceSearchAttributeRequest):
            names = self._classify_pattern(request.service_search_pattern)
            matching_services = server.match_services(
                request.service_search_pattern
            )
            matched = tuple(
                name
                for name, _service_uuid, handle in _KNOWN_SERVICES
                if name in names
                and handle in matching_services
                and sdp.Server.get_service_attributes(
                    matching_services[handle], request.attribute_id_list.value
                ).value
            )
        elif isinstance(request, sdp.SDP_ServiceAttributeRequest):
            names = tuple(
                _sdp_core.sdp_classify_handle(request.service_record_handle)
            )
            matched = tuple(
                name
                for name in names
                if request.service_record_handle in server.service_records
                and sdp.Server.get_service_attributes(
                    server.service_records[request.service_record_handle],
                    request.attribute_id_list.value,
                ).value
            )
        else:
            names = ()

        self._state.account_query(list(names))
        return matched

    def _record_psm(self, psm: int) -> None:
        timeline_kind = self._state.observe_psm(psm)
        self._timeline.record(ProtocolTimelineKind(timeline_kind))

    @contextmanager
    def observe(self, device: object) -> Iterator[None]:
        """Attach only to this Device's server and restore every hook."""

        if self._active:
            raise SDPDiagnosticsError("SDP diagnostics are already active")
        manager = getattr(device, "l2cap_channel_manager", None)
        server = getattr(device, "sdp_server", None)
        if not isinstance(manager, l2cap.ChannelManager) or not isinstance(
            server, sdp.Server
        ):
            raise SDPDiagnosticsError(
                "temporary Bumble Device lacks the reviewed SDP runtime"
            )
        sdp_l2cap_server = manager.servers.get(sdp.SDP_PSM)
        handler = getattr(sdp_l2cap_server, "handler", None)
        if (
            sdp_l2cap_server is None
            or getattr(handler, "__self__", None) is not server
            or getattr(handler, "__func__", None) is not _STOCK_SDP_ON_CONNECTION
        ):
            raise SDPDiagnosticsError(
                "Bumble PSM 1 is not wired to the reviewed SDP server"
            )

        self._reset()
        self._active = True
        previous_server_on_pdu = server.__dict__.get("on_pdu", _MISSING)
        original_server_on_pdu = server.on_pdu
        previous_manager_handler = manager.__dict__.get(
            "on_l2cap_connection_request", _MISSING
        )
        original_manager_handler = manager.on_l2cap_connection_request

        def observe_pdu(pdu: bytes) -> None:
            matched: tuple[str, ...] = ()
            try:
                request = sdp.SDP_PDU.from_bytes(pdu)
            except Exception:
                self._state.observe_request()
                self._state.account_query([])
            else:
                matched = self._classify_request(request, server)
            original_server_on_pdu(pdu)
            for name in matched:
                self._state.mark_served(name)

        def observe_connection_request(
            connection: object,
            cid: int,
            request: l2cap.L2CAP_Connection_Request,
        ) -> None:
            self._record_psm(request.psm)
            original_manager_handler(connection, cid, request)

        def observe_sdp_connection(_channel: object) -> None:
            self._state.observe_connection()

        server.on_pdu = observe_pdu  # type: ignore[method-assign]
        manager.on_l2cap_connection_request = (  # type: ignore[method-assign]
            observe_connection_request
        )
        sdp_l2cap_server.on(
            sdp_l2cap_server.EVENT_CONNECTION, observe_sdp_connection
        )
        primary_error: BaseException | None = None
        try:
            yield
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                sdp_l2cap_server.remove_listener(
                    sdp_l2cap_server.EVENT_CONNECTION,
                    observe_sdp_connection,
                )
                if previous_server_on_pdu is _MISSING:
                    del server.on_pdu
                else:
                    server.on_pdu = previous_server_on_pdu  # type: ignore[method-assign]
                if previous_manager_handler is _MISSING:
                    del manager.on_l2cap_connection_request
                else:
                    manager.on_l2cap_connection_request = (  # type: ignore[method-assign]
                        previous_manager_handler
                    )
            except BaseException as cleanup_error:
                if primary_error is not None:
                    primary_error.add_note(
                        "SDP diagnostic hooks also failed to restore"
                    )
                else:
                    raise SDPDiagnosticsError(
                        "SDP diagnostic hooks failed to restore"
                    ) from cleanup_error
            finally:
                self._active = False
