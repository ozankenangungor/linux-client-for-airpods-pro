"""Hardware-independent tests for telemetry semantics capture."""

from __future__ import annotations


import json
import tempfile
import unittest


from pathlib import Path


from airpods_hr.heart_rate_diagnostics import JsonlDiagnosticSink


from airpods_hr.heartrate import HeartRateReport, parse_heart_rate_packet
from airpods_hr.hr_semantics import PARSER_STRUCTURE, HRSemanticsRecorder, HRSemanticsScenario


from airpods_hr.protocol import HEART_RATE_MARKER, HEART_RATE_REPORT_ID, HEART_RATE_REPORT_SIZE


LOCAL_ADDRESS = "00:11:22:33:44:55"
REMOTE_ADDRESS = "AA:BB:CC:DD:EE:FF"


class MemorySink:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.close_calls = 0

    def write_event(self, event) -> None:
        self.events.append(dict(event))

    def close(self) -> None:
        self.close_calls += 1


class ClockNS:
    def __init__(self, values: list[int]) -> None:
        self.values = list(values)

    def __call__(self) -> int:
        return self.values.pop(0)


def raw_report(
    bpm: int,
    sequence: int,
    *,
    aux: int = 2,
    field_5: int = 3,
    timestamp_ticks: int = 100,
    flags: int = 0,
) -> bytes:
    report = bytearray(HEART_RATE_REPORT_SIZE)
    report[0] = HEART_RATE_REPORT_ID
    report[1] = bpm
    report[2] = aux
    report[3:5] = sequence.to_bytes(2, "little")
    report[5] = field_5
    report[6:14] = timestamp_ticks.to_bytes(8, "little")
    report[14:18] = flags.to_bytes(4, "little")
    return bytes(report)


def parsed_report(*args, **kwargs) -> HeartRateReport:
    return parse_heart_rate_packet(
        HEART_RATE_MARKER + raw_report(*args, **kwargs)
    )


def make_recorder(
    sink: MemorySink,
    *,
    scenario: HRSemanticsScenario,
    clock: ClockNS | None = None,
) -> HRSemanticsRecorder:
    return HRSemanticsRecorder(
        sink,
        scenario=scenario,
        requested_samples=(
            30 if scenario is HRSemanticsScenario.BASELINE else None
        ),
        requested_samples_per_cycle=(
            10
            if scenario is HRSemanticsScenario.ACTIVATION_RESTART
            else None
        ),
        restart_delay_seconds=5,
        monotonic_clock_ns=clock or ClockNS([0] * 100),
    )


