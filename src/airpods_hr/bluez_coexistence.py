"""Private BlueZ/kernel coexistence transport used by the coexistence probe."""

from __future__ import annotations


import ctypes
import errno
import os


from collections.abc import Mapping

from dataclasses import dataclass
from enum import StrEnum

from typing import Any

from dbus_next.service import ServiceInterface, method

from airpods_hr.aap import HandshakeObservation


from airpods_hr.discovery import (
    AirPodsCandidate,
    BlueZDeviceDiscovery,
    DeviceDiscoveryError,
    MultipleAirPodsCandidatesError,
    NoAirPodsCandidatesError,
    select_single_candidate,
)
from airpods_hr.heart_rate_session import ControlFrameSummary


from airpods_hr.sdp import BlueZSDPServiceRecord


DEFAULT_DBUS_TIMEOUT = 5.0
DEFAULT_L2CAP_CONNECT_TIMEOUT = 10.0
DEFAULT_HANDSHAKE_TIMEOUT = 5.0
DEFAULT_DESCRIPTOR_TIMEOUT = 3.0
DEFAULT_RECEIVE_SIZE = 64 * 1024
_AAP_LOCAL_RX_IMTU = 2048
# Verified against this Linux host's bluetooth/bluetooth.h and l2cap.h. Python
# 3.14 exposes SOL_L2CAP here but omits L2CAP_OPTIONS.
_LINUX_SOL_L2CAP = 6
_LINUX_L2CAP_OPTIONS = 1


class _NativeL2CAPOptions(ctypes.Structure):
    """Native Linux ``struct l2cap_options`` from bluetooth/l2cap.h."""

    _fields_ = (
        ("omtu", ctypes.c_uint16),
        ("imtu", ctypes.c_uint16),
        ("flush_to", ctypes.c_uint16),
        ("mode", ctypes.c_uint8),
        ("fcs", ctypes.c_uint8),
        ("max_tx", ctypes.c_uint8),
        ("txwin_size", ctypes.c_uint16),
    )


_L2CAP_OPTIONS_SIZE = ctypes.sizeof(_NativeL2CAPOptions)
_L2CAP_IMTU_OFFSET = _NativeL2CAPOptions.imtu.offset


class CoexistencePhase(StrEnum):
    PREFLIGHT = "preflight"
    PROFILE_REGISTRATION = "profile_registration"
    L2CAP_CONNECTION = "l2cap_connection"
    AAP_HANDSHAKE = "aap_handshake"
    HR_ACTIVATION = "hr_activation"
    HR_RECEPTION = "hr_reception"
    CLEANUP = "cleanup"


class CoexistenceCategory(StrEnum):
    PREFLIGHT_FAILED = "preflight_failed"
    BLUEZ_NOT_AVAILABLE = "bluez_not_available"
    AIRPODS_NOT_CONNECTED = "airpods_not_connected"
    FRESH_ACL_REQUIRES_DISCONNECTED_DEVICE = (
        "fresh_acl_requires_disconnected_device"
    )
    PROFILE_REGISTRATION_FAILED = "profile_registration_failed"
    L2CAP_SOCKET_FAILED = "l2cap_socket_failed"
    L2CAP_BIND_FAILED = "l2cap_bind_failed"
    L2CAP_SECURITY_FAILED = "l2cap_security_failed"
    L2CAP_LOCAL_RX_MTU_FAILED = "l2cap_local_rx_mtu_failed"
    L2CAP_CONNECT_FAILED = "l2cap_connect_failed"
    L2CAP_ROUTE_MISMATCH = "l2cap_route_mismatch"
    AAP_HANDSHAKE_FAILED = "aap_handshake_failed"
    AAP_DESCRIPTOR_TIMEOUT = "aap_descriptor_timeout"
    HR_ACTIVATION_FAILED = "hr_activation_failed"
    HR_TIMEOUT = "hr_timeout"
    BLUEZ_CONNECTION_LOST = "bluez_connection_lost"
    CLEANUP_FAILED = "cleanup_failed"


class CoexistenceFailure(RuntimeError):
    """A safe phase/category failure suitable for terminal diagnostics."""

    def __init__(
        self,
        category: CoexistenceCategory,
        phase: CoexistencePhase,
        detail: str | None = None,
        handshake_observation: HandshakeObservation | None = None,
        hr_timeout_diagnostics: CoexistenceHRTimeoutDiagnostics | None = None,
        l2cap_local_rx_observation: KernelL2CAPLocalRXObservation | None = None,
        experimental_ack_only_hr_attempted: bool = False,
    ) -> None:
        self.category = category
        self.phase = phase
        self.detail = detail
        self.handshake_observation = handshake_observation
        self.hr_timeout_diagnostics = hr_timeout_diagnostics
        self.l2cap_local_rx_observation = l2cap_local_rx_observation
        self.experimental_ack_only_hr_attempted = (
            experimental_ack_only_hr_attempted
        )
        super().__init__(f"{category.value} at {phase.value}")


