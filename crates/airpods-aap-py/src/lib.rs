#![forbid(unsafe_code)]
//! Private PyO3 bridge to authoritative `airpods-aap-core` analysis.

use airpods_aap_core::{
    AapFrameSummary, AapType2bFrameSummary, ControlFrameSummary, DescriptorEvidence,
    HeartRateParseError, HeartRateReport, SourceSide, parse_heart_rate_packet as parse_core,
};
use pyo3::create_exception;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyModule, PyTuple};

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
#[pyclass(
    frozen,
    module = "airpods_hr._airpods_aap_core",
    name = "_HeartRateReport"
)]
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

type Type2bValues = (
    usize,
    Option<u8>,
    Option<u16>,
    Option<usize>,
    Option<bool>,
    Option<bool>,
    Option<usize>,
    Option<usize>,
    Vec<(u8, u16, usize)>,
    Option<bool>,
);

fn type_2b_values(summary: AapType2bFrameSummary) -> Type2bValues {
    (
        summary.frame_length,
        summary.header_u8_6,
        summary.declared_body_length_u16_7_8,
        summary.actual_body_length_after_offset_17,
        summary.declared_body_length_consistent,
        summary.body_aligned_to_17_bytes,
        summary.record_count_17,
        summary.record_suffix_distinct_count,
        summary
            .record_suffix_histogram
            .into_iter()
            .map(|item| (item.suffix_field_u8, item.suffix_field_u16, item.count))
            .collect(),
        summary.unit_bytes_8_13_uniform,
    )
}

#[pyfunction]
fn merge_descriptor_evidence(
    frame: &Bound<'_, PyBytes>,
    sensor_framework: bool,
    heart_rate_service: bool,
    heart_rate: bool,
    heartrate_access: bool,
) -> (bool, bool, bool, bool) {
    let result = DescriptorEvidence {
        sensor_framework,
        heart_rate_service,
        heart_rate,
        heartrate_access,
    }
    .merged(frame.as_bytes());
    (
        result.sensor_framework,
        result.heart_rate_service,
        result.heart_rate,
        result.heartrate_access,
    )
}

#[pyfunction]
fn summarize_type_2b_frame(frame: &Bound<'_, PyBytes>) -> Type2bValues {
    type_2b_values(AapType2bFrameSummary::from_frame(frame.as_bytes()))
}

#[pyfunction]
fn summarize_aap_frame(
    frame: &Bound<'_, PyBytes>,
) -> (usize, Option<u16>, Option<u16>, Option<Type2bValues>) {
    let AapFrameSummary {
        length,
        header_u16_2_3,
        header_u16_4_5,
        type_2b_summary,
    } = AapFrameSummary::from_frame(frame.as_bytes());
    (
        length,
        header_u16_2_3,
        header_u16_4_5,
        type_2b_summary.map(type_2b_values),
    )
}

