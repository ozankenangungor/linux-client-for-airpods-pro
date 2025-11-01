"""Minimal, bounded AAP handshake and descriptor-observation layer."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import AsyncContextManager, Protocol

from airpods_hr.aap_channel import AAPChannel, AAPChannelSession
from airpods_hr.authentication import AuthenticatedClassicContext
from airpods_hr.classic_diagnostics import ClassicHostStateSnapshot
from airpods_hr.protocol import HeartRateCommand
from airpods_hr.sdp import SDPCompatibilityProfile
from airpods_hr.sdp_diagnostics import (
    BumbleSDPDiagnostics,
    ProtocolTimelineKind,
    SafeProtocolTimeline,
    SDPDiagnosticsSnapshot,
)


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


class AAPHandshakeError(RuntimeError):
    """Base error for the AAP handshake exchange."""


class AAPHandshakeTimeoutError(AAPHandshakeError):
    """Raised when the exact known ACK is not observed before its deadline."""


class AAPDescriptorObservationTimeoutError(AAPHandshakeError):
    """Raised when required descriptor evidence is absent after a valid ACK."""

    def __init__(
        self,
        observation: HandshakeObservation,
        sdp_diagnostics: SDPDiagnosticsSnapshot | None = None,
        host_state_snapshot: ClassicHostStateSnapshot | None = None,
    ) -> None:
        self.observation = observation
        self.evidence = observation.evidence
        self.sdp_diagnostics = sdp_diagnostics
        self.host_state_snapshot = host_state_snapshot
        super().__init__(
            "AAP handshake succeeded, but required descriptor evidence was not observed"
        )


class AAPReceiveStateError(AAPHandshakeError):
    """Raised when the bounded receive collector is used outside its scope."""


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


@dataclass(frozen=True, slots=True)
class HandshakeObservation:
    """Safe, immutable state accumulated around exact ACK recognition."""

    ack_observed: bool
    evidence: DescriptorEvidence
    pre_ack_frame_count: int = 0
    post_ack_frame_count: int = 0
    receive_frames_dropped: int = 0
    pre_ack_frame_summaries: tuple[AAPFrameSummary, ...] = ()
    post_ack_frame_summaries: tuple[AAPFrameSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class AAPHandshakeResult:
    observation: HandshakeObservation
    application_payloads_sent: int
    handshake_sent_at: float

    @property
    def evidence(self) -> DescriptorEvidence:
        return self.observation.evidence


class ReceiveTransport(Protocol):
    @property
    def application_payloads_sent(self) -> int: ...

    @property
    def dropped_frames(self) -> int: ...

    def collect(self) -> AsyncContextManager[ReceiveTransport]: ...

    def send_handshake_request(self) -> None: ...

    def send_heart_rate_command(self, command: HeartRateCommand) -> None: ...

    async def receive(self, timeout: float) -> bytes: ...


class SecureClassicSession(Protocol):
    def open(
        self, *, pre_connect_profile: object | None = None
    ) -> AsyncContextManager[AuthenticatedClassicContext]: ...


Clock = Callable[[], float]
AAPProgressCallback = Callable[[AAPProgress], None]


class BumbleAAPTransport:
    """Bounded receive adapter with one deliberately restricted send method."""

    def __init__(
        self,
        raw_channel: object,
        channel: AAPChannel,
        *,
        queue_limit: int = 32,
        frame_size_limit: int = 64 * 1024,
    ) -> None:
        if queue_limit <= 0 or frame_size_limit <= 0:
            raise ValueError("AAP receive limits must be positive")
        self.channel = channel
        self._raw_channel = raw_channel
        self._queue_limit = queue_limit
        self._frame_size_limit = frame_size_limit
        self._queue: asyncio.Queue[bytes] | None = None
        self._application_payloads_sent = 0
        self.dropped_frames = 0

    @property
    def application_payloads_sent(self) -> int:
        return self._application_payloads_sent

    @property
    def queued_frames(self) -> int:
        return 0 if self._queue is None else self._queue.qsize()

    @property
    def pending_receive_frames(self) -> int:
        """Return only the current bounded receive-queue count."""

        return self.queued_frames

    @asynccontextmanager
    async def collect(self) -> AsyncIterator[BumbleAAPTransport]:
        if self._queue is not None:
            raise AAPReceiveStateError("AAP receive collection is already active")
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self._queue_limit)
        previous_sink = self._raw_channel.sink

        def receive_sdu(sdu: bytes) -> None:
            if not isinstance(sdu, bytes) or len(sdu) > self._frame_size_limit:
                self.dropped_frames += 1
                return
            if queue.full():
                queue.get_nowait()
                self.dropped_frames += 1
            queue.put_nowait(sdu)

        self._queue = queue
        self._raw_channel.sink = receive_sdu
        try:
            yield self
        finally:
            self._raw_channel.sink = previous_sink
            self._queue = None

    def send_handshake_request(self) -> None:
        if self._queue is None:
            raise AAPReceiveStateError("AAP receive collection is not active")
        if self._application_payloads_sent != 0:
            raise AAPHandshakeError("AAP handshake request was already sent")
        self._raw_channel.write(AAP_HANDSHAKE_REQUEST)
        self._application_payloads_sent = 1

    def send_heart_rate_command(self, command: HeartRateCommand) -> None:
        """Send one member of the verified heart-rate command set."""

        if self._queue is None:
            raise AAPReceiveStateError("AAP receive collection is not active")
        if not isinstance(command, HeartRateCommand):
            raise TypeError("command must be a HeartRateCommand")
        if self._application_payloads_sent < 1:
            raise AAPReceiveStateError("AAP handshake has not been sent")
        self._raw_channel.write(command.payload)
        self._application_payloads_sent += 1

    async def receive(self, timeout: float) -> bytes:
        if self._queue is None:
            raise AAPReceiveStateError("AAP receive collection is not active")
        if timeout <= 0:
            raise TimeoutError
        return await asyncio.wait_for(self._queue.get(), timeout=timeout)


class AAPHandshakeSession:
    """Send one known request and observe ACK/descriptor evidence."""

    def __init__(
        self,
        *,
        ack_timeout: float = 5.0,
        descriptor_timeout: float = 3.0,
        frame_summary_limit: int = AAP_FRAME_SUMMARY_LIMIT,
        clock: Clock = monotonic,
        progress: AAPProgressCallback | None = None,
        timeline: SafeProtocolTimeline | None = None,
    ) -> None:
        if ack_timeout <= 0 or descriptor_timeout <= 0:
            raise ValueError("AAP handshake timeouts must be positive")
        if frame_summary_limit <= 0:
            raise ValueError("AAP frame summary limit must be positive")
        self._ack_timeout = ack_timeout
        self._descriptor_timeout = descriptor_timeout
        self._frame_summary_limit = frame_summary_limit
        self._clock = clock
        self._progress = progress
        self.timeline = timeline or SafeProtocolTimeline(clock=clock)
        self._first_post_ack_frame_observed = False
        self._first_357_byte_frame_observed = False

    async def run(self, transport: ReceiveTransport) -> AAPHandshakeResult:
        """Run the AAP handshake exchange while owning its receive collector."""

        async with transport.collect():
            return await self.run_collected(transport)

    async def run_collected(
        self, transport: ReceiveTransport
    ) -> AAPHandshakeResult:
        """Run while a caller-owned receive collector remains active."""

        self._first_post_ack_frame_observed = False
        self._first_357_byte_frame_observed = False
        transport.send_handshake_request()
        handshake_sent_at = self._clock()
        self.timeline.record(ProtocolTimelineKind.HANDSHAKE_SENT)
        self._emit(AAPProgress.HANDSHAKE_SENT)
        observation = await self._wait_for_ack(transport)
        self._emit(AAPProgress.ACK_OBSERVED)
        observation = await self._observe_descriptors(transport, observation)
        self._emit(AAPProgress.DESCRIPTORS_OBSERVED)

        if transport.application_payloads_sent != 1:
            raise AAPHandshakeError("unexpected AAP application payload count")
        return AAPHandshakeResult(
            observation,
            transport.application_payloads_sent,
            handshake_sent_at,
        )

    async def _wait_for_ack(
        self, transport: ReceiveTransport
    ) -> HandshakeObservation:
        deadline = self._clock() + self._ack_timeout
        evidence = DescriptorEvidence()
        pre_ack_frame_count = 0
        summaries: tuple[AAPFrameSummary, ...] = ()
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                frame = await transport.receive(remaining)
            except TimeoutError:
                break
            if frame == AAP_HANDSHAKE_ACK:
                self.timeline.record(ProtocolTimelineKind.ACK_OBSERVED)
                return HandshakeObservation(
                    ack_observed=True,
                    evidence=evidence,
                    pre_ack_frame_count=pre_ack_frame_count,
                    receive_frames_dropped=transport.dropped_frames,
                    pre_ack_frame_summaries=summaries,
                )
            pre_ack_frame_count += 1
            self._record_frame_timeline(frame, post_ack=False)
            if len(summaries) < self._frame_summary_limit:
                summaries += (AAPFrameSummary.from_frame(frame),)
            evidence = evidence.merged(frame)
        raise AAPHandshakeTimeoutError("AAP handshake ACK was not observed")

    async def _observe_descriptors(
        self,
        transport: ReceiveTransport,
        observation: HandshakeObservation,
    ) -> HandshakeObservation:
        if observation.evidence.required:
            return observation

        deadline = self._clock() + self._descriptor_timeout
        evidence = observation.evidence
        post_ack_frame_count = observation.post_ack_frame_count
        summaries = observation.post_ack_frame_summaries
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                frame = await transport.receive(remaining)
            except TimeoutError:
                break
            post_ack_frame_count += 1
            self._record_frame_timeline(frame, post_ack=True)
            if (
                len(observation.pre_ack_frame_summaries) + len(summaries)
                < self._frame_summary_limit
            ):
                summaries += (AAPFrameSummary.from_frame(frame),)
            evidence = evidence.merged(frame)
            if evidence.required:
                return HandshakeObservation(
                    ack_observed=True,
                    evidence=evidence,
                    pre_ack_frame_count=observation.pre_ack_frame_count,
                    post_ack_frame_count=post_ack_frame_count,
                    receive_frames_dropped=transport.dropped_frames,
                    pre_ack_frame_summaries=(
                        observation.pre_ack_frame_summaries
                    ),
                    post_ack_frame_summaries=summaries,
                )
        final_observation = HandshakeObservation(
            ack_observed=True,
            evidence=evidence,
            pre_ack_frame_count=observation.pre_ack_frame_count,
            post_ack_frame_count=post_ack_frame_count,
            receive_frames_dropped=transport.dropped_frames,
            pre_ack_frame_summaries=observation.pre_ack_frame_summaries,
            post_ack_frame_summaries=summaries,
        )
        if not evidence.required:
            raise AAPDescriptorObservationTimeoutError(final_observation)
        return final_observation

    def _record_frame_timeline(self, frame: bytes, *, post_ack: bool) -> None:
        if post_ack and not self._first_post_ack_frame_observed:
            self._first_post_ack_frame_observed = True
            self.timeline.record(ProtocolTimelineKind.FIRST_POST_ACK_FRAME)
        if len(frame) == 357 and not self._first_357_byte_frame_observed:
            self._first_357_byte_frame_observed = True
            self.timeline.record(ProtocolTimelineKind.FIRST_357_BYTE_FRAME)

    def _emit(self, event: AAPProgress) -> None:
        if self._progress is not None:
            self._progress(event)


@dataclass(frozen=True, slots=True)
class AAPHandshakeProbeResult:
    display_name: str
    evidence: DescriptorEvidence
    application_payloads_sent: int
    replacement_key_reported: bool
    sdp_diagnostics: SDPDiagnosticsSnapshot
    host_state_snapshot: ClassicHostStateSnapshot | None = None


class AAPHandshakeProbeSession:
    """Compose secure Classic, temporary SDP, AAP L2CAP, and handshake."""

    def __init__(
        self,
        secure_session: SecureClassicSession,
        channel_session: AAPChannelSession,
        handshake_session: AAPHandshakeSession,
        *,
        progress: AAPProgressCallback | None = None,
        queue_limit: int = 32,
        frame_size_limit: int = 64 * 1024,
    ) -> None:
        self._secure_session = secure_session
        self._channel_session = channel_session
        self._handshake_session = handshake_session
        self._progress = progress
        self._queue_limit = queue_limit
        self._frame_size_limit = frame_size_limit

    async def run(self) -> AAPHandshakeProbeResult:
        secure: AuthenticatedClassicContext | None = None
        handshake: AAPHandshakeResult | None = None
        timeline = getattr(
            self._handshake_session, "timeline", SafeProtocolTimeline()
        )
        diagnostics = BumbleSDPDiagnostics(timeline=timeline)
        profile = SDPCompatibilityProfile(
            installed_callback=lambda: self._emit(AAPProgress.SDP_INSTALLED),
            diagnostics=diagnostics,
        )
        try:
            async with self._secure_session.open(
                pre_connect_profile=profile
            ) as secure:

                def transport_factory(
                    raw_channel: object, channel: AAPChannel
                ) -> BumbleAAPTransport:
                    return BumbleAAPTransport(
                        raw_channel,
                        channel,
                        queue_limit=self._queue_limit,
                        frame_size_limit=self._frame_size_limit,
                    )

                async with self._channel_session.open_protocol(
                    secure.connection, transport_factory
                ) as transport:
                    handshake = await self._handshake_session.run(transport)
        except AAPDescriptorObservationTimeoutError as error:
            raise AAPDescriptorObservationTimeoutError(
                error.observation,
                sdp_diagnostics=diagnostics.snapshot(),
                host_state_snapshot=(
                    secure.host_state_snapshot if secure is not None else None
                ),
            ) from None

        assert secure is not None and handshake is not None
        return AAPHandshakeProbeResult(
            display_name=secure.display_name,
            evidence=handshake.evidence,
            application_payloads_sent=handshake.application_payloads_sent,
            replacement_key_reported=secure.replacement_key_reported,
            sdp_diagnostics=diagnostics.snapshot(),
            host_state_snapshot=secure.host_state_snapshot,
        )

    def _emit(self, event: AAPProgress) -> None:
        if self._progress is not None:
            self._progress(event)
