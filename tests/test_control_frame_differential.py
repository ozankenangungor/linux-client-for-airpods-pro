"""Synthetic differential and Python-boundary tests for Iteration 10.3."""

from __future__ import annotations

import random
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
from tests import control_frame_parent as parent


def envelope(identifier: bytes, tail: bytes, *, declared: int | None = None) -> bytes:
    body = b"\x08" + identifier + tail
    return (
        b"\x04\x00\x04\x00\x17\x00\x00\x00\x10\x00"
        + (len(body) if declared is None else declared).to_bytes(2, "little")
        + body
    )


def corpus() -> list[bytes]:
    rng = random.Random(103)
    cases = [bytes([n & 255]) * n for n in range(257)]
    for _ in range(5000):
        cases.append(rng.randbytes(rng.randrange(257)))
    identifiers = [
        b"", b"\x00", b"\x01", b"\x7f", b"\x80", b"\x81", b"\xff",
        b"\x80\x00", b"\x80\x01", b"\xff\x7f", b"\x80\x80\x01",
        b"\xff\xff\xff\xff\x7f", b"\x80" * 5, b"\x80" * 6,
        b"\x80\x80\x80\x80\x00",
    ]
    tails = [
        b"", b"\x10", b"\x10\x01", b"\x10\x03", b"\x10\x04",
        b"\x10\x01\x4a\x02\x08\x0e", b"\x10\x03\x4a\x02\x08\x13",
        b"\x10\x01\x62\x02\x08\x10",
        b"\x10\x03\x62\x02\x08\x11\x62\x02\x08\x12\x62\x02\x08\x13",
        b"\x62\x02\x08\x01" * 6,
        b"\x10\x01" + b"\x62\x02\x08\x01" * 6,
        b"\x10\x01\x62\x02\x08\x10\x62\x02\x08\x11",
    ]
    for identifier in identifiers:
        for tail in tails:
            frame = envelope(identifier, tail)
            cases.extend([frame, frame[:-1], envelope(identifier, tail, declared=0), b"junk" + frame])
            for offset in range(min(13, len(frame))):
                changed = bytearray(frame)
                changed[offset] ^= 0xff
                cases.append(bytes(changed))
    for n in range(1, 8):
        cases.append(envelope(b"\x01", b"\x10\x01" + b"\x62\x02\x08\x10" * n))
    cases.extend([CONNECT4_ACK, CONNECT4_ACK[:-1]])
    for offset in range(len(CONNECT4_ACK)):
        changed = bytearray(CONNECT4_ACK)
        changed[offset] ^= 1
        cases.append(bytes(changed))
    return cases


class DifferentialTests(unittest.TestCase):
    def test_parent_equivalence(self) -> None:
        cases = corpus()
        self.assertGreater(len(cases), 8000)
        names = tuple(field.name for field in fields(parent.ControlFrameSummary))
        self.assertEqual(names, tuple(field.name for field in fields(ControlFrameSummary)))
        for index, frame in enumerate(cases):
            old = parent.ControlFrameSummary.from_frame(frame)
            new = ControlFrameSummary.from_frame(frame)
            self.assertEqual(
                tuple(getattr(old, name) for name in names),
                tuple(getattr(new, name) for name in names),
                f"case {index}: {frame.hex()}",
            )
            for service in (0x0e, 0x13, 0x20):
                self.assertEqual(
                    parent.is_observed_service_ack(frame, service),
                    is_observed_service_ack(frame, service),
                    f"case {index}, service {service}",
                )
            self.assertEqual(parent.is_connect4_ack(frame), is_connect4_ack(frame))

    def test_python_contract_and_native_delegation(self) -> None:
        names = tuple(field.name for field in fields(parent.ControlFrameSummary))
        self.assertEqual(names, tuple(field.name for field in fields(ControlFrameSummary)))
        self.assertEqual(fields(ControlFrameSummary)[-1].default, None)
        summary = ControlFrameSummary.from_frame(envelope(b"\x01", b"\x10\x01"), relative_to_stop_head_seconds=-1)
        self.assertIs(type(summary), ControlFrameSummary)
        self.assertEqual(summary.relative_to_stop_head_seconds, 0.0)
        self.assertIsNone(ControlFrameSummary.from_frame(b"").relative_to_stop_head_seconds)
        self.assertEqual(ControlFrameSummary.from_frame(b"", relative_to_stop_head_seconds=2.5).relative_to_stop_head_seconds, 2.5)
        self.assertIsInstance(summary.observed_62_02_08_terminal_values, tuple)
        self.assertIsInstance(summary.observed_62_02_08_group_offsets, tuple)
        self.assertFalse(hasattr(summary, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            summary.length = 9
        with patch.object(native, "summarize_control_frame", wraps=native.summarize_control_frame) as fn:
            ControlFrameSummary.from_frame(b"\x00")
            fn.assert_called_once_with(b"\x00")
        with patch.object(native, "is_observed_service_ack", wraps=native.is_observed_service_ack) as fn:
            is_observed_service_ack(b"", 0x0e)
            fn.assert_called_once_with(b"", 0x0e)
        with patch.object(native, "is_connect4_ack", wraps=native.is_connect4_ack) as fn:
            is_connect4_ack(b"")
            fn.assert_called_once_with(b"")
        for function, call in (
            ("summarize_control_frame", lambda: ControlFrameSummary.from_frame(b"")),
            ("is_observed_service_ack", lambda: is_observed_service_ack(b"", 0x0e)),
            ("is_connect4_ack", lambda: is_connect4_ack(b"")),
        ):
            with patch.object(native, function, side_effect=RuntimeError("native failure")):
                with self.assertRaisesRegex(RuntimeError, "native failure"):
                    call()

    def test_input_compatibility(self) -> None:
        class BytesChild(bytes):
            pass

        for value in (b"", BytesChild(b""), bytearray(), memoryview(b""), "", None, object()):
            for function in (parent.ControlFrameSummary.from_frame, ControlFrameSummary.from_frame):
                if isinstance(value, bytes):
                    self.assertEqual(function(value).length, 0)
                else:
                    with self.assertRaisesRegex(TypeError, "control frame must be bytes"):
                        function(value)
            for old, new in ((parent.is_connect4_ack, is_connect4_ack),):
                self.assertEqual(old(value), new(value))
            for service in (0x0e, 0x13, 0x20):
                self.assertEqual(parent.is_observed_service_ack(value, service), is_observed_service_ack(value, service))
        for service in (14.0, 19.0, -1, None, "14"):
            self.assertEqual(parent.is_observed_service_ack(b"", service), is_observed_service_ack(b"", service))
        valid = envelope(b"\x01", b"\x10\x01\x4a\x02\x08\x0e")
        malformed = valid[:-1]
        for fn in (parent.is_observed_service_ack, is_observed_service_ack):
            self.assertFalse(fn(malformed, 14.0))
            with self.assertRaises(TypeError):
                fn(valid, 14.0)
        for fn in (parent.is_observed_service_ack, is_observed_service_ack):
            with self.assertRaises(TypeError):
                fn(b"", [])


if __name__ == "__main__":
    unittest.main()
