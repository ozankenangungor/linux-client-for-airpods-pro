#![forbid(unsafe_code)]
//! Experimental asynchronous client for the local `airpods-hubd` Unix socket.
//!
//! This crate owns no Bluetooth resources and never starts the daemon. Protocol
//! version 1 has no request IDs, so requests on one client are serialized while
//! a single reader routes responses and heart-rate events.

use serde_json::{Map, Value, json};
use std::env;
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex, MutexGuard as StdMutexGuard, Weak};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::net::unix::OwnedWriteHalf;
use tokio::sync::{Mutex, OwnedMutexGuard, broadcast, oneshot};

/// Experimental daemon protocol version supported by this crate.
pub const PROTOCOL_VERSION: u64 = 1;
/// Maximum JSON payload size, excluding the newline delimiter.
pub const MAX_FRAME_SIZE: usize = 4096;
const DEFAULT_SOCKET_NAME: &str = "airpods-hubd.sock";
const EVENT_BUFFER_SIZE: usize = 32;

/// Errors produced by the client boundary.
#[derive(Clone, Debug, Eq, PartialEq)]
#[non_exhaustive]
pub enum Error {
    XdgRuntimeDirMissing,
    Connect {
        path: PathBuf,
        kind: io::ErrorKind,
        message: String,
    },
    Io {
        context: &'static str,
        kind: io::ErrorKind,
        message: String,
    },
    FrameTooLarge {
        limit: usize,
    },
    InvalidJson {
        message: String,
    },
    ProtocolVersion {
        expected: u64,
        received: Option<u64>,
    },
    UnexpectedMessage {
        message: String,
    },
    DaemonError {
        code: String,
        message: String,
    },
    ConnectionClosed,
    SubscriptionActive,
    EventLagged {
        skipped: u64,
    },
}

impl fmt::Display for Error {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::XdgRuntimeDirMissing => {
                formatter.write_str("XDG_RUNTIME_DIR is required to locate airpods-hubd")
            }
            Self::Connect { path, message, .. } => {
                write!(
                    formatter,
                    "could not connect to {}: {message}",
                    path.display()
                )
            }
            Self::Io {
                context, message, ..
            } => write!(formatter, "{context}: {message}"),
            Self::FrameTooLarge { limit } => {
                write!(formatter, "daemon frame exceeds the {limit}-byte limit")
            }
            Self::InvalidJson { message } => write!(formatter, "invalid daemon JSON: {message}"),
            Self::ProtocolVersion { expected, received } => match received {
                Some(received) => write!(
                    formatter,
                    "unsupported daemon protocol version {received}; expected {expected}"
                ),
                None => write!(
                    formatter,
                    "daemon protocol version is missing or invalid; expected {expected}"
                ),
            },
            Self::UnexpectedMessage { message } => {
                write!(formatter, "unexpected daemon message: {message}")
            }
            Self::DaemonError { code, message } => {
                write!(formatter, "daemon error {code}: {message}")
            }
            Self::ConnectionClosed => formatter.write_str("airpods-hubd closed the connection"),
            Self::SubscriptionActive => {
                formatter.write_str("this client already has a heart-rate subscription")
            }
            Self::EventLagged { skipped } => {
                write!(
                    formatter,
                    "heart-rate consumer fell behind by {skipped} events"
                )
            }
        }
    }
}

impl std::error::Error for Error {}

/// Source-side metadata exactly as encoded by protocol version 1.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SourceSide {
    Left,
    Right,
    Unknown(u8),
}

impl fmt::Display for SourceSide {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Left => formatter.write_str("left"),
            Self::Right => formatter.write_str("right"),
            Self::Unknown(raw) => write!(formatter, "unknown({raw})"),
        }
    }
}

/// One unmodified heart-rate event from the daemon.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct HeartRateSample {
    pub bpm: u8,
    pub source_side: SourceSide,
}

/// Result of the protocol `hello` operation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Hello {
    pub service: String,
    pub experimental: bool,
}

/// Daemon lifecycle state reported by protocol version 1.
#[derive(Clone, Debug, Eq, PartialEq)]
#[non_exhaustive]
pub enum DaemonState {
    Stopped,
    Starting,
    Ready,
    StartingHeartRate,
    Streaming,
    StoppingHeartRate,
    Failed,
    ShuttingDown,
    Unknown(String),
}

/// Result of the protocol `status` operation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Status {
    pub state: DaemonState,
    pub subscriber_count: u64,
}

