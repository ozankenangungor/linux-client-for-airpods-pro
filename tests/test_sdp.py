"""Tests for the four-record temporary SDP compatibility profile."""

from __future__ import annotations


import unittest


from airpods_hr.sdp import AdapterIdentityError, USBAdapterIdentity


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


