"""Current Python boundary for the native control-frame classifier."""

import unittest
from dataclasses import FrozenInstanceError, fields
from unittest.mock import patch

from airpods_hr import _airpods_aap_core as native
from airpods_hr.heart_rate_session import (
    CONNECT4_ACK,
    ControlFrameSummary,
    is_connect4_ack,
    is_observed_service_ack,
)


class ControlFrameBoundaryTests(unittest.TestCase):
    def test_summary_model_and_native_delegation(self) -> None:
        self.assertEqual(fields(ControlFrameSummary)[-1].name, "relative_to_stop_head_seconds")
        summary = ControlFrameSummary.from_frame(b"\x00", relative_to_stop_head_seconds=-1)
        self.assertEqual(summary.length, 1)
        self.assertEqual(summary.relative_to_stop_head_seconds, 0.0)
        self.assertFalse(hasattr(summary, "__dict__"))
        self.assertNotIn("raw", repr(summary))
        with self.assertRaises(FrozenInstanceError):
            summary.length = 9
        with patch.object(native, "summarize_control_frame", wraps=native.summarize_control_frame) as call:
            self.assertEqual(ControlFrameSummary.from_frame(b"\x00").length, 1)
            call.assert_called_once_with(b"\x00")
        for name, invoke in (
            ("summarize_control_frame", lambda: ControlFrameSummary.from_frame(b"")),
            ("is_observed_service_ack", lambda: is_observed_service_ack(b"", 14)),
            ("is_connect4_ack", lambda: is_connect4_ack(b"")),
        ):
            with self.subTest(name=name), patch.object(native, name, side_effect=RuntimeError("native failure")):
                with self.assertRaisesRegex(RuntimeError, "native failure"):
                    invoke()

    def test_python_input_and_exact_ack_boundary(self) -> None:
        class BytesChild(bytes):
            pass

        for frame in (b"", BytesChild(b"")):
            self.assertEqual(ControlFrameSummary.from_frame(frame).length, 0)
        for frame in (bytearray(), memoryview(b""), "", None, object()):
            with self.subTest(frame=type(frame).__name__), self.assertRaisesRegex(TypeError, "control frame must be bytes"):
                ControlFrameSummary.from_frame(frame)
        self.assertTrue(is_connect4_ack(CONNECT4_ACK))
        self.assertFalse(is_connect4_ack(CONNECT4_ACK[:-1]))
        self.assertFalse(is_observed_service_ack(b"", 14))
        with self.assertRaises(TypeError):
            is_observed_service_ack(b"", [])


if __name__ == "__main__":
    unittest.main()
