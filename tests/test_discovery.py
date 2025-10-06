"""Tests for safe address handling and AirPods candidate discovery."""

from __future__ import annotations

import unittest


from airpods_hr.address import BluetoothAddress, InvalidBluetoothAddressError


def synthetic_address(start: int) -> str:
    return ":".join(f"{start + offset:02x}" for offset in range(6))


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


