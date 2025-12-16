"""Tests for the reference-probe-only SDP footprint experiment."""

from __future__ import annotations

import unittest
from dataclasses import fields


from bumble import sdp
from bumble.core import BT_L2CAP_PROTOCOL_ID, BT_OBEX_PROTOCOL_ID, BT_RFCOMM_PROTOCOL_ID


from bumble.device import Device, DeviceConfiguration


from airpods_hr.reference_sdp_footprint import ATT_L2CAP_PSM, BLUEZ_LIKE_EXTRA_SERVICE_SPECS, BlueZLikeServiceSpec, NOKIA_OBEX_PC_SUITE_SERVICE, ReferenceSDPFootprint, augment_reference_sdp_records


from airpods_hr.sdp import USBAdapterIdentity, build_sdp_compatibility_records


def _attributes(record: sdp.Server.Service) -> dict[int, sdp.DataElement]:
    return {attribute.id: attribute.value for attribute in record}


class ReferenceSDPFootprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proven = build_sdp_compatibility_records(
            USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)
        )

    def test_proven_footprint_is_the_exact_existing_four_records(self) -> None:
        selected = augment_reference_sdp_records(
            self.proven, ReferenceSDPFootprint.PROVEN
        )
        self.assertIs(selected, self.proven)
        self.assertEqual(len(selected), 4)

    def test_bluez_like_contains_proven_records_unchanged(self) -> None:
        selected = augment_reference_sdp_records(
            self.proven, ReferenceSDPFootprint.BLUEZ_LIKE
        )
        self.assertEqual(len(selected), 4 + len(BLUEZ_LIKE_EXTRA_SERVICE_SPECS))
        for handle, record in self.proven.items():
            self.assertIs(selected[handle], record)

    def test_extra_records_are_deterministic_and_l2cap_searchable(self) -> None:
        first = augment_reference_sdp_records(
            self.proven, ReferenceSDPFootprint.BLUEZ_LIKE
        )
        second = augment_reference_sdp_records(
            self.proven, ReferenceSDPFootprint.BLUEZ_LIKE
        )
        first_extra = [
            record for key, record in first.items() if key not in self.proven
        ]
        second_extra = [
            record for key, record in second.items() if key not in self.proven
        ]
        self.assertEqual(
            [
                [(attribute.id, bytes(attribute.value)) for attribute in record]
                for record in first_extra
            ],
            [
                [(attribute.id, bytes(attribute.value)) for attribute in record]
                for record in second_extra
            ],
        )
        for record in first_extra:
            protocols = _attributes(record)[
                sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID
            ].value
            self.assertEqual(protocols[0].value[0].value, BT_L2CAP_PROTOCOL_ID)

    def test_gatt_extras_use_observed_att_psm_without_invented_ranges(self) -> None:
        selected = augment_reference_sdp_records(
            self.proven, ReferenceSDPFootprint.BLUEZ_LIKE
        )
        extras = [record for key, record in selected.items() if key not in self.proven]
        for spec, record in zip(BLUEZ_LIKE_EXTRA_SERVICE_SPECS, extras):
            if spec.protocol != "att":
                continue
            attributes = _attributes(record)
            protocols = attributes[
                sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID
            ].value
            self.assertEqual(protocols[0].value[1].value, ATT_L2CAP_PSM)
            self.assertNotIn(
                sdp.SDP_ADDITIONAL_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
                attributes,
            )

    def test_fixture_has_no_handle_or_machine_identity_semantics(self) -> None:
        self.assertNotIn(
            "handle", {field.name for field in fields(BlueZLikeServiceSpec)}
        )
        rendered = repr(BLUEZ_LIKE_EXTRA_SERVICE_SPECS).lower()
        for forbidden in ("address", "linkkey", "credential", "/var/lib/bluetooth"):
            self.assertNotIn(forbidden, rendered)

    def test_l2cap_search_distinguishes_proven_and_bluez_like(self) -> None:
        device = Device(
            config=DeviceConfiguration(classic_enabled=True, le_enabled=False)
        )
        pattern = sdp.DataElement.sequence(
            [sdp.DataElement.uuid(BT_L2CAP_PROTOCOL_ID)]
        )
        device.sdp_service_records = self.proven
        proven_matches = device.sdp_server.match_services(pattern)
        device.sdp_service_records = augment_reference_sdp_records(
            self.proven, ReferenceSDPFootprint.BLUEZ_LIKE
        )
        bluez_like_matches = device.sdp_server.match_services(pattern)
        self.assertEqual(len(proven_matches), 2)
        self.assertGreater(len(bluez_like_matches), len(proven_matches))


    def test_classic_extra_profile_values_match_observed_footprint(self) -> None:
        selected = augment_reference_sdp_records(
            self.proven, ReferenceSDPFootprint.BLUEZ_LIKE
        )
        extras = [record for key, record in selected.items() if key not in self.proven]
        by_service = {}
        for record in extras:
            attributes = _attributes(record)
            service = attributes[
                sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID
            ].value[0].value
            by_service[service.to_hex_str()] = attributes

        avrcp = by_service["110F"]
        avrcp_protocols = avrcp[
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID
        ].value
        self.assertEqual(avrcp_protocols[0].value[1].value, 0x0017)
        self.assertEqual(avrcp_protocols[1].value[1].value, 0x0104)
        self.assertEqual(
            avrcp[sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID]
            .value[0]
            .value[1]
            .value,
            0x0106,
        )

        audio_sink = by_service["110B"]
        audio_protocols = audio_sink[
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID
        ].value
        self.assertEqual(audio_protocols[0].value[1].value, 0x0019)
        self.assertEqual(audio_protocols[1].value[1].value, 0x0103)
        self.assertEqual(
            audio_sink[sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID]
            .value[0]
            .value[1]
            .value,
            0x0104,
        )

        handsfree = by_service["111E"]
        handsfree_protocols = handsfree[
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID
        ].value
        self.assertEqual(handsfree_protocols[1].value[1].value, 7)
        self.assertEqual(
            handsfree[sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID]
            .value[0]
            .value[1]
            .value,
            0x0109,
        )

    def test_audited_obex_records_have_exact_protocol_and_profile_values(
        self,
    ) -> None:
        selected = augment_reference_sdp_records(
            self.proven, ReferenceSDPFootprint.BLUEZ_LIKE
        )
        extras = [record for key, record in selected.items() if key not in self.proven]
        by_service = {}
        for record in extras:
            attributes = _attributes(record)
            service = attributes[
                sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID
            ].value[0].value
            by_service[service.to_hex_str()] = attributes

        nokia = NOKIA_OBEX_PC_SUITE_SERVICE.to_hex_str()
        expected = {
            "1133": (17, "1134", 0x0104),
            "1132": (16, "1134", 0x0100),
            "112F": (15, "1130", 0x0101),
            "1104": (14, "1104", 0x0100),
            "1106": (10, "1106", 0x0103),
            "1105": (9, "1105", 0x0102),
            nokia: (24, nokia, 0x0100),
        }
        self.assertEqual(
            {
                spec.service_uuid.to_hex_str()
                for spec in BLUEZ_LIKE_EXTRA_SERVICE_SPECS
                if spec.protocol == "obex"
            },
            set(expected),
        )
        self.assertNotIn(
            "112E",
            {spec.service_uuid.to_hex_str() for spec in BLUEZ_LIKE_EXTRA_SERVICE_SPECS},
        )
        for service_uuid, (channel, profile_uuid, version) in expected.items():
            attributes = by_service[service_uuid]
            protocols = attributes[
                sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID
            ].value
            self.assertEqual(protocols[0].value[0].value, BT_L2CAP_PROTOCOL_ID)
            self.assertEqual(protocols[1].value[0].value, BT_RFCOMM_PROTOCOL_ID)
            self.assertEqual(protocols[1].value[1].value, channel)
            self.assertEqual(protocols[2].value[0].value, BT_OBEX_PROTOCOL_ID)
            profile = attributes[
                sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID
            ].value[0].value
            self.assertEqual(profile[0].value.to_hex_str(), profile_uuid)
            self.assertEqual(profile[1].value, version)


