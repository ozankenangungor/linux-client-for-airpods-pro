//! Signal and output policy; Python owns tasks, events, callbacks and recorders.

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SignalAction {
    Noop,
    GracefulStop,
    Cancel,
}

pub fn signal_action(
    active: bool,
    shutdown_requested: bool,
    start_acknowledged: bool,
) -> SignalAction {
    if !active {
        SignalAction::Noop
    } else if shutdown_requested || !start_acknowledged {
        SignalAction::Cancel
    } else {
        SignalAction::GracefulStop
    }
}

pub fn signal_exit_code(signum: Option<i32>) -> Result<Option<i32>, &'static str> {
    match signum {
        None => Ok(None),
        Some(2) => Ok(Some(130)),
        Some(15) => Ok(Some(143)),
        _ => Err("unsupported signal"),
    }
}

pub const fn termination_reason(exit_code: i32) -> &'static str {
    match exit_code {
        0 => "completed",
        130 => "sigint",
        143 => "sigterm",
        _ => "failure",
    }
}

pub fn exception_reason(category: &str) -> Option<&'static str> {
    match category {
        "cancelled" => Some("cancelled"),
        "diagnostic" => Some("diagnostic_error"),
        "other" => Some("failure"),
        _ => None,
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ProgressRoute {
    StartStatus,
    SimpleSample,
    DiagnosticSample,
    Ignore,
}

pub fn progress_route(event: &str, has_report: bool, diagnostic: bool) -> Option<ProgressRoute> {
    Some(match event {
        "start_acknowledged" => ProgressRoute::StartStatus,
        "sample" if has_report && diagnostic => ProgressRoute::DiagnosticSample,
        "sample" if has_report => ProgressRoute::SimpleSample,
        "sample" => ProgressRoute::Ignore,
        "bootstrap_complete"
        | "stop_head_acknowledged"
        | "control_channels_ready"
        | "stop_acknowledged"
        | "stop_ack_missing"
        | "hr_off_sent" => ProgressRoute::Ignore,
        _ => return None,
    })
}

pub fn completion_message(
    bluez_restored: bool,
    shutdown_requested: bool,
    start_acknowledged: bool,
) -> Option<&'static str> {
    if bluez_restored {
        Some("Bluetooth ownership and BlueZ state restored.")
    } else if shutdown_requested && !start_acknowledged {
        Some("Shutdown completed before heart-rate monitoring started.")
    } else {
        None
    }
}

pub fn command_error(category: &str) -> Option<&'static str> {
    Some(match category {
        "diagnostic_open" => "Error: the diagnostic output file could not be opened.",
        "diagnostic" => "Error: diagnostic capture failed; cleanup was attempted.",
        "no_candidates" => "Error: no paired AirPods candidate was found.",
        "multiple_candidates" => "Error: multiple paired AirPods candidates require selection.",
        "pairing_store" => "Error: existing local Classic credentials could not be loaded.",
        "sdp_compatibility" => "Error: the local SDP compatibility profile could not be prepared.",
        "aap_handshake" => "Error: the AAP handshake or descriptor phase failed.",
        "aap_channel" => "Error: the AAP channel failed.",
        "heart_rate_session" => "Error: the heart-rate session failed; cleanup was attempted.",
        "adapter_restore" => "Error: BlueZ adapter restoration failed.",
        "handoff" => "Error: controller handoff failed; cleanup was attempted.",
        "classic_authentication" => "Error: the Classic security session failed.",
        "unexpected" => "Error: an unexpected monitor failure occurred.",
        _ => return None,
    })
}

