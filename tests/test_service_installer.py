"""Hardware-independent tests for the relocatable user-service installer."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from airpods_hr import service_installer
from airpods_hr.service_installer import (
    ForeignUnitError,
    OWNERSHIP_MARKER,
    SYSTEMCTL_TIMEOUT,
    ServiceInstallerError,
    Systemctl,
    SystemctlError,
    atomic_write_unit,
    exec_start_for,
    inspect_installation,
    install_service,
    installed_python,
    render_systemd_executable_path,
    render_unit,
    uninstall_service,
    user_unit_path,
)


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

    def test_quote_in_executable_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceInstallerError, "unsupported double quote"):
            render_systemd_executable_path(Path('/home/a"b/python'))

    def test_single_quote_in_executable_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceInstallerError, "unsupported single quote"):
            render_systemd_executable_path(Path("/home/a'b/python"))

    def test_backslash_in_executable_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceInstallerError, "unsupported backslash"):
            render_systemd_executable_path(Path("/home/a\\b/python"))

    def test_dollar_in_executable_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceInstallerError, "unsupported dollar sign"):
            render_systemd_executable_path(Path("/home/$name/bin/python"))

    def test_systemd_glob_metacharacters_are_rejected(self) -> None:
        unsupported = {
            "*": "asterisk",
            "?": "question mark",
            "[": "opening square bracket",
        }
        for character, description in unsupported.items():
            with self.subTest(character=character):
                with self.assertRaisesRegex(
                    ServiceInstallerError, f"unsupported {description}"
                ):
                    render_systemd_executable_path(
                        Path(f"/home/a{character}b/python")
                    )

    def test_percent_in_executable_path_uses_systemd_specifier_escape(self) -> None:
        self.assertEqual(
            render_systemd_executable_path(Path("/home/name/100%/python")),
            '"/home/name/100%%/python"',
        )

    def test_control_character_is_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceInstallerError, "control character"):
            render_systemd_executable_path(Path("/bad\npath"))

    def test_relative_executable_is_rejected(self) -> None:
        with self.assertRaisesRegex(ServiceInstallerError, "must be absolute"):
            exec_start_for(Path("venv/bin/python"))

    def test_executable_error_messages_match_historical_contract(self) -> None:
        paths = (
            ('/home/a"b/python', "double quote"),
            ("/home/a'b/python", "single quote"),
            ("/home/a\\b/python", "backslash"),
            ("/home/$name/bin/python", "dollar sign"),
            ("/home/a*b/python", "asterisk"),
            ("/home/a?b/python", "question mark"),
            ("/home/a[b/python", "opening square bracket"),
        )
        for path, name in paths:
            with self.subTest(path=path), self.assertRaises(ServiceInstallerError) as caught:
                render_systemd_executable_path(Path(path))
            self.assertEqual(
                str(caught.exception),
                f"daemon Python executable contains unsupported {name}: {path}",
            )
        for path, expected in (
            ("venv/bin/python", "daemon Python executable must be absolute"),
            ("/bad\npath", "daemon Python executable contains a control character"),
        ):
            with self.subTest(path=path), self.assertRaises(ServiceInstallerError) as caught:
                render_systemd_executable_path(Path(path))
            self.assertEqual(str(caught.exception), expected)

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

    def test_dry_run_mutates_nothing_and_calls_no_systemctl(self) -> None:
        output: list[str] = []
        environment_python = Path(self.temporary.name) / "installed env/bin/python"
        environment_python.parent.mkdir(parents=True)
        environment_python.touch()
        with patch.object(
            service_installer.sys, "executable", os.fspath(environment_python)
        ):
            status = service_installer.main(
                ["install", "--dry-run", "--enable"],
                environment={"XDG_CONFIG_HOME": os.fspath(self.path.parents[2])},
                systemctl=self.systemctl,
                output=output.append,
            )
        self.assertEqual(status, 0)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.systemctl.operations, [])
        self.assertIn(f"unit_path={self.path}", output)
        self.assertIn(
            f'exec_start="{environment_python}" -m airpods_hr._hubd.main', output
        )
        self.assertEqual(sum(line.startswith("would_run=") for line in output), 2)

    def test_invalid_executable_preserves_owned_unit_and_skips_systemctl(
        self,
    ) -> None:
        previous = render_unit(Path("/old/environment/bin/python"))
        self.path.parent.mkdir(parents=True)
        self.path.write_text(previous, encoding="utf-8")
        with self.assertRaisesRegex(ServiceInstallerError, "unsupported double quote"):
            install_service(
                self.path,
                Path('/new/invalid"environment/bin/python'),
                systemctl=self.systemctl,
            )
        self.assertEqual(self.path.read_text(encoding="utf-8"), previous)
        self.assertEqual(self.systemctl.operations, [])

    def test_single_quote_executable_preserves_owned_unit_and_skips_systemctl(
        self,
    ) -> None:
        previous = render_unit(Path("/old/environment/bin/python"))
        self.path.parent.mkdir(parents=True)
        self.path.write_text(previous, encoding="utf-8")
        with self.assertRaisesRegex(ServiceInstallerError, "unsupported single quote"):
            install_service(
                self.path,
                Path("/new/invalid'environment/bin/python"),
                systemctl=self.systemctl,
            )
        self.assertEqual(self.path.read_text(encoding="utf-8"), previous)
        self.assertEqual(self.systemctl.operations, [])

    def test_invalid_executable_dry_run_is_safe_cli_error(self) -> None:
        bad_python = Path(self.temporary.name) / 'invalid"env/bin/python'
        bad_python.parent.mkdir(parents=True)
        bad_python.touch()
        stderr = StringIO()
        output: list[str] = []
        with (
            patch.object(service_installer.sys, "executable", os.fspath(bad_python)),
            redirect_stderr(stderr),
        ):
            status = service_installer.main(
                ["install", "--dry-run"],
                environment={"XDG_CONFIG_HOME": os.fspath(self.path.parents[2])},
                systemctl=self.systemctl,
                output=output.append,
            )
        self.assertEqual(status, 2)
        self.assertIn("unsupported double quote", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(self.path.exists())
        self.assertEqual(self.systemctl.operations, [])
        self.assertEqual(output, [f"unit_path={self.path}"])

    def test_invalid_current_executable_verify_is_safe_cli_error(self) -> None:
        bad_python = Path(self.temporary.name) / "invalid$env/bin/python"
        bad_python.parent.mkdir(parents=True)
        bad_python.touch()
        stderr = StringIO()
        with (
            patch.object(service_installer.sys, "executable", os.fspath(bad_python)),
            redirect_stderr(stderr),
        ):
            status = service_installer.main(
                ["verify"],
                environment={"XDG_CONFIG_HOME": os.fspath(self.path.parents[2])},
                systemctl=self.systemctl,
            )
        self.assertEqual(status, 2)
        self.assertIn("unsupported dollar sign", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(self.systemctl.operations, [])

    def test_single_quote_executable_dry_run_is_safe_cli_error(self) -> None:
        bad_python = Path(self.temporary.name) / "invalid'env/bin/python"
        bad_python.parent.mkdir(parents=True)
        bad_python.touch()
        stderr = StringIO()
        output: list[str] = []
        with (
            patch.object(service_installer.sys, "executable", os.fspath(bad_python)),
            redirect_stderr(stderr),
        ):
            status = service_installer.main(
                ["install", "--dry-run"],
                environment={"XDG_CONFIG_HOME": os.fspath(self.path.parents[2])},
                systemctl=self.systemctl,
                output=output.append,
            )
        self.assertEqual(status, 2)
        self.assertIn("unsupported single quote", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(self.path.exists())
        self.assertEqual(self.systemctl.operations, [])
        self.assertEqual(output, [f"unit_path={self.path}"])

    def test_single_quote_current_executable_verify_is_safe_cli_error(self) -> None:
        bad_python = Path(self.temporary.name) / "invalid'env/bin/python"
        bad_python.parent.mkdir(parents=True)
        bad_python.touch()
        stderr = StringIO()
        with (
            patch.object(service_installer.sys, "executable", os.fspath(bad_python)),
            redirect_stderr(stderr),
        ):
            status = service_installer.main(
                ["verify"],
                environment={"XDG_CONFIG_HOME": os.fspath(self.path.parents[2])},
                systemctl=self.systemctl,
            )
        self.assertEqual(status, 2)
        self.assertIn("unsupported single quote", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(self.systemctl.operations, [])


class RealSystemdExecutablePathTests(unittest.TestCase):
    def _assert_systemd_accepts(self, relative_executable: str) -> None:
        analyzer = shutil.which("systemd-analyze")
        if analyzer is None:
            self.skipTest("systemd-analyze is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            (runtime / "systemd").mkdir(mode=0o700)
            executable = root / relative_executable
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            unit_path = root / "airpods-hubd.service"
            unit_path.write_text(render_unit(executable), encoding="utf-8")
            environment = dict(os.environ)
            environment["XDG_RUNTIME_DIR"] = os.fspath(runtime)
            result = subprocess.run(
                [analyzer, "verify", "--user", os.fspath(unit_path)],
                shell=False,
                timeout=15,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_systemd_accepts_ordinary_executable_path(self) -> None:
        self._assert_systemd_accepts("ordinary/bin/python")

    def test_real_systemd_accepts_space_in_executable_path(self) -> None:
        self._assert_systemd_accepts("installed env/bin/python")

    def test_real_systemd_accepts_percent_in_executable_path(self) -> None:
        self._assert_systemd_accepts("percent%env/bin/python")


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


class InvalidUnitEncodingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config_home = Path(self.temporary.name) / "config"
        self.path = self.config_home / "systemd/user/airpods-hubd.service"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff\xfe\x00")
        self.python = Path("/clean/venv/bin/python")
        self.systemctl = FakeSystemctl()

    def test_install_does_not_replace_invalid_utf8_unit(self) -> None:
        previous = self.path.read_bytes()
        with self.assertRaisesRegex(ServiceInstallerError, "not valid UTF-8"):
            install_service(self.path, self.python, systemctl=self.systemctl)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.systemctl.operations, [])

    def test_verify_reports_invalid_utf8_as_safe_cli_error(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            status = service_installer.main(
                ["verify"],
                environment={"XDG_CONFIG_HOME": os.fspath(self.config_home)},
                systemctl=self.systemctl,
            )
        self.assertEqual(status, 2)
        self.assertIn("not valid UTF-8", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(self.systemctl.operations, [])

    def test_uninstall_does_not_delete_invalid_utf8_unit(self) -> None:
        previous = self.path.read_bytes()
        with self.assertRaisesRegex(ServiceInstallerError, "not valid UTF-8"):
            uninstall_service(self.path, systemctl=self.systemctl)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.systemctl.operations, [])

    def test_uninstall_dry_run_reports_invalid_utf8_as_safe_cli_error(self) -> None:
        previous = self.path.read_bytes()
        stderr = StringIO()
        with redirect_stderr(stderr):
            status = service_installer.main(
                ["uninstall", "--dry-run"],
                environment={"XDG_CONFIG_HOME": os.fspath(self.config_home)},
                systemctl=self.systemctl,
            )
        self.assertEqual(status, 2)
        self.assertIn("not valid UTF-8", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.systemctl.operations, [])


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

    def test_verify_cli_is_hardware_independent(self) -> None:
        output: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            environment = {"XDG_CONFIG_HOME": directory}
            stderr = StringIO()
            with redirect_stderr(stderr):
                status = service_installer.main(
                    ["verify"],
                    environment=environment,
                    systemctl=FakeSystemctl(),
                    output=output.append,
                )
        self.assertEqual(status, 1)
        self.assertIn("exists=false", output)
        self.assertEqual(stderr.getvalue(), "")


class PackageBoundaryTests(unittest.TestCase):
    def test_production_distribution_excludes_standalone_python_sdk(self) -> None:
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            project["tool"]["maturin"]["python-packages"], ["airpods_hr"]
        )
        self.assertFalse((root / "src/airpods_client").exists())
        self.assertTrue(
            (root / "packages/airpods-client-python/src/airpods_client").is_dir()
        )


if __name__ == "__main__":
    unittest.main()
