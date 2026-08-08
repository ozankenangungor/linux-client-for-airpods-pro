use super::*;
use airpods_client::{Error as ClientError, SourceSide};
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixListener;
use tokio::sync::oneshot;

static NEXT: AtomicU64 = AtomicU64::new(0);
fn path() -> PathBuf {
    std::env::temp_dir().join(format!(
        "resilient-{}-{}.sock",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ))
}
fn connect(kind: io::ErrorKind) -> ClientError {
    ClientError::Connect {
        path: PathBuf::from("socket"),
        kind,
        message: "test".into(),
    }
}
fn io_error(kind: io::ErrorKind) -> ClientError {
    ClientError::Io {
        context: "test",
        kind,
        message: "test".into(),
    }
}

#[test]
fn policy_validation_and_defaults() {
    assert_eq!(
        ReconnectPolicy::default().delays(),
        &[1, 2, 5, 10, 10].map(Duration::from_secs)
    );
    assert_eq!(ReconnectPolicy::new(vec![]), Err(PolicyError::Empty));
    assert_eq!(
        ReconnectPolicy::new(vec![Duration::ZERO]),
        Err(PolicyError::ZeroDelay)
    );
    assert_eq!(
        ReconnectPolicy::new(vec![Duration::from_secs(1); 17]),
        Err(PolicyError::TooManyAttempts)
    );
}

#[test]
fn classification_is_explicit_and_closed() {
    use io::ErrorKind as K;
    assert!(is_recoverable(&ClientError::ConnectionClosed));
    for kind in [
        K::NotFound,
        K::ConnectionRefused,
        K::ConnectionReset,
        K::ConnectionAborted,
        K::NotConnected,
        K::TimedOut,
    ] {
        assert!(is_recoverable(&connect(kind)));
    }
    for kind in [
        K::ConnectionReset,
        K::ConnectionAborted,
        K::BrokenPipe,
        K::NotConnected,
        K::UnexpectedEof,
        K::TimedOut,
    ] {
        assert!(is_recoverable(&io_error(kind)));
    }
    for kind in [
        K::PermissionDenied,
        K::InvalidData,
        K::Other,
        K::BrokenPipe,
        K::UnexpectedEof,
    ] {
        assert!(!is_recoverable(&connect(kind)));
    }
    for kind in [K::PermissionDenied, K::InvalidData, K::Other, K::NotFound] {
        assert!(!is_recoverable(&io_error(kind)));
    }
    for error in [
        ClientError::XdgRuntimeDirMissing,
        ClientError::FrameTooLarge { limit: 1 },
        ClientError::InvalidJson {
            message: "bad".into(),
        },
        ClientError::ProtocolVersion {
            expected: 1,
            received: Some(2),
        },
        ClientError::UnexpectedMessage {
            message: "bad".into(),
        },
        ClientError::DaemonError {
            code: "no".into(),
            message: "bad".into(),
        },
        ClientError::SubscriptionActive,
        ClientError::EventLagged { skipped: 1 },
    ] {
        assert!(!is_recoverable(&error), "{error:?}");
    }
}

async fn request(reader: &mut BufReader<tokio::net::unix::OwnedReadHalf>) -> String {
    let mut line = String::new();
    reader.read_line(&mut line).await.unwrap();
    line
}
async fn send(writer: &mut tokio::net::unix::OwnedWriteHalf, line: &str) {
    writer.write_all(line.as_bytes()).await.unwrap();
}
const SUB_OK: &str = "{\"protocol_version\":1,\"ok\":true,\"operation\":\"subscribe\",\"stream\":\"heart_rate\",\"subscribed\":true,\"already_subscribed\":false}\n";
const UNSUB_OK: &str = "{\"protocol_version\":1,\"ok\":true,\"operation\":\"unsubscribe\",\"stream\":\"heart_rate\",\"subscribed\":false,\"already_unsubscribed\":false}\n";
fn policy() -> ReconnectPolicy {
    ReconnectPolicy::new(vec![Duration::from_millis(50), Duration::from_millis(80)]).unwrap()
}

#[tokio::test]
async fn initial_missing_socket_and_exact_retry_exhaustion() {
    let mut stream = ResilientHeartRateStream::explicit_socket(path(), policy());
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Reconnecting {
            attempt: 1,
            delay: Duration::from_millis(50)
        })
    );
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Reconnecting {
            attempt: 2,
            delay: Duration::from_millis(80)
        })
    );
    assert!(matches!(
        stream.next().await,
        Err(Error::RetryExhausted {
            attempts: 2,
            last_error: ClientError::Connect {
                kind: io::ErrorKind::NotFound,
                ..
            }
        })
    ));
    assert_eq!(stream.next().await.unwrap(), None);
}

