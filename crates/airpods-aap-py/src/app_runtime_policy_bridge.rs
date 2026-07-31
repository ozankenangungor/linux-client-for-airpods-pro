//! Scalar conversion for the private pure application core.

use airpods_app_core::{daemon, monitor, path_policy, production_config, runner, service};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

fn daemon_state(value: &str) -> PyResult<daemon::DaemonState> {
    daemon::DaemonState::parse(value).ok_or_else(|| PyValueError::new_err("invalid daemon state"))
}

#[pyfunction]
fn app_daemon_transition(
    state: &str,
    event: &str,
    start_attempted: bool,
    shutdown_requested: bool,
) -> PyResult<&'static str> {
    let event =
        daemon::Event::parse(event).ok_or_else(|| PyValueError::new_err("invalid daemon event"))?;
    daemon::transition(
        daemon_state(state)?,
        event,
        start_attempted,
        shutdown_requested,
    )
    .map(daemon::DaemonState::as_str)
    .map_err(|error| match error {
        daemon::TransitionError::SingleUse => {
            PyValueError::new_err("daemon objects are single-use")
        }
        daemon::TransitionError::CannotStartFrom(state) => {
            PyValueError::new_err(format!("cannot start daemon from {}", state.as_str()))
        }
        daemon::TransitionError::IllegalState => PyValueError::new_err("illegal daemon transition"),
    })
}

#[pyfunction]
fn app_daemon_reader_failure(
    state: &str,
    shutdown_requested: bool,
    recoverable: bool,
) -> PyResult<&'static str> {
    Ok(
        match daemon::reader_failure(daemon_state(state)?, shutdown_requested, recoverable) {
            daemon::FailureAction::Ignore => "ignore",
            daemon::FailureAction::Recover => "recover",
            daemon::FailureAction::Terminal => "terminal",
        },
    )
}

#[pyfunction]
fn app_daemon_begin_recovery(shutdown_requested: bool, recovery_active: bool) -> bool {
    daemon::begin_recovery(shutdown_requested, recovery_active)
}

#[pyfunction]
fn app_daemon_client_plan(event: &str) -> PyResult<(bool, &'static str)> {
    let event = daemon::ClientEvent::parse(event)
        .ok_or_else(|| PyValueError::new_err("invalid daemon client event"))?;
    let plan = daemon::client_plan(event);
    let notification = match plan.notification {
        daemon::Notification::None => "none",
        daemon::Notification::All => "all",
        daemon::Notification::OtherClients => "other_clients",
    };
    Ok((plan.clear_subscriptions, notification))
}

#[pyfunction]
fn app_daemon_validate_constructor(
    operation_timeout: f64,
    recovery_delays: Vec<f64>,
) -> PyResult<()> {
    daemon::validate_constructor(operation_timeout, &recovery_delays).map_err(PyValueError::new_err)
}

#[pyfunction]
fn app_socket_path_validate(path: &[u8]) -> PyResult<()> {
    path_policy::validate_socket_path(path).map_err(|error| {
        PyValueError::new_err(match error {
            path_policy::SocketPathError::NotAbsoluteFile => {
                "socket path must be an absolute file path"
            }
            path_policy::SocketPathError::TooLong => "socket path is too long",
        })
    })
}

fn timeouts(
    descriptor: f64,
    dbus: f64,
    connect: f64,
    handshake: f64,
    start: f64,
    stop: f64,
    daemon: f64,
) -> production_config::Timeouts {
    production_config::Timeouts {
        descriptor,
        dbus,
        connect,
        handshake,
        start,
        stop,
        daemon,
    }
}

#[pyfunction]
fn app_production_defaults() -> (f64, f64) {
    (
        production_config::DEFAULT_DESCRIPTOR_TIMEOUT,
        production_config::DEFAULT_DAEMON_OPERATION_TIMEOUT,
    )
}

#[pyfunction]
fn app_production_minimum(
    descriptor: f64,
    dbus: f64,
    connect: f64,
    handshake: f64,
    start: f64,
    stop: f64,
) -> f64 {
    production_config::minimum(timeouts(
        descriptor, dbus, connect, handshake, start, stop, 1.0,
    ))
}

#[pyfunction]
fn app_production_validate(
    descriptor: f64,
    dbus: f64,
    connect: f64,
    handshake: f64,
    start: f64,
    stop: f64,
    daemon: f64,
) -> PyResult<f64> {
    production_config::validate(timeouts(
        descriptor, dbus, connect, handshake, start, stop, daemon,
    ))
    .map_err(|error| {
        PyValueError::new_err(match error {
            production_config::TimeoutError::NonPositiveOrNonFinite => {
                "production hub timeouts must be finite and positive"
            }
            production_config::TimeoutError::BelowMinimum => {
                "daemon operation timeout is below the production open window"
            }
        })
    })
}

