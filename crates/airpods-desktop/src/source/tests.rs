use super::*;
use crate::model::{Connection, Model};
use airpods_client::{HeartRateSample, SourceSide};
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, unix::OwnedReadHalf};

static NEXT_SOCKET: AtomicU64 = AtomicU64::new(0);
const SUBSCRIBED: &str = "{\"protocol_version\":1,\"ok\":true,\"operation\":\"subscribe\",\"stream\":\"heart_rate\",\"subscribed\":true,\"already_subscribed\":false}\n";
const UNSUBSCRIBED: &str = "{\"protocol_version\":1,\"ok\":true,\"operation\":\"unsubscribe\",\"stream\":\"heart_rate\",\"subscribed\":false,\"already_unsubscribed\":false}\n";

struct SocketPath(PathBuf);

impl SocketPath {
    fn new() -> Self {
        Self(std::env::temp_dir().join(format!(
            "desktop-{}-{}.sock",
            std::process::id(),
            NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
        )))
    }
}

impl Drop for SocketPath {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

async fn request(reader: &mut BufReader<OwnedReadHalf>) -> String {
    let mut line = String::new();
    tokio::time::timeout(Duration::from_secs(3), reader.read_line(&mut line))
        .await
        .unwrap()
        .unwrap();
    line
}

async fn wait_for(
    source: &mut RealSource,
    predicate: impl Fn(&[TimedEvent]) -> bool,
) -> Vec<TimedEvent> {
    tokio::time::timeout(Duration::from_secs(3), async {
        let mut events = Vec::new();
        loop {
            events.extend(source.poll());
            if predicate(&events) {
                return events;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .expect("worker did not deliver the expected event")
}

#[tokio::test]
async fn real_worker_preserves_samples_and_confirms_one_unsubscribe() {
    let path = SocketPath::new();
    let listener = UnixListener::bind(&path.0).unwrap();
    let server = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let (read, mut write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        write.write_all(SUBSCRIBED.as_bytes()).await.unwrap();
        for (bpm, side, extra) in [
            (169, "left", ""),
            (88, "right", ""),
            (88, "right", ""),
            (73, "unknown", ",\"source_side_raw\":37"),
        ] {
            write.write_all(format!("{{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":{bpm},\"source_side\":\"{side}\"{extra}}}\n").as_bytes()).await.unwrap();
        }
        assert!(request(&mut reader).await.contains("unsubscribe"));
        write.write_all(UNSUBSCRIBED.as_bytes()).await.unwrap();
        assert!(request(&mut reader).await.is_empty());
    });
    let mut source =
        RealSource::start(Some(path.0.clone()), Context::default(), Instant::now()).unwrap();
    let events = wait_for(&mut source, |events| {
        events
            .iter()
            .filter(|event| matches!(event.event, AppEvent::Sample(_)))
            .count()
            == 4
    })
    .await;
    let mut model = Model::default();
    for event in events {
        model.apply(event);
    }
    assert_eq!(model.connection, Connection::Streaming);
    assert_eq!(
        model
            .history
            .iter()
            .map(|point| point.sample)
            .collect::<Vec<_>>(),
        [
            HeartRateSample {
                bpm: 169,
                source_side: SourceSide::Left
            },
            HeartRateSample {
                bpm: 88,
                source_side: SourceSide::Right
            },
            HeartRateSample {
                bpm: 88,
                source_side: SourceSide::Right
            },
            HeartRateSample {
                bpm: 73,
                source_side: SourceSide::Unknown(37)
            },
        ]
    );
    source.request_stop();
    server.await.unwrap();
    source.shutdown();
    assert!(source.thread.is_none());
}

#[tokio::test]
async fn real_reconnect_resubscribes_once_and_reuses_the_session_model() {
    let path = SocketPath::new();
    let listener = UnixListener::bind(&path.0).unwrap();
    let (allow_disconnect, disconnected) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let mut disconnected = Some(disconnected);
        for (bpm, side) in [(90, "left"), (100, "right")] {
            let (socket, _) = listener.accept().await.unwrap();
            let (read, mut write) = socket.into_split();
            let mut reader = BufReader::new(read);
            assert!(request(&mut reader).await.contains("subscribe"));
            write.write_all(SUBSCRIBED.as_bytes()).await.unwrap();
            write.write_all(format!("{{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":{bpm},\"source_side\":\"{side}\"}}\n").as_bytes()).await.unwrap();
            if bpm == 100 {
                assert!(request(&mut reader).await.contains("unsubscribe"));
                write.write_all(UNSUBSCRIBED.as_bytes()).await.unwrap();
                assert!(request(&mut reader).await.is_empty());
            } else {
                disconnected.take().unwrap().await.unwrap();
            }
        }
    });
    let (sender, mut events) = mpsc::channel(16);
    let (stop, stopped) = watch::channel(false);
    let (_retry, retries) = mpsc::channel(1);
    let task = tokio::spawn(drive(
        Some(path.0.clone()),
        Instant::now(),
        sender,
        stopped,
        retries,
        Context::default(),
        ReconnectPolicy::new(vec![Duration::from_millis(10)]).unwrap(),
    ));
    let mut model = Model::default();
    let mut allow_disconnect = Some(allow_disconnect);
    tokio::time::timeout(Duration::from_secs(3), async {
        while model.stats.count < 2 {
            model.apply(events.recv().await.unwrap());
            if model.stats.count == 1
                && let Some(allow) = allow_disconnect.take()
            {
                allow.send(()).unwrap();
            }
        }
    })
    .await
    .unwrap();
    assert_eq!(model.stats.average(), Some(95.0));
    assert_ne!(model.history[0].segment, model.history[1].segment);
    assert_eq!(model.history[0].sample.source_side, SourceSide::Left);
    assert_eq!(model.history[1].sample.source_side, SourceSide::Right);
    assert!(
        model
            .activity
            .iter()
            .any(|event| event.text == "Reconnected after 1 attempt")
    );
    stop.send(true).unwrap();
    task.await.unwrap();
    server.await.unwrap();
}

#[tokio::test]
async fn quiet_subscription_closes_without_waiting_for_a_sample() {
    let path = SocketPath::new();
    let listener = UnixListener::bind(&path.0).unwrap();
    let (ready, waiting) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let (read, mut write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        write.write_all(SUBSCRIBED.as_bytes()).await.unwrap();
        ready.send(()).unwrap();
        // Window close can race the initial subscription acknowledgement. An
        // already active stream confirms unsubscribe; a cancelled establishment
        // closes its socket instead. Both release the fake daemon's subscriber.
        // Linux can report ConnectionReset rather than EOF in the latter race.
        let mut cleanup = String::new();
        match tokio::time::timeout(Duration::from_secs(3), reader.read_line(&mut cleanup))
            .await
            .expect("subscription cleanup timed out")
        {
            Ok(_) => {}
            Err(error)
                if error.kind() == std::io::ErrorKind::ConnectionReset && cleanup.is_empty() => {}
            Err(error) => panic!("failed to read subscription cleanup: {error}"),
        }
        if !cleanup.is_empty() {
            assert_eq!(
                cleanup,
                "{\"operation\":\"unsubscribe\",\"protocol_version\":1,\"stream\":\"heart_rate\"}\n"
            );
            write.write_all(UNSUBSCRIBED.as_bytes()).await.unwrap();
            assert!(request(&mut reader).await.is_empty());
        }
    });
    let mut source =
        RealSource::start(Some(path.0.clone()), Context::default(), Instant::now()).unwrap();
    waiting.await.unwrap();
    source.request_stop();
    server.await.unwrap();
    source.shutdown();
}

#[tokio::test]
async fn silent_unsubscribe_cannot_hang_window_close() {
    let path = SocketPath::new();
    let listener = UnixListener::bind(&path.0).unwrap();
    let (ready, waiting) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let (read, mut write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        write.write_all(SUBSCRIBED.as_bytes()).await.unwrap();
        write.write_all(b"{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":88,\"source_side\":\"left\"}\n").await.unwrap();
        ready.send(()).unwrap();
        assert!(request(&mut reader).await.contains("unsubscribe"));
        // Intentionally no reply. Cancellation must ultimately close the socket.
        assert!(request(&mut reader).await.is_empty());
    });
    let mut source =
        RealSource::start(Some(path.0.clone()), Context::default(), Instant::now()).unwrap();
    waiting.await.unwrap();
    wait_for(&mut source, |events| {
        events
            .iter()
            .any(|event| matches!(event.event, AppEvent::Sample(_)))
    })
    .await;
    let before = Instant::now();
    source.shutdown();
    assert!(before.elapsed() < Duration::from_secs(2));
    server.await.unwrap();
}

