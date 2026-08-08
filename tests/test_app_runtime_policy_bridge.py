"""Focused native-boundary tests for application policy without live effects."""

from __future__ import annotations

import math
import unittest
from pathlib import Path
from unittest.mock import patch

from airpods_hr import _airpods_aap_core as native
from airpods_hr._hubd.production import ProductionHubConfig
from airpods_hr import service_installer


class AppRuntimeBridgeTests(unittest.TestCase):
    def test_daemon_transition_is_a_commit_after_effect_and_rejects_unknowns(self) -> None:
        self.assertEqual(
            native.app_daemon_transition("starting", "opened_without_subscribers", True, False),
            "ready",
        )
        with self.assertRaisesRegex(ValueError, "single-use"):
            native.app_daemon_transition("stopped", "start", True, False)
        with self.assertRaises(ValueError):
            native.app_daemon_transition("ready", "heart_rate_started", True, False)
        with self.assertRaises(ValueError):
            native.app_daemon_transition("secret", "start", False, False)
        self.assertEqual(
            native.app_daemon_transition("starting_hr", "recovery_retry", True, False),
            "starting",
        )
        for event in ("begin_heart_rate", "opened_without_subscribers"):
            with self.subTest(event=event), self.assertRaises(ValueError):
                native.app_daemon_transition("starting_hr", event, True, False)

    def test_production_config_rejects_nonfinite_timeout_through_native(self) -> None:
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "finite"):
                ProductionHubConfig(descriptor_timeout=bad)

    def test_service_renderer_delegates_and_native_failure_propagates(self) -> None:
        with patch.object(native, "app_service_render_unit", return_value="native unit\n") as render:
            self.assertEqual(service_installer.render_unit(Path("/usr/bin/python")), "native unit\n")
            render.assert_called_once_with("/usr/bin/python")
        with patch.object(native, "app_service_render_unit", side_effect=ValueError("invalid executable")):
            with self.assertRaisesRegex(service_installer.ServiceInstallerError, "invalid executable"):
                service_installer.render_unit(Path("/usr/bin/python"))

    def test_native_service_error_messages_include_rejected_path_only_when_required(self) -> None:
        for path, name in (
            ('/home/a"b/python', "double quote"),
            ("/home/a'b/python", "single quote"),
            ("/home/a\\b/python", "backslash"),
            ("/home/$name/bin/python", "dollar sign"),
            ("/home/a*b/python", "asterisk"),
            ("/home/a?b/python", "question mark"),
            ("/home/a[b/python", "opening square bracket"),
        ):
            with self.subTest(path=path), self.assertRaises(ValueError) as caught:
                native.app_service_render_executable(path)
            self.assertEqual(
                str(caught.exception),
                f"daemon Python executable contains unsupported {name}: {path}",
            )
        for path, expected in (
            ("venv/bin/python", "daemon Python executable must be absolute"),
            ("/bad\npath", "daemon Python executable contains a control character"),
        ):
            with self.subTest(path=path), self.assertRaises(ValueError) as caught:
                native.app_service_render_executable(path)
            self.assertEqual(str(caught.exception), expected)
        for renderer in (native.app_service_exec_start, native.app_service_render_unit):
            with self.subTest(renderer=renderer.__name__), self.assertRaises(ValueError) as caught:
                renderer("/home/$name/bin/python")
            self.assertEqual(
                str(caught.exception),
                "daemon Python executable contains unsupported dollar sign: /home/$name/bin/python",
            )

    def test_native_service_plan_and_parser_fail_closed(self) -> None:
        self.assertEqual(
            native.app_service_install_plan(True, True, False, True),
            ["write", "daemon-reload", "enable"],
        )
        with self.assertRaises(ValueError):
            native.app_service_install_plan(True, False, False, False)
        with self.assertRaises(ValueError):
            native.app_service_argv("systemctl", "start")

    def test_native_client_disposition_is_closed(self) -> None:
        self.assertEqual(native.app_daemon_client_plan("shutdown"), (True, "none"))
        self.assertEqual(native.app_daemon_client_plan("terminal_failure"), (True, "all"))
        self.assertEqual(native.app_daemon_client_plan("operation_failure"), (False, "other_clients"))
        with self.assertRaises(ValueError):
            native.app_daemon_client_plan("unknown")

    def test_native_monitor_and_path_conversions(self) -> None:
        self.assertEqual(native.app_monitor_signal_action(True, False, True), "graceful_stop")
        self.assertEqual(native.app_monitor_signal_exit_code(15), 143)
        self.assertIsNone(native.app_monitor_signal_exit_code(None))
        with self.assertRaises(ValueError):
            native.app_monitor_progress_route("unknown", True, False)
        native.app_socket_path_validate(b"/tmp/daemon.sock")
        with self.assertRaisesRegex(ValueError, "too long"):
            native.app_socket_path_validate(b"/" + b"a" * 107)


if __name__ == "__main__":
    unittest.main()
