"""Direct hub FFI checks after `cargo build -p airpods-aap-py`."""

import importlib.util
import json
import math
import types
import unittest
from pathlib import Path
from unittest.mock import patch


LIBRARY = Path(__file__).resolve().parents[3] / "target/debug/lib_airpods_aap_core.so"


class HubBindingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("_airpods_aap_core", LIBRARY)
        assert spec is not None and spec.loader is not None
        cls.native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.native)

    def test_constants(self):
        version, max_frame, queue_size, stream, operations, states = self.native.hub_constants()
        self.assertEqual(version, 1)
        self.assertEqual(max_frame, 4096)
        self.assertEqual(queue_size, 16)
        self.assertEqual(stream, "heart_rate")
        self.assertEqual(
            operations, ["hello", "status", "subscribe", "unsubscribe", "ping"]
        )
        self.assertEqual(
            states,
            [
                "stopped", "starting", "ready", "starting_hr", "streaming",
                "stopping_hr", "failed", "shutting_down",
            ],
        )

    def test_validation_reports_core_error_code_and_message(self):
        version = self.native.hub_constants()[0]
        self.assertIsNone(
            self.native.hub_validate_request(
                f'{{"protocol_version":{version},"operation":"ping"}}'.encode()
            )
        )
        for frame, code, message in (
            (b"{", "invalid_json", "request must be valid UTF-8 JSON"),
            (b"[]", "invalid_request", "request must be a JSON object"),
            (
                b'{"protocol_version":999,"operation":"ping"}',
                "unsupported_version", "unsupported experimental protocol version",
            ),
            (
                b'{"protocol_version":1,"operation":"subscribe","stream":"bad"}',
                "invalid_stream", "stream must be heart_rate",
            ),
        ):
            with self.subTest(frame=frame), self.assertRaises(
                self.native.HubRequestError
            ) as caught:
                self.native.hub_validate_request(frame)
            error = caught.exception
            self.assertIsInstance(error, ValueError)
            self.assertEqual(error.code, code)
            self.assertEqual(error.message, message)
            self.assertEqual(str(error), message)
        with self.assertRaises(self.native.HubRequestError) as caught:
            self.native.hub_validate_request(b" " * (self.native.hub_constants()[1] + 1))
        self.assertEqual(caught.exception.code, "frame_too_large")
        with self.assertRaises(TypeError):
            self.native.hub_validate_request("not bytes")

    def test_decode_request_keeps_python_json_types_and_parses_once(self):
        decode = self.native.hub_decode_request
        self.assertIsInstance(decode, types.BuiltinFunctionType)
        self.assertIsInstance(self.native.hub_validate_request, types.BuiltinFunctionType)
        request = {
            "protocol_version": 1, "operation": "ping",
            "opaque": {"large": 2**100, "float": float("nan"), "list": [True, None]},
        }
        frame = json.dumps(request).encode("utf-16")
        with patch("json.loads", wraps=json.loads) as loads:
            result = decode(frame)
            self.assertEqual(loads.call_count, 1)
        self.assertIs(type(result), dict)
        self.assertEqual(result["opaque"]["large"], 2**100)
        self.assertTrue(math.isnan(result["opaque"]["float"]))
        self.assertEqual(result["opaque"]["list"], [True, None])
        self.assertEqual(decode(bytearray(frame))["operation"], "ping")
        self.assertEqual(
            decode('{"protocol_version":1,"operation":"ping","extra":true}')["extra"],
            True,
        )
        self.assertEqual(
            decode(b'{"protocol_version":1,"operation":"status","operation":"ping"}')
            ["operation"],
            "ping",
        )
        with patch("json.loads", wraps=json.loads) as loads:
            self.assertIsNone(self.native.hub_validate_request(frame))
            self.assertEqual(loads.call_count, 1)

    def test_decode_request_validation_and_error_precedence(self):
        decode = self.native.hub_decode_request
        invalid = (
            (b"", "invalid_json"),
            (b"\xff", "invalid_json"),
            (b"[]", "invalid_request"),
            (b"NaN", "invalid_request"),
            (b'{"protocol_version":true,"operation":"ping"}', "unsupported_version"),
            (b'{"protocol_version":9223372036854775808,"operation":"ping"}', "unsupported_version"),
            (b'{"protocol_version":1,"operation":""}', "invalid_operation"),
            (b'{"protocol_version":1,"operation":"oops"}', "unknown_operation"),
            (b'{"protocol_version":1,"operation":"subscribe"}', "invalid_stream"),
            (json.dumps({"protocol_version": 1, "operation": "\ud800"}), "unknown_operation"),
            (
                json.dumps({"protocol_version": 1, "operation": "subscribe", "stream": "\ud800"}),
                "invalid_stream",
            ),
        )
        for frame, code in invalid:
            with self.subTest(frame=frame), self.assertRaises(self.native.HubRequestError) as caught:
                decode(frame)
            self.assertEqual(caught.exception.code, code)
            self.assertIsInstance(caught.exception.message, str)
            self.assertEqual(str(caught.exception), caught.exception.message)

        for frame in (None, 1, True, [], {}, memoryview(b"{}")):
            with self.subTest(frame=frame), self.assertRaises(TypeError):
                decode(frame)
        limit = self.native.hub_constants()[1]
        payload = '{"protocol_version":1,"operation":"ping","extra":"'
        exact = payload + "é" * (limit - len(payload) - 2) + '"}'
        self.assertEqual(len(exact), limit)
        self.assertGreater(len(exact.encode()), limit)
        self.assertEqual(decode(exact)["operation"], "ping")
        with patch("json.loads", side_effect=AssertionError("parsed oversized frame")) as loads:
            with self.assertRaises(self.native.HubRequestError) as caught:
                decode(exact + " ")
            self.assertEqual(caught.exception.code, "frame_too_large")
            loads.assert_not_called()

    def test_source_side_accepts_signed_i64(self):
        self.assertEqual(self.native.hub_source_side(1), ("left", None))
        self.assertEqual(self.native.hub_source_side(2), ("right", None))
        for raw in (0, -1, -(2**63), 2**63 - 1):
            with self.subTest(raw=raw):
                self.assertEqual(self.native.hub_source_side(raw), ("unknown", raw))
        for raw in (-(2**63) - 1, 2**63):
            with self.subTest(raw=raw), self.assertRaises(OverflowError):
                self.native.hub_source_side(raw)

    def test_subscription_decisions_and_invalid_state(self):
        decide = self.native.hub_subscribe_decision
        self.assertEqual(decide("ready", True, False, 1, True), ("already", None, None))
        self.assertEqual(decide("ready", False, False, 0, True), ("start", None, None))
        self.assertEqual(decide("streaming", False, False, 1, True), ("join", None, None))
        self.assertEqual(
            decide("ready", False, True, 0, True),
            ("reject", "connection_closing", "connection is closing"),
        )
        self.assertEqual(
            decide("failed", False, False, 0, True),
            ("reject", "service_failed", "sensor service is unavailable"),
        )
        with self.assertRaisesRegex(ValueError, "invalid daemon state"):
            decide("unknown", False, False, 0, True)

    def test_unsubscription_decisions(self):
        decide = self.native.hub_unsubscribe_decision
        self.assertEqual(decide(False, 0, "streaming", True), "already")
        self.assertEqual(decide(True, 1, "streaming", True), "remove")
        self.assertEqual(decide(True, 0, "streaming", True), "stop")
        with self.assertRaisesRegex(ValueError, "invalid daemon state"):
            decide(True, 0, "unknown", True)


    def test_recovery_decisions(self):
        step = self.native.hub_recovery_step
        self.assertEqual(step(0, [1.0, 2.0], False), (1.0, 1))
        self.assertEqual(step(1, [1.0, 2.0], False), (2.0, 2))
        self.assertEqual(step(2, [1.0, 2.0], False), (2.0, 3))
        self.assertIsNone(step(0, [], False))
        self.assertIsNone(step(0, [1.0], True))
        self.assertEqual(self.native.hub_restore_decision(0, False), "ready")
        self.assertEqual(self.native.hub_restore_decision(1, False), "start_heart_rate")
        self.assertEqual(self.native.hub_restore_decision(1, True), "shutdown")
        disposition = self.native.hub_recovery_disposition
        self.assertEqual(disposition(True, True, False), "retry")
        self.assertEqual(disposition(False, True, False), "terminal")
        self.assertEqual(disposition(True, False, False), "terminal")
        self.assertEqual(disposition(True, True, True), "shutdown")


