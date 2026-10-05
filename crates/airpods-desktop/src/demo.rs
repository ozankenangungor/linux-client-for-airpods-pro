//! A repeatable showroom script, evaluated by elapsed time rather than frame rate.

use crate::model::{AppEvent, TimedEvent};
use airpods_client::{HeartRateSample, SourceSide};
use std::time::Duration;

pub const LOOP_DURATION: Duration = Duration::from_secs(18);

pub struct DemoSource {
    script: Vec<TimedEvent>,
    cycle: Option<u128>,
    cursor: usize,
}

impl Default for DemoSource {
    fn default() -> Self {
        let mut script = vec![
            event(0, AppEvent::Reset),
            event(0, AppEvent::Connecting),
            event(250, AppEvent::Connected),
            event(8000, AppEvent::Disconnected),
            event(
                8000,
                AppEvent::Reconnecting {
                    attempt: 1,
                    delay: Duration::from_secs(1),
                },
            ),
            event(
                9000,
                AppEvent::Reconnecting {
                    attempt: 2,
                    delay: Duration::from_secs(1),
                },
            ),
            event(
                10_000,
                AppEvent::Reconnecting {
                    attempt: 3,
                    delay: Duration::from_secs(1),
                },
            ),
            event(11_000, AppEvent::Reconnected { attempts: 3 }),
        ];
        // Uneven receipt intervals and small fixed variations avoid a perfect sine.
        let mut time_ms = 600;
        let mut index = 0;
        while time_ms < LOOP_DURATION.as_millis() as u64 {
            if !(8000..11_000).contains(&time_ms) {
                script.push(event(
                    time_ms,
                    AppEvent::Sample(HeartRateSample {
                        bpm: waveform(time_ms),
                        source_side: if index % 7 < 4 {
                            SourceSide::Left
                        } else {
                            SourceSide::Right
                        },
                    }),
                ));
                index += 1;
            }
            time_ms += [480, 620, 510, 570][index % 4];
        }
        script.sort_by_key(|event| event.at);
        Self {
            script,
            cycle: None,
            cursor: 0,
        }
    }
}

impl DemoSource {
    pub fn time(elapsed: Duration) -> Duration {
        Duration::from_nanos((elapsed.as_nanos() % LOOP_DURATION.as_nanos()) as u64)
    }

    pub fn poll(&mut self, elapsed: Duration) -> Vec<TimedEvent> {
        let cycle = elapsed.as_nanos() / LOOP_DURATION.as_nanos();
        if self.cycle != Some(cycle) {
            self.cycle = Some(cycle);
            self.cursor = 0;
        }
        // If the window was hidden for hours, only replay the current cycle.
        let now = Self::time(elapsed);
        let begin = self.cursor;
        while self.cursor < self.script.len() && self.script[self.cursor].at <= now {
            self.cursor += 1;
        }
        self.script[begin..self.cursor].to_vec()
    }
}

fn event(time_ms: u64, event: AppEvent) -> TimedEvent {
    TimedEvent {
        at: Duration::from_millis(time_ms),
        event,
    }
}