#[tokio::test]
async fn initial_healthy_samples_are_unmodified_and_close_confirms_once() {
    let path = path();
    let listener = UnixListener::bind(&path).unwrap();
    let server = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let (read, mut write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        send(&mut write, SUB_OK).await;
        for side in ["left", "left", "right", "unknown"] {
            let raw = if side == "unknown" {
                ",\"source_side_raw\":37"
            } else {
                ""
            };
            send(&mut write, &format!("{{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":169,\"source_side\":\"{side}\"{raw}}}\n")).await;
        }
        assert!(request(&mut reader).await.contains("unsubscribe"));
        send(&mut write, UNSUB_OK).await;
        assert!(request(&mut reader).await.is_empty());
    });
    let mut stream = ResilientHeartRateStream::explicit_socket(&path, policy());
    for side in [
        SourceSide::Left,
        SourceSide::Left,
        SourceSide::Right,
        SourceSide::Unknown(37),
    ] {
        assert_eq!(
            stream.next().await.unwrap(),
            Some(ResilientHeartRateEvent::Sample(HeartRateSample {
                bpm: 169,
                source_side: side
            }))
        );
    }
    stream.close().await.unwrap();
    server.await.unwrap();
    std::fs::remove_file(path).unwrap();
}

#[tokio::test]
async fn retry_commits_only_after_subscribe_and_resets_after_second_loss() {
    let path = path();
    let mut stream = ResilientHeartRateStream::explicit_socket(&path, policy());
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Reconnecting {
            attempt: 1,
            delay: Duration::from_millis(50)
        })
    );
    let listener = UnixListener::bind(&path).unwrap();
    let (subscribed_tx, subscribed_rx) = oneshot::channel();
    let (resume_tx, resume_rx) = oneshot::channel();
    let (close_tx, close_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let (read, mut write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        subscribed_tx.send(()).unwrap();
        resume_rx.await.unwrap();
        send(&mut write, SUB_OK).await;
        send(&mut write, "{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":88,\"source_side\":\"right\"}\n").await;
        close_rx.await.unwrap();
    });
    let mut pending = Box::pin(stream.next());
    tokio::select! { _ = &mut pending => panic!("reconnected before subscribe response"), _ = subscribed_rx => {} }
    resume_tx.send(()).unwrap();
    assert_eq!(
        pending.await.unwrap(),
        Some(ResilientHeartRateEvent::Reconnected { attempts: 1 })
    );
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Sample(HeartRateSample {
            bpm: 88,
            source_side: SourceSide::Right
        }))
    );
    close_tx.send(()).unwrap();
    server.await.unwrap();
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Reconnecting {
            attempt: 1,
            delay: Duration::from_millis(50)
        })
    );
    stream.close().await.unwrap();
    std::fs::remove_file(path).unwrap();
}

#[tokio::test]
async fn cancellation_during_backoff_keeps_deadline_and_close_wins() {
    tokio::time::pause();
    let path = path();
    let mut stream = ResilientHeartRateStream::explicit_socket(&path, policy());
    assert!(matches!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Reconnecting { attempt: 1, .. })
    ));
    let mut pending = Box::pin(stream.next());
    tokio::select! { _ = &mut pending => panic!("retry ran before deadline"), _ = tokio::task::yield_now() => {} }
    drop(pending);
    tokio::time::advance(Duration::from_millis(60)).await;
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Reconnecting {
            attempt: 2,
            delay: Duration::from_millis(80)
        })
    );
    stream.close().await.unwrap();
    let listener = UnixListener::bind(&path).unwrap();
    tokio::time::advance(Duration::from_secs(1)).await;
    assert!(
        tokio::time::timeout(Duration::from_millis(1), listener.accept())
            .await
            .is_err()
    );
    std::fs::remove_file(path).unwrap();
}

