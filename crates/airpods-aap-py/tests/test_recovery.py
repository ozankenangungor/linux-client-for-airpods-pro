"""Direct FFI checks against the debug cdylib (build with cargo --locked first)."""

import errno
import importlib.util
import types
import unittest
from pathlib import Path


LIBRARY = Path(__file__).resolve().parents[3] / "target/debug/lib_airpods_aap_core.so"

# CoexistenceCategory declaration order in bluez_coexistence.py.
RECOVERABLE_CATEGORIES = frozenset({0, 1, 2, 4, 6, 9, 11, 12, 13, 14, 15})
RECOVERABLE_ERRNOS = frozenset(
    {
        errno.EADDRNOTAVAIL,
        errno.EAGAIN,
        errno.EBUSY,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.EINTR,
        errno.ENETDOWN,
        errno.ENETRESET,
        errno.ENETUNREACH,
        errno.ENODEV,
        errno.ENOENT,
        errno.ENOTCONN,
        errno.ETIMEDOUT,
    }
)
TRANSIENT_DBUS_NAMES = frozenset(
    {
        "org.bluez.Error.NotConnected",
        "org.bluez.Error.NotReady",
        "org.freedesktop.DBus.Error.Disconnected",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
        "org.freedesktop.DBus.Error.NoNetwork",
        "org.freedesktop.DBus.Error.NoReply",
        "org.freedesktop.DBus.Error.NoServer",
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.Timeout",
    }
)


def expected_recovery(category, cause_kind, nested_recoverable, os_errno, dbus_name):
    if category not in RECOVERABLE_CATEGORIES:
        return False
    if cause_kind == 0:  # No cause
        return True
    if cause_kind == 1:  # NestedCoexistenceResult
        return nested_recoverable
    if cause_kind == 2:  # DirectRecoverable
        return True
    if cause_kind == 3:  # OsError
        return os_errno in RECOVERABLE_ERRNOS
    if cause_kind == 4:  # DBusError
        return dbus_name in TRANSIENT_DBUS_NAMES
    return False  # Other


class RecoveryBindingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("_airpods_aap_core", LIBRARY)
        assert spec is not None and spec.loader is not None
        cls.native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.native)

    def test_every_category_cause_and_nested_flag(self):
        classify = self.native.classify_coexistence_recovery
        self.assertIsInstance(classify, types.BuiltinFunctionType)
        for category in range(17):
            for cause in range(6):
                for nested in (False, True):
                    for os_errno in (None, errno.EBUSY, errno.EACCES):
                        for dbus_name in (
                            None,
                            "org.bluez.Error.NotReady",
                            "org.bluez.Error.Failed",
                        ):
                            with self.subTest(
                                category=category, cause=cause, nested=nested,
                                os_errno=os_errno, dbus_name=dbus_name,
                            ):
                                result = classify(
                                    category, cause, nested, os_errno, dbus_name
                                )
                                self.assertIs(type(result), bool)
                                self.assertEqual(
                                    result,
                                    expected_recovery(
                                        category, cause, nested, os_errno, dbus_name
                                    ),
                                )

    def test_os_errno_allowlist_and_i32_boundaries(self):
        classify = self.native.classify_coexistence_recovery
        for category in range(17):
            for os_errno in (*RECOVERABLE_ERRNOS, None, 0, -1, -(2**31), 2**31 - 1):
                with self.subTest(category=category, os_errno=os_errno):
                    self.assertIs(
                        classify(category, 3, False, os_errno, None),
                        category in RECOVERABLE_CATEGORIES
                        and os_errno in RECOVERABLE_ERRNOS,
                    )

    def test_dbus_name_allowlist_is_exact(self):
        classify = self.native.classify_coexistence_recovery
        for category in range(17):
            for dbus_name in (
                *TRANSIENT_DBUS_NAMES,
                None,
                "",
                "org.bluez.Error.Failed",
                "org.freedesktop.DBus.Error.AccessDenied",
                "org.bluez.Error.notReady",
                "org.bluez.Error.NotReady ",
            ):
                with self.subTest(category=category, dbus_name=dbus_name):
                    self.assertIs(
                        classify(category, 4, False, None, dbus_name),
                        category in RECOVERABLE_CATEGORIES
                        and dbus_name in TRANSIENT_DBUS_NAMES,
                    )

    def test_unknown_u8_identities(self):
        classify = self.native.classify_coexistence_recovery
        for first_bad, invoke in (
            (17, lambda bad: classify(bad, 0, False, None, None)),
            (6, lambda bad: classify(0, bad, False, None, None)),
        ):
            for bad in range(first_bad, 256):
                with self.subTest(first_bad=first_bad, bad=bad):
                    with self.assertRaises(ValueError):
                        invoke(bad)

    def test_invalid_identity_types_and_ranges(self):
        classify = self.native.classify_coexistence_recovery
        for invoke in (
            lambda bad: classify(bad, 0, False, None, None),
            lambda bad: classify(0, bad, False, None, None),
        ):
            for bad in (True, False, -1, -256, 256, 2**64, 1.5, "1", b"1", None, object()):
                with self.subTest(invoke=invoke, bad=bad):
                    with self.assertRaises(ValueError):
                        invoke(bad)

    def test_nested_recoverable_requires_bool_even_when_ignored(self):
        classify = self.native.classify_coexistence_recovery
        for category, cause in ((0, 1), (3, 1), (0, 0), (0, 5)):
            for bad in (0, 1, -1, 1.0, "True", None, object()):
                with self.subTest(category=category, cause=cause, bad=bad):
                    with self.assertRaises(ValueError):
                        classify(category, cause, bad, None, None)

    def test_errno_requires_nullable_i32_even_when_ignored(self):
        classify = self.native.classify_coexistence_recovery
        for category, cause in ((0, 3), (3, 3), (0, 0)):
            for bad in (True, False, -(2**31) - 1, 2**31, 1.5, "11", b"11", object()):
                with self.subTest(category=category, cause=cause, bad=bad):
                    with self.assertRaises(ValueError):
                        classify(category, cause, False, bad, None)

    def test_dbus_name_requires_nullable_str_even_when_ignored(self):
        classify = self.native.classify_coexistence_recovery
        for category, cause in ((0, 4), (3, 4), (0, 0)):
            for bad in (True, False, 0, 1.5, b"org.bluez.Error.NotReady", [], object()):
                with self.subTest(category=category, cause=cause, bad=bad):
                    with self.assertRaises(ValueError):
                        classify(category, cause, False, None, bad)


if __name__ == "__main__":
    unittest.main()
