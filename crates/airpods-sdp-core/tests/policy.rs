use airpods_sdp_core :: * ;
use std :: collections :: HashSet ;

fn identity(vendor_id: u16, product_id: u16, version: u16) -> UsbIdentity {
    UsbIdentity {
        vendor_id,
        product_id,
        version,
    }
}

#[test]
fn modalias_exact_ascii_grammar_and_error_kinds() {
    assert_eq!(parse_bluez_modalias(None), Err(ModaliasError::Missing));
    for (input, expected) in [
        ("usb:v1234p5678d9AbC", identity(0x1234, 0x5678, 0x9abc)),
        ("usb:v0000p0000d0000", identity(0, 0, 0)),
        ("usb:vFFFFpFFFFdFFFF", identity(0xffff, 0xffff, 0xffff)),
        ("usb:vAaFfp00aAdFf00", identity(0xaaff, 0x00aa, 0xff00)),
    ] {
        assert_eq!(parse_bluez_modalias(Some(input)), Ok(expected));
    }
    for bad in [
        "",
        "usb:",
        "usb:v1234",
        "usb:v1234p5678",
        "usb:v1234p5678d",
        "usb:v123p5678d9abc",
        "usb:v12345p5678d9abc",
        "usb:v1234p567d9abc",
        "usb:v1234p56789d9abc",
        "usb:v1234p5678d9ab",
        "usb:v1234p5678d9abcd",
        "USB:v1234p5678d9abc",
        "usb:V1234p5678d9abc",
        "usb:v1234P5678d9abc",
        "usb:v1234p5678D9abc",
        "prefixusb:v1234p5678d9abc",
        "usb:v1234p5678d9abc/extra",
        " usb:v1234p5678d9abc",
        "usb:v1234p5678d9abc\n",
        "usb:v1234p5678d9abg",
        "usb:v1234p5678d9abé",
        "usb:v１２３４p5678d9abc",
    ] {
        assert_eq!(
            parse_bluez_modalias(Some(bad)),
            Err(ModaliasError::Unsupported),
            "{bad:?}"
        );
    }
    for digit in b"0123456789abcdefABCDEF" {
        let input = format!("usb:v{}000p0000d0000", *digit as char);
        assert!(parse_bluez_modalias(Some(&input)).is_ok(), "{input}");
    }
}

#[test]
fn modalias_deterministic_random_never_panics() {
    let mut seed = 0x8a62_1711_u64;
    for _ in 0..10_000 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        let len = (seed % 32) as usize;
        let value: String = (0..len)
            .map(|offset| char::from((seed.rotate_left(offset as u32) % 128) as u8))
            .collect();
        let result = parse_bluez_modalias(Some(&value));
        if let Ok(parsed) = result {
            assert_eq!(value.len(), 19);
            assert_eq!(parse_bluez_modalias(Some(&value)), Ok(parsed));
        }
    }
}

#[test]
fn canonical_records_are_ordered_typed_and_identity_specific() {
    for id in [
        identity(0, 0, 0),
        identity(0x1234, 0x5678, 0x9abc),
        identity(0xffff, 0xffff, 0xffff),
    ] {
        let records = canonical_records(id);
        assert_eq!(
            records.map(|record| (record.name, record.handle, record.service_uuid)),
            [
                ("PnPInformation", 0x10001, 0x1200),
                ("HandsfreeAudioGateway", 0x10002, 0x111f),
                ("AudioSource", 0x10003, 0x110a),
                ("A/V RemoteControlTarget", 0x10004, 0x110c),
            ]
        );
        let records = canonical_records(id);
        assert_eq!(
            records[0].attributes,
            vec![
                (0, Element::U32(0x10001)),
                (1, Element::Sequence(vec![Element::Uuid16(0x1200)])),
                (0x201, Element::U16(id.vendor_id)),
                (0x202, Element::U16(id.product_id)),
                (0x203, Element::U16(id.version)),
                (0x205, Element::U16(2)),
            ]
        );
        assert_eq!(
            records[1].attributes[2],
            (
                4,
                Element::Sequence(vec![
                    Element::Sequence(vec![Element::Uuid16(0x0100)]),
                    Element::Sequence(vec![Element::Uuid16(0x0003), Element::U8(13)]),
                ])
            )
        );
        assert_eq!(
            records[2].attributes[2],
            (
                4,
                Element::Sequence(vec![
                    Element::Sequence(vec![Element::Uuid16(0x0100), Element::U16(0x0019)]),
                    Element::Sequence(vec![Element::Uuid16(0x0019), Element::U16(0x0103)]),
                ])
            )
        );
        assert_eq!(
            records[3].attributes[2],
            (
                9,
                Element::Sequence(vec![Element::Sequence(vec![
                    Element::Uuid16(0x110e),
                    Element::U16(0x0106)
                ]),])
            )
        );
    }
}

