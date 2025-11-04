"""Hardware-independent tests for the heart-rate session."""

from __future__ import annotations

import asyncio
import unittest

from dataclasses import fields

from types import SimpleNamespace


from airpods_hr.aap import AAP_HANDSHAKE_REQUEST, AAPHandshakeResult, BumbleAAPTransport, DescriptorEvidence, HandshakeObservation


from airpods_hr.heart_rate_session import CONNECT4_ACK, ControlFrameSummary, HeartRateActivationSession, HeartRateBootstrapAckTimeoutError, HeartRateCleanupError, HeartRateCompletion, HeartRateConnectAckTimeoutError, HeartRateMonitorActivationSession, HeartRateMonitorSessionResult, HeartRateNoSamplesError, HeartRateProgress, HeartRateStateError, HeartRateStartAckTimeoutError, is_connect4_ack, is_observed_service_ack


from airpods_hr.heartrate import HeartRateReport, parse_heart_rate_packet
from airpods_hr.protocol import HEART_RATE_MARKER, HeartRateCommand


REAL_STOP_ACK_ONE_BYTE_ID = bytes.fromhex(
    "04 00 04 00 17 00 00 00 10 00 08 00 "
    "08 27 10 01 4a 02 08 0e"
)
REAL_START_ACK_ONE_BYTE_ID = bytes.fromhex(
    "04 00 04 00 17 00 00 00 10 00 08 00 "
    "08 28 10 01 4a 02 08 13"
)
REAL_STOP_ACK_TWO_BYTE_ID = bytes.fromhex(
    "04 00 04 00 17 00 00 00 10 00 09 00 "
    "08 b4 01 10 01 4a 02 08 0e"
)
REAL_START_ACK_TWO_BYTE_ID = bytes.fromhex(
    "04 00 04 00 17 00 00 00 10 00 09 00 "
    "08 b5 01 10 01 4a 02 08 13"
)
REAL_BOOTSTRAP_TAIL_10_ONE_BYTE_ID = bytes.fromhex(
    "04 00 04 00 17 00 00 00 10 00 08 00 "
    "08 25 10 01 62 02 08 10"
)
REAL_BOOTSTRAP_TAIL_10_TWO_BYTE_ID = bytes.fromhex(
    "04 00 04 00 17 00 00 00 10 00 09 00 "
    "08 b2 01 10 01 62 02 08 10"
)
REAL_BOOTSTRAP_TAIL_11_12_13_ONE_BYTE_ID = bytes.fromhex(
    "04 00 04 00 17 00 00 00 10 00 10 00 "
    "08 26 10 01 62 02 08 11 62 02 08 12 62 02 08 13"
)
REAL_BOOTSTRAP_TAIL_11_12_13_TWO_BYTE_ID = bytes.fromhex(
    "04 00 04 00 17 00 00 00 10 00 11 00 "
    "08 b3 01 10 01 62 02 08 11 62 02 08 12 62 02 08 13"
)
BOOTSTRAP_TAIL_11_12_13 = bytes.fromhex(
    "10 01 62 02 08 11 62 02 08 12 62 02 08 13"
)


def service_ack(
    service_id: int,
    identifier: bytes = b"\x7f",
    control_prefix: bytes = b"\x10\x01",
) -> bytes:
    suffix = control_prefix + bytes.fromhex("4a 02 08") + bytes((service_id,))
    payload = b"\x08" + identifier + suffix
    return (
        bytes.fromhex("04 00 04 00 17 00 00 00")
        + bytes.fromhex("10 00")
        + len(payload).to_bytes(2, "little")
        + payload
    )


def observed_envelope(identifier: bytes, tail: bytes) -> bytes:
    trailing = b"\x08" + identifier + tail
    return (
        bytes.fromhex("04 00 04 00 17 00 00 00 10 00")
        + len(trailing).to_bytes(2, "little")
        + trailing
    )


def heart_rate_packet(
    bpm: int,
    sequence: int,
    *,
    outer_length: int = 40,
) -> bytes:
    report = (
        bytes((1, bpm, 9))
        + sequence.to_bytes(2, "little")
        + bytes((7,))
        + (1_000_000_000 * sequence).to_bytes(8, "little")
        + (3).to_bytes(4, "little")
    )
    prefix_size = outer_length - len(HEART_RATE_MARKER) - len(report)
    return bytes((0xA0,)) * prefix_size + HEART_RATE_MARKER + report


def completed_handshake(sent_at: float = 0.0) -> AAPHandshakeResult:
    return AAPHandshakeResult(
        observation=HandshakeObservation(
            ack_observed=True,
            evidence=DescriptorEvidence(
                sensor_framework=True,
                heart_rate_service=True,
            ),
        ),
        application_payloads_sent=1,
        handshake_sent_at=sent_at,
    )


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class FakeCollectedTransport:
    def __init__(
        self,
        frames: list[bytes | BaseException],
        clock: FakeClock,
        *,
        fail_send: set[HeartRateCommand] | None = None,
        pending_receive_frames: int = 0,
    ) -> None:
        self.frames = list(frames)
        self.clock = clock
        self.commands: list[HeartRateCommand] = []
        self.application_payloads_sent = 1
        self.fail_send = fail_send or set()
        self.send_times: list[float] = []
        self._pending_receive_frames = pending_receive_frames

    @property
    def pending_receive_frames(self) -> int:
        return self._pending_receive_frames

    def send_heart_rate_command(self, command: HeartRateCommand) -> None:
        if command in self.fail_send:
            raise RuntimeError("synthetic send failure")
        self.commands.append(command)
        self.send_times.append(self.clock())
        self.application_payloads_sent += 1

    async def receive(self, timeout: float) -> bytes:
        if self.frames:
            item = self.frames.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        self.clock.now += timeout
        raise TimeoutError


class TimedFakeCollectedTransport(FakeCollectedTransport):
    def __init__(
        self,
        frames: list[tuple[float, bytes]],
        clock: FakeClock,
        *,
        pending_receive_frames: int = 0,
    ) -> None:
        super().__init__(
            [],
            clock,
            pending_receive_frames=pending_receive_frames,
        )
        self.timed_frames = list(frames)

    async def receive(self, timeout: float) -> bytes:
        if self.timed_frames:
            arrival, frame = self.timed_frames.pop(0)
            if arrival - self.clock.now > timeout:
                self.clock.now += timeout
                raise TimeoutError
            self.clock.now = arrival
            return frame
        self.clock.now += timeout
        raise TimeoutError


def successful_frames(sample_count: int = 5, *, include_stop_ack: bool = True):
    frames = [
        service_ack(0x0E),
        CONNECT4_ACK,
        service_ack(0x13, b"\x80\x01"),
    ]
    frames.extend(
        heart_rate_packet(70 + index, index + 1)
        for index in range(sample_count)
    )
    if include_stop_ack:
        frames.append(service_ack(0x13, b"\x81\x01"))
    return frames


