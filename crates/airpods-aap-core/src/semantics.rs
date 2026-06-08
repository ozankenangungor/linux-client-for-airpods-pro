//! Deterministic heart-rate capture semantics. Clock values are supplied by the caller.
use crate :: HEART_RATE_REPORT_SIZE ;


pub const SCHEMA_VERSION: u8 = 1;
pub const SEQUENCE_WIDTH_BITS: u32 = u16::BITS;
pub const TIMESTAMP_WIDTH_BITS: u32 = u64::BITS;
pub const FLAGS_WIDTH_BITS: u32 = u32::BITS;





#[derive(Clone, Debug, PartialEq)]
pub struct HeaderRecord {
    pub scenario: Scenario,
    pub requested_samples: Option<i64>,
    pub requested_samples_per_cycle: Option<i64>,
    pub restart_delay_seconds: f64,
    pub descriptor_complete: bool,
    pub local_rx_imtu: Option<u32>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Scenario {
    Baseline,
    ActivationRestart,
}
impl Scenario {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Baseline => "baseline",
            Self::ActivationRestart => "activation-restart",
        }
    }
    pub const fn cycle_count(self) -> usize {
        match self {
            Self::Baseline => 1,
            Self::ActivationRestart => 2,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SemanticsError {
    BaselinePresence,
    BaselineCount,
    RestartPresence,
    RestartCount,
    DuplicateHeader,
    SingleUseCycle,
    MissingHeader,
    UnexpectedAck,
    ActiveCycle,
    BeforeAck,
    InvalidRawReport,
    InactiveCycle,
    DuplicateSummary,
    InvalidCycle,
}
impl SemanticsError {
    pub const fn message(self) -> &'static str {
        match self {
            Self::BaselinePresence => "baseline requires requested_samples only",
            Self::BaselineCount => "baseline sample count must be positive",
            Self::RestartPresence => "activation-restart requires samples per cycle only",
            Self::RestartCount => "restart sample count must be positive",
            Self::DuplicateHeader => "semantics capture header is already written",
            Self::SingleUseCycle => "HR semantics cycle is single-use",
            Self::MissingHeader => "semantics capture header is not written",
            Self::UnexpectedAck => "unexpected HR activation acknowledgement",
            Self::ActiveCycle => "another HR semantics cycle is active",
            Self::BeforeAck => "HR sample arrived before activation acknowledgement",
            Self::InvalidRawReport => "canonical report does not retain 18 raw bytes",
            Self::InactiveCycle => "completed HR semantics cycle is not active",
            Self::DuplicateSummary => "semantics capture summary is already written",
            Self::InvalidCycle => "cycle index is outside this scenario",
        }
    }
}

pub fn cycle_plan(
    scenario: Scenario,
    requested: Option<i64>,
    per_cycle: Option<i64>,
) -> Result<Vec<i64>, SemanticsError> {
    match scenario {
        Scenario::Baseline => match (requested, per_cycle) {
            (Some(n), None) if n > 0 => Ok(vec![n]),
            (Some(_), None) => Err(SemanticsError::BaselineCount),
            _ => Err(SemanticsError::BaselinePresence),
        },
        Scenario::ActivationRestart => match (requested, per_cycle) {
            (None, Some(n)) if n > 0 => Ok(vec![n, n]),
            (None, Some(_)) => Err(SemanticsError::RestartCount),
            _ => Err(SemanticsError::RestartPresence),
        },
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReportFacts {
    pub bpm: u8,
    pub aux: u8,
    pub sequence: u16,
    pub field_5: u8,
    pub timestamp_ticks: u64,
    pub flags: u32,
    pub raw_report: [u8; HEART_RATE_REPORT_SIZE],
}
pub fn validate_raw_report(raw_report: &[u8]) -> Result<(), SemanticsError> {
    if raw_report.len() == HEART_RATE_REPORT_SIZE {
        Ok(())
    } else {
        Err(SemanticsError::InvalidRawReport)
    }
}
impl ReportFacts {
    pub fn from_fields(
        bpm: u8,
        aux: u8,
        sequence: u16,
        field_5: u8,
        timestamp_ticks: u64,
        flags: u32,
        raw_report: &[u8],
    ) -> Result<Self, SemanticsError> {
        validate_raw_report(raw_report)?;
        let raw_report = raw_report
            .try_into()
            .map_err(|_| SemanticsError::InvalidRawReport)?;
        Ok(Self {
            bpm,
            aux,
            sequence,
            field_5,
            timestamp_ticks,
            flags,
            raw_report,
        })
    }
}


pub fn flags_bits_set(flags: u32) -> Vec<u8> {
    (0..32).filter(|bit| flags & (1_u32 << bit) != 0).collect()
}
pub fn flags_hex(flags: u32) -> String {
    format!("0x{flags:08x}")
}
// i128 safely covers real monotonic clocks; extreme opposite-sign inputs use
// floating arithmetic rather than overflowing a debug build.













#[cfg(test)]
#[path = "semantics_tests.rs"]
mod tests;
