use super::*;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_FIXTURE: AtomicU64 = AtomicU64::new(0);

struct Fixture {
    root: PathBuf,
    paths: InstallPaths,
    socket: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "desktop-bootstrap-{}-{}",
            std::process::id(),
            NEXT_FIXTURE.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&root).unwrap();
        Self {
            paths: InstallPaths {
                source: root.join("source checkout"),
                data: root.join("data home/airpods-hr-linux"),
                unit: root.join("config home/systemd/user/airpods-hubd.service"),
            },
            socket: root.join("daemon.sock"),
            root,
        }
    }

    fn environment(&self) {
        std::fs::create_dir_all(self.paths.environment().join("bin")).unwrap();
        for name in [
            "bin/python",
            "bin/airpods-hubd",
            "bin/airpods-hubd-service",
            "pyvenv.cfg",
        ] {
            std::fs::write(
                self.paths.environment().join(name),
                b"fake installed entrypoint\n",
            )
            .unwrap();
            if name.starts_with("bin/") {
                std::fs::set_permissions(
                    self.paths.environment().join(name),
                    std::fs::Permissions::from_mode(0o700),
                )
                .unwrap();
            }
        }
    }

    fn unit(&self, python: &str) {
        std::fs::create_dir_all(self.paths.unit.parent().unwrap()).unwrap();
        std::fs::write(&self.paths.unit, service::render_unit(python).unwrap()).unwrap();
    }

    fn valid_installation(&self) {
        self.environment();
        self.unit(self.paths.python().to_str().unwrap());
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.root).unwrap();
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Action {
    Version,
    Venv,
    Pip,
    EnvCheck,
    Installer,
    Manager,
    LoadedUnit,
    Start,
    Status,
}

struct FakeHost {
    paths: InstallPaths,
    commands: Vec<(Action, CommandSpec)>,
    probes: usize,
    sequence: VecDeque<Probe>,
    started: bool,
    ready: bool,
    status: &'static [u8],
    version: &'static [u8],
    fail: Option<Action>,
    unavailable: Option<Action>,
    block: Option<Action>,
    entered: Option<tokio::sync::oneshot::Sender<()>>,
    loaded: String,
    replace_unit_at: Option<Action>,
}

impl FakeHost {
    fn new(fixture: &Fixture) -> Self {
        Self {
            paths: fixture.paths.clone(),
            commands: Vec::new(),
            probes: 0,
            sequence: VecDeque::new(),
            started: false,
            ready: true,
            status: b"ActiveState=active\nSubState=running\n",
            version: b"3.14\n",
            fail: None,
            unavailable: None,
            block: None,
            entered: None,
            loaded: "FragmentPath=\nDropInPaths=\n".into(),
            replace_unit_at: None,
        }
    }

    fn actions(&self) -> Vec<Action> {
        self.commands.iter().map(|(action, _)| *action).collect()
    }

    fn action(&self, spec: &CommandSpec) -> Action {
        let has = |arg: &str| spec.args.iter().any(|value| value == arg);
        if spec.program == "python3.14" {
            if has("venv") {
                Action::Venv
            } else {
                Action::Version
            }
        } else if spec.program == self.paths.python() {
            if has("pip") {
                Action::Pip
            } else {
                Action::EnvCheck
            }
        } else if spec.program == self.paths.installer() {
            Action::Installer
        } else {
            assert_eq!(spec.program, "systemctl");
            if has("--property=Version") {
                Action::Manager
            } else if has("--property=FragmentPath") {
                Action::LoadedUnit
            } else if has("start") {
                Action::Start
            } else {
                assert!(has("--property=ActiveState"));
                Action::Status
            }
        }
    }
}

