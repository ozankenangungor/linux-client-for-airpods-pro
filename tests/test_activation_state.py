"""Parent-model parity and native delegation for the pure HR transition boundary."""

import ast
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from airpods_hr import _airpods_aap_core as native
from airpods_hr.heart_rate_session import (
    HeartRateActivationSession, HeartRateActivationState, HeartRateStateError,
)
from airpods_hr.protocol import HeartRateCommand

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = tuple(HeartRateCommand)
STATES = tuple(HeartRateActivationState)
SEND_RULES = {
    HeartRateCommand.STOP_HEAD: (0, 1),
    HeartRateCommand.CONNECT0: (2, 3),
    HeartRateCommand.CAPS0: (3, 4),
    HeartRateCommand.CONNECT4: (4, 5),
    HeartRateCommand.CAPS4: (6, 7),
    HeartRateCommand.HR_ON: (7, 8),
    HeartRateCommand.START_HR: (8, 9),
}
EVENT_RULES = ((1, 2), (5, 6), (9, 10), (10, 11), (12, 13), (14, 15))


class FakeTransport:
    def __init__(self, fail=False):
        self.fail = fail
        self.commands = []

    def send_heart_rate_command(self, command):
        if self.fail:
            raise OSError("synthetic send failure")
        self.commands.append(command)


def parent_plan(path, state, sent, command):
    """Test-only transcription of the pre-Task-10.4 private send checks."""
    if path == "activation":
        rule = SEND_RULES.get(command)
        if rule is None or state != rule[0] or command in sent:
            return "invalid or duplicate HR activation transition", None
        return None, rule[1]
    if command not in (HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF):
        return "activation command cannot use cleanup path", None
    if command in sent:
        return "duplicate HR cleanup command", None
    if command is HeartRateCommand.STOP_HR and HeartRateCommand.START_HR not in sent:
        return "STOP_HR requires START_HR", None
    if command is HeartRateCommand.HR_OFF and HeartRateCommand.HR_ON not in sent:
        return "HR_OFF requires HR_ON", None
    return None, 12 if command is HeartRateCommand.STOP_HR else 14


class ActivationTransitionTests(unittest.TestCase):
    def test_parent_model_send_parity_and_atomicity(self):
        masks = {0, 511, 1 << 5, 1 << 6, (1 << 5) | (1 << 6)}
        masks.update(1 << i for i in range(9))
        cases = 0
        for state in STATES:
            for command in COMMANDS:
                for mask in sorted(masks):
                    sent = {item for i, item in enumerate(COMMANDS) if mask & (1 << i)}
                    for path in ("activation", "cleanup"):
                        message, next_state = parent_plan(path, state.value, sent, command)
                        for fail in (False, True):
                            session = HeartRateActivationSession()
                            session.state = state
                            session._sent = sent.copy()
                            transport = FakeTransport(fail)
                            method = getattr(session, "_send_" + path)
                            if message:
                                with self.assertRaisesRegex(HeartRateStateError, '^' + message + '$'):
                                    method(transport, command)
                            elif fail:
                                with self.assertRaisesRegex(OSError, "synthetic send failure"):
                                    method(transport, command)
                            else:
                                method(transport, command)
                            expected_sent = sent | {command} if not message and not fail else sent
                            expected_state = HeartRateActivationState(next_state) if not message and not fail else state
                            self.assertEqual(session._sent, expected_sent)
                            self.assertIs(session.state, expected_state)
                            self.assertEqual(transport.commands, [command] if not message and not fail else [])
                            cases += 1
        self.assertEqual(cases, 16 * 9 * len(masks) * 2 * 2)

    def test_every_event_fails_closed_from_other_states(self):
        for event, (source, destination) in enumerate(EVENT_RULES):
            for state in STATES:
                session = HeartRateActivationSession()
                session.state = state
                if state.value == source:
                    session._advance(event)
                    self.assertIs(session.state, HeartRateActivationState(destination))
                else:
                    with self.assertRaisesRegex(HeartRateStateError, '^invalid HR state advancement$'):
                        session._advance(event)
                    self.assertIs(session.state, state)

    def test_native_input_boundary(self):
        for state in range(16):
            for command in range(9):
                for planner in (native.plan_activation_transition, native.plan_cleanup_transition):
                    try:
                        result = planner(state, 0, command)
                        self.assertIn(result, range(16))
                    except ValueError as error:
                        self.assertIn(error.args[0], range(1, 9))
            for event in range(6):
                try:
                    result = native.advance_activation_transition(state, event)
                    self.assertIn(result, range(16))
                except ValueError as error:
                    self.assertEqual(error.args[0], 6)
        for bad in (-1, 16, 256, True, 1.5, '1', None):
            for call in (lambda: native.plan_activation_transition(bad, 0, 0),
                         lambda: native.plan_cleanup_transition(bad, 0, 7),
                         lambda: native.advance_activation_transition(bad, 0)):
                with self.assertRaises(ValueError):
                    call()
        for bad in (-1, 9, 256, True, 1.5, '1', None):
            with self.assertRaises(ValueError):
                native.plan_activation_transition(0, 0, bad)
        for bad in (-1, 6, 256, True, 1.5, '1', None):
            with self.assertRaises(ValueError):
                native.advance_activation_transition(1, bad)
        for bad in (-1, 512, 65536, True, 1.5, '1', None):
            with self.assertRaises(ValueError):
                native.plan_cleanup_transition(9, bad, 7)

    def test_session_calls_native_planner(self):
        session = HeartRateActivationSession()
        with patch.object(native, 'plan_activation_transition', wraps=native.plan_activation_transition) as call:
            session._send_activation(FakeTransport(), HeartRateCommand.STOP_HEAD)
            call.assert_called_once_with(0, 0, 0)
        self.assertIs(session.state, HeartRateActivationState.STOP_HEAD_SENT)

    def test_async_orchestration_matches_parent_after_transition_sites(self):
        parent = subprocess.check_output([
            'git', 'show',
            '3e4afffc9ced337b47c3a5a2b6b3532be2a6568d:src/airpods_hr/heart_rate_session.py',
        ], cwd=ROOT, text=True)
        current = (ROOT / 'src/airpods_hr/heart_rate_session.py').read_text()

        class StripTransitionSites(ast.NodeTransformer):
            def visit_Expr(self, node):
                if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute):
                    if node.value.func.attr == '_advance':
                        return ast.copy_location(ast.Pass(), node)
                return self.generic_visit(node)

            def visit_Assign(self, node):
                if len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute) and node.targets[0].attr == 'state':
                    if isinstance(node.value, ast.Attribute) and isinstance(node.value.value, ast.Name) and node.value.value.id == 'HeartRateActivationState':
                        if node.value.attr in ('STOP_HR_SENT', 'HR_OFF_SENT'):
                            return None
                        return ast.copy_location(ast.Pass(), node)
                return self.generic_visit(node)

            def visit_Call(self, node):
                self.generic_visit(node)
                if isinstance(node.func, ast.Attribute) and node.func.attr == '_send_activation':
                    node.args = node.args[:2]
                return node

        def flows(source):
            tree = ast.parse(source)
            selected = []
            for cls in tree.body:
                if isinstance(cls, ast.ClassDef) and cls.name in ('HeartRateActivationSession', 'HeartRateMonitorActivationSession'):
                    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'run_collected')
                    selected.append(ast.dump(StripTransitionSites().visit(method), include_attributes=False))
            return selected

        self.assertEqual(flows(current), flows(parent))
