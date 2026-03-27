#!/usr/bin/env python3
"""Build and validate a local release candidate without publishing or hardware.

This is the canonical release-validation entrypoint.  Every child
process uses an argv array and a finite timeout.  No check starts the production
daemon, invokes Bluetooth tooling, or accesses publication credentials.
"""

from __future__ import annotations


import email.parser
import hashlib

import os


import subprocess
import sys
import tarfile


import tomllib

import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, Sequence


ROOT = Path(__file__).resolve().parents[1]
PYTHON_CLIENT_ROOT = ROOT / "packages/airpods-client-python"
RUST_CLIENT_ROOT = ROOT / "crates/airpods-client"
RELEASE_VERSION = "0.1.0"
MANIFEST_SCHEMA_VERSION = 1
BUILD_VERSION = "1.3.0"
SETUPTOOLS_VERSION = "84.0.0"
WHEEL_VERSION = "0.48.0"
COMMAND_TIMEOUT = 600
TEST_TIMEOUT = 1_200

PYTHON_TESTS = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
CROSS_LANGUAGE_TESTS = [
    sys.executable,
    "-m",
    "unittest",
    "-v",
    "tests.test_rust_client_hubd_integration",
    "tests.test_python_client_hubd_integration",
    "tests.test_mixed_client_hubd_integration",
]
RUST_CHECKS = (
    ["cargo", "fmt", "--check"],
    ["cargo", "check", "--workspace", "--locked"],
    ["cargo", "test", "--workspace", "--locked"],
    [
        "cargo",
        "clippy",
        "--workspace",
        "--all-targets",
        "--all-features",
        "--locked",
        "--",
        "-D",
        "warnings",
    ],
)


class ValidationError(RuntimeError):
    """A release invariant or bounded command failed."""


def fail(message: str) -> NoReturn:
    raise ValidationError(message)