class CommandPayloadTests(unittest.TestCase):
    def test_all_nine_payloads_are_exact(self) -> None:
        expected = {
            HeartRateCommand.STOP_HEAD: (
                "04 00 04 00 17 00 00 00 10 00 10 00 "
                "08 86 01 42 0b 08 0e 10 02 1a 05 01 00 00 00 00"
            ),
            HeartRateCommand.CONNECT0: (
                "00 00 00 00 01 00 03 00 00 00 00 00 00 00 00 00"
            ),
            HeartRateCommand.CAPS0: "04 00 00 00 01 00 00",
            HeartRateCommand.CONNECT4: (
                "00 00 04 00 01 00 03 00 00 00 00 00 00 00 00 00"
            ),
            HeartRateCommand.CAPS4: "04 00 04 00 01 00 00",
            HeartRateCommand.HR_ON: "04 00 04 00 09 00 30 01 00 00 00",
            HeartRateCommand.START_HR: (
                "04 00 04 00 17 00 00 00 10 00 10 00 "
                "08 e3 46 42 0b 08 13 10 02 1a 05 01 40 42 0f 00"
            ),
            HeartRateCommand.STOP_HR: (
                "04 00 04 00 17 00 00 00 10 00 10 00 "
                "08 e2 46 42 0b 08 13 10 02 1a 05 01 00 00 00 00"
            ),
            HeartRateCommand.HR_OFF: "04 00 04 00 09 00 30 00 00 00 00",
        }

        self.assertEqual(set(expected), set(HeartRateCommand))
        for command, hex_value in expected.items():
            self.assertEqual(command.payload, bytes.fromhex(hex_value))

    def test_workout_packet_is_not_a_command(self) -> None:
        workout = bytes.fromhex("04 00 04 00 44 00 04 00 02 00 03 07")
        self.assertNotIn(workout, {command.payload for command in HeartRateCommand})

    def test_transport_has_no_arbitrary_byte_send_api(self) -> None:
        self.assertFalse(hasattr(BumbleAAPTransport, "send"))
        self.assertFalse(hasattr(BumbleAAPTransport, "write"))
        self.assertTrue(hasattr(BumbleAAPTransport, "send_heart_rate_command"))

    async def _exercise_restricted_transport(
        self,
    ) -> tuple[list[bytes], object, list[object], tuple[int, int, int]]:
        class RawChannel:
            def __init__(self) -> None:
                self._sink = object()
                self.assignments: list[object] = []
                self.writes: list[bytes] = []

            @property
            def sink(self):
                return self._sink

            @sink.setter
            def sink(self, value):
                self._sink = value
                self.assignments.append(value)

            def write(self, payload: bytes) -> None:
                self.writes.append(payload)

        raw = RawChannel()
        original_sink = raw.sink
        transport = BumbleAAPTransport(raw, SimpleNamespace())
        async with transport.collect():
            transport.send_handshake_request()
            transport.send_heart_rate_command(HeartRateCommand.STOP_HEAD)
            with self.assertRaises(TypeError):
                transport.send_heart_rate_command(HeartRateCommand.STOP_HEAD.payload)
            raw.sink(b"queued-one")
            raw.sink(b"queued-two")
            first_pending = transport.pending_receive_frames
            second_pending = transport.pending_receive_frames
            await transport.receive(1)
            pending_after_receive = transport.pending_receive_frames

        return (
            raw.writes,
            original_sink,
            raw.assignments,
            (first_pending, second_pending, pending_after_receive),
        )

    def test_transport_accepts_only_closed_commands_and_restores_sink_once(self) -> None:
        writes, original_sink, assignments, pending_counts = asyncio.run(
            self._exercise_restricted_transport()
        )

        self.assertEqual(
            writes,
            [AAP_HANDSHAKE_REQUEST, HeartRateCommand.STOP_HEAD.payload],
        )
        self.assertEqual(len(assignments), 2)
        self.assertTrue(callable(assignments[0]))
        self.assertIs(assignments[1], original_sink)
        self.assertEqual(pending_counts, (2, 2, 1))


class AcknowledgementClassifierTests(unittest.TestCase):
    def test_all_four_real_ack_literals_and_cross_service_rejection(self) -> None:
        self.assertTrue(
            is_observed_service_ack(REAL_STOP_ACK_ONE_BYTE_ID, 0x0E)
        )
        self.assertTrue(
            is_observed_service_ack(REAL_STOP_ACK_TWO_BYTE_ID, 0x0E)
        )
        self.assertTrue(
            is_observed_service_ack(REAL_START_ACK_ONE_BYTE_ID, 0x13)
        )
        self.assertTrue(
            is_observed_service_ack(REAL_START_ACK_TWO_BYTE_ID, 0x13)
        )
        self.assertFalse(
            is_observed_service_ack(REAL_STOP_ACK_ONE_BYTE_ID, 0x13)
        )
        self.assertFalse(
            is_observed_service_ack(REAL_START_ACK_ONE_BYTE_ID, 0x0E)
        )

    def test_service_ack_accepts_varying_one_and_two_byte_identifiers(self) -> None:
        self.assertTrue(is_observed_service_ack(service_ack(0x0E, b"\x7f"), 0x0E))
        self.assertTrue(
            is_observed_service_ack(service_ack(0x0E, b"\x80\x01"), 0x0E)
        )
        self.assertTrue(
            is_observed_service_ack(service_ack(0x13, b"\xff\x01"), 0x13)
        )

    def test_both_observed_control_prefixes_accept_both_services_and_ids(
        self,
    ) -> None:
        for control_prefix in (b"\x10\x01", b"\x10\x03"):
            for service_id in (0x0E, 0x13):
                for identifier in (b"\x27", b"\xb4\x01"):
                    with self.subTest(
                        control_prefix=control_prefix,
                        service_id=service_id,
                        identifier_octets=len(identifier),
                    ):
                        frame = service_ack(
                            service_id,
                            identifier,
                            control_prefix,
                        )
                        self.assertTrue(
                            is_observed_service_ack(frame, service_id)
                        )
                        self.assertTrue(
                            ControlFrameSummary.from_frame(
                                frame
                            ).post_identifier_prefix_is_observed
                        )

    def test_unobserved_prefixes_and_wide_identifiers_remain_rejected(
        self,
    ) -> None:
        for control_prefix in (
            b"\x10\x00",
            b"\x10\x02",
            b"\x10\x04",
            b"\x21\x7f",
        ):
            for identifier in (b"\x27", b"\xb4\x01"):
                with self.subTest(
                    control_prefix=control_prefix,
                    identifier_octets=len(identifier),
                ):
                    self.assertFalse(
                        is_observed_service_ack(
                            service_ack(0x0E, identifier, control_prefix),
                            0x0E,
                        )
                    )
        for control_prefix in (b"\x10\x01", b"\x10\x03"):
            with self.subTest(control_prefix=control_prefix):
                self.assertFalse(
                    is_observed_service_ack(
                        service_ack(
                            0x0E,
                            b"\x81\x81\x01",
                            control_prefix,
                        ),
                        0x0E,
                    )
                )

    def test_malformed_and_near_service_acks_are_rejected(self) -> None:
        valid = REAL_STOP_ACK_ONE_BYTE_ID
        wrong_prefix = b"\x05" + valid[1:]
        wrong_fixed_word = valid[:8] + b"\x0f\x00" + valid[10:]
        inner_length_short = valid[:10] + b"\x07\x00" + valid[12:]
        inner_length_long = valid[:10] + b"\x09\x00" + valid[12:]
        missing_identifier_tag = valid[:12] + b"\x09" + valid[13:]
        zero_length_identifier = (
            valid[:10]
            + b"\x07\x00"
            + b"\x08"
            + bytes.fromhex("10 01 4a 02 08 0e")
        )
        noncanonical_two_byte_identifier = service_ack(0x0E, b"\x81\x00")
        three_byte_identifier = service_ack(0x0E, b"\x81\x81\x01")
        wrong_suffix = valid[:-6] + bytes.fromhex("10 01 4b 02 08 0e")
        wrong_parameter = valid[:-6] + bytes.fromhex("10 01 4a 03 08 0e")
        wrong_service = valid[:-1] + b"\x7f"
        extra_trailing_byte = valid + b"\x00"
        truncated = valid[:-1]

        for frame in (
            wrong_prefix,
            wrong_fixed_word,
            inner_length_short,
            inner_length_long,
            missing_identifier_tag,
            zero_length_identifier,
            noncanonical_two_byte_identifier,
            three_byte_identifier,
            wrong_suffix,
            wrong_parameter,
            wrong_service,
            extra_trailing_byte,
            truncated,
        ):
            with self.subTest(frame_length=len(frame)):
                self.assertFalse(is_observed_service_ack(frame, 0x0E))

    def test_40_and_41_byte_hr_samples_are_not_service_acks(self) -> None:
        for outer_length in (40, 41):
            packet = heart_rate_packet(72, 1, outer_length=outer_length)
            with self.subTest(outer_length=outer_length):
                self.assertFalse(is_observed_service_ack(packet, 0x13))

    def test_connect4_ack_is_exact(self) -> None:
        self.assertTrue(is_connect4_ack(CONNECT4_ACK))
        self.assertFalse(is_connect4_ack(CONNECT4_ACK[:-1] + b"\x01"))