#[tokio::test]
async fn shutdown_interrupts_a_stalled_subscription_handshake() {
    let path = SocketPath::new();
    let listener = UnixListener::bind(&path.0).unwrap();
    let (ready, waiting) = tokio::sync::oneshot::channel();
    let server = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let (read, _write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        ready.send(()).unwrap();
        assert!(request(&mut reader).await.is_empty());
    });
    let mut source =
        RealSource::start(Some(path.0.clone()), Context::default(), Instant::now()).unwrap();
    waiting.await.unwrap();
    let before = Instant::now();
    source.shutdown();
    assert!(before.elapsed() < Duration::from_secs(2));
    server.await.unwrap();
}

#[tokio::test]
async fn missing_daemon_is_nonblocking_and_backoff_is_cancellable() {
    let path = SocketPath::new();
    let mut source =
        RealSource::start(Some(path.0.clone()), Context::default(), Instant::now()).unwrap();
    let events = wait_for(&mut source, |events| {
        events
            .iter()
            .any(|event| matches!(event.event, AppEvent::Reconnecting { attempt: 1, .. }))
    })
    .await;
    assert_eq!(events[0].event, AppEvent::Connecting);
    let before = Instant::now();
    source.shutdown();
    assert!(before.elapsed() < Duration::from_secs(2));
}

