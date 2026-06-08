//! Conversion-only bridge for deterministic semantics state.
use airpods_aap_core::semantics::{
    self as core, CaptureSummary, Recorder, ReportFacts, SampleRecord, Scenario, SemanticsError,
};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBool, PyBytes, PyDict, PyModule};

fn error(err: SemanticsError) -> PyErr {
    match err {
        SemanticsError::InvalidCycle
        | SemanticsError::BaselinePresence
        | SemanticsError::BaselineCount
        | SemanticsError::RestartPresence
        | SemanticsError::RestartCount => PyValueError::new_err(err.message()),
        _ => PyRuntimeError::new_err(err.message()),
    }
}
fn cycle_index(value: &Bound<'_, PyAny>) -> PyResult<usize> {
    if value.is_instance_of::<PyBool>() {
        return Ok(usize::from(value.extract::<bool>()?));
    }
    value
        .extract::<usize>()
        .map_err(|_| error(SemanticsError::InvalidCycle))
}
fn scenario(value: &str) -> PyResult<Scenario> {
    match value {
        "baseline" => Ok(Scenario::Baseline),
        "activation-restart" => Ok(Scenario::ActivationRestart),
        _ => Err(PyValueError::new_err("invalid HR semantics scenario")),
    }
}

#[pyfunction]
fn semantics_parser_structure() -> (usize, u32, u32, u32) {
    (
        airpods_aap_core::HEART_RATE_REPORT_SIZE,
        core::SEQUENCE_WIDTH_BITS,
        core::TIMESTAMP_WIDTH_BITS,
        core::FLAGS_WIDTH_BITS,
    )
}
#[pyfunction]
fn semantics_schema_version() -> u8 {
    core::SCHEMA_VERSION
}
#[pyfunction]
fn semantics_cycle_plan(
    name: &str,
    requested: Option<i64>,
    per_cycle: Option<i64>,
) -> PyResult<Vec<i64>> {
    core::cycle_plan(scenario(name)?, requested, per_cycle).map_err(error)
}

#[pyclass(module = "airpods_hr._airpods_aap_core", name = "HRSemanticsState")]
struct HRSemanticsState {
    core: Recorder,
    pending_sample: Option<SampleRecord>,
}

