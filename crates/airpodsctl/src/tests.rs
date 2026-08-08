use super::*;
use serde_json::{Value, json};
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixListener;
use tokio::time::{Duration, timeout};

#[test]
fn cli_parses_commands_and_global_option_positions() {
    for command in ["hello", "ping", "status", "watch"] {
        assert!(Cli::try_parse_from(["airpodsctl", command]).is_ok());
        for args in [
            vec!["airpodsctl", "--json", command],
            vec!["airpodsctl", command, "--json"],
            vec!["airpodsctl", "--socket", "/tmp/example.sock", command],
            vec!["airpodsctl", command, "--socket", "/tmp/example.sock"],
        ] {
            assert!(Cli::try_parse_from(args).is_ok());
        }
    }
    assert!(matches!(
        Cli::try_parse_from(["airpodsctl", "watch", "--count", "10"]),
        Ok(Cli {
            command: Command::Watch {
                count: Some(_),
                reconnect: false
            },
            ..
        })
    ));
    assert!(Cli::try_parse_from(["airpodsctl", "watch", "--count", "0"]).is_err());
    assert!(Cli::try_parse_from(["airpodsctl", "watch", "--count", "-1"]).is_err());
    assert!(matches!(
        Cli::try_parse_from(["airpodsctl", "watch", "--reconnect"]),
        Ok(Cli {
            command: Command::Watch {
                reconnect: true,
                ..
            },
            ..
        })
    ));
    for command in ["hello", "ping", "status"] {
        assert!(Cli::try_parse_from(["airpodsctl", command, "--reconnect"]).is_err());
    }
}

#[test]
fn human_rendering_covers_every_state_and_source() {
    let states = [
        (DaemonState::Stopped, "stopped"),
        (DaemonState::Starting, "starting"),
        (DaemonState::Ready, "ready"),
        (DaemonState::StartingHeartRate, "starting_hr"),
        (DaemonState::Streaming, "streaming"),
        (DaemonState::StoppingHeartRate, "stopping_hr"),
        (DaemonState::Failed, "failed"),
        (DaemonState::ShuttingDown, "shutting_down"),
        (
            DaemonState::Unknown("future_state".to_owned()),
            "future_state",
        ),
    ];
    for (state, expected) in states {
        assert_eq!(
            render_status(
                &Status {
                    state,
                    subscriber_count: 0
                },
                false
            ),
            format!("state: {expected}\nsubscribers: 0")
        );
    }
    assert_eq!(
        render_hello(
            &Hello {
                service: "custom".to_owned(),
                experimental: false
            },
            false
        ),
        "service: custom\nexperimental: false"
    );
    assert_eq!(render_ping(false), "pong");
    for (side, expected) in [
        (SourceSide::Left, "169 bpm (left)"),
        (SourceSide::Right, "169 bpm (right)"),
        (SourceSide::Unknown(37), "169 bpm (unknown(37))"),
    ] {
        assert_eq!(
            render_sample(
                HeartRateSample {
                    bpm: 169,
                    source_side: side
                },
                false
            ),
            expected
        );
    }
}

#[test]
fn json_rendering_has_exact_semantic_content() {
    assert_eq!(
        serde_json::from_str::<Value>(&render_hello(
            &Hello {
                service: "hub".to_owned(),
                experimental: true
            },
            true
        ))
        .unwrap(),
        json!({"service":"hub","experimental":true})
    );
    assert_eq!(
        serde_json::from_str::<Value>(&render_ping(true)).unwrap(),
        json!({"pong":true})
    );
    assert_eq!(
        serde_json::from_str::<Value>(&render_status(
            &Status {
                state: DaemonState::Ready,
                subscriber_count: 3
            },
            true
        ))
        .unwrap(),
        json!({"state":"ready","subscriber_count":3})
    );
    for (side, expected) in [
        (SourceSide::Left, json!({"bpm":169,"source_side":"left"})),
        (SourceSide::Right, json!({"bpm":169,"source_side":"right"})),
        (
            SourceSide::Unknown(37),
            json!({"bpm":169,"source_side":"unknown","source_side_raw":37}),
        ),
    ] {
        let rendered = render_sample(
            HeartRateSample {
                bpm: 169,
                source_side: side,
            },
            true,
        );
        assert!(!rendered.contains('\n'));
        assert_eq!(serde_json::from_str::<Value>(&rendered).unwrap(), expected);
    }
}

