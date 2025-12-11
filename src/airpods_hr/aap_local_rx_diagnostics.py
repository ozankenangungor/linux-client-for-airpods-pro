"""Reference-probe-only AAP local receive-MTU wire diagnostics."""

from __future__ import annotations


from dataclasses import dataclass


from bumble import l2cap

from airpods_hr.aap import HandshakeObservation


L2CAP_CLASSIC_DEFAULT_MTU = 672
AAP_POST_ACK_TYPE_17_LENGTH_LIMIT = 8
_MISSING = object()
_STOCK_SEND_CONFIGURE_REQUEST = l2cap.ClassicChannel.send_configure_request
_STOCK_SEND_CONTROL_FRAME = l2cap.ChannelManager.send_control_frame
_STOCK_CONFIGURE_RESPONSE_HANDLER = (
    l2cap.ChannelManager.on_l2cap_configure_response
)


@dataclass(frozen=True, slots=True)
class AAPPostACKShapeObservation:
    """Bounded frame-length metadata derived from canonical safe summaries."""

    first_type_0x002b_length: int | None
    type_0x0017_frame_lengths: tuple[int, ...]
    max_post_ack_frame_length: int | None
    summaries_considered: int

    @classmethod
    def from_handshake(
        cls, observation: HandshakeObservation
    ) -> AAPPostACKShapeObservation:
        first_type_2b: int | None = None
        type_17_lengths: list[int] = []
        maximum: int | None = None
        for summary in observation.post_ack_frame_summaries:
            maximum = (
                summary.length
                if maximum is None
                else max(maximum, summary.length)
            )
            if first_type_2b is None and summary.header_u16_4_5 == 0x002B:
                first_type_2b = summary.length
            if (
                summary.header_u16_4_5 == 0x0017
                and len(type_17_lengths) < AAP_POST_ACK_TYPE_17_LENGTH_LIMIT
            ):
                type_17_lengths.append(summary.length)
        return cls(
            first_type_0x002b_length=first_type_2b,
            type_0x0017_frame_lengths=tuple(type_17_lengths),
            max_post_ack_frame_length=maximum,
            summaries_considered=len(observation.post_ack_frame_summaries),
        )


