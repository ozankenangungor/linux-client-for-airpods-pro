//! Local daemon prerequisites, separate from the resilient IPC subscription.
//! Files belong to this user; only the package's installer writes service units.

mod process;
#[cfg(test)]
mod tests;

use airpods_app_core::service;
use airpods_client::{AirPodsClient, DaemonState};
use process::{CommandResult, CommandSpec, ProcessError};
use std::ffi::OsString;
use std::future::Future;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;
use tokio::fs;
use tokio::io::AsyncReadExt;
use tokio::sync::watch;
use tokio::time::Instant;

const COMMAND_TIMEOUT: Duration = Duration::from_secs(20);
const INSTALL_TIMEOUT: Duration = Duration::from_secs(900);
const READY_TIMEOUT: Duration = Duration::from_secs(30);
const POLL_INTERVAL: Duration = Duration::from_millis(250);
const ENV_MARKER: &str = "airpods-desktop daemon environment v1\n";
const VERSION_CHECK: &str =
    "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')";
const ENV_CHECK: &str = "import sys, pathlib, importlib.metadata; assert sys.version_info[:2] == (3, 14); assert pathlib.Path(sys.prefix) == pathlib.Path(sys.argv[1]); import airpods_hr._airpods_aap_core, airpods_hr.service_installer, bumble, dbus_next; assert importlib.metadata.version('airpods-hr-linux') == '0.1.0'";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Stage {
    CheckingDaemon,
    InspectingInstallation,
    WaitingForSetup,
    FindingPython,
    CreatingEnvironment,
    InstallingDaemon,
    InstallingService,
    StartingDaemon,
    WaitingForDaemon,
    Ready,
}

impl Stage {
    pub fn title(self) -> &'static str {
        match self {
            Self::CheckingDaemon => "Checking airpods-hubd",
            Self::StartingDaemon | Self::WaitingForDaemon => "Starting airpods-hubd",
            Self::Ready => "airpods-hubd is ready",
            _ => "Preparing airpods-hubd",
        }
    }

    pub fn hint(self) -> &'static str {
        match self {
            Self::CheckingDaemon => "Looking for an existing local daemon…",
            Self::InspectingInstallation => "Checking the local daemon installation…",
            Self::WaitingForSetup => "Another app instance is preparing the daemon…",
            Self::FindingPython => "Looking for Python 3.14…",
            Self::CreatingEnvironment => "Creating a local daemon environment…",
            Self::InstallingDaemon => {
                "Installing the daemon and its dependencies. First setup can take a few minutes…"
            }
            Self::InstallingService => "Installing the local user service…",
            Self::StartingDaemon => "Starting the local daemon…",
            Self::WaitingForDaemon => "Waiting for the daemon and paired AirPods to become ready…",
            Self::Ready => "Connecting to the heart-rate stream…",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Failure {
    Cancelled,
    Storage,
    SourceCheckout,
    PythonMissing,
    PythonVersion,
    Environment,
    PackageInstall,
    ServiceInstall,
    ForeignService,
    SystemdUnavailable,
    ServiceStart,
    DaemonExited,
    ReadinessTimeout,
    AirPodsUnavailable,
    Socket,
    SetupBusy,
}

impl Failure {
    pub fn title(self) -> &'static str {
        match self {
            Self::Cancelled => "Daemon setup was cancelled",
            Self::Storage => "The daemon data directory is not usable",
            Self::SourceCheckout => "The source checkout is not available",
            Self::PythonMissing | Self::PythonVersion => "Python 3.14 is required",
            Self::Environment => "The local daemon environment could not be prepared",
            Self::PackageInstall => "The daemon package could not be installed",
            Self::ServiceInstall => "The local daemon service could not be installed",
            Self::ForeignService => "Existing daemon service was left unchanged",
            Self::SystemdUnavailable => "The systemd user session is not available",
            Self::ServiceStart | Self::DaemonExited => "airpods-hubd could not start",
            Self::ReadinessTimeout => "airpods-hubd did not become ready",
            Self::AirPodsUnavailable => "Waiting for AirPods",
            Self::Socket => "The daemon socket could not be verified",
            Self::SetupBusy => "Another daemon setup is still running",
        }
    }

    pub fn hint(self) -> &'static str {
        match self {
            Self::Cancelled => "Retry when you are ready.",
            Self::Storage => "Use an absolute, persistent XDG data directory owned by your user.",
            Self::SourceCheckout => "Run the app from a build of this repository checkout.",
            Self::PythonMissing | Self::PythonVersion => "Install Python 3.14, then retry.",
            Self::Environment => {
                "Check Python 3.14 venv support and available disk space, then retry."
            }
            Self::PackageInstall => {
                "Check your network, Python development headers, and Rust toolchain, then retry."
            }
            Self::ServiceInstall => "Check the user service directory permissions, then retry.",
            Self::ForeignService => {
                "The existing service or its overrides are not managed by this project. Resolve them manually."
            }
            Self::SystemdUnavailable => {
                "Run the app in a Linux desktop session with a systemd user manager, then retry."
            }
            Self::ServiceStart | Self::DaemonExited | Self::ReadinessTimeout => {
                "Check the daemon status and that your paired AirPods are available, then retry."
            }
            Self::AirPodsUnavailable => {
                "Pair AirPods in Linux Bluetooth settings and connect them, then retry."
            }
            Self::Socket => {
                "Check that the socket belongs to your user and supports IPC protocol v1."
            }
            Self::SetupBusy => "Wait for the other app instance to finish, then retry.",
        }
    }
}

