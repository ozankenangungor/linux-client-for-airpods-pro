//! Pure state and Information Response policy for the reference pre-AAP probe.

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Mode {
    Proven,
    DelayOnly,
    BluezL2capInfo,
}
impl TryFrom<&str> for Mode {
    type Error = ();
    fn try_from(value: &str) -> Result<Self, Self::Error> {
        match value {
            "proven" => Ok(Self::Proven),
            "delay-only" => Ok(Self::DelayOnly),
            "bluez-l2cap-info" => Ok(Self::BluezL2capInfo),
            _ => Err(()),
        }
    }
}
impl Mode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Proven => "proven",
            Self::DelayOnly => "delay-only",
            Self::BluezL2capInfo => "bluez-l2cap-info",
        }
    }
    pub const fn delay_ms(self) -> u8 {
        if matches!(self, Self::DelayOnly) {
            20
        } else {
            0
        }
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ResultKind {
    NotApplicable,
    Success,
    NotSupported,
    Timeout,
    Other,
}
impl TryFrom<&str> for ResultKind {
    type Error = ();
    fn try_from(value: &str) -> Result<Self, Self::Error> {
        match value {
            "not-applicable" => Ok(Self::NotApplicable),
            "success" => Ok(Self::Success),
            "not_supported" => Ok(Self::NotSupported),
            "timeout" => Ok(Self::Timeout),
            "other" => Ok(Self::Other),
            _ => Err(()),
        }
    }
}
impl ResultKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::NotApplicable => "not-applicable",
            Self::Success => "success",
            Self::NotSupported => "not_supported",
            Self::Timeout => "timeout",
            Self::Other => "other",
        }
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InformationType {
    ExtendedFeatures,
    FixedChannels,
}
impl TryFrom<u16> for InformationType {
    type Error = ();
    fn try_from(value: u16) -> Result<Self, Self::Error> {
        match value {
            2 => Ok(Self::ExtendedFeatures),
            3 => Ok(Self::FixedChannels),
            _ => Err(()),
        }
    }
}
pub fn decode_response(
    info_type: InformationType,
    result: Option<u64>,
    data: &[u8],
) -> (ResultKind, Option<u64>) {
    if result == Some(1) {
        return (ResultKind::NotSupported, None);
    }
    if result != Some(0) {
        return (ResultKind::Other, None);
    }
    match info_type {
        InformationType::ExtendedFeatures => match <[u8; 4]>::try_from(data) {
            Ok(bytes) => (ResultKind::Success, Some(u32::from_le_bytes(bytes) as u64)),
            Err(_) => (ResultKind::Other, None),
        },
        InformationType::FixedChannels => match <[u8; 8]>::try_from(data) {
            Ok(bytes) => (ResultKind::Success, Some(u64::from_le_bytes(bytes))),
            Err(_) => (ResultKind::Other, None),
        },
    }
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct State {
    pub mode: Mode,
    pub extended_request_sent: bool,
    pub extended_response_observed: bool,
    pub extended_result: ResultKind,
    pub extended_mask: Option<u64>,
    pub fixed_request_sent: bool,
    pub fixed_response_observed: bool,
    pub fixed_result: ResultKind,
    pub fixed_mask: Option<u64>,
    pub aap_open_attempted: bool,
}
impl State {
    pub fn new(mode: Mode) -> Self {
        Self {
            mode,
            extended_request_sent: false,
            extended_response_observed: false,
            extended_result: ResultKind::NotApplicable,
            extended_mask: None,
            fixed_request_sent: false,
            fixed_response_observed: false,
            fixed_result: ResultKind::NotApplicable,
            fixed_mask: None,
            aap_open_attempted: false,
        }
    }
    pub fn request_sent(&mut self, info_type: InformationType) {
        match info_type {
            InformationType::ExtendedFeatures => {
                self.extended_request_sent = true;
                self.extended_result = ResultKind::Other;
            }
            InformationType::FixedChannels => {
                self.fixed_request_sent = true;
                self.fixed_result = ResultKind::Other;
            }
        }
    }
    pub fn response(
        &mut self,
        info_type: InformationType,
        observed: bool,
        result: ResultKind,
        mask: Option<u64>,
    ) {
        match info_type {
            InformationType::ExtendedFeatures => {
                self.extended_response_observed = observed;
                self.extended_result = result;
                self.extended_mask = mask;
            }
            InformationType::FixedChannels => {
                self.fixed_response_observed = observed;
                self.fixed_result = result;
                self.fixed_mask = mask;
            }
        }
    }
    pub fn mark_aap_open_attempted(&mut self) {
        self.aap_open_attempted = true;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn modes_results_types_and_initial_snapshots() {
        for (id, mode, delay) in [
            ("proven", Mode::Proven, 0),
            ("delay-only", Mode::DelayOnly, 20),
            ("bluez-l2cap-info", Mode::BluezL2capInfo, 0),
        ] {
            assert_eq!(Mode::try_from(id), Ok(mode));
            assert_eq!(mode.as_str(), id);
            assert_eq!(mode.delay_ms(), delay);
            let s = State::new(mode);
            assert_eq!(s.mode, mode);
            assert_eq!(
                (
                    s.extended_request_sent,
                    s.extended_response_observed,
                    s.extended_result,
                    s.extended_mask
                ),
                (false, false, ResultKind::NotApplicable, None)
            );
            assert_eq!(
                (
                    s.fixed_request_sent,
                    s.fixed_response_observed,
                    s.fixed_result,
                    s.fixed_mask
                ),
                (false, false, ResultKind::NotApplicable, None)
            );
            assert!(!s.aap_open_attempted);
        }
        for id in ["", "BLUEZ-L2CAP-INFO", "other"] {
            assert_eq!(Mode::try_from(id), Err(()));
        }
        for id in [
            "not-applicable",
            "success",
            "not_supported",
            "timeout",
            "other",
        ] {
            assert_eq!(ResultKind::try_from(id).unwrap().as_str(), id);
        }
        assert_eq!(ResultKind::try_from("bad"), Err(()));
        assert_eq!(
            InformationType::try_from(2),
            Ok(InformationType::ExtendedFeatures)
        );
        assert_eq!(
            InformationType::try_from(3),
            Ok(InformationType::FixedChannels)
        );
        for id in [0, 1, 4, 65535] {
            assert_eq!(InformationType::try_from(id), Err(()));
        }
    }

    #[test]
    fn extended_decode_vectors_and_lengths() {
        let info = InformationType::ExtendedFeatures;
        assert_eq!(
            decode_response(info, Some(0), &[0x78, 0x56, 0x34, 0x12]),
            (ResultKind::Success, Some(0x1234_5678))
        );
        assert_eq!(
            decode_response(info, Some(0), &[0; 4]),
            (ResultKind::Success, Some(0))
        );
        assert_eq!(
            decode_response(info, Some(0), &[0xff; 4]),
            (ResultKind::Success, Some(u32::MAX as u64))
        );
        for len in [0, 1, 2, 3, 5, 8] {
            assert_eq!(
                decode_response(info, Some(0), &vec![0; len]),
                (ResultKind::Other, None)
            );
        }
        assert_eq!(
            decode_response(info, Some(1), &[0; 4]),
            (ResultKind::NotSupported, None)
        );
        for result in [Some(2), Some(u64::MAX), None] {
            assert_eq!(
                decode_response(info, result, &[0; 4]),
                (ResultKind::Other, None)
            );
        }
    }

    #[test]
    fn fixed_decode_vectors_and_lengths() {
        let info = InformationType::FixedChannels;
        assert_eq!(
            decode_response(info, Some(0), &[8, 7, 6, 5, 4, 3, 2, 1]),
            (ResultKind::Success, Some(0x0102_0304_0506_0708))
        );
        assert_eq!(
            decode_response(info, Some(0), &[0; 8]),
            (ResultKind::Success, Some(0))
        );
        assert_eq!(
            decode_response(info, Some(0), &[0xff; 8]),
            (ResultKind::Success, Some(u64::MAX))
        );
        for len in [0, 1, 4, 7, 9] {
            assert_eq!(
                decode_response(info, Some(0), &vec![0; len]),
                (ResultKind::Other, None)
            );
        }
        assert_eq!(
            decode_response(info, Some(1), &[0; 8]),
            (ResultKind::NotSupported, None)
        );
        for result in [Some(2), Some(u64::MAX), None] {
            assert_eq!(
                decode_response(info, result, &[0; 8]),
                (ResultKind::Other, None)
            );
        }
    }

    #[test]
    fn success_and_failure_stream_snapshots() {
        let mut s = State::new(Mode::BluezL2capInfo);
        s.request_sent(InformationType::ExtendedFeatures);
        assert_eq!(
            (
                s.extended_request_sent,
                s.extended_response_observed,
                s.extended_result,
                s.fixed_request_sent
            ),
            (true, false, ResultKind::Other, false)
        );
        s.response(
            InformationType::ExtendedFeatures,
            true,
            ResultKind::Success,
            Some(u32::MAX as u64),
        );
        assert_eq!(
            (
                s.extended_response_observed,
                s.extended_result,
                s.extended_mask
            ),
            (true, ResultKind::Success, Some(u32::MAX as u64))
        );
        s.request_sent(InformationType::FixedChannels);
        assert_eq!(
            (s.fixed_request_sent, s.fixed_result, s.aap_open_attempted),
            (true, ResultKind::Other, false)
        );
        s.response(
            InformationType::FixedChannels,
            true,
            ResultKind::Success,
            Some(u64::MAX),
        );
        assert_eq!(
            (s.fixed_response_observed, s.fixed_result, s.fixed_mask),
            (true, ResultKind::Success, Some(u64::MAX))
        );
        s.mark_aap_open_attempted();
        s.mark_aap_open_attempted();
        assert!(s.aap_open_attempted);
        for (slot, result, observed) in [
            (
                InformationType::ExtendedFeatures,
                ResultKind::NotSupported,
                true,
            ),
            (InformationType::FixedChannels, ResultKind::Timeout, false),
            (InformationType::ExtendedFeatures, ResultKind::Other, false),
        ] {
            s.response(slot, observed, result, None);
            match slot {
                InformationType::ExtendedFeatures => assert_eq!(
                    (
                        s.extended_response_observed,
                        s.extended_result,
                        s.extended_mask
                    ),
                    (observed, result, None)
                ),
                InformationType::FixedChannels => assert_eq!(
                    (s.fixed_response_observed, s.fixed_result, s.fixed_mask),
                    (observed, result, None)
                ),
            }
        }
    }

    #[test]
    fn deterministic_random_little_endian_and_slot_isolation() {
        let mut seed = 0x0123_4567_89ab_cdefu64;
        let mut s = State::new(Mode::BluezL2capInfo);
        for i in 0..4000 {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            let four = (seed as u32).to_le_bytes();
            let eight = seed.to_le_bytes();
            assert_eq!(
                decode_response(InformationType::ExtendedFeatures, Some(0), &four),
                (ResultKind::Success, Some(seed as u32 as u64))
            );
            assert_eq!(
                decode_response(InformationType::FixedChannels, Some(0), &eight),
                (ResultKind::Success, Some(seed))
            );
            let before = s.clone();
            if i % 2 == 0 {
                s.response(
                    InformationType::ExtendedFeatures,
                    true,
                    ResultKind::Success,
                    Some(seed as u32 as u64),
                );
                assert_eq!(
                    (s.fixed_result, s.fixed_mask, s.fixed_response_observed),
                    (
                        before.fixed_result,
                        before.fixed_mask,
                        before.fixed_response_observed
                    )
                );
            } else {
                s.response(
                    InformationType::FixedChannels,
                    true,
                    ResultKind::Success,
                    Some(seed),
                );
                assert_eq!(
                    (
                        s.extended_result,
                        s.extended_mask,
                        s.extended_response_observed
                    ),
                    (
                        before.extended_result,
                        before.extended_mask,
                        before.extended_response_observed
                    )
                );
            }
            assert_eq!(InformationType::try_from(4), Err(()));
        }
    }
}
