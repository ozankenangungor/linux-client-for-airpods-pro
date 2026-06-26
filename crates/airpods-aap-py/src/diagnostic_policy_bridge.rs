//! Conversion of neutral diagnostic facts to and from the pure core.

use airpods_aap_core::{
    aap_config_diagnostics as config, aap_local_rx_diagnostics as rx,
    classic_diagnostics as classic, heart_rate_diagnostics as hr,
};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyDict, PyModule, PyString};

fn rx_error(error: rx::OptionError) -> PyErr {
    PyValueError::new_err(match error {
        rx::OptionError::Malformed => "host Configure Request options are malformed",
        rx::OptionError::MalformedMtu => "Configure MTU option is malformed",
    })
}

type LocalRXRewriteResult = (Vec<u8>, Vec<u8>, Option<u16>, Option<u16>);

#[pyfunction]
fn diagnostic_config_plan(
    kernel_mtu_only: bool,
    options: &[u8],
    flags: u16,
) -> (bool, Option<Vec<u8>>) {
    match config::request_plan(kernel_mtu_only, options, flags) {
        config::RequestPlan::FailUnknownOptions => (true, None),
        config::RequestPlan::Delegate { mtu_encoded } => (false, mtu_encoded),
    }
}

#[pyfunction]
fn diagnostic_config_rewrite(
    kernel_mtu_only: bool,
    request_identifier: u8,
    response_identifier: u8,
    success: bool,
    mtu_encoded: Option<&[u8]>,
) -> (bool, Option<Vec<u8>>) {
    config::response_rewrite(
        kernel_mtu_only,
        request_identifier,
        response_identifier,
        success,
        mtu_encoded,
    )
}

fn set_summary(
    dict: &Bound<'_, PyDict>,
    prefix: &str,
    summary: config::OptionSummary,
) -> PyResult<()> {
    dict.set_item(format!("{prefix}option_types"), summary.types)?;
    dict.set_item(format!("{prefix}mtu"), summary.mtu)?;
    dict.set_item(format!("{prefix}flush_timeout"), summary.flush_timeout)?;
    dict.set_item(format!("{prefix}rfc_present"), summary.rfc_present)?;
    dict.set_item(format!("{prefix}rfc_mode"), summary.rfc_mode)?;
    Ok(())
}

#[pyfunction]
fn diagnostic_config_observation<'py>(
    py: Python<'py>,
    mode: &str,
    request_options: &[u8],
    response_result: Option<u16>,
    response_options: Option<&[u8]>,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("response_mode", mode)?;
    set_summary(&dict, "peer_", config::summarize_bytes(request_options))?;
    dict.set_item("response_result", response_result)?;
    set_summary(
        &dict,
        "response_",
        config::summarize_bytes(response_options.unwrap_or_default()),
    )?;
    Ok(dict)
}

#[pyfunction]
fn diagnostic_local_rx_rewrite(
    kernel_default: bool,
    options: &[u8],
) -> PyResult<LocalRXRewriteResult> {
    let r = rx::rewrite(kernel_default, options).map_err(rx_error)?;
    Ok((r.options, r.option_types, r.mtu, r.original_mtu))
}

#[pyfunction]
fn diagnostic_local_rx_parse(options: &[u8]) -> PyResult<(Vec<u8>, Option<u16>)> {
    let (options, mtu) = rx::parse(options).map_err(rx_error)?;
    Ok((options.iter().map(|o| o.option_type).collect(), mtu))
}

#[pyclass(module = "airpods_hr._airpods_aap_core", name = "_LocalRXState")]
struct PyLocalRXState {
    inner: rx::State,
}

