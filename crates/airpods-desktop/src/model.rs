//! Presentation state. Timestamps are local receipt times, never sensor times.

use crate::bootstrap::{Failure, Stage};
use airpods_client::{HeartRateSample, SourceSide};
use std::collections::VecDeque;
use std::time::Duration;

pub const HISTORY_SECONDS: u64 = 120;
pub const HISTORY_CAPACITY: usize = 2048;
pub const ACTIVITY_CAPACITY: usize = 32;
pub const FRESH_FOR: Duration = Duration::from_secs(3);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Problem {
    Bootstrap(Failure),
    SensorUnavailable,
    Unavailable,
    Configuration,
    Permission,
    Protocol,
    Rejected,
    Lagged,
    Worker,
}

impl Problem {
    pub fn title(self) -> &'static str {
        match self {
            Self::Bootstrap(failure) => failure.title(),
            Self::SensorUnavailable => "Waiting for AirPods",
            Self::Unavailable => "airpods-hubd is not available",
            Self::Configuration => "The daemon socket is not configured",
            Self::Permission => "The daemon socket is not accessible",
            Self::Protocol => "The daemon response was not understood",
            Self::Rejected => "The daemon could not start this stream",
            Self::Lagged => "The sample stream fell behind",
            Self::Worker => "The application worker stopped",
        }
    }

    pub fn hint(self) -> &'static str {
        match self {
            Self::Bootstrap(failure) => failure.hint(),
            Self::SensorUnavailable => {
                "Pair AirPods in Linux Bluetooth settings and connect them, then retry."
            }
            Self::Unavailable => "Retry the connection when the daemon is available.",
            Self::Configuration => "Set XDG_RUNTIME_DIR or launch with --socket PATH.",
            Self::Permission => "Run the app as the same user as airpods-hubd.",
            Self::Protocol => "Check that the daemon supports IPC protocol v1, then retry.",
            Self::Rejected => "Check the daemon's status, then retry when it is ready.",
            Self::Lagged => "Some samples were missed. Retry to begin a new stream.",
            Self::Worker => "Close and reopen the application to reconnect.",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum AppEvent {
    Bootstrap(Stage),
    Reset,
    Connecting,
    Connected,
    Sample(HeartRateSample),
    Reconnecting { attempt: usize, delay: Duration },
    Reconnected { attempts: usize },
    Disconnected,
    Failed(Problem),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TimedEvent {
    pub at: Duration,
    pub event: AppEvent,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Connection {
    Preparing(Stage),
    Idle,
    Connecting,
    Connected,
    Streaming,
    Reconnecting { attempt: usize, retry_at: Duration },
    Disconnected,
    Error,
}

impl Connection {
    pub fn has_connection(self) -> bool {
        matches!(self, Self::Connected | Self::Streaming)
    }

    pub fn can_retry(self) -> bool {
        matches!(self, Self::Disconnected | Self::Error)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Freshness {
    Fresh,
    Waiting,
    Unavailable,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SamplePoint {
    pub at: Duration,
    pub sample: HeartRateSample,
    /// Samples in different segments must never be joined by the chart.
    pub segment: u64,
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Statistics {
    pub count: u64,
    sum: u64,
    pub min: Option<u8>,
    pub max: Option<u8>,
}

impl Statistics {
    fn record(&mut self, bpm: u8) {
        self.count = self.count.saturating_add(1);
        self.sum = self.sum.saturating_add(u64::from(bpm));
        self.min = Some(self.min.map_or(bpm, |value| value.min(bpm)));
        self.max = Some(self.max.map_or(bpm, |value| value.max(bpm)));
    }

    pub fn average(self) -> Option<f64> {
        (self.count > 0).then(|| self.sum as f64 / self.count as f64)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ActivityKind {
    Info,
    Success,
    Recovery,
    Error,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Activity {
    pub at: Duration,
    pub kind: ActivityKind,
    pub text: String,
}

#[derive(Debug)]
pub struct Model {
    pub connection: Connection,
    pub problem: Option<Problem>,
    pub history: VecDeque<SamplePoint>,
    pub activity: VecDeque<Activity>,
    pub stats: Statistics,
    pub latest: Option<SamplePoint>,
    pub connection_since: Option<Duration>,
    segment: u64,
    waiting: bool,
    ever_connected: bool,
    stream_started: bool,
}

impl Default for Model {
    fn default() -> Self {
        Self {
            connection: Connection::Idle,
            problem: None,
            history: VecDeque::with_capacity(HISTORY_CAPACITY),
            activity: VecDeque::with_capacity(ACTIVITY_CAPACITY),
            stats: Statistics::default(),
            latest: None,
            connection_since: None,
            segment: 0,
            waiting: false,
            ever_connected: false,
            stream_started: false,
        }
    }
}

impl Model {
    pub fn apply(&mut self, TimedEvent { at, event }: TimedEvent) {
        match event {
            AppEvent::Bootstrap(stage) => {
                self.connection = Connection::Preparing(stage);
                self.problem = None;
                self.log(at, ActivityKind::Info, stage.hint());
            }
            AppEvent::Reset => *self = Self::default(),
            AppEvent::Connecting => {
                self.connection = Connection::Connecting;
                self.problem = None;
                self.log(at, ActivityKind::Info, "Connecting to airpods-hubd");
            }
            AppEvent::Connected => self.connected(at, "Connected to airpods-hubd".into()),
            AppEvent::Sample(sample) => {
                if !self.connection.has_connection() {
                    self.connected(at, "Connected to airpods-hubd".into());
                }
                let stale_gap = self
                    .latest
                    .is_some_and(|last| at.saturating_sub(last.at) > FRESH_FOR);
                if stale_gap && !self.waiting {
                    self.segment += 1;
                }
                if self.connection != Connection::Streaming || self.waiting || stale_gap {
                    let text = if self.stream_started {
                        "Heart-rate stream resumed"
                    } else {
                        "Heart-rate stream started"
                    };
                    self.log(at, ActivityKind::Success, text);
                }
                self.stream_started = true;
                self.waiting = false;
                self.connection = Connection::Streaming;
                let point = SamplePoint {
                    at,
                    sample,
                    segment: self.segment,
                };
                self.latest = Some(point);
                self.stats.record(sample.bpm);
                self.history.push_back(point);
            }
            AppEvent::Reconnecting { attempt, delay } => {
                if !matches!(
                    self.connection,
                    Connection::Reconnecting { .. } | Connection::Disconnected
                ) {
                    let had_connection = self.connection.has_connection();
                    self.gap();
                    if had_connection {
                        self.log(
                            at,
                            ActivityKind::Recovery,
                            "Connection lost · history retained",
                        );
                    }
                }
                self.connection = Connection::Reconnecting {
                    attempt,
                    retry_at: at + delay,
                };
                self.problem = None;
                self.log(
                    at,
                    ActivityKind::Recovery,
                    format!("Reconnecting · attempt {attempt}"),
                );
            }
            AppEvent::Reconnected { attempts } => {
                let verb = if self.ever_connected {
                    "Reconnected"
                } else {
                    "Connected"
                };
                self.connected(
                    at,
                    format!(
                        "{verb} after {attempts} {}",
                        if attempts == 1 { "attempt" } else { "attempts" }
                    ),
                );
            }
            AppEvent::Disconnected => {
                self.gap();
                self.connection = Connection::Disconnected;
                self.log(
                    at,
                    ActivityKind::Recovery,
                    "Connection lost · history retained",
                );
            }
            AppEvent::Failed(problem) => {
                self.gap();
                self.problem = Some(problem);
                self.connection = if matches!(
                    problem,
                    Problem::Unavailable
                        | Problem::SensorUnavailable
                        | Problem::Bootstrap(Failure::AirPodsUnavailable)
                ) {
                    Connection::Disconnected
                } else {
                    Connection::Error
                };
                self.log(
                    at,
                    if problem == Problem::Unavailable {
                        ActivityKind::Recovery
                    } else {
                        ActivityKind::Error
                    },
                    problem.title(),
                );
            }
        }
        self.trim(at);
    }

    fn connected(&mut self, at: Duration, text: String) {
        self.connection = Connection::Connected;
        self.connection_since = Some(at);
        self.problem = None;
        self.ever_connected = true;
        self.log(at, ActivityKind::Success, text);
    }

    fn gap(&mut self) {
        self.segment += 1;
        self.connection_since = None;
        self.waiting = true;
    }

    fn log(&mut self, at: Duration, kind: ActivityKind, text: impl Into<String>) {
        if self.activity.len() == ACTIVITY_CAPACITY {
            self.activity.pop_front();
        }
        self.activity.push_back(Activity {
            at,
            kind,
            text: text.into(),
        });
    }

    pub fn tick(&mut self, now: Duration) {
        if self.connection == Connection::Streaming
            && self.freshness(now) == Freshness::Waiting
            && !self.waiting
        {
            self.waiting = true;
            self.segment += 1;
            self.log(now, ActivityKind::Info, "Waiting for a new sample");
        }
        self.trim(now);
    }

    fn trim(&mut self, now: Duration) {
        let oldest = now.saturating_sub(Duration::from_secs(HISTORY_SECONDS));
        while self.history.len() > HISTORY_CAPACITY
            || self.history.front().is_some_and(|point| point.at < oldest)
        {
            self.history.pop_front();
        }
    }

    pub fn freshness(&self, now: Duration) -> Freshness {
        if !self.connection.has_connection() {
            return Freshness::Unavailable;
        }
        if self
            .latest
            .is_some_and(|point| !self.waiting && now.saturating_sub(point.at) <= FRESH_FOR)
        {
            Freshness::Fresh
        } else {
            Freshness::Waiting
        }
    }

    pub fn uptime(&self, now: Duration) -> Option<Duration> {
        self.connection_since.map(|at| now.saturating_sub(at))
    }
}

pub fn side_label(side: SourceSide) -> &'static str {
    match side {
        SourceSide::Left => "Left AirPod",
        SourceSide::Right => "Right AirPod",
        SourceSide::Unknown(_) => "Unknown source",
    }
}

pub fn clock_time(time: Duration) -> String {
    let seconds = time.as_secs();
    if seconds >= 3600 {
        format!(
            "{:02}:{:02}:{:02}",
            seconds / 3600,
            seconds / 60 % 60,
            seconds % 60
        )
    } else {
        format!("{:02}:{:02}", seconds / 60, seconds % 60)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn send(model: &mut Model, seconds: u64, event: AppEvent) {
        model.apply(TimedEvent {
            at: Duration::from_secs(seconds),
            event,
        });
    }

    fn sample(bpm: u8) -> AppEvent {
        AppEvent::Sample(HeartRateSample {
            bpm,
            source_side: SourceSide::Unknown(37),
        })
    }

    #[test]
    fn exact_values_order_duplicates_and_unknown_side_survive() {
        let mut model = Model::default();
        for bpm in [169, 88, 88, 0, 255] {
            send(&mut model, 1, sample(bpm));
        }
        assert_eq!(
            model
                .history
                .iter()
                .map(|point| point.sample.bpm)
                .collect::<Vec<_>>(),
            [169, 88, 88, 0, 255]
        );
        assert!(
            model
                .history
                .iter()
                .all(|point| point.sample.source_side == SourceSide::Unknown(37))
        );
        assert_eq!(model.stats.count, 5);
        assert_eq!(model.stats.average(), Some(120.0));
        assert_eq!(model.stats.min, Some(0));
        assert_eq!(model.stats.max, Some(255));
    }

    #[test]
    fn reconnect_keeps_session_statistics_and_breaks_the_chart() {
        let mut model = Model::default();
        send(&mut model, 1, sample(90));
        send(
            &mut model,
            2,
            AppEvent::Reconnecting {
                attempt: 1,
                delay: Duration::from_secs(1),
            },
        );
        assert_eq!(
            model.freshness(Duration::from_secs(2)),
            Freshness::Unavailable
        );
        assert_eq!(model.uptime(Duration::from_secs(2)), None);
        send(&mut model, 3, AppEvent::Reconnected { attempts: 1 });
        assert_eq!(model.freshness(Duration::from_secs(3)), Freshness::Waiting);
        send(&mut model, 3, sample(100));
        assert_ne!(model.history[0].segment, model.history[1].segment);
        assert_eq!(model.stats.average(), Some(95.0));
        assert_eq!(
            model.uptime(Duration::from_secs(5)),
            Some(Duration::from_secs(2))
        );
        send(&mut model, 6, AppEvent::Failed(Problem::Unavailable));
        send(&mut model, 7, AppEvent::Connecting);
        send(
            &mut model,
            7,
            AppEvent::Reconnecting {
                attempt: 1,
                delay: Duration::from_secs(1),
            },
        );
        assert_eq!(
            model
                .activity
                .iter()
                .filter(|event| event.text.starts_with("Connection lost"))
                .count(),
            1
        );
        assert_eq!(model.stats.count, 2);
    }

    #[test]
    fn freshness_expires_once_and_resumption_creates_a_gap() {
        let mut model = Model::default();
        send(&mut model, 1, sample(90));
        assert_eq!(model.freshness(Duration::from_secs(4)), Freshness::Fresh);
        model.tick(Duration::from_millis(4001));
        assert_eq!(
            model.freshness(Duration::from_millis(4001)),
            Freshness::Waiting
        );
        let activities = model.activity.len();
        model.tick(Duration::from_secs(6));
        assert_eq!(model.activity.len(), activities);
        send(&mut model, 7, sample(91));
        assert_ne!(model.history[0].segment, model.history[1].segment);
        assert_eq!(model.freshness(Duration::from_secs(7)), Freshness::Fresh);
    }

    #[test]
    fn missing_render_ticks_still_break_a_sample_gap() {
        let mut model = Model::default();
        send(&mut model, 1, sample(90));
        send(&mut model, 10, sample(91));
        assert_ne!(model.history[0].segment, model.history[1].segment);
    }

    #[test]
    fn history_is_bounded_by_count_and_duration_without_losing_session_stats() {
        let mut model = Model::default();
        for _ in 0..HISTORY_CAPACITY + 20 {
            send(&mut model, 0, sample(88));
        }
        assert_eq!(model.history.len(), HISTORY_CAPACITY);
        assert_eq!(model.stats.count, (HISTORY_CAPACITY + 20) as u64);
        send(&mut model, HISTORY_SECONDS, sample(90));
        assert_eq!(model.history.len(), HISTORY_CAPACITY);
        model.tick(Duration::from_secs(HISTORY_SECONDS + 1));
        assert_eq!(model.history.len(), 1);
        model.tick(Duration::from_secs(2 * HISTORY_SECONDS + 1));
        assert!(model.history.is_empty());
        assert_eq!(model.latest.unwrap().sample.bpm, 90);
        assert_eq!(model.stats.min, Some(88));
    }

    #[test]
    fn activity_is_bounded_and_terminal_errors_are_actionable() {
        let mut model = Model::default();
        for attempt in 1..100 {
            send(
                &mut model,
                attempt,
                AppEvent::Reconnecting {
                    attempt: attempt as usize,
                    delay: Duration::from_secs(1),
                },
            );
        }
        assert_eq!(model.activity.len(), ACTIVITY_CAPACITY);
        send(&mut model, 100, AppEvent::Failed(Problem::Unavailable));
        assert_eq!(model.connection, Connection::Disconnected);
        assert!(model.connection.can_retry());
        assert_eq!(model.problem, Some(Problem::Unavailable));
        send(&mut model, 101, AppEvent::Connecting);
        assert_eq!(model.problem, None);
        assert!(!model.connection.can_retry());
        send(&mut model, 102, AppEvent::Failed(Problem::Protocol));
        assert_eq!(model.connection, Connection::Error);
    }

    #[test]
    fn empty_model_and_source_labels_are_honest() {
        let model = Model::default();
        assert_eq!(model.freshness(Duration::ZERO), Freshness::Unavailable);
        assert_eq!(model.stats.average(), None);
        assert_eq!(side_label(SourceSide::Unknown(37)), "Unknown source");
        assert_eq!(side_label(SourceSide::Left), "Left AirPod");
        assert_eq!(side_label(SourceSide::Right), "Right AirPod");
        assert_eq!(clock_time(Duration::from_secs(7384)), "02:03:04");
    }

    #[test]
    fn bootstrap_progress_failure_retry_and_sensor_state_remain_distinct() {
        let mut model = Model::default();
        for (at, stage) in [
            Stage::CheckingDaemon,
            Stage::InstallingDaemon,
            Stage::StartingDaemon,
            Stage::Ready,
        ]
        .into_iter()
        .enumerate()
        {
            send(&mut model, at as u64, AppEvent::Bootstrap(stage));
            assert_eq!(model.connection, Connection::Preparing(stage));
            assert_eq!(
                model.freshness(Duration::from_secs(at as u64)),
                Freshness::Unavailable
            );
            assert!(!model.connection.can_retry());
            assert_eq!(model.stats.count, 0);
        }
        send(
            &mut model,
            4,
            AppEvent::Failed(Problem::Bootstrap(Failure::PythonMissing)),
        );
        assert!(model.connection.can_retry());
        assert_eq!(model.problem.unwrap().title(), "Python 3.14 is required");
        send(&mut model, 5, AppEvent::Bootstrap(Stage::CheckingDaemon));
        assert_eq!(model.problem, None);
        send(
            &mut model,
            6,
            AppEvent::Failed(Problem::Bootstrap(Failure::AirPodsUnavailable)),
        );
        assert_eq!(model.connection, Connection::Disconnected);
        send(&mut model, 7, sample(169));
        assert_eq!(model.connection, Connection::Streaming);
        assert_eq!(model.latest.unwrap().sample.bpm, 169);
        assert_eq!(model.stats.count, 1);
    }
}
