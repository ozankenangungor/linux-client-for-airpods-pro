//! Neutral-value conversion for the private SDP core; no Bumble objects cross this boundary.
use airpods_sdp_core as core;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyModule};

fn identity(vendor: u16, product: u16, version: u16) -> core::UsbIdentity {
    core::UsbIdentity {
        vendor_id: vendor,
        product_id: product,
        version,
    }
}

#[pyfunction]
fn sdp_parse_bluez_modalias(value: Option<&str>) -> PyResult<(u16, u16, u16)> {
    let value = core::parse_bluez_modalias(value).map_err(|error| {
        PyValueError::new_err(match error {
            core::ModaliasError::Missing => "BlueZ adapter has no Modalias",
            core::ModaliasError::Unsupported => {
                "BlueZ adapter Modalias is not a supported USB identity"
            }
        })
    })?;
    Ok((value.vendor_id, value.product_id, value.version))
}

fn element<'py>(py: Python<'py>, value: core::Element) -> PyResult<Py<PyDict>> {
    let result = PyDict::new(py);
    match value {
        core::Element::U8(number) => {
            result.set_item("kind", "u8")?;
            result.set_item("value", number)?;
        }
        core::Element::U16(number) => {
            result.set_item("kind", "u16")?;
            result.set_item("value", number)?;
        }
        core::Element::U32(number) => {
            result.set_item("kind", "u32")?;
            result.set_item("value", number)?;
        }
        core::Element::Uuid16(number) => {
            result.set_item("kind", "uuid16")?;
            result.set_item("value", number)?;
        }
        core::Element::Sequence(values) => {
            result.set_item("kind", "sequence")?;
            result.set_item(
                "value",
                values
                    .into_iter()
                    .map(|item| element(py, item))
                    .collect::<PyResult<Vec<_>>>()?,
            )?;
        }
    }
    Ok(result.unbind())
}

#[pyfunction]
fn sdp_canonical_records(
    py: Python<'_>,
    vendor: u16,
    product: u16,
    version: u16,
) -> PyResult<Vec<Py<PyDict>>> {
    core::canonical_records(identity(vendor, product, version))
        .into_iter()
        .map(|record| {
            let result = PyDict::new(py);
            result.set_item("name", record.name)?;
            result.set_item("handle", record.handle)?;
            result.set_item("service_uuid", record.service_uuid)?;
            let attributes = record
                .attributes
                .into_iter()
                .map(|(id, value)| Ok((id, element(py, value)?)))
                .collect::<PyResult<Vec<_>>>()?;
            result.set_item("attributes", attributes)?;
            Ok(result.unbind())
        })
        .collect()
}

#[pyfunction]
fn sdp_bluez_xml_records(
    vendor: u16,
    product: u16,
    version: u16,
) -> Vec<(&'static str, String, String)> {
    core::bluez_xml_records(identity(vendor, product, version))
        .into_iter()
        .map(|record| (record.name, record.uuid, record.xml))
        .collect()
}

#[pyfunction]
fn sdp_expected_attributes(
    identity: Option<(u16, u16, u16)>,
) -> Vec<Vec<(&'static str, Option<u16>)>> {
    let identity =
        identity.map(|(vendor, product, version)| self::identity(vendor, product, version));
    core::expected_attributes(identity).into_iter().collect()
}

#[pyfunction]
fn sdp_compare_attribute(
    expected: Option<u16>,
    observable: bool,
    value: Option<i64>,
) -> &'static str {
    core::compare_attribute(expected, observable.then_some(value)).as_str()
}

#[pyfunction]
fn sdp_full_record_equivalence(
    statuses: Vec<String>,
    uuids_present: Vec<bool>,
) -> PyResult<&'static str> {
    let statuses = statuses
        .into_iter()
        .map(|status| match status.as_str() {
            "match" => Ok(core::ComparisonStatus::Match),
            "mismatch" => Ok(core::ComparisonStatus::Mismatch),
            "unknown/not-observable" => Ok(core::ComparisonStatus::NotObservable),
            _ => Err(PyValueError::new_err("invalid SDP comparison status")),
        })
        .collect::<PyResult<Vec<_>>>()?;
    Ok(core::full_record_equivalence(&statuses, &uuids_present).as_str())
}

