"""Hardware-independent tests for the minimal AAP handshake layer."""

from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager, contextmanager
from dataclasses import fields
from types import SimpleNamespace
from bumble import l2cap
from airpods_hr.aap import AAP_HANDSHAKE_ACK, AAP_HANDSHAKE_REQUEST, AAPDescriptorObservationTimeoutError, AAPFrameSummary, AAPHandshakeError, AAPHandshakeProbeSession, AAPHandshakeSession, AAPHandshakeTimeoutError, AAPReceiveStateError, BumbleAAPTransport, DescriptorEvidence, HandshakeObservation
from airpods_hr.aap_channel import AAPChannelSession
from airpods_hr.aap_channel import AAPChannelOpenError
from airpods_hr.authentication import AuthenticatedClassicContext
from airpods_hr.protocol import AAP_PSM
from airpods_hr.sdp_diagnostics import ProtocolTimelineKind, SafeProtocolTimeline


ALL_DESCRIPTOR_EVIDENCE = (
    b"\x00AccessoryService\x00HeartRateService\x00HeartRate\x00"
    b"com.apple.hid.heartrate-access\x00"
)



def make_synthetic_type_2b_frame(
    suffixes: list[tuple[int, int]],
    *,
    declared_body_length: int | None = None,
    trailing_bytes: bytes = b"",
    hidden_fields: list[bytes] | None = None,
) -> bytes:
    """Build a sanitized structural fixture without historical frame content."""

    if hidden_fields is None:
        hidden_fields = [b"SAFE!!"] * len(suffixes)
    if len(hidden_fields) != len(suffixes):
        raise ValueError("one hidden-field fixture is required per unit")

    body = bytearray()
    for index, ((suffix_u8, suffix_u16), hidden_field) in enumerate(
        zip(suffixes, hidden_fields, strict=True)
    ):
        if len(hidden_field) != 6:
            raise ValueError("hidden-field fixtures must contain six bytes")
        unit = bytearray(17)
        for byte_index in range(14):
            unit[byte_index] = (index * 19 + byte_index * 7 + 3) & 0xFF
        unit[8:14] = hidden_field
        unit[14] = suffix_u8
        unit[15] = suffix_u16 & 0xFF
        unit[16] = suffix_u16 >> 8
        body.extend(unit)
    body.extend(trailing_bytes)

    declared_length = (
        len(body) if declared_body_length is None else declared_body_length
    )
    header = bytearray(17)
    header[2:4] = (4).to_bytes(2, "little")
    header[4:6] = (0x002B).to_bytes(2, "little")
    header[6] = 0x05
    header[7:9] = declared_length.to_bytes(2, "little")
    return bytes(header + body)



class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now



class FakeReceiveTransport:
    def __init__(self, frames: list[bytes], clock: FakeClock) -> None:
        self.frames = list(frames)
        self.clock = clock
        self.sent: list[bytes] = []
        self.collect_active = False
        self.dropped_frames = 0

    @property
    def application_payloads_sent(self) -> int:
        return len(self.sent)

    @asynccontextmanager
    async def collect(self):
        self.collect_active = True
        try:
            yield self
        finally:
            self.collect_active = False

    def send_handshake_request(self) -> None:
        if self.sent:
            raise AAPHandshakeError("already sent")
        self.sent.append(AAP_HANDSHAKE_REQUEST)

    async def receive(self, timeout: float) -> bytes:
        if self.frames:
            return self.frames.pop(0)
        self.clock.now += timeout
        raise TimeoutError



class FakeRawChannel:
    def __init__(self) -> None:
        self.sink = None
        self.writes: list[bytes] = []
        self.mtu = 2048
        self.peer_mtu = 2750
        self.mode = l2cap.TransmissionMode.BASIC
        self.state = l2cap.ClassicChannel.State.OPEN
        self.psm = AAP_PSM
        self.events: list[str] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    async def disconnect(self) -> None:
        self.events.append("channel_close")



class HandshakeProtocolTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, frames: list[bytes]):
        clock = FakeClock()
        transport = FakeReceiveTransport(frames, clock)
        session = AAPHandshakeSession(
            ack_timeout=4,
            descriptor_timeout=3,
            clock=clock,
        )
        return session, transport, clock

    def test_exact_handshake_request_bytes(self) -> None:
        self.assertEqual(
            AAP_HANDSHAKE_REQUEST,
            bytes.fromhex("00 00 04 00 01 00 02 00 00 00 00 00 00 00 00 00"),
        )

    async def test_exact_known_ack_is_accepted(self) -> None:
        session, transport, _ = self.make_session(
            [AAP_HANDSHAKE_ACK, ALL_DESCRIPTOR_EVIDENCE]
        )
        result = await session.run(transport)
        self.assertTrue(result.evidence.required)
        self.assertEqual(result.application_payloads_sent, 1)
        self.assertEqual(result.observation.pre_ack_frame_count, 0)
        self.assertEqual(result.observation.post_ack_frame_count, 1)

    async def test_all_required_descriptor_evidence_before_ack_succeeds(self) -> None:
        session, transport, _ = self.make_session(
            [ALL_DESCRIPTOR_EVIDENCE, AAP_HANDSHAKE_ACK]
        )

        result = await session.run(transport)

        self.assertTrue(result.evidence.required)
        self.assertEqual(result.observation.pre_ack_frame_count, 1)
        self.assertEqual(result.observation.post_ack_frame_count, 0)

    async def test_evidence_split_across_pre_ack_frames_is_retained(self) -> None:
        session, transport, _ = self.make_session(
            [
                b"\x00AccessoryService\x00",
                b"\x00HeartRateService\x00",
                AAP_HANDSHAKE_ACK,
            ]
        )

        result = await session.run(transport)

        self.assertTrue(result.evidence.required)
        self.assertEqual(result.observation.pre_ack_frame_count, 2)
        self.assertEqual(result.observation.post_ack_frame_count, 0)

    async def test_evidence_split_before_and_after_ack_is_merged(self) -> None:
        session, transport, _ = self.make_session(
            [
                b"\x00ReportDescriptor\x00",
                AAP_HANDSHAKE_ACK,
                b"\x00HeartRateService\x00",
            ]
        )

        result = await session.run(transport)

        self.assertTrue(result.evidence.required)
        self.assertEqual(result.observation.pre_ack_frame_count, 1)
        self.assertEqual(result.observation.post_ack_frame_count, 1)

    async def test_similar_malformed_ack_is_rejected(self) -> None:
        malformed = AAP_HANDSHAKE_ACK[:-1] + b"\x01"
        session, transport, _ = self.make_session([malformed])
        with self.assertRaises(AAPHandshakeTimeoutError):
            await session.run(transport)

    async def test_handshake_timeout_uses_one_bounded_deadline(self) -> None:
        session, transport, clock = self.make_session([])
        with self.assertRaises(AAPHandshakeTimeoutError):
            await session.run(transport)
        self.assertEqual(clock.now, 4)

    async def test_missing_descriptor_is_distinct_from_missing_ack(self) -> None:
        session, transport, clock = self.make_session([AAP_HANDSHAKE_ACK])
        with self.assertRaises(AAPDescriptorObservationTimeoutError) as caught:
            await session.run(transport)
        self.assertFalse(caught.exception.evidence.required)
        self.assertEqual(caught.exception.observation.pre_ack_frame_count, 0)
        self.assertEqual(caught.exception.observation.post_ack_frame_count, 0)
        self.assertEqual(clock.now, 3)

    async def test_unexpected_frames_do_not_crash_ack_wait(self) -> None:
        session, transport, _ = self.make_session(
            [b"unrelated", AAP_HANDSHAKE_ACK, ALL_DESCRIPTOR_EVIDENCE]
        )
        result = await session.run(transport)
        self.assertTrue(result.evidence.required)
        self.assertEqual(result.observation.pre_ack_frame_count, 1)

    async def test_ack_like_marker_frame_is_evidence_but_not_ack(self) -> None:
        ack_like = (
            AAP_HANDSHAKE_ACK
            + b"\x00AccessoryService\x00HeartRateService\x00"
        )
        session, transport, _ = self.make_session(
            [ack_like, AAP_HANDSHAKE_ACK]
        )

        result = await session.run(transport)

        self.assertTrue(result.evidence.required)
        self.assertEqual(result.observation.pre_ack_frame_count, 1)

    async def test_descriptor_evidence_without_exact_ack_still_times_out(self) -> None:
        session, transport, _ = self.make_session([ALL_DESCRIPTOR_EVIDENCE])

        with self.assertRaises(AAPHandshakeTimeoutError):
            await session.run(transport)

    async def test_pre_ack_evidence_survives_post_ack_descriptor_timeout(self) -> None:
        session, transport, _ = self.make_session(
            [b"\x00AccessoryService\x00", AAP_HANDSHAKE_ACK, b"unrelated"]
        )
        transport.dropped_frames = 2

        with self.assertRaises(AAPDescriptorObservationTimeoutError) as caught:
            await session.run(transport)

        observation = caught.exception.observation
        self.assertTrue(observation.evidence.sensor_framework)
        self.assertFalse(observation.evidence.heart_rate_service)
        self.assertEqual(observation.pre_ack_frame_count, 1)
        self.assertEqual(observation.post_ack_frame_count, 1)
        self.assertEqual(observation.receive_frames_dropped, 2)

    async def test_only_handshake_payload_is_sent(self) -> None:
        session, transport, _ = self.make_session(
            [AAP_HANDSHAKE_ACK, ALL_DESCRIPTOR_EVIDENCE]
        )
        await session.run(transport)
        self.assertEqual(transport.sent, [AAP_HANDSHAKE_REQUEST])
        self.assertNotIn(0x44, transport.sent[0])

    async def test_frame_summary_history_is_bounded_across_both_phases(self) -> None:
        clock = FakeClock()
        transport = FakeReceiveTransport(
            [
                b"pre-one",
                b"pre-two",
                AAP_HANDSHAKE_ACK,
                b"post-one",
                b"post-two",
                ALL_DESCRIPTOR_EVIDENCE,
            ],
            clock,
        )
        session = AAPHandshakeSession(
            ack_timeout=4,
            descriptor_timeout=3,
            frame_summary_limit=3,
            clock=clock,
        )

        result = await session.run(transport)

        observation = result.observation
        self.assertEqual(observation.pre_ack_frame_count, 2)
        self.assertEqual(observation.post_ack_frame_count, 3)
        self.assertEqual(len(observation.pre_ack_frame_summaries), 2)
        self.assertEqual(len(observation.post_ack_frame_summaries), 1)

    async def test_timeline_orders_handshake_ack_and_first_357_byte_frame(self) -> None:
        clock = FakeClock()
        timeline = SafeProtocolTimeline(clock=clock)
        transport = FakeReceiveTransport(
            [AAP_HANDSHAKE_ACK, bytes(357), ALL_DESCRIPTOR_EVIDENCE], clock
        )
        session = AAPHandshakeSession(clock=clock, timeline=timeline)

        result = await session.run(transport)

        self.assertEqual(result.application_payloads_sent, 1)
        self.assertEqual(
            [event.kind for event in timeline.snapshot().events],
            [
                ProtocolTimelineKind.HANDSHAKE_SENT,
                ProtocolTimelineKind.ACK_OBSERVED,
                ProtocolTimelineKind.FIRST_POST_ACK_FRAME,
                ProtocolTimelineKind.FIRST_357_BYTE_FRAME,
            ],
        )

    async def test_first_post_ack_timeline_records_an_85_byte_frame(self) -> None:
        clock = FakeClock()
        timeline = SafeProtocolTimeline(clock=clock)
        transport = FakeReceiveTransport(
            [AAP_HANDSHAKE_ACK, bytes(85), ALL_DESCRIPTOR_EVIDENCE], clock
        )

        await AAPHandshakeSession(clock=clock, timeline=timeline).run(transport)

        snapshot = timeline.snapshot()
        self.assertIsNotNone(
            snapshot.first(ProtocolTimelineKind.FIRST_POST_ACK_FRAME)
        )
        self.assertIsNone(
            snapshot.first(ProtocolTimelineKind.FIRST_357_BYTE_FRAME)
        )



