#![forbid(unsafe_code)]
//! Platform-independent types and parsers for proven AAP wire structures.
//!
//! This crate performs no operating-system I/O. The Python implementation in
//! the repository remains authoritative for production behavior.

mod heart_rate;

pub use heart_rate::{
    HEART_RATE_MARKER, HEART_RATE_REPORT_ID, HEART_RATE_REPORT_SIZE, HeartRateParseError,
    HeartRateReport, SourceSide, parse_heart_rate_packet,
};
