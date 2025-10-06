"""Tests for hardware-independent heart-rate report parsing."""

import unittest

from airpods_hr.heartrate import (
    HeartRateMarkerNotFoundError,
    HeartRateReport,
    HeartRateReportIDError,
    HeartRateReportTruncatedError,
    parse_heart_rate_packet,
)
from airpods_hr.protocol import HEART_RATE_MARKER


def make_report(*, report_id: int = 0x01) -> bytes:
    """Build a synthetic report containing values with clear byte order."""

    return b"".join(
        (
            bytes((report_id, 72, 0xA5)),
            (0x1234).to_bytes(2, "little"),
            bytes((0x5A,)),
            (0x0102030405060708).to_bytes(8, "little"),
            (0x89ABCDEF).to_bytes(4, "little"),
        )
    )


class ParseHeartRatePacketTests(unittest.TestCase):
    def assert_decoded_fields(self, parsed: HeartRateReport) -> None:
        self.assertEqual(parsed.bpm, 72)
        self.assertEqual(parsed.aux, 0xA5)
        self.assertEqual(parsed.sequence, 0x1234)
        self.assertEqual(parsed.field_5, 0x5A)
        self.assertEqual(parsed.timestamp_ticks, 0x0102030405060708)
        self.assertEqual(parsed.flags, 0x89ABCDEF)

    def test_valid_sample_after_one_byte_outer_varint(self) -> None:
        packet = b"\x08\x7f\x12\x01\x00" + HEART_RATE_MARKER + make_report()

        parsed = parse_heart_rate_packet(packet)

        self.assert_decoded_fields(parsed)

    def test_valid_sample_after_multi_byte_outer_varint(self) -> None:
        packet = b"\x08\x80\x01\x12\x01\x00" + HEART_RATE_MARKER + make_report()

        parsed = parse_heart_rate_packet(packet)

        self.assert_decoded_fields(parsed)

    def test_missing_marker_is_rejected(self) -> None:
        with self.assertRaises(HeartRateMarkerNotFoundError):
            parse_heart_rate_packet(b"\x08\x01" + make_report())

    def test_truncated_report_is_rejected(self) -> None:
        packet = b"\x08\x01" + HEART_RATE_MARKER + make_report()[:-1]

        with self.assertRaises(HeartRateReportTruncatedError):
            parse_heart_rate_packet(packet)

    def test_unexpected_report_id_is_rejected(self) -> None:
        packet = b"\x08\x01" + HEART_RATE_MARKER + make_report(report_id=0x02)

        with self.assertRaises(HeartRateReportIDError):
            parse_heart_rate_packet(packet)


if __name__ == "__main__":
    unittest.main()
