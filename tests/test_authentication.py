"""Hardware-independent tests for the Classic authentication-only session."""

from __future__ import annotations


import logging

import unittest

from io import StringIO


from airpods_hr.logging_safety import (
    BUMBLE_SECRET_SAFE_LEVEL,
    BUMBLE_SENSITIVE_LOGGERS,
    harden_bumble_logging,
)


class BumbleLoggingSafetyTests(unittest.TestCase):
    def test_sensitive_logger_levels_are_hardened_before_authentication(self) -> None:
        logger_names = set(BUMBLE_SENSITIVE_LOGGERS)
        logger_names.update(
            name
            for name in logging.Logger.manager.loggerDict
            if name == "bumble" or name.startswith("bumble.")
        )
        saved = {
            name: (
                logging.getLogger(name).level,
                logging.getLogger(name).disabled,
            )
            for name in logger_names
        }
        logger = logging.getLogger("bumble.host")
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            harden_bumble_logging()
            logger.debug(bytes(range(16)).hex())

            for name in BUMBLE_SENSITIVE_LOGGERS:
                self.assertEqual(
                    logging.getLogger(name).level,
                    BUMBLE_SECRET_SAFE_LEVEL,
                )
                self.assertTrue(logging.getLogger(name).disabled)
            self.assertEqual(stream.getvalue(), "")
        finally:
            logger.removeHandler(handler)
            for name, (level, disabled) in saved.items():
                logging.getLogger(name).setLevel(level)
                logging.getLogger(name).disabled = disabled


