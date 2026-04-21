"""Release manifest, version, and packaging-policy foundation tests."""

from __future__ import annotations

import copy
import io
import inspect
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools import validate_release


# Verified commit from dtolnay/rust-toolchain's 1.97.0 branch. Its action.yml
# fixes the toolchain version internally, including when invoked by commit SHA.
RUST_TOOLCHAIN_ACTION = (
    "dtolnay/rust-toolchain@0f870b6babcd9f4faebaee75d487ff6763f18c07"
)


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

    def test_current_package_urls_use_the_new_repository_target(self) -> None:
        repository = "https://github.com/ozankenangungor/linux-client-for-airpods-pro"
        for relative in (
            "pyproject.toml",
            "packages/airpods-client-python/pyproject.toml",
        ):
            with self.subTest(manifest=relative):
                project = tomllib.loads(
                    (validate_release.ROOT / relative).read_text()
                )["project"]
                self.assertEqual(project["urls"]["Repository"], repository)
                for url in project["urls"].values():
                    self.assertTrue(
                        url == repository or url.startswith(repository + "/")
                    )
        for crate in ("airpods-client", "airpods-client-resilient", "airpodsctl"):
            with self.subTest(crate=crate):
                path = validate_release.ROOT / "crates" / crate / "Cargo.toml"
                package = tomllib.loads(path.read_text())["package"]
                self.assertEqual(package["repository"], repository)
                self.assertEqual(package["homepage"], repository)
                if "documentation" in package:
                    self.assertTrue(
                        package["documentation"].startswith(repository + "/")
                    )
        appstream = ET.parse(
            validate_release.ROOT / "packaging/io.github.ozankenangungor.AirPodsHR.metainfo.xml"
        )
        self.assertEqual(
            {url.attrib["type"]: url.text for url in appstream.findall("url")},
            {"homepage": repository, "bugtracker": repository + "/issues"},
        )

    def test_workflow_actions_are_immutable_and_permissions_are_read_only(self) -> None:
        for path in (validate_release.ROOT / ".github/workflows").glob("*.yml"):
            with self.subTest(workflow=path.name):
                workflow = path.read_text()
                actions = re.findall(r"(?m)^\s*(?:-\s*)?uses:\s*(\S+)", workflow)
                self.assertTrue(actions)
                for action in actions:
                    self.assertRegex(action, r"^[\w./-]+@[0-9a-f]{40}$")
                self.assertIn("permissions:\n  contents: read\n", workflow)
                permissions = re.findall(
                    r"(?m)^\s+([\w-]+):\s+(read|write|none)\s*$", workflow
                )
                self.assertEqual(permissions, [("contents", "read")])

    def test_release_validator_never_uses_shell_true(self) -> None:
        source = inspect.getsource(validate_release)
        self.assertNotIn("shell=" + "True", source)
        self.assertNotIn("systemctl --user start", source)
        self.assertNotIn("bluetoothctl", source)
        self.assertIn('compile_environment["PYTHONPYCACHEPREFIX"]', source)

    def test_python_production_ci_installs_root_native_package(self) -> None:
        workflow = (validate_release.ROOT / ".github/workflows/ci.yml").read_text()
        production = workflow.split("  python-production:\n", 1)[1].split(
            "\n  python-client:", 1
        )[0]
        rust_setup = production.index("uses: " + RUST_TOOLCHAIN_ACTION)
        install = production.index(
            "python -m pip install -e . --config-settings=build-args=--locked"
        )
        smoke = production.index('ffi = sys.modules["airpods_hr._airpods_aap_core"]')
        validation = production.index("python tools/validate_release.py --scope python")
        self.assertLess(rust_setup, install)
        self.assertLess(install, smoke)
        self.assertLess(smoke, validation)
        self.assertNotIn("ffi-wheel", production)
        self.assertNotIn("crates/airpods-aap-py", production)

    def test_clean_production_consumer_probes_native_lifecycle_from_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "consumer"
            bin_dir = work / "bin"
            bin_dir.mkdir(parents=True)
            for name in ("airpods-hr", "airpods-hubd", "airpods-hubd-service"):
                (bin_dir / name).touch()
            python = bin_dir / "python"
            calls = []

            def fake_run(argv, **kwargs):
                calls.append((argv, kwargs))
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout=f'exec_start="{python}" -m airpods_hr._hubd.main',
                )

            with (
                patch.object(validate_release.venv.EnvBuilder, "create"),
                patch.object(validate_release, "venv_python", return_value=python),
                patch.object(validate_release, "run", side_effect=fake_run),
            ):
                validate_release.clean_install_production(
                    Path(directory) / "production.whl", work
                )

            probes = [
                (argv, kwargs) for argv, kwargs in calls
                if len(argv) == 4 and argv[:3] == [python, "-I", "-c"]
            ]
            self.assertEqual(len(probes), 1)
            argv, kwargs = probes[0]
            self.assertEqual(kwargs["cwd"], work)
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            code = argv[3]
            compile(code, "<clean production consumer>", "exec")
            for check in (
                "import airpods_hr.production_session as production",
                "assert inspect.isbuiltin(native.production_operation)",
                "assert inspect.isbuiltin(native.production_transition)",
                "object.__new__(production.InternalProductionSession)",
                'patch.object(native, "production_operation"',
                'patch.object(native, "production_transition"',
                "probe._require_operation(operation)",
                "call.assert_called_once_with(states.index(before), native_operation)",
                "production._ProductionEvent(event), cleanup_complete=cleanup_complete",
                'guard("open", 0)',
                'guard("start", 1, valid=False)',
                'guard("receive_report", 2)',
                'guard("stop", 3)',
                'guard("close", 4)',
                "advance(2, states[6])",
                "advance(5, states[6])",
                "advance(8, states[6], cleanup_complete=False)",
                "advance(8, states[0], cleanup_complete=True)",
            ):
                with self.subTest(check=check):
                    self.assertIn(check, code)

    def test_every_rust_ci_job_uses_the_accepted_release_toolchain(self) -> None:
        workflow = (validate_release.ROOT / ".github/workflows/ci.yml").read_text()
        self.assertNotIn("dtolnay/rust-toolchain@stable", workflow)
        self.assertEqual(workflow.count("uses: " + RUST_TOOLCHAIN_ACTION), 4)
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
            self.assertIn("uses: " + RUST_TOOLCHAIN_ACTION + " # 1.97.0", section)

    def test_release_artifacts_ci_retains_only_validated_canonical_output(
        self,
    ) -> None:
        workflow = (validate_release.ROOT / ".github/workflows/ci.yml").read_text()
        release_job = workflow.split("  release-artifacts:\n", 1)[1]

        checkout = release_job.index("uses: actions/checkout@")
        python_setup = release_job.index("uses: actions/setup-python@")
        rust_setup = release_job.index("uses: " + RUST_TOOLCHAIN_ACTION)
        validation = release_job.index(
            'python tools/validate_release.py --scope artifacts --output-dir "${{ runner.temp }}/release"'
        )
        upload = release_job.index("uses: actions/upload-artifact@")

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
                os.fspath(validate_release.ROOT / "packages/airpods-client-python/src"),
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

    def test_manifest_rejects_c_sdk_as_sixth_artifact(self) -> None:
        broken = copy.deepcopy(self.manifest)
        extra = copy.deepcopy(broken["artifacts"][4])
        extra.update(package="airpods-client-c", filename="airpods-client-c.crate")
        broken["artifacts"].append(extra)
        with self.assertRaisesRegex(validate_release.ValidationError, "five artifacts"):
            validate_release.validate_manifest(broken)

    def test_manifest_rejects_replacing_canonical_artifact_with_c_sdk(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["artifacts"][4]["package"] = "airpods-client-c"
        with self.assertRaisesRegex(validate_release.ValidationError, "package/kind set"):
            validate_release.validate_manifest(broken)

    def test_manifest_rejects_unpublished_ffi_filename(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["artifacts"][4]["filename"] = "ffi.crate"
        with self.assertRaisesRegex(validate_release.ValidationError, "unpublished"):
            validate_release.validate_manifest(broken)


class SdistPolicyTests(unittest.TestCase):
    @staticmethod
    def write_sdist(path: Path, members: tuple[str, ...]) -> None:
        with tarfile.open(path, "w:gz") as archive:
            for name in members:
                content = b"test fixture\n"
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

    def test_python_sdists_reject_repository_tests(self) -> None:
        cases = (
            ("airpods-hr-linux", "airpods_hr_linux-0.1.0", "airpods_hr"),
            ("airpods-client", "airpods_client-0.1.0", "airpods_client"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for package, root, import_name in cases:
                with self.subTest(package=package):
                    path = Path(directory) / f"{import_name}.tar.gz"
                    self.write_sdist(
                        path,
                        (
                            f"{root}/LICENSE",
                            f"{root}/src/{import_name}/__init__.py",
                            f"{root}/tests/test_example.py",
                        ),
                    )
                    with self.assertRaisesRegex(
                        validate_release.ValidationError,
                        "generated/private material",
                    ):
                        validate_release.audit_sdist(path, package=package)

    def test_production_sdist_requires_rust_build_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "airpods_hr_linux-0.1.0.tar.gz"
            self.write_sdist(
                path,
                (
                    "airpods_hr_linux-0.1.0/LICENSE",
                    "airpods_hr_linux-0.1.0/src/airpods_hr/__init__.py",
                ),
            )
            with self.assertRaisesRegex(
                validate_release.ValidationError, "production sdist lacks Cargo.toml"
            ):
                validate_release.audit_sdist(path, package="airpods-hr-linux")

    def test_production_sdist_requires_lifecycle_policy_source(self) -> None:
        root = "airpods_hr_linux-0.1.0"
        sources = (
            "LICENSE",
            "src/airpods_hr/__init__.py",
            "Cargo.toml",
            "Cargo.lock",
            "pyproject.toml",
            "crates/airpods-aap-core/Cargo.toml",
            "crates/airpods-aap-core/src/lib.rs",
            "crates/airpods-aap-core/src/heart_rate.rs",
            "crates/airpods-aap-py/Cargo.toml",
            "crates/airpods-aap-py/src/lib.rs",
        )
        policy = "crates/airpods-aap-core/src/production.rs"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"{root}.tar.gz"
            self.write_sdist(path, tuple(f"{root}/{name}" for name in sources))
            with self.assertRaisesRegex(
                validate_release.ValidationError,
                "production sdist lacks crates/airpods-aap-core/src/production.rs",
            ):
                validate_release.audit_sdist(path, package="airpods-hr-linux")
            self.write_sdist(path, tuple(f"{root}/{name}" for name in (*sources, policy)))
            with self.assertRaisesRegex(
                validate_release.ValidationError,
                "production sdist lacks crates/airpods-aap-core/src/recovery.rs",
            ):
                validate_release.audit_sdist(path, package="airpods-hr-linux")
            self.write_sdist(
                path,
                tuple(f"{root}/{name}" for name in (*sources, policy,
                      "crates/airpods-aap-core/src/recovery.rs")),
            )
            with self.assertRaisesRegex(
                validate_release.ValidationError,
                "production sdist lacks crates/airpods-aap-py/src/app_runtime_policy_bridge.rs",
            ):
                validate_release.audit_sdist(path, package="airpods-hr-linux")
            new_sources = (
                "crates/airpods-aap-py/src/app_runtime_policy_bridge.rs",
                "crates/airpods-app-core/Cargo.toml",
                "crates/airpods-app-core/src/lib.rs",
                "crates/airpods-app-core/src/daemon.rs",
                "crates/airpods-app-core/src/production_config.rs",
                "crates/airpods-app-core/src/runner.rs",
                "crates/airpods-app-core/src/monitor.rs",
                "crates/airpods-app-core/src/service.rs",
                "crates/airpods-app-core/src/path_policy.rs",
            )
            self.write_sdist(
                path,
                tuple(f"{root}/{name}" for name in (*sources, policy,
                      "crates/airpods-aap-core/src/recovery.rs", *new_sources)),
            )
            validate_release.audit_sdist(path, package="airpods-hr-linux")


class PythonBuildPolicyTests(unittest.TestCase):
    def test_maturin_pep517_compatibility_is_explicit_for_both_build_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "pyproject.toml").write_text(
                '[build-system]\nbuild-backend="maturin"\n'
                '[tool.maturin]\ncompatibility="manylinux_2_34"\n'
            )
            self.assertEqual(validate_release.python_build_settings(source), [
                "--config-setting=build-args=--locked --compatibility manylinux_2_34",
            ])
            self.assertEqual(validate_release.python_build_settings(source, locked=False), [
                "--config-setting=build-args=--compatibility manylinux_2_34",
            ])

    def test_maturin_missing_or_wrong_compatibility_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for policy in ("", '[tool.maturin]\ncompatibility="off"\n',
                           '[tool.maturin]\ncompatibility="manylinux_2_28"\n'):
                with self.subTest(policy=policy):
                    (source / "pyproject.toml").write_text(
                        '[build-system]\nbuild-backend="maturin"\n' + policy
                    )
                    with self.assertRaisesRegex(validate_release.ValidationError, "must configure compatibility"):
                        validate_release.python_build_settings(source)

    def test_python_client_build_settings_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "pyproject.toml").write_text(
                '[build-system]\nbuild-backend="setuptools.build_meta"\n'
            )
            for locked in (True, False):
                self.assertEqual(validate_release.python_build_settings(source, locked=locked), [])


class WheelTagPolicyTests(unittest.TestCase):
    production_name = "airpods_hr_linux-0.1.0-cp314-cp314-manylinux_2_34_x86_64.whl"
    production_info = "airpods_hr_linux-0.1.0.dist-info"
    production_headers = "Wheel-Version: 1.0\nTag: cp314-cp314-manylinux_2_34_x86_64\n"

    @classmethod
    def write_wheel(
        cls, path: Path, *, production: bool = True,
        headers: str | None = None, wheel_names: tuple[str, ...] | None = None,
    ) -> None:
        info = cls.production_info if production else "airpods_client-0.1.0.dist-info"
        package = "airpods-hr-linux" if production else "airpods-client"
        members = {
            f"{info}/METADATA": (
                f"Name: {package}\nVersion: 0.1.0\nLicense-Expression: MIT\n"
                + ("Requires-Dist: bumble==0.0.234\nRequires-Dist: dbus-next>=0.2.3\n"
                   if production else "")
            ),
            f"{info}/licenses/LICENSE": "fixture license",
        }
        if production:
            members.update({
                "airpods_hr/__init__.py": "",
                "airpods_hr/service_installer.py": "",
                "airpods_hr/_airpods_aap_core.cpython-314-x86_64-linux-gnu.so": "fixture",
                f"{info}/entry_points.txt": (
                    "[console_scripts]\n"
                    "airpods-hr = airpods_hr.cli:main\n"
                    "airpods-hubd = airpods_hr._hubd.main:main\n"
                    "airpods-hubd-service = airpods_hr.service_installer:main\n"
                ),
            })
        else:
            members["airpods_client/__init__.py"] = ""
        if headers is None:
            headers = cls.production_headers if production else "Wheel-Version: 1.0\nTag: py3-none-any\n"
        if wheel_names is None:
            wheel_names = (f"{info}/WHEEL",)
        members.update({name: headers for name in wheel_names})
        with zipfile.ZipFile(path, "w") as archive:
            for name, contents in members.items():
                archive.writestr(name, contents)

    def test_expected_manylinux_production_wheel_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / self.production_name
            self.write_wheel(path)
            validate_release.audit_production_wheel(path)

    def test_generic_linux_production_wheel_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "airpods_hr_linux-0.1.0-cp314-cp314-linux_x86_64.whl"
            self.write_wheel(path, headers="Tag: cp314-cp314-linux_x86_64\n")
            with self.assertRaisesRegex(validate_release.ValidationError, "filename must be"):
                validate_release.audit_production_wheel(path)

    def test_other_production_filename_tags_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for tag in (
                "cp313-cp313-manylinux_2_34_x86_64",
                "cp314-abi3-manylinux_2_34_x86_64",
                "cp314-cp314-manylinux_2_28_x86_64",
                "cp314-cp314-manylinux_2_34_aarch64",
            ):
                with self.subTest(tag=tag):
                    path = Path(directory) / f"airpods_hr_linux-0.1.0-{tag}.whl"
                    self.write_wheel(path, headers=f"Tag: {tag}\n")
                    with self.assertRaisesRegex(validate_release.ValidationError, "filename must be"):
                        validate_release.audit_production_wheel(path)

    def test_production_filename_and_wheel_tags_must_agree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / self.production_name
            for headers in (
                "Tag: cp314-cp314-linux_x86_64\n",
                "Tag: cp313-cp313-manylinux_2_34_x86_64\n",
                "Wheel-Version: 1.0\n",
                self.production_headers + "Tag: cp314-cp314-linux_x86_64\n",
                self.production_headers + " extra\n",
            ):
                with self.subTest(headers=headers):
                    self.write_wheel(path, headers=headers)
                    with self.assertRaisesRegex(validate_release.ValidationError, "WHEEL Tag must match"):
                        validate_release.audit_production_wheel(path)

    def test_production_wheel_metadata_must_be_unique_and_match_distribution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / self.production_name
            for names in (
                (), ("other.dist-info/WHEEL",),
                (f"{self.production_info}/WHEEL", "other.dist-info/WHEEL"),
            ):
                with self.subTest(names=names):
                    self.write_wheel(path, wheel_names=names)
                    with self.assertRaisesRegex(validate_release.ValidationError, "one matching WHEEL"):
                        validate_release.audit_production_wheel(path)

    def test_python_client_py3_none_any_remains_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "airpods_client-0.1.0-py3-none-any.whl"
            self.write_wheel(path, production=False)
            validate_release.audit_python_client_wheel(path)


class CSDKArtifactIsolationTests(unittest.TestCase):
    def test_all_five_archive_audits_reject_c_sdk_material(self) -> None:
        markers = (
            "crates/airpods-client-c/src/lib.rs", "include/airpods_client.h",
            "libairpods_client_c.so", "libairpods_client_c.a", "tests/c_ffi_probe.c",
            "debug/c-probe", "src/ffi.rs",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for member in markers:
                for audit in (validate_release.audit_production_wheel,
                              validate_release.audit_python_client_wheel):
                    with self.subTest(member=member, audit=audit.__name__):
                        path = root / "fixture.whl"
                        with zipfile.ZipFile(path, "w") as archive:
                            archive.writestr(member, "repository-only")
                        with self.assertRaisesRegex(validate_release.ValidationError, "C SDK material"):
                            audit(path)
                for package in ("airpods-hr-linux", "airpods-client", "rust-client"):
                    with self.subTest(member=member, package=package):
                        path = root / "fixture.tar.gz"
                        SdistPolicyTests.write_sdist(path, (f"fixture/{member}",))
                        with self.assertRaisesRegex(validate_release.ValidationError, "C SDK material"):
                            if package == "rust-client":
                                validate_release.audit_rust_crate(path)
                            else:
                                validate_release.audit_sdist(path, package=package)


if __name__ == "__main__":
    unittest.main()