def run(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path = ROOT,
    timeout: int = COMMAND_TIMEOUT,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [os.fspath(value) for value in argv]
    print("+", " ".join(command), flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=capture,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        fail(f"command timed out after {timeout}s: {command!r}")
    if result.returncode != 0:
        detail = ""
        if capture:
            detail = "\n" + (result.stdout + result.stderr)[-8_000:]
        fail(f"command exited {result.returncode}: {command!r}{detail}")
    return result


def output(argv: Sequence[str], *, cwd: Path = ROOT) -> str:
    return run(argv, cwd=cwd, capture=True).stdout.strip()


def project_versions() -> dict[str, str]:
    production = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    python_client = tomllib.loads(
        (PYTHON_CLIENT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    rust_client = tomllib.loads(
        (RUST_CLIENT_ROOT / "Cargo.toml").read_text(encoding="utf-8")
    )
    return {
        production["project"]["name"]: production["project"]["version"],
        "python:airpods-client": python_client["project"]["version"],
        "rust:airpods-client": rust_client["package"]["version"],
    }


def validate_version_consistency() -> None:
    versions = project_versions()
    if set(versions.values()) != {RELEASE_VERSION}:
        fail(f"release versions differ: {versions}")
    references = {
        ROOT
        / "docs/release-process.md": f"Current coordinated release candidate: `{RELEASE_VERSION}`",
        ROOT / "docs/sdk-v0.1-api.md": "v0.1",
        PYTHON_CLIENT_ROOT / "README.md": "`airpods-client` 0.1",
        RUST_CLIENT_ROOT / "README.md": "`airpods-client` 0.1",
    }
    for path, expected in references.items():
        if expected not in path.read_text(encoding="utf-8"):
            fail(f"current version reference missing from {path.relative_to(ROOT)}")
    print(f"version consistency: {RELEASE_VERSION}")


def validate_sensitive_paths() -> None:
    tracked = output(["git", "ls-files", "-z"])
    forbidden_parts = {"captures", "dumps", "secrets", "credentials", "target", "dist"}
    forbidden_suffixes = {".btsnoop", ".log", ".pcap", ".pcapng", ".pem", ".key"}
    for raw_path in tracked.split("\0"):
        if not raw_path:
            continue
        path = PurePosixPath(raw_path)
        lowered = {part.lower() for part in path.parts}
        if lowered & forbidden_parts:
            fail(f"sensitive/generated directory is tracked: {raw_path}")
        if path.suffix.lower() in forbidden_suffixes:
            fail(f"sensitive/generated file is tracked: {raw_path}")
        filename = path.name.lower()
        if "linkkey" in filename or "link-key" in filename:
            fail(f"credential-like filename is tracked: {raw_path}")
    print("sensitive-data filename scan: pass")


def validate_static_policy() -> None:
    validate_version_consistency()
    validate_sensitive_paths()
    run(["git", "diff", "--check"])


def wheel_metadata(path: Path) -> tuple[email.message.Message, dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        metadata_names = [
            name for name in names if name.endswith(".dist-info/METADATA")
        ]
        entry_names = [
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        ]
        if len(metadata_names) != 1:
            fail(f"{path.name}: expected one METADATA file")
        parser = email.parser.BytesParser()
        metadata = parser.parsebytes(archive.read(metadata_names[0]))
        entries: dict[str, str] = {}
        if entry_names:
            section = ""
            for line in archive.read(entry_names[0]).decode().splitlines():
                stripped = line.strip()
                if stripped.startswith("["):
                    section = stripped
                elif section == "[console_scripts]" and "=" in stripped:
                    key, value = stripped.split("=", 1)
                    entries[key.strip()] = value.strip()
    return metadata, entries


def _unsafe_member(name: str) -> bool:
    path = PurePosixPath(name)
    lowered = [part.lower() for part in path.parts]
    forbidden = {"tests", "captures", "dumps", "target", "__pycache__"}
    return bool(set(lowered) & forbidden) or any(
        part.endswith((".log", ".pcap", ".pcapng", ".btsnoop", ".pem", ".key"))
        for part in lowered
    )


def audit_production_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    if not any(name.startswith("airpods_hr/") for name in names):
        fail("production wheel lacks airpods_hr")
    if not any(name == "airpods_hr/service_installer.py" for name in names):
        fail("production wheel lacks service installer")
    if any(name.startswith("airpods_client/") for name in names):
        fail("production wheel contains standalone airpods_client")
    if any(_unsafe_member(name) for name in names):
        fail("production wheel contains repository-only/private material")
    metadata, entries = wheel_metadata(path)
    if metadata["Name"] != "airpods-hr-linux" or metadata["Version"] != RELEASE_VERSION:
        fail("production wheel identity/version mismatch")
    dependencies = metadata.get_all("Requires-Dist", [])
    dependency_names = {value.split(";", 1)[0].strip() for value in dependencies}
    if dependency_names != {"bumble==0.0.234", "dbus-next>=0.2.3"}:
        fail(f"production dependency metadata mismatch: {sorted(dependencies)}")
    expected_entries = {"airpods-hr", "airpods-hubd", "airpods-hubd-service"}
    if set(entries) != expected_entries:
        fail(f"production console entrypoints mismatch: {entries}")


def audit_python_client_wheel(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    roots = {PurePosixPath(name).parts[0] for name in names}
    if not any(name.startswith("airpods_client/") for name in names):
        fail("Python client wheel lacks airpods_client")
    if not all(
        root == "airpods_client" or root.endswith(".dist-info") for root in roots
    ):
        fail(f"Python client wheel contains unexpected roots: {sorted(roots)}")
    if any("airpods_hr" in name or _unsafe_member(name) for name in names):
        fail("Python client wheel contains daemon/repository-only material")
    metadata, entries = wheel_metadata(path)
    if metadata["Name"] != "airpods-client" or metadata["Version"] != RELEASE_VERSION:
        fail("Python client wheel identity/version mismatch")
    if metadata.get_all("Requires-Dist", []) or entries:
        fail("Python client wheel must have zero dependencies and no entrypoints")


def audit_sdist(path: Path, *, package: str) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
    if not names or any(
        PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
        for name in names
    ):
        fail(f"unsafe sdist members in {path.name}")
    forbidden_parts = {"captures", "dumps", "target", "__pycache__"}
    forbidden_suffixes = (".log", ".pcap", ".pcapng", ".btsnoop", ".pem", ".key")
    for name in names:
        parts = {part.lower() for part in PurePosixPath(name).parts}
        if parts & forbidden_parts or name.lower().endswith(forbidden_suffixes):
            fail(f"generated/private material in {path.name}: {name}")
    joined = "\n".join(names)
    required = (
        "src/airpods_hr/" if package == "airpods-hr-linux" else "src/airpods_client/"
    )
    if required not in joined:
        fail(f"{path.name} lacks {required}")
    if package == "airpods-hr-linux" and "/src/airpods_client/" in joined:
        fail("production sdist contains standalone client")
    if package == "airpods-client" and ("airpods_hr" in joined or "/crates/" in joined):
        fail("Python client sdist contains daemon/Rust material")


def audit_rust_crate(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
    forbidden = (
        "integration_probe.rs",
        "/target/",
        ".py",
        "airpods_hr",
        "airpods-client-python",
    )
    if any(any(item in name for item in forbidden) for name in names):
        fail("Rust client crate contains repository-only Python/probe material")
    if not any(name.endswith("/src/lib.rs") for name in names):
        fail("Rust client crate lacks src/lib.rs")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_record(path: Path, *, kind: str, package: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "package": package,
        "version": RELEASE_VERSION,
        "filename": path.name,
        "sha256": sha256(path),
        "size": path.stat().st_size,
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "release_version",
        "git",
        "source_date_epoch",
        "toolchain",
        "artifacts",
        "repeat_build_check",
        "validation",
    }
    if set(manifest) != required:
        fail(f"manifest top-level fields differ: {sorted(manifest)}")
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        fail("manifest schema version mismatch")
    if manifest["release_version"] != RELEASE_VERSION:
        fail("manifest release version mismatch")
    git = manifest["git"]
    if (
        set(git) != {"commit", "clean"}
        or len(git["commit"]) != 40
        or git["clean"] is not True
    ):
        fail("manifest git provenance is invalid")
    artifacts = manifest["artifacts"]
    if len(artifacts) != 5:
        fail("manifest must describe five artifacts")
    filenames: set[str] = set()
    expected_fields = {"kind", "package", "version", "filename", "sha256", "size"}
    for artifact in artifacts:
        if set(artifact) != expected_fields or artifact["version"] != RELEASE_VERSION:
            fail(f"invalid manifest artifact: {artifact}")
        if (
            artifact["filename"] in filenames
            or PurePosixPath(artifact["filename"]).name != artifact["filename"]
        ):
            fail("manifest artifact filenames must be unique basenames")
        filenames.add(artifact["filename"])
        if len(artifact["sha256"]) != 64 or artifact["size"] <= 0:
            fail(f"invalid artifact hash/size: {artifact}")
    repeat = manifest["repeat_build_check"]
    if repeat["artifact_count"] != 5 or repeat["matched_artifact_count"] not in range(
        6
    ):
        fail("manifest repeated-build counts are invalid")
    repeated_names = {item["filename"] for item in repeat["artifacts"]}
    if repeated_names != filenames:
        fail("manifest repeated-build results do not match artifact filenames")


def write_summary(path: Path, manifest: dict[str, Any]) -> None:
    repeat = manifest["repeat_build_check"]
    lines = [
        f"AirPods HR Linux release candidate {RELEASE_VERSION}",
        "",
        f"Git commit: {manifest['git']['commit']}",
        "Git tree clean: yes",
        f"Python: {manifest['toolchain']['python']}",
        f"Rust: {manifest['toolchain']['rustc']}",
        f"Cargo: {manifest['toolchain']['cargo']}",
        f"Platform: {manifest['toolchain']['platform']}",
        f"SOURCE_DATE_EPOCH: {manifest['source_date_epoch']}",
        f"Same-host repeated builds matched: {'yes' if repeat['byte_for_byte_equal'] else 'no'}",
        "Reproducible-build guarantee: no; this is one same-host repeated-build observation.",
        "",
        "Artifacts:",
    ]
    for artifact in manifest["artifacts"]:
        lines.append(
            f"- {artifact['filename']} | {artifact['package']} {artifact['version']} | "
            f"{artifact['size']} bytes | sha256 {artifact['sha256']}"
        )
    differing = [
        result["filename"]
        for result in repeat["artifacts"]
        if not result["byte_for_byte_equal"]
    ]
    if differing:
        lines.append("")
        lines.append("Repeated-build byte differences: " + ", ".join(differing))
    lines.extend(
        [
            "",
            "Validation used no Bluetooth hardware, did not start the production daemon,",
            "used no publication credentials, and published nothing.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


