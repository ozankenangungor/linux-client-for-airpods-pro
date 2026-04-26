//! Pure descriptor evidence and allowlisted AAP frame shape analysis.

use std::collections::BTreeMap;

const SENSOR_FRAMEWORK_MARKERS: [&[u8]; 4] = [
    b"AccessoryService",
    b"devmotion6",
    b"MaxReportSize",
    b"ReportDescriptor",
];
const HEART_RATE_SERVICE_MARKER: &[u8] = b"HeartRateService";
const HEART_RATE_ACCESS_MARKER: &[u8] = b"com.apple.hid.heartrate-access";
const HEART_RATE_MARKER: &[u8] = b"HeartRate";
const TYPE_2B_BODY_OFFSET: usize = 17;
const TYPE_2B_UNIT_SIZE: usize = 17;
const SUFFIX_HISTOGRAM_LIMIT: usize = 8;

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct DescriptorEvidence {
    pub sensor_framework: bool,
    pub heart_rate_service: bool,
    pub heart_rate: bool,
    pub heartrate_access: bool,
}

impl DescriptorEvidence {
    #[must_use]
    pub fn merged(self, frame: &[u8]) -> Self {
        Self {
            sensor_framework: self.sensor_framework
                || SENSOR_FRAMEWORK_MARKERS
                    .iter()
                    .any(|marker| contains(frame, marker)),
            heart_rate_service: self.heart_rate_service
                || contains(frame, HEART_RATE_SERVICE_MARKER),
            heart_rate: self.heart_rate || contains_standalone_heart_rate(frame),
            heartrate_access: self.heartrate_access || contains(frame, HEART_RATE_ACCESS_MARKER),
        }
    }
}

fn contains(frame: &[u8], marker: &[u8]) -> bool {
    frame.windows(marker.len()).any(|window| window == marker)
}

