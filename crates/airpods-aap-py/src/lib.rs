#![forbid(unsafe_code)]
//! Private PyO3 bridge to authoritative `airpods-aap-core` analysis.

mod sdp_bridge;

use airpods_aap_core::{
    AapFrameSummary, AapType2bFrameSummary, ActivationCommand, ActivationEvent, ActivationState,
    ControlFrameSummary, DescriptorEvidence, HeartRateParseError, HeartRateReport, ProductionError,
    ProductionEvent, ProductionOperation, ProductionState, RecoveryCauseKind, RecoveryError,
    SentCommands, SourceSide, TransitionError, advance,
    classify_coexistence_recovery as classify_core_coexistence_recovery,
    parse_heart_rate_packet as parse_core, plan_activation_send, plan_cleanup_send,
    production_operation as check_production_operation,
    production_transition as advance_production,
};
use airpods_hub_core::{
    self as hub, DaemonState, Operation, RecoveryDisposition, RequestField, RestoreDecision,
    SubscribeDecision, UnsubscribeDecision,
};
use pyo3::create_exception;
use pyo3::exceptions::{PyUnicodeDecodeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBool, PyBytes, PyDict, PyFloat, PyInt, PyModule, PyString, PyTuple};

fn neutral_u8(value: &Bound<'_, PyAny>) -> PyResult<u8> {
    if value.is_instance_of::<PyBool>() {
        return Err(PyValueError::new_err("invalid transition identity"));
    }
    value
        .extract::<u8>()
        .map_err(|_| PyValueError::new_err("invalid transition identity"))
}

fn neutral_u16(value: &Bound<'_, PyAny>) -> PyResult<u16> {
    if value.is_instance_of::<PyBool>() {
        return Err(PyValueError::new_err("invalid sent-command mask"));
    }
    value
        .extract::<u16>()
        .map_err(|_| PyValueError::new_err("invalid sent-command mask"))
}

fn transition_error(error: TransitionError) -> PyErr {
    // Numeric categories are private to the bridge. Public messages live in Python.
    PyValueError::new_err(match error {
        TransitionError::InvalidActivation => 1,
        TransitionError::ActivationOnCleanupPath => 2,
        TransitionError::DuplicateCleanup => 3,
        TransitionError::StopHrRequiresStartHr => 4,
        TransitionError::HrOffRequiresHrOn => 5,
        TransitionError::InvalidAdvance => 6,
        TransitionError::UnknownIdentity => 7,
        TransitionError::UnknownSentBits => 8,
    })
}

fn production_error(error: ProductionError) -> PyErr {
    PyValueError::new_err(match error {
        ProductionError::UnknownIdentity => 7,
        ProductionError::InvalidOperation => 9,
        ProductionError::InvalidTransition => 10,
    })
}

fn recovery_error(error: RecoveryError) -> PyErr {
    match error {
        RecoveryError::UnknownIdentity => PyValueError::new_err(7),
    }
}

/// Accept only neutral values; core owns all recovery policy and identity validation.
#[pyfunction]
fn classify_coexistence_recovery(
    category: &Bound<'_, PyAny>,
    cause_kind: &Bound<'_, PyAny>,
    nested_recoverable: &Bound<'_, PyAny>,
    errno: &Bound<'_, PyAny>,
    dbus_error_name: &Bound<'_, PyAny>,
) -> PyResult<bool> {
    let category = neutral_u8(category)?;
    let cause_kind =
        RecoveryCauseKind::try_from(neutral_u8(cause_kind)?).map_err(recovery_error)?;
    if !nested_recoverable.is_instance_of::<PyBool>() {
        return Err(PyValueError::new_err("invalid nested_recoverable flag"));
    }
    let nested_recoverable = nested_recoverable.extract::<bool>()?;
    let errno = if errno.is_none() {
        None
    } else {
        if errno.is_instance_of::<PyBool>() {
            return Err(PyValueError::new_err("invalid errno"));
        }
        Some(
            errno
                .extract::<i32>()
                .map_err(|_| PyValueError::new_err("invalid errno"))?,
        )
    };
    let dbus_error_name = if dbus_error_name.is_none() {
        None
    } else {
        Some(
            dbus_error_name
                .extract::<&str>()
                .map_err(|_| PyValueError::new_err("invalid dbus_error_name"))?,
        )
    };
    classify_core_coexistence_recovery(
        category,
        cause_kind,
        nested_recoverable,
        errno,
        dbus_error_name,
    )
    .map_err(recovery_error)
}

