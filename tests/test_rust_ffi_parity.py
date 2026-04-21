"""Production Rust delegation and historical parser compatibility tests."""

from __future__ import annotations

import csv
import inspect
import importlib.machinery
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields
from unittest.mock import patch
from pathlib import Path
import tomllib
import unittest

import airpods_hr._airpods_aap_core as rust_core
import airpods_hr
from airpods_hr.heartrate import (
    HeartRateParseError,
    HeartRateReport,
    HeartRateMarkerNotFoundError,
    HeartRateReportIDError,
    HeartRateReportTruncatedError,
    parse_heart_rate_packet as parse_public,
)
from airpods_hr.protocol import HEART_RATE_MARKER


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/testdata/hr_report_golden.tsv"
FIELDS = (
    "bpm",
    "aux",
    "sequence",
    "field_5",
    "timestamp_ticks",
    "flags",
    "raw_report",
)


def golden_cases() -> list[dict[str, str]]:
    with CORPUS.open(encoding="ascii", newline="") as fixture:
        return list(csv.DictReader(fixture, delimiter="\t"))


def make_packet(
    *,
    bpm: int,
    aux: int,
    sequence: int,
    field_5: int,
    timestamp_ticks: int,
    flags: int,
    prefix: bytes = b"\x08\x7f\x12\x01\x00",
    suffix: bytes = b"",
) -> bytes:
    report = b"".join(
        (
            bytes((1, bpm, aux)),
            sequence.to_bytes(2, "little"),
            bytes((field_5,)),
            timestamp_ticks.to_bytes(8, "little"),
            flags.to_bytes(4, "little"),
        )
    )
    return prefix + HEART_RATE_MARKER + report + suffix


