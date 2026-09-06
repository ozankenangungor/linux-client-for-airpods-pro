//! Local daemon prerequisites, separate from the resilient IPC subscription.
//! Files belong to this user; only the package's installer writes service units.

mod process;
#[cfg(test)]
mod tests;

use airpods_app_core :: service ;
use airpods_client :: { AirPodsClient , DaemonState } ;
use process :: { CommandResult , CommandSpec , ProcessError } ;
use std :: ffi :: OsString ;
use std :: future :: Future ;
use std :: os :: unix :: fs :: PermissionsExt ;
use std :: path :: { Path , PathBuf } ;
use std :: time :: Duration ;
use tokio :: fs ;
use tokio :: io :: AsyncReadExt ;
use tokio :: sync :: watch ;










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






