//! Closed Classic diagnostic identities and safe neutral snapshots.

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Profile {
    ProjectDefault,
    LegacyPoc,
}

impl TryFrom<&str> for Profile {
    type Error = ();
    fn try_from(value: &str) -> Result<Self, Self::Error> {
        match value {
            "project-default" => Ok(Self::ProjectDefault),
            "legacy-poc" => Ok(Self::LegacyPoc),
            _ => Err(()),
        }
    }
}

impl Profile {
    pub fn name(self) -> &'static str {
        match self {
            Self::ProjectDefault => "airpods-hr authentication probe",
            Self::LegacyPoc => "AirPods-RE",
        }
    }
}

pub enum LocalName<'a> {
    Text(&'a str),
    Bytes(&'a [u8]),
    Unavailable,
}

pub fn local_name_matches(value: LocalName<'_>, profile: Profile) -> Option<bool> {
    let name = match value {
        LocalName::Text(s) => s.split('\0').next().unwrap_or(""),
        LocalName::Bytes(bytes) => {
            std::str::from_utf8(bytes.split(|b| *b == 0).next().unwrap_or(&[])).ok()?
        }
        LocalName::Unavailable => return None,
    };
    Some(name == profile.name())
}

pub const POWER_ON_CLASSIC_WRITES: &[&str] = &[
    "local_name",
    "class_of_device",
    "simple_pairing_mode",
    "secure_connections_host_support",
    "scan_enable",
    "extended_inquiry_response",
    "page_scan_type_if_supported",
    "inquiry_scan_type_if_supported",
];
pub const POWER_ON_NOT_EXPLICITLY_WRITTEN: &[&str] = &[
    "authentication_enable",
    "connection_accept_timeout",
    "default_link_policy",
    "page_timeout",
    "page_scan_activity",
    "inquiry_scan_activity",
    "voice_setting",
];

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Audit {
    pub profile: Profile,
    pub flags: [bool; 15],
    pub class_of_device: i64,
    pub io_capability: i64,
    pub l2cap_extended_features: Vec<i64>,
}

pub fn audit(
    profile: Profile,
    flags: [bool; 15],
    class_of_device: i64,
    io_capability: i64,
    features: &[i64],
) -> Audit {
    Audit {
        profile,
        flags,
        class_of_device,
        io_capability,
        l2cap_extended_features: features.to_vec(),
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct HostSnapshot {
    pub profile: Profile,
    pub configured_flags: [bool; 6],
    pub io_capability: i64,
    pub local_name_match: Option<bool>,
    pub observations: [Option<i64>; 10],
    pub unavailable: Vec<String>,
}

pub fn host_snapshot(
    profile: Profile,
    configured_flags: [bool; 6],
    io_capability: i64,
    local_name_match: Option<bool>,
    observations: [Option<i64>; 10],
    unavailable: &[String],
) -> HostSnapshot {
    HostSnapshot {
        profile,
        configured_flags,
        io_capability,
        local_name_match,
        observations,
        unavailable: unavailable.to_vec(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn profiles_and_names_are_closed() {
        assert_eq!(
            Profile::try_from("project-default").unwrap().name(),
            "airpods-hr authentication probe"
        );
        assert_eq!(
            Profile::try_from("legacy-poc").unwrap().name(),
            "AirPods-RE"
        );
        assert!(Profile::try_from("custom").is_err());
    }
    #[test]
    fn local_name_rules() {
        let p = Profile::LegacyPoc;
        assert_eq!(
            local_name_matches(LocalName::Text("AirPods-RE\0garbage"), p),
            Some(true)
        );
        assert_eq!(
            local_name_matches(LocalName::Bytes(b"AirPods-RE\0garbage"), p),
            Some(true)
        );
        let mut padded = b"AirPods-RE".to_vec();
        padded.resize(248, 0);
        assert_eq!(local_name_matches(LocalName::Bytes(&padded), p), Some(true));
        assert_eq!(local_name_matches(LocalName::Bytes(&[0xff]), p), None);
        assert_eq!(local_name_matches(LocalName::Text(""), p), Some(false));
        assert_eq!(
            local_name_matches(LocalName::Text("\0AirPods-RE"), p),
            Some(false)
        );
        assert_eq!(local_name_matches(LocalName::Unavailable, p), None);
    }
    #[test]
    fn static_facts_and_neutral_snapshots() {
        assert_eq!(POWER_ON_CLASSIC_WRITES[0], "local_name");
        assert_eq!(POWER_ON_NOT_EXPLICITLY_WRITTEN[6], "voice_setting");
        assert!(
            POWER_ON_CLASSIC_WRITES
                .iter()
                .all(|x| !POWER_ON_NOT_EXPLICITLY_WRITTEN.contains(x))
        );
        let flags = [true; 15];
        let a = audit(Profile::ProjectDefault, flags, 7, 8, &[3, 2, 1]);
        assert_eq!(a.flags, flags);
        assert_eq!(a.l2cap_extended_features, [3, 2, 1]);
        let h = host_snapshot(
            Profile::LegacyPoc,
            [false; 6],
            4,
            None,
            [None; 10],
            &[
                "local_name_matches_profile".into(),
                "page_scan_activity".into(),
            ],
        );
        assert_eq!(
            h.unavailable,
            ["local_name_matches_profile", "page_scan_activity"]
        );
        assert_eq!(h.local_name_match, None);
    }
}
