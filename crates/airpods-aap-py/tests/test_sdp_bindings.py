"""Direct neutral-value checks for the private SDP PyO3 bridge."""

import inspect
import unittest

from airpods_hr import _airpods_aap_core as native


class SDPBindingsTests(unittest.TestCase):
    def test_native_entry_points_and_modalias_error_mapping(self) -> None:
        for name in (
            "sdp_parse_bluez_modalias", "sdp_canonical_records",
            "sdp_bluez_xml_records", "sdp_expected_attributes",
            "sdp_compare_attribute", "sdp_full_record_equivalence",
            "sdp_extra_service_specs", "sdp_allocate_handles",
            "sdp_query_decision", "sdp_classify_service_uuid_bytes",
            "sdp_query_uuid16s", "sdp_query_attribute_ranges",
            "sdp_psm_category",
        ):
            self.assertTrue(inspect.isbuiltin(getattr(native, name)), name)
        self.assertEqual(native.sdp_parse_bluez_modalias("usb:v1234p5678d9AbC"),
                         (0x1234, 0x5678, 0x9ABC))
        with self.assertRaisesRegex(ValueError, "has no Modalias"):
            native.sdp_parse_bluez_modalias(None)
        with self.assertRaisesRegex(ValueError, "not a supported USB identity"):
            native.sdp_parse_bluez_modalias("USB:v1234p5678d9abc")

    def test_records_xml_audit_and_reference_plan_are_native_facts(self) -> None:
        records = native.sdp_canonical_records(0x1234, 0x5678, 0x9ABC)
        self.assertEqual([record["handle"] for record in records],
                         [0x10001, 0x10002, 0x10003, 0x10004])
        self.assertEqual(records[0]["attributes"][2][1]["value"], 0x1234)
        xml = native.sdp_bluez_xml_records(0x1234, 0x5678, 0x9ABC)
        self.assertIn('value="0x9abc"', xml[0][2])
        self.assertEqual(len(native.sdp_extra_service_specs()), 20)
        self.assertEqual(native.sdp_allocate_handles(["65537", "65540"], 2),
                         ["65541", "65542"])
        self.assertEqual(native.sdp_expected_attributes(None)[0][0],
                         ("pnp_vendor_id", None))
        self.assertEqual(native.sdp_compare_attribute(13, True, 12), "mismatch")
        self.assertEqual(native.sdp_full_record_equivalence(
            ["match", "unknown/not-observable"], [True]),
            "unknown/not-observable")

    def test_query_diagnostic_and_timeline_state(self) -> None:
        self.assertEqual(native.sdp_query_uuid16s([b"\x00\x01", b"\x01"]), [0x0100])
        self.assertEqual(native.sdp_query_attribute_ranges([(0x0000FFFF, 4)]),
                         [(0, 0xFFFF)])
        self.assertEqual(native.sdp_query_decision(
            [0x0100], [(0, 0xFFFF)], 100, 50, 42, 2, 8, True),
            (True, True, False, True))
        self.assertEqual(native.sdp_classify_service_uuid_bytes([b"\x00\x12"]), ["pnp"])
        self.assertEqual(native.sdp_psm_category(23), ("23", "psm_23_request"))
        state = native.SDPDiagnosticState()
        state.observe_request()
        state.account_query(["pnp"])
        state.mark_served("pnp")
        self.assertEqual(state.observe_psm(23), "psm_23_request")
        self.assertEqual(state.snapshot()[2], [1, 0, 0, 0, 0])
        timeline = native.SDPTimelineState(1)
        timeline.record("ack_observed", -2.0)
        timeline.record("handshake_sent", 4.0)
        self.assertEqual(timeline.snapshot(), ([("ack_observed", 0.0)], 2))


if __name__ == "__main__":
    unittest.main()
