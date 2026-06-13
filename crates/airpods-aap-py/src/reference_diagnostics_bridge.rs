//! Neutral conversion boundary for reference diagnostic state and response data.
use airpods_aap_core::{pre_aap_diagnostics as aap, pre_auth_diagnostics as auth};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyDict, PyModule};

fn auth_result(value: &str) -> PyResult<auth::ResultKind> {
    value
        .try_into()
        .map_err(|_| PyValueError::new_err("unknown remote discovery result"))
}
fn aap_result(value: &str) -> PyResult<aap::ResultKind> {
    value
        .try_into()
        .map_err(|_| PyValueError::new_err("unknown information response result"))
}
fn info_type(value: &Bound<'_, PyAny>) -> PyResult<aap::InformationType> {
    value
        .extract::<u16>()
        .map_err(|_| PyValueError::new_err("unsupported diagnostic Information Type"))?
        .try_into()
        .map_err(|_| PyValueError::new_err("unsupported diagnostic Information Type"))
}
fn nonnegative_u64(value: &Bound<'_, PyAny>) -> Option<u64> {
    value.extract::<u64>().ok()
}

#[pyclass(
    module = "airpods_hr._airpods_aap_core",
    name = "_PreAuthDiagnosticState"
)]
pub struct PyPreAuthState {
    inner: auth::State,
}
#[pymethods]
impl PyPreAuthState {
    #[new]
    fn new(mode: &str) -> PyResult<Self> {
        let mode = mode
            .try_into()
            .map_err(|_| PyValueError::new_err("unknown pre-auth mode"))?;
        Ok(Self {
            inner: auth::State::new(mode),
        })
    }
    fn snapshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let s = &self.inner;
        let d = PyDict::new(py);
        d.set_item("mode", s.mode.as_str())?;
        d.set_item("delay_ms", s.mode.delay_ms())?;
        d.set_item(
            "remote_supported_features_request_sent",
            s.supported_request_sent,
        )?;
        d.set_item(
            "remote_supported_features_command_accepted",
            s.supported_command_accepted,
        )?;
        d.set_item(
            "remote_supported_features_response_observed",
            s.supported_response_observed,
        )?;
        d.set_item(
            "remote_supported_features_result",
            s.supported_result.as_str(),
        )?;
        d.set_item("remote_supported_features_mask", s.supported_mask)?;
        d.set_item(
            "remote_extended_features_request_sent",
            s.extended_request_sent,
        )?;
        d.set_item(
            "remote_extended_features_command_accepted",
            s.extended_command_accepted,
        )?;
        d.set_item("remote_extended_features_page", s.extended_page())?;
        d.set_item(
            "remote_extended_features_response_observed",
            s.extended_response_observed,
        )?;
        d.set_item(
            "remote_extended_features_result",
            s.extended_result.as_str(),
        )?;
        d.set_item("remote_extended_features_max_page", s.extended_max_page)?;
        d.set_item("remote_extended_features_mask", s.extended_mask)?;
        d.set_item("remote_name_request_sent", s.name_request_sent)?;
        d.set_item("remote_name_command_accepted", s.name_command_accepted)?;
        d.set_item("remote_name_response_observed", s.name_response_observed)?;
        d.set_item("remote_name_result", s.name_result.as_str())?;
        d.set_item("authentication_attempted", s.authentication_attempted)?;
        Ok(d)
    }
    fn supported_request(&mut self) {
        self.inner.supported_request();
    }
    fn supported_accepted(&mut self) {
        self.inner.supported_accepted();
    }
    fn supported_response(
        &mut self,
        observed: bool,
        result: &str,
        mask: Option<u64>,
    ) -> PyResult<()> {
        self.inner
            .supported_response(observed, auth_result(result)?, mask);
        Ok(())
    }
    fn extended_request(&mut self) {
        self.inner.extended_request();
    }
    fn extended_accepted(&mut self) {
        self.inner.extended_accepted();
    }
    fn extended_response(
        &mut self,
        observed: bool,
        result: &str,
        maximum_page: Option<u8>,
        mask: Option<u64>,
    ) -> PyResult<()> {
        self.inner
            .extended_response(observed, auth_result(result)?, maximum_page, mask);
        Ok(())
    }
    fn name_request(&mut self) {
        self.inner.name_request();
    }
    fn name_accepted(&mut self) {
        self.inner.name_accepted();
    }
    fn name_response(&mut self, observed: bool, result: &str) -> PyResult<()> {
        self.inner.name_response(observed, auth_result(result)?);
        Ok(())
    }
    fn mark_authentication_attempted(&mut self) {
        self.inner.mark_authentication_attempted();
    }
}

