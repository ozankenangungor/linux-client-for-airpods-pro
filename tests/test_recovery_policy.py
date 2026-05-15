"""Iteration 10.6 differential oracle from the exact parent Python implementation.

Only the old policy nodes are compiled from git show; this is test-only and never
installed as a fallback. The parent source, not a rewritten decision table,
provides the expected result for every normalized case.
"""

from __future__ import annotations

import ast
import asyncio
import errno
import subprocess
import unittest
from enum import IntEnum
from pathlib import Path
from unittest.mock import patch

from dbus_next.errors import DBusError

from airpods_hr import production_session as production
from airpods_hr.bluez_coexistence import (
    CoexistenceCategory,
    CoexistenceFailure,
    CoexistencePhase,
)


ROOT = Path(__file__).resolve().parents[1]
PARENT = "ea54f6376b43c26d99e7fecad2987721e609edf4"
POLICY_NODES = {
    "_RECOVERABLE_COEXISTENCE_CATEGORIES",
    "_RECOVERABLE_SESSION_EXCEPTIONS",
    "_RECOVERABLE_OS_ERRNOS",
    "_TRANSIENT_DBUS_ERROR_NAMES",
    "_is_transient_dbus_error",
    "_is_recoverable_session_error",
}
FROZEN_METHODS = (
    "open", "start", "receive_report", "stop", "close",
    "_wait_for_activation_start", "_stop_locked", "_abort_activation",
    "_cancel_activation_task", "_cleanup_resources", "_record_failed_operation",
)


def parent_source(path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{PARENT}:{path}"], cwd=ROOT,
        text=True, capture_output=True, check=True,
    ).stdout


def parent_classifier():
    source = ast.parse(parent_source("src/airpods_hr/production_session.py"))
    nodes = [
        node for node in source.body
        if isinstance(node, (ast.FunctionDef, ast.Assign))
        and (
            node.name if isinstance(node, ast.FunctionDef) else node.targets[0].id
        ) in POLICY_NODES
    ]
    assert len(nodes) == len(POLICY_NODES)
    namespace = dict(vars(production))
    namespace["errno"] = errno
    exec(compile(ast.Module(body=nodes, type_ignores=[]), f"{PARENT}:production_session.py", "exec"), namespace)
    return namespace


class SyntheticOSError(OSError):
    """Avoid Python's ETIMEDOUT -> TimeoutError constructor remapping."""


def failure(category: CoexistenceCategory, cause: BaseException | None = None):
    error = CoexistenceFailure(category, CoexistencePhase.PREFLIGHT)
    error.__cause__ = cause
    return error


class ParentRecoveryDifferentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.parent = parent_classifier()
        cls.oracle = staticmethod(cls.parent["_is_recoverable_session_error"])
        cls.classify = staticmethod(production._is_recoverable_session_error)

    def compare(self, error: BaseException) -> None:
        self.assertIs(self.classify(error), self.oracle(error), repr(error))

    def test_parent_category_declaration_order_and_identity(self) -> None:
        path = "src/airpods_hr/bluez_coexistence.py"
        def enum(tree):
            return next(node for node in ast.parse(tree).body if isinstance(node, ast.ClassDef) and node.name == "CoexistenceCategory")
        self.assertEqual(
            ast.dump(enum(parent_source(path)), include_attributes=False),
            ast.dump(enum((ROOT / path).read_text()), include_attributes=False),
        )
        self.assertEqual(tuple(production._NATIVE_COEXISTENCE_CATEGORIES), tuple(CoexistenceCategory))
        self.assertEqual(len(CoexistenceCategory), 17)

    def test_exhaustive_category_cause_matrix_against_executed_parent(self) -> None:
        self.assertEqual(
            {node.value for node in self.parent["_RECOVERABLE_COEXISTENCE_CATEGORIES"]},
            {"preflight_failed", "bluez_not_available", "airpods_not_connected",
             "profile_registration_failed", "l2cap_bind_failed", "l2cap_connect_failed",
             "aap_handshake_failed", "aap_descriptor_timeout", "hr_activation_failed",
             "hr_timeout", "bluez_connection_lost"},
        )
        for category in CoexistenceCategory:
            for cause in (
                None,
                failure(CoexistenceCategory.PREFLIGHT_FAILED),
                failure(CoexistenceCategory.CLEANUP_FAILED),
                TimeoutError(), SyntheticOSError(errno.EBUSY, "synthetic"),
                DBusError("org.bluez.Error.NotReady", "synthetic"), TypeError(),
            ):
                with self.subTest(category=category, cause=type(cause).__name__):
                    self.compare(failure(category, cause))
                    kind = (0 if cause is None else 1 if isinstance(cause, CoexistenceFailure)
                            else 2 if isinstance(cause, TimeoutError) else 3 if isinstance(cause, OSError)
                            else 4 if isinstance(cause, DBusError) else 5)
                    nested = self.oracle(cause) if kind == 1 else False
                    os_errno = cause.errno if kind == 3 else None
                    name = cause.type if kind == 4 else None
                    self.assertIs(
                        production._native.classify_coexistence_recovery(
                            tuple(CoexistenceCategory).index(category), kind, nested, os_errno, name
                        ), self.oracle(failure(category, cause)),
                    )

    def test_all_parent_errno_values_and_terminal_edges(self) -> None:
        values = {getattr(errno, key) for key in (
            "EADDRNOTAVAIL", "EAGAIN", "EBUSY", "ECONNABORTED", "ECONNREFUSED",
            "ECONNRESET", "EHOSTDOWN", "EHOSTUNREACH", "EINTR", "ENETDOWN",
            "ENETRESET", "ENETUNREACH", "ENODEV", "ENOENT", "ENOTCONN", "ETIMEDOUT",
        )}
        self.assertEqual(values, self.parent["_RECOVERABLE_OS_ERRNOS"])
        for code in (*sorted(values), errno.EINVAL, errno.EPERM, 0, -1, -100, 99999,
                    -(1 << 40), 1 << 40, None):
            for category in CoexistenceCategory:
                with self.subTest(code=code, category=category):
                    self.compare(failure(category, SyntheticOSError(code, "synthetic")))
                    self.assertIs(
                        production._native.classify_coexistence_recovery(
                            tuple(CoexistenceCategory).index(category), 3, False,
                                                        code if code is None or -(1 << 31) <= code < (1 << 31) else None,
                                                        None
                        ), self.oracle(failure(category, SyntheticOSError(code, "synthetic"))),
                    )

    def test_parent_exact_dbus_names_and_near_misses(self) -> None:
        names = self.parent["_TRANSIENT_DBUS_ERROR_NAMES"]
        self.assertEqual(len(names), 9)
        for name in (
            *sorted(names), "org.bluez.Error.InvalidArguments", "org.bluez.Error.NotSupported",
            "com.example.AirPods.Error.TemporarilyMysterious",
            "org.freedesktop.DBus.Error.NoReplyExtra", "prefix.org.bluez.Error.NotReady",
            "org.bluez.Error.NotReadyExtra", "org.bluez.Error.notReady",
        ):
            for category in CoexistenceCategory:
                with self.subTest(name=name, category=category):
                    self.compare(failure(category, DBusError(name, "synthetic")))
                    self.assertIs(
                        production._native.classify_coexistence_recovery(
                            tuple(CoexistenceCategory).index(category), 4, False, None, name
                        ), self.oracle(failure(category, DBusError(name, "synthetic"))),
                    )
        for invalid_name in (None, "", "Timeout", "org.bluez.Error.NotReady "):
            self.assertFalse(production._native.classify_coexistence_recovery(
                0, 4, False, None, invalid_name
            ))

    def test_top_level_direct_exceptions_and_production_errors(self) -> None:
        for recoverable in (False, True):
            error = production.ProductionSessionError(
                production.ProductionSessionCategory.PREFLIGHT_FAILED, "open", "synthetic",
                recoverable=recoverable,
            )
            self.compare(error)
            self.assertIs(production._translate_session_error(
                production.ProductionSessionCategory.RECEIVE_FAILED, "receive_report", error
            ).recoverable, recoverable)
        for cls in self.parent["_RECOVERABLE_SESSION_EXCEPTIONS"]:
            with self.subTest(cls=cls):
                error = cls.__new__(cls)
                self.compare(error)
                self.compare(failure(CoexistenceCategory.PREFLIGHT_FAILED, error))
        for error in (RuntimeError(), TypeError(), SyntheticOSError(errno.ETIMEDOUT, "synthetic")):
            self.compare(error)

    def test_unknown_runtime_category_fails_closed_like_parent(self) -> None:
        for category in ("unknown", "", None):
            outer = failure(CoexistenceCategory.PREFLIGHT_FAILED,
                            failure(CoexistenceCategory.PREFLIGHT_FAILED, TimeoutError()))
            outer.category = category
            with self.subTest(category=category), patch.object(
                production._native, "classify_coexistence_recovery",
                wraps=production._native.classify_coexistence_recovery,
            ) as native:
                self.compare(outer)
                translated = production._translate_session_error(
                    production.ProductionSessionCategory.PREFLIGHT_FAILED, "open", outer
                )
            self.assertFalse(translated.recoverable)
            native.assert_not_called()

    def test_integer_subclass_errno_matches_parent(self) -> None:
        class Code(IntEnum):
            BUSY = errno.EBUSY
            INVALID = errno.EINVAL

        for value in Code:
            cause = SyntheticOSError(errno.EINVAL, "synthetic")
            cause.errno = value
            error = failure(CoexistenceCategory.L2CAP_BIND_FAILED, cause)
            with self.subTest(value=value), patch.object(
                production._native, "classify_coexistence_recovery",
                wraps=production._native.classify_coexistence_recovery,
            ) as native:
                self.compare(error)
            self.assertEqual(native.call_args_list[1].args,
                             (6, 3, False, int(value), None))

    def test_nested_cause_only_and_terminal_short_circuit(self) -> None:
        nested = failure(CoexistenceCategory.PREFLIGHT_FAILED, TimeoutError())
        self.compare(failure(CoexistenceCategory.L2CAP_CONNECT_FAILED, nested))
        self.compare(failure(CoexistenceCategory.L2CAP_CONNECT_FAILED,
                             failure(CoexistenceCategory.CLEANUP_FAILED, nested)))
        context_only = failure(CoexistenceCategory.PREFLIGHT_FAILED)
        context_only.__context__ = TypeError("not a recovery cause")
        self.compare(context_only)
        outer = failure(CoexistenceCategory.CLEANUP_FAILED, nested)
        with patch.object(production._native, "classify_coexistence_recovery",
                          wraps=production._native.classify_coexistence_recovery) as native:
            self.compare(outer)
        native.assert_called_once_with(16, 0, False, None, None)

    def test_translated_error_fields_native_delegation_and_fail_closed(self) -> None:
        cause = failure(CoexistenceCategory.L2CAP_BIND_FAILED,
                        OSError(errno.EINVAL, "synthetic"))
        with patch.object(production._native, "classify_coexistence_recovery",
                          wraps=production._native.classify_coexistence_recovery) as native:
            result = production._translate_session_error(
                production.ProductionSessionCategory.TRANSPORT_FAILED, "open", cause
            )
        self.assertEqual(result.category, production.ProductionSessionCategory.TRANSPORT_FAILED)
        self.assertEqual(result.phase, "open")
        self.assertEqual(result.detail, "CoexistenceFailure")
        self.assertEqual(str(result), "transport_failed at open: CoexistenceFailure")
        self.assertFalse(result.recoverable)
        self.assertIsNone(result.__cause__)
        self.assertEqual(native.call_args_list[0].args, (6, 0, False, None, None))
        self.assertEqual(native.call_args_list[1].args, (6, 3, False, errno.EINVAL, None))
        with (patch.object(production._native, "classify_coexistence_recovery",
                           side_effect=ValueError("native failure")),
              self.assertRaisesRegex(ValueError, "native failure")):
            production._is_recoverable_session_error(cause)

    def test_control_flow_context_and_cause_remain_separate(self) -> None:
        cancelled = asyncio.CancelledError()
        wrapper = failure(CoexistenceCategory.PREFLIGHT_FAILED)
        wrapper.__context__ = cancelled
        self.assertIs(production._nested_control_flow(wrapper), cancelled)
        self.compare(wrapper)
        wrapper.__cause__ = TypeError("wrapped")
        self.assertIsNone(production._nested_control_flow(wrapper))
        self.compare(wrapper)


