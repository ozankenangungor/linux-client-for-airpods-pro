#![forbid(unsafe_code)]

use airpods_client::{
    AirPodsClient, DaemonState, Error, HeartRateSample, MAX_FRAME_SIZE, SourceSide,
};
use serde_json::{Value, json};
use std::future::Future;
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::task::JoinHandle;

static NEXT_SOCKET: AtomicU64 = AtomicU64::new(0);

struct TestServer {
    path: PathBuf,
    task: JoinHandle<()>,
}

impl TestServer {
    async fn finish(self) {
        self.task.await.unwrap();
        let _ = std::fs::remove_file(self.path);
    }
}

async fn spawn_server<F, Fut>(handler: F) -> TestServer
where
    F: FnOnce(UnixStream) -> Fut + Send + 'static,
    Fut: Future<Output = ()> + Send + 'static,
{
    let serial = NEXT_SOCKET.fetch_add(1, Ordering::Relaxed);
    let path = std::env::temp_dir().join(format!(
        "airpods-client-{}-{serial}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);
    let listener = UnixListener::bind(&path).unwrap();
    let task = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        handler(stream).await;
    });
    TestServer { path, task }
}

async fn read_request(reader: &mut BufReader<tokio::net::unix::OwnedReadHalf>) -> Value {
    let mut line = Vec::new();
    assert!(reader.read_until(b'\n', &mut line).await.unwrap() > 0);
    serde_json::from_slice(&line).unwrap()
}

async fn write_value(writer: &mut tokio::net::unix::OwnedWriteHalf, value: Value) {
    let mut frame = serde_json::to_vec(&value).unwrap();
    frame.push(b'\n');
    writer.write_all(&frame).await.unwrap();
}

fn success(operation: &str) -> Value {
    json!({"protocol_version": 1, "ok": true, "operation": operation})
}

#[test]
fn default_socket_derives_from_xdg_runtime_dir() {
    const CHILD: &str = "AIRPODS_CLIENT_XDG_CHILD";
    if std::env::var_os(CHILD).is_some() {
        assert_eq!(
            AirPodsClient::default_socket_path().unwrap(),
            PathBuf::from("/runtime/test-user/airpods-hubd.sock")
        );
        return;
    }
    let status = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "default_socket_derives_from_xdg_runtime_dir"])
        .env(CHILD, "1")
        .env("XDG_RUNTIME_DIR", "/runtime/test-user")
        .status()
        .unwrap();
    assert!(status.success());
}

#[test]
fn missing_xdg_runtime_dir_is_clear() {
    const CHILD: &str = "AIRPODS_CLIENT_NO_XDG_CHILD";
    if std::env::var_os(CHILD).is_some() {
        assert_eq!(
            AirPodsClient::default_socket_path().unwrap_err(),
            Error::XdgRuntimeDirMissing
        );
        return;
    }
    let status = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "missing_xdg_runtime_dir_is_clear"])
        .env(CHILD, "1")
        .env_remove("XDG_RUNTIME_DIR")
        .status()
        .unwrap();
    assert!(status.success());
}

#[tokio::test]
async fn explicit_socket_hello_and_ping_work() {
    let server = spawn_server(|stream| async move {
        let (reader, mut writer) = stream.into_split();
        let mut reader = BufReader::new(reader);
        assert_eq!(read_request(&mut reader).await["operation"], "hello");
        write_value(
            &mut writer,
            json!({
                "protocol_version": 1,
                "ok": true,
                "operation": "hello",
                "service": "airpods-hubd",
                "experimental": true
            }),
        )
        .await;
        assert_eq!(read_request(&mut reader).await["operation"], "ping");
        let mut response = success("ping");
        response["pong"] = json!(true);
        write_value(&mut writer, response).await;
    })
    .await;

    let client = AirPodsClient::connect_to(&server.path).await.unwrap();
    let hello = client.hello().await.unwrap();
    assert_eq!(hello.service, "airpods-hubd");
    assert!(hello.experimental);
    client.ping().await.unwrap();
    drop(client);
    server.finish().await;
}

