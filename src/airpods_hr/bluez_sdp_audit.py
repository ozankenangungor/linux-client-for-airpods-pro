"""Read-only local BlueZ SDP comparison for the coexistence probe."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from airpods_hr.bluez_coexistence import BlueZCoexistenceState
from airpods_hr import _airpods_aap_core as _sdp_core
from airpods_hr.sdp import (
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
        return SDPComparisonStatus(
            _sdp_core.sdp_full_record_equivalence(
                [
                    attribute.status.value
                    for record in self.records
                    for attribute in record.attributes
                ],
                [record.service_class_uuid_present for record in self.records],
            )
        )


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
    expected_rows = _sdp_core.sdp_expected_attributes(
        None if identity is None else
        (identity.vendor_id, identity.product_id, identity.version)
    )
    records = build_bluez_sdp_service_records(
        identity or USBAdapterIdentity(0, 0, 0)
    )
    comparisons: list[SDPAuditRecord] = []
    for expected_record, expected_attributes in zip(records, expected_rows):
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
                SDPComparisonStatus(
                    _sdp_core.sdp_compare_attribute(
                        expected,
                        observed_record is not None
                        and name in observed_record.attributes
                        and observed_record.attributes[name].observable,
                        (
                            observed_record.attributes[name].value
                            if observed_record is not None
                            and name in observed_record.attributes
                            else None
                        ),
                    )
                ),
            )
            for name, expected in expected_attributes
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