#[test]
fn bluez_xml_matches_exact_parent_golden_records() {
    let expected_fixed = [
        "<record>\n  <attribute id=\"0x0001\"><sequence><uuid value=\"0x111f\"/></sequence></attribute>\n  <attribute id=\"0x0004\"><sequence><sequence><uuid value=\"0x0100\"/></sequence><sequence><uuid value=\"0x0003\"/><uint8 value=\"0x0d\"/></sequence></sequence></attribute>\n</record>",
        "<record>\n  <attribute id=\"0x0001\"><sequence><uuid value=\"0x110a\"/></sequence></attribute>\n  <attribute id=\"0x0004\"><sequence><sequence><uuid value=\"0x0100\"/><uint16 value=\"0x0019\"/></sequence><sequence><uuid value=\"0x0019\"/><uint16 value=\"0x0103\"/></sequence></sequence></attribute>\n</record>",
        "<record>\n  <attribute id=\"0x0001\"><sequence><uuid value=\"0x110c\"/></sequence></attribute>\n  <attribute id=\"0x0009\"><sequence><sequence><uuid value=\"0x110e\"/><uint16 value=\"0x0106\"/></sequence></sequence></attribute>\n</record>",
    ];
    for (id, fields) in [
        (identity(0, 0, 0), ["0000", "0000", "0000"]),
        (identity(0x1234, 0x5678, 0x9abc), ["1234", "5678", "9abc"]),
        (identity(0xffff, 0xffff, 0xffff), ["ffff", "ffff", "ffff"]),
    ] {
        let records = bluez_xml_records(id);
        assert_eq!(
            records.iter().map(|record| record.name).collect::<Vec<_>>(),
            [
                "PnPInformation",
                "HandsfreeAudioGateway",
                "AudioSource",
                "A/V RemoteControlTarget"
            ]
        );
        assert_eq!(
            records
                .iter()
                .map(|record| record.uuid.as_str())
                .collect::<Vec<_>>(),
            [
                "00001200-0000-1000-8000-00805f9b34fb",
                "0000111f-0000-1000-8000-00805f9b34fb",
                "0000110a-0000-1000-8000-00805f9b34fb",
                "0000110c-0000-1000-8000-00805f9b34fb",
            ]
        );
        let pnp = format!(
            "<record>\n  <attribute id=\"0x0001\"><sequence><uuid value=\"0x1200\"/></sequence></attribute>\n  <attribute id=\"0x0201\"><uint16 value=\"0x{}\"/></attribute>\n  <attribute id=\"0x0202\"><uint16 value=\"0x{}\"/></attribute>\n  <attribute id=\"0x0203\"><uint16 value=\"0x{}\"/></attribute>\n  <attribute id=\"0x0205\"><uint16 value=\"0x0002\"/></attribute>\n</record>",
            fields[0], fields[1], fields[2]
        );
        assert_eq!(records[0].xml, pnp);
        for (record, expected) in records[1..].iter().zip(expected_fixed) {
            assert_eq!(record.xml, expected);
        }
    }
}

#[test]
fn audit_attribute_matrix_and_overall_fail_closed() {
    for expected in [None, Some(7)] {
        for observed in [None, Some(None), Some(Some(7)), Some(Some(8))] {
            let status = compare_attribute(expected, observed);
            let expected_status = match (expected, observed) {
                (Some(7), Some(Some(7))) => ComparisonStatus::Match,
                (Some(_), Some(_)) => ComparisonStatus::Mismatch,
                _ => ComparisonStatus::NotObservable,
            };
            assert_eq!(status, expected_status);
        }
    }
    for attributes in expected_attributes(None) {
        for (name, expected) in attributes {
            if ["pnp_vendor_id", "pnp_product_id", "pnp_version"].contains(&name) {
                assert_eq!(expected, None);
            } else {
                assert!(expected.is_some());
            }
        }
    }
    assert_eq!(
        expected_attributes(Some(identity(1, 2, 3)))[0],
        vec![
            ("pnp_vendor_id", Some(1)),
            ("pnp_product_id", Some(2)),
            ("pnp_version", Some(3)),
            ("pnp_vendor_id_source", Some(2)),
        ]
    );
    let all_match = vec![ComparisonStatus::Match; 8];
    assert_eq!(
        full_record_equivalence(&all_match, &[true; 4]),
        ComparisonStatus::Match
    );
    for missing in 0..4 {
        let mut uuids = [true; 4];
        uuids[missing] = false;
        assert_eq!(
            full_record_equivalence(&all_match, &uuids),
            ComparisonStatus::Mismatch
        );
    }
    for mismatch in 0..8 {
        let mut statuses = all_match.clone();
        statuses[mismatch] = ComparisonStatus::Mismatch;
        assert_eq!(
            full_record_equivalence(&statuses, &[true; 4]),
            ComparisonStatus::Mismatch
        );
    }
    assert_eq!(
        full_record_equivalence(
            &[ComparisonStatus::Match, ComparisonStatus::NotObservable],
            &[true; 4]
        ),
        ComparisonStatus::NotObservable
    );
    assert_eq!(
        full_record_equivalence(&[], &[]),
        ComparisonStatus::NotObservable
    );
    assert_eq!(
        full_record_equivalence(&[ComparisonStatus::NotObservable; 8], &[true; 4]),
        ComparisonStatus::NotObservable
    );
}

