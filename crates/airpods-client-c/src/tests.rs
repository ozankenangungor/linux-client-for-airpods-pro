use crate::error::Error;
use crate::ffi::*;
use crate::model::{Hello, HrSample, Status, StringView};
use crate::worker::{Client, Connect, Operation, Reply, Wait};
use airpods_client::{DaemonState, HeartRateSample, SourceSide};
use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Write};
use std::mem::{align_of, offset_of, size_of};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread::{self, JoinHandle};
use std::time::Duration;

#[test]
fn pod_layout() {
    assert_eq!(size_of::<StringView>(), 2 * size_of::<usize>());
    assert_eq!(align_of::<StringView>(), align_of::<usize>());
    assert_eq!(offset_of!(StringView, data), 0);
    assert_eq!(offset_of!(StringView, len), size_of::<usize>());
    assert_eq!(size_of::<HrSample>(), 16);
    assert_eq!(align_of::<HrSample>(), align_of::<u32>());
    assert_eq!(offset_of!(HrSample, bpm), 0);
    assert_eq!(offset_of!(HrSample, source_side), 4);
    assert_eq!(offset_of!(HrSample, source_side_raw), 8);
    assert_eq!(offset_of!(HrSample, reserved), 12);
}

#[test]
fn versions_delegate() {
    assert_eq!(airpods_client_c_abi_version(), 1);
    assert_eq!(
        airpods_client_protocol_version(),
        airpods_client::PROTOCOL_VERSION
    );
}

#[test]
fn all_sdk_error_kinds_and_daemon_codes() {
    use airpods_client::Error as E;
    let variants = [
        (E::XdgRuntimeDirMissing, 10),
        (
            E::Connect {
                path: "/explicit".into(),
                kind: std::io::ErrorKind::NotFound,
                message: "missing".into(),
            },
            11,
        ),
        (
            E::Io {
                context: "reading",
                kind: std::io::ErrorKind::BrokenPipe,
                message: "closed".into(),
            },
            12,
        ),
        (E::FrameTooLarge { limit: 4096 }, 13),
        (
            E::InvalidJson {
                message: "invalid".into(),
            },
            14,
        ),
        (
            E::ProtocolVersion {
                expected: 1,
                received: Some(2),
            },
            15,
        ),
        (
            E::UnexpectedMessage {
                message: "unexpected".into(),
            },
            16,
        ),
        (
            E::DaemonError {
                code: "future\0code".into(),
                message: "safe\0message".into(),
            },
            17,
        ),
        (E::ConnectionClosed, 18),
        (E::SubscriptionActive, 19),
        (E::EventLagged { skipped: 37 }, 20),
    ];
    for (sdk, expected) in variants {
        let error = Box::into_raw(Box::new(Error::sdk(sdk, false)));
        // SAFETY: error is a fresh ABI-compatible allocation; getters borrow
        // only until free, which consumes it exactly once.
        unsafe {
            assert_eq!(airpods_error_kind(error), expected);
            let code = airpods_error_daemon_code(error);
            if expected == 17 {
                assert_eq!(
                    std::slice::from_raw_parts(code.data.cast::<u8>(), code.len),
                    b"future\0code"
                );
                let message = airpods_error_message(error);
                assert!(
                    std::slice::from_raw_parts(message.data.cast::<u8>(), message.len).contains(&0)
                );
            } else {
                assert!(code.data.is_null());
                assert_eq!(code.len, 0);
            }
            airpods_error_free(error);
        }
    }
}

#[test]
fn default_path_masked_explicit_path_retained() {
    let make = || airpods_client::Error::Connect {
        path: "/private-looking-task11-4/runtime/airpods-hubd.sock".into(),
        kind: std::io::ErrorKind::NotFound,
        message: "No such file or directory (os error 2)".into(),
    };
    let default = Error::sdk(make(), true);
    assert_eq!(default.kind, 11);
    assert_eq!(
        default.message,
        "could not connect to default airpods-hubd socket: No such file or directory (os error 2)"
    );
    assert!(!default.message.contains("private-looking"));
    assert!(
        Error::sdk(make(), false)
            .message
            .contains("/private-looking-task11-4/runtime/airpods-hubd.sock")
    );
}

