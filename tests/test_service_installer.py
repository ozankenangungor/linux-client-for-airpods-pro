"""Hardware-independent tests for the relocatable user-service installer."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from airpods_hr import service_installer
from airpods_hr.service_installer import ForeignUnitError, OWNERSHIP_MARKER, SYSTEMCTL_TIMEOUT, ServiceInstallerError, Systemctl, SystemctlError, atomic_write_unit, exec_start_for, inspect_installation, install_service, installed_python, render_unit, systemd_quote_argument, uninstall_service, user_unit_path
from airpods_hr.service_installer import OWNERSHIP_MARKER, ServiceInstallerError, exec_start_for, installed_python, render_unit, systemd_quote_argument, user_unit_path


REPOSITORY = "/home/kenan/airpods-hr-linux"



class FakeSystemctl(Systemctl):
    def __init__(self) -> None:
        super().__init__()
        self.operations: list[str] = []

    def run(self, operation: str) -> None:
        self.operations.append(operation)



class DestinationTests(unittest.TestCase):
    def test_default_destination_uses_home_config(self) -> None:
        path = user_unit_path({"HOME": "/home/example"})
        self.assertEqual(
            path,
            Path("/home/example/.config/systemd/user/airpods-hubd.service"),
        )

    def test_xdg_config_home_overrides_default(self) -> None:
        path = user_unit_path(
            {"HOME": "/home/example", "XDG_CONFIG_HOME": "/private/config"}
        )
        self.assertEqual(
            path, Path("/private/config/systemd/user/airpods-hubd.service")
        )

    def test_relative_xdg_config_home_is_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceInstallerError, "must be absolute"):
            user_unit_path({"HOME": "/home/example", "XDG_CONFIG_HOME": "relative"})

    def test_installed_python_preserves_environment_symlink_path(self) -> None:
        path = installed_python("/opt/pipx/venvs/airpods-hr-linux/bin/python")
        self.assertEqual(path, Path("/opt/pipx/venvs/airpods-hr-linux/bin/python"))
        self.assertNotIn(REPOSITORY, os.fspath(path))



class UnitRenderingTests(unittest.TestCase):
    def test_ordinary_absolute_exec_start_is_direct(self) -> None:
        self.assertEqual(
            exec_start_for(Path("/opt/airpods/bin/python")),
            '"/opt/airpods/bin/python" -m airpods_hr._hubd.main',
        )

    def test_space_in_executable_path_is_quoted(self) -> None:
        self.assertEqual(
            exec_start_for(Path("/home/Air Pods/bin/python")),
            '"/home/Air Pods/bin/python" -m airpods_hr._hubd.main',
        )

    def test_quote_and_backslash_in_executable_path_are_escaped(self) -> None:
        self.assertEqual(
            systemd_quote_argument('/home/a"b\\c/python'),
            '"/home/a\\"b\\\\c/python"',
        )

    def test_systemd_expansion_characters_are_escaped(self) -> None:
        self.assertEqual(
            systemd_quote_argument("/home/$name/100%/python"),
            '"/home/$$name/100%%/python"',
        )

    def test_control_character_is_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceInstallerError, "control character"):
            systemd_quote_argument("/bad\npath")

    def test_relative_executable_is_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceInstallerError, "must be absolute"):
            exec_start_for(Path("venv/bin/python"))

    def test_generated_unit_preserves_accepted_service_options(self) -> None:
        unit = render_unit(Path("/opt/airpods/bin/python"))
        for expected in (
            OWNERSHIP_MARKER,
            "Type=simple",
            "Restart=no",
            "KillSignal=SIGTERM",
            "SuccessExitStatus=130 143",
            "UMask=0077",
            "NoNewPrivileges=yes",
        ):
            self.assertIn(expected, unit)
        self.assertNotIn("Environment=PATH", unit)
        self.assertNotIn("/usr/bin/env", unit)
        self.assertNotIn(REPOSITORY, unit)

    def test_shutdown_timeout_is_at_least_330_seconds(self) -> None:
        unit = render_unit(Path("/opt/airpods/bin/python"))
        line = next(
            line for line in unit.splitlines() if line.startswith("TimeoutStopSec=")
        )
        self.assertGreaterEqual(int(line.partition("=")[2]), 330)



class AtomicInstallTests(unittest.TestCase):
    def test_atomic_write_installs_complete_restrictive_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "airpods-hubd.service"
            contents = render_unit(Path("/opt/airpods/bin/python"))
            atomic_write_unit(path, contents)
            self.assertEqual(path.read_text(encoding="utf-8"), contents)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_replace_failure_preserves_previous_valid_unit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "airpods-hubd.service"
            previous = render_unit(Path("/old/environment/bin/python"))
            path.write_text(previous, encoding="utf-8")
            with patch.object(
                service_installer.os, "replace", side_effect=OSError("blocked")
            ):
                with self.assertRaisesRegex(ServiceInstallerError, "atomically"):
                    atomic_write_unit(
                        path, render_unit(Path("/new/environment/bin/python"))
                    )
            self.assertEqual(path.read_text(encoding="utf-8"), previous)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])



class InstallActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = (
            Path(self.temporary.name) / "config/systemd/user/airpods-hubd.service"
        )
        self.python = Path("/opt/pipx/venvs/airpods-hr-linux/bin/python")
        self.systemctl = FakeSystemctl()

    def test_default_install_writes_unit_and_only_reloads(self) -> None:
        install_service(self.path, self.python, systemctl=self.systemctl)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.systemctl.operations, ["daemon-reload"])
        self.assertNotIn("start", self.systemctl.operations)

    def test_explicit_enable_adds_only_enable(self) -> None:
        install_service(self.path, self.python, systemctl=self.systemctl, enable=True)
        self.assertEqual(self.systemctl.operations, ["daemon-reload", "enable"])

    def test_owned_reinstall_is_idempotent(self) -> None:
        install_service(self.path, self.python, systemctl=self.systemctl)
        first = self.path.read_bytes()
        install_service(self.path, self.python, systemctl=self.systemctl)
        self.assertEqual(self.path.read_bytes(), first)
        self.assertEqual(self.systemctl.operations, ["daemon-reload", "daemon-reload"])

    def test_foreign_unit_is_not_overwritten(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[Service]\nExecStart=/foreign\n", encoding="utf-8")
        with self.assertRaises(ForeignUnitError):
            install_service(self.path, self.python, systemctl=self.systemctl)
        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            "[Service]\nExecStart=/foreign\n",
        )
        self.assertEqual(self.systemctl.operations, [])

    def test_force_can_replace_foreign_unit(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("foreign\n", encoding="utf-8")
        install_service(self.path, self.python, systemctl=self.systemctl, force=True)
        self.assertTrue(
            self.path.read_text(encoding="utf-8").startswith(OWNERSHIP_MARKER)
        )



class VerifyAndUninstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "airpods-hubd.service"
        self.python = Path("/clean/venv/bin/python")
        self.systemctl = FakeSystemctl()

    def test_verify_recognizes_owned_matching_unit(self) -> None:
        self.path.write_text(render_unit(self.python), encoding="utf-8")
        state = inspect_installation(self.path, self.python)
        self.assertTrue(state.valid)

    def test_verify_reports_mismatched_installed_environment(self) -> None:
        self.path.write_text(
            render_unit(Path("/other/venv/bin/python")), encoding="utf-8"
        )
        state = inspect_installation(self.path, self.python)
        self.assertTrue(state.owned)
        self.assertFalse(state.exec_start_matches)
        self.assertFalse(state.valid)

    def test_verify_reports_foreign_unit(self) -> None:
        self.path.write_text("[Service]\nExecStart=/foreign\n", encoding="utf-8")
        state = inspect_installation(self.path, self.python)
        self.assertTrue(state.exists)
        self.assertFalse(state.owned)
        self.assertFalse(state.valid)

    def test_verify_reports_missing_unit(self) -> None:
        state = inspect_installation(self.path, self.python)
        self.assertFalse(state.exists)
        self.assertFalse(state.valid)

    def test_uninstall_removes_only_owned_unit_and_reloads(self) -> None:
        self.path.write_text(render_unit(self.python), encoding="utf-8")
        uninstall_service(self.path, systemctl=self.systemctl)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.systemctl.operations, ["daemon-reload"])

    def test_foreign_unit_is_not_deleted(self) -> None:
        self.path.write_text("foreign\n", encoding="utf-8")
        with self.assertRaises(ForeignUnitError):
            uninstall_service(self.path, systemctl=self.systemctl)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.systemctl.operations, [])

    def test_explicit_disable_precedes_remove_reload(self) -> None:
        self.path.write_text(render_unit(self.python), encoding="utf-8")
        uninstall_service(self.path, systemctl=self.systemctl, disable=True)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.systemctl.operations, ["disable", "daemon-reload"])

    def test_missing_unit_uninstall_is_idempotent(self) -> None:
        uninstall_service(self.path, systemctl=self.systemctl)
        self.assertEqual(self.systemctl.operations, ["daemon-reload"])



class SystemctlBoundaryTests(unittest.TestCase):
    def test_subprocess_uses_argv_no_shell_and_bounded_timeout(self) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        systemctl = Systemctl(executable="/usr/bin/systemctl", run_command=runner)
        systemctl.run("enable")
        args, kwargs = runner.call_args
        self.assertEqual(
            args[0],
            [
                "/usr/bin/systemctl",
                "--user",
                "--no-ask-password",
                "enable",
                "airpods-hubd.service",
            ],
        )
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["timeout"], SYSTEMCTL_TIMEOUT)
        self.assertTrue(kwargs["capture_output"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)

    def test_nonzero_systemctl_error_includes_captured_stderr(self) -> None:
        runner = Mock(
            return_value=subprocess.CompletedProcess([], 1, "", "permission denied")
        )
        systemctl = Systemctl(run_command=runner)
        with self.assertRaisesRegex(SystemctlError, "permission denied"):
            systemctl.run("daemon-reload")

    def test_systemctl_timeout_is_typed(self) -> None:
        runner = Mock(side_effect=subprocess.TimeoutExpired(["systemctl"], 15))
        systemctl = Systemctl(run_command=runner)
        with self.assertRaisesRegex(SystemctlError, "timed out"):
            systemctl.run("disable")

