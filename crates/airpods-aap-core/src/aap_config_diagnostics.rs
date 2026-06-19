//! Pure policy for the probe-only AAP Configure exchange.

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OptionEntry {
    pub option_type: u8,
    pub value: Vec<u8>,
    pub encoded: Vec<u8>,
}

pub fn decode_options(data: &[u8]) -> Option<Vec<OptionEntry>> {
    let mut result = Vec::new();
    let mut offset = 0;
    while offset < data.len() {
        if data.len() - offset < 2 {
            return None;
        }
        let end = offset + 2 + usize::from(data[offset + 1]);
        if end > data.len() {
            return None;
        }
        result.push(OptionEntry {
            option_type: data[offset],
            value: data[offset + 2..end].to_vec(),
            encoded: data[offset..end].to_vec(),
        });
        offset = end;
    }
    Some(result)
}

pub fn value_u16(options: &[OptionEntry], option_type: u8) -> Option<u16> {
    let mut matching = options.iter().filter(|o| o.option_type == option_type);
    let first = matching.next()?;
    if matching.next().is_some() || first.value.len() != 2 {
        return None;
    }
    Some(u16::from_le_bytes([first.value[0], first.value[1]]))
}

pub fn rfc_mode(options: &[OptionEntry]) -> Option<u8> {
    let mut matching = options.iter().filter(|o| o.option_type == 4);
    let first = matching.next()?;
    if matching.next().is_some() || first.value.len() != 9 {
        return None;
    }
    Some(first.value[0])
}

pub fn reviewed_request_mtu(options: &[OptionEntry]) -> Option<&OptionEntry> {
    if options.iter().any(|o| !matches!(o.option_type, 1 | 2 | 4))
        || value_u16(options, 1).is_none()
        || value_u16(options, 2).is_none()
    {
        return None;
    }
    let rfc: Vec<_> = options.iter().filter(|o| o.option_type == 4).collect();
    if rfc.len() > 1
        || rfc
            .first()
            .is_some_and(|o| o.value.len() != 9 || o.value[0] != 0)
    {
        return None;
    }
    options.iter().find(|o| o.option_type == 1)
}

#[derive(Debug, PartialEq, Eq)]
pub enum RequestPlan {
    Delegate { mtu_encoded: Option<Vec<u8>> },
    FailUnknownOptions,
}

pub fn request_plan(kernel_mtu_only: bool, options: &[u8], flags: u16) -> RequestPlan {
    let decoded = decode_options(options);
    let mtu = decoded.as_ref().and_then(|o| {
        if flags == 0 {
            reviewed_request_mtu(o)
        } else {
            None
        }
    });
    if kernel_mtu_only && mtu.is_none() {
        RequestPlan::FailUnknownOptions
    } else {
        RequestPlan::Delegate {
            mtu_encoded: mtu.map(|o| o.encoded.clone()),
        }
    }
}

