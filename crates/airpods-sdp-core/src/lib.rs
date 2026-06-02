#![forbid(unsafe_code)]
//! Deterministic SDP identity, audit, reference footprint, and diagnostic policy.
//! Runtime Bluetooth objects and effects are deliberately absent.

pub const PNP_INFORMATION_HANDLE: u32 = 0x0001_0001;
pub const HANDS_FREE_AUDIO_GATEWAY_HANDLE: u32 = 0x0001_0002;
pub const AUDIO_SOURCE_HANDLE: u32 = 0x0001_0003;
pub const AVRCP_TARGET_HANDLE: u32 = 0x0001_0004;
pub const HANDS_FREE_RFCOMM_CHANNEL: u8 = 13;
pub const AVDTP_L2CAP_PSM: u16 = 0x0019;
pub const AVDTP_VERSION: u16 = 0x0103;
pub const AVRCP_VERSION: u16 = 0x0106;
pub const PNP_VENDOR_ID_SOURCE_USB: u16 = 2;
pub const PNP_VENDOR_ID_ATTRIBUTE_ID: u16 = 0x0201;
pub const PNP_PRODUCT_ID_ATTRIBUTE_ID: u16 = 0x0202;
pub const PNP_VERSION_ATTRIBUTE_ID: u16 = 0x0203;
pub const PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID: u16 = 0x0205;
pub const ATT_L2CAP_PSM: u16 = 0x001f;
pub const AVCTP_L2CAP_PSM: u16 = 0x0017;
pub const AVCTP_VERSION: u16 = 0x0104;
pub const ADVANCED_AUDIO_VERSION: u16 = 0x0104;
pub const HANDS_FREE_VERSION: u16 = 0x0109;
pub const HANDS_FREE_UNIT_RFCOMM_CHANNEL: u8 = 7;
pub const REFERENCE_SDP_QUERY_SUMMARY_LIMIT: usize = 8;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UsbIdentity {
    pub vendor_id: u16,
    pub product_id: u16,
    pub version: u16,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ModaliasError {
    Missing,
    Unsupported,
}

pub fn parse_bluez_modalias(modalias: Option<&str>) -> Result<UsbIdentity, ModaliasError> {
    let value = modalias.ok_or(ModaliasError::Missing)?.as_bytes();
    if value.len() != 19 || &value[..5] != b"usb:v" || value[9] != b'p' || value[14] != b'd' {
        return Err(ModaliasError::Unsupported);
    }
    fn hex(bytes: &[u8]) -> Option<u16> {
        bytes.iter().try_fold(0u16, |value, byte| {
            let digit = match byte {
                b'0'..=b'9' => byte - b'0',
                b'a'..=b'f' => byte - b'a' + 10,
                b'A'..=b'F' => byte - b'A' + 10,
                _ => return None,
            };
            Some((value << 4) | u16::from(digit))
        })
    }
    Ok(UsbIdentity {
        vendor_id: hex(&value[5..9]).ok_or(ModaliasError::Unsupported)?,
        product_id: hex(&value[10..14]).ok_or(ModaliasError::Unsupported)?,
        version: hex(&value[15..19]).ok_or(ModaliasError::Unsupported)?,
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Element {
    U8(u8),
    U16(u16),
    U32(u32),
    Uuid16(u16),
    Sequence(Vec<Element>),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecordSpec {
    pub name: &'static str,
    pub handle: u32,
    pub service_uuid: u16,
    pub attributes: Vec<(u16, Element)>,
}

fn seq(values: impl IntoIterator<Item = Element>) -> Element {
    Element::Sequence(values.into_iter().collect())
}
fn descriptor(uuid: u16, parameter: Option<Element>) -> Element {
    let mut elements = vec![Element::Uuid16(uuid)];
    if let Some(parameter) = parameter {
        elements.push(parameter);
    }
    seq(elements)
}

pub fn canonical_records(identity: UsbIdentity) -> [RecordSpec; 4] {
    let record = |name, handle, service_uuid, extra: Vec<(u16, Element)>| {
        let mut attributes = vec![
            (0x0000, Element::U32(handle)),
            (0x0001, seq([Element::Uuid16(service_uuid)])),
        ];
        attributes.extend(extra);
        RecordSpec {
            name,
            handle,
            service_uuid,
            attributes,
        }
    };
    [
        record(
            "PnPInformation",
            PNP_INFORMATION_HANDLE,
            0x1200,
            vec![
                (PNP_VENDOR_ID_ATTRIBUTE_ID, Element::U16(identity.vendor_id)),
                (
                    PNP_PRODUCT_ID_ATTRIBUTE_ID,
                    Element::U16(identity.product_id),
                ),
                (PNP_VERSION_ATTRIBUTE_ID, Element::U16(identity.version)),
                (
                    PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID,
                    Element::U16(PNP_VENDOR_ID_SOURCE_USB),
                ),
            ],
        ),
        record(
            "HandsfreeAudioGateway",
            HANDS_FREE_AUDIO_GATEWAY_HANDLE,
            0x111f,
            vec![(
                0x0004,
                seq([
                    descriptor(0x0100, None),
                    descriptor(0x0003, Some(Element::U8(HANDS_FREE_RFCOMM_CHANNEL))),
                ]),
            )],
        ),
        record(
            "AudioSource",
            AUDIO_SOURCE_HANDLE,
            0x110a,
            vec![(
                0x0004,
                seq([
                    descriptor(0x0100, Some(Element::U16(AVDTP_L2CAP_PSM))),
                    descriptor(0x0019, Some(Element::U16(AVDTP_VERSION))),
                ]),
            )],
        ),
        record(
            "A/V RemoteControlTarget",
            AVRCP_TARGET_HANDLE,
            0x110c,
            vec![(
                0x0009,
                seq([descriptor(0x110e, Some(Element::U16(AVRCP_VERSION)))]),
            )],
        ),
    ]
}

pub fn uuid16_full(value: u16) -> String {
    format!("0000{value:04x}-0000-1000-8000-00805f9b34fb")
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct XmlRecord {
    pub name: &'static str,
    pub uuid: String,
    pub xml: String,
}

pub fn bluez_xml_records(identity: UsbIdentity) -> [XmlRecord; 4] {
    let mut result = Vec::new();
    for record in canonical_records(identity) {
        let mut lines = vec![format!(
            "<attribute id=\"0x0001\"><sequence><uuid value=\"0x{:04x}\"/></sequence></attribute>",
            record.service_uuid
        )];
        match record.service_uuid {
            0x1200 => for (id, value) in [
                (PNP_VENDOR_ID_ATTRIBUTE_ID, identity.vendor_id),
                (PNP_PRODUCT_ID_ATTRIBUTE_ID, identity.product_id),
                (PNP_VERSION_ATTRIBUTE_ID, identity.version),
                (PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID, PNP_VENDOR_ID_SOURCE_USB),
            ] {
                lines.push(format!("<attribute id=\"0x{id:04x}\"><uint16 value=\"0x{value:04x}\"/></attribute>"));
            },
            0x111f => lines.push(format!("<attribute id=\"0x0004\"><sequence><sequence><uuid value=\"0x0100\"/></sequence><sequence><uuid value=\"0x0003\"/><uint8 value=\"0x{:02x}\"/></sequence></sequence></attribute>", HANDS_FREE_RFCOMM_CHANNEL)),
            0x110a => lines.push(format!("<attribute id=\"0x0004\"><sequence><sequence><uuid value=\"0x0100\"/><uint16 value=\"0x{:04x}\"/></sequence><sequence><uuid value=\"0x0019\"/><uint16 value=\"0x{:04x}\"/></sequence></sequence></attribute>", AVDTP_L2CAP_PSM, AVDTP_VERSION)),
            0x110c => lines.push(format!("<attribute id=\"0x0009\"><sequence><sequence><uuid value=\"0x110e\"/><uint16 value=\"0x{:04x}\"/></sequence></sequence></attribute>", AVRCP_VERSION)),
            _ => unreachable!("closed canonical specification"),
        }
        result.push(XmlRecord {
            name: record.name,
            uuid: uuid16_full(record.service_uuid),
            xml: format!("<record>\n  {}\n</record>", lines.join("\n  ")),
        });
    }
    result.try_into().expect("four canonical records")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ComparisonStatus {
    Match,
    Mismatch,
    NotObservable,
}
impl ComparisonStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Match => "match",
            Self::Mismatch => "mismatch",
            Self::NotObservable => "unknown/not-observable",
        }
    }
}

pub fn expected_attributes(identity: Option<UsbIdentity>) -> [Vec<(&'static str, Option<u16>)>; 4] {
    [
        vec![
            ("pnp_vendor_id", identity.map(|value| value.vendor_id)),
            ("pnp_product_id", identity.map(|value| value.product_id)),
            ("pnp_version", identity.map(|value| value.version)),
            ("pnp_vendor_id_source", Some(PNP_VENDOR_ID_SOURCE_USB)),
        ],
        vec![("rfcomm_channel", Some(u16::from(HANDS_FREE_RFCOMM_CHANNEL)))],
        vec![
            ("l2cap_psm", Some(AVDTP_L2CAP_PSM)),
            ("avdtp_version", Some(AVDTP_VERSION)),
        ],
        vec![("avrcp_profile_version", Some(AVRCP_VERSION))],
    ]
}

/// `observed` is present only if the inspector marked the field observable.
pub fn compare_attribute(expected: Option<u16>, observed: Option<Option<i64>>) -> ComparisonStatus {
    match (expected, observed) {
        (Some(expected), Some(Some(observed))) if i64::from(expected) == observed => {
            ComparisonStatus::Match
        }
        (Some(_), Some(_)) => ComparisonStatus::Mismatch,
        _ => ComparisonStatus::NotObservable,
    }
}

pub fn full_record_equivalence(
    statuses: &[ComparisonStatus],
    uuids_present: &[bool],
) -> ComparisonStatus {
    if statuses.contains(&ComparisonStatus::Mismatch) || uuids_present.contains(&false) {
        ComparisonStatus::Mismatch
    } else if !statuses.is_empty()
        && statuses
            .iter()
            .all(|status| *status == ComparisonStatus::Match)
    {
        ComparisonStatus::Match
    } else {
        ComparisonStatus::NotObservable
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServiceUuid {
    Short(u16),
    Full(&'static str),
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExtraServiceSpec {
    pub name: &'static str,
    pub uuid: ServiceUuid,
    pub protocol: &'static str,
    pub rfcomm_channel: Option<u8>,
    pub profile_uuid: Option<ServiceUuid>,
    pub profile_version: Option<u16>,
}
const NOKIA_UUID: ServiceUuid = ServiceUuid::Full("00005005-0000-1000-8000-0002ee000001");
const fn extra(
    name: &'static str,
    uuid: ServiceUuid,
    protocol: &'static str,
    rfcomm_channel: Option<u8>,
    profile_uuid: Option<ServiceUuid>,
    profile_version: Option<u16>,
) -> ExtraServiceSpec {
    ExtraServiceSpec {
        name,
        uuid,
        protocol,
        rfcomm_channel,
        profile_uuid,
        profile_version,
    }
}
const fn short(uuid: u16) -> ServiceUuid {
    ServiceUuid::Short(uuid)
}
pub const BLUEZ_LIKE_EXTRA_SERVICE_SPECS: [ExtraServiceSpec; 20] = [
    extra("Generic Access", short(0x1800), "att", None, None, None),
    extra("Generic Attribute", short(0x1801), "att", None, None, None),
    extra("Device Information", short(0x180a), "att", None, None, None),
    extra(
        "Audio Input Control",
        short(0x1843),
        "att",
        None,
        None,
        None,
    ),
    extra("Volume Control", short(0x1844), "att", None, None, None),
    extra(
        "Volume Offset Control",
        short(0x1845),
        "att",
        None,
        None,
        None,
    ),
    extra(
        "Generic Media Control",
        short(0x1849),
        "att",
        None,
        None,
        None,
    ),
    extra("Microphone Control", short(0x184d), "att", None, None, None),
    extra(
        "Broadcast Audio Scan",
        short(0x184f),
        "att",
        None,
        None,
        None,
    ),
    extra("Ranging Service", short(0x185b), "att", None, None, None),
    extra(
        "A/V RemoteControlController",
        short(0x110f),
        "avrcp-controller",
        None,
        None,
        None,
    ),
    extra("Audio Sink", short(0x110b), "audio-sink", None, None, None),
    extra("Handsfree", short(0x111e), "handsfree", None, None, None),
    extra(
        "Message Notification Server",
        short(0x1133),
        "obex",
        Some(17),
        Some(short(0x1134)),
        Some(0x0104),
    ),
    extra(
        "Message Access Server",
        short(0x1132),
        "obex",
        Some(16),
        Some(short(0x1134)),
        Some(0x0100),
    ),
    extra(
        "Phone Book Access Server",
        short(0x112f),
        "obex",
        Some(15),
        Some(short(0x1130)),
        Some(0x0101),
    ),
    extra(
        "Synchronization",
        short(0x1104),
        "obex",
        Some(14),
        Some(short(0x1104)),
        Some(0x0100),
    ),
    extra(
        "OBEX File Transfer",
        short(0x1106),
        "obex",
        Some(10),
        Some(short(0x1106)),
        Some(0x0103),
    ),
    extra(
        "OBEX Object Push",
        short(0x1105),
        "obex",
        Some(9),
        Some(short(0x1105)),
        Some(0x0102),
    ),
    extra(
        "Nokia OBEX PC Suite Services",
        NOKIA_UUID,
        "obex",
        Some(24),
        Some(NOKIA_UUID),
        Some(0x0100),
    ),
];

/// Decimal arithmetic keeps Python's unbounded integer handle behavior.
/// Every new candidate exceeds every existing handle, so occupied skipping is implicit.
pub fn allocate_handles(existing: &[String], count: usize) -> Result<Vec<String>, &'static str> {
    fn normalized(value: &str) -> Result<&str, &'static str> {
        let positive = value.strip_prefix('+').unwrap_or(value);
        if positive.starts_with('-') {
            return Ok("0");
        }
        if positive.is_empty() || !positive.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err("invalid handle");
        }
        Ok(positive.trim_start_matches('0'))
    }
    let mut maximum = String::from("0");
    for value in existing {
        let value = normalized(value)?;
        if value.len() > maximum.len() || (value.len() == maximum.len() && value > maximum.as_str())
        {
            maximum = value.to_owned();
        }
    }
    fn increment(value: &mut String) {
        let mut bytes = value.as_bytes().to_vec();
        for byte in bytes.iter_mut().rev() {
            if *byte < b'9' {
                *byte += 1;
                *value = String::from_utf8(bytes).expect("decimal digits");
                return;
            }
            *byte = b'0';
        }
        bytes.insert(0, b'1');
        *value = String::from_utf8(bytes).expect("decimal digits");
    }
    let mut result = Vec::with_capacity(count);
    for _ in 0..count {
        increment(&mut maximum);
        result.push(maximum.clone());
    }
    Ok(result)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct QueryDecision {
    pub target: bool,
    pub continuation_used: bool,
    pub retain_summary: bool,
    pub mark_prior_target: bool,
}
/// The caller extracts SDP facts and keeps the first target summary. This function
/// decides whether later requests enter the bounded general list or mark it.
#[allow(clippy::too_many_arguments)] // Each argument is one neutral fact extracted at the Bumble boundary.
pub fn query_decision(
    uuids: &[u16],
    ranges: &[(u16, u16)],
    maximum: i64,
    peer_mtu: Option<i64>,
    response_bytes: i64,
    continuation_state_len: usize,
    stored: usize,
    target_already_seen: bool,
) -> QueryDecision {
    let target = uuids.contains(&0x0100) && ranges.contains(&(0, 0xffff));
    let continuation = continuation_state_len > 1;
    let effective = peer_mtu.map_or(maximum, |mtu| maximum.min(mtu - 9)).max(0);
    QueryDecision {
        target,
        continuation_used: continuation || response_bytes > effective,
        retain_summary: !target && !continuation && stored < REFERENCE_SDP_QUERY_SUMMARY_LIMIT,
        mark_prior_target: target && target_already_seen && continuation,
    }
}

pub fn first_l2cap_summary_index(summaries: &[Vec<u16>]) -> Option<usize> {
    summaries.iter().position(|uuids| uuids.contains(&0x0100))
}

/// Reference summaries retain only UUIDs encoded by Bumble as short UUIDs.
/// Unlike diagnostic service classification, equivalent 128-bit UUIDs are
/// deliberately excluded to preserve the parent observer's output.
pub fn query_uuid16s(values: &[Vec<u8>]) -> Vec<u16> {
    values
        .iter()
        .filter(|value| value.len() == 2)
        .map(|value| u16::from_le_bytes([value[0], value[1]]))
        .collect()
}

pub fn query_attribute_ranges(values: &[(u32, u8)]) -> Vec<(u16, u16)> {
    values
        .iter()
        .filter_map(|(value, size)| match size {
            2 => Some((*value as u16, *value as u16)),
            4 => Some(((value >> 16) as u16, *value as u16)),
            _ => None,
        })
        .collect()
}
















