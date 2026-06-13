//! Pure state and response policy for the reference pre-authentication probe.

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Mode {
    Proven,
    DelayOnly,
    BluezDiscovery,
}
impl TryFrom<&str> for Mode {
    type Error = ();
    fn try_from(value: &str) -> Result<Self, Self::Error> {
        match value {
            "proven" => Ok(Self::Proven),
            "delay-only" => Ok(Self::DelayOnly),
            "bluez-discovery" => Ok(Self::BluezDiscovery),
            _ => Err(()),
        }
    }
}
impl Mode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Proven => "proven",
            Self::DelayOnly => "delay-only",
            Self::BluezDiscovery => "bluez-discovery",
        }
    }
    pub const fn delay_ms(self) -> u8 {
        if matches!(self, Self::DelayOnly) {
            85
        } else {
            0
        }
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ResultKind {
    NotApplicable,
    Success,
    Timeout,
    Other,
}
impl TryFrom<&str> for ResultKind {
    type Error = ();
    fn try_from(value: &str) -> Result<Self, Self::Error> {
        match value {
            "not-applicable" => Ok(Self::NotApplicable),
            "success" => Ok(Self::Success),
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
            Self::Timeout => "timeout",
            Self::Other => "other",
        }
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FeatureResponse {
    pub result: ResultKind,
    pub mask: Option<u64>,
    pub maximum_page: Option<u8>,
}
pub const fn command_status_pending(status: Option<u64>) -> bool {
    matches!(status, Some(0))
}

pub fn supported_completion(
    status: Option<u64>,
    page: Option<u64>,
    features: Option<u64>,
) -> FeatureResponse {
    match (status, page, features) {
        (Some(0), Some(0), Some(mask)) => FeatureResponse {
            result: ResultKind::Success,
            mask: Some(mask),
            maximum_page: None,
        },
        _ => FeatureResponse {
            result: ResultKind::Other,
            mask: None,
            maximum_page: None,
        },
    }
}
pub fn extended_completion(
    status: Option<u64>,
    page: Option<u64>,
    maximum_page: Option<u64>,
    features: Option<u64>,
) -> FeatureResponse {
    match (status, page, maximum_page, features) {
        (Some(0), Some(1), Some(max @ 1..=255), Some(mask)) => FeatureResponse {
            result: ResultKind::Success,
            mask: Some(mask),
            maximum_page: Some(max as u8),
        },
        _ => FeatureResponse {
            result: ResultKind::Other,
            mask: None,
            maximum_page: None,
        },
    }
}
/// Stores no remote name or address material.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct State {
    pub mode: Mode,
    pub supported_request_sent: bool,
    pub supported_command_accepted: bool,
    pub supported_response_observed: bool,
    pub supported_result: ResultKind,
    pub supported_mask: Option<u64>,
    pub extended_request_sent: bool,
    pub extended_command_accepted: bool,
    pub extended_response_observed: bool,
    pub extended_result: ResultKind,
    pub extended_max_page: Option<u8>,
    pub extended_mask: Option<u64>,
    pub name_request_sent: bool,
    pub name_command_accepted: bool,
    pub name_response_observed: bool,
    pub name_result: ResultKind,
    pub authentication_attempted: bool,
}
impl State {
    pub fn new(mode: Mode) -> Self {
        Self {
            mode,
            supported_request_sent: false,
            supported_command_accepted: false,
            supported_response_observed: false,
            supported_result: ResultKind::NotApplicable,
            supported_mask: None,
            extended_request_sent: false,
            extended_command_accepted: false,
            extended_response_observed: false,
            extended_result: ResultKind::NotApplicable,
            extended_max_page: None,
            extended_mask: None,
            name_request_sent: false,
            name_command_accepted: false,
            name_response_observed: false,
            name_result: ResultKind::NotApplicable,
            authentication_attempted: false,
        }
    }
    pub fn supported_request(&mut self) {
        self.supported_request_sent = true;
        self.supported_result = ResultKind::Other;
    }
    pub fn supported_accepted(&mut self) {
        self.supported_command_accepted = true;
    }
    pub fn supported_response(&mut self, observed: bool, result: ResultKind, mask: Option<u64>) {
        self.supported_response_observed = observed;
        self.supported_result = result;
        self.supported_mask = mask;
    }
    pub fn extended_request(&mut self) {
        self.extended_request_sent = true;
        self.extended_result = ResultKind::Other;
    }
    pub fn extended_accepted(&mut self) {
        self.extended_command_accepted = true;
    }
    pub fn extended_response(
        &mut self,
        observed: bool,
        result: ResultKind,
        maximum_page: Option<u8>,
        mask: Option<u64>,
    ) {
        self.extended_response_observed = observed;
        self.extended_result = result;
        self.extended_max_page = maximum_page;
        self.extended_mask = mask;
    }
    pub fn name_request(&mut self) {
        self.name_request_sent = true;
        self.name_result = ResultKind::Other;
    }
    pub fn name_accepted(&mut self) {
        self.name_command_accepted = true;
    }
    pub fn name_response(&mut self, observed: bool, result: ResultKind) {
        self.name_response_observed = observed;
        self.name_result = result;
    }
    pub fn mark_authentication_attempted(&mut self) {
        self.authentication_attempted = true;
    }
    pub const fn extended_page(&self) -> Option<u8> {
        if self.extended_request_sent {
            Some(1)
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn modes_and_initial_snapshots() {
        for (id, mode, delay) in [
            ("proven", Mode::Proven, 0),
            ("delay-only", Mode::DelayOnly, 85),
            ("bluez-discovery", Mode::BluezDiscovery, 0),
        ] {
            assert_eq!(Mode::try_from(id), Ok(mode));
            assert_eq!(mode.as_str(), id);
            assert_eq!(mode.delay_ms(), delay);
            let s = State::new(mode);
            assert_eq!(s.mode, mode);
            assert!(
                !s.supported_request_sent
                    && !s.supported_command_accepted
                    && !s.supported_response_observed
            );
            assert_eq!(
                (s.supported_result, s.supported_mask),
                (ResultKind::NotApplicable, None)
            );
            assert!(
                !s.extended_request_sent
                    && !s.extended_command_accepted
                    && !s.extended_response_observed
            );
            assert_eq!(
                (
                    s.extended_result,
                    s.extended_max_page,
                    s.extended_mask,
                    s.extended_page()
                ),
                (ResultKind::NotApplicable, None, None, None)
            );
            assert!(!s.name_request_sent && !s.name_command_accepted && !s.name_response_observed);
            assert_eq!(s.name_result, ResultKind::NotApplicable);
            assert!(!s.authentication_attempted);
        }
        for id in ["", "BLUEZ-DISCOVERY", "other", "not-applicable"] {
            assert_eq!(Mode::try_from(id), Err(()));
            assert_eq!(
                ResultKind::try_from(id),
                match id {
                    "not-applicable" => Ok(ResultKind::NotApplicable),
                    "other" => Ok(ResultKind::Other),
                    _ => Err(()),
                }
            );
        }
        for id in ["not-applicable", "success", "timeout", "other"] {
            assert_eq!(ResultKind::try_from(id).unwrap().as_str(), id);
        }
    }

    #[test]
    fn supported_completion_validity() {
        assert!(command_status_pending(Some(0)));
        assert!(!command_status_pending(Some(1)));
        assert!(!command_status_pending(None));
        for mask in [0, 1, u64::MAX] {
            assert_eq!(
                supported_completion(Some(0), Some(0), Some(mask)),
                FeatureResponse {
                    result: ResultKind::Success,
                    mask: Some(mask),
                    maximum_page: None
                }
            );
        }
        for invalid in [
            supported_completion(Some(1), Some(0), Some(4)),
            supported_completion(Some(0), Some(1), Some(4)),
            supported_completion(Some(0), Some(0), None),
            supported_completion(None, Some(0), Some(4)),
        ] {
            assert_eq!(
                invalid,
                FeatureResponse {
                    result: ResultKind::Other,
                    mask: None,
                    maximum_page: None
                }
            );
        }
    }

    #[test]
    fn extended_completion_validity() {
        for max in [1, 255] {
            for mask in [0, u64::MAX] {
                assert_eq!(
                    extended_completion(Some(0), Some(1), Some(max), Some(mask)),
                    FeatureResponse {
                        result: ResultKind::Success,
                        mask: Some(mask),
                        maximum_page: Some(max as u8)
                    }
                );
            }
        }
        for invalid in [
            extended_completion(Some(1), Some(1), Some(1), Some(2)),
            extended_completion(Some(0), Some(0), Some(1), Some(2)),
            extended_completion(Some(0), Some(1), Some(0), Some(2)),
            extended_completion(Some(0), Some(1), Some(256), Some(2)),
            extended_completion(Some(0), Some(1), Some(1), None),
        ] {
            assert_eq!(
                invalid,
                FeatureResponse {
                    result: ResultKind::Other,
                    mask: None,
                    maximum_page: None
                }
            );
        }
    }

    #[test]
    fn full_success_stream_snapshots() {
        let mut s = State::new(Mode::BluezDiscovery);
        s.supported_request();
        assert_eq!(
            (
                s.supported_request_sent,
                s.supported_command_accepted,
                s.supported_result
            ),
            (true, false, ResultKind::Other)
        );
        s.supported_accepted();
        assert!(s.supported_command_accepted);
        s.supported_response(true, ResultKind::Success, Some(u64::MAX));
        assert_eq!(
            (
                s.supported_response_observed,
                s.supported_result,
                s.supported_mask
            ),
            (true, ResultKind::Success, Some(u64::MAX))
        );
        s.extended_request();
        assert_eq!(
            (
                s.extended_request_sent,
                s.extended_page(),
                s.extended_result,
                s.name_request_sent
            ),
            (true, Some(1), ResultKind::Other, false)
        );
        s.extended_accepted();
        assert!(s.extended_command_accepted);
        s.extended_response(true, ResultKind::Success, Some(255), Some(0));
        assert_eq!(
            (
                s.extended_response_observed,
                s.extended_result,
                s.extended_max_page,
                s.extended_mask
            ),
            (true, ResultKind::Success, Some(255), Some(0))
        );
        s.name_request();
        assert_eq!(
            (s.name_request_sent, s.name_result),
            (true, ResultKind::Other)
        );
        s.name_accepted();
        assert!(s.name_command_accepted);
        s.name_response(true, ResultKind::Success);
        assert_eq!(
            (s.name_response_observed, s.name_result),
            (true, ResultKind::Success)
        );
        assert!(!s.authentication_attempted);
        s.mark_authentication_attempted();
        s.mark_authentication_attempted();
        assert!(s.authentication_attempted);
    }

    #[test]
    fn failure_streams_and_slot_isolation() {
        for slot in 0..3 {
            let mut s = State::new(Mode::BluezDiscovery);
            s.supported_request();
            s.supported_accepted();
            if slot == 0 {
                s.supported_response(false, ResultKind::Timeout, None);
                assert_eq!(
                    (s.supported_result, s.extended_result, s.name_result),
                    (
                        ResultKind::Timeout,
                        ResultKind::NotApplicable,
                        ResultKind::NotApplicable
                    )
                );
                continue;
            }
            s.supported_response(true, ResultKind::Success, Some(0));
            s.extended_request();
            s.extended_accepted();
            if slot == 1 {
                s.extended_response(true, ResultKind::Other, None, None);
                assert_eq!(
                    (s.supported_result, s.extended_result, s.name_result),
                    (
                        ResultKind::Success,
                        ResultKind::Other,
                        ResultKind::NotApplicable
                    )
                );
                continue;
            }
            s.extended_response(true, ResultKind::Success, Some(1), Some(0));
            s.name_request();
            s.name_accepted();
            s.name_response(true, ResultKind::Other);
            assert_eq!(
                (s.supported_result, s.extended_result, s.name_result),
                (ResultKind::Success, ResultKind::Success, ResultKind::Other)
            );
            s.name_response(false, ResultKind::Timeout);
            assert_eq!(
                (s.name_response_observed, s.name_result),
                (false, ResultKind::Timeout)
            );
        }
        let mut s = State::new(Mode::Proven);
        let before = s.clone();
        assert_eq!(ResultKind::try_from("bad"), Err(()));
        assert_eq!(Mode::try_from("bad"), Err(()));
        assert_eq!(s, before);
        s.name_request();
        s.name_accepted();
        s.name_response(false, ResultKind::Other);
        assert_eq!(
            (
                s.name_request_sent,
                s.name_command_accepted,
                s.name_response_observed,
                s.name_result
            ),
            (true, true, false, ResultKind::Other)
        );
    }

    #[test]
    fn deterministic_random_completions_never_leak_invalid_masks() {
        let mut seed = 0x1234_5678_9abc_def0u64;
        for _ in 0..4000 {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            let mask = seed;
            assert_eq!(
                supported_completion(Some(0), Some(0), Some(mask)).mask,
                Some(mask)
            );
            assert_eq!(
                extended_completion(Some(0), Some(1), Some(1), Some(mask)).mask,
                Some(mask)
            );
            assert_eq!(
                supported_completion(Some(0), Some(2), Some(mask)).mask,
                None
            );
            assert_eq!(
                extended_completion(Some(0), Some(1), Some(256), Some(mask)).mask,
                None
            );
        }
    }
}
