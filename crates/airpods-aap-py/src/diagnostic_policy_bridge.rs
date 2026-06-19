//! Conversion of neutral diagnostic facts to and from the pure core.

use airpods_aap_core :: { aap_config_diagnostics as config } ;

use pyo3 :: prelude :: * ;
use pyo3 :: types :: { PyDict , PyModule } ;





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















































pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
                module.add_function(wrap_pyfunction!(diagnostic_config_plan, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_config_rewrite, module)?)?;
    module.add_function(wrap_pyfunction!(diagnostic_config_observation, module)?)?;
                                                                Ok(())
}
