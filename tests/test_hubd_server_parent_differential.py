"""Iteration 10.7: parent-policy differential and server/BlueZ safety boundaries."""

from __future__ import annotations

import ast
import asyncio
import copy
import math
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from airpods_hr._hubd import server
from tests.test_hubd_protocol_parent_differential import PARENT, ROOT, parent_protocol


SERVER_PATH = "src/airpods_hr/_hubd/server.py"
BLUEZ_PATH = "src/airpods_hr/bluez_coexistence.py"
POLICY_METHODS = {
    "_dispatch", "_subscribe", "_unsubscribe", "_wait_before_recovery",
    "_reset_recovery_backoff", "_safe_error_name", "_enqueue",
}
RESOURCE_FUNCTIONS = {
    "linux_peer_uid", "_validate_socket_path", "_probe_existing_socket",
    "_remove_safe_stale_socket",
}
RESOURCE_METHODS = {
    "_before_process_lock", "_acquire_process_lock", "_release_process_lock",
    "_acquire_listener", "_listener_bound", "_write_client",
    "_close_client_transport", "_remove_owned_socket", "_spawn_background",
}
# These are effects, not policy calls. Migrated decision helpers may be inserted
# without changing the ordering or multiplicity of hardware/IPC effects.
EFFECTS = {
    "start": {"self._before_process_lock", "self._acquire_process_lock",
              "_remove_safe_stale_socket", "self._acquire_listener",
              "self._session_factory", "asyncio.start_unix_server",
              "self._restore_session_locked", "self._dispose_current_session",
              "self._prepare_recovery_retry", "self._wait_before_recovery",
              "self._remove_owned_socket", "self._release_process_lock", "server.wait_closed"},
    "shutdown": {"task.cancel", "server.close", "self._session.stop",
                 "reader_task.cancel", "self._close_client_transport",
                 "server.wait_closed", "self._session.close",
                 "self._remove_owned_socket", "self._release_process_lock"},
    "_restore_session_locked": {"self._session_factory", "session.open", "session.start",
                                "self._read_reports"},
    "_handle_client": {"self._peer_uid_provider", "reader.readline", "decode_request",
                       "self._dispatch", "self._enqueue", "self._disconnect_client"},
    "_dispatch": {"self._subscribe", "self._unsubscribe"},
    "_subscribe": {"self._session.start", "self._begin_recovery_locked",
                   "self._notify_service_failure_locked", "self._read_reports"},
    "_unsubscribe": {"self._session.stop", "self._begin_recovery_locked",
                     "self._notify_service_failure_locked", "reader_task.cancel"},
    "_read_reports": {"session.receive_report", "heart_rate_event", "self._enqueue",
                      "self._spawn_background"},
    "_recover_session": {"self._dispose_current_session", "self._prepare_recovery_retry",
                         "self._wait_before_recovery", "self._restore_session_locked"},
    "_dispose_current_session": {"session.stop", "session.close",
                                 "self._session_cleanup_completed"},
    "_prepare_recovery_retry": {"self._epoch_refresh_is_eligible", "refresher.refresh",
                                "self._lifecycle_output"},
    "_disconnect_client": {"self._unsubscribe", "self._close_client_transport"},
}


