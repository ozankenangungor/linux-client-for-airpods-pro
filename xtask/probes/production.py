"""Installed distribution assertions only: synthetic policy inputs, no transport."""
import errno
import importlib.metadata as metadata
import inspect
import os
import sys
from pathlib import Path
from unittest.mock import call, patch

import airpods_hr
from airpods_hr import aap, aap_channel, authentication, bluetooth, discovery
from airpods_hr import session_reopen, heartrate, heart_rate_session as activation
from airpods_hr import production_session as production
from airpods_hr.bluez_coexistence import (
    CoexistenceCategory as Category, CoexistenceFailure as Failure,
    CoexistencePhase as Phase,
)
from airpods_hr.protocol import HeartRateCommand as Command
from dbus_next.errors import DBusError

distribution = metadata.distribution("airpods-hr-linux")
assert distribution.version == "0.1.1"
assert "PYTHONPATH" not in os.environ
assert "hub" not in " ".join(airpods_hr.__all__).lower()
installed = Path(distribution.locate_file("")).resolve()
assert "site-packages" in installed.parts
assert installed.is_relative_to(Path(sys.prefix).resolve())
native = sys.modules["airpods_hr._airpods_aap_core"]
for module in (airpods_hr, aap, heartrate, activation, production, native):
    assert Path(module.__file__).resolve().is_relative_to(installed), module.__name__
assert native.__file__.endswith(".so")

operations = (
    "parse_heart_rate_packet", "merge_descriptor_evidence", "summarize_aap_frame",
    "summarize_type_2b_frame", "summarize_control_frame", "plan_activation_transition",
    "plan_cleanup_transition", "advance_activation_transition", "production_operation",
    "production_transition", "classify_coexistence_recovery", "runtime_positive_timeouts",
    "runtime_expected_payload_count", "runtime_descriptors_required", "runtime_channel_facts",
    "runtime_adapter_digits", "runtime_supported_airpods_name", "runtime_display_name",
    "runtime_optional_string", "runtime_candidate_count", "runtime_authentication_observation",
    "runtime_encryption_observation", "runtime_disconnect_cleanup", "runtime_poll_delay",
    "runtime_restoration", "runtime_reopen_checkpoint_holds", "runtime_reopen_failure_category",
    "runtime_aggregate_reopen_counts", "runtime_session1_activate_hr",
)
assert all(inspect.isbuiltin(getattr(native, name)) for name in operations)
for name in ("HandshakeAccumulator", "TransportPolicy", "AuthenticationLifecycle",
             "ReopenObservationCounters"):
    assert getattr(native, name).__module__ == native.__name__
for module in (aap, aap_channel, authentication, bluetooth, discovery, session_reopen):
    assert module._rust_core is native
    assert Path(module.__file__).resolve().is_relative_to(installed)
assert native.runtime_supported_airpods_name("(AIRPODS)", None)
assert native.runtime_adapter_digits("hci0001") == "0001"
assert native.runtime_channel_facts(True, True, 0x1001, 1, 1, 0x1001) == 0
assert native.runtime_reopen_checkpoint_holds(True, True, True)
assert native.HandshakeAccumulator(1).snapshot()[0] is False
assert native.TransportPolicy().application_payloads_sent == 0
assert native.AuthenticationLifecycle().state == 0
assert native.ReopenObservationCounters().attempts == 0

marker = bytes.fromhex("3a1608131a12")
raw = bytes.fromhex("01a914341202080706050403020101108281")
packet = b"prefix" + marker + raw + b"ignored trailing bytes"
with patch.object(native, "parse_heart_rate_packet", wraps=native.parse_heart_rate_packet) as observed:
    first, second = heartrate.parse_heart_rate_packet(packet), heartrate.parse_heart_rate_packet(packet)
    assert observed.call_args_list == [call(packet), call(packet)]
assert type(first) is heartrate.HeartRateReport
assert first == second and first is not second
assert (first.bpm, first.aux, first.sequence, first.field_5, first.timestamp_ticks,
        first.flags, first.raw_report) == (169, 20, 0x1234, 2, 0x0102030405060708, 0x81821001, raw)