class ParentStructuralSafetyTests(unittest.TestCase):
    def test_unrelated_production_session_ast_unchanged(self) -> None:
        path = "src/airpods_hr/production_session.py"
        old = ast.parse(parent_source(path))
        new = ast.parse((ROOT / path).read_text())
        def declarations(tree):
            return {node.name: node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
        before, after = declarations(old), declarations(new)
        for name in before.keys() - {"_is_transient_dbus_error", "_is_recoverable_session_error"}:
            self.assertIn(name, after)
            self.assertEqual(ast.dump(before[name], include_attributes=False),
                             ast.dump(after[name], include_attributes=False), name)
        session = after["InternalProductionSession"]
        old_session = before["InternalProductionSession"]
        old_methods = {node.name: node for node in old_session.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        new_methods = {node.name: node for node in session.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in FROZEN_METHODS:
            self.assertEqual(ast.dump(old_methods[name], include_attributes=False),
                             ast.dump(new_methods[name], include_attributes=False), name)
        self.assertFalse(hasattr(production, "_RECOVERABLE_COEXISTENCE_CATEGORIES"))
        self.assertFalse(hasattr(production, "_RECOVERABLE_OS_ERRNOS"))
        self.assertFalse(hasattr(production, "_TRANSIENT_DBUS_ERROR_NAMES"))


if __name__ == "__main__":
    unittest.main()
