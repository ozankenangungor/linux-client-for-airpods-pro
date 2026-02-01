"""Hardware-independent tests for the private production hubd composition."""

from __future__ import annotations


import unittest

from pathlib import Path

from unittest.mock import Mock, patch

from airpods_hr._hubd import production as hubd_production
from airpods_hr._hubd.production import DEFAULT_DAEMON_OPERATION_TIMEOUT, ProductionHubConfig, ProductionSessionFactory, minimum_daemon_operation_timeout


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SESSION_SHA256 = (
    "f4141c6372c9bda65b4aca1b09c40e2f024ec8fe1b75964e8cc5f26c2156371e"
)
PACKAGE_INIT_SHA256 = (
    "b50576f701568dd5d63190568c47427d6d2b65c02596a1608dbdb87f3afea35f"
)


class ProductionFactoryTests(unittest.TestCase):
    def test_factory_delegates_to_existing_production_builder_once(self) -> None:
        config = ProductionHubConfig()
        session = Mock()
        with patch.object(
            hubd_production,
            "create_production_session",
            return_value=session,
        ) as builder:
            factory = ProductionSessionFactory(
                config, output=lambda _line: None
            )
            self.assertIs(factory(), session)
            with self.assertRaisesRegex(RuntimeError, "single-use"):
                factory()
        builder.assert_called_once_with(
            descriptor_timeout=30.0,
            dbus_timeout=5.0,
            connect_timeout=10.0,
            handshake_timeout=5.0,
            start_timeout=15.0,
            stop_timeout=5.0,
            output=unittest.mock.ANY,
        )
        self.assertEqual(factory.calls, 1)
        self.assertIs(factory.session, session)

    def test_outer_timeout_covers_complete_production_windows(self) -> None:
        minimum = minimum_daemon_operation_timeout(
            descriptor_timeout=30,
            dbus_timeout=5,
            connect_timeout=10,
            handshake_timeout=5,
            start_timeout=15,
            stop_timeout=5,
        )
        self.assertEqual(minimum, 130)
        self.assertEqual(DEFAULT_DAEMON_OPERATION_TIMEOUT, 150)
        with self.assertRaisesRegex(ValueError, "production open window"):
            ProductionHubConfig(daemon_operation_timeout=129)
        with self.assertRaisesRegex(ValueError, "production open window"):
            ProductionHubConfig(
                start_timeout=150,
                daemon_operation_timeout=150,
            )


