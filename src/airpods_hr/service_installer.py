"""Install the production daemon as a relocatable systemd user service."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from collections.abc import Mapping


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

    _OPERATIONS = {
        "daemon-reload": ("daemon-reload",),
        "enable": ("enable", UNIT_NAME),
        "disable": ("disable", UNIT_NAME),
    }

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
            arguments = self._OPERATIONS[operation]
        except KeyError as error:
            raise ValueError(f"unsupported systemctl operation: {operation}") from error
        return [self.executable, "--user", "--no-ask-password", *arguments]

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
        return self.exists and self.owned and self.exec_start_matches



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



def systemd_quote_argument(value: str) -> str:
    """Quote one systemd command argument without applying shell semantics."""

    if not value:
        raise ServiceInstallerError("systemd command argument must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ServiceInstallerError(
            "systemd command argument contains a control character"
        )
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "$$")
        .replace("%", "%%")
    )
    return f'"{escaped}"'



def exec_start_for(python: Path) -> str:
    if not python.is_absolute():
        raise ServiceInstallerError("daemon Python executable must be absolute")
    return f"{systemd_quote_argument(os.fspath(python))} -m airpods_hr._hubd.main"



def render_unit(python: Path) -> str:
    """Render the sole authoritative production user-unit definition."""

    exec_start = exec_start_for(python)
    return f"""{OWNERSHIP_MARKER}
# Generated by airpods-hubd-service. Local edits may be replaced.
[Unit]
Description=AirPods persistent local sensor daemon

[Service]
Type=simple
ExecStart={exec_start}
Restart=no
KillSignal=SIGTERM
SuccessExitStatus=130 143
TimeoutStopSec=330
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
"""



def is_project_owned(contents: str) -> bool:
    return contents.startswith(f"{OWNERSHIP_MARKER}\n")



def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)



def _installed_exec_start(contents: str) -> str | None:
    values = [
        line.removeprefix("ExecStart=")
        for line in contents.splitlines()
        if line.startswith("ExecStart=")
    ]
    if len(values) != 1:
        return None
    return values[0]



def inspect_installation(unit_path: Path, python: Path) -> InstallationState:
    expected = exec_start_for(python)
    if not _path_exists(unit_path):
        return InstallationState(unit_path, False, False, False, expected, None)
    try:
        contents = unit_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ServiceInstallerError(
            f"cannot read service unit {unit_path}: {error}"
        ) from error
    installed = _installed_exec_start(contents)
    return InstallationState(
        unit_path,
        True,
        is_project_owned(contents),
        installed == expected,
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
) -> None:
    if _path_exists(unit_path):
        try:
            previous = unit_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ServiceInstallerError(
                f"cannot read existing service unit: {error}"
            ) from error
        if not is_project_owned(previous) and not force:
            raise ForeignUnitError(
                f"refusing to replace unrecognized service unit: {unit_path}; "
                "use --force to replace it"
            )
    writer(unit_path, render_unit(python))
    systemctl.run("daemon-reload")
    if enable:
        systemctl.run("enable")



def uninstall_service(
    unit_path: Path,
    *,
    systemctl: SystemctlBoundary,
    disable: bool = False,
) -> None:
    if _path_exists(unit_path):
        try:
            contents = unit_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ServiceInstallerError(
                f"cannot read existing service unit: {error}"
            ) from error
        if not is_project_owned(contents):
            raise ForeignUnitError(
                f"refusing to remove unrecognized service unit: {unit_path}"
            )
    if disable:
        systemctl.run("disable")
    if _path_exists(unit_path):
        try:
            unit_path.unlink()
        except OSError as error:
            raise ServiceInstallerError(
                f"cannot remove service unit: {error}"
            ) from error
    systemctl.run("daemon-reload")