impl Host for FakeHost {
    async fn command(
        &mut self,
        command: CommandSpec,
        stop: &mut watch::Receiver<bool>,
    ) -> Result<CommandResult, ProcessError> {
        if *stop.borrow() {
            return Err(ProcessError::Cancelled);
        }
        let action = self.action(&command);
        assert!(!command.args.iter().any(|value| matches!(
            value.to_str(),
            Some("--force" | "--enable" | "restart" | "sudo")
        )));
        assert!(command.timeout > Duration::ZERO);
        self.commands.push((action, command));
        if self.block == Some(action) {
            self.entered.take().unwrap().send(()).unwrap();
            stop.changed().await.unwrap();
            return Err(ProcessError::Cancelled);
        }
        if self.unavailable == Some(action) {
            return Err(ProcessError::Unavailable);
        }
        if self.fail == Some(action) {
            return Ok(CommandResult {
                success: false,
                stdout: b"private error data that must never reach the UI".to_vec(),
            });
        }
        let stdout = match action {
            Action::Version => self.version.to_vec(),
            Action::LoadedUnit => self.loaded.as_bytes().to_vec(),
            Action::Status => self.status.to_vec(),
            Action::Venv => {
                fs::create_dir_all(self.paths.environment().join("bin"))
                    .await
                    .unwrap();
                fs::write(self.paths.python(), b"fake python")
                    .await
                    .unwrap();
                fs::set_permissions(self.paths.python(), std::fs::Permissions::from_mode(0o700))
                    .await
                    .unwrap();
                fs::write(self.paths.environment().join("pyvenv.cfg"), b"fake venv")
                    .await
                    .unwrap();
                Vec::new()
            }
            Action::Pip => {
                for path in [
                    self.paths.installer(),
                    self.paths.environment().join("bin/airpods-hubd"),
                ] {
                    fs::write(&path, b"fake entrypoint").await.unwrap();
                    fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
                        .await
                        .unwrap();
                }
                Vec::new()
            }
            Action::Installer => {
                fs::create_dir_all(self.paths.unit.parent().unwrap())
                    .await
                    .unwrap();
                fs::write(
                    &self.paths.unit,
                    service::render_unit(self.paths.python().to_str().unwrap()).unwrap(),
                )
                .await
                .unwrap();
                Vec::new()
            }
            Action::Start => {
                self.started = true;
                Vec::new()
            }
            _ => Vec::new(),
        };
        if self.replace_unit_at == Some(action) {
            fs::create_dir_all(self.paths.unit.parent().unwrap())
                .await
                .unwrap();
            fs::write(&self.paths.unit, b"[Service]\nExecStart=/foreign\n")
                .await
                .unwrap();
        }
        Ok(CommandResult {
            success: true,
            stdout,
        })
    }

    async fn probe(
        &mut self,
        socket: &Path,
        stop: &mut watch::Receiver<bool>,
    ) -> Result<Probe, Failure> {
        if *stop.borrow() {
            return Err(Failure::Cancelled);
        }
        assert!(socket.is_absolute());
        self.probes += 1;
        Ok(self
            .sequence
            .pop_front()
            .unwrap_or(if self.started && self.ready {
                Probe::Ready
            } else {
                Probe::Missing
            }))
    }
}

async fn execute(fixture: &Fixture, host: &mut FakeHost) -> (Result<(), Failure>, Vec<Stage>) {
    let (_stop, mut stopped) = watch::channel(false);
    let mut stages = Vec::new();
    let result = prepare(
        &fixture.socket,
        || Ok(fixture.paths.clone()),
        host,
        &mut stopped,
        |stage| {
            stages.push(stage);
            std::future::ready(true)
        },
    )
    .await;
    (result, stages)
}

#[tokio::test]
async fn existing_usable_daemon_never_discovers_or_mutates_installation() {
    let fixture = Fixture::new();
    let mut host = FakeHost::new(&fixture);
    host.sequence.push_back(Probe::Ready);
    let (_stop, mut stopped) = watch::channel(false);
    let result = prepare(
        &fixture.socket,
        || panic!("running daemon must bypass source, XDG paths and Python discovery"),
        &mut host,
        &mut stopped,
        |_| std::future::ready(true),
    )
    .await;
    assert_eq!(result, Ok(()));
    assert!(host.commands.is_empty());
    assert_eq!(host.probes, 1);
    assert!(!fixture.paths.data.exists());
    assert!(!fixture.paths.unit.exists());
}

