//! Deterministic heart-rate capture semantics. Clock values are supplied by the caller.
use crate::HEART_RATE_REPORT_SIZE;
use std::collections::BTreeSet;

pub const SCHEMA_VERSION: u8 = 1;
pub const SEQUENCE_WIDTH_BITS: u32 = u16::BITS;
pub const TIMESTAMP_WIDTH_BITS: u32 = u64::BITS;
pub const FLAGS_WIDTH_BITS: u32 = u32::BITS;
pub const TRANSPORT: &str = "bluez-kernel-coexistence";
pub const HEADER_RECORD_TYPE: &str = "capture_header";
pub const SAMPLE_RECORD_TYPE: &str = "sample";
pub const SUMMARY_RECORD_TYPE: &str = "capture_summary";

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
#[derive(Clone, Debug, PartialEq)]
pub struct SampleRecord {
    pub scenario: Scenario,
    pub cycle_index: usize,
    pub sample_index_within_cycle: usize,
    pub sample_index_global: usize,
    pub receive_monotonic_ns: i128,
    pub delta_from_previous_report_ms: Option<f64>,
    pub milliseconds_since_activation_ack: f64,
    pub report: ReportFacts,
    pub flags_hex: String,
    pub flags_bits_set: Vec<u8>,
    pub sequence_delta_modulo: Option<u16>,
    pub timestamp_delta_modulo: Option<u64>,
    pub duplicate_parsed_report: bool,
    pub raw_report_hex: String,
}