#[test]
fn hello_conversion_preserves_bytes_and_boolean() {
    for flag in [false, true] {
        let hello = Hello::from(airpods_client::Hello {
            service: "service\0名".into(),
            experimental: flag,
        });
        assert_eq!(hello.service, "service\0名");
        assert_eq!(hello.experimental, u32::from(flag));
    }
}

#[test]
fn status_conversion_all_states() {
    let known = [
        DaemonState::Stopped,
        DaemonState::Starting,
        DaemonState::Ready,
        DaemonState::StartingHeartRate,
        DaemonState::Streaming,
        DaemonState::StoppingHeartRate,
        DaemonState::Failed,
        DaemonState::ShuttingDown,
    ];
    for (code, state) in known.into_iter().enumerate() {
        let status = Status::from(airpods_client::Status {
            state,
            subscriber_count: u64::MAX,
        });
        assert_eq!(status.state, code as u32);
        assert_eq!(status.subscriber_count, u64::MAX);
        assert!(status.unknown_state.is_none());
    }
    let unknown = Status::from(airpods_client::Status {
        state: DaemonState::Unknown("future_state\0名".into()),
        subscriber_count: 169,
    });
    assert_eq!(unknown.state, 255);
    assert_eq!(unknown.unknown_state.as_deref(), Some("future_state\0名"));
    assert_eq!(unknown.subscriber_count, 169);
}

#[test]
fn sample_conversion_exact() {
    for (bpm, side, expected, raw) in [
        (169, SourceSide::Left, 1, 0),
        (88, SourceSide::Right, 2, 0),
        (74, SourceSide::Unknown(37), 0, 37),
    ] {
        assert_eq!(
            HrSample::from(HeartRateSample {
                bpm,
                source_side: side
            }),
            HrSample {
                bpm: u32::from(bpm),
                source_side: expected,
                source_side_raw: raw,
                reserved: 0
            }
        );
    }
}

#[test]
fn null_arguments_and_outputs_are_defensive() {
    let mut error = std::ptr::null_mut();
    use std::ptr::null_mut as null;
    // SAFETY: only NULL arguments and live local output slots are passed. Each
    // error returned is freed before the next call; no dangling pointer is used.
    unsafe {
        assert_eq!(airpods_client_connect(null(), &mut error), ERROR);
        assert_eq!(airpods_error_kind(error), 1);
        airpods_error_free(error);
        let mut client = std::ptr::null_mut();
        assert_eq!(
            airpods_client_connect_to(std::ptr::null(), &mut client, &mut error),
            ERROR
        );
        assert!(client.is_null());
        assert_eq!(airpods_error_kind(error), 1);
        airpods_error_free(error);
        assert_eq!(airpods_client_hello(null(), null(), &mut error), ERROR);
        assert_eq!(airpods_error_kind(error), 1);
        airpods_error_free(error);
        assert_eq!(airpods_client_status(null(), null(), &mut error), ERROR);
        assert_eq!(airpods_error_kind(error), 1);
        airpods_error_free(error);
        assert_eq!(airpods_client_hr_next(null(), 0, null(), &mut error), ERROR);
        assert_eq!(airpods_error_kind(error), 1);
        airpods_error_free(error);
        let mut sample = HrSample {
            bpm: 999,
            source_side: 999,
            source_side_raw: 999,
            reserved: 999,
        };
        assert_eq!(
            airpods_client_hr_next(null(), 0, &mut sample, std::ptr::null_mut()),
            ERROR
        );
        assert_eq!(sample, HrSample::default());
        airpods_client_free(null());
        airpods_error_free(null());
        airpods_hello_free(null());
        airpods_status_free(null());
        assert_eq!(airpods_error_kind(null()), 255);
        assert!(airpods_error_message(null()).data.is_null());
        assert!(airpods_error_daemon_code(null()).data.is_null());
        assert!(airpods_hello_service(null()).data.is_null());
        assert_eq!(airpods_hello_experimental(null()), 0);
        assert_eq!(airpods_status_state(null()), 255);
        assert_eq!(airpods_status_subscriber_count(null()), 0);
        assert!(airpods_status_unknown_state(null()).data.is_null());
    }
}

