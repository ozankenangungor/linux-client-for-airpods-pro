"""Hardware-independent tests for the minimal AAP handshake layer."""

from __future__ import annotations


import unittest

from dataclasses import fields


from airpods_hr.aap import AAPFrameSummary


ALL_DESCRIPTOR_EVIDENCE = (
    b"\x00AccessoryService\x00HeartRateService\x00HeartRate\x00"
    b"com.apple.hid.heartrate-access\x00"
)


def make_synthetic_type_2b_frame(
    suffixes: list[tuple[int, int]],
    *,
    declared_body_length: int | None = None,
    trailing_bytes: bytes = b"",
    hidden_fields: list[bytes] | None = None,
) -> bytes:
    """Build a sanitized structural fixture without historical frame content."""

    if hidden_fields is None:
        hidden_fields = [b"SAFE!!"] * len(suffixes)
    if len(hidden_fields) != len(suffixes):
        raise ValueError("one hidden-field fixture is required per unit")

    body = bytearray()
    for index, ((suffix_u8, suffix_u16), hidden_field) in enumerate(
        zip(suffixes, hidden_fields, strict=True)
    ):
        if len(hidden_field) != 6:
            raise ValueError("hidden-field fixtures must contain six bytes")
        unit = bytearray(17)
        for byte_index in range(14):
            unit[byte_index] = (index * 19 + byte_index * 7 + 3) & 0xFF
        unit[8:14] = hidden_field
        unit[14] = suffix_u8
        unit[15] = suffix_u16 & 0xFF
        unit[16] = suffix_u16 >> 8
        body.extend(unit)
    body.extend(trailing_bytes)

    declared_length = (
        len(body) if declared_body_length is None else declared_body_length
    )
    header = bytearray(17)
    header[2:4] = (4).to_bytes(2, "little")
    header[4:6] = (0x002B).to_bytes(2, "little")
    header[6] = 0x05
    header[7:9] = declared_length.to_bytes(2, "little")
    return bytes(header + body)


class AAPFrameSummaryTests(unittest.TestCase):
    def test_summary_exposes_only_length_and_neutral_header_fields(self) -> None:
        synthetic_payload = bytes.fromhex("04 00 04 00 2B 00") + bytes(351)

        summary = AAPFrameSummary.from_frame(synthetic_payload)

        self.assertEqual(summary.length, 357)
        self.assertEqual(summary.header_u16_2_3, 0x0004)
        self.assertEqual(summary.header_u16_4_5, 0x002B)
        self.assertNotIn(synthetic_payload.hex(), repr(summary))

    def test_short_frame_exposes_length_only(self) -> None:
        summary = AAPFrameSummary.from_frame(b"\xAA\xBB\xCC")

        self.assertEqual(summary.length, 3)
        self.assertIsNone(summary.header_u16_2_3)
        self.assertIsNone(summary.header_u16_4_5)

    def test_partially_available_header_exposes_only_complete_field(self) -> None:
        summary = AAPFrameSummary.from_frame(bytes.fromhex("AA BB 34 12 CC"))

        self.assertEqual(summary.header_u16_2_3, 0x1234)
        self.assertIsNone(summary.header_u16_4_5)