pub fn response_rewrite(
    kernel_mtu_only: bool,
    request_identifier: u8,
    response_identifier: u8,
    success: bool,
    mtu_encoded: Option<&[u8]>,
) -> (bool, Option<Vec<u8>>) {
    let matching = request_identifier == response_identifier;
    if kernel_mtu_only && matching && success {
        (matching, mtu_encoded.map(<[u8]>::to_vec))
    } else {
        (matching, None)
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OptionSummary {
    pub types: Vec<u8>,
    pub mtu: Option<u16>,
    pub flush_timeout: Option<u16>,
    pub rfc_present: bool,
    pub rfc_mode: Option<u8>,
}

pub fn summarize(options: &[OptionEntry]) -> OptionSummary {
    OptionSummary {
        types: options.iter().map(|o| o.option_type).collect(),
        mtu: value_u16(options, 1),
        flush_timeout: value_u16(options, 2),
        rfc_present: options.iter().any(|o| o.option_type == 4),
        rfc_mode: rfc_mode(options),
    }
}

pub fn summarize_bytes(data: &[u8]) -> OptionSummary {
    summarize(&decode_options(data).unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::*;

    const HISTORICAL: &[u8] = &[
        1, 2, 0x16, 0x0a, 2, 2, 0x1e, 0, 4, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ];

    #[test]
    fn decode_is_atomic_and_retains_wire_bytes() {
        assert_eq!(decode_options(&[]), Some(vec![]));
        assert_eq!(decode_options(&[1]), None);
        assert_eq!(decode_options(&[1, 2, 1]), None);
        assert_eq!(decode_options(&[1, 0, 2]), None);
        let options = decode_options(HISTORICAL).unwrap();
        assert_eq!(
            options.iter().map(|o| o.option_type).collect::<Vec<_>>(),
            [1, 2, 4]
        );
        assert_eq!(options[0].encoded, [1, 2, 0x16, 0x0a]);
        assert_eq!(options[2].value, [0; 9]);
        for kind in u8::MIN..=u8::MAX {
            assert_eq!(decode_options(&[kind, 0]).unwrap()[0].option_type, kind);
        }
    }

    #[test]
    fn values_and_reviewed_policy() {
        let options = decode_options(HISTORICAL).unwrap();
        assert_eq!(value_u16(&options, 1), Some(2582));
        assert_eq!(value_u16(&options, 2), Some(30));
        assert_eq!(rfc_mode(&options), Some(0));
        assert_eq!(
            reviewed_request_mtu(&options).unwrap().encoded,
            [1, 2, 0x16, 0x0a]
        );
        assert!(reviewed_request_mtu(&options[..2]).is_some());
        assert!(reviewed_request_mtu(&options[..1]).is_none());
        for kind in 0..=255u8 {
            if !matches!(kind, 1 | 2 | 4) {
                let mut extra = options.clone();
                extra.push(OptionEntry {
                    option_type: kind,
                    value: vec![],
                    encoded: vec![kind, 0],
                });
                assert!(reviewed_request_mtu(&extra).is_none());
            }
        }
        for kind in [1, 2, 4] {
            let mut duplicate = options.clone();
            duplicate.push(
                options
                    .iter()
                    .find(|o| o.option_type == kind)
                    .unwrap()
                    .clone(),
            );
            assert!(reviewed_request_mtu(&duplicate).is_none());
        }
        for value in [0u16, 1, u16::MAX] {
            let entry = OptionEntry {
                option_type: 1,
                value: value.to_le_bytes().to_vec(),
                encoded: vec![],
            };
            assert_eq!(value_u16(&[entry], 1), Some(value));
        }
        assert_eq!(
            value_u16(
                &[OptionEntry {
                    option_type: 1,
                    value: vec![1],
                    encoded: vec![]
                }],
                1
            ),
            None
        );
        let mut bad_rfc = options.clone();
        bad_rfc[2].value[0] = 1;
        assert!(reviewed_request_mtu(&bad_rfc).is_none());
        bad_rfc[2].value.pop();
        assert_eq!(rfc_mode(&bad_rfc), None);
    }

    #[test]
    fn plans_and_observation() {
        assert_eq!(
            request_plan(true, HISTORICAL, 0),
            RequestPlan::Delegate {
                mtu_encoded: Some(vec![1, 2, 0x16, 0x0a])
            }
        );
        assert_eq!(
            request_plan(true, HISTORICAL, 1),
            RequestPlan::FailUnknownOptions
        );
        assert_eq!(request_plan(true, &[1], 0), RequestPlan::FailUnknownOptions);
        assert_eq!(
            request_plan(false, &[1], 0),
            RequestPlan::Delegate { mtu_encoded: None }
        );
        assert_eq!(
            response_rewrite(true, 7, 7, true, Some(&[1, 2, 3, 4])),
            (true, Some(vec![1, 2, 3, 4]))
        );
        assert_eq!(
            response_rewrite(true, 7, 7, false, Some(&[1, 2, 3, 4])),
            (true, None)
        );
        assert_eq!(
            response_rewrite(false, 7, 7, true, Some(&[1, 2, 3, 4])),
            (true, None)
        );
        assert_eq!(
            response_rewrite(true, 7, 8, true, Some(&[1])),
            (false, None)
        );
        let summary = summarize_bytes(HISTORICAL);
        assert_eq!(summary.types, [1, 2, 4]);
        assert_eq!(summary.mtu, Some(2582));
        assert_eq!(summary.flush_timeout, Some(30));
        assert_eq!(summary.rfc_mode, Some(0));
        assert!(summarize_bytes(&[1]).types.is_empty());
    }

    #[test]
    fn seeded_valid_wire_roundtrips_without_reordering_or_partial_acceptance() {
        let mut seed = 0x1234_5678_u64;
        for _ in 0..5_000 {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            let count = (seed % 8) as usize;
            let mut wire = Vec::new();
            let mut kinds = Vec::new();
            for _ in 0..count {
                seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
                let kind = seed as u8;
                let len = ((seed >> 8) % 32) as usize;
                kinds.push(kind);
                wire.push(kind);
                wire.push(len as u8);
                for _ in 0..len {
                    seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
                    wire.push(seed as u8);
                }
            }
            let parsed = decode_options(&wire).unwrap();
            assert_eq!(
                parsed.iter().map(|o| o.option_type).collect::<Vec<_>>(),
                kinds
            );
            assert_eq!(
                parsed
                    .iter()
                    .flat_map(|o| o.encoded.iter().copied())
                    .collect::<Vec<_>>(),
                wire
            );
            if !wire.is_empty() {
                assert!(decode_options(&wire[..wire.len() - 1]).is_none());
            }
        }
    }
}
