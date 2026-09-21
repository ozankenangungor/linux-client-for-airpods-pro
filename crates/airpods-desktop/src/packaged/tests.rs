use super::*;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
static NEXT: AtomicU64 = AtomicU64::new(0);

pub(crate) struct Fixture {
    pub root: PathBuf,
    pub payload: Payload,
}
impl Fixture {
    pub fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "appimage-fixture-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&root).unwrap();
        let payload = Payload {
            root: root.join("App Dir"),
            image: root.join("download.AppImage"),
        };
        for path in [
            payload.python(),
            payload.root.join("AppRun"),
            payload.root.join("usr/bin/airpods-desktop"),
        ] {
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, b"fake executable").unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let package = payload
            .root
            .join("usr/lib/airpods-hr-linux/python/lib/python3.14/site-packages/airpods_hr");
        std::fs::create_dir_all(package.join("_hubd")).unwrap();
        std::fs::write(package.join("service_installer.py"), b"fake installer").unwrap();
        std::fs::write(package.join("_hubd/main.py"), b"fake daemon").unwrap();
        std::fs::write(payload.root.join("airpods-distribution"), IDENTITY).unwrap();
        std::fs::write(
            &payload.image,
            b"\x7fELF\x02\x01\x01\0AI\x02fake squashfs payload",
        )
        .unwrap();
        Self { root, payload }
    }
    fn detect(&self) -> Result<Option<Payload>, Failure> {
        let env: HashMap<&str, OsString> = [
            ("AIRPODS_APPIMAGE", "1".into()),
            ("APPDIR", self.payload.root.as_os_str().into()),
            ("APPIMAGE", self.payload.image.as_os_str().into()),
        ]
        .into();
        Payload::discover_with_origin(
            |key| env.get(key).cloned(),
            &self.payload.root.join("usr/bin/airpods-desktop"),
            |_, _| true,
        )
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.root).unwrap();
    }
}