class DescriptorEvidenceTests(unittest.TestCase):
    def test_heart_rate_service_evidence(self) -> None:
        evidence = DescriptorEvidence().merged(b"xHeartRateService\x00")
        self.assertTrue(evidence.heart_rate_service)
        self.assertFalse(evidence.heart_rate)

    def test_standalone_heart_rate_evidence(self) -> None:
        evidence = DescriptorEvidence().merged(b"\x09HeartRate\x00")
        self.assertTrue(evidence.heart_rate)
        self.assertFalse(evidence.heart_rate_service)

    def test_heartrate_access_evidence(self) -> None:
        evidence = DescriptorEvidence().merged(
            b"\x00com.apple.hid.heartrate-access\x00"
        )
        self.assertTrue(evidence.heartrate_access)

    def test_sensor_framework_evidence_is_conservative(self) -> None:
        self.assertTrue(
            DescriptorEvidence().merged(b"\x00ReportDescriptor\x00").sensor_framework
        )
        self.assertFalse(
            DescriptorEvidence().merged(b"unrelated private device text").sensor_framework
        )

    def test_result_repr_contains_only_booleans(self) -> None:
        evidence = DescriptorEvidence().merged(ALL_DESCRIPTOR_EVIDENCE)
        rendered = repr(evidence)
        self.assertNotIn("AccessoryService", rendered)
        self.assertNotIn("com.apple", rendered)



