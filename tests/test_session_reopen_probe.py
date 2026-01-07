"""Hardware-independent session lifecycle characterization tests."""

from __future__ import annotations


import unittest


from pathlib import Path
from types import SimpleNamespace


from airpods_hr.aap import AAPDescriptorObservationTimeoutError, DescriptorEvidence, HandshakeObservation


from airpods_hr.session_reopen import BlueZReopenCheckpointObserver, _ObservedHandshakeSession


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SESSION_SHA256 = (
    "f4141c6372c9bda65b4aca1b09c40e2f024ec8fe1b75964e8cc5f26c2156371e"
)


class SessionReopenDiagnosticBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_observer_is_read_only(self) -> None:
        candidate = object()
        state = SimpleNamespace(
            candidate=candidate,
            adapter_powered=True,
            device_connected=True,
        )

        class FakeBlueZClient:
            def __init__(self) -> None:
                self.calls: list[object] = []

            async def connect(self) -> None:
                self.calls.append("connect")

            async def preflight(self, *, require_connected: bool):
                self.calls.append(("preflight", require_connected))
                return state

            async def snapshot(self, selected):
                self.calls.append(("snapshot", selected))
                return state

            def close(self) -> None:
                self.calls.append("close")

        client = FakeBlueZClient()
        observer = BlueZReopenCheckpointObserver(client, timeout=1)
        initial = await observer.open()
        later = await observer.checkpoint("after_session_1_close")
        observer.close()
        self.assertTrue(initial.invariant_holds)
        self.assertTrue(later.invariant_holds)
        self.assertEqual(
            client.calls,
            [
                "connect",
                ("preflight", True),
                ("snapshot", candidate),
                "close",
            ],
        )

    async def test_handshake_observer_retains_canonical_safe_timeout(self) -> None:
        observation = HandshakeObservation(
            True,
            DescriptorEvidence(sensor_framework=True),
            post_ack_frame_count=26,
            receive_frames_dropped=0,
        )

        class Delegate:
            async def run_collected(self, transport):
                del transport
                raise AAPDescriptorObservationTimeoutError(observation)

        wrapper = _ObservedHandshakeSession(Delegate())
        with self.assertRaises(AAPDescriptorObservationTimeoutError):
            await wrapper.run_collected(object())
        self.assertIs(wrapper.observation, observation)
        self.assertEqual(wrapper.attempts, 1)
        self.assertEqual(wrapper.completed, 0)
        self.assertIsInstance(
            wrapper.error, AAPDescriptorObservationTimeoutError
        )
