#![forbid(unsafe_code)]
//! Private PyO3 bridge to `airpods-aap-core` for development parity tests.

use airpods_aap_core::{
    HeartRateParseError, HeartRateReport, SourceSide, parse_heart_rate_packet as parse_core,
};
use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyModule};

create_exception!(
    _airpods_aap_core,
    MarkerNotFoundError,
    PyValueError,
    "The heart-rate marker is absent."
);
create_exception!(
    _airpods_aap_core,
    TruncatedReportError,
    PyValueError,
    "The heart-rate report is shorter than 18 bytes."
);
create_exception!(
    _airpods_aap_core,
    InvalidReportIdError,
    PyValueError,
    "The heart-rate report ID is not the verified value."
);

/// Private Python view of the neutral core report.
#[pyclass(frozen, module = "_airpods_aap_core", name = "_HeartRateReport")]
struct PyHeartRateReport {
    inner: HeartRateReport,
}

#[pymethods]
impl PyHeartRateReport {
    #[getter]
    const fn bpm(&self) -> u8 {
        self.inner.bpm
    }

    #[getter]
    const fn aux(&self) -> u8 {
        self.inner.aux
    }

    #[getter]
    const fn sequence(&self) -> u16 {
        self.inner.sequence
    }

    #[getter]
    const fn field_5(&self) -> u8 {
        self.inner.field_5
    }

    #[getter]
    const fn timestamp_ticks(&self) -> u64 {
        self.inner.timestamp_ticks
    }

    #[getter]
    const fn flags(&self) -> u32 {
        self.inner.flags
    }

    #[getter]
    fn raw_report<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, self.inner.raw_report())
    }

    /// Returns `(side, raw)` where `raw` is present only for unknown values.
    fn source_side(&self) -> (&'static str, Option<u8>) {
        match self.inner.source_side() {
            SourceSide::Left => ("left", None),
            SourceSide::Right => ("right", None),
            SourceSide::Unknown(raw) => ("unknown", Some(raw)),
        }
    }
}

/// Parses one packet through the Rust core. PyO3's `PyBytes` extraction keeps
/// this private boundary aligned with Python's bytes-only input contract.
#[pyfunction]
fn parse_heart_rate_packet(packet: &Bound<'_, PyBytes>) -> PyResult<PyHeartRateReport> {
    parse_core(packet.as_bytes())
        .map(|inner| PyHeartRateReport { inner })
        .map_err(map_parse_error)
}

fn map_parse_error(error: HeartRateParseError) -> PyErr {
    match error {
        HeartRateParseError::MarkerNotFound => MarkerNotFoundError::new_err(error.to_string()),
        HeartRateParseError::InvalidLength { .. } => {
            TruncatedReportError::new_err(error.to_string())
        }
        HeartRateParseError::InvalidReportId { .. } => {
            InvalidReportIdError::new_err(error.to_string())
        }
    }
}

#[pymodule]
fn _airpods_aap_core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyHeartRateReport>()?;
    module.add(
        "MarkerNotFoundError",
        module.py().get_type::<MarkerNotFoundError>(),
    )?;
    module.add(
        "TruncatedReportError",
        module.py().get_type::<TruncatedReportError>(),
    )?;
    module.add(
        "InvalidReportIdError",
        module.py().get_type::<InvalidReportIdError>(),
    )?;
    module.add_function(wrap_pyfunction!(parse_heart_rate_packet, module)?)?;
    Ok(())
}
