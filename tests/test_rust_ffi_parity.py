"""Real native-extension parity tests for the private Rust core binding."""

from __future__ import annotations

import ast
import csv
import hashlib
import inspect
from pathlib import Path
import tomllib
import unittest

import _airpods_aap_core as rust_core
import airpods_hr
from airpods_hr.heartrate import (
    HeartRateMarkerNotFoundError,
    HeartRateReportIDError,
    HeartRateReportTruncatedError,
    parse_heart_rate_packet as parse_python,
)
from airpods_hr.protocol import HEART_RATE_MARKER
from tools.rust_shadow import (
    ShadowFailureCategory,
    compare_heart_rate_packet,
)


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "testdata/hr_report_golden.tsv"
FIELDS = (
    "bpm",
    "aux",
    "sequence",
    "field_5",
    "timestamp_ticks",
    "flags",
    "raw_report",
)
FROZEN_SHA256 = {
    "src/airpods_hr/production_session.py": (
        "f4141c6372c9bda65b4aca1b09c40e2f024ec8fe1b75964e8cc5f26c2156371e"
    ),
    "src/airpods_hr/bluez_coexistence.py": (
        "55824df2b95d698e60e972d52c500c7ef3e5901cb75793663bd6d9401e26305d"
    ),
    "src/airpods_hr/protocol.py": (
        "b4d1daea0582841e48ba9efc3a8a7d4d74bba9b69cdbf54d3767b8bb45afecca"
    ),
    "src/airpods_hr/heartrate.py": (
        "df0ddb9824146c7ab23eb30c2548aaa9ec7e8dc26461d76aaf92f2c19c3dc045"
    ),
    "src/airpods_hr/heart_rate_session.py": (
        "80e7031a8688444180dd23e3c22c9d7a062009869403aeb257602c07f547bf8f"
    ),
    "src/airpods_hr/monitor_cli.py": (
        "41332f411af2e89374b42047a2e009aef035ca2e8bce74440d0d9d891d7aded4"
    ),
    "src/airpods_hr/hr_semantics.py": (
        "f6004987032f02c5b2a7c59590a4e3e2edb5ef85788f8259f2e0b9499f5bc4ba"
    ),
    "tools/probe_hr_semantics.py": (
        "4e0fa4c54f3b29d376284e882225a0194f07fa79b8be5c2dec44d7bf73057e21"
    ),
}


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
        python_result = parse_python(packet)
        rust_result = rust_core.parse_heart_rate_packet(packet)
        for field in FIELDS:
            self.assertEqual(
                getattr(rust_result, field),
                getattr(python_result, field),
                field,
            )

    def test_extension_is_real_compiled_ffi(self) -> None:
        self.assertTrue(inspect.isbuiltin(rust_core.parse_heart_rate_packet))
        extension_files = list(Path(rust_core.__file__).parent.glob("*.so"))
        self.assertEqual(len(extension_files), 1)

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
                        parse_python(packet)
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

    def test_binding_accepts_only_python_bytes(self) -> None:
        for value in (bytearray(b"packet"), memoryview(b"packet"), "packet", None):
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(TypeError):
                    rust_core.parse_heart_rate_packet(value)


class ShadowHarnessTests(unittest.TestCase):
    def test_success_is_compared_explicitly(self) -> None:
        result = compare_heart_rate_packet(
            make_packet(
                bpm=68,
                aux=20,
                sequence=1,
                field_5=1,
                timestamp_ticks=1_000_000_000,
                flags=0x0000_1000,
            )
        )
        self.assertTrue(result.succeeded)
        self.assertIsNone(result.failure_category)

    def test_failure_is_compared_without_fallback(self) -> None:
        result = compare_heart_rate_packet(b"no marker")
        self.assertFalse(result.succeeded)
        self.assertEqual(
            result.failure_category,
            ShadowFailureCategory.MARKER_NOT_FOUND,
        )


class BindingBoundaryTests(unittest.TestCase):
    def test_binding_is_private_and_absent_from_airpods_hr_exports(self) -> None:
        self.assertNotIn("_airpods_aap_core", airpods_hr.__all__)
        self.assertFalse(hasattr(airpods_hr, "_airpods_aap_core"))

    def test_core_is_pyo3_free_and_binding_dependency_is_one_way(self) -> None:
        workspace = tomllib.loads((ROOT / "Cargo.toml").read_text())
        self.assertEqual(
            workspace["workspace"]["members"],
            ["crates/airpods-aap-core", "crates/airpods-aap-py"],
        )

        core_manifest = tomllib.loads(
            (ROOT / "crates/airpods-aap-core/Cargo.toml").read_text()
        )
        binding_manifest = tomllib.loads(
            (ROOT / "crates/airpods-aap-py/Cargo.toml").read_text()
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
            binding_manifest["dependencies"]["pyo3"]["version"],
            "=0.29.2",
        )
        self.assertFalse(core_manifest["package"]["publish"])
        self.assertFalse(binding_manifest["package"]["publish"])

    def test_binding_build_configuration_is_isolated_from_setuptools(self) -> None:
        root_text = (ROOT / "pyproject.toml").read_text()
        root_manifest = tomllib.loads(root_text)
        binding_manifest = tomllib.loads(
            (ROOT / "crates/airpods-aap-py/pyproject.toml").read_text()
        )
        self.assertEqual(
            root_manifest["build-system"]["build-backend"],
            "setuptools.build_meta",
        )
        self.assertNotIn("maturin", root_text.lower())
        self.assertEqual(
            binding_manifest["build-system"]["build-backend"],
            "maturin",
        )
        self.assertEqual(
            binding_manifest["tool"]["maturin"]["module-name"],
            "_airpods_aap_core",
        )

    def test_production_modules_do_not_import_or_select_rust(self) -> None:
        production_paths = (
            "src/airpods_hr/production_session.py",
            "src/airpods_hr/bluez_coexistence.py",
            "src/airpods_hr/heart_rate_session.py",
            "src/airpods_hr/monitor_cli.py",
            "src/airpods_hr/hr_semantics.py",
            "src/airpods_hr/heartrate.py",
        )
        for relative in production_paths:
            source = (ROOT / relative).read_text()
            tree = ast.parse(source)
            imports = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            imports.update(
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            )
            with self.subTest(path=relative):
                self.assertNotIn("_airpods_aap_core", imports)
                self.assertNotIn("tools.rust_shadow", imports)
                self.assertNotIn("AIRPODS_HR_USE_RUST", source)

    def test_frozen_python_components_match_task_9_7a(self) -> None:
        for relative, expected in FROZEN_SHA256.items():
            with self.subTest(path=relative):
                actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
