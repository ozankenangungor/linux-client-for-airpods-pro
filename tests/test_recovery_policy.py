"""Current Python exception normalization for the native recovery classifier."""

import asyncio
import errno
import unittest
from unittest.mock import patch

from dbus_next.errors import DBusError

from airpods_hr import production_session as production
from airpods_hr.bluez_coexistence import (
    CoexistenceCategory,
    CoexistenceFailure,
    CoexistencePhase,
)


def failure(category, cause=None):
    error = CoexistenceFailure(category, CoexistencePhase.PREFLIGHT)
    error.__cause__ = cause
    return error


class RecoveryBoundaryTests(unittest.TestCase):
    def test_category_mapping_and_terminal_short_circuit(self):
        self.assertEqual(production._NATIVE_COEXISTENCE_CATEGORIES, tuple(CoexistenceCategory))
        recoverable = failure(CoexistenceCategory.PREFLIGHT_FAILED)
        terminal = failure(CoexistenceCategory.CLEANUP_FAILED, recoverable)
        self.assertTrue(production._is_recoverable_session_error(recoverable))
        with patch.object(production._native, "classify_coexistence_recovery", wraps=production._native.classify_coexistence_recovery) as native:
            self.assertFalse(production._is_recoverable_session_error(terminal))
            native.assert_called_once_with(16, 0, False, None, None)

    def test_cause_traversal_and_fail_closed(self):
        category = CoexistenceCategory.L2CAP_BIND_FAILED
        self.assertTrue(production._is_recoverable_session_error(failure(category, OSError(errno.EBUSY, "busy"))))
        self.assertTrue(production._is_recoverable_session_error(failure(category, DBusError("org.bluez.Error.NotReady", "not ready"))))
        for cause in (
            OSError(errno.EINVAL, "invalid"),
            DBusError("org.bluez.Error.InvalidArguments", "invalid"),
            TypeError("bug"),
        ):
            with self.subTest(cause=type(cause).__name__):
                self.assertFalse(production._is_recoverable_session_error(failure(category, cause)))
        self.assertTrue(production._is_recoverable_session_error(failure(category, failure(CoexistenceCategory.PREFLIGHT_FAILED))))
        self.assertFalse(production._is_recoverable_session_error(failure(category, failure(CoexistenceCategory.CLEANUP_FAILED))))
        self.assertFalse(production._is_recoverable_session_error(TypeError("bug")))

    def test_normalized_cause_reaches_native_and_native_errors_propagate(self):
        error = failure(CoexistenceCategory.L2CAP_BIND_FAILED, OSError(errno.EBUSY, "busy"))
        with patch.object(production._native, "classify_coexistence_recovery", wraps=production._native.classify_coexistence_recovery) as native:
            self.assertTrue(production._is_recoverable_session_error(error))
            self.assertEqual(native.call_args_list[0].args, (6, 0, False, None, None))
            self.assertEqual(native.call_args_list[1].args, (6, 3, False, errno.EBUSY, None))
        with patch.object(production._native, "classify_coexistence_recovery", side_effect=ValueError("native failure")):
            with self.assertRaisesRegex(ValueError, "native failure"):
                production._is_recoverable_session_error(error)

    def test_translation_provenance_and_cancellation(self):
        error = failure(CoexistenceCategory.L2CAP_BIND_FAILED, OSError(errno.EINVAL, "invalid"))
        translated = production._translate_session_error(
            production.ProductionSessionCategory.TRANSPORT_FAILED, "open", error
        )
        self.assertEqual(translated.category, production.ProductionSessionCategory.TRANSPORT_FAILED)
        self.assertEqual(translated.phase, "open")
        self.assertEqual(translated.detail, "CoexistenceFailure")
        self.assertFalse(translated.recoverable)
        wrapper = failure(CoexistenceCategory.PREFLIGHT_FAILED)
        cancelled = asyncio.CancelledError()
        wrapper.__context__ = cancelled
        self.assertIs(production._nested_control_flow(wrapper), cancelled)
        self.assertTrue(production._is_recoverable_session_error(wrapper))
        wrapper.__cause__ = TypeError("wrapped")
        self.assertIsNone(production._nested_control_flow(wrapper))
        self.assertFalse(production._is_recoverable_session_error(wrapper))


if __name__ == "__main__":
    unittest.main()
