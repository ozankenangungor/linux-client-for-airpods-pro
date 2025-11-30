"""Private BlueZ/kernel coexistence transport used by the coexistence probe."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import socket
import struct
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum

from typing import Any

from dbus_next.service import ServiceInterface, method

from airpods_hr.aap import AAP_HANDSHAKE_REQUEST, AAPHandshakeError, HandshakeObservation


from airpods_hr.address import BluetoothAddress, InvalidBluetoothAddressError
from airpods_hr.discovery import (
    AirPodsCandidate,
    BlueZDeviceDiscovery,
    DeviceDiscoveryError,
    MultipleAirPodsCandidatesError,
    NoAirPodsCandidatesError,
    select_single_candidate,
)
from airpods_hr.heart_rate_session import DEFAULT_CONTROL_SUMMARY_LIMIT, ControlFrameSummary


from airpods_hr.protocol import AAP_PSM, HEART_RATE_MARKER, HeartRateCommand
from airpods_hr.sdp import (
    REQUIRED_BLUEZ_SDP_COMPATIBILITY_UUIDS,
    BlueZSDPServiceRecord,
    USBAdapterIdentity,
    build_bluez_sdp_service_records,
)


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


@dataclass(frozen=True, slots=True)
class _L2CAPOptionsValues:
    omtu: int
    imtu: int
    flush_to: int
    mode: int
    fcs: int
    max_tx: int
    txwin_size: int


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


class BlueZCompatibilityRegistration:
    """Register only compatibility service classes absent from Adapter1."""

    OBJECT_PATH_PREFIX = "/org/airpods_hr/coexistence/profile"

    def __init__(
        self,
        client: DBusNextBlueZCoexistenceClient,
        *,
        operation_timeout: float = DEFAULT_DBUS_TIMEOUT,
    ) -> None:
        self._client = client
        self._operation_timeout = operation_timeout
        self._registered: list[tuple[str, _BlueZProfileObject]] = []
        self._registered_total = 0
        self._registration_attempted = False

    @property
    def registered_count(self) -> int:
        return self._registered_total

    async def register(self, state: BlueZCoexistenceState) -> None:
        if self._registration_attempted:
            raise RuntimeError("compatibility registration is single-use")
        self._registration_attempted = True
        if REQUIRED_BLUEZ_SDP_COMPATIBILITY_UUIDS.issubset(
            state.adapter_uuids
        ):
            return
        identity = USBAdapterIdentity.from_bluez_modalias(
            state.candidate.adapter_modalias
        )
        records = build_bluez_sdp_service_records(identity)
        missing = [
            record
            for record in records
            if record.uuid.lower() not in state.adapter_uuids
        ]
        try:
            for index, record in enumerate(missing):
                object_path = f"{self.OBJECT_PATH_PREFIX}_{index}"
                profile = _BlueZProfileObject()
                await asyncio.wait_for(
                    self._client.register_profile(object_path, profile, record),
                    timeout=self._operation_timeout,
                )
                self._registered.append((object_path, profile))
                self._registered_total += 1
        except BaseException as error:
            try:
                await self.unregister()
            except BaseException:
                pass
            raise CoexistenceFailure(
                CoexistenceCategory.PROFILE_REGISTRATION_FAILED,
                CoexistencePhase.PROFILE_REGISTRATION,
                _safe_error_detail(error),
            ) from error

    async def unregister(self) -> None:
        errors: list[BaseException] = []
        while self._registered:
            object_path, profile = self._registered.pop()
            try:
                await asyncio.wait_for(
                    self._client.unregister_profile(object_path, profile),
                    timeout=self._operation_timeout,
                )
            except BaseException as error:
                errors.append(error)
        if errors:
            raise CoexistenceFailure(
                CoexistenceCategory.CLEANUP_FAILED,
                CoexistencePhase.CLEANUP,
                _safe_error_detail(errors[-1]),
            )


class KernelL2CAPTransport:
    """AAP receive transport over one kernel-managed BR/EDR L2CAP socket."""

    _SECURITY_STRUCT = struct.Struct("=BB")

    def __init__(
        self,
        *,
        connect_timeout: float = DEFAULT_L2CAP_CONNECT_TIMEOUT,
        receive_size: int = DEFAULT_RECEIVE_SIZE,
        socket_module: Any = socket,
        socket_factory: Callable[..., Any] | None = None,
        stream_summary_limit: int = DEFAULT_CONTROL_SUMMARY_LIMIT,
    ) -> None:
        if connect_timeout <= 0 or receive_size <= 0:
            raise ValueError("L2CAP timeouts and receive size must be positive")
        if not 1 <= stream_summary_limit <= DEFAULT_CONTROL_SUMMARY_LIMIT:
            raise ValueError(
                "stream summary limit must be between 1 and "
                f"{DEFAULT_CONTROL_SUMMARY_LIMIT}"
            )
        self._connect_timeout = connect_timeout
        self._receive_size = receive_size
        self._socket_module = socket_module
        self._socket_factory = socket_factory or socket_module.socket
        self._socket: Any | None = None
        self._collecting = False
        self._closed = False
        self._application_payloads_sent = 0
        self.dropped_frames = 0
        self._stream_summary_limit = stream_summary_limit
        self._hr_stream_observation_active = False
        self._hr_stream_observation_was_armed = False
        self._hr_stream_observation_cleanly_disarmed = False
        self._hr_stream_frames_observed = 0
        self._hr_stream_frames_with_marker = 0
        self._hr_stream_frame_summaries: list[ControlFrameSummary] = []
        self._local_rx_observation = KernelL2CAPLocalRXObservation(
            target_imtu=_AAP_LOCAL_RX_IMTU,
            options_source="unresolved",
            before_imtu=None,
            after_imtu=None,
            preserved_omtu=None,
            preserved_flush_to=None,
            preserved_mode=None,
            preserved_fcs=None,
            preserved_max_tx=None,
            preserved_txwin_size=None,
            verified=False,
        )

    @property
    def application_payloads_sent(self) -> int:
        return self._application_payloads_sent

    @property
    def pending_receive_frames(self) -> int:
        return 0

    @property
    def hr_stream_observation(self) -> CoexistenceHRStreamObservation:
        frames_without_marker = (
            self._hr_stream_frames_observed
            - self._hr_stream_frames_with_marker
        )
        return CoexistenceHRStreamObservation(
            frames_observed=self._hr_stream_frames_observed,
            frames_with_hr_marker=self._hr_stream_frames_with_marker,
            frames_without_hr_marker=frames_without_marker,
            frame_summaries=tuple(self._hr_stream_frame_summaries),
            observation_armed=self._hr_stream_observation_was_armed,
            observation_cleanly_disarmed=(
                self._hr_stream_observation_cleanly_disarmed
            ),
            receive_frames_dropped=self.dropped_frames,
        )

    @property
    def local_rx_observation(self) -> KernelL2CAPLocalRXObservation:
        return self._local_rx_observation

    @staticmethod
    def _decode_l2cap_options(raw: bytes) -> _L2CAPOptionsValues:
        if not isinstance(raw, bytes) or len(raw) != _L2CAP_OPTIONS_SIZE:
            raise ValueError(
                "kernel returned an unexpected l2cap_options structure size"
            )
        native = _NativeL2CAPOptions.from_buffer_copy(raw)
        return _L2CAPOptionsValues(
            omtu=native.omtu,
            imtu=native.imtu,
            flush_to=native.flush_to,
            mode=native.mode,
            fcs=native.fcs,
            max_tx=native.max_tx,
            txwin_size=native.txwin_size,
        )

    def _l2cap_option_ids(self) -> tuple[int, int, str]:
        exposed_sol = getattr(self._socket_module, "SOL_L2CAP", None)
        exposed_option = getattr(self._socket_module, "L2CAP_OPTIONS", None)
        if exposed_sol is not None and exposed_sol != _LINUX_SOL_L2CAP:
            raise ValueError("socket.SOL_L2CAP conflicts with Linux UAPI")
        if (
            exposed_option is not None
            and exposed_option != _LINUX_L2CAP_OPTIONS
        ):
            raise ValueError("socket.L2CAP_OPTIONS conflicts with Linux UAPI")
        if isinstance(exposed_sol, int) and isinstance(exposed_option, int):
            return exposed_sol, exposed_option, "python-socket"
        return (
            _LINUX_SOL_L2CAP,
            _LINUX_L2CAP_OPTIONS,
            "linux-uapi-fallback",
        )

    @staticmethod
    def _preserved_fields(
        before: _L2CAPOptionsValues, after: _L2CAPOptionsValues
    ) -> dict[str, bool]:
        return {
            "omtu": after.omtu == before.omtu,
            "flush_to": after.flush_to == before.flush_to,
            "mode": after.mode == before.mode,
            "fcs": after.fcs == before.fcs,
            "max_tx": after.max_tx == before.max_tx,
            "txwin_size": after.txwin_size == before.txwin_size,
        }

    def _set_local_rx_imtu(self, sock: Any) -> None:
        level, option, source = self._l2cap_option_ids()
        self._local_rx_observation = KernelL2CAPLocalRXObservation(
            target_imtu=_AAP_LOCAL_RX_IMTU,
            options_source=source,
            before_imtu=None,
            after_imtu=None,
            preserved_omtu=None,
            preserved_flush_to=None,
            preserved_mode=None,
            preserved_fcs=None,
            preserved_max_tx=None,
            preserved_txwin_size=None,
            verified=False,
        )
        original = sock.getsockopt(level, option, _L2CAP_OPTIONS_SIZE)
        before = self._decode_l2cap_options(original)
        self._local_rx_observation = KernelL2CAPLocalRXObservation(
            target_imtu=_AAP_LOCAL_RX_IMTU,
            options_source=source,
            before_imtu=before.imtu,
            after_imtu=None,
            preserved_omtu=None,
            preserved_flush_to=None,
            preserved_mode=None,
            preserved_fcs=None,
            preserved_max_tx=None,
            preserved_txwin_size=None,
            verified=False,
        )
        updated = bytearray(original)
        struct.pack_into("@H", updated, _L2CAP_IMTU_OFFSET, _AAP_LOCAL_RX_IMTU)
        sock.setsockopt(level, option, bytes(updated))
        observed = sock.getsockopt(level, option, _L2CAP_OPTIONS_SIZE)
        after = self._decode_l2cap_options(observed)
        preserved = self._preserved_fields(before, after)
        verified = after.imtu == _AAP_LOCAL_RX_IMTU and all(
            preserved.values()
        )
        self._local_rx_observation = KernelL2CAPLocalRXObservation(
            target_imtu=_AAP_LOCAL_RX_IMTU,
            options_source=source,
            before_imtu=before.imtu,
            after_imtu=after.imtu,
            preserved_omtu=preserved["omtu"],
            preserved_flush_to=preserved["flush_to"],
            preserved_mode=preserved["mode"],
            preserved_fcs=preserved["fcs"],
            preserved_max_tx=preserved["max_tx"],
            preserved_txwin_size=preserved["txwin_size"],
            verified=verified,
        )
        if not verified:
            raise ValueError("kernel did not preserve verified L2CAP options")

    def arm_hr_stream_observation(self) -> None:
        if self._hr_stream_observation_was_armed:
            raise RuntimeError("HR stream observation is single-use")
        self._hr_stream_observation_was_armed = True
        self._hr_stream_observation_active = True

    def disarm_hr_stream_observation(self) -> None:
        if self._hr_stream_observation_active:
            self._hr_stream_observation_active = False
            self._hr_stream_observation_cleanly_disarmed = True

    async def open(self, local_address: str, remote_address: str) -> None:
        if self._socket is not None or self._closed:
            raise RuntimeError("L2CAP transport is single-use")
        try:
            family = self._required_constant("AF_BLUETOOTH")
            socket_type = self._required_constant("SOCK_SEQPACKET")
            protocol = self._required_constant("BTPROTO_L2CAP")
            sock = self._socket_factory(family, socket_type, protocol)
            self._socket = sock
        except Exception as error:
            raise CoexistenceFailure(
                CoexistenceCategory.L2CAP_SOCKET_FAILED,
                CoexistencePhase.L2CAP_CONNECTION,
                _safe_error_detail(error),
            ) from error
        try:
            sock.bind((local_address, 0))
        except Exception as error:
            self.close()
            raise CoexistenceFailure(
                CoexistenceCategory.L2CAP_BIND_FAILED,
                CoexistencePhase.L2CAP_CONNECTION,
                _safe_error_detail(error),
            ) from error
        try:
            security = self._SECURITY_STRUCT.pack(
                self._required_constant("BT_SECURITY_MEDIUM"), 0
            )
            sock.setsockopt(
                self._required_constant("SOL_BLUETOOTH"),
                self._required_constant("BT_SECURITY"),
                security,
            )
        except Exception as error:
            self.close()
            raise CoexistenceFailure(
                CoexistenceCategory.L2CAP_SECURITY_FAILED,
                CoexistencePhase.L2CAP_CONNECTION,
                _safe_error_detail(error),
            ) from error
        try:
            self._set_local_rx_imtu(sock)
        except Exception as error:
            self.close()
            raise CoexistenceFailure(
                CoexistenceCategory.L2CAP_LOCAL_RX_MTU_FAILED,
                CoexistencePhase.L2CAP_CONNECTION,
                _safe_error_detail(error),
                l2cap_local_rx_observation=self._local_rx_observation,
            ) from error
        try:
            sock.settimeout(self._connect_timeout)
            await asyncio.wait_for(
                asyncio.to_thread(
                    sock.connect, (remote_address, AAP_PSM)
                ),
                timeout=self._connect_timeout,
            )
        except BaseException as error:
            self.close()
            if isinstance(error, asyncio.CancelledError):
                raise
            raise CoexistenceFailure(
                CoexistenceCategory.L2CAP_CONNECT_FAILED,
                CoexistencePhase.L2CAP_CONNECTION,
                _safe_error_detail(error),
            ) from error
        try:
            local_endpoint = sock.getsockname()
            if (
                not isinstance(local_endpoint, tuple)
                or not local_endpoint
                or not isinstance(local_endpoint[0], str)
            ):
                raise ValueError("unexpected Bluetooth socket endpoint")
            selected_adapter = BluetoothAddress.parse(local_address)
            routed_adapter = BluetoothAddress.parse(local_endpoint[0])
        except (InvalidBluetoothAddressError, ValueError, OSError) as error:
            self.close()
            raise CoexistenceFailure(
                CoexistenceCategory.L2CAP_ROUTE_MISMATCH,
                CoexistencePhase.L2CAP_CONNECTION,
                _safe_error_detail(error),
            ) from error
        if routed_adapter != selected_adapter:
            self.close()
            raise CoexistenceFailure(
                CoexistenceCategory.L2CAP_ROUTE_MISMATCH,
                CoexistencePhase.L2CAP_CONNECTION,
                "connected socket used a different local adapter",
            )

    @asynccontextmanager
    async def collect(self) -> AsyncIterator[KernelL2CAPTransport]:
        if self._socket is None or self._closed:
            raise RuntimeError("L2CAP transport is not open")
        if self._collecting:
            raise RuntimeError("L2CAP collection is already active")
        self._collecting = True
        try:
            yield self
        finally:
            self._collecting = False

    def send_handshake_request(self) -> None:
        if self._application_payloads_sent != 0:
            raise AAPHandshakeError("AAP handshake request was already sent")
        self._send(AAP_HANDSHAKE_REQUEST)
        self._application_payloads_sent = 1

    def send_heart_rate_command(self, command: HeartRateCommand) -> None:
        if not isinstance(command, HeartRateCommand):
            raise TypeError("command must be a HeartRateCommand")
        if self._application_payloads_sent < 1:
            raise RuntimeError("AAP handshake has not been sent")
        if command is HeartRateCommand.STOP_HR:
            self.disarm_hr_stream_observation()
        self._send(command.payload)
        self._application_payloads_sent += 1

    async def receive(self, timeout: float) -> bytes:
        if not self._collecting or self._socket is None or self._closed:
            raise RuntimeError("AAP receive collection is not active")
        if timeout <= 0:
            raise TimeoutError

        def receive_one() -> bytes:
            assert self._socket is not None
            self._socket.settimeout(timeout)
            return self._socket.recv(self._receive_size)

        receive_task = asyncio.create_task(asyncio.to_thread(receive_one))
        try:
            frame = await asyncio.shield(receive_task)
        except asyncio.CancelledError:
            # A worker must not remain blocked in recv while canonical HR stop
            # commands use the same socket. The socket timeout bounds this wait.
            try:
                await receive_task
            except BaseException:
                pass
            raise
        except TimeoutError:
            raise TimeoutError from None
        if not frame:
            raise ConnectionError("AAP L2CAP channel closed by peer")
        received = bytes(frame)
        if self._hr_stream_observation_active:
            self._hr_stream_frames_observed += 1
            if HEART_RATE_MARKER in received:
                self._hr_stream_frames_with_marker += 1
            if (
                len(self._hr_stream_frame_summaries)
                < self._stream_summary_limit
            ):
                self._hr_stream_frame_summaries.append(
                    ControlFrameSummary.from_frame(received)
                )
        return received

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _send(self, payload: bytes) -> None:
        if not self._collecting or self._socket is None or self._closed:
            raise RuntimeError("AAP send collection is not active")
        sent = self._socket.send(payload)
        if sent != len(payload):
            raise ConnectionError("incomplete AAP L2CAP SDU send")

    def _required_constant(self, name: str) -> int:
        value = getattr(self._socket_module, name, None)
        if not isinstance(value, int):
            raise RuntimeError(f"host Python socket API lacks {name}")
        return value


