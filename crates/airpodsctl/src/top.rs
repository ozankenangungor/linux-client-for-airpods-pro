//! Interactive, opt-in dashboard for the existing resilient heart-rate stream.

use airpods_client::HeartRateSample;
use airpods_client_resilient::{
    ReconnectPolicy, ResilientHeartRateEvent, ResilientHeartRateStream,
};
use crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::widgets::{Block, List, ListItem, Paragraph, Sparkline};
use ratatui::{DefaultTerminal, Frame};
use std::collections::VecDeque;
use std::future::Future;
use std::io::{self, IsTerminal};
use std::path::PathBuf;
use std::time::Duration;
use tokio::signal::unix::{SignalKind, signal};
use tokio::time::MissedTickBehavior;

const HISTORY_CAPACITY: usize = 120;
// Below this size, a compact help screen avoids truncated dashboard sections.
const MIN_WIDTH: u16 = 50;
const MIN_HEIGHT: u16 = 18;
const INPUT_TICK: Duration = Duration::from_millis(50);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TopConnectionState {
    Connecting,
    Connected,
    Reconnecting { attempt: usize, delay: Duration },
    Reconnected { attempts: usize },
    Ended,
}

impl TopConnectionState {
    fn label(self) -> String {
        match self {
            Self::Connecting => "connecting".into(),
            Self::Connected => "connected".into(),
            Self::Reconnecting { attempt, delay } => {
                format!("reconnecting attempt {attempt} in {}ms", delay.as_millis())
            }
            Self::Reconnected { attempts } => format!(
                "reconnected after {attempts} {}",
                if attempts == 1 { "attempt" } else { "attempts" }
            ),
            Self::Ended => "ended".into(),
        }
    }
}

#[derive(Debug)]
struct TopModel {
    history: VecDeque<HeartRateSample>,
    total_samples: u64,
    connection: TopConnectionState,
}

impl Default for TopModel {
    fn default() -> Self {
        Self {
            history: VecDeque::with_capacity(HISTORY_CAPACITY),
            total_samples: 0,
            connection: TopConnectionState::Connecting,
        }
    }
}

impl TopModel {
    fn apply(&mut self, event: ResilientHeartRateEvent) {
        match event {
            ResilientHeartRateEvent::Sample(sample) => {
                if self.history.len() == HISTORY_CAPACITY {
                    self.history.pop_front();
                }
                self.history.push_back(sample);
                self.total_samples = self.total_samples.saturating_add(1);
                self.connection = TopConnectionState::Connected;
            }
            ResilientHeartRateEvent::Reconnecting { attempt, delay } => {
                self.connection = TopConnectionState::Reconnecting { attempt, delay };
            }
            ResilientHeartRateEvent::Reconnected { attempts } => {
                self.connection = TopConnectionState::Reconnected { attempts };
            }
        }
    }

    fn render(&self, frame: &mut Frame<'_>) {
        let area = frame.area();
        if area.width < MIN_WIDTH || area.height < MIN_HEIGHT {
            frame.render_widget(
                Paragraph::new("AirPods HR\nterminal too small\nq/Esc to quit"),
                area,
            );
            return;
        }
        let regions = Layout::vertical([
            Constraint::Length(1),
            Constraint::Length(3),
            Constraint::Length(2),
            Constraint::Length(2),
            Constraint::Length(4),
            Constraint::Min(3),
            Constraint::Length(1),
        ])
        .split(area);
        frame.render_widget(Paragraph::new("AirPods HR"), regions[0]);
        let reading = match self.history.back() {
            Some(sample) => format!("{} BPM ({})", sample.bpm, sample.source_side),
            None => "waiting for sample".into(),
        };
        frame.render_widget(
            Paragraph::new(reading).block(Block::bordered().title("Current reading")),
            regions[1],
        );
        frame.render_widget(
            Paragraph::new(format!("Connection: {}", self.connection.label())),
            regions[2],
        );
        frame.render_widget(
            Paragraph::new(format!("Samples received: {}", self.total_samples)),
            regions[3],
        );
        let bpm: Vec<u64> = self
            .history
            .iter()
            .map(|sample| u64::from(sample.bpm))
            .collect();
        frame.render_widget(
            Sparkline::default()
                .block(Block::bordered().title("History"))
                .data(&bpm)
                .max(255),
            regions[4],
        );
        self.render_recent(frame, regions[5]);
        frame.render_widget(
            Paragraph::new("q / Esc  quit    Ctrl-C  interrupt"),
            regions[6],
        );
    }

