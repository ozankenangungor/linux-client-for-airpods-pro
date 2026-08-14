use super :: * ;
use airpods_client :: SourceSide ;
use crossterm :: event :: KeyEvent ;
use ratatui :: Terminal ;
use ratatui :: backend :: TestBackend ;
use std :: cell :: RefCell ;

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

#[test]
fn input_policy_handles_press_only_and_resize() {
    let key = |code, modifiers| Event::Key(KeyEvent::new(code, modifiers));
    assert_eq!(
        input_action(key(KeyCode::Char('q'), KeyModifiers::NONE)),
        InputAction::Quit(0)
    );
    assert_eq!(
        input_action(key(KeyCode::Esc, KeyModifiers::NONE)),
        InputAction::Quit(0)
    );
    assert_eq!(
        input_action(key(KeyCode::Char('c'), KeyModifiers::CONTROL)),
        InputAction::Quit(130)
    );
    assert_eq!(input_action(Event::Resize(100, 30)), InputAction::Redraw);
    assert_eq!(
        input_action(Event::Key(KeyEvent::new_with_kind(
            KeyCode::Char('q'),
            KeyModifiers::NONE,
            KeyEventKind::Release
        ))),
        InputAction::Ignore
    );
}

#[tokio::test]
async fn settle_restores_then_closes_on_normal_exit() {
    let calls = RefCell::new(Vec::new());
    let result = settle(
        Ok(0),
        || {
            calls.borrow_mut().push("restore");
            Ok(())
        },
        || async {
            calls.borrow_mut().push("close");
            Ok(())
        },
    )
    .await;
    assert_eq!(result, Ok(0));
    assert_eq!(*calls.borrow(), ["restore", "close"]);
}

#[tokio::test]
async fn settle_attempts_both_cleanups_and_preserves_primary_error() {
    let calls = RefCell::new(Vec::new());
    let result = settle(
        Err("primary failure".into()),
        || {
            calls.borrow_mut().push("restore");
            Err("restore failure".into())
        },
        || async {
            calls.borrow_mut().push("close");
            Err("close failure".into())
        },
    )
    .await
    .unwrap_err();
    assert_eq!(*calls.borrow(), ["restore", "close"]);
    assert!(result.starts_with("primary failure; "));
    assert!(result.contains("restore failure"));
    assert!(result.contains("close failure"));
}

#[tokio::test]
async fn settle_clean_exit_reports_cleanup_failure() {
    assert_eq!(
        settle(Ok(130), || Ok(()), || async { Err("close failure".into()) }).await,
        Err("close failure".into())
    );
}
