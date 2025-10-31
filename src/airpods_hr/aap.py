"""Minimal, bounded AAP handshake and descriptor-observation layer."""

from __future__ import annotations


import re
from collections.abc import Callable

from dataclasses import dataclass
from enum import StrEnum


AAP_HANDSHAKE_REQUEST = bytes.fromhex(
    "00 00 04 00 01 00 02 00 00 00 00 00 00 00 00 00"
)
AAP_HANDSHAKE_ACK = bytes.fromhex(
    "01 00 04 00 00 00 01 00 03 00 00 00 00 00 00 00 00 00"
)

SENSOR_FRAMEWORK_MARKERS: tuple[bytes, ...] = (
    b"AccessoryService",
    b"devmotion6",
    b"MaxReportSize",
    b"ReportDescriptor",
)
HEART_RATE_SERVICE_MARKER = b"HeartRateService"
HEART_RATE_ACCESS_MARKER = b"com.apple.hid.heartrate-access"
_HEART_RATE_MARKER = re.compile(rb"(?<![A-Za-z0-9])HeartRate(?![A-Za-z0-9])")
AAP_FRAME_SUMMARY_LIMIT = 64
AAP_TYPE_2B_BODY_OFFSET = 17
AAP_TYPE_2B_UNIT_SIZE = 17
AAP_TYPE_2B_SUFFIX_HISTOGRAM_LIMIT = 8


class AAPProgress(StrEnum):
    SDP_INSTALLED = "sdp_installed"
    HANDSHAKE_SENT = "handshake_sent"
    ACK_OBSERVED = "ack_observed"
    DESCRIPTORS_OBSERVED = "descriptors_observed"


@dataclass(frozen=True, slots=True)
class DescriptorEvidence:
    """Non-sensitive booleans derived from descriptor traffic."""

    sensor_framework: bool = False
    heart_rate_service: bool = False
    heart_rate: bool = False
    heartrate_access: bool = False

    def merged(self, frame: bytes) -> DescriptorEvidence:
        return DescriptorEvidence(
            sensor_framework=self.sensor_framework
            or any(marker in frame for marker in SENSOR_FRAMEWORK_MARKERS),
            heart_rate_service=self.heart_rate_service
            or HEART_RATE_SERVICE_MARKER in frame,
            heart_rate=self.heart_rate or _HEART_RATE_MARKER.search(frame) is not None,
            heartrate_access=self.heartrate_access
            or HEART_RATE_ACCESS_MARKER in frame,
        )

    @property
    def required(self) -> bool:
        return self.sensor_framework and self.heart_rate_service


@dataclass(frozen=True, slots=True)
class RecordSuffixSummary:
    """Count one allowlisted pair from an observed 17-byte unit suffix."""

    suffix_field_u8: int
    suffix_field_u16: int
    count: int


@dataclass(frozen=True, slots=True)
class AAPType2BFrameSummary:
    """Bounded structural metadata for an observed type-0x002B frame."""

    frame_length: int
    header_u8_6: int | None
    declared_body_length_u16_7_8: int | None
    actual_body_length_after_offset_17: int | None
    declared_body_length_consistent: bool | None
    body_aligned_to_17_bytes: bool | None
    record_count_17: int | None
    record_suffix_distinct_count: int | None
    record_suffix_histogram: tuple[RecordSuffixSummary, ...]
    unit_bytes_8_13_uniform: bool | None

    @classmethod
    def from_frame(cls, frame: bytes) -> AAPType2BFrameSummary:
        frame_length = len(frame)
        header_u8_6 = frame[6] if frame_length >= 7 else None
        declared_body_length = (
            frame[7] | (frame[8] << 8) if frame_length >= 9 else None
        )
        actual_body_length = (
            frame_length - AAP_TYPE_2B_BODY_OFFSET
            if frame_length >= AAP_TYPE_2B_BODY_OFFSET
            else None
        )
        length_consistent = (
            declared_body_length == actual_body_length
            if declared_body_length is not None
            and actual_body_length is not None
            else None
        )
        body_aligned = (
            actual_body_length % AAP_TYPE_2B_UNIT_SIZE == 0
            if actual_body_length is not None
            else None
        )

        record_count: int | None = None
        distinct_count: int | None = None
        histogram: tuple[RecordSuffixSummary, ...] = ()
        hidden_field_uniform: bool | None = None
        if length_consistent is True and body_aligned is True:
            record_count = actual_body_length // AAP_TYPE_2B_UNIT_SIZE
            suffix_counts: dict[tuple[int, int], int] = {}
            for record_index in range(record_count):
                unit_start = (
                    AAP_TYPE_2B_BODY_OFFSET
                    + record_index * AAP_TYPE_2B_UNIT_SIZE
                )
                suffix_u8 = frame[unit_start + 14]
                suffix_u16 = (
                    frame[unit_start + 15]
                    | (frame[unit_start + 16] << 8)
                )
                suffix_key = (suffix_u8, suffix_u16)
                suffix_counts[suffix_key] = suffix_counts.get(suffix_key, 0) + 1

            distinct_count = len(suffix_counts)
            histogram = tuple(
                RecordSuffixSummary(suffix_u8, suffix_u16, count)
                for (suffix_u8, suffix_u16), count in sorted(
                    suffix_counts.items()
                )[:AAP_TYPE_2B_SUFFIX_HISTOGRAM_LIMIT]
            )
            if record_count:
                first_hidden_start = AAP_TYPE_2B_BODY_OFFSET + 8
                hidden_field_uniform = all(
                    frame[
                        AAP_TYPE_2B_BODY_OFFSET
                        + record_index * AAP_TYPE_2B_UNIT_SIZE
                        + 8
                        + byte_index
                    ]
                    == frame[first_hidden_start + byte_index]
                    for record_index in range(1, record_count)
                    for byte_index in range(6)
                )

        return cls(
            frame_length=frame_length,
            header_u8_6=header_u8_6,
            declared_body_length_u16_7_8=declared_body_length,
            actual_body_length_after_offset_17=actual_body_length,
            declared_body_length_consistent=length_consistent,
            body_aligned_to_17_bytes=body_aligned,
            record_count_17=record_count,
            record_suffix_distinct_count=distinct_count,
            record_suffix_histogram=histogram,
            unit_bytes_8_13_uniform=hidden_field_uniform,
        )


@dataclass(frozen=True, slots=True)
class AAPFrameSummary:
    """Allowlisted AAP frame shape with no payload content."""

    length: int
    header_u16_2_3: int | None = None
    header_u16_4_5: int | None = None
    type_2b_summary: AAPType2BFrameSummary | None = None

    @classmethod
    def from_frame(cls, frame: bytes) -> AAPFrameSummary:
        header_u16_4_5 = (
            int.from_bytes(frame[4:6], "little")
            if len(frame) >= 6
            else None
        )
        return cls(
            length=len(frame),
            header_u16_2_3=(
                int.from_bytes(frame[2:4], "little")
                if len(frame) >= 4
                else None
            ),
            header_u16_4_5=header_u16_4_5,
            type_2b_summary=(
                AAPType2BFrameSummary.from_frame(frame)
                if header_u16_4_5 == 0x002B
                else None
            ),
        )


Clock = Callable[[], float]
AAPProgressCallback = Callable[[AAPProgress], None]