    fn render_recent(&self, frame: &mut Frame<'_>, area: Rect) {
        let visible = usize::from(area.height.saturating_sub(2));
        let mut recent: Vec<_> = self.history.iter().rev().take(visible).collect();
        recent.reverse();
        let rows: Vec<ListItem<'_>> = recent
            .into_iter()
            .map(|sample| ListItem::new(format!("{} BPM ({})", sample.bpm, sample.source_side)))
            .collect();
        frame.render_widget(
            List::new(rows).block(Block::bordered().title("Recent samples")),
            area,
        );
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum InputAction {
    Ignore,
    Redraw,
    Quit(u8),
}

fn input_action(event: Event) -> InputAction {
    match event {
        Event::Resize(..) => InputAction::Redraw,
        Event::Key(key) if key.kind == KeyEventKind::Press => match key.code {
            KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                InputAction::Quit(130)
            }
            KeyCode::Char('q') | KeyCode::Esc => InputAction::Quit(0),
            _ => InputAction::Ignore,
        },
        _ => InputAction::Ignore,
    }
}

struct TerminalGuard {
    terminal: Option<DefaultTerminal>,
}

impl TerminalGuard {
    fn init() -> Result<Self, String> {
        match ratatui::try_init() {
            Ok(terminal) => Ok(Self {
                terminal: Some(terminal),
            }),
            Err(error) => {
                let restore = ratatui::try_restore().err();
                let mut message = format!("initializing terminal: {error}");
                if let Some(restore) = restore {
                    message.push_str(&format!("; restoring terminal: {restore}"));
                }
                Err(message)
            }
        }
    }

    fn draw(&mut self, model: &TopModel) -> Result<(), String> {
        match self.terminal.as_mut() {
            Some(terminal) => terminal
                .draw(|frame| model.render(frame))
                .map(|_| ())
                .map_err(|error| format!("drawing terminal: {error}")),
            None => Err("terminal has already been restored".into()),
        }
    }

    fn restore(&mut self) -> Result<(), String> {
        self.terminal.take();
        ratatui::try_restore().map_err(|error| format!("restoring terminal: {error}"))
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        if self.terminal.is_some() {
            let _ = self.restore();
        }
    }
}

// Both effects run in order on every ordinary exit. The primary runtime error wins.
async fn settle<R, C, F>(primary: Result<u8, String>, restore: R, close: C) -> Result<u8, String>
where
    R: FnOnce() -> Result<(), String>,
    C: FnOnce() -> F,
    F: Future<Output = Result<(), String>>,
{
    let restore_error = restore().err();
    let close_error = close().await.err();
    let mut errors = Vec::new();
    if let Err(error) = primary.as_ref() {
        errors.push(error.clone());
    }
    if let Some(error) = restore_error {
        errors.push(error);
    }
    if let Some(error) = close_error {
        errors.push(error);
    }
    if errors.is_empty() {
        primary
    } else {
        Err(errors.join("; "))
    }
}

pub(super) async fn run(
    socket: Option<PathBuf>,
    json_mode: bool,
    explicit_socket: bool,
) -> Result<u8, String> {
    if json_mode {
        return Err("--json cannot be used with top".into());
    }
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err("top requires terminal stdin and stdout".into());
    }
    let mut term = signal(SignalKind::terminate())
        .map_err(|error| format!("listening for SIGTERM: {error}"))?;
    let mut interrupt = signal(SignalKind::interrupt())
        .map_err(|error| format!("listening for SIGINT: {error}"))?;
    let mut stream = match socket {
        Some(path) => ResilientHeartRateStream::explicit_socket(path, ReconnectPolicy::default()),
        None => ResilientHeartRateStream::default_socket(ReconnectPolicy::default()),
    };
    let mut guard = TerminalGuard::init()?;
    let mut model = TopModel::default();
    let mut dirty = true;
    let mut ended = false;
    let mut tick = tokio::time::interval(INPUT_TICK);
    tick.set_missed_tick_behavior(MissedTickBehavior::Skip);
    let outcome = 'event_loop: loop {
        // Keep this future alive across input ticks and resizes. Dropping it on
        // every tick could cancel an in-flight connect or subscribe repeatedly.
        let was_ended = ended;
        let next = async {
            if was_ended {
                std::future::pending().await
            } else {
                stream.next().await
            }
        };
        tokio::pin!(next);
        loop {
            if dirty {
                if let Err(error) = guard.draw(&model) {
                    break 'event_loop Err(error);
                }
                dirty = false;
            }
            tokio::select! {
                biased;
                received = interrupt.recv() => {
                    break 'event_loop received.map(|()| 130).ok_or_else(|| "SIGINT listener ended".into());
                }
                received = term.recv() => {
                    break 'event_loop received.map(|()| 143).ok_or_else(|| "SIGTERM listener ended".into());
                }
                _ = tick.tick() => {
                    for _ in 0..64 {
                        match event::poll(Duration::ZERO) {
                            Ok(false) => break,
                            Ok(true) => match event::read() {
                                Ok(input) => match input_action(input) {
                                    InputAction::Ignore => {},
                                    InputAction::Redraw => dirty = true,
                                    InputAction::Quit(code) => break 'event_loop Ok(code),
                                },
                                Err(error) => break 'event_loop Err(format!("reading terminal event: {error}")),
                            },
                            Err(error) => break 'event_loop Err(format!("polling terminal events: {error}")),
                        }
                    }
                }
                result = &mut next => {
                    match result {
                        Ok(Some(event)) => { model.apply(event); dirty = true; break; }
                        Ok(None) => { model.connection = TopConnectionState::Ended; ended = true; dirty = true; break; }
                        Err(error) => break 'event_loop Err(super::resilient_error(error, explicit_socket)),
                    }
                }
            }
        }
    };
    settle(
        outcome,
        || guard.restore(),
        || async {
            stream
                .close()
                .await
                .map_err(|error| super::resilient_error(error, explicit_socket))
        },
    )
    .await
}

#[cfg(test)]
mod tests;
