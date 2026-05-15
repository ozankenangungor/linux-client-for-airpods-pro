#![forbid(unsafe_code)]
//! Protocol-v1 identities, validation and pure daemon decisions. No transport or session effects.

use serde_json :: { Value , json } ;

pub const PROTOCOL_VERSION: u64 = 1;
pub const MAX_FRAME_SIZE: usize = 4096;

pub const HEART_RATE_STREAM: &str = "heart_rate";


#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Operation {
    Hello,
    Status,
    Subscribe,
    Unsubscribe,
    Ping,
}

impl Operation {
    pub const ALL: [Self; 5] = [
        Self::Hello,
        Self::Status,
        Self::Subscribe,
        Self::Unsubscribe,
        Self::Ping,
    ];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Hello => "hello",
            Self::Status => "status",
            Self::Subscribe => "subscribe",
            Self::Unsubscribe => "unsubscribe",
            Self::Ping => "ping",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|operation| operation.as_str() == value)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DaemonState {
    Stopped,
    Starting,
    Ready,
    StartingHeartRate,
    Streaming,
    StoppingHeartRate,
    Failed,
    ShuttingDown,
}

impl DaemonState {
    pub const ALL: [Self; 8] = [
        Self::Stopped,
        Self::Starting,
        Self::Ready,
        Self::StartingHeartRate,
        Self::Streaming,
        Self::StoppingHeartRate,
        Self::Failed,
        Self::ShuttingDown,
    ];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Stopped => "stopped",
            Self::Starting => "starting",
            Self::Ready => "ready",
            Self::StartingHeartRate => "starting_hr",
            Self::Streaming => "streaming",
            Self::StoppingHeartRate => "stopping_hr",
            Self::Failed => "failed",
            Self::ShuttingDown => "shutting_down",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|state| state.as_str() == value)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RequestError {
    FrameTooLarge,
    InvalidJson,
    InvalidRequest,
    UnsupportedVersion,
    InvalidOperation,
    UnknownOperation,
    InvalidStream,
}

impl RequestError {
    pub const fn code(self) -> &'static str {
        match self {
            Self::FrameTooLarge => "frame_too_large",
            Self::InvalidJson => "invalid_json",
            Self::InvalidRequest => "invalid_request",
            Self::UnsupportedVersion => "unsupported_version",
            Self::InvalidOperation => "invalid_operation",
            Self::UnknownOperation => "unknown_operation",
            Self::InvalidStream => "invalid_stream",
        }
    }

    pub const fn message(self) -> &'static str {
        match self {
            Self::FrameTooLarge => "request frame exceeds limit",
            Self::InvalidJson => "request must be valid UTF-8 JSON",
            Self::InvalidRequest => "request must be a JSON object",
            Self::UnsupportedVersion => "unsupported experimental protocol version",
            Self::InvalidOperation => "operation must be a non-empty string",
            Self::UnknownOperation => "operation is not supported",
            Self::InvalidStream => "stream must be heart_rate",
        }
    }
}

/// A neutral projection of a parsed JSON field. The bridge parses with Python's JSON
/// decoder to preserve its accepted encodings, numbers, nesting and opaque values.
/// The core, not the bridge, owns the validation and its error precedence.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RequestField<'a> {
    Missing,
    Integer(i64),
    Text(&'a str),
    /// A non-empty Python Unicode string containing an unpaired surrogate.
    NonUtf8Text,
    Other,
}

pub fn validate_frame_size(length: usize) -> Result<(), RequestError> {
    if length > MAX_FRAME_SIZE {
        Err(RequestError::FrameTooLarge)
    } else {
        Ok(())
    }
}

pub fn validate_fields(
    version: RequestField<'_>,
    operation: RequestField<'_>,
    stream: RequestField<'_>,
) -> Result<Operation, RequestError> {
    if version != RequestField::Integer(PROTOCOL_VERSION as i64) {
        return Err(RequestError::UnsupportedVersion);
    }
    let operation = match operation {
        RequestField::Text("") => return Err(RequestError::InvalidOperation),
        RequestField::Text(value) => {
            Operation::parse(value).ok_or(RequestError::UnknownOperation)?
        }
        RequestField::NonUtf8Text => return Err(RequestError::UnknownOperation),
        _ => return Err(RequestError::InvalidOperation),
    };
    if matches!(operation, Operation::Subscribe | Operation::Unsubscribe)
        && stream != RequestField::Text(HEART_RATE_STREAM)
    {
        return Err(RequestError::InvalidStream);
    }
    Ok(operation)
}

