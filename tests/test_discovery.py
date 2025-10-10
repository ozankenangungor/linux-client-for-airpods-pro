"""Tests for safe address handling and AirPods candidate discovery."""

from __future__ import annotations

import unittest
from airpods_hr.address import BluetoothAddress, InvalidBluetoothAddressError
from airpods_hr.discovery import BlueZDeviceDiscovery, MultipleAirPodsCandidatesError, NoAirPodsCandidatesError, select_single_candidate


def synthetic_address(start: int) -> str:
    return ":".join(f"{start + offset:02x}" for offset in range(6))



def device_object(
    address: str,
    *,
    name: str = "AirPods Pro",
    alias: str | None = None,
    paired: bool = True,
    adapter: str = "/org/bluez/hci0",
) -> dict[str, dict[str, object]]:
    properties: dict[str, object] = {
        "Address": address,
        "Name": name,
        "Paired": paired,
        "Adapter": adapter,
    }
    if alias is not None:
        properties["Alias"] = alias
    return {"org.bluez.Device1": properties}



def managed_objects(
    devices: dict[str, dict[str, dict[str, object]]],
) -> dict[str, dict[str, dict[str, object]]]:
    return {
        "/org/bluez/hci0": {
            "org.bluez.Adapter1": {
                "Address": synthetic_address(0),
                "Modalias": "usb:v1234p5678d9ABC",
            }
        },
        **devices,
    }



class FakeManagedObjectsBackend:
    def __init__(self, objects: dict[str, dict[str, dict[str, object]]]) -> None:
        self.objects = objects

    async def get_managed_objects(self):
        return self.objects



class BluetoothAddressTests(unittest.TestCase):
    def test_valid_address_is_normalized(self) -> None:
        raw = synthetic_address(10)

        address = BluetoothAddress.parse(raw)

        self.assertEqual(str(address), raw.upper())
        self.assertEqual(address.path_component, raw.upper())

    def test_malformed_addresses_are_rejected(self) -> None:
        valid_shape = synthetic_address(0)
        malformed = (
            "",
            "not-an-address",
            valid_shape.rsplit(":", 1)[0],
            f"{valid_shape[:-2]}GG",
            valid_shape.replace(":", ""),
        )

        for value in malformed:
            with self.subTest(value=value):
                with self.assertRaises(InvalidBluetoothAddressError):
                    BluetoothAddress.parse(value)

    def test_path_traversal_attempts_are_rejected(self) -> None:
        valid_shape = synthetic_address(0)
        for value in (
            f"../{valid_shape}",
            "..",
            valid_shape.replace(":", "\\", 1),
        ):
            with self.subTest(value=value):
                with self.assertRaises(InvalidBluetoothAddressError):
                    BluetoothAddress.parse(value)



class BlueZDeviceDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_paired_airpods_candidates(self) -> None:
        backend = FakeManagedObjectsBackend(
            managed_objects({
                "/org/bluez/hci0/dev_other": device_object(
                    synthetic_address(20), name="Keyboard"
                )
            })
        )

        candidates = await BlueZDeviceDiscovery(backend).discover_candidates()

        self.assertEqual(candidates, ())
        with self.assertRaises(NoAirPodsCandidatesError):
            select_single_candidate(candidates)

    async def test_exactly_one_candidate_is_selected(self) -> None:
        backend = FakeManagedObjectsBackend(
            managed_objects({
                "/org/bluez/hci0/dev_candidate": device_object(
                    synthetic_address(30),
                    name="AirPods Pro",
                    alias="My AirPods Pro",
                )
            })
        )

        candidates = await BlueZDeviceDiscovery(backend).discover_candidates()
        selected = select_single_candidate(candidates)

        self.assertEqual(len(candidates), 1)
        self.assertIs(selected, candidates[0])
        self.assertEqual(selected.display_name, "My AirPods Pro")
        self.assertEqual(selected.adapter_name, "hci0")
        self.assertEqual(
            selected.adapter_address,
            BluetoothAddress.parse(synthetic_address(0)),
        )
        self.assertEqual(selected.adapter_modalias, "usb:v1234p5678d9ABC")
        self.assertNotIn(str(selected.address), repr(selected))
        self.assertNotIn(str(selected.adapter_address), repr(selected))

    async def test_multiple_candidates_are_not_silently_selected(self) -> None:
        backend = FakeManagedObjectsBackend(
            managed_objects({
                "/org/bluez/hci0/dev_one": device_object(
                    synthetic_address(40), alias="First AirPods"
                ),
                "/org/bluez/hci0/dev_two": device_object(
                    synthetic_address(50), alias="Second AirPods"
                ),
            })
        )
        candidates = await BlueZDeviceDiscovery(backend).discover_candidates()

        with self.assertRaises(MultipleAirPodsCandidatesError) as caught:
            select_single_candidate(candidates)

        self.assertEqual(caught.exception.candidates, candidates)
        self.assertEqual(len(caught.exception.candidates), 2)

    async def test_non_paired_airpods_like_device_is_excluded(self) -> None:
        backend = FakeManagedObjectsBackend(
            managed_objects({
                "/org/bluez/hci0/dev_unpaired": device_object(
                    synthetic_address(60), paired=False
                )
            })
        )

        candidates = await BlueZDeviceDiscovery(backend).discover_candidates()

        self.assertEqual(candidates, ())

    async def test_name_rule_requires_airpods_as_a_distinct_token(self) -> None:
        backend = FakeManagedObjectsBackend(
            managed_objects({
                "/org/bluez/hci0/dev_similar": device_object(
                    synthetic_address(70), name="AirPodsClone"
                )
            })
        )

        candidates = await BlueZDeviceDiscovery(backend).discover_candidates()

        self.assertEqual(candidates, ())