struct PendingRequest {
    response: oneshot::Sender<Result<Value, Error>>,
    _request_guard: OwnedMutexGuard<()>,
}

struct Inner {
    writer: Mutex<OwnedWriteHalf>,
    request_gate: Arc<Mutex<()>>,
    pending: StdMutex<Option<PendingRequest>>,
    events: StdMutex<Option<broadcast::Sender<Result<HeartRateSample, Error>>>>,
    closed: AtomicBool,
}

impl Inner {
    fn pending(&self) -> StdMutexGuard<'_, Option<PendingRequest>> {
        self.pending
            .lock()
            .unwrap_or_else(|error| error.into_inner())
    }

    fn events(
        &self,
    ) -> StdMutexGuard<'_, Option<broadcast::Sender<Result<HeartRateSample, Error>>>> {
        self.events
            .lock()
            .unwrap_or_else(|error| error.into_inner())
    }

    fn fail(&self, error: Error) {
        if self.closed.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Some(pending) = self.pending().take() {
            let _ = pending.response.send(Err(error.clone()));
        }
        if let Some(events) = self.events().take() {
            let _ = events.send(Err(error));
        }
    }
}

/// A connection to one running `airpods-hubd` instance.
pub struct AirPodsClient {
    inner: Arc<Inner>,
}

impl AirPodsClient {
    /// Resolves `$XDG_RUNTIME_DIR/airpods-hubd.sock`.
    pub fn default_socket_path() -> Result<PathBuf, Error> {
        let runtime = env::var_os("XDG_RUNTIME_DIR").ok_or(Error::XdgRuntimeDirMissing)?;
        if runtime.is_empty() {
            return Err(Error::XdgRuntimeDirMissing);
        }
        Ok(PathBuf::from(runtime).join(DEFAULT_SOCKET_NAME))
    }

    /// Connects to the default daemon socket without starting or retrying it.
    pub async fn connect() -> Result<Self, Error> {
        Self::connect_to(Self::default_socket_path()?).await
    }

    /// Connects to an explicit Unix socket path for tests and development.
    pub async fn connect_to(path: impl AsRef<Path>) -> Result<Self, Error> {
        let path = path.as_ref();
        let stream = UnixStream::connect(path)
            .await
            .map_err(|error| Error::Connect {
                path: path.to_path_buf(),
                kind: error.kind(),
                message: error.to_string(),
            })?;
        let (reader, writer) = stream.into_split();
        let inner = Arc::new(Inner {
            writer: Mutex::new(writer),
            request_gate: Arc::new(Mutex::new(())),
            pending: StdMutex::new(None),
            events: StdMutex::new(None),
            closed: AtomicBool::new(false),
        });
        tokio::spawn(read_loop(reader, Arc::downgrade(&inner)));
        Ok(Self { inner })
    }

    pub async fn hello(&self) -> Result<Hello, Error> {
        let response = request(&self.inner, "hello", None).await?;
        let object = success_object(&response, "hello")?;
        let service = required_string(object, "service")?;
        let experimental = required_bool(object, "experimental")?;
        Ok(Hello {
            service: service.to_owned(),
            experimental,
        })
    }

    pub async fn status(&self) -> Result<Status, Error> {
        let response = request(&self.inner, "status", None).await?;
        let object = success_object(&response, "status")?;
        let state = match required_string(object, "state")? {
            "stopped" => DaemonState::Stopped,
            "starting" => DaemonState::Starting,
            "ready" => DaemonState::Ready,
            "starting_hr" => DaemonState::StartingHeartRate,
            "streaming" => DaemonState::Streaming,
            "stopping_hr" => DaemonState::StoppingHeartRate,
            "failed" => DaemonState::Failed,
            "shutting_down" => DaemonState::ShuttingDown,
            other => DaemonState::Unknown(other.to_owned()),
        };
        Ok(Status {
            state,
            subscriber_count: required_u64(object, "subscriber_count")?,
        })
    }

    pub async fn ping(&self) -> Result<(), Error> {
        let response = request(&self.inner, "ping", None).await?;
        let object = success_object(&response, "ping")?;
        if required_bool(object, "pong")? {
            Ok(())
        } else {
            Err(unexpected("ping response did not contain pong=true"))
        }
    }