/// Convert only the core model's fields; all frame interpretation stays in core.
#[pyfunction]
fn summarize_control_frame<'py>(
    py: Python<'py>,
    frame: &Bound<'py, PyBytes>,
) -> PyResult<Bound<'py, PyDict>> {
    let summary = ControlFrameSummary::from_frame(frame.as_bytes());
    let result = PyDict::new(py);
    result.set_item("length", summary.length)?;
    result.set_item("header_u16_2_3", summary.header_u16_2_3)?;
    result.set_item("header_u16_4_5", summary.header_u16_4_5)?;
    result.set_item("word_u16_8_9", summary.word_u16_8_9)?;
    result.set_item("word_u16_10_11", summary.word_u16_10_11)?;
    result.set_item(
        "outer_service_envelope_match",
        summary.outer_service_envelope_match,
    )?;
    result.set_item("fixed_word_10_00_match", summary.fixed_word_10_00_match)?;
    result.set_item(
        "trailing_length_consistent",
        summary.trailing_length_consistent,
    )?;
    result.set_item("tag_08_at_offset_12", summary.tag_08_at_offset_12)?;
    result.set_item("service_ack_suffix_0e", summary.service_ack_suffix_0e)?;
    result.set_item("service_ack_suffix_13", summary.service_ack_suffix_13)?;
    result.set_item(
        "candidate_identifier_terminated",
        summary.candidate_identifier_terminated,
    )?;
    result.set_item(
        "candidate_identifier_octets",
        summary.candidate_identifier_octets,
    )?;
    result.set_item(
        "candidate_identifier_canonical",
        summary.candidate_identifier_canonical,
    )?;
    result.set_item(
        "identifier_is_current_canonical_1_or_2",
        summary.identifier_is_current_canonical_1_or_2,
    )?;
    result.set_item("post_identifier_length", summary.post_identifier_length)?;
    result.set_item(
        "post_identifier_prefix_octet_0",
        summary.post_identifier_prefix_octet_0,
    )?;
    result.set_item(
        "post_identifier_prefix_octet_1",
        summary.post_identifier_prefix_octet_1,
    )?;
    result.set_item(
        "post_identifier_starts_10_01",
        summary.post_identifier_starts_10_01,
    )?;
    result.set_item(
        "post_identifier_prefix_is_observed",
        summary.post_identifier_prefix_is_observed,
    )?;
    result.set_item(
        "post_identifier_field_tag",
        summary.post_identifier_field_tag,
    )?;
    result.set_item(
        "post_identifier_field_parameter",
        summary.post_identifier_field_parameter,
    )?;
    result.set_item(
        "remainder_is_ack_0e_shape",
        summary.remainder_is_ack_0e_shape,
    )?;
    result.set_item(
        "remainder_is_ack_13_shape",
        summary.remainder_is_ack_13_shape,
    )?;
    result.set_item(
        "remainder_is_bootstrap_10_shape",
        summary.remainder_is_bootstrap_10_shape,
    )?;
    result.set_item(
        "remainder_is_bootstrap_11_12_13_shape",
        summary.remainder_is_bootstrap_11_12_13_shape,
    )?;
    result.set_item("terminal_tag_08", summary.terminal_tag_08)?;
    result.set_item("terminal_value", summary.terminal_value)?;
    result.set_item(
        "observed_62_02_08_group_count",
        summary.observed_62_02_08_group_count,
    )?;
    result.set_item(
        "observed_62_02_08_terminal_values",
        summary
            .observed_62_02_08_terminal_values
            .map(|values| PyTuple::new(py, values))
            .transpose()?,
    )?;
    result.set_item(
        "observed_62_02_08_group_offsets",
        summary
            .observed_62_02_08_group_offsets
            .map(|values| PyTuple::new(py, values))
            .transpose()?,
    )?;
    result.set_item(
        "bootstrap_tail_10_suffix_present",
        summary.bootstrap_tail_10_suffix_present,
    )?;
    result.set_item(
        "bootstrap_tail_11_12_13_suffix_present",
        summary.bootstrap_tail_11_12_13_suffix_present,
    )?;
    result.set_item("bootstrap_tail_10", summary.bootstrap_tail_10)?;
    result.set_item("bootstrap_tail_11_12_13", summary.bootstrap_tail_11_12_13)?;
    result.set_item(
        "heart_rate_marker_present",
        summary.heart_rate_marker_present,
    )?;
    Ok(result)
}

#[pyfunction]
fn is_observed_service_ack(frame: &Bound<'_, PyBytes>, service_id: u8) -> bool {
    airpods_aap_core::is_observed_service_ack(frame.as_bytes(), service_id)
}

#[pyfunction]
fn is_service_ack_candidate_shape(frame: &Bound<'_, PyBytes>) -> bool {
    airpods_aap_core::is_service_ack_candidate_shape(frame.as_bytes())
}

#[pyfunction]
fn is_connect4_ack(frame: &Bound<'_, PyBytes>) -> bool {
    airpods_aap_core::is_connect4_ack(frame.as_bytes())
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
    module.add_function(wrap_pyfunction!(merge_descriptor_evidence, module)?)?;
    module.add_function(wrap_pyfunction!(summarize_type_2b_frame, module)?)?;
    module.add_function(wrap_pyfunction!(summarize_aap_frame, module)?)?;
    module.add_function(wrap_pyfunction!(summarize_control_frame, module)?)?;
    module.add_function(wrap_pyfunction!(is_observed_service_ack, module)?)?;
    module.add_function(wrap_pyfunction!(is_service_ack_candidate_shape, module)?)?;
    module.add_function(wrap_pyfunction!(is_connect4_ack, module)?)?;
    Ok(())
}
