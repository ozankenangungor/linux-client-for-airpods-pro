"""Hardware-independent tests for the heart-rate session."""

from __future__ import annotations

import asyncio
import unittest


from types import SimpleNamespace


from airpods_hr.aap import AAP_HANDSHAKE_REQUEST, BumbleAAPTransport


from airpods_hr.heart_rate_session import CONNECT4_ACK, is_connect4_ack, is_observed_service_ack


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


