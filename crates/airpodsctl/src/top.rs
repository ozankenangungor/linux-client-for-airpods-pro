//! Interactive, opt-in dashboard for the existing resilient heart-rate stream.

use airpods_client :: HeartRateSample ;
use airpods_client_resilient :: { ResilientHeartRateEvent } ;
use crossterm :: event :: { self } ;
use ratatui :: layout :: { Constraint , Layout , Rect } ;
use ratatui :: widgets :: { Block , List , ListItem , Paragraph , Sparkline } ;
use ratatui :: { Frame } ;
use std :: collections :: VecDeque ;



use std :: time :: Duration ;



const HISTORY_CAPACITY: usize = 120;
// Below this size, a compact help screen avoids truncated dashboard sections.
const MIN_WIDTH: u16 = 50;
const MIN_HEIGHT: u16 = 18;


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











// Both effects run in order on every ordinary exit. The primary runtime error wins.




#[cfg(test)]
mod tests;
