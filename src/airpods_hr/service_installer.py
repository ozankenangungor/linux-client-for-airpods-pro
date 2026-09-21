"""Install the production daemon as a relocatable systemd user service."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from airpods_hr import _airpods_aap_core as _native


UNIT_NAME = "airpods-hubd.service"
OWNERSHIP_MARKER = "# X-AirPods-HR-Linux-Managed: 1"
SYSTEMCTL_TIMEOUT = 15.0


class ServiceInstallerError(RuntimeError):
    """Base error for safe service installation failures."""


class ForeignUnitError(ServiceInstallerError):
    """The destination contains a unit not owned by this project."""


class SystemctlError(ServiceInstallerError):
    """A bounded systemctl operation failed."""

    def __init__(self, argv: Sequence[str], detail: str) -> None:
        self.argv = tuple(argv)
        self.detail = detail
        super().__init__(f"{' '.join(argv)} failed: {detail}")


class SystemctlBoundary(Protocol):
    """The one command boundary used by service actions."""

    def argv_for(self, operation: str) -> list[str]: ...

    def run(self, operation: str) -> None: ...


class Systemctl:
    """Run an allowlisted systemctl user operation without a shell."""

    def __init__(
        self,
        *,
        executable: str = "systemctl",
        timeout: float = SYSTEMCTL_TIMEOUT,
        run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        if timeout <= 0:
            raise ValueError("systemctl timeout must be positive")
        self.executable = executable
        self.timeout = timeout
        self._run_command = run_command

    def argv_for(self, operation: str) -> list[str]:
        try:
            return _native.app_service_argv(self.executable, operation)
        except ValueError as error:
            raise ValueError(f"unsupported systemctl operation: {operation}") from error

    def run(self, operation: str) -> None:
        argv = self.argv_for(operation)
        try:
            result = self._run_command(
                argv,
                shell=False,
                timeout=self.timeout,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise SystemctlError(argv, f"timed out after {self.timeout:g}s") from error
        except OSError as error:
            raise SystemctlError(argv, str(error)) from error
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or f"exit status {result.returncode}"
            raise SystemctlError(argv, detail)


@dataclass(frozen=True)
class InstallationState:
    unit_path: Path
    exists: bool
    owned: bool
    exec_start_matches: bool
    expected_exec_start: str
    installed_exec_start: str | None

    @property
    def valid(self) -> bool:
        return _native.app_service_installation_valid(
            self.exists, self.owned, self.exec_start_matches
        )


def user_unit_path(environment: Mapping[str, str] | None = None) -> Path:
    """Resolve the systemd user unit path using the XDG config convention."""

    env = os.environ if environment is None else environment
    xdg_config_home = env.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        base = Path(xdg_config_home).expanduser()
    else:
        home = env.get("HOME")
        if not home:
            raise ServiceInstallerError(
                "HOME is required when XDG_CONFIG_HOME is unset"
            )
        base = Path(home).expanduser() / ".config"
    if not base.is_absolute():
        raise ServiceInstallerError("user configuration directory must be absolute")
    return base / "systemd" / "user" / UNIT_NAME


def installed_python(executable: str | os.PathLike[str] | None = None) -> Path:
    """Return the absolute interpreter path without resolving a venv symlink."""

    raw = os.fspath(executable) if executable is not None else sys.executable
    if not raw:
        raise ServiceInstallerError("current Python executable is unavailable")
    path = Path(os.path.abspath(raw))
    if not path.is_absolute():
        raise ServiceInstallerError("current Python executable must be absolute")
    if executable is None and not path.is_file():
        raise ServiceInstallerError(f"current Python executable does not exist: {path}")
    return path


def render_systemd_executable_path(path: Path) -> str:
    """Render a systemd-valid absolute executable token or fail closed."""
    try:
        return _native.app_service_render_executable(os.fspath(path))
    except ValueError as error:
        raise ServiceInstallerError(str(error)) from error


def exec_start_for(python: Path) -> str:
    try:
        return _native.app_service_exec_start(os.fspath(python))
    except ValueError as error:
        raise ServiceInstallerError(str(error)) from error


def render_unit(python: Path) -> str:
    """Render the sole authoritative production user-unit definition."""

    try:
        return _native.app_service_render_unit(os.fspath(python))
    except ValueError as error:
        raise ServiceInstallerError(str(error)) from error


def is_project_owned(contents: str) -> bool:
    return _native.app_service_owned(contents)


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _read_unit(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ServiceInstallerError(
            f"service unit is not valid UTF-8: {path}"
        ) from error
    except OSError as error:
        raise ServiceInstallerError(
            f"cannot read service unit {path}: {error}"
        ) from error


def _installed_exec_start(contents: str) -> str | None:
    return _native.app_service_installed_exec_start(contents)


def inspect_installation(unit_path: Path, python: Path) -> InstallationState:
    expected = exec_start_for(python)
    if not _path_exists(unit_path):
        return InstallationState(unit_path, False, False, False, expected, None)
    contents = _read_unit(unit_path)
    owned, matches, installed = _native.app_service_installation_state(
        contents, expected
    )
    return InstallationState(
        unit_path,
        True,
        owned,
        matches,
        expected,
        installed,
    )


def atomic_write_unit(path: Path, contents: str) -> None:
    """Replace a unit through a restrictive same-directory temporary file."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as error:
        raise ServiceInstallerError(
            f"cannot atomically install service unit: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def install_service(
    unit_path: Path,
    python: Path,
    *,
    systemctl: SystemctlBoundary,
    enable: bool = False,
    force: bool = False,
    writer: Callable[[Path, str], None] = atomic_write_unit,
    appimage: Path | None = None,
) -> None:
    if appimage is None:
        rendered = render_unit(python)
    else:
        try:
            rendered = _native.app_service_render_appimage_unit(os.fspath(appimage))
        except ValueError as error:
            raise ServiceInstallerError(str(error)) from error
    exists = _path_exists(unit_path)
    owned = is_project_owned(_read_unit(unit_path)) if exists else False
    try:
        actions = _native.app_service_install_plan(exists, owned, force, enable)
    except ValueError as error:
        raise ForeignUnitError(
            f"refusing to replace unrecognized service unit: {unit_path}; "
            "use --force to replace it"
        ) from error
    for action in actions:
        if action == "write":
            writer(unit_path, rendered)
        else:
            systemctl.run(action)


