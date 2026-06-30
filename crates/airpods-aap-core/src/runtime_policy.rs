//! Pure runtime decisions. No transport, clock, or exception crosses this boundary.







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

















































#[cfg(test)]
mod tests {
    use super :: * ;

    

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