#[tokio::test]
async fn first_run_creates_installs_starts_and_waits_for_readiness() {
    let fixture = Fixture::new();
    let mut host = FakeHost::new(&fixture);
    let (result, stages) = execute(&fixture, &mut host).await;
    assert_eq!(result, Ok(()));
    assert_eq!(
        host.actions(),
        [
            Action::Manager,
            Action::LoadedUnit,
            Action::Version,
            Action::Venv,
            Action::Pip,
            Action::EnvCheck,
            Action::Installer,
            Action::Start
        ]
    );
    assert_eq!(
        stages,
        [
            Stage::CheckingDaemon,
            Stage::InspectingInstallation,
            Stage::FindingPython,
            Stage::CreatingEnvironment,
            Stage::InstallingDaemon,
            Stage::InstallingService,
            Stage::StartingDaemon,
            Stage::WaitingForDaemon,
            Stage::Ready
        ]
    );
    let pip = &host
        .commands
        .iter()
        .find(|(action, _)| *action == Action::Pip)
        .unwrap()
        .1;
    assert!(
        pip.args
            .contains(&OsString::from("--config-settings=build-args=--locked"))
    );
    assert_eq!(
        pip.args.last(),
        Some(&fixture.paths.source.as_os_str().into())
    );
    assert_eq!(host.probes, 4);
    assert!(
        fs::read_to_string(&fixture.paths.unit)
            .await
            .unwrap()
            .contains(&service::exec_start(fixture.paths.python().to_str().unwrap()).unwrap())
    );
}

#[tokio::test]
async fn valid_environment_and_service_skip_all_installs() {
    let fixture = Fixture::new();
    fixture.valid_installation();
    let before = fs::read(&fixture.paths.unit).await.unwrap();
    let mut host = FakeHost::new(&fixture);
    assert_eq!(execute(&fixture, &mut host).await.0, Ok(()));
    assert_eq!(
        host.actions(),
        [
            Action::Manager,
            Action::LoadedUnit,
            Action::EnvCheck,
            Action::Start
        ]
    );
    assert_eq!(fs::read(&fixture.paths.unit).await.unwrap(), before);
}

#[tokio::test]
async fn missing_service_uses_valid_environment_without_reinstalling_package() {
    let fixture = Fixture::new();
    fixture.environment();
    let mut host = FakeHost::new(&fixture);
    assert_eq!(execute(&fixture, &mut host).await.0, Ok(()));
    assert_eq!(
        host.actions(),
        [
            Action::Manager,
            Action::LoadedUnit,
            Action::EnvCheck,
            Action::Installer,
            Action::Start
        ]
    );
}

#[tokio::test]
async fn stale_project_owned_tmp_unit_is_repaired_by_installer_without_force() {
    let fixture = Fixture::new();
    fixture.environment();
    let deleted = fixture.root.join("deleted-development-env/bin/python");
    assert!(!deleted.exists());
    fixture.unit(deleted.to_str().unwrap());
    let before = fs::read_to_string(&fixture.paths.unit).await.unwrap();
    assert!(before.contains("/tmp/"));
    let mut host = FakeHost::new(&fixture);
    assert_eq!(execute(&fixture, &mut host).await.0, Ok(()));
    let installer = &host
        .commands
        .iter()
        .find(|(action, _)| *action == Action::Installer)
        .unwrap()
        .1;
    assert_eq!(installer.program, fixture.paths.installer());
    assert_eq!(installer.args, [OsString::from("install")]);
    let after = fs::read_to_string(&fixture.paths.unit).await.unwrap();
    assert!(!after.contains("deleted-development-env"));
    assert!(after.contains("daemon-venv/bin/python"));
    assert_eq!(host.actions().last(), Some(&Action::Start));
}

#[tokio::test]
async fn foreign_service_is_unchanged_and_fails_before_python_or_commands() {
    let fixture = Fixture::new();
    fs::create_dir_all(fixture.paths.unit.parent().unwrap())
        .await
        .unwrap();
    let foreign = b"[Service]\nExecStart=/usr/bin/unrelated-service\n";
    fs::write(&fixture.paths.unit, foreign).await.unwrap();
    let mut host = FakeHost::new(&fixture);
    assert_eq!(
        execute(&fixture, &mut host).await.0,
        Err(Failure::ForeignService)
    );
    assert!(host.commands.is_empty());
    assert_eq!(fs::read(&fixture.paths.unit).await.unwrap(), foreign);
    assert!(!fixture.paths.data.exists());
}

