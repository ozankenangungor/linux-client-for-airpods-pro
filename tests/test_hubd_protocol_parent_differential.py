"""Iteration 10.7: independent JSONL oracle from the exact pre-migration parent."""

from __future__ import annotations

import ast
import inspect
import json
import random
import subprocess
import unittest
from pathlib import Path
from typing import Any

from airpods_hr._hubd import protocol
from airpods_hr.heartrate import HeartRateReport


ROOT = Path(__file__).resolve().parents[1]
PARENT = "900ea9ae3eded3040a558123c516ebaf60750e7c"
PATH = "src/airpods_hr/_hubd/protocol.py"
NAMES = {
    "PROTOCOL_VERSION", "MAX_FRAME_SIZE", "OUTBOUND_QUEUE_SIZE",
    "HEART_RATE_STREAM", "SUPPORTED_OPERATIONS", "RequestError",
    "decode_request", "response", "error_response", "heart_rate_event",
    "encode_message",
}


def parent_protocol() -> dict[str, Any]:
    source = subprocess.run(
        ["git", "show", f"{PARENT}:{PATH}"], cwd=ROOT,
        capture_output=True, text=True, check=True,
    ).stdout
    nodes = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in NAMES:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in NAMES
            for target in node.targets
        ):
            nodes.append(node)
    if len(nodes) != len(NAMES):
        raise AssertionError("parent protocol declarations changed")
    namespace: dict[str, Any] = {
        "json": json, "Any": Any, "HeartRateReport": HeartRateReport,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), f"{PARENT}:{PATH}", "exec"), namespace)
    return namespace


class ProtocolParentDifferentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.old = parent_protocol()

    def compare_decode(self, frame: bytes | bytearray | str) -> None:
        try:
            expected = self.old["decode_request"](frame)
        except Exception as old_error:
            with self.assertRaises(Exception) as raised:
                protocol.decode_request(frame)
            new_error = raised.exception
            if isinstance(old_error, self.old["RequestError"]):
                self.assertIs(type(new_error), protocol.RequestError, repr(frame))
                self.assertEqual(
                    (new_error.code, new_error.message, str(new_error)),
                    (old_error.code, old_error.message, str(old_error)), repr(frame),
                )
            else:
                self.assertIs(type(new_error), type(old_error), repr(frame))
                self.assertEqual(str(new_error), str(old_error), repr(frame))
        else:
            self.assertEqual(protocol.decode_request(frame), expected, repr(frame))

    def test_parent_constants_and_error_contract(self) -> None:
        for name in (
            "PROTOCOL_VERSION", "MAX_FRAME_SIZE", "OUTBOUND_QUEUE_SIZE",
            "HEART_RATE_STREAM", "SUPPORTED_OPERATIONS",
        ):
            self.assertEqual(getattr(protocol, name), self.old[name], name)
        self.assertIs(protocol.RequestError.__base__, ValueError)
        for name in ("decode_request", "response", "error_response", "heart_rate_event", "encode_message"):
            old_params = inspect.signature(self.old[name]).parameters
            new_params = inspect.signature(getattr(protocol, name)).parameters
            self.assertEqual(
                [(param.name, param.kind, param.default) for param in new_params.values()],
                [(param.name, param.kind, param.default) for param in old_params.values()],
                name,
            )
        for code, message in (("invalid_json", "synthetic"), ("", "")):
            error = protocol.RequestError(code, message)
            self.assertEqual((error.code, error.message, str(error)), (code, message, message))

    def test_decode_request_parent_boundaries_and_precedence(self) -> None:
        limit = self.old["MAX_FRAME_SIZE"]
        valid = b'{"protocol_version":1,"operation":"ping"}'
        self.assertLess(len(valid), limit)
        frames: list[bytes | bytearray | str] = [
            b"", b"\xff", b"\xc0\xaf", b"\xef\xbb\xbf{}", b"null", b"[]",
            b"true", b"1", b"{}", b'{"protocol_version":true,"operation":"ping"}',
            b'{"protocol_version":1,"operation":"ping"}\n',
            b'{"protocol_version":1,"operation":"ping"} {}',
            b'{"protocol_version":1,"protocol_version":2,"operation":"ping"}',
            b'{"protocol_version":1,"operation":"ping","operation":"unknown"}',
            b" " * limit, b" " * (limit + 1),
            valid + b" " * (limit - len(valid)),
            valid + b" " * (limit + 1 - len(valid)),
            bytearray(b'{"protocol_version":1,"operation":"ping"}'),
            '{"protocol_version":1,"operation":"ping"}',
        ]
        self.assertEqual(len(frames[16]), limit)
        self.assertEqual(len(frames[17]), limit + 1)
        for version in (None, False, True, 0, 1, 1.0, "1", [], {}):
            for operation in (None, "", 0, False, "ping", "hello", "status", "subscribe", "unsubscribe", "PING", "é"):
                for stream in (None, "heart_rate", "other"):
                    frames.append(json.dumps({
                        "protocol_version": version, "operation": operation,
                        "stream": stream, "extra": {"opaque": [1, None, True]},
                    }).encode())
        for index, frame in enumerate(frames):
            with self.subTest(index=index):
                self.compare_decode(frame)

    def test_decode_request_random_bytes_and_json_objects(self) -> None:
        rng = random.Random(107)
        for index in range(1800):
            if index % 2:
                frame = rng.randbytes(rng.randrange(0, 130))
            else:
                frame = json.dumps({
                    "protocol_version": rng.choice((None, 0, 1, 2, True, 1.0, "1")),
                    "operation": rng.choice(("ping", "hello", "status", "subscribe", "unsubscribe", "unknown", "")),
                    "stream": rng.choice(("heart_rate", "Heart_Rate", None)),
                    "nonce": index,
                }, ensure_ascii=index % 4 == 0).encode()
            with self.subTest(index=index):
                self.compare_decode(frame)

    def test_responses_and_exact_encoding_match_parent(self) -> None:
        for operation, fields in (
            ("hello", {}), ("status", {"state": "ready", "subscriber_count": 2}),
            ("subscribe", {"stream": "heart_rate", "already_subscribed": False}),
            ("hello", {"protocol_version": 7, "ok": False}),
            ("é", {"meta": {"z": [None, True, "café"], "a": -1.5}}),
        ):
            with self.subTest(operation=operation, fields=fields):
                old = self.old["response"](operation, **fields)
                new = protocol.response(operation, **fields)
                self.assertEqual(new, old)
                self.assertEqual(protocol.encode_message(new), self.old["encode_message"](old))
        for fn in (self.old["response"], protocol.response):
            with self.assertRaisesRegex(TypeError, "operation"):
                fn("hello", operation="override")
        for code in ("service_failed", "", "é", None, 17):
            self.assertEqual(protocol.error_response(code), self.old["error_response"](code))
            for message in ("", "bad request", "sensor service is unavailable"):
                old = self.old["RequestError"](code, message)
                new = protocol.RequestError(code, message)
                self.assertEqual(protocol.error_response(new), self.old["error_response"](old))
        for message in (
            {}, {"b": 2, "a": 1}, {"msg": "café\n\u0000"},
            {"array": [None, True, 1.25]}, {"nested": {"z": 0, "a": -7}},
        ):
            with self.subTest(message=message):
                self.assertEqual(protocol.encode_message(message), self.old["encode_message"](message))
        with self.assertRaises(TypeError):
            self.old["encode_message"]({"value": object()})
        with self.assertRaises(TypeError):
            protocol.encode_message({"value": object()})

    def test_heart_rate_event_matches_parent_for_every_side(self) -> None:
        for bpm in (0, 1, 80, 255, -1, 100_000):
            for side in (None, 0, 1, 2, 3, 255, -1, True, "1"):
                report = HeartRateReport(
                    bpm=bpm, aux=0, sequence=0, field_5=side,
                    timestamp_ticks=0, flags=0,
                )
                with self.subTest(bpm=bpm, side=side):
                    expected = self.old["heart_rate_event"](report)
                    actual = protocol.heart_rate_event(report)
                    self.assertEqual(actual, expected)
                    self.assertEqual(
                        protocol.encode_message(actual), self.old["encode_message"](expected),
                    )


if __name__ == "__main__":
    unittest.main()
