"""Tests for the local receive-MTU diagnostics."""

from __future__ import annotations

import asyncio
import dataclasses
import unittest
from pathlib import Path
from types import SimpleNamespace

from bumble import l2cap

from airpods_hr.aap import AAPFrameSummary, DescriptorEvidence, HandshakeObservation
from airpods_hr.aap_config_diagnostics import (
    AAPConfigurationDiagnosticStrategy,
    AAPConfigureResponseMode,
)
from airpods_hr.aap_local_rx_diagnostics import (
    AAP_POST_ACK_TYPE_17_LENGTH_LIMIT,
    AAPLocalRXConfigurationObservation,
    AAPLocalRXDiagnosticError,
    AAPLocalRXDiagnosticStrategy,
    AAPLocalRXProfile,
    AAPPostACKShapeObservation,
    validate_bumble_local_rx_api,
)
from airpods_hr.protocol import AAP_PSM


class ConfigureHarness:
    def __init__(
        self,
        *,
        psm: int = AAP_PSM,
        fcs_enabled: bool = False,
    ) -> None:
        self.frames: list[l2cap.L2CAP_Control_Frame] = []
        self.manager = l2cap.ChannelManager()
        self.original_send = self._capture_frame
        self.manager.send_control_frame = self.original_send
        self.manager.next_identifier = lambda connection: 7
        self.connection = SimpleNamespace(handle=1, peer_address="redacted")
        self.channel = l2cap.ClassicChannel(
            manager=self.manager,
            connection=self.connection,
            signaling_cid=l2cap.L2CAP_SIGNALING_CID,
            psm=psm,
            source_cid=0x0040,
            spec=l2cap.ClassicChannelSpec(
                psm=psm,
                mtu=l2cap.L2CAP_DEFAULT_MTU,
                fcs_enabled=fcs_enabled,
            ),
        )
        self.channel.destination_cid = 0x0041
        self.channel.state = self.channel.State.WAIT_CONFIG_REQ_RSP
        self.manager.channels[self.connection.handle] = {
            self.channel.source_cid: self.channel
        }

    def _capture_frame(self, connection, cid, frame) -> None:
        del connection, cid
        self.frames.append(frame)

    def send_request(
        self, strategy: AAPLocalRXDiagnosticStrategy
    ) -> l2cap.L2CAP_Configure_Request:
        with strategy(self.manager):
            self.channel.send_configure_request()
        requests = [
            frame
            for frame in self.frames
            if isinstance(frame, l2cap.L2CAP_Configure_Request)
        ]
        self.assert_single(requests)
        return requests[0]

    @staticmethod
    def assert_single(frames: list[object]) -> None:
        if len(frames) != 1:
            raise AssertionError(f"expected one frame, got {len(frames)}")


