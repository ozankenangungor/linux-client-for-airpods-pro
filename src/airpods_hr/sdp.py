"""Temporary SDP compatibility records for the AirPods AAP session."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from typing import Protocol

from bumble import sdp
from bumble.core import (
    BT_AUDIO_SOURCE_SERVICE,
    BT_AV_REMOTE_CONTROL_SERVICE,
    BT_AV_REMOTE_CONTROL_TARGET_SERVICE,
    BT_AVDTP_PROTOCOL_ID,
    BT_HANDSFREE_AUDIO_GATEWAY_SERVICE,
    BT_L2CAP_PROTOCOL_ID,
    BT_PNP_INFORMATION_SERVICE,
    BT_RFCOMM_PROTOCOL_ID,
)

from airpods_hr.discovery import AirPodsCandidate

REQUIRED_SDP_COMPATIBILITY_RECORDS: tuple[str, ...] = (
    "PnPInformation",
    "HandsfreeAudioGateway",
    "AudioSource",
    "A/V RemoteControlTarget",
)

PNP_INFORMATION_HANDLE = 0x00010001
HANDS_FREE_AUDIO_GATEWAY_HANDLE = 0x00010002
AUDIO_SOURCE_HANDLE = 0x00010003
AVRCP_TARGET_HANDLE = 0x00010004
HANDS_FREE_RFCOMM_CHANNEL = 13
AVDTP_L2CAP_PSM = 0x0019
AVDTP_VERSION = 0x0103
AVRCP_VERSION = 0x0106
PNP_VENDOR_ID_SOURCE_USB = 0x0002

PNP_VENDOR_ID_ATTRIBUTE_ID = 0x0201
PNP_PRODUCT_ID_ATTRIBUTE_ID = 0x0202
PNP_VERSION_ATTRIBUTE_ID = 0x0203
PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID = 0x0205

_USB_MODALIAS = re.compile(
    r"usb:v(?P<vendor>[0-9A-Fa-f]{4})"
    r"p(?P<product>[0-9A-Fa-f]{4})"
    r"d(?P<version>[0-9A-Fa-f]{4})"
)


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
        if modalias is None:
            raise AdapterIdentityError("BlueZ adapter has no Modalias")
        match = _USB_MODALIAS.fullmatch(modalias)
        if match is None:
            raise AdapterIdentityError(
                "BlueZ adapter Modalias is not a supported USB identity"
            )
        return cls(
            vendor_id=int(match.group("vendor"), 16),
            product_id=int(match.group("product"), 16),
            version=int(match.group("version"), 16),
        )


def _attribute(attribute_id: int, value: sdp.DataElement) -> sdp.ServiceAttribute:
    return sdp.ServiceAttribute(attribute_id, value)


def _handle(value: int) -> sdp.ServiceAttribute:
    return _attribute(
        sdp.SDP_SERVICE_RECORD_HANDLE_ATTRIBUTE_ID,
        sdp.DataElement.unsigned_integer_32(value),
    )


def _service_class(value: object) -> sdp.ServiceAttribute:
    return _attribute(
        sdp.SDP_SERVICE_CLASS_ID_LIST_ATTRIBUTE_ID,
        sdp.DataElement.sequence([sdp.DataElement.uuid(value)]),
    )


def build_sdp_compatibility_records(
    identity: USBAdapterIdentity,
) -> dict[int, sdp.Server.Service]:
    """Build exactly the four records proven by earlier experiments."""

    pnp = sdp.Server.Service(
        [
            _handle(PNP_INFORMATION_HANDLE),
            _service_class(BT_PNP_INFORMATION_SERVICE),
            _attribute(
                PNP_VENDOR_ID_ATTRIBUTE_ID,
                sdp.DataElement.unsigned_integer_16(identity.vendor_id),
            ),
            _attribute(
                PNP_PRODUCT_ID_ATTRIBUTE_ID,
                sdp.DataElement.unsigned_integer_16(identity.product_id),
            ),
            _attribute(
                PNP_VERSION_ATTRIBUTE_ID,
                sdp.DataElement.unsigned_integer_16(identity.version),
            ),
            _attribute(
                PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID,
                sdp.DataElement.unsigned_integer_16(PNP_VENDOR_ID_SOURCE_USB),
            ),
        ]
    )
    hands_free = sdp.Server.Service(
        [
            _handle(HANDS_FREE_AUDIO_GATEWAY_HANDLE),
            _service_class(BT_HANDSFREE_AUDIO_GATEWAY_SERVICE),
            _attribute(
                sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
                sdp.DataElement.sequence(
                    [
                        sdp.DataElement.sequence(
                            [sdp.DataElement.uuid(BT_L2CAP_PROTOCOL_ID)]
                        ),
                        sdp.DataElement.sequence(
                            [
                                sdp.DataElement.uuid(BT_RFCOMM_PROTOCOL_ID),
                                sdp.DataElement.unsigned_integer_8(
                                    HANDS_FREE_RFCOMM_CHANNEL
                                ),
                            ]
                        ),
                    ]
                ),
            ),
        ]
    )
    audio_source = sdp.Server.Service(
        [
            _handle(AUDIO_SOURCE_HANDLE),
            _service_class(BT_AUDIO_SOURCE_SERVICE),
            _attribute(
                sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID,
                sdp.DataElement.sequence(
                    [
                        sdp.DataElement.sequence(
                            [
                                sdp.DataElement.uuid(BT_L2CAP_PROTOCOL_ID),
                                sdp.DataElement.unsigned_integer_16(
                                    AVDTP_L2CAP_PSM
                                ),
                            ]
                        ),
                        sdp.DataElement.sequence(
                            [
                                sdp.DataElement.uuid(BT_AVDTP_PROTOCOL_ID),
                                sdp.DataElement.unsigned_integer_16(AVDTP_VERSION),
                            ]
                        ),
                    ]
                ),
            ),
        ]
    )
    avrcp_target = sdp.Server.Service(
        [
            _handle(AVRCP_TARGET_HANDLE),
            _service_class(BT_AV_REMOTE_CONTROL_TARGET_SERVICE),
            _attribute(
                sdp.SDP_BLUETOOTH_PROFILE_DESCRIPTOR_LIST_ATTRIBUTE_ID,
                sdp.DataElement.sequence(
                    [
                        sdp.DataElement.sequence(
                            [
                                sdp.DataElement.uuid(
                                    BT_AV_REMOTE_CONTROL_SERVICE
                                ),
                                sdp.DataElement.unsigned_integer_16(AVRCP_VERSION),
                            ]
                        )
                    ]
                ),
            ),
        ]
    )
    return {
        PNP_INFORMATION_HANDLE: pnp,
        HANDS_FREE_AUDIO_GATEWAY_HANDLE: hands_free,
        AUDIO_SOURCE_HANDLE: audio_source,
        AVRCP_TARGET_HANDLE: avrcp_target,
    }


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