for malformed, kind, message in (
    (b"missing", heartrate.HeartRateMarkerNotFoundError, "heart-rate marker not found"),
    (marker + raw[:17], heartrate.HeartRateReportTruncatedError,
     "heart-rate report is truncated: expected 18 bytes, found 17"),
    (marker + b"\x02" + raw[1:], heartrate.HeartRateReportIDError,
     "unexpected heart-rate report ID: 0x02"),
):
    try:
        heartrate.parse_heart_rate_packet(malformed)
    except heartrate.HeartRateParseError as error:
        assert type(error) is kind and str(error) == message
    else:
        raise AssertionError("malformed parser input accepted")
for wrong_type in (bytearray(raw), memoryview(raw), "packet", None):
    try:
        heartrate.parse_heart_rate_packet(wrong_type)
    except TypeError as error:
        assert str(error) == "packet must be a bytes object"
    else:
        raise AssertionError("non-bytes parser input accepted")

with patch.object(native, "merge_descriptor_evidence", wraps=native.merge_descriptor_evidence) as observed:
    evidence = aap.DescriptorEvidence().merged(b"AccessoryService HeartRateService _HeartRate_")
    assert observed.call_count == 1
assert type(evidence) is aap.DescriptorEvidence and evidence.required and evidence.heart_rate
frame = bytearray(34)
frame[4:6], frame[7:9], frame[31:34] = b"\x2b\x00", b"\x11\x00", b"\x7f\xff\xff"
with patch.object(native, "summarize_aap_frame", wraps=native.summarize_aap_frame) as observed:
    summary = aap.AAPFrameSummary.from_frame(bytes(frame))
    assert observed.call_count == 1
assert type(summary) is aap.AAPFrameSummary
inner = summary.type_2b_summary
assert type(inner) is aap.AAPType2BFrameSummary and inner.record_count_17 == 1
assert inner.record_suffix_histogram == (aap.RecordSuffixSummary(0x7f, 0xffff, 1),)
assert aap.AAPType2BFrameSummary.from_frame(bytes(frame)) == inner
assert activation.is_connect4_ack(activation.CONNECT4_ACK)
assert activation.ControlFrameSummary.from_frame(b"").length == 0

class CommandRecorder:
    def __init__(self):
        self.commands = []

    def send_heart_rate_command(self, command):
        self.commands.append(command)

session, recorder = activation.HeartRateActivationSession(), CommandRecorder()
with patch.object(native, "plan_activation_transition", wraps=native.plan_activation_transition) as observed:
    session._send_activation(recorder, Command.STOP_HEAD)
    observed.assert_called_once_with(0, 0, 0)
assert session.state is activation.HeartRateActivationState.STOP_HEAD_SENT
assert recorder.commands == [Command.STOP_HEAD]
with patch.object(native, "advance_activation_transition", wraps=native.advance_activation_transition) as observed:
    session._advance(0)
    observed.assert_called_once_with(1, 0)
assert session.state is activation.HeartRateActivationState.STOP_HEAD_ACKNOWLEDGED
session.state = activation.HeartRateActivationState.START_HR_SENT
session._sent.add(Command.START_HR)
with patch.object(native, "plan_cleanup_transition", wraps=native.plan_cleanup_transition) as observed:
    session._send_cleanup(recorder, Command.STOP_HR)
    observed.assert_called_once_with(9, (1 << 0) | (1 << 6), 7)
assert session.state is activation.HeartRateActivationState.STOP_HR_SENT
assert recorder.commands[-1] is Command.STOP_HR

# Construct the policy adapter without constructing its hardware-facing session.
states = tuple(getattr(production.ProductionSessionState, name) for name in
               ("CLOSED", "OPENING", "READY", "STARTING", "STREAMING", "STOPPING", "FAILED"))
assert tuple(production.ProductionSessionState) == states
adapter = object.__new__(production.InternalProductionSession)
adapter.state = states[0]

def operation(name, identity, valid=True):
    prior = adapter.state
    with patch.object(native, "production_operation", wraps=native.production_operation) as observed:
        try:
            adapter._require_operation(name)
        except production.ProductionSessionStateError as error:
            assert not valid and error.category is production.ProductionSessionCategory.INVALID_STATE
        else:
            assert valid
        observed.assert_called_once_with(states.index(prior), identity)
    assert adapter.state is prior

