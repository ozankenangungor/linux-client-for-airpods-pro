#![forbid(unsafe_code)]
//! Experimental asynchronous client for the local `airpods-hubd` Unix socket.
//!
//! This crate owns no Bluetooth resources and never starts the daemon. Protocol
//! version 1 has no request IDs, so requests on one client are serialized while
//! a single reader routes responses and heart-rate events.

use serde_json :: { Map , Value } ;

use std :: fmt ;
use std :: io ;
use std :: path :: { PathBuf } ;


use tokio :: io :: { AsyncBufReadExt , AsyncWriteExt } ;




/// Experimental daemon protocol version supported by this crate.
pub const PROTOCOL_VERSION: u64 = 1;
/// Maximum JSON payload size, excluding the newline delimiter.
pub const MAX_FRAME_SIZE: usize = 4096;



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





fn required_string<'a>(object: &'a Map<String, Value>, field: &str) -> Result<&'a str, Error> {
    object
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| unexpected(format!("{field} must be a string")))
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
    use super :: * ;
    use tokio :: io :: BufReader ;

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