def parent_source(path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{PARENT}:{path}"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    ).stdout


def declarations(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        node.name: node for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def server_nodes(tree: ast.Module) -> tuple[dict[str, ast.AST], dict[str, ast.AST]]:
    top = declarations(tree)
    return top, {
        node.name: node for node in top["AirPodsHubDaemon"].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def parent_methods() -> dict[str, Any]:
    _, methods = server_nodes(ast.parse(parent_source(SERVER_PATH)))
    selected = []
    for name in sorted(POLICY_METHODS):
        node = copy.deepcopy(methods[name])
        node.decorator_list = []
        selected.append(node)
    namespace = dict(vars(server))
    namespace.update(parent_protocol())
    exec(compile(ast.Module(body=selected, type_ignores=[]), f"{PARENT}:{SERVER_PATH}", "exec"), namespace)
    return namespace


def parent_constructor_validation():
    _, methods = server_nodes(ast.parse(parent_source(SERVER_PATH)))
    checks = [node for node in methods["__init__"].body if isinstance(node, ast.If)]
    if len(checks) != 2:
        raise AssertionError("parent constructor validation structure changed")
    module = ast.parse("def validate(operation_timeout, recovery_delays):\n    pass\n")
    module.body[0].body = copy.deepcopy(checks) + [ast.parse("True").body[0]]
    namespace = {"math": math}
    exec(compile(ast.fix_missing_locations(module), f"{PARENT}:{SERVER_PATH}:validation", "exec"), namespace)
    return namespace["validate"]


def effects(node: ast.AST, allowed: set[str]) -> list[str]:
    calls = [child for child in ast.walk(node) if isinstance(child, ast.Call)]
    calls.sort(key=lambda call: (call.lineno, call.col_offset))
    return [name for call in calls if (name := ast.unparse(call.func)) in allowed]


class ParentStructuralSafetyTests(unittest.TestCase):
    def test_bluez_coexistence_ast_unchanged_from_exact_parent(self) -> None:
        old = ast.parse(parent_source(BLUEZ_PATH))
        new = ast.parse((ROOT / BLUEZ_PATH).read_text())
        self.assertEqual(ast.dump(new, include_attributes=False), ast.dump(old, include_attributes=False))

    def test_server_resource_declarations_and_state_enum_unchanged(self) -> None:
        old_top, old_methods = server_nodes(ast.parse(parent_source(SERVER_PATH)))
        new_top, new_methods = server_nodes(ast.parse((ROOT / SERVER_PATH).read_text()))
        for name in RESOURCE_FUNCTIONS | {"DaemonState"}:
            with self.subTest(declaration=name):
                self.assertEqual(ast.dump(new_top[name], include_attributes=False),
                                 ast.dump(old_top[name], include_attributes=False))
        for name in RESOURCE_METHODS:
            with self.subTest(method=name):
                self.assertEqual(ast.dump(new_methods[name], include_attributes=False),
                                 ast.dump(old_methods[name], include_attributes=False))

    def test_async_effect_order_and_multiplicity_match_parent(self) -> None:
        _, old_methods = server_nodes(ast.parse(parent_source(SERVER_PATH)))
        _, new_methods = server_nodes(ast.parse((ROOT / SERVER_PATH).read_text()))
        for name, allowed in EFFECTS.items():
            with self.subTest(method=name):
                before = effects(old_methods[name], allowed)
                self.assertTrue(before, name)
                self.assertEqual(effects(new_methods[name], allowed), before)


class _Session:
    def __init__(self, events: list[tuple[str, str, bool]], error: BaseException | None = None) -> None:
        self.events = events
        self.error = error
        self.daemon: server.AirPodsHubDaemon
        self.client: server._Client

    async def start(self) -> None:
        self.events.append(("start", self.daemon.state.value, self.client.subscribed))
        if self.error is not None:
            raise self.error

    async def stop(self) -> None:
        self.events.append(("stop", self.daemon.state.value, self.client.subscribed))
        if self.error is not None:
            raise self.error

    async def receive_report(self) -> None:
        await asyncio.Future()


class ParentServerPolicyDifferentialTests(unittest.IsolatedAsyncioTestCase):
    def test_constructor_validation_matches_parent_nodes(self) -> None:
        validate = parent_constructor_validation()
        for timeout, delays in (
            (0.0, (1.0,)), (-1, (1.0,)), (1.0, ()),
            (1.0, (0.0,)), (1.0, (-1.0,)), (1.0, (math.inf,)),
            (1.0, (math.nan,)), (math.nan, (1.0,)),
            (math.inf, (1.0,)), (1.0, (0.125, 2.0)),
            (1.0, None), (1.0, (None,)),
        ):
            with self.subTest(timeout=timeout, delays=delays):
                try:
                    validate(timeout, delays)
                except Exception as expected:
                    with self.assertRaises(type(expected)) as raised:
                        server.AirPodsHubDaemon(
                            lambda: None, "/tmp/airpods-hubd-parent-differential.sock",
                            operation_timeout=timeout, recovery_delays=delays,
                        )
                    self.assertEqual(str(raised.exception), str(expected))
                else:
                    daemon = server.AirPodsHubDaemon(
                        lambda: None, "/tmp/airpods-hubd-parent-differential.sock",
                        operation_timeout=timeout, recovery_delays=delays,
                    )
                    self.assertEqual((daemon._operation_timeout, daemon._recovery_delays),
                                     (timeout, delays))

    @classmethod
    def setUpClass(cls) -> None:
        cls.old = parent_methods()

    def daemon(self, *, recoverable: bool = False) -> server.AirPodsHubDaemon:
        return server.AirPodsHubDaemon(
            lambda: None, "/tmp/airpods-hubd-parent-differential.sock",
            session_error_is_recoverable=lambda _error: recoverable,
            operation_timeout=1.0,
        )

    @staticmethod
    def client(*, subscribed: bool = False, closing: bool = False) -> server._Client:
        return server._Client(asyncio.StreamReader(), object(), subscribed=subscribed, closing=closing)

    async def run_subscription(
        self, name: str, *, old: bool, state: server.DaemonState,
        subscribed: bool = False, closing: bool = False, another: bool = False,
        has_session: bool = True, error: bool = False, recoverable: bool = False,
    ) -> tuple[Any, ...]:
        daemon = self.daemon(recoverable=recoverable)
        client = self.client(subscribed=subscribed, closing=closing)
        daemon._clients.add(client)
        if another:
            daemon._clients.add(self.client(subscribed=True))
        daemon.state = state
        events: list[tuple[str, str, bool]] = []
        session = _Session(events, RuntimeError("synthetic") if error else None)
        session.daemon, session.client = daemon, client
        daemon._session = session if has_session else None
        recoveries: list[str] = []
        # Keep the decision comparison deterministic; recovery scheduling is
        # independently covered by lifecycle tests and the effect-order guard.
        def begin_recovery(_error: BaseException) -> None:
            recoveries.append(daemon.state.value)
            daemon.state = server.DaemonState.STARTING
        daemon._begin_recovery_locked = begin_recovery
        fn = self.old[name] if old else getattr(server.AirPodsHubDaemon, name)
        try:
            try:
                result = await fn(daemon, client)
            except server.RequestError as raised:
                result = ("request_error", raised.code, raised.message, type(raised.__cause__).__name__ if raised.__cause__ else None)
            except self.old["RequestError"] as raised:
                result = ("request_error", raised.code, raised.message, type(raised.__cause__).__name__ if raised.__cause__ else None)
            return (result, daemon.state.value, client.subscribed, daemon.subscriber_count,
                    daemon._hr_may_be_active, tuple(events), tuple(recoveries),
                    daemon._reader_task is not None)
        finally:
            task = daemon._reader_task
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_subscribe_and_unsubscribe_parent_state_matrix(self) -> None:
        scenarios = (
            ("_subscribe", server.DaemonState.STARTING, {}),
            ("_subscribe", server.DaemonState.FAILED, {}),
            ("_subscribe", server.DaemonState.STOPPING_HR, {}),
            ("_subscribe", server.DaemonState.READY, {"closing": True}),
            ("_subscribe", server.DaemonState.READY, {"subscribed": True, "closing": True}),
            ("_subscribe", server.DaemonState.READY, {"has_session": False}),
            ("_subscribe", server.DaemonState.READY, {}),
            ("_subscribe", server.DaemonState.READY, {"error": True}),
            ("_subscribe", server.DaemonState.READY, {"error": True, "recoverable": True}),
            ("_subscribe", server.DaemonState.STREAMING, {"another": True}),
            ("_unsubscribe", server.DaemonState.STREAMING, {}),
            ("_unsubscribe", server.DaemonState.STREAMING, {"subscribed": True, "another": True}),
            ("_unsubscribe", server.DaemonState.STREAMING, {"subscribed": True}),
            ("_unsubscribe", server.DaemonState.STREAMING, {"subscribed": True, "error": True}),
            ("_unsubscribe", server.DaemonState.STREAMING, {"subscribed": True, "error": True, "recoverable": True}),
            ("_unsubscribe", server.DaemonState.STARTING, {"subscribed": True}),
            ("_unsubscribe", server.DaemonState.READY, {"subscribed": True}),
        )
        for name, state, options in scenarios:
            with self.subTest(name=name, state=state, options=options):
                expected = await self.run_subscription(name, old=True, state=state, **options)
                actual = await self.run_subscription(name, old=False, state=state, **options)
                self.assertEqual(actual, expected)

    async def test_dispatch_responses_and_effects_match_parent(self) -> None:
        for operation in ("hello", "ping", "status", "subscribe", "unsubscribe"):
            for already in (False, True):
                results = []
                for old in (True, False):
                    daemon = self.daemon()
                    daemon.state = server.DaemonState.STREAMING
                    client = self.client(subscribed=True)
                    daemon._clients.add(client)
                    daemon._subscribe = AsyncMock(return_value=already)
                    daemon._unsubscribe = AsyncMock(return_value=already)
                    fn = self.old["_dispatch"] if old else server.AirPodsHubDaemon._dispatch
                    result = await fn(daemon, client, {"operation": operation})
                    results.append((result, daemon._subscribe.await_args_list, daemon._unsubscribe.await_args_list))
                with self.subTest(operation=operation, already=already):
                    self.assertEqual(results[1][0], results[0][0])
                    self.assertEqual(len(results[1][1]), len(results[0][1]))
                    self.assertEqual(len(results[1][2]), len(results[0][2]))

    async def test_backoff_sequence_is_parent_behavior(self) -> None:
        for delays in ((1.0, 2.0, 5.0, 10.0), (0.125, 0.75)):
            observations = []
            for old in (True, False):
                sleeps: list[float] = []
                logs: list[str] = []
                async def sleep(delay: float) -> None:
                    sleeps.append(delay)
                daemon = server.AirPodsHubDaemon(
                    lambda: None, "/tmp/airpods-hubd-parent-differential.sock",
                    recovery_delays=delays, recovery_sleep=sleep,
                    lifecycle_output=logs.append,
                )
                wait = self.old["_wait_before_recovery"] if old else server.AirPodsHubDaemon._wait_before_recovery
                reset = self.old["_reset_recovery_backoff"] if old else server.AirPodsHubDaemon._reset_recovery_backoff
                for _ in range(len(delays) + 4):
                    await wait(daemon)
                reset(daemon)
                await wait(daemon)
                observations.append((sleeps, logs, daemon._recovery_delay_index))
            with self.subTest(delays=delays):
                self.assertEqual(observations[1], observations[0])

    def test_safe_error_name_uses_exact_parent_function(self) -> None:
        for category in (None, SimpleNamespace(value="hr_timeout"),
                         SimpleNamespace(value=""), SimpleNamespace(value=123),
                         "plain-string"):
            error = RuntimeError("do not log this message")
            error.category = category
            with self.subTest(category=category):
                self.assertEqual(server.AirPodsHubDaemon._safe_error_name(error),
                                 self.old["_safe_error_name"](error))

    async def test_enqueue_queue_full_closing_and_encoded_bytes(self) -> None:
        for closing in (False, True):
            for count in (0, server.OUTBOUND_QUEUE_SIZE - 1, server.OUTBOUND_QUEUE_SIZE):
                observations = []
                for old in (True, False):
                    daemon = self.daemon()
                    client = self.client(closing=closing)
                    for _ in range(count):
                        client.outbound.put_nowait(b"existing\n")
                    fn = self.old["_enqueue"] if old else server.AirPodsHubDaemon._enqueue
                    ok = fn(daemon, client, {"event": "heart_rate", "bpm": 169})
                    observations.append((ok, tuple(client.outbound._queue)))
                with self.subTest(closing=closing, count=count):
                    self.assertEqual(observations[1], observations[0])


if __name__ == "__main__":
    unittest.main()