fn uuid_spec(value: core::ServiceUuid) -> (Option<u16>, Option<&'static str>) {
    match value {
        core::ServiceUuid::Short(value) => (Some(value), None),
        core::ServiceUuid::Full(value) => (None, Some(value)),
    }
}

#[pyfunction]
#[allow(clippy::type_complexity)]
fn sdp_extra_service_specs() -> Vec<(
    &'static str,
    (Option<u16>, Option<&'static str>),
    &'static str,
    Option<u8>,
    Option<(Option<u16>, Option<&'static str>)>,
    Option<u16>,
)> {
    core::BLUEZ_LIKE_EXTRA_SERVICE_SPECS
        .iter()
        .map(|spec| {
            (
                spec.name,
                uuid_spec(spec.uuid),
                spec.protocol,
                spec.rfcomm_channel,
                spec.profile_uuid.map(uuid_spec),
                spec.profile_version,
            )
        })
        .collect()
}

#[pyfunction]
fn sdp_allocate_handles(existing: Vec<String>, count: usize) -> PyResult<Vec<String>> {
    core::allocate_handles(&existing, count).map_err(PyValueError::new_err)
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn sdp_query_decision(
    uuids: Vec<u16>,
    ranges: Vec<(u16, u16)>,
    maximum: i64,
    peer_mtu: Option<i64>,
    response_bytes: i64,
    continuation_state_len: usize,
    stored: usize,
    target_already_seen: bool,
) -> (bool, bool, bool, bool) {
    let decision = core::query_decision(
        &uuids,
        &ranges,
        maximum,
        peer_mtu,
        response_bytes,
        continuation_state_len,
        stored,
        target_already_seen,
    );
    (
        decision.target,
        decision.continuation_used,
        decision.retain_summary,
        decision.mark_prior_target,
    )
}

#[pyfunction]
fn sdp_first_l2cap_summary_index(summaries: Vec<Vec<u16>>) -> Option<usize> {
    core::first_l2cap_summary_index(&summaries)
}
#[pyfunction]
fn sdp_query_uuid16s(values: Vec<Vec<u8>>) -> Vec<u16> {
    core::query_uuid16s(&values)
}
#[pyfunction]
fn sdp_query_attribute_ranges(values: Vec<(u32, u8)>) -> Vec<(u16, u16)> {
    core::query_attribute_ranges(&values)
}

#[pyfunction]
fn sdp_classify_services(uuids: Vec<u16>) -> Vec<&'static str> {
    core::classify_services(&uuids)
}
#[pyfunction]
fn sdp_classify_service_uuid_bytes(values: Vec<Vec<u8>>) -> Vec<&'static str> {
    core::classify_service_uuid_bytes(&values)
}
#[pyfunction]
fn sdp_classify_handle(handle: u32) -> Vec<&'static str> {
    core::classify_handle(handle)
}
#[pyfunction]
fn sdp_known_services() -> Vec<(&'static str, u16, u32)> {
    core::SERVICE_IDENTITIES.to_vec()
}
#[pyfunction]
fn sdp_constants() -> Vec<(&'static str, u32)> {
    vec![
        ("PNP_INFORMATION_HANDLE", core::PNP_INFORMATION_HANDLE),
        (
            "HANDS_FREE_AUDIO_GATEWAY_HANDLE",
            core::HANDS_FREE_AUDIO_GATEWAY_HANDLE,
        ),
        ("AUDIO_SOURCE_HANDLE", core::AUDIO_SOURCE_HANDLE),
        ("AVRCP_TARGET_HANDLE", core::AVRCP_TARGET_HANDLE),
        (
            "HANDS_FREE_RFCOMM_CHANNEL",
            u32::from(core::HANDS_FREE_RFCOMM_CHANNEL),
        ),
        ("AVDTP_L2CAP_PSM", u32::from(core::AVDTP_L2CAP_PSM)),
        ("AVDTP_VERSION", u32::from(core::AVDTP_VERSION)),
        ("AVRCP_VERSION", u32::from(core::AVRCP_VERSION)),
        (
            "PNP_VENDOR_ID_SOURCE_USB",
            u32::from(core::PNP_VENDOR_ID_SOURCE_USB),
        ),
        (
            "PNP_VENDOR_ID_ATTRIBUTE_ID",
            u32::from(core::PNP_VENDOR_ID_ATTRIBUTE_ID),
        ),
        (
            "PNP_PRODUCT_ID_ATTRIBUTE_ID",
            u32::from(core::PNP_PRODUCT_ID_ATTRIBUTE_ID),
        ),
        (
            "PNP_VERSION_ATTRIBUTE_ID",
            u32::from(core::PNP_VERSION_ATTRIBUTE_ID),
        ),
        (
            "PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID",
            u32::from(core::PNP_VENDOR_ID_SOURCE_ATTRIBUTE_ID),
        ),
        (
            "REFERENCE_SDP_QUERY_SUMMARY_LIMIT",
            core::REFERENCE_SDP_QUERY_SUMMARY_LIMIT as u32,
        ),
        ("ATT_L2CAP_PSM", u32::from(core::ATT_L2CAP_PSM)),
        ("AVCTP_L2CAP_PSM", u32::from(core::AVCTP_L2CAP_PSM)),
        ("AVCTP_VERSION", u32::from(core::AVCTP_VERSION)),
        (
            "ADVANCED_AUDIO_VERSION",
            u32::from(core::ADVANCED_AUDIO_VERSION),
        ),
        ("HANDS_FREE_VERSION", u32::from(core::HANDS_FREE_VERSION)),
        (
            "HANDS_FREE_UNIT_RFCOMM_CHANNEL",
            u32::from(core::HANDS_FREE_UNIT_RFCOMM_CHANNEL),
        ),
    ]
}
#[pyfunction]
fn sdp_psm_category(psm: u16) -> (&'static str, &'static str) {
    core::psm_category(psm)
}

