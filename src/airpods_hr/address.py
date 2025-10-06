"""Strict Bluetooth address values for safe internal path construction."""

from __future__ import annotations

import re
from dataclasses import dataclass


class InvalidBluetoothAddressError(ValueError):
    """Raised when a Bluetooth address is not six canonical octets."""


@dataclass(frozen=True, slots=True, repr=False)
class BluetoothAddress:
    """A validated, uppercase, colon-separated Bluetooth address."""

    value: str

    _PATTERN = re.compile(r"^[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}$")

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self._PATTERN.fullmatch(
            self.value
        ):
            raise InvalidBluetoothAddressError(
                "Bluetooth address must contain six hexadecimal octets"
            )
        object.__setattr__(self, "value", self.value.upper())

    @classmethod
    def parse(cls, value: str) -> BluetoothAddress:
        """Validate and normalize a Bluetooth address."""

        return cls(value)

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return "<BluetoothAddress redacted>"

    @property
    def path_component(self) -> str:
        """Return the validated BlueZ directory component."""

        return self.value
