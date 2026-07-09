//! Neutral Python bindings for pure runtime decisions.

use airpods_aap_core :: handshake :: { HandshakeAccumulator , expected_payload_count } ;
use airpods_aap_core :: runtime_policy :: { AuthenticationEvent , AuthenticationState , CandidateCount , ChannelFacts , Cleanup , Observation , TransportLegality , TransportPolicy , TransportSend , adapter_index_digits , authentication_observation , authentication_transition , candidate_count , channel_facts , disconnect_cleanup , display_name , encryption_observation , positive_timeouts , supported_airpods_name , transport_legality } ;
use airpods_aap_core :: { AapFrameSummary , AapType2bFrameSummary , DescriptorEvidence } ;
use pyo3 :: exceptions :: PyValueError ;
use pyo3 :: prelude :: * ;
use pyo3 :: types :: { PyAny , PyBytes , PyModule , PyString } ;

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
type FrameValues = (usize, Option<u16>, Option<u16>, Option<Type2bValues>);
type Snapshot = (
    bool,
    (bool, bool, bool, bool),
    usize,
    usize,
    usize,
    Vec<FrameValues>,
    Vec<FrameValues>,
);

fn type_2b(summary: AapType2bFrameSummary) -> Type2bValues {
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
            .map(|x| (x.suffix_field_u8, x.suffix_field_u16, x.count))
            .collect(),
        summary.unit_bytes_8_13_uniform,
    )
}

fn frame(summary: AapFrameSummary) -> FrameValues {
    (
        summary.length,
        summary.header_u16_2_3,
        summary.header_u16_4_5,
        summary.type_2b_summary.map(type_2b),
    )
}

#[pyclass(name = "HandshakeAccumulator", module = "airpods_hr._airpods_aap_core")]
pub struct PyHandshakeAccumulator {
    inner: HandshakeAccumulator,
}

#[pymethods]
impl PyHandshakeAccumulator {
    #[new]
    fn new(summary_limit: usize) -> PyResult<Self> {
        Ok(Self {
            inner: HandshakeAccumulator::new(summary_limit)
                .ok_or_else(|| PyValueError::new_err("AAP frame summary limit must be positive"))?,
        })
    }

    fn observe(&mut self, frame: &Bound<'_, PyBytes>, dropped_frames: usize) -> (bool, bool, bool) {
        let decision = self.inner.observe(frame.as_bytes(), dropped_frames);
        (
            decision.ack_observed_now,
            decision.first_post_ack_frame,
            decision.first_357_byte_frame,
        )
    }

    fn snapshot_dropped(&mut self, dropped_frames: usize) {
        self.inner.snapshot_dropped(dropped_frames);
    }

    #[getter]
    fn ack_observed(&self) -> bool {
        self.inner.ack_observed
    }

    #[getter]
    fn descriptors_complete(&self) -> bool {
        self.inner.descriptors_complete()
    }

    fn snapshot(&self) -> Snapshot {
        let e = self.inner.evidence;
        (
            self.inner.ack_observed,
            (
                e.sensor_framework,
                e.heart_rate_service,
                e.heart_rate,
                e.heartrate_access,
            ),
            self.inner.pre_ack_frame_count,
            self.inner.post_ack_frame_count,
            self.inner.receive_frames_dropped,
            self.inner
                .pre_ack_frame_summaries
                .clone()
                .into_iter()
                .map(frame)
                .collect(),
            self.inner
                .post_ack_frame_summaries
                .clone()
                .into_iter()
                .map(frame)
                .collect(),
        )
    }
}

#[pyfunction]
fn runtime_positive_timeouts(values: Vec<f64>) -> bool {
    positive_timeouts(&values)
}

#[pyfunction]
fn runtime_expected_payload_count(count: usize) -> bool {
    expected_payload_count(count)
}

#[pyfunction]
fn runtime_descriptors_required(sensor_framework: bool, heart_rate_service: bool) -> bool {
    DescriptorEvidence {
        sensor_framework,
        heart_rate_service,
        heart_rate: false,
        heartrate_access: false,
    }
    .required()
}