#[tokio::test]
async fn terminal_protocol_and_daemon_errors_do_not_retry() {
    for response in [
        "{\"protocol_version\":2,\"ok\":true,\"operation\":\"subscribe\"}\n",
        "{\"protocol_version\":1,\"ok\":false,\"operation\":\"subscribe\",\"error\":{\"code\":\"denied\",\"message\":\"no\"}}\n",
    ] {
        let path = path();
        let listener = UnixListener::bind(&path).unwrap();
        let server = tokio::spawn(async move {
            let (socket, _) = listener.accept().await.unwrap();
            let (read, mut write) = socket.into_split();
            let mut reader = BufReader::new(read);
            assert!(request(&mut reader).await.contains("subscribe"));
            send(&mut write, response).await;
        });
        let mut stream = ResilientHeartRateStream::explicit_socket(&path, policy());
        assert!(matches!(
            stream.next().await,
            Err(Error::Client(
                ClientError::ProtocolVersion { .. } | ClientError::DaemonError { .. }
            ))
        ));
        assert_eq!(stream.next().await.unwrap(), None);
        server.await.unwrap();
        std::fs::remove_file(path).unwrap();
    }
}

#[tokio::test(start_paused = true)]
async fn default_schedule_exhausts_after_exactly_five_retries() {
    let mut stream = ResilientHeartRateStream::explicit_socket(path(), ReconnectPolicy::default());
    for (index, seconds) in [1, 2, 5, 10, 10].into_iter().enumerate() {
        assert_eq!(
            stream.next().await.unwrap(),
            Some(ResilientHeartRateEvent::Reconnecting {
                attempt: index + 1,
                delay: Duration::from_secs(seconds)
            })
        );
        tokio::time::advance(Duration::from_secs(seconds)).await;
    }
    assert!(matches!(
        stream.next().await,
        Err(Error::RetryExhausted {
            attempts: 5,
            last_error: ClientError::Connect {
                kind: io::ErrorKind::NotFound,
                ..
            }
        })
    ));
    assert_eq!(stream.next().await.unwrap(), None);
}

#[tokio::test]
async fn active_loss_reconnects_and_replaces_subscription() {
    let path = path();
    let listener_a = UnixListener::bind(&path).unwrap();
    let (close_tx, close_rx) = oneshot::channel();
    let server_a = tokio::spawn(async move {
        let (socket, _) = listener_a.accept().await.unwrap();
        let (read, mut write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        send(&mut write, SUB_OK).await;
        send(&mut write, "{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":169,\"source_side\":\"left\"}\n").await;
        close_rx.await.unwrap();
    });
    let mut stream = ResilientHeartRateStream::explicit_socket(&path, policy());
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Sample(HeartRateSample {
            bpm: 169,
            source_side: SourceSide::Left
        }))
    );
    close_tx.send(()).unwrap();
    server_a.await.unwrap();
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Reconnecting {
            attempt: 1,
            delay: Duration::from_millis(50)
        })
    );
    std::fs::remove_file(&path).unwrap();
    let listener_b = UnixListener::bind(&path).unwrap();
    let server_b = tokio::spawn(async move {
        let (socket, _) = listener_b.accept().await.unwrap();
        let (read, mut write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        send(&mut write, SUB_OK).await;
        send(&mut write, "{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":74,\"source_side\":\"unknown\",\"source_side_raw\":37}\n").await;
        assert!(request(&mut reader).await.contains("unsubscribe"));
        send(&mut write, UNSUB_OK).await;
    });
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Reconnected { attempts: 1 })
    );
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Sample(HeartRateSample {
            bpm: 74,
            source_side: SourceSide::Unknown(37)
        }))
    );
    stream.close().await.unwrap();
    server_b.await.unwrap();
    std::fs::remove_file(path).unwrap();
}

#[tokio::test]
async fn cancelled_active_read_preserves_subscription() {
    let path = path();
    let listener = UnixListener::bind(&path).unwrap();
    let (send_tx, send_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let (read, mut write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        send(&mut write, SUB_OK).await;
        send(&mut write, "{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":72,\"source_side\":\"left\"}\n").await;
        send_rx.await.unwrap();
        send(&mut write, "{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":88,\"source_side\":\"right\"}\n").await;
        assert!(request(&mut reader).await.contains("unsubscribe"));
        send(&mut write, UNSUB_OK).await;
    });
    let mut stream = ResilientHeartRateStream::explicit_socket(&path, policy());
    assert!(matches!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Sample(HeartRateSample {
            bpm: 72,
            ..
        }))
    ));
    let mut pending = Box::pin(stream.next());
    tokio::select! { _ = &mut pending => panic!("sample arrived early"), _ = tokio::task::yield_now() => {} }
    drop(pending);
    send_tx.send(()).unwrap();
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Sample(HeartRateSample {
            bpm: 88,
            source_side: SourceSide::Right
        }))
    );
    stream.close().await.unwrap();
    server.await.unwrap();
    std::fs::remove_file(path).unwrap();
}

