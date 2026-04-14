"""Release manifest, version, and packaging-policy foundation tests."""

from __future__ import annotations

import copy
import inspect
import os
import subprocess
import sys
import tomllib
import unittest

from tools import validate_release


class VersionPolicyTests(unittest.TestCase):
    def test_coordinated_release_versions_and_docs_match(self) -> None:
        self.assertEqual(
            validate_release.project_versions(),
            {
                "airpods-hr-linux": "0.1.0",
                "python:airpods-client": "0.1.0",
                "rust:airpods-client": "0.1.0",
            },
        )
        validate_release.validate_version_consistency()

    def test_release_validator_never_uses_shell_true(self) -> None:
        source = inspect.getsource(validate_release)
        self.assertNotIn("shell=" + "True", source)
        self.assertNotIn("systemctl --user start", source)
        self.assertNotIn("bluetoothctl", source)
        self.assertIn('compile_environment["PYTHONPYCACHEPREFIX"]', source)

    def test_python_production_ci_builds_and_installs_private_ffi_wheel(self) -> None:
        workflow = (validate_release.ROOT / ".github/workflows/ci.yml").read_text()
        production = workflow.split("  python-production:\n", 1)[1].split(
            "\n  python-client:", 1
        )[0]

        setup_python = production.index("uses: actions/setup-python@v6")
        setup_python_id = production.index("id: setup-python")
        rust_setup = production.index("uses: dtolnay/rust-toolchain@1.97.0")
        ffi_build = production.index("maturin build --release --locked")
        ffi_install = production.index(
            'python -m pip install --no-deps "${{ runner.temp }}/ffi-wheel/"*.whl'
        )
        ffi_smoke = production.index("import _airpods_aap_core as ffi")
        validation = production.index(
            "python tools/validate_release.py --scope python"
        )

        self.assertIn(
            "- uses: actions/setup-python@v6\n        id: setup-python",
            production,
        )
        self.assertNotIn("maturin develop", production)
        self.assertNotIn("env.pythonLocation", production)
        self.assertIn(
            '--interpreter "${{ steps.setup-python.outputs.python-path }}"',
            production,
        )
        self.assertLess(setup_python, setup_python_id)
        self.assertLess(setup_python_id, rust_setup)
        self.assertLess(rust_setup, ffi_build)
        self.assertLess(ffi_build, ffi_install)
        self.assertLess(ffi_install, ffi_smoke)
        self.assertLess(ffi_smoke, validation)

    def test_every_rust_ci_job_uses_the_accepted_release_toolchain(self) -> None:
        workflow = (validate_release.ROOT / ".github/workflows/ci.yml").read_text()
        self.assertNotIn("dtolnay/rust-toolchain@stable", workflow)
        self.assertEqual(
            workflow.count("uses: dtolnay/rust-toolchain@1.97.0"), 4
        )
        boundaries = (
            ("python-production", "python-client"),
            ("rust", "cross-language"),
            ("cross-language", "release-artifacts"),
            ("release-artifacts", None),
        )
        for job, following_job in boundaries:
            section = workflow.split(f"  {job}:\n", 1)[1]
            if following_job is not None:
                section = section.split(f"\n  {following_job}:\n", 1)[0]
            self.assertIn("uses: dtolnay/rust-toolchain@1.97.0", section)

    def test_release_artifacts_ci_retains_only_validated_canonical_output(
        self,
    ) -> None:
        workflow = (validate_release.ROOT / ".github/workflows/ci.yml").read_text()
        release_job = workflow.split("  release-artifacts:\n", 1)[1]

        checkout = release_job.index("uses: actions/checkout@v5")
        python_setup = release_job.index("uses: actions/setup-python@v6")
        rust_setup = release_job.index("uses: dtolnay/rust-toolchain@1.97.0")
        validation = release_job.index(
            'python tools/validate_release.py --scope artifacts --output-dir "${{ runner.temp }}/release"'
        )
        upload = release_job.index("uses: actions/upload-artifact@v4")

        self.assertLess(checkout, python_setup)
        self.assertLess(python_setup, rust_setup)
        self.assertLess(rust_setup, validation)
        self.assertLess(validation, upload)
        self.assertIn(
            "needs:\n"
            "      - static-policy\n"
            "      - python-production\n"
            "      - python-client\n"
            "      - rust\n"
            "      - cross-language",
            release_job[:checkout],
        )
        self.assertIn(
            "name: airpods-hr-linux-0.1.0-${{ github.sha }}",
            release_job[upload:],
        )
        self.assertIn("path: ${{ runner.temp }}/release", release_job[upload:])
        self.assertIn("if-no-files-found: error", release_job[upload:])
        self.assertIn("retention-days: 14", release_job[upload:])
        self.assertNotIn("if: always()", release_job)
        self.assertNotIn("ffi-wheel", release_job)

    def test_coexistence_fakes_run_without_host_bluetooth_constants(self) -> None:
        test_path = validate_release.ROOT / "tests/test_bluez_coexistence.py"
        source = test_path.read_text()
        attributes = (
            "AF_BLUETOOTH",
            "SOCK_SEQPACKET",
            "BTPROTO_L2CAP",
            "SOL_BLUETOOTH",
            "BT_SECURITY",
            "BT_SECURITY_MEDIUM",
        )
        for attribute in attributes:
            self.assertNotIn(f"socket.{attribute}", source)

        code = """
import socket
import unittest

for attribute in (
    "AF_BLUETOOTH",
    "SOCK_SEQPACKET",
    "BTPROTO_L2CAP",
    "SOL_BLUETOOTH",
    "BT_SECURITY",
    "BT_SECURITY_MEDIUM",
):
    if hasattr(socket, attribute):
        delattr(socket, attribute)

from tests.test_bluez_coexistence import KernelL2CAPTransportTests

names = (
    "test_bind_security_connect_and_route_order",
    "test_python_constant_gap_uses_verified_linux_uapi_fallback",
    "test_conflicting_exposed_l2cap_constant_fails_closed",
)
suite = unittest.TestSuite(KernelL2CAPTransportTests(name) for name in names)
result = unittest.TextTestRunner(verbosity=2).run(suite)
if result.testsRun != len(names) or not result.wasSuccessful():
    raise SystemExit(1)
"""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                os.fspath(validate_release.ROOT / "src"),
                os.fspath(
                    validate_release.ROOT / "packages/airpods-client-python/src"
                ),
            )
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=validate_release.ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tracked_public_docs_exclude_personal_checkout_paths(self) -> None:
        validate_release.validate_public_documentation()

    def test_tracked_public_docs_exclude_stale_current_status(self) -> None:
        validate_release.validate_public_documentation_status()
        architecture = (
            validate_release.ROOT / "docs/client-sdk-architecture.md"
        ).read_text()
        self.assertIn(
            "Rust client                  Python client / future Unity / C# / JS",
            architecture,
        )

    def test_source_license_metadata_is_consistently_mit(self) -> None:
        root = validate_release.ROOT
        production = tomllib.loads((root / "pyproject.toml").read_text())
        python_client = tomllib.loads(
            (root / "packages/airpods-client-python/pyproject.toml").read_text()
        )
        rust_client = tomllib.loads(
            (root / "crates/airpods-client/Cargo.toml").read_text()
        )

        self.assertTrue((root / "LICENSE").is_file())
        self.assertEqual(production["project"]["license"], "MIT")
        self.assertEqual(python_client["project"]["license"], "MIT")
        self.assertEqual(rust_client["package"]["license"], "MIT")
        self.assertEqual(
            (root / "LICENSE").read_bytes(),
            (root / "packages/airpods-client-python/LICENSE").read_bytes(),
        )
        self.assertEqual(
            (root / "LICENSE").read_bytes(),
            (root / "crates/airpods-client/LICENSE").read_bytes(),
        )


class ManifestSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = {
            "schema_version": 1,
            "release_version": "0.1.0",
            "git": {"commit": "a" * 40, "clean": True},
            "source_date_epoch": 1,
            "toolchain": {
                "python": "3.14.6",
                "python_implementation": "CPython",
                "python_build_frontend": "build 1.3.0",
                "setuptools": "84.0.0",
                "wheel": "0.48.0",
                "rustc": "rustc 1.97.0",
                "cargo": "cargo 1.97.0",
                "platform": "Linux-test",
            },
            "artifacts": [
                {
                    "kind": kind,
                    "package": package,
                    "version": "0.1.0",
                    "filename": filename,
                    "sha256": character * 64,
                    "size": index + 1,
                }
                for index, (kind, package, filename, character) in enumerate(
                    (
                        ("python-wheel", "airpods-hr-linux", "production.whl", "a"),
                        ("python-sdist", "airpods-hr-linux", "production.tar.gz", "b"),
                        ("python-wheel", "airpods-client", "client.whl", "c"),
                        ("python-sdist", "airpods-client", "client.tar.gz", "d"),
                        ("rust-crate", "airpods-client", "client.crate", "e"),
                    )
                )
            ],
            "repeat_build_check": {
                "source_date_epoch": 1,
                "byte_for_byte_equal": True,
                "artifact_count": 5,
                "matched_artifact_count": 5,
                "artifacts": [
                    {"filename": "production.whl", "byte_for_byte_equal": True},
                    {
                        "filename": "production.tar.gz",
                        "byte_for_byte_equal": True,
                    },
                    {"filename": "client.whl", "byte_for_byte_equal": True},
                    {"filename": "client.tar.gz", "byte_for_byte_equal": True},
                    {"filename": "client.crate", "byte_for_byte_equal": True},
                ],
                "scope": "test",
                "reproducible_build_guarantee": False,
            },
            "validation": {
                "bluetooth_hardware_used": False,
                "production_daemon_started": False,
                "published": False,
            },
        }

    def test_manifest_schema_accepts_complete_release_candidate(self) -> None:
        validate_release.validate_manifest(self.manifest)

    def test_manifest_schema_rejects_missing_field(self) -> None:
        broken = copy.deepcopy(self.manifest)
        del broken["source_date_epoch"]
        with self.assertRaises(validate_release.ValidationError):
            validate_release.validate_manifest(broken)

    def test_manifest_schema_rejects_duplicate_artifact_names(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["artifacts"][1]["filename"] = broken["artifacts"][0]["filename"]
        with self.assertRaises(validate_release.ValidationError):
            validate_release.validate_manifest(broken)

    def test_manifest_schema_rejects_private_artifact_paths(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["artifacts"][0]["filename"] = "/home/person/private.whl"
        with self.assertRaises(validate_release.ValidationError):
            validate_release.validate_manifest(broken)


if __name__ == "__main__":
    unittest.main()
