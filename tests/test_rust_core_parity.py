"""Golden-vector parity checks for the public API and the historical Python contract."""

import csv
from pathlib import Path
import unittest

from airpods_hr.heartrate import (
    HeartRateMarkerNotFoundError,
    HeartRateReportIDError,
    HeartRateReportTruncatedError,
    parse_heart_rate_packet,
)


CORPUS = Path(__file__).parent / "testdata" / "hr_report_golden.tsv"


class RustCoreGoldenParityTests(unittest.TestCase):
    def test_public_parser_matches_shared_golden_corpus(self) -> None:
        with CORPUS.open(encoding="ascii", newline="") as fixture:
            cases = list(csv.DictReader(fixture, delimiter="\t"))

        self.assertTrue(cases)
        error_types = {
            "marker_not_found": HeartRateMarkerNotFoundError,
            "invalid_length": HeartRateReportTruncatedError,
            "invalid_report_id": HeartRateReportIDError,
        }

        for case in cases:
            packet = bytes.fromhex(case["packet_hex"])
            with self.subTest(case=case["name"]):
                if case["outcome"] != "valid":
                    with self.assertRaises(error_types[case["outcome"]]):
                        parse_heart_rate_packet(packet)
                    continue

                parsed = parse_heart_rate_packet(packet)
                self.assertEqual(parsed.bpm, int(case["bpm"]))
                self.assertEqual(parsed.aux, int(case["aux"]))
                self.assertEqual(parsed.sequence, int(case["sequence"]))
                self.assertEqual(parsed.field_5, int(case["field_5"]))
                self.assertEqual(parsed.timestamp_ticks, int(case["timestamp_ticks"]))
                self.assertEqual(parsed.flags, int(case["flags"]))
                self.assertEqual(len(parsed.raw_report), 18)
                marker = bytes.fromhex("3a1608131a12")
                report_offset = packet.index(marker) + len(marker)
                self.assertEqual(
                    parsed.raw_report, packet[report_offset : report_offset + 18]
                )


if __name__ == "__main__":
    unittest.main()
