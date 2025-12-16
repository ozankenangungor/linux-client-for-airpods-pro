"""Tests for the reference-probe-only SDP footprint experiment."""

from __future__ import annotations

import unittest
from dataclasses import fields
from bumble import sdp
from bumble.core import BT_L2CAP_PROTOCOL_ID, BT_OBEX_PROTOCOL_ID, BT_RFCOMM_PROTOCOL_ID, UUID
from bumble.device import Device, DeviceConfiguration
from airpods_hr.authentication import BumbleClassicRuntime
from airpods_hr.reference_sdp_footprint import ATT_L2CAP_PSM, BLUEZ_LIKE_EXTRA_SERVICE_SPECS, REFERENCE_SDP_QUERY_SUMMARY_LIMIT, BlueZLikeServiceSpec, NOKIA_OBEX_PC_SUITE_SERVICE, ReferenceSDPFootprint, ReferenceSDPQueryDiagnostics, augment_reference_sdp_records
from airpods_hr.sdp import USBAdapterIdentity, build_sdp_compatibility_records
from bumble.core import BT_L2CAP_PROTOCOL_ID, BT_OBEX_PROTOCOL_ID, BT_RFCOMM_PROTOCOL_ID
from airpods_hr.reference_sdp_footprint import ATT_L2CAP_PSM, BLUEZ_LIKE_EXTRA_SERVICE_SPECS, BlueZLikeServiceSpec, NOKIA_OBEX_PC_SUITE_SERVICE, ReferenceSDPFootprint, augment_reference_sdp_records


def _attributes(record: sdp.Server.Service) -> dict[int, sdp.DataElement]:
    return {attribute.id: attribute.value for attribute in record}



def _search_attribute_request(
    service_uuid: UUID,
    attribute: sdp.DataElement,
    *,
    transaction_id: int = 1,
    continuation_state: bytes = b"",
) -> bytes:
    return bytes(
        sdp.SDP_ServiceSearchAttributeRequest(
            transaction_id=transaction_id,
            service_search_pattern=sdp.DataElement.sequence(
                [sdp.DataElement.uuid(service_uuid)]
            ),
            maximum_attribute_byte_count=0xFFFF,
            attribute_id_list=sdp.DataElement.sequence([attribute]),
            continuation_state=continuation_state,
        )
    )



def _broad_l2cap_request(
    transaction_id: int = 1, continuation_state: bytes = b""
) -> bytes:
    return _search_attribute_request(
        BT_L2CAP_PROTOCOL_ID,
        sdp.DataElement.unsigned_integer_32(0x0000FFFF),
        transaction_id=transaction_id,
        continuation_state=continuation_state,
    )



def _ordinary_l2cap_request(transaction_id: int = 1) -> bytes:
    return _search_attribute_request(
        BT_L2CAP_PROTOCOL_ID,
        sdp.DataElement.unsigned_integer_16(
            sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID
        ),
        transaction_id=transaction_id,
    )



def _unrelated_request(transaction_id: int) -> bytes:
    return _search_attribute_request(
        UUID.from_16_bits(0xF000 + transaction_id),
        sdp.DataElement.unsigned_integer_16(
            sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID
        ),
        transaction_id=transaction_id,
    )



class ReplayChannel:
    def __init__(self, peer_mtu: int = 260) -> None:
        self.peer_mtu = peer_mtu
        self.sink = None
        self.responses: list[bytes] = []

    def write(self, response: object) -> None:
        self.responses.append(bytes(response))



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



