"""Read-only SDP audit tests for the BlueZ coexistence probe."""

from __future__ import annotations

import unittest
from types import MappingProxyType
from unittest.mock import AsyncMock, Mock, patch
from airpods_hr.address import BluetoothAddress
from airpods_hr.bluez_coexistence import BlueZCoexistenceState
from airpods_hr.bluez_sdp_audit import LocalSDPInspection, ObservedLocalSDPRecord, ObservedSDPAttribute, SDPComparisonStatus, audit_bluez_sdp_identity
from airpods_hr.discovery import AirPodsCandidate
from airpods_hr.sdp import AVDTP_L2CAP_PSM, AVDTP_VERSION, AVRCP_VERSION, HANDS_FREE_RFCOMM_CHANNEL, PNP_VENDOR_ID_SOURCE_USB, USBAdapterIdentity, build_bluez_sdp_service_records
from tools.probe_bluez_coexistence import run_live_sdp_audit


IDENTITY = USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)



RECORDS = build_bluez_sdp_service_records(IDENTITY)



UUID_BY_NAME = {record.name: record.uuid for record in RECORDS}



def audit_state(*, uuids: frozenset[str] | None = None) -> BlueZCoexistenceState:
    selected = AirPodsCandidate(
        display_name="Test AirPods",
        adapter_name="hci0",
        adapter_path="/org/bluez/hci0",
        adapter_address=BluetoothAddress.parse("00:11:22:33:44:55"),
        address=BluetoothAddress.parse("AA:BB:CC:DD:EE:FF"),
        object_path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        adapter_modalias="usb:v1234p5678d9ABC",
    )
    return BlueZCoexistenceState(
        selected,
        adapter_powered=True,
        device_connected=True,
        adapter_uuids=(
            uuids
            if uuids is not None
            else frozenset(record.uuid for record in RECORDS)
        ),
    )



def observed(value: int) -> ObservedSDPAttribute:
    return ObservedSDPAttribute(observable=True, value=value)



def matching_inspection() -> LocalSDPInspection:
    return LocalSDPInspection(
        records_by_uuid=MappingProxyType(
            {
                UUID_BY_NAME["PnPInformation"]: ObservedLocalSDPRecord(
                    MappingProxyType(
                        {
                            "pnp_vendor_id": observed(IDENTITY.vendor_id),
                            "pnp_product_id": observed(IDENTITY.product_id),
                            "pnp_version": observed(IDENTITY.version),
                            "pnp_vendor_id_source": observed(
                                PNP_VENDOR_ID_SOURCE_USB
                            ),
                        }
                    )
                ),
                UUID_BY_NAME[
                    "HandsfreeAudioGateway"
                ]: ObservedLocalSDPRecord(
                    MappingProxyType(
                        {"rfcomm_channel": observed(HANDS_FREE_RFCOMM_CHANNEL)}
                    )
                ),
                UUID_BY_NAME["AudioSource"]: ObservedLocalSDPRecord(
                    MappingProxyType(
                        {
                            "l2cap_psm": observed(AVDTP_L2CAP_PSM),
                            "avdtp_version": observed(AVDTP_VERSION),
                        }
                    )
                ),
                UUID_BY_NAME[
                    "A/V RemoteControlTarget"
                ]: ObservedLocalSDPRecord(
                    MappingProxyType(
                        {"avrcp_profile_version": observed(AVRCP_VERSION)}
                    )
                ),
            }
        )
    )



class BlueZSDPAuditComparisonTests(unittest.TestCase):
    def test_uuid_only_evidence_keeps_every_attribute_unknown(self) -> None:
        result = audit_bluez_sdp_identity(audit_state())
        self.assertTrue(all(record.service_class_uuid_present for record in result.records))
        self.assertTrue(
            all(
                attribute.status is SDPComparisonStatus.NOT_OBSERVABLE
                for record in result.records
                for attribute in record.attributes
            )
        )
        self.assertFalse(result.detailed_attribute_inspection_available)
        self.assertIs(
            result.full_record_equivalence,
            SDPComparisonStatus.NOT_OBSERVABLE,
        )

    def test_structured_observable_attributes_are_compared_exactly(self) -> None:
        result = audit_bluez_sdp_identity(audit_state(), matching_inspection())
        self.assertTrue(result.detailed_attribute_inspection_available)
        self.assertTrue(
            all(
                attribute.status is SDPComparisonStatus.MATCH
                for record in result.records
                for attribute in record.attributes
            )
        )
        self.assertIs(result.full_record_equivalence, SDPComparisonStatus.MATCH)

    def test_observable_mismatch_is_not_reported_as_equivalent(self) -> None:
        inspection = matching_inspection()
        records = dict(inspection.records_by_uuid)
        records[UUID_BY_NAME["HandsfreeAudioGateway"]] = (
            ObservedLocalSDPRecord(
                MappingProxyType({"rfcomm_channel": observed(12)})
            )
        )
        result = audit_bluez_sdp_identity(
            audit_state(), LocalSDPInspection(MappingProxyType(records))
        )
        hands_free = next(
            record
            for record in result.records
            if record.name == "HandsfreeAudioGateway"
        )
        self.assertIs(
            hands_free.attributes[0].status, SDPComparisonStatus.MISMATCH
        )
        self.assertIs(
            result.full_record_equivalence, SDPComparisonStatus.MISMATCH
        )

    def test_missing_modalias_degrades_pnp_comparison_to_unknown(self) -> None:
        selected_state = audit_state()
        candidate = selected_state.candidate
        without_modalias = AirPodsCandidate(
            display_name=candidate.display_name,
            adapter_name=candidate.adapter_name,
            adapter_path=candidate.adapter_path,
            adapter_address=candidate.adapter_address,
            address=candidate.address,
            object_path=candidate.object_path,
            adapter_modalias=None,
        )
        result = audit_bluez_sdp_identity(
            BlueZCoexistenceState(
                without_modalias, True, True, selected_state.adapter_uuids
            ),
            matching_inspection(),
        )
        pnp = next(record for record in result.records if record.name == "PnPInformation")
        self.assertTrue(
            all(
                attribute.status is SDPComparisonStatus.NOT_OBSERVABLE
                for attribute in pnp.attributes[:3]
            )
        )



class BlueZSDPAuditProbeTests(unittest.IsolatedAsyncioTestCase):


    async def test_live_audit_uses_only_read_only_bluez_state_calls(self) -> None:
        client = Mock()
        client.connect = AsyncMock()
        client.preflight = AsyncMock(return_value=audit_state())
        client.close = Mock()
        with patch(
            "tools.probe_bluez_coexistence.DBusNextBlueZCoexistenceClient",
            return_value=client,
        ), patch(
            "tools.probe_bluez_coexistence.BlueZCompatibilityRegistration"
        ) as registration, patch(
            "tools.probe_bluez_coexistence.KernelL2CAPTransport"
        ) as transport:
            result = await run_live_sdp_audit(lambda message: None, 5)
        self.assertFalse(result.detailed_attribute_inspection_available)
        client.connect.assert_awaited_once()
        client.preflight.assert_awaited_once()
        client.close.assert_called_once()
        registration.assert_not_called()
        transport.assert_not_called()

