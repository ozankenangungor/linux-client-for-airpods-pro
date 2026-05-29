use airpods_sdp_core :: * ;


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


















