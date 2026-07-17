//! Lifecycle commits and failure decisions for the private daemon.

pub use airpods_hub_core::DaemonState;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Event {
    Start,
    StartupFailed,
    OpenedWithoutSubscribers,
    BeginHeartRate,
    HeartRateStarted,
    BeginStop,
    HeartRateStopped,
    BeginRecovery,
    RecoveryRetry,
    TerminalFailure,
    BeginShutdown,
    CleanupSucceeded,
    CleanupFailed,
}

impl Event {
    pub fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "start" => Self::Start,
            "startup_failed" => Self::StartupFailed,
            "opened_without_subscribers" => Self::OpenedWithoutSubscribers,
            "begin_heart_rate" => Self::BeginHeartRate,
            "heart_rate_started" => Self::HeartRateStarted,
            "begin_stop" => Self::BeginStop,
            "heart_rate_stopped" => Self::HeartRateStopped,
            "begin_recovery" => Self::BeginRecovery,
            "recovery_retry" => Self::RecoveryRetry,
            "terminal_failure" => Self::TerminalFailure,
            "begin_shutdown" => Self::BeginShutdown,
            "cleanup_succeeded" => Self::CleanupSucceeded,
            "cleanup_failed" => Self::CleanupFailed,
            _ => return None,
        })
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TransitionError {
    SingleUse,
    CannotStartFrom(DaemonState),
    IllegalState,
}

