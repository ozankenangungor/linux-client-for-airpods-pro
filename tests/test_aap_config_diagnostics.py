"""Tests for the probe-only AAP Configure Response experiment."""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from types import SimpleNamespace

from bumble import l2cap

from airpods_hr.aap_channel import AAPChannelSession
from airpods_hr.aap_config_diagnostics import (
    AAPConfigurationDiagnosticStrategy,
    AAPConfigureResponseMode,
    AAPL2CAPConfigurationObservation,
)
from airpods_hr.bumble_compat import aap_flush_timeout_compatibility
from airpods_hr.protocol import AAP_PSM


HISTORICAL_AAP_OPTIONS = bytes.fromhex(
    "01 02 16 0a "
    "02 02 1e 00 "
    "04 09 00 00 00 00 00 00 00 00 00"
)
RECENT_KERNEL_CAPTURE_OPTIONS = bytes.fromhex(
    "01 02 16 0a 02 02 1e 00"
)
MTU_ONLY_OPTIONS = bytes.fromhex("01 02 16 0a")


class ConfigureHarness:
    def __init__(
        self,
        *,
        psm: int = AAP_PSM,
        options: bytes = HISTORICAL_AAP_OPTIONS,
    ) -> None:
        self.frames: list[l2cap.L2CAP_Control_Frame] = []
        self.manager = l2cap.ChannelManager()
        self.manager.send_control_frame = self._capture_frame
        self.connection = SimpleNamespace(handle=1, peer_address="redacted")
        self.channel = l2cap.ClassicChannel(
            manager=self.manager,
            connection=self.connection,
            signaling_cid=l2cap.L2CAP_SIGNALING_CID,
            psm=psm,
            source_cid=0x0040,
            spec=l2cap.ClassicChannelSpec(psm=psm),
        )
        self.channel.destination_cid = 0x0041
        self.channel.state = self.channel.State.WAIT_CONFIG_REQ_RSP
        self.manager.channels[self.connection.handle] = {
            self.channel.source_cid: self.channel
        }
        self.request = l2cap.L2CAP_Configure_Request(
            identifier=7,
            destination_cid=self.channel.source_cid,
            flags=0,
            options=options,
        )

    def _capture_frame(self, connection, cid, frame) -> None:
        del connection, cid
        self.frames.append(frame)

    def dispatch(
        self, strategy: AAPConfigurationDiagnosticStrategy
    ) -> l2cap.L2CAP_Configure_Response:
        with strategy(self.manager):
            self.manager.on_l2cap_configure_request(
                self.connection,
                l2cap.L2CAP_SIGNALING_CID,
                self.request,
            )
        responses = [
            frame
            for frame in self.frames
            if isinstance(frame, l2cap.L2CAP_Configure_Response)
        ]
        if len(responses) != 1:
            raise AssertionError(
                f"expected one Configure Response, got {len(responses)}"
            )
        return responses[0]