    pub async fn subscribe_heart_rate(&self) -> Result<HeartRateSubscription, Error> {
        let (sender, receiver) = broadcast::channel(EVENT_BUFFER_SIZE);
        {
            let mut events = self.inner.events();
            if events.is_some() {
                return Err(Error::SubscriptionActive);
            }
            *events = Some(sender);
        }

        let response = match request(&self.inner, "subscribe", Some("heart_rate")).await {
            Ok(response) => response,
            Err(error) => {
                self.inner.events().take();
                return Err(error);
            }
        };
        if let Err(error) = validate_subscription_response(&response, "subscribe", true) {
            self.inner.events().take();
            return Err(error);
        }
        Ok(HeartRateSubscription {
            inner: Some(Arc::clone(&self.inner)),
            receiver,
            finished: false,
        })
    }
}

/// An active heart-rate subscription on its parent client's connection.
///
/// [`unsubscribe`](Self::unsubscribe) is the reliable cleanup path. Dropping an
/// active value schedules a best-effort unsubscribe when a Tokio runtime is
/// available and never blocks in `Drop`.
pub struct HeartRateSubscription {
    inner: Option<Arc<Inner>>,
    receiver: broadcast::Receiver<Result<HeartRateSample, Error>>,
    finished: bool,
}

impl HeartRateSubscription {
    /// Returns the next event, preserving daemon order and duplicates.
    pub async fn next(&mut self) -> Result<Option<HeartRateSample>, Error> {
        if self.finished {
            return Ok(None);
        }
        match self.receiver.recv().await {
            Ok(Ok(sample)) => Ok(Some(sample)),
            Ok(Err(error)) => {
                self.finished = true;
                Err(error)
            }
            Err(broadcast::error::RecvError::Closed) => {
                self.finished = true;
                Err(Error::ConnectionClosed)
            }
            Err(broadcast::error::RecvError::Lagged(skipped)) => {
                Err(Error::EventLagged { skipped })
            }
        }
    }

    /// Unsubscribes and waits for the daemon response.
    pub async fn unsubscribe(mut self) -> Result<(), Error> {
        let inner = self.inner.take().ok_or(Error::ConnectionClosed)?;
        let result = unsubscribe(&inner).await;
        inner.events().take();
        self.finished = true;
        result
    }
}

impl Drop for HeartRateSubscription {
    fn drop(&mut self) {
        if self.finished {
            return;
        }
        let Some(inner) = self.inner.take() else {
            return;
        };
        if let Ok(runtime) = tokio::runtime::Handle::try_current() {
            runtime.spawn(async move {
                let _ = unsubscribe(&inner).await;
                inner.events().take();
            });
        }
    }
}

async fn unsubscribe(inner: &Arc<Inner>) -> Result<(), Error> {
    let response = request(inner, "unsubscribe", Some("heart_rate")).await?;
    validate_subscription_response(&response, "unsubscribe", false)
}

fn validate_subscription_response(
    response: &Value,
    operation: &'static str,
    subscribed: bool,
) -> Result<(), Error> {
    let object = success_object(response, operation)?;
    if required_string(object, "stream")? != "heart_rate" {
        return Err(unexpected("subscription response has an invalid stream"));
    }
    if required_bool(object, "subscribed")? != subscribed {
        return Err(unexpected("subscription response has an invalid state"));
    }
    let idempotence_field = if subscribed {
        "already_subscribed"
    } else {
        "already_unsubscribed"
    };
    required_bool(object, idempotence_field)?;
    Ok(())
}

async fn request(
    inner: &Arc<Inner>,
    operation: &'static str,
    stream: Option<&'static str>,
) -> Result<Value, Error> {
    if inner.closed.load(Ordering::SeqCst) {
        return Err(Error::ConnectionClosed);
    }
    let mut message = json!({
        "protocol_version": PROTOCOL_VERSION,
        "operation": operation,
    });
    if let Some(stream) = stream {
        message["stream"] = Value::String(stream.to_owned());
    }
    let mut frame = serde_json::to_vec(&message).map_err(|error| Error::InvalidJson {
        message: error.to_string(),
    })?;
    if frame.len() > MAX_FRAME_SIZE {
        return Err(Error::FrameTooLarge {
            limit: MAX_FRAME_SIZE,
        });
    }
    frame.push(b'\n');

    let request_guard = Arc::clone(&inner.request_gate).lock_owned().await;
    if inner.closed.load(Ordering::SeqCst) {
        return Err(Error::ConnectionClosed);
    }
    let (response, receiver) = oneshot::channel();
    *inner.pending() = Some(PendingRequest {
        response,
        _request_guard: request_guard,
    });

    let writer_inner = Arc::clone(inner);
    tokio::spawn(async move {
        if let Err(error) = write_frame(&writer_inner, &frame).await {
            writer_inner.fail(error);
        }
    });
    receiver.await.unwrap_or(Err(Error::ConnectionClosed))
}

