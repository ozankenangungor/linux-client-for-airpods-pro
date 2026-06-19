//! Pure, transactional heart-rate diagnostic event policy.

pub const SCHEMA_VERSION: u8 = 1;
pub const RAW_REPORT_SIZE: usize = 18;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StartEvent {
    pub reference_ns: i128,
    pub wall_clock_utc: String,
}
pub fn start_event(reference_ns: i128, wall_clock_utc: &str) -> StartEvent {
    StartEvent {
        reference_ns,
        wall_clock_utc: wall_clock_utc.to_owned(),
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StopEvent {
    pub observed_ns: i128,
    pub elapsed_ms: i128,
    pub count: u64,
}
pub fn stop_event(reference_ns: i128, observed_ns: i128, count: u64) -> StopEvent {
    StopEvent {
        observed_ns,
        elapsed_ms: elapsed_ms(reference_ns, observed_ns),
        count,
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    AlreadyStarted,
    NotActive,
    InvalidPayload,
    InvalidCommit,
}

pub fn elapsed_ms(reference_ns: i128, observed_ns: i128) -> i128 {
    // Quotients fit even when the nanosecond subtraction exceeds i128.
    let quotient = observed_ns.div_euclid(1_000_000) - reference_ns.div_euclid(1_000_000);
    let borrow = i128::from(observed_ns.rem_euclid(1_000_000) < reference_ns.rem_euclid(1_000_000));
    quotient - borrow
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Sample {
    pub host_monotonic_ns: i128,
    pub elapsed_ms: i128,
    pub bpm: u8,
    pub aux: u8,
    pub sequence: u16,
    pub field_5: u8,
    pub timestamp_ticks: u64,
    pub flags: u32,
    pub raw_report_hex: String,
}

impl Sample {
    pub fn human(&self) -> String {
        format!(
            "Heart rate diagnostic: host_monotonic_ns={} elapsed_ms={} bpm={} aux={} sequence={} field_5={} timestamp_ticks={} flags={} raw_report_hex={}",
            self.host_monotonic_ns,
            self.elapsed_ms,
            self.bpm,
            self.aux,
            self.sequence,
            self.field_5,
            self.timestamp_ticks,
            self.flags,
            self.raw_report_hex
        )
    }
}

pub fn sample(
    reference_ns: i128,
    observed_ns: i128,
    fields: [u64; 6],
    raw: &[u8],
) -> Result<Sample, Error> {
    sample_values(
        observed_ns,
        elapsed_ms(reference_ns, observed_ns),
        fields,
        raw,
    )
}

pub fn sample_values(
    observed_ns: i128,
    elapsed_ms: i128,
    fields: [u64; 6],
    raw: &[u8],
) -> Result<Sample, Error> {
    if raw.len() != RAW_REPORT_SIZE {
        return Err(Error::InvalidPayload);
    }
    let mut raw_report_hex = String::with_capacity(36);
    for byte in raw {
        use std::fmt::Write;
        write!(&mut raw_report_hex, "{byte:02x}").expect("writing to String");
    }
    Ok(Sample {
        host_monotonic_ns: observed_ns,
        elapsed_ms,
        bpm: u8::try_from(fields[0]).map_err(|_| Error::InvalidPayload)?,
        aux: u8::try_from(fields[1]).map_err(|_| Error::InvalidPayload)?,
        sequence: u16::try_from(fields[2]).map_err(|_| Error::InvalidPayload)?,
        field_5: u8::try_from(fields[3]).map_err(|_| Error::InvalidPayload)?,
        timestamp_ticks: fields[4],
        flags: u32::try_from(fields[5]).map_err(|_| Error::InvalidPayload)?,
        raw_report_hex,
    })
}

pub fn validate_raw(raw: &[u8]) -> Result<(), Error> {
    if raw.len() == RAW_REPORT_SIZE {
        Ok(())
    } else {
        Err(Error::InvalidPayload)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PlanKind {
    Start,
    Sample,
    Stop,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct PlanToken {
    kind: PlanKind,
    generation: u64,
    reference_ns: Option<i128>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct State {
    pub reference_ns: Option<i128>,
    pub sample_count: u64,
    pub stopped: bool,
    generation: u64,
}

impl State {
    fn token(&self, kind: PlanKind, reference_ns: Option<i128>) -> Result<PlanToken, Error> {
        Ok(PlanToken {
            kind,
            generation: self.generation.checked_add(1).ok_or(Error::InvalidCommit)?,
            reference_ns,
        })
    }
    fn commit(&mut self, token: PlanToken, kind: PlanKind) -> Result<(), Error> {
        if token.kind != kind || self.generation.checked_add(1) != Some(token.generation) {
            return Err(Error::InvalidCommit);
        }
        self.generation = token.generation;
        Ok(())
    }
    pub fn check_start(&self) -> Result<(), Error> {
        if self.reference_ns.is_some() {
            Err(Error::AlreadyStarted)
        } else {
            Ok(())
        }
    }
    pub fn check_active(&self) -> Result<i128, Error> {
        match (self.reference_ns, self.stopped) {
            (Some(reference), false) => Ok(reference),
            _ => Err(Error::NotActive),
        }
    }
    pub fn plan_start(&self, reference_ns: i128) -> Result<PlanToken, Error> {
        self.check_start()?;
        self.token(PlanKind::Start, Some(reference_ns))
    }
    pub fn plan_sample(
        &self,
        observed_ns: i128,
        fields: [u64; 6],
        raw: &[u8],
    ) -> Result<(PlanToken, Sample), Error> {
        let reference = self.check_active()?;
        let event = sample(reference, observed_ns, fields, raw)?;
        Ok((self.token(PlanKind::Sample, None)?, event))
    }
    pub fn plan_stop(&self, observed_ns: i128) -> Result<(PlanToken, StopEvent), Error> {
        let reference = self.check_active()?;
        let event = stop_event(reference, observed_ns, self.sample_count);
        Ok((self.token(PlanKind::Stop, None)?, event))
    }
    pub fn commit_start(&mut self, token: PlanToken) -> Result<(), Error> {
        self.check_start()?;
        let reference_ns = token.reference_ns.ok_or(Error::InvalidCommit)?;
        self.commit(token, PlanKind::Start)?;
        self.reference_ns = Some(reference_ns);
        Ok(())
    }
    pub fn commit_sample(&mut self, token: PlanToken) -> Result<(), Error> {
        self.check_active()?;
        let count = self
            .sample_count
            .checked_add(1)
            .ok_or(Error::InvalidCommit)?;
        self.commit(token, PlanKind::Sample)?;
        self.sample_count = count;
        Ok(())
    }
    pub fn commit_stop(&mut self, token: PlanToken) -> Result<(), Error> {
        self.check_active()?;
        self.commit(token, PlanKind::Stop)?;
        self.stopped = true;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const RAW: [u8; 18] = [
        1, 0x48, 0xa5, 0x34, 0x12, 0x5a, 8, 7, 6, 5, 4, 3, 2, 1, 0xef, 0xcd, 0xab, 0x89,
    ];
    const FIELDS: [u64; 6] = [72, 165, 4660, 90, 0x0102030405060708, 0x89abcdef];
    #[test]
    fn floor_division_and_large_timestamps() {
        for (delta, expected) in [
            (0, 0),
            (999_999, 0),
            (1_000_000, 1),
            (1_999_999, 1),
            (-1, -1),
            (-999_999, -1),
            (-1_000_000, -1),
            (-1_000_001, -2),
        ] {
            assert_eq!(elapsed_ms(0, delta), expected);
        }
        assert_eq!(elapsed_ms(i128::MAX - 2, i128::MAX - 1), 0);
        assert_eq!(elapsed_ms(i128::MIN + 2, i128::MIN + 1), -1);
        assert!(elapsed_ms(i128::MIN, i128::MAX) > 0);
        assert!(elapsed_ms(i128::MAX, i128::MIN) < 0);
        let mut seed: u64 = 0xaced1234;
        for _ in 0..10_000 {
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            let reference = (seed as i64) as i128;
            seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
            let observed = (seed as i64) as i128;
            assert_eq!(
                elapsed_ms(reference, observed),
                (observed - reference).div_euclid(1_000_000)
            );
        }
    }
    #[test]
    fn sample_golden_and_transactional_lifecycle() {
        let mut state = State::default();
        assert_eq!(state.check_active(), Err(Error::NotActive));
        let before = state.clone();
        let token = state.plan_start(1_000_000_000).unwrap();
        assert_eq!(state, before);
        let mut invalid = token;
        invalid.generation += 1;
        assert_eq!(state.commit_start(invalid), Err(Error::InvalidCommit));
        state.commit_start(token).unwrap();
        assert_eq!(state.plan_start(2), Err(Error::AlreadyStarted));
        assert_eq!(state.commit_sample(token), Err(Error::InvalidCommit));
        let before = state.clone();
        let (token, event) = state.plan_sample(1_123_456_789, FIELDS, &RAW).unwrap();
        assert_eq!(state, before);
        assert_eq!(event.elapsed_ms, 123);
        assert_eq!(event.raw_report_hex, "0148a534125a0807060504030201efcdab89");
        assert_eq!(
            event.human(),
            "Heart rate diagnostic: host_monotonic_ns=1123456789 elapsed_ms=123 bpm=72 aux=165 sequence=4660 field_5=90 timestamp_ticks=72623859790382856 flags=2309737967 raw_report_hex=0148a534125a0807060504030201efcdab89"
        );
        state.commit_sample(token).unwrap();
        assert_eq!(state.commit_sample(token), Err(Error::InvalidCommit));
        assert_eq!(state.sample_count, 1);
        assert_eq!(
            state.plan_sample(1, FIELDS, &RAW[..17]),
            Err(Error::InvalidPayload)
        );
        let before = state.clone();
        let (token, event) = state.plan_stop(1_500_000_000).unwrap();
        assert_eq!(state, before);
        assert_eq!((event.elapsed_ms, event.count), (500, 1));
        assert!(!state.stopped);
        state.commit_stop(token).unwrap();
        assert!(state.stopped);
        assert_eq!(state.plan_stop(2), Err(Error::NotActive));
    }
}
