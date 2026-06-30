//! Pure runtime decisions. No transport, clock, or exception crosses this boundary.

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ChannelFacts {
    Valid,
    NotOpen,
    NotBasic,
    WrongPsm,
    InvalidMtu,
}

#[must_use]
pub fn channel_facts(
    is_open: bool,
    is_basic: bool,
    psm: i64,
    local_mtu: i64,
    peer_mtu: i64,
    expected_psm: i64,
) -> ChannelFacts {
    if !is_open {
        ChannelFacts::NotOpen
    } else if !is_basic {
        ChannelFacts::NotBasic
    } else if psm != expected_psm {
        ChannelFacts::WrongPsm
    } else if local_mtu <= 0 || peer_mtu <= 0 {
        ChannelFacts::InvalidMtu
    } else {
        ChannelFacts::Valid
    }
}

#[must_use]
pub fn positive_timeouts(values: &[f64]) -> bool {
    values.iter().all(|value| *value > 0.0)
}

#[must_use]
pub fn adapter_index_digits(name: &str) -> Option<&str> {
    name.strip_prefix("hci")
        .filter(|digits| !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit()))
}

fn regex_ascii_alphanumeric(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '\u{0130}' | '\u{0131}' | '\u{017f}' | '\u{212a}')
}

fn regex_char_equals(c: char, expected: char) -> bool {
    c.eq_ignore_ascii_case(&expected)
        || matches!(
            (c, expected),
            ('\u{0130}' | '\u{0131}', 'i') | ('\u{017f}', 's')
        )
}

/// Match Python re.I semantics for `(?<![a-z0-9])airpods(?![a-z0-9])`.
#[must_use]
pub fn supported_airpods_name(name: Option<&str>, alias: Option<&str>) -> bool {
    [name, alias].into_iter().flatten().any(|value| {
        let chars: Vec<char> = value.chars().collect();
        chars.windows(7).enumerate().any(|(start, window)| {
            (start == 0 || !regex_ascii_alphanumeric(chars[start - 1]))
                && window
                    .iter()
                    .zip("airpods".chars())
                    .all(|(actual, expected)| regex_char_equals(*actual, expected))
                && (start + 7 == chars.len() || !regex_ascii_alphanumeric(chars[start + 7]))
        })
    })
}

