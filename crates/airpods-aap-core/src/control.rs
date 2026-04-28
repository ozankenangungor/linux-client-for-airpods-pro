//! Pure interpretation of the observed heart-rate control frames.

use crate::HEART_RATE_MARKER;

const ENVELOPE: &[u8] = b"\x04\x00\x04\x00\x17\x00\x00\x00";
const FIXED_WORD: &[u8] = b"\x10\x00";
const ACK_REMAINDER: &[u8] = b"\x4a\x02\x08";
const BOOTSTRAP_10: &[u8] = b"\x62\x02\x08\x10";
const BOOTSTRAP_11_12_13: &[u8] = b"\x62\x02\x08\x11\x62\x02\x08\x12\x62\x02\x08\x13";
const CONNECT4_ACK: &[u8] =
    b"\x01\x00\x04\x00\x85\x00\x01\x00\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00";
const GROUP: &[u8] = b"\x62\x02\x08";

fn u16_at(frame: &[u8], start: usize) -> Option<u16> {
    frame
        .get(start..start + 2)
        .map(|s| u16::from_le_bytes([s[0], s[1]]))
}

fn canonical_current(value: &[u8]) -> bool {
    match value {
        [a] => *a < 0x80,
        [a, b] => *a >= 0x80 && *b < 0x80 && *b != 0,
        _ => false,
    }
}

fn canonical_candidate(value: &[u8]) -> bool {
    (1..=5).contains(&value.len())
        && value[..value.len() - 1].iter().all(|byte| *byte >= 0x80)
        && value[value.len() - 1] < 0x80
        && (value.len() == 1 || value[value.len() - 1] != 0)
}

fn observed_body(frame: &[u8], remainder: &[u8]) -> bool {
    (1..=2).any(|octets| {
        frame.get(13..13 + octets).is_some_and(canonical_current)
            && frame.get(13 + octets..).is_some_and(|body| {
                [b"\x10\x01".as_slice(), b"\x10\x03".as_slice()]
                    .iter()
                    .any(|prefix| body.starts_with(prefix) && &body[prefix.len()..] == remainder)
            })
    })
}

fn observed_bootstrap(frame: &[u8], remainder: &[u8]) -> bool {
    frame.len() >= 13 + 1 + 2 + remainder.len()
        && frame.get(..8) == Some(ENVELOPE)
        && frame.get(8..10) == Some(FIXED_WORD)
        && u16_at(frame, 10).is_some_and(|n| usize::from(n) == frame.len() - 12)
        && frame.get(12) == Some(&8)
        && observed_body(frame, remainder)
}

