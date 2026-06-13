"""Direct private-extension contract for synthetic reference diagnostic data."""

import unittest

from airpods_hr import _airpods_aap_core as native


class ReferenceDiagnosticBridgeTests(unittest.TestCase):


    def test_unknown_identities_fail_without_mutation(self):
        with self.assertRaises(ValueError):
            native._PreAuthDiagnosticState("unknown")
        with self.assertRaises(ValueError):
            native._PreAAPDiagnosticState("unknown")
        auth = native._PreAuthDiagnosticState("proven")
        aap = native._PreAAPDiagnosticState("proven")
        before_auth = auth.snapshot()
        before_aap = aap.snapshot()
        with self.assertRaises(ValueError):
            auth.supported_response(True, "unknown", 1)
        with self.assertRaisesRegex(ValueError, "unsupported diagnostic Information Type"):
            aap.request_sent(0x10000)
        with self.assertRaisesRegex(ValueError, "unsupported diagnostic Information Type"):
            aap.response(-1, True, "success", 0)
        with self.assertRaises(ValueError):
            aap.response(2, True, "unknown", 0)
        self.assertEqual(auth.snapshot(), before_auth)
        self.assertEqual(aap.snapshot(), before_aap)

    def test_response_validation_is_native(self):
        self.assertEqual(native.pre_auth_supported_completion(0, 0, 0), ("success", 0))
        self.assertEqual(native.pre_auth_supported_completion(0, 0, 1 << 64), ("other", None))
        self.assertEqual(native.pre_auth_supported_completion(0, 1, 4), ("other", None))
        self.assertEqual(native.pre_auth_extended_completion(0, 1, 255, (1 << 64) - 1), ("success", (1 << 64) - 1, 255))
        self.assertEqual(native.pre_auth_extended_completion(0, 1, 256, 4), ("other", None, None))
        self.assertTrue(native.pre_auth_command_accepted(0))
        self.assertFalse(native.pre_auth_command_accepted(1))
        self.assertEqual(native.pre_aap_decode_response(2, 0, b"\x78\x56\x34\x12"), ("success", 0x12345678))
        self.assertEqual(native.pre_aap_decode_response(3, 0, b"\x08\x07\x06\x05\x04\x03\x02\x01"), ("success", 0x0102030405060708))
        self.assertEqual(native.pre_aap_decode_response(2, 1, b"private"), ("not_supported", None))
        self.assertEqual(native.pre_aap_decode_response(2, 0, b"private"), ("other", None))
        self.assertEqual(native.pre_aap_decode_response(2, -1, b""), ("other", None))
        with self.assertRaisesRegex(ValueError, "unsupported diagnostic Information Type"):
            native.pre_aap_decode_response(4, 0, b"\0" * 8)


