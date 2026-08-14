use super :: * ;
use airpods_client :: SourceSide ;

use ratatui :: Terminal ;
use ratatui :: backend :: TestBackend ;


fn sample(bpm: u8, source_side: SourceSide) -> HeartRateSample {
    HeartRateSample { bpm, source_side }
}

fn screen(model: &TopModel, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal.draw(|frame| model.render(frame)).unwrap();
    terminal
        .backend()
        .buffer()
        .content()
        .iter()
        .fold(String::new(), |mut text, cell| {
            text.push_str(cell.symbol());
            text
        })
}

#[test]
fn initial_state_and_connection_transitions() {
    let mut model = TopModel::default();
    assert!(model.history.is_empty());
    assert_eq!(model.total_samples, 0);
    assert_eq!(model.connection, TopConnectionState::Connecting);
    model.apply(ResilientHeartRateEvent::Reconnecting {
        attempt: 3,
        delay: Duration::from_secs(5),
    });
    assert_eq!(
        model.connection,
        TopConnectionState::Reconnecting {
            attempt: 3,
            delay: Duration::from_secs(5)
        }
    );
    assert_eq!(model.connection.label(), "reconnecting attempt 3 in 5000ms");
    model.apply(ResilientHeartRateEvent::Reconnected { attempts: 3 });
    assert_eq!(
        model.connection,
        TopConnectionState::Reconnected { attempts: 3 }
    );
    assert_eq!(model.connection.label(), "reconnected after 3 attempts");
    model.apply(ResilientHeartRateEvent::Sample(sample(
        169,
        SourceSide::Left,
    )));
    assert_eq!(model.connection, TopConnectionState::Connected);
    assert_eq!(
        model.history.back().copied(),
        Some(sample(169, SourceSide::Left))
    );
    assert_eq!(model.total_samples, 1);
}

#[test]
fn history_preserves_order_duplicates_and_unknown_raw_with_exact_cap() {
    let mut model = TopModel::default();
    model.apply(ResilientHeartRateEvent::Sample(sample(
        169,
        SourceSide::Left,
    )));
    for _ in 0..119 {
        model.apply(ResilientHeartRateEvent::Sample(sample(
            88,
            SourceSide::Right,
        )));
    }
    assert_eq!(model.history.len(), HISTORY_CAPACITY);
    assert_eq!(
        model.history.front().copied(),
        Some(sample(169, SourceSide::Left))
    );
    model.apply(ResilientHeartRateEvent::Sample(sample(
        74,
        SourceSide::Unknown(37),
    )));
    assert_eq!(model.history.len(), HISTORY_CAPACITY);
    assert_eq!(
        model.history.front().copied(),
        Some(sample(88, SourceSide::Right))
    );
    assert_eq!(
        model.history.back().copied(),
        Some(sample(74, SourceSide::Unknown(37)))
    );
    assert_eq!(model.total_samples, 121);
    assert_eq!(
        model
            .history
            .iter()
            .filter(|sample| sample.bpm == 88)
            .count(),
        119
    );
}

#[test]
fn testbackend_renders_values_duplicates_help_and_responsive_sizes() {
    let mut model = TopModel::default();
    for value in [
        sample(169, SourceSide::Left),
        sample(88, SourceSide::Right),
        sample(88, SourceSide::Right),
        sample(74, SourceSide::Unknown(37)),
    ] {
        model.apply(ResilientHeartRateEvent::Sample(value));
    }
    let normal = screen(&model, 80, 24);
    assert!(normal.contains("AirPods HR"));
    assert!(normal.contains("169 BPM (left)"));
    assert!(normal.matches("88 BPM (right)").count() >= 2);
    assert!(normal.contains("74 BPM (unknown(37))"));
    assert!(normal.contains("Samples received: 4"));
    assert!(normal.contains("Ctrl-C"));
    let reduced = screen(&model, MIN_WIDTH, MIN_HEIGHT);
    assert!(reduced.contains("AirPods HR"));
    assert!(reduced.contains("History"));
    let fallback = screen(&model, 30, 6);
    assert!(fallback.contains("terminal too small"));
    assert!(fallback.contains("q/Esc to quit"));
    for (width, height) in [(0, 0), (1, 1), (4, 2), (49, 17)] {
        let _ = screen(&model, width, height);
    }
}








