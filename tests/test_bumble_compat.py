"""Hardware-independent tests for the Bumble L2CAP compatibility boundary."""

from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from bumble import l2cap

from airpods_hr.bumble_compat import (
    SUPPORTED_BUMBLE_VERSION,
    UnsupportedBumbleAPIError,
    UnsupportedBumbleVersionError,
    aap_flush_timeout_compatibility,
    validate_bumble_l2cap_api,
)
from airpods_hr.heartrate import parse_heart_rate_packet
from airpods_hr.protocol import AAP_PSM, HEART_RATE_MARKER


AIRPODS_OPTIONS = bytes.fromhex(
    "01 02 16 0a "
    "02 02 1e 00 "
    "04 09 00 00 00 00 00 00 00 00 00"
)


class ConfigureHarness:
    def __init__(self, *, psm: int = AAP_PSM, options: bytes = AIRPODS_OPTIONS):
        self.frames: list[l2cap.L2CAP_Control_Frame] = []
        self.manager = l2cap.ChannelManager()
        self.manager.send_control_frame = self._capture_frame
        self.connection = SimpleNamespace(handle=1, peer_address="synthetic-peer")
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

    def dispatch(self) -> l2cap.L2CAP_Configure_Response:
        self.manager.on_l2cap_configure_request(
            self.connection, l2cap.L2CAP_SIGNALING_CID, self.request
        )
        responses = [
            frame
            for frame in self.frames
            if isinstance(frame, l2cap.L2CAP_Configure_Response)
        ]
        if len(responses) != 1:
            raise AssertionError(f"expected one Configure Response, got {len(responses)}")
        return responses[0]


