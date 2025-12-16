"""Reference-probe-only SDP footprint and safe query observation."""

from __future__ import annotations


from dataclasses import dataclass
from enum import StrEnum


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


REFERENCE_SDP_QUERY_SUMMARY_LIMIT = 8
ATT_L2CAP_PSM = 0x001F
AVCTP_L2CAP_PSM = 0x0017
AVDTP_L2CAP_PSM = 0x0019
HANDS_FREE_UNIT_RFCOMM_CHANNEL = 7
AVCTP_VERSION = 0x0104
AVRCP_VERSION = 0x0106
AVDTP_VERSION = 0x0103
ADVANCED_AUDIO_VERSION = 0x0104
HANDS_FREE_VERSION = 0x0109

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


BLUEZ_LIKE_EXTRA_SERVICE_SPECS: tuple[BlueZLikeServiceSpec, ...] = (
    BlueZLikeServiceSpec("Generic Access", UUID.from_16_bits(0x1800), "att"),
    BlueZLikeServiceSpec("Generic Attribute", UUID.from_16_bits(0x1801), "att"),
    BlueZLikeServiceSpec("Device Information", UUID.from_16_bits(0x180A), "att"),
    BlueZLikeServiceSpec("Audio Input Control", UUID.from_16_bits(0x1843), "att"),
    BlueZLikeServiceSpec("Volume Control", UUID.from_16_bits(0x1844), "att"),
    BlueZLikeServiceSpec("Volume Offset Control", UUID.from_16_bits(0x1845), "att"),
    BlueZLikeServiceSpec("Generic Media Control", UUID.from_16_bits(0x1849), "att"),
    BlueZLikeServiceSpec("Microphone Control", UUID.from_16_bits(0x184D), "att"),
    BlueZLikeServiceSpec("Broadcast Audio Scan", UUID.from_16_bits(0x184F), "att"),
    BlueZLikeServiceSpec("Ranging Service", UUID.from_16_bits(0x185B), "att"),
    BlueZLikeServiceSpec(
        "A/V RemoteControlController",
        BT_AV_REMOTE_CONTROL_CONTROLLER_SERVICE,
        "avrcp-controller",
    ),
    BlueZLikeServiceSpec("Audio Sink", BT_AUDIO_SINK_SERVICE, "audio-sink"),
    BlueZLikeServiceSpec("Handsfree", BT_HANDSFREE_SERVICE, "handsfree"),
    BlueZLikeServiceSpec(
        "Message Notification Server",
        BT_MESSAGE_NOTIFICATION_SERVER_SERVICE,
        "obex",
        rfcomm_channel=17,
        profile_uuid=BT_MESSAGE_ACCESS_PROFILE_SERVICE,
        profile_version=0x0104,
    ),
    BlueZLikeServiceSpec(
        "Message Access Server",
        BT_MESSAGE_ACCESS_SERVER_SERVICE,
        "obex",
        rfcomm_channel=16,
        profile_uuid=BT_MESSAGE_ACCESS_PROFILE_SERVICE,
        profile_version=0x0100,
    ),
    BlueZLikeServiceSpec(
        "Phone Book Access Server",
        BT_PHONEBOOK_ACCESS_PSE_SERVICE,
        "obex",
        rfcomm_channel=15,
        profile_uuid=BT_PHONEBOOK_ACCESS_SERVICE,
        profile_version=0x0101,
    ),
    BlueZLikeServiceSpec(
        "Synchronization",
        BT_IR_MCSYNC_SERVICE,
        "obex",
        rfcomm_channel=14,
        profile_uuid=BT_IR_MCSYNC_SERVICE,
        profile_version=0x0100,
    ),
    BlueZLikeServiceSpec(
        "OBEX File Transfer",
        BT_OBEX_FILE_TRANSFER_SERVICE,
        "obex",
        rfcomm_channel=10,
        profile_uuid=BT_OBEX_FILE_TRANSFER_SERVICE,
        profile_version=0x0103,
    ),
    BlueZLikeServiceSpec(
        "OBEX Object Push",
        BT_OBEX_OBJECT_PUSH_SERVICE,
        "obex",
        rfcomm_channel=9,
        profile_uuid=BT_OBEX_OBJECT_PUSH_SERVICE,
        profile_version=0x0102,
    ),
    BlueZLikeServiceSpec(
        "Nokia OBEX PC Suite Services",
        NOKIA_OBEX_PC_SUITE_SERVICE,
        "obex",
        rfcomm_channel=24,
        profile_uuid=NOKIA_OBEX_PC_SUITE_SERVICE,
        profile_version=0x0100,
    ),
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
    handle = max(records, default=0) + 1
    for spec in BLUEZ_LIKE_EXTRA_SERVICE_SPECS:
        while handle in records:
            handle += 1
        records[handle] = _build_extra_record(spec, handle)
        handle += 1
    return records