class ControlFrameSummaryTests(unittest.TestCase):
    def test_summary_model_has_no_raw_frame_field_or_payload_repr(self) -> None:
        frame = b"private-payload-that-must-not-be-retained"
        summary = ControlFrameSummary.from_frame(frame)

        field_names = {field.name for field in fields(ControlFrameSummary)}
        self.assertTrue(
            {"frame", "payload", "raw", "bytes"}.isdisjoint(field_names)
        )
        self.assertNotIn(frame.decode(), repr(summary))

    def test_real_service_0e_ack_identifier_shapes(self) -> None:
        one_byte = ControlFrameSummary.from_frame(REAL_STOP_ACK_ONE_BYTE_ID)
        two_byte = ControlFrameSummary.from_frame(REAL_STOP_ACK_TWO_BYTE_ID)

        self.assertTrue(one_byte.service_ack_suffix_0e)
        self.assertTrue(one_byte.candidate_identifier_terminated)
        self.assertEqual(one_byte.candidate_identifier_octets, 1)
        self.assertTrue(one_byte.candidate_identifier_canonical)
        self.assertTrue(one_byte.identifier_is_current_canonical_1_or_2)
        self.assertEqual(one_byte.post_identifier_length, 6)
        self.assertEqual(one_byte.post_identifier_prefix_octet_0, 0x10)
        self.assertEqual(one_byte.post_identifier_prefix_octet_1, 0x01)
        self.assertTrue(one_byte.post_identifier_starts_10_01)
        self.assertEqual(one_byte.post_identifier_field_tag, 0x4A)
        self.assertEqual(one_byte.post_identifier_field_parameter, 0x02)
        self.assertTrue(one_byte.remainder_is_ack_0e_shape)
        self.assertFalse(one_byte.remainder_is_ack_13_shape)
        self.assertFalse(one_byte.remainder_is_bootstrap_10_shape)
        self.assertFalse(one_byte.remainder_is_bootstrap_11_12_13_shape)
        self.assertTrue(one_byte.terminal_tag_08)
        self.assertEqual(one_byte.terminal_value, 0x0E)
        self.assertEqual(one_byte.observed_62_02_08_group_count, 0)
        self.assertEqual(one_byte.observed_62_02_08_terminal_values, ())
        self.assertEqual(one_byte.observed_62_02_08_group_offsets, ())
        self.assertTrue(two_byte.service_ack_suffix_0e)
        self.assertTrue(two_byte.candidate_identifier_terminated)
        self.assertEqual(two_byte.candidate_identifier_octets, 2)
        self.assertTrue(two_byte.candidate_identifier_canonical)
        self.assertTrue(two_byte.identifier_is_current_canonical_1_or_2)
        self.assertEqual(two_byte.post_identifier_length, 6)
        self.assertEqual(two_byte.post_identifier_field_tag, 0x4A)
        self.assertEqual(two_byte.post_identifier_field_parameter, 0x02)
        self.assertEqual(two_byte.terminal_value, 0x0E)
        self.assertTrue(two_byte.remainder_is_ack_0e_shape)

    def test_real_service_13_ack_remainder_shapes(self) -> None:
        for frame, identifier_octets in (
            (REAL_START_ACK_ONE_BYTE_ID, 1),
            (REAL_START_ACK_TWO_BYTE_ID, 2),
        ):
            with self.subTest(identifier_octets=identifier_octets):
                summary = ControlFrameSummary.from_frame(frame)
                self.assertEqual(summary.candidate_identifier_octets, identifier_octets)
                self.assertEqual(summary.post_identifier_prefix_octet_0, 0x10)
                self.assertEqual(summary.post_identifier_prefix_octet_1, 0x01)
                self.assertFalse(summary.remainder_is_ack_0e_shape)
                self.assertTrue(summary.remainder_is_ack_13_shape)
                self.assertFalse(summary.remainder_is_bootstrap_10_shape)
                self.assertFalse(
                    summary.remainder_is_bootstrap_11_12_13_shape
                )

    def test_three_byte_identifier_is_diagnostic_only(self) -> None:
        frame = service_ack(0x0E, b"\x81\x81\x01")
        summary = ControlFrameSummary.from_frame(frame)

        self.assertTrue(summary.service_ack_suffix_0e)
        self.assertTrue(summary.candidate_identifier_terminated)
        self.assertEqual(summary.candidate_identifier_octets, 3)
        self.assertTrue(summary.candidate_identifier_canonical)
        self.assertFalse(summary.identifier_is_current_canonical_1_or_2)
        self.assertEqual(summary.post_identifier_length, 6)
        self.assertEqual(summary.post_identifier_field_tag, 0x4A)
        self.assertFalse(is_observed_service_ack(frame, 0x0E))

        tail_frame = observed_envelope(
            b"\x81\x81\x01", bytes.fromhex("10 01 62 02 08 10")
        )
        tail_summary = ControlFrameSummary.from_frame(tail_frame)
        self.assertTrue(tail_summary.bootstrap_tail_10_suffix_present)
        self.assertFalse(tail_summary.bootstrap_tail_10)
        current_tail_frame = observed_envelope(
            b"\x81\x81\x01", bytes.fromhex("10 03 62 02 08 10")
        )
        current_tail_summary = ControlFrameSummary.from_frame(current_tail_frame)
        self.assertFalse(current_tail_summary.bootstrap_tail_10_suffix_present)
        self.assertFalse(current_tail_summary.bootstrap_tail_10)

    def test_identifier_diagnostic_does_not_require_a_known_suffix(self) -> None:
        frame = observed_envelope(
            b"\x25", bytes.fromhex("10 01 7a 03 08 99")
        )
        summary = ControlFrameSummary.from_frame(frame)

        self.assertFalse(summary.service_ack_suffix_0e)
        self.assertFalse(summary.service_ack_suffix_13)
        self.assertTrue(summary.candidate_identifier_terminated)
        self.assertEqual(summary.candidate_identifier_octets, 1)
        self.assertTrue(summary.candidate_identifier_canonical)
        self.assertEqual(summary.post_identifier_length, 6)
        self.assertTrue(summary.post_identifier_starts_10_01)
        self.assertEqual(summary.post_identifier_field_tag, 0x7A)
        self.assertEqual(summary.post_identifier_field_parameter, 0x03)
        self.assertTrue(summary.terminal_tag_08)
        self.assertEqual(summary.terminal_value, 0x99)

    def test_changed_prefix_recognizes_only_the_historical_remainder(self) -> None:
        for prefix in (b"\x10\x02", b"\x21\x7f"):
            ack_frame = observed_envelope(
                b"\x25", prefix + bytes.fromhex("4a 02 08 0e")
            )
            ack_summary = ControlFrameSummary.from_frame(ack_frame)
            bootstrap_frame = observed_envelope(
                b"\x25", prefix + bytes.fromhex("62 02 08 10")
            )
            bootstrap_summary = ControlFrameSummary.from_frame(bootstrap_frame)

            with self.subTest(prefix=prefix):
                self.assertEqual(
                    (
                        ack_summary.post_identifier_prefix_octet_0,
                        ack_summary.post_identifier_prefix_octet_1,
                    ),
                    tuple(prefix),
                )
                self.assertTrue(ack_summary.remainder_is_ack_0e_shape)
                self.assertFalse(ack_summary.post_identifier_starts_10_01)
                self.assertFalse(ack_summary.post_identifier_prefix_is_observed)
                self.assertIsNone(ack_summary.post_identifier_field_tag)
                self.assertIsNone(ack_summary.post_identifier_field_parameter)
                self.assertFalse(ack_summary.service_ack_suffix_0e)
                self.assertFalse(is_observed_service_ack(ack_frame, 0x0E))
                self.assertTrue(
                    bootstrap_summary.remainder_is_bootstrap_10_shape
                )
                self.assertEqual(
                    bootstrap_summary.observed_62_02_08_group_offsets,
                    (2,),
                )
                self.assertFalse(
                    bootstrap_summary.bootstrap_tail_10_suffix_present
                )
                self.assertFalse(bootstrap_summary.bootstrap_tail_10)

    def test_unknown_62_terminal_0e_shape_is_not_an_ack(self) -> None:
        frame = observed_envelope(
            b"\x27", bytes.fromhex("10 01 62 02 08 0e")
        )
        summary = ControlFrameSummary.from_frame(frame)

        self.assertFalse(summary.service_ack_suffix_0e)
        self.assertEqual(summary.post_identifier_field_tag, 0x62)
        self.assertEqual(summary.post_identifier_field_parameter, 0x02)
        self.assertEqual(summary.terminal_value, 0x0E)
        self.assertEqual(summary.observed_62_02_08_group_count, 1)
        self.assertEqual(summary.observed_62_02_08_terminal_values, (0x0E,))
        self.assertFalse(is_observed_service_ack(frame, 0x0E))

    def test_unterminated_and_noncanonical_identifiers_are_safe(self) -> None:
        unterminated = ControlFrameSummary.from_frame(
            observed_envelope(b"\x81\x81\x81\x81\x81", b"")
        )
        overlong = ControlFrameSummary.from_frame(
            observed_envelope(
                b"\x81\x81\x81\x81\x81\x01",
                bytes.fromhex("10 01 7a 03"),
            )
        )
        canonical_five = ControlFrameSummary.from_frame(
            observed_envelope(
                b"\x81\x81\x81\x81\x01",
                bytes.fromhex("10 01 7a 03"),
            )
        )
        noncanonical = ControlFrameSummary.from_frame(
            observed_envelope(b"\x81\x00", bytes.fromhex("10 01 7a 03"))
        )

        self.assertFalse(unterminated.candidate_identifier_terminated)
        self.assertEqual(unterminated.candidate_identifier_octets, 5)
        self.assertFalse(unterminated.candidate_identifier_canonical)
        self.assertIsNone(unterminated.post_identifier_length)
        self.assertIsNone(unterminated.post_identifier_field_tag)
        self.assertFalse(overlong.candidate_identifier_terminated)
        self.assertEqual(overlong.candidate_identifier_octets, 5)
        self.assertFalse(overlong.candidate_identifier_canonical)
        self.assertIsNone(overlong.post_identifier_length)
        self.assertTrue(canonical_five.candidate_identifier_terminated)
        self.assertEqual(canonical_five.candidate_identifier_octets, 5)
        self.assertTrue(canonical_five.candidate_identifier_canonical)
        self.assertFalse(
            canonical_five.identifier_is_current_canonical_1_or_2
        )
        self.assertFalse(noncanonical.candidate_identifier_canonical)
        self.assertTrue(noncanonical.candidate_identifier_terminated)
        self.assertEqual(noncanonical.candidate_identifier_octets, 2)
        self.assertEqual(noncanonical.post_identifier_field_tag, 0x7A)
        self.assertFalse(is_observed_service_ack(
            service_ack(0x0E, b"\x81\x00"), 0x0E
        ))

    def test_observed_group_values_are_bounded(self) -> None:
        frame = observed_envelope(
            b"\x25",
            bytes.fromhex(
                "10 01 62 02 08 10 62 02 08 11 62 02 08 12 "
                "62 02 08 13 62 02 08 14"
            ),
        )
        summary = ControlFrameSummary.from_frame(frame)

        self.assertEqual(summary.observed_62_02_08_group_count, 5)
        self.assertIsNone(summary.observed_62_02_08_terminal_values)
        self.assertEqual(summary.observed_62_02_08_group_offsets, (2, 6, 10, 14))

    def test_hr_sample_reports_only_marker_evidence(self) -> None:
        summary = ControlFrameSummary.from_frame(heart_rate_packet(72, 1))

        self.assertTrue(summary.heart_rate_marker_present)
        self.assertFalse(summary.service_ack_suffix_0e)
        self.assertFalse(summary.service_ack_suffix_13)
        self.assertIsNone(summary.candidate_identifier_octets)

    def test_all_four_real_historical_bootstrap_tails_are_classified(self) -> None:
        self.assertEqual(
            observed_envelope(b"\x25", bytes.fromhex("10 01 62 02 08 10")),
            REAL_BOOTSTRAP_TAIL_10_ONE_BYTE_ID,
        )
        self.assertEqual(
            observed_envelope(b"\xb2\x01", bytes.fromhex("10 01 62 02 08 10")),
            REAL_BOOTSTRAP_TAIL_10_TWO_BYTE_ID,
        )
        self.assertEqual(
            observed_envelope(b"\x26", BOOTSTRAP_TAIL_11_12_13),
            REAL_BOOTSTRAP_TAIL_11_12_13_ONE_BYTE_ID,
        )
        self.assertEqual(
            observed_envelope(b"\xb3\x01", BOOTSTRAP_TAIL_11_12_13),
            REAL_BOOTSTRAP_TAIL_11_12_13_TWO_BYTE_ID,
        )
        vectors = (
            (REAL_BOOTSTRAP_TAIL_10_ONE_BYTE_ID, 1, True, False, (0x10,)),
            (REAL_BOOTSTRAP_TAIL_10_TWO_BYTE_ID, 2, True, False, (0x10,)),
            (
                REAL_BOOTSTRAP_TAIL_11_12_13_ONE_BYTE_ID,
                1,
                False,
                True,
                (0x11, 0x12, 0x13),
            ),
            (
                REAL_BOOTSTRAP_TAIL_11_12_13_TWO_BYTE_ID,
                2,
                False,
                True,
                (0x11, 0x12, 0x13),
            ),
        )
        for (
            frame,
            expected_identifier_octets,
            expected_tail_10,
            expected_tail_11_12_13,
            expected_group_values,
        ) in vectors:
            with self.subTest(frame_length=len(frame)):
                summary = ControlFrameSummary.from_frame(frame)
                self.assertTrue(summary.candidate_identifier_terminated)
                self.assertEqual(
                    summary.candidate_identifier_octets,
                    expected_identifier_octets,
                )
                self.assertTrue(summary.candidate_identifier_canonical)
                self.assertTrue(summary.post_identifier_starts_10_01)
                self.assertEqual(summary.post_identifier_prefix_octet_0, 0x10)
                self.assertEqual(summary.post_identifier_prefix_octet_1, 0x01)
                self.assertEqual(summary.post_identifier_field_tag, 0x62)
                self.assertEqual(summary.post_identifier_field_parameter, 0x02)
                self.assertTrue(summary.terminal_tag_08)
                self.assertEqual(
                    summary.terminal_value,
                    expected_group_values[-1],
                )
                self.assertEqual(
                    summary.observed_62_02_08_group_count,
                    len(expected_group_values),
                )
                self.assertEqual(
                    summary.observed_62_02_08_terminal_values,
                    expected_group_values,
                )
                expected_offsets = (
                    (2,)
                    if expected_tail_10
                    else (2, 6, 10)
                )
                self.assertEqual(
                    summary.observed_62_02_08_group_offsets,
                    expected_offsets,
                )
                self.assertIs(
                    summary.remainder_is_bootstrap_10_shape,
                    expected_tail_10,
                )
                self.assertIs(
                    summary.remainder_is_bootstrap_11_12_13_shape,
                    expected_tail_11_12_13,
                )
                self.assertIs(
                    summary.bootstrap_tail_10_suffix_present,
                    expected_tail_10,
                )
                self.assertIs(
                    summary.bootstrap_tail_11_12_13_suffix_present,
                    expected_tail_11_12_13,
                )
                self.assertIs(summary.bootstrap_tail_10, expected_tail_10)
                self.assertIs(
                    summary.bootstrap_tail_11_12_13,
                    expected_tail_11_12_13,
                )

    def test_bootstrap_tail_trailing_length_near_matches_are_rejected(self) -> None:
        for frame in (
            REAL_BOOTSTRAP_TAIL_10_ONE_BYTE_ID,
            REAL_BOOTSTRAP_TAIL_10_TWO_BYTE_ID,
            REAL_BOOTSTRAP_TAIL_11_12_13_ONE_BYTE_ID,
            REAL_BOOTSTRAP_TAIL_11_12_13_TWO_BYTE_ID,
        ):
            actual_length = int.from_bytes(frame[10:12], "little")
            for delta in (-1, 1):
                near = (
                    frame[:10]
                    + (actual_length + delta).to_bytes(2, "little")
                    + frame[12:]
                )
                summary = ControlFrameSummary.from_frame(near)
                with self.subTest(frame_length=len(frame), delta=delta):
                    self.assertFalse(summary.bootstrap_tail_10)
                    self.assertFalse(summary.bootstrap_tail_11_12_13)

    def test_current_observed_prefix_bootstrap_tails_are_classified(self) -> None:
        for identifier in (b"\x25", b"\xb2\x01"):
            tail_10 = ControlFrameSummary.from_frame(
                observed_envelope(
                    identifier,
                    bytes.fromhex("10 03 62 02 08 10"),
                )
            )
            tail_11_12_13 = ControlFrameSummary.from_frame(
                observed_envelope(
                    identifier,
                    b"\x10\x03" + BOOTSTRAP_TAIL_11_12_13[2:],
                )
            )
            with self.subTest(identifier_octets=len(identifier)):
                self.assertTrue(tail_10.post_identifier_prefix_is_observed)
                self.assertTrue(tail_10.remainder_is_bootstrap_10_shape)
                self.assertTrue(tail_10.bootstrap_tail_10)
                self.assertFalse(tail_10.bootstrap_tail_11_12_13)
                self.assertTrue(
                    tail_11_12_13.post_identifier_prefix_is_observed
                )
                self.assertTrue(
                    tail_11_12_13.remainder_is_bootstrap_11_12_13_shape
                )
                self.assertFalse(tail_11_12_13.bootstrap_tail_10)
                self.assertTrue(tail_11_12_13.bootstrap_tail_11_12_13)

    def test_unobserved_prefix_bootstrap_tails_remain_rejected(self) -> None:
        remainders = (
            bytes.fromhex("62 02 08 10"),
            BOOTSTRAP_TAIL_11_12_13[2:],
        )
        for control_prefix in (b"\x10\x02", b"\x10\x04", b"\x21\x7f"):
            for identifier in (b"\x25", b"\xb2\x01"):
                for remainder in remainders:
                    frame = observed_envelope(
                        identifier,
                        control_prefix + remainder,
                    )
                    summary = ControlFrameSummary.from_frame(frame)
                    with self.subTest(
                        control_prefix=control_prefix,
                        identifier_octets=len(identifier),
                        remainder_length=len(remainder),
                    ):
                        self.assertFalse(summary.bootstrap_tail_10)
                        self.assertFalse(summary.bootstrap_tail_11_12_13)
        for control_prefix in (b"\x10\x01", b"\x10\x03"):
            for remainder in remainders:
                frame = observed_envelope(
                    b"\x81\x81\x01",
                    control_prefix + remainder,
                )
                summary = ControlFrameSummary.from_frame(frame)
                with self.subTest(
                    control_prefix=control_prefix,
                    identifier_octets=3,
                    remainder_length=len(remainder),
                ):
                    self.assertFalse(summary.bootstrap_tail_10)
                    self.assertFalse(summary.bootstrap_tail_11_12_13)

    def test_near_bootstrap_tail_and_short_frame_are_safe(self) -> None:
        near = REAL_BOOTSTRAP_TAIL_10_ONE_BYTE_ID[:-1] + b"\x11"
        near_summary = ControlFrameSummary.from_frame(near)
        short_summary = ControlFrameSummary.from_frame(b"\x04\x00\x04")

        self.assertFalse(near_summary.bootstrap_tail_10)
        self.assertEqual(short_summary.length, 3)
        self.assertIsNone(short_summary.header_u16_2_3)
        self.assertIsNone(short_summary.word_u16_8_9)
        self.assertIsNone(short_summary.word_u16_10_11)
        self.assertIsNone(short_summary.trailing_length_consistent)
        self.assertIsNone(short_summary.tag_08_at_offset_12)
        self.assertIsNone(short_summary.candidate_identifier_terminated)
        self.assertIsNone(short_summary.candidate_identifier_canonical)
        self.assertIsNone(short_summary.post_identifier_length)
        self.assertFalse(short_summary.terminal_tag_08)
        self.assertIsNone(short_summary.observed_62_02_08_group_count)
        self.assertIsNone(short_summary.observed_62_02_08_group_offsets)

    def test_short_and_malformed_post_identifier_shapes_are_safe(self) -> None:
        no_post = ControlFrameSummary.from_frame(observed_envelope(b"\x25", b""))
        one_octet = ControlFrameSummary.from_frame(
            observed_envelope(b"\x25", b"\x10")
        )
        truncated = ControlFrameSummary.from_frame(
            observed_envelope(b"\x25", b"\x10\x01\x4a")
        )
        malformed_envelope = ControlFrameSummary.from_frame(
            b"\x05" + REAL_STOP_ACK_ONE_BYTE_ID[1:]
        )

        for summary in (no_post, one_octet, malformed_envelope):
            with self.subTest(length=summary.length):
                self.assertIsNone(summary.post_identifier_prefix_octet_0)
                self.assertIsNone(summary.post_identifier_prefix_octet_1)
                self.assertIsNone(summary.remainder_is_ack_0e_shape)
                self.assertIsNone(summary.remainder_is_ack_13_shape)
                self.assertIsNone(summary.remainder_is_bootstrap_10_shape)
                self.assertIsNone(
                    summary.remainder_is_bootstrap_11_12_13_shape
                )

        self.assertEqual(truncated.post_identifier_prefix_octet_0, 0x10)
        self.assertEqual(truncated.post_identifier_prefix_octet_1, 0x01)
        self.assertFalse(truncated.remainder_is_ack_0e_shape)
        self.assertFalse(truncated.remainder_is_ack_13_shape)
        self.assertFalse(truncated.remainder_is_bootstrap_10_shape)
        self.assertFalse(truncated.remainder_is_bootstrap_11_12_13_shape)