#[tokio::test]
async fn loaded_foreign_service_and_dropins_are_never_shadowed() {
    for dropin in [false, true] {
        let fixture = Fixture::new();
        let foreign = fixture.root.join("foreign.service");
        fs::write(&foreign, b"[Service]\nExecStart=/foreign\n")
            .await
            .unwrap();
        let mut host = FakeHost::new(&fixture);
        host.loaded = if dropin {
            "FragmentPath=\nDropInPaths=/foreign.conf\n".into()
        } else {
            format!("FragmentPath={}\nDropInPaths=\n", foreign.display())
        };
        assert_eq!(
            execute(&fixture, &mut host).await.0,
            Err(Failure::ForeignService)
        );
        assert_eq!(host.actions(), [Action::Manager, Action::LoadedUnit]);
        assert!(!fixture.paths.unit.exists());
    }
}

#[tokio::test]
async fn missing_python_is_actionable_and_does_not_install() {
    let fixture = Fixture::new();
    let mut host = FakeHost::new(&fixture);
    host.unavailable = Some(Action::Version);
    assert_eq!(
        execute(&fixture, &mut host).await.0,
        Err(Failure::PythonMissing)
    );
    assert!(!host.actions().contains(&Action::Venv));
    assert_eq!(Failure::PythonMissing.title(), "Python 3.14 is required");
}

#[tokio::test]
async fn wrong_python_version_is_rejected() {
    let fixture = Fixture::new();
    let mut host = FakeHost::new(&fixture);
    host.version = b"3.13\n";
    assert_eq!(
        execute(&fixture, &mut host).await.0,
        Err(Failure::PythonVersion)
    );
    assert!(!host.actions().contains(&Action::Venv));
}

#[tokio::test]
async fn each_install_and_start_failure_is_typed_and_terminal() {
    for (action, failure) in [
        (Action::Manager, Failure::SystemdUnavailable),
        (Action::LoadedUnit, Failure::SystemdUnavailable),
        (Action::Venv, Failure::Environment),
        (Action::Pip, Failure::PackageInstall),
        (Action::Installer, Failure::ServiceInstall),
        (Action::Start, Failure::ServiceStart),
    ] {
        let fixture = Fixture::new();
        let mut host = FakeHost::new(&fixture);
        host.fail = Some(action);
        assert_eq!(execute(&fixture, &mut host).await.0, Err(failure));
        assert_eq!(host.actions().last(), Some(&action));
        assert!(!failure.title().contains("private"));
        assert!(!failure.hint().contains("private"));
    }
}

#[tokio::test(start_paused = true)]
async fn readiness_deadline_is_bounded_and_does_not_reinstall() {
    let fixture = Fixture::new();
    fixture.valid_installation();
    let mut host = FakeHost::new(&fixture);
    host.ready = false;
    let before = Instant::now();
    assert_eq!(
        execute(&fixture, &mut host).await.0,
        Err(Failure::ReadinessTimeout)
    );
    assert!(before.elapsed() <= READY_TIMEOUT + POLL_INTERVAL);
    assert_eq!(
        host.actions()
            .iter()
            .filter(|action| **action == Action::Start)
            .count(),
        1
    );
    assert!(!host.actions().contains(&Action::Installer));
}

#[tokio::test]
async fn service_exit_is_reported_without_raw_output() {
    let fixture = Fixture::new();
    fixture.valid_installation();
    let mut host = FakeHost::new(&fixture);
    host.ready = false;
    host.status = b"ActiveState=failed\nSubState=dead\n";
    assert_eq!(
        execute(&fixture, &mut host).await.0,
        Err(Failure::DaemonExited)
    );
}

#[tokio::test(start_paused = true)]
async fn existing_starting_daemon_waits_for_airpods_without_bootstrapping() {
    let fixture = Fixture::new();
    let mut host = FakeHost::new(&fixture);
    host.sequence = VecDeque::from(vec![Probe::Starting; 150]);
    assert_eq!(
        execute(&fixture, &mut host).await.0,
        Err(Failure::AirPodsUnavailable)
    );
    assert!(host.commands.is_empty());
    assert!(!fixture.paths.data.exists());
}

#[tokio::test]
async fn daemon_started_by_another_client_before_activation_is_reused() {
    let fixture = Fixture::new();
    fixture.valid_installation();
    let mut host = FakeHost::new(&fixture);
    host.sequence = VecDeque::from([Probe::Missing, Probe::Missing, Probe::Ready]);
    assert_eq!(execute(&fixture, &mut host).await.0, Ok(()));
    assert!(!host.actions().contains(&Action::Start));
}

