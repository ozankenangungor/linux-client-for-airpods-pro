//! Conversion of neutral diagnostic facts to and from the pure core.

use airpods_aap_core :: { aap_config_diagnostics as config , aap_local_rx_diagnostics as rx , classic_diagnostics as classic } ;
use pyo3 :: exceptions :: PyValueError ;
use pyo3 :: prelude :: * ;
use pyo3 :: types :: { PyAny , PyBytes , PyDict , PyModule , PyString } ;

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





















pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyLocalRXState>()?;
            module.add_function(wrap_pyfunction!(diagnostic_config_plan, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_config_rewrite, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_config_observation, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_local_rx_rewrite, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_local_rx_parse, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_post_ack_shape, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_post_ack_type_17_limit, module)?)?;
        module.add_function(wrap_pyfunction!(diagnostic_runtime_name, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_local_name_matches, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_power_on_facts, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_classic_audit, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_classic_host_snapshot, module)?)?;
                        Ok(())
}
