"""Frozen parent implementation for test-only differential checks."""
from __future__ import annotations
from dataclasses import dataclass
from airpods_hr.protocol import HEART_RATE_MARKER, HEART_RATE_SERVICE_ID

CONNECT4_ACK = bytes.fromhex('01 00 04 00 85 00 01 00 03 00 00 00 00 00 00 00 00 00')

_SERVICE_ACK_PREFIX = bytes.fromhex('04 00 04 00 17 00 00 00')

_SERVICE_ACK_SUFFIX_PREFIX = bytes.fromhex('10 01 4a 02 08')

_SUPPORTED_ACK_SERVICES = frozenset((14, HEART_RATE_SERVICE_ID))

_OBSERVED_CONTROL_PREFIXES = (b'\x10\x01', b'\x10\x03')

_SERVICE_ACK_REMAINDER_PREFIX = bytes.fromhex('4a 02 08')

_BOOTSTRAP_REMAINDER_10 = bytes.fromhex('62 02 08 10')

_BOOTSTRAP_REMAINDER_11_12_13 = bytes.fromhex('62 02 08 11 62 02 08 12 62 02 08 13')

_BOOTSTRAP_TAIL_10 = _OBSERVED_CONTROL_PREFIXES[0] + _BOOTSTRAP_REMAINDER_10

_BOOTSTRAP_TAIL_11_12_13 = _OBSERVED_CONTROL_PREFIXES[0] + _BOOTSTRAP_REMAINDER_11_12_13