@dataclass(frozen=True, slots=True)
class BlueZCoexistenceState:
    candidate: AirPodsCandidate
    adapter_powered: bool
    device_connected: bool
    adapter_uuids: frozenset[str]


@dataclass(frozen=True, slots=True)
class CoexistenceHRStreamObservation:
    """Bounded structural evidence from the post-START_HR stream window."""

    frames_observed: int
    frames_with_hr_marker: int
    frames_without_hr_marker: int
    frame_summaries: tuple[ControlFrameSummary, ...]
    observation_armed: bool
    observation_cleanly_disarmed: bool
    receive_frames_dropped: int


@dataclass(frozen=True, slots=True)
class CoexistenceHRTimeoutDiagnostics:
    """Safe transport and canonical-session counters for an HR timeout."""

    stream_observation: CoexistenceHRStreamObservation
    canonical_non_hr_frames: int
    canonical_malformed_hr_frames: int
    control_frames_observed: int
    frame_count_corresponds: bool


@dataclass(frozen=True, slots=True)
class KernelL2CAPLocalRXObservation:
    """Safe metadata for the pre-connect Classic L2CAP receive-MTU setup."""

    target_imtu: int
    options_source: str
    before_imtu: int | None
    after_imtu: int | None
    preserved_omtu: bool | None
    preserved_flush_to: bool | None
    preserved_mode: bool | None
    preserved_fcs: bool | None
    preserved_max_tx: bool | None
    preserved_txwin_size: bool | None
    verified: bool


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _safe_error_detail(error: BaseException) -> str:
    error_name = getattr(error, "type", None)
    if isinstance(error_name, str) and error_name:
        return f"D-Bus error {error_name}"
    error_number = getattr(error, "errno", None)
    if isinstance(error_number, int):
        name = errno.errorcode.get(error_number, "UNKNOWN")
        return f"errno {name} ({error_number})"
    return type(error).__name__