#[test]
fn all_reference_extra_specs_match_parent_order_and_values() {
    use ServiceUuid::{Full, Short};
    let expected = [
        ("Generic Access", Short(0x1800), "att", None, None, None),
        ("Generic Attribute", Short(0x1801), "att", None, None, None),
        ("Device Information", Short(0x180a), "att", None, None, None),
        (
            "Audio Input Control",
            Short(0x1843),
            "att",
            None,
            None,
            None,
        ),
        ("Volume Control", Short(0x1844), "att", None, None, None),
        (
            "Volume Offset Control",
            Short(0x1845),
            "att",
            None,
            None,
            None,
        ),
        (
            "Generic Media Control",
            Short(0x1849),
            "att",
            None,
            None,
            None,
        ),
        ("Microphone Control", Short(0x184d), "att", None, None, None),
        (
            "Broadcast Audio Scan",
            Short(0x184f),
            "att",
            None,
            None,
            None,
        ),
        ("Ranging Service", Short(0x185b), "att", None, None, None),
        (
            "A/V RemoteControlController",
            Short(0x110f),
            "avrcp-controller",
            None,
            None,
            None,
        ),
        ("Audio Sink", Short(0x110b), "audio-sink", None, None, None),
        ("Handsfree", Short(0x111e), "handsfree", None, None, None),
        (
            "Message Notification Server",
            Short(0x1133),
            "obex",
            Some(17),
            Some(Short(0x1134)),
            Some(0x0104),
        ),
        (
            "Message Access Server",
            Short(0x1132),
            "obex",
            Some(16),
            Some(Short(0x1134)),
            Some(0x0100),
        ),
        (
            "Phone Book Access Server",
            Short(0x112f),
            "obex",
            Some(15),
            Some(Short(0x1130)),
            Some(0x0101),
        ),
        (
            "Synchronization",
            Short(0x1104),
            "obex",
            Some(14),
            Some(Short(0x1104)),
            Some(0x0100),
        ),
        (
            "OBEX File Transfer",
            Short(0x1106),
            "obex",
            Some(10),
            Some(Short(0x1106)),
            Some(0x0103),
        ),
        (
            "OBEX Object Push",
            Short(0x1105),
            "obex",
            Some(9),
            Some(Short(0x1105)),
            Some(0x0102),
        ),
        (
            "Nokia OBEX PC Suite Services",
            Full("00005005-0000-1000-8000-0002ee000001"),
            "obex",
            Some(24),
            Some(Full("00005005-0000-1000-8000-0002ee000001")),
            Some(0x0100),
        ),
    ];
    let actual = BLUEZ_LIKE_EXTRA_SERVICE_SPECS.map(|spec| {
        (
            spec.name,
            spec.uuid,
            spec.protocol,
            spec.rfcomm_channel,
            spec.profile_uuid,
            spec.profile_version,
        )
    });
    assert_eq!(actual, expected);
    assert_eq!(
        (ATT_L2CAP_PSM, AVCTP_L2CAP_PSM, AVDTP_L2CAP_PSM),
        (31, 23, 25)
    );
    assert_eq!(
        (
            AVCTP_VERSION,
            AVRCP_VERSION,
            AVDTP_VERSION,
            ADVANCED_AUDIO_VERSION,
            HANDS_FREE_VERSION
        ),
        (0x104, 0x106, 0x103, 0x104, 0x109)
    );
    assert_eq!(HANDS_FREE_UNIT_RFCOMM_CHANNEL, 7);
}