@dataclass(frozen=True, slots=True)
class ControlFrameSummary:
    """Allowlisted structure and relative timing without received bytes."""
    length: int
    header_u16_2_3: int | None
    header_u16_4_5: int | None
    word_u16_8_9: int | None
    word_u16_10_11: int | None
    outer_service_envelope_match: bool
    fixed_word_10_00_match: bool
    trailing_length_consistent: bool | None
    tag_08_at_offset_12: bool | None
    service_ack_suffix_0e: bool
    service_ack_suffix_13: bool
    candidate_identifier_terminated: bool | None
    candidate_identifier_octets: int | None
    candidate_identifier_canonical: bool | None
    identifier_is_current_canonical_1_or_2: bool | None
    post_identifier_length: int | None
    post_identifier_prefix_octet_0: int | None
    post_identifier_prefix_octet_1: int | None
    post_identifier_starts_10_01: bool | None
    post_identifier_prefix_is_observed: bool | None
    post_identifier_field_tag: int | None
    post_identifier_field_parameter: int | None
    remainder_is_ack_0e_shape: bool | None
    remainder_is_ack_13_shape: bool | None
    remainder_is_bootstrap_10_shape: bool | None
    remainder_is_bootstrap_11_12_13_shape: bool | None
    terminal_tag_08: bool
    terminal_value: int | None
    observed_62_02_08_group_count: int | None
    observed_62_02_08_terminal_values: tuple[int, ...] | None
    observed_62_02_08_group_offsets: tuple[int, ...] | None
    bootstrap_tail_10_suffix_present: bool
    bootstrap_tail_11_12_13_suffix_present: bool
    bootstrap_tail_10: bool
    bootstrap_tail_11_12_13: bool
    heart_rate_marker_present: bool
    relative_to_stop_head_seconds: float | None = None

    @classmethod
    def from_frame(cls, frame: bytes, *, relative_to_stop_head_seconds: float | None=None) -> ControlFrameSummary:
        """Derive a summary without retaining any part of ``frame``."""
        if not isinstance(frame, bytes):
            raise TypeError('control frame must be bytes')
        envelope_match = frame[:8] == _SERVICE_ACK_PREFIX
        fixed_word_match = len(frame) >= 10 and frame[8:10] == b'\x10\x00'
        trailing_length_consistent = int.from_bytes(frame[10:12], 'little') == len(frame) - 12 if len(frame) >= 12 else None
        tag_08 = frame[12] == 8 if len(frame) >= 13 else None
        suffix_0e = frame.endswith(_SERVICE_ACK_SUFFIX_PREFIX + b'\x0e')
        suffix_13 = frame.endswith(_SERVICE_ACK_SUFFIX_PREFIX + b'\x13')
        candidate_shape = envelope_match and fixed_word_match and (trailing_length_consistent is True) and (tag_08 is True)
        identifier_terminated: bool | None = None
        identifier_octets: int | None = None
        identifier_canonical: bool | None = None
        identifier_is_current_canonical: bool | None = None
        post_identifier: bytes | None = None
        if candidate_shape:
            identifier_terminated = False
            identifier_octets = min(5, max(0, len(frame) - 13))
            identifier_canonical = False
            identifier_is_current_canonical = False
            for offset in range(identifier_octets):
                if frame[13 + offset] < 128:
                    identifier_terminated = True
                    identifier_octets = offset + 1
                    candidate_identifier = frame[13:13 + identifier_octets]
                    identifier_canonical = _is_canonical_candidate_identifier(candidate_identifier)
                    identifier_is_current_canonical = _is_canonical_one_or_two_byte_varint(candidate_identifier)
                    post_identifier = frame[13 + identifier_octets:]
                    break
        post_identifier_length = len(post_identifier) if post_identifier is not None else None
        has_post_identifier_prefix = post_identifier is not None and len(post_identifier) >= 2
        post_identifier_prefix_octet_0 = post_identifier[0] if has_post_identifier_prefix else None
        post_identifier_prefix_octet_1 = post_identifier[1] if has_post_identifier_prefix else None
        post_identifier_starts_10_01 = post_identifier.startswith(b'\x10\x01') if post_identifier is not None else None
        post_identifier_prefix_is_observed = post_identifier[:2] in _OBSERVED_CONTROL_PREFIXES if has_post_identifier_prefix else None
        post_identifier_field_tag = post_identifier[2] if post_identifier_starts_10_01 and len(post_identifier) >= 3 else None
        post_identifier_field_parameter = post_identifier[3] if post_identifier_starts_10_01 and len(post_identifier) >= 4 else None
        post_identifier_remainder = post_identifier[2:] if has_post_identifier_prefix else None
        remainder_is_ack_0e_shape = post_identifier_remainder == b'J\x02\x08\x0e' if post_identifier_remainder is not None else None
        remainder_is_ack_13_shape = post_identifier_remainder == b'J\x02\x08\x13' if post_identifier_remainder is not None else None
        remainder_is_bootstrap_10_shape = post_identifier_remainder == b'b\x02\x08\x10' if post_identifier_remainder is not None else None
        remainder_is_bootstrap_11_12_13_shape = post_identifier_remainder == b'b\x02\x08\x11b\x02\x08\x12b\x02\x08\x13' if post_identifier_remainder is not None else None
        group_count: int | None = None
        group_values: tuple[int, ...] | None = None
        group_offsets: tuple[int, ...] | None = None
        if post_identifier is not None:
            group_count = 0
            bounded_values: list[int] = []
            bounded_offsets: list[int] = []
            offset = 0
            while offset + 4 <= len(post_identifier):
                if post_identifier[offset:offset + 3] == b'b\x02\x08':
                    group_count += 1
                    if group_count <= 4:
                        bounded_values.append(post_identifier[offset + 3])
                        bounded_offsets.append(offset)
                    offset += 4
                else:
                    offset += 1
            group_offsets = tuple(bounded_offsets)
            if group_count <= 4:
                group_values = tuple(bounded_values)
        terminal_tag_08 = len(frame) >= 2 and frame[-2] == 8
        terminal_value = frame[-1] if terminal_tag_08 else None
        bootstrap_tail_10_suffix_present = frame.endswith(_BOOTSTRAP_TAIL_10)
        bootstrap_tail_11_12_13_suffix_present = frame.endswith(_BOOTSTRAP_TAIL_11_12_13)
        return cls(length=len(frame), header_u16_2_3=int.from_bytes(frame[2:4], 'little') if len(frame) >= 4 else None, header_u16_4_5=int.from_bytes(frame[4:6], 'little') if len(frame) >= 6 else None, word_u16_8_9=int.from_bytes(frame[8:10], 'little') if len(frame) >= 10 else None, word_u16_10_11=int.from_bytes(frame[10:12], 'little') if len(frame) >= 12 else None, outer_service_envelope_match=envelope_match, fixed_word_10_00_match=fixed_word_match, trailing_length_consistent=trailing_length_consistent, tag_08_at_offset_12=tag_08, service_ack_suffix_0e=suffix_0e, service_ack_suffix_13=suffix_13, candidate_identifier_terminated=identifier_terminated, candidate_identifier_octets=identifier_octets, candidate_identifier_canonical=identifier_canonical, identifier_is_current_canonical_1_or_2=identifier_is_current_canonical, post_identifier_length=post_identifier_length, post_identifier_prefix_octet_0=post_identifier_prefix_octet_0, post_identifier_prefix_octet_1=post_identifier_prefix_octet_1, post_identifier_starts_10_01=post_identifier_starts_10_01, post_identifier_prefix_is_observed=post_identifier_prefix_is_observed, post_identifier_field_tag=post_identifier_field_tag, post_identifier_field_parameter=post_identifier_field_parameter, remainder_is_ack_0e_shape=remainder_is_ack_0e_shape, remainder_is_ack_13_shape=remainder_is_ack_13_shape, remainder_is_bootstrap_10_shape=remainder_is_bootstrap_10_shape, remainder_is_bootstrap_11_12_13_shape=remainder_is_bootstrap_11_12_13_shape, terminal_tag_08=terminal_tag_08, terminal_value=terminal_value, observed_62_02_08_group_count=group_count, observed_62_02_08_terminal_values=group_values, observed_62_02_08_group_offsets=group_offsets, bootstrap_tail_10_suffix_present=bootstrap_tail_10_suffix_present, bootstrap_tail_11_12_13_suffix_present=bootstrap_tail_11_12_13_suffix_present, bootstrap_tail_10=_matches_observed_bootstrap_tail(frame, _BOOTSTRAP_REMAINDER_10), bootstrap_tail_11_12_13=_matches_observed_bootstrap_tail(frame, _BOOTSTRAP_REMAINDER_11_12_13), heart_rate_marker_present=HEART_RATE_MARKER in frame, relative_to_stop_head_seconds=max(0.0, relative_to_stop_head_seconds) if relative_to_stop_head_seconds is not None else None)

