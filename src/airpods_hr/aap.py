"""Minimal, bounded AAP handshake and descriptor-observation layer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import AsyncContextManager, Protocol

import airpods_hr._airpods_aap_core as _rust_core

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

AAP_FRAME_SUMMARY_LIMIT = 64


class AAPHandshakeError(RuntimeError):
    """Base error for the AAP handshake exchange."""


class AAPHandshakeTimeoutError(AAPHandshakeError):
    """Raised when the exact known ACK is not observed before its deadline."""

    def __init__(
        self,
        message: str,
        observation: HandshakeObservation | None = None,
    ) -> None:
        self.observation = observation
        super().__init__(message)


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
            *_rust_core.merge_descriptor_evidence(
                frame,
                self.sensor_framework,
                self.heart_rate_service,
                self.heart_rate,
                self.heartrate_access,
            )
        )

    @property
    def required(self) -> bool:
        return _rust_core.runtime_descriptors_required(
            self.sensor_framework, self.heart_rate_service
        )


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
        return _type_2b_from_native(cls, _rust_core.summarize_type_2b_frame(frame))


@dataclass(frozen=True, slots=True)
class AAPFrameSummary:
    """Allowlisted AAP frame shape with no payload content."""

    length: int
    header_u16_2_3: int | None = None
    header_u16_4_5: int | None = None
    type_2b_summary: AAPType2BFrameSummary | None = None

    @classmethod
    def from_frame(cls, frame: bytes) -> AAPFrameSummary:
        length, header_u16_2_3, header_u16_4_5, type_2b = (
            _rust_core.summarize_aap_frame(frame)
        )
        return cls(
            length,
            header_u16_2_3,
            header_u16_4_5,
            _type_2b_from_native(AAPType2BFrameSummary, type_2b)
            if type_2b is not None
            else None,
        )


def _type_2b_from_native(
    cls: type[AAPType2BFrameSummary], values: tuple
) -> AAPType2BFrameSummary:
    """Adapt the private core result to the established frozen Python model."""
    return cls(
        *values[:8],
        tuple(RecordSuffixSummary(*item) for item in values[8]),
        values[9],
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
        self._policy = _rust_core.TransportPolicy()
        self.dropped_frames = 0

    @property
    def application_payloads_sent(self) -> int:
        return self._policy.application_payloads_sent

    @property
    def queued_frames(self) -> int:
        return 0 if self._queue is None else self._queue.qsize()

    @property
    def pending_receive_frames(self) -> int:
        """Return only the current bounded receive-queue count."""

        return self.queued_frames

    @asynccontextmanager
    async def collect(self) -> AsyncIterator[BumbleAAPTransport]:
        if self._policy.collection_active:
            raise AAPReceiveStateError("AAP receive collection is already active")
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=self._queue_limit)
        previous_sink = self._raw_channel.sink
        self._policy.begin_collection()

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
            self._policy.end_collection()

    def send_handshake_request(self) -> None:
        legality = self._policy.send_legality(True)
        if legality == 1:
            raise AAPReceiveStateError("AAP receive collection is not active")
        if legality == 2:
            raise AAPHandshakeError("AAP handshake request was already sent")
        self._raw_channel.write(AAP_HANDSHAKE_REQUEST)
        self._policy.sent(True)

    def send_heart_rate_command(self, command: HeartRateCommand) -> None:
        """Send one member of the verified heart-rate command set."""

        legality = self._policy.send_legality(False)
        if legality == 1:
            raise AAPReceiveStateError("AAP receive collection is not active")
        if not isinstance(command, HeartRateCommand):
            raise TypeError("command must be a HeartRateCommand")
        if legality == 3:
            raise AAPReceiveStateError("AAP handshake has not been sent")
        self._raw_channel.write(command.payload)
        self._policy.sent(False)

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
        if not _rust_core.runtime_positive_timeouts(
            (ack_timeout, descriptor_timeout)
        ):
            raise ValueError("AAP handshake timeouts must be positive")
        if frame_summary_limit <= 0:
            raise ValueError("AAP frame summary limit must be positive")
        self._ack_timeout = ack_timeout
        self._descriptor_timeout = descriptor_timeout
        self._frame_summary_limit = frame_summary_limit
        self._clock = clock
        self._progress = progress
        self.timeline = timeline or SafeProtocolTimeline(clock=clock)
        self._accumulator: _rust_core.HandshakeAccumulator | None = None

    async def run(self, transport: ReceiveTransport) -> AAPHandshakeResult:
        """Run the AAP handshake exchange while owning its receive collector."""

        async with transport.collect():
            return await self.run_collected(transport)

    async def run_collected(
        self, transport: ReceiveTransport
    ) -> AAPHandshakeResult:
        """Run while a caller-owned receive collector remains active."""

        self._accumulator = _rust_core.HandshakeAccumulator(self._frame_summary_limit)
        transport.send_handshake_request()
        handshake_sent_at = self._clock()
        self.timeline.record(ProtocolTimelineKind.HANDSHAKE_SENT)
        self._emit(AAPProgress.HANDSHAKE_SENT)
        observation = await self._wait_for_ack(transport)
        self._emit(AAPProgress.ACK_OBSERVED)
        observation = await self._observe_descriptors(transport, observation)
        self._emit(AAPProgress.DESCRIPTORS_OBSERVED)

        if not _rust_core.runtime_expected_payload_count(
            transport.application_payloads_sent
        ):
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
        accumulator = self._accumulator
        assert accumulator is not None
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                frame = await transport.receive(remaining)
            except TimeoutError:
                break
            ack, first_post, first_357 = accumulator.observe(
                frame, transport.dropped_frames
            )
            self._record_frame_timeline(first_post, first_357)
            if ack:
                self.timeline.record(ProtocolTimelineKind.ACK_OBSERVED)
                return self._snapshot(transport)
        raise AAPHandshakeTimeoutError(
            "AAP handshake ACK was not observed",
            self._snapshot(transport),
        )

    async def _observe_descriptors(
        self,
        transport: ReceiveTransport,
        observation: HandshakeObservation,
    ) -> HandshakeObservation:
        accumulator = self._accumulator
        assert accumulator is not None
        if accumulator.descriptors_complete:
            return observation

        deadline = self._clock() + self._descriptor_timeout
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                frame = await transport.receive(remaining)
            except TimeoutError:
                break
            _, first_post, first_357 = accumulator.observe(
                frame, transport.dropped_frames
            )
            self._record_frame_timeline(first_post, first_357)
            if accumulator.descriptors_complete:
                return self._snapshot(transport)
        raise AAPDescriptorObservationTimeoutError(self._snapshot(transport))

    def _snapshot(self, transport: ReceiveTransport) -> HandshakeObservation:
        accumulator = self._accumulator
        assert accumulator is not None
        accumulator.snapshot_dropped(transport.dropped_frames)
        ack, evidence, pre_count, post_count, dropped, pre, post = (
            accumulator.snapshot()
        )
        def adapt(values: tuple) -> AAPFrameSummary:
            length, header_2_3, header_4_5, type_2b = values
            return AAPFrameSummary(
                length, header_2_3, header_4_5,
                _type_2b_from_native(AAPType2BFrameSummary, type_2b)
                if type_2b is not None else None,
            )
        return HandshakeObservation(
            ack_observed=ack,
            evidence=DescriptorEvidence(*evidence),
            pre_ack_frame_count=pre_count,
            post_ack_frame_count=post_count,
            receive_frames_dropped=dropped,
            pre_ack_frame_summaries=tuple(adapt(item) for item in pre),
            post_ack_frame_summaries=tuple(adapt(item) for item in post),
        )

    def _record_frame_timeline(
        self, first_post: bool, first_357: bool
    ) -> None:
        if first_post:
            self.timeline.record(ProtocolTimelineKind.FIRST_POST_ACK_FRAME)
        if first_357:
            self.timeline.record(ProtocolTimelineKind.FIRST_357_BYTE_FRAME)

    def _emit(self, event: AAPProgress) -> None:
        if self._progress is not None:
            self._progress(event)


@dataclass(frozen=True, slots=True)
class AAPHandshakeProbeResult:
    display_name: str
    observation: HandshakeObservation
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
            observation=handshake.observation,
            evidence=handshake.evidence,
            application_payloads_sent=handshake.application_payloads_sent,
            replacement_key_reported=secure.replacement_key_reported,
            sdp_diagnostics=diagnostics.snapshot(),
            host_state_snapshot=secure.host_state_snapshot,
        )

    def _emit(self, event: AAPProgress) -> None:
        if self._progress is not None:
            self._progress(event)
