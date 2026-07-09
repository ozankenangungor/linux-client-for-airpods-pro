"""Direct checks of the compiled private runtime policy bindings."""

import importlib.util
import types
import unittest
from pathlib import Path


LIBRARY = Path(__file__).resolve().parents[3] / "target/debug/lib_airpods_aap_core.so"


class RuntimePolicyBindingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("_airpods_aap_core", LIBRARY)
        assert spec is not None and spec.loader is not None
        cls.native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.native)

    def test_handshake_accumulator_is_native_and_bounded(self):
        state = self.native.HandshakeAccumulator(1)
        self.assertEqual(type(state).__module__, "airpods_hr._airpods_aap_core")
        self.assertEqual(state.snapshot(), (False, (False, False, False, False), 0, 0, 0, [], []))
        self.assertEqual(state.observe(b"AccessoryService", 2), (False, False, False))
        self.assertFalse(state.descriptors_complete)
        ack = bytes.fromhex("01 00 04 00 00 00 01 00 03 00 00 00 00 00 00 00 00 00")
        self.assertEqual(state.observe(ack[:-1], 3), (False, False, False))
        self.assertEqual(state.observe(ack, 4), (True, False, False))
        self.assertTrue(state.ack_observed)
        self.assertEqual(state.observe(b"HeartRateService", 5), (False, True, False))
        self.assertTrue(state.descriptors_complete)
        self.assertEqual(state.observe(bytes(357), 6), (False, False, True))
        self.assertEqual(state.observe(bytes(357), 7), (False, False, False))
        snapshot = state.snapshot()
        self.assertEqual(snapshot[2:5], (2, 3, 7))
        self.assertEqual((len(snapshot[5]), len(snapshot[6])), (1, 0))
        with self.assertRaises(ValueError):
            self.native.HandshakeAccumulator(0)

    def test_transport_channel_and_timeout_bindings(self):
        self.assertIsInstance(self.native.runtime_channel_facts, types.BuiltinFunctionType)
        self.assertEqual(self.native.runtime_channel_facts(False, False, 0, 0, 0, 0), 1)
        self.assertEqual(self.native.runtime_channel_facts(True, False, 0, 0, 0, 0), 2)
        self.assertEqual(self.native.runtime_channel_facts(True, True, 1, 0, 0, 0), 3)
        self.assertEqual(self.native.runtime_channel_facts(True, True, 0, 0, 1, 0), 4)
        self.assertEqual(self.native.runtime_channel_facts(True, True, 0, 1, 65535, 0), 0)
        self.assertTrue(self.native.runtime_positive_timeouts([0.1, 1]))
        self.assertFalse(self.native.runtime_positive_timeouts([1, 0]))
        self.assertFalse(self.native.runtime_positive_timeouts([float("nan")]))
        self.assertEqual(self.native.runtime_transport_legality(False, 0, True), 1)
        self.assertEqual(self.native.runtime_transport_legality(True, 1, True), 2)
        self.assertEqual(self.native.runtime_transport_legality(True, 0, False), 3)
        self.assertEqual(self.native.runtime_transport_legality(True, 1, False), 0)
        policy = self.native.TransportPolicy()
        self.assertFalse(policy.collection_active)
        self.assertEqual(policy.send_legality(True), 1)
        self.assertTrue(policy.begin_collection())
        self.assertFalse(policy.begin_collection())
        self.assertEqual(policy.send_legality(False), 3)
        self.assertEqual(policy.sent(True), 1)
        self.assertEqual(policy.sent(False), 2)
        self.assertEqual(policy.application_payloads_sent, 2)
        policy.end_collection()
        self.assertFalse(policy.collection_active)
        self.assertTrue(self.native.runtime_expected_payload_count(1))
        self.assertFalse(self.native.runtime_expected_payload_count(2))

    def test_discovery_unicode_boundaries_and_adapter_digits(self):
        cases = {
            "AirPods": True, "(airpods)": True, "my_airpods": True,
            "xairpods": False, "airpods2": False, "airpodsİ": False,
            "airpodſ": True, "中airpods中": True, "": False,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(self.native.runtime_supported_airpods_name(value, None), expected)
        self.assertEqual(self.native.runtime_display_name("", "Name"), "Name")
        self.assertEqual(self.native.runtime_display_name(None, None), "AirPods")
        self.assertEqual(self.native.runtime_adapter_digits("hci0001"), "0001")
        for bad in ("hci", "HCI0", " hci0", "hci+1", "hci١"):
            self.assertIsNone(self.native.runtime_adapter_digits(bad))
        self.assertEqual([self.native.runtime_candidate_count(n) for n in (0, 1, 2, 100)], [0, 1, 2, 2])

    def test_authentication_lifecycle_and_cleanup(self):
        state = self.native.AuthenticationLifecycle()
        self.assertEqual(state.state, 0)
        with self.assertRaises(ValueError):
            state.advance(1)
        for event, expected in enumerate((1, 2, 3, 4, 5)):
            self.assertEqual(state.advance(event), expected)
        self.assertFalse(state.replacement_key_reported)
        state.report_replacement_key()
        self.assertTrue(state.replacement_key_reported)
        self.assertEqual(state.advance(5), 6)
        self.assertEqual(state.advance(6), 7)
        with self.assertRaises(ValueError):
            state.advance(5)
        self.assertEqual(self.native.runtime_authentication_observation(False), 1)
        self.assertEqual(self.native.runtime_encryption_observation(False), 2)
        self.assertEqual(
            [self.native.runtime_disconnect_cleanup(*facts) for facts in
             ((False, False, False), (True, True, True), (False, True, True), (False, True, False))],
            [0, 1, 2, 3],
        )

    def test_controller_and_reopen_bindings(self):
        self.assertEqual(self.native.runtime_poll_delay(0.1, 2.0, 3.0), 0.0)
        self.assertEqual(self.native.runtime_restoration(False, True), 0)
        self.assertEqual(self.native.runtime_restoration(True, True), 1)
        self.assertEqual(self.native.runtime_restoration(True, False), 2)
        for mask in range(8):
            self.assertEqual(
                self.native.runtime_reopen_checkpoint_holds(
                    bool(mask & 1), bool(mask & 2), bool(mask & 4)
                ), mask == 7
            )
        counts = self.native.ReopenObservationCounters()
        counts.handshake_attempt()
        counts.handshake_complete()
        counts.transport_open()
        counts.transport_close()
        self.assertEqual(
            (counts.attempts, counts.completed, counts.open_calls, counts.close_calls),
            (1, 1, 1, 1),
        )
        self.assertEqual(
            self.native.runtime_aggregate_reopen_counts([[1, 1, 1, 1, 1, 2, 2, 5]]),
            [1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 5, 0],
        )
        self.assertEqual(self.native.runtime_reopen_failure_category(True, True, True, True, True), 1)
        self.assertEqual(self.native.runtime_reopen_failure_category(False, True, None, True, True), 2)
        self.assertEqual(self.native.runtime_reopen_failure_category(False, False, None, True, True), 4)
        self.assertEqual(self.native.runtime_reopen_failure_category(False, False, None, False, True), 3)
        self.assertEqual(self.native.runtime_reopen_failure_category(False, False, None, False, False), 5)
        self.assertTrue(self.native.runtime_session1_activate_hr("hr-cycle"))
        self.assertFalse(self.native.runtime_session1_activate_hr("descriptor-only"))
        with self.assertRaises(ValueError):
            self.native.runtime_session1_activate_hr("HR-CYCLE")


if __name__ == "__main__":
    unittest.main()