/// Pure Rust parser for standard UTF-8 JSON. Python's compatibility bridge calls
/// `validate_fields` after Python JSON conversion to preserve the parent's wider
/// parsing rules (including UTF-16, arbitrary integers and opaque nonfinite values).
pub fn decode_request(frame: &[u8]) -> Result<Value, RequestError> {
    validate_frame_size(frame.len())?;
    // Python's json.loads accepts a UTF-8 BOM on byte input.
    let frame = frame.strip_prefix(b"\xef\xbb\xbf").unwrap_or(frame);
    let value: Value = serde_json::from_slice(frame).map_err(|_| RequestError::InvalidJson)?;
    let object = value.as_object().ok_or(RequestError::InvalidRequest)?;
    let field = |name| match object.get(name) {
        None => RequestField::Missing,
        Some(Value::String(text)) => RequestField::Text(text),
        Some(Value::Number(number)) => number
            .as_i64()
            .map(RequestField::Integer)
            .unwrap_or(RequestField::Other),
        Some(_) => RequestField::Other,
    };
    validate_fields(
        field("protocol_version"),
        field("operation"),
        field("stream"),
    )?;
    Ok(value)
}

/// The daemon's source field is an integer; unrecognized values retain their raw identity.
pub fn source_side(raw: i64) -> (&'static str, Option<i64>) {
    match raw {
        1 => ("left", None),
        2 => ("right", None),
        _ => ("unknown", Some(raw)),
    }
}

/// Fixed daemon-owned replies. Arbitrary extension fields and canonical Python JSON encoding
/// remain at the compatibility boundary; these constructors own the fixed v1 wire structure.
pub fn hello_response() -> Value {
    json!({"protocol_version": PROTOCOL_VERSION, "ok": true, "operation": Operation::Hello.as_str(), "service": "airpods-hubd", "experimental": true})
}

pub fn ping_response() -> Value {
    json!({"protocol_version": PROTOCOL_VERSION, "ok": true, "operation": Operation::Ping.as_str(), "pong": true})
}

pub fn status_response(state: DaemonState, subscriber_count: usize) -> Value {
    json!({"protocol_version": PROTOCOL_VERSION, "ok": true, "operation": Operation::Status.as_str(), "state": state.as_str(), "subscriber_count": subscriber_count})
}

pub fn subscription_response(subscribed: bool, already: bool) -> Value {
    if subscribed {
        json!({"protocol_version": PROTOCOL_VERSION, "ok": true, "operation": Operation::Subscribe.as_str(), "stream": HEART_RATE_STREAM, "subscribed": true, "already_subscribed": already})
    } else {
        json!({"protocol_version": PROTOCOL_VERSION, "ok": true, "operation": Operation::Unsubscribe.as_str(), "stream": HEART_RATE_STREAM, "subscribed": false, "already_unsubscribed": already})
    }
}

pub fn heart_rate_event(bpm: i64, raw: i64) -> Value {
    let (side, unknown) = source_side(raw);
    let mut event = json!({"protocol_version": PROTOCOL_VERSION, "event": HEART_RATE_STREAM, "bpm": bpm, "source_side": side});
    if let Some(raw) = unknown {
        event["source_side_raw"] = json!(raw);
    }
    event
}

























#[cfg(test)]
mod tests {
    use super :: * ;

    fn frame(version: &str, operation: &str, stream: &str) -> Vec<u8> {
        format!("{{\"protocol_version\":{version},\"operation\":{operation},\"stream\":{stream}}}")
            .into_bytes()
    }