class AAPFrameSummaryTests(unittest.TestCase):
    def test_summary_exposes_only_length_and_neutral_header_fields(self) -> None:
        synthetic_payload = bytes.fromhex("04 00 04 00 2B 00") + bytes(351)

        summary = AAPFrameSummary.from_frame(synthetic_payload)

        self.assertEqual(summary.length, 357)
        self.assertEqual(summary.header_u16_2_3, 0x0004)
        self.assertEqual(summary.header_u16_4_5, 0x002B)
        self.assertNotIn(synthetic_payload.hex(), repr(summary))

    def test_short_frame_exposes_length_only(self) -> None:
        summary = AAPFrameSummary.from_frame(b"\xAA\xBB\xCC")

        self.assertEqual(summary.length, 3)
        self.assertIsNone(summary.header_u16_2_3)
        self.assertIsNone(summary.header_u16_4_5)

    def test_partially_available_header_exposes_only_complete_field(self) -> None:
        summary = AAPFrameSummary.from_frame(bytes.fromhex("AA BB 34 12 CC"))

        self.assertEqual(summary.header_u16_2_3, 0x1234)
        self.assertIsNone(summary.header_u16_4_5)



class AAPType2BFrameSummaryTests(unittest.TestCase):
    def test_non_type_2b_frame_has_no_structural_summary(self) -> None:
        frame = bytearray(20)
        frame[4:6] = (0x002A).to_bytes(2, "little")

        self.assertIsNone(AAPFrameSummary.from_frame(bytes(frame)).type_2b_summary)

    def test_short_type_2b_frame_is_safe(self) -> None:
        summary = AAPFrameSummary.from_frame(
            bytes.fromhex("00 00 04 00 2B 00")
        ).type_2b_summary

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.frame_length, 6)
        self.assertIsNone(summary.header_u8_6)
        self.assertIsNone(summary.declared_body_length_u16_7_8)
        self.assertIsNone(summary.actual_body_length_after_offset_17)
        self.assertIsNone(summary.record_count_17)
        self.assertEqual(summary.record_suffix_histogram, ())

    def test_synthetic_357_byte_frame_has_twenty_aligned_units(self) -> None:
        frame = make_synthetic_type_2b_frame([(0x21, 0x3100)] * 20)

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.frame_length, 357)
        self.assertEqual(summary.declared_body_length_u16_7_8, 340)
        self.assertEqual(summary.actual_body_length_after_offset_17, 340)
        self.assertTrue(summary.declared_body_length_consistent)
        self.assertTrue(summary.body_aligned_to_17_bytes)
        self.assertEqual(summary.record_count_17, 20)

    def test_synthetic_51_byte_frame_has_two_aligned_units(self) -> None:
        frame = make_synthetic_type_2b_frame(
            [(0x10, 0x1100), (0x20, 0x2200)]
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.frame_length, 51)
        self.assertEqual(summary.declared_body_length_u16_7_8, 34)
        self.assertEqual(summary.actual_body_length_after_offset_17, 34)
        self.assertTrue(summary.declared_body_length_consistent)
        self.assertTrue(summary.body_aligned_to_17_bytes)
        self.assertEqual(summary.record_count_17, 2)

    def test_declared_length_smaller_than_actual_is_inconsistent(self) -> None:
        frame = make_synthetic_type_2b_frame(
            [(0x10, 0x1000)], declared_body_length=16
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        assert summary is not None
        self.assertFalse(summary.declared_body_length_consistent)
        self.assertIsNone(summary.record_count_17)
        self.assertEqual(summary.record_suffix_histogram, ())

    def test_declared_length_larger_than_actual_is_inconsistent(self) -> None:
        frame = make_synthetic_type_2b_frame(
            [(0x10, 0x1000)], declared_body_length=18
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        assert summary is not None
        self.assertFalse(summary.declared_body_length_consistent)
        self.assertIsNone(summary.record_count_17)

    def test_unaligned_body_has_no_record_count(self) -> None:
        frame = make_synthetic_type_2b_frame(
            [(0x10, 0x1000)], trailing_bytes=b"x"
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        assert summary is not None
        self.assertTrue(summary.declared_body_length_consistent)
        self.assertFalse(summary.body_aligned_to_17_bytes)
        self.assertIsNone(summary.record_count_17)
        self.assertIsNone(summary.record_suffix_distinct_count)

    def test_suffix_histogram_counts_and_sorts_pairs(self) -> None:
        frame = make_synthetic_type_2b_frame(
            [(0x20, 0x3000), (0x10, 0x2000), (0x20, 0x3000)]
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        assert summary is not None
        self.assertEqual(summary.record_suffix_distinct_count, 2)
        self.assertEqual(
            [
                (item.suffix_field_u8, item.suffix_field_u16, item.count)
                for item in summary.record_suffix_histogram
            ],
            [(0x10, 0x2000, 1), (0x20, 0x3000, 2)],
        )

    def test_suffix_histogram_is_deterministically_bounded(self) -> None:
        suffixes = [(value, 0x2000 + value) for value in range(10, -1, -1)]

        summary = AAPFrameSummary.from_frame(
            make_synthetic_type_2b_frame(suffixes)
        ).type_2b_summary

        assert summary is not None
        self.assertEqual(summary.record_suffix_distinct_count, 11)
        self.assertEqual(len(summary.record_suffix_histogram), 8)
        self.assertEqual(
            [item.suffix_field_u8 for item in summary.record_suffix_histogram],
            list(range(8)),
        )

    def test_summary_dataclasses_retain_no_record_prefix_bytes(self) -> None:
        hidden_marker = b"QZ9mK!"
        frame = make_synthetic_type_2b_frame(
            [(0x10, 0x2000)], hidden_fields=[hidden_marker]
        )

        summary = AAPFrameSummary.from_frame(frame).type_2b_summary

        assert summary is not None
        allowed_types = (int, bool, type(None), tuple)
        self.assertTrue(
            all(
                isinstance(getattr(summary, field.name), allowed_types)
                for field in fields(summary)
            )
        )
        self.assertNotIn(hidden_marker.decode(), repr(summary))
        self.assertNotIn(hidden_marker.hex(), repr(summary).lower())
        self.assertNotIn(frame.hex(), repr(summary).lower())

    def test_hidden_unit_field_reports_only_uniformity(self) -> None:
        uniform = AAPFrameSummary.from_frame(
            make_synthetic_type_2b_frame(
                [(0x10, 0x2000), (0x11, 0x2001)],
                hidden_fields=[b"SAFE!!", b"SAFE!!"],
            )
        ).type_2b_summary
        differing = AAPFrameSummary.from_frame(
            make_synthetic_type_2b_frame(
                [(0x10, 0x2000), (0x11, 0x2001)],
                hidden_fields=[b"SAFE!!", b"OTHER!"],
            )
        ).type_2b_summary

        assert uniform is not None and differing is not None
        self.assertTrue(uniform.unit_bytes_8_13_uniform)
        self.assertFalse(differing.unit_bytes_8_13_uniform)



class BumbleAAPTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_receive_queue_is_bounded_and_drops_oldest(self) -> None:
        raw = FakeRawChannel()
        transport = BumbleAAPTransport(raw, SimpleNamespace(), queue_limit=2)
        async with transport.collect():
            raw.sink(b"one")
            raw.sink(b"two")
            raw.sink(b"three")
            self.assertEqual(transport.queued_frames, 2)
            self.assertEqual(transport.dropped_frames, 1)
            self.assertEqual(await transport.receive(1), b"two")
            self.assertEqual(await transport.receive(1), b"three")

    async def test_oversized_frame_is_dropped(self) -> None:
        raw = FakeRawChannel()
        transport = BumbleAAPTransport(
            raw, SimpleNamespace(), queue_limit=2, frame_size_limit=3
        )
        async with transport.collect():
            raw.sink(b"four")
            self.assertEqual(transport.queued_frames, 0)
            self.assertEqual(transport.dropped_frames, 1)

    async def test_previous_sink_is_restored_on_normal_exit(self) -> None:
        previous = lambda frame: None
        raw = FakeRawChannel()
        raw.sink = previous
        transport = BumbleAAPTransport(raw, SimpleNamespace())
        async with transport.collect():
            self.assertIsNot(raw.sink, previous)
        self.assertIs(raw.sink, previous)

    async def test_previous_sink_is_restored_on_failure(self) -> None:
        previous = lambda frame: None
        raw = FakeRawChannel()
        raw.sink = previous
        transport = BumbleAAPTransport(raw, SimpleNamespace())
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            async with transport.collect():
                raise RuntimeError("synthetic")
        self.assertIs(raw.sink, previous)

    async def test_previous_sink_is_restored_on_cancellation(self) -> None:
        previous = lambda frame: None
        raw = FakeRawChannel()
        raw.sink = previous
        transport = BumbleAAPTransport(raw, SimpleNamespace())
        with self.assertRaises(asyncio.CancelledError):
            async with transport.collect():
                raise asyncio.CancelledError
        self.assertIs(raw.sink, previous)

    async def test_send_is_deliberately_limited_to_one_handshake(self) -> None:
        raw = FakeRawChannel()
        transport = BumbleAAPTransport(raw, SimpleNamespace())
        async with transport.collect():
            transport.send_handshake_request()
            with self.assertRaises(AAPHandshakeError):
                transport.send_handshake_request()
        self.assertEqual(raw.writes, [AAP_HANDSHAKE_REQUEST])
        self.assertFalse(hasattr(transport, "write"))
        self.assertFalse(hasattr(transport, "send"))
        self.assertFalse(hasattr(transport, "send_pdu"))

    async def test_receive_outside_collection_is_rejected(self) -> None:
        transport = BumbleAAPTransport(FakeRawChannel(), SimpleNamespace())
        with self.assertRaises(AAPReceiveStateError):
            await transport.receive(1)



class CompatibilityRecorder:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.active = False

    @contextmanager
    def __call__(self, manager):
        del manager
        self.active = True
        self.events.append("compat_enter")
        try:
            yield
        finally:
            self.events.append("compat_exit")
            self.active = False



class OrchestrationRawChannel(FakeRawChannel):
    def __init__(
        self,
        events: list[str],
        compatibility: CompatibilityRecorder,
        records_active,
    ) -> None:
        super().__init__()
        self.events = events
        self.compatibility = compatibility
        self.records_active = records_active

    def write(self, payload: bytes) -> None:
        if not self.compatibility.active or not self.records_active():
            raise AssertionError("handshake began outside compatibility lifetime")
        self.events.append("handshake_send")
        super().write(payload)
        self.sink(AAP_HANDSHAKE_ACK)
        self.sink(ALL_DESCRIPTOR_EVIDENCE)

    async def disconnect(self) -> None:
        if not self.compatibility.active or not self.records_active():
            raise AssertionError("channel closed outside compatibility lifetime")
        self.events.append("channel_close")



class OrchestrationConnection:
    def __init__(
        self,
        events: list[str],
        compatibility: CompatibilityRecorder,
        records_active,
    ):
        self.events = events
        self.compatibility = compatibility
        self.l2cap_channel_manager = object()
        self.open_error = None
        self.channel = OrchestrationRawChannel(
            events, compatibility, records_active
        )

    async def create_l2cap_channel(self, spec):
        self.assertions = (spec.psm, spec.mode)
        self.events.append("channel_create")
        if self.open_error is not None:
            raise self.open_error
        return self.channel



class OrchestrationRuntime:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.records = None

    @contextmanager
    def temporary_sdp_records(self, records):
        self.records = records
        self.events.append("sdp_enter")
        try:
            yield
        finally:
            self.events.append("sdp_exit")
            self.records = None

    @contextmanager
    def observe_sdp(self, observer):
        del observer
        yield



class FakeSecureSession:
    def __init__(
        self,
        events: list[str],
        connection: OrchestrationConnection,
        runtime: OrchestrationRuntime,
    ):
        self.events = events
        self.connection = connection
        self.runtime = runtime

    @asynccontextmanager
    async def open(self, *, pre_connect_profile=None):
        prepared_profile = pre_connect_profile.prepare(
            SimpleNamespace(adapter_modalias="usb:v1234p5678d9ABC")
        )
        self.events.append("secure_enter")
        try:
            with prepared_profile.activate(self.runtime):
                yield AuthenticatedClassicContext(
                    "Synthetic AirPods",
                    self.connection,
                    SimpleNamespace(reported=False),
                    "usb:v1234p5678d9ABC",
                )
        finally:
            self.events.append("secure_exit")



class AAPOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def components(self):
        events: list[str] = []
        compatibility = CompatibilityRecorder(events)
        runtime = OrchestrationRuntime(events)
        connection = OrchestrationConnection(
            events, compatibility, lambda: runtime.records is not None
        )
        secure = FakeSecureSession(events, connection, runtime)
        session = AAPHandshakeProbeSession(
            secure,
            AAPChannelSession(compatibility=compatibility),
            AAPHandshakeSession(),
        )
        return session, connection, compatibility, events

    async def test_sdp_compatibility_and_handshake_lifetimes(self) -> None:
        session, connection, compatibility, events = self.components()
        result = await session.run()
        self.assertTrue(result.evidence.required)
        self.assertEqual(connection.channel.writes, [AAP_HANDSHAKE_REQUEST])
        self.assertEqual(
            events,
            [
                "secure_enter",
                "sdp_enter",
                "compat_enter",
                "channel_create",
                "handshake_send",
                "channel_close",
                "compat_exit",
                "sdp_exit",
                "secure_exit",
            ],
        )
        self.assertFalse(compatibility.active)
        self.assertEqual(connection.assertions[0], AAP_PSM)
        self.assertEqual(connection.assertions[1], l2cap.TransmissionMode.BASIC)

    async def test_handshake_failure_unwinds_channel_sdp_and_secure_session(self) -> None:
        session, connection, compatibility, events = self.components()

        class FailingHandshake:
            async def run(self, transport):
                async with transport.collect():
                    transport.send_handshake_request()
                    raise AAPHandshakeTimeoutError("synthetic timeout")

        session._handshake_session = FailingHandshake()
        with self.assertRaises(AAPHandshakeTimeoutError):
            await session.run()
        self.assertEqual(events[-4:], ["channel_close", "compat_exit", "sdp_exit", "secure_exit"])
        self.assertFalse(compatibility.active)

    async def test_channel_open_failure_restores_sdp_and_secure_session(self) -> None:
        session, connection, compatibility, events = self.components()
        connection.open_error = RuntimeError("synthetic channel failure")

        with self.assertRaises(AAPChannelOpenError):
            await session.run()

        self.assertEqual(
            events[-4:],
            ["channel_create", "compat_exit", "sdp_exit", "secure_exit"],
        )
        self.assertFalse(compatibility.active)

    async def test_descriptor_timeout_unwinds_channel_sdp_and_secure_session(self) -> None:
        session, connection, compatibility, events = self.components()

        class DescriptorTimeoutHandshake:
            async def run(self, transport):
                async with transport.collect():
                    transport.send_handshake_request()
                    self.assert_ack = await transport.receive(1)
                    raise AAPDescriptorObservationTimeoutError(
                        HandshakeObservation(True, DescriptorEvidence())
                    )

        timeout = DescriptorTimeoutHandshake()
        session._handshake_session = timeout
        with self.assertRaises(AAPDescriptorObservationTimeoutError):
            await session.run()
        self.assertEqual(timeout.assert_ack, AAP_HANDSHAKE_ACK)
        self.assertEqual(events[-4:], ["channel_close", "compat_exit", "sdp_exit", "secure_exit"])
        self.assertFalse(compatibility.active)

    async def test_cancellation_during_descriptor_observation_unwinds(self) -> None:
        session, connection, compatibility, events = self.components()

        class CancellingDuringDescriptors:
            async def run(self, transport):
                async with transport.collect():
                    transport.send_handshake_request()
                    self.assert_ack = await transport.receive(1)
                    raise asyncio.CancelledError

        cancelling = CancellingDuringDescriptors()
        session._handshake_session = cancelling
        with self.assertRaises(asyncio.CancelledError):
            await session.run()
        self.assertEqual(cancelling.assert_ack, AAP_HANDSHAKE_ACK)
        self.assertIsNone(connection.channel.sink)
        self.assertEqual(events[-4:], ["channel_close", "compat_exit", "sdp_exit", "secure_exit"])
        self.assertFalse(compatibility.active)