/// Private identity inputs follow `neutral_u8`: booleans and non-u8 values
/// raise ValueError("invalid transition identity"); unknown u8 IDs raise
/// ValueError(7), illegal operations ValueError(9), illegal events ValueError(10).
#[pyfunction]
fn production_operation(state: &Bound<'_, PyAny>, operation: &Bound<'_, PyAny>) -> PyResult<u8> {
    let state = ProductionState::try_from(neutral_u8(state)?).map_err(production_error)?;
    let operation =
        ProductionOperation::try_from(neutral_u8(operation)?).map_err(production_error)?;
    check_production_operation(state, operation)
        .map(|next| next as u8)
        .map_err(production_error)
}

/// `cleanup_complete` must be a Python bool, even when the event ignores it.
#[pyfunction]
fn production_transition(
    state: &Bound<'_, PyAny>,
    event: &Bound<'_, PyAny>,
    cleanup_complete: &Bound<'_, PyAny>,
) -> PyResult<u8> {
    let state = ProductionState::try_from(neutral_u8(state)?).map_err(production_error)?;
    let event = ProductionEvent::try_from(neutral_u8(event)?).map_err(production_error)?;
    if !cleanup_complete.is_instance_of::<PyBool>() {
        return Err(PyValueError::new_err("invalid cleanup_complete flag"));
    }
    let cleanup_complete = cleanup_complete.extract::<bool>()?;
    advance_production(state, event, cleanup_complete)
        .map(|next| next as u8)
        .map_err(production_error)
}

#[pyfunction]
fn plan_activation_transition(
    state: &Bound<'_, PyAny>,
    sent: &Bound<'_, PyAny>,
    command: &Bound<'_, PyAny>,
) -> PyResult<u8> {
    let state = ActivationState::try_from(neutral_u8(state)?).map_err(transition_error)?;
    let sent = SentCommands::try_from(neutral_u16(sent)?).map_err(transition_error)?;
    let command = ActivationCommand::try_from(neutral_u8(command)?).map_err(transition_error)?;
    plan_activation_send(state, sent, command)
        .map(|next| next as u8)
        .map_err(transition_error)
}

#[pyfunction]
fn plan_cleanup_transition(
    state: &Bound<'_, PyAny>,
    sent: &Bound<'_, PyAny>,
    command: &Bound<'_, PyAny>,
) -> PyResult<u8> {
    let state = ActivationState::try_from(neutral_u8(state)?).map_err(transition_error)?;
    let sent = SentCommands::try_from(neutral_u16(sent)?).map_err(transition_error)?;
    let command = ActivationCommand::try_from(neutral_u8(command)?).map_err(transition_error)?;
    plan_cleanup_send(state, sent, command)
        .map(|next| next as u8)
        .map_err(transition_error)
}

#[pyfunction]
fn advance_activation_transition(
    state: &Bound<'_, PyAny>,
    event: &Bound<'_, PyAny>,
) -> PyResult<u8> {
    let state = ActivationState::try_from(neutral_u8(state)?).map_err(transition_error)?;
    let event = ActivationEvent::try_from(neutral_u8(event)?).map_err(transition_error)?;
    advance(state, event)
        .map(|next| next as u8)
        .map_err(transition_error)
}

create_exception!(
    _airpods_aap_core,
    HubRequestError,
    PyValueError,
    "Invalid hub request."
);

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

fn hub_reject<T>(py: Python<'_>, error: hub::RequestError) -> PyResult<T> {
    let exception = HubRequestError::new_err(error.message());
    let value = exception.value(py);
    value.setattr("code", error.code())?;
    value.setattr("message", error.message())?;
    Err(exception)
}

fn hub_request_field<'a>(value: Option<&'a Bound<'_, PyAny>>) -> RequestField<'a> {
    match value {
        None => RequestField::Missing,
        Some(value) if value.is_instance_of::<PyBool>() => RequestField::Other,
        Some(value) if value.is_instance_of::<PyInt>() => value
            .extract::<i64>()
            .map(RequestField::Integer)
            .unwrap_or(RequestField::Other),
        Some(value) if value.is_instance_of::<PyString>() => value
            .extract::<&str>()
            .map(RequestField::Text)
            .unwrap_or(RequestField::NonUtf8Text),
        Some(_) => RequestField::Other,
    }
}

