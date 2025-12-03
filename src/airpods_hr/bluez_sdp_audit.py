"""Read-only local BlueZ SDP comparison for the coexistence probe."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from airpods_hr.bluez_coexistence import BlueZCoexistenceState
from airpods_hr.sdp import (
    AVDTP_L2CAP_PSM,
    AVDTP_VERSION,
    AVRCP_VERSION,
    HANDS_FREE_RFCOMM_CHANNEL,
    PNP_VENDOR_ID_SOURCE_USB,
    AdapterIdentityError,
    USBAdapterIdentity,
    build_bluez_sdp_service_records,
)


class SDPComparisonStatus(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    NOT_OBSERVABLE = "unknown/not-observable"


@dataclass(frozen=True, slots=True)
class ObservedSDPAttribute:
    """One optional value from a read-only structured local SDP inspector."""

    observable: bool = False
    value: int | None = None


@dataclass(frozen=True, slots=True)
class ObservedLocalSDPRecord:
    """Allowlisted local SDP attributes; no free-form record content."""

    attributes: Mapping[str, ObservedSDPAttribute] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True, slots=True)
class LocalSDPInspection:
    """Structured output from an optional, separately reviewed inspector."""

    records_by_uuid: Mapping[str, ObservedLocalSDPRecord]


@dataclass(frozen=True, slots=True)
class SDPAuditAttribute:
    name: str
    expected_value: int | None
    observed_value: int | None
    status: SDPComparisonStatus


@dataclass(frozen=True, slots=True)
class SDPAuditRecord:
    name: str
    service_class_uuid_present: bool
    attributes: tuple[SDPAuditAttribute, ...]


@dataclass(frozen=True, slots=True)
class BlueZSDPAuditResult:
    records: tuple[SDPAuditRecord, ...]
    detailed_attribute_inspection_available: bool

    @property
    def full_record_equivalence(self) -> SDPComparisonStatus:
        statuses = tuple(
            attribute.status
            for record in self.records
            for attribute in record.attributes
        )
        if any(status is SDPComparisonStatus.MISMATCH for status in statuses):
            return SDPComparisonStatus.MISMATCH
        if not all(record.service_class_uuid_present for record in self.records):
            return SDPComparisonStatus.MISMATCH
        if statuses and all(
            status is SDPComparisonStatus.MATCH for status in statuses
        ):
            return SDPComparisonStatus.MATCH
        return SDPComparisonStatus.NOT_OBSERVABLE


def _expected_attributes(
    identity: USBAdapterIdentity | None,
) -> Mapping[str, tuple[tuple[str, int | None], ...]]:
    return {
        "PnPInformation": (
            ("pnp_vendor_id", None if identity is None else identity.vendor_id),
            ("pnp_product_id", None if identity is None else identity.product_id),
            ("pnp_version", None if identity is None else identity.version),
            ("pnp_vendor_id_source", PNP_VENDOR_ID_SOURCE_USB),
        ),
        "HandsfreeAudioGateway": (
            ("rfcomm_channel", HANDS_FREE_RFCOMM_CHANNEL),
        ),
        "AudioSource": (
            ("l2cap_psm", AVDTP_L2CAP_PSM),
            ("avdtp_version", AVDTP_VERSION),
        ),
        "A/V RemoteControlTarget": (
            ("avrcp_profile_version", AVRCP_VERSION),
        ),
    }


def _compare_attribute(
    observed: ObservedSDPAttribute | None,
    expected: int | None,
) -> SDPComparisonStatus:
    if expected is None or observed is None or not observed.observable:
        return SDPComparisonStatus.NOT_OBSERVABLE
    if observed.value == expected:
        return SDPComparisonStatus.MATCH
    return SDPComparisonStatus.MISMATCH


def audit_bluez_sdp_identity(
    state: BlueZCoexistenceState,
    inspection: LocalSDPInspection | None = None,
) -> BlueZSDPAuditResult:
    """Compare only evidence available through read-only structured inputs."""

    try:
        identity = USBAdapterIdentity.from_bluez_modalias(
            state.candidate.adapter_modalias
        )
    except AdapterIdentityError:
        identity = None
    expected_attributes = _expected_attributes(identity)
    records = build_bluez_sdp_service_records(
        identity or USBAdapterIdentity(0, 0, 0)
    )
    comparisons: list[SDPAuditRecord] = []
    for expected_record in records:
        observed_record = (
            inspection.records_by_uuid.get(expected_record.uuid)
            if inspection is not None
            else None
        )
        attributes = tuple(
            SDPAuditAttribute(
                name,
                expected,
                (
                    observed_record.attributes[name].value
                    if observed_record is not None
                    and name in observed_record.attributes
                    and observed_record.attributes[name].observable
                    else None
                ),
                _compare_attribute(
                    (
                        observed_record.attributes.get(name)
                        if observed_record is not None
                        else None
                    ),
                    expected,
                ),
            )
            for name, expected in expected_attributes[expected_record.name]
        )
        comparisons.append(
            SDPAuditRecord(
                name=expected_record.name,
                service_class_uuid_present=(
                    expected_record.uuid in state.adapter_uuids
                ),
                attributes=attributes,
            )
        )
    return BlueZSDPAuditResult(
        records=tuple(comparisons),
        detailed_attribute_inspection_available=inspection is not None,
    )