class HeartRateActivationTests(unittest.IsolatedAsyncioTestCase):
    def make_session(
        self,
        clock: FakeClock,
        *,
        sample_target: int = 5,
        control_summary_limit: int = 8,
        progress=None,
    ) -> HeartRateActivationSession:
        return HeartRateActivationSession(
            sample_target=sample_target,
            stream_timeout=12,
            control_ack_timeout=3,
            stop_ack_timeout=2,
            control_summary_limit=control_summary_limit,
            clock=clock,
            sleep=clock.sleep,
            progress=progress,
        )

    async def test_normal_order_and_total_payload_count(self) -> None:
        clock = FakeClock()
        transport = FakeCollectedTransport(successful_frames(), clock)

        result = await self.make_session(clock).run_collected(
            transport, completed_handshake()
        )

        self.assertEqual(
            transport.commands,
            [
                HeartRateCommand.STOP_HEAD,
                HeartRateCommand.CONNECT0,
                HeartRateCommand.CAPS0,
                HeartRateCommand.CONNECT4,
                HeartRateCommand.CAPS4,
                HeartRateCommand.HR_ON,
                HeartRateCommand.START_HR,
                HeartRateCommand.STOP_HR,
                HeartRateCommand.HR_OFF,
            ],
        )
        self.assertEqual(result.application_payloads_sent, 10)
        self.assertEqual(result.completion, HeartRateCompletion.TARGET_REACHED)
        self.assertTrue(result.stop_acknowledged)

    async def test_real_literal_acknowledgements_drive_full_session(self) -> None:
        clock = FakeClock(now=2)
        frames = [
            REAL_STOP_ACK_ONE_BYTE_ID,
            CONNECT4_ACK,
            REAL_START_ACK_TWO_BYTE_ID,
            *(heart_rate_packet(70 + index, index + 1) for index in range(5)),
            REAL_START_ACK_ONE_BYTE_ID,
        ]
        transport = FakeCollectedTransport(frames, clock)

        result = await self.make_session(clock).run_collected(
            transport, completed_handshake()
        )

        self.assertEqual(
            [report.bpm for report in result.samples],
            [70, 71, 72, 73, 74],
        )
        self.assertEqual(
            transport.commands,
            [
                HeartRateCommand.STOP_HEAD,
                HeartRateCommand.CONNECT0,
                HeartRateCommand.CAPS0,
                HeartRateCommand.CONNECT4,
                HeartRateCommand.CAPS4,
                HeartRateCommand.HR_ON,
                HeartRateCommand.START_HR,
                HeartRateCommand.STOP_HR,
                HeartRateCommand.HR_OFF,
            ],
        )
        self.assertTrue(result.stop_acknowledged)
        self.assertEqual(result.application_payloads_sent, 10)

    async def test_current_prefix_drives_complete_activation_without_retry(
        self,
    ) -> None:
        clock = FakeClock(now=2)
        frames = [
            observed_envelope(
                b"\x25", bytes.fromhex("10 03 62 02 08 10")
            ),
            observed_envelope(
                b"\x26", b"\x10\x03" + BOOTSTRAP_TAIL_11_12_13[2:]
            ),
            service_ack(0x0E, b"\x27", b"\x10\x03"),
            CONNECT4_ACK,
            service_ack(0x13, b"\x28", b"\x10\x03"),
            *(heart_rate_packet(70 + index, index + 1) for index in range(5)),
            service_ack(0x13, b"\x29", b"\x10\x03"),
        ]
        transport = FakeCollectedTransport(frames, clock)

        result = await self.make_session(clock).run_collected(
            transport, completed_handshake()
        )

        self.assertEqual(
            [report.bpm for report in result.samples],
            [70, 71, 72, 73, 74],
        )
        self.assertEqual(
            transport.commands,
            [
                HeartRateCommand.STOP_HEAD,
                HeartRateCommand.CONNECT0,
                HeartRateCommand.CAPS0,
                HeartRateCommand.CONNECT4,
                HeartRateCommand.CAPS4,
                HeartRateCommand.HR_ON,
                HeartRateCommand.START_HR,
                HeartRateCommand.STOP_HR,
                HeartRateCommand.HR_OFF,
            ],
        )
        self.assertTrue(result.stop_acknowledged)
        self.assertEqual(result.application_payloads_sent, 10)

    async def test_observed_control_prefixes_may_mix_per_frame(self) -> None:
        clock = FakeClock(now=2)
        frames = [
            service_ack(0x0E, b"\x27", b"\x10\x03"),
            CONNECT4_ACK,
            service_ack(0x13, b"\x28", b"\x10\x01"),
            *(heart_rate_packet(70 + index, index + 1) for index in range(5)),
            service_ack(0x13, b"\x29", b"\x10\x03"),
        ]
        transport = FakeCollectedTransport(frames, clock)

        result = await self.make_session(clock).run_collected(
            transport, completed_handshake()
        )

        self.assertEqual(len(result.samples), 5)
        self.assertTrue(result.stop_acknowledged)
        self.assertEqual(result.application_payloads_sent, 10)
        self.assertEqual(len(transport.commands), 9)
        self.assertEqual(
            transport.commands[-2:],
            [HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF],
        )

    async def test_minimum_bootstrap_window_is_measured_from_handshake_send(self) -> None:
        clock = FakeClock(now=0.25)
        transport = FakeCollectedTransport(successful_frames(), clock)

        await self.make_session(clock).run_collected(
            transport, completed_handshake(sent_at=0.0)
        )

        self.assertEqual(clock.sleeps, [1.25])
        self.assertGreaterEqual(transport.send_times[0], 1.5)

    async def test_late_descriptors_add_no_fixed_bootstrap_delay(self) -> None:
        clock = FakeClock(now=2.0)
        transport = FakeCollectedTransport(successful_frames(), clock)

        await self.make_session(clock).run_collected(
            transport, completed_handshake(sent_at=0.0)
        )

        self.assertEqual(clock.sleeps, [])
        self.assertEqual(transport.send_times[0], 2.0)

    async def test_five_reports_emit_five_existing_models(self) -> None:
        clock = FakeClock(now=2)
        emitted: list[HeartRateReport] = []

        def progress(event, report):
            if event is HeartRateProgress.SAMPLE:
                emitted.append(report)

        transport = FakeCollectedTransport(successful_frames(), clock)
        result = await self.make_session(clock, progress=progress).run_collected(
            transport, completed_handshake()
        )

        self.assertEqual(len(result.samples), 5)
        self.assertEqual(result.samples, tuple(emitted))
        self.assertTrue(all(isinstance(item, HeartRateReport) for item in emitted))
        self.assertEqual([item.bpm for item in emitted], [70, 71, 72, 73, 74])

    async def test_non_hr_and_malformed_hr_frames_do_not_count(self) -> None:
        clock = FakeClock(now=2)
        malformed = b"prefix" + HEART_RATE_MARKER + b"\x01\x48"
        frames = [
            service_ack(0x0E),
            CONNECT4_ACK,
            service_ack(0x13),
            b"unrelated AAP frame",
            malformed,
            heart_rate_packet(72, 1),
            service_ack(0x13),
        ]
        transport = FakeCollectedTransport(frames, clock)

        result = await self.make_session(clock, sample_target=1).run_collected(
            transport, completed_handshake()
        )

        self.assertEqual(len(result.samples), 1)
        self.assertEqual(result.non_hr_frames, 1)
        self.assertEqual(result.malformed_hr_frames, 1)

    async def test_zero_samples_is_a_typed_bounded_failure(self) -> None:
        clock = FakeClock(now=2)
        transport = FakeCollectedTransport(
            [service_ack(0x0E), CONNECT4_ACK, service_ack(0x13)], clock
        )

        with self.assertRaises(HeartRateNoSamplesError):
            await self.make_session(clock).run_collected(
                transport, completed_handshake()
            )

        self.assertEqual(
            transport.commands[-2:],
            [HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF],
        )
        self.assertGreaterEqual(clock.now, 14)

    async def test_partial_result_is_distinct_from_target(self) -> None:
        clock = FakeClock(now=2)
        transport = FakeCollectedTransport(successful_frames(2), clock)

        result = await self.make_session(clock).run_collected(
            transport, completed_handshake()
        )

        self.assertEqual(len(result.samples), 2)
        self.assertEqual(result.completion, HeartRateCompletion.PARTIAL)

    async def test_failure_before_hr_on_sends_no_hr_cleanup(self) -> None:
        clock = FakeClock(now=2)
        transport = FakeCollectedTransport([], clock)

        with self.assertRaises(HeartRateBootstrapAckTimeoutError):
            await self.make_session(clock).run_collected(
                transport, completed_handshake()
            )

        self.assertEqual(transport.commands, [HeartRateCommand.STOP_HEAD])

    async def test_stop_timeout_carries_bounded_safe_relative_diagnostics(
        self,
    ) -> None:
        clock = FakeClock(now=2)
        three_byte_identifier = service_ack(0x0E, b"\x81\x81\x01")
        transport = TimedFakeCollectedTransport(
            [
                (2.125, REAL_BOOTSTRAP_TAIL_10_ONE_BYTE_ID),
                (2.500, three_byte_identifier),
                (2.750, b"unrelated-private-body"),
            ],
            clock,
            pending_receive_frames=4,
        )

        with self.assertRaises(HeartRateBootstrapAckTimeoutError) as raised:
            await self.make_session(
                clock, control_summary_limit=2
            ).run_collected(transport, completed_handshake())

        error = raised.exception
        self.assertEqual(error.frames_observed, 3)
        self.assertEqual(error.frames_queued_before_stop_head, 4)
        self.assertEqual(error.application_payloads_sent, 2)
        self.assertEqual(len(error.summaries), 2)
        self.assertEqual(
            [summary.relative_to_stop_head_seconds for summary in error.summaries],
            [0.125, 0.5],
        )
        self.assertTrue(error.summaries[0].bootstrap_tail_10)
        self.assertEqual(error.summaries[1].candidate_identifier_octets, 3)
        self.assertFalse(
            error.summaries[1].identifier_is_current_canonical_1_or_2
        )
        self.assertEqual(transport.commands, [HeartRateCommand.STOP_HEAD])
        self.assertEqual(transport.send_times, [2])
        self.assertNotIn("unrelated-private-body", repr(error))

    async def test_connect_control_ack_timeout_is_typed_and_bounded(self) -> None:
        clock = FakeClock(now=2)
        transport = FakeCollectedTransport([service_ack(0x0E)], clock)

        with self.assertRaises(HeartRateConnectAckTimeoutError):
            await self.make_session(clock).run_collected(
                transport, completed_handshake()
            )

        self.assertEqual(clock.now, 5)
        self.assertNotIn(HeartRateCommand.HR_ON, transport.commands)

    async def test_failure_after_hr_on_before_start_sends_only_hr_off(self) -> None:
        clock = FakeClock(now=2)
        transport = FakeCollectedTransport(
            [service_ack(0x0E), CONNECT4_ACK],
            clock,
            fail_send={HeartRateCommand.START_HR},
        )

        with self.assertRaises(RuntimeError):
            await self.make_session(clock).run_collected(
                transport, completed_handshake()
            )

        self.assertEqual(
            transport.commands[-2:],
            [HeartRateCommand.HR_ON, HeartRateCommand.HR_OFF],
        )
        self.assertNotIn(HeartRateCommand.STOP_HR, transport.commands)

    async def test_start_timeout_attempts_stop_then_hr_off(self) -> None:
        clock = FakeClock(now=2)
        transport = FakeCollectedTransport(
            [service_ack(0x0E), CONNECT4_ACK], clock
        )

        with self.assertRaises(HeartRateStartAckTimeoutError):
            await self.make_session(clock).run_collected(
                transport, completed_handshake()
            )

        self.assertEqual(
            transport.commands[-2:],
            [HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF],
        )

    async def test_stop_ack_timeout_still_sends_hr_off(self) -> None:
        clock = FakeClock(now=2)
        transport = FakeCollectedTransport(
            successful_frames(include_stop_ack=False), clock
        )

        result = await self.make_session(clock).run_collected(
            transport, completed_handshake()
        )

        self.assertFalse(result.stop_acknowledged)
        self.assertEqual(
            transport.commands[-2:],
            [HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF],
        )

    async def test_primary_failure_is_not_masked_by_cleanup_failure(self) -> None:
        clock = FakeClock(now=2)
        transport = FakeCollectedTransport(
            [service_ack(0x0E), CONNECT4_ACK, service_ack(0x13)],
            clock,
            fail_send={HeartRateCommand.HR_OFF},
        )

        with self.assertRaises(HeartRateNoSamplesError) as raised:
            await self.make_session(clock).run_collected(
                transport, completed_handshake()
            )

        self.assertTrue(any("cleanup" in note for note in raised.exception.__notes__))

    async def test_stop_send_failure_still_attempts_hr_off(self) -> None:
        clock = FakeClock(now=2)
        transport = FakeCollectedTransport(
            successful_frames(),
            clock,
            fail_send={HeartRateCommand.STOP_HR},
        )

        with self.assertRaises(HeartRateCleanupError):
            await self.make_session(clock).run_collected(
                transport, completed_handshake()
            )

        self.assertIn(HeartRateCommand.HR_OFF, transport.commands)

    async def test_cancellation_after_start_attempts_both_cleanup_commands(self) -> None:
        clock = FakeClock(now=2)
        transport = FakeCollectedTransport(
            [
                service_ack(0x0E),
                CONNECT4_ACK,
                service_ack(0x13),
                asyncio.CancelledError(),
            ],
            clock,
        )

        with self.assertRaises(asyncio.CancelledError):
            await self.make_session(clock).run_collected(
                transport, completed_handshake()
            )

        self.assertEqual(
            transport.commands[-2:],
            [HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF],
        )

    async def test_ack_wait_is_bounded(self) -> None:
        clock = FakeClock(now=2)
        transport = FakeCollectedTransport([], clock)

        with self.assertRaises(HeartRateBootstrapAckTimeoutError):
            await self.make_session(clock).run_collected(
                transport, completed_handshake()
            )

        self.assertEqual(clock.now, 5)


