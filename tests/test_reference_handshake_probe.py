"""Hardware-independent tests for the reference handshake diagnostics."""

from __future__ import annotations


import unittest


from airpods_hr.aap import AAPFrameSummary, DescriptorEvidence, HandshakeObservation


from airpods_hr.aap_config_diagnostics import AAPConfigureResponseMode, AAPL2CAPConfigurationObservation


from airpods_hr.aap_local_rx_diagnostics import AAPLocalRXConfigurationObservation, AAPLocalRXProfile


from tools.probe_reference_handshake import _print_l2cap_configuration_summary, _print_local_rx_configuration_summary, _print_post_ack_shape_summary


class ReferenceProbeDryRunTests(unittest.IsolatedAsyncioTestCase):


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