#[tokio::test]
async fn interrupted_service_install_repeats_authoritative_reload_on_retry() {
    let fixture = Fixture::new();
    fixture.valid_installation();
    fs::write(
        fixture.paths.data.join("service-install-pending"),
        b"install\n",
    )
    .await
    .unwrap();
    let mut host = FakeHost::new(&fixture);
    assert_eq!(execute(&fixture, &mut host).await.0, Ok(()));
    assert!(host.actions().contains(&Action::Installer));
    assert!(!fixture.paths.data.join("service-install-pending").exists());
}

#[tokio::test]
async fn interrupted_environment_is_rebuilt_only_when_owned() {
    for owned in [false, true] {
        let fixture = Fixture::new();
        fs::create_dir_all(fixture.paths.environment())
            .await
            .unwrap();
        if owned {
            fs::write(
                fixture.paths.data.join("managed-environment-v1"),
                ENV_MARKER,
            )
            .await
            .unwrap();
        }
        let mut host = FakeHost::new(&fixture);
        let result = execute(&fixture, &mut host).await.0;
        assert_eq!(
            result,
            if owned {
                Ok(())
            } else {
                Err(Failure::Environment)
            }
        );
        assert_eq!(host.actions().contains(&Action::Venv), owned);
    }
}

#[tokio::test]
async fn closing_during_each_setup_command_is_bounded() {
    for action in [Action::Venv, Action::Pip, Action::Installer, Action::Start] {
        let fixture = Fixture::new();
        let mut host = FakeHost::new(&fixture);
        let (entered, waiting) = tokio::sync::oneshot::channel();
        host.block = Some(action);
        host.entered = Some(entered);
        let (stop, mut stopped) = watch::channel(false);
        let mut preparing = Box::pin(prepare(
            &fixture.socket,
            || Ok(fixture.paths.clone()),
            &mut host,
            &mut stopped,
            |_| std::future::ready(true),
        ));
        tokio::select! { result = &mut preparing => panic!("bootstrap completed early: {result:?}"), _ = waiting => {} }
        let before = std::time::Instant::now();
        stop.send(true).unwrap();
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(1), preparing)
                .await
                .unwrap(),
            Err(Failure::Cancelled)
        );
        assert!(before.elapsed() < Duration::from_secs(1));
    }
}

#[tokio::test]
async fn closing_during_readiness_is_bounded() {
    let fixture = Fixture::new();
    fixture.valid_installation();
    let mut host = FakeHost::new(&fixture);
    host.ready = false;
    let (stop, mut stopped) = watch::channel(false);
    let preparing = prepare(
        &fixture.socket,
        || Ok(fixture.paths.clone()),
        &mut host,
        &mut stopped,
        |stage| {
            if stage == Stage::WaitingForDaemon {
                stop.send(true).unwrap();
            }
            std::future::ready(true)
        },
    );
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(1), preparing)
            .await
            .unwrap(),
        Err(Failure::Cancelled)
    );
}

#[tokio::test]
async fn closing_while_waiting_for_setup_lock_is_bounded() {
    let fixture = Fixture::new();
    fs::create_dir_all(&fixture.paths.data).await.unwrap();
    let lock = std::fs::File::create(fixture.paths.data.join("desktop-bootstrap.lock")).unwrap();
    rustix::fs::flock(&lock, rustix::fs::FlockOperation::NonBlockingLockExclusive).unwrap();
    let mut host = FakeHost::new(&fixture);
    let (stop, mut stopped) = watch::channel(false);
    let preparing = prepare(
        &fixture.socket,
        || Ok(fixture.paths.clone()),
        &mut host,
        &mut stopped,
        |stage| {
            if stage == Stage::WaitingForSetup {
                stop.send(true).unwrap();
            }
            std::future::ready(true)
        },
    );
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(1), preparing)
            .await
            .unwrap(),
        Err(Failure::Cancelled)
    );
    assert!(host.commands.is_empty());
}