fn waveform(time_ms: u64) -> u8 {
    let t = time_ms as f64 / 1000.0;
    let ramp = if t < 8.0 {
        (t - 0.6) * 1.25
    } else {
        8.0 - (t - 11.0) * 0.9
    };
    let detail = [0.0, 1.0, -1.0, 0.0, 2.0, -1.0, 1.0, 0.0][(time_ms / 500) as usize % 8];
    (91.0 + ramp + 3.0 * (t * 0.77).sin() + 1.4 * (t * 2.1).sin() + detail).round() as u8
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{Connection, Freshness, Model, STARTUP_SAMPLES, StreamPhase};

    fn advance(demo: &mut DemoSource, model: &mut Model, millis: u64) {
        let elapsed = Duration::from_millis(millis);
        for event in demo.poll(elapsed) {
            model.apply(event);
        }
        model.tick(DemoSource::time(elapsed));
    }

    #[test]
    fn same_timeline_produces_identical_events_and_known_values() {
        let mut a = DemoSource::default();
        let mut b = DemoSource::default();
        for millis in [
            0, 250, 600, 7500, 8000, 10_000, 11_000, 17_900, 18_000, 18_600,
        ] {
            assert_eq!(
                a.poll(Duration::from_millis(millis)),
                b.poll(Duration::from_millis(millis))
            );
        }
        assert_eq!(waveform(600), 95);
        assert_eq!(waveform(7500), 98);
    }

    #[test]
    fn samples_do_not_depend_on_render_cadence() {
        let mut fine = DemoSource::default();
        let mut all = Vec::new();
        for millis in (0..=17_900).step_by(37) {
            all.extend(fine.poll(Duration::from_millis(millis)));
        }
        all.extend(fine.poll(Duration::from_millis(17_900)));
        let coarse = DemoSource::default().poll(Duration::from_millis(17_900));
        assert_eq!(all, coarse);
    }

    #[test]
    fn demo_exercises_startup_again_after_recovery_without_changing_samples() {
        let mut demo = DemoSource::default();
        let mut model = Model::default();
        advance(&mut demo, &mut model, 250);
        assert_eq!(model.stream_phase, StreamPhase::Starting);
        advance(&mut demo, &mut model, 2300);
        assert_eq!(model.stats.count, STARTUP_SAMPLES as u64);
        assert_eq!(model.stream_phase, StreamPhase::Starting);
        assert_eq!(model.stats.average(), None);
        advance(&mut demo, &mut model, 3000);
        assert_eq!(model.stream_phase, StreamPhase::Live);
        assert!(model.stats.average().is_some());
        advance(&mut demo, &mut model, 8000);
        let before = model.stats;
        advance(&mut demo, &mut model, 11_000);
        assert_eq!(model.stream_phase, StreamPhase::Starting);
        advance(&mut demo, &mut model, 13_400);
        assert_eq!(model.stream_phase, StreamPhase::Starting);
        assert_eq!(model.stats.count, before.count + STARTUP_SAMPLES as u64);
        assert_eq!(model.stats.average(), before.average());
        advance(&mut demo, &mut model, 14_000);
        assert_eq!(model.stream_phase, StreamPhase::Live);
        assert_eq!(model.history.len() as u64, model.stats.count);
        for point in &model.history {
            assert_eq!(point.sample.bpm, waveform(point.at.as_millis() as u64));
        }
    }

    #[test]
    fn showroom_connection_loss_and_resume_happen_at_fixed_times() {
        let mut demo = DemoSource::default();
        let mut model = Model::default();
        advance(&mut demo, &mut model, 0);
        assert_eq!(model.connection, Connection::Connecting);
        advance(&mut demo, &mut model, 250);
        assert_eq!(model.connection, Connection::Connected);
        advance(&mut demo, &mut model, 7500);
        assert_eq!(model.connection, Connection::Streaming);
        let before = model.stats.count;
        let segment = model.history.back().unwrap().segment;
        advance(&mut demo, &mut model, 8000);
        assert!(matches!(
            model.connection,
            Connection::Reconnecting { attempt: 1, .. }
        ));
        assert_eq!(
            model.freshness(Duration::from_secs(8)),
            Freshness::Unavailable
        );
        let disconnected_count = model.stats.count;
        assert!(disconnected_count >= before);
        advance(&mut demo, &mut model, 10_000);
        assert!(matches!(
            model.connection,
            Connection::Reconnecting { attempt: 3, .. }
        ));
        assert_eq!(model.stats.count, disconnected_count);
        advance(&mut demo, &mut model, 11_000);
        assert_eq!(model.connection, Connection::Connected);
        advance(&mut demo, &mut model, 12_000);
        assert_eq!(model.connection, Connection::Streaming);
        assert_ne!(model.history.back().unwrap().segment, segment);
        assert!(model.stats.count > disconnected_count);
    }

    #[test]
    fn loop_resets_samples_statistics_and_timeline_cleanly() {
        let mut demo = DemoSource::default();
        let mut model = Model::default();
        advance(&mut demo, &mut model, 17_900);
        assert_eq!(model.stats.count, 27);
        advance(&mut demo, &mut model, 18_000);
        assert_eq!(model.stats.count, 0);
        assert!(model.history.is_empty());
        assert_eq!(model.activity.len(), 1);
        assert_eq!(model.connection, Connection::Connecting);
        let second = demo.poll(Duration::from_millis(18_600));
        let first = DemoSource::default().poll(Duration::from_millis(600));
        assert_eq!(
            second,
            first
                .into_iter()
                .filter(|event| event.at > Duration::ZERO)
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn hidden_window_catches_up_only_one_cycle() {
        let mut demo = DemoSource::default();
        demo.poll(Duration::ZERO);
        let events = demo.poll(Duration::from_secs(18 * 10_000 + 14));
        assert_eq!(events, DemoSource::default().poll(Duration::from_secs(14)));
        assert!(demo.poll(Duration::from_secs(18 * 10_000 + 14)).is_empty());
    }
}
