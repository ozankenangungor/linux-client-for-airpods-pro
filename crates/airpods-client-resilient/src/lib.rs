#![forbid(unsafe_code)]
//! Opt-in finite reconnect and resubscribe for a running local daemon.

use airpods_client::{AirPodsClient, HeartRateSample, HeartRateSubscription};
use std::fmt;
use std::io;
use std::path::PathBuf;
use std::time::Duration;
use tokio::time::{Instant, sleep_until};

/// A finite, ordered list of retry delays. Each entry authorizes one retry.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReconnectPolicy {
    delays: Vec<Duration>,
}

/// Invalid retry policy.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PolicyError {
    Empty,
    ZeroDelay,
    TooManyAttempts,
}

impl fmt::Display for PolicyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Empty => f.write_str("retry schedule must not be empty"),
            Self::ZeroDelay => f.write_str("retry delays must be positive"),
            Self::TooManyAttempts => f.write_str("retry schedule exceeds 16 attempts"),
        }
    }
}
impl std::error::Error for PolicyError {}

impl ReconnectPolicy {
    pub const MAX_ATTEMPTS: usize = 16;

    pub fn new(delays: Vec<Duration>) -> Result<Self, PolicyError> {
        if delays.is_empty() {
            return Err(PolicyError::Empty);
        }
        if delays.len() > Self::MAX_ATTEMPTS {
            return Err(PolicyError::TooManyAttempts);
        }
        if delays.contains(&Duration::ZERO) {
            return Err(PolicyError::ZeroDelay);
        }
        Ok(Self { delays })
    }

    pub fn delays(&self) -> &[Duration] {
        &self.delays
    }
}

impl Default for ReconnectPolicy {
    fn default() -> Self {
        Self {
            delays: [1, 2, 5, 10, 10].map(Duration::from_secs).to_vec(),
        }
    }
}

/// A sample or an observable gap in daemon connectivity.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ResilientHeartRateEvent {
    Sample(HeartRateSample),
    Reconnecting { attempt: usize, delay: Duration },
    Reconnected { attempts: usize },
}

/// An underlying typed failure, or an exhausted finite retry episode.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Error {
    Client(airpods_client::Error),
    RetryExhausted {
        attempts: usize,
        last_error: airpods_client::Error,
    },
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Client(error) => write!(f, "{error}"),
            Self::RetryExhausted {
                attempts,
                last_error,
            } => write!(
                f,
                "daemon reconnect exhausted after {attempts} {}: {last_error}",
                if *attempts == 1 {
                    "attempt"
                } else {
                    "attempts"
                }
            ),
        }
    }
}
impl std::error::Error for Error {}

/// Only explicitly known transport failures authorize reconnect.
pub fn is_recoverable(error: &airpods_client::Error) -> bool {
    use airpods_client::Error as E;
    use io::ErrorKind as K;
    match error {
        E::ConnectionClosed => true,
        E::Connect { kind, .. } => matches!(
            kind,
            K::NotFound
                | K::ConnectionRefused
                | K::ConnectionReset
                | K::ConnectionAborted
                | K::NotConnected
                | K::TimedOut
        ),
        E::Io { kind, .. } => matches!(
            kind,
            K::ConnectionReset
                | K::ConnectionAborted
                | K::BrokenPipe
                | K::NotConnected
                | K::UnexpectedEof
                | K::TimedOut
        ),
        E::XdgRuntimeDirMissing
        | E::FrameTooLarge { .. }
        | E::InvalidJson { .. }
        | E::ProtocolVersion { .. }
        | E::UnexpectedMessage { .. }
        | E::DaemonError { .. }
        | E::SubscriptionActive
        | E::EventLagged { .. } => false,
        _ => false,
    }
}

enum Target {
    Default,
    Explicit(PathBuf),
}

enum State {
    Initial,
    Backoff {
        attempt: usize,
        deadline: Instant,
    },
    Active {
        _client: AirPodsClient,
        subscription: HeartRateSubscription,
    },
    Terminal,
}

