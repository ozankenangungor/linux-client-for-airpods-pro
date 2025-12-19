"""Hardware-independent tests for the reference handshake diagnostics."""

from __future__ import annotations


import unittest


from airpods_hr.aap import AAPFrameSummary, AAPProgress, DescriptorEvidence, HandshakeObservation


from airpods_hr.aap_channel import AAPChannel, AAPChannelProgress
from airpods_hr.aap_config_diagnostics import AAPConfigureResponseMode, AAPL2CAPConfigurationObservation


from airpods_hr.aap_local_rx_diagnostics import AAPLocalRXConfigurationObservation, AAPLocalRXProfile


from airpods_hr.authentication import AuthenticationProgress


from airpods_hr.reference_sdp_footprint import ReferenceSDPFootprint, ReferenceSDPQuerySnapshot, ReferenceSDPQuerySummary


from tools.probe_reference_handshake import _aap_progress, _authentication_progress, _channel_progress, _print_l2cap_configuration_summary, _print_local_rx_configuration_summary, _print_post_ack_shape_summary, _print_sdp_query_summary


class ReferenceProbeDryRunTests(unittest.IsolatedAsyncioTestCase):


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


