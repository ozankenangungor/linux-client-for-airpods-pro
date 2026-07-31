//! Safe daemon runner names and exit decisions over stable categories.

pub const EXIT_SUCCESS: i32 = 0;
pub const EXIT_FAILURE: i32 = 1;
pub const EXIT_CONFIGURATION: i32 = 2;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Failure {
    AlreadyRunning,
    UnsafeSocketPath,
    InvalidConfiguration,
    ProductionSession,
    SessionOperation,
    HubDaemon,
    Unexpected,
}

impl Failure {
    pub fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "already_running" => Self::AlreadyRunning,
            "unsafe_socket_path" => Self::UnsafeSocketPath,
            "invalid_configuration" => Self::InvalidConfiguration,
            "production_session" => Self::ProductionSession,
            "session_operation" => Self::SessionOperation,
            "hub_daemon" => Self::HubDaemon,
            "unexpected" => Self::Unexpected,
            _ => return None,
        })
    }

    pub const fn safe_name(self) -> &'static str {
        match self {
            Self::AlreadyRunning => "already_running",
            Self::UnsafeSocketPath => "unsafe_socket_path",
            Self::InvalidConfiguration => "invalid_configuration",
            Self::ProductionSession => "production_session_failed",
            Self::SessionOperation => "SessionOperationError",
            Self::HubDaemon => "HubDaemonError",
            Self::Unexpected => "unexpected_failure",
        }
    }
}

pub fn production_category(value: &str) -> Option<&'static str> {
    Some(match value {
        "invalid_state" => "invalid_state",
        "preflight_failed" => "preflight_failed",
        "registration_failed" => "registration_failed",
        "transport_failed" => "transport_failed",
        "aap_ack_timeout" => "aap_ack_timeout",
        "aap_descriptor_timeout" => "aap_descriptor_timeout",
        "descriptor_handshake_failed" => "descriptor_handshake_failed",
        "activation_failed" => "activation_failed",
        "receive_failed" => "receive_failed",
        "stop_failed" => "stop_failed",
        "cleanup_failed" => "cleanup_failed",
        _ => return None,
    })
}

pub fn signal_exit_code(signum: Option<i32>) -> Result<i32, &'static str> {
    match signum {
        None => Ok(EXIT_SUCCESS),
        Some(2) => Ok(130),
        Some(15) => Ok(143),
        _ => Err("unsupported signal"),
    }
}

pub fn outcome(
    failure: Option<Failure>,
    signal: Option<i32>,
    cleanup_complete: bool,
) -> Result<i32, &'static str> {
    if !cleanup_complete {
        return Ok(EXIT_FAILURE);
    }
    match failure {
        Some(Failure::InvalidConfiguration) => Ok(EXIT_CONFIGURATION),
        Some(_) => Ok(EXIT_FAILURE),
        None => signal_exit_code(signal),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn all_outcomes_and_names() {
        assert_eq!(signal_exit_code(None), Ok(0));
        assert_eq!(signal_exit_code(Some(2)), Ok(130));
        assert_eq!(signal_exit_code(Some(15)), Ok(143));
        assert!(signal_exit_code(Some(9)).is_err());
        for (name, expected) in [
            ("already_running", "already_running"),
            ("unsafe_socket_path", "unsafe_socket_path"),
            ("invalid_configuration", "invalid_configuration"),
            ("production_session", "production_session_failed"),
            ("session_operation", "SessionOperationError"),
            ("hub_daemon", "HubDaemonError"),
            ("unexpected", "unexpected_failure"),
        ] {
            let category = Failure::parse(name).unwrap();
            assert_eq!(category.safe_name(), expected);
            assert_eq!(
                outcome(Some(category), Some(2), true).unwrap(),
                if category == Failure::InvalidConfiguration {
                    2
                } else {
                    1
                }
            );
        }
        assert_eq!(outcome(None, Some(15), false), Ok(1));
        assert!(Failure::parse("private path").is_none());
        for name in [
            "invalid_state",
            "preflight_failed",
            "registration_failed",
            "transport_failed",
            "aap_ack_timeout",
            "aap_descriptor_timeout",
            "descriptor_handshake_failed",
            "activation_failed",
            "receive_failed",
            "stop_failed",
            "cleanup_failed",
        ] {
            assert_eq!(production_category(name), Some(name));
        }
        assert_eq!(production_category("private name"), None);
    }
}