#[pyfunction]
fn runtime_channel_facts(
    is_open: bool,
    is_basic: bool,
    psm: i64,
    local_mtu: i64,
    peer_mtu: i64,
    expected_psm: i64,
) -> u8 {
    match channel_facts(is_open, is_basic, psm, local_mtu, peer_mtu, expected_psm) {
        ChannelFacts::Valid => 0,
        ChannelFacts::NotOpen => 1,
        ChannelFacts::NotBasic => 2,
        ChannelFacts::WrongPsm => 3,
        ChannelFacts::InvalidMtu => 4,
    }
}

#[pyfunction]
fn runtime_adapter_digits(name: &str) -> Option<String> {
    adapter_index_digits(name).map(str::to_owned)
}

#[pyfunction]
fn runtime_supported_airpods_name(name: Option<&str>, alias: Option<&str>) -> bool {
    supported_airpods_name(name, alias)
}

#[pyfunction]
fn runtime_display_name(alias: Option<&str>, name: Option<&str>) -> String {
    display_name(alias, name).to_owned()
}

#[pyfunction]
fn runtime_optional_string(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    if !value.is_instance_of::<PyString>() {
        return Ok(None);
    }
    let value = value.extract::<String>()?;
    Ok((!value.is_empty()).then_some(value))
}

#[pyfunction]
fn runtime_candidate_count(count: usize) -> u8 {
    match candidate_count(count) {
        CandidateCount::None => 0,
        CandidateCount::One => 1,
        CandidateCount::Ambiguous => 2,
    }
}

#[pyfunction]
fn runtime_transport_legality(active: bool, sent: usize, handshake: bool) -> u8 {
    let operation = if handshake {
        TransportSend::Handshake
    } else {
        TransportSend::HeartRate
    };
    match transport_legality(active, sent, operation) {
        TransportLegality::Allowed => 0,
        TransportLegality::CollectionInactive => 1,
        TransportLegality::HandshakeAlreadySent => 2,
        TransportLegality::HandshakeMissing => 3,
    }
}

fn transport_legality_id(legality: TransportLegality) -> u8 {
    match legality {
        TransportLegality::Allowed => 0,
        TransportLegality::CollectionInactive => 1,
        TransportLegality::HandshakeAlreadySent => 2,
        TransportLegality::HandshakeMissing => 3,
    }
}

#[pyclass(name = "TransportPolicy", module = "airpods_hr._airpods_aap_core")]
pub struct PyTransportPolicy {
    inner: TransportPolicy,
}

#[pymethods]
impl PyTransportPolicy {
    #[new]
    fn new() -> Self {
        Self {
            inner: TransportPolicy::default(),
        }
    }
    fn begin_collection(&mut self) -> bool {
        self.inner.begin_collection()
    }
    fn end_collection(&mut self) {
        self.inner.end_collection();
    }
    fn send_legality(&self, handshake: bool) -> u8 {
        transport_legality_id(self.inner.send_legality(if handshake {
            TransportSend::Handshake
        } else {
            TransportSend::HeartRate
        }))
    }
    fn sent(&mut self, handshake: bool) -> PyResult<usize> {
        self.inner
            .sent(if handshake {
                TransportSend::Handshake
            } else {
                TransportSend::HeartRate
            })
            .map_err(|reason| PyValueError::new_err(transport_legality_id(reason)))
    }
    #[getter]
    fn application_payloads_sent(&self) -> usize {
        self.inner.application_payloads_sent
    }
    #[getter]
    fn collection_active(&self) -> bool {
        self.inner.collection_active
    }
}

fn auth_state(value: u8) -> PyResult<AuthenticationState> {
    use AuthenticationState as S;
    match value {
        0 => Ok(S::Start),
        1 => Ok(S::Selected),
        2 => Ok(S::Connected),
        3 => Ok(S::Authenticated),
        4 => Ok(S::Encrypted),
        5 => Ok(S::Active),
        6 => Ok(S::DisconnectAttempted),
        7 => Ok(S::Disconnected),
        _ => Err(PyValueError::new_err("invalid authentication state")),
    }
}

