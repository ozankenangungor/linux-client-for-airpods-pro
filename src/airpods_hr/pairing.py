"""Parse Classic pairing credentials from a configurable BlueZ store."""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path

from airpods_hr.address import BluetoothAddress


class PairingStoreError(RuntimeError):
    """Base error for local BlueZ pairing storage."""


class PairingInfoNotFoundError(PairingStoreError):
    """Raised when the requested BlueZ info file does not exist."""


class PairingPermissionError(PairingStoreError):
    """Raised when BlueZ pairing information cannot be read."""


class PairingInfoFormatError(PairingStoreError):
    """Raised when the BlueZ info file cannot be parsed."""


class LinkKeySectionMissingError(PairingStoreError):
    """Raised when a paired device has no Classic LinkKey section."""


class InvalidLinkKeyError(PairingStoreError):
    """Raised when a Classic LinkKey is missing or malformed."""


class InvalidLinkKeyTypeError(PairingStoreError):
    """Raised when BlueZ LinkKey Type is missing or invalid."""


class InvalidPINLengthError(PairingStoreError):
    """Raised when an optional PINLength value is malformed."""


class BluetoothLinkKey:
    """A redacting holder for one 16-byte Classic Bluetooth LinkKey.

    The type prevents accidental disclosure through string conversion and
    representation. Python does not provide hardened secret memory or reliable
    zeroization; this boundary aims to prevent logging and persistence.
    """

    __slots__ = ("_value",)

    SIZE = 16

    def __init__(self, value: bytes) -> None:
        if not isinstance(value, bytes) or len(value) != self.SIZE:
            raise InvalidLinkKeyError(
                "Classic Bluetooth LinkKey must contain exactly 16 bytes"
            )
        self._value = value

    def __str__(self) -> str:
        return "<BluetoothLinkKey redacted>"

    def __repr__(self) -> str:
        return "<BluetoothLinkKey redacted>"

    def _as_bytes_for_bumble(self) -> bytes:
        """Expose bytes only at the intentional in-memory Bumble boundary."""

        return self._value


@dataclass(frozen=True, slots=True)
class ClassicPairingCredentials:
    """Classic pairing data retained from a BlueZ LinkKey record."""

    link_key: BluetoothLinkKey
    link_key_type: int
    authenticated: bool
    pin_length: int | None


class BlueZPairingStore:
    """Read standard BlueZ per-device info files from a configurable root."""

    DEFAULT_STORAGE_ROOT = Path("/var/lib/bluetooth")
    _KEY_HEX = re.compile(r"^[0-9A-Fa-f]{32}$")
    _VALID_LINK_KEY_TYPES = frozenset(range(9))
    _AUTHENTICATED_LINK_KEY_TYPES = frozenset((5, 8))

    def __init__(self, storage_root: Path | str = DEFAULT_STORAGE_ROOT) -> None:
        self._storage_root = Path(storage_root)

    def info_path(
        self,
        adapter_address: BluetoothAddress,
        device_address: BluetoothAddress,
    ) -> Path:
        """Build a path only from validated Bluetooth address components."""

        if not isinstance(adapter_address, BluetoothAddress) or not isinstance(
            device_address, BluetoothAddress
        ):
            raise TypeError("adapter and device addresses must be BluetoothAddress")
        return (
            self._storage_root
            / adapter_address.path_component
            / device_address.path_component
            / "info"
        )

    def load_classic_credentials(
        self,
        adapter_address: BluetoothAddress,
        device_address: BluetoothAddress,
    ) -> ClassicPairingCredentials:
        """Load one Classic LinkKey without logging or persisting it."""

        info_path = self.info_path(adapter_address, device_address)
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str

        try:
            with info_path.open("r", encoding="utf-8") as info_file:
                parser.read_file(info_file)
        except FileNotFoundError:
            raise PairingInfoNotFoundError(
                "BlueZ pairing information was not found"
            ) from None
        except PermissionError:
            raise PairingPermissionError(
                "permission denied reading BlueZ pairing information"
            ) from None
        except configparser.Error:
            raise PairingInfoFormatError(
                "BlueZ pairing information is malformed"
            ) from None
        except OSError:
            raise PairingStoreError(
                "could not read BlueZ pairing information"
            ) from None

        if not parser.has_section("LinkKey"):
            raise LinkKeySectionMissingError(
                "BlueZ pairing information has no Classic LinkKey section"
            )

        section = parser["LinkKey"]
        key_text = section.get("Key", "").strip()
        if not self._KEY_HEX.fullmatch(key_text):
            raise InvalidLinkKeyError(
                "Classic Bluetooth LinkKey must be 32 hexadecimal characters"
            )

        try:
            link_key_type = int(section.get("Type", ""), 10)
        except ValueError:
            raise InvalidLinkKeyTypeError(
                "Classic Bluetooth LinkKey Type must be an integer"
            ) from None
        if link_key_type not in self._VALID_LINK_KEY_TYPES:
            raise InvalidLinkKeyTypeError(
                "Classic Bluetooth LinkKey Type is outside the supported range"
            )

        pin_length_text = section.get("PINLength")
        pin_length: int | None = None
        if pin_length_text is not None:
            try:
                pin_length = int(pin_length_text, 10)
            except ValueError:
                raise InvalidPINLengthError(
                    "Classic Bluetooth PINLength must be an integer"
                ) from None
            if not 0 <= pin_length <= 16:
                raise InvalidPINLengthError(
                    "Classic Bluetooth PINLength is outside the valid range"
                )

        return ClassicPairingCredentials(
            link_key=BluetoothLinkKey(bytes.fromhex(key_text)),
            link_key_type=link_key_type,
            authenticated=(
                link_key_type in self._AUTHENTICATED_LINK_KEY_TYPES
            ),
            pin_length=pin_length,
        )
