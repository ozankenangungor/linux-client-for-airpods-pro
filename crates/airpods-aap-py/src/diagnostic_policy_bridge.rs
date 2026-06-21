//! Conversion of neutral diagnostic facts to and from the pure core.

use airpods_aap_core :: { aap_config_diagnostics as config , aap_local_rx_diagnostics as rx } ;
use pyo3 :: exceptions :: PyValueError ;
use pyo3 :: prelude :: * ;
use pyo3 :: types :: { PyDict , PyModule } ;

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



































pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyLocalRXState>()?;
            module.add_function(wrap_pyfunction!(diagnostic_config_plan, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_config_rewrite, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_config_observation, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_local_rx_rewrite, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_local_rx_parse, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_post_ack_shape, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_post_ack_type_17_limit, module)?)?;
                                                Ok(())
}