#[test]
fn panic_boundary_returns_internal() {
    let (result, error) = panic_result_for_test();
    assert_eq!(result, ERROR);
    assert_eq!(error.kind, 255);
    assert_eq!(error.message, "internal C client failure");
    assert!(error.daemon_code.is_none());
    assert_eq!(guarded(37, || panic!("getter test")), 37);
    guarded((), || panic!("destructor test"));
}

#[test]
fn panic_payload_destructor_cannot_escape() {
    struct BadDrop;
    impl Drop for BadDrop {
        fn drop(&mut self) {
            panic!("payload Drop must not run");
        }
    }
    assert_eq!(guarded(255, || std::panic::panic_any(BadDrop)), 255);
}

struct Wire {
    read: BufReader<UnixStream>,
    write: UnixStream,
}

impl Wire {
    fn receive(&mut self) -> Option<Value> {
        let mut line = String::new();
        match self.read.read_line(&mut line).unwrap() {
            0 => None,
            _ => Some(serde_json::from_str(&line).unwrap()),
        }
    }
    fn expect(&mut self, operation: &str) {
        assert_eq!(self.receive().unwrap()["operation"], operation);
    }
    fn send(&mut self, mut value: Value) {
        value["protocol_version"] = json!(airpods_client::PROTOCOL_VERSION);
        writeln!(self.write, "{value}").unwrap();
    }
    fn subscribe(&mut self) {
        self.expect("subscribe");
        self.send(json!({"operation":"subscribe","ok":true,"stream":"heart_rate","subscribed":true,"already_subscribed":false}));
    }
    fn unsubscribe(&mut self) {
        self.expect("unsubscribe");
        self.send(json!({"operation":"unsubscribe","ok":true,"stream":"heart_rate","subscribed":false,"already_unsubscribed":false}));
    }
    fn ping(&mut self) {
        self.expect("ping");
        self.send(json!({"operation":"ping","ok":true,"pong":true}));
    }
}

struct Server {
    path: PathBuf,
    join: Option<JoinHandle<()>>,
}

impl Server {
    fn new(script: impl FnOnce(Wire) + Send + 'static) -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let dir = std::env::temp_dir().join(format!(
            "ap-c-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&dir).unwrap();
        let path = dir.join("socket");
        Self::at(path, script)
    }
    fn at(path: PathBuf, script: impl FnOnce(Wire) + Send + 'static) -> Self {
        let listener = UnixListener::bind(&path).unwrap();
        let join = thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            stream
                .set_write_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            script(Wire {
                read: BufReader::new(stream.try_clone().unwrap()),
                write: stream,
            });
        });
        Self {
            path,
            join: Some(join),
        }
    }
    fn client(&self) -> Client {
        Client::connect(Connect::Explicit(self.path.clone())).unwrap()
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        let joined = self.join.take().unwrap().join();
        std::fs::remove_dir_all(self.path.parent().unwrap()).unwrap();
        if !thread::panicking() {
            joined.unwrap();
        }
    }
}

#[test]
fn close_idempotent_joins_and_rejects_all_operations() {
    let server = Server::new(|mut wire| {
        wire.ping();
        wire.subscribe();
        wire.unsubscribe();
        assert!(wire.receive().is_none());
    });
    let client = server.client();
    client.request(Operation::Ping).unwrap();
    client.request(Operation::Subscribe).unwrap();
    client.close().unwrap();
    assert!(client.joined());
    client.close().unwrap();
    for operation in [
        Operation::Ping,
        Operation::Hello,
        Operation::Status,
        Operation::Subscribe,
        Operation::Unsubscribe,
        Operation::Next(Wait::milliseconds(0)),
    ] {
        assert_eq!(client.request(operation).err().unwrap().kind, 2);
    }
    drop(client);
}