fn auth_event(value: u8) -> PyResult<AuthenticationEvent> {
    use AuthenticationEvent as E;
    match value {
        0 => Ok(E::Select),
        1 => Ok(E::Connect),
        2 => Ok(E::Authenticate),
        3 => Ok(E::Encrypt),
        4 => Ok(E::Yield),
        5 => Ok(E::AttemptDisconnect),
        6 => Ok(E::Disconnected),
        _ => Err(PyValueError::new_err("invalid authentication event")),
    }
}

fn auth_state_id(value: AuthenticationState) -> u8 {
    use AuthenticationState as S;
    match value {
        S::Start => 0,
        S::Selected => 1,
        S::Connected => 2,
        S::Authenticated => 3,
        S::Encrypted => 4,
        S::Active => 5,
        S::DisconnectAttempted => 6,
        S::Disconnected => 7,
    }
}

#[pyfunction]
fn runtime_authentication_transition(state: u8, event: u8) -> PyResult<u8> {
    authentication_transition(auth_state(state)?, auth_event(event)?)
        .map(auth_state_id)
        .ok_or_else(|| PyValueError::new_err("illegal authentication transition"))
}

#[pyclass(
    name = "AuthenticationLifecycle",
    module = "airpods_hr._airpods_aap_core"
)]
pub struct PyAuthenticationLifecycle {
    state: AuthenticationState,
    replacement_key_reported: bool,
}

#[pymethods]
impl PyAuthenticationLifecycle {
    #[new]
    fn new() -> Self {
        Self {
            state: AuthenticationState::Start,
            replacement_key_reported: false,
        }
    }

    fn advance(&mut self, event: u8) -> PyResult<u8> {
        let next = authentication_transition(self.state, auth_event(event)?)
            .ok_or_else(|| PyValueError::new_err("illegal authentication transition"))?;
        self.state = next;
        Ok(auth_state_id(next))
    }

    fn report_replacement_key(&mut self) {
        self.replacement_key_reported = true;
    }

    #[getter]
    fn replacement_key_reported(&self) -> bool {
        self.replacement_key_reported
    }

    #[getter]
    fn state(&self) -> u8 {
        auth_state_id(self.state)
    }
}

#[pyfunction]
fn runtime_authentication_observation(observed: bool) -> u8 {
    match authentication_observation(observed) {
        Observation::Success => 0,
        Observation::AuthenticationNotObserved => 1,
        Observation::EncryptionNotObserved => unreachable!(),
    }
}

#[pyfunction]
fn runtime_encryption_observation(observed: bool) -> u8 {
    match encryption_observation(observed) {
        Observation::Success => 0,
        Observation::EncryptionNotObserved => 2,
        Observation::AuthenticationNotObserved => unreachable!(),
    }
}

#[pyfunction]
fn runtime_disconnect_cleanup(primary: bool, failed: bool, cancelled: bool) -> u8 {
    match disconnect_cleanup(primary, failed, cancelled) {
        Cleanup::EmitDisconnected => 0,
        Cleanup::NotePrimary => 1,
        Cleanup::PropagateCancellation => 2,
        Cleanup::RaiseDisconnectError => 3,
    }
}

















pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyHandshakeAccumulator>()?;
    module.add_class::<PyAuthenticationLifecycle>()?;
        module.add_class::<PyTransportPolicy>()?;
    module.add_function(wrap_pyfunction!(runtime_positive_timeouts, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_expected_payload_count, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_descriptors_required, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_channel_facts, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_adapter_digits, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_supported_airpods_name, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_display_name, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_optional_string, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_candidate_count, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_transport_legality, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_authentication_transition, module)?)?;
    module.add_function(wrap_pyfunction!(
        runtime_authentication_observation,
        module
    )?)?;
    module.add_function(wrap_pyfunction!(runtime_encryption_observation, module)?)?;
    module.add_function(wrap_pyfunction!(runtime_disconnect_cleanup, module)?)?;
                            Ok(())
}
