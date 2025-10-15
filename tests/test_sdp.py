"""Tests for the four-record temporary SDP compatibility profile."""

from __future__ import annotations


import unittest

from unittest.mock import patch

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
)


from airpods_hr.sdp import (
    AUDIO_SOURCE_HANDLE,
    AVDTP_L2CAP_PSM,
    AVDTP_VERSION,
    AVRCP_TARGET_HANDLE,
    AVRCP_VERSION,
    HANDS_FREE_AUDIO_GATEWAY_HANDLE,
    HANDS_FREE_RFCOMM_CHANNEL,
    PNP_INFORMATION_HANDLE,
    PNP_PRODUCT_ID_ATTRIBUTE_ID,
    PNP_VENDOR_ID_ATTRIBUTE_ID,
    PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID,
    PNP_VENDOR_ID_SOURCE_USB,
    PNP_VERSION_ATTRIBUTE_ID,
    AdapterIdentityError,
    USBAdapterIdentity,
    build_sdp_compatibility_records,
)


def attributes(record):
    return {attribute.id: attribute.value for attribute in record}


class AdapterIdentityTests(unittest.TestCase):
    def test_supported_bluez_usb_modalias_is_parsed(self) -> None:
        identity = USBAdapterIdentity.from_bluez_modalias("usb:v1234p5678d9AbC")
        self.assertEqual(identity, USBAdapterIdentity(0x1234, 0x5678, 0x9ABC))

    def test_missing_or_malformed_modalias_is_rejected(self) -> None:
        malformed = (
            None,
            "",
            "pci:v1234p5678d9ABC",
            "usb:v123p5678d9ABC",
            "usb:v1234p5678d9ABC/extra",
            "../usb:v1234p5678d9ABC",
        )
        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(AdapterIdentityError):
                    USBAdapterIdentity.from_bluez_modalias(value)


class SDPRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)
        self.records = build_sdp_compatibility_records(self.identity)

    def test_profile_contains_exactly_four_records(self) -> None:
        self.assertEqual(
            set(self.records),
            {
                PNP_INFORMATION_HANDLE,
                HANDS_FREE_AUDIO_GATEWAY_HANDLE,
                AUDIO_SOURCE_HANDLE,
                AVRCP_TARGET_HANDLE,
            },
        )

    def test_pnp_record_uses_derived_usb_identity(self) -> None:
        record = attributes(self.records[PNP_INFORMATION_HANDLE])
        self.assertEqual(
            record[sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID].value[0].value,
            BT_PNP_INFORMATION_SERVICE,
        )
        self.assertEqual(record[PNP_VENDOR_ID_ATTRIBUTE_ID].value, 0x1234)
        self.assertEqual(record[PNP_PRODUCT_ID_ATTRIBUTE_ID].value, 0x5678)
        self.assertEqual(record[PNP_VERSION_ATTRIBUTE_ID].value, 0x9ABC)
        self.assertEqual(
            record[PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID].value,
            PNP_VENDOR_ID_SOURCE_USB,
        )

    def test_hands_free_record_uses_rfcomm_channel_13(self) -> None:
        record = attributes(self.records[HANDS_FREE_AUDIO_GATEWAY_HANDLE])
        self.assertEqual(
            record[sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID].value[0].value,
            BT_HANDSFREE_AUDIO_GATEWAY_SERVICE,
        )
        protocols = record[sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID].value
        self.assertEqual(protocols[0].value[0].value, BT_L2CAP_PROTOCOL_ID)
        self.assertEqual(protocols[1].value[0].value, BT_RFCOMM_PROTOCOL_ID)
        self.assertEqual(protocols[1].value[1].value, HANDS_FREE_RFCOMM_CHANNEL)

    def test_audio_source_record_uses_avdtp_profile_data(self) -> None:
        record = attributes(self.records[AUDIO_SOURCE_HANDLE])
        self.assertEqual(
            record[sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID].value[0].value,
            BT_AUDIO_SOURCE_SERVICE,
        )
        protocols = record[sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID].value
        self.assertEqual(protocols[0].value[0].value, BT_L2CAP_PROTOCOL_ID)
        self.assertEqual(protocols[0].value[1].value, AVDTP_L2CAP_PSM)
        self.assertEqual(protocols[1].value[0].value, BT_AVDTP_PROTOCOL_ID)
        self.assertEqual(protocols[1].value[1].value, AVDTP_VERSION)

    def test_avrcp_target_record_has_minimal_profile_descriptor(self) -> None:
        record = attributes(self.records[AVRCP_TARGET_HANDLE])
        self.assertEqual(
            record[sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID].value[0].value,
            BT_AV_REMOTE_CONTROL_TARGET_SERVICE,
        )
        profile = record[
            sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID
        ].value[0].value
        self.assertEqual(profile[0].value, BT_AV_REMOTE_CONTROL_SERVICE)
        self.assertEqual(profile[1].value, AVRCP_VERSION)

    def test_building_records_does_not_register_extra_psm_servers(self) -> None:
        with patch.object(sdp.Server, "register") as register:
            build_sdp_compatibility_records(self.identity)
        register.assert_not_called()


