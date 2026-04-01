"""Release manifest, version, and packaging-policy foundation tests."""

from __future__ import annotations

import copy
import inspect
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
