"""Hardware-independent tests for the reference handshake diagnostics."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import unittest
from contextlib import asynccontextmanager, redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from airpods_hr.aap import (
    AAP_FRAME_SUMMARY_LIMIT,
    AAP_HANDSHAKE_ACK,
    AAPDescriptorObservationTimeoutError,
    AAPFrameSummary,
    AAPHandshakeError,
    AAPHandshakeSession,
    AAPHandshakeTimeoutError,
    AAPProgress,
    DescriptorEvidence,
    HandshakeObservation,
)
from airpods_hr.aap_channel import AAPChannel, AAPChannelProgress
from airpods_hr.aap_config_diagnostics import (
    AAPConfigurationDiagnosticStrategy,
    AAPConfigureResponseMode,
    AAPL2CAPConfigurationObservation,
)
from airpods_hr.aap_local_rx_diagnostics import (
    AAPLocalRXConfigurationObservation,
    AAPLocalRXDiagnosticStrategy,
    AAPLocalRXProfile,
)
from airpods_hr.authentication import AuthenticationProgress
from airpods_hr.pairing import PairingStoreError
from airpods_hr.pre_aap_diagnostics import (
    PreAAPAAPChannelSession,
    PreAAPSequenceMode,
    PreAAPSequenceSecureSession,
)
from airpods_hr.pre_auth_diagnostics import (
    PreAuthSequenceMode,
)
from airpods_hr.reference_sdp_footprint import (
    ReferenceSDPFootprint,
    ReferenceSDPFootprintSecureSession,
    ReferenceSDPQuerySnapshot,
    ReferenceSDPQuerySummary,
)
from tools.probe_reference_handshake import (
    _ReportingHandoffTransport,
    _ReferenceAAPCompatibility,
    _aap_progress,
    _authentication_progress,
    _channel_progress,
    _print_l2cap_configuration_summary,
    _print_local_rx_configuration_summary,
    _print_post_ack_shape_summary,
    _print_pre_aap_sequence_summary,
    _print_pre_auth_sequence_summary,
    _print_sdp_query_summary,
    build_parser,
    main,
    run_live_probe,
    run_probe,
)


def observation(
    *,
    ack_observed: bool = True,
    pre_ack_frames: int = 2,
    post_ack_frames: int = 26,
    summaries: tuple[AAPFrameSummary, ...] = (),
) -> HandshakeObservation:
    return HandshakeObservation(
        ack_observed=ack_observed,
        evidence=DescriptorEvidence(
            sensor_framework=False,
            heart_rate_service=False,
            heart_rate=True,
            heartrate_access=False,
        ),
        pre_ack_frame_count=pre_ack_frames,
        post_ack_frame_count=post_ack_frames,
        receive_frames_dropped=0,
        pre_ack_frame_summaries=summaries,
    )


class FakeHandoff:
    def __init__(self) -> None:
        self.events: list[str] = []

    @asynccontextmanager
    async def acquire(self, adapter_name: str):
        self.events.append(f"enter:{adapter_name}")
        try:
            yield object()
        finally:
            self.events.append(f"exit:{adapter_name}")


class MissingACKTransport:
    def __init__(self, frames: list[bytes]) -> None:
        self.frames = list(frames)
        self.application_payloads_sent = 0
        self.dropped_frames = 0

    @asynccontextmanager
    async def collect(self):
        yield self

    def send_handshake_request(self) -> None:
        self.application_payloads_sent += 1

    async def receive(self, timeout: float) -> bytes:
        del timeout
        if not self.frames:
            raise TimeoutError
        return self.frames.pop(0)


class ReferenceProbeDryRunTests(unittest.IsolatedAsyncioTestCase):
    def test_parser_defaults_to_non_destructive_dry_run(self) -> None:
        args = build_parser().parse_args([])
        self.assertFalse(args.execute)
        self.assertFalse(args.verbose)
        self.assertEqual(args.descriptor_timeout, 3)
        self.assertEqual(
            args.aap_config_response, AAPConfigureResponseMode.PROVEN
        )
        self.assertEqual(args.sdp_footprint, ReferenceSDPFootprint.PROVEN)
        self.assertEqual(args.pre_aap_sequence, PreAAPSequenceMode.PROVEN)
        self.assertEqual(args.pre_auth_sequence, PreAuthSequenceMode.PROVEN)
        self.assertEqual(args.aap_local_rx_profile, AAPLocalRXProfile.PROVEN)

    def test_aap_local_rx_profiles_are_explicit(self) -> None:
        args = build_parser().parse_args(
            ["--aap-local-rx-profile", "kernel-default"]
        )
        self.assertEqual(
            args.aap_local_rx_profile, AAPLocalRXProfile.KERNEL_DEFAULT
        )

    def test_pre_aap_sequence_modes_are_explicit(self) -> None:
        for value, expected in (
            ("delay-only", PreAAPSequenceMode.DELAY_ONLY),
            ("bluez-l2cap-info", PreAAPSequenceMode.BLUEZ_L2CAP_INFO),
        ):
            with self.subTest(value=value):
                args = build_parser().parse_args(
                    ["--pre-aap-sequence", value]
                )
                self.assertEqual(args.pre_aap_sequence, expected)

    def test_pre_auth_sequence_modes_are_explicit(self) -> None:
        for value, expected in (
            ("delay-only", PreAuthSequenceMode.DELAY_ONLY),
            ("bluez-discovery", PreAuthSequenceMode.BLUEZ_DISCOVERY),
        ):
            with self.subTest(value=value):
                args = build_parser().parse_args(
                    ["--pre-auth-sequence", value]
                )
                self.assertEqual(args.pre_auth_sequence, expected)

    def test_bluez_like_sdp_footprint_is_explicit(self) -> None:
        args = build_parser().parse_args(["--sdp-footprint", "bluez-like"])
        self.assertEqual(
            args.sdp_footprint, ReferenceSDPFootprint.BLUEZ_LIKE
        )

    def test_kernel_mtu_only_response_mode_is_explicit(self) -> None:
        args = build_parser().parse_args(
            ["--aap-config-response", "kernel-mtu-only"]
        )
        self.assertEqual(
            args.aap_config_response,
            AAPConfigureResponseMode.KERNEL_MTU_ONLY,
        )

    def test_descriptor_timeout_bounds(self) -> None:
        self.assertEqual(
            build_parser()
            .parse_args(["--descriptor-timeout", "10"])
            .descriptor_timeout,
            10,
        )
        self.assertEqual(
            build_parser()
            .parse_args(["--descriptor-timeout", "30"])
            .descriptor_timeout,
            30,
        )
        for value in ("0", "0.9", "30.1", "31"):
            with self.subTest(value=value), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit):
                    build_parser().parse_args(["--descriptor-timeout", value])

    def test_main_dry_run_constructs_no_live_backend(self) -> None:
        output = StringIO()
        with patch(
            "tools.probe_reference_handshake.DBusNextManagedObjectsBackend"
        ) as discovery, patch(
            "tools.probe_reference_handshake.DBusNextBlueZBackend"
        ) as bluez:
            self.assertEqual(main([], stream=output), 0)
        discovery.assert_not_called()
        bluez.assert_not_called()
        rendered = output.getvalue()
        self.assertIn("DRY RUN", rendered)
        self.assertIn("existing controller-handoff/Bumble backend", rendered)
        self.assertIn("HR activation commands: none", rendered)
        self.assertIn("ACK=5s", rendered)
        self.assertIn("descriptor=3s", rendered)
        self.assertIn("AAP Configure Response mode: proven", rendered)
        self.assertIn("Reference SDP footprint: proven", rendered)
        self.assertIn("Pre-AAP sequence: proven", rendered)
        self.assertIn("Pre-auth sequence: proven", rendered)

    async def test_dry_run_never_invokes_live_runner(self) -> None:
        runner = AsyncMock()
        output: list[str] = []
        self.assertEqual(
            await run_probe(
                execute=False, output=output.append, live_runner=runner
            ),
            0,
        )
        runner.assert_not_awaited()

    async def test_dry_run_and_verbose_execute_report_configured_timeout(
        self,
    ) -> None:
        dry_output: list[str] = []
        await run_probe(
            execute=False,
            descriptor_timeout=10,
            output=dry_output.append,
        )
        self.assertIn("  descriptor=10s", dry_output)

        selected = observation()
        live_output: list[str] = []
        runner = AsyncMock(return_value=selected)
        self.assertEqual(
            await run_probe(
                execute=True,
                descriptor_timeout=30,
                verbose=True,
                output=live_output.append,
                live_runner=runner,
            ),
            0,
        )
        runner.assert_awaited_once_with(
            live_output.append,
            30,
            AAPConfigureResponseMode.PROVEN,
            ReferenceSDPFootprint.PROVEN,
            PreAAPSequenceMode.PROVEN,
            PreAuthSequenceMode.PROVEN,
            AAPLocalRXProfile.PROVEN,
        )
        self.assertIn("  descriptor=30s", live_output)

    async def test_experimental_response_mode_is_labeled_and_forwarded(
        self,
    ) -> None:
        selected = observation()
        runner = AsyncMock(return_value=selected)
        output: list[str] = []
        status = await run_probe(
            execute=True,
            descriptor_timeout=30,
            response_mode=AAPConfigureResponseMode.KERNEL_MTU_ONLY,
            output=output.append,
            live_runner=runner,
        )
        self.assertEqual(status, 0)
        runner.assert_awaited_once_with(
            output.append,
            30,
            AAPConfigureResponseMode.KERNEL_MTU_ONLY,
            ReferenceSDPFootprint.PROVEN,
            PreAAPSequenceMode.PROVEN,
            PreAuthSequenceMode.PROVEN,
            AAPLocalRXProfile.PROVEN,
        )
        rendered = "\n".join(output)
        self.assertIn("AAP Configure Response mode: kernel-mtu-only", rendered)
        self.assertIn("EXPERIMENTAL:", rendered)

    async def test_experimental_footprint_is_labeled_and_forwarded(self) -> None:
        runner = AsyncMock(return_value=observation())
        output: list[str] = []
        status = await run_probe(
            execute=True,
            descriptor_timeout=30,
            sdp_footprint=ReferenceSDPFootprint.BLUEZ_LIKE,
            output=output.append,
            live_runner=runner,
        )
        self.assertEqual(status, 0)
        runner.assert_awaited_once_with(
            output.append,
            30,
            AAPConfigureResponseMode.PROVEN,
            ReferenceSDPFootprint.BLUEZ_LIKE,
            PreAAPSequenceMode.PROVEN,
            PreAuthSequenceMode.PROVEN,
            AAPLocalRXProfile.PROVEN,
        )
        rendered = "\n".join(output)
        self.assertIn("Reference SDP footprint: bluez-like", rendered)
        self.assertIn("EXPERIMENTAL:", rendered)

    async def test_pre_aap_mode_is_labeled_and_forwarded(self) -> None:
        runner = AsyncMock(return_value=observation())
        output: list[str] = []
        status = await run_probe(
            execute=True,
            descriptor_timeout=30,
            pre_aap_sequence=PreAAPSequenceMode.BLUEZ_L2CAP_INFO,
            output=output.append,
            live_runner=runner,
        )
        self.assertEqual(status, 0)
        runner.assert_awaited_once_with(
            output.append,
            30,
            AAPConfigureResponseMode.PROVEN,
            ReferenceSDPFootprint.PROVEN,
            PreAAPSequenceMode.BLUEZ_L2CAP_INFO,
            PreAuthSequenceMode.PROVEN,
            AAPLocalRXProfile.PROVEN,
        )
        rendered = "\n".join(output)
        self.assertIn("Pre-AAP sequence: bluez-l2cap-info", rendered)
        self.assertIn("EXPERIMENTAL:", rendered)

    async def test_pre_auth_mode_is_labeled_and_forwarded(self) -> None:
        runner = AsyncMock(return_value=observation())
        output: list[str] = []
        status = await run_probe(
            execute=True,
            descriptor_timeout=30,
            pre_auth_sequence=PreAuthSequenceMode.BLUEZ_DISCOVERY,
            output=output.append,
            live_runner=runner,
        )
        self.assertEqual(status, 0)
        runner.assert_awaited_once_with(
            output.append,
            30,
            AAPConfigureResponseMode.PROVEN,
            ReferenceSDPFootprint.PROVEN,
            PreAAPSequenceMode.PROVEN,
            PreAuthSequenceMode.BLUEZ_DISCOVERY,
            AAPLocalRXProfile.PROVEN,
        )
        rendered = "\n".join(output)
        self.assertIn("Pre-auth sequence: bluez-discovery", rendered)
        self.assertIn("EXPERIMENTAL:", rendered)

    async def test_local_rx_experiment_is_labeled_and_forwarded(self) -> None:
        runner = AsyncMock(return_value=observation())
        output: list[str] = []
        status = await run_probe(
            execute=True,
            descriptor_timeout=30,
            local_rx_profile=AAPLocalRXProfile.KERNEL_DEFAULT,
            output=output.append,
            live_runner=runner,
        )
        self.assertEqual(status, 0)
        runner.assert_awaited_once_with(
            output.append,
            30,
            AAPConfigureResponseMode.PROVEN,
            ReferenceSDPFootprint.PROVEN,
            PreAAPSequenceMode.PROVEN,
            PreAuthSequenceMode.PROVEN,
            AAPLocalRXProfile.KERNEL_DEFAULT,
        )
        rendered = "\n".join(output)
        self.assertIn("AAP local RX profile: kernel-default", rendered)
        self.assertIn("EXPERIMENTAL:", rendered)

    def test_safe_sdp_query_summary_reports_metadata_only(self) -> None:
        snapshot = ReferenceSDPQuerySnapshot(
            footprint=ReferenceSDPFootprint.BLUEZ_LIKE,
            requests_observed=3,
            summaries=(
                ReferenceSDPQuerySummary(
                    search_uuids=(0x0100,),
                    attribute_ranges=((0x0004, 0x0004),),
                    maximum_attribute_byte_count=128,
                    continuation_used=False,
                    matching_record_count=2,
                    total_response_bytes=76,
                ),
            ),
            target_l2cap_full_attribute_query=ReferenceSDPQuerySummary(
                search_uuids=(0x0100,),
                attribute_ranges=((0x0000, 0xFFFF),),
                maximum_attribute_byte_count=65535,
                continuation_used=True,
                matching_record_count=22,
                total_response_bytes=1000,
            ),
        )
        output: list[str] = []
        _print_sdp_query_summary(output.append, snapshot)
        rendered = "\n".join(output)
        for expected in (
            "sdp_footprint=bluez-like",
            "service_search_attribute_requests=3",
            "l2cap_full_attribute_search_observed=yes",
            "search_uuids=0x0100",
            "attribute_ranges=0x0000FFFF",
            "max_attribute_bytes=65535",
            "continuation_used=yes",
            "matching_record_count=22",
            "response_bytes=1000",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("payload", rendered.lower())
        self.assertNotIn("attribute_ranges=0x0004", rendered)

    def test_safe_configuration_summary_reports_only_allowlisted_fields(
        self,
    ) -> None:
        selected = AAPL2CAPConfigurationObservation(
            response_mode=AAPConfigureResponseMode.KERNEL_MTU_ONLY,
            peer_option_types=(1, 2, 4),
            peer_mtu=2582,
            peer_flush_timeout=30,
            peer_rfc_present=True,
            peer_rfc_mode=0,
            response_result=0,
            response_option_types=(1,),
            response_mtu=2582,
            response_flush_timeout=None,
            response_rfc_present=False,
            response_rfc_mode=None,
        )
        output: list[str] = []
        _print_l2cap_configuration_summary(
            output.append,
            AAPConfigureResponseMode.KERNEL_MTU_ONLY,
            selected,
        )
        rendered = "\n".join(output)
        for expected in (
            "response_mode=kernel-mtu-only",
            "peer_option_types=0x01,0x02,0x04",
            "peer_mtu=2582",
            "peer_flush_timeout=30",
            "peer_rfc_present=yes",
            "peer_rfc_mode=Basic",
            "response_result=success",
            "response_option_types=0x01",
            "response_mtu=2582",
            "response_flush_timeout_present=no",
            "response_flush_timeout=not-observed",
            "response_rfc_present=no",
            "response_rfc_mode=not-observed",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("payload", rendered.lower())

    def test_safe_local_rx_summary_distinguishes_implicit_default_mtu(
        self,
    ) -> None:
        selected = AAPLocalRXConfigurationObservation(
            mode=AAPLocalRXProfile.KERNEL_DEFAULT,
            request_observed=True,
            request_flags=0,
            internal_receive_mtu=2048,
            peer_response_observed=True,
            peer_response_result=0,
            peer_response_option_types=(1,),
            peer_response_mtu=672,
        )
        output: list[str] = []
        _print_local_rx_configuration_summary(output.append, selected)
        rendered = "\n".join(output)
        for expected in (
            "mode=kernel-default",
            "request_observed=yes",
            "request_option_types=none",
            "request_mtu=default-672/not-explicit",
            "request_flags=0x0000",
            "internal_receive_mtu=2048",
            "peer_response_observed=yes",
            "peer_response_result=success",
            "peer_response_option_types=0x01",
            "peer_response_mtu=672",
        ):
            self.assertIn(expected, rendered)
        self.assertNotIn("payload", rendered.lower())

    def test_post_ack_shape_summary_uses_canonical_metadata_only(self) -> None:
        selected = HandshakeObservation(
            ack_observed=True,
            evidence=DescriptorEvidence(sensor_framework=True),
            post_ack_frame_count=3,
            post_ack_frame_summaries=(
                AAPFrameSummary(255, 4, 0x002B),
                AAPFrameSummary(996, 4, 0x0017),
                AAPFrameSummary(20, 4, 0x0017),
            ),
        )
        output: list[str] = []
        _print_post_ack_shape_summary(output.append, selected)
        rendered = "\n".join(output)
        self.assertIn("first_type_0x002b_length=255", rendered)
        self.assertIn("type_0x0017_frame_lengths=996,20", rendered)
        self.assertIn("max_post_ack_frame_length=996", rendered)
        self.assertNotIn("payload", rendered.lower())


class ReferenceProgressTests(unittest.TestCase):
    def test_reference_phases_report_safe_success_evidence(self) -> None:
        output: list[str] = []
        auth = _authentication_progress(output.append)
        channel = _channel_progress(output.append)
        aap = _aap_progress(output.append)
        auth(AuthenticationProgress.DEVICE_SELECTED, "PRIVATE DEVICE")
        aap(AAPProgress.SDP_INSTALLED)
        auth(AuthenticationProgress.CONNECTED, None)
        auth(AuthenticationProgress.AUTHENTICATED, None)
        auth(AuthenticationProgress.ENCRYPTED, None)
        auth(
            AuthenticationProgress.REPLACEMENT_KEY_REPORTED,
            "00112233445566778899AABBCCDDEEFF",
        )
        channel(AAPChannelProgress.OPENED, AAPChannel(672, 672))
        aap(AAPProgress.HANDSHAKE_SENT)
        aap(AAPProgress.ACK_OBSERVED)
        aap(AAPProgress.DESCRIPTORS_OBSERVED)

        rendered = "\n".join(output)
        self.assertIn("Known-good reference SDP records installed: 4/4", rendered)
        self.assertIn("BR/EDR authentication: succeeded", rendered)
        self.assertIn("BR/EDR encryption: established", rendered)
        self.assertIn("Reference AAP PSM 0x1001: opened", rendered)
        self.assertIn("Canonical AAP handshake request: sent", rendered)
        self.assertIn("Exact canonical AAP ACK: observed", rendered)
        self.assertNotIn("PRIVATE DEVICE", rendered)
        self.assertNotIn("00112233445566778899AABBCCDDEEFF", rendered)


class ReportingHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_handoff_cleanup_runs_on_success(self) -> None:
        delegate = FakeHandoff()
        reporting = _ReportingHandoffTransport(delegate, lambda message: None)
        async with reporting.acquire("hci0"):
            pass
        self.assertEqual(delegate.events, ["enter:hci0", "exit:hci0"])

    async def test_existing_handoff_cleanup_runs_on_handshake_failures(self) -> None:
        failures = (
            AAPDescriptorObservationTimeoutError(observation()),
            AAPHandshakeTimeoutError("missing ACK", observation(ack_observed=False)),
            AAPHandshakeError("generic handshake failure"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                delegate = FakeHandoff()
                reporting = _ReportingHandoffTransport(
                    delegate, lambda message: None
                )
                with self.assertRaises(type(failure)):
                    async with reporting.acquire("hci0"):
                        raise failure
                self.assertEqual(
                    delegate.events, ["enter:hci0", "exit:hci0"]
                )


class ReferenceCompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_composition_reuses_existing_reference_components(
        self,
    ) -> None:
        selected_observation = observation(
            pre_ack_frames=0, post_ack_frames=3
        )
        discovery_backend = SimpleNamespace(
            connect=AsyncMock(), close=Mock()
        )
        bluez_backend = SimpleNamespace(connect=AsyncMock(), close=Mock())
        reference_handoff = object()
        hci_transport = SimpleNamespace(ensure_available=AsyncMock())
        secure_session = object()
        channel_session = object()
        handshake_session = object()
        probe_session = SimpleNamespace(
            run=AsyncMock(
                return_value=SimpleNamespace(observation=selected_observation)
            )
        )
        output: list[str] = []
        with patch(
            "tools.probe_reference_handshake.DBusNextManagedObjectsBackend",
            return_value=discovery_backend,
        ), patch(
            "tools.probe_reference_handshake.DBusNextBlueZBackend",
            return_value=bluez_backend,
        ), patch(
            "tools.probe_reference_handshake.create_controller_handoff_transport",
            return_value=(reference_handoff, hci_transport),
        ) as create_handoff, patch(
            "tools.probe_reference_handshake.BlueZDeviceDiscovery"
        ) as discovery_class, patch(
            "tools.probe_reference_handshake.BlueZPairingStore"
        ) as pairing_class, patch(
            "tools.probe_reference_handshake.BumbleClassicRuntimeFactory"
        ) as runtime_class, patch(
            "tools.probe_reference_handshake.ClassicAuthenticationSession",
            return_value=secure_session,
        ) as authentication_class, patch(
            "tools.probe_reference_handshake.AAPChannelSession",
            return_value=channel_session,
        ) as channel_class, patch(
            "tools.probe_reference_handshake.AAPHandshakeSession",
            return_value=handshake_session,
        ) as handshake_class, patch(
            "tools.probe_reference_handshake.AAPHandshakeProbeSession",
            return_value=probe_session,
        ) as probe_class:
            result = await run_live_probe(output.append, 10)

        self.assertIs(result, selected_observation)
        discovery_backend.connect.assert_awaited_once_with()
        bluez_backend.connect.assert_awaited_once_with()
        create_handoff.assert_called_once_with(bluez_backend)
        hci_transport.ensure_available.assert_awaited_once_with()
        discovery_class.assert_called_once_with(discovery_backend)
        pairing_class.assert_called_once_with()
        runtime_class.assert_called_once_with()
        self.assertIs(
            authentication_class.call_args.args[3], runtime_class.return_value
        )
        self.assertTrue(
            callable(
                authentication_class.call_args.kwargs["pre_authentication"]
            )
        )
        reporting_handoff = authentication_class.call_args.args[2]
        self.assertIsInstance(reporting_handoff, _ReportingHandoffTransport)
        self.assertIs(reporting_handoff._delegate, reference_handoff)
        channel_class.assert_called_once()
        self.assertTrue(callable(channel_class.call_args.kwargs["progress"]))
        compatibility = channel_class.call_args.kwargs["compatibility"]
        self.assertIsInstance(compatibility, _ReferenceAAPCompatibility)
        self.assertIsInstance(
            compatibility.response, AAPConfigurationDiagnosticStrategy
        )
        self.assertEqual(
            compatibility.response.mode, AAPConfigureResponseMode.PROVEN
        )
        self.assertIsInstance(
            compatibility.local_rx, AAPLocalRXDiagnosticStrategy
        )
        self.assertEqual(
            compatibility.local_rx.mode, AAPLocalRXProfile.PROVEN
        )
        self.assertIn("AAP L2CAP CONFIGURATION SUMMARY", output)
        self.assertIn("  response_mode=proven", output)
        self.assertIn("AAP LOCAL RX CONFIGURATION SUMMARY", output)
        handshake_class.assert_called_once()
        self.assertEqual(
            handshake_class.call_args.kwargs["descriptor_timeout"], 10
        )
        self.assertTrue(callable(handshake_class.call_args.kwargs["progress"]))
        probe_class.assert_called_once()
        wrapped_session = probe_class.call_args.args[0]
        self.assertIsInstance(
            wrapped_session, PreAAPSequenceSecureSession
        )
        footprint_session = wrapped_session.delegate
        self.assertIsInstance(
            footprint_session, ReferenceSDPFootprintSecureSession
        )
        self.assertIs(footprint_session.delegate, secure_session)
        self.assertEqual(
            footprint_session.strategy.footprint,
            ReferenceSDPFootprint.PROVEN,
        )
        self.assertEqual(
            wrapped_session.strategy.mode, PreAAPSequenceMode.PROVEN
        )
        wrapped_channel = probe_class.call_args.args[1]
        self.assertIsInstance(wrapped_channel, PreAAPAAPChannelSession)
        self.assertIs(wrapped_channel.delegate, channel_session)
        self.assertIs(wrapped_channel.strategy, wrapped_session.strategy)
        self.assertIs(
            probe_class.call_args.args[2],
            handshake_session,
        )
        self.assertIn("REFERENCE SDP QUERY SUMMARY", output)
        self.assertIn("  sdp_footprint=proven", output)
        self.assertIn("PRE-AAP SEQUENCE SUMMARY", output)
        self.assertIn("  mode=proven", output)
        self.assertIn("PRE-AUTH SEQUENCE SUMMARY", output)
        discovery_backend.close.assert_called_once_with()
        bluez_backend.close.assert_called_once_with()


class ReferenceObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_summary_preserves_canonical_observation(self) -> None:
        selected = HandshakeObservation(
            ack_observed=True,
            evidence=DescriptorEvidence(
                sensor_framework=True,
                heart_rate_service=True,
                heart_rate=True,
                heartrate_access=True,
            ),
            pre_ack_frame_count=4,
            post_ack_frame_count=7,
            receive_frames_dropped=0,
        )
        output: list[str] = []
        status = await run_probe(
            execute=True,
            output=output.append,
            live_runner=AsyncMock(return_value=selected),
        )
        rendered = "\n".join(output)
        self.assertEqual(status, 0)
        self.assertIn("REFERENCE HANDSHAKE SUMMARY", rendered)
        self.assertIn("exact_ack_observed=yes", rendered)
        self.assertIn("pre_ack_frames=4", rendered)
        self.assertIn("post_ack_frames=7", rendered)
        self.assertIn("sensor_framework=yes", rendered)
        self.assertIn("heart_rate_service=yes", rendered)
        self.assertIn("heart_rate=yes", rendered)
        self.assertIn("heartrate_access=yes", rendered)
        self.assertIn("descriptor_complete=yes", rendered)
        self.assertEqual(output[-1], "REFERENCE HANDSHAKE PASS")

    async def test_descriptor_timeout_is_safe_bounded_and_never_activates_hr(
        self,
    ) -> None:
        hidden_payload = b"PRIVATE_DESCRIPTOR_AND_LINK_KEY_0011223344556677"
        frame = bytearray(51)
        frame[2:4] = (4).to_bytes(2, "little")
        frame[4:6] = (0x002B).to_bytes(2, "little")
        frame[6] = 3
        frame[7:9] = (34).to_bytes(2, "little")
        frame[17 : 17 + min(34, len(hidden_payload))] = hidden_payload[:34]
        summary = AAPFrameSummary.from_frame(bytes(frame))
        selected = observation(
            summaries=(summary,) * (AAP_FRAME_SUMMARY_LIMIT + 5)
        )
        output: list[str] = []
        status = await run_probe(
            execute=True,
            output=output.append,
            live_runner=AsyncMock(
                side_effect=AAPDescriptorObservationTimeoutError(selected)
            ),
        )
        rendered = "\n".join(output)
        self.assertEqual(status, 1)
        self.assertIn("Reference AAP descriptor timeout diagnostics:", rendered)
        self.assertIn("exact_ack_observed=yes", rendered)
        self.assertIn("pre_ack_frames=2", rendered)
        self.assertIn("post_ack_frames=26", rendered)
        self.assertIn("receive_frames_dropped=0", rendered)
        self.assertIn("header_u16_4_5=0x002B", rendered)
        self.assertIn("Type-0x002B structural summary:", rendered)
        self.assertIn("descriptor_complete=no", rendered)
        summary_headers = [
            line for line in output if line.startswith("  pre_ack_frame_")
        ]
        self.assertEqual(len(summary_headers), AAP_FRAME_SUMMARY_LIMIT)
        self.assertNotIn(hidden_payload.decode(), rendered)
        self.assertNotIn(hidden_payload.hex(), rendered.lower())
        self.assertNotIn("HeartRateActivationSession", rendered)

    async def test_missing_ack_observation_preserves_counts_and_evidence(
        self,
    ) -> None:
        selected = observation(
            ack_observed=False, pre_ack_frames=9, post_ack_frames=0
        )
        output: list[str] = []
        status = await run_probe(
            execute=True,
            output=output.append,
            live_runner=AsyncMock(
                side_effect=AAPHandshakeTimeoutError("missing", selected)
            ),
        )
        rendered = "\n".join(output)
        self.assertEqual(status, 1)
        self.assertIn("missing_exact_ack", rendered)
        self.assertIn("exact_ack_observed=no", rendered)
        self.assertIn("pre_ack_frames=9", rendered)
        self.assertIn("post_ack_frames=0", rendered)

    async def test_canonical_missing_ack_policy_retains_safe_observation(
        self,
    ) -> None:
        private_frame = b"PRIVATE PRE ACK FRAME"
        transport = MissingACKTransport([private_frame])
        with self.assertRaises(AAPHandshakeTimeoutError) as raised:
            await AAPHandshakeSession().run(transport)
        selected = raised.exception.observation
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertFalse(selected.ack_observed)
        self.assertEqual(selected.pre_ack_frame_count, 1)
        self.assertEqual(selected.post_ack_frame_count, 0)
        self.assertEqual(len(selected.pre_ack_frame_summaries), 1)
        self.assertNotIn(private_frame.hex(), repr(selected))

    async def test_canonical_descriptor_policy_and_timeouts_are_unchanged(
        self,
    ) -> None:
        session = AAPHandshakeSession()
        self.assertEqual(session._ack_timeout, 5)
        self.assertEqual(session._descriptor_timeout, 3)
        transport = MissingACKTransport([AAP_HANDSHAKE_ACK])
        with self.assertRaises(AAPDescriptorObservationTimeoutError) as raised:
            await session.run(transport)
        self.assertTrue(raised.exception.observation.ack_observed)
        self.assertFalse(raised.exception.observation.evidence.required)

    async def test_pairing_error_never_prints_link_key(self) -> None:
        secret = "00112233445566778899AABBCCDDEEFF"
        output: list[str] = []
        status = await run_probe(
            execute=True,
            verbose=True,
            output=output.append,
            live_runner=AsyncMock(side_effect=PairingStoreError(secret)),
        )
        self.assertEqual(status, 1)
        self.assertIn(
            "REFERENCE HANDSHAKE FAIL: pairing_credentials_unavailable",
            output,
        )
        self.assertNotIn(secret, "\n".join(output))

    async def test_generic_handshake_failure_has_no_bypass(self) -> None:
        output: list[str] = []
        status = await run_probe(
            execute=True,
            output=output.append,
            live_runner=AsyncMock(
                side_effect=AAPHandshakeError("PRIVATE RAW PAYLOAD")
            ),
        )
        self.assertEqual(status, 1)
        self.assertEqual(output, ["REFERENCE HANDSHAKE FAIL: aap_handshake_failed"])


class ReferenceStaticSafetyTests(unittest.TestCase):
    def test_configuration_experiment_is_reference_probe_only(self) -> None:
        root = Path(__file__).resolve().parents[1]
        production_sources = (
            root / "src/airpods_hr/aap_channel.py",
            root / "src/airpods_hr/authentication.py",
            root / "src/airpods_hr/monitor_cli.py",
            root / "tools/probe_bluez_coexistence.py",
        )
        for source in production_sources:
            text = source.read_text(encoding="utf-8")
            self.assertNotIn("AAPConfigurationDiagnosticStrategy", text)
            self.assertNotIn("KERNEL_MTU_ONLY", text)
            self.assertNotIn("ReferenceSDPFootprintStrategy", text)
            self.assertNotIn("BLUEZ_LIKE", text)
            self.assertNotIn("PreAuthSequenceStrategy", text)
            self.assertNotIn("bluez-discovery", text)

    def test_reference_probe_uses_no_coexistence_or_hr_activation_path(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = root / "tools/probe_reference_handshake.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported_modules: set[str] = set()
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_modules.add(node.module or "")
                imported_names.update(alias.name for alias in node.names)
        self.assertNotIn("airpods_hr.bluez_coexistence", imported_modules)
        self.assertNotIn("KernelL2CAPTransport", imported_names)
        self.assertNotIn("HeartRateActivationSession", imported_names)
        self.assertIn("AAPHandshakeProbeSession", imported_names)
        self.assertIn("ClassicAuthenticationSession", imported_names)
        self.assertIn("create_controller_handoff_transport", imported_names)

    def test_protocol_and_normal_monitor_remain_byte_identical(self) -> None:
        root = Path(__file__).resolve().parents[1]
        expected = {
            "src/airpods_hr/protocol.py": (
                "b4d1daea0582841e48ba9efc3a8a7d4d74bba9b69cdbf54d3767b8bb45afecca"
            ),
            "src/airpods_hr/monitor_cli.py": (
                "41332f411af2e89374b42047a2e009aef035ca2e8bce74440d0d9d891d7aded4"
            ),
        }
        for relative_path, expected_hash in expected.items():
            self.assertEqual(
                hashlib.sha256((root / relative_path).read_bytes()).hexdigest(),
                expected_hash,
            )


if __name__ == "__main__":
    unittest.main()