class SemanticsRecorderTests(unittest.TestCase):
    def test_parser_widths_are_derived_from_frozen_parser(self) -> None:
        self.assertEqual(PARSER_STRUCTURE.canonical_report_length, 18)
        self.assertEqual(PARSER_STRUCTURE.sequence_width_bits, 16)
        self.assertEqual(PARSER_STRUCTURE.timestamp_width_bits, 64)
        self.assertEqual(PARSER_STRUCTURE.flags_width_bits, 32)

    def test_records_exact_fields_timing_flags_and_modulo_deltas(self) -> None:
        sink = MemorySink()
        recorder = make_recorder(
            sink,
            scenario=HRSemanticsScenario.BASELINE,
            clock=ClockNS([1_000_000_000, 1_250_000_000, 1_750_000_000]),
        )
        recorder.write_header(descriptor_complete=True, local_rx_imtu=2048)
        recorder.mark_cycle_attempted(1)
        recorder.begin_cycle(1)
        first = recorder.record_sample(
            parsed_report(
                169,
                0xFFFF,
                aux=7,
                field_5=9,
                timestamp_ticks=(1 << 64) - 1,
                flags=0x81800000,
            )
        )
        second = recorder.record_sample(
            parsed_report(
                42,
                0,
                aux=8,
                field_5=10,
                timestamp_ticks=1,
                flags=3,
            )
        )

        self.assertEqual([r.bpm for r in recorder.records], [169, 42])
        self.assertEqual(first.sample_index_within_cycle, 1)
        self.assertEqual(first.sample_index_global, 1)
        self.assertIsNone(first.delta_from_previous_report_ms)
        self.assertEqual(first.milliseconds_since_activation_ack, 250)
        self.assertEqual(first.flags_decimal, 0x81800000)
        self.assertEqual(first.flags_hex, "0x81800000")
        self.assertEqual(first.flags_bits_set, (23, 24, 31))
        self.assertEqual(first.raw_report_bytes, 18)
        self.assertEqual(first.raw_report_hex, first.raw_report_hex.lower())
        self.assertEqual(second.delta_from_previous_report_ms, 500)
        self.assertEqual(second.milliseconds_since_activation_ack, 750)
        self.assertEqual(second.sequence_delta_modulo, 1)
        self.assertEqual(second.timestamp_delta_modulo, 2)
        self.assertEqual(second.aux, 8)
        self.assertEqual(second.field_5, 10)
        self.assertEqual(second.flags_bits_set, (0, 1))

    def test_duplicate_reports_remain_ordered_and_unfiltered(self) -> None:
        sink = MemorySink()
        recorder = make_recorder(
            sink,
            scenario=HRSemanticsScenario.BASELINE,
            clock=ClockNS([0, 1_000_000, 2_000_000]),
        )
        recorder.write_header(descriptor_complete=True, local_rx_imtu=2048)
        recorder.mark_cycle_attempted(1)
        recorder.begin_cycle(1)
        report = parsed_report(169, 0, field_5=2)
        first = recorder.record_sample(report)
        second = recorder.record_sample(report)
        self.assertFalse(first.duplicate_parsed_report)
        self.assertTrue(second.duplicate_parsed_report)
        self.assertEqual(len(recorder.records), 2)
        self.assertEqual([record.bpm for record in recorder.records], [169, 169])

    def test_header_sample_and_summary_are_safe_independent_json_lines(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "semantics.jsonl"
            recorder = HRSemanticsRecorder(
                JsonlDiagnosticSink.open(path),
                scenario=HRSemanticsScenario.BASELINE,
                requested_samples=1,
                requested_samples_per_cycle=None,
                restart_delay_seconds=5,
                monotonic_clock_ns=ClockNS([0, 1_000_000]),
            )
            recorder.write_header(descriptor_complete=True, local_rx_imtu=2048)
            recorder.mark_cycle_attempted(1)
            recorder.begin_cycle(1)
            recorder.record_sample(parsed_report(169, 0))
            recorder.complete_cycle(1)
            summary = recorder.write_summary(
                status="complete", failure_category=None
            )
            recorder.close()

            events = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(
            [event["record_type"] for event in events],
            ["capture_header", "sample", "capture_summary"],
        )
        self.assertEqual(events[0]["local_rx_imtu"], 2048)
        self.assertTrue(events[0]["descriptor_complete"])
        self.assertEqual(events[1]["raw_report_bytes"], 18)
        self.assertEqual(summary["first_bpm_per_cycle"], [169])
        self.assertEqual(summary["last_bpm_per_cycle"], [169])
        serialized = json.dumps(events)
        for forbidden in (LOCAL_ADDRESS, REMOTE_ADDRESS, "LinkKey", "address"):
            self.assertNotIn(forbidden, serialized)

    def test_restart_summary_reports_only_mechanical_cycle_evidence(self) -> None:
        sink = MemorySink()
        recorder = make_recorder(
            sink,
            scenario=HRSemanticsScenario.ACTIVATION_RESTART,
            clock=ClockNS([0, 1, 2, 3]),
        )
        recorder.write_header(descriptor_complete=True, local_rx_imtu=2048)
        recorder.mark_cycle_attempted(1)
        recorder.begin_cycle(1)
        recorder.record_sample(parsed_report(80, 7, field_5=3, flags=4))
        recorder.complete_cycle(1)
        recorder.mark_cycle_attempted(2)
        recorder.begin_cycle(2)
        recorder.record_sample(parsed_report(90, 0, field_5=5, flags=8))
        recorder.complete_cycle(2)
        summary = recorder.write_summary(
            status="complete", failure_category=None
        )
        self.assertEqual(summary["cycles_completed"], 2)
        self.assertEqual(summary["first_bpm_per_cycle"], [80, 90])
        self.assertEqual(
            summary["sequence_reset_observed_between_cycles"], "yes"
        )
        self.assertEqual(
            summary["cycle_summaries"][0]["field_5_unique_values"], [3]
        )
        self.assertEqual(
            summary["cycle_summaries"][1]["flags_unique_hex_values"],
            ["0x00000008"],
        )
        self.assertEqual(summary["flags_bit_positions_observed"], [2, 3])