#[test]
fn xdg_paths_use_stable_data_and_installer_config_semantics() {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    for xdg in [false, true] {
        let paths = InstallPaths::from_environment(
            |key| match key {
                "HOME" => Some("/home/example".into()),
                "XDG_DATA_HOME" if xdg => Some("/home/example/data space".into()),
                "XDG_CONFIG_HOME" if xdg => Some("/home/example/config space".into()),
                _ => None,
            },
            manifest,
        )
        .unwrap();
        assert_eq!(
            paths.environment(),
            PathBuf::from(if xdg {
                "/home/example/data space/airpods-hr-linux/daemon-venv"
            } else {
                "/home/example/.local/share/airpods-hr-linux/daemon-venv"
            })
        );
        assert_eq!(
            paths.unit,
            PathBuf::from(if xdg {
                "/home/example/config space/systemd/user/airpods-hubd.service"
            } else {
                "/home/example/.config/systemd/user/airpods-hubd.service"
            })
        );
        assert!(paths.source.join("pyproject.toml").is_file());
    }
}

#[test]
fn relative_or_volatile_data_paths_and_arbitrary_source_roots_are_rejected() {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR"));
    for data in [
        "relative",
        "/tmp/env",
        "/var/tmp/env",
        "/run/env",
        "/dev/shm/env",
    ] {
        assert_eq!(
            InstallPaths::from_environment(
                |key| match key {
                    "HOME" => Some("/home/example".into()),
                    "XDG_DATA_HOME" => Some(data.into()),
                    _ => None,
                },
                manifest
            )
            .unwrap_err(),
            Failure::Storage
        );
    }
    assert_eq!(
        InstallPaths::from_environment(
            |key| (key == "HOME").then(|| "/home/example".into()),
            Path::new("/not/a/source/crates/airpods-desktop")
        )
        .unwrap_err(),
        Failure::SourceCheckout
    );
}

#[tokio::test]
async fn service_becoming_foreign_during_package_install_is_left_unchanged() {
    let fixture = Fixture::new();
    let mut host = FakeHost::new(&fixture);
    host.replace_unit_at = Some(Action::Pip);
    assert_eq!(
        execute(&fixture, &mut host).await.0,
        Err(Failure::ForeignService)
    );
    assert!(!host.actions().contains(&Action::Installer));
    assert!(!host.actions().contains(&Action::Start));
    assert_eq!(
        fs::read(&fixture.paths.unit).await.unwrap(),
        b"[Service]\nExecStart=/foreign\n"
    );
}

#[tokio::test]
async fn owned_environment_with_missing_executable_permissions_is_repaired() {
    let fixture = Fixture::new();
    fixture.valid_installation();
    fs::write(
        fixture.paths.data.join("managed-environment-v1"),
        ENV_MARKER,
    )
    .await
    .unwrap();
    fs::set_permissions(
        fixture.paths.installer(),
        std::fs::Permissions::from_mode(0o600),
    )
    .await
    .unwrap();
    let mut host = FakeHost::new(&fixture);
    assert_eq!(execute(&fixture, &mut host).await.0, Ok(()));
    assert!(host.actions().contains(&Action::Venv));
    assert!(host.actions().contains(&Action::Pip));
    assert!(!host.actions().contains(&Action::Installer));
}

#[tokio::test]
async fn native_probe_uses_only_hello_and_status_on_a_fake_socket() {
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    for (state, expected) in [("ready", Probe::Ready), ("starting", Probe::Starting)] {
        let fixture = Fixture::new();
        let listener = tokio::net::UnixListener::bind(&fixture.socket).unwrap();
        let server = tokio::spawn(async move {
            let (socket, _) = listener.accept().await.unwrap();
            let (read, mut write) = socket.into_split();
            let mut reader = BufReader::new(read);
            let mut request = String::new();
            reader.read_line(&mut request).await.unwrap();
            assert!(request.contains("\"operation\":\"hello\""));
            write.write_all(b"{\"protocol_version\":1,\"ok\":true,\"operation\":\"hello\",\"service\":\"airpods-hubd\",\"experimental\":true}\n").await.unwrap();
            request.clear();
            reader.read_line(&mut request).await.unwrap();
            assert!(request.contains("\"operation\":\"status\""));
            write.write_all(format!("{{\"protocol_version\":1,\"ok\":true,\"operation\":\"status\",\"state\":\"{state}\",\"subscriber_count\":0}}\n").as_bytes()).await.unwrap();
        });
        let (_stop, mut stopped) = watch::channel(false);
        assert_eq!(
            NativeHost.probe(&fixture.socket, &mut stopped).await,
            Ok(expected)
        );
        server.await.unwrap();
    }
}