#[pyfunction]
fn hub_decode_request<'py>(
    py: Python<'py>,
    frame: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    if let Err(error) = hub::validate_frame_size(frame.len()?) {
        return hub_reject(py, error);
    }
    let json = py.import("json")?;
    let decoded = match json.call_method1("loads", (frame,)) {
        Ok(value) => value,
        Err(error)
            if error.is_instance_of::<PyUnicodeDecodeError>(py)
                || error.matches(py, json.getattr("JSONDecodeError")?)? =>
        {
            return hub_reject(py, hub::RequestError::InvalidJson);
        }
        Err(error) => return Err(error),
    };
    let object = match decoded.cast_into::<PyDict>() {
        Ok(value) => value,
        Err(_) => return hub_reject(py, hub::RequestError::InvalidRequest),
    };
    let version = object.get_item("protocol_version")?;
    let operation = object.get_item("operation")?;
    let stream = object.get_item("stream")?;
    if let Err(error) = hub::validate_fields(
        hub_request_field(version.as_ref()),
        hub_request_field(operation.as_ref()),
        hub_request_field(stream.as_ref()),
    ) {
        return hub_reject(py, error);
    }
    Ok(object)
}

#[pyfunction]
fn hub_validate_request(py: Python<'_>, frame: &Bound<'_, PyBytes>) -> PyResult<()> {
    hub_decode_request(py, frame.as_any())?;
    Ok(())
}

#[pyfunction]
fn hub_source_side(raw: i64) -> (&'static str, Option<i64>) {
    hub::source_side(raw)
}

fn hub_state(value: &str) -> PyResult<DaemonState> {
    DaemonState::parse(value).ok_or_else(|| PyValueError::new_err("invalid daemon state"))
}

#[pyfunction]
fn hub_subscribe_decision(
    state: &str,
    subscribed: bool,
    closing: bool,
    count: usize,
    has_session: bool,
) -> PyResult<(&'static str, Option<&'static str>, Option<&'static str>)> {
    Ok(
        match hub::subscribe_decision(hub_state(state)?, subscribed, closing, count, has_session) {
            SubscribeDecision::Already => ("already", None, None),
            SubscribeDecision::Join => ("join", None, None),
            SubscribeDecision::Start => ("start", None, None),
            SubscribeDecision::Reject(error) => {
                ("reject", Some(error.code()), Some(error.message()))
            }
        },
    )
}

#[pyfunction]
fn hub_unsubscribe_decision(
    subscribed: bool,
    remaining: usize,
    state: &str,
    has_session: bool,
) -> PyResult<&'static str> {
    Ok(
        match hub::unsubscribe_decision(subscribed, remaining, hub_state(state)?, has_session) {
            UnsubscribeDecision::Already => "already",
            UnsubscribeDecision::Remove => "remove",
            UnsubscribeDecision::Stop => "stop",
        },
    )
}

#[pyfunction]
fn hub_recovery_step(index: usize, delays: Vec<f64>, shutdown: bool) -> Option<(f64, usize)> {
    hub::recovery_step(index, &delays, shutdown)
}

#[pyfunction]
fn hub_reset_recovery_backoff() -> usize {
    hub::reset_recovery_backoff()
}

#[pyfunction]
fn hub_default_recovery_delays() -> Vec<f64> {
    hub::DEFAULT_RECOVERY_DELAYS.to_vec()
}

#[pyfunction]
fn hub_restore_decision(count: usize, shutdown: bool) -> &'static str {
    match hub::restore_decision(count, shutdown) {
        RestoreDecision::Shutdown => "shutdown",
        RestoreDecision::Ready => "ready",
        RestoreDecision::StartHeartRate => "start_heart_rate",
    }
}

#[pyfunction]
fn hub_recovery_disposition(cleanup: bool, recoverable: bool, shutdown: bool) -> &'static str {
    match hub::recovery_disposition(cleanup, recoverable, shutdown) {
        RecoveryDisposition::Shutdown => "shutdown",
        RecoveryDisposition::Terminal => "terminal",
        RecoveryDisposition::Retry => "retry",
    }
}

#[pyfunction]
fn hub_constants() -> (
    u64,
    usize,
    usize,
    &'static str,
    Vec<&'static str>,
    Vec<&'static str>,
) {
    let operations = Operation::ALL.map(Operation::as_str).to_vec();
    let states = DaemonState::ALL.map(DaemonState::as_str).to_vec();
    (
        hub::PROTOCOL_VERSION,
        hub::MAX_FRAME_SIZE,
        hub::OUTBOUND_QUEUE_SIZE,
        hub::HEART_RATE_STREAM,
        operations,
        states,
    )
}