static NEXT_SOCKET: AtomicU64 = AtomicU64::new(0);

fn socket_path() -> PathBuf {
    std::env::temp_dir().join(format!(
        "airpodsctl-test-{}-{}.sock",
        std::process::id(),
        NEXT_SOCKET.fetch_add(1, Ordering::Relaxed)
    ))
}

async fn read_request(reader: &mut BufReader<tokio::net::unix::OwnedReadHalf>) -> Value {
    let mut line = String::new();
    timeout(Duration::from_secs(3), reader.read_line(&mut line))
        .await
        .unwrap()
        .unwrap();
    serde_json::from_str(&line).unwrap()
}

async fn respond(writer: &mut tokio::net::unix::OwnedWriteHalf, value: Value) {
    writer
        .write_all(format!("{value}\n").as_bytes())
        .await
        .unwrap();
}

async fn one_command(command: &str, response: Value) -> Value {
    let path = socket_path();
    let listener = UnixListener::bind(&path).unwrap();
    let name = command.to_owned();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let (read, mut write) = stream.into_split();
        let mut reader = BufReader::new(read);
        let request = read_request(&mut reader).await;
        assert_eq!(request["operation"], name);
        respond(&mut write, response).await;
        let mut extra = String::new();
        timeout(Duration::from_secs(3), reader.read_line(&mut extra))
            .await
            .unwrap()
            .unwrap();
        assert!(extra.is_empty(), "unexpected second request: {extra}");
        request
    });
    let cli =
        Cli::try_parse_from(["airpodsctl", command, "--socket", path.to_str().unwrap()]).unwrap();
    assert_eq!(run(cli).await.unwrap(), 0);
    let request = server.await.unwrap();
    std::fs::remove_file(path).unwrap();
    request
}

#[tokio::test]
async fn fake_server_hello_only_sends_hello() {
    assert_eq!(one_command("hello", json!({"protocol_version":1,"ok":true,"operation":"hello","service":"airpods-hubd","experimental":true})).await, json!({"protocol_version":1,"operation":"hello"}));
}

#[tokio::test]
async fn fake_server_ping_only_sends_ping() {
    assert_eq!(
        one_command(
            "ping",
            json!({"protocol_version":1,"ok":true,"operation":"ping","pong":true})
        )
        .await,
        json!({"protocol_version":1,"operation":"ping"})
    );
}

#[tokio::test]
async fn fake_server_status_maps_daemon_response() {
    assert_eq!(one_command("status", json!({"protocol_version":1,"ok":true,"operation":"status","state":"streaming","subscriber_count":4})).await, json!({"protocol_version":1,"operation":"status"}));
}

#[tokio::test]
async fn fake_server_watch_counts_and_unsubscribes() {
    let path = socket_path();
    let listener = UnixListener::bind(&path).unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let (read, mut write) = stream.into_split();
        let mut reader = BufReader::new(read);
        assert_eq!(
            read_request(&mut reader).await,
            json!({"protocol_version":1,"operation":"subscribe","stream":"heart_rate"})
        );
        respond(&mut write, json!({"protocol_version":1,"ok":true,"operation":"subscribe","stream":"heart_rate","subscribed":true,"already_subscribed":false})).await;
        for (bpm, side) in [(169, "left"), (169, "left"), (72, "right")] {
            respond(
                &mut write,
                json!({"protocol_version":1,"event":"heart_rate","bpm":bpm,"source_side":side}),
            )
            .await;
        }
        assert_eq!(
            read_request(&mut reader).await,
            json!({"protocol_version":1,"operation":"unsubscribe","stream":"heart_rate"})
        );
        respond(&mut write, json!({"protocol_version":1,"ok":true,"operation":"unsubscribe","stream":"heart_rate","subscribed":false,"already_unsubscribed":false})).await;
        let mut extra = String::new();
        timeout(Duration::from_secs(3), reader.read_line(&mut extra))
            .await
            .unwrap()
            .unwrap();
        assert!(extra.is_empty());
    });
    let cli = Cli::try_parse_from([
        "airpodsctl",
        "watch",
        "--json",
        "--count",
        "2",
        "--socket",
        path.to_str().unwrap(),
    ])
    .unwrap();
    assert_eq!(run(cli).await.unwrap(), 0);
    server.await.unwrap();
    std::fs::remove_file(path).unwrap();
}