#[pyfunction]
fn app_runner_safe_failure(category: &str) -> PyResult<&'static str> {
    runner::Failure::parse(category)
        .map(runner::Failure::safe_name)
        .ok_or_else(|| PyValueError::new_err("unknown runner failure category"))
}

#[pyfunction]
fn app_runner_production_category(category: &str) -> PyResult<&'static str> {
    runner::production_category(category)
        .ok_or_else(|| PyValueError::new_err("unknown production failure category"))
}

#[pyfunction]
fn app_runner_outcome(
    failure: Option<&str>,
    signal: Option<i32>,
    cleanup_complete: bool,
) -> PyResult<i32> {
    let failure = failure
        .map(|value| {
            runner::Failure::parse(value)
                .ok_or_else(|| PyValueError::new_err("unknown runner failure category"))
        })
        .transpose()?;
    runner::outcome(failure, signal, cleanup_complete).map_err(PyValueError::new_err)
}

#[pyfunction]
fn app_monitor_signal_action(
    active: bool,
    shutdown_requested: bool,
    start_acknowledged: bool,
) -> &'static str {
    match monitor::signal_action(active, shutdown_requested, start_acknowledged) {
        monitor::SignalAction::Noop => "noop",
        monitor::SignalAction::GracefulStop => "graceful_stop",
        monitor::SignalAction::Cancel => "cancel",
    }
}

#[pyfunction]
fn app_monitor_signal_exit_code(signal: Option<i32>) -> PyResult<Option<i32>> {
    monitor::signal_exit_code(signal).map_err(PyValueError::new_err)
}

#[pyfunction]
fn app_monitor_termination_reason(exit_code: i32) -> &'static str {
    monitor::termination_reason(exit_code)
}

#[pyfunction]
fn app_monitor_exception_reason(category: &str) -> PyResult<&'static str> {
    monitor::exception_reason(category)
        .ok_or_else(|| PyValueError::new_err("unknown monitor exception category"))
}

#[pyfunction]
fn app_monitor_progress_route(
    event: &str,
    has_report: bool,
    diagnostic: bool,
) -> PyResult<&'static str> {
    monitor::progress_route(event, has_report, diagnostic)
        .map(|route| match route {
            monitor::ProgressRoute::StartStatus => "start_status",
            monitor::ProgressRoute::SimpleSample => "simple_sample",
            monitor::ProgressRoute::DiagnosticSample => "diagnostic_sample",
            monitor::ProgressRoute::Ignore => "ignore",
        })
        .ok_or_else(|| PyValueError::new_err("unknown monitor progress event"))
}

#[pyfunction]
fn app_monitor_completion_message(
    bluez_restored: bool,
    shutdown_requested: bool,
    start_acknowledged: bool,
) -> Option<&'static str> {
    monitor::completion_message(bluez_restored, shutdown_requested, start_acknowledged)
}

#[pyfunction]
fn app_monitor_command_error(category: &str) -> PyResult<&'static str> {
    monitor::command_error(category)
        .ok_or_else(|| PyValueError::new_err("unknown monitor error category"))
}

#[pyfunction]
fn app_monitor_dry_run_lines(diagnostic: bool, output_path: bool) -> Vec<&'static str> {
    monitor::dry_run_lines(diagnostic, output_path)
}

#[pyfunction]
fn app_service_argv(executable: &str, operation: &str) -> PyResult<Vec<String>> {
    let operation = service::Operation::parse(operation)
        .ok_or_else(|| PyValueError::new_err("unsupported systemctl operation"))?;
    Ok(service::systemctl_argv(executable, operation))
}

#[pyfunction]
fn app_service_render_executable(path: &str) -> PyResult<String> {
    service::render_executable(path)
        .map_err(|error| PyValueError::new_err(error.format_message(path)))
}

#[pyfunction]
fn app_service_exec_start(path: &str) -> PyResult<String> {
    service::exec_start(path).map_err(|error| PyValueError::new_err(error.format_message(path)))
}

#[pyfunction]
fn app_service_render_unit(path: &str) -> PyResult<String> {
    service::render_unit(path).map_err(|error| PyValueError::new_err(error.format_message(path)))
}

#[pyfunction]
fn app_service_owned(contents: &str) -> bool {
    service::is_project_owned(contents)
}

