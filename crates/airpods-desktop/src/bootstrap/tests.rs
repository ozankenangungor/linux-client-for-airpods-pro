use super :: * ;

use std :: sync :: atomic :: { AtomicU64 , Ordering } ;

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