class AAPConfigurationDiagnosticTests(unittest.TestCase):
    def test_proven_mode_delegates_to_frozen_compatibility_bytes(self) -> None:
        harness = ConfigureHarness()
        strategy = AAPConfigurationDiagnosticStrategy(
            AAPConfigureResponseMode.PROVEN
        )

        response = harness.dispatch(strategy)

        self.assertEqual(
            response.result, l2cap.L2CAP_Configure_Response.Result.SUCCESS
        )
        self.assertEqual(response.options, HISTORICAL_AAP_OPTIONS)
        expected = l2cap.L2CAP_Configure_Response(
            identifier=harness.request.identifier,
            source_cid=harness.channel.destination_cid,
            flags=0,
            result=l2cap.L2CAP_Configure_Response.Result.SUCCESS,
            options=HISTORICAL_AAP_OPTIONS,
        )
        self.assertEqual(bytes(response), bytes(expected))
        self.assertEqual(harness.channel.peer_mtu, 2582)
        self.assertEqual(harness.channel.mode, l2cap.TransmissionMode.BASIC)

    def test_kernel_style_changes_only_successful_wire_options(self) -> None:
        harness = ConfigureHarness()
        original_processor_type = type(harness.channel.processor)
        strategy = AAPConfigurationDiagnosticStrategy(
            AAPConfigureResponseMode.KERNEL_MTU_ONLY
        )

        response = harness.dispatch(strategy)

        self.assertEqual(
            response.result, l2cap.L2CAP_Configure_Response.Result.SUCCESS
        )
        self.assertEqual(response.options, MTU_ONLY_OPTIONS)
        self.assertEqual(harness.channel.peer_mtu, 2582)
        self.assertEqual(harness.channel.mode, l2cap.TransmissionMode.BASIC)
        self.assertIs(type(harness.channel.processor), original_processor_type)

    def test_kernel_style_accepts_recent_request_without_rfc(self) -> None:
        harness = ConfigureHarness(options=RECENT_KERNEL_CAPTURE_OPTIONS)
        strategy = AAPConfigurationDiagnosticStrategy(
            AAPConfigureResponseMode.KERNEL_MTU_ONLY
        )

        response = harness.dispatch(strategy)

        self.assertEqual(
            response.result, l2cap.L2CAP_Configure_Response.Result.SUCCESS
        )
        self.assertEqual(response.options, MTU_ONLY_OPTIONS)
        self.assertEqual(harness.channel.peer_mtu, 2582)
        self.assertEqual(harness.channel.mode, l2cap.TransmissionMode.BASIC)

    def test_safe_observation_reports_reviewed_request_and_response(self) -> None:
        harness = ConfigureHarness()
        strategy = AAPConfigurationDiagnosticStrategy(
            AAPConfigureResponseMode.KERNEL_MTU_ONLY
        )
        harness.dispatch(strategy)

        observed = strategy.observation
        self.assertIsInstance(observed, AAPL2CAPConfigurationObservation)
        assert observed is not None
        self.assertEqual(observed.response_mode, "kernel-mtu-only")
        self.assertEqual(observed.peer_option_types, (0x01, 0x02, 0x04))
        self.assertEqual(observed.peer_mtu, 2582)
        self.assertEqual(observed.peer_flush_timeout, 30)
        self.assertTrue(observed.peer_rfc_present)
        self.assertEqual(observed.peer_rfc_mode, 0)
        self.assertEqual(observed.response_result, 0)
        self.assertEqual(observed.response_option_types, (0x01,))
        self.assertEqual(observed.response_mtu, 2582)
        self.assertIsNone(observed.response_flush_timeout)
        self.assertFalse(observed.response_rfc_present)
        self.assertIsNone(observed.response_rfc_mode)

    def test_safe_observation_retains_no_raw_control_payload(self) -> None:
        field_names = {
            field.name
            for field in dataclasses.fields(AAPL2CAPConfigurationObservation)
        }
        self.assertFalse(
            field_names.intersection(
                {"raw", "payload", "request", "response", "options"}
            )
        )
        harness = ConfigureHarness()
        strategy = AAPConfigurationDiagnosticStrategy(
            AAPConfigureResponseMode.KERNEL_MTU_ONLY
        )
        harness.dispatch(strategy)
        self.assertNotIn(HISTORICAL_AAP_OPTIONS.hex(), repr(strategy.observation))

    def test_non_aap_psm_retains_stock_behavior(self) -> None:
        harness = ConfigureHarness(psm=0x1003)
        strategy = AAPConfigurationDiagnosticStrategy(
            AAPConfigureResponseMode.KERNEL_MTU_ONLY
        )

        response = harness.dispatch(strategy)

        self.assertEqual(
            response.result,
            l2cap.L2CAP_Configure_Response.Result.FAILURE_UNKNOWN_OPTIONS,
        )
        self.assertEqual(response.options, bytes.fromhex("02 02 1e 00"))
        self.assertIsNone(strategy.observation)

    def test_malformed_flush_timeout_fails_closed(self) -> None:
        harness = ConfigureHarness(
            options=bytes.fromhex("01 02 16 0a 02 01 1e")
        )
        strategy = AAPConfigurationDiagnosticStrategy(
            AAPConfigureResponseMode.KERNEL_MTU_ONLY
        )

        response = harness.dispatch(strategy)

        self.assertEqual(
            response.result,
            l2cap.L2CAP_Configure_Response.Result.FAILURE_UNKNOWN_OPTIONS,
        )
        self.assertEqual(response.options, b"")

    def test_probe_strategy_does_not_change_channel_or_monitor_defaults(
        self,
    ) -> None:
        session = AAPChannelSession()
        self.assertIs(session._compatibility, aap_flush_timeout_compatibility)
        monitor_source = (
            Path(__file__).resolve().parents[1]
            / "src/airpods_hr/monitor_cli.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("AAPConfigurationDiagnosticStrategy", monitor_source)
        self.assertNotIn("KERNEL_MTU_ONLY", monitor_source)


if __name__ == "__main__":
    unittest.main()
