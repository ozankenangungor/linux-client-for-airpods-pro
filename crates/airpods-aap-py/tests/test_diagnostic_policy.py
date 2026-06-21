"""Direct synthetic contract for the compiled diagnostic policy bridge."""

import unittest

from airpods_hr import _airpods_aap_core as native


HISTORICAL = bytes.fromhex(
    "01 02 16 0a 02 02 1e 00 04 09 00 00 00 00 00 00 00 00 00"
)
RAW = bytes.fromhex("01 48 a5 34 12 5a 08 07 06 05 04 03 02 01 ef cd ab 89")
FIELDS = [72, 165, 4660, 90, 0x0102030405060708, 0x89ABCDEF]


class DiagnosticPolicyBridgeTests(unittest.TestCase):
    def test_compiled_entrypoints_and_config_policy(self):
        self.assertEqual(native._LocalRXState.__module__, native.__name__)
        self.assertEqual(native.diagnostic_config_plan(True, HISTORICAL, 0),
                         (False, b"\x01\x02\x16\x0a"))
        self.assertEqual(native.diagnostic_config_plan(True, HISTORICAL, 1), (True, None))
        self.assertEqual(native.diagnostic_config_plan(True, HISTORICAL + b"\x03", 0),
                         (True, None))
        self.assertEqual(native.diagnostic_config_rewrite(True, 7, 7, True, b"\x01\x02\x16\x0a"),
                         (True, b"\x01\x02\x16\x0a"))
        self.assertEqual(native.diagnostic_config_rewrite(True, 7, 8, True, b"x"), (False, None))
        self.assertEqual(native.diagnostic_config_rewrite(True, 7, 7, False, b"x"), (True, None))
        facts = native.diagnostic_config_observation("kernel-mtu-only", HISTORICAL, 0, b"\x01")
        self.assertEqual(facts["peer_option_types"], b"\x01\x02\x04")
        self.assertEqual(facts["peer_mtu"], 2582)
        self.assertEqual(facts["response_option_types"], b"")

    def test_local_rx_errors_state_and_post_ack(self):
        with self.assertRaisesRegex(ValueError, "host Configure Request options are malformed"):
            native.diagnostic_local_rx_parse(b"\x01")
        with self.assertRaisesRegex(ValueError, "Configure MTU option is malformed"):
            native.diagnostic_local_rx_parse(b"\x01\x01\x02")
        self.assertEqual(native.diagnostic_local_rx_rewrite(True, b"\x01\x02\x00\x08\x04\x00"),
                         (b"\x04\x00", b"\x04", None, 2048))
        state = native._LocalRXState(True)
        self.assertFalse(state.snapshot()["request_observed"])
        state.observe_request(True, b"\x01\x02\x00\x08", 0, 7, 64, 2048)
        self.assertTrue(state.response_matches(7, 3, 3))
        self.assertFalse(state.response_matches(8, 3, 3))
        state.observe_response(0, b"\x01\x02\xa0\x02")
        self.assertEqual(state.snapshot()["peer_response_mtu"], 672)
        self.assertEqual(native.diagnostic_post_ack_shape([(0x2b, 5), (0x17, 7), (0x2b, 8)]),
                         (5, [7], 8, 3))