class AAPType2BFrameSummaryTests(unittest.TestCase):
    def test_non_type_2b_frame_has_no_structural_summary(self) -> None:
        frame = bytearray(20)
        frame[4:6] = (0x002A).to_bytes(2, "little")

        self.assertIsNone(AAPFrameSummary.from_frame(bytes(frame)).type_2b_summary)

    def test_short_type_2b_frame_is_safe(self) -> None:
        summary = AAPFrameSummary.from_frame(
            bytes.fromhex("00 00 04 00 2B 00")
        ).type_2b_summary

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.frame_length, 6)
        self.assertIsNone(summary.header_u8_6)
        self.assertIsNone(summary.declared_body_length_u16_7_8)
        self.assertIsNone(summary.actual_body_length_after_offset_17)
        self.assertIsNone(summary.record_count_17)
        self.assertEqual(summary.record_suffix_histogram, ())

    def test_synthetic_357_byte_frame_has_twenty_aligned_units(self) -> None:
        frame = make_synthetic_type_2b_frame([(0x21, 0x3100)] * 20)

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.frame_length, 357)
        self.assertEqual(summary.declared_body_length_u16_7_8, 340)
        self.assertEqual(summary.actual_body_length_after_offset_17, 340)
        self.assertTrue(summary.declared_body_length_consistent)
        self.assertTrue(summary.body_aligned_to_17_bytes)
        self.assertEqual(summary.record_count_17, 20)

    def test_synthetic_51_byte_frame_has_two_aligned_units(self) -> None:
        frame = make_synthetic_type_2b_frame(
            [(0x10, 0x1100), (0x20, 0x2200)]
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.frame_length, 51)
        self.assertEqual(summary.declared_body_length_u16_7_8, 34)
        self.assertEqual(summary.actual_body_length_after_offset_17, 34)
        self.assertTrue(summary.declared_body_length_consistent)
        self.assertTrue(summary.body_aligned_to_17_bytes)
        self.assertEqual(summary.record_count_17, 2)

    def test_declared_length_smaller_than_actual_is_inconsistent(self) -> None:
        frame = make_synthetic_type_2b_frame(
            [(0x10, 0x1000)], declared_body_length=16
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        assert summary is not None
        self.assertFalse(summary.declared_body_length_consistent)
        self.assertIsNone(summary.record_count_17)
        self.assertEqual(summary.record_suffix_histogram, ())

    def test_declared_length_larger_than_actual_is_inconsistent(self) -> None:
        frame = make_synthetic_type_2b_frame(
            [(0x10, 0x1000)], declared_body_length=18
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        assert summary is not None
        self.assertFalse(summary.declared_body_length_consistent)
        self.assertIsNone(summary.record_count_17)

    def test_unaligned_body_has_no_record_count(self) -> None:
        frame = make_synthetic_type_2b_frame(
            [(0x10, 0x1000)], trailing_bytes=b"x"
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        assert summary is not None
        self.assertTrue(summary.declared_body_length_consistent)
        self.assertFalse(summary.body_aligned_to_17_bytes)
        self.assertIsNone(summary.record_count_17)
        self.assertIsNone(summary.record_suffix_distinct_count)

    def test_suffix_histogram_counts_and_sorts_pairs(self) -> None:
        frame = make_synthetic_type_2b_frame(
            [(0x20, 0x3000), (0x10, 0x2000), (0x20, 0x3000)]
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        assert summary is not None
        self.assertEqual(summary.record_suffix_distinct_count, 2)
        self.assertEqual(
            [
                (item.suffix_field_u8, item.suffix_field_u16, item.count)
                for item in summary.record_suffix_histogram
            ],
            [(0x10, 0x2000, 1), (0x20, 0x3000, 2)],
        )

    def test_suffix_histogram_is_deterministically_bounded(self) -> None:
        suffixes = [(value, 0x2000 + value) for value in range(10, -1, -1)]

        summary = AAPFrameSummary.from_frame(
            make_synthetic_type_2b_frame(suffixes)
        ).type_2b_summary

        assert summary is not None
        self.assertEqual(summary.record_suffix_distinct_count, 11)
        self.assertEqual(len(summary.record_suffix_histogram), 8)
        self.assertEqual(
            [item.suffix_field_u8 for item in summary.record_suffix_histogram],
            list(range(8)),
        )

    def test_summary_dataclasses_retain_no_record_prefix_bytes(self) -> None:
        hidden_marker = b"QZ9mK!"
        frame = make_synthetic_type_2b_frame(
            [(0x10, 0x2000)], hidden_fields=[hidden_marker]
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        assert summary is not None
        allowed_types = (int, bool, type(None), tuple)
        self.assertTrue(
            all(
                isinstance(getattr(summary, field.name), allowed_types)
                for field in fields(summary)
            )
        )
        self.assertNotIn(hidden_marker.decode(), repr(summary))
        self.assertNotIn(hidden_marker.hex(), repr(summary).lower())
        self.assertNotIn(frame.hex(), repr(summary).lower())

    def test_hidden_unit_field_reports_only_uniformity(self) -> None:
        uniform = AAPFrameSummary.from_frame(
            make_synthetic_type_2b_frame(
                [(0x10, 0x2000), (0x11, 0x2001)],
                hidden_fields=[b"SAFE!!", b"SAFE!!"],
            )
        ).type_2b_summary
        differing = AAPFrameSummary.from_frame(
            make_synthetic_type_2b_frame(
                [(0x10, 0x2000), (0x11, 0x2001)],
                hidden_fields=[b"SAFE!!", b"OTHER!"],
            )
        ).type_2b_summary

        assert uniform is not None and differing is not None
        self.assertTrue(uniform.unit_bytes_8_13_uniform)
        self.assertFalse(differing.unit_bytes_8_13_uniform)