pub fn flags_bits_set(flags: u32) -> Vec<u8> {
    (0..32).filter(|bit| flags & (1_u32 << bit) != 0).collect()
}
pub fn flags_hex(flags: u32) -> String {
    format!("0x{flags:08x}")
}
// i128 safely covers real monotonic clocks; extreme opposite-sign inputs use
// floating arithmetic rather than overflowing a debug build.
fn millis_between(current: i128, previous: i128) -> f64 {
    current.checked_sub(previous).map_or_else(
        || (current as f64 - previous as f64) / 1_000_000.0,
        |difference| difference as f64 / 1_000_000.0,
    )
}
fn raw_hex(raw: &[u8]) -> String {
    raw.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[derive(Clone, Debug, Default)]
struct Cycle {
    attempted: bool,
    activation_ack_ns: Option<i128>,
    complete: bool,
    sample_count: usize,
}
#[derive(Clone, Debug)]
pub struct Recorder {
    scenario: Scenario,
    requested_samples: Option<i64>,
    requested_samples_per_cycle: Option<i64>,
    restart_delay_seconds: f64,
    cycles: Vec<Cycle>,
    header_written: bool,
    summary_written: bool,
    active_cycle: Option<usize>,
    records: Vec<SampleRecord>,
}
impl Recorder {
    pub fn new(
        scenario: Scenario,
        requested_samples: Option<i64>,
        requested_samples_per_cycle: Option<i64>,
        restart_delay_seconds: f64,
    ) -> Self {
        Self {
            scenario,
            requested_samples,
            requested_samples_per_cycle,
            restart_delay_seconds,
            cycles: vec![Cycle::default(); scenario.cycle_count()],
            header_written: false,
            summary_written: false,
            active_cycle: None,
            records: Vec::new(),
        }
    }
    pub fn header_written(&self) -> bool {
        self.header_written
    }
    pub fn cycle_count(&self) -> usize {
        self.cycles.len()
    }
    pub fn scenario(&self) -> Scenario {
        self.scenario
    }
    pub fn records(&self) -> &[SampleRecord] {
        &self.records
    }
    pub fn prepare_header(&self) -> Result<(), SemanticsError> {
        if self.header_written {
            Err(SemanticsError::DuplicateHeader)
        } else {
            Ok(())
        }
    }
    pub fn header(
        &self,
        descriptor_complete: bool,
        local_rx_imtu: Option<u32>,
    ) -> Result<HeaderRecord, SemanticsError> {
        self.prepare_header()?;
        Ok(HeaderRecord {
            scenario: self.scenario,
            requested_samples: self.requested_samples,
            requested_samples_per_cycle: self.requested_samples_per_cycle,
            restart_delay_seconds: self.restart_delay_seconds,
            descriptor_complete,
            local_rx_imtu,
        })
    }
    pub fn commit_header(&mut self) -> Result<(), SemanticsError> {
        self.prepare_header()?;
        self.header_written = true;
        Ok(())
    }
    fn cycle(&self, index: usize) -> Result<&Cycle, SemanticsError> {
        self.cycles
            .get(index.checked_sub(1).ok_or(SemanticsError::InvalidCycle)?)
            .ok_or(SemanticsError::InvalidCycle)
    }
    fn cycle_mut(&mut self, index: usize) -> Result<&mut Cycle, SemanticsError> {
        self.cycles
            .get_mut(index.checked_sub(1).ok_or(SemanticsError::InvalidCycle)?)
            .ok_or(SemanticsError::InvalidCycle)
    }
    pub fn mark_cycle_attempted(&mut self, index: usize) -> Result<(), SemanticsError> {
        let cycle = self.cycle_mut(index)?;
        if cycle.attempted {
            return Err(SemanticsError::SingleUseCycle);
        }
        cycle.attempted = true;
        Ok(())
    }
    pub fn validate_begin_cycle(&self, index: usize) -> Result<(), SemanticsError> {
        if !self.header_written {
            return Err(SemanticsError::MissingHeader);
        }
        let cycle = self.cycle(index)?;
        if !cycle.attempted || cycle.activation_ack_ns.is_some() {
            return Err(SemanticsError::UnexpectedAck);
        }
        if self.active_cycle.is_some() {
            return Err(SemanticsError::ActiveCycle);
        }
        Ok(())
    }
    pub fn begin_cycle(&mut self, index: usize, ack_ns: i128) -> Result<(), SemanticsError> {
        self.validate_begin_cycle(index)?;
        self.cycle_mut(index)?.activation_ack_ns = Some(ack_ns);
        self.active_cycle = Some(index);
        Ok(())
    }
    pub fn validate_sample(&self) -> Result<(), SemanticsError> {
        self.active_cycle
            .ok_or(SemanticsError::BeforeAck)
            .map(|_| ())
    }
    pub fn prepare_sample(
        &self,
        report: ReportFacts,
        received_ns: i128,
    ) -> Result<SampleRecord, SemanticsError> {
        let index = self.active_cycle.ok_or(SemanticsError::BeforeAck)?;
        let cycle = self.cycle(index)?;
        let previous = self.records.last();
        Ok(SampleRecord {
            scenario: self.scenario,
            cycle_index: index,
            sample_index_within_cycle: cycle.sample_count + 1,
            sample_index_global: self.records.len() + 1,
            receive_monotonic_ns: received_ns,
            delta_from_previous_report_ms: previous
                .map(|p| millis_between(received_ns, p.receive_monotonic_ns)),
            milliseconds_since_activation_ack: millis_between(
                received_ns,
                cycle.activation_ack_ns.ok_or(SemanticsError::BeforeAck)?,
            ),
            flags_hex: flags_hex(report.flags),
            flags_bits_set: flags_bits_set(report.flags),
            sequence_delta_modulo: previous
                .map(|p| report.sequence.wrapping_sub(p.report.sequence)),
            timestamp_delta_modulo: previous.map(|p| {
                report
                    .timestamp_ticks
                    .wrapping_sub(p.report.timestamp_ticks)
            }),
            duplicate_parsed_report: previous.is_some_and(|p| p.report == report),
            raw_report_hex: raw_hex(&report.raw_report),
            report,
        })
    }
    pub fn commit_sample(&mut self, sample: SampleRecord) -> Result<(), SemanticsError> {
        if self.active_cycle != Some(sample.cycle_index) {
            return Err(SemanticsError::BeforeAck);
        }
        self.cycle_mut(sample.cycle_index)?.sample_count += 1;
        self.records.push(sample);
        Ok(())
    }
    pub fn complete_cycle(&mut self, index: usize) -> Result<(), SemanticsError> {
        self.cycle(index)?;
        if self.active_cycle != Some(index) {
            return Err(SemanticsError::InactiveCycle);
        }
        self.cycle_mut(index)?.complete = true;
        self.active_cycle = None;
        Ok(())
    }
    pub fn abandon_cycle(&mut self, index: usize) {
        if self.active_cycle == Some(index) {
            self.active_cycle = None;
        }
    }
    pub fn prepare_summary(
        &self,
        status: String,
        failure_category: Option<String>,
    ) -> Result<CaptureSummary, SemanticsError> {
        if !self.header_written {
            return Err(SemanticsError::MissingHeader);
        }
        if self.summary_written {
            return Err(SemanticsError::DuplicateSummary);
        }
        let cycle_records: Vec<Vec<&SampleRecord>> = (1..=self.cycles.len())
            .map(|i| self.records.iter().filter(|r| r.cycle_index == i).collect())
            .collect();
        let cycle_summaries = cycle_records
            .iter()
            .enumerate()
            .map(|(i, records)| {
                let cycle = &self.cycles[i];
                CycleSummary {
                    cycle_index: i + 1,
                    attempted: cycle.attempted,
                    activation_ack_observed: cycle.activation_ack_ns.is_some(),
                    complete: cycle.complete,
                    reports_received: records.len(),
                    sequence_first: records.first().map(|r| r.report.sequence),
                    sequence_last: records.last().map(|r| r.report.sequence),
                    timestamp_first: records.first().map(|r| r.report.timestamp_ticks),
                    timestamp_last: records.last().map(|r| r.report.timestamp_ticks),
                    field_5_unique_values: records
                        .iter()
                        .map(|r| r.report.field_5)
                        .collect::<BTreeSet<_>>()
                        .into_iter()
                        .collect(),
                    flags_unique_hex_values: records
                        .iter()
                        .map(|r| r.flags_hex.clone())
                        .collect::<BTreeSet<_>>()
                        .into_iter()
                        .collect(),
                }
            })
            .collect();
        let intervals: Vec<f64> = self
            .records
            .iter()
            .filter_map(|r| r.delta_from_previous_report_ms)
            .collect();
        let sequence_reset = if self.scenario == Scenario::ActivationRestart {
            reset_observation(
                cycle_records[0].last().map(|r| r.report.sequence),
                cycle_records[1].first().map(|r| r.report.sequence),
            )
        } else {
            "unknown"
        };
        let timestamp_reset = if self.scenario == Scenario::ActivationRestart {
            reset_observation(
                cycle_records[0].last().map(|r| r.report.timestamp_ticks),
                cycle_records[1].first().map(|r| r.report.timestamp_ticks),
            )
        } else {
            "unknown"
        };
        Ok(CaptureSummary {
            scenario: self.scenario,
            status,
            failure_category,
            canonical_reports_received: self.records.len(),
            cycles_completed: self.cycles.iter().filter(|c| c.complete).count(),
            first_bpm_per_cycle: cycle_records
                .iter()
                .map(|records| records.first().map(|r| r.report.bpm))
                .collect(),
            last_bpm_per_cycle: cycle_records
                .iter()
                .map(|records| records.last().map(|r| r.report.bpm))
                .collect(),
            unique_bpm_values: self
                .records
                .iter()
                .map(|r| r.report.bpm)
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect(),
            cycle_summaries,
            sequence_reset_observed_between_cycles: sequence_reset,
            timestamp_reset_observed_between_cycles: timestamp_reset,
            flags_bit_positions_observed: self
                .records
                .iter()
                .flat_map(|r| r.flags_bits_set.iter().copied())
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect(),
            receive_interval_ms: IntervalSummary::from_intervals(&intervals),
        })
    }
    pub fn commit_summary(&mut self) -> Result<(), SemanticsError> {
        self.prepare_summary(String::new(), None)?;
        self.summary_written = true;
        Ok(())
    }
}

pub fn reset_observation<T: Ord + Copy + Default>(
    previous: Option<T>,
    current: Option<T>,
) -> &'static str {
    match (previous, current) {
        (Some(p), Some(c)) if c == T::default() && p != T::default() => "yes",
        (Some(p), Some(c)) if c >= p => "no",
        _ => "unknown",
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct CycleSummary {
    pub cycle_index: usize,
    pub attempted: bool,
    pub activation_ack_observed: bool,
    pub complete: bool,
    pub reports_received: usize,
    pub sequence_first: Option<u16>,
    pub sequence_last: Option<u16>,
    pub timestamp_first: Option<u64>,
    pub timestamp_last: Option<u64>,
    pub field_5_unique_values: Vec<u8>,
    pub flags_unique_hex_values: Vec<String>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct IntervalSummary {
    pub count: usize,
    pub min: Option<f64>,
    pub median: Option<f64>,
    pub max: Option<f64>,
}
impl IntervalSummary {
    pub fn from_intervals(values: &[f64]) -> Self {
        let mut ordered = values.to_vec();
        ordered.sort_by(f64::total_cmp);
        let count = ordered.len();
        Self {
            count,
            min: ordered.first().copied(),
            median: if count == 0 {
                None
            } else if count % 2 == 1 {
                Some(ordered[count / 2])
            } else {
                Some((ordered[count / 2 - 1] + ordered[count / 2]) / 2.0)
            },
            max: ordered.last().copied(),
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct CaptureSummary {
    pub scenario: Scenario,
    pub status: String,
    pub failure_category: Option<String>,
    pub canonical_reports_received: usize,
    pub cycles_completed: usize,
    pub first_bpm_per_cycle: Vec<Option<u8>>,
    pub last_bpm_per_cycle: Vec<Option<u8>>,
    pub unique_bpm_values: Vec<u8>,
    pub cycle_summaries: Vec<CycleSummary>,
    pub sequence_reset_observed_between_cycles: &'static str,
    pub timestamp_reset_observed_between_cycles: &'static str,
    pub flags_bit_positions_observed: Vec<u8>,
    pub receive_interval_ms: IntervalSummary,
}

#[cfg(test)]
#[path = "semantics_tests.rs"]
mod tests;
