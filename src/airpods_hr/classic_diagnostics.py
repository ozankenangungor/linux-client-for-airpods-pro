"""Secret-safe Classic runtime parity and controller-state diagnostics.

The live observer issues only HCI Read commands that Bumble 0.0.234 models.
It deliberately omits controller and peer addresses, raw EIR data, local-name
bytes, packets, and credentials from its result.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RuntimeNameProfile(StrEnum):
    """Reviewed local-name choices for a controlled single-variable A/B test."""

    PROJECT_DEFAULT = "project-default"
    LEGACY_POC = "legacy-poc"


_RUNTIME_NAMES = {
    RuntimeNameProfile.PROJECT_DEFAULT: "airpods-hr authentication probe",
    RuntimeNameProfile.LEGACY_POC: "AirPods-RE",
}


def runtime_name_for_profile(profile: RuntimeNameProfile) -> str:
    """Return a project-owned name without accepting arbitrary live input."""

    try:
        return _RUNTIME_NAMES[RuntimeNameProfile(profile)]
    except (KeyError, ValueError) as error:
        raise ValueError("unsupported Classic runtime-name profile") from error


def local_name_matches_profile(
    value: object,
    profile: RuntimeNameProfile,
) -> bool | None:
    """Compare a Bumble HCI local-name value without retaining its contents.

    Bumble 0.0.234 exposes the 248-octet Read Local Name return field as
    ``bytes``.  Tests and controlled adapters may also provide the mapped
    ``str`` form.  Both forms are NUL-terminated; malformed values are treated
    as unavailable rather than rendered or retained.
    """

    normalized: str
    if isinstance(value, bytes):
        try:
            normalized = value.split(b"\x00", 1)[0].decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(value, str):
        normalized = value.split("\x00", 1)[0]
    else:
        return None

    return normalized == runtime_name_for_profile(profile)


@dataclass(frozen=True, slots=True)
class DeviceConfigurationAudit:
    """Allowlisted DeviceConfiguration state used for parity review."""

    runtime_name_profile: RuntimeNameProfile
    classic_enabled: bool
    le_enabled: bool
    configured_address_is_default: bool
    class_of_device: int
    classic_sc_enabled: bool
    classic_ssp_enabled: bool
    classic_smp_enabled: bool
    classic_accept_any: bool
    classic_interlaced_scan_enabled: bool
    connectable: bool
    discoverable: bool
    io_capability: int
    gap_service_enabled: bool
    gatt_service_enabled: bool
    enhanced_retransmission_supported: bool
    l2cap_extended_features: tuple[int, ...]
    config_keystore_present: bool
    keystore_available_before_power_on: bool


def audit_device_configuration(
    config: object,
    *,
    runtime_name_profile: RuntimeNameProfile,
    keystore_available_before_power_on: bool,
) -> DeviceConfigurationAudit:
    """Snapshot reviewed fields without retaining names, addresses, or secrets."""

    from bumble.device import DEVICE_DEFAULT_ADDRESS
    from bumble.hci import Address

    return DeviceConfigurationAudit(
        runtime_name_profile=RuntimeNameProfile(runtime_name_profile),
        classic_enabled=bool(config.classic_enabled),
        le_enabled=bool(config.le_enabled),
        configured_address_is_default=(
            config.address == Address(DEVICE_DEFAULT_ADDRESS)
        ),
        class_of_device=int(config.class_of_device),
        classic_sc_enabled=bool(config.classic_sc_enabled),
        classic_ssp_enabled=bool(config.classic_ssp_enabled),
        classic_smp_enabled=bool(config.classic_smp_enabled),
        classic_accept_any=bool(config.classic_accept_any),
        classic_interlaced_scan_enabled=bool(
            config.classic_interlaced_scan_enabled
        ),
        connectable=bool(config.connectable),
        discoverable=bool(config.discoverable),
        io_capability=int(config.io_capability),
        gap_service_enabled=bool(config.gap_service_enabled),
        gatt_service_enabled=bool(config.gatt_service_enabled),
        enhanced_retransmission_supported=bool(
            config.enhanced_retransmission_supported
        ),
        l2cap_extended_features=tuple(
            int(value) for value in config.l2cap_extended_features
        ),
        config_keystore_present=config.keystore is not None,
        keystore_available_before_power_on=keystore_available_before_power_on,
    )


# Source-audit facts for Bumble 0.0.234. These are names only; they contain no
# controller identity or packet data.
POWER_ON_CLASSIC_WRITES: tuple[str, ...] = (
    "local_name",
    "class_of_device",
    "simple_pairing_mode",
    "secure_connections_host_support",
    "scan_enable",
    "extended_inquiry_response",
    "page_scan_type_if_supported",
    "inquiry_scan_type_if_supported",
)

POWER_ON_NOT_EXPLICITLY_WRITTEN: tuple[str, ...] = (
    "authentication_enable",
    "connection_accept_timeout",
    "default_link_policy",
    "page_timeout",
    "page_scan_activity",
    "inquiry_scan_activity",
    "voice_setting",
)