/// A state is committed only when Python calls this at the corresponding effect boundary.
pub fn transition(
    state: DaemonState,
    event: Event,
    start_attempted: bool,
    shutdown_requested: bool,
) -> Result<DaemonState, TransitionError> {
    use DaemonState as S;
    use Event as E;
    if event == E::Start {
        if state != S::Stopped {
            return Err(TransitionError::CannotStartFrom(state));
        }
        if start_attempted {
            return Err(TransitionError::SingleUse);
        }
    }
    // A successful in-flight effect may commit while shutdown waits for Python's
    // lifecycle lock. New recovery work and retry bookkeeping are suppressed.
    if shutdown_requested && matches!(event, E::BeginRecovery | E::RecoveryRetry) {
        return Err(TransitionError::IllegalState);
    }
    match (state, event) {
        (S::Stopped, E::Start) => Ok(S::Starting),
        (S::Starting, E::OpenedWithoutSubscribers) => Ok(S::Ready),
        (S::Starting | S::Ready | S::Streaming, E::BeginHeartRate) => Ok(S::StartingHeartRate),
        (S::StartingHeartRate, E::HeartRateStarted) => Ok(S::Streaming),
        (S::Streaming, E::BeginStop) => Ok(S::StoppingHeartRate),
        (S::StoppingHeartRate, E::HeartRateStopped) => Ok(S::Ready),
        (S::StartingHeartRate | S::Streaming | S::StoppingHeartRate, E::BeginRecovery) => {
            Ok(S::Starting)
        }
        (S::Starting | S::StartingHeartRate, E::RecoveryRetry) => Ok(S::Starting),
        (
            S::Starting | S::StartingHeartRate | S::Streaming | S::StoppingHeartRate,
            E::TerminalFailure,
        )
        | (_, E::StartupFailed)
        | (S::ShuttingDown, E::CleanupFailed) => Ok(S::Failed),
        (
            S::Starting
            | S::Ready
            | S::StartingHeartRate
            | S::Streaming
            | S::StoppingHeartRate
            | S::Failed,
            E::BeginShutdown,
        ) => Ok(S::ShuttingDown),
        (S::ShuttingDown, E::CleanupSucceeded) => Ok(S::Stopped),
        _ => Err(TransitionError::IllegalState),
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FailureAction {
    Ignore,
    Recover,
    Terminal,
}

/// Reader errors outside streaming are stale; shutdown always suppresses new recovery.
pub fn reader_failure(
    state: DaemonState,
    shutdown_requested: bool,
    recoverable: bool,
) -> FailureAction {
    if state != DaemonState::Streaming || shutdown_requested {
        FailureAction::Ignore
    } else if recoverable {
        FailureAction::Recover
    } else {
        FailureAction::Terminal
    }
}

/// A new recovery task is allowed only once for the current loss episode.
pub fn begin_recovery(shutdown_requested: bool, recovery_active: bool) -> bool {
    !shutdown_requested && !recovery_active
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ClientEvent {
    Shutdown,
    TerminalFailure,
    OperationFailure,
}

impl ClientEvent {
    pub fn parse(value: &str) -> Option<Self> {
        match value {
            "shutdown" => Some(Self::Shutdown),
            "terminal_failure" => Some(Self::TerminalFailure),
            "operation_failure" => Some(Self::OperationFailure),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Notification {
    None,
    All,
    OtherClients,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ClientPlan {
    pub clear_subscriptions: bool,
    pub notification: Notification,
}

pub const fn client_plan(event: ClientEvent) -> ClientPlan {
    match event {
        ClientEvent::Shutdown => ClientPlan {
            clear_subscriptions: true,
            notification: Notification::None,
        },
        ClientEvent::TerminalFailure => ClientPlan {
            clear_subscriptions: true,
            notification: Notification::All,
        },
        ClientEvent::OperationFailure => ClientPlan {
            clear_subscriptions: false,
            notification: Notification::OtherClients,
        },
    }
}

pub fn validate_constructor(
    operation_timeout: f64,
    recovery_delays: &[f64],
) -> Result<(), &'static str> {
    // Preserve the existing operation-timeout contract: NaN passed the old <= 0 check.
    if operation_timeout <= 0.0 {
        return Err("operation_timeout must be positive");
    }
    if recovery_delays.is_empty()
        || recovery_delays
            .iter()
            .any(|delay| *delay <= 0.0 || !delay.is_finite())
    {
        return Err("recovery delays must be finite and positive");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transitions_are_closed_and_commit_at_effect_boundaries() {
        use DaemonState as S;
        use Event as E;
        let allowed = [
            (S::Stopped, E::Start, S::Starting),
            (S::Starting, E::OpenedWithoutSubscribers, S::Ready),
            (S::Starting, E::BeginHeartRate, S::StartingHeartRate),
            (S::Ready, E::BeginHeartRate, S::StartingHeartRate),
            (S::Streaming, E::BeginHeartRate, S::StartingHeartRate),
            (S::StartingHeartRate, E::HeartRateStarted, S::Streaming),
            (S::Streaming, E::BeginStop, S::StoppingHeartRate),
            (S::StoppingHeartRate, E::HeartRateStopped, S::Ready),
            (S::StartingHeartRate, E::BeginRecovery, S::Starting),
            (S::Streaming, E::BeginRecovery, S::Starting),
            (S::StoppingHeartRate, E::BeginRecovery, S::Starting),
            (S::Starting, E::RecoveryRetry, S::Starting),
            (S::StartingHeartRate, E::RecoveryRetry, S::Starting),
            (S::Starting, E::TerminalFailure, S::Failed),
            (S::StartingHeartRate, E::TerminalFailure, S::Failed),
            (S::Streaming, E::TerminalFailure, S::Failed),
            (S::StoppingHeartRate, E::TerminalFailure, S::Failed),
            (S::Starting, E::StartupFailed, S::Failed),
            (S::Stopped, E::StartupFailed, S::Failed),
            (S::StartingHeartRate, E::StartupFailed, S::Failed),
            (S::Streaming, E::StartupFailed, S::Failed),
            (S::Ready, E::StartupFailed, S::Failed),
            (S::StoppingHeartRate, E::StartupFailed, S::Failed),
            (S::Failed, E::StartupFailed, S::Failed),
            (S::ShuttingDown, E::StartupFailed, S::Failed),
            (S::ShuttingDown, E::CleanupSucceeded, S::Stopped),
            (S::ShuttingDown, E::CleanupFailed, S::Failed),
        ];
        let events = [
            E::Start,
            E::StartupFailed,
            E::OpenedWithoutSubscribers,
            E::BeginHeartRate,
            E::HeartRateStarted,
            E::BeginStop,
            E::HeartRateStopped,
            E::BeginRecovery,
            E::RecoveryRetry,
            E::TerminalFailure,
            E::BeginShutdown,
            E::CleanupSucceeded,
            E::CleanupFailed,
        ];
        for state in S::ALL {
            for event in events {
                let expected = allowed
                    .iter()
                    .find(|(s, e, _)| *s == state && *e == event)
                    .map(|(_, _, next)| *next)
                    .or_else(|| {
                        (event == E::BeginShutdown
                            && !matches!(state, S::Stopped | S::ShuttingDown))
                        .then_some(S::ShuttingDown)
                    });
                assert_eq!(
                    transition(state, event, false, false).ok(),
                    expected,
                    "{state:?} {event:?}"
                );
            }
        }
        assert_eq!(
            transition(S::Stopped, E::Start, true, false),
            Err(TransitionError::SingleUse)
        );
        assert_eq!(
            transition(S::Failed, E::Start, true, false),
            Err(TransitionError::CannotStartFrom(S::Failed))
        );
        assert_eq!(
            transition(S::Starting, E::OpenedWithoutSubscribers, false, true),
            Ok(S::Ready)
        );
        assert_eq!(
            transition(S::StartingHeartRate, E::HeartRateStarted, false, true),
            Ok(S::Streaming)
        );
        assert_eq!(
            transition(S::StoppingHeartRate, E::HeartRateStopped, false, true),
            Ok(S::Ready)
        );
        assert_eq!(
            transition(S::Streaming, E::BeginRecovery, false, true),
            Err(TransitionError::IllegalState)
        );
    }

    #[test]
    fn recovery_retry_has_a_closed_boundary_before_the_next_restore() {
        use DaemonState as S;
        use Event as E;
        assert_eq!(E::parse("recovery_retry"), Some(E::RecoveryRetry));
        assert_eq!(E::parse("unknown_recovery_retry"), None);
        assert_eq!(
            transition(S::Starting, E::RecoveryRetry, true, false),
            Ok(S::Starting)
        );
        assert_eq!(
            transition(S::StartingHeartRate, E::RecoveryRetry, true, false),
            Ok(S::Starting)
        );
        for state in S::ALL {
            let expected = if matches!(state, S::Starting | S::StartingHeartRate) {
                Ok(S::Starting)
            } else {
                Err(TransitionError::IllegalState)
            };
            assert_eq!(transition(state, E::RecoveryRetry, true, false), expected);
            assert_eq!(
                transition(state, E::RecoveryRetry, true, true),
                Err(TransitionError::IllegalState)
            );
        }
        assert_eq!(
            transition(S::StartingHeartRate, E::BeginHeartRate, true, false),
            Err(TransitionError::IllegalState)
        );
        assert_eq!(
            transition(
                S::StartingHeartRate,
                E::OpenedWithoutSubscribers,
                true,
                false
            ),
            Err(TransitionError::IllegalState)
        );
        let starting = transition(S::StartingHeartRate, E::RecoveryRetry, true, false).unwrap();
        let starting_hr = transition(starting, E::BeginHeartRate, true, false).unwrap();
        assert_eq!(
            transition(starting_hr, E::HeartRateStarted, true, false),
            Ok(S::Streaming)
        );
        assert_eq!(
            transition(starting, E::OpenedWithoutSubscribers, true, false),
            Ok(S::Ready)
        );
    }

    #[test]
    fn reader_failure_and_recovery_precedence() {
        for state in DaemonState::ALL {
            for recoverable in [false, true] {
                for shutdown in [false, true] {
                    let expected = if state != DaemonState::Streaming || shutdown {
                        FailureAction::Ignore
                    } else if recoverable {
                        FailureAction::Recover
                    } else {
                        FailureAction::Terminal
                    };
                    assert_eq!(reader_failure(state, shutdown, recoverable), expected);
                }
            }
        }
        assert!(begin_recovery(false, false));
        assert!(!begin_recovery(true, false));
        assert!(!begin_recovery(false, true));
    }

    #[test]
    fn client_dispositions_preserve_subscription_and_broadcast_boundaries() {
        for (name, clear, notification) in [
            ("shutdown", true, Notification::None),
            ("terminal_failure", true, Notification::All),
            ("operation_failure", false, Notification::OtherClients),
        ] {
            let plan = client_plan(ClientEvent::parse(name).unwrap());
            assert_eq!(plan.clear_subscriptions, clear);
            assert_eq!(plan.notification, notification);
        }
        assert_eq!(ClientEvent::parse("unknown"), None);
    }

    #[test]
    fn constructor_edges() {
        assert!(validate_constructor(10.0, &[1.0, 2.0]).is_ok());
        for value in [0.0, -1.0, f64::NEG_INFINITY] {
            assert!(validate_constructor(value, &[1.0]).is_err());
        }
        for value in [0.0, -1.0, f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            assert!(validate_constructor(1.0, &[value]).is_err());
        }
        assert!(validate_constructor(1.0, &[]).is_err());
        assert!(validate_constructor(f64::NAN, &[1.0]).is_ok());
    }
}
