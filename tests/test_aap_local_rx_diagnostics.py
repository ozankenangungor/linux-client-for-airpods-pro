"""Tests for the local receive-MTU diagnostics."""

from __future__ import annotations


import dataclasses
import unittest


from airpods_hr.aap import AAPFrameSummary, DescriptorEvidence, HandshakeObservation


from airpods_hr.aap_local_rx_diagnostics import AAP_POST_ACK_TYPE_17_LENGTH_LIMIT, AAPPostACKShapeObservation


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


