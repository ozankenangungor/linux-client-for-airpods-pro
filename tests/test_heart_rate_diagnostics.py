"""Tests for evidence-preserving heart-rate diagnostic capture."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from airpods_hr.heart_rate_diagnostics import (
    DIAGNOSTIC_SCHEMA_VERSION,
    DiagnosticOutputOpenError,
    DiagnosticStateError,
    HeartRateDiagnosticRecorder,
    JsonlDiagnosticSink,
)
from airpods_hr.heartrate import HeartRateReport, parse_heart_rate_packet
from airpods_hr.protocol import HEART_RATE_MARKER


RAW_REPORT = bytes.fromhex(
    "01 48 a5 34 12 5a 08 07 06 05 04 03 02 01 ef cd ab 89"
)


def parsed_report(raw_report: bytes = RAW_REPORT) -> HeartRateReport:
    """Produce a report through the single production parser."""

    return parse_heart_rate_packet(b"\x08\x01" + HEART_RATE_MARKER + raw_report)


class SequenceClock:
    def __init__(self, *values: int) -> None:
        self._values = iter(values)

    def __call__(self) -> int:
        return next(self._values)


class MemorySink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.close_count = 0

    def write_event(self, event: Mapping[str, Any]) -> None:
        self.events.append(dict(event))

    def close(self) -> None:
        self.close_count += 1


class DiagnosticRecorderTests(unittest.TestCase):
    def test_sink_failure_does_not_commit_native_lifecycle(self) -> None:
        class FailOnceSink(MemorySink):
            def __init__(self) -> None:
                super().__init__()
                self.fail_next = True

            def write_event(self, event: Mapping[str, Any]) -> None:
                if self.fail_next:
                    self.fail_next = False
                    raise OSError("synthetic sink failure")
                super().write_event(event)

        sink = FailOnceSink()
        recorder = self.make_recorder(sink, 0, 1, 2, 3, 4, 5)
        with self.assertRaises(OSError):
            recorder.start_session()
        self.assertFalse(recorder.session_started)
        recorder.start_session()
        sink.fail_next = True
        with self.assertRaises(OSError):
            recorder.record_sample(parsed_report())
        self.assertEqual(recorder.sample_events_emitted, 0)
        recorder.record_sample(parsed_report())
        sink.fail_next = True
        with self.assertRaises(OSError):
            recorder.stop_session("failed")
        self.assertFalse(recorder.session_stopped)
        recorder.stop_session("completed")
        self.assertTrue(recorder.session_stopped)
        self.assertEqual(recorder.sample_events_emitted, 1)

    def make_recorder(
        self,
        sink: MemorySink,
        *monotonic_values: int,
    ) -> HeartRateDiagnosticRecorder:
        return HeartRateDiagnosticRecorder(
            sink,
            monotonic_clock_ns=SequenceClock(*monotonic_values),
            utc_clock=lambda: datetime(
                2026,
                1,
                2,
                3,
                4,
                5,
                tzinfo=timezone.utc,
            ),
        )

    def test_session_events_use_deterministic_schema_and_timing(self) -> None:
        sink = MemorySink()
        recorder = self.make_recorder(
            sink,
            1_000_000_000,
            1_123_456_789,
            1_500_000_000,
        )

        recorder.start_session()
        sample = recorder.record_sample(parsed_report())
        recorder.stop_session("sigint")
        recorder.close()
        recorder.close()

        self.assertEqual(
            sink.events[0],
            {
                "schema_version": 1,
                "event": "session_start",
                "host_monotonic_reference_ns": 1_000_000_000,
                "wall_clock_utc": "2026-01-02T03:04:05Z",
            },
        )
        self.assertEqual(sample.host_monotonic_ns, 1_123_456_789)
        self.assertEqual(sample.elapsed_ms, 123)
        self.assertEqual(
            sink.events[1],
            {
                "schema_version": 1,
                "event": "heart_rate_sample",
                "host_monotonic_ns": 1_123_456_789,
                "elapsed_ms": 123,
                "bpm": 72,
                "aux": 0xA5,
                "sequence": 0x1234,
                "field_5": 0x5A,
                "timestamp_ticks": 0x0102030405060708,
                "flags": 0x89ABCDEF,
                "raw_report_hex": RAW_REPORT.hex(),
            },
        )
        self.assertEqual(
            sink.events[2],
            {
                "schema_version": 1,
                "event": "session_stop",
                "host_monotonic_ns": 1_500_000_000,
                "elapsed_ms": 500,
                "heart_rate_samples_emitted": 1,
                "termination_reason": "sigint",
            },
        )
        self.assertEqual(sink.close_count, 1)

    def test_multiple_parsed_reports_produce_one_event_each(self) -> None:
        sink = MemorySink()
        recorder = self.make_recorder(sink, 0, 1, 2, 3)
        first = parsed_report()
        second_raw = bytes((1, 99)) + RAW_REPORT[2:]
        second = parsed_report(second_raw)

        recorder.start_session()
        recorder.record_sample(first)
        recorder.record_sample(second)
        recorder.stop_session("completed")

        sample_events = [
            event
            for event in sink.events
            if event["event"] == "heart_rate_sample"
        ]
        self.assertEqual(len(sample_events), 2)
        self.assertEqual([event["bpm"] for event in sample_events], [72, 99])
        self.assertEqual(recorder.sample_events_emitted, 2)

    def test_sample_values_remain_numbers_and_raw_hex_is_exact(self) -> None:
        sink = MemorySink()
        recorder = self.make_recorder(sink, 10, 20)
        recorder.start_session()

        sample = recorder.record_sample(parsed_report())
        event = sample.as_event()

        numeric_keys = {
            "schema_version",
            "host_monotonic_ns",
            "elapsed_ms",
            "bpm",
            "aux",
            "sequence",
            "field_5",
            "timestamp_ticks",
            "flags",
        }
        self.assertTrue(all(type(event[key]) is int for key in numeric_keys))
        self.assertEqual(event["schema_version"], DIAGNOSTIC_SCHEMA_VERSION)
        self.assertEqual(event["raw_report_hex"], RAW_REPORT.hex())
        self.assertEqual(len(event["raw_report_hex"]), 36)

    def test_human_output_is_deterministic_and_uses_canonical_values(self) -> None:
        sink = MemorySink()
        recorder = self.make_recorder(sink, 100, 2_000_100)
        recorder.start_session()

        sample = recorder.record_sample(parsed_report())

        self.assertEqual(
            sample.format_human(),
            "Heart rate diagnostic: host_monotonic_ns=2000100 elapsed_ms=2 "
            "bpm=72 aux=165 sequence=4660 field_5=90 "
            "timestamp_ticks=72623859790382856 flags=2309737967 "
            f"raw_report_hex={RAW_REPORT.hex()}",
        )

    def test_allowlisted_events_do_not_serialize_secrets(self) -> None:
        sink = MemorySink()
        recorder = self.make_recorder(sink, 0, 1, 2)

        recorder.start_session()
        recorder.record_sample(parsed_report())
        recorder.stop_session("completed")

        serialized = json.dumps(sink.events).lower()
        for forbidden in (
            "linkkey",
            "pairing",
            "credential",
            "device_identifier",
            "mac_address",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_report_without_retained_payload_is_rejected(self) -> None:
        sink = MemorySink()
        recorder = self.make_recorder(sink, 0)
        recorder.start_session()
        report = HeartRateReport(72, 0, 0, 0, 0, 0)

        with self.assertRaises(DiagnosticStateError):
            recorder.record_sample(report)

    def test_jsonl_sink_writes_independently_valid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.jsonl"
            recorder = HeartRateDiagnosticRecorder(
                JsonlDiagnosticSink.open(output),
                monotonic_clock_ns=SequenceClock(100, 200, 300),
                utc_clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            recorder.start_session()
            recorder.record_sample(parsed_report())
            recorder.stop_session("completed")
            recorder.close()

            lines = output.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in lines]

        self.assertEqual(len(events), 3)
        self.assertEqual(
            [event["event"] for event in events],
            ["session_start", "heart_rate_sample", "session_stop"],
        )
        self.assertTrue(
            all(
                event["schema_version"] == DIAGNOSTIC_SCHEMA_VERSION
                for event in events
            )
        )

    def test_jsonl_sink_refuses_to_overwrite_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "capture.jsonl"
            output.write_text("existing evidence\n", encoding="utf-8")

            with self.assertRaises(DiagnosticOutputOpenError):
                JsonlDiagnosticSink.open(output)

            self.assertEqual(
                output.read_text(encoding="utf-8"), "existing evidence\n"
            )


if __name__ == "__main__":
    unittest.main()