#[pymethods]
impl PyLocalRXState {
    #[new]
    fn new(kernel_default: bool) -> Self {
        Self {
            inner: rx::State::new(kernel_default),
        }
    }
    #[getter]
    fn request_observed(&self) -> bool {
        self.inner.request_observed
    }
    fn snapshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let s = &self.inner;
        let d = PyDict::new(py);
        d.set_item("mode", s.mode)?;
        d.set_item("request_observed", s.request_observed)?;
        d.set_item("request_option_types", &s.request_option_types)?;
        d.set_item("request_mtu", s.request_mtu)?;
        d.set_item("request_flags", s.request_flags)?;
        d.set_item("request_identifier", s.request_identifier)?;
        d.set_item("request_destination_cid", s.request_destination_cid)?;
        d.set_item("internal_receive_mtu", s.internal_receive_mtu)?;
        d.set_item("peer_response_observed", s.peer_response_observed)?;
        d.set_item("peer_response_result", s.peer_response_result)?;
        d.set_item("peer_response_option_types", &s.peer_response_option_types)?;
        d.set_item("peer_response_mtu", s.peer_response_mtu)?;
        Ok(d)
    }
    fn observe_request(
        &mut self,
        kernel_default: bool,
        options: &[u8],
        flags: u16,
        identifier: u8,
        cid: u16,
        internal_mtu: u16,
    ) -> PyResult<()> {
        let rewrite = rx::rewrite(kernel_default, options).map_err(rx_error)?;
        self.inner
            .observe_request(&rewrite, flags, identifier, cid, internal_mtu);
        Ok(())
    }
    fn identifier_matches(&self, identifier: u8) -> bool {
        self.inner.identifier_matches(identifier)
    }
    fn response_matches(&self, identifier: u8, channel_psm: Option<u16>, aap_psm: u16) -> bool {
        self.inner
            .matches_response(identifier, channel_psm, aap_psm)
    }
    fn observe_response(&mut self, result: u16, options: &[u8]) -> PyResult<()> {
        self.inner
            .observe_response(result, options)
            .map_err(rx_error)
    }
}

#[pyfunction]
fn diagnostic_post_ack_shape(
    summaries: Vec<(Option<u16>, usize)>,
) -> (Option<usize>, Vec<usize>, Option<usize>, usize) {
    let shape = rx::post_ack_shape(&summaries);
    (
        shape.first_type_2b_length,
        shape.type_17_lengths,
        shape.maximum,
        shape.considered,
    )
}

#[pyfunction]
fn diagnostic_post_ack_type_17_limit() -> usize {
    rx::POST_ACK_TYPE_17_LENGTH_LIMIT
}

#[pyfunction]
fn diagnostic_schema_version() -> u8 {
    hr::SCHEMA_VERSION
}

fn profile(value: &str) -> PyResult<classic::Profile> {
    classic::Profile::try_from(value)
        .map_err(|_| PyValueError::new_err("unsupported Classic runtime-name profile"))
}

#[pyfunction]
fn diagnostic_runtime_name(value: &str) -> PyResult<&'static str> {
    Ok(profile(value)?.name())
}

#[pyfunction]
fn diagnostic_local_name_matches(
    value: &Bound<'_, PyAny>,
    profile_id: &str,
) -> PyResult<Option<bool>> {
    let profile = profile(profile_id)?;
    if value.is_instance_of::<PyBytes>() {
        let bytes = value.cast::<PyBytes>()?;
        Ok(classic::local_name_matches(
            classic::LocalName::Bytes(bytes.as_bytes()),
            profile,
        ))
    } else if value.is_instance_of::<PyString>() {
        let string = value.cast::<PyString>()?;
        let text = string.to_string_lossy();
        Ok(classic::local_name_matches(
            classic::LocalName::Text(&text),
            profile,
        ))
    } else {
        Ok(classic::local_name_matches(
            classic::LocalName::Unavailable,
            profile,
        ))
    }
}

#[pyfunction]
fn diagnostic_power_on_facts() -> (Vec<&'static str>, Vec<&'static str>) {
    (
        classic::POWER_ON_CLASSIC_WRITES.to_vec(),
        classic::POWER_ON_NOT_EXPLICITLY_WRITTEN.to_vec(),
    )
}

#[pyfunction]
fn diagnostic_classic_audit<'py>(
    py: Python<'py>,
    profile_id: &str,
    flags: [bool; 15],
    class_of_device: i64,
    io_capability: i64,
    features: Vec<i64>,
) -> PyResult<Bound<'py, PyDict>> {
    let audit = classic::audit(
        profile(profile_id)?,
        flags,
        class_of_device,
        io_capability,
        &features,
    );
    let d = PyDict::new(py);
    d.set_item("runtime_name_profile", profile_id)?;
    let names = [
        "classic_enabled",
        "le_enabled",
        "configured_address_is_default",
        "classic_sc_enabled",
        "classic_ssp_enabled",
        "classic_smp_enabled",
        "classic_accept_any",
        "classic_interlaced_scan_enabled",
        "connectable",
        "discoverable",
        "gap_service_enabled",
        "gatt_service_enabled",
        "enhanced_retransmission_supported",
        "config_keystore_present",
        "keystore_available_before_power_on",
    ];
    for (name, value) in names.iter().zip(audit.flags) {
        d.set_item(name, value)?;
    }
    d.set_item("class_of_device", audit.class_of_device)?;
    d.set_item("io_capability", audit.io_capability)?;
    d.set_item("l2cap_extended_features", audit.l2cap_extended_features)?;
    Ok(d)
}