#[test]
fn handle_plan_preserves_max_plus_one_and_never_collides() {
    for input in [
        vec![],
        vec!["1"],
        vec!["65537", "65538", "65539", "65540"],
        vec!["1", "4", "99"],
        vec!["0", "1", "2", "3", "4", "5", "6"],
        vec!["4294967295"],
        vec!["99999999999999999999999999999999999999"],
    ] {
        let existing: Vec<String> = input.into_iter().map(str::to_owned).collect();
        let output = allocate_handles(&existing, 20).unwrap();
        assert_eq!(output.len(), 20);
        assert_eq!(output.iter().collect::<HashSet<_>>().len(), 20);
        assert!(output.iter().all(|handle| !existing.contains(handle)));
        assert!(
            output
                .windows(2)
                .all(|pair| pair[1].len() > pair[0].len() || pair[1] > pair[0])
        );
    }
    assert_eq!(allocate_handles(&[], 3).unwrap(), ["1", "2", "3"]);
    assert_eq!(
        allocate_handles(&["65537".into(), "65540".into()], 2).unwrap(),
        ["65541", "65542"]
    );
    assert_eq!(
        allocate_handles(&["99".into()], 0).unwrap(),
        Vec::<String>::new()
    );
    let many_occupied: Vec<_> = (0..10_000).map(|value| value.to_string()).collect();
    assert_eq!(
        allocate_handles(&many_occupied, 2).unwrap(),
        ["10000", "10001"]
    );
}

#[test]
fn random_handle_sets_keep_plan_unique_and_above_every_input() {
    let mut seed = 0x5a17_c0de_u64;
    for _ in 0..1000 {
        seed ^= seed << 13;
        seed ^= seed >> 7;
        seed ^= seed << 17;
        let existing: Vec<String> = (0..(seed % 30))
            .map(|offset| ((seed.rotate_left(offset as u32) % 10_000) as u32).to_string())
            .collect();
        let plan = allocate_handles(&existing, 20).unwrap();
        let maximum = existing
            .iter()
            .map(|value| value.parse::<u32>().unwrap())
            .max()
            .unwrap_or(0);
        assert_eq!(plan[0].parse::<u32>().unwrap(), maximum + 1);
        assert!(plan.iter().all(|value| !existing.contains(value)));
    }
}

#[test]
fn query_policy_boundary_matrix_and_retention() {
    assert_eq!(
        query_uuid16s(&[vec![0, 1], vec![0, 0x12], vec![], vec![1, 2, 3]]),
        [0x0100, 0x1200]
    );
    assert_eq!(
        query_attribute_ranges(&[(4, 2), (0x0000ffff, 4), (0x12345678, 4), (7, 1)]),
        [(4, 4), (0, 0xffff), (0x1234, 0x5678)]
    );
    for has_uuid in [false, true] {
        for has_range in [false, true] {
            let uuids = if has_uuid { vec![0x0100] } else { vec![0x110a] };
            let ranges = if has_range {
                vec![(0, 0xffff)]
            } else {
                vec![(4, 4)]
            };
            let decision = query_decision(&uuids, &ranges, 100, None, 100, 0, 0, false);
            assert_eq!(decision.target, has_uuid && has_range);
            assert!(!decision.continuation_used);
            assert_eq!(decision.retain_summary, !(has_uuid && has_range));
        }
    }
    for (maximum, mtu, bytes, state_len, expected) in [
        (100, None, 99, 0, false),
        (100, None, 100, 0, false),
        (100, None, 101, 0, true),
        (100, Some(109), 100, 0, false),
        (100, Some(109), 101, 0, true),
        (100, Some(50), 40, 0, false),
        (100, Some(50), 42, 0, true),
        (100, Some(8), 0, 0, false),
        (100, Some(8), 1, 0, true),
        (100, Some(200), 101, 0, true),
        (100, None, 0, 1, false),
        (100, None, 0, 2, true),
    ] {
        assert_eq!(
            query_decision(&[], &[], maximum, mtu, bytes, state_len, 0, false).continuation_used,
            expected
        );
    }
    for stored in 0..11 {
        let decision = query_decision(&[], &[], 100, None, 0, 0, stored, false);
        assert_eq!(decision.retain_summary, stored < 8);
    }
    assert!(query_decision(&[0x0100], &[(0, 0xffff)], 100, None, 0, 2, 8, true).mark_prior_target);
    assert!(!query_decision(&[0x0100], &[(0, 0xffff)], 100, None, 0, 1, 8, true).mark_prior_target);
    assert!(
        !query_decision(&[0x0100], &[(0, 0xffff)], 100, None, 0, 2, 8, false).mark_prior_target
    );
    assert_eq!(
        first_l2cap_summary_index(&[vec![0x110a], vec![0x0100], vec![0x0100]]),
        Some(1)
    );
    assert_eq!(first_l2cap_summary_index(&[vec![0x110a]]), None);
}








