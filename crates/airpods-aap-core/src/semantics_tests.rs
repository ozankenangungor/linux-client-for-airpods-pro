use super :: * ;







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