#[must_use]
pub fn display_name<'a>(alias: Option<&'a str>, name: Option<&'a str>) -> &'a str {
    alias
        .filter(|value| !value.is_empty())
        .or_else(|| name.filter(|value| !value.is_empty()))
        .unwrap_or("AirPods")
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CandidateCount {
    None,
    One,
    Ambiguous,
}

#[must_use]
pub fn candidate_count(count: usize) -> CandidateCount {
    match count {
        0 => CandidateCount::None,
        1 => CandidateCount::One,
        _ => CandidateCount::Ambiguous,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TransportSend {
    Handshake,
    HeartRate,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TransportLegality {
    Allowed,
    CollectionInactive,
    HandshakeAlreadySent,
    HandshakeMissing,
}

#[must_use]
pub fn transport_legality(
    active: bool,
    sent: usize,
    operation: TransportSend,
) -> TransportLegality {
    if !active {
        TransportLegality::CollectionInactive
    } else {
        match operation {
            TransportSend::Handshake if sent != 0 => TransportLegality::HandshakeAlreadySent,
            TransportSend::HeartRate if sent == 0 => TransportLegality::HandshakeMissing,
            _ => TransportLegality::Allowed,
        }
    }
}

/// Tracks only application send policy, never the raw channel or queue.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct TransportPolicy {
    pub collection_active: bool,
    pub application_payloads_sent: usize,
}

impl TransportPolicy {
    pub fn begin_collection(&mut self) -> bool {
        if self.collection_active {
            false
        } else {
            self.collection_active = true;
            true
        }
    }

    pub fn end_collection(&mut self) {
        self.collection_active = false;
    }

    #[must_use]
    pub fn send_legality(&self, operation: TransportSend) -> TransportLegality {
        transport_legality(
            self.collection_active,
            self.application_payloads_sent,
            operation,
        )
    }

    pub fn sent(&mut self, operation: TransportSend) -> Result<usize, TransportLegality> {
        let legality = self.send_legality(operation);
        if legality != TransportLegality::Allowed {
            return Err(legality);
        }
        self.application_payloads_sent += 1;
        Ok(self.application_payloads_sent)
    }
}







































#[cfg(test)]
mod tests {
    use super :: * ;

    #[test]
    fn channel_precedence_and_boundaries() {
        assert_eq!(
            channel_facts(false, false, 0, 0, 0, 0),
            ChannelFacts::NotOpen
        );
        assert_eq!(
            channel_facts(true, false, 0, 0, 0, 0),
            ChannelFacts::NotBasic
        );
        assert_eq!(
            channel_facts(true, true, 1, 0, 0, 0),
            ChannelFacts::WrongPsm
        );
        assert_eq!(
            channel_facts(true, true, 0, 0, 1, 0),
            ChannelFacts::InvalidMtu
        );
        assert_eq!(
            channel_facts(true, true, 0, 1, i64::MAX, 0),
            ChannelFacts::Valid
        );
    }

    #[test]
    fn discovery_names_and_adapter_boundaries() {
        for name in [
            "AirPods",
            "(airpods)",
            "my_airpods",
            "AIRPODS!",
            "中airpods中",
        ] {
            assert!(supported_airpods_name(Some(name), None), "{name}");
        }
        for name in [
            "",
            "xairpods",
            "airpods2",
            "airpodsx",
            "airpod",
            "airpods\u{0130}",
        ] {
            assert!(!supported_airpods_name(Some(name), None), "{name}");
        }
        assert!(supported_airpods_name(None, Some("airpod\u{017f}")));
        assert_eq!(display_name(Some("Alias"), Some("Name")), "Alias");
        assert_eq!(display_name(Some(""), Some("Name")), "Name");
        assert_eq!(display_name(None, None), "AirPods");
        for name in ["hci0", "hci0001", "hci10"] {
            assert!(adapter_index_digits(name).is_some());
        }
        for name in ["hci", "HCI0", "hci-1", "hci+1", " hci0", "hci0x", "hci١"] {
            assert!(adapter_index_digits(name).is_none());
        }
        assert_eq!(candidate_count(0), CandidateCount::None);
        assert_eq!(candidate_count(1), CandidateCount::One);
        assert_eq!(candidate_count(2), CandidateCount::Ambiguous);
    }

    #[test]
    fn seeded_ascii_name_parity() {
        let mut seed = 0x5eed_u64;
        for _ in 0..10_000 {
            seed = seed.wrapping_mul(1664525).wrapping_add(1013904223);
            let left = (seed as u8 % 128) as char;
            let right = ((seed >> 8) as u8 % 128) as char;
            let value = format!("{left}airpods{right}");
            assert_eq!(
                supported_airpods_name(Some(&value), None),
                !left.is_ascii_alphanumeric() && !right.is_ascii_alphanumeric(),
                "{value:?}"
            );
        }
    }

    

    

    

    #[test]
    fn transport_send_legality_is_closed_and_ordered() {
        use TransportLegality as L;
        use TransportSend as S;
        for sent in [0, 1, 2, usize::MAX] {
            assert_eq!(
                transport_legality(false, sent, S::Handshake),
                L::CollectionInactive
            );
            assert_eq!(
                transport_legality(false, sent, S::HeartRate),
                L::CollectionInactive
            );
            assert_eq!(
                transport_legality(true, sent, S::Handshake),
                if sent == 0 {
                    L::Allowed
                } else {
                    L::HandshakeAlreadySent
                }
            );
            assert_eq!(
                transport_legality(true, sent, S::HeartRate),
                if sent == 0 {
                    L::HandshakeMissing
                } else {
                    L::Allowed
                }
            );
        }
    }

    #[test]
    fn transport_policy_counts_only_accepted_sends_and_survives_collections() {
        use TransportLegality as L;
        use TransportSend as S;
        let mut policy = TransportPolicy::default();
        assert_eq!(policy.send_legality(S::Handshake), L::CollectionInactive);
        assert_eq!(policy.sent(S::Handshake), Err(L::CollectionInactive));
        assert!(policy.begin_collection());
        assert!(!policy.begin_collection());
        assert_eq!(policy.sent(S::HeartRate), Err(L::HandshakeMissing));
        assert_eq!(policy.application_payloads_sent, 0);
        assert_eq!(policy.sent(S::Handshake), Ok(1));
        assert_eq!(policy.sent(S::Handshake), Err(L::HandshakeAlreadySent));
        assert_eq!(policy.sent(S::HeartRate), Ok(2));
        policy.end_collection();
        assert_eq!(policy.sent(S::HeartRate), Err(L::CollectionInactive));
        assert!(policy.begin_collection());
        assert_eq!(policy.sent(S::HeartRate), Ok(3));
        assert_eq!(policy.application_payloads_sent, 3);
    }

    #[test]
    fn channel_failure_precedence_exhausts_boolean_facts() {
        for mask in 0..8 {
            let open = mask & 1 != 0;
            let basic = mask & 2 != 0;
            let correct_psm = mask & 4 != 0;
            let actual = channel_facts(open, basic, i64::from(!correct_psm), 0, 0, 0);
            let expected = if !open {
                ChannelFacts::NotOpen
            } else if !basic {
                ChannelFacts::NotBasic
            } else if !correct_psm {
                ChannelFacts::WrongPsm
            } else {
                ChannelFacts::InvalidMtu
            };
            assert_eq!(actual, expected, "mask={mask}");
        }
        for (local, peer, expected) in [
            (-1, 1, ChannelFacts::InvalidMtu),
            (0, 1, ChannelFacts::InvalidMtu),
            (1, 0, ChannelFacts::InvalidMtu),
            (1, 1, ChannelFacts::Valid),
            (i64::MAX, 1, ChannelFacts::Valid),
        ] {
            assert_eq!(
                channel_facts(true, true, 0x1001, local, peer, 0x1001),
                expected
            );
        }
    }

    #[test]
    fn unicode_regex_surroundings_and_optional_alias_are_exact() {
        for value in [
            "airpods",
            "AIRPODS",
            "(AirPods)",
            "AirPods!",
            "AirPods Pro",
            "my_airpods",
            "éairpodsé",
            "airpodſ",
            "aİrpods",
            "aırpods",
        ] {
            assert!(supported_airpods_name(Some(value), None), "{value}");
        }
        for value in [
            "",
            "airpod",
            "airpodss",
            "xairpods",
            "airpods9",
            "AirPodsK",
            "airpodsİ",
            "airpodsı",
            "airpodsſ",
            "airpodsK",
        ] {
            assert!(!supported_airpods_name(Some(value), None), "{value}");
        }
        assert!(supported_airpods_name(None, Some("airpods")));
        assert!(supported_airpods_name(
            Some("not matching"),
            Some("AIRPODS")
        ));
        assert!(!supported_airpods_name(None, None));
        assert!(!supported_airpods_name(Some(""), Some("")));
        assert_eq!(display_name(Some("Alias"), Some("Name")), "Alias");
        assert_eq!(display_name(Some(""), Some("Name")), "Name");
        assert_eq!(display_name(Some(""), Some("")), "AirPods");
    }

    

    
}
