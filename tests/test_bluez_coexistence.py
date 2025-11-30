"""Hardware-independent tests for the BlueZ coexistence probe."""

from __future__ import annotations


import unittest


from types import SimpleNamespace
from unittest.mock import AsyncMock


from airpods_hr.address import BluetoothAddress
from airpods_hr.bluez_coexistence import CoexistenceCategory, CoexistenceFailure, DBusNextBlueZCoexistenceClient


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