#[tokio::test]
async fn full_gui_queue_cannot_prevent_shutdown() {
    let (sender, _receiver) = mpsc::channel(1);
    sender
        .send(TimedEvent {
            at: Duration::ZERO,
            event: AppEvent::Connecting,
        })
        .await
        .unwrap();
    let (stop, stopped) = watch::channel(false);
    let (_retry, retries) = mpsc::channel(1);
    let task = tokio::spawn(drive(
        None,
        Instant::now(),
        sender,
        stopped,
        retries,
        Context::default(),
        ReconnectPolicy::default(),
    ));
    tokio::task::yield_now().await;
    stop.send(true).unwrap();
    tokio::time::timeout(Duration::from_secs(1), task)
        .await
        .unwrap()
        .unwrap();
}

#[tokio::test(start_paused = true)]
async fn retry_exhaustion_waits_for_user_and_user_retry_creates_one_new_episode() {
    let path = SocketPath::new();
    let (sender, mut events) = mpsc::channel(16);
    let (stop, stopped) = watch::channel(false);
    let (retry, retries) = mpsc::channel(1);
    let policy = ReconnectPolicy::new(vec![Duration::from_secs(1)]).unwrap();
    let task = tokio::spawn(drive(
        Some(path.0.clone()),
        Instant::now(),
        sender,
        stopped,
        retries,
        Context::default(),
        policy,
    ));
    assert_eq!(events.recv().await.unwrap().event, AppEvent::Connecting);
    assert!(matches!(
        events.recv().await.unwrap().event,
        AppEvent::Reconnecting { attempt: 1, .. }
    ));
    assert_eq!(
        events.recv().await.unwrap().event,
        AppEvent::Failed(Problem::Unavailable)
    );
    tokio::time::advance(Duration::from_secs(300)).await;
    assert!(events.try_recv().is_err());
    retry.send(()).await.unwrap();
    assert_eq!(events.recv().await.unwrap().event, AppEvent::Connecting);
    assert!(matches!(
        events.recv().await.unwrap().event,
        AppEvent::Reconnecting { attempt: 1, .. }
    ));
    stop.send(true).unwrap();
    task.await.unwrap();
}

#[test]
fn typed_errors_never_expose_daemon_messages_or_private_paths() {
    let error = Error::Client(ClientError::DaemonError {
        code: "secret".into(),
        message: "private packet /private/path".into(),
    });
    assert_eq!(problem(error), Problem::Rejected);
    assert!(!Problem::Rejected.title().contains("private"));
    assert_eq!(
        problem(Error::Client(ClientError::EventLagged { skipped: 9 })),
        Problem::Lagged
    );
    assert_eq!(
        problem(Error::Client(ClientError::ProtocolVersion {
            expected: 1,
            received: Some(2)
        })),
        Problem::Protocol
    );
}

#[test]
fn demo_never_enters_real_source_or_bootstrap_boundaries() {
    let mut real_calls = 0;
    let mut source = DataSource::choose(true, || {
        real_calls += 1;
        panic!("demo entered the real source: socket/Python/pip/systemctl access is forbidden")
    })
    .unwrap();
    for second in 0..=90 {
        source.poll(Duration::from_secs(second));
    }
    assert!(source.is_demo());
    assert!(!source.retry());
    source.shutdown();
    assert_eq!(real_calls, 0);
    // The exact production constructor also remains on this pure branch.
    let mut source = DataSource::start(true, None, Context::default(), Instant::now()).unwrap();
    assert!(matches!(source, DataSource::Demo(_)));
    source.shutdown();
}

#[test]
fn sensor_availability_errors_do_not_claim_bootstrap_failure_or_expose_messages() {
    for code in [
        "service_unavailable",
        "service_failed",
        "session_start_failed",
    ] {
        let error = Error::Client(ClientError::DaemonError {
            code: code.into(),
            message: "private hardware detail".into(),
        });
        assert_eq!(problem(error), Problem::SensorUnavailable);
        assert_eq!(Problem::SensorUnavailable.title(), "Waiting for AirPods");
    }
}
