#!/usr/bin/env python3
"""Build and validate a local release candidate without publishing or hardware.

This is the canonical release-validation entrypoint.  Every child
process uses an argv array and a finite timeout.  No check starts the production
daemon, invokes Bluetooth tooling, or accesses publication credentials.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import tomllib
import venv
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


def git_commit() -> str:
    value = output(["git", "rev-parse", "HEAD"])
    if len(value) != 40 or any(
        character not in "0123456789abcdef" for character in value
    ):
        fail(f"unexpected git commit: {value!r}")
    return value


def git_is_clean() -> bool:
    return output(["git", "status", "--porcelain=v1"]) == ""


def require_clean_tree() -> None:
    if not git_is_clean():
        fail("canonical release validation requires a clean git tree")


def source_date_epoch() -> int:
    value = output(["git", "show", "-s", "--format=%ct", "HEAD"])
    try:
        epoch = int(value)
    except ValueError:
        fail(f"invalid commit timestamp: {value!r}")
    if epoch <= 0:
        fail("commit timestamp must be positive")
    return epoch


def python_environment() -> dict[str, str]:
    environment = dict(os.environ)
    paths = [os.fspath(ROOT / "src"), os.fspath(PYTHON_CLIENT_ROOT / "src")]
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def clean_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PIP_NO_INPUT"] = "1"
    return environment


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


def validate_public_documentation() -> None:
    tracked = output(["git", "ls-files", "-z", "--", "*.md"])
    forbidden = ("/home/kenan/", "~/airpods-hr-linux")
    for relative in tracked.split("\0"):
        if not relative:
            continue
        text = (ROOT / relative).read_text(encoding="utf-8")
        for value in forbidden:
            if value in text:
                fail(f"personal checkout path in public documentation: {relative}")
    print("public-documentation path scan: pass")


def validate_public_documentation_status() -> None:
    tracked = output(["git", "ls-files", "-z", "--", "*.md"])
    forbidden = (
        "Real AirPods validation of the Rust client remains pending",
        "A future Rust client crate should speak to the daemon",
        "future Python / Unity / C# / JS clients",
        "The daemon, SDKs, and IPC layer are future components",
        "The future execution uses",
        "Before a future owner run",
    )
    for relative in tracked.split("\0"):
        if not relative:
            continue
        text = (ROOT / relative).read_text(encoding="utf-8")
        for value in forbidden:
            if value in text:
                fail(f"stale current-state documentation in {relative}: {value!r}")
    print("public-documentation current-state scan: pass")


def validate_static_policy() -> None:
    validate_version_consistency()
    validate_sensitive_paths()
    validate_public_documentation()
    validate_public_documentation_status()
    run(["git", "diff", "--check"])


def validate_systemd_parser() -> bool:
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        print("systemd parser: SKIP (systemd-analyze unavailable)", flush=True)
        return False
    sys.path.insert(0, os.fspath(ROOT / "src"))
    from airpods_hr.service_installer import render_unit

    with tempfile.TemporaryDirectory(prefix="airpods-systemd-") as directory:
        root = Path(directory)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        (runtime / "systemd").mkdir(mode=0o700)
        interpreter = root / "installed environment/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o700)
        unit = root / "airpods-hubd.service"
        unit.write_text(render_unit(interpreter), encoding="utf-8")
        environment = clean_environment()
        environment["XDG_RUNTIME_DIR"] = os.fspath(runtime)
        run([analyzer, "verify", "--user", unit], cwd=root, env=environment, timeout=30)
    print("systemd parser: pass")
    return True


def run_python_checks() -> None:
    environment = python_environment()
    run(PYTHON_TESTS, env=environment, timeout=TEST_TIMEOUT)
    with tempfile.TemporaryDirectory(prefix="airpods-compileall-") as directory:
        compile_environment = dict(environment)
        compile_environment["PYTHONPYCACHEPREFIX"] = directory
        run(
            [
                sys.executable,
                "-m",
                "compileall",
                "-q",
                "src",
                "packages/airpods-client-python/src",
            ],
            env=compile_environment,
        )
    validate_systemd_parser()


def run_cross_language_checks() -> None:
    run(CROSS_LANGUAGE_TESTS, env=python_environment(), timeout=TEST_TIMEOUT)


def run_rust_checks() -> None:
    for command in RUST_CHECKS:
        run(command, timeout=TEST_TIMEOUT)


def venv_python(directory: Path) -> Path:
    return directory / "bin/python"


def prepare_build_environment(directory: Path) -> Path:
    venv.EnvBuilder(with_pip=True, clear=True).create(directory)
    python = venv_python(directory)
    run(
        [
            python,
            "-m",
            "pip",
            "install",
            f"build=={BUILD_VERSION}",
            f"setuptools=={SETUPTOOLS_VERSION}",
            f"wheel=={WHEEL_VERSION}",
        ],
        cwd=directory.parent,
        env=clean_environment(),
    )
    return python


def build_python_package(
    python: Path, source: Path, destination: Path, *, epoch: int
) -> list[Path]:
    destination.mkdir(parents=True)
    environment = clean_environment()
    environment["SOURCE_DATE_EPOCH"] = str(epoch)
    run(
        [
            python,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--sdist",
            "--outdir",
            destination,
            source,
        ],
        cwd=destination.parent,
        env=environment,
        timeout=TEST_TIMEOUT,
    )
    artifacts = sorted(path for path in destination.iterdir() if path.is_file())
    if len(artifacts) != 2 or not any(path.suffix == ".whl" for path in artifacts):
        fail(f"expected one wheel and one sdist from {source}, got {artifacts}")
    return artifacts


def build_rust_package(destination: Path, source_root: Path, *, epoch: int) -> Path:
    target = destination / "cargo-target"
    environment = clean_environment()
    environment["SOURCE_DATE_EPOCH"] = str(epoch)
    run(
        [
            "cargo",
            "package",
            "--locked",
            "--package",
            "airpods-client",
            "--target-dir",
            target,
        ],
        cwd=source_root,
        env=environment,
        timeout=TEST_TIMEOUT,
    )
    crate = target / "package" / f"airpods-client-{RELEASE_VERSION}.crate"
    if not crate.is_file():
        fail(f"cargo package did not produce {crate}")
    return crate


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
    if metadata["License-Expression"] != "MIT" or not any(
        name.endswith(".dist-info/licenses/LICENSE") for name in names
    ):
        fail("production wheel license metadata/content mismatch")
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
    if metadata["License-Expression"] != "MIT" or not any(
        name.endswith(".dist-info/licenses/LICENSE") for name in names
    ):
        fail("Python client wheel license metadata/content mismatch")
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
    forbidden_parts = {"tests", "captures", "dumps", "target", "__pycache__"}
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
    if not any(name.endswith("/LICENSE") for name in names):
        fail(f"{path.name} lacks packaged license")
    if package == "airpods-hr-linux" and "/src/airpods_client/" in joined:
        fail("production sdist contains standalone client")
    if package == "airpods-client" and ("airpods_hr" in joined or "/crates/" in joined):
        fail("Python client sdist contains daemon/Rust material")


def audit_rust_crate(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        names = archive.getnames()
        license_name = f"airpods-client-{RELEASE_VERSION}/LICENSE"
        if license_name not in names:
            fail("Rust client crate lacks packaged LICENSE")
        license_file = archive.extractfile(license_name)
        if license_file is None:
            fail("Rust client crate LICENSE is unreadable")
        if license_file.read() != (ROOT / "LICENSE").read_bytes():
            fail("Rust client crate packaged LICENSE differs from root LICENSE")
        manifest_name = next(
            (name for name in names if name.endswith("/Cargo.toml")), None
        )
        if manifest_name is None:
            fail("Rust client crate lacks Cargo.toml")
        manifest_file = archive.extractfile(manifest_name)
        if manifest_file is None:
            fail("Rust client crate Cargo.toml is unreadable")
        manifest = tomllib.loads(manifest_file.read().decode("utf-8"))
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
    if manifest["package"].get("license") != "MIT":
        fail("Rust client crate license metadata mismatch")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def clean_install_production(wheel: Path, work: Path) -> None:
    environment = clean_environment()
    venv.EnvBuilder(with_pip=True).create(work)
    python = venv_python(work)
    run(
        [python, "-m", "pip", "install", wheel],
        cwd=work,
        env=environment,
        timeout=TEST_TIMEOUT,
    )
    run([python, "-m", "pip", "check"], cwd=work, env=environment)
    code = textwrap.dedent(
        """
        import importlib.metadata
        import airpods_hr
        assert importlib.metadata.version("airpods-hr-linux") == "0.1.0"
        assert "hub" not in " ".join(airpods_hr.__all__).lower()
        """
    )
    run([python, "-I", "-c", code], cwd=work, env=environment)
    for command in ("airpods-hr", "airpods-hubd", "airpods-hubd-service"):
        executable = work / "bin" / command
        if not executable.is_file():
            fail(f"clean production install lacks {command}")
        run([executable, "--help"], cwd=work, env=environment, timeout=30, capture=True)
    home = work / "isolated-home"
    config = work / "isolated-config"
    runtime = work / "isolated-runtime"
    home.mkdir()
    config.mkdir()
    runtime.mkdir(mode=0o700)
    isolated = dict(environment)
    isolated.update(
        HOME=os.fspath(home),
        XDG_CONFIG_HOME=os.fspath(config),
        XDG_RUNTIME_DIR=os.fspath(runtime),
    )
    dry_run = run(
        [work / "bin/airpods-hubd-service", "install", "--dry-run"],
        cwd=work,
        env=isolated,
        capture=True,
    ).stdout
    expected = f'exec_start="{python}" -m airpods_hr._hubd.main'
    if expected not in dry_run or os.fspath(ROOT / ".venv") in dry_run:
        fail("service dry run did not use the clean installed interpreter")
    if any(config.rglob("*.service")):
        fail("service dry run mutated isolated user configuration")
    run([python, "-m", "compileall", "-q", work / "lib"], cwd=work, env=environment)


def clean_install_python_client(wheel: Path, work: Path) -> None:
    environment = clean_environment()
    venv.EnvBuilder(with_pip=True).create(work)
    python = venv_python(work)
    run(
        [python, "-m", "pip", "install", "--no-index", "--no-deps", wheel],
        cwd=work,
        env=environment,
    )
    run([python, "-m", "pip", "check"], cwd=work, env=environment)
    code = textwrap.dedent(
        """
        import asyncio
        import importlib.metadata
        import os
        import tempfile
        from pathlib import Path
        from airpods_client import AirPodsClient, ConnectionFailed, XdgRuntimeDirMissing

        assert importlib.metadata.version("airpods-client") == "0.1.0"
        async def check():
            os.environ.pop("XDG_RUNTIME_DIR", None)
            try:
                await AirPodsClient.connect()
            except XdgRuntimeDirMissing:
                pass
            else:
                raise AssertionError("missing XDG_RUNTIME_DIR was not typed")
            with tempfile.TemporaryDirectory() as directory:
                try:
                    await AirPodsClient.connect_to(Path(directory) / "missing.sock")
                except ConnectionFailed:
                    pass
                else:
                    raise AssertionError("missing daemon was not typed")
        asyncio.run(check())
        """
    )
    run([python, "-I", "-c", code], cwd=work, env=environment)
    run([python, "-m", "compileall", "-q", work / "lib"], cwd=work, env=environment)


def external_rust_consumer(crate: Path, work: Path) -> None:
    package_root = work / "package"
    package_root.mkdir(parents=True)
    with tarfile.open(crate, "r:gz") as archive:
        archive.extractall(package_root, filter="data")
    extracted = package_root / f"airpods-client-{RELEASE_VERSION}"
    consumer = work / "consumer"
    (consumer / "src").mkdir(parents=True)
    relative = os.path.relpath(extracted, consumer).replace(os.sep, "/")
    (consumer / "Cargo.toml").write_text(
        textwrap.dedent(
            f"""
            [package]
            name = "release-consumer"
            version = "0.0.0"
            edition = "2024"

            [dependencies]
            airpods-client = {{ path = "{relative}" }}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (consumer / "src/main.rs").write_text(
        textwrap.dedent(
            """
            use airpods_client::{AirPodsClient, Error, MAX_FRAME_SIZE, PROTOCOL_VERSION};
            use std::path::Path;

            async fn public_contract() -> Result<(), Error> {
                let client = AirPodsClient::connect_to(Path::new("/tmp/not-opened")).await?;
                let _ = client.hello().await?;
                let _ = client.ping().await?;
                let _ = client.status().await?;
                let mut subscription = client.subscribe_heart_rate().await?;
                let _ = subscription.next().await?;
                subscription.unsubscribe().await?;
                Ok(())
            }

            fn main() {
                let _ = (PROTOCOL_VERSION, MAX_FRAME_SIZE, public_contract);
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )
    run(["cargo", "generate-lockfile"], cwd=consumer, timeout=TEST_TIMEOUT)
    run(["cargo", "check", "--locked"], cwd=consumer, timeout=TEST_TIMEOUT)


def artifact_record(path: Path, *, kind: str, package: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "package": package,
        "version": RELEASE_VERSION,
        "filename": path.name,
        "sha256": sha256(path),
        "size": path.stat().st_size,
    }


def export_committed_source(destination: Path) -> Path:
    """Materialize HEAD so ignored build state cannot affect release archives."""

    destination.mkdir(parents=True)
    archive_path = destination.parent / f"{destination.name}.tar"
    run(["git", "archive", "--format=tar", "--output", archive_path, "HEAD"])
    with tarfile.open(archive_path, "r:") as archive:
        archive.extractall(destination, filter="data")
    archive_path.unlink()
    return destination


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


def build_and_validate_artifacts(output_dir: Path) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        fail(f"output directory must be absent or empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    work = output_dir / ".work"
    first = work / "first"
    second = work / "second"
    first_source = export_committed_source(work / "source-first")
    second_source = export_committed_source(work / "source-second")
    build_python = prepare_build_environment(work / "build-venv")
    epoch = source_date_epoch()

    first_production = build_python_package(
        build_python, first_source, first / "production", epoch=epoch
    )
    first_client = build_python_package(
        build_python,
        first_source / "packages/airpods-client-python",
        first / "python-client",
        epoch=epoch,
    )
    first_crate = build_rust_package(first / "rust-client", first_source, epoch=epoch)
    second_production = build_python_package(
        build_python, second_source, second / "production", epoch=epoch
    )
    second_client = build_python_package(
        build_python,
        second_source / "packages/airpods-client-python",
        second / "python-client",
        epoch=epoch,
    )
    second_crate = build_rust_package(
        second / "rust-client", second_source, epoch=epoch
    )

    pairs = (
        list(zip(first_production, second_production, strict=True))
        + list(zip(first_client, second_client, strict=True))
        + [(first_crate, second_crate)]
    )
    repeat_results = [
        {
            "filename": left.name,
            "byte_for_byte_equal": left.name == right.name
            and sha256(left) == sha256(right),
        }
        for left, right in pairs
    ]
    repeat_equal = all(result["byte_for_byte_equal"] for result in repeat_results)
    artifacts = first_production + first_client + [first_crate]
    copied: list[Path] = []
    for artifact in artifacts:
        destination = output_dir / artifact.name
        shutil.copy2(artifact, destination)
        copied.append(destination)

    production_wheel = next(
        path
        for path in copied
        if path.suffix == ".whl" and path.name.startswith("airpods_hr_linux-")
    )
    production_sdist = next(
        path
        for path in copied
        if path.name.startswith("airpods_hr_linux-") and path.name.endswith(".tar.gz")
    )
    client_wheel = next(
        path
        for path in copied
        if path.suffix == ".whl" and path.name.startswith("airpods_client-")
    )
    client_sdist = next(
        path
        for path in copied
        if path.name.startswith("airpods_client-") and path.name.endswith(".tar.gz")
    )
    rust_crate = next(path for path in copied if path.suffix == ".crate")
    audit_production_wheel(production_wheel)
    audit_sdist(production_sdist, package="airpods-hr-linux")
    audit_python_client_wheel(client_wheel)
    audit_sdist(client_sdist, package="airpods-client")
    audit_rust_crate(rust_crate)
    clean_install_production(production_wheel, work / "production-consumer")
    clean_install_python_client(client_wheel, work / "python-client-consumer")
    external_rust_consumer(rust_crate, work / "rust-consumer")

    records = [
        artifact_record(
            production_wheel, kind="python-wheel", package="airpods-hr-linux"
        ),
        artifact_record(
            production_sdist, kind="python-sdist", package="airpods-hr-linux"
        ),
        artifact_record(client_wheel, kind="python-wheel", package="airpods-client"),
        artifact_record(client_sdist, kind="python-sdist", package="airpods-client"),
        artifact_record(rust_crate, kind="rust-crate", package="airpods-client"),
    ]
    records.sort(key=lambda item: (item["package"], item["kind"], item["filename"]))
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_version": RELEASE_VERSION,
        "git": {"commit": git_commit(), "clean": True},
        "source_date_epoch": epoch,
        "toolchain": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "python_build_frontend": f"build {BUILD_VERSION}",
            "setuptools": SETUPTOOLS_VERSION,
            "wheel": WHEEL_VERSION,
            "rustc": output(["rustc", "--version"]),
            "cargo": output(["cargo", "--version"]),
            "platform": platform.platform(),
        },
        "artifacts": records,
        "repeat_build_check": {
            "source_date_epoch": epoch,
            "byte_for_byte_equal": repeat_equal,
            "artifact_count": len(pairs),
            "matched_artifact_count": sum(
                result["byte_for_byte_equal"] for result in repeat_results
            ),
            "artifacts": repeat_results,
            "scope": "same source, host, toolchain, and SOURCE_DATE_EPOCH",
            "reproducible_build_guarantee": False,
        },
        "validation": {
            "bluetooth_hardware_used": False,
            "production_daemon_started": False,
            "published": False,
        },
    }
    validate_manifest(manifest)
    (output_dir / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_summary(output_dir / "release-summary.txt", manifest)
    shutil.rmtree(work)
    print(f"release manifest: {output_dir / 'release-manifest.json'}")
    print(f"release summary: {output_dir / 'release-summary.txt'}")
    return manifest


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        choices=("all", "static", "python", "rust", "cross-language", "artifacts"),
        default="all",
        help="bounded CI subset; 'all' is the canonical local release gate",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="new or empty directory for generated artifacts (required by all/artifacts)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_clean_tree()
        if args.scope in {"all", "static"}:
            validate_static_policy()
        if args.scope in {"all", "python"}:
            run_python_checks()
        if args.scope in {"all", "rust"}:
            run_rust_checks()
        if args.scope in {"all", "cross-language"}:
            run_cross_language_checks()
        if args.scope in {"all", "artifacts"}:
            if args.output_dir is None:
                fail("--output-dir is required for all/artifacts validation")
            output_dir = args.output_dir.expanduser().resolve()
            if output_dir == ROOT or ROOT in output_dir.parents:
                fail("release output must be outside the repository")
            build_and_validate_artifacts(output_dir)
    except (OSError, ValidationError, subprocess.SubprocessError) as error:
        print(f"release validation failed: {error}", file=sys.stderr)
        return 1
    print(f"release validation PASS ({args.scope})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