#[pyclass(name = "SDPTimelineState")]
struct PyTimeline {
    timeline: core::Timeline,
}
#[pymethods]
impl PyTimeline {
    #[new]
    fn new(limit: usize) -> PyResult<Self> {
        Ok(Self {
            timeline: core::Timeline::new(limit).map_err(PyValueError::new_err)?,
        })
    }
    fn full(&self) -> bool {
        self.timeline.full()
    }
    fn record(&mut self, kind: &str, elapsed: f64) -> PyResult<()> {
        self.timeline
            .record(kind, elapsed)
            .map_err(PyValueError::new_err)
    }
    fn snapshot(&self) -> (Vec<(&'static str, f64)>, u64) {
        let (events, total) = self.timeline.snapshot();
        (events.to_vec(), total)
    }
    fn first(&self, kind: &str) -> Option<(&'static str, f64)> {
        self.timeline.first(kind)
    }
}

#[pyclass(name = "SDPDiagnosticState")]
struct PyDiagnosticState {
    state: core::DiagnosticState,
}
#[pymethods]
impl PyDiagnosticState {
    #[new]
    fn new() -> Self {
        Self {
            state: core::DiagnosticState::default(),
        }
    }
    fn observe_connection(&mut self) {
        self.state.observe_connection();
    }
    fn observe_request(&mut self) {
        self.state.observe_request();
    }
    fn account_query(&mut self, names: Vec<String>) {
        let names: Vec<_> = names.iter().map(String::as_str).collect();
        self.state.account_query(&names);
    }
    fn mark_served(&mut self, name: &str) {
        self.state.mark_served(name);
    }
    fn observe_psm(&mut self, psm: u16) -> &'static str {
        self.state.observe_psm(psm)
    }
    fn snapshot(&self) -> (bool, u64, Vec<u64>, Vec<bool>, Vec<u64>) {
        let snapshot = self.state.snapshot();
        (
            snapshot.connection_observed,
            snapshot.requests,
            snapshot.queries.to_vec(),
            snapshot.matches_served.to_vec(),
            snapshot.psm.to_vec(),
        )
    }
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(sdp_parse_bluez_modalias, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_canonical_records, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_bluez_xml_records, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_expected_attributes, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_compare_attribute, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_full_record_equivalence, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_extra_service_specs, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_allocate_handles, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_query_decision, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_first_l2cap_summary_index, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_query_uuid16s, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_query_attribute_ranges, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_classify_services, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_classify_service_uuid_bytes, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_classify_handle, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_known_services, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_constants, module)?)?;
    module.add_function(wrap_pyfunction!(sdp_psm_category, module)?)?;
    module.add_class::<PyTimeline>()?;
    module.add_class::<PyDiagnosticState>()?;
    Ok(())
}