class NativeExtensionTests(unittest.TestCase):
    def assert_equal_results(self, packet: bytes) -> None:
        python_result = parse_public(packet)
        rust_result = rust_core.parse_heart_rate_packet(packet)
        for field in FIELDS:
            self.assertEqual(
                getattr(rust_result, field),
                getattr(python_result, field),
                field,
            )

    def test_extension_is_real_compiled_ffi(self) -> None:
        self.assertTrue(inspect.isbuiltin(rust_core.parse_heart_rate_packet))
        self.assertTrue(
            any(
                rust_core.__file__.endswith(suffix)
                for suffix in importlib.machinery.EXTENSION_SUFFIXES
            )
        )

    def test_all_successful_golden_vectors_match_exactly(self) -> None:
        for case in golden_cases():
            if case["outcome"] == "valid":
                with self.subTest(case=case["name"]):
                    self.assert_equal_results(bytes.fromhex(case["packet_hex"]))

    def test_all_failure_golden_vectors_match_categories(self) -> None:
        python_errors = {
            "marker_not_found": HeartRateMarkerNotFoundError,
            "invalid_length": HeartRateReportTruncatedError,
            "invalid_report_id": HeartRateReportIDError,
        }
        rust_errors = {
            "marker_not_found": rust_core.MarkerNotFoundError,
            "invalid_length": rust_core.TruncatedReportError,
            "invalid_report_id": rust_core.InvalidReportIdError,
        }
        for case in golden_cases():
            if case["outcome"] != "valid":
                packet = bytes.fromhex(case["packet_hex"])
                with self.subTest(case=case["name"]):
                    with self.assertRaises(python_errors[case["outcome"]]):
                        parse_public(packet)
                    with self.assertRaises(rust_errors[case["outcome"]]):
                        rust_core.parse_heart_rate_packet(packet)

    def test_raw_report_is_exact_source_slice_with_trailing_bytes(self) -> None:
        packet = make_packet(
            bpm=68,
            aux=20,
            sequence=0x7FFF,
            field_5=2,
            timestamp_ticks=0x7FFF_FFFF_FFFF_FFFF,
            flags=0x8182_1001,
            suffix=b"ignored trailing frame bytes",
        )
        report_offset = packet.index(HEART_RATE_MARKER) + len(HEART_RATE_MARKER)
        parsed = rust_core.parse_heart_rate_packet(packet)
        self.assertEqual(parsed.raw_report, packet[report_offset : report_offset + 18])
        self.assert_equal_results(packet)

    def test_actual_integer_width_edges_match(self) -> None:
        packet = make_packet(
            bpm=255,
            aux=255,
            sequence=0xFFFF,
            field_5=255,
            timestamp_ticks=0xFFFF_FFFF_FFFF_FFFF,
            flags=0xFFFF_FFFF,
        )
        self.assert_equal_results(packet)
        parsed = rust_core.parse_heart_rate_packet(packet)
        self.assertEqual(parsed.sequence, 0xFFFF)
        self.assertEqual(parsed.timestamp_ticks, 0xFFFF_FFFF_FFFF_FFFF)
        self.assertEqual(parsed.flags, 0xFFFF_FFFF)

    def test_source_side_derivation_preserves_field_5(self) -> None:
        for raw, expected in (
            (1, ("left", None)),
            (2, ("right", None)),
            (0, ("unknown", 0)),
            (127, ("unknown", 127)),
            (255, ("unknown", 255)),
        ):
            with self.subTest(field_5=raw):
                parsed = rust_core.parse_heart_rate_packet(
                    make_packet(
                        bpm=68,
                        aux=20,
                        sequence=1,
                        field_5=raw,
                        timestamp_ticks=1,
                        flags=0,
                    )
                )
                self.assertEqual(parsed.field_5, raw)
                self.assertEqual(parsed.source_side(), expected)

    def test_bpm_169_is_unchanged(self) -> None:
        parsed = rust_core.parse_heart_rate_packet(
            make_packet(
                bpm=169,
                aux=20,
                sequence=0,
                field_5=1,
                timestamp_ticks=0,
                flags=0x8182_1001,
            )
        )
        self.assertEqual(parsed.bpm, 169)

    def test_duplicate_packets_produce_equivalent_independent_results(self) -> None:
        packet = make_packet(
            bpm=68,
            aux=20,
            sequence=1,
            field_5=2,
            timestamp_ticks=1_000_000_000,
            flags=0x0000_1000,
        )
        first = rust_core.parse_heart_rate_packet(packet)
        second = rust_core.parse_heart_rate_packet(packet)
        self.assertIsNot(first, second)
        self.assertEqual(
            tuple(getattr(first, field) for field in FIELDS),
            tuple(getattr(second, field) for field in FIELDS),
        )

    def test_deterministic_generated_corpus_matches(self) -> None:
        bpms = (0, 1, 68, 169, 255)
        aux_values = (0, 20, 255)
        sequences = (0, 1, 0x7FFF, 0xFFFE, 0xFFFF)
        field_5_values = (0, 1, 2, 127, 255)
        timestamps = (0, 1, 1_000_000_000, 0x7FFF_FFFF_FFFF_FFFF, 0xFFFF_FFFF_FFFF_FFFF)
        flags_values = (0, 0x0000_1000, 0x0000_2000, 0x8182_1001, 0xFFFF_FFFF)

        for index in range(30):
            packet = make_packet(
                bpm=bpms[index % len(bpms)],
                aux=aux_values[(index // len(bpms)) % len(aux_values)],
                sequence=sequences[(index * 2) % len(sequences)],
                field_5=field_5_values[(index * 3) % len(field_5_values)],
                timestamp_ticks=timestamps[(index * 4) % len(timestamps)],
                flags=flags_values[index % len(flags_values)],
                prefix=b"\x08\x80\x01\x12\x01\x00" if index % 2 else b"\x08\x7f",
                suffix=b"\xde\xad\xbe\xef" if index % 3 == 0 else b"",
            )
            with self.subTest(index=index):
                self.assert_equal_results(packet)
                parsed = parse_public(packet)
                self.assertEqual(
                    tuple(getattr(parsed, name) for name in FIELDS[:-1]),
                    (
                        bpms[index % len(bpms)],
                        aux_values[(index // len(bpms)) % len(aux_values)],
                        sequences[(index * 2) % len(sequences)],
                        field_5_values[(index * 3) % len(field_5_values)],
                        timestamps[(index * 4) % len(timestamps)],
                        flags_values[index % len(flags_values)],
                    ),
                )

    def test_generated_truncations_and_report_ids_preserve_error_messages(self) -> None:
        for length in range(18):
            # Length validation precedes ID validation.
            with (
                self.subTest(length=length),
                self.assertRaisesRegex(
                    HeartRateReportTruncatedError,
                    f"^heart-rate report is truncated: expected 18 bytes, found {length}$",
                ),
            ):
                parse_public(HEART_RATE_MARKER + bytes(length))
        for report_id in range(256):
            if report_id == 1:
                continue
            with (
                self.subTest(report_id=report_id),
                self.assertRaisesRegex(
                    HeartRateReportIDError,
                    f"^unexpected heart-rate report ID: 0x{report_id:02x}$",
                ),
            ):
                parse_public(HEART_RATE_MARKER + bytes([report_id]) + bytes(17))

    def test_first_marker_wins_even_if_later_report_is_valid(self) -> None:
        valid = make_packet(
            bpm=68, aux=20, sequence=1, field_5=1, timestamp_ticks=1, flags=0
        )
        with self.assertRaises(HeartRateReportIDError):
            parse_public(HEART_RATE_MARKER + bytes(18) + valid)
        self.assertEqual(parse_public(valid + valid), parse_public(valid))

    def test_bytes_subclasses_remain_accepted(self) -> None:
        class Packet(bytes):
            pass

        packet = make_packet(
            bpm=68, aux=20, sequence=1, field_5=1, timestamp_ticks=1, flags=0
        )
        self.assertEqual(parse_public(Packet(packet)), parse_public(packet))

    def test_binding_accepts_only_python_bytes(self) -> None:
        for value in (
            bytearray(b"packet"),
            memoryview(b"packet"),
            "packet",
            None,
            1,
            [1],
            (1,),
        ):
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(TypeError):
                    rust_core.parse_heart_rate_packet(value)
                with self.assertRaisesRegex(
                    TypeError, "^packet must be a bytes object$"
                ):
                    parse_public(value)


class BindingBoundaryTests(unittest.TestCase):
    def test_binding_is_private_and_absent_from_airpods_hr_exports(self) -> None:
        self.assertNotIn("_airpods_aap_core", airpods_hr.__all__)
        self.assertEqual(
            set(airpods_hr.__all__),
            {
                "HeartRateParseError",
                "HeartRateMarkerNotFoundError",
                "HeartRateReportTruncatedError",
                "HeartRateReportIDError",
                "HeartRateReport",
                "parse_heart_rate_packet",
            },
        )

    def test_core_is_pyo3_free_and_binding_dependency_is_one_way(self) -> None:
        workspace = tomllib.loads((ROOT / "Cargo.toml").read_text())
        self.assertEqual(
            workspace["workspace"]["members"],
            [
                "xtask",
                "crates/airpods-app-core",
                "crates/airpods-aap-core",
                "crates/airpods-aap-py",
                "crates/airpods-client",
                "crates/airpods-client-c",
                "crates/airpods-client-resilient",
                "crates/airpods-desktop",
                "crates/airpodsctl",
                "crates/airpods-hub-core",
                "crates/airpods-sdp-core",
            ],
        )

        core_manifest = tomllib.loads(
            (ROOT / "crates/airpods-aap-core/Cargo.toml").read_text()
        )
        binding_manifest = tomllib.loads(
            (ROOT / "crates/airpods-aap-py/Cargo.toml").read_text()
        )
        hub_manifest = tomllib.loads(
            (ROOT / "crates/airpods-hub-core/Cargo.toml").read_text()
        )
        app_manifest = tomllib.loads(
            (ROOT / "crates/airpods-app-core/Cargo.toml").read_text()
        )
        sdp_manifest = tomllib.loads(
            (ROOT / "crates/airpods-sdp-core/Cargo.toml").read_text()
        )
        ctl_manifest = tomllib.loads(
            (ROOT / "crates/airpodsctl/Cargo.toml").read_text()
        )
        resilient_manifest = tomllib.loads(
            (ROOT / "crates/airpods-client-resilient/Cargo.toml").read_text()
        )
        c_manifest = tomllib.loads(
            (ROOT / "crates/airpods-client-c/Cargo.toml").read_text()
        )
        self.assertFalse(c_manifest["package"]["publish"])
        self.assertEqual(c_manifest["package"]["edition"], "2024")
        self.assertEqual(c_manifest["lib"]["crate-type"], ["cdylib", "staticlib"])
        self.assertEqual(set(c_manifest["dependencies"]), {"airpods-client", "tokio"})
        self.assertEqual(
            c_manifest["dependencies"]["airpods-client"]["path"], "../airpods-client"
        )
        self.assertFalse(resilient_manifest["package"]["publish"])
        self.assertEqual(
            resilient_manifest["dependencies"]["airpods-client"]["path"],
            "../airpods-client",
        )
        self.assertEqual(
            set(resilient_manifest["dependencies"]), {"airpods-client", "tokio"}
        )
        self.assertFalse(ctl_manifest["package"]["publish"])
        self.assertEqual(
            ctl_manifest["dependencies"]["airpods-client"]["path"],
            "../airpods-client",
        )
        self.assertEqual(
            ctl_manifest["dependencies"]["airpods-client-resilient"]["path"],
            "../airpods-client-resilient",
        )
        self.assertFalse(
            set(ctl_manifest["dependencies"])
            & {
                "airpods-app-core",
                "airpods-aap-core",
                "airpods-aap-py",
                "airpods-hub-core",
                "airpods-sdp-core",
            }
        )
        self.assertEqual(core_manifest["dependencies"], {})
        core_source = "\n".join(
            path.read_text()
            for path in (ROOT / "crates/airpods-aap-core").rglob("*")
            if path.is_file()
        )
        self.assertNotIn("pyo3", core_source.lower())
        self.assertEqual(
            binding_manifest["dependencies"]["airpods-aap-core"]["path"],
            "../airpods-aap-core",
        )
        self.assertEqual(
            binding_manifest["dependencies"]["airpods-app-core"]["path"],
            "../airpods-app-core",
        )
        self.assertFalse(app_manifest["package"]["publish"])
        self.assertEqual(
            app_manifest["dependencies"],
            {"airpods-hub-core": {"path": "../airpods-hub-core"}},
        )
        app_source = "\n".join(
            path.read_text()
            for path in (ROOT / "crates/airpods-app-core/src").glob("*.rs")
        )
        for forbidden in (
            "pyo3", "tokio", "zbus", "bluer", "nix::", "std::fs",
            "std::process", "std::os::unix::net", "std::thread::sleep",
        ):
            self.assertNotIn(forbidden, app_source.lower())
        self.assertEqual(
            binding_manifest["dependencies"]["airpods-hub-core"]["path"],
            "../airpods-hub-core",
        )
        self.assertEqual(
            binding_manifest["dependencies"]["airpods-sdp-core"]["path"],
            "../airpods-sdp-core",
        )
        self.assertFalse(sdp_manifest["package"]["publish"])
        self.assertEqual(sdp_manifest.get("dependencies", {}), {})
        self.assertNotIn(
            "pyo3",
            (ROOT / "crates/airpods-sdp-core/src/lib.rs").read_text().lower(),
        )
        self.assertFalse(hub_manifest["package"]["publish"])
        self.assertNotIn("pyo3", hub_manifest.get("dependencies", {}))
        self.assertNotIn("tokio", hub_manifest.get("dependencies", {}))
        client_manifest = tomllib.loads(
            (ROOT / "crates/airpods-client/Cargo.toml").read_text()
        )
        self.assertNotIn("airpods-hub-core", client_manifest["dependencies"])
        self.assertEqual(
            binding_manifest["dependencies"]["pyo3"]["version"],
            "=0.29.2",
        )
        self.assertFalse(core_manifest["package"]["publish"])
        self.assertFalse(binding_manifest["package"]["publish"])

    def test_production_build_includes_private_extension(self) -> None:
        manifest = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(manifest["build-system"]["build-backend"], "maturin")
        self.assertEqual(
            manifest["tool"]["maturin"]["module-name"], "airpods_hr._airpods_aap_core"
        )
        self.assertFalse((ROOT / "crates/airpods-aap-py/pyproject.toml").exists())

    def test_public_parser_calls_compiled_core_for_each_packet(self) -> None:
        packet = make_packet(
            bpm=169, aux=20, sequence=1, field_5=2, timestamp_ticks=1, flags=0
        )
        native_parse = rust_core.parse_heart_rate_packet
        self.assertTrue(inspect.isbuiltin(native_parse))
        with patch.object(
            rust_core, "parse_heart_rate_packet", wraps=native_parse
        ) as call:
            first = parse_public(packet)
            second = parse_public(packet)
        self.assertEqual(call.call_count, 2)
        call.assert_called_with(packet)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertIs(type(first), HeartRateReport)

    def test_unexpected_native_failure_is_not_hidden_by_fallback(self) -> None:
        with patch.object(
            rust_core,
            "parse_heart_rate_packet",
            side_effect=RuntimeError("native failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "^native failure$"):
                parse_public(b"packet")

    def test_missing_extension_fails_public_import_explicitly(self) -> None:
        code = """
import importlib.abc
import sys
class BlockNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "airpods_hr._airpods_aap_core":
            raise ModuleNotFoundError("required native extension unavailable")
sys.meta_path.insert(0, BlockNative())
import airpods_hr.heartrate
"""
        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("required native extension unavailable", result.stderr)

    def test_public_dataclass_and_exception_hierarchy_are_preserved(self) -> None:
        self.assertEqual(tuple(field.name for field in fields(HeartRateReport)), FIELDS)
        report = HeartRateReport(1, 2, 3, 4, 5, 6)
        self.assertEqual(report.raw_report, b"")
        self.assertNotIn("raw_report", repr(report))
        self.assertFalse(hasattr(report, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            report.bpm = 2
        for error in (
            HeartRateMarkerNotFoundError,
            HeartRateReportTruncatedError,
            HeartRateReportIDError,
        ):
            self.assertEqual(error.__bases__, (HeartRateParseError,))
        self.assertEqual(HeartRateParseError.__bases__, (ValueError,))





if __name__ == "__main__":
    unittest.main()
