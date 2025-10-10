"""In-memory conversion from local BlueZ credentials to Bumble keys."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from bumble.keys import KeyStore, PairingKeys

from airpods_hr.address import BluetoothAddress, InvalidBluetoothAddressError
from airpods_hr.pairing import ClassicPairingCredentials


class BumbleCredentialSerializationError(RuntimeError):
    """Raised when code tries to serialize or print imported credentials."""


class InvalidBumbleClassicPeerNameError(ValueError):
    """Raised when a Bumble keystore name is not an explicit public address."""


_BUMBLE_PUBLIC_ADDRESS_SUFFIX = "/P"


def parse_bumble_classic_peer_name(name: str) -> BluetoothAddress:
    """Convert Bumble's exact public-address form to a safe project address."""

    if not isinstance(name, str) or not name.endswith(
        _BUMBLE_PUBLIC_ADDRESS_SUFFIX
    ):
        raise InvalidBumbleClassicPeerNameError(
            "Classic Bumble peer name must use the public-address qualifier"
        )

    unqualified = name[: -len(_BUMBLE_PUBLIC_ADDRESS_SUFFIX)]
    try:
        return BluetoothAddress.parse(unqualified)
    except InvalidBluetoothAddressError:
        raise InvalidBumbleClassicPeerNameError(
            "Classic Bumble peer name contains an invalid address"
        ) from None


def format_bumble_classic_peer_name(address: BluetoothAddress) -> str:
    """Format one validated address for Bumble's Classic KeyStore API."""

    if not isinstance(address, BluetoothAddress):
        raise TypeError("address must be BluetoothAddress")
    return f"{address}{_BUMBLE_PUBLIC_ADDRESS_SUFFIX}"


class _RedactedBumbleKey(PairingKeys.Key):
    """Bumble-compatible key whose accidental representation is redacted."""

    def __str__(self) -> str:
        return "<BumblePairingKey redacted>"

    def __repr__(self) -> str:
        return "<BumblePairingKey redacted>"

    def to_dict(self) -> dict[str, object]:
        raise BumbleCredentialSerializationError(
            "serialization of imported Bluetooth credentials is disabled"
        )


def to_bumble_pairing_keys(
    credentials: ClassicPairingCredentials,
) -> PairingKeys:
    """Create Bumble's required Classic key representation in memory."""

    return PairingKeys(
        link_key=_RedactedBumbleKey(
            value=credentials.link_key._as_bytes_for_bumble(),
            authenticated=credentials.authenticated,
        ),
        link_key_type=credentials.link_key_type,
    )


class InMemoryBumbleKeyStore(KeyStore):
    """Read-only Bumble KeyStore backed by redacting local credentials."""

    def __init__(
        self,
        credentials: Mapping[BluetoothAddress, ClassicPairingCredentials],
        *,
        replacement_key_observer: Callable[[], None] | None = None,
    ) -> None:
        self._credentials = dict(credentials)
        self._replacement_key_observer = replacement_key_observer

    def __str__(self) -> str:
        return "<InMemoryBumbleKeyStore redacted>"

    def __repr__(self) -> str:
        return "<InMemoryBumbleKeyStore redacted>"

    async def get(self, name: str) -> PairingKeys | None:
        address = parse_bumble_classic_peer_name(name)
        credentials = self._credentials.get(address)
        if credentials is None:
            return None
        return to_bumble_pairing_keys(credentials)

    async def get_all(self) -> list[tuple[str, PairingKeys]]:
        return [
            (
                format_bumble_classic_peer_name(address),
                to_bumble_pairing_keys(credentials),
            )
            for address, credentials in self._credentials.items()
        ]

    async def update(self, name: str, keys: PairingKeys) -> None:
        del name, keys
        if self._replacement_key_observer is not None:
            self._replacement_key_observer()
        raise RuntimeError("the imported BlueZ credential store is read-only")

    async def delete(self, name: str) -> None:
        del name
        raise RuntimeError("the imported BlueZ credential store is read-only")

    async def delete_all(self) -> None:
        raise RuntimeError("the imported BlueZ credential store is read-only")

    async def print(self, prefix: str = "") -> None:
        del prefix
        raise BumbleCredentialSerializationError(
            "printing imported Bluetooth credentials is disabled"
        )
