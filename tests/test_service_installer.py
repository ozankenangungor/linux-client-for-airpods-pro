"""Hardware-independent tests for the relocatable user-service installer."""

from __future__ import annotations

import os


import unittest


from pathlib import Path


from airpods_hr.service_installer import OWNERSHIP_MARKER, ServiceInstallerError, exec_start_for, installed_python, render_unit, systemd_quote_argument, user_unit_path


REPOSITORY = "/home/kenan/airpods-hr-linux"


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


