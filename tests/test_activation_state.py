"""Python effect boundary for the native activation planner."""

import unittest
from unittest.mock import patch

from airpods_hr import _airpods_aap_core as native
from airpods_hr.heart_rate_session import (
    HeartRateActivationSession,
    HeartRateActivationState,
    HeartRateStateError,
)
from airpods_hr.protocol import HeartRateCommand


class FakeTransport:
    def __init__(self, fail=False):
        self.fail = fail
        self.commands = []

    def send_heart_rate_command(self, command):
        if self.fail:
            raise OSError("synthetic send failure")
        self.commands.append(command)


class ActivationBoundaryTests(unittest.TestCase):
    def test_send_commits_only_after_effect_and_uses_native_plan(self):
        session = HeartRateActivationSession()
        with patch.object(native, "plan_activation_transition", wraps=native.plan_activation_transition) as planner:
            with self.assertRaisesRegex(OSError, "synthetic send failure"):
                session._send_activation(FakeTransport(fail=True), HeartRateCommand.STOP_HEAD)
            planner.assert_called_once_with(0, 0, 0)
        self.assertIs(session.state, HeartRateActivationState.DESCRIPTORS_READY)
        self.assertEqual(session._sent, set())
        transport = FakeTransport()
        session._send_activation(transport, HeartRateCommand.STOP_HEAD)
        self.assertEqual(transport.commands, [HeartRateCommand.STOP_HEAD])
        self.assertIs(session.state, HeartRateActivationState.STOP_HEAD_SENT)
        self.assertEqual(session._sent, {HeartRateCommand.STOP_HEAD})
        with self.assertRaisesRegex(HeartRateStateError, "invalid or duplicate"):
            session._send_activation(transport, HeartRateCommand.STOP_HEAD)
        self.assertEqual(transport.commands, [HeartRateCommand.STOP_HEAD])

    def test_native_error_propagates_without_effect(self):
        session = HeartRateActivationSession()
        transport = FakeTransport()
        with patch.object(native, "plan_activation_transition", side_effect=RuntimeError("native failure")):
            with self.assertRaisesRegex(RuntimeError, "native failure"):
                session._send_activation(transport, HeartRateCommand.STOP_HEAD)
        self.assertEqual(transport.commands, [])
        self.assertIs(session.state, HeartRateActivationState.DESCRIPTORS_READY)

    def test_cleanup_and_advance_delegate_to_native(self):
        session = HeartRateActivationSession()
        session.state = HeartRateActivationState.START_HR_SENT
        session._sent = {HeartRateCommand.HR_ON, HeartRateCommand.START_HR}
        with patch.object(native, "advance_activation_transition", wraps=native.advance_activation_transition) as planner:
            session._advance(2)
            planner.assert_called_once_with(9, 2)
        self.assertIs(session.state, HeartRateActivationState.START_ACKNOWLEDGED)
        with patch.object(native, "plan_cleanup_transition", wraps=native.plan_cleanup_transition) as planner:
            session._send_cleanup(FakeTransport(), HeartRateCommand.STOP_HR)
            self.assertEqual(planner.call_count, 1)
        self.assertIs(session.state, HeartRateActivationState.STOP_HR_SENT)


if __name__ == "__main__":
    unittest.main()
