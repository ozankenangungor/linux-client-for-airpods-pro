use airpods_aap_core::{
    HEART_RATE_MARKER, HEART_RATE_REPORT_SIZE, HeartRateParseError, SourceSide,
    parse_heart_rate_packet,
};

const GOLDEN_CORPUS: &str = include_str!("../../../tests/testdata/hr_report_golden.tsv");

#[derive(Debug)]
struct GoldenCase {
    name: String,
    packet: Vec<u8>,
    outcome: String,
    bpm: Option<u8>,
    aux: Option<u8>,
    sequence: Option<u16>,
    field_5: Option<u8>,
    timestamp_ticks: Option<u64>,
    flags: Option<u32>,
}

fn decode_hex(value: &str) -> Vec<u8> {
    assert!(value.len().is_multiple_of(2), "hex input must have pairs");
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let text = std::str::from_utf8(pair).expect("fixture hex must be ASCII");
            u8::from_str_radix(text, 16).expect("fixture must contain hexadecimal bytes")
        })
        .collect()
}

fn optional_number<T: std::str::FromStr>(value: &str) -> Option<T>
where
    T::Err: std::fmt::Debug,
{
    (value != "-").then(|| value.parse().expect("fixture number must be valid"))
}

fn cases() -> Vec<GoldenCase> {
    GOLDEN_CORPUS
        .lines()
        .skip(1)
        .map(|line| {
            let fields: Vec<_> = line.split('\t').collect();
            assert_eq!(fields.len(), 9, "fixture row must have nine fields: {line}");
            GoldenCase {
                name: fields[0].to_owned(),
                packet: decode_hex(fields[1]),
                outcome: fields[2].to_owned(),
                bpm: optional_number(fields[3]),
                aux: optional_number(fields[4]),
                sequence: optional_number(fields[5]),
                field_5: optional_number(fields[6]),
                timestamp_ticks: optional_number(fields[7]),
                flags: optional_number(fields[8]),
            }
        })
        .collect()
}

fn case(name: &str) -> GoldenCase {
    cases()
        .into_iter()
        .find(|item| item.name == name)
        .unwrap_or_else(|| panic!("missing golden case: {name}"))
}

#[test]
fn every_valid_golden_vector_matches_all_python_fields() {
    for item in cases().into_iter().filter(|item| item.outcome == "valid") {
        let report = parse_heart_rate_packet(&item.packet)
            .unwrap_or_else(|error| panic!("{} unexpectedly failed: {error}", item.name));
        assert_eq!(report.bpm, item.bpm.unwrap(), "{} bpm", item.name);
        assert_eq!(report.aux, item.aux.unwrap(), "{} aux", item.name);
        assert_eq!(
            report.sequence,
            item.sequence.unwrap(),
            "{} sequence",
            item.name
        );
        assert_eq!(
            report.field_5,
            item.field_5.unwrap(),
            "{} field_5",
            item.name
        );
        assert_eq!(
            report.timestamp_ticks,
            item.timestamp_ticks.unwrap(),
            "{} timestamp_ticks",
            item.name
        );
        assert_eq!(report.flags, item.flags.unwrap(), "{} flags", item.name);
    }
}

#[test]
fn canonical_report_is_exactly_eighteen_bytes() {
    let item = case("dual_bootstrap_left_169");
    let report = parse_heart_rate_packet(&item.packet).unwrap();
    assert_eq!(report.raw_report().len(), HEART_RATE_REPORT_SIZE);
}

#[test]
fn truncated_report_matches_python_length_error() {
    let item = case("truncated_report");
    assert_eq!(
        parse_heart_rate_packet(&item.packet),
        Err(HeartRateParseError::InvalidLength {
            expected: 18,
            actual: 17,
        })
    );
}

#[test]
fn bytes_after_the_eighteen_byte_report_are_ignored_like_python() {
    let item = case("dual_steady_left");
    let report = parse_heart_rate_packet(&item.packet).unwrap();
    assert_eq!(report.bpm, 73);
    assert_eq!(report.raw_report().len(), 18);
    assert!(item.packet.ends_with(&[0xde, 0xad, 0xbe, 0xef]));
}

#[test]
fn invalid_report_id_matches_python_error() {
    let item = case("invalid_report_id");
    assert_eq!(
        parse_heart_rate_packet(&item.packet),
        Err(HeartRateParseError::InvalidReportId {
            expected: 1,
            actual: 2,
        })
    );
}

#[test]
fn missing_marker_matches_python_error() {
    let item = case("missing_marker");
    assert_eq!(
        parse_heart_rate_packet(&item.packet),
        Err(HeartRateParseError::MarkerNotFound)
    );
}

#[test]
fn sequence_uses_little_endian_u16_edges() {
    assert_eq!(case_report("single_bootstrap_right").sequence, 0x1234);
    assert_eq!(case_report("single_early_left").sequence, u16::MAX - 1);
    assert_eq!(case_report("single_steady_unknown").sequence, u16::MAX);
}

#[test]
fn timestamp_ticks_uses_little_endian_u64_edges() {
    assert_eq!(
        case_report("single_bootstrap_right").timestamp_ticks,
        0x0102_0304_0506_0708
    );
    assert_eq!(
        case_report("single_steady_unknown").timestamp_ticks,
        u64::MAX
    );
}

#[test]
fn source_side_is_derived_without_renaming_field_5() {
    let left = case_report("dual_bootstrap_left_169");
    let right = case_report("dual_early_right");
    let unknown = case_report("single_steady_unknown");

    assert_eq!((left.field_5, left.source_side()), (1, SourceSide::Left));
    assert_eq!((right.field_5, right.source_side()), (2, SourceSide::Right));
    assert_eq!(
        (unknown.field_5, unknown.source_side()),
        (127, SourceSide::Unknown(127))
    );
}

#[test]
fn bpm_169_is_preserved_unchanged() {
    assert_eq!(case_report("dual_bootstrap_left_169").bpm, 169);
}

#[test]
fn duplicate_reports_are_preserved_as_equal_data() {
    let first = case_report("duplicate_a");
    let second = case_report("duplicate_b");
    assert_eq!(first, second);
    assert_eq!(first.raw_report(), second.raw_report());
}

#[test]
fn representative_flags_remain_exact_raw_u32_values() {
    let actual: Vec<_> = [
        "dual_bootstrap_left_169",
        "dual_early_right",
        "dual_steady_left",
        "single_bootstrap_right",
        "single_early_left",
        "single_steady_unknown",
    ]
    .into_iter()
    .map(|name| case_report(name).flags)
    .collect();

    assert_eq!(
        actual,
        [
            0x8182_1001,
            0x8102_1000,
            0x0000_1000,
            0x8082_2001,
            0x8002_2000,
            0x0000_2000,
        ]
    );
}

#[test]
fn exact_raw_report_bytes_are_preserved() {
    let item = case("single_bootstrap_right");
    let marker_offset = item
        .packet
        .windows(HEART_RATE_MARKER.len())
        .position(|window| window == HEART_RATE_MARKER)
        .unwrap();
    let report_offset = marker_offset + HEART_RATE_MARKER.len();
    let report = parse_heart_rate_packet(&item.packet).unwrap();
    assert_eq!(
        report.raw_report().as_slice(),
        &item.packet[report_offset..report_offset + HEART_RATE_REPORT_SIZE]
    );
}

fn case_report(name: &str) -> airpods_aap_core::HeartRateReport {
    let item = case(name);
    parse_heart_rate_packet(&item.packet).unwrap()
}