class HeartRateMonitorActivationTests(unittest.IsolatedAsyncioTestCase):
    def make_session(
        self,
        clock: FakeClock,
        *,
        progress=None,
        receive_poll_interval: float = 0.5,
    ) -> HeartRateMonitorActivationSession:
        return HeartRateMonitorActivationSession(
            receive_poll_interval=receive_poll_interval,
            control_ack_timeout=3,
            stop_ack_timeout=2,
            clock=clock,
            sleep=clock.sleep,
            progress=progress,
        )

    async def test_normal_continuous_flow_stops_from_sample_callback(self) -> None:
        clock = FakeClock()
        stop_event = asyncio.Event()
        reports: list[HeartRateReport] = []

        def progress(event, report):
            if event is HeartRateProgress.SAMPLE and report is not None:
                reports.append(report)
                if len(reports) == 3:
                    stop_event.set()

        transport = FakeCollectedTransport(successful_frames(3), clock)
        result = await self.make_session(clock, progress=progress).run_collected(
            transport, completed_handshake(), stop_event
        )

        self.assertEqual(result.samples_observed, 3)
        self.assertEqual([report.bpm for report in reports], [70, 71, 72])
        self.assertEqual(result.application_payloads_sent, 10)
        self.assertTrue(result.stop_acknowledged)
        self.assertEqual(
            transport.commands,
            [
                HeartRateCommand.STOP_HEAD,
                HeartRateCommand.CONNECT0,
                HeartRateCommand.CAPS0,
                HeartRateCommand.CONNECT4,
                HeartRateCommand.CAPS4,
                HeartRateCommand.HR_ON,
                HeartRateCommand.START_HR,
                HeartRateCommand.STOP_HR,
                HeartRateCommand.HR_OFF,
            ],
        )

    async def test_result_has_only_bounded_status_fields(self) -> None:
        clock = FakeClock(now=2)
        stop_event = asyncio.Event()
        observed = 0

        def progress(event, report):
            nonlocal observed
            if event is HeartRateProgress.SAMPLE and report is not None:
                observed += 1
                if observed == 256:
                    stop_event.set()

        frames = [service_ack(0x0E), CONNECT4_ACK, service_ack(0x13)]
        frames.extend(
            heart_rate_packet(60 + (index % 100), index + 1)
            for index in range(256)
        )
        frames.append(service_ack(0x13))
        transport = FakeCollectedTransport(frames, clock)
        result = await self.make_session(clock, progress=progress).run_collected(
            transport, completed_handshake(), stop_event
        )

        self.assertEqual(result.samples_observed, 256)
        self.assertEqual(
            {field.name for field in fields(HeartRateMonitorSessionResult)},
            {
                "samples_observed",
                "stop_acknowledged",
                "application_payloads_sent",
                "control_frames_observed",
                "non_hr_frames",
                "malformed_hr_frames",
            },
        )
        self.assertFalse(
            any(
                isinstance(value, (HeartRateReport, list, tuple))
                for value in (
                    getattr(result, field.name)
                    for field in fields(HeartRateMonitorSessionResult)
                )
            )
        )

    async def test_idle_receive_timeouts_do_not_end_monitor(self) -> None:
        clock = FakeClock(now=2)
        stop_event = asyncio.Event()

        def progress(event, report):
            if event is HeartRateProgress.SAMPLE and report is not None:
                stop_event.set()

        frames = [
            service_ack(0x0E),
            CONNECT4_ACK,
            service_ack(0x13),
            TimeoutError(),
            TimeoutError(),
            heart_rate_packet(74, 1),
            service_ack(0x13),
        ]
        transport = FakeCollectedTransport(frames, clock)
        result = await self.make_session(clock, progress=progress).run_collected(
            transport, completed_handshake(), stop_event
        )

        self.assertEqual(result.samples_observed, 1)
        self.assertTrue(result.stop_acknowledged)

    async def test_pre_set_stop_skips_observation_and_cleans_up(self) -> None:
        class TimeoutRecordingTransport(FakeCollectedTransport):
            def __init__(self, frames, clock):
                super().__init__(frames, clock)
                self.receive_timeouts: list[float] = []

            async def receive(self, timeout: float) -> bytes:
                self.receive_timeouts.append(timeout)
                return await super().receive(timeout)

        clock = FakeClock(now=2)
        stop_event = asyncio.Event()
        stop_event.set()
        transport = TimeoutRecordingTransport(
            [service_ack(0x0E), CONNECT4_ACK, service_ack(0x13), service_ack(0x13)],
            clock,
        )

        result = await self.make_session(clock).run_collected(
            transport, completed_handshake(), stop_event
        )

        self.assertEqual(result.samples_observed, 0)
        self.assertEqual(transport.receive_timeouts, [3, 3, 3, 2])
        self.assertEqual(
            transport.commands[-2:],
            [HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF],
        )

    async def test_non_hr_and_malformed_frames_are_counted(self) -> None:
        clock = FakeClock(now=2)
        stop_event = asyncio.Event()
        malformed = b"prefix" + HEART_RATE_MARKER + b"\x01\x48"

        def progress(event, report):
            if event is HeartRateProgress.SAMPLE and report is not None:
                stop_event.set()

        transport = FakeCollectedTransport(
            [
                service_ack(0x0E),
                CONNECT4_ACK,
                service_ack(0x13),
                b"unrelated AAP frame",
                malformed,
                heart_rate_packet(72, 1),
                service_ack(0x13),
            ],
            clock,
        )

        result = await self.make_session(clock, progress=progress).run_collected(
            transport, completed_handshake(), stop_event
        )

        self.assertEqual(result.non_hr_frames, 1)
        self.assertEqual(result.malformed_hr_frames, 1)
        self.assertEqual(result.samples_observed, 1)

    async def test_missing_stop_ack_is_a_successful_false_result(self) -> None:
        clock = FakeClock(now=2)
        stop_event = asyncio.Event()

        def progress(event, report):
            if event is HeartRateProgress.SAMPLE and report is not None:
                stop_event.set()

        transport = FakeCollectedTransport(
            successful_frames(1, include_stop_ack=False), clock
        )
        result = await self.make_session(clock, progress=progress).run_collected(
            transport, completed_handshake(), stop_event
        )

        self.assertFalse(result.stop_acknowledged)
        self.assertEqual(result.application_payloads_sent, 10)
        self.assertEqual(
            transport.commands[-2:],
            [HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF],
        )

    async def test_stop_send_failure_still_attempts_hr_off(self) -> None:
        class AttemptTrackingTransport(FakeCollectedTransport):
            def __init__(self, frames, clock):
                super().__init__(
                    frames,
                    clock,
                    fail_send={HeartRateCommand.STOP_HR},
                )
                self.attempts: list[HeartRateCommand] = []

            def send_heart_rate_command(self, command):
                self.attempts.append(command)
                super().send_heart_rate_command(command)

        clock = FakeClock(now=2)
        stop_event = asyncio.Event()
        stop_event.set()
        transport = AttemptTrackingTransport(
            [service_ack(0x0E), CONNECT4_ACK, service_ack(0x13)], clock
        )

        with self.assertRaises(HeartRateCleanupError):
            await self.make_session(clock).run_collected(
                transport, completed_handshake(), stop_event
            )

        self.assertEqual(transport.attempts.count(HeartRateCommand.STOP_HR), 1)
        self.assertEqual(transport.attempts.count(HeartRateCommand.HR_OFF), 1)

    async def test_receive_failure_preserves_primary_after_cleanup(self) -> None:
        class MonitorReceiveError(RuntimeError):
            pass

        class AttemptTrackingTransport(FakeCollectedTransport):
            def __init__(self, frames, clock):
                super().__init__(
                    frames,
                    clock,
                    fail_send={HeartRateCommand.HR_OFF},
                )
                self.attempts: list[HeartRateCommand] = []

            def send_heart_rate_command(self, command):
                self.attempts.append(command)
                super().send_heart_rate_command(command)

        clock = FakeClock(now=2)
        transport = AttemptTrackingTransport(
            [
                service_ack(0x0E),
                CONNECT4_ACK,
                service_ack(0x13),
                MonitorReceiveError("synthetic monitor receive failure"),
                service_ack(0x13),
            ],
            clock,
        )

        with self.assertRaises(MonitorReceiveError) as raised:
            await self.make_session(clock).run_collected(
                transport, completed_handshake(), asyncio.Event()
            )

        self.assertEqual(transport.attempts.count(HeartRateCommand.STOP_HR), 1)
        self.assertEqual(transport.attempts.count(HeartRateCommand.HR_OFF), 1)
        self.assertTrue(any("cleanup" in note for note in raised.exception.__notes__))

    async def test_progress_callback_failure_preserves_primary(self) -> None:
        class ProgressError(RuntimeError):
            pass

        clock = FakeClock(now=2)

        def progress(event, report):
            if event is HeartRateProgress.SAMPLE and report is not None:
                raise ProgressError("synthetic progress failure")

        transport = FakeCollectedTransport(successful_frames(1), clock)
        with self.assertRaises(ProgressError):
            await self.make_session(clock, progress=progress).run_collected(
                transport, completed_handshake(), asyncio.Event()
            )

        self.assertEqual(
            transport.commands[-2:],
            [HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF],
        )

    async def test_cancellation_during_receive_cleans_up_and_propagates(self) -> None:
        class BlockingMonitorTransport(FakeCollectedTransport):
            def __init__(self, frames, clock):
                super().__init__(frames, clock)
                self.monitor_receive_entered = asyncio.Event()
                self.monitor_receive_cancelled = False

            async def receive(self, timeout: float) -> bytes:
                if self.frames:
                    return await super().receive(timeout)
                if not self.monitor_receive_cancelled:
                    self.monitor_receive_entered.set()
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        self.monitor_receive_cancelled = True
                        raise
                return service_ack(0x13)

        clock = FakeClock(now=2)
        transport = BlockingMonitorTransport(
            [service_ack(0x0E), CONNECT4_ACK, service_ack(0x13)], clock
        )
        task = asyncio.create_task(
            self.make_session(clock).run_collected(
                transport, completed_handshake(), asyncio.Event()
            )
        )
        await transport.monitor_receive_entered.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(
            transport.commands[-2:],
            [HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF],
        )
        self.assertEqual(transport.commands.count(HeartRateCommand.STOP_HR), 1)
        self.assertEqual(transport.commands.count(HeartRateCommand.HR_OFF), 1)

    async def test_cancellation_during_stop_ack_still_attempts_hr_off(self) -> None:
        class BlockingStopAckTransport(FakeCollectedTransport):
            def __init__(self, frames, clock):
                super().__init__(frames, clock)
                self.stop_ack_wait_entered = asyncio.Event()

            async def receive(self, timeout: float) -> bytes:
                if self.frames:
                    return await super().receive(timeout)
                self.stop_ack_wait_entered.set()
                await asyncio.Future()
                raise AssertionError("unreachable")

        clock = FakeClock(now=2)
        stop_event = asyncio.Event()

        def progress(event, report):
            if event is HeartRateProgress.SAMPLE and report is not None:
                stop_event.set()

        transport = BlockingStopAckTransport(
            [
                service_ack(0x0E),
                CONNECT4_ACK,
                service_ack(0x13),
                heart_rate_packet(72, 1),
            ],
            clock,
        )
        task = asyncio.create_task(
            self.make_session(clock, progress=progress).run_collected(
                transport, completed_handshake(), stop_event
            )
        )
        await transport.stop_ack_wait_entered.wait()
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(transport.commands.count(HeartRateCommand.STOP_HR), 1)
        self.assertEqual(transport.commands.count(HeartRateCommand.HR_OFF), 1)
        self.assertEqual(transport.commands[-1], HeartRateCommand.HR_OFF)

    async def test_monitor_activation_is_single_use(self) -> None:
        clock = FakeClock(now=2)
        stop_event = asyncio.Event()
        stop_event.set()
        session = self.make_session(clock)
        transport = FakeCollectedTransport(
            [service_ack(0x0E), CONNECT4_ACK, service_ack(0x13), service_ack(0x13)],
            clock,
        )
        await session.run_collected(transport, completed_handshake(), stop_event)
        second_transport = FakeCollectedTransport([], clock)

        with self.assertRaises(HeartRateStateError):
            await session.run_collected(
                second_transport, completed_handshake(), stop_event
            )

    def test_receive_poll_interval_validation(self) -> None:
        clock = FakeClock()
        for value in (0, -0.1, 5.01):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.make_session(clock, receive_poll_interval=value)
        for value in (0.001, 0.5, 5.0):
            with self.subTest(value=value):
                self.make_session(clock, receive_poll_interval=value)


class ParserReuseTests(unittest.TestCase):
    def test_marker_parser_accepts_40_and_41_byte_outer_packets(self) -> None:
        first = parse_heart_rate_packet(heart_rate_packet(68, 1, outer_length=40))
        second = parse_heart_rate_packet(heart_rate_packet(69, 2, outer_length=41))

        self.assertEqual((first.bpm, second.bpm), (68, 69))
        self.assertEqual((first.sequence, second.sequence), (1, 2))