pub fn dry_run_lines(diagnostic: bool, output_path: bool) -> Vec<&'static str> {
    let mut lines = vec![
        "DRY RUN: no Bluetooth state will be changed.",
        "Planned operations:",
        "  1. Discover one paired AirPods candidate.",
        "  2. Read its existing local Classic credentials in memory.",
        "  3. Hand the controller from BlueZ to Bumble temporarily.",
        "  4. Connect securely and start continuous heart-rate monitoring.",
        "  5. Stop on SIGINT or SIGTERM and restore BlueZ ownership.",
    ];
    if diagnostic {
        lines.push(if output_path {
            "Diagnostic evidence destination: JSONL file."
        } else {
            "Diagnostic evidence destination: standard output."
        });
    }
    lines.push("No reconnect, retry, or arbitrary protocol command is used.");
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn signal_matrix() {
        for active in [false, true] {
            for requested in [false, true] {
                for started in [false, true] {
                    let expected = if !active {
                        SignalAction::Noop
                    } else if requested || !started {
                        SignalAction::Cancel
                    } else {
                        SignalAction::GracefulStop
                    };
                    assert_eq!(signal_action(active, requested, started), expected);
                }
            }
        }
        assert_eq!(signal_exit_code(None), Ok(None));
        assert_eq!(signal_exit_code(Some(2)), Ok(Some(130)));
        assert_eq!(signal_exit_code(Some(15)), Ok(Some(143)));
        assert!(signal_exit_code(Some(9)).is_err());
    }
    #[test]
    fn reasons_routes_and_completion() {
        for (code, reason) in [
            (0, "completed"),
            (130, "sigint"),
            (143, "sigterm"),
            (1, "failure"),
            (255, "failure"),
        ] {
            assert_eq!(termination_reason(code), reason);
        }
        for (category, reason) in [
            ("cancelled", "cancelled"),
            ("diagnostic", "diagnostic_error"),
            ("other", "failure"),
        ] {
            assert_eq!(exception_reason(category), Some(reason));
        }
        assert_eq!(exception_reason("secret"), None);
        assert_eq!(
            progress_route("start_acknowledged", false, false),
            Some(ProgressRoute::StartStatus)
        );
        assert_eq!(
            progress_route("sample", true, false),
            Some(ProgressRoute::SimpleSample)
        );
        assert_eq!(
            progress_route("sample", true, true),
            Some(ProgressRoute::DiagnosticSample)
        );
        assert_eq!(
            progress_route("sample", false, true),
            Some(ProgressRoute::Ignore)
        );
        assert_eq!(progress_route("unknown", true, false), None);
        assert_eq!(
            completion_message(true, true, false),
            Some("Bluetooth ownership and BlueZ state restored.")
        );
        assert_eq!(
            completion_message(false, true, false),
            Some("Shutdown completed before heart-rate monitoring started.")
        );
        assert_eq!(completion_message(false, true, true), None);
        let cases = [
            (
                "diagnostic_open",
                "Error: the diagnostic output file could not be opened.",
            ),
            (
                "diagnostic",
                "Error: diagnostic capture failed; cleanup was attempted.",
            ),
            (
                "no_candidates",
                "Error: no paired AirPods candidate was found.",
            ),
            (
                "multiple_candidates",
                "Error: multiple paired AirPods candidates require selection.",
            ),
            (
                "pairing_store",
                "Error: existing local Classic credentials could not be loaded.",
            ),
            (
                "sdp_compatibility",
                "Error: the local SDP compatibility profile could not be prepared.",
            ),
            (
                "aap_handshake",
                "Error: the AAP handshake or descriptor phase failed.",
            ),
            ("aap_channel", "Error: the AAP channel failed."),
            (
                "heart_rate_session",
                "Error: the heart-rate session failed; cleanup was attempted.",
            ),
            (
                "adapter_restore",
                "Error: BlueZ adapter restoration failed.",
            ),
            (
                "handoff",
                "Error: controller handoff failed; cleanup was attempted.",
            ),
            (
                "classic_authentication",
                "Error: the Classic security session failed.",
            ),
            (
                "unexpected",
                "Error: an unexpected monitor failure occurred.",
            ),
        ];
        for (category, message) in cases {
            assert_eq!(command_error(category), Some(message));
        }
        assert_eq!(command_error("private details"), None);
        assert_eq!(dry_run_lines(false, false).len(), 8);
        assert_eq!(
            dry_run_lines(true, false)[7],
            "Diagnostic evidence destination: standard output."
        );
        assert_eq!(
            dry_run_lines(true, true)[7],
            "Diagnostic evidence destination: JSONL file."
        );
    }
}