fn hub_value_to_dict<'py>(
    py: Python<'py>,
    value: serde_json::Value,
) -> PyResult<Bound<'py, PyDict>> {
    let json =
        serde_json::to_string(&value).map_err(|error| PyValueError::new_err(error.to_string()))?;
    Ok(py
        .import("json")?
        .call_method1("loads", (json,))?
        .extract::<Bound<'py, PyDict>>()?)
}

#[pyfunction(signature = (operation, state=None, subscriber_count=None, subscribed=None, already=None))]
fn hub_message<'py>(
    py: Python<'py>,
    operation: &str,
    state: Option<&str>,
    subscriber_count: Option<usize>,
    subscribed: Option<bool>,
    already: Option<bool>,
) -> PyResult<Bound<'py, PyDict>> {
    let value = match Operation::parse(operation) {
        Some(Operation::Hello) => hub::hello_response(),
        Some(Operation::Ping) => hub::ping_response(),
        Some(Operation::Status) => hub::status_response(
            hub_state(state.ok_or_else(|| PyValueError::new_err("state is required"))?)?,
            subscriber_count
                .ok_or_else(|| PyValueError::new_err("subscriber_count is required"))?,
        ),
        Some(operation @ (Operation::Subscribe | Operation::Unsubscribe)) => {
            let subscribed =
                subscribed.ok_or_else(|| PyValueError::new_err("subscribed is required"))?;
            if subscribed != matches!(operation, Operation::Subscribe) {
                return Err(PyValueError::new_err("subscribed does not match operation"));
            }
            hub::subscription_response(
                subscribed,
                already.ok_or_else(|| PyValueError::new_err("already is required"))?,
            )
        }
        None => return Err(PyValueError::new_err("invalid hub operation")),
    };
    hub_value_to_dict(py, value)
}

#[pyfunction]
fn hub_heart_rate_event<'py>(
    py: Python<'py>,
    bpm: &Bound<'py, PyAny>,
    raw: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let numeric = if raw.is_instance_of::<PyBool>() {
        i64::from(raw.extract::<bool>()?)
    } else if let Ok(value) = raw.extract::<i64>() {
        value
    } else if raw.is_instance_of::<PyFloat>() {
        let value = raw.extract::<f64>()?;
        if value.is_finite()
            && value.fract() == 0.0
            && value >= i64::MIN as f64
            && value < i64::MAX as f64
        {
            value as i64
        } else {
            0
        }
    } else {
        0
    };
    let event = hub_value_to_dict(py, hub::heart_rate_event(0, numeric))?;
    event.set_item("bpm", bpm)?;
    if event.contains("source_side_raw")? {
        event.set_item("source_side_raw", raw)?;
    }
    Ok(event)
}

#[pymodule]
fn _airpods_aap_core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    sdp_bridge::register(module)?;
    module.add_function(wrap_pyfunction!(plan_activation_transition, module)?)?;
    module.add_function(wrap_pyfunction!(plan_cleanup_transition, module)?)?;
    module.add_function(wrap_pyfunction!(advance_activation_transition, module)?)?;
    module.add_function(wrap_pyfunction!(production_operation, module)?)?;
    module.add_function(wrap_pyfunction!(production_transition, module)?)?;
    module.add_function(wrap_pyfunction!(classify_coexistence_recovery, module)?)?;
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
    module.add("HubRequestError", module.py().get_type::<HubRequestError>())?;
    module.add_function(wrap_pyfunction!(hub_validate_request, module)?)?;
    module.add_function(wrap_pyfunction!(hub_decode_request, module)?)?;
    module.add_function(wrap_pyfunction!(hub_source_side, module)?)?;
    module.add_function(wrap_pyfunction!(hub_subscribe_decision, module)?)?;
    module.add_function(wrap_pyfunction!(hub_unsubscribe_decision, module)?)?;
    module.add_function(wrap_pyfunction!(hub_recovery_step, module)?)?;
    module.add_function(wrap_pyfunction!(hub_reset_recovery_backoff, module)?)?;
    module.add_function(wrap_pyfunction!(hub_default_recovery_delays, module)?)?;
    module.add_function(wrap_pyfunction!(hub_restore_decision, module)?)?;
    module.add_function(wrap_pyfunction!(hub_recovery_disposition, module)?)?;
    module.add_function(wrap_pyfunction!(hub_constants, module)?)?;
    module.add_function(wrap_pyfunction!(hub_message, module)?)?;
    module.add_function(wrap_pyfunction!(hub_heart_rate_event, module)?)?;
    Ok(())
}