#[pyfunction]
fn diagnostic_classic_host_snapshot<'py>(
    py: Python<'py>,
    profile_id: &str,
    flags: [bool; 6],
    io_capability: i64,
    name_match: Option<bool>,
    observations: [Option<i64>; 10],
    unavailable: Vec<String>,
) -> PyResult<Bound<'py, PyDict>> {
    let s = classic::host_snapshot(
        profile(profile_id)?,
        flags,
        io_capability,
        name_match,
        observations,
        &unavailable,
    );
    let d = PyDict::new(py);
    d.set_item("runtime_name_profile", profile_id)?;
    let flag_names = [
        "classic_enabled",
        "le_enabled",
        "connectable",
        "discoverable",
        "configured_ssp_enabled",
        "configured_sc_enabled",
    ];
    for (name, value) in flag_names.iter().zip(s.configured_flags) {
        d.set_item(name, value)?;
    }
    d.set_item("configured_io_capability", s.io_capability)?;
    d.set_item("observed_local_name_matches_profile", s.local_name_match)?;
    let observation_names = [
        "observed_class_of_device",
        "observed_authentication_enable",
        "observed_simple_pairing_mode",
        "observed_secure_connections_host_support",
        "observed_scan_enable",
        "observed_page_timeout",
        "observed_page_scan_type",
        "observed_page_scan_interval",
        "observed_page_scan_window",
        "observed_default_link_policy",
    ];
    for (name, value) in observation_names.iter().zip(s.observations) {
        d.set_item(name, value)?;
    }
    d.set_item("unavailable_fields", s.unavailable)?;
    Ok(d)
}

fn hr_error(error: hr::Error) -> PyErr {
    PyValueError::new_err(match error {
        hr::Error::AlreadyStarted => "diagnostic session is already started",
        hr::Error::NotActive => "diagnostic session is not active",
        hr::Error::InvalidPayload => "parsed report does not retain the validated 18-byte payload",
        hr::Error::InvalidCommit => "invalid diagnostic event commit",
    })
}

fn sample_dict<'py>(py: Python<'py>, sample: &hr::Sample) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("schema_version", hr::SCHEMA_VERSION)?;
    d.set_item("event", "heart_rate_sample")?;
    d.set_item("host_monotonic_ns", sample.host_monotonic_ns)?;
    d.set_item("elapsed_ms", sample.elapsed_ms)?;
    d.set_item("bpm", sample.bpm)?;
    d.set_item("aux", sample.aux)?;
    d.set_item("sequence", sample.sequence)?;
    d.set_item("field_5", sample.field_5)?;
    d.set_item("timestamp_ticks", sample.timestamp_ticks)?;
    d.set_item("flags", sample.flags)?;
    d.set_item("raw_report_hex", &sample.raw_report_hex)?;
    Ok(d)
}

#[pyfunction]
fn diagnostic_sample_event<'py>(
    py: Python<'py>,
    reference_ns: i128,
    observed_ns: i128,
    fields: [u64; 6],
    raw: &[u8],
) -> PyResult<Bound<'py, PyDict>> {
    let sample = hr::sample(reference_ns, observed_ns, fields, raw).map_err(hr_error)?;
    sample_dict(py, &sample)
}

#[pyfunction]
fn diagnostic_sample_event_values<'py>(
    py: Python<'py>,
    observed_ns: i128,
    elapsed_ms: i128,
    fields: [u64; 6],
    raw: &[u8],
) -> PyResult<Bound<'py, PyDict>> {
    sample_dict(
        py,
        &hr::sample_values(observed_ns, elapsed_ms, fields, raw).map_err(hr_error)?,
    )
}

#[pyfunction]
fn diagnostic_sample_human_values(
    observed_ns: i128,
    elapsed_ms: i128,
    fields: [u64; 6],
    raw: &[u8],
) -> PyResult<String> {
    Ok(hr::sample_values(observed_ns, elapsed_ms, fields, raw)
        .map_err(hr_error)?
        .human())
}

#[pyfunction]
fn diagnostic_validate_raw(raw: &Bound<'_, PyAny>) -> PyResult<()> {
    let bytes = raw
        .cast::<PyBytes>()
        .map_err(|_| hr_error(hr::Error::InvalidPayload))?;
    hr::validate_raw(bytes.as_bytes()).map_err(hr_error)
}

#[pyfunction]
fn diagnostic_sample_human(
    reference_ns: i128,
    observed_ns: i128,
    fields: [u64; 6],
    raw: &[u8],
) -> PyResult<String> {
    Ok(hr::sample(reference_ns, observed_ns, fields, raw)
        .map_err(hr_error)?
        .human())
}