#[pyfunction]
fn app_service_installed_exec_start(contents: &str) -> Option<String> {
    service::installed_exec_start(contents).map(str::to_owned)
}

#[pyfunction]
fn app_service_installation_state(
    contents: Option<&str>,
    expected: &str,
) -> (bool, bool, Option<String>) {
    let state = service::installation_state(contents, expected);
    (
        state.owned,
        state.exec_start_matches,
        state.installed_exec_start,
    )
}

#[pyfunction]
fn app_service_installation_valid(exists: bool, owned: bool, matches: bool) -> bool {
    service::installation_valid(exists, owned, matches)
}

fn action_name(action: service::Action) -> &'static str {
    match action {
        service::Action::WriteUnit => "write",
        service::Action::UnlinkUnit => "unlink",
        service::Action::DaemonReload => "daemon-reload",
        service::Action::Enable => "enable",
        service::Action::Disable => "disable",
    }
}

#[pyfunction]
fn app_service_install_plan(
    exists: bool,
    owned: bool,
    force: bool,
    enable: bool,
) -> PyResult<Vec<&'static str>> {
    service::install_plan(exists, owned, force, enable)
        .map(|actions| actions.into_iter().map(action_name).collect())
        .map_err(PyValueError::new_err)
}

#[pyfunction]
fn app_service_uninstall_plan(
    exists: bool,
    owned: bool,
    disable: bool,
) -> PyResult<Vec<&'static str>> {
    service::uninstall_plan(exists, owned, disable)
        .map(|actions| actions.into_iter().map(action_name).collect())
        .map_err(PyValueError::new_err)
}

#[pyfunction]
fn app_service_dry_run_operations(install: bool, optional: bool) -> Vec<&'static str> {
    service::dry_run_operations(install, optional)
        .into_iter()
        .map(service::Operation::as_str)
        .collect()
}

pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(app_daemon_transition, module)?)?;
    module.add_function(wrap_pyfunction!(app_daemon_reader_failure, module)?)?;
    module.add_function(wrap_pyfunction!(app_daemon_begin_recovery, module)?)?;
    module.add_function(wrap_pyfunction!(app_daemon_client_plan, module)?)?;
    module.add_function(wrap_pyfunction!(app_daemon_validate_constructor, module)?)?;
    module.add_function(wrap_pyfunction!(app_socket_path_validate, module)?)?;
    module.add_function(wrap_pyfunction!(app_production_defaults, module)?)?;
    module.add_function(wrap_pyfunction!(app_production_minimum, module)?)?;
    module.add_function(wrap_pyfunction!(app_production_validate, module)?)?;
    module.add_function(wrap_pyfunction!(app_runner_safe_failure, module)?)?;
    module.add_function(wrap_pyfunction!(app_runner_production_category, module)?)?;
    module.add_function(wrap_pyfunction!(app_runner_outcome, module)?)?;
    module.add_function(wrap_pyfunction!(app_monitor_signal_action, module)?)?;
    module.add_function(wrap_pyfunction!(app_monitor_signal_exit_code, module)?)?;
    module.add_function(wrap_pyfunction!(app_monitor_termination_reason, module)?)?;
    module.add_function(wrap_pyfunction!(app_monitor_exception_reason, module)?)?;
    module.add_function(wrap_pyfunction!(app_monitor_progress_route, module)?)?;
    module.add_function(wrap_pyfunction!(app_monitor_completion_message, module)?)?;
    module.add_function(wrap_pyfunction!(app_monitor_command_error, module)?)?;
    module.add_function(wrap_pyfunction!(app_monitor_dry_run_lines, module)?)?;
    module.add_function(wrap_pyfunction!(app_service_argv, module)?)?;
    module.add_function(wrap_pyfunction!(app_service_render_executable, module)?)?;
    module.add_function(wrap_pyfunction!(app_service_exec_start, module)?)?;
    module.add_function(wrap_pyfunction!(app_service_render_unit, module)?)?;
    module.add_function(wrap_pyfunction!(app_service_owned, module)?)?;
    module.add_function(wrap_pyfunction!(app_service_installed_exec_start, module)?)?;
    module.add_function(wrap_pyfunction!(app_service_installation_state, module)?)?;
    module.add_function(wrap_pyfunction!(app_service_installation_valid, module)?)?;
    module.add_function(wrap_pyfunction!(app_service_install_plan, module)?)?;
    module.add_function(wrap_pyfunction!(app_service_uninstall_plan, module)?)?;
    module.add_function(wrap_pyfunction!(app_service_dry_run_operations, module)?)?;
    Ok(())
}