#[test]
fn close_failure_preserves_daemon_category_and_joins() {
    let server = Server::new(|mut wire| {
        wire.subscribe();
        wire.expect("unsubscribe");
        wire.send(json!({"operation":"unsubscribe","ok":false,"error":{"code":"test_stop","message":"stop failed"}}));
        assert!(wire.receive().is_none());
    });
    let client = server.client();
    client.request(Operation::Subscribe).unwrap();
    let error = client.close().unwrap_err();
    assert_eq!(error.kind, 17);
    assert_eq!(error.daemon_code.as_deref(), Some("test_stop"));
    assert!(client.joined());
    client.close().unwrap();
}

#[test]
fn free_active_client_without_daemon_acknowledgement() {
    let server = Server::new(|mut wire| {
        wire.subscribe();
        // No unsubscribe response exists. Free must close rather than await it.
        assert!(wire.receive().is_none());
    });
    let client = server.client();
    client.request(Operation::Subscribe).unwrap();
    // SAFETY: move the uniquely owned, live client into the matching ABI free.
    unsafe { airpods_client_free(Box::into_raw(Box::new(client))) };
}

#[test]
fn wrapper_lifecycle_errors_and_double_subscribe() {
    let server = Server::new(|mut wire| {
        wire.subscribe();
        wire.unsubscribe();
        assert!(wire.receive().is_none());
    });
    let client = server.client();
    assert_eq!(
        client
            .request(Operation::Next(Wait::milliseconds(0)))
            .err()
            .unwrap()
            .kind,
        2
    );
    assert_eq!(
        client.request(Operation::Unsubscribe).err().unwrap().kind,
        2
    );
    client.request(Operation::Subscribe).unwrap();
    assert_eq!(client.request(Operation::Subscribe).err().unwrap().kind, 19);
    client.request(Operation::Unsubscribe).unwrap();
    client.close().unwrap();
}

#[test]
fn timeouts_preserve_sample_and_zero_poll_prefers_ready_sample() {
    let server = Server::new(|mut wire| {
        wire.subscribe();
        wire.expect("ping");
        wire.send(json!({"event":"heart_rate","bpm":169,"source_side":"left"}));
        // Reader must route the preceding sample before delivering ping response.
        wire.send(json!({"operation":"ping","ok":true,"pong":true}));
        wire.unsubscribe();
        assert!(wire.receive().is_none());
    });
    let client = server.client();
    client.request(Operation::Subscribe).unwrap();
    for _ in 0..2 {
        assert!(matches!(
            client
                .request(Operation::Next(Wait::milliseconds(5)))
                .unwrap(),
            Reply::Timeout
        ));
    }
    client.request(Operation::Ping).unwrap();
    let Reply::Sample(sample) = client
        .request(Operation::Next(Wait::milliseconds(0)))
        .unwrap()
    else {
        panic!("ready sample lost")
    };
    assert_eq!(
        sample,
        HrSample {
            bpm: 169,
            source_side: 1,
            source_side_raw: 0,
            reserved: 0
        }
    );
    assert!(matches!(
        client
            .request(Operation::Next(Wait::milliseconds(0)))
            .unwrap(),
        Reply::Timeout
    ));
    client.close().unwrap();
}

#[test]
fn lag_is_reported_and_disconnect_is_terminal_then_end() {
    let server = Server::new(|mut wire| {
        wire.subscribe();
        wire.expect("ping");
        for _ in 0..40 {
            wire.send(json!({"event":"heart_rate","bpm":88,"source_side":"right"}));
        }
        wire.send(json!({"operation":"ping","ok":true,"pong":true}));
        wire.expect("ping");
        // Peer closes without a response; no reconnect is attempted.
    });
    let client = server.client();
    client.request(Operation::Subscribe).unwrap();
    client.request(Operation::Ping).unwrap();
    assert_eq!(
        client
            .request(Operation::Next(Wait::milliseconds(0)))
            .err()
            .unwrap()
            .kind,
        20
    );
    assert_eq!(client.request(Operation::Ping).err().unwrap().kind, 18);
    // Drain retained samples, then get one terminal error followed by END.
    loop {
        match client.request(Operation::Next(Wait::Forever)) {
            Ok(Reply::Sample(_)) => (),
            Err(error) if error.kind == 20 => (),
            Err(error) => {
                assert_eq!(error.kind, 18);
                break;
            }
            _ => panic!("unexpected stream result"),
        }
    }
    assert!(matches!(
        client.request(Operation::Next(Wait::Forever)).unwrap(),
        Reply::End
    ));
    assert_eq!(client.close().unwrap_err().kind, 18);
    assert!(client.joined());
}