class AAPLocalRXWireTests(unittest.TestCase):
    def test_proven_request_is_byte_identical_and_reports_mtu_2048(self) -> None:
        harness = ConfigureHarness()
        strategy = AAPLocalRXDiagnosticStrategy(AAPLocalRXProfile.PROVEN)

        request = harness.send_request(strategy)

        expected = l2cap.L2CAP_Configure_Request(
            identifier=request.identifier,
            destination_cid=0x0041,
            flags=0,
            options=bytes.fromhex("01 02 00 08"),
        )
        self.assertEqual(bytes(request), bytes(expected))
        self.assertEqual(len(request.payload), 8)
        observed = strategy.observation
        self.assertTrue(observed.request_observed)
        self.assertEqual(observed.request_option_types, (0x01,))
        self.assertEqual(observed.request_mtu, 2048)
        self.assertEqual(observed.internal_receive_mtu, 2048)

    def test_kernel_default_removes_only_mtu_from_aap_wire_request(self) -> None:
        harness = ConfigureHarness(fcs_enabled=True)
        strategy = AAPLocalRXDiagnosticStrategy(
            AAPLocalRXProfile.KERNEL_DEFAULT
        )

        request = harness.send_request(strategy)

        self.assertEqual(request.options, bytes.fromhex("05 01 01"))
        self.assertNotIn(bytes.fromhex("01 02 00 08"), request.options)
        self.assertEqual(request.flags, 0)
        self.assertEqual(request.identifier, 7)
        self.assertEqual(request.destination_cid, 0x0041)
        self.assertEqual(strategy.observation.request_identifier, request.identifier)
        self.assertEqual(strategy.observation.request_option_types, (0x05,))
        self.assertIsNone(strategy.observation.request_mtu)
        self.assertEqual(strategy.observation.internal_receive_mtu, 2048)
        self.assertEqual(harness.channel.mtu, 2048)

    def test_kernel_default_basic_request_has_exact_empty_option_shape(self) -> None:
        harness = ConfigureHarness()
        strategy = AAPLocalRXDiagnosticStrategy(
            AAPLocalRXProfile.KERNEL_DEFAULT
        )

        request = harness.send_request(strategy)

        self.assertEqual(request.options, b"")
        self.assertEqual(len(request.payload), 4)
        self.assertEqual(strategy.observation.request_option_types, ())
        self.assertIsNone(strategy.observation.request_mtu)

    def test_non_aap_request_is_untouched_and_not_observed(self) -> None:
        harness = ConfigureHarness(psm=0x1003)
        strategy = AAPLocalRXDiagnosticStrategy(
            AAPLocalRXProfile.KERNEL_DEFAULT
        )

        request = harness.send_request(strategy)

        self.assertEqual(request.options, bytes.fromhex("01 02 00 08"))
        self.assertFalse(strategy.observation.request_observed)

    def test_peer_response_is_observed_without_changing_channel_handling(
        self,
    ) -> None:
        harness = ConfigureHarness()
        strategy = AAPLocalRXDiagnosticStrategy(
            AAPLocalRXProfile.KERNEL_DEFAULT
        )
        with strategy(harness.manager):
            harness.channel.send_configure_request()
            request = harness.frames[-1]
            assert isinstance(request, l2cap.L2CAP_Configure_Request)
            response = l2cap.L2CAP_Configure_Response(
                identifier=request.identifier,
                source_cid=harness.channel.source_cid,
                flags=0,
                result=l2cap.L2CAP_Configure_Response.Result.SUCCESS,
                options=bytes.fromhex("01 02 a0 02"),
            )
            harness.manager.on_l2cap_configure_response(
                harness.connection,
                l2cap.L2CAP_SIGNALING_CID,
                response,
            )

        observed = strategy.observation
        self.assertTrue(observed.peer_response_observed)
        self.assertEqual(observed.peer_response_result, 0)
        self.assertEqual(observed.peer_response_option_types, (0x01,))
        self.assertEqual(observed.peer_response_mtu, 672)
        self.assertEqual(
            harness.channel.state, harness.channel.State.WAIT_CONFIG_REQ
        )

    def test_airpods_originated_request_and_task_9_4_7_response_are_unchanged(
        self,
    ) -> None:
        harness = ConfigureHarness()
        local_rx = AAPLocalRXDiagnosticStrategy(
            AAPLocalRXProfile.KERNEL_DEFAULT
        )
        response_strategy = AAPConfigurationDiagnosticStrategy(
            AAPConfigureResponseMode.PROVEN
        )
        peer_options = bytes.fromhex(
            "01 02 16 0a 02 02 1e 00 04 09 00 00 00 00 00 00 00 00 00"
        )
        peer_request = l2cap.L2CAP_Configure_Request(
            identifier=9,
            destination_cid=harness.channel.source_cid,
            flags=0,
            options=peer_options,
        )

        with response_strategy(harness.manager), local_rx(harness.manager):
            harness.manager.on_l2cap_configure_request(
                harness.connection,
                l2cap.L2CAP_SIGNALING_CID,
                peer_request,
            )

        responses = [
            frame
            for frame in harness.frames
            if isinstance(frame, l2cap.L2CAP_Configure_Response)
        ]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].options, peer_options)
        assert response_strategy.observation is not None
        self.assertEqual(response_strategy.observation.response_mtu, 2582)
        self.assertFalse(local_rx.observation.request_observed)

    def test_observation_retains_metadata_only(self) -> None:
        field_names = {
            field.name
            for field in dataclasses.fields(AAPLocalRXConfigurationObservation)
        }
        self.assertFalse(
            field_names.intersection(
                {"raw", "payload", "frame", "request", "response", "options"}
            )
        )

    def test_pinned_bumble_contract_guard(self) -> None:
        validate_bumble_local_rx_api()
        with self.assertRaisesRegex(
            AAPLocalRXDiagnosticError, "unsupported Bumble version"
        ):
            validate_bumble_local_rx_api(installed_version="0.0.235")


class AAPLocalRXRestorationTests(unittest.TestCase):
    def _assert_restored_after(self, error: BaseException | None) -> None:
        harness = ConfigureHarness()
        strategy = AAPLocalRXDiagnosticStrategy(
            AAPLocalRXProfile.KERNEL_DEFAULT
        )
        original_response = harness.manager.on_l2cap_configure_response
        try:
            with strategy(harness.manager):
                self.assertIsNot(
                    harness.manager.send_control_frame, harness.original_send
                )
                if error is not None:
                    raise error
        except BaseException as caught:
            self.assertIs(caught, error)
        self.assertIs(harness.manager.send_control_frame, harness.original_send)
        self.assertEqual(
            harness.manager.on_l2cap_configure_response, original_response
        )

    def test_hooks_restore_on_success(self) -> None:
        self._assert_restored_after(None)

    def test_hooks_restore_on_timeout(self) -> None:
        self._assert_restored_after(TimeoutError())

    def test_hooks_restore_on_exception(self) -> None:
        self._assert_restored_after(RuntimeError("synthetic"))

    def test_hooks_restore_on_cancellation(self) -> None:
        self._assert_restored_after(asyncio.CancelledError())


class AAPPostACKShapeTests(unittest.TestCase):
    def test_post_ack_shape_is_metadata_only_and_bounded(self) -> None:
        summaries = (
            AAPFrameSummary(255, 4, 0x002B),
            *(AAPFrameSummary(100 + index, 4, 0x0017) for index in range(20)),
            AAPFrameSummary(1200, 4, 0x0030),
        )
        handshake = HandshakeObservation(
            ack_observed=True,
            evidence=DescriptorEvidence(sensor_framework=True),
            post_ack_frame_count=len(summaries),
            post_ack_frame_summaries=tuple(summaries),
        )

        shape = AAPPostACKShapeObservation.from_handshake(handshake)

        self.assertEqual(shape.first_type_0x002b_length, 255)
        self.assertEqual(
            len(shape.type_0x0017_frame_lengths),
            AAP_POST_ACK_TYPE_17_LENGTH_LIMIT,
        )
        self.assertEqual(shape.max_post_ack_frame_length, 1200)
        self.assertEqual(handshake.evidence, DescriptorEvidence(sensor_framework=True))
        self.assertFalse(
            {field.name for field in dataclasses.fields(shape)}
            & {"raw", "payload", "frame", "data"}
        )

if __name__ == "__main__":
    unittest.main()