#[tokio::test]
async fn terminal_consumer_lag_attempts_cleanup_and_keeps_original_error() {
    let path = path();
    let listener = UnixListener::bind(&path).unwrap();
    let (flood_tx, flood_rx) = oneshot::channel();
    let (done_tx, done_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let (read, mut write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        send(&mut write, SUB_OK).await;
        send(&mut write, "{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":72,\"source_side\":\"left\"}\n").await;
        flood_rx.await.unwrap();
        for _ in 0..100 {
            send(&mut write, "{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":88,\"source_side\":\"right\"}\n").await;
        }
        done_tx.send(()).unwrap();
        assert!(request(&mut reader).await.contains("unsubscribe"));
        send(&mut write, "{\"protocol_version\":1,\"ok\":false,\"operation\":\"unsubscribe\",\"error\":{\"code\":\"cleanup_failed\",\"message\":\"cleanup failed\"}}\n").await;
    });
    let mut stream = ResilientHeartRateStream::explicit_socket(&path, policy());
    assert!(matches!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Sample(_))
    ));
    flood_tx.send(()).unwrap();
    done_rx.await.unwrap();
    tokio::time::sleep(Duration::from_millis(10)).await;
    assert!(matches!(
        stream.next().await,
        Err(Error::Client(ClientError::EventLagged { .. }))
    ));
    assert_eq!(stream.next().await.unwrap(), None);
    server.await.unwrap();
    std::fs::remove_file(path).unwrap();
}

#[tokio::test]
async fn cancelled_subscribe_does_not_commit_active_state() {
    let path = path();
    let listener = UnixListener::bind(&path).unwrap();
    let (first_tx, first_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        let (read, _first_write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        first_tx.send(()).unwrap();
        let (socket, _) = listener.accept().await.unwrap();
        let (read, mut write) = socket.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        send(&mut write, SUB_OK).await;
        send(&mut write, "{\"protocol_version\":1,\"event\":\"heart_rate\",\"bpm\":91,\"source_side\":\"right\"}\n").await;
        assert!(request(&mut reader).await.contains("unsubscribe"));
        send(&mut write, UNSUB_OK).await;
    });
    let mut stream = ResilientHeartRateStream::explicit_socket(&path, policy());
    let mut pending = Box::pin(stream.next());
    tokio::select! { _ = &mut pending => panic!("subscribe completed without response"), _ = first_rx => {} }
    drop(pending);
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Sample(HeartRateSample {
            bpm: 91,
            source_side: SourceSide::Right
        }))
    );
    stream.close().await.unwrap();
    server.await.unwrap();
    std::fs::remove_file(path).unwrap();
}

#[tokio::test]
async fn cancelled_in_flight_retry_keeps_attempt_number() {
    let path = path();
    let policy = ReconnectPolicy::new(vec![Duration::from_millis(1)]).unwrap();
    let mut stream = ResilientHeartRateStream::explicit_socket(&path, policy);
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Reconnecting {
            attempt: 1,
            delay: Duration::from_millis(1)
        })
    );
    let listener = UnixListener::bind(&path).unwrap();
    let (first_tx, first_rx) = oneshot::channel();
    let server = tokio::spawn(async move {
        let (first, _) = listener.accept().await.unwrap();
        let (read, _write) = first.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        first_tx.send(()).unwrap();
        let (second, _) = listener.accept().await.unwrap();
        let (read, mut write) = second.into_split();
        let mut reader = BufReader::new(read);
        assert!(request(&mut reader).await.contains("subscribe"));
        send(&mut write, SUB_OK).await;
        assert!(request(&mut reader).await.contains("unsubscribe"));
        send(&mut write, UNSUB_OK).await;
    });
    let mut pending = Box::pin(stream.next());
    tokio::select! { _ = &mut pending => panic!("retry completed before subscription response"), _ = first_rx => {} }
    drop(pending);
    assert_eq!(
        stream.next().await.unwrap(),
        Some(ResilientHeartRateEvent::Reconnected { attempts: 1 })
    );
    stream.close().await.unwrap();
    server.await.unwrap();
    std::fs::remove_file(path).unwrap();
}
