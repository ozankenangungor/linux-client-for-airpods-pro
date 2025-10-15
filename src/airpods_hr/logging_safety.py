"""Logging safeguards for operations involving Classic pairing secrets."""

from __future__ import annotations

import logging


BUMBLE_SENSITIVE_LOGGERS = (
    "bumble",
    "bumble.device",
    "bumble.hci",
    "bumble.host",
    "bumble.keys",
    "bumble.transport",
)

# Higher than every standard logging level, including CRITICAL. This is used
# because Bumble 0.0.234 has both DEBUG and ERROR paths that can format raw HCI
# packets containing Classic LinkKeys.
BUMBLE_SECRET_SAFE_LEVEL = logging.CRITICAL + 1


def harden_bumble_logging() -> None:
    """Disable all records from Bumble loggers that may carry key material.

    The setting is intentionally process-wide and persistent. Authentication
    code must not restore a more verbose Bumble level after secrets are loaded.
    """

    names = set(BUMBLE_SENSITIVE_LOGGERS)
    names.update(
        name
        for name in logging.Logger.manager.loggerDict
        if name == "bumble" or name.startswith("bumble.")
    )
    for name in names:
        logger = logging.getLogger(name)
        logger.setLevel(BUMBLE_SECRET_SAFE_LEVEL)
        logger.disabled = True