#[tokio::test]
async fn daemon_error_is_runtime_failure() {
    let path = socket_path();
    let listener = UnixListener::bind(&path).unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let (read, mut write) = stream.into_split();
        let mut reader = BufReader::new(read);
        let _ = read_request(&mut reader).await;
        respond(&mut write, json!({"protocol_version":1,"ok":false,"operation":"ping","error":{"code":"nope","message":"unavailable"}})).await;
    });
    let cli =
        Cli::try_parse_from(["airpodsctl", "ping", "--socket", path.to_str().unwrap()]).unwrap();
    assert!(run(cli).await.unwrap_err().contains("unavailable"));
    server.await.unwrap();
    std::fs::remove_file(path).unwrap();
}

#[tokio::test]
async fn absent_socket_is_clean_error() {
    let path = socket_path();
    let cli =
        Cli::try_parse_from(["airpodsctl", "status", "--socket", path.to_str().unwrap()]).unwrap();
    assert!(run(cli).await.unwrap_err().contains("could not connect"));
}

#[tokio::test]
async fn resilient_retry_exhaustion_is_a_runtime_failure() {
    let path = socket_path();
    let policy = ReconnectPolicy::new(vec![Duration::from_millis(1)]).unwrap();
    let error = watch_resilient_with_policy(Some(path), true, None, true, policy)
        .await
        .unwrap_err();
    assert!(error.contains("exhausted after 1 attempt"));
}

#[test]
fn resilient_exhaustion_masks_default_socket_path() {
    let private_path = PathBuf::from("/distinctive/private/review-only.sock");
    let rendered = resilient_error(
        airpods_client_resilient::Error::RetryExhausted {
            attempts: 5,
            last_error: airpods_client::Error::Connect {
                path: private_path.clone(),
                kind: std::io::ErrorKind::ConnectionRefused,
                message: "connection refused".to_owned(),
            },
        },
        false,
    );
    assert!(rendered.contains("daemon reconnect exhausted after 5 attempts"));
    assert!(
        rendered.contains("could not connect to default airpods-hubd socket: connection refused")
    );
    assert!(!rendered.contains(private_path.to_str().unwrap()));
}

#[test]
fn resilient_exhaustion_keeps_explicit_socket_path() {
    let private_path = PathBuf::from("/distinctive/private/review-only.sock");
    let rendered = resilient_error(
        airpods_client_resilient::Error::RetryExhausted {
            attempts: 5,
            last_error: airpods_client::Error::Connect {
                path: private_path.clone(),
                kind: std::io::ErrorKind::ConnectionRefused,
                message: "connection refused".to_owned(),
            },
        },
        true,
    );
    assert!(rendered.contains("daemon reconnect exhausted after 5 attempts"));
    assert!(rendered.contains(private_path.to_str().unwrap()));
}

#[tokio::test]
async fn resilient_protocol_failure_does_not_retry() {
    let path = socket_path();
    let listener = UnixListener::bind(&path).unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let (read, mut write) = stream.into_split();
        let mut reader = BufReader::new(read);
        assert_eq!(read_request(&mut reader).await["operation"], "subscribe");
        respond(
            &mut write,
            json!({"protocol_version":2,"ok":true,"operation":"subscribe"}),
        )
        .await;
        let mut extra = String::new();
        timeout(Duration::from_secs(3), reader.read_line(&mut extra))
            .await
            .unwrap()
            .unwrap();
        assert!(extra.is_empty());
    });
    let cli = Cli::try_parse_from([
        "airpodsctl",
        "watch",
        "--reconnect",
        "--socket",
        path.to_str().unwrap(),
    ])
    .unwrap();
    assert!(
        run(cli)
            .await
            .unwrap_err()
            .contains("unsupported daemon protocol version")
    );
    server.await.unwrap();
    std::fs::remove_file(path).unwrap();
}
