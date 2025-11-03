#!/usr/bin/env python3.14
"""Opt-in probe for the known AAP handshake and descriptor evidence."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TextIO

from airpods_hr.aap import (
    AAPDescriptorObservationTimeoutError,
    AAPFrameSummary,
    AAPHandshakeError,
    AAPHandshakeProbeSession,
    AAPHandshakeSession,
    AAPProgress,
    AAPType2BFrameSummary,
)
from airpods_hr.aap_channel import (
    AAPChannel,
    AAPChannelError,
    AAPChannelProgress,
    AAPChannelSession,
)
from airpods_hr.authentication import (
    AuthenticationProgress,
    BumbleClassicRuntimeFactory,
    ClassicAuthenticationError,
    ClassicAuthenticationSession,
    create_controller_handoff_transport,
)
from airpods_hr.bluetooth import AdapterRestoreError, DBusNextBlueZBackend, HandoffError
from airpods_hr.classic_diagnostics import (
    ClassicHostStateObserver,
    ClassicHostStateSnapshot,
    RuntimeNameProfile,
)
from airpods_hr.discovery import (
    BlueZDeviceDiscovery,
    DBusNextManagedObjectsBackend,
    MultipleAirPodsCandidatesError,
    NoAirPodsCandidatesError,
)
from airpods_hr.pairing import BlueZPairingStore, PairingStoreError
from airpods_hr.sdp import SDPCompatibilityError
from airpods_hr.sdp_diagnostics import (
    ProtocolTimelineKind,
    ProtocolTimelineSnapshot,
    SDPDiagnosticsError,
    SDPDiagnosticsSnapshot,
)


LiveRunner = Callable[
    [Callable[[str], None], RuntimeNameProfile, bool], Awaitable[None]
]


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _create_host_state_observer(
    enabled: bool,
) -> ClassicHostStateObserver | None:
    """Construct the timing-affecting HCI observer only after explicit opt-in."""

    return ClassicHostStateObserver() if enabled else None


def _print_frame_summaries(
    output: Callable[[str], None],
    label: str,
    summaries: tuple[AAPFrameSummary, ...],
) -> None:
    for index, summary in enumerate(summaries, 1):
        message = f"{label} frame {index}: length={summary.length}"
        if summary.header_u16_2_3 is not None:
            message += f", header_u16_2_3=0x{summary.header_u16_2_3:04X}"
        if summary.header_u16_4_5 is not None:
            message += f", header_u16_4_5=0x{summary.header_u16_4_5:04X}"
        output(message)
        if summary.type_2b_summary is not None:
            _print_type_2b_summary(output, summary.type_2b_summary)


def _format_optional_integer(value: int | None) -> str:
    return "unavailable" if value is None else str(value)


def _format_optional_boolean(value: bool | None) -> str:
    if value is None:
        return "unavailable"
    return _yes_no(value)


def _print_type_2b_summary(
    output: Callable[[str], None], summary: AAPType2BFrameSummary
) -> None:
    output("Type-0x002B structural summary:")
    output(f"  frame length={summary.frame_length}")
    header_u8_6 = (
        "unavailable"
        if summary.header_u8_6 is None
        else f"0x{summary.header_u8_6:02X}"
    )
    output(f"  header byte 6={header_u8_6}")
    output(
        "  declared body length u16@7="
        f"{_format_optional_integer(summary.declared_body_length_u16_7_8)}"
    )
    output(
        "  actual body length after offset 17="
        f"{_format_optional_integer(summary.actual_body_length_after_offset_17)}"
    )
    output(
        "  declared/actual length consistent="
        f"{_format_optional_boolean(summary.declared_body_length_consistent)}"
    )
    output(
        "  body 17-byte aligned="
        f"{_format_optional_boolean(summary.body_aligned_to_17_bytes)}"
    )
    output(
        "  observed 17-byte record count="
        f"{_format_optional_integer(summary.record_count_17)}"
    )
    output(
        "  distinct suffix pairs="
        f"{_format_optional_integer(summary.record_suffix_distinct_count)}"
    )
    for index, suffix in enumerate(summary.record_suffix_histogram, 1):
        output(
            f"  suffix pair {index}: field_u8=0x{suffix.suffix_field_u8:02X} "
            f"field_u16=0x{suffix.suffix_field_u16:04X} count={suffix.count}"
        )
    output(
        "  unit bytes 8..13 uniform="
        f"{_format_optional_boolean(summary.unit_bytes_8_13_uniform)}"
    )


def _print_sdp_diagnostics(
    output: Callable[[str], None], snapshot: SDPDiagnosticsSnapshot
) -> None:
    output(f"SDP connection observed: {_yes_no(snapshot.sdp_connection_observed)}")
    output(f"SDP requests observed: {snapshot.sdp_requests_observed}")
    output(f"PnPInformation queries: {snapshot.pnp_information_queries}")
    output(
        "HandsfreeAudioGateway queries: "
        f"{snapshot.handsfree_audio_gateway_queries}"
    )
    output(f"AudioSource queries: {snapshot.audio_source_queries}")
    output(f"AVRCP Target queries: {snapshot.avrcp_target_queries}")
    output(f"Other SDP queries: {snapshot.other_queries}")
    output(
        "PnPInformation match served: "
        f"{_yes_no(snapshot.pnp_information_match_served)}"
    )
    output(
        "HandsfreeAudioGateway match served: "
        f"{_yes_no(snapshot.handsfree_audio_gateway_match_served)}"
    )
    output(
        "AudioSource match served: "
        f"{_yes_no(snapshot.audio_source_match_served)}"
    )
    output(
        "AVRCP Target match served: "
        f"{_yes_no(snapshot.avrcp_target_match_served)}"
    )
    output(
        "Peer L2CAP connection requests: "
        f"PSM1={snapshot.psm_1_requests}, "
        f"PSM3={snapshot.psm_3_requests}, "
        f"PSM23={snapshot.psm_23_requests}, "
        f"PSM25={snapshot.psm_25_requests}, "
        f"other={snapshot.other_psm_requests}"
    )
    _print_protocol_timeline(output, snapshot.timeline)


def _print_protocol_timeline(
    output: Callable[[str], None], snapshot: ProtocolTimelineSnapshot
) -> None:
    output(f"Protocol timeline events retained: {len(snapshot.events)}")
    output(f"Protocol timeline events observed: {snapshot.total_events}")
    labels = {
        ProtocolTimelineKind.HANDSHAKE_SENT: "Handshake sent",
        ProtocolTimelineKind.ACK_OBSERVED: "ACK observed",
        ProtocolTimelineKind.FIRST_POST_ACK_FRAME: "First post-ACK frame",
        ProtocolTimelineKind.FIRST_357_BYTE_FRAME: "First 357-byte frame",
        ProtocolTimelineKind.PSM_1_REQUEST: "PSM1 request",
        ProtocolTimelineKind.PSM_3_REQUEST: "PSM3 request",
        ProtocolTimelineKind.PSM_23_REQUEST: "PSM23 request",
        ProtocolTimelineKind.PSM_25_REQUEST: "PSM25 request",
        ProtocolTimelineKind.OTHER_PSM_REQUEST: "Other PSM request",
    }
    for kind, label in labels.items():
        event = snapshot.first(kind)
        if event is None:
            output(f"{label}: not observed")
        else:
            output(f"{label}: +{event.elapsed_seconds:.3f}s")


def _print_host_state(
    output: Callable[[str], None], snapshot: ClassicHostStateSnapshot | None
) -> None:
    if snapshot is None:
        output("Classic host-state snapshot: unavailable")
        return

    output("Classic host-state snapshot: available")
    output(f"Runtime name profile: {snapshot.runtime_name_profile.value}")
    output(f"Classic enabled: {_yes_no(snapshot.classic_enabled)}")
    output(f"LE enabled: {_yes_no(snapshot.le_enabled)}")
    output(f"Connectable configured: {_yes_no(snapshot.connectable)}")
    output(f"Discoverable configured: {_yes_no(snapshot.discoverable)}")
    output(f"SSP configured: {_yes_no(snapshot.configured_ssp_enabled)}")
    output(f"Secure Connections configured: {_yes_no(snapshot.configured_sc_enabled)}")
    output(f"IO capability: {snapshot.configured_io_capability}")
    observed = (
        (
            "Local name matches selected profile",
            snapshot.observed_local_name_matches_profile,
        ),
        ("Class of Device", snapshot.observed_class_of_device),
        ("Authentication enable", snapshot.observed_authentication_enable),
        ("Simple Pairing mode", snapshot.observed_simple_pairing_mode),
        (
            "Secure Connections host support",
            snapshot.observed_secure_connections_host_support,
        ),
        ("Scan enable", snapshot.observed_scan_enable),
        ("Page timeout", snapshot.observed_page_timeout),
        ("Page scan type", snapshot.observed_page_scan_type),
        ("Page scan interval", snapshot.observed_page_scan_interval),
        ("Page scan window", snapshot.observed_page_scan_window),
        ("Default link policy", snapshot.observed_default_link_policy),
    )
    for label, value in observed:
        if value is None:
            rendered = "unavailable"
        elif isinstance(value, bool):
            rendered = _yes_no(value)
        else:
            rendered = str(value)
        output(f"{label}: {rendered}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Experimentally perform the known AAP handshake without HR commands."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the state-changing AAP handshake experiment",
    )
    parser.add_argument(
        "--runtime-name-profile",
        choices=tuple(profile.value for profile in RuntimeNameProfile),
        default=RuntimeNameProfile.PROJECT_DEFAULT.value,
        help=(
            "select the project default or the legacy-PoC local name for a "
            "controlled single-variable A/B experiment"
        ),
    )
    parser.add_argument(
        "--classic-host-snapshot",
        action="store_true",
        help=(
            "opt in to allowlisted HCI Read commands before the Classic "
            "connection"
        ),
    )
    return parser


def _authentication_progress(output: Callable[[str], None]):
    def emit(event: AuthenticationProgress, detail: str | None) -> None:
        if event is AuthenticationProgress.DEVICE_SELECTED:
            output(f"Device: {detail or 'AirPods'}")
        elif event is AuthenticationProgress.CONNECTED:
            output("BR/EDR connection: OK")
        elif event is AuthenticationProgress.AUTHENTICATED:
            output("Authentication: OK")
        elif event is AuthenticationProgress.ENCRYPTED:
            output("Encryption: OK")
        elif event is AuthenticationProgress.DISCONNECTED:
            output("Disconnect: OK")
        elif event is AuthenticationProgress.REPLACEMENT_KEY_REPORTED:
            output("Controller reported a new/replacement link key.")

    return emit


def _channel_progress(output: Callable[[str], None]):
    def emit(event: AAPChannelProgress, channel: AAPChannel | None) -> None:
        if event is AAPChannelProgress.OPENED:
            output("AAP L2CAP channel: OPEN")
        elif event is AAPChannelProgress.CLOSED:
            output("AAP close: OK")

    return emit


def _aap_progress(output: Callable[[str], None]):
    def emit(event: AAPProgress) -> None:
        if event is AAPProgress.SDP_INSTALLED:
            output("SDP compatibility records: installed")
        elif event is AAPProgress.HANDSHAKE_SENT:
            output("AAP handshake request: sent")
        elif event is AAPProgress.ACK_OBSERVED:
            output("AAP handshake ACK: OK")
        elif event is AAPProgress.DESCRIPTORS_OBSERVED:
            output("Sensor framework descriptor: observed")
            output("HeartRateService: observed")
            output("HR commands sent: no")

    return emit


async def run_live_probe(
    output: Callable[[str], None],
    runtime_name_profile: RuntimeNameProfile = RuntimeNameProfile.PROJECT_DEFAULT,
    classic_host_snapshot: bool = False,
) -> None:
    runtime_name_profile = RuntimeNameProfile(runtime_name_profile)
    output(f"Runtime name profile: {runtime_name_profile.value}")
    output(
        "Classic host-state snapshot: "
        + ("enabled" if classic_host_snapshot else "disabled")
    )
    discovery_backend = DBusNextManagedObjectsBackend()
    bluez_backend = DBusNextBlueZBackend()
    try:
        await discovery_backend.connect()
        await bluez_backend.connect()
        handoff, transport = create_controller_handoff_transport(bluez_backend)
        await transport.ensure_available()
        secure_session = ClassicAuthenticationSession(
            BlueZDeviceDiscovery(discovery_backend),
            BlueZPairingStore(),
            handoff,
            BumbleClassicRuntimeFactory(
                runtime_name_profile=runtime_name_profile,
                host_state_observer=_create_host_state_observer(
                    classic_host_snapshot
                ),
            ),
            progress=_authentication_progress(output),
        )
        aap_progress = _aap_progress(output)
        session = AAPHandshakeProbeSession(
            secure_session,
            AAPChannelSession(progress=_channel_progress(output)),
            AAPHandshakeSession(progress=aap_progress),
            progress=aap_progress,
        )
        result = await session.run()
        if classic_host_snapshot:
            _print_host_state(output, result.host_state_snapshot)
        output("BlueZ restoration: OK")
    finally:
        discovery_backend.close()
        bluez_backend.close()


async def run_probe(
    *,
    execute: bool,
    output: Callable[[str], None] = print,
    live_runner: LiveRunner = run_live_probe,
    runtime_name_profile: RuntimeNameProfile = RuntimeNameProfile.PROJECT_DEFAULT,
    classic_host_snapshot: bool = False,
) -> int:
    runtime_name_profile = RuntimeNameProfile(runtime_name_profile)
    if not execute:
        output("DRY RUN: no Bluetooth state will be changed.")
        output(f"Runtime name profile: {runtime_name_profile.value}")
        output(
            "Classic host-state snapshot: "
            + ("enabled (not captured in dry run)" if classic_host_snapshot else "disabled")
        )
        output("Planned operations:")
        output("  1. Discover one paired AirPods candidate.")
        output("  2. Load its existing local Classic credentials.")
        output("  3. Hand the powered-down controller from BlueZ to Bumble.")
        output("  4. Power the temporary Bumble Device and install four SDP records.")
        output("  5. Connect over BR/EDR, authenticate, and enable encryption.")
        output("  6. Open AAP PSM 0x1001 with FLUSH_TIMEOUT compatibility.")
        output(
            "  7. Send one AAP handshake request and observe safe AAP/SDP summaries."
        )
        output("  8. Send no HR commands.")
        output("  9. Close, disconnect, release the controller, and restore BlueZ.")
        output("Use --execute only after reviewing the experiment.")
        return 0

    try:
        await live_runner(output, runtime_name_profile, classic_host_snapshot)
        return 0
    except NoAirPodsCandidatesError:
        output("FAIL: no paired AirPods candidate was found.")
    except MultipleAirPodsCandidatesError:
        output("FAIL: multiple paired AirPods candidates require selection.")
    except PairingStoreError:
        output("FAIL: existing local Classic credentials could not be loaded.")
    except SDPCompatibilityError:
        output("FAIL: local adapter PnP identity is unavailable or malformed.")
    except AAPDescriptorObservationTimeoutError as error:
        output("FAIL: handshake ACK arrived, but required descriptors were absent.")
        evidence = error.observation.evidence
        output(
            "Sensor framework marker: "
            f"{'yes' if evidence.sensor_framework else 'no'}"
        )
        output(
            "HeartRateService marker: "
            f"{'yes' if evidence.heart_rate_service else 'no'}"
        )
        output(f"HeartRate marker: {'yes' if evidence.heart_rate else 'no'}")
        output(
            "heartrate-access marker: "
            f"{'yes' if evidence.heartrate_access else 'no'}"
        )
        output(f"Frames before ACK: {error.observation.pre_ack_frame_count}")
        output(f"Frames after ACK: {error.observation.post_ack_frame_count}")
        output(
            "Receive frames dropped: "
            f"{error.observation.receive_frames_dropped}"
        )
        _print_frame_summaries(
            output,
            "Pre-ACK",
            error.observation.pre_ack_frame_summaries,
        )
        _print_frame_summaries(
            output,
            "Post-ACK",
            error.observation.post_ack_frame_summaries,
        )
        if error.sdp_diagnostics is not None:
            _print_sdp_diagnostics(output, error.sdp_diagnostics)
        else:
            output("SDP diagnostics unavailable.")
        if classic_host_snapshot:
            _print_host_state(output, error.host_state_snapshot)
    except SDPDiagnosticsError:
        output("FAIL: Bumble SDP diagnostics are unavailable for this build.")
    except (AAPHandshakeError, AAPChannelError):
        output("FAIL: AAP handshake probe failed; cleanup was attempted.")
    except AdapterRestoreError:
        output("FAIL: BlueZ adapter restoration reported an error.")
    except HandoffError:
        output("FAIL: controller handoff failed; cleanup was attempted.")
    except ClassicAuthenticationError:
        output("FAIL: Classic security session failed; cleanup was attempted.")
    except asyncio.CancelledError:
        raise
    except Exception:
        output("FAIL: unexpected AAP handshake probe error.")
    return 1


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit = print if stream is None else lambda message: print(message, file=stream)
    runtime_name_profile = RuntimeNameProfile(args.runtime_name_profile)

    try:
        return asyncio.run(
            run_probe(
                execute=args.execute,
                output=emit,
                runtime_name_profile=runtime_name_profile,
                classic_host_snapshot=args.classic_host_snapshot,
            )
        )
    except KeyboardInterrupt:
        emit("FAIL: interrupted; cleanup and restoration were attempted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
