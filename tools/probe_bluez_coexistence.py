#!/usr/bin/env python3.14
"""Safe-by-default BlueZ coexistence feasibility probe."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TextIO

from airpods_hr.aap import (
    AAP_FRAME_SUMMARY_LIMIT,
    AAPFrameSummary,
    AAPHandshakeSession,
    AAPProgress,
    AAPType2BFrameSummary,
    HandshakeObservation,
)
from airpods_hr.bluez_coexistence import (
    DEFAULT_DBUS_TIMEOUT,
    DEFAULT_DESCRIPTOR_TIMEOUT,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_L2CAP_CONNECT_TIMEOUT,
    BlueZCompatibilityRegistration,
    BlueZCoexistenceSession,
    CoexistenceFailure,
    CoexistenceResult,
    DBusNextBlueZCoexistenceClient,
    KernelL2CAPLocalRXObservation,
    KernelL2CAPTransport,
)
from airpods_hr.bluez_sdp_audit import (
    BlueZSDPAuditResult,
    SDPComparisonStatus,
    audit_bluez_sdp_identity,
)
from airpods_hr.heart_rate_session import (
    ControlFrameSummary,
    DEFAULT_CONTROL_SUMMARY_LIMIT,
    DEFAULT_SAMPLE_TARGET,
    DEFAULT_STREAM_TIMEOUT,
    HeartRateActivationSession,
    HeartRateProgress,
)
from airpods_hr.heartrate import HeartRateReport


LiveRunner = Callable[
    [Callable[[str], None], int, float, float, float, float, float, bool, bool],
    Awaitable[CoexistenceResult],
]
AuditRunner = Callable[
    [Callable[[str], None], float], Awaitable[BlueZSDPAuditResult]
]


async def run_live_sdp_audit(
    output: Callable[[str], None], dbus_timeout: float
) -> BlueZSDPAuditResult:
    del output
    client = DBusNextBlueZCoexistenceClient()
    try:
        await asyncio.wait_for(client.connect(), timeout=dbus_timeout)
        state = await asyncio.wait_for(client.preflight(), timeout=dbus_timeout)
        return audit_bluez_sdp_identity(state)
    finally:
        client.close()


def _print_sdp_audit(
    output: Callable[[str], None], result: BlueZSDPAuditResult
) -> None:
    def format_value(name: str, value: int | None) -> str:
        if value is None:
            return "unknown"
        if name == "rfcomm_channel":
            return str(value)
        return f"0x{value:04X}"

    output("BLUEZ SDP AUDIT: read-only local identity comparison")
    output("Adapter1.UUIDs evidence: service-class coverage only")
    attribute_source = (
        "structured local record inspector"
        if result.detailed_attribute_inspection_available
        else "unknown/not-observable through BlueZ D-Bus"
    )
    output(f"Detailed SDP attribute source: {attribute_source}")
    for record in result.records:
        output(f"  {record.name}:")
        output(
            "    service_class_uuid="
            f"{'present' if record.service_class_uuid_present else 'absent'}"
        )
        for attribute in record.attributes:
            observed = (
                "not-observable"
                if attribute.status is SDPComparisonStatus.NOT_OBSERVABLE
                else (
                    "absent"
                    if attribute.observed_value is None
                    else format_value(attribute.name, attribute.observed_value)
                )
            )
            output(
                f"    {attribute.name}: "
                f"expected={format_value(attribute.name, attribute.expected_value)}, "
                f"observed={observed}, comparison={attribute.status.value}"
            )
    output(
        "Full local SDP record equivalence="
        f"{result.full_record_equivalence.value}"
    )
    if result.full_record_equivalence is SDPComparisonStatus.NOT_OBSERVABLE:
        output("UUID coverage alone does not establish SDP record equivalence.")
    output("BLUEZ SDP AUDIT COMPLETE")


