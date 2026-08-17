//! Owned errors; this module never handles caller pointers.

#[derive(Debug)]
pub(crate) struct Error {
    pub kind: u32,
    pub message: String,
    pub daemon_code: Option<String>,
}

impl Error {
    pub fn invalid_argument() -> Self {
        Self::new(1, "required argument is NULL")
    }

    pub fn invalid_state(message: &str) -> Self {
        Self::new(2, message)
    }

    pub fn internal() -> Self {
        Self::new(255, "internal C client failure")
    }

    fn new(kind: u32, message: &str) -> Self {
        Self {
            kind,
            message: message.to_owned(),
            daemon_code: None,
        }
    }

    pub fn sdk(error: airpods_client::Error, default_connect: bool) -> Self {
        use airpods_client::Error as Sdk;
        let kind = match &error {
            Sdk::XdgRuntimeDirMissing => 10,
            Sdk::Connect { .. } => 11,
            Sdk::Io { .. } => 12,
            Sdk::FrameTooLarge { .. } => 13,
            Sdk::InvalidJson { .. } => 14,
            Sdk::ProtocolVersion { .. } => 15,
            Sdk::UnexpectedMessage { .. } => 16,
            Sdk::DaemonError { .. } => 17,
            Sdk::ConnectionClosed => 18,
            Sdk::SubscriptionActive => 19,
            Sdk::EventLagged { .. } => 20,
            // The SDK enum is non-exhaustive. Future variants fail closed.
            _ => return Self::internal(),
        };
        let message = match &error {
            Sdk::Connect { message, .. } if default_connect => {
                format!("could not connect to default airpods-hubd socket: {message}")
            }
            _ => error.to_string(),
        };
        let daemon_code = match error {
            Sdk::DaemonError { code, .. } => Some(code),
            _ => None,
        };
        Self {
            kind,
            message,
            daemon_code,
        }
    }
}