#[derive(Clone, Debug)]
struct InstallPaths {
    source: PathBuf,
    data: PathBuf,
    unit: PathBuf,
}

impl InstallPaths {
    fn discover() -> Result<Self, Failure> {
        Self::from_environment(
            |key| std::env::var_os(key),
            Path::new(env!("CARGO_MANIFEST_DIR")),
        )
    }

    fn from_environment(
        env: impl Fn(&str) -> Option<OsString>,
        manifest: &Path,
    ) -> Result<Self, Failure> {
        let base = |key: &str, fallback: &str| {
            let path = match env(key).filter(|value| !value.is_empty()) {
                Some(value) => PathBuf::from(value),
                None => PathBuf::from(env("HOME").ok_or(Failure::Storage)?).join(fallback),
            };
            if !path.is_absolute() {
                return Err(Failure::Storage);
            }
            Ok(path)
        };
        let data = base("XDG_DATA_HOME", ".local/share")?.join("airpods-hr-linux");
        // Reject volatile locations even through an existing parent symlink.
        let mut parent = data.as_path();
        while !parent.exists() {
            parent = parent.parent().ok_or(Failure::Storage)?;
        }
        let resolved = parent.canonicalize().map_err(|_| Failure::Storage)?;
        for path in [&data, &resolved] {
            if ["/tmp", "/var/tmp", "/run", "/dev/shm"]
                .iter()
                .any(|prefix| path.starts_with(prefix))
            {
                return Err(Failure::Storage);
            }
        }
        let data = resolved.join(data.strip_prefix(parent).map_err(|_| Failure::Storage)?);
        let source = manifest
            .parent()
            .and_then(Path::parent)
            .ok_or(Failure::SourceCheckout)?
            .canonicalize()
            .map_err(|_| Failure::SourceCheckout)?;
        let project = std::fs::read_to_string(source.join("pyproject.toml"))
            .map_err(|_| Failure::SourceCheckout)?;
        if !project.contains("\nname = \"airpods-hr-linux\"\n")
            || !source.join("Cargo.toml").is_file()
            || !source.join("Cargo.lock").is_file()
            || !source.join("src/airpods_hr/service_installer.py").is_file()
        {
            return Err(Failure::SourceCheckout);
        }
        Ok(Self {
            source,
            data,
            unit: base("XDG_CONFIG_HOME", ".config")?
                .join("systemd/user")
                .join(service::UNIT_NAME),
        })
    }

    fn environment(&self) -> PathBuf {
        self.data.join("daemon-venv")
    }

    fn python(&self) -> PathBuf {
        self.environment().join("bin/python")
    }