#[test]
fn source_mode_ignores_untrusted_appdir_alone() {
    assert!(
        Payload::discover(
            |key| (key == "APPDIR").then(|| "/untrusted".into()),
            Path::new("/build/airpods-desktop")
        )
        .unwrap()
        .is_none()
    );
}
#[test]
fn packaged_detection_requires_own_executable_and_payload_identity() {
    let f = Fixture::new();
    assert!(f.detect().unwrap().is_some());
    std::fs::write(
        f.payload.root.join("airpods-distribution"),
        "another application",
    )
    .unwrap();
    assert_eq!(f.detect().unwrap_err(), Failure::PackagedDistribution);
}
#[test]
fn packaged_detection_rejects_missing_python_and_invalid_image() {
    let f = Fixture::new();
    std::fs::write(&f.payload.image, b"not an AppImage").unwrap();
    assert_eq!(f.detect().unwrap_err(), Failure::PackagedDistribution);
    std::fs::remove_file(f.payload.python()).unwrap();
    assert_eq!(f.detect().unwrap_err(), Failure::PackagedDistribution);
}
#[test]
fn packaged_detection_rejects_spoofed_executable() {
    let f = Fixture::new();
    assert!(
        Payload::discover(
            |key| match key {
                "AIRPODS_APPIMAGE" => Some("1".into()),
                "APPDIR" => Some(f.payload.root.as_os_str().into()),
                "APPIMAGE" => Some(f.payload.image.as_os_str().into()),
                _ => None,
            },
            &f.payload.python()
        )
        .is_err()
    );
}
#[test]
fn versioned_path_is_stable_and_not_a_mount_path() {
    assert_eq!(
        Payload::stable_image(Path::new("/data/airpods-hr-linux")),
        Path::new("/data/airpods-hr-linux/app/0.1.0/AirPods-HR-0.1.0-x86_64.AppImage")
    );
}
#[tokio::test]
async fn copy_is_atomic_verified_executable_and_idempotent() {
    let f = Fixture::new();
    let (_tx, stop) = watch::channel(false);
    let data = f.root.join("user data");
    let path = f.payload.install(&data, &stop).await.unwrap();
    assert_eq!(
        std::fs::read(&path).unwrap(),
        std::fs::read(&f.payload.image).unwrap()
    );
    let before = std::fs::metadata(&path).unwrap();
    assert_eq!(before.permissions().mode() & 0o777, 0o700);
    f.payload.install(&data, &stop).await.unwrap();
    assert_eq!(std::fs::metadata(&path).unwrap().ino(), before.ino());
    std::fs::write(&f.payload.image, b"changed image bytes").unwrap();
    f.payload.install(&data, &stop).await.unwrap();
    assert_eq!(std::fs::read(&path).unwrap(), b"changed image bytes");
    assert_ne!(std::fs::metadata(&path).unwrap().ino(), before.ino());
}
#[tokio::test]
async fn copy_refuses_destination_and_parent_symlinks() {
    let f = Fixture::new();
    let (_tx, stop) = watch::channel(false);
    let data = f.root.join("data");
    std::os::unix::fs::symlink(&f.payload.root, &data).unwrap();
    assert_eq!(f.payload.install(&data, &stop).await, Err(Failure::Storage));
    std::fs::remove_file(&data).unwrap();
    let path = f.payload.install(&data, &stop).await.unwrap();
    std::fs::remove_file(&path).unwrap();
    std::os::unix::fs::symlink(&f.payload.image, &path).unwrap();
    assert_eq!(f.payload.install(&data, &stop).await, Err(Failure::Storage));
}
#[tokio::test]
async fn interrupted_copy_never_accepts_partial_executable() {
    let f = Fixture::new();
    let data = f.root.join("data");
    let source = std::fs::OpenOptions::new()
        .write(true)
        .open(&f.payload.image)
        .unwrap();
    source.set_len(64 * 1024 * 1024).unwrap();
    let (tx, stop) = watch::channel(false);
    let payload = f.payload.clone();
    let target = data.clone();
    let copy = tokio::spawn(async move { payload.install(&target, &stop).await });
    let stage = data
        .join("app/0.1.0")
        .join(format!(".image-{}", std::process::id()));
    tokio::time::timeout(std::time::Duration::from_secs(5), async {
        while !stage.exists() {
            tokio::time::sleep(std::time::Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap();
    tx.send(true).unwrap();
    assert_eq!(
        tokio::time::timeout(std::time::Duration::from_millis(750), copy)
            .await
            .unwrap()
            .unwrap(),
        Err(Failure::Cancelled)
    );
    assert!(!Payload::stable_image(&data).exists());
    assert!(!stage.exists());
}

#[test]
fn ordinary_directories_cannot_spoof_a_running_appimage() {
    let f = Fixture::new();
    let env: HashMap<&str, OsString> = [
        ("AIRPODS_APPIMAGE", "1".into()),
        ("APPDIR", f.payload.root.as_os_str().into()),
        ("APPIMAGE", f.payload.image.as_os_str().into()),
    ]
    .into();
    assert_eq!(
        Payload::discover(
            |key| env.get(key).cloned(),
            &f.payload.root.join("usr/bin/airpods-desktop")
        )
        .unwrap_err(),
        Failure::PackagedDistribution
    );
}

#[tokio::test]
async fn cancelled_update_keeps_the_previous_verified_image() {
    let f = Fixture::new();
    let data = f.root.join("data");
    let (tx, stop) = watch::channel(false);
    let stable = f.payload.install(&data, &stop).await.unwrap();
    let old = std::fs::read(&stable).unwrap();
    std::fs::write(&f.payload.image, b"new image bytes").unwrap();
    tx.send(true).unwrap();
    assert_eq!(
        f.payload.install(&data, &stop).await,
        Err(Failure::Cancelled)
    );
    assert_eq!(std::fs::read(stable).unwrap(), old);
}
