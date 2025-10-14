"""Temporary SDP compatibility records for the AirPods AAP session."""

from __future__ import annotations

import re


from dataclasses import dataclass


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


