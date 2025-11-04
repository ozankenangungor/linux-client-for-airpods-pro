"""Bounded activation and observation of the proven AAP heart-rate stream."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from time import monotonic
from typing import Protocol

from airpods_hr.aap import AAPHandshakeResult


from airpods_hr.heartrate import (
    HeartRateMarkerNotFoundError,
    HeartRateParseError,
    HeartRateReport,
    parse_heart_rate_packet,
)
from airpods_hr.protocol import (
    HEART_RATE_MARKER,
    HEART_RATE_SERVICE_ID,
    HeartRateCommand,
)


MINIMUM_BOOTSTRAP_SECONDS = 1.5
DEFAULT_SAMPLE_TARGET = 5
DEFAULT_STREAM_TIMEOUT = 12.0
DEFAULT_CONTROL_ACK_TIMEOUT = 3.0
DEFAULT_STOP_ACK_TIMEOUT = 2.0
DEFAULT_CONTROL_SUMMARY_LIMIT = 8

CONNECT4_ACK = bytes.fromhex(
    "01 00 04 00 85 00 01 00 03 00 00 00 00 00 00 00 00 00"
)

_SERVICE_ACK_PREFIX = bytes.fromhex("04 00 04 00 17 00 00 00")
_SERVICE_ACK_SUFFIX_PREFIX = bytes.fromhex("10 01 4a 02 08")
_SUPPORTED_ACK_SERVICES = frozenset((0x0E, HEART_RATE_SERVICE_ID))
_OBSERVED_CONTROL_PREFIXES = (b"\x10\x01", b"\x10\x03")
_SERVICE_ACK_REMAINDER_PREFIX = bytes.fromhex("4a 02 08")
_BOOTSTRAP_REMAINDER_10 = bytes.fromhex("62 02 08 10")
_BOOTSTRAP_REMAINDER_11_12_13 = bytes.fromhex(
    "62 02 08 11 62 02 08 12 62 02 08 13"
)
_BOOTSTRAP_TAIL_10 = _OBSERVED_CONTROL_PREFIXES[0] + _BOOTSTRAP_REMAINDER_10
_BOOTSTRAP_TAIL_11_12_13 = (
    _OBSERVED_CONTROL_PREFIXES[0] + _BOOTSTRAP_REMAINDER_11_12_13
)


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
    def from_frame(
        cls,
        frame: bytes,
        *,
        relative_to_stop_head_seconds: float | None = None,
    ) -> ControlFrameSummary:
        """Derive a summary without retaining any part of ``frame``."""

        if not isinstance(frame, bytes):
            raise TypeError("control frame must be bytes")
        envelope_match = frame[:8] == _SERVICE_ACK_PREFIX
        fixed_word_match = len(frame) >= 10 and frame[8:10] == b"\x10\x00"
        trailing_length_consistent = (
            int.from_bytes(frame[10:12], "little") == len(frame) - 12
            if len(frame) >= 12
            else None
        )
        tag_08 = frame[12] == 0x08 if len(frame) >= 13 else None
        suffix_0e = frame.endswith(_SERVICE_ACK_SUFFIX_PREFIX + b"\x0e")
        suffix_13 = frame.endswith(_SERVICE_ACK_SUFFIX_PREFIX + b"\x13")
        candidate_shape = (
            envelope_match
            and fixed_word_match
            and trailing_length_consistent is True
            and tag_08 is True
        )

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
                if frame[13 + offset] < 0x80:
                    identifier_terminated = True
                    identifier_octets = offset + 1
                    candidate_identifier = frame[13 : 13 + identifier_octets]
                    identifier_canonical = _is_canonical_candidate_identifier(
                        candidate_identifier
                    )
                    identifier_is_current_canonical = (
                        _is_canonical_one_or_two_byte_varint(candidate_identifier)
                    )
                    post_identifier = frame[13 + identifier_octets :]
                    break

        post_identifier_length = (
            len(post_identifier) if post_identifier is not None else None
        )
        has_post_identifier_prefix = (
            post_identifier is not None and len(post_identifier) >= 2
        )
        post_identifier_prefix_octet_0 = (
            post_identifier[0] if has_post_identifier_prefix else None
        )
        post_identifier_prefix_octet_1 = (
            post_identifier[1] if has_post_identifier_prefix else None
        )
        post_identifier_starts_10_01 = (
            post_identifier.startswith(b"\x10\x01")
            if post_identifier is not None
            else None
        )
        post_identifier_prefix_is_observed = (
            post_identifier[:2] in _OBSERVED_CONTROL_PREFIXES
            if has_post_identifier_prefix
            else None
        )
        post_identifier_field_tag = (
            post_identifier[2]
            if post_identifier_starts_10_01 and len(post_identifier) >= 3
            else None
        )
        post_identifier_field_parameter = (
            post_identifier[3]
            if post_identifier_starts_10_01 and len(post_identifier) >= 4
            else None
        )
        post_identifier_remainder = (
            post_identifier[2:] if has_post_identifier_prefix else None
        )
        remainder_is_ack_0e_shape = (
            post_identifier_remainder == b"\x4a\x02\x08\x0e"
            if post_identifier_remainder is not None
            else None
        )
        remainder_is_ack_13_shape = (
            post_identifier_remainder == b"\x4a\x02\x08\x13"
            if post_identifier_remainder is not None
            else None
        )
        remainder_is_bootstrap_10_shape = (
            post_identifier_remainder == b"\x62\x02\x08\x10"
            if post_identifier_remainder is not None
            else None
        )
        remainder_is_bootstrap_11_12_13_shape = (
            post_identifier_remainder
            == b"\x62\x02\x08\x11\x62\x02\x08\x12\x62\x02\x08\x13"
            if post_identifier_remainder is not None
            else None
        )
        group_count: int | None = None
        group_values: tuple[int, ...] | None = None
        group_offsets: tuple[int, ...] | None = None
        if post_identifier is not None:
            group_count = 0
            bounded_values: list[int] = []
            bounded_offsets: list[int] = []
            offset = 0
            while offset + 4 <= len(post_identifier):
                if post_identifier[offset : offset + 3] == b"\x62\x02\x08":
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

        terminal_tag_08 = len(frame) >= 2 and frame[-2] == 0x08
        terminal_value = frame[-1] if terminal_tag_08 else None
        bootstrap_tail_10_suffix_present = frame.endswith(_BOOTSTRAP_TAIL_10)
        bootstrap_tail_11_12_13_suffix_present = frame.endswith(
            _BOOTSTRAP_TAIL_11_12_13
        )
        return cls(
            length=len(frame),
            header_u16_2_3=(
                int.from_bytes(frame[2:4], "little")
                if len(frame) >= 4
                else None
            ),
            header_u16_4_5=(
                int.from_bytes(frame[4:6], "little")
                if len(frame) >= 6
                else None
            ),
            word_u16_8_9=(
                int.from_bytes(frame[8:10], "little")
                if len(frame) >= 10
                else None
            ),
            word_u16_10_11=(
                int.from_bytes(frame[10:12], "little")
                if len(frame) >= 12
                else None
            ),
            outer_service_envelope_match=envelope_match,
            fixed_word_10_00_match=fixed_word_match,
            trailing_length_consistent=trailing_length_consistent,
            tag_08_at_offset_12=tag_08,
            service_ack_suffix_0e=suffix_0e,
            service_ack_suffix_13=suffix_13,
            candidate_identifier_terminated=identifier_terminated,
            candidate_identifier_octets=identifier_octets,
            candidate_identifier_canonical=identifier_canonical,
            identifier_is_current_canonical_1_or_2=(
                identifier_is_current_canonical
            ),
            post_identifier_length=post_identifier_length,
            post_identifier_prefix_octet_0=post_identifier_prefix_octet_0,
            post_identifier_prefix_octet_1=post_identifier_prefix_octet_1,
            post_identifier_starts_10_01=post_identifier_starts_10_01,
            post_identifier_prefix_is_observed=(
                post_identifier_prefix_is_observed
            ),
            post_identifier_field_tag=post_identifier_field_tag,
            post_identifier_field_parameter=post_identifier_field_parameter,
            remainder_is_ack_0e_shape=remainder_is_ack_0e_shape,
            remainder_is_ack_13_shape=remainder_is_ack_13_shape,
            remainder_is_bootstrap_10_shape=remainder_is_bootstrap_10_shape,
            remainder_is_bootstrap_11_12_13_shape=(
                remainder_is_bootstrap_11_12_13_shape
            ),
            terminal_tag_08=terminal_tag_08,
            terminal_value=terminal_value,
            observed_62_02_08_group_count=group_count,
            observed_62_02_08_terminal_values=group_values,
            observed_62_02_08_group_offsets=group_offsets,
            bootstrap_tail_10_suffix_present=bootstrap_tail_10_suffix_present,
            bootstrap_tail_11_12_13_suffix_present=(
                bootstrap_tail_11_12_13_suffix_present
            ),
            bootstrap_tail_10=_matches_observed_bootstrap_tail(
                frame, _BOOTSTRAP_REMAINDER_10
            ),
            bootstrap_tail_11_12_13=_matches_observed_bootstrap_tail(
                frame, _BOOTSTRAP_REMAINDER_11_12_13
            ),
            heart_rate_marker_present=HEART_RATE_MARKER in frame,
            relative_to_stop_head_seconds=(
                max(0.0, relative_to_stop_head_seconds)
                if relative_to_stop_head_seconds is not None
                else None
            ),
        )


class HeartRateSessionError(RuntimeError):
    """Base class for heart-rate activation/stream errors."""


class HeartRateStateError(HeartRateSessionError):
    """Raised when a command transition violates the proven sequence."""


class HeartRateBootstrapAckTimeoutError(HeartRateSessionError):
    """Raised when the observed service-0x0e acknowledgement is absent."""

    def __init__(
        self,
        frames_observed: int,
        *,
        frames_queued_before_stop_head: int = 0,
        summaries: tuple[ControlFrameSummary, ...] = (),
        application_payloads_sent: int = 0,
    ) -> None:
        self.frames_observed = frames_observed
        self.frames_queued_before_stop_head = frames_queued_before_stop_head
        self.summaries = tuple(summaries)
        self.application_payloads_sent = application_payloads_sent
        super().__init__("STOP_HEAD acknowledgement was not observed")


class HeartRateConnectAckTimeoutError(HeartRateSessionError):
    """Raised when the exact observed CONNECT4 acknowledgement is absent."""

    def __init__(self, frames_observed: int) -> None:
        self.frames_observed = frames_observed
        super().__init__("AAP control-channel acknowledgement was not observed")


class HeartRateStartAckTimeoutError(HeartRateSessionError):
    """Raised when the observed service-0x13 acknowledgement is absent."""

    def __init__(self, frames_observed: int) -> None:
        self.frames_observed = frames_observed
        super().__init__("heart-rate start acknowledgement was not observed")


class HeartRateNoSamplesError(HeartRateSessionError):
    """Raised when the bounded stream window contains no valid report."""

    def __init__(
        self,
        *,
        non_hr_frames: int,
        malformed_hr_frames: int,
        control_frames_observed: int,
    ) -> None:
        self.non_hr_frames = non_hr_frames
        self.malformed_hr_frames = malformed_hr_frames
        self.control_frames_observed = control_frames_observed
        super().__init__("no valid heart-rate sample was observed before the deadline")


class HeartRateCleanupError(HeartRateSessionError):
    """Raised when HR cleanup fails without an earlier primary failure."""


class HeartRateActivationState(IntEnum):
    """Internal ordering states for the proven activation/cleanup sequence."""

    DESCRIPTORS_READY = 0
    STOP_HEAD_SENT = 1
    STOP_HEAD_ACKNOWLEDGED = 2
    CONNECT0_SENT = 3
    CAPS0_SENT = 4
    CONNECT4_SENT = 5
    CONNECT4_ACKNOWLEDGED = 6
    CAPS4_SENT = 7
    HR_ON_SENT = 8
    START_HR_SENT = 9
    START_ACKNOWLEDGED = 10
    STREAM_COMPLETE = 11
    STOP_HR_SENT = 12
    STOP_HR_ACKNOWLEDGED = 13
    HR_OFF_SENT = 14
    COMPLETE = 15


class HeartRateCompletion(StrEnum):
    TARGET_REACHED = "target_reached"
    PARTIAL = "partial"


class HeartRateProgress(StrEnum):
    BOOTSTRAP_COMPLETE = "bootstrap_complete"
    STOP_HEAD_ACKNOWLEDGED = "stop_head_acknowledged"
    CONTROL_CHANNELS_READY = "control_channels_ready"
    START_ACKNOWLEDGED = "start_acknowledged"
    SAMPLE = "sample"
    STOP_ACKNOWLEDGED = "stop_acknowledged"
    STOP_ACK_MISSING = "stop_ack_missing"
    HR_OFF_SENT = "hr_off_sent"


@dataclass(frozen=True, slots=True)
class HeartRateSessionResult:
    samples: tuple[HeartRateReport, ...]
    completion: HeartRateCompletion
    requested_samples: int
    stop_acknowledged: bool
    application_payloads_sent: int
    control_frames_observed: int
    non_hr_frames: int
    malformed_hr_frames: int


@dataclass(frozen=True, slots=True)
class HeartRateMonitorSessionResult:
    """Bounded-memory outcome of an explicitly stopped continuous stream."""

    samples_observed: int
    stop_acknowledged: bool
    application_payloads_sent: int
    control_frames_observed: int
    non_hr_frames: int
    malformed_hr_frames: int


class CollectedHeartRateTransport(Protocol):
    @property
    def application_payloads_sent(self) -> int: ...

    @property
    def pending_receive_frames(self) -> int: ...

    def send_heart_rate_command(self, command: HeartRateCommand) -> None: ...

    async def receive(self, timeout: float) -> bytes: ...


Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
HeartRateProgressCallback = Callable[
    [HeartRateProgress, HeartRateReport | None], None
]


def _is_canonical_one_or_two_byte_varint(value: bytes) -> bool:
    if len(value) == 1:
        return value[0] < 0x80
    if len(value) == 2:
        return value[0] >= 0x80 and value[1] < 0x80 and value[1] != 0
    return False


def _is_canonical_candidate_identifier(value: bytes) -> bool:
    """Check the observed candidate encoding without retaining its value."""

    if not 1 <= len(value) <= 5:
        return False
    if any(octet < 0x80 for octet in value[:-1]) or value[-1] >= 0x80:
        return False
    return len(value) == 1 or value[-1] != 0


def _has_observed_control_body(frame: bytes, remainder: bytes) -> bool:
    for identifier_octets in (1, 2):
        identifier_end = 13 + identifier_octets
        if not _is_canonical_one_or_two_byte_varint(frame[13:identifier_end]):
            continue
        if any(
            frame[identifier_end:] == prefix + remainder
            for prefix in _OBSERVED_CONTROL_PREFIXES
        ):
            return True
    return False


def _matches_observed_bootstrap_tail(frame: bytes, remainder: bytes) -> bool:
    if len(frame) < 13 + 1 + 2 + len(remainder):
        return False
    if frame[:8] != _SERVICE_ACK_PREFIX or frame[8:10] != b"\x10\x00":
        return False
    if int.from_bytes(frame[10:12], "little") != len(frame) - 12:
        return False
    if frame[12] != 0x08:
        return False
    return _has_observed_control_body(frame, remainder)


def is_observed_service_ack(frame: bytes, service_id: int) -> bool:
    """Recognize the observed ACK shape while leaving its identifier variable."""

    if service_id not in _SUPPORTED_ACK_SERVICES or not isinstance(frame, bytes):
        return False
    if len(frame) < 20 or not frame.startswith(_SERVICE_ACK_PREFIX):
        return False

    if frame[8:10] != b"\x10\x00":
        return False
    trailing_length = int.from_bytes(frame[10:12], "little")
    if trailing_length != len(frame) - 12:
        return False
    if frame[12] != 0x08:
        return False
    remainder = _SERVICE_ACK_REMAINDER_PREFIX + bytes((service_id,))
    return _has_observed_control_body(frame, remainder)


def is_connect4_ack(frame: bytes) -> bool:
    """Match the exact connect acknowledgement observed in proven runs."""

    return isinstance(frame, bytes) and frame == CONNECT4_ACK


class HeartRateActivationSession:
    """Run the proven HR sequence on an already-collected AAP transport."""

    def __init__(
        self,
        *,
        sample_target: int = DEFAULT_SAMPLE_TARGET,
        stream_timeout: float = DEFAULT_STREAM_TIMEOUT,
        control_ack_timeout: float = DEFAULT_CONTROL_ACK_TIMEOUT,
        stop_ack_timeout: float = DEFAULT_STOP_ACK_TIMEOUT,
        minimum_bootstrap_seconds: float = MINIMUM_BOOTSTRAP_SECONDS,
        control_summary_limit: int = DEFAULT_CONTROL_SUMMARY_LIMIT,
        clock: Clock = monotonic,
        sleep: Sleeper = asyncio.sleep,
        progress: HeartRateProgressCallback | None = None,
    ) -> None:
        if not 1 <= sample_target <= 10:
            raise ValueError("sample target must be between 1 and 10")
        if not 0 < stream_timeout <= 30:
            raise ValueError("stream timeout must be between 0 and 30 seconds")
        if min(control_ack_timeout, stop_ack_timeout) <= 0:
            raise ValueError("acknowledgement timeouts must be positive")
        if minimum_bootstrap_seconds < 0:
            raise ValueError("minimum bootstrap time cannot be negative")
        if not 1 <= control_summary_limit <= DEFAULT_CONTROL_SUMMARY_LIMIT:
            raise ValueError("control summary limit must be between 1 and 8")
        self._sample_target = sample_target
        self._stream_timeout = stream_timeout
        self._control_ack_timeout = control_ack_timeout
        self._stop_ack_timeout = stop_ack_timeout
        self._minimum_bootstrap_seconds = minimum_bootstrap_seconds
        self._control_summary_limit = control_summary_limit
        self._clock = clock
        self._sleep = sleep
        self._progress = progress
        self.state = HeartRateActivationState.DESCRIPTORS_READY
        self._sent: set[HeartRateCommand] = set()
        self._control_frames_observed = 0

    async def run_collected(
        self,
        transport: CollectedHeartRateTransport,
        handshake: AAPHandshakeResult,
    ) -> HeartRateSessionResult:
        """Activate, observe, and clean up while the caller owns collection."""

        if not handshake.evidence.required:
            raise HeartRateStateError("required descriptor evidence is absent")
        if transport.application_payloads_sent != 1:
            raise HeartRateStateError("heart-rate activation requires one handshake")
        if self._sent or self.state is not HeartRateActivationState.DESCRIPTORS_READY:
            raise HeartRateStateError("heart-rate activation session is single-use")

        samples: tuple[HeartRateReport, ...] = ()
        non_hr_frames = 0
        malformed_hr_frames = 0
        stop_acknowledged = False
        primary_error: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        try:
            remaining = (
                handshake.handshake_sent_at
                + self._minimum_bootstrap_seconds
                - self._clock()
            )
            if remaining > 0:
                await self._sleep(remaining)
            self._emit(HeartRateProgress.BOOTSTRAP_COMPLETE)

            frames_queued_before_stop_head = transport.pending_receive_frames
            self._send_activation(
                transport,
                HeartRateCommand.STOP_HEAD,
                HeartRateActivationState.DESCRIPTORS_READY,
                HeartRateActivationState.STOP_HEAD_SENT,
            )
            stop_head_sent_at = self._clock()
            await self._wait_required_ack(
                transport,
                lambda frame: is_observed_service_ack(frame, 0x0E),
                HeartRateBootstrapAckTimeoutError,
                stop_head_sent_at=stop_head_sent_at,
                frames_queued_before_stop_head=frames_queued_before_stop_head,
            )
            self.state = HeartRateActivationState.STOP_HEAD_ACKNOWLEDGED
            self._emit(HeartRateProgress.STOP_HEAD_ACKNOWLEDGED)

            self._send_activation(
                transport,
                HeartRateCommand.CONNECT0,
                HeartRateActivationState.STOP_HEAD_ACKNOWLEDGED,
                HeartRateActivationState.CONNECT0_SENT,
            )
            self._send_activation(
                transport,
                HeartRateCommand.CAPS0,
                HeartRateActivationState.CONNECT0_SENT,
                HeartRateActivationState.CAPS0_SENT,
            )
            self._send_activation(
                transport,
                HeartRateCommand.CONNECT4,
                HeartRateActivationState.CAPS0_SENT,
                HeartRateActivationState.CONNECT4_SENT,
            )
            await self._wait_required_ack(
                transport,
                is_connect4_ack,
                HeartRateConnectAckTimeoutError,
            )
            self.state = HeartRateActivationState.CONNECT4_ACKNOWLEDGED
            self._emit(HeartRateProgress.CONTROL_CHANNELS_READY)

            self._send_activation(
                transport,
                HeartRateCommand.CAPS4,
                HeartRateActivationState.CONNECT4_ACKNOWLEDGED,
                HeartRateActivationState.CAPS4_SENT,
            )
            self._send_activation(
                transport,
                HeartRateCommand.HR_ON,
                HeartRateActivationState.CAPS4_SENT,
                HeartRateActivationState.HR_ON_SENT,
            )
            self._send_activation(
                transport,
                HeartRateCommand.START_HR,
                HeartRateActivationState.HR_ON_SENT,
                HeartRateActivationState.START_HR_SENT,
            )
            await self._wait_required_ack(
                transport,
                lambda frame: is_observed_service_ack(
                    frame, HEART_RATE_SERVICE_ID
                ),
                HeartRateStartAckTimeoutError,
            )
            self.state = HeartRateActivationState.START_ACKNOWLEDGED
            self._emit(HeartRateProgress.START_ACKNOWLEDGED)

            (
                samples,
                non_hr_frames,
                malformed_hr_frames,
            ) = await self._observe_samples(transport)
            if not samples:
                raise HeartRateNoSamplesError(
                    non_hr_frames=non_hr_frames,
                    malformed_hr_frames=malformed_hr_frames,
                    control_frames_observed=self._control_frames_observed,
                )
            self.state = HeartRateActivationState.STREAM_COMPLETE
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if HeartRateCommand.START_HR in self._sent:
                try:
                    self._send_cleanup(transport, HeartRateCommand.STOP_HR)
                    self.state = HeartRateActivationState.STOP_HR_SENT
                    stop_acknowledged = await self._wait_optional_stop_ack(
                        transport
                    )
                    if stop_acknowledged:
                        self.state = HeartRateActivationState.STOP_HR_ACKNOWLEDGED
                        self._emit(HeartRateProgress.STOP_ACKNOWLEDGED)
                    else:
                        self._emit(HeartRateProgress.STOP_ACK_MISSING)
                except BaseException as error:
                    cleanup_errors.append(error)

            if HeartRateCommand.HR_ON in self._sent:
                try:
                    self._send_cleanup(transport, HeartRateCommand.HR_OFF)
                    self.state = HeartRateActivationState.HR_OFF_SENT
                    self._emit(HeartRateProgress.HR_OFF_SENT)
                except BaseException as error:
                    cleanup_errors.append(error)

            if cleanup_errors:
                if primary_error is not None:
                    primary_error.add_note(
                        "heart-rate stop cleanup also reported an error"
                    )
                else:
                    raise HeartRateCleanupError(
                        "heart-rate stop cleanup failed"
                    ) from cleanup_errors[-1]

        self.state = HeartRateActivationState.COMPLETE
        if transport.application_payloads_sent != 10:
            raise HeartRateStateError(
                "successful heart-rate session sent an unexpected payload count"
            )
        completion = (
            HeartRateCompletion.TARGET_REACHED
            if len(samples) >= self._sample_target
            else HeartRateCompletion.PARTIAL
        )
        return HeartRateSessionResult(
            samples=samples,
            completion=completion,
            requested_samples=self._sample_target,
            stop_acknowledged=stop_acknowledged,
            application_payloads_sent=transport.application_payloads_sent,
            control_frames_observed=self._control_frames_observed,
            non_hr_frames=non_hr_frames,
            malformed_hr_frames=malformed_hr_frames,
        )

    def _send_activation(
        self,
        transport: CollectedHeartRateTransport,
        command: HeartRateCommand,
        expected: HeartRateActivationState,
        next_state: HeartRateActivationState,
    ) -> None:
        if self.state is not expected or command in self._sent:
            raise HeartRateStateError("invalid or duplicate HR activation transition")
        transport.send_heart_rate_command(command)
        self._sent.add(command)
        self.state = next_state

    def _send_cleanup(
        self,
        transport: CollectedHeartRateTransport,
        command: HeartRateCommand,
    ) -> None:
        if command not in (HeartRateCommand.STOP_HR, HeartRateCommand.HR_OFF):
            raise HeartRateStateError("activation command cannot use cleanup path")
        if command in self._sent:
            raise HeartRateStateError("duplicate HR cleanup command")
        if command is HeartRateCommand.STOP_HR:
            if HeartRateCommand.START_HR not in self._sent:
                raise HeartRateStateError("STOP_HR requires START_HR")
        elif HeartRateCommand.HR_ON not in self._sent:
            raise HeartRateStateError("HR_OFF requires HR_ON")
        transport.send_heart_rate_command(command)
        self._sent.add(command)

    async def _wait_required_ack(
        self,
        transport: CollectedHeartRateTransport,
        predicate: Callable[[bytes], bool],
        error_type: type[
            HeartRateBootstrapAckTimeoutError
            | HeartRateConnectAckTimeoutError
            | HeartRateStartAckTimeoutError
        ],
        *,
        stop_head_sent_at: float | None = None,
        frames_queued_before_stop_head: int = 0,
    ) -> None:
        deadline = self._clock() + self._control_ack_timeout
        frames = 0
        summaries: list[ControlFrameSummary] = []
        while (remaining := deadline - self._clock()) > 0:
            try:
                frame = await transport.receive(remaining)
            except TimeoutError:
                break
            frames += 1
            self._control_frames_observed += 1
            if (
                stop_head_sent_at is not None
                and len(summaries) < self._control_summary_limit
            ):
                summaries.append(
                    ControlFrameSummary.from_frame(
                        frame,
                        relative_to_stop_head_seconds=(
                            self._clock() - stop_head_sent_at
                        ),
                    )
                )
            if predicate(frame):
                return
        if error_type is HeartRateBootstrapAckTimeoutError:
            raise HeartRateBootstrapAckTimeoutError(
                frames,
                frames_queued_before_stop_head=frames_queued_before_stop_head,
                summaries=tuple(summaries),
                application_payloads_sent=transport.application_payloads_sent,
            )
        raise error_type(frames)

    async def _wait_optional_stop_ack(
        self, transport: CollectedHeartRateTransport
    ) -> bool:
        deadline = self._clock() + self._stop_ack_timeout
        while (remaining := deadline - self._clock()) > 0:
            try:
                frame = await transport.receive(remaining)
            except TimeoutError:
                return False
            self._control_frames_observed += 1
            if is_observed_service_ack(frame, HEART_RATE_SERVICE_ID):
                return True
        return False

    async def _observe_samples(
        self, transport: CollectedHeartRateTransport
    ) -> tuple[tuple[HeartRateReport, ...], int, int]:
        deadline = self._clock() + self._stream_timeout
        samples: list[HeartRateReport] = []
        non_hr_frames = 0
        malformed_hr_frames = 0
        while len(samples) < self._sample_target:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                frame = await transport.receive(remaining)
            except TimeoutError:
                break
            try:
                report = parse_heart_rate_packet(frame)
            except HeartRateMarkerNotFoundError:
                non_hr_frames += 1
                continue
            except HeartRateParseError:
                malformed_hr_frames += 1
                continue
            samples.append(report)
            self._emit(HeartRateProgress.SAMPLE, report)
        return tuple(samples), non_hr_frames, malformed_hr_frames

    def _emit(
        self,
        event: HeartRateProgress,
        report: HeartRateReport | None = None,
    ) -> None:
        if self._progress is not None:
            self._progress(event, report)


class HeartRateMonitorActivationSession(HeartRateActivationSession):
    """Continuously observe HR reports until a caller-owned event is set."""

    def __init__(
        self,
        *,
        receive_poll_interval: float = 0.5,
        control_ack_timeout: float = DEFAULT_CONTROL_ACK_TIMEOUT,
        stop_ack_timeout: float = DEFAULT_STOP_ACK_TIMEOUT,
        minimum_bootstrap_seconds: float = MINIMUM_BOOTSTRAP_SECONDS,
        control_summary_limit: int = DEFAULT_CONTROL_SUMMARY_LIMIT,
        clock: Clock = monotonic,
        sleep: Sleeper = asyncio.sleep,
        progress: HeartRateProgressCallback | None = None,
    ) -> None:
        if not 0 < receive_poll_interval <= 5.0:
            raise ValueError(
                "receive poll interval must be greater than zero and at most 5 seconds"
            )
        # The bounded observation settings are unused by this subclass. Calling
        # the proven initializer preserves its state machine, validation, ACK
        # handling, diagnostics, and closed-command cleanup implementation.
        super().__init__(
            sample_target=1,
            stream_timeout=receive_poll_interval,
            control_ack_timeout=control_ack_timeout,
            stop_ack_timeout=stop_ack_timeout,
            minimum_bootstrap_seconds=minimum_bootstrap_seconds,
            control_summary_limit=control_summary_limit,
            clock=clock,
            sleep=sleep,
            progress=progress,
        )
        self._receive_poll_interval = receive_poll_interval

    async def run_collected(
        self,
        transport: CollectedHeartRateTransport,
        handshake: AAPHandshakeResult,
        stop_event: asyncio.Event,
    ) -> HeartRateMonitorSessionResult:
        """Activate once, stream without retaining reports, and clean up."""

        if not isinstance(stop_event, asyncio.Event):
            raise TypeError("stop_event must be an asyncio.Event")
        if not handshake.evidence.required:
            raise HeartRateStateError("required descriptor evidence is absent")
        if transport.application_payloads_sent != 1:
            raise HeartRateStateError("heart-rate activation requires one handshake")
        if self._sent or self.state is not HeartRateActivationState.DESCRIPTORS_READY:
            raise HeartRateStateError("heart-rate activation session is single-use")

        samples_observed = 0
        non_hr_frames = 0
        malformed_hr_frames = 0
        stop_acknowledged = False
        primary_error: BaseException | None = None
        cleanup_errors: list[BaseException] = []
        cleanup_cancellation: asyncio.CancelledError | None = None
        try:
            remaining = (
                handshake.handshake_sent_at
                + self._minimum_bootstrap_seconds
                - self._clock()
            )
            if remaining > 0:
                await self._sleep(remaining)
            self._emit(HeartRateProgress.BOOTSTRAP_COMPLETE)

            frames_queued_before_stop_head = transport.pending_receive_frames
            self._send_activation(
                transport,
                HeartRateCommand.STOP_HEAD,
                HeartRateActivationState.DESCRIPTORS_READY,
                HeartRateActivationState.STOP_HEAD_SENT,
            )
            stop_head_sent_at = self._clock()
            await self._wait_required_ack(
                transport,
                lambda frame: is_observed_service_ack(frame, 0x0E),
                HeartRateBootstrapAckTimeoutError,
                stop_head_sent_at=stop_head_sent_at,
                frames_queued_before_stop_head=frames_queued_before_stop_head,
            )
            self.state = HeartRateActivationState.STOP_HEAD_ACKNOWLEDGED
            self._emit(HeartRateProgress.STOP_HEAD_ACKNOWLEDGED)

            self._send_activation(
                transport,
                HeartRateCommand.CONNECT0,
                HeartRateActivationState.STOP_HEAD_ACKNOWLEDGED,
                HeartRateActivationState.CONNECT0_SENT,
            )
            self._send_activation(
                transport,
                HeartRateCommand.CAPS0,
                HeartRateActivationState.CONNECT0_SENT,
                HeartRateActivationState.CAPS0_SENT,
            )
            self._send_activation(
                transport,
                HeartRateCommand.CONNECT4,
                HeartRateActivationState.CAPS0_SENT,
                HeartRateActivationState.CONNECT4_SENT,
            )
            await self._wait_required_ack(
                transport,
                is_connect4_ack,
                HeartRateConnectAckTimeoutError,
            )
            self.state = HeartRateActivationState.CONNECT4_ACKNOWLEDGED
            self._emit(HeartRateProgress.CONTROL_CHANNELS_READY)

            self._send_activation(
                transport,
                HeartRateCommand.CAPS4,
                HeartRateActivationState.CONNECT4_ACKNOWLEDGED,
                HeartRateActivationState.CAPS4_SENT,
            )
            self._send_activation(
                transport,
                HeartRateCommand.HR_ON,
                HeartRateActivationState.CAPS4_SENT,
                HeartRateActivationState.HR_ON_SENT,
            )
            self._send_activation(
                transport,
                HeartRateCommand.START_HR,
                HeartRateActivationState.HR_ON_SENT,
                HeartRateActivationState.START_HR_SENT,
            )
            await self._wait_required_ack(
                transport,
                lambda frame: is_observed_service_ack(
                    frame, HEART_RATE_SERVICE_ID
                ),
                HeartRateStartAckTimeoutError,
            )
            self.state = HeartRateActivationState.START_ACKNOWLEDGED
            self._emit(HeartRateProgress.START_ACKNOWLEDGED)

            while not stop_event.is_set():
                try:
                    frame = await transport.receive(self._receive_poll_interval)
                except TimeoutError:
                    continue
                try:
                    report = parse_heart_rate_packet(frame)
                except HeartRateMarkerNotFoundError:
                    non_hr_frames += 1
                    continue
                except HeartRateParseError:
                    malformed_hr_frames += 1
                    continue
                samples_observed += 1
                self._emit(HeartRateProgress.SAMPLE, report)
            self.state = HeartRateActivationState.STREAM_COMPLETE
        except BaseException as error:
            primary_error = error
            raise
        finally:
            if HeartRateCommand.START_HR in self._sent:
                try:
                    self._send_cleanup(transport, HeartRateCommand.STOP_HR)
                    self.state = HeartRateActivationState.STOP_HR_SENT
                    stop_acknowledged = await self._wait_optional_stop_ack(
                        transport
                    )
                    if stop_acknowledged:
                        self.state = HeartRateActivationState.STOP_HR_ACKNOWLEDGED
                        self._emit(HeartRateProgress.STOP_ACKNOWLEDGED)
                    else:
                        self._emit(HeartRateProgress.STOP_ACK_MISSING)
                except asyncio.CancelledError as error:
                    cleanup_cancellation = error
                except BaseException as error:
                    cleanup_errors.append(error)

            if HeartRateCommand.HR_ON in self._sent:
                try:
                    self._send_cleanup(transport, HeartRateCommand.HR_OFF)
                    self.state = HeartRateActivationState.HR_OFF_SENT
                    self._emit(HeartRateProgress.HR_OFF_SENT)
                except asyncio.CancelledError as error:
                    cleanup_cancellation = error
                except BaseException as error:
                    cleanup_errors.append(error)

            if cleanup_cancellation is not None:
                if cleanup_errors:
                    cleanup_cancellation.add_note(
                        "heart-rate stop cleanup also reported an error"
                    )
                raise cleanup_cancellation
            if cleanup_errors:
                if primary_error is not None:
                    primary_error.add_note(
                        "heart-rate stop cleanup also reported an error"
                    )
                else:
                    raise HeartRateCleanupError(
                        "heart-rate stop cleanup failed"
                    ) from cleanup_errors[-1]

        self.state = HeartRateActivationState.COMPLETE
        if transport.application_payloads_sent != 10:
            raise HeartRateStateError(
                "successful heart-rate monitor sent an unexpected payload count"
            )
        return HeartRateMonitorSessionResult(
            samples_observed=samples_observed,
            stop_acknowledged=stop_acknowledged,
            application_payloads_sent=transport.application_payloads_sent,
            control_frames_observed=self._control_frames_observed,
            non_hr_frames=non_hr_frames,
            malformed_hr_frames=malformed_hr_frames,
        )


