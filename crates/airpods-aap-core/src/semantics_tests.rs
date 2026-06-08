use super::*;

fn report(bpm: u8, sequence: u16, timestamp_ticks: u64, flags: u32) -> ReportFacts {
    ReportFacts {
        bpm,
        aux: 7,
        sequence,
        field_5: 3,
        timestamp_ticks,
        flags,
        raw_report: [0xabu8; HEART_RATE_REPORT_SIZE],
    }
}
fn recorder(scenario: Scenario) -> Recorder {
    Recorder::new(scenario, None, None, 5.0)
}
fn started(scenario: Scenario) -> Recorder {
    let mut r = recorder(scenario);
    r.commit_header().unwrap();
    r.mark_cycle_attempted(1).unwrap();
    r.begin_cycle(1, 1_000_000_000).unwrap();
    r
}
fn add(r: &mut Recorder, report: ReportFacts, ns: i128) -> SampleRecord {
    let sample = r.prepare_sample(report, ns).unwrap();
    r.commit_sample(sample.clone()).unwrap();
    sample
}
fn summary(r: &Recorder) -> CaptureSummary {
    r.prepare_summary("complete".into(), None).unwrap()
}

#[test]
fn parser_structure_matches_canonical_typed_layout() {
    assert_eq!(HEART_RATE_REPORT_SIZE, 18);
    assert_eq!(SEQUENCE_WIDTH_BITS, 16);
    assert_eq!(TIMESTAMP_WIDTH_BITS, 64);
    assert_eq!(FLAGS_WIDTH_BITS, 32);
    assert_eq!(SCHEMA_VERSION, 1);
}
#[test]
fn scenario_plan_exact_presence_and_targets() {
    assert_eq!(cycle_plan(Scenario::Baseline, Some(30), None), Ok(vec![30]));
    assert_eq!(
        cycle_plan(Scenario::ActivationRestart, None, Some(10)),
        Ok(vec![10, 10])
    );
    for args in [(None, None), (None, Some(1)), (Some(1), Some(1))] {
        assert_eq!(
            cycle_plan(Scenario::Baseline, args.0, args.1),
            Err(SemanticsError::BaselinePresence)
        );
    }
    for args in [(None, None), (Some(1), None), (Some(1), Some(1))] {
        assert_eq!(
            cycle_plan(Scenario::ActivationRestart, args.0, args.1),
            Err(SemanticsError::RestartPresence)
        );
    }
    assert_eq!(
        cycle_plan(Scenario::Baseline, Some(0), None),
        Err(SemanticsError::BaselineCount)
    );
    assert_eq!(
        cycle_plan(Scenario::Baseline, Some(-1), None),
        Err(SemanticsError::BaselineCount)
    );
    assert_eq!(
        cycle_plan(Scenario::ActivationRestart, None, Some(0)),
        Err(SemanticsError::RestartCount)
    );
    assert_eq!(
        cycle_plan(Scenario::ActivationRestart, None, Some(-1)),
        Err(SemanticsError::RestartCount)
    );
    assert_eq!(Scenario::Baseline.cycle_count(), 1);
    assert_eq!(Scenario::ActivationRestart.cycle_count(), 2);
}
#[test]
fn lifecycle_error_messages_and_order_match_parent() {
    let mut r = recorder(Scenario::ActivationRestart);
    assert_eq!(
        r.prepare_summary("failed".into(), None).unwrap_err(),
        SemanticsError::MissingHeader
    );
    assert_eq!(r.begin_cycle(1, 0), Err(SemanticsError::MissingHeader));
    assert_eq!(
        r.prepare_sample(report(1, 0, 0, 0), 0),
        Err(SemanticsError::BeforeAck)
    );
    assert_eq!(r.mark_cycle_attempted(0), Err(SemanticsError::InvalidCycle));
    assert_eq!(r.mark_cycle_attempted(3), Err(SemanticsError::InvalidCycle));
    // Parent permits marking an attempt before writing the header.
    r.mark_cycle_attempted(1).unwrap();
    assert_eq!(
        r.mark_cycle_attempted(1),
        Err(SemanticsError::SingleUseCycle)
    );
    r.commit_header().unwrap();
    assert_eq!(r.prepare_header(), Err(SemanticsError::DuplicateHeader));
    assert_eq!(r.commit_header(), Err(SemanticsError::DuplicateHeader));
    assert_eq!(r.begin_cycle(2, 0), Err(SemanticsError::UnexpectedAck));
    assert_eq!(r.begin_cycle(3, 0), Err(SemanticsError::InvalidCycle));
    r.begin_cycle(1, 0).unwrap();
    assert_eq!(r.begin_cycle(1, 1), Err(SemanticsError::UnexpectedAck));
    r.mark_cycle_attempted(2).unwrap();
    assert_eq!(r.begin_cycle(2, 1), Err(SemanticsError::ActiveCycle));
    assert_eq!(r.complete_cycle(2), Err(SemanticsError::InactiveCycle));
    assert_eq!(r.complete_cycle(3), Err(SemanticsError::InvalidCycle));
    r.abandon_cycle(2);
    assert!(r.validate_sample().is_ok());
    r.complete_cycle(1).unwrap();
    assert_eq!(r.complete_cycle(1), Err(SemanticsError::InactiveCycle));
    r.abandon_cycle(1); // Inactive abandon is a no-op in the parent.
    assert_eq!(r.validate_sample(), Err(SemanticsError::BeforeAck));
    r.begin_cycle(2, 2).unwrap();
    r.abandon_cycle(2);
    assert_eq!(r.begin_cycle(2, 3), Err(SemanticsError::UnexpectedAck));
    assert_eq!(
        r.mark_cycle_attempted(2),
        Err(SemanticsError::SingleUseCycle)
    );
    let s = summary(&r);
    assert_eq!(s.cycles_completed, 1);
    assert!(s.cycle_summaries[1].attempted);
    assert!(s.cycle_summaries[1].activation_ack_observed);
    assert!(!s.cycle_summaries[1].complete);
    r.commit_summary().unwrap();
    assert_eq!(
        r.prepare_summary("complete".into(), None).unwrap_err(),
        SemanticsError::DuplicateSummary
    );
    assert_eq!(r.commit_summary(), Err(SemanticsError::DuplicateSummary));
    for (err, text) in [
        (
            SemanticsError::DuplicateHeader,
            "semantics capture header is already written",
        ),
        (
            SemanticsError::SingleUseCycle,
            "HR semantics cycle is single-use",
        ),
        (
            SemanticsError::MissingHeader,
            "semantics capture header is not written",
        ),
        (
            SemanticsError::UnexpectedAck,
            "unexpected HR activation acknowledgement",
        ),
        (
            SemanticsError::ActiveCycle,
            "another HR semantics cycle is active",
        ),
        (
            SemanticsError::BeforeAck,
            "HR sample arrived before activation acknowledgement",
        ),
        (
            SemanticsError::InvalidRawReport,
            "canonical report does not retain 18 raw bytes",
        ),
        (
            SemanticsError::InactiveCycle,
            "completed HR semantics cycle is not active",
        ),
        (
            SemanticsError::DuplicateSummary,
            "semantics capture summary is already written",
        ),
        (
            SemanticsError::InvalidCycle,
            "cycle index is outside this scenario",
        ),
    ] {
        assert_eq!(err.message(), text);
    }
}
#[test]
fn header_content_and_sink_transaction_boundary() {
    let mut r = Recorder::new(Scenario::Baseline, Some(3), None, 5.0);
    let h = r.header(true, Some(2048)).unwrap();
    assert_eq!(h.scenario, Scenario::Baseline);
    assert_eq!(h.requested_samples, Some(3));
    assert_eq!(h.requested_samples_per_cycle, None);
    assert_eq!(h.restart_delay_seconds, 5.0);
    assert!(h.descriptor_complete);
    assert_eq!(h.local_rx_imtu, Some(2048));
    assert_eq!(TRANSPORT, "bluez-kernel-coexistence");
    assert!(!r.header_written()); // prepare alone does not commit a failed sink write.
    r.commit_header().unwrap();
    assert!(r.header_written());
}
#[test]
fn first_sample_timing_raw_and_indices() {
    let mut r = started(Scenario::Baseline);
    let first = add(
        &mut r,
        report(169, 65535, u64::MAX, 0x81800000),
        1_250_000_000,
    );
    assert_eq!(first.scenario, Scenario::Baseline);
    assert_eq!(
        (
            first.cycle_index,
            first.sample_index_within_cycle,
            first.sample_index_global
        ),
        (1, 1, 1)
    );
    assert_eq!(first.receive_monotonic_ns, 1_250_000_000);
    assert_eq!(first.delta_from_previous_report_ms, None);
    assert_eq!(first.milliseconds_since_activation_ack, 250.0);
    assert_eq!(first.report.bpm, 169);
    assert_eq!(first.flags_hex, "0x81800000");
    assert_eq!(first.flags_bits_set, vec![23, 24, 31]);
    assert_eq!(first.raw_report_hex, "ab".repeat(18));
    assert_eq!(first.report.raw_report.len(), 18);
    assert!(!first.duplicate_parsed_report);
    assert_eq!(first.sequence_delta_modulo, None);
    assert_eq!(first.timestamp_delta_modulo, None);
    let second = add(&mut r, report(42, 0, 1, 3), 1_750_000_000);
    assert_eq!(
        (second.sample_index_within_cycle, second.sample_index_global),
        (2, 2)
    );
    assert_eq!(second.delta_from_previous_report_ms, Some(500.0));
    assert_eq!(second.milliseconds_since_activation_ack, 750.0);
    assert_eq!(second.sequence_delta_modulo, Some(1));
    assert_eq!(second.timestamp_delta_modulo, Some(2));
}
#[test]
fn timing_keeps_zero_fractional_negative_and_large_values() {
    let mut r = started(Scenario::Baseline);
    let a = add(&mut r, report(1, 0, 0, 0), 1_000_000_000);
    assert_eq!(a.milliseconds_since_activation_ack, 0.0);
    let b = add(&mut r, report(1, 0, 0, 0), 1_000_001_250);
    assert_eq!(b.delta_from_previous_report_ms, Some(0.00125));
    let c = add(&mut r, report(1, 0, 0, 0), 999_999_000);
    assert_eq!(c.delta_from_previous_report_ms, Some(-0.00225));
    assert_eq!(c.milliseconds_since_activation_ack, -0.001);
    assert!(millis_between(i128::MAX, i128::MIN).is_finite());
    let mut big = recorder(Scenario::Baseline);
    big.commit_header().unwrap();
    big.mark_cycle_attempted(1).unwrap();
    big.begin_cycle(1, 10_000_000_000_000_000_000).unwrap();
    assert_eq!(
        add(&mut big, report(1, 0, 0, 0), 10_000_000_000_001_000_000)
            .milliseconds_since_activation_ack,
        1.0
    );
}
#[test]
fn sequence_modulo_edges_and_property_pairs() {
    for (previous, current, expected) in [
        (0, 0, 0),
        (0, 1, 1),
        (1, 0, 65535),
        (65534, 65535, 1),
        (65535, 0, 1),
        (65535, 1, 2),
        (0, 65535, 65535),
    ] {
        let mut r = started(Scenario::Baseline);
        add(&mut r, report(1, previous, 0, 0), 0);
        assert_eq!(
            add(&mut r, report(1, current, 0, 0), 1).sequence_delta_modulo,
            Some(expected)
        );
    }
    let mut seed = 0x1234_5678_9abc_def0u64;
    for _ in 0..5000 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        let a = seed as u16;
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        let b = seed as u16;
        assert_eq!(
            b.wrapping_sub(a) as u32,
            ((b as i64 - a as i64).rem_euclid(65536)) as u32
        );
    }
}
#[test]
fn timestamp_modulo_edges_and_property_pairs() {
    for (previous, current, expected) in [
        (0, 0, 0),
        (0, 1, 1),
        (1, 0, u64::MAX),
        (u64::MAX - 1, u64::MAX, 1),
        (u64::MAX, 0, 1),
        (u64::MAX, 1, 2),
        (0, u64::MAX, u64::MAX),
    ] {
        let mut r = started(Scenario::Baseline);
        add(&mut r, report(1, 0, previous, 0), 0);
        assert_eq!(
            add(&mut r, report(1, 0, current, 0), 1).timestamp_delta_modulo,
            Some(expected)
        );
    }
    let mut seed = 0x9876_5432_10fe_dcba_u64;
    for _ in 0..5000 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        let a = seed;
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        let b = seed;
        assert_eq!(
            b.wrapping_sub(a) as u128,
            ((b as i128 - a as i128).rem_euclid(1i128 << 64)) as u128
        );
    }
}
#[test]
fn flags_format_bits_and_seeded_roundtrip() {
    for (value, hex, bits) in [
        (0, "0x00000000", vec![]),
        (1, "0x00000001", vec![0]),
        (2, "0x00000002", vec![1]),
        (3, "0x00000003", vec![0, 1]),
        (0x80000000, "0x80000000", vec![31]),
        (0x81800000, "0x81800000", vec![23, 24, 31]),
        (u32::MAX, "0xffffffff", (0..32).collect()),
    ] {
        assert_eq!(flags_hex(value), hex);
        assert_eq!(flags_bits_set(value), bits);
    }
    for bit in 0..32 {
        assert_eq!(flags_bits_set(1u32 << bit), vec![bit as u8]);
    }
    let mut seed = 0xabcdef01u32;
    for _ in 0..5000 {
        seed ^= seed << 13;
        seed ^= seed >> 17;
        seed ^= seed << 5;
        assert_eq!(
            u32::from_str_radix(&flags_hex(seed)[2..], 16).unwrap(),
            seed
        );
        assert_eq!(
            flags_bits_set(seed)
                .into_iter()
                .fold(0u32, |v, b| v | (1u32 << b)),
            seed
        );
    }
}
#[test]
fn duplicate_requires_every_field_and_raw_byte() {
    let baseline = report(169, 17, 50, 0x81800000);
    let mut r = started(Scenario::Baseline);
    add(&mut r, baseline.clone(), 0);
    assert!(add(&mut r, baseline.clone(), 1).duplicate_parsed_report);
    let mutations: [fn(&mut ReportFacts); 7] = [
        |r| r.bpm += 1,
        |r| r.aux += 1,
        |r| r.sequence += 1,
        |r| r.field_5 += 1,
        |r| r.timestamp_ticks += 1,
        |r| r.flags += 1,
        |r| r.raw_report[7] ^= 1,
    ];
    for change in mutations {
        let mut r = started(Scenario::Baseline);
        add(&mut r, baseline.clone(), 0);
        let mut changed = baseline.clone();
        change(&mut changed);
        assert!(!add(&mut r, changed, 1).duplicate_parsed_report);
    }
    assert_eq!(r.records().len(), 2); // Identical reports remain separate ordered records.
}
#[test]
fn cycle_summaries_empty_one_multiple_and_ordered_uniques() {
    let mut r = recorder(Scenario::ActivationRestart);
    r.commit_header().unwrap();
    let empty = summary(&r);
    assert_eq!(empty.first_bpm_per_cycle, vec![None, None]);
    assert_eq!(empty.last_bpm_per_cycle, vec![None, None]);
    assert_eq!(empty.cycle_summaries[0].sequence_first, None);
    assert_eq!(empty.cycle_summaries[1].timestamp_last, None);
    r.mark_cycle_attempted(1).unwrap();
    r.begin_cycle(1, 0).unwrap();
    let mut a = report(80, 8, 90, 8);
    a.field_5 = 9;
    add(&mut r, a, 0);
    let mut b = report(70, 5, 70, 4);
    b.field_5 = 3;
    add(&mut r, b, 1);
    add(&mut r, report(80, 7, 80, 8), 2);
    r.complete_cycle(1).unwrap();
    r.mark_cycle_attempted(2).unwrap();
    r.begin_cycle(2, 3).unwrap();
    let c = add(&mut r, report(90, 0, 0, 1), 4);
    assert_eq!(
        (
            c.cycle_index,
            c.sample_index_within_cycle,
            c.sample_index_global
        ),
        (2, 1, 4)
    );
    let s = summary(&r);
    assert_eq!(s.first_bpm_per_cycle, vec![Some(80), Some(90)]);
    assert_eq!(s.last_bpm_per_cycle, vec![Some(80), Some(90)]);
    assert_eq!(s.unique_bpm_values, vec![70, 80, 90]);
    assert_eq!(s.cycle_summaries[0].sequence_first, Some(8));
    assert_eq!(s.cycle_summaries[0].sequence_last, Some(7));
    assert_eq!(s.cycle_summaries[0].timestamp_first, Some(90));
    assert_eq!(s.cycle_summaries[0].timestamp_last, Some(80));
    assert_eq!(s.cycle_summaries[0].field_5_unique_values, vec![3, 9]);
    assert_eq!(
        s.cycle_summaries[0].flags_unique_hex_values,
        vec!["0x00000004", "0x00000008"]
    );
    assert_eq!(s.cycle_summaries[1].reports_received, 1);
    assert!(!s.cycle_summaries[1].complete);
    assert_eq!(s.flags_bit_positions_observed, vec![0, 2, 3]);
}
#[test]
fn reset_classification_exact_vocabulary_and_missing_cases() {
    for (previous, current, expected) in [
        (7, 0, "yes"),
        (0, 0, "no"),
        (7, 8, "no"),
        (7, 7, "no"),
        (7, 3, "unknown"),
    ] {
        assert_eq!(reset_observation(Some(previous), Some(current)), expected);
        assert_eq!(
            reset_observation(Some(previous as u64), Some(current as u64)),
            expected
        );
    }
    assert_eq!(reset_observation::<u16>(None, Some(0)), "unknown");
    assert_eq!(reset_observation::<u64>(Some(0), None), "unknown");
    let mut b = started(Scenario::Baseline);
    add(&mut b, report(1, 7, 7, 0), 0);
    assert_eq!(
        summary(&b).sequence_reset_observed_between_cycles,
        "unknown"
    );
    assert_eq!(
        summary(&b).timestamp_reset_observed_between_cycles,
        "unknown"
    );
    let mut r = recorder(Scenario::ActivationRestart);
    r.commit_header().unwrap();
    assert_eq!(
        summary(&r).sequence_reset_observed_between_cycles,
        "unknown"
    );
    r.mark_cycle_attempted(1).unwrap();
    r.begin_cycle(1, 0).unwrap();
    add(&mut r, report(1, 7, 7, 0), 0);
    r.complete_cycle(1).unwrap();
    assert_eq!(
        summary(&r).timestamp_reset_observed_between_cycles,
        "unknown"
    );
    r.mark_cycle_attempted(2).unwrap();
    r.begin_cycle(2, 1).unwrap();
    add(&mut r, report(1, 0, 0, 0), 1);
    assert_eq!(summary(&r).sequence_reset_observed_between_cycles, "yes");
    assert_eq!(summary(&r).timestamp_reset_observed_between_cycles, "yes");
}
#[test]
fn interval_median_matches_python_cases() {
    for (values, count, min, median, max) in [
        (vec![], 0, None, None, None),
        (vec![2.5], 1, Some(2.5), Some(2.5), Some(2.5)),
        (vec![1.0, 3.0], 2, Some(1.0), Some(2.0), Some(3.0)),
        (vec![3.0, 1.0, 2.0], 3, Some(1.0), Some(2.0), Some(3.0)),
        (vec![4.0, 1.0, 3.0, 2.0], 4, Some(1.0), Some(2.5), Some(4.0)),
        (
            vec![1.25, 1.25, 1.25],
            3,
            Some(1.25),
            Some(1.25),
            Some(1.25),
        ),
        (
            vec![-3.5, -1.5, 0.5, 5.0],
            4,
            Some(-3.5),
            Some(-0.5),
            Some(5.0),
        ),
    ] {
        assert_eq!(
            IntervalSummary::from_intervals(&values),
            IntervalSummary {
                count,
                min,
                median,
                max
            }
        );
    }
}
#[test]
fn full_summary_golden_baseline_and_restart() {
    let mut b = recorder(Scenario::Baseline);
    b.commit_header().unwrap();
    let empty = summary(&b);
    assert_eq!(empty.scenario, Scenario::Baseline);
    assert_eq!(empty.status, "complete");
    assert_eq!(empty.failure_category, None);
    assert_eq!(empty.canonical_reports_received, 0);
    assert_eq!(empty.cycles_completed, 0);
    assert_eq!(empty.first_bpm_per_cycle, vec![None]);
    assert_eq!(empty.last_bpm_per_cycle, vec![None]);
    assert_eq!(empty.unique_bpm_values, Vec::<u8>::new());
    assert_eq!(
        empty.cycle_summaries[0],
        CycleSummary {
            cycle_index: 1,
            attempted: false,
            activation_ack_observed: false,
            complete: false,
            reports_received: 0,
            sequence_first: None,
            sequence_last: None,
            timestamp_first: None,
            timestamp_last: None,
            field_5_unique_values: vec![],
            flags_unique_hex_values: vec![]
        }
    );
    assert_eq!(empty.flags_bit_positions_observed, vec![]);
    assert_eq!(
        empty.receive_interval_ms,
        IntervalSummary {
            count: 0,
            min: None,
            median: None,
            max: None
        }
    );
    b.mark_cycle_attempted(1).unwrap();
    b.begin_cycle(1, 0).unwrap();
    add(&mut b, report(169, 65535, u64::MAX, 3), 0);
    b.complete_cycle(1).unwrap();
    let one = summary(&b);
    assert_eq!(one.canonical_reports_received, 1);
    assert_eq!(one.cycles_completed, 1);
    assert_eq!(one.first_bpm_per_cycle, vec![Some(169)]);
    assert_eq!(one.last_bpm_per_cycle, vec![Some(169)]);
    assert_eq!(one.unique_bpm_values, vec![169]);
    assert_eq!(one.flags_bit_positions_observed, vec![0, 1]);
    let mut r = started(Scenario::ActivationRestart);
    add(&mut r, report(80, u16::MAX, u64::MAX, 0x80000000), 0);
    add(
        &mut r,
        report(80, u16::MAX, u64::MAX, 0x80000000),
        1_000_000,
    );
    r.complete_cycle(1).unwrap();
    r.mark_cycle_attempted(2).unwrap();
    r.begin_cycle(2, 2_000_000).unwrap();
    add(&mut r, report(90, 0, 1, 3), 3_000_000);
    let incomplete = r
        .prepare_summary("failed".into(), Some("cycle_timeout".into()))
        .unwrap();
    assert_eq!(incomplete.canonical_reports_received, 3);
    assert_eq!(incomplete.cycles_completed, 1);
    assert_eq!(incomplete.first_bpm_per_cycle, vec![Some(80), Some(90)]);
    assert_eq!(incomplete.last_bpm_per_cycle, vec![Some(80), Some(90)]);
    assert_eq!(incomplete.unique_bpm_values, vec![80, 90]);
    assert_eq!(incomplete.sequence_reset_observed_between_cycles, "yes");
    assert_eq!(
        incomplete.timestamp_reset_observed_between_cycles,
        "unknown"
    );
    assert_eq!(incomplete.flags_bit_positions_observed, vec![0, 1, 31]);
    assert_eq!(
        incomplete.receive_interval_ms,
        IntervalSummary {
            count: 2,
            min: Some(1.0),
            median: Some(1.5),
            max: Some(2.0)
        }
    );
    assert_eq!(incomplete.failure_category, Some("cycle_timeout".into()));
    r.complete_cycle(2).unwrap();
    assert_eq!(summary(&r).cycles_completed, 2);
}
#[test]
fn deterministic_streams_preserve_indices_counts_and_sorting() {
    let mut seed = 0x1234_5678u32;
    for length in [1, 2, 3, 10, 100, 1000] {
        let mut r = started(Scenario::ActivationRestart);
        for i in 0..length {
            if i == length / 2 && i != 0 {
                r.complete_cycle(1).unwrap();
                r.mark_cycle_attempted(2).unwrap();
                r.begin_cycle(2, 0).unwrap();
            }
            seed ^= seed << 13;
            seed ^= seed >> 17;
            seed ^= seed << 5;
            let sample = add(
                &mut r,
                report(seed as u8, seed as u16, seed as u64, seed),
                i as i128,
            );
            assert_eq!(sample.sample_index_global, i + 1);
            assert_eq!(
                sample.sample_index_within_cycle,
                r.cycles[sample.cycle_index - 1].sample_count
            );
        }
        let s = summary(&r);
        assert_eq!(s.canonical_reports_received, length);
        assert_eq!(
            s.cycle_summaries
                .iter()
                .map(|c| c.reports_received)
                .sum::<usize>(),
            length
        );
        assert!(s.unique_bpm_values.windows(2).all(|w| w[0] < w[1]));
        assert!(
            s.flags_bit_positions_observed
                .windows(2)
                .all(|w| w[0] < w[1])
        );
        for c in s.cycle_summaries {
            assert!(c.field_5_unique_values.windows(2).all(|w| w[0] < w[1]));
            assert!(c.flags_unique_hex_values.windows(2).all(|w| w[0] < w[1]));
        }
    }
}

#[test]
fn canonical_raw_length_fails_closed() {
    for n in [0, 1, 17, 19, 36] {
        assert_eq!(
            ReportFacts::from_fields(169, 1, 2, 3, 4, 5, &vec![0; n]),
            Err(SemanticsError::InvalidRawReport)
        );
    }
    let raw: Vec<u8> = (0..18).collect();
    let facts = ReportFacts::from_fields(169, 1, 2, 3, 4, 5, &raw).unwrap();
    assert_eq!(facts.raw_report.as_slice(), raw.as_slice());
    let mut recorder = started(Scenario::Baseline);
    assert_eq!(
        add(&mut recorder, facts, 0).raw_report_hex,
        "000102030405060708090a0b0c0d0e0f1011"
    );
}
