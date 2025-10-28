"""Replay tests for Bumble's real SDP server and safe diagnostics."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from bumble import l2cap, sdp
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
from airpods_hr.sdp_diagnostics import (
    BumbleSDPDiagnostics,
    ProtocolTimelineKind,
    SafeProtocolTimeline,
    SDPDiagnosticsError,
    validate_bumble_sdp_diagnostics_api,
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


class SDPDiagnosticsTests(unittest.TestCase):
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
        self.records = build_sdp_compatibility_records(
            USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)
        )

    def replay(self, request: bytes) -> None:
        channel = ReplayChannel()
        self.device.l2cap_channel_manager.servers[sdp.SDP_PSM].on_connection(
            channel
        )
        channel.sink(request)

    def test_unreviewed_bumble_version_is_rejected(self) -> None:
        with self.assertRaises(SDPDiagnosticsError):
            validate_bumble_sdp_diagnostics_api(installed_version="0.0.235")

    def test_diagnostics_classify_four_services_and_successful_matches(self) -> None:
        observer = BumbleSDPDiagnostics()
        queries = (
            (BT_PNP_INFORMATION_SERVICE, PNP_VENDOR_ID_ATTRIBUTE_ID),
            (
                BT_HANDSFREE_AUDIO_GATEWAY_SERVICE,
                sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            ),
            (
                BT_AUDIO_SOURCE_SERVICE,
                sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            ),
            (
                BT_AV_REMOTE_CONTROL_TARGET_SERVICE,
                sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
            ),
        )
        with self.runtime.temporary_sdp_records(self.records):
            with self.runtime.observe_sdp(observer):
                for service_uuid, attribute_id in queries:
                    self.replay(
                        search_attribute_request(service_uuid, (attribute_id,))
                    )

        snapshot = observer.snapshot()
        self.assertTrue(snapshot.sdp_connection_observed)
        self.assertEqual(snapshot.sdp_requests_observed, 4)
        self.assertEqual(snapshot.pnp_information_queries, 1)
        self.assertEqual(snapshot.handsfree_audio_gateway_queries, 1)
        self.assertEqual(snapshot.audio_source_queries, 1)
        self.assertEqual(snapshot.avrcp_target_queries, 1)
        self.assertEqual(snapshot.other_queries, 0)
        self.assertTrue(snapshot.pnp_information_match_served)
        self.assertTrue(snapshot.handsfree_audio_gateway_match_served)
        self.assertTrue(snapshot.audio_source_match_served)
        self.assertTrue(snapshot.avrcp_target_match_served)

    def test_unknown_query_changes_only_generic_counter(self) -> None:
        observer = BumbleSDPDiagnostics()
        request = search_attribute_request(
            UUID.from_16_bits(0xF00D),
            (sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,),
        )
        with self.runtime.temporary_sdp_records(self.records):
            with self.runtime.observe_sdp(observer):
                self.replay(request)

        snapshot = observer.snapshot()
        self.assertEqual(snapshot.sdp_requests_observed, 1)
        self.assertEqual(snapshot.other_queries, 1)
        self.assertEqual(snapshot.pnp_information_queries, 0)
        self.assertFalse(snapshot.pnp_information_match_served)
        self.assertNotIn(request.hex(), repr(snapshot))

    def test_known_query_without_returned_attribute_is_not_marked_served(self) -> None:
        observer = BumbleSDPDiagnostics()
        request = search_attribute_request(
            BT_PNP_INFORMATION_SERVICE,
            (0x7777,),
        )
        with self.runtime.temporary_sdp_records(self.records):
            with self.runtime.observe_sdp(observer):
                self.replay(request)

        snapshot = observer.snapshot()
        self.assertEqual(snapshot.pnp_information_queries, 1)
        self.assertFalse(snapshot.pnp_information_match_served)

    def test_peer_psm_diagnostics_are_counters_only(self) -> None:
        clock_values = iter((10.0, 10.1, 10.2, 10.3, 10.4, 10.5))
        timeline = SafeProtocolTimeline(clock=lambda: next(clock_values))
        observer = BumbleSDPDiagnostics(timeline=timeline)
        manager = self.device.l2cap_channel_manager
        delegated: list[int] = []

        def previous_handler(connection, cid, request):
            del connection, cid
            delegated.append(request.psm)

        manager.on_l2cap_connection_request = previous_handler
        try:
            with self.runtime.observe_sdp(observer):
                for psm in (1, 3, 23, 25, 0x1001):
                    manager.on_control_frame(
                        SimpleNamespace(handle=1, peer_address="synthetic"),
                        l2cap.L2CAP_SIGNALING_CID,
                        l2cap.L2CAP_Connection_Request(
                            identifier=1,
                            psm=psm,
                            source_cid=0x0040,
                        ),
                    )
            self.assertIs(
                manager.on_l2cap_connection_request,
                previous_handler,
            )
        finally:
            del manager.on_l2cap_connection_request

        snapshot = observer.snapshot()
        self.assertEqual(delegated, [1, 3, 23, 25, 0x1001])
        self.assertEqual(snapshot.psm_1_requests, 1)
        self.assertEqual(snapshot.psm_3_requests, 1)
        self.assertEqual(snapshot.psm_23_requests, 1)
        self.assertEqual(snapshot.psm_25_requests, 1)
        self.assertEqual(snapshot.other_psm_requests, 1)
        self.assertEqual(
            [event.kind for event in snapshot.timeline.events],
            [
                ProtocolTimelineKind.PSM_1_REQUEST,
                ProtocolTimelineKind.PSM_3_REQUEST,
                ProtocolTimelineKind.PSM_23_REQUEST,
                ProtocolTimelineKind.PSM_25_REQUEST,
                ProtocolTimelineKind.OTHER_PSM_REQUEST,
            ],
        )
        self.assertEqual(snapshot.timeline.total_events, 5)

    def test_protocol_timeline_is_bounded(self) -> None:
        now = 0.0

        def clock() -> float:
            nonlocal now
            value = now
            now += 0.25
            return value

        timeline = SafeProtocolTimeline(limit=2, clock=clock)
        timeline.record(ProtocolTimelineKind.HANDSHAKE_SENT)
        timeline.record(ProtocolTimelineKind.ACK_OBSERVED)
        timeline.record(ProtocolTimelineKind.PSM_3_REQUEST)

        snapshot = timeline.snapshot()
        self.assertEqual(len(snapshot.events), 2)
        self.assertEqual(snapshot.total_events, 3)
        self.assertEqual(snapshot.events[0].elapsed_seconds, 0.25)
        self.assertIsNone(snapshot.first(ProtocolTimelineKind.PSM_3_REQUEST))

    def test_hooks_restore_exact_previous_state_on_exception(self) -> None:
        observer = BumbleSDPDiagnostics()
        manager = self.device.l2cap_channel_manager
        server = self.device.sdp_server

        def prior_manager_handler(connection, cid, request):
            del connection, cid, request

        def prior_server_handler(pdu):
            del pdu

        manager.on_l2cap_connection_request = prior_manager_handler
        server.on_pdu = prior_server_handler
        try:
            with self.assertRaisesRegex(RuntimeError, "synthetic"):
                with self.runtime.observe_sdp(observer):
                    raise RuntimeError("synthetic")
            self.assertIs(
                manager.on_l2cap_connection_request,
                prior_manager_handler,
            )
            self.assertIs(server.on_pdu, prior_server_handler)
        finally:
            del manager.on_l2cap_connection_request
            del server.on_pdu

    def test_hooks_restore_after_cancellation(self) -> None:
        observer = BumbleSDPDiagnostics()
        manager = self.device.l2cap_channel_manager
        server = self.device.sdp_server

        with self.assertRaises(asyncio.CancelledError):
            with self.runtime.observe_sdp(observer):
                raise asyncio.CancelledError

        self.assertNotIn("on_l2cap_connection_request", manager.__dict__)
        self.assertNotIn("on_pdu", server.__dict__)


if __name__ == "__main__":
    unittest.main()
