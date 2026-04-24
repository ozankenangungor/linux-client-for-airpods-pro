use core::fmt;

/// Marker immediately preceding a canonical heart-rate report in an AAP frame.
pub const HEART_RATE_MARKER: [u8; 6] = [0x3a, 0x16, 0x08, 0x13, 0x1a, 0x12];

/// Exact size of a canonical heart-rate report.
pub const HEART_RATE_REPORT_SIZE: usize = 18;

/// Verified report identifier at byte zero of a canonical report.
pub const HEART_RATE_REPORT_ID: u8 = 0x01;

/// Source-side interpretation supported by controlled evidence.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SourceSide {
    Left,
    Right,
    Unknown(u8),
}

/// Decoded fields and exact bytes from a canonical 18-byte report.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct HeartRateReport {
    pub bpm: u8,
    pub aux: u8,
    pub sequence: u16,
    pub field_5: u8,
    pub timestamp_ticks: u64,
    pub flags: u32,
    raw_report: [u8; HEART_RATE_REPORT_SIZE],
}

impl HeartRateReport {
    /// Returns the controlled source-side interpretation without hiding the
    /// original `field_5` wire value.
    #[must_use]
    pub const fn source_side(&self) -> SourceSide {
        match self.field_5 {
            1 => SourceSide::Left,
            2 => SourceSide::Right,
            raw => SourceSide::Unknown(raw),
        }
    }

    /// Returns the exact canonical report bytes copied from the AAP packet.
    #[must_use]
    pub const fn raw_report(&self) -> &[u8; HEART_RATE_REPORT_SIZE] {
        &self.raw_report
    }
}

/// Failures mapped to the public Python parser exception categories.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HeartRateParseError {
    MarkerNotFound,
    InvalidLength { expected: usize, actual: usize },
    InvalidReportId { expected: u8, actual: u8 },
}

impl fmt::Display for HeartRateParseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MarkerNotFound => formatter.write_str("heart-rate marker not found"),
            Self::InvalidLength { expected, actual } => write!(
                formatter,
                "heart-rate report is truncated: expected {expected} bytes, found {actual}"
            ),
            Self::InvalidReportId { actual, .. } => {
                write!(formatter, "unexpected heart-rate report ID: 0x{actual:02x}")
            }
        }
    }
}

impl std::error::Error for HeartRateParseError {}

/// Finds and decodes the first canonical heart-rate report in an AAP packet.
///
/// This preserves the established Python API contract: it searches for the
/// marker, requires at least 18 bytes after that marker, validates report ID
/// `0x01`, and ignores bytes after the report slice.
pub fn parse_heart_rate_packet(packet: &[u8]) -> Result<HeartRateReport, HeartRateParseError> {
    let marker_offset = packet
        .windows(HEART_RATE_MARKER.len())
        .position(|window| window == HEART_RATE_MARKER)
        .ok_or(HeartRateParseError::MarkerNotFound)?;
    let report_offset = marker_offset + HEART_RATE_MARKER.len();
    let available = packet.len() - report_offset;
    if available < HEART_RATE_REPORT_SIZE {
        return Err(HeartRateParseError::InvalidLength {
            expected: HEART_RATE_REPORT_SIZE,
            actual: available,
        });
    }

    let mut raw_report = [0_u8; HEART_RATE_REPORT_SIZE];
    raw_report.copy_from_slice(&packet[report_offset..report_offset + HEART_RATE_REPORT_SIZE]);

    if raw_report[0] != HEART_RATE_REPORT_ID {
        return Err(HeartRateParseError::InvalidReportId {
            expected: HEART_RATE_REPORT_ID,
            actual: raw_report[0],
        });
    }

    Ok(HeartRateReport {
        bpm: raw_report[1],
        aux: raw_report[2],
        sequence: u16::from_le_bytes([raw_report[3], raw_report[4]]),
        field_5: raw_report[5],
        timestamp_ticks: u64::from_le_bytes([
            raw_report[6],
            raw_report[7],
            raw_report[8],
            raw_report[9],
            raw_report[10],
            raw_report[11],
            raw_report[12],
            raw_report[13],
        ]),
        flags: u32::from_le_bytes([
            raw_report[14],
            raw_report[15],
            raw_report[16],
            raw_report[17],
        ]),
        raw_report,
    })
}
