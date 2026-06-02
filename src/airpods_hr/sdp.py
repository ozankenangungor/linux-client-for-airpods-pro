"""Temporary SDP compatibility records for the AirPods AAP session."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from typing import Protocol

from bumble import sdp
from bumble.core import UUID
from airpods_hr import _airpods_aap_core as _sdp_core

from airpods_hr.discovery import AirPodsCandidate

_CONSTANTS = dict(_sdp_core.sdp_constants())
REQUIRED_SDP_COMPATIBILITY_RECORDS: tuple[str, ...] = tuple(
    spec["name"] for spec in _sdp_core.sdp_canonical_records(0, 0, 0)
)
PNP_INFORMATION_HANDLE = _CONSTANTS["PNP_INFORMATION_HANDLE"]
HANDS_FREE_AUDIO_GATEWAY_HANDLE = _CONSTANTS["HANDS_FREE_AUDIO_GATEWAY_HANDLE"]
AUDIO_SOURCE_HANDLE = _CONSTANTS["AUDIO_SOURCE_HANDLE"]
AVRCP_TARGET_HANDLE = _CONSTANTS["AVRCP_TARGET_HANDLE"]
HANDS_FREE_RFCOMM_CHANNEL = _CONSTANTS["HANDS_FREE_RFCOMM_CHANNEL"]
AVDTP_L2CAP_PSM = _CONSTANTS["AVDTP_L2CAP_PSM"]
AVDTP_VERSION = _CONSTANTS["AVDTP_VERSION"]
AVRCP_VERSION = _CONSTANTS["AVRCP_VERSION"]
PNP_VENDOR_ID_SOURCE_USB = _CONSTANTS["PNP_VENDOR_ID_SOURCE_USB"]
PNP_VENDOR_ID_ATTRIBUTE_ID = _CONSTANTS["PNP_VENDOR_ID_ATTRIBUTE_ID"]
PNP_PRODUCT_ID_ATTRIBUTE_ID = _CONSTANTS["PNP_PRODUCT_ID_ATTRIBUTE_ID"]
PNP_VERSION_ATTRIBUTE_ID = _CONSTANTS["PNP_VERSION_ATTRIBUTE_ID"]
PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID = _CONSTANTS["PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID"]


@dataclass(frozen=True, slots=True)
class BlueZSDPServiceRecord:
    """One BlueZ ProfileManager registration derived from the known profile."""

    name: str
    uuid: str
    service_record: str


class SDPCompatibilityError(RuntimeError):
    """Base error for the temporary SDP compatibility profile."""


class AdapterIdentityError(SDPCompatibilityError):
    """Raised when Adapter1 Modalias cannot provide supported PnP identity."""


class TemporarySDPRuntime(Protocol):
    """Narrow runtime boundary needed to install an in-memory SDP profile."""

    def temporary_sdp_records(
        self, records: object
    ) -> AbstractContextManager[None]: ...

    def observe_sdp(self, observer: object) -> AbstractContextManager[None]: ...


@dataclass(frozen=True, slots=True)
class USBAdapterIdentity:
    """Sanitized USB identity parsed from a BlueZ Adapter1 Modalias."""

    vendor_id: int
    product_id: int
    version: int

    @classmethod
    def from_bluez_modalias(cls, modalias: str | None) -> USBAdapterIdentity:
        try:
            vendor_id, product_id, version = _sdp_core.sdp_parse_bluez_modalias(modalias)
        except ValueError as error:
            raise AdapterIdentityError(str(error)) from error
        return cls(vendor_id, product_id, version)


def _bumble_element(spec: dict[str, object]) -> sdp.DataElement:
    kind = spec["kind"]
    value = spec["value"]
    if kind == "sequence":
        return sdp.DataElement.sequence([_bumble_element(item) for item in value])
    if kind == "uuid16":
        return sdp.DataElement.uuid(UUID.from_16_bits(value))
    if kind == "u8":
        return sdp.DataElement.unsigned_integer_8(value)
    if kind == "u16":
        return sdp.DataElement.unsigned_integer_16(value)
    return sdp.DataElement.unsigned_integer_32(value)


def build_sdp_compatibility_records(
    identity: USBAdapterIdentity,
) -> dict[int, sdp.Server.Service]:
    """Build the four native-specified records as Bumble runtime objects."""

    return {
        spec["handle"]: sdp.Server.Service(
            [
                sdp.ServiceAttribute(attribute_id, _bumble_element(element))
                for attribute_id, element in spec["attributes"]
            ]
        )
        for spec in _sdp_core.sdp_canonical_records(
            identity.vendor_id, identity.product_id, identity.version
        )
    }


REQUIRED_BLUEZ_SDP_COMPATIBILITY_UUIDS = frozenset(
    uuid for _, uuid, _ in _sdp_core.sdp_bluez_xml_records(0, 0, 0)
)


def build_bluez_sdp_service_records(
    identity: USBAdapterIdentity,
) -> tuple[BlueZSDPServiceRecord, ...]:
    """Adapt native ProfileManager XML to the existing Python model."""

    return tuple(
        BlueZSDPServiceRecord(*record)
        for record in _sdp_core.sdp_bluez_xml_records(
            identity.vendor_id, identity.product_id, identity.version
        )
    )


@dataclass(frozen=True, slots=True)
class PreparedSDPCompatibilityProfile:
    """Candidate-derived SDP records ready before controller handoff."""

    records: dict[int, sdp.Server.Service]
    installed_callback: Callable[[], None] | None = None
    diagnostics: object | None = None

    @contextmanager
    def activate(self, runtime: TemporarySDPRuntime) -> Iterator[None]:
        with runtime.temporary_sdp_records(self.records):
            observer_context = (
                runtime.observe_sdp(self.diagnostics)
                if self.diagnostics is not None
                else nullcontext()
            )
            with observer_context:
                if self.installed_callback is not None:
                    self.installed_callback()
                yield


class SDPCompatibilityProfile:
    """Prepare adapter-specific records before a BR/EDR runtime is acquired."""

    def __init__(
        self,
        *,
        installed_callback: Callable[[], None] | None = None,
        diagnostics: object | None = None,
    ) -> None:
        self._installed_callback = installed_callback
        self._diagnostics = diagnostics

    def prepare(
        self, candidate: AirPodsCandidate
    ) -> PreparedSDPCompatibilityProfile:
        identity = USBAdapterIdentity.from_bluez_modalias(
            candidate.adapter_modalias
        )
        return PreparedSDPCompatibilityProfile(
            records=build_sdp_compatibility_records(identity),
            installed_callback=self._installed_callback,
            diagnostics=self._diagnostics,
        )
