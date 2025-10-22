"""Replay tests for Bumble's real SDP server and safe diagnostics."""

from __future__ import annotations


import unittest

from bumble import sdp
from bumble.core import (
    BT_AUDIO_SOURCE_SERVICE,
    BT_AV_REMOTE_CONTROL_SERVICE,
    BT_AV_REMOTE_CONTROL_TARGET_SERVICE,
    BT_AVDTP_PROTOCOL_ID,
    BT_HANDSFREE_AUDIO_GATEWAY_SERVICE,
    BT_L2CAP_PROTOCOL_ID,
    BT_PNP_INFORMATION_SERVICE,
    BT_RFCOMM_PROTOCOL_ID,
    UUID,
)
from bumble.device import Device, DeviceConfiguration

from airpods_hr.authentication import BumbleClassicRuntime
from airpods_hr.sdp import (
    AVDTP_L2CAP_PSM,
    AVDTP_VERSION,
    AVRCP_VERSION,
    HANDS_FREE_RFCOMM_CHANNEL,
    PNP_PRODUCT_ID_ATTRIBUTE_ID,
    PNP_VENDOR_ID_ATTRIBUTE_ID,
    PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID,
    PNP_VENDOR_ID_SOURCE_USB,
    PNP_VERSION_ATTRIBUTE_ID,
    USBAdapterIdentity,
    build_sdp_compatibility_records,
)


class ReplayChannel:
    def __init__(self) -> None:
        self.peer_mtu = 2048
        self.sink = None
        self.responses: list[bytes] = []

    def write(self, response: object) -> None:
        self.responses.append(bytes(response))


def search_attribute_request(
    service_uuid: UUID, attribute_ids: tuple[int, ...]
) -> bytes:
    return bytes(
        sdp.SDP_ServiceSearchAttributeRequest(
            transaction_id=1,
            service_search_pattern=sdp.DataElement.sequence(
                [sdp.DataElement.uuid(service_uuid)]
            ),
            maximum_attribute_byte_count=0xFFFF,
            attribute_id_list=sdp.DataElement.sequence(
                [
                    sdp.DataElement.unsigned_integer_16(attribute_id)
                    for attribute_id in attribute_ids
                ]
            ),
            continuation_state=b"",
        )
    )


def response_attributes(response_bytes: bytes) -> dict[int, sdp.DataElement]:
    response = sdp.SDP_PDU.from_bytes(response_bytes)
    assert isinstance(response, sdp.SDP_ServiceSearchAttributeResponse)
    outer = sdp.DataElement.from_bytes(response.attribute_lists)
    if not outer.value:
        return {}
    values = outer.value[0].value
    return {
        values[index].value: values[index + 1]
        for index in range(0, len(values), 2)
    }


class RealBumbleSDPReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.device = Device(
            config=DeviceConfiguration(classic_enabled=True, le_enabled=False)
        )
        self.runtime = BumbleClassicRuntime(
            self.device,
            connect_timeout=1,
            security_timeout=1,
            disconnect_timeout=1,
        )
        self.identity = USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)
        self.records = build_sdp_compatibility_records(self.identity)

    def replay(
        self, service_uuid: UUID, attribute_ids: tuple[int, ...]
    ) -> dict[int, sdp.DataElement]:
        channel = ReplayChannel()
        psm_server = self.device.l2cap_channel_manager.servers[sdp.SDP_PSM]
        psm_server.on_connection(channel)
        self.assertIsNotNone(channel.sink)
        channel.sink(search_attribute_request(service_uuid, attribute_ids))
        self.assertEqual(len(channel.responses), 1)
        return response_attributes(channel.responses[0])

    def test_device_property_replacement_reaches_registered_server(self) -> None:
        self.assertIs(
            self.device.l2cap_channel_manager.servers[sdp.SDP_PSM].handler.__self__,
            self.device.sdp_server,
        )
        with self.runtime.temporary_sdp_records(self.records):
            attributes = self.replay(
                BT_PNP_INFORMATION_SERVICE,
                (PNP_VENDOR_ID_ATTRIBUTE_ID,),
            )
            self.assertEqual(
                attributes[PNP_VENDOR_ID_ATTRIBUTE_ID].value,
                self.identity.vendor_id,
            )

        self.assertEqual(
            self.replay(
                BT_PNP_INFORMATION_SERVICE,
                (PNP_VENDOR_ID_ATTRIBUTE_ID,),
            ),
            {},
        )

    def test_pnp_query_returns_derived_requested_attributes(self) -> None:
        with self.runtime.temporary_sdp_records(self.records):
            attributes = self.replay(
                BT_PNP_INFORMATION_SERVICE,
                (
                    PNP_VENDOR_ID_ATTRIBUTE_ID,
                    PNP_PRODUCT_ID_ATTRIBUTE_ID,
                    PNP_VERSION_ATTRIBUTE_ID,
                    PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID,
                ),
            )

        self.assertEqual(attributes[PNP_VENDOR_ID_ATTRIBUTE_ID].value, 0x1234)
        self.assertEqual(attributes[PNP_PRODUCT_ID_ATTRIBUTE_ID].value, 0x5678)
        self.assertEqual(attributes[PNP_VERSION_ATTRIBUTE_ID].value, 0x9ABC)
        self.assertEqual(
            attributes[PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID].value,
            PNP_VENDOR_ID_SOURCE_USB,
        )

    def test_handsfree_query_returns_l2cap_and_rfcomm_channel_13(self) -> None:
        with self.runtime.temporary_sdp_records(self.records):
            attributes = self.replay(
                BT_HANDSFREE_AUDIO_GATEWAY_SERVICE,
                (sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,),
            )

        protocols = attributes[
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID
        ].value
        self.assertEqual(protocols[0].value[0].value, BT_L2CAP_PROTOCOL_ID)
        self.assertEqual(protocols[1].value[0].value, BT_RFCOMM_PROTOCOL_ID)
        self.assertEqual(protocols[1].value[1].value, HANDS_FREE_RFCOMM_CHANNEL)

    def test_audio_source_query_returns_avdtp_profile(self) -> None:
        with self.runtime.temporary_sdp_records(self.records):
            attributes = self.replay(
                BT_AUDIO_SOURCE_SERVICE,
                (sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,),
            )

        protocols = attributes[
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID
        ].value
        self.assertEqual(protocols[0].value[0].value, BT_L2CAP_PROTOCOL_ID)
        self.assertEqual(protocols[0].value[1].value, AVDTP_L2CAP_PSM)
        self.assertEqual(protocols[1].value[0].value, BT_AVDTP_PROTOCOL_ID)
        self.assertEqual(protocols[1].value[1].value, AVDTP_VERSION)

    def test_avrcp_target_query_returns_avrcp_1_6(self) -> None:
        with self.runtime.temporary_sdp_records(self.records):
            attributes = self.replay(
                BT_AV_REMOTE_CONTROL_TARGET_SERVICE,
                (sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,),
            )

        profile = attributes[
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID
        ].value[0].value
        self.assertEqual(profile[0].value, BT_AV_REMOTE_CONTROL_SERVICE)
        self.assertEqual(profile[1].value, AVRCP_VERSION)

    def test_unknown_service_query_returns_no_record(self) -> None:
        with self.runtime.temporary_sdp_records(self.records):
            attributes = self.replay(
                UUID.from_16_bits(0xF00D),
                (sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,),
            )

        self.assertEqual(attributes, {})