/// A lazy, pull-driven heart-rate stream with bounded daemon reconnect.
pub struct ResilientHeartRateStream {
    target: Target,
    policy: ReconnectPolicy,
    state: State,
}

impl ResilientHeartRateStream {
    pub fn default_socket(policy: ReconnectPolicy) -> Self {
        Self {
            target: Target::Default,
            policy,
            state: State::Initial,
        }
    }

    pub fn explicit_socket(path: impl Into<PathBuf>, policy: ReconnectPolicy) -> Self {
        Self {
            target: Target::Explicit(path.into()),
            policy,
            state: State::Initial,
        }
    }

    async fn establish(
        &self,
    ) -> Result<(AirPodsClient, HeartRateSubscription), airpods_client::Error> {
        let client = match &self.target {
            Target::Default => AirPodsClient::connect().await?,
            Target::Explicit(path) => AirPodsClient::connect_to(path).await?,
        };
        let subscription = client.subscribe_heart_rate().await?;
        Ok((client, subscription))
    }

    fn failed(
        &mut self,
        completed_attempt: usize,
        error: airpods_client::Error,
    ) -> Result<Option<ResilientHeartRateEvent>, Error> {
        if !is_recoverable(&error) {
            self.state = State::Terminal;
            return Err(Error::Client(error));
        }
        if completed_attempt == self.policy.delays.len() {
            self.state = State::Terminal;
            return Err(Error::RetryExhausted {
                attempts: completed_attempt,
                last_error: error,
            });
        }
        let attempt = completed_attempt + 1;
        let delay = self.policy.delays[completed_attempt];
        self.state = State::Backoff {
            attempt,
            deadline: Instant::now() + delay,
        };
        Ok(Some(ResilientHeartRateEvent::Reconnecting {
            attempt,
            delay,
        }))
    }

    /// Read the next sample or lifecycle event. Cancellation preserves the pending retry.
    pub async fn next(&mut self) -> Result<Option<ResilientHeartRateEvent>, Error> {
        loop {
            match &mut self.state {
                State::Terminal => return Ok(None),
                State::Initial => match self.establish().await {
                    Ok((client, subscription)) => {
                        self.state = State::Active {
                            _client: client,
                            subscription,
                        };
                    }
                    Err(error) => return self.failed(0, error),
                },
                State::Backoff { attempt, deadline } => {
                    let attempt = *attempt;
                    sleep_until(*deadline).await;
                    // Keep Backoff committed while connect and subscribe are in flight.
                    match self.establish().await {
                        Ok((client, subscription)) => {
                            self.state = State::Active {
                                _client: client,
                                subscription,
                            };
                            return Ok(Some(ResilientHeartRateEvent::Reconnected {
                                attempts: attempt,
                            }));
                        }
                        Err(error) => return self.failed(attempt, error),
                    }
                }
                State::Active { subscription, .. } => match subscription.next().await {
                    Ok(Some(sample)) => return Ok(Some(ResilientHeartRateEvent::Sample(sample))),
                    Ok(None) => {
                        self.state = State::Terminal;
                        return Ok(None);
                    }
                    Err(error) if is_recoverable(&error) => {
                        self.state = State::Terminal;
                        return self.failed(0, error);
                    }
                    Err(error) => {
                        let state = std::mem::replace(&mut self.state, State::Terminal);
                        if let State::Active { subscription, .. } = state {
                            // The original terminal error remains authoritative.
                            let _ = subscription.unsubscribe().await;
                        }
                        return Err(Error::Client(error));
                    }
                },
            }
        }
    }

    /// Permanently stop. An active subscription awaits confirmed unsubscribe.
    pub async fn close(mut self) -> Result<(), Error> {
        let state = std::mem::replace(&mut self.state, State::Terminal);
        if let State::Active { subscription, .. } = state {
            subscription.unsubscribe().await.map_err(Error::Client)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests;