#[pyclass(
    frozen,
    module = "airpods_hr._airpods_aap_core",
    name = "_DiagnosticPlanToken"
)]
struct PyDiagnosticPlanToken {
    inner: hr::PlanToken,
}

#[pyclass(
    module = "airpods_hr._airpods_aap_core",
    name = "_DiagnosticRecorderState"
)]
struct PyDiagnosticRecorderState {
    inner: hr::State,
}

#[pymethods]
impl PyDiagnosticRecorderState {
    #[new]
    fn new() -> Self {
        Self {
            inner: hr::State::default(),
        }
    }
    #[getter]
    fn session_started(&self) -> bool {
        self.inner.reference_ns.is_some()
    }
    #[getter]
    fn session_stopped(&self) -> bool {
        self.inner.stopped
    }
    #[getter]
    fn sample_events_emitted(&self) -> u64 {
        self.inner.sample_count
    }
    #[getter]
    fn reference_ns(&self) -> Option<i128> {
        self.inner.reference_ns
    }
    fn check_start(&self) -> PyResult<()> {
        self.inner.check_start().map_err(hr_error)
    }
    fn check_active(&self) -> PyResult<i128> {
        self.inner.check_active().map_err(hr_error)
    }
    fn plan_start<'py>(
        &self,
        py: Python<'py>,
        reference_ns: i128,
        wall_clock_utc: &str,
    ) -> PyResult<(PyDiagnosticPlanToken, Bound<'py, PyDict>)> {
        let token = self.inner.plan_start(reference_ns).map_err(hr_error)?;
        let event = hr::start_event(reference_ns, wall_clock_utc);
        let d = PyDict::new(py);
        d.set_item("schema_version", hr::SCHEMA_VERSION)?;
        d.set_item("event", "session_start")?;
        d.set_item("host_monotonic_reference_ns", event.reference_ns)?;
        d.set_item("wall_clock_utc", event.wall_clock_utc)?;
        Ok((PyDiagnosticPlanToken { inner: token }, d))
    }
    fn commit_start(&mut self, token: PyRef<'_, PyDiagnosticPlanToken>) -> PyResult<()> {
        self.inner.commit_start(token.inner).map_err(hr_error)
    }
    fn plan_sample<'py>(
        &self,
        py: Python<'py>,
        observed_ns: i128,
        fields: [u64; 6],
        raw: &[u8],
    ) -> PyResult<(PyDiagnosticPlanToken, Bound<'py, PyDict>)> {
        let (token, sample) = self
            .inner
            .plan_sample(observed_ns, fields, raw)
            .map_err(hr_error)?;
        Ok((
            PyDiagnosticPlanToken { inner: token },
            sample_dict(py, &sample)?,
        ))
    }
    fn commit_sample(&mut self, token: PyRef<'_, PyDiagnosticPlanToken>) -> PyResult<()> {
        self.inner.commit_sample(token.inner).map_err(hr_error)
    }
    fn plan_stop<'py>(
        &self,
        py: Python<'py>,
        observed_ns: i128,
        termination_reason: &Bound<'_, PyAny>,
    ) -> PyResult<(PyDiagnosticPlanToken, Bound<'py, PyDict>)> {
        let (token, event) = self.inner.plan_stop(observed_ns).map_err(hr_error)?;
        let d = PyDict::new(py);
        d.set_item("schema_version", hr::SCHEMA_VERSION)?;
        d.set_item("event", "session_stop")?;
        d.set_item("host_monotonic_ns", event.observed_ns)?;
        d.set_item("elapsed_ms", event.elapsed_ms)?;
        d.set_item("heart_rate_samples_emitted", event.count)?;
        d.set_item("termination_reason", termination_reason)?;
        Ok((PyDiagnosticPlanToken { inner: token }, d))
    }
    fn commit_stop(&mut self, token: PyRef<'_, PyDiagnosticPlanToken>) -> PyResult<()> {
        self.inner.commit_stop(token.inner).map_err(hr_error)
    }
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyLocalRXState>()?;
    module.add_class::<PyDiagnosticPlanToken>()?;
    module.add_class::<PyDiagnosticRecorderState>()?;
    module.add_function(wrap_pyfunction!(diagnostic_config_plan, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_config_rewrite, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_config_observation, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_local_rx_rewrite, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_local_rx_parse, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_post_ack_shape, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_post_ack_type_17_limit, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_schema_version, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_runtime_name, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_local_name_matches, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_power_on_facts, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_classic_audit, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_classic_host_snapshot, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_sample_event, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_sample_human, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_sample_event_values, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_sample_human_values, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_validate_raw, module)?)?;
    Ok(())
}