#[pymethods]
impl HRSemanticsState {
    #[new]
    fn new(
        name: &str,
        requested: Option<i64>,
        per_cycle: Option<i64>,
        restart_delay_seconds: f64,
    ) -> PyResult<Self> {
        let scenario = scenario(name)?;
        // The recorder historically accepts counts without validating them. The session
        // validates its own plan before running; retaining this constructor behavior matters.
        Ok(Self {
            core: Recorder::new(scenario, requested, per_cycle, restart_delay_seconds),
            pending_sample: None,
        })
    }
    #[getter]
    fn header_written(&self) -> bool {
        self.core.header_written()
    }
    fn prepare_header<'py>(
        &self,
        py: Python<'py>,
        descriptor_complete: bool,
        local_rx_imtu: Option<u32>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let header = self
            .core
            .header(descriptor_complete, local_rx_imtu)
            .map_err(error)?;
        let dict = PyDict::new(py);
        dict.set_item("schema_version", core::SCHEMA_VERSION)?;
        dict.set_item("record_type", core::HEADER_RECORD_TYPE)?;
        dict.set_item("scenario", header.scenario.as_str())?;
        dict.set_item("requested_samples", header.requested_samples)?;
        dict.set_item(
            "requested_samples_per_cycle",
            header.requested_samples_per_cycle,
        )?;
        dict.set_item("restart_delay_seconds", header.restart_delay_seconds)?;
        dict.set_item("descriptor_complete", header.descriptor_complete)?;
        dict.set_item("transport", core::TRANSPORT)?;
        dict.set_item("local_rx_imtu", header.local_rx_imtu)?;
        dict.set_item(
            "canonical_report_length",
            airpods_aap_core::HEART_RATE_REPORT_SIZE,
        )?;
        dict.set_item("sequence_width_bits", core::SEQUENCE_WIDTH_BITS)?;
        dict.set_item("timestamp_width_bits", core::TIMESTAMP_WIDTH_BITS)?;
        dict.set_item("flags_width_bits", core::FLAGS_WIDTH_BITS)?;
        dict.set_item("raw_report_hex_included", true)?;
        Ok(dict)
    }
    fn commit_header(&mut self) -> PyResult<()> {
        self.core.commit_header().map_err(error)
    }
    fn mark_cycle_attempted(&mut self, index: &Bound<'_, PyAny>) -> PyResult<()> {
        self.core
            .mark_cycle_attempted(cycle_index(index)?)
            .map_err(error)
    }
    fn begin_cycle(&mut self, index: &Bound<'_, PyAny>, ack_ns: i128) -> PyResult<()> {
        self.core
            .begin_cycle(cycle_index(index)?, ack_ns)
            .map_err(error)
    }
    fn validate_begin_cycle(&self, index: &Bound<'_, PyAny>) -> PyResult<()> {
        self.core
            .validate_begin_cycle(cycle_index(index)?)
            .map_err(error)
    }
    fn validate_sample(&self) -> PyResult<()> {
        self.core.validate_sample().map_err(error)
    }
    fn validate_raw_report(&self, raw: &Bound<'_, PyAny>) -> PyResult<()> {
        let bytes = raw
            .cast::<PyBytes>()
            .map_err(|_| error(SemanticsError::InvalidRawReport))?;
        core::validate_raw_report(bytes.as_bytes()).map_err(error)
    }
    fn prepare_sample<'py>(
        &mut self,
        py: Python<'py>,
        received_ns: i128,
        fields: (u8, u8, u16, u8, u64, u32),
        raw_report: &Bound<'py, PyBytes>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let facts = ReportFacts::from_fields(
            fields.0,
            fields.1,
            fields.2,
            fields.3,
            fields.4,
            fields.5,
            raw_report.as_bytes(),
        )
        .map_err(error)?;
        let sample = self
            .core
            .prepare_sample(facts, received_ns)
            .map_err(error)?;
        let dict = sample_dict(py, &sample)?;
        self.pending_sample = Some(sample);
        Ok(dict)
    }
    fn commit_sample(&mut self) -> PyResult<()> {
        let sample = self
            .pending_sample
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("no prepared HR semantics sample"))?;
        self.core.commit_sample(sample).map_err(error)
    }
    fn complete_cycle(&mut self, index: &Bound<'_, PyAny>) -> PyResult<()> {
        self.core.complete_cycle(cycle_index(index)?).map_err(error)
    }
    fn abandon_cycle(&mut self, index: &Bound<'_, PyAny>) {
        if let Ok(index) = cycle_index(index) {
            self.core.abandon_cycle(index);
        }
    }
    fn prepare_summary<'py>(
        &self,
        py: Python<'py>,
        status: String,
        failure_category: Option<String>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let summary = self
            .core
            .prepare_summary(status, failure_category)
            .map_err(error)?;
        summary_dict(py, &summary)
    }
    fn commit_summary(&mut self) -> PyResult<()> {
        self.core.commit_summary().map_err(error)
    }
}
fn sample_dict<'py>(py: Python<'py>, r: &SampleRecord) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("schema_version", core::SCHEMA_VERSION)?;
    d.set_item("scenario", r.scenario.as_str())?;
    d.set_item("cycle_index", r.cycle_index)?;
    d.set_item("sample_index_within_cycle", r.sample_index_within_cycle)?;
    d.set_item("sample_index_global", r.sample_index_global)?;
    d.set_item("receive_monotonic_ns", r.receive_monotonic_ns)?;
    d.set_item(
        "delta_from_previous_report_ms",
        r.delta_from_previous_report_ms,
    )?;
    d.set_item(
        "milliseconds_since_activation_ack",
        r.milliseconds_since_activation_ack,
    )?;
    d.set_item("bpm", r.report.bpm)?;
    d.set_item("aux", r.report.aux)?;
    d.set_item("sequence", r.report.sequence)?;
    d.set_item("field_5", r.report.field_5)?;
    d.set_item("timestamp_ticks", r.report.timestamp_ticks)?;
    d.set_item("flags_decimal", r.report.flags)?;
    d.set_item("flags_hex", &r.flags_hex)?;
    d.set_item(
        "flags_bits_set",
        r.flags_bits_set
            .iter()
            .map(|x| usize::from(*x))
            .collect::<Vec<_>>(),
    )?;
    d.set_item("sequence_delta_modulo", r.sequence_delta_modulo)?;
    d.set_item("timestamp_delta_modulo", r.timestamp_delta_modulo)?;
    d.set_item("duplicate_parsed_report", r.duplicate_parsed_report)?;
    d.set_item("raw_report_bytes", r.report.raw_report.len())?;
    d.set_item("raw_report_hex", &r.raw_report_hex)?;
    Ok(d)
}
fn summary_dict<'py>(py: Python<'py>, s: &CaptureSummary) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("schema_version", core::SCHEMA_VERSION)?;
    d.set_item("record_type", core::SUMMARY_RECORD_TYPE)?;
    d.set_item("scenario", s.scenario.as_str())?;
    d.set_item("status", &s.status)?;
    d.set_item("failure_category", &s.failure_category)?;
    d.set_item("canonical_reports_received", s.canonical_reports_received)?;
    d.set_item("cycles_completed", s.cycles_completed)?;
    d.set_item("first_bpm_per_cycle", &s.first_bpm_per_cycle)?;
    d.set_item("last_bpm_per_cycle", &s.last_bpm_per_cycle)?;
    d.set_item(
        "unique_bpm_values",
        s.unique_bpm_values
            .iter()
            .map(|x| usize::from(*x))
            .collect::<Vec<_>>(),
    )?;
    let cycle_summaries = pyo3::types::PyList::empty(py);
    for c in &s.cycle_summaries {
        let x = PyDict::new(py);
        x.set_item("cycle_index", c.cycle_index)?;
        x.set_item("attempted", c.attempted)?;
        x.set_item("activation_ack_observed", c.activation_ack_observed)?;
        x.set_item("complete", c.complete)?;
        x.set_item("reports_received", c.reports_received)?;
        x.set_item("sequence_first", c.sequence_first)?;
        x.set_item("sequence_last", c.sequence_last)?;
        x.set_item("timestamp_first", c.timestamp_first)?;
        x.set_item("timestamp_last", c.timestamp_last)?;
        x.set_item(
            "field_5_unique_values",
            c.field_5_unique_values
                .iter()
                .map(|x| usize::from(*x))
                .collect::<Vec<_>>(),
        )?;
        x.set_item("flags_unique_hex_values", &c.flags_unique_hex_values)?;
        cycle_summaries.append(x)?;
    }
    d.set_item("cycle_summaries", cycle_summaries)?;
    d.set_item(
        "sequence_reset_observed_between_cycles",
        s.sequence_reset_observed_between_cycles,
    )?;
    d.set_item(
        "timestamp_reset_observed_between_cycles",
        s.timestamp_reset_observed_between_cycles,
    )?;
    d.set_item(
        "flags_bit_positions_observed",
        s.flags_bit_positions_observed
            .iter()
            .map(|x| usize::from(*x))
            .collect::<Vec<_>>(),
    )?;
    let interval = PyDict::new(py);
    interval.set_item("count", s.receive_interval_ms.count)?;
    interval.set_item("min", s.receive_interval_ms.min)?;
    interval.set_item("median", s.receive_interval_ms.median)?;
    interval.set_item("max", s.receive_interval_ms.max)?;
    d.set_item("receive_interval_ms", interval)?;
    Ok(d)
}
pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(semantics_parser_structure, module)?)?;
    module.add_function(wrap_pyfunction!(semantics_cycle_plan, module)?)?;
    module.add_function(wrap_pyfunction!(semantics_schema_version, module)?)?;
    module.add_class::<HRSemanticsState>()?;
    module.add("SEMANTICS_SAMPLE_RECORD_TYPE", core::SAMPLE_RECORD_TYPE)?;
    Ok(())
}