class DBusNextBlueZCoexistenceClient:
    """Read BlueZ state and own temporary ProfileManager registrations."""

    BLUEZ_SERVICE = "org.bluez"
    OBJECT_MANAGER_INTERFACE = "org.freedesktop.DBus.ObjectManager"
    PROFILE_MANAGER_INTERFACE = "org.bluez.ProfileManager1"

    def __init__(self) -> None:
        self._bus: Any | None = None
        self._variant: Any | None = None
        self._profile_manager: Any | None = None

    async def connect(self) -> None:
        if self._bus is not None:
            return
        try:
            from dbus_next import BusType, Variant
            from dbus_next.aio import MessageBus

            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            self._variant = Variant
            await self.get_managed_objects()
        except Exception as error:
            self.close()
            raise CoexistenceFailure(
                CoexistenceCategory.BLUEZ_NOT_AVAILABLE,
                CoexistencePhase.PREFLIGHT,
                _safe_error_detail(error),
            ) from error

    def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
        self._bus = None
        self._variant = None
        self._profile_manager = None

    async def get_managed_objects(self) -> Mapping[str, Mapping[str, Any]]:
        if self._bus is None:
            raise CoexistenceFailure(
                CoexistenceCategory.BLUEZ_NOT_AVAILABLE,
                CoexistencePhase.PREFLIGHT,
            )
        introspection = await self._bus.introspect(self.BLUEZ_SERVICE, "/")
        proxy = self._bus.get_proxy_object(
            self.BLUEZ_SERVICE, "/", introspection
        )
        manager = proxy.get_interface(self.OBJECT_MANAGER_INTERFACE)
        return await manager.call_get_managed_objects()

    async def preflight(
        self, *, require_connected: bool = True
    ) -> BlueZCoexistenceState:
        try:
            candidates = await BlueZDeviceDiscovery(self).discover_candidates()
            candidate = select_single_candidate(candidates)
            state = await self.snapshot(candidate)
        except CoexistenceFailure:
            raise
        except (NoAirPodsCandidatesError, MultipleAirPodsCandidatesError) as error:
            raise CoexistenceFailure(
                CoexistenceCategory.PREFLIGHT_FAILED,
                CoexistencePhase.PREFLIGHT,
                type(error).__name__,
            ) from error
        except DeviceDiscoveryError as error:
            raise CoexistenceFailure(
                CoexistenceCategory.PREFLIGHT_FAILED,
                CoexistencePhase.PREFLIGHT,
                type(error).__name__,
            ) from error
        except Exception as error:
            raise CoexistenceFailure(
                CoexistenceCategory.BLUEZ_NOT_AVAILABLE,
                CoexistencePhase.PREFLIGHT,
                _safe_error_detail(error),
            ) from error
        if not state.adapter_powered:
            raise CoexistenceFailure(
                CoexistenceCategory.PREFLIGHT_FAILED,
                CoexistencePhase.PREFLIGHT,
                "adapter is not powered",
            )
        if require_connected and not state.device_connected:
            raise CoexistenceFailure(
                CoexistenceCategory.AIRPODS_NOT_CONNECTED,
                CoexistencePhase.PREFLIGHT,
                "Device1.Connected is false",
            )
        return state

    async def snapshot(
        self, candidate: AirPodsCandidate
    ) -> BlueZCoexistenceState:
        try:
            objects = await self.get_managed_objects()
        except Exception as error:
            raise CoexistenceFailure(
                CoexistenceCategory.BLUEZ_NOT_AVAILABLE,
                CoexistencePhase.PREFLIGHT,
                _safe_error_detail(error),
            ) from error
        device_interfaces = objects.get(candidate.object_path)
        device = (
            device_interfaces.get("org.bluez.Device1")
            if device_interfaces is not None
            else None
        )
        if device is None:
            raise CoexistenceFailure(
                CoexistenceCategory.BLUEZ_CONNECTION_LOST,
                CoexistencePhase.PREFLIGHT,
                "BlueZ Device1 object is unavailable",
            )
        adapter_interfaces = objects.get(candidate.adapter_path)
        adapter = (
            adapter_interfaces.get("org.bluez.Adapter1")
            if adapter_interfaces is not None
            else None
        )
        if adapter is None:
            raise CoexistenceFailure(
                CoexistenceCategory.BLUEZ_NOT_AVAILABLE,
                CoexistencePhase.PREFLIGHT,
                "BlueZ Adapter1 object is unavailable",
            )
        try:
            paired = bool(_value(device.get("Paired", False)))
            connected = bool(_value(device.get("Connected", False)))
            powered = bool(_value(adapter.get("Powered", False)))
            uuids_value = _value(adapter.get("UUIDs", []))
            uuids = frozenset(
                item.lower()
                for item in uuids_value
                if isinstance(item, str)
            )
        except Exception as error:
            raise CoexistenceFailure(
                CoexistenceCategory.BLUEZ_NOT_AVAILABLE,
                CoexistencePhase.PREFLIGHT,
                _safe_error_detail(error),
            ) from error
        if not paired:
            raise CoexistenceFailure(
                CoexistenceCategory.PREFLIGHT_FAILED,
                CoexistencePhase.PREFLIGHT,
                "Device1.Paired is false",
            )
        return BlueZCoexistenceState(candidate, powered, connected, uuids)

    async def register_profile(
        self,
        object_path: str,
        profile: _BlueZProfileObject,
        record: BlueZSDPServiceRecord,
    ) -> None:
        if self._bus is None:
            raise RuntimeError("BlueZ client is not connected")
        if self._profile_manager is None:
            introspection = await self._bus.introspect(
                self.BLUEZ_SERVICE, "/org/bluez"
            )
            proxy = self._bus.get_proxy_object(
                self.BLUEZ_SERVICE, "/org/bluez", introspection
            )
            self._profile_manager = proxy.get_interface(
                self.PROFILE_MANAGER_INTERFACE
            )
        self._bus.export(object_path, profile)
        try:
            options = {
                "Name": self._variant("s", f"AirPods HR {record.name}"),
                "ServiceRecord": self._variant("s", record.service_record),
                "RequireAuthentication": self._variant("b", True),
            }
            await self._profile_manager.call_register_profile(
                object_path, record.uuid, options
            )
        except BaseException:
            self._bus.unexport(object_path, profile)
            raise

    async def unregister_profile(
        self, object_path: str, profile: _BlueZProfileObject
    ) -> None:
        if self._bus is None or self._profile_manager is None:
            return
        try:
            await self._profile_manager.call_unregister_profile(object_path)
        except Exception as error:
            if getattr(error, "type", "") != "org.bluez.Error.DoesNotExist":
                raise
        finally:
            self._bus.unexport(object_path, profile)


class _BlueZProfileObject(ServiceInterface):
    """Minimal Profile1 lifecycle object for an SDP-only registration."""

    def __init__(self) -> None:
        super().__init__("org.bluez.Profile1")
        self.released = False

    @method()
    def Release(self) -> "":
        self.released = True

    @method()
    def NewConnection(
        self, device: "o", file_descriptor: "h", properties: "a{sv}"
    ) -> "":
        del device, properties
        if isinstance(file_descriptor, int):
            os.close(file_descriptor)

    @method()
    def RequestDisconnection(self, device: "o") -> "":
        del device

    @method()
    def Cancel(self) -> "":
        pass