class BumbleCompatibilityTests(unittest.TestCase):
    def test_observed_airpods_options_decode_exactly(self) -> None:
        decoded = l2cap.L2CAP_Control_Frame.decode_configuration_options(
            AIRPODS_OPTIONS
        )

        self.assertEqual(
            decoded,
            [
                (l2cap.L2CAP_Configure_Request.ParameterType.MTU, b"\x16\x0a"),
                (
                    l2cap.L2CAP_Configure_Request.ParameterType.FLUSH_TIMEOUT,
                    b"\x1e\x00",
                ),
                (
                    l2cap.L2CAP_Configure_Request.ParameterType.RETRANSMISSION_AND_FLOW_CONTROL,
                    b"\x00" * 9,
                ),
            ],
        )

    def test_stock_bumble_rejects_flush_timeout(self) -> None:
        harness = ConfigureHarness()

        response = harness.dispatch()

        self.assertEqual(
            response.result,
            l2cap.L2CAP_Configure_Response.Result.FAILURE_UNKNOWN_OPTIONS,
        )
        self.assertEqual(response.options, bytes.fromhex("02 02 1e 00"))

    def test_compatibility_accepts_observed_request_and_preserves_options(self) -> None:
        harness = ConfigureHarness()

        with aap_flush_timeout_compatibility(harness.manager):
            response = harness.dispatch()

        self.assertEqual(
            response.result, l2cap.L2CAP_Configure_Response.Result.SUCCESS
        )
        self.assertEqual(response.options, AIRPODS_OPTIONS)

    def test_configure_response_bytes_match_the_proven_patch_semantics(self) -> None:
        harness = ConfigureHarness()

        with aap_flush_timeout_compatibility(harness.manager):
            response = harness.dispatch()

        legacy_patch_equivalent = l2cap.L2CAP_Configure_Response(
            identifier=harness.request.identifier,
            source_cid=harness.channel.destination_cid,
            flags=0,
            result=l2cap.L2CAP_Configure_Response.Result.SUCCESS,
            options=AIRPODS_OPTIONS,
        )
        self.assertEqual(bytes(response), bytes(legacy_patch_equivalent))
        self.assertEqual(response.flags, 0)
        self.assertEqual(response.options, AIRPODS_OPTIONS)
        self.assertEqual(harness.channel.peer_mtu, 2582)
        self.assertEqual(harness.channel.mode, l2cap.TransmissionMode.BASIC)

    def test_mtu_and_basic_mode_behavior_remain_unchanged(self) -> None:
        harness = ConfigureHarness()
        original_processor_type = type(harness.channel.processor)

        with aap_flush_timeout_compatibility(harness.manager):
            response = harness.dispatch()

        self.assertEqual(response.result, 0)
        self.assertEqual(harness.channel.peer_mtu, 2582)
        self.assertEqual(harness.channel.mode, l2cap.TransmissionMode.BASIC)
        self.assertIs(type(harness.channel.processor), original_processor_type)

    def test_malformed_flush_timeout_is_rejected(self) -> None:
        options = bytes.fromhex("01 02 16 0a 02 01 1e 04 09") + b"\x00" * 9
        harness = ConfigureHarness(options=options)

        with aap_flush_timeout_compatibility(harness.manager):
            response = harness.dispatch()

        self.assertEqual(
            response.result,
            l2cap.L2CAP_Configure_Response.Result.FAILURE_UNKNOWN_OPTIONS,
        )
        self.assertEqual(response.options, bytes.fromhex("02 01 1e"))

    def test_truncated_flush_timeout_is_rejected(self) -> None:
        harness = ConfigureHarness(options=bytes.fromhex("02 02 1e"))

        with aap_flush_timeout_compatibility(harness.manager):
            response = harness.dispatch()

        self.assertEqual(
            response.result,
            l2cap.L2CAP_Configure_Response.Result.FAILURE_UNKNOWN_OPTIONS,
        )
        self.assertEqual(response.options, b"")

    def test_flush_timeout_missing_length_is_rejected(self) -> None:
        harness = ConfigureHarness(options=bytes.fromhex("01 02 16 0a 02"))

        with aap_flush_timeout_compatibility(harness.manager):
            response = harness.dispatch()

        self.assertEqual(
            response.result,
            l2cap.L2CAP_Configure_Response.Result.FAILURE_UNKNOWN_OPTIONS,
        )
        self.assertEqual(response.options, b"")

    def test_unrelated_unknown_option_is_not_accepted(self) -> None:
        harness = ConfigureHarness(options=AIRPODS_OPTIONS + bytes.fromhex("7f 01 aa"))

        with aap_flush_timeout_compatibility(harness.manager):
            response = harness.dispatch()

        self.assertEqual(
            response.result,
            l2cap.L2CAP_Configure_Response.Result.FAILURE_UNKNOWN_OPTIONS,
        )
        self.assertEqual(response.options, bytes.fromhex("7f 01 aa"))

    def test_other_psm_retains_stock_rejection(self) -> None:
        harness = ConfigureHarness(psm=0x1003)

        with aap_flush_timeout_compatibility(harness.manager):
            response = harness.dispatch()

        self.assertEqual(
            response.result,
            l2cap.L2CAP_Configure_Response.Result.FAILURE_UNKNOWN_OPTIONS,
        )

    def test_other_manager_retains_stock_rejection(self) -> None:
        adapted = ConfigureHarness()
        untouched = ConfigureHarness()

        with aap_flush_timeout_compatibility(adapted.manager):
            response = untouched.dispatch()

        self.assertEqual(
            response.result,
            l2cap.L2CAP_Configure_Response.Result.FAILURE_UNKNOWN_OPTIONS,
        )

    def test_activation_is_nested_and_restores_original_handler(self) -> None:
        harness = ConfigureHarness()
        original = harness.manager.on_l2cap_configure_request

        with ExitStack() as stack:
            stack.enter_context(aap_flush_timeout_compatibility(harness.manager))
            installed = harness.manager.on_l2cap_configure_request
            stack.enter_context(aap_flush_timeout_compatibility(harness.manager))
            self.assertIs(harness.manager.on_l2cap_configure_request, installed)

        self.assertEqual(harness.manager.on_l2cap_configure_request, original)

    def test_unsupported_version_fails_clearly(self) -> None:
        with self.assertRaisesRegex(
            UnsupportedBumbleVersionError, "unsupported Bumble version"
        ):
            validate_bumble_l2cap_api(installed_version="0.0.235")

    def test_replaced_internal_api_fails_clearly(self) -> None:
        def incompatible_handler(self, request, extra):
            del self, request, extra

        with patch.object(
            l2cap.ClassicChannel,
            "on_configure_request",
            incompatible_handler,
        ):
            with self.assertRaisesRegex(
                UnsupportedBumbleAPIError, "handlers have been replaced"
            ):
                validate_bumble_l2cap_api(
                    installed_version=SUPPORTED_BUMBLE_VERSION
                )

    def test_heart_rate_parser_is_unchanged_while_compatibility_is_active(self) -> None:
        report = bytes.fromhex(
            "01 48 09 34 12 ab 08 07 06 05 04 03 02 01 44 33 22 11"
        )
        packet = b"\x08\x81\x01" + HEART_RATE_MARKER + report
        harness = ConfigureHarness()

        with aap_flush_timeout_compatibility(harness.manager):
            parsed = parse_heart_rate_packet(packet)

        self.assertEqual(parsed.bpm, 0x48)
        self.assertEqual(parsed.sequence, 0x1234)


if __name__ == "__main__":
    unittest.main()