def uninstall_service(
    unit_path: Path,
    *,
    systemctl: SystemctlBoundary,
    disable: bool = False,
) -> None:
    exists = _path_exists(unit_path)
    owned = is_project_owned(_read_unit(unit_path)) if exists else False
    try:
        actions = _native.app_service_uninstall_plan(exists, owned, disable)
    except ValueError as error:
        raise ForeignUnitError(
            f"refusing to remove unrecognized service unit: {unit_path}"
        ) from error
    for action in actions:
        if action == "unlink":
            if _path_exists(unit_path):
                try:
                    unit_path.unlink()
                except OSError as error:
                    raise ServiceInstallerError(
                        f"cannot remove service unit: {error}"
                    ) from error
        else:
            systemctl.run(action)


def _print_plan(
    *,
    unit_path: Path,
    python: Path,
    operations: Sequence[str],
    systemctl: SystemctlBoundary,
    output: Callable[[str], None],
) -> None:
    output(f"unit_path={unit_path}")
    output(f"exec_start={exec_start_for(python)}")
    for operation in operations:
        output(f"would_run={' '.join(systemctl.argv_for(operation))}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airpods-hubd-service",
        description="Manage the airpods-hubd systemd user-service definition.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    install = subparsers.add_parser(
        "install", help="install the user-service unit"
    )
    install.add_argument("--appimage", type=Path, help=argparse.SUPPRESS)
    install.add_argument(
        "--enable", action="store_true", help="enable at login without starting now"
    )
    install.add_argument(
        "--force",
        action="store_true",
        help="replace an unrecognized unit at the destination",
    )
    install.add_argument(
        "--dry-run",
        action="store_true",
        help="show the unit destination and actions without changes",
    )

    uninstall = subparsers.add_parser(
        "uninstall", help="remove the owned user-service unit"
    )
    uninstall.add_argument(
        "--disable",
        action="store_true",
        help="disable the unit before removing it",
    )
    uninstall.add_argument(
        "--dry-run",
        action="store_true",
        help="show the destination and actions without changes",
    )

    subparsers.add_parser(
        "verify", help="inspect the installed unit without running the daemon"
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    systemctl: SystemctlBoundary | None = None,
    output: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    command = systemctl or Systemctl()
    try:
        path = user_unit_path(environment)
        python = installed_python()
        if args.action == "install":
            operations = _native.app_service_dry_run_operations(True, args.enable)
            if args.dry_run and args.appimage is not None:
                # Internal packaged mode has a fixed command, never a shell fragment.
                output(f"unit_path={path}")
                output(f"exec_start={_native.app_service_appimage_exec_start(os.fspath(args.appimage))}")
                for operation in operations:
                    output(f"would_run={' '.join(command.argv_for(operation))}")
            elif args.dry_run:
                _print_plan(
                    unit_path=path,
                    python=python,
                    operations=operations,
                    systemctl=command,
                    output=output,
                )
            else:
                install_service(
                    path,
                    python,
                    systemctl=command,
                    enable=args.enable,
                    force=args.force,
                    appimage=args.appimage,
                )
                output(f"installed={path}")
            return 0
        if args.action == "uninstall":
            operations = _native.app_service_dry_run_operations(False, args.disable)
            if args.dry_run:
                if _path_exists(path) and not is_project_owned(
                    _read_unit(path)
                ):
                    raise ForeignUnitError(
                        f"refusing to remove unrecognized service unit: {path}"
                    )
                _print_plan(
                    unit_path=path,
                    python=python,
                    operations=operations,
                    systemctl=command,
                    output=output,
                )
            else:
                uninstall_service(path, systemctl=command, disable=args.disable)
                output(f"uninstalled={path}")
            return 0

        state = inspect_installation(path, python)
        output(f"unit_path={state.unit_path}")
        output(f"exists={str(state.exists).lower()}")
        output(f"project_owned={str(state.owned).lower()}")
        output(f"exec_start_matches={str(state.exec_start_matches).lower()}")
        output(f"expected_exec_start={state.expected_exec_start}")
        return 0 if state.valid else 1
    except (OSError, ServiceInstallerError) as error:
        print(f"airpods-hubd-service: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
