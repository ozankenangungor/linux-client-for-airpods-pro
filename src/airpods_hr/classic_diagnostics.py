"""Secret-safe Classic runtime parity and controller-state diagnostics.

The live observer issues only HCI Read commands that Bumble 0.0.234 models.
It deliberately omits controller and peer addresses, raw EIR data, local-name
bytes, packets, and credentials from its result.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from airpods_hr import _airpods_aap_core as _native


class RuntimeNameProfile(StrEnum):
    """Reviewed local-name choices for a controlled single-variable A/B test."""

    PROJECT_DEFAULT = "project-default"
    LEGACY_POC = "legacy-poc"


def runtime_name_for_profile(profile: RuntimeNameProfile) -> str:
    """Return a project-owned name without accepting arbitrary live input."""

    try:
        return _native.diagnostic_runtime_name(profile)
    except ValueError as error:
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

    if not isinstance(value, (bytes, str)):
        return None
    return _native.diagnostic_local_name_matches(value, profile)


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

    facts = _native.diagnostic_classic_audit(
        runtime_name_profile,
        [
            bool(config.classic_enabled),
            bool(config.le_enabled),
            config.address == Address(DEVICE_DEFAULT_ADDRESS),
            bool(config.classic_sc_enabled),
            bool(config.classic_ssp_enabled),
            bool(config.classic_smp_enabled),
            bool(config.classic_accept_any),
            bool(config.classic_interlaced_scan_enabled),
            bool(config.connectable),
            bool(config.discoverable),
            bool(config.gap_service_enabled),
            bool(config.gatt_service_enabled),
            bool(config.enhanced_retransmission_supported),
            config.keystore is not None,
            keystore_available_before_power_on,
        ],
        int(config.class_of_device),
        int(config.io_capability),
        [int(value) for value in config.l2cap_extended_features],
    )
    facts["runtime_name_profile"] = RuntimeNameProfile(facts["runtime_name_profile"])
    facts["l2cap_extended_features"] = tuple(facts["l2cap_extended_features"])
    return DeviceConfigurationAudit(**facts)


# Source-audit facts for Bumble 0.0.234. These are names only; they contain no
# controller identity or packet data.
POWER_ON_CLASSIC_WRITES, POWER_ON_NOT_EXPLICITLY_WRITTEN = (
    tuple(group) for group in _native.diagnostic_power_on_facts()
)


@dataclass(frozen=True, slots=True)
class ClassicHostStateSnapshot:
    """Allowlisted configured and observed Classic controller state."""

    runtime_name_profile: RuntimeNameProfile
    classic_enabled: bool
    le_enabled: bool
    connectable: bool
    discoverable: bool
    configured_ssp_enabled: bool
    configured_sc_enabled: bool
    configured_io_capability: int
    observed_local_name_matches_profile: bool | None = None
    observed_class_of_device: int | None = None
    observed_authentication_enable: int | None = None
    observed_simple_pairing_mode: int | None = None
    observed_secure_connections_host_support: int | None = None
    observed_scan_enable: int | None = None
    observed_page_timeout: int | None = None
    observed_page_scan_type: int | None = None
    observed_page_scan_interval: int | None = None
    observed_page_scan_window: int | None = None
    observed_default_link_policy: int | None = None
    unavailable_fields: tuple[str, ...] = ()


class _ReadableBumbleDevice(Protocol):
    config: object
    classic_enabled: bool
    le_enabled: bool
    connectable: bool
    discoverable: bool
    classic_ssp_enabled: bool
    classic_sc_enabled: bool
    host: object

    async def send_sync_command(self, command: object) -> object: ...


class ClassicHostStateObserver:
    """Best-effort read-only HCI snapshot for the temporary Bumble Device."""

    _READS = (
        ("local_name_matches_profile", "HCI_Read_Local_Name_Command"),
        ("class_of_device", "HCI_Read_Class_Of_Device_Command"),
        ("authentication_enable", "HCI_Read_Authentication_Enable_Command"),
        ("simple_pairing_mode", "HCI_Read_Simple_Pairing_Mode_Command"),
        (
            "secure_connections_host_support",
            "HCI_Read_Secure_Connections_Host_Support_Command",
        ),
        ("scan_enable", "HCI_Read_Scan_Enable_Command"),
        ("page_timeout", "HCI_Read_Page_Timeout_Command"),
        ("page_scan_type", "HCI_Read_Page_Scan_Type_Command"),
        ("page_scan_activity", "HCI_Read_Page_Scan_Activity_Command"),
        (
            "default_link_policy",
            "HCI_Read_Default_Link_Policy_Settings_Command",
        ),
    )

    async def capture(
        self,
        device: _ReadableBumbleDevice,
        runtime_name_profile: RuntimeNameProfile,
    ) -> ClassicHostStateSnapshot:
        """Read supported fields; failures become allowlisted availability flags."""

        from bumble import hci

        values: dict[str, int | bool] = {}
        unavailable: list[str] = []
        supports_command = getattr(device.host, "supports_command", None)
        for field_name, command_name in self._READS:
            command_type = getattr(hci, command_name, None)
            if command_type is None:
                unavailable.append(field_name)
                continue
            command = command_type()
            if callable(supports_command) and not supports_command(command.op_code):
                unavailable.append(field_name)
                continue
            try:
                response = await device.send_sync_command(command)
            except Exception:
                unavailable.append(field_name)
                continue

            if field_name == "local_name_matches_profile":
                matches = local_name_matches_profile(
                    response.local_name,
                    runtime_name_profile,
                )
                if matches is None:
                    unavailable.append(field_name)
                else:
                    values[field_name] = matches
            elif field_name == "page_scan_activity":
                values["page_scan_interval"] = int(response.page_scan_interval)
                values["page_scan_window"] = int(response.page_scan_window)
            else:
                values[field_name] = int(getattr(response, field_name))

        facts = _native.diagnostic_classic_host_snapshot(
            runtime_name_profile,
            [bool(device.classic_enabled), bool(device.le_enabled),
             bool(device.connectable), bool(device.discoverable),
             bool(device.classic_ssp_enabled), bool(device.classic_sc_enabled)],
            int(device.config.io_capability),
            values.get("local_name_matches_profile"),
            [values.get(name) for name in (
                "class_of_device", "authentication_enable", "simple_pairing_mode",
                "secure_connections_host_support", "scan_enable", "page_timeout",
                "page_scan_type", "page_scan_interval", "page_scan_window",
                "default_link_policy",
            )],
            unavailable,
        )
        facts["runtime_name_profile"] = RuntimeNameProfile(facts["runtime_name_profile"])
        facts["unavailable_fields"] = tuple(facts["unavailable_fields"])
        return ClassicHostStateSnapshot(**facts)