    fn installer(&self) -> PathBuf {
        self.environment().join("bin/airpods-hubd-service")
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Probe {
    Missing,
    Starting,
    Ready,
}

trait Host {
    fn command(
        &mut self,
        command: CommandSpec,
        stop: &mut watch::Receiver<bool>,
    ) -> impl Future<Output = Result<CommandResult, ProcessError>> + Send;

    fn probe(
        &mut self,
        socket: &Path,
        stop: &mut watch::Receiver<bool>,
    ) -> impl Future<Output = Result<Probe, Failure>> + Send;
}

pub struct NativeHost;

impl Host for NativeHost {
    async fn command(
        &mut self,
        command: CommandSpec,
        stop: &mut watch::Receiver<bool>,
    ) -> Result<CommandResult, ProcessError> {
        process::run(command, stop).await
    }

    async fn probe(
        &mut self,
        socket: &Path,
        stop: &mut watch::Receiver<bool>,
    ) -> Result<Probe, Failure> {
        let result = cancellable(stop, async {
            tokio::time::timeout(Duration::from_secs(2), async {
                let client = AirPodsClient::connect_to(socket).await?;
                let hello = client.hello().await?;
                if hello.service != "airpods-hubd" {
                    return Err(airpods_client::Error::UnexpectedMessage {
                        message: "unexpected daemon service".into(),
                    });
                }
                client.status().await
            })
            .await
        })
        .await?;
        match result {
            Ok(Ok(status)) => match status.state {
                DaemonState::Starting
                | DaemonState::StartingHeartRate
                | DaemonState::StoppingHeartRate => Ok(Probe::Starting),
                DaemonState::Failed => Err(Failure::AirPodsUnavailable),
                DaemonState::Stopped | DaemonState::ShuttingDown => Err(Failure::DaemonExited),
                _ => Ok(Probe::Ready),
            },
            Ok(Err(error)) if airpods_client_resilient::is_recoverable(&error) => {
                Ok(Probe::Missing)
            }
            Err(_) => Ok(Probe::Missing),
            Ok(Err(_)) => Err(Failure::Socket),
        }
    }
}

pub async fn prepare_default<F, Fut>(
    stop: &mut watch::Receiver<bool>,
    progress: F,
) -> Result<(), Failure>
where
    F: FnMut(Stage) -> Fut,
    Fut: Future<Output = bool>,
{
    let socket = AirPodsClient::default_socket_path().map_err(|_| Failure::Socket)?;
    if !socket.is_absolute() {
        return Err(Failure::Socket);
    }
    prepare(
        &socket,
        InstallPaths::discover,
        &mut NativeHost,
        stop,
        progress,
    )
    .await
}

async fn progress<F, Fut>(report: &mut F, stage: Stage) -> Result<(), Failure>
where
    F: FnMut(Stage) -> Fut,
    Fut: Future<Output = bool>,
{
    if report(stage).await {
        Ok(())
    } else {
        Err(Failure::Cancelled)
    }
}

async fn cancellable<T>(
    stop: &mut watch::Receiver<bool>,
    future: impl Future<Output = T>,
) -> Result<T, Failure> {
    if *stop.borrow() {
        return Err(Failure::Cancelled);
    }
    tokio::select! {
        biased;
        _ = stop.changed() => Err(Failure::Cancelled),
        result = future => Ok(result),
    }
}

async fn read_unit(path: &Path) -> Result<Option<String>, Failure> {
    match fs::symlink_metadata(path).await {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Ok(metadata) if metadata.is_file() => {}
        _ => return Err(Failure::ForeignService),
    }
    let file = fs::File::open(path)
        .await
        .map_err(|_| Failure::ForeignService)?;
    let mut data = Vec::new();
    file.take(65_537)
        .read_to_end(&mut data)
        .await
        .map_err(|_| Failure::ForeignService)?;
    if data.len() > 65_536 {
        return Err(Failure::ForeignService);
    }
    String::from_utf8(data)
        .map(Some)
        .map_err(|_| Failure::ForeignService)
}

fn owned_unit(contents: &Option<String>) -> Result<(), Failure> {
    if contents
        .as_deref()
        .is_some_and(|unit| !service::is_project_owned(unit))
    {
        Err(Failure::ForeignService)
    } else {
        Ok(())
    }
}

async fn run(
    host: &mut impl Host,
    stop: &mut watch::Receiver<bool>,
    command: CommandSpec,
    failure: Failure,
) -> Result<CommandResult, Failure> {
    host.command(command, stop).await.map_err(|error| {
        if error == ProcessError::Cancelled {
            Failure::Cancelled
        } else {
            failure
        }
    })
}

async fn require(
    host: &mut impl Host,
    stop: &mut watch::Receiver<bool>,
    command: CommandSpec,
    failure: Failure,
) -> Result<(), Failure> {
    if run(host, stop, command, failure).await?.success {
        Ok(())
    } else {
        Err(failure)
    }
}

async fn prepare<H, F, Fut>(
    socket: &Path,
    installation: impl FnOnce() -> Result<InstallPaths, Failure>,
    host: &mut H,
    stop: &mut watch::Receiver<bool>,
    mut report: F,
) -> Result<(), Failure>
where
    H: Host,
    F: FnMut(Stage) -> Fut,
    Fut: Future<Output = bool>,
{
    progress(&mut report, Stage::CheckingDaemon).await?;
    match host.probe(socket, stop).await? {
        Probe::Ready => return progress(&mut report, Stage::Ready).await,
        Probe::Starting => return wait_ready(socket, host, stop, &mut report, false).await,
        Probe::Missing => {}
    }
    progress(&mut report, Stage::InspectingInstallation).await?;
    let paths = installation()?;
    let unit = cancellable(stop, read_unit(&paths.unit)).await??;
    owned_unit(&unit)?;
    let expected = service::exec_start(paths.python().to_str().ok_or(Failure::Storage)?)
        .map_err(|_| Failure::Storage)?;
    let lock = cancellable(stop, async {
        fs::create_dir_all(&paths.data)
            .await
            .map_err(|_| Failure::Storage)?;
        fs::set_permissions(
            &paths.data,
            std::os::unix::fs::PermissionsExt::from_mode(0o700),
        )
        .await
        .map_err(|_| Failure::Storage)?;
        fs::OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .mode(0o600)
            .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
            .open(paths.data.join("desktop-bootstrap.lock"))
            .await
            .map_err(|_| Failure::Storage)?
            .try_into_std()
            .map_err(|_| Failure::Storage)
    })
    .await??;
    let deadline = Instant::now() + INSTALL_TIMEOUT;
    let mut reported_waiting = false;
    loop {
        match rustix::fs::flock(&lock, rustix::fs::FlockOperation::NonBlockingLockExclusive) {
            Ok(()) => break,
            Err(rustix::io::Errno::WOULDBLOCK) => {
                if Instant::now() >= deadline {
                    return Err(Failure::SetupBusy);
                }
                if !reported_waiting {
                    progress(&mut report, Stage::WaitingForSetup).await?;
                    reported_waiting = true;
                }
                cancellable(stop, tokio::time::sleep(POLL_INTERVAL)).await?;
            }
            Err(_) => return Err(Failure::Storage),
        }
    }
    // Another app may have completed setup while this instance waited.
    match host.probe(socket, stop).await? {
        Probe::Ready => return progress(&mut report, Stage::Ready).await,
        Probe::Starting => return wait_ready(socket, host, stop, &mut report, false).await,
        Probe::Missing => {}
    }
    let unit = cancellable(stop, read_unit(&paths.unit)).await??;
    owned_unit(&unit)?;
    require(
        host,
        stop,
        CommandSpec::new(
            "systemctl",
            ["--user", "show", "--property=Version", "--value"],
            COMMAND_TIMEOUT,
        ),
        Failure::SystemdUnavailable,
    )
    .await?;
    // Loaded units elsewhere and local overrides must not be shadowed silently.
    let loaded = run(
        host,
        stop,
        CommandSpec::new(
            "systemctl",
            [
                "--user",
                "show",
                service::UNIT_NAME,
                "--property=LoadState",
                "--property=FragmentPath",
                "--property=DropInPaths",
            ],
            COMMAND_TIMEOUT,
        ),
        Failure::SystemdUnavailable,
    )
    .await?;
    let loaded_text = std::str::from_utf8(&loaded.stdout).map_err(|_| Failure::ForeignService)?;
    if loaded.stdout.len() >= 4096 {
        return Err(Failure::ForeignService);
    }
    if !loaded.success
        && !loaded_text
            .lines()
            .any(|line| line == "LoadState=not-found")
    {
        return Err(Failure::SystemdUnavailable);
    }
    for line in loaded_text.lines() {
        if let Some(fragment) = line
            .strip_prefix("FragmentPath=")
            .filter(|value| !value.is_empty())
        {
            let contents = cancellable(stop, read_unit(Path::new(fragment))).await??;
            if contents.is_none() {
                return Err(Failure::ForeignService);
            }
            owned_unit(&contents)?;
        }
        if line
            .strip_prefix("DropInPaths=")
            .is_some_and(|value| !value.is_empty())
        {
            return Err(Failure::ForeignService);
        }
    }
    let environment = paths.environment();
    match cancellable(stop, fs::symlink_metadata(&environment)).await? {
        Ok(metadata) if metadata.is_dir() => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        _ => return Err(Failure::Storage),
    }
    let mut complete = true;
    for file in [
        paths.python(),
        paths.installer(),
        environment.join("bin/airpods-hubd"),
    ] {
        complete &= cancellable(stop, fs::metadata(file))
            .await?
            .is_ok_and(|metadata| metadata.is_file() && metadata.permissions().mode() & 0o111 != 0);
    }
    complete &= cancellable(stop, fs::metadata(environment.join("pyvenv.cfg")))
        .await?
        .is_ok_and(|metadata| metadata.is_file());
    let env_check = || {
        CommandSpec::new(
            paths.python(),
            [
                OsString::from("-I"),
                OsString::from("-c"),
                OsString::from(ENV_CHECK),
                environment.as_os_str().into(),
            ],
            COMMAND_TIMEOUT,
        )
    };
    if complete {
        complete = match run(host, stop, env_check(), Failure::Environment).await {
            Ok(result) => result.success,
            Err(Failure::Cancelled) => return Err(Failure::Cancelled),
            Err(_) => false,
        };
    }
    if !complete {
        progress(&mut report, Stage::FindingPython).await?;
        let version = run(
            host,
            stop,
            CommandSpec::new("python3.14", ["-I", "-c", VERSION_CHECK], COMMAND_TIMEOUT),
            Failure::PythonMissing,
        )
        .await?;
        if !version.success || version.stdout != b"3.14\n" {
            return Err(Failure::PythonVersion);
        }
        let marker = paths.data.join("managed-environment-v1");
        let owned = cancellable(stop, fs::read(&marker))
            .await?
            .is_ok_and(|contents| contents == ENV_MARKER.as_bytes());
        // Never clear an unrelated directory that happens to occupy this path.
        if !owned
            && cancellable(stop, fs::symlink_metadata(&environment))
                .await?
                .is_ok()
        {
            return Err(Failure::Environment);
        }
        cancellable(stop, fs::write(marker, ENV_MARKER))
            .await?
            .map_err(|_| Failure::Storage)?;
        progress(&mut report, Stage::CreatingEnvironment).await?;
        require(
            host,
            stop,
            CommandSpec::new(
                "python3.14",
                [
                    OsString::from("-I"),
                    OsString::from("-m"),
                    OsString::from("venv"),
                    OsString::from("--clear"),
                    environment.as_os_str().into(),
                ],
                Duration::from_secs(90),
            ),
            Failure::Environment,
        )
        .await?;
        progress(&mut report, Stage::InstallingDaemon).await?;
        require(
            host,
            stop,
            CommandSpec::new(
                paths.python(),
                [
                    OsString::from("-I"),
                    OsString::from("-m"),
                    OsString::from("pip"),
                    OsString::from("--isolated"),
                    OsString::from("--disable-pip-version-check"),
                    OsString::from("install"),
                    OsString::from("--no-input"),
                    OsString::from("--config-settings=build-args=--locked"),
                    paths.source.as_os_str().into(),
                ],
                INSTALL_TIMEOUT,
            ),
            Failure::PackageInstall,
        )
        .await?;
        require(host, stop, env_check(), Failure::Environment).await?;
        for file in [
            paths.python(),
            paths.installer(),
            environment.join("bin/airpods-hubd"),
        ] {
            if !cancellable(stop, fs::metadata(file))
                .await?
                .is_ok_and(|metadata| {
                    metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
                })
            {
                return Err(Failure::Environment);
            }
        }
    }
    // Setup may have taken minutes. Recheck ownership before asking the
    // authoritative installer to replace anything; it also checks at write time.
    let unit = cancellable(stop, read_unit(&paths.unit)).await??;
    owned_unit(&unit)?;
    let state = service::installation_state(unit.as_deref(), &expected);
    let pending = paths.data.join("service-install-pending");
    if !service::installation_valid(unit.is_some(), state.owned, state.exec_start_matches)
        || cancellable(stop, fs::symlink_metadata(&pending))
            .await?
            .is_ok()
    {
        progress(&mut report, Stage::InstallingService).await?;
        cancellable(stop, fs::write(&pending, b"install\n"))
            .await?
            .map_err(|_| Failure::Storage)?;
        let installation = run(
            host,
            stop,
            CommandSpec::new(paths.installer(), ["install"], COMMAND_TIMEOUT),
            Failure::ServiceInstall,
        )
        .await?;
        if !installation.success {
            owned_unit(&cancellable(stop, read_unit(&paths.unit)).await??)?;
            return Err(Failure::ServiceInstall);
        }
        let installed = cancellable(stop, read_unit(&paths.unit)).await??;
        let state = service::installation_state(installed.as_deref(), &expected);
        if !service::installation_valid(installed.is_some(), state.owned, state.exec_start_matches)
        {
            return Err(Failure::ServiceInstall);
        }
        cancellable(stop, fs::remove_file(pending))
            .await?
            .map_err(|_| Failure::Storage)?;
    }
    // Recheck immediately before activation; never restart an external session.
    match host.probe(socket, stop).await? {
        Probe::Ready => return progress(&mut report, Stage::Ready).await,
        Probe::Starting => return wait_ready(socket, host, stop, &mut report, false).await,
        Probe::Missing => {}
    }
    progress(&mut report, Stage::StartingDaemon).await?;
    require(
        host,
        stop,
        CommandSpec::new(
            "systemctl",
            ["--user", "start", service::UNIT_NAME],
            COMMAND_TIMEOUT,
        ),
        Failure::ServiceStart,
    )
    .await?;
    wait_ready(socket, host, stop, &mut report, true).await
}

async fn wait_ready<H, F, Fut>(
    socket: &Path,
    host: &mut H,
    stop: &mut watch::Receiver<bool>,
    report: &mut F,
    started_service: bool,
) -> Result<(), Failure>
where
    H: Host,
    F: FnMut(Stage) -> Fut,
    Fut: Future<Output = bool>,
{
    progress(report, Stage::WaitingForDaemon).await?;
    let deadline = Instant::now() + READY_TIMEOUT;
    let mut waiting_for_airpods = false;
    loop {
        let timeout_failure = if waiting_for_airpods {
            Failure::AirPodsUnavailable
        } else {
            Failure::ReadinessTimeout
        };
        match tokio::time::timeout_at(deadline, host.probe(socket, stop))
            .await
            .map_err(|_| timeout_failure)??
        {
            Probe::Ready => return progress(report, Stage::Ready).await,
            Probe::Starting => waiting_for_airpods = true,
            Probe::Missing => {}
        }
        if Instant::now() >= deadline {
            return Err(if waiting_for_airpods {
                Failure::AirPodsUnavailable
            } else {
                Failure::ReadinessTimeout
            });
        }
        if started_service {
            let state = run(
                host,
                stop,
                CommandSpec::new(
                    "systemctl",
                    [
                        "--user",
                        "show",
                        service::UNIT_NAME,
                        "--property=ActiveState",
                        "--property=SubState",
                    ],
                    COMMAND_TIMEOUT.min(deadline.saturating_duration_since(Instant::now())),
                ),
                Failure::ReadinessTimeout,
            )
            .await?;
            if !state.success
                || state.stdout.split(|byte| *byte == b'\n').any(|line| {
                    matches!(
                        line,
                        b"ActiveState=failed" | b"ActiveState=inactive" | b"SubState=dead"
                    )
                })
            {
                return Err(Failure::DaemonExited);
            }
        }
        cancellable(
            stop,
            tokio::time::sleep_until((Instant::now() + POLL_INTERVAL).min(deadline)),
        )
        .await?;
    }
}