def _is_canonical_one_or_two_byte_varint(value: bytes) -> bool:
    if len(value) == 1:
        return value[0] < 128
    if len(value) == 2:
        return value[0] >= 128 and value[1] < 128 and (value[1] != 0)
    return False

def _is_canonical_candidate_identifier(value: bytes) -> bool:
    """Check the observed candidate encoding without retaining its value."""
    if not 1 <= len(value) <= 5:
        return False
    if any((octet < 128 for octet in value[:-1])) or value[-1] >= 128:
        return False
    return len(value) == 1 or value[-1] != 0

def _has_observed_control_body(frame: bytes, remainder: bytes) -> bool:
    for identifier_octets in (1, 2):
        identifier_end = 13 + identifier_octets
        if not _is_canonical_one_or_two_byte_varint(frame[13:identifier_end]):
            continue
        if any((frame[identifier_end:] == prefix + remainder for prefix in _OBSERVED_CONTROL_PREFIXES)):
            return True
    return False

def _matches_observed_bootstrap_tail(frame: bytes, remainder: bytes) -> bool:
    if len(frame) < 13 + 1 + 2 + len(remainder):
        return False
    if frame[:8] != _SERVICE_ACK_PREFIX or frame[8:10] != b'\x10\x00':
        return False
    if int.from_bytes(frame[10:12], 'little') != len(frame) - 12:
        return False
    if frame[12] != 8:
        return False
    return _has_observed_control_body(frame, remainder)

def is_observed_service_ack(frame: bytes, service_id: int) -> bool:
    """Recognize the observed ACK shape while leaving its identifier variable."""
    if service_id not in _SUPPORTED_ACK_SERVICES or not isinstance(frame, bytes):
        return False
    if len(frame) < 20 or not frame.startswith(_SERVICE_ACK_PREFIX):
        return False
    if frame[8:10] != b'\x10\x00':
        return False
    trailing_length = int.from_bytes(frame[10:12], 'little')
    if trailing_length != len(frame) - 12:
        return False
    if frame[12] != 8:
        return False
    remainder = _SERVICE_ACK_REMAINDER_PREFIX + bytes((service_id,))
    return _has_observed_control_body(frame, remainder)

def is_connect4_ack(frame: bytes) -> bool:
    """Match the exact connect acknowledgement observed in proven runs."""
    return isinstance(frame, bytes) and frame == CONNECT4_ACK