fn ends_with_control_tail(frame: &[u8], remainder: &[u8]) -> bool {
    frame.len() >= 2 + remainder.len()
        && frame.ends_with(remainder)
        && frame[..frame.len() - remainder.len()].ends_with(b"\x10\x01")
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ControlFrameSummary {
    pub length: usize,
    pub header_u16_2_3: Option<u16>,
    pub header_u16_4_5: Option<u16>,
    pub word_u16_8_9: Option<u16>,
    pub word_u16_10_11: Option<u16>,
    pub outer_service_envelope_match: bool,
    pub fixed_word_10_00_match: bool,
    pub trailing_length_consistent: Option<bool>,
    pub tag_08_at_offset_12: Option<bool>,
    pub service_ack_suffix_0e: bool,
    pub service_ack_suffix_13: bool,
    pub candidate_identifier_terminated: Option<bool>,
    pub candidate_identifier_octets: Option<usize>,
    pub candidate_identifier_canonical: Option<bool>,
    pub identifier_is_current_canonical_1_or_2: Option<bool>,
    pub post_identifier_length: Option<usize>,
    pub post_identifier_prefix_octet_0: Option<u8>,
    pub post_identifier_prefix_octet_1: Option<u8>,
    pub post_identifier_starts_10_01: Option<bool>,
    pub post_identifier_prefix_is_observed: Option<bool>,
    pub post_identifier_field_tag: Option<u8>,
    pub post_identifier_field_parameter: Option<u8>,
    pub remainder_is_ack_0e_shape: Option<bool>,
    pub remainder_is_ack_13_shape: Option<bool>,
    pub remainder_is_bootstrap_10_shape: Option<bool>,
    pub remainder_is_bootstrap_11_12_13_shape: Option<bool>,
    pub terminal_tag_08: bool,
    pub terminal_value: Option<u8>,
    pub observed_62_02_08_group_count: Option<usize>,
    pub observed_62_02_08_terminal_values: Option<Vec<u8>>,
    pub observed_62_02_08_group_offsets: Option<Vec<usize>>,
    pub bootstrap_tail_10_suffix_present: bool,
    pub bootstrap_tail_11_12_13_suffix_present: bool,
    pub bootstrap_tail_10: bool,
    pub bootstrap_tail_11_12_13: bool,
    pub heart_rate_marker_present: bool,
}

impl ControlFrameSummary {
    #[must_use]
    pub fn from_frame(frame: &[u8]) -> Self {
        let envelope = frame.get(..8) == Some(ENVELOPE);
        let fixed = frame.get(8..10) == Some(FIXED_WORD);
        let trailing = u16_at(frame, 10).map(|n| usize::from(n) == frame.len() - 12);
        let tag = frame.get(12).map(|value| *value == 8);
        let mut terminated = None;
        let mut octets = None;
        let mut canonical = None;
        let mut current = None;
        let mut post = None;
        if envelope && fixed && trailing == Some(true) && tag == Some(true) {
            terminated = Some(false);
            let limit = frame.len().saturating_sub(13).min(5);
            octets = Some(limit);
            canonical = Some(false);
            current = Some(false);
            for offset in 0..limit {
                if frame[13 + offset] < 0x80 {
                    let end = 14 + offset;
                    let identifier = &frame[13..end];
                    terminated = Some(true);
                    octets = Some(offset + 1);
                    canonical = Some(canonical_candidate(identifier));
                    current = Some(canonical_current(identifier));
                    post = Some(&frame[end..]);
                    break;
                }
            }
        }
        let prefix = post.filter(|body: &&[u8]| body.len() >= 2);
        let remainder = prefix.map(|body| &body[2..]);
        let starts_10_01 = post.map(|body| body.starts_with(b"\x10\x01"));
        let mut group_count = None;
        let mut group_values = None;
        let mut group_offsets = None;
        if let Some(body) = post {
            let mut count = 0;
            let mut values = Vec::new();
            let mut offsets = Vec::new();
            let mut offset = 0;
            while offset + 4 <= body.len() {
                if body.get(offset..offset + 3) == Some(GROUP) {
                    count += 1;
                    if count <= 4 {
                        values.push(body[offset + 3]);
                        offsets.push(offset);
                    }
                    offset += 4;
                } else {
                    offset += 1;
                }
            }
            group_count = Some(count);
            group_offsets = Some(offsets);
            if count <= 4 {
                group_values = Some(values);
            }
        }
        let terminal_tag = frame.len() >= 2 && frame[frame.len() - 2] == 8;
        Self {
            length: frame.len(),
            header_u16_2_3: u16_at(frame, 2),
            header_u16_4_5: u16_at(frame, 4),
            word_u16_8_9: u16_at(frame, 8),
            word_u16_10_11: u16_at(frame, 10),
            outer_service_envelope_match: envelope,
            fixed_word_10_00_match: fixed,
            trailing_length_consistent: trailing,
            tag_08_at_offset_12: tag,
            service_ack_suffix_0e: frame.ends_with(b"\x10\x01\x4a\x02\x08\x0e"),
            service_ack_suffix_13: frame.ends_with(b"\x10\x01\x4a\x02\x08\x13"),
            candidate_identifier_terminated: terminated,
            candidate_identifier_octets: octets,
            candidate_identifier_canonical: canonical,
            identifier_is_current_canonical_1_or_2: current,
            post_identifier_length: post.map(<[u8]>::len),
            post_identifier_prefix_octet_0: prefix.map(|body| body[0]),
            post_identifier_prefix_octet_1: prefix.map(|body| body[1]),
            post_identifier_starts_10_01: starts_10_01,
            post_identifier_prefix_is_observed: prefix
                .map(|body| body.starts_with(b"\x10\x01") || body.starts_with(b"\x10\x03")),
            post_identifier_field_tag: post
                .filter(|body| body.len() >= 3 && body.starts_with(b"\x10\x01"))
                .map(|body| body[2]),
            post_identifier_field_parameter: post
                .filter(|body| body.len() >= 4 && body.starts_with(b"\x10\x01"))
                .map(|body| body[3]),
            remainder_is_ack_0e_shape: remainder.map(|body| body == b"\x4a\x02\x08\x0e"),
            remainder_is_ack_13_shape: remainder.map(|body| body == b"\x4a\x02\x08\x13"),
            remainder_is_bootstrap_10_shape: remainder.map(|body| body == BOOTSTRAP_10),
            remainder_is_bootstrap_11_12_13_shape: remainder.map(|body| body == BOOTSTRAP_11_12_13),
            terminal_tag_08: terminal_tag,
            terminal_value: terminal_tag.then(|| frame[frame.len() - 1]),
            observed_62_02_08_group_count: group_count,
            observed_62_02_08_terminal_values: group_values,
            observed_62_02_08_group_offsets: group_offsets,
            bootstrap_tail_10_suffix_present: ends_with_control_tail(frame, BOOTSTRAP_10),
            bootstrap_tail_11_12_13_suffix_present: ends_with_control_tail(
                frame,
                BOOTSTRAP_11_12_13,
            ),
            bootstrap_tail_10: observed_bootstrap(frame, BOOTSTRAP_10),
            bootstrap_tail_11_12_13: observed_bootstrap(frame, BOOTSTRAP_11_12_13),
            heart_rate_marker_present: frame
                .windows(HEART_RATE_MARKER.len())
                .any(|bytes| bytes == HEART_RATE_MARKER),
        }
    }
}

#[must_use]
pub fn is_service_ack_candidate_shape(frame: &[u8]) -> bool {
    frame.len() >= 20
        && frame.get(..8) == Some(ENVELOPE)
        && frame.get(8..10) == Some(FIXED_WORD)
        && u16_at(frame, 10).is_some_and(|n| usize::from(n) == frame.len() - 12)
        && frame.get(12) == Some(&8)
}

#[must_use]
pub fn is_observed_service_ack(frame: &[u8], service_id: u8) -> bool {
    matches!(service_id, 0x0e | 0x13)
        && is_service_ack_candidate_shape(frame)
        && observed_body(frame, &[ACK_REMAINDER, &[service_id]].concat())
}

#[must_use]
pub fn is_connect4_ack(frame: &[u8]) -> bool {
    frame == CONNECT4_ACK
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(identifier: &[u8], body: &[u8]) -> Vec<u8> {
        let mut result = ENVELOPE.to_vec();
        result.extend_from_slice(FIXED_WORD);
        let length = u16::try_from(1 + identifier.len() + body.len()).unwrap();
        result.extend_from_slice(&length.to_le_bytes());
        result.push(8);
        result.extend_from_slice(identifier);
        result.extend_from_slice(body);
        result
    }

    #[test]
    fn short_boundaries_headers_and_envelope_bytes() {
        for length in 0..=20 {
            let mut bytes = vec![0; length];
            if length >= 4 {
                bytes[2..4].copy_from_slice(&0x1234_u16.to_le_bytes());
            }
            if length >= 6 {
                bytes[4..6].copy_from_slice(&0x5678_u16.to_le_bytes());
            }
            if length >= 10 {
                bytes[8..10].copy_from_slice(&0xabcd_u16.to_le_bytes());
            }
            if length >= 12 {
                bytes[10..12].copy_from_slice(&0xef01_u16.to_le_bytes());
            }
            let summary = ControlFrameSummary::from_frame(&bytes);
            assert_eq!(summary.length, length);
            assert_eq!(summary.header_u16_2_3, (length >= 4).then_some(0x1234));
            assert_eq!(summary.header_u16_4_5, (length >= 6).then_some(0x5678));
            assert_eq!(summary.word_u16_8_9, (length >= 10).then_some(0xabcd));
            assert_eq!(summary.word_u16_10_11, (length >= 12).then_some(0xef01));
            assert_eq!(
                summary.trailing_length_consistent,
                (length >= 12).then_some(false)
            );
            assert_eq!(summary.tag_08_at_offset_12, (length >= 13).then_some(false));
        }
        let good = frame(b"\x01", b"\x10\x01\x4a\x02\x08\x0e");
        assert!(is_observed_service_ack(&good, 0x0e));
        for offset in 0..8 {
            let mut changed = good.clone();
            changed[offset] ^= 1;
            assert!(!ControlFrameSummary::from_frame(&changed).outer_service_envelope_match);
            assert!(!is_observed_service_ack(&changed, 0x0e));
        }
        for offset in 8..13 {
            let mut changed = good.clone();
            changed[offset] ^= 1;
            assert!(!is_observed_service_ack(&changed, 0x0e));
        }
        assert_eq!(
            ControlFrameSummary::from_frame(&good).word_u16_10_11,
            Some(8)
        );
    }

    #[test]
    fn identifier_boundaries_and_ack_prefixes() {
        for byte in [0, 1, 0x7f, 0x80, 0x81, 0xff] {
            assert_eq!(canonical_current(&[byte]), byte < 0x80);
            assert_eq!(canonical_candidate(&[byte]), byte < 0x80);
        }
        for identifier in [
            b"\x00".as_slice(),
            b"\x01",
            b"\x7f",
            b"\x80\x01",
            b"\xff\x7f",
            b"\x80\x80\x01",
            b"\x80\x80\x80\x01",
            b"\x80\x80\x80\x80\x01",
            b"\x80\x00",
            b"\x80\x80\x00",
            b"\x80\x80\x80\x80\x00",
            b"\x80",
            b"\x81",
            b"\xff",
            b"\x80\x80\x80\x80\x80",
            b"\x80\x80\x80\x80\x80\x01",
        ] {
            let bytes = frame(identifier, b"\x10\x01\x4a\x02\x08\x0e");
            let s = ControlFrameSummary::from_frame(&bytes);
            let candidate = &bytes[13..];
            let terminated = candidate.iter().take(5).any(|byte| *byte < 0x80);
            assert_eq!(s.candidate_identifier_terminated, Some(terminated));
            if terminated {
                let end = candidate
                    .iter()
                    .take(5)
                    .position(|byte| *byte < 0x80)
                    .unwrap()
                    + 1;
                assert_eq!(s.candidate_identifier_octets, Some(end));
                assert_eq!(
                    s.candidate_identifier_canonical,
                    Some(canonical_candidate(&candidate[..end]))
                );
                assert_eq!(
                    s.identifier_is_current_canonical_1_or_2,
                    Some(canonical_current(&candidate[..end]))
                );
            } else {
                assert_eq!(s.candidate_identifier_octets, Some(5));
            }
            assert_eq!(
                is_observed_service_ack(&bytes, 0x0e),
                canonical_current(identifier)
            );
        }
        for prefix in [b"\x10\x01", b"\x10\x03", b"\x10\x04", b"\x11\x01"] {
            let bytes = frame(b"\x01", &[prefix.as_slice(), b"\x4a\x02\x08\x13"].concat());
            let s = ControlFrameSummary::from_frame(&bytes);
            assert_eq!(
                s.post_identifier_prefix_is_observed,
                Some(prefix != b"\x10\x04" && prefix != b"\x11\x01")
            );
            assert_eq!(s.remainder_is_ack_13_shape, Some(true));
            assert_eq!(
                is_observed_service_ack(&bytes, 0x13),
                prefix == b"\x10\x01" || prefix == b"\x10\x03"
            );
            assert!(!is_observed_service_ack(&bytes, 0x0e));
            assert!(!is_observed_service_ack(&bytes, 0x20));
        }
    }

    #[test]
    fn varint_boundary_octets_at_each_length() {
        const BOUNDARIES: [u8; 6] = [0, 1, 0x7f, 0x80, 0x81, 0xff];
        for length in 1..=7 {
            for first in BOUNDARIES {
                for last in BOUNDARIES {
                    let mut value = vec![0x80; length];
                    value[0] = first;
                    value[length - 1] = last;
                    let expected_candidate = length <= 5
                        && value[..length - 1].iter().all(|byte| *byte >= 0x80)
                        && value[length - 1] < 0x80
                        && (length == 1 || value[length - 1] != 0);
                    assert_eq!(canonical_candidate(&value), expected_candidate, "{value:?}");
                    let expected_current = length == 1 && last < 0x80
                        || length == 2 && first >= 0x80 && last > 0 && last < 0x80;
                    assert_eq!(canonical_current(&value), expected_current, "{value:?}");
                }
            }
        }
    }

    #[test]
    fn bootstrap_groups_terminal_and_marker() {
        let tail10 = frame(b"\x01", b"\x10\x01\x62\x02\x08\x10");
        let s = ControlFrameSummary::from_frame(&tail10);
        assert!(s.bootstrap_tail_10 && s.bootstrap_tail_10_suffix_present);
        assert_eq!(s.remainder_is_bootstrap_10_shape, Some(true));
        assert_eq!(s.observed_62_02_08_group_count, Some(1));
        assert_eq!(s.observed_62_02_08_terminal_values, Some(vec![0x10]));
        assert_eq!(s.observed_62_02_08_group_offsets, Some(vec![2]));
        let tail_long = frame(
            b"\x80\x01",
            b"\x10\x03\x62\x02\x08\x11\x62\x02\x08\x12\x62\x02\x08\x13",
        );
        let s = ControlFrameSummary::from_frame(&tail_long);
        assert!(s.bootstrap_tail_11_12_13);
        assert!(!s.bootstrap_tail_11_12_13_suffix_present);
        assert_eq!(s.observed_62_02_08_group_count, Some(3));
        assert_eq!(s.observed_62_02_08_group_offsets, Some(vec![2, 6, 10]));
        let suffix_only = [b"noise".as_slice(), b"\x10\x01\x62\x02\x08\x10"].concat();
        let s = ControlFrameSummary::from_frame(&suffix_only);
        assert!(s.bootstrap_tail_10_suffix_present);
        assert!(!s.bootstrap_tail_10);
        let many = frame(
            b"\x01",
            &[
                b"\x10\x01".as_slice(),
                b"\x62\x02\x08\x01".repeat(6).as_slice(),
            ]
            .concat(),
        );
        let s = ControlFrameSummary::from_frame(&many);
        assert_eq!(s.observed_62_02_08_group_count, Some(6));
        assert_eq!(s.observed_62_02_08_terminal_values, None);
        assert_eq!(s.observed_62_02_08_group_offsets, Some(vec![2, 6, 10, 14]));
        assert_eq!(
            ControlFrameSummary::from_frame(b"\x08\xff").terminal_value,
            Some(0xff)
        );
        assert_eq!(
            ControlFrameSummary::from_frame(b"\x07\xff").terminal_value,
            None
        );
        assert!(ControlFrameSummary::from_frame(&HEART_RATE_MARKER).heart_rate_marker_present);
        assert!(!ControlFrameSummary::from_frame(b"HeartRate").heart_rate_marker_present);
    }

    #[test]
    fn exact_connect_ack_and_malformed_input_never_panics() {
        assert!(is_connect4_ack(CONNECT4_ACK));
        for offset in 0..CONNECT4_ACK.len() {
            let mut changed = CONNECT4_ACK.to_vec();
            changed[offset] ^= 1;
            assert!(!is_connect4_ack(&changed));
        }
        let mut seed = 103_u64;
        for length in 0..=512 {
            let mut bytes = vec![0; length];
            for byte in &mut bytes {
                seed ^= seed << 13;
                seed ^= seed >> 7;
                seed ^= seed << 17;
                *byte = seed as u8;
            }
            let _ = ControlFrameSummary::from_frame(&bytes);
            let _ = is_observed_service_ack(&bytes, 0x0e);
            let _ = is_observed_service_ack(&bytes, 0x13);
            let _ = is_connect4_ack(&bytes);
        }
    }
}