def transition(event, expected, complete=False):
    prior = states.index(adapter.state)
    with patch.object(native, "production_transition", wraps=native.production_transition) as observed:
        adapter._advance(production._ProductionEvent(event), cleanup_complete=complete)
        observed.assert_called_once_with(prior, event, complete)
    assert adapter.state is states[expected]

operation("open", 0)
operation("start", 1, False)
transition(0, 1); transition(1, 2)
operation("start", 1)
transition(3, 3); transition(4, 4)
operation("receive_report", 2); operation("stop", 3)
transition(6, 5)
operation("stop", 3, False)
transition(7, 2)
operation("close", 4)
transition(8, 0, True); transition(0, 1); transition(2, 6)
operation("start", 1, False); operation("close", 4)
transition(8, 6); transition(8, 0, True)
for event, expected in ((0, 1), (1, 2), (3, 3), (4, 4), (5, 6)):
    transition(event, expected)
transition(8, 0, True)

category_names = (
    "PREFLIGHT_FAILED", "BLUEZ_NOT_AVAILABLE", "AIRPODS_NOT_CONNECTED",
    "FRESH_ACL_REQUIRES_DISCONNECTED_DEVICE", "PROFILE_REGISTRATION_FAILED",
    "L2CAP_SOCKET_FAILED", "L2CAP_BIND_FAILED", "L2CAP_SECURITY_FAILED",
    "L2CAP_LOCAL_RX_MTU_FAILED", "L2CAP_CONNECT_FAILED", "L2CAP_ROUTE_MISMATCH",
    "AAP_HANDSHAKE_FAILED", "AAP_DESCRIPTOR_TIMEOUT", "HR_ACTIVATION_FAILED",
    "HR_TIMEOUT", "BLUEZ_CONNECTION_LOST", "CLEANUP_FAILED",
)
categories = tuple(getattr(Category, name) for name in category_names)
assert tuple(Category) == categories

def recovery(error, args, expected, nested=None):
    with patch.object(native, "classify_coexistence_recovery", wraps=native.classify_coexistence_recovery) as observed:
        result = production._translate_session_error(
            production.ProductionSessionCategory.TRANSPORT_FAILED, "synthetic_probe", error)
        expected_calls = ([call(args[0], 0, False, None, None)] if error.__cause__ is not None else [])
        if nested is not None:
            expected_calls.append(call(*nested))
        expected_calls.append(call(*args))
        assert observed.call_args_list == expected_calls
    assert type(result) is production.ProductionSessionError
    assert result.category is production.ProductionSessionCategory.TRANSPORT_FAILED
    assert result.phase == "synthetic_probe" and result.detail == "CoexistenceFailure"
    assert result.recoverable is expected

for index, category in enumerate(categories):
    recovery(Failure(category, Phase.PREFLIGHT), (index, 0, False, None, None),
             index in {0, 1, 2, 4, 6, 9, 11, 12, 13, 14, 15})

def caused(cause):
    error = Failure(Category.L2CAP_BIND_FAILED, Phase.L2CAP_CONNECTION)
    error.__cause__ = cause
    return error

for index, expected in ((1, True), (7, False)):
    recovery(caused(Failure(categories[index], Phase.PREFLIGHT)), (6, 1, expected, None, None),
             expected, nested=(index, 0, False, None, None))
recovery(caused(TimeoutError("synthetic")), (6, 2, False, None, None), True)

class ErrnoProbe(OSError):
    pass

for code, expected in ((errno.EINVAL, False), (errno.ETIMEDOUT, True)):
    recovery(caused(ErrnoProbe(code, "synthetic")), (6, 3, False, code, None), expected)
for name, expected in (("org.freedesktop.DBus.Error.NoReply", True),
                       ("org.bluez.Error.InvalidArguments", False),
                       ("com.example.Error.Unknown", False)):
    recovery(caused(DBusError(name, "synthetic")), (6, 4, False, None, name), expected)
recovery(caused(ValueError("synthetic")), (6, 5, False, None, None), False)
for args in ((17, 0, False, None, None), (255, 0, False, None, None),
             (6, 6, False, None, None), (6, 255, False, None, None)):
    try:
        native.classify_coexistence_recovery(*args)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown recovery identity accepted")
print("installed production consumer PASS: compiled parser, AAP, runtime, lifecycle, recovery")