async fn write_frame(inner: &Inner, frame: &[u8]) -> Result<(), Error> {
    let mut writer = inner.writer.lock().await;
    writer
        .write_all(frame)
        .await
        .map_err(|error| io_error("writing daemon request", error))?;
    writer
        .flush()
        .await
        .map_err(|error| io_error("flushing daemon request", error))
}

async fn read_loop(reader: tokio::net::unix::OwnedReadHalf, inner: Weak<Inner>) {
    let mut reader = BufReader::new(reader);
    loop {
        let frame = match read_frame(&mut reader).await {
            Ok(Some(frame)) => frame,
            Ok(None) => {
                if let Some(inner) = inner.upgrade() {
                    inner.fail(Error::ConnectionClosed);
                }
                return;
            }
            Err(error) => {
                if let Some(inner) = inner.upgrade() {
                    inner.fail(error);
                }
                return;
            }
        };
        let inbound = match parse_inbound(&frame) {
            Ok(inbound) => inbound,
            Err(error) => {
                if let Some(inner) = inner.upgrade() {
                    inner.fail(error);
                }
                return;
            }
        };
        let Some(inner) = inner.upgrade() else {
            return;
        };
        match inbound {
            Inbound::Event(sample) => {
                if let Some(events) = inner.events().as_ref() {
                    let _ = events.send(Ok(sample));
                }
            }
            Inbound::Response(response) => {
                if let Some(pending) = inner.pending().take() {
                    let _ = pending.response.send(Ok(response));
                } else if let Ok(error) = daemon_error(&response) {
                    if let Some(events) = inner.events().take() {
                        let _ = events.send(Err(error));
                    }
                } else {
                    inner.fail(unexpected("response arrived without a pending request"));
                    return;
                }
            }
        }
    }
}

async fn read_frame<R: tokio::io::AsyncBufRead + Unpin>(
    reader: &mut R,
) -> Result<Option<Vec<u8>>, Error> {
    let mut frame = Vec::with_capacity(256);
    loop {
        let available = reader
            .fill_buf()
            .await
            .map_err(|error| io_error("reading daemon response", error))?;
        if available.is_empty() {
            return if frame.is_empty() {
                Ok(None)
            } else {
                Err(unexpected("connection closed in the middle of a frame"))
            };
        }
        if let Some(newline) = available.iter().position(|byte| *byte == b'\n') {
            if frame.len() + newline > MAX_FRAME_SIZE {
                return Err(Error::FrameTooLarge {
                    limit: MAX_FRAME_SIZE,
                });
            }
            frame.extend_from_slice(&available[..newline]);
            reader.consume(newline + 1);
            return Ok(Some(frame));
        }
        if frame.len() + available.len() > MAX_FRAME_SIZE {
            return Err(Error::FrameTooLarge {
                limit: MAX_FRAME_SIZE,
            });
        }
        let consumed = available.len();
        frame.extend_from_slice(available);
        reader.consume(consumed);
    }
}

#[derive(Debug)]
enum Inbound {
    Event(HeartRateSample),
    Response(Value),
}

fn parse_inbound(frame: &[u8]) -> Result<Inbound, Error> {
    let value: Value = serde_json::from_slice(frame).map_err(|error| Error::InvalidJson {
        message: error.to_string(),
    })?;
    let object = value
        .as_object()
        .ok_or_else(|| unexpected("message must be a JSON object"))?;
    let received = object.get("protocol_version").and_then(Value::as_u64);
    if received != Some(PROTOCOL_VERSION) {
        return Err(Error::ProtocolVersion {
            expected: PROTOCOL_VERSION,
            received,
        });
    }

    match (object.get("event"), object.get("ok")) {
        (Some(_), Some(_)) => Err(unexpected("message cannot be both an event and a response")),
        (Some(event), None) => {
            if event.as_str() != Some("heart_rate") {
                return Err(unexpected("unsupported event type"));
            }
            let bpm = required_u8(object, "bpm")?;
            let source_side = match required_string(object, "source_side")? {
                "left" => SourceSide::Left,
                "right" => SourceSide::Right,
                "unknown" => SourceSide::Unknown(required_u8(object, "source_side_raw")?),
                _ => return Err(unexpected("invalid source_side value")),
            };
            Ok(Inbound::Event(HeartRateSample { bpm, source_side }))
        }
        (None, Some(ok)) if ok.is_boolean() => Ok(Inbound::Response(value)),
        _ => Err(unexpected(
            "message is neither a response nor a heart-rate event",
        )),
    }
}

