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





























