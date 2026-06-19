//! Pure host Configure Request and post-ACK observation policy.

use crate::aap_config_diagnostics::{OptionEntry, decode_options};

pub const POST_ACK_TYPE_17_LENGTH_LIMIT: usize = 8;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OptionError {
    Malformed,
    MalformedMtu,
}

pub fn parse(data: &[u8]) -> Result<(Vec<OptionEntry>, Option<u16>), OptionError> {
    let options = decode_options(data).ok_or(OptionError::Malformed)?;
    let mut mtu = options.iter().filter(|o| o.option_type == 1);
    let first = mtu.next();
    if mtu.next().is_some() || first.is_some_and(|o| o.value.len() != 2) {
        return Err(OptionError::MalformedMtu);
    }
    let value = first.map(|o| u16::from_le_bytes([o.value[0], o.value[1]]));
    Ok((options, value))
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Rewrite {
    pub options: Vec<u8>,
    pub option_types: Vec<u8>,
    pub mtu: Option<u16>,
    pub original_mtu: Option<u16>,
}

pub fn rewrite(kernel_default: bool, data: &[u8]) -> Result<Rewrite, OptionError> {
    let (options, mtu) = parse(data)?;
    let retained: Vec<_> = options
        .iter()
        .filter(|o| !kernel_default || o.option_type != 1)
        .collect();
    Ok(Rewrite {
        options: retained
            .iter()
            .flat_map(|o| o.encoded.iter().copied())
            .collect(),
        option_types: retained.iter().map(|o| o.option_type).collect(),
        mtu: if kernel_default { None } else { mtu },
        original_mtu: mtu,
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct State {
    pub mode: &'static str,
    pub request_observed: bool,
    pub request_option_types: Vec<u8>,
    pub request_mtu: Option<u16>,
    pub request_flags: Option<u16>,
    pub request_identifier: Option<u8>,
    pub request_destination_cid: Option<u16>,
    pub internal_receive_mtu: Option<u16>,
    pub peer_response_observed: bool,
    pub peer_response_result: Option<u16>,
    pub peer_response_option_types: Vec<u8>,
    pub peer_response_mtu: Option<u16>,
}

impl State {
    pub fn new(kernel_default: bool) -> Self {
        Self {
            mode: if kernel_default {
                "kernel-default"
            } else {
                "proven"
            },
            request_observed: false,
            request_option_types: vec![],
            request_mtu: None,
            request_flags: None,
            request_identifier: None,
            request_destination_cid: None,
            internal_receive_mtu: None,
            peer_response_observed: false,
            peer_response_result: None,
            peer_response_option_types: vec![],
            peer_response_mtu: None,
        }
    }

    pub fn observe_request(
        &mut self,
        rewrite: &Rewrite,
        flags: u16,
        identifier: u8,
        cid: u16,
        mtu: u16,
    ) {
        if self.request_observed {
            return;
        }
        self.request_observed = true;
        self.request_option_types = rewrite.option_types.clone();
        self.request_mtu = rewrite.mtu;
        self.request_flags = Some(flags);
        self.request_identifier = Some(identifier);
        self.request_destination_cid = Some(cid);
        self.internal_receive_mtu = Some(mtu);
    }

    pub fn matches_response(&self, identifier: u8, channel_psm: Option<u16>, aap_psm: u16) -> bool {
        self.identifier_matches(identifier) && channel_psm == Some(aap_psm)
    }

    pub fn identifier_matches(&self, identifier: u8) -> bool {
        self.request_observed && self.request_identifier == Some(identifier)
    }

    pub fn observe_response(&mut self, result: u16, data: &[u8]) -> Result<(), OptionError> {
        let (options, mtu) = parse(data)?;
        self.peer_response_observed = true;
        self.peer_response_result = Some(result);
        self.peer_response_option_types = options.iter().map(|o| o.option_type).collect();
        self.peer_response_mtu = mtu;
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct PostAckShape {
    pub first_type_2b_length: Option<usize>,
    pub type_17_lengths: Vec<usize>,
    pub maximum: Option<usize>,
    pub considered: usize,
}

pub fn post_ack_shape(summaries: &[(Option<u16>, usize)]) -> PostAckShape {
    let mut result = PostAckShape {
        first_type_2b_length: None,
        type_17_lengths: vec![],
        maximum: None,
        considered: summaries.len(),
    };
    for &(kind, length) in summaries {
        result.maximum = Some(result.maximum.map_or(length, |old| old.max(length)));
        if kind == Some(0x2b) && result.first_type_2b_length.is_none() {
            result.first_type_2b_length = Some(length);
        }
        if kind == Some(0x17) && result.type_17_lengths.len() < POST_ACK_TYPE_17_LENGTH_LIMIT {
            result.type_17_lengths.push(length);
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parser_and_rewrite() {
        assert_eq!(parse(&[1]).unwrap_err(), OptionError::Malformed);
        assert_eq!(parse(&[1, 1, 2]).unwrap_err(), OptionError::MalformedMtu);
        assert_eq!(
            parse(&[1, 2, 0, 0, 1, 2, 1, 0]).unwrap_err(),
            OptionError::MalformedMtu
        );
        assert_eq!(parse(&[2, 0]).unwrap().1, None);
        for mtu in [0u16, 1, u16::MAX] {
            let mut wire = vec![2, 1, 0, 1, 2];
            wire.extend(mtu.to_le_bytes());
            wire.extend([4, 0, 5, 2, 0, 255]);
            assert_eq!(parse(&wire).unwrap().1, Some(mtu));
            assert_eq!(rewrite(false, &wire).unwrap().options, wire);
            assert_eq!(
                rewrite(true, &wire).unwrap().options,
                [2, 1, 0, 4, 0, 5, 2, 0, 255]
            );
            assert_eq!(rewrite(true, &wire).unwrap().option_types, [2, 4, 5]);
        }
        assert_eq!(rewrite(true, &[1, 2, 0, 0]).unwrap().options, []);
    }

    #[test]
    fn state_and_shape() {
        let mut state = State::new(true);
        assert!(!state.request_observed);
        assert!(!state.matches_response(7, Some(3), 3));
        let rewrite = rewrite(true, &[1, 2, 0, 8, 2, 0]).unwrap();
        state.observe_request(&rewrite, 0, 7, 64, 2048);
        state.observe_request(&rewrite, 1, 8, 65, 42);
        assert_eq!(state.request_identifier, Some(7));
        assert!(!state.matches_response(8, Some(3), 3));
        assert!(!state.matches_response(7, None, 3));
        assert!(!state.matches_response(7, Some(4), 3));
        assert!(state.matches_response(7, Some(3), 3));
        state.observe_response(0, &[1, 2, 0xa0, 2]).unwrap();
        assert_eq!(state.peer_response_mtu, Some(672));
        let empty = post_ack_shape(&[]);
        assert_eq!(empty.maximum, None);
        let summaries = [
            (Some(0x2b), 5),
            (Some(0x2b), 6),
            (None, 100),
            (Some(0x17), 2),
        ];
        let shape = post_ack_shape(&summaries);
        assert_eq!(shape.first_type_2b_length, Some(5));
        assert_eq!(shape.type_17_lengths, [2]);
        assert_eq!(shape.maximum, Some(100));
        let many = post_ack_shape(&(0..20).map(|i| (Some(0x17), i)).collect::<Vec<_>>());
        assert_eq!(many.type_17_lengths, (0..8).collect::<Vec<_>>());
        assert_eq!(many.considered, 20);
    }

    #[test]
    fn seeded_rewrite_and_post_ack_invariants() {
        let mut seed = 0xdecafbad_u64;
        for _ in 0..5_000 {
            let mut wire = vec![1, 2];
            seed = seed
                .wrapping_mul(2862933555777941757)
                .wrapping_add(3037000493);
            wire.extend((seed as u16).to_le_bytes());
            let count = ((seed >> 20) % 12) as usize;
            let mut non_mtu = Vec::new();
            for _ in 0..count {
                seed = seed
                    .wrapping_mul(2862933555777941757)
                    .wrapping_add(3037000493);
                let kind = (seed as u8).max(2);
                let value = [(seed >> 8) as u8, (seed >> 16) as u8];
                wire.extend([kind, 2]);
                wire.extend(value);
                non_mtu.extend([kind, 2]);
                non_mtu.extend(value);
            }
            assert_eq!(rewrite(true, &wire).unwrap().options, non_mtu);
            assert_eq!(rewrite(false, &wire).unwrap().options, wire);
            let summaries: Vec<_> = (0..count)
                .map(|i| {
                    let kind = if i % 3 == 0 {
                        Some(0x17)
                    } else if i % 3 == 1 {
                        Some(0x2b)
                    } else {
                        None
                    };
                    (kind, i * 7)
                })
                .collect();
            let shape = post_ack_shape(&summaries);
            assert_eq!(shape.considered, count);
            assert_eq!(
                shape.maximum,
                summaries.iter().map(|(_, length)| *length).max()
            );
            assert_eq!(
                shape.type_17_lengths,
                summaries
                    .iter()
                    .filter(|(kind, _)| *kind == Some(0x17))
                    .take(8)
                    .map(|(_, length)| *length)
                    .collect::<Vec<_>>()
            );
            assert_eq!(
                shape.first_type_2b_length,
                summaries
                    .iter()
                    .find(|(kind, _)| *kind == Some(0x2b))
                    .map(|(_, length)| *length)
            );
        }
    }
}