fn success_object<'a>(
    value: &'a Value,
    expected_operation: &'static str,
) -> Result<&'a Map<String, Value>, Error> {
    let object = value
        .as_object()
        .ok_or_else(|| unexpected("response must be a JSON object"))?;
    match object.get("ok").and_then(Value::as_bool) {
        Some(false) => return Err(daemon_error(value)?),
        Some(true) => {}
        None => return Err(unexpected("response is missing a boolean ok field")),
    }
    if required_string(object, "operation")? != expected_operation {
        return Err(unexpected("response operation does not match the request"));
    }
    Ok(object)
}

fn daemon_error(value: &Value) -> Result<Error, Error> {
    let object = value
        .as_object()
        .ok_or_else(|| unexpected("error response must be a JSON object"))?;
    if object.get("ok").and_then(Value::as_bool) != Some(false) {
        return Err(unexpected("response is not a daemon error"));
    }
    let error = object
        .get("error")
        .and_then(Value::as_object)
        .ok_or_else(|| unexpected("daemon error response is missing error details"))?;
    Ok(Error::DaemonError {
        code: required_string(error, "code")?.to_owned(),
        message: required_string(error, "message")?.to_owned(),
    })
}

fn required_string<'a>(object: &'a Map<String, Value>, field: &str) -> Result<&'a str, Error> {
    object
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| unexpected(format!("{field} must be a string")))
}

fn required_bool(object: &Map<String, Value>, field: &str) -> Result<bool, Error> {
    object
        .get(field)
        .and_then(Value::as_bool)
        .ok_or_else(|| unexpected(format!("{field} must be a boolean")))
}

fn required_u64(object: &Map<String, Value>, field: &str) -> Result<u64, Error> {
    object
        .get(field)
        .and_then(Value::as_u64)
        .ok_or_else(|| unexpected(format!("{field} must be a non-negative integer")))
}

fn required_u8(object: &Map<String, Value>, field: &str) -> Result<u8, Error> {
    u8::try_from(required_u64(object, field)?)
        .map_err(|_| unexpected(format!("{field} must fit in one byte")))
}

fn unexpected(message: impl Into<String>) -> Error {
    Error::UnexpectedMessage {
        message: message.into(),
    }
}

fn io_error(context: &'static str, error: io::Error) -> Error {
    Error::Io {
        context,
        kind: error.kind(),
        message: error.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::BufReader;

    #[tokio::test]
    async fn bounded_reader_accepts_the_daemon_limit() {
        let mut input = vec![b'x'; MAX_FRAME_SIZE];
        input.push(b'\n');
        let mut reader = BufReader::new(input.as_slice());
        assert_eq!(
            read_frame(&mut reader).await.unwrap().unwrap().len(),
            MAX_FRAME_SIZE
        );
    }

    #[tokio::test]
    async fn bounded_reader_rejects_an_oversized_frame() {
        let mut input = vec![b'x'; MAX_FRAME_SIZE + 1];
        input.push(b'\n');
        let mut reader = BufReader::new(input.as_slice());
        assert_eq!(
            read_frame(&mut reader).await.unwrap_err(),
            Error::FrameTooLarge {
                limit: MAX_FRAME_SIZE
            }
        );
    }

    #[test]
    fn malformed_json_is_rejected() {
        assert!(matches!(
            parse_inbound(b"{"),
            Err(Error::InvalidJson { .. })
        ));
    }

    #[test]
    fn non_object_is_rejected() {
        assert!(matches!(
            parse_inbound(br#"[1,2,3]"#),
            Err(Error::UnexpectedMessage { .. })
        ));
    }

    #[test]
    fn unsupported_protocol_version_is_rejected() {
        assert_eq!(
            parse_inbound(br#"{"protocol_version":2,"ok":true}"#).unwrap_err(),
            Error::ProtocolVersion {
                expected: 1,
                received: Some(2)
            }
        );
    }

    #[test]
    fn unknown_source_side_retains_raw_value() {
        let Inbound::Event(sample) = parse_inbound(
            br#"{"protocol_version":1,"event":"heart_rate","bpm":169,"source_side":"unknown","source_side_raw":37}"#,
        )
        .unwrap()
        else {
            panic!("expected event");
        };
        assert_eq!(sample.source_side, SourceSide::Unknown(37));
        assert_eq!(sample.bpm, 169);
    }
}