class ReferenceSDPQueryDiagnosticsTests(unittest.TestCase):
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
        proven = build_sdp_compatibility_records(
            USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)
        )
        self.records = augment_reference_sdp_records(
            proven, ReferenceSDPFootprint.BLUEZ_LIKE
        )

    def test_broad_query_retains_only_safe_metadata(self) -> None:
        observer = ReferenceSDPQueryDiagnostics(
            ReferenceSDPFootprint.BLUEZ_LIKE
        )
        channel = ReplayChannel()
        with self.runtime.temporary_sdp_records(self.records):
            with self.runtime.observe_sdp(observer):
                self.device.l2cap_channel_manager.servers[
                    sdp.SDP_PSM
                ].on_connection(channel)
                assert channel.sink is not None
                request = _broad_l2cap_request()
                channel.sink(request)

        snapshot = observer.snapshot()
        self.assertEqual(snapshot.requests_observed, 1)
        summary = snapshot.l2cap_full_attribute_query()
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.search_uuids, (0x0100,))
        self.assertEqual(summary.attribute_ranges, ((0x0000, 0xFFFF),))
        self.assertEqual(summary.maximum_attribute_byte_count, 65535)
        self.assertTrue(summary.continuation_used)
        self.assertGreater(summary.matching_record_count, 2)
        self.assertGreater(summary.total_response_bytes, 251)
        self.assertNotIn(request.hex(), repr(snapshot).lower())

    def test_summary_list_is_bounded_and_hooks_restore(self) -> None:
        observer = ReferenceSDPQueryDiagnostics(
            ReferenceSDPFootprint.BLUEZ_LIKE
        )
        server = self.device.sdp_server
        channel = ReplayChannel(peer_mtu=2048)
        with self.runtime.temporary_sdp_records(self.records):
            with self.runtime.observe_sdp(observer):
                self.device.l2cap_channel_manager.servers[
                    sdp.SDP_PSM
                ].on_connection(channel)
                assert channel.sink is not None
                for transaction_id in range(REFERENCE_SDP_QUERY_SUMMARY_LIMIT + 4):
                    channel.sink(_unrelated_request(transaction_id + 1))
        self.assertEqual(
            observer.snapshot().requests_observed,
            REFERENCE_SDP_QUERY_SUMMARY_LIMIT + 4,
        )
        self.assertEqual(
            len(observer.snapshot().summaries),
            REFERENCE_SDP_QUERY_SUMMARY_LIMIT,
        )
        self.assertNotIn("on_pdu", server.__dict__)

    def test_target_survives_after_general_summary_limit(self) -> None:
        observer = ReferenceSDPQueryDiagnostics(
            ReferenceSDPFootprint.BLUEZ_LIKE
        )
        channel = ReplayChannel()
        with self.runtime.temporary_sdp_records(self.records):
            with self.runtime.observe_sdp(observer):
                self.device.l2cap_channel_manager.servers[
                    sdp.SDP_PSM
                ].on_connection(channel)
                assert channel.sink is not None
                for transaction_id in range(1, REFERENCE_SDP_QUERY_SUMMARY_LIMIT + 4):
                    channel.sink(_unrelated_request(transaction_id))
                channel.sink(_broad_l2cap_request(100))

        snapshot = observer.snapshot()
        self.assertEqual(
            len(snapshot.summaries), REFERENCE_SDP_QUERY_SUMMARY_LIMIT
        )
        target = snapshot.l2cap_full_attribute_query()
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.search_uuids, (0x0100,))
        self.assertEqual(target.attribute_ranges, ((0x0000, 0xFFFF),))

    def test_ordinary_l2cap_query_does_not_replace_full_range_target(self) -> None:
        observer = ReferenceSDPQueryDiagnostics(
            ReferenceSDPFootprint.BLUEZ_LIKE
        )
        channel = ReplayChannel()
        with self.runtime.temporary_sdp_records(self.records):
            with self.runtime.observe_sdp(observer):
                self.device.l2cap_channel_manager.servers[
                    sdp.SDP_PSM
                ].on_connection(channel)
                assert channel.sink is not None
                channel.sink(_ordinary_l2cap_request(1))
                channel.sink(_broad_l2cap_request(2))

        snapshot = observer.snapshot()
        self.assertEqual(snapshot.l2cap_query().attribute_ranges, ((0x0004, 0x0004),))
        target = snapshot.l2cap_full_attribute_query()
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target.attribute_ranges, ((0x0000, 0xFFFF),))

    def test_target_continuation_pages_use_one_dedicated_slot(self) -> None:
        observer = ReferenceSDPQueryDiagnostics(
            ReferenceSDPFootprint.BLUEZ_LIKE
        )
        channel = ReplayChannel()
        with self.runtime.temporary_sdp_records(self.records):
            with self.runtime.observe_sdp(observer):
                self.device.l2cap_channel_manager.servers[
                    sdp.SDP_PSM
                ].on_connection(channel)
                assert channel.sink is not None
                channel.sink(_broad_l2cap_request(1))
                response = sdp.SDP_PDU.from_bytes(channel.responses[-1])
                pages = 1
                while len(response.continuation_state) > 1:
                    channel.sink(
                        _broad_l2cap_request(
                            1, response.continuation_state
                        )
                    )
                    response = sdp.SDP_PDU.from_bytes(channel.responses[-1])
                    pages += 1

        snapshot = observer.snapshot()
        self.assertGreater(pages, 1)
        self.assertEqual(snapshot.requests_observed, pages)
        self.assertEqual(snapshot.summaries, ())
        self.assertTrue(
            snapshot.l2cap_full_attribute_query().continuation_used
        )

