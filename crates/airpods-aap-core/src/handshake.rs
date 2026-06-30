//! Bounded, payload-free state for the AAP handshake observation.

use crate::{AapFrameSummary, DescriptorEvidence};

pub const HANDSHAKE_ACK: &[u8] = &[1, 0, 4, 0, 0, 0, 1, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FrameDecision {
    pub ack_observed_now: bool,
    pub first_post_ack_frame: bool,
    pub first_357_byte_frame: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct HandshakeAccumulator {
    pub ack_observed: bool,
    pub evidence: DescriptorEvidence,
    pub pre_ack_frame_count: usize,
    pub post_ack_frame_count: usize,
    pub receive_frames_dropped: usize,
    pub pre_ack_frame_summaries: Vec<AapFrameSummary>,
    pub post_ack_frame_summaries: Vec<AapFrameSummary>,
    summary_limit: usize,
    first_post_ack_frame_observed: bool,
    first_357_byte_frame_observed: bool,
}

impl HandshakeAccumulator {
    pub fn new(summary_limit: usize) -> Option<Self> {
        (summary_limit > 0).then(|| Self {
            ack_observed: false,
            evidence: DescriptorEvidence::default(),
            pre_ack_frame_count: 0,
            post_ack_frame_count: 0,
            receive_frames_dropped: 0,
            pre_ack_frame_summaries: Vec::new(),
            post_ack_frame_summaries: Vec::new(),
            summary_limit,
            first_post_ack_frame_observed: false,
            first_357_byte_frame_observed: false,
        })
    }

    #[must_use]
    pub fn descriptors_complete(&self) -> bool {
        self.evidence.required()
    }

    #[must_use]
    pub fn observe(&mut self, frame: &[u8], dropped_frames: usize) -> FrameDecision {
        self.receive_frames_dropped = dropped_frames;
        if !self.ack_observed && frame == HANDSHAKE_ACK {
            self.ack_observed = true;
            return FrameDecision {
                ack_observed_now: true,
                first_post_ack_frame: false,
                first_357_byte_frame: false,
            };
        }
        let post_ack = self.ack_observed;
        if post_ack {
            self.post_ack_frame_count += 1;
        } else {
            self.pre_ack_frame_count += 1;
        }
        let first_post_ack_frame = post_ack && !self.first_post_ack_frame_observed;
        self.first_post_ack_frame_observed |= post_ack;
        let first_357_byte_frame = frame.len() == 357 && !self.first_357_byte_frame_observed;
        self.first_357_byte_frame_observed |= frame.len() == 357;
        if self.pre_ack_frame_summaries.len() + self.post_ack_frame_summaries.len()
            < self.summary_limit
        {
            let summary = AapFrameSummary::from_frame(frame);
            if post_ack {
                self.post_ack_frame_summaries.push(summary);
            } else {
                self.pre_ack_frame_summaries.push(summary);
            }
        }
        self.evidence = self.evidence.merged(frame);
        FrameDecision {
            ack_observed_now: false,
            first_post_ack_frame,
            first_357_byte_frame,
        }
    }

    pub fn snapshot_dropped(&mut self, dropped_frames: usize) {
        self.receive_frames_dropped = dropped_frames;
    }
}

#[must_use]
pub fn expected_payload_count(count: usize) -> bool {
    count == 1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_ack_and_near_ack() {
        let mut state = HandshakeAccumulator::new(1).unwrap();
        assert!(!state.ack_observed);
        assert!(!state.descriptors_complete());
        let mut near = HANDSHAKE_ACK.to_vec();
        near.push(0);
        assert!(!state.observe(&near, 2).ack_observed_now);
        assert_eq!(state.pre_ack_frame_count, 1);
        assert!(state.observe(HANDSHAKE_ACK, 3).ack_observed_now);
        assert_eq!(state.pre_ack_frame_count, 1);
        assert_eq!(state.receive_frames_dropped, 3);
        assert_eq!(state.pre_ack_frame_summaries.len(), 1);
        assert!(!state.observe(HANDSHAKE_ACK, 4).ack_observed_now);
        assert_eq!(state.post_ack_frame_count, 1);
    }

    #[test]
    fn descriptor_evidence_and_shared_bound() {
        let mut state = HandshakeAccumulator::new(2).unwrap();
        let _ = state.observe(b"AccessoryService", 0);
        assert!(!state.descriptors_complete());
        let _ = state.observe(HANDSHAKE_ACK, 0);
        let decision = state.observe(b"HeartRateService", 1);
        assert!(decision.first_post_ack_frame);
        assert!(state.descriptors_complete());
        assert_eq!(state.pre_ack_frame_summaries.len(), 1);
        assert_eq!(state.post_ack_frame_summaries.len(), 1);
        assert!(!state.observe(&vec![0; 357], 2).first_post_ack_frame);
        assert!(!state.observe(&vec![0; 357], 2).first_357_byte_frame);
        assert_eq!(state.post_ack_frame_summaries.len(), 1);
        assert_eq!(state.receive_frames_dropped, 2);
    }

    #[test]
    fn one_shot_357_and_payload_count() {
        let mut state = HandshakeAccumulator::new(64).unwrap();
        assert!(state.observe(&vec![0; 357], 0).first_357_byte_frame);
        let _ = state.observe(HANDSHAKE_ACK, 0);
        let decision = state.observe(&vec![0; 357], 0);
        assert!(decision.first_post_ack_frame);
        assert!(!decision.first_357_byte_frame);
        assert!(!expected_payload_count(0));
        assert!(expected_payload_count(1));
        assert!(!expected_payload_count(2));
        assert!(HandshakeAccumulator::new(0).is_none());
    }

    #[test]
    fn seeded_streams_match_independent_counts_and_bounds() {
        let mut seed = 0x1234_5678_u64;
        for limit in [1, 2, 17, 64] {
            for _ in 0..32 {
                let mut state = HandshakeAccumulator::new(limit).unwrap();
                let mut pre = 0;
                let mut post = 0;
                for index in 0..1000 {
                    seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
                    let frame = if index == 200 {
                        HANDSHAKE_ACK.to_vec()
                    } else {
                        vec![(seed >> 32) as u8; ((seed >> 16) as usize % 400) + 1]
                    };
                    let was_ack = state.ack_observed;
                    let decision = state.observe(&frame, index);
                    if index == 200 {
                        assert!(decision.ack_observed_now);
                    } else if was_ack {
                        post += 1;
                    } else {
                        pre += 1;
                    }
                    assert_eq!(state.pre_ack_frame_count, pre);
                    assert_eq!(state.post_ack_frame_count, post);
                    assert_eq!(
                        state.pre_ack_frame_summaries.len() + state.post_ack_frame_summaries.len(),
                        (pre + post).min(limit)
                    );
                }
            }
        }
    }

    #[test]
    fn ack_first_and_descriptors_before_ack_have_distinct_observation_paths() {
        let mut ack_first = HandshakeAccumulator::new(4).unwrap();
        assert!(ack_first.observe(HANDSHAKE_ACK, 0).ack_observed_now);
        assert_eq!(ack_first.pre_ack_frame_count, 0);
        assert!(!ack_first.descriptors_complete());
        assert!(
            ack_first
                .observe(b"AccessoryService", 1)
                .first_post_ack_frame
        );
        assert!(!ack_first.descriptors_complete());
        let _ = ack_first.observe(b"HeartRateService", 2);
        assert!(ack_first.descriptors_complete());
        assert_eq!(ack_first.post_ack_frame_count, 2);
        assert_eq!(ack_first.post_ack_frame_summaries.len(), 2);

        let mut pre_complete = HandshakeAccumulator::new(1).unwrap();
        let _ = pre_complete.observe(b"AccessoryService", 0);
        let _ = pre_complete.observe(b"HeartRateService", 0);
        assert!(pre_complete.descriptors_complete());
        let _ = pre_complete.observe(HANDSHAKE_ACK, 0);
        assert_eq!(pre_complete.post_ack_frame_count, 0);
        assert_eq!(pre_complete.pre_ack_frame_summaries.len(), 1);
        assert!(pre_complete.post_ack_frame_summaries.is_empty());
    }

    #[test]
    fn summaries_share_one_limit_across_both_phases() {
        for limit in [1, 2, 3, 64] {
            for pre_count in 0..=limit + 1 {
                let mut state = HandshakeAccumulator::new(limit).unwrap();
                for _ in 0..pre_count {
                    let _ = state.observe(b"pre", 0);
                }
                let _ = state.observe(HANDSHAKE_ACK, 0);
                for _ in 0..limit + 1 {
                    let _ = state.observe(b"post", 0);
                }
                assert_eq!(state.pre_ack_frame_summaries.len(), pre_count.min(limit));
                assert_eq!(
                    state.post_ack_frame_summaries.len(),
                    limit.saturating_sub(pre_count)
                );
                assert_eq!(state.pre_ack_frame_count, pre_count);
                assert_eq!(state.post_ack_frame_count, limit + 1);
            }
        }
    }

    #[test]
    fn dropped_snapshot_is_latest_observation_or_explicit_final_snapshot() {
        let mut state = HandshakeAccumulator::new(1).unwrap();
        let _ = state.observe(b"pre", 4);
        assert_eq!(state.receive_frames_dropped, 4);
        let _ = state.observe(HANDSHAKE_ACK, 8);
        assert_eq!(state.receive_frames_dropped, 8);
        state.snapshot_dropped(11);
        assert_eq!(state.receive_frames_dropped, 11);
        assert_eq!(state.pre_ack_frame_count, 1);
    }

    #[test]
    fn seeded_mixed_streams_match_reference_timeline_and_evidence() {
        let mut seed = 0xc0ff_ee12_3456_789a_u64;
        for _ in 0..32 {
            let mut state = HandshakeAccumulator::new(17).unwrap();
            let mut expected = DescriptorEvidence::default();
            let mut post_seen = false;
            let mut length_357_seen = false;
            for index in 0..512 {
                seed = seed
                    .wrapping_mul(2862933555777941757)
                    .wrapping_add(3037000493);
                let frame: Vec<u8> = match seed % 29 {
                    0 => HANDSHAKE_ACK.to_vec(),
                    1 => b"AccessoryService".to_vec(),
                    2 => b"HeartRateService".to_vec(),
                    3 => b"com.apple.hid.heartrate-access".to_vec(),
                    4 => vec![0; 357],
                    _ => (0..((seed >> 8) as usize % 64))
                        .map(|offset| ((seed >> (offset % 8)) as u8) ^ offset as u8)
                        .collect(),
                };
                let was_ack = state.ack_observed;
                let decision = state.observe(&frame, index);
                let is_ack = !was_ack && frame == HANDSHAKE_ACK;
                assert_eq!(decision.ack_observed_now, is_ack);
                if !is_ack {
                    expected = expected.merged(&frame);
                    assert_eq!(decision.first_post_ack_frame, was_ack && !post_seen);
                    assert_eq!(
                        decision.first_357_byte_frame,
                        frame.len() == 357 && !length_357_seen
                    );
                    post_seen |= was_ack;
                    length_357_seen |= frame.len() == 357;
                }
                assert_eq!(state.evidence, expected);
                assert_eq!(state.descriptors_complete(), expected.required());
                assert_eq!(state.receive_frames_dropped, index);
            }
        }
    }
}
