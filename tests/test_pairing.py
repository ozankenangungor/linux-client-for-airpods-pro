"""Tests for BlueZ Classic LinkKey parsing and redaction."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from airpods_hr.address import BluetoothAddress
from airpods_hr.pairing import (
    BlueZPairingStore,
    BluetoothLinkKey,
    InvalidLinkKeyError,
    InvalidLinkKeyTypeError,
    LinkKeySectionMissingError,
    PairingInfoNotFoundError,
    PairingPermissionError,
)


def synthetic_address(start: int) -> BluetoothAddress:
    value = ":".join(f"{start + offset:02X}" for offset in range(6))
    return BluetoothAddress.parse(value)


class BlueZPairingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.adapter = synthetic_address(10)
        self.device = synthetic_address(30)
        self.store = BlueZPairingStore(self.root)
        self.secret = bytes(range(16))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_info(self, content: str) -> Path:
        path = self.store.info_path(self.adapter, self.device)
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
        return path

    def valid_info(self, *, key_type: str = "8") -> str:
        return (
            "[General]\nName=Test Device\n\n"
            "[LinkKey]\n"
            f"Key={self.secret.hex().upper()}\n"
            f"Type={key_type}\n"
            "PINLength=0\n"
        )

    def test_valid_link_key_info_parses(self) -> None:
        self.write_info(self.valid_info())

        credentials = self.store.load_classic_credentials(
            self.adapter, self.device
        )

        self.assertEqual(
            credentials.link_key._as_bytes_for_bumble(), self.secret
        )
        self.assertEqual(credentials.link_key_type, 8)
        self.assertTrue(credentials.authenticated)
        self.assertEqual(credentials.pin_length, 0)

    def test_missing_link_key_section(self) -> None:
        self.write_info("[General]\nName=Test Device\n")

        with self.assertRaises(LinkKeySectionMissingError):
            self.store.load_classic_credentials(self.adapter, self.device)

    def test_missing_info_file(self) -> None:
        with self.assertRaises(PairingInfoNotFoundError):
            self.store.load_classic_credentials(self.adapter, self.device)

    def test_invalid_key_hex(self) -> None:
        self.write_info("[LinkKey]\nKey=not-hex\nType=8\nPINLength=0\n")

        with self.assertRaises(InvalidLinkKeyError):
            self.store.load_classic_credentials(self.adapter, self.device)

    def test_wrong_key_length(self) -> None:
        short_key = bytes(range(15)).hex()
        self.write_info(
            f"[LinkKey]\nKey={short_key}\nType=8\nPINLength=0\n"
        )

        with self.assertRaises(InvalidLinkKeyError):
            self.store.load_classic_credentials(self.adapter, self.device)

    def test_invalid_link_key_type(self) -> None:
        self.write_info(self.valid_info(key_type="not-an-integer"))

        with self.assertRaises(InvalidLinkKeyTypeError):
            self.store.load_classic_credentials(self.adapter, self.device)

    def test_permission_error_is_wrapped_without_path(self) -> None:
        hidden_path = "private-pairing-location"
        with patch.object(
            Path,
            "open",
            side_effect=PermissionError(hidden_path),
        ):
            with self.assertRaises(PairingPermissionError) as caught:
                self.store.load_classic_credentials(self.adapter, self.device)

        self.assertNotIn(hidden_path, str(caught.exception))

    def test_secret_str_and_repr_are_redacted(self) -> None:
        secret = BluetoothLinkKey(self.secret)
        secret_hex = self.secret.hex()

        self.assertEqual(str(secret), "<BluetoothLinkKey redacted>")
        self.assertEqual(repr(secret), "<BluetoothLinkKey redacted>")
        self.assertNotIn(secret_hex, str(secret))
        self.assertNotIn(secret_hex, repr(secret))

    def test_exception_does_not_include_invalid_secret_value(self) -> None:
        synthetic_secret = "SYNTHETIC_SECRET_MUST_NOT_BE_REPORTED"
        self.write_info(
            f"[LinkKey]\nKey={synthetic_secret}\nType=8\nPINLength=0\n"
        )

        with self.assertRaises(InvalidLinkKeyError) as caught:
            self.store.load_classic_credentials(self.adapter, self.device)

        self.assertNotIn(synthetic_secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