fn contains_standalone_heart_rate(frame: &[u8]) -> bool {
    frame
        .windows(HEART_RATE_MARKER.len())
        .enumerate()
        .any(|(start, window)| {
            window == HEART_RATE_MARKER
                && (start == 0 || !frame[start - 1].is_ascii_alphanumeric())
                && (start + HEART_RATE_MARKER.len() == frame.len()
                    || !frame[start + HEART_RATE_MARKER.len()].is_ascii_alphanumeric())
        })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RecordSuffixSummary {
    pub suffix_field_u8: u8,
    pub suffix_field_u16: u16,
    pub count: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AapType2bFrameSummary {
    pub frame_length: usize,
    pub header_u8_6: Option<u8>,
    pub declared_body_length_u16_7_8: Option<u16>,
    pub actual_body_length_after_offset_17: Option<usize>,
    pub declared_body_length_consistent: Option<bool>,
    pub body_aligned_to_17_bytes: Option<bool>,
    pub record_count_17: Option<usize>,
    pub record_suffix_distinct_count: Option<usize>,
    pub record_suffix_histogram: Vec<RecordSuffixSummary>,
    pub unit_bytes_8_13_uniform: Option<bool>,
}

impl AapType2bFrameSummary {
    #[must_use]
    pub fn from_frame(frame: &[u8]) -> Self {
        let declared = frame
            .get(7..9)
            .map(|bytes| u16::from_le_bytes([bytes[0], bytes[1]]));
        let actual = frame.len().checked_sub(TYPE_2B_BODY_OFFSET);
        let consistent = declared
            .zip(actual)
            .map(|(declared, actual)| usize::from(declared) == actual);
        let aligned = actual.map(|actual| actual % TYPE_2B_UNIT_SIZE == 0);
        let mut summary = Self {
            frame_length: frame.len(),
            header_u8_6: frame.get(6).copied(),
            declared_body_length_u16_7_8: declared,
            actual_body_length_after_offset_17: actual,
            declared_body_length_consistent: consistent,
            body_aligned_to_17_bytes: aligned,
            record_count_17: None,
            record_suffix_distinct_count: None,
            record_suffix_histogram: Vec::new(),
            unit_bytes_8_13_uniform: None,
        };
        if consistent == Some(true) && aligned == Some(true) {
            let body = &frame[TYPE_2B_BODY_OFFSET..];
            let record_count = body.len() / TYPE_2B_UNIT_SIZE;
            let mut suffix_counts = BTreeMap::<(u8, u16), usize>::new();
            let first_hidden = body.get(8..14);
            let mut uniform = true;
            for unit in body.chunks_exact(TYPE_2B_UNIT_SIZE) {
                let suffix = (unit[14], u16::from_le_bytes([unit[15], unit[16]]));
                *suffix_counts.entry(suffix).or_default() += 1;
                uniform &= first_hidden == Some(&unit[8..14]);
            }
            summary.record_count_17 = Some(record_count);
            summary.record_suffix_distinct_count = Some(suffix_counts.len());
            summary.record_suffix_histogram = suffix_counts
                .into_iter()
                .take(SUFFIX_HISTOGRAM_LIMIT)
                .map(
                    |((suffix_field_u8, suffix_field_u16), count)| RecordSuffixSummary {
                        suffix_field_u8,
                        suffix_field_u16,
                        count,
                    },
                )
                .collect();
            summary.unit_bytes_8_13_uniform = (record_count != 0).then_some(uniform);
        }
        summary
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AapFrameSummary {
    pub length: usize,
    pub header_u16_2_3: Option<u16>,
    pub header_u16_4_5: Option<u16>,
    pub type_2b_summary: Option<AapType2bFrameSummary>,
}

impl AapFrameSummary {
    #[must_use]
    pub fn from_frame(frame: &[u8]) -> Self {
        let header_u16_2_3 = frame
            .get(2..4)
            .map(|bytes| u16::from_le_bytes([bytes[0], bytes[1]]));
        let header_u16_4_5 = frame
            .get(4..6)
            .map(|bytes| u16::from_le_bytes([bytes[0], bytes[1]]));
        Self {
            length: frame.len(),
            header_u16_2_3,
            header_u16_4_5,
            type_2b_summary: (header_u16_4_5 == Some(0x002b))
                .then(|| AapType2bFrameSummary::from_frame(frame)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(suffixes: &[(u8, u16)], hidden: Option<&[[u8; 6]]>) -> Vec<u8> {
        let mut frame = vec![0; TYPE_2B_BODY_OFFSET];
        frame[2..4].copy_from_slice(&0x1234_u16.to_le_bytes());
        frame[4..6].copy_from_slice(&0x002b_u16.to_le_bytes());
        frame[6] = 0xff;
        for (index, (suffix_u8, suffix_u16)) in suffixes.iter().enumerate() {
            let mut unit = [0; TYPE_2B_UNIT_SIZE];
            unit[8..14].copy_from_slice(&hidden.map_or(*b"SYNTH!", |fields| fields[index]));
            unit[14] = *suffix_u8;
            unit[15..17].copy_from_slice(&suffix_u16.to_le_bytes());
            frame.extend_from_slice(&unit);
        }
        let body_length = u16::try_from(frame.len() - 17).unwrap();
        frame[7..9].copy_from_slice(&body_length.to_le_bytes());
        frame
    }

    #[test]
    fn descriptor_markers_accumulate_and_remain_monotonic() {
        for marker in SENSOR_FRAMEWORK_MARKERS {
            assert!(
                DescriptorEvidence::default()
                    .merged(marker)
                    .sensor_framework
            );
        }
        let evidence = DescriptorEvidence::default()
            .merged(b"AccessoryService")
            .merged(b"HeartRateService")
            .merged(b"com.apple.hid.heartrate-access")
            .merged(b"_HeartRate_")
            .merged(b"unrelated");
        assert_eq!(
            evidence,
            DescriptorEvidence {
                sensor_framework: true,
                heart_rate_service: true,
                heart_rate: true,
                heartrate_access: true
            }
        );
        assert!(
            !DescriptorEvidence::default()
                .merged(b"unrelated")
                .sensor_framework
        );
    }

    #[test]
    fn heart_rate_ascii_boundaries_match_python_regex() {
        for value in [
            b"HeartRate".as_slice(),
            b"\0HeartRate\0",
            b"_HeartRate_",
            b"-HeartRate-",
            b"HeartRate!",
            b"!HeartRate",
            b"\xffHeartRate\xff",
        ] {
            assert!(
                DescriptorEvidence::default().merged(value).heart_rate,
                "{value:?}"
            );
        }
        for value in [
            b"xHeartRate".as_slice(),
            b"HeartRate2",
            b"AHeartRateB",
            b"9HeartRate",
            b"HeartRatez",
            b"HeartRateService",
        ] {
            assert!(
                !DescriptorEvidence::default().merged(value).heart_rate,
                "{value:?}"
            );
        }
        assert!(
            DescriptorEvidence::default()
                .merged(b"xHeartRate HeartRate2 _HeartRate")
                .heart_rate
        );
    }

    #[test]
    fn all_short_lengths_and_header_endianness() {
        for length in 0..=20 {
            let mut bytes = vec![0; length];
            if length >= 4 {
                bytes[2..4].copy_from_slice(&0x1234_u16.to_le_bytes());
            }
            if length >= 6 {
                bytes[4..6].copy_from_slice(&0x002b_u16.to_le_bytes());
            }
            let summary = AapFrameSummary::from_frame(&bytes);
            assert_eq!(summary.length, length);
            assert_eq!(summary.header_u16_2_3, (length >= 4).then_some(0x1234));
            assert_eq!(summary.header_u16_4_5, (length >= 6).then_some(0x002b));
            assert_eq!(summary.type_2b_summary.is_some(), length >= 6);
            if let Some(inner) = summary.type_2b_summary {
                assert_eq!(inner.header_u8_6, (length >= 7).then_some(0));
                assert_eq!(
                    inner.declared_body_length_u16_7_8,
                    (length >= 9).then_some(0)
                );
                assert_eq!(
                    inner.actual_body_length_after_offset_17,
                    length.checked_sub(17)
                );
                assert_eq!(
                    inner.body_aligned_to_17_bytes,
                    length.checked_sub(17).map(|n| n % 17 == 0)
                );
            }
        }
        let mut non_type = frame(&[], None);
        non_type[4] = 0x2a;
        assert!(
            AapFrameSummary::from_frame(&non_type)
                .type_2b_summary
                .is_none()
        );
    }

    #[test]
    fn zero_one_and_multiple_records() {
        let zero = AapType2bFrameSummary::from_frame(&frame(&[], None));
        assert_eq!(zero.record_count_17, Some(0));
        assert_eq!(zero.record_suffix_distinct_count, Some(0));
        assert_eq!(zero.unit_bytes_8_13_uniform, None);
        assert!(zero.record_suffix_histogram.is_empty());
        let one = AapType2bFrameSummary::from_frame(&frame(&[(0xff, 0xffff)], None));
        assert_eq!(one.header_u8_6, Some(0xff));
        assert_eq!(one.record_count_17, Some(1));
        assert_eq!(
            one.record_suffix_histogram,
            vec![RecordSuffixSummary {
                suffix_field_u8: 0xff,
                suffix_field_u16: 0xffff,
                count: 1
            }]
        );
        assert_eq!(one.unit_bytes_8_13_uniform, Some(true));
        let multiple =
            AapType2bFrameSummary::from_frame(&frame(&[(2, 3), (1, 0xffff), (2, 3)], None));
        assert_eq!(multiple.record_count_17, Some(3));
        assert_eq!(multiple.record_suffix_distinct_count, Some(2));
        assert_eq!(
            multiple.record_suffix_histogram,
            vec![
                RecordSuffixSummary {
                    suffix_field_u8: 1,
                    suffix_field_u16: 0xffff,
                    count: 1
                },
                RecordSuffixSummary {
                    suffix_field_u8: 2,
                    suffix_field_u16: 3,
                    count: 2
                }
            ]
        );
    }

    #[test]
    fn malformed_lengths_and_trailing_bytes_suppress_records() {
        let mut mismatch = frame(&[(1, 2)], None);
        mismatch[7] = 16;
        let summary = AapType2bFrameSummary::from_frame(&mismatch);
        assert_eq!(summary.declared_body_length_consistent, Some(false));
        assert_eq!(summary.body_aligned_to_17_bytes, Some(true));
        assert_eq!(summary.record_count_17, None);
        assert_eq!(summary.unit_bytes_8_13_uniform, None);
        assert!(summary.record_suffix_histogram.is_empty());
        let mut trailing = frame(&[(1, 2)], None);
        trailing.push(0xee);
        trailing[7] = 18;
        let summary = AapType2bFrameSummary::from_frame(&trailing);
        assert_eq!(summary.declared_body_length_consistent, Some(true));
        assert_eq!(summary.body_aligned_to_17_bytes, Some(false));
        assert_eq!(summary.record_count_17, None);
        let short = AapType2bFrameSummary::from_frame(&[0; 8]);
        assert_eq!(short.declared_body_length_consistent, None);
        assert_eq!(short.body_aligned_to_17_bytes, None);
    }

    #[test]
    fn histogram_limit_and_hidden_field_uniformity() {
        let suffixes: Vec<_> = (0..11).rev().map(|n| (n, 0x100 + u16::from(n))).collect();
        let summary = AapType2bFrameSummary::from_frame(&frame(&suffixes, None));
        assert_eq!(summary.record_suffix_distinct_count, Some(11));
        assert_eq!(summary.record_suffix_histogram.len(), 8);
        assert_eq!(summary.record_suffix_histogram[0].suffix_field_u8, 0);
        assert_eq!(summary.record_suffix_histogram[7].suffix_field_u8, 7);
        let differing = frame(&[(1, 2), (3, 4)], Some(&[*b"SYNTH!", *b"OTHER!"]));
        assert_eq!(
            AapType2bFrameSummary::from_frame(&differing).unit_bytes_8_13_uniform,
            Some(false)
        );
    }
}
