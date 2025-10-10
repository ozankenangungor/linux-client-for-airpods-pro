"""Tests for the in-memory Bumble Classic credential adapter."""

from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO

from bumble.core import PhysicalTransport
from bumble.hci import Address
from bumble.keys import PairingKeys

from airpods_hr.address import BluetoothAddress, InvalidBluetoothAddressError
from airpods_hr.bumble_keys import (
    BumbleCredentialSerializationError,
    InMemoryBumbleKeyStore,
    InvalidBumbleClassicPeerNameError,
)
from airpods_hr.pairing import BluetoothLinkKey, ClassicPairingCredentials


def synthetic_address(start: int) -> BluetoothAddress:
    value = ":".join(f"{start + offset:02X}" for offset in range(6))
    return BluetoothAddress.parse(value)


class InMemoryBumbleKeyStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.peer = synthetic_address(20)
        self.secret_bytes = bytes(reversed(range(16)))
        self.credentials = ClassicPairingCredentials(
            link_key=BluetoothLinkKey(self.secret_bytes),
            link_key_type=8,
            authenticated=True,
            pin_length=0,
        )
        self.store = InMemoryBumbleKeyStore(
            {self.peer: self.credentials}
        )
        self.bumble_peer = Address.from_string_for_transport(
            str(self.peer), PhysicalTransport.BR_EDR
        )

    async def test_real_bumble_public_peer_returns_classic_key(self) -> None:
        self.assertTrue(self.bumble_peer.is_public)
        self.assertEqual(str(self.bumble_peer), f"{self.peer}/P")

        keys = await self.store.get(str(self.bumble_peer))

        self.assertIsNotNone(keys)
        self.assertIsNotNone(keys.link_key)
        self.assertEqual(keys.link_key.value, self.secret_bytes)

    async def test_plain_project_address_is_rejected_as_ambiguous(self) -> None:
        with self.assertRaises(InvalidBumbleClassicPeerNameError):
            await self.store.get(str(self.peer))

    async def test_malformed_qualified_address_is_rejected(self) -> None:
        malformed = f"{str(self.peer).rsplit(':', 1)[0]}/P"

        with self.assertRaises(InvalidBumbleClassicPeerNameError) as caught:
            await self.store.get(malformed)

        self.assertNotIn(malformed, str(caught.exception))

    async def test_path_traversal_with_qualifier_is_rejected(self) -> None:
        traversal = f"../{self.peer}/P"

        with self.assertRaises(InvalidBumbleClassicPeerNameError) as caught:
            await self.store.get(traversal)

        self.assertNotIn(str(self.peer), repr(caught.exception))

    async def test_random_qualifier_is_rejected(self) -> None:
        with self.assertRaises(InvalidBumbleClassicPeerNameError):
            await self.store.get(f"{self.peer}/R")

    def test_project_address_remains_strictly_unqualified(self) -> None:
        with self.assertRaises(InvalidBluetoothAddressError):
            BluetoothAddress.parse(str(self.bumble_peer))

    async def test_unknown_peer_returns_no_key(self) -> None:
        unknown = synthetic_address(60)
        bumble_unknown = Address.from_string_for_transport(
            str(unknown), PhysicalTransport.BR_EDR
        )

        keys = await self.store.get(str(bumble_unknown))

        self.assertIsNone(keys)

    async def test_type_and_authentication_metadata_survive_conversion(self) -> None:
        keys = await self.store.get(str(self.bumble_peer))

        self.assertEqual(keys.link_key_type, self.credentials.link_key_type)
        self.assertEqual(
            keys.link_key.authenticated, self.credentials.authenticated
        )

    async def test_bumble_representation_is_redacted(self) -> None:
        keys = await self.store.get(str(self.bumble_peer))
        secret_hex = self.secret_bytes.hex()

        self.assertNotIn(secret_hex, repr(keys))
        self.assertNotIn(secret_hex, str(keys))
        self.assertEqual(repr(self.store), "<InMemoryBumbleKeyStore redacted>")

    async def test_serialization_and_printing_are_disabled(self) -> None:
        keys = await self.store.get(str(self.bumble_peer))
        output = StringIO()

        with self.assertRaises(BumbleCredentialSerializationError):
            keys.to_dict()
        with redirect_stdout(output):
            with self.assertRaises(BumbleCredentialSerializationError):
                await self.store.print()

        self.assertEqual(output.getvalue(), "")

    async def test_get_all_uses_bumble_public_names_that_round_trip(self) -> None:
        entries = await self.store.get_all()

        self.assertEqual(len(entries), 1)
        name, keys = entries[0]
        reparsed = Address.from_string_for_transport(
            name, PhysicalTransport.BR_EDR
        )
        self.assertEqual(name, str(self.bumble_peer))
        self.assertEqual(str(reparsed), name)
        self.assertEqual(keys.link_key.value, self.secret_bytes)

    async def test_replacement_key_notification_is_not_persisted_or_printed(
        self,
    ) -> None:
        notifications: list[str] = []
        store = InMemoryBumbleKeyStore(
            {self.peer: self.credentials},
            replacement_key_observer=lambda: notifications.append("reported"),
        )
        replacement = bytes(range(16))
        output = StringIO()

        with redirect_stdout(output):
            with self.assertRaises(RuntimeError) as caught:
                await store.update(
                    str(self.bumble_peer),
                    PairingKeys(link_key=PairingKeys.Key(replacement)),
                )

        retained = await store.get(str(self.bumble_peer))
        self.assertEqual(notifications, ["reported"])
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(replacement.hex(), str(caught.exception))
        self.assertEqual(retained.link_key.value, self.secret_bytes)


if __name__ == "__main__":
    unittest.main()