#[tokio::test]
async fn ready_and_streaming_status_parse() {
    let server = spawn_server(|stream| async move {
        let (reader, mut writer) = stream.into_split();
        let mut reader = BufReader::new(reader);
        for (state, subscribers) in [("ready", 0), ("streaming", 2)] {
            assert_eq!(read_request(&mut reader).await["operation"], "status");
            write_value(
                &mut writer,
                json!({
                    "protocol_version": 1,
                    "ok": true,
                    "operation": "status",
                    "state": state,
                    "subscriber_count": subscribers
                }),
            )
            .await;
        }
    })
    .await;

    let client = AirPodsClient::connect_to(&server.path).await.unwrap();
    assert_eq!(client.status().await.unwrap().state, DaemonState::Ready);
    let status = client.status().await.unwrap();
    assert_eq!(status.state, DaemonState::Streaming);
    assert_eq!(status.subscriber_count, 2);
    drop(client);
    server.finish().await;
}

#[tokio::test]
async fn events_interleave_with_response_and_preserve_values_and_order() {
    let server = spawn_server(|stream| async move {
        let (reader, mut writer) = stream.into_split();
        let mut reader = BufReader::new(reader);
        let request = read_request(&mut reader).await;
        assert_eq!(request["operation"], "subscribe");
        assert_eq!(request["stream"], "heart_rate");

        write_value(
            &mut writer,
            json!({"protocol_version":1,"event":"heart_rate","bpm":169,"source_side":"left"}),
        )
        .await;
        write_value(
            &mut writer,
            json!({
                "protocol_version":1,"ok":true,"operation":"subscribe",
                "stream":"heart_rate","subscribed":true,"already_subscribed":false
            }),
        )
        .await;
        for event in [
            json!({"protocol_version":1,"event":"heart_rate","bpm":169,"source_side":"left"}),
            json!({"protocol_version":1,"event":"heart_rate","bpm":91,"source_side":"right"}),
            json!({"protocol_version":1,"event":"heart_rate","bpm":72,"source_side":"unknown","source_side_raw":37}),
        ] {
            write_value(&mut writer, event).await;
        }

        let request = read_request(&mut reader).await;
        assert_eq!(request["operation"], "unsubscribe");
        write_value(
            &mut writer,
            json!({
                "protocol_version":1,"ok":true,"operation":"unsubscribe",
                "stream":"heart_rate","subscribed":false,"already_unsubscribed":false
            }),
        )
        .await;
        assert_eq!(read_request(&mut reader).await["operation"], "status");
        write_value(
            &mut writer,
            json!({
                "protocol_version":1,"ok":true,"operation":"status",
                "state":"ready","subscriber_count":0
            }),
        )
        .await;
    })
    .await;

    let client = AirPodsClient::connect_to(&server.path).await.unwrap();
    let mut subscription = client.subscribe_heart_rate().await.unwrap();
    let mut samples = Vec::new();
    for _ in 0..4 {
        samples.push(subscription.next().await.unwrap().unwrap());
    }
    assert_eq!(
        samples,
        vec![
            HeartRateSample {
                bpm: 169,
                source_side: SourceSide::Left
            },
            HeartRateSample {
                bpm: 169,
                source_side: SourceSide::Left
            },
            HeartRateSample {
                bpm: 91,
                source_side: SourceSide::Right
            },
            HeartRateSample {
                bpm: 72,
                source_side: SourceSide::Unknown(37)
            },
        ]
    );
    subscription.unsubscribe().await.unwrap();
    assert_eq!(client.status().await.unwrap().state, DaemonState::Ready);
    drop(client);
    server.finish().await;
}

#[tokio::test]
async fn connection_closure_terminates_subscription() {
    let server = spawn_server(|stream| async move {
        let (reader, mut writer) = stream.into_split();
        let mut reader = BufReader::new(reader);
        assert_eq!(read_request(&mut reader).await["operation"], "subscribe");
        write_value(
            &mut writer,
            json!({
                "protocol_version":1,"ok":true,"operation":"subscribe",
                "stream":"heart_rate","subscribed":true,"already_subscribed":false
            }),
        )
        .await;
    })
    .await;
    let client = AirPodsClient::connect_to(&server.path).await.unwrap();
    let mut subscription = client.subscribe_heart_rate().await.unwrap();
    assert_eq!(
        subscription.next().await.unwrap_err(),
        Error::ConnectionClosed
    );
    assert_eq!(subscription.next().await.unwrap(), None);
    drop(client);
    server.finish().await;
}

