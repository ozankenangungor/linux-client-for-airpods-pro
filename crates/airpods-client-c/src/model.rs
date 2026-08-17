//! Owned object models and the two C POD layouts, without pointer dereferences.

use std::ffi::c_char;

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct StringView {
    pub data: *const c_char,
    pub len: usize,
}

impl StringView {
    pub const ABSENT: Self = Self {
        data: std::ptr::null(),
        len: 0,
    };

    pub fn borrowed(value: &str) -> Self {
        Self {
            data: value.as_ptr().cast(),
            len: value.len(),
        }
    }
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct HrSample {
    pub bpm: u32,
    pub source_side: u32,
    pub source_side_raw: u32,
    pub reserved: u32,
}

impl From<airpods_client::HeartRateSample> for HrSample {
    fn from(sample: airpods_client::HeartRateSample) -> Self {
        use airpods_client::SourceSide;
        let (source_side, source_side_raw) = match sample.source_side {
            SourceSide::Left => (1, 0),
            SourceSide::Right => (2, 0),
            SourceSide::Unknown(raw) => (0, u32::from(raw)),
        };
        Self {
            bpm: u32::from(sample.bpm),
            source_side,
            source_side_raw,
            reserved: 0,
        }
    }
}

#[derive(Debug)]
pub(crate) struct Hello {
    pub service: String,
    pub experimental: u32,
}

impl From<airpods_client::Hello> for Hello {
    fn from(value: airpods_client::Hello) -> Self {
        Self {
            service: value.service,
            experimental: u32::from(value.experimental),
        }
    }
}

#[derive(Debug)]
pub(crate) struct Status {
    pub state: u32,
    pub subscriber_count: u64,
    pub unknown_state: Option<String>,
}

impl From<airpods_client::Status> for Status {
    fn from(value: airpods_client::Status) -> Self {
        use airpods_client::DaemonState;
        let (state, unknown_state) = match value.state {
            DaemonState::Stopped => (0, None),
            DaemonState::Starting => (1, None),
            DaemonState::Ready => (2, None),
            DaemonState::StartingHeartRate => (3, None),
            DaemonState::Streaming => (4, None),
            DaemonState::StoppingHeartRate => (5, None),
            DaemonState::Failed => (6, None),
            DaemonState::ShuttingDown => (7, None),
            DaemonState::Unknown(raw) => (255, Some(raw)),
            _ => (255, None),
        };
        Self {
            state,
            subscriber_count: value.subscriber_count,
            unknown_state,
        }
    }
}