#[pyclass(
    module = "airpods_hr._airpods_aap_core",
    name = "_PreAAPDiagnosticState"
)]
pub struct PyPreAAPState {
    inner: aap::State,
}
#[pymethods]
impl PyPreAAPState {
    #[new]
    fn new(mode: &str) -> PyResult<Self> {
        let mode = mode
            .try_into()
            .map_err(|_| PyValueError::new_err("unknown pre-AAP mode"))?;
        Ok(Self {
            inner: aap::State::new(mode),
        })
    }
    fn snapshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let s = &self.inner;
        let d = PyDict::new(py);
        d.set_item("mode", s.mode.as_str())?;
        d.set_item("delay_ms", s.mode.delay_ms())?;
        d.set_item("extended_features_request_sent", s.extended_request_sent)?;
        d.set_item(
            "extended_features_response_observed",
            s.extended_response_observed,
        )?;
        d.set_item("extended_features_result", s.extended_result.as_str())?;
        d.set_item("extended_features_mask", s.extended_mask)?;
        d.set_item("fixed_channels_request_sent", s.fixed_request_sent)?;
        d.set_item(
            "fixed_channels_response_observed",
            s.fixed_response_observed,
        )?;
        d.set_item("fixed_channels_result", s.fixed_result.as_str())?;
        d.set_item("fixed_channels_mask", s.fixed_mask)?;
        d.set_item("aap_open_attempted", s.aap_open_attempted)?;
        Ok(d)
    }
    fn request_sent(&mut self, info: &Bound<'_, PyAny>) -> PyResult<()> {
        self.inner.request_sent(info_type(info)?);
        Ok(())
    }
    fn response(
        &mut self,
        info: &Bound<'_, PyAny>,
        observed: bool,
        result: &str,
        mask: Option<u64>,
    ) -> PyResult<()> {
        let info = info_type(info)?;
        let result = aap_result(result)?;
        self.inner.response(info, observed, result, mask);
        Ok(())
    }
    fn mark_aap_open_attempted(&mut self) {
        self.inner.mark_aap_open_attempted();
    }
}

#[pyfunction]
fn pre_auth_supported_completion(
    status: &Bound<'_, PyAny>,
    page: &Bound<'_, PyAny>,
    features: &Bound<'_, PyAny>,
) -> (&'static str, Option<u64>) {
    let r = auth::supported_completion(
        nonnegative_u64(status),
        nonnegative_u64(page),
        nonnegative_u64(features),
    );
    (r.result.as_str(), r.mask)
}
#[pyfunction]
fn pre_auth_extended_completion(
    status: &Bound<'_, PyAny>,
    page: &Bound<'_, PyAny>,
    maximum_page: &Bound<'_, PyAny>,
    features: &Bound<'_, PyAny>,
) -> (&'static str, Option<u64>, Option<u8>) {
    let r = auth::extended_completion(
        nonnegative_u64(status),
        nonnegative_u64(page),
        nonnegative_u64(maximum_page),
        nonnegative_u64(features),
    );
    (r.result.as_str(), r.mask, r.maximum_page)
}
#[pyfunction]
fn pre_auth_command_accepted(status: &Bound<'_, PyAny>) -> bool {
    auth::command_status_pending(nonnegative_u64(status))
}
#[pyfunction]
fn pre_aap_decode_response(
    info: &Bound<'_, PyAny>,
    result: &Bound<'_, PyAny>,
    data: &[u8],
) -> PyResult<(&'static str, Option<u64>)> {
    let (result, mask) = aap::decode_response(info_type(info)?, nonnegative_u64(result), data);
    Ok((result.as_str(), mask))
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyPreAuthState>()?;
    module.add_class::<PyPreAAPState>()?;
    module.add_function(wrap_pyfunction!(pre_auth_supported_completion, module)?)?;
    module.add_function(wrap_pyfunction!(pre_auth_extended_completion, module)?)?;
    module.add_function(wrap_pyfunction!(pre_auth_command_accepted, module)?)?;
    module.add_function(wrap_pyfunction!(pre_aap_decode_response, module)?)?;
    Ok(())
}
