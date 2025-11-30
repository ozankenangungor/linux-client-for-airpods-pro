"""Hardware-independent tests for the BlueZ coexistence probe."""

from __future__ import annotations


import unittest


from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree


from airpods_hr.address import BluetoothAddress
from airpods_hr.bluez_coexistence import BlueZCompatibilityRegistration, BlueZCoexistenceState, CoexistenceCategory, CoexistenceFailure, DBusNextBlueZCoexistenceClient


from airpods_hr.discovery import AirPodsCandidate


from airpods_hr.sdp import (
    USBAdapterIdentity,
    build_bluez_sdp_service_records,
)


ALL_COMPATIBILITY_UUIDS = frozenset(
    record.uuid for record in build_bluez_sdp_service_records(
        USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)
    )
)


LOCAL_ADAPTER_ADDRESS = "00:11:22:33:44:55"
REMOTE_AIRPODS_ADDRESS = "AA:BB:CC:DD:EE:FF"


def candidate(
    *, adapter_modalias: str | None = "usb:v1234p5678d9ABC"
) -> AirPodsCandidate:
    return AirPodsCandidate(
        display_name="Test AirPods",
        adapter_name="hci0",
        adapter_path="/org/bluez/hci0",
        adapter_address=BluetoothAddress.parse(LOCAL_ADAPTER_ADDRESS),
        address=BluetoothAddress.parse(REMOTE_AIRPODS_ADDRESS),
        object_path="/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        adapter_modalias=adapter_modalias,
    )


def state(*, connected: bool = True, powered: bool = True):
    return BlueZCoexistenceState(
        candidate(), powered, connected, ALL_COMPATIBILITY_UUIDS
    )


class BlueZStateTests(unittest.IsolatedAsyncioTestCase):
    def managed_objects(self, *, connected: bool, powered: bool = True):
        selected = candidate()
        return {
            selected.adapter_path: {
                "org.bluez.Adapter1": {
                    "Address": SimpleNamespace(value="00:11:22:33:44:55"),
                    "Powered": SimpleNamespace(value=powered),
                    "Modalias": SimpleNamespace(value="usb:v1234p5678d9ABC"),
                    "UUIDs": SimpleNamespace(value=list(ALL_COMPATIBILITY_UUIDS)),
                }
            },
            selected.object_path: {
                "org.bluez.Device1": {
                    "Address": SimpleNamespace(value="AA:BB:CC:DD:EE:FF"),
                    "Adapter": SimpleNamespace(value=selected.adapter_path),
                    "Name": SimpleNamespace(value="AirPods Pro"),
                    "Alias": SimpleNamespace(value="Test AirPods"),
                    "Paired": SimpleNamespace(value=True),
                    "Connected": SimpleNamespace(value=connected),
                }
            },
        }

    async def test_connected_powered_preflight_is_accepted(self) -> None:
        client = DBusNextBlueZCoexistenceClient()
        client.get_managed_objects = AsyncMock(
            return_value=self.managed_objects(connected=True)
        )
        result = await client.preflight()
        self.assertTrue(result.device_connected)
        self.assertTrue(result.adapter_powered)
        self.assertEqual(result.adapter_uuids, ALL_COMPATIBILITY_UUIDS)

    async def test_disconnected_preflight_is_rejected(self) -> None:
        client = DBusNextBlueZCoexistenceClient()
        client.get_managed_objects = AsyncMock(
            return_value=self.managed_objects(connected=False)
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await client.preflight()
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.AIRPODS_NOT_CONNECTED,
        )

    async def test_fresh_acl_preflight_accepts_paired_disconnected_device(
        self,
    ) -> None:
        client = DBusNextBlueZCoexistenceClient()
        client.get_managed_objects = AsyncMock(
            return_value=self.managed_objects(connected=False)
        )
        result = await client.preflight(require_connected=False)
        self.assertFalse(result.device_connected)
        self.assertTrue(result.adapter_powered)

    async def test_unpowered_preflight_is_rejected(self) -> None:
        client = DBusNextBlueZCoexistenceClient()
        client.get_managed_objects = AsyncMock(
            return_value=self.managed_objects(connected=True, powered=False)
        )
        with self.assertRaises(CoexistenceFailure) as raised:
            await client.preflight()
        self.assertEqual(
            raised.exception.category, CoexistenceCategory.PREFLIGHT_FAILED
        )


class ProfileLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_records_register_and_unregister_once_each(self) -> None:
        profile_client = SimpleNamespace(
            register_profile=AsyncMock(), unregister_profile=AsyncMock()
        )
        registration = BlueZCompatibilityRegistration(profile_client)
        missing_state = BlueZCoexistenceState(candidate(), True, True, frozenset())
        await registration.register(missing_state)
        self.assertEqual(profile_client.register_profile.await_count, 4)
        self.assertEqual(registration.registered_count, 4)
        first_profile = profile_client.register_profile.await_args_list[0].args[1]
        first_profile.Release()
        first_profile.Release()
        self.assertTrue(first_profile.released)
        await registration.unregister()
        self.assertEqual(profile_client.unregister_profile.await_count, 4)
        await registration.unregister()
        self.assertEqual(profile_client.unregister_profile.await_count, 4)

    async def test_existing_adapter_identity_needs_no_duplicate_profile(self) -> None:
        profile_client = SimpleNamespace(
            register_profile=AsyncMock(), unregister_profile=AsyncMock()
        )
        registration = BlueZCompatibilityRegistration(profile_client)
        await registration.register(state())
        profile_client.register_profile.assert_not_awaited()
        await registration.unregister()
        profile_client.unregister_profile.assert_not_awaited()

    async def test_complete_uuid_set_does_not_require_adapter_modalias(self) -> None:
        profile_client = SimpleNamespace(
            register_profile=AsyncMock(), unregister_profile=AsyncMock()
        )
        registration = BlueZCompatibilityRegistration(profile_client)
        no_identity_state = BlueZCoexistenceState(
            candidate(adapter_modalias=None),
            True,
            True,
            ALL_COMPATIBILITY_UUIDS,
        )
        await registration.register(no_identity_state)
        self.assertEqual(registration.registered_count, 0)
        profile_client.register_profile.assert_not_awaited()
        await registration.unregister()
        profile_client.unregister_profile.assert_not_awaited()

    async def test_partial_registration_failure_cleans_prior_profile(self) -> None:
        profile_client = SimpleNamespace(
            register_profile=AsyncMock(
                side_effect=[None, PermissionError(13, "private path")]
            ),
            unregister_profile=AsyncMock(),
        )
        registration = BlueZCompatibilityRegistration(profile_client)
        missing_state = BlueZCoexistenceState(candidate(), True, True, frozenset())
        with self.assertRaises(CoexistenceFailure) as raised:
            await registration.register(missing_state)
        self.assertEqual(
            raised.exception.category,
            CoexistenceCategory.PROFILE_REGISTRATION_FAILED,
        )
        profile_client.unregister_profile.assert_awaited_once()

    def test_bluez_xml_records_are_well_formed_and_canonical(self) -> None:
        records = build_bluez_sdp_service_records(
            USBAdapterIdentity(0x1234, 0x5678, 0x9ABC)
        )
        self.assertEqual(len(records), 4)
        roots = [ElementTree.fromstring(record.service_record) for record in records]
        self.assertTrue(all(root.tag == "record" for root in roots))
        joined = "".join(record.service_record for record in records)
        values = (
            "0x1234",
            "0x5678",
            "0x9abc",
            "0x0d",
            "0x0019",
            "0x0103",
            "0x0106",
        )
        for value in values:
            self.assertIn(value, joined)