async fn request_receives_frame(frame: Vec<u8>) -> Error {
    let server = spawn_server(move |stream| async move {
        let (reader, mut writer) = stream.into_split();
        let mut reader = BufReader::new(reader);
        read_request(&mut reader).await;
        writer.write_all(&frame).await.unwrap();
    })
    .await;
    let client = AirPodsClient::connect_to(&server.path).await.unwrap();
    let error = client.ping().await.unwrap_err();
    drop(client);
    server.finish().await;
    error
}

#[tokio::test]
async fn malformed_json_is_rejected_from_socket() {
    assert!(matches!(
        request_receives_frame(b"not-json\n".to_vec()).await,
        Error::InvalidJson { .. }
    ));
}

#[tokio::test]
async fn non_object_response_is_rejected_from_socket() {
    assert!(matches!(
        request_receives_frame(b"[]\n".to_vec()).await,
        Error::UnexpectedMessage { .. }
    ));
}

#[tokio::test]
async fn oversized_frame_is_rejected_from_socket() {
    let mut frame = vec![b'x'; MAX_FRAME_SIZE + 1];
    frame.push(b'\n');
    assert_eq!(
        request_receives_frame(frame).await,
        Error::FrameTooLarge {
            limit: MAX_FRAME_SIZE
        }
    );
}

#[tokio::test]
async fn unsupported_version_is_rejected_from_socket() {
    assert_eq!(
        request_receives_frame(b"{\"protocol_version\":2,\"ok\":true}\n".to_vec()).await,
        Error::ProtocolVersion {
            expected: 1,
            received: Some(2)
        }
    );
}

#[tokio::test]
async fn unexpected_response_shape_is_rejected() {
    assert!(matches!(
        request_receives_frame(
            b"{\"protocol_version\":1,\"ok\":true,\"operation\":\"ping\",\"pong\":\"yes\"}\n"
                .to_vec()
        )
        .await,
        Error::UnexpectedMessage { .. }
    ));
}

#[tokio::test]
async fn daemon_error_maps_to_typed_error() {
    assert_eq!(
        request_receives_frame(
            b"{\"protocol_version\":1,\"ok\":false,\"error\":{\"code\":\"service_failed\",\"message\":\"sensor service is unavailable\"}}\n"
                .to_vec()
        )
        .await,
        Error::DaemonError {
            code: "service_failed".to_owned(),
            message: "sensor service is unavailable".to_owned()
        }
    );
}

#[tokio::test]
async fn pending_request_ends_when_daemon_disconnects() {
    assert_eq!(
        request_receives_frame(Vec::new()).await,
        Error::ConnectionClosed
    );
}

#[tokio::test]
async fn absent_daemon_is_only_a_connection_error() {
    let serial = NEXT_SOCKET.fetch_add(1, Ordering::Relaxed);
    let path = std::env::temp_dir().join(format!(
        "airpods-client-absent-{}-{serial}.sock",
        std::process::id()
    ));
    let _ = std::fs::remove_file(&path);
    assert!(matches!(
        AirPodsClient::connect_to(&path).await,
        Err(Error::Connect { .. })
    ));
    assert!(!path.exists());
}

#[test]
fn crate_has_only_local_ipc_dependencies() {
    let manifest = include_str!("../Cargo.toml");
    assert!(manifest.contains("serde_json"));
    assert!(manifest.contains("tokio"));
    for forbidden in [
        "bluez",
        "dbus",
        "bluetooth",
        "bumble",
        "pyo3",
        "airpods-aap-core",
    ] {
        assert!(!manifest.to_ascii_lowercase().contains(forbidden));
    }
}

#[test]
fn source_cannot_start_daemons_or_open_sensor_paths() {
    let source = include_str!("../src/lib.rs");
    for forbidden in [
        "Command::new",
        "systemctl",
        "AF_BLUETOOTH",
        "ProductionHeartRateSession",
        "production_session",
        "bluez_coexistence",
        "airpods_aap_core",
    ] {
        assert!(!source.contains(forbidden));
    }
    assert_eq!(source.matches("UnixStream::connect").count(), 1);
}