#[test]
fn handle_can_move_between_native_threads() {
    let server = Server::new(|mut wire| {
        wire.ping();
        assert!(wire.receive().is_none());
    });
    let client = server.client();
    let client = thread::spawn(move || {
        client.request(Operation::Ping).unwrap();
        client
    })
    .join()
    .unwrap();
    client.close().unwrap();
    assert!(client.joined());
}

#[test]
fn connect_failure_returns_no_handle() {
    // SAFETY: local live output slots, and a NUL-terminated explicit path.
    unsafe {
        let path = c"/missing-task11-4-explicit/socket";
        let mut client = std::ptr::null_mut();
        let mut error = std::ptr::null_mut();
        assert_eq!(
            airpods_client_connect_to(path.as_ptr(), &mut client, &mut error),
            ERROR
        );
        assert!(client.is_null());
        assert_eq!(airpods_error_kind(error), 11);
        airpods_error_free(error);
    }
}

#[test]
fn explicit_non_utf8_unix_path_connects() {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    let dir = std::env::temp_dir().join(format!("ap-c-nonutf8-{}", std::process::id()));
    std::fs::create_dir(&dir).unwrap();
    let path = dir.join(std::ffi::OsString::from_vec(b"socket-\xff".to_vec()));
    let server = Server::at(path.clone(), |mut wire| {
        wire.ping();
        assert!(wire.receive().is_none());
    });
    let c_path = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
    // SAFETY: c_path is NUL terminated; outputs are live locals; the returned
    // unique client is closed then freed once, with no concurrent operations.
    unsafe {
        let mut client = std::ptr::null_mut();
        let mut error = std::ptr::null_mut();
        assert_eq!(
            airpods_client_connect_to(c_path.as_ptr(), &mut client, &mut error),
            OK
        );
        assert!(error.is_null());
        assert_eq!(airpods_client_ping(client, &mut error), OK);
        assert!(error.is_null());
        assert_eq!(airpods_client_close(client, &mut error), OK);
        assert!(error.is_null());
        airpods_client_free(client);
    }
    drop(server);
}

#[test]
fn owned_objects_outlive_client_and_preserve_embedded_nuls() {
    let server = Server::new(|mut wire| {
        wire.expect("hello");
        wire.send(
            json!({"operation":"hello","ok":true,"service":"service\0名","experimental":false}),
        );
        wire.expect("status");
        wire.send(
            json!({"operation":"status","ok":true,"state":"future\0state","subscriber_count":169}),
        );
        assert!(wire.receive().is_none());
    });
    let client = Box::into_raw(Box::new(server.client()));
    // SAFETY: input objects are live allocations and output slots are writable
    // locals. Snapshots have independent ownership; each view is read before its
    // corresponding free, and every allocation is freed exactly once.
    unsafe {
        let mut hello = std::ptr::null_mut();
        let mut status = std::ptr::null_mut();
        let mut error = std::ptr::null_mut();
        assert_eq!(airpods_client_hello(client, &mut hello, &mut error), OK);
        assert!(error.is_null());
        assert_eq!(airpods_client_status(client, &mut status, &mut error), OK);
        assert!(error.is_null());
        airpods_client_free(client);
        let service = airpods_hello_service(hello);
        assert_eq!(
            std::slice::from_raw_parts(service.data.cast::<u8>(), service.len),
            "service\0名".as_bytes()
        );
        assert_eq!(airpods_hello_experimental(hello), 0);
        assert_eq!(airpods_status_state(status), 255);
        assert_eq!(airpods_status_subscriber_count(status), 169);
        let raw = airpods_status_unknown_state(status);
        assert_eq!(
            std::slice::from_raw_parts(raw.data.cast::<u8>(), raw.len),
            b"future\0state"
        );
        airpods_hello_free(hello);
        airpods_status_free(status);
    }
}
