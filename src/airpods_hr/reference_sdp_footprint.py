"""Reference-probe-only SDP footprint and safe query observation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Iterator, Protocol

from bumble import sdp
from bumble.core import (
    BT_ADVANCED_AUDIO_DISTRIBUTION_SERVICE,
    BT_ATT_PROTOCOL_ID,
    BT_AUDIO_SINK_SERVICE,
    BT_AV_REMOTE_CONTROL_CONTROLLER_SERVICE,
    BT_AV_REMOTE_CONTROL_SERVICE,
    BT_AVCTP_PROTOCOL_ID,
    BT_AVDTP_PROTOCOL_ID,
    BT_HANDSFREE_SERVICE,
    BT_IR_MCSYNC_SERVICE,
    BT_L2CAP_PROTOCOL_ID,
    BT_MESSAGE_ACCESS_PROFILE_SERVICE,
    BT_MESSAGE_ACCESS_SERVER_SERVICE,
    BT_MESSAGE_NOTIFICATION_SERVER_SERVICE,
    BT_OBEX_FILE_TRANSFER_SERVICE,
    BT_OBEX_OBJECT_PUSH_SERVICE,
    BT_OBEX_PROTOCOL_ID,
    BT_PHONEBOOK_ACCESS_PSE_SERVICE,
    BT_PHONEBOOK_ACCESS_SERVICE,
    BT_RFCOMM_PROTOCOL_ID,
    UUID,
)

from airpods_hr.sdp import PreparedSDPCompatibilityProfile
from airpods_hr import _airpods_aap_core as _sdp_core


_CONSTANTS = dict(_sdp_core.sdp_constants())
REFERENCE_SDP_QUERY_SUMMARY_LIMIT = _CONSTANTS["REFERENCE_SDP_QUERY_SUMMARY_LIMIT"]
ATT_L2CAP_PSM = _CONSTANTS["ATT_L2CAP_PSM"]
AVCTP_L2CAP_PSM = _CONSTANTS["AVCTP_L2CAP_PSM"]
AVDTP_L2CAP_PSM = _CONSTANTS["AVDTP_L2CAP_PSM"]
HANDS_FREE_UNIT_RFCOMM_CHANNEL = _CONSTANTS["HANDS_FREE_UNIT_RFCOMM_CHANNEL"]
AVCTP_VERSION = _CONSTANTS["AVCTP_VERSION"]
AVRCP_VERSION = _CONSTANTS["AVRCP_VERSION"]
AVDTP_VERSION = _CONSTANTS["AVDTP_VERSION"]
ADVANCED_AUDIO_VERSION = _CONSTANTS["ADVANCED_AUDIO_VERSION"]
HANDS_FREE_VERSION = _CONSTANTS["HANDS_FREE_VERSION"]

_MISSING = object()


class ReferenceSDPFootprint(StrEnum):
    """Private SDP identity choices for the reference diagnostic."""

    PROVEN = "proven"
    BLUEZ_LIKE = "bluez-like"


@dataclass(frozen=True, slots=True)
class BlueZLikeServiceSpec:
    """Evidence-backed record semantics; handles are deliberately absent."""

    name: str
    service_uuid: UUID
    protocol: str
    rfcomm_channel: int | None = None
    profile_uuid: UUID | None = None
    profile_version: int | None = None


NOKIA_OBEX_PC_SUITE_SERVICE = UUID(
    "00005005-0000-1000-8000-0002ee000001",
    "Nokia OBEX PC Suite Services",
)


def _spec_uuid(value: tuple[int | None, str | None]) -> UUID:
    short, full = value
    if short is not None:
        return UUID.from_16_bits(short)
    if full == "00005005-0000-1000-8000-0002ee000001":
        return NOKIA_OBEX_PC_SUITE_SERVICE
    return UUID(full)


BLUEZ_LIKE_EXTRA_SERVICE_SPECS: tuple[BlueZLikeServiceSpec, ...] = tuple(
    BlueZLikeServiceSpec(
        name=name,
        service_uuid=_spec_uuid(service_uuid),
        protocol=protocol,
        rfcomm_channel=rfcomm_channel,
        profile_uuid=_spec_uuid(profile_uuid) if profile_uuid is not None else None,
        profile_version=profile_version,
    )
    for name, service_uuid, protocol, rfcomm_channel, profile_uuid, profile_version
    in _sdp_core.sdp_extra_service_specs()
)


def _attribute(attribute_id: int, value: sdp.DataElement) -> sdp.ServiceAttribute:
    return sdp.ServiceAttribute(attribute_id, value)


def _sequence(*elements: sdp.DataElement) -> sdp.DataElement:
    return sdp.DataElement.sequence(list(elements))


def _uuid(value: UUID) -> sdp.DataElement:
    return sdp.DataElement.uuid(value)


def _protocol(*descriptors: sdp.DataElement) -> sdp.ServiceAttribute:
    return _attribute(
        sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
        _sequence(*descriptors),
    )


def _profile(service: UUID, version: int) -> sdp.ServiceAttribute:
    return _attribute(
        sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
        _sequence(
            _sequence(
                _uuid(service), sdp.DataElement.unsigned_integer_16(version)
            )
        ),
    )


def _build_extra_record(
    spec: BlueZLikeServiceSpec, handle: int
) -> sdp.Server.Service:
    attributes = [
        _attribute(
            sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
            sdp.DataElement.unsigned_integer_32(handle),
        ),
        _attribute(
            sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
            _sequence(_uuid(spec.service_uuid)),
        ),
    ]
    if spec.protocol == "att":
        attributes.append(
            _protocol(
                _sequence(
                    _uuid(BT_L2CAP_PROTOCOL_ID),
                    sdp.DataElement.unsigned_integer_16(ATT_L2CAP_PSM),
                ),
                _sequence(_uuid(BT_ATT_PROTOCOL_ID)),
            )
        )
    elif spec.protocol == "avrcp-controller":
        attributes.extend(
            (
                _protocol(
                    _sequence(
                        _uuid(BT_L2CAP_PROTOCOL_ID),
                        sdp.DataElement.unsigned_integer_16(AVCTP_L2CAP_PSM),
                    ),
                    _sequence(
                        _uuid(BT_AVCTP_PROTOCOL_ID),
                        sdp.DataElement.unsigned_integer_16(AVCTP_VERSION),
                    ),
                ),
                _profile(BT_AV_REMOTE_CONTROL_SERVICE, AVRCP_VERSION),
            )
        )
    elif spec.protocol == "audio-sink":
        attributes.extend(
            (
                _protocol(
                    _sequence(
                        _uuid(BT_L2CAP_PROTOCOL_ID),
                        sdp.DataElement.unsigned_integer_16(AVDTP_L2CAP_PSM),
                    ),
                    _sequence(
                        _uuid(BT_AVDTP_PROTOCOL_ID),
                        sdp.DataElement.unsigned_integer_16(AVDTP_VERSION),
                    ),
                ),
                _profile(
                    BT_ADVANCED_AUDIO_DISTRIBUTION_SERVICE,
                    ADVANCED_AUDIO_VERSION,
                ),
            )
        )
    elif spec.protocol == "handsfree":
        attributes.extend(
            (
                _protocol(
                    _sequence(_uuid(BT_L2CAP_PROTOCOL_ID)),
                    _sequence(
                        _uuid(BT_RFCOMM_PROTOCOL_ID),
                        sdp.DataElement.unsigned_integer_8(
                            HANDS_FREE_UNIT_RFCOMM_CHANNEL
                        ),
                    ),
                ),
                _profile(BT_HANDSFREE_SERVICE, HANDS_FREE_VERSION),
            )
        )
    elif spec.protocol == "obex":
        if (
            spec.rfcomm_channel is None
            or spec.profile_uuid is None
            or spec.profile_version is None
        ):
            raise ValueError("incomplete reference OBEX service specification")
        attributes.extend(
            (
                _protocol(
                    _sequence(_uuid(BT_L2CAP_PROTOCOL_ID)),
                    _sequence(
                        _uuid(BT_RFCOMM_PROTOCOL_ID),
                        sdp.DataElement.unsigned_integer_8(
                            spec.rfcomm_channel
                        ),
                    ),
                    _sequence(_uuid(BT_OBEX_PROTOCOL_ID)),
                ),
                _profile(spec.profile_uuid, spec.profile_version),
            )
        )
    else:  # pragma: no cover - closed enum-like fixture
        raise ValueError(f"unsupported reference SDP protocol {spec.protocol!r}")
    return sdp.Server.Service(attributes)


def augment_reference_sdp_records(
    proven_records: dict[int, sdp.Server.Service],
    footprint: ReferenceSDPFootprint,
) -> dict[int, sdp.Server.Service]:
    """Return the proven records, optionally with deterministic extras."""

    footprint = ReferenceSDPFootprint(footprint)
    if footprint is ReferenceSDPFootprint.PROVEN:
        return proven_records
    records = dict(proven_records)
    handles = _sdp_core.sdp_allocate_handles(
        [str(handle) for handle in records], len(BLUEZ_LIKE_EXTRA_SERVICE_SPECS)
    )
    for spec, handle_text in zip(BLUEZ_LIKE_EXTRA_SERVICE_SPECS, handles):
        handle = int(handle_text)
        records[handle] = _build_extra_record(spec, handle)
    return records


@dataclass(frozen=True, slots=True)
class ReferenceSDPQuerySummary:
    """Allowlisted metadata for one parsed ServiceSearchAttribute request."""

    search_uuids: tuple[int, ...]
    attribute_ranges: tuple[tuple[int, int], ...]
    maximum_attribute_byte_count: int
    continuation_used: bool
    matching_record_count: int
    total_response_bytes: int


@dataclass(frozen=True, slots=True)
class ReferenceSDPQuerySnapshot:
    footprint: ReferenceSDPFootprint
    requests_observed: int = 0
    summaries: tuple[ReferenceSDPQuerySummary, ...] = ()
    target_l2cap_full_attribute_query: ReferenceSDPQuerySummary | None = None

    def l2cap_query(self) -> ReferenceSDPQuerySummary | None:
        index = _sdp_core.sdp_first_l2cap_summary_index(
            [list(summary.search_uuids) for summary in self.summaries]
        )
        return (
            self.summaries[index]
            if index is not None
            else self.target_l2cap_full_attribute_query
        )

    def l2cap_full_attribute_query(self) -> ReferenceSDPQuerySummary | None:
        return self.target_l2cap_full_attribute_query


def _uuid16s(pattern: sdp.DataElement) -> tuple[int, ...]:
    return tuple(
        _sdp_core.sdp_query_uuid16s(
            [
                getattr(getattr(element, "value", None), "uuid_bytes", b"")
                for element in getattr(pattern, "value", ())
            ]
        )
    )


def _attribute_ranges(
    attribute_id_list: sdp.DataElement,
) -> tuple[tuple[int, int], ...]:
    return tuple(
        _sdp_core.sdp_query_attribute_ranges(
            [
                (value, element.value_size)
                for element in getattr(attribute_id_list, "value", ())
                if isinstance((value := getattr(element, "value", None)), int)
            ]
        )
    )


def _response_metadata(
    server: sdp.Server, request: sdp.SDP_ServiceSearchAttributeRequest
) -> tuple[int, int]:
    matching_services = server.match_services(request.service_search_pattern)
    attribute_lists = sdp.DataElement.sequence([])
    for service in matching_services.values():
        attributes = sdp.Server.get_service_attributes(
            service, request.attribute_id_list.value
        )
        if attributes.value:
            attribute_lists.value.append(attributes)
    return len(matching_services), len(bytes(attribute_lists))




class ReferenceSDPQueryDiagnostics:
    """Observe parsed SDP query metadata without retaining packet bytes."""

    def __init__(self, footprint: ReferenceSDPFootprint) -> None:
        self.footprint = ReferenceSDPFootprint(footprint)
        self._active = False
        self._requests_observed = 0
        self._summaries: list[ReferenceSDPQuerySummary] = []
        self._target_l2cap_full_attribute_query: (
            ReferenceSDPQuerySummary | None
        ) = None

    def snapshot(self) -> ReferenceSDPQuerySnapshot:
        return ReferenceSDPQuerySnapshot(
            footprint=self.footprint,
            requests_observed=self._requests_observed,
            summaries=tuple(self._summaries),
            target_l2cap_full_attribute_query=(
                self._target_l2cap_full_attribute_query
            ),
        )

    @staticmethod
    def _decision(
        summary: ReferenceSDPQuerySummary,
        continuation_state_len: int,
        stored: int,
        target_already_seen: bool,
    ) -> tuple[bool, bool, bool, bool]:
        return _sdp_core.sdp_query_decision(
            list(summary.search_uuids),
            list(summary.attribute_ranges),
            summary.maximum_attribute_byte_count,
            None,
            summary.total_response_bytes,
            continuation_state_len,
            stored,
            target_already_seen,
        )

    @staticmethod
    def _summarize(
        server: sdp.Server,
        request: sdp.SDP_ServiceSearchAttributeRequest,
    ) -> ReferenceSDPQuerySummary:
        matches, response_bytes = _response_metadata(server, request)
        channel = getattr(server, "channel", None)
        peer_mtu = getattr(channel, "peer_mtu", None)
        if not isinstance(peer_mtu, int):
            peer_mtu = None
        search_uuids = _uuid16s(request.service_search_pattern)
        attribute_ranges = _attribute_ranges(request.attribute_id_list)
        _, continuation_used, _, _ = _sdp_core.sdp_query_decision(
            list(search_uuids),
            list(attribute_ranges),
            request.maximum_attribute_byte_count,
            peer_mtu,
            response_bytes,
            len(request.continuation_state),
            0,
            False,
        )
        return ReferenceSDPQuerySummary(
            search_uuids=search_uuids,
            attribute_ranges=attribute_ranges,
            maximum_attribute_byte_count=request.maximum_attribute_byte_count,
            continuation_used=continuation_used,
            matching_record_count=matches,
            total_response_bytes=response_bytes,
        )

    @contextmanager
    def observe(self, device: object) -> Iterator[None]:
        if self._active:
            raise RuntimeError("reference SDP query diagnostics are already active")
        server = getattr(device, "sdp_server", None)
        if not isinstance(server, sdp.Server):
            raise RuntimeError("reference runtime has no Bumble SDP server")
        previous = server.__dict__.get("on_pdu", _MISSING)
        original = server.on_pdu
        self._active = True
        self._requests_observed = 0
        self._summaries.clear()
        self._target_l2cap_full_attribute_query = None

        def observe_pdu(pdu: bytes) -> None:
            try:
                request = sdp.SDP_PDU.from_bytes(pdu)
            except Exception:
                request = None
            if isinstance(request, sdp.SDP_ServiceSearchAttributeRequest):
                self._requests_observed += 1
                summary = self._summarize(server, request)
                target, _, retain, mark_prior = self._decision(
                    summary,
                    len(request.continuation_state),
                    len(self._summaries),
                    self._target_l2cap_full_attribute_query is not None,
                )
                if target:
                    if self._target_l2cap_full_attribute_query is None:
                        self._target_l2cap_full_attribute_query = summary
                    elif mark_prior:
                        self._target_l2cap_full_attribute_query = replace(
                            self._target_l2cap_full_attribute_query,
                            continuation_used=True,
                        )
                elif retain:
                    self._summaries.append(summary)
            original(pdu)

        server.on_pdu = observe_pdu  # type: ignore[method-assign]
        try:
            yield
        finally:
            if previous is _MISSING:
                del server.on_pdu
            else:
                server.on_pdu = previous  # type: ignore[method-assign]
            self._active = False


class _Profile(Protocol):
    def prepare(self, candidate: object) -> PreparedSDPCompatibilityProfile: ...


class _CombinedObserver:
    def __init__(self, first: object, second: ReferenceSDPQueryDiagnostics) -> None:
        self._first = first
        self._second = second

    @contextmanager
    def observe(self, device: object) -> Iterator[None]:
        first_context = getattr(self._first, "observe")(device)
        with first_context:
            with self._second.observe(device):
                yield


class ReferenceSDPFootprintProfile:
    """Wrap a prepared reference profile without changing its proven builder."""

    def __init__(
        self,
        delegate: _Profile,
        footprint: ReferenceSDPFootprint,
        diagnostics: ReferenceSDPQueryDiagnostics,
    ) -> None:
        self._delegate = delegate
        self._footprint = ReferenceSDPFootprint(footprint)
        self._diagnostics = diagnostics

    def prepare(self, candidate: object) -> PreparedSDPCompatibilityProfile:
        prepared = self._delegate.prepare(candidate)
        observer: object = self._diagnostics
        if prepared.diagnostics is not None:
            observer = _CombinedObserver(prepared.diagnostics, self._diagnostics)
        return PreparedSDPCompatibilityProfile(
            records=augment_reference_sdp_records(
                prepared.records, self._footprint
            ),
            installed_callback=prepared.installed_callback,
            diagnostics=observer,
        )


class ReferenceSDPFootprintStrategy:
    """Inject a footprint only at the reference diagnostic session boundary."""

    def __init__(self, footprint: ReferenceSDPFootprint) -> None:
        self.footprint = ReferenceSDPFootprint(footprint)
        self.diagnostics = ReferenceSDPQueryDiagnostics(self.footprint)

    def wrap_profile(self, profile: _Profile) -> ReferenceSDPFootprintProfile:
        return ReferenceSDPFootprintProfile(
            profile, self.footprint, self.diagnostics
        )


class ReferenceSDPFootprintSecureSession:
    """Private proxy that replaces only the probe-created SDP profile."""

    def __init__(self, delegate: object, strategy: ReferenceSDPFootprintStrategy):
        self.delegate = delegate
        self.strategy = strategy

    def open(self, *, pre_connect_profile: _Profile | None = None) -> Any:
        profile = (
            None
            if pre_connect_profile is None
            else self.strategy.wrap_profile(pre_connect_profile)
        )
        return getattr(self.delegate, "open")(
            pre_connect_profile=profile
        )
