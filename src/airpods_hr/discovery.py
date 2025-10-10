"""Read-only discovery of paired AirPods candidates through BlueZ D-Bus."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from airpods_hr.address import BluetoothAddress, InvalidBluetoothAddressError


class DeviceDiscoveryError(RuntimeError):
    """Base error for BlueZ device discovery."""


class DeviceDiscoveryUnavailableError(DeviceDiscoveryError):
    """Raised when the read-only BlueZ D-Bus backend is unavailable."""


class InvalidDeviceMetadataError(DeviceDiscoveryError):
    """Raised when a matching BlueZ device has unsafe metadata."""


class NoAirPodsCandidatesError(DeviceDiscoveryError):
    """Raised when no paired AirPods candidate is available."""


class MultipleAirPodsCandidatesError(DeviceDiscoveryError):
    """Raised instead of silently choosing among multiple candidates."""

    def __init__(self, candidates: Sequence[AirPodsCandidate]) -> None:
        self.candidates = tuple(candidates)
        super().__init__(
            f"multiple paired AirPods candidates found ({len(self.candidates)})"
        )


@dataclass(frozen=True, slots=True)
class BlueZDevice:
    """Non-secret Device1 metadata needed for candidate selection."""

    object_path: str = field(repr=False)
    adapter_path: str
    address: BluetoothAddress = field(repr=False)
    name: str | None
    alias: str | None
    paired: bool

    @property
    def display_name(self) -> str:
        return self.alias or self.name or "AirPods"


@dataclass(frozen=True, slots=True)
class AirPodsCandidate:
    """A paired device matching the initial conservative AirPods rule."""

    display_name: str
    adapter_name: str
    adapter_path: str
    adapter_address: BluetoothAddress = field(repr=False)
    address: BluetoothAddress = field(repr=False)
    object_path: str = field(repr=False)
    adapter_modalias: str | None = None


class ManagedObjectsBackend(Protocol):
    """Read-only ObjectManager boundary used by discovery."""

    async def get_managed_objects(self) -> Mapping[str, Mapping[str, Any]]:
        """Return BlueZ ObjectManager data."""


class BlueZDeviceDiscovery:
    """Enumerate paired devices matching a replaceable name policy."""

    DEVICE_INTERFACE = "org.bluez.Device1"
    ADAPTER_INTERFACE = "org.bluez.Adapter1"
    _AIRPODS_NAME = re.compile(r"(?<![a-z0-9])airpods(?![a-z0-9])", re.I)

    def __init__(self, backend: ManagedObjectsBackend) -> None:
        self._backend = backend

    async def discover_candidates(self) -> tuple[AirPodsCandidate, ...]:
        managed_objects = await self._backend.get_managed_objects()
        candidates: list[AirPodsCandidate] = []

        for object_path, interfaces in managed_objects.items():
            properties = interfaces.get(self.DEVICE_INTERFACE)
            if properties is None:
                continue

            paired = bool(_property_value(properties.get("Paired", False)))
            name = _optional_string(properties.get("Name"))
            alias = _optional_string(properties.get("Alias"))
            if not paired or not self._matches_supported_name(name, alias):
                continue

            address_value = _property_value(properties.get("Address"))
            adapter_path = _property_value(properties.get("Adapter"))
            if not isinstance(address_value, str) or not isinstance(
                adapter_path, str
            ):
                raise InvalidDeviceMetadataError(
                    "matching BlueZ device has incomplete address metadata"
                )

            try:
                address = BluetoothAddress.parse(address_value)
            except InvalidBluetoothAddressError:
                raise InvalidDeviceMetadataError(
                    "matching BlueZ device has a malformed Bluetooth address"
                ) from None

            adapter_name = adapter_path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"hci[0-9]+", adapter_name):
                raise InvalidDeviceMetadataError(
                    "matching BlueZ device has a malformed adapter path"
                )

            adapter_interfaces = managed_objects.get(adapter_path)
            adapter_properties = (
                adapter_interfaces.get(self.ADAPTER_INTERFACE)
                if adapter_interfaces is not None
                else None
            )
            adapter_address_value = (
                _property_value(adapter_properties.get("Address"))
                if adapter_properties is not None
                else None
            )
            adapter_modalias = (
                _optional_string(adapter_properties.get("Modalias"))
                if adapter_properties is not None
                else None
            )
            if not isinstance(adapter_address_value, str):
                raise InvalidDeviceMetadataError(
                    "matching BlueZ device has incomplete adapter metadata"
                )
            try:
                adapter_address = BluetoothAddress.parse(adapter_address_value)
            except InvalidBluetoothAddressError:
                raise InvalidDeviceMetadataError(
                    "matching BlueZ device has a malformed adapter address"
                ) from None

            device = BlueZDevice(
                object_path=object_path,
                adapter_path=adapter_path,
                address=address,
                name=name,
                alias=alias,
                paired=paired,
            )
            candidates.append(
                AirPodsCandidate(
                    display_name=device.display_name,
                    adapter_name=adapter_name,
                    adapter_path=device.adapter_path,
                    adapter_address=adapter_address,
                    address=device.address,
                    object_path=device.object_path,
                    adapter_modalias=adapter_modalias,
                )
            )

        return tuple(candidates)

    @classmethod
    def _matches_supported_name(
        cls, name: str | None, alias: str | None
    ) -> bool:
        return any(
            cls._AIRPODS_NAME.search(value) is not None
            for value in (name, alias)
            if value
        )


def select_single_candidate(
    candidates: Sequence[AirPodsCandidate],
) -> AirPodsCandidate:
    """Select one candidate, preserving ambiguity for a future CLI choice."""

    if not candidates:
        raise NoAirPodsCandidatesError("no paired AirPods candidates found")
    if len(candidates) > 1:
        raise MultipleAirPodsCandidatesError(candidates)
    return candidates[0]


class DBusNextManagedObjectsBackend:
    """Pure-Python, read-only BlueZ ObjectManager client."""

    BLUEZ_SERVICE = "org.bluez"
    OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"

    def __init__(self) -> None:
        self._bus: Any | None = None

    async def connect(self) -> None:
        if self._bus is not None:
            return
        try:
            from dbus_next import BusType
            from dbus_next.aio import MessageBus
        except ModuleNotFoundError as error:
            raise DeviceDiscoveryUnavailableError(
                "dbus-next is required for BlueZ device discovery"
            ) from error

        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as error:
            raise DeviceDiscoveryUnavailableError(
                "could not connect to the system D-Bus"
            ) from error

    def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def get_managed_objects(self) -> Mapping[str, Mapping[str, Any]]:
        if self._bus is None:
            raise DeviceDiscoveryUnavailableError(
                "the BlueZ discovery backend is not connected"
            )
        try:
            introspection = await self._bus.introspect(self.BLUEZ_SERVICE, "/")
            proxy = self._bus.get_proxy_object(
                self.BLUEZ_SERVICE, "/", introspection
            )
            manager = proxy.get_interface(self.OBJECT_MANAGER_INTERFACE)
            return await manager.call_get_managed_objects()
        except Exception as error:
            raise DeviceDiscoveryUnavailableError(
                "org.bluez is not available on the system bus"
            ) from error


def _property_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _optional_string(value: Any) -> str | None:
    unwrapped = _property_value(value)
    return unwrapped if isinstance(unwrapped, str) and unwrapped else None