    #[test]
    fn operations_and_state_wire_round_trips() {
        for operation in Operation::ALL {
            assert_eq!(Operation::parse(operation.as_str()), Some(operation));
            let frame = frame(
                "1",
                &format!("\"{}\"", operation.as_str()),
                "\"heart_rate\"",
            );
            assert_eq!(
                decode_request(&frame).unwrap()["operation"],
                operation.as_str()
            );
        }
        assert_eq!(Operation::parse("HELLO"), None);
        for state in DaemonState::ALL {
            assert_eq!(DaemonState::parse(state.as_str()), Some(state));
            assert_eq!(status_response(state, 3)["state"], state.as_str());
        }
        assert_eq!(DaemonState::parse("unknown"), None);
    }

    #[test]
    fn request_categories_and_precedence() {
        let cases: &[(Vec<u8>, RequestError)] = &[
            (b"\xff".to_vec(), RequestError::InvalidJson),
            (b"{".to_vec(), RequestError::InvalidJson),
            (b"[]".to_vec(), RequestError::InvalidRequest),
            (b"null".to_vec(), RequestError::InvalidRequest),
            (b"{}".to_vec(), RequestError::UnsupportedVersion),
            (
                frame("true", "\"ping\"", "null"),
                RequestError::UnsupportedVersion,
            ),
            (
                frame("1.0", "\"ping\"", "null"),
                RequestError::UnsupportedVersion,
            ),
            (
                frame("\"1\"", "\"ping\"", "null"),
                RequestError::UnsupportedVersion,
            ),
            (
                frame("0", "\"ping\"", "null"),
                RequestError::UnsupportedVersion,
            ),
            (
                frame("2", "\"ping\"", "null"),
                RequestError::UnsupportedVersion,
            ),
            (
                b"{\"protocol_version\":1}".to_vec(),
                RequestError::InvalidOperation,
            ),
            (frame("1", "\"\"", "null"), RequestError::InvalidOperation),
            (frame("1", "false", "null"), RequestError::InvalidOperation),
            (
                frame("1", "\"PING\"", "null"),
                RequestError::UnknownOperation,
            ),
            (
                frame("1", "\"subscribe\"", "null"),
                RequestError::InvalidStream,
            ),
            (
                frame("1", "\"unsubscribe\"", "\"Heart_Rate\""),
                RequestError::InvalidStream,
            ),
        ];
        for (input, error) in cases {
            assert_eq!(decode_request(input), Err(*error), "{input:?}");
            assert!(!error.code().is_empty());
            assert!(!error.message().is_empty());
        }
        for operation in ["subscribe", "unsubscribe"] {
            let valid = frame("1", &format!("\"{operation}\""), "\"heart_rate\"");
            assert!(decode_request(&valid).is_ok());
        }
        assert!(decode_request(&frame("1", "\"hello\"", "null")).is_ok());
        assert_eq!(
            decode_request(b"\xef\xbb\xbf{}"),
            Err(RequestError::UnsupportedVersion)
        );
    }

    #[test]
    fn frame_size_and_opaque_fields() {
        let valid = b"{\"protocol_version\":1,\"operation\":\"ping\"}";
        for (size, expected) in [(4095, true), (4096, true), (4097, false)] {
            let mut input = valid.to_vec();
            input.resize(size, b' ');
            assert_eq!(decode_request(&input).is_ok(), expected);
        }
        let value = decode_request(
            b"{\"protocol_version\":1,\"operation\":\"status\",\"opaque\":{\"x\":[1,true,null]}}",
        )
        .unwrap();
        assert_eq!(value["opaque"]["x"], json!([1, true, null]));
    }

    #[test]
    fn source_side_exact_and_fixed_messages() {
        for raw in [i64::MIN, -1, 0, 3, 255, i64::MAX] {
            assert_eq!(source_side(raw), ("unknown", Some(raw)));
            assert_eq!(heart_rate_event(169, raw)["source_side_raw"], raw);
        }
        assert_eq!(source_side(1), ("left", None));
        assert_eq!(source_side(2), ("right", None));
        assert!(heart_rate_event(169, 1).get("source_side_raw").is_none());
        assert_eq!(hello_response()["service"], "airpods-hubd");
        assert_eq!(hello_response()["experimental"], true);
        assert_eq!(ping_response()["pong"], true);
        assert_eq!(
            subscription_response(true, true)["already_subscribed"],
            true
        );
        assert_eq!(
            subscription_response(false, false)["already_unsubscribed"],
            false
        );
    }

    

    
}
