//! Validated AppImage payload and atomic, versioned persistent installation.
//! This provider never discovers host Python, invokes pip, or downloads anything.

use crate::bootstrap::Failure;
use sha2::{Digest, Sha256};
use std::ffi::OsString;
use std::fs::File;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Component, Path, PathBuf};
use tokio::io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt};
use tokio::sync::watch;

pub const IDENTITY: &str = "airpods-hr-linux AppImage v1\n0.1.0\n";
const IMAGE_NAME: &str = "AirPods-HR-0.1.0-x86_64.AppImage";
const MAX_IMAGE: u64 = 2 * 1024 * 1024 * 1024;

#[derive(Clone, Debug)]
pub struct Payload {
    pub root: PathBuf,
    pub image: PathBuf,
}

impl Payload {
    pub fn discover(
        env: impl Fn(&str) -> Option<OsString>,
        executable: &Path,
    ) -> Result<Option<Self>, Failure> {
        Self::discover_with_origin(env, executable, mounted_origin)
    }

    fn discover_with_origin(
        env: impl Fn(&str) -> Option<OsString>,
        executable: &Path,
        origin: impl Fn(&Path, &Path) -> bool,
    ) -> Result<Option<Self>, Failure> {
        if env("AIRPODS_APPIMAGE").is_none() {
            return Ok(None);
        }
        if env("AIRPODS_APPIMAGE").as_deref() != Some(std::ffi::OsStr::new("1")) {
            return Err(Failure::PackagedDistribution);
        }
        let root = PathBuf::from(env("APPDIR").ok_or(Failure::PackagedDistribution)?);
        let image = PathBuf::from(env("APPIMAGE").ok_or(Failure::PackagedDistribution)?);
        if !root.is_absolute()
            || !image.is_absolute()
            || std::fs::read_to_string(root.join("airpods-distribution"))
                .ok()
                .as_deref()
                != Some(IDENTITY)
            || executable.canonicalize().ok()
                != root.join("usr/bin/airpods-desktop").canonicalize().ok()
            || executable.canonicalize().is_err()
        {
            return Err(Failure::PackagedDistribution);
        }
        if !origin(&root, &image) {
            return Err(Failure::PackagedDistribution);
        }
        let payload = Self { root, image };
        for path in [
            payload.python(),
            payload.root.join("AppRun"),
            payload.root.join("usr/bin/airpods-desktop"),
        ] {
            let metadata =
                std::fs::symlink_metadata(path).map_err(|_| Failure::PackagedDistribution)?;
            if !metadata.is_file() || metadata.permissions().mode() & 0o111 == 0 {
                return Err(Failure::PackagedDistribution);
            }
        }
        let package = payload
            .root
            .join("usr/lib/airpods-hr-linux/python/lib/python3.14/site-packages/airpods_hr");
        if !package.join("service_installer.py").is_file()
            || !package.join("_hubd/main.py").is_file()
        {
            return Err(Failure::PackagedDistribution);
        }
        let mut file = open_image(&payload.image).map_err(|_| Failure::PackagedDistribution)?;
        let mut header = [0; 11];
        std::io::Read::read_exact(&mut file, &mut header)
            .map_err(|_| Failure::PackagedDistribution)?;
        if &header[..4] != b"\x7fELF" || &header[8..11] != b"AI\x02" {
            return Err(Failure::PackagedDistribution);
        }
        Ok(Some(payload))
    }

    pub fn python(&self) -> PathBuf {
        self.root
            .join("usr/lib/airpods-hr-linux/python/bin/python3.14")
    }

    pub fn stable_image(data: &Path) -> PathBuf {
        data.join("app/0.1.0").join(IMAGE_NAME)
    }

    pub async fn install(
        &self,
        data: &Path,
        stop: &watch::Receiver<bool>,
    ) -> Result<PathBuf, Failure> {
        let stable = Self::stable_image(data);
        let directory = secure_directory(stable.parent().ok_or(Failure::Storage)?)
            .map_err(|_| Failure::Storage)?;
        let source = open_image(&self.image).map_err(|_| Failure::PackagedDistribution)?;
        let source_metadata = source.metadata().map_err(|_| Failure::Storage)?;
        let mut source = tokio::fs::File::from_std(source);
        let source_hash = digest(&mut source, stop).await?;
        source.rewind().await.map_err(|_| Failure::Storage)?;
        match rustix::fs::openat(
            &directory,
            IMAGE_NAME,
            rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::NOFOLLOW | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        ) {
            Ok(fd) => {
                let file = File::from(fd);
                let m = file.metadata().map_err(|_| Failure::Storage)?;
                if !m.is_file() || m.uid() != rustix::process::getuid().as_raw() {
                    return Err(Failure::Storage);
                }
                let mut existing = tokio::fs::File::from_std(file);
                if m.len() == source_metadata.len()
                    && digest(&mut existing, stop).await? == source_hash
                {
                    existing
                        .set_permissions(std::fs::Permissions::from_mode(0o700))
                        .await
                        .map_err(|_| Failure::Storage)?;
                    return Ok(stable);
                }
            }
            Err(rustix::io::Errno::NOENT) => {}
            Err(_) => return Err(Failure::Storage),
        }
        let staging = format!(".image-{}", std::process::id());
        let fd = rustix::fs::openat(
            &directory,
            staging.as_str(),
            rustix::fs::OFlags::WRONLY
                | rustix::fs::OFlags::CREATE
                | rustix::fs::OFlags::EXCL
                | rustix::fs::OFlags::NOFOLLOW
                | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::from_raw_mode(0o700),
        )
        .map_err(|_| Failure::Storage)?;
        let cleanup = Staging {
            directory: &directory,
            name: &staging,
        };
        let mut output = tokio::fs::File::from_std(File::from(fd));
        let mut buffer = vec![0; 65536];
        let mut copied_hash = Sha256::new();
        let mut total = 0u64;
        loop {
            check_stop(stop)?;
            let count = source
                .read(&mut buffer)
                .await
                .map_err(|_| Failure::Storage)?;
            if count == 0 {
                break;
            }
            total += count as u64;
            if total > MAX_IMAGE {
                return Err(Failure::Storage);
            }
            copied_hash.update(&buffer[..count]);
            output
                .write_all(&buffer[..count])
                .await
                .map_err(|_| Failure::Storage)?;
        }
        if total != source_metadata.len() || copied_hash.finalize().as_slice() != source_hash {
            return Err(Failure::PackagedDistribution);
        }
        output.sync_all().await.map_err(|_| Failure::Storage)?;
        check_stop(stop)?;
        // A directory handle and NOFOLLOW protect every destination operation.
        rustix::fs::renameat(&directory, staging.as_str(), &directory, IMAGE_NAME)
            .map_err(|_| Failure::Storage)?;
        rustix::fs::fsync(&directory).map_err(|_| Failure::Storage)?;
        drop(cleanup);
        Ok(stable)
    }
}

// The pinned runtime preserves its mounted directory on fd 1023 across exec.
// Also require a live runtime process whose executable is the original image.
// APPDIR/APPIMAGE strings alone cannot select a file to install as a service.
fn mounted_origin(root: &Path, image: &Path) -> bool {
    let Ok(mount) = std::fs::metadata("/proc/self/fd/1023") else {
        return false;
    };
    let Ok(root_metadata) = std::fs::metadata(root) else {
        return false;
    };
    if mount.dev() != root_metadata.dev() || mount.ino() != root_metadata.ino() {
        return false;
    }
    let Ok(image_metadata) = std::fs::symlink_metadata(image) else {
        return false;
    };
    if !image_metadata.is_file() {
        return false;
    }
    let Ok(processes) = std::fs::read_dir("/proc") else {
        return false;
    };
    processes.flatten().any(|process| {
        process
            .file_name()
            .to_string_lossy()
            .chars()
            .all(|c| c.is_ascii_digit())
            && std::fs::metadata(process.path().join("exe"))
                .is_ok_and(|m| m.dev() == image_metadata.dev() && m.ino() == image_metadata.ino())
    })
}

fn check_stop(stop: &watch::Receiver<bool>) -> Result<(), Failure> {
    if *stop.borrow() || stop.has_changed().is_err() {
        Err(Failure::Cancelled)
    } else {
        Ok(())
    }
}

async fn digest(
    file: &mut tokio::fs::File,
    stop: &watch::Receiver<bool>,
) -> Result<[u8; 32], Failure> {
    let mut hash = Sha256::new();
    let mut buffer = vec![0; 65536];
    let mut total = 0u64;
    loop {
        check_stop(stop)?;
        let count = file.read(&mut buffer).await.map_err(|_| Failure::Storage)?;
        if count == 0 {
            return Ok(hash.finalize().into());
        }
        total += count as u64;
        if total > MAX_IMAGE {
            return Err(Failure::Storage);
        }
        hash.update(&buffer[..count]);
    }
}

fn open_image(path: &Path) -> std::io::Result<File> {
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
        .open(path)?;
    let m = file.metadata()?;
    if !m.is_file() || m.len() > MAX_IMAGE || m.uid() != rustix::process::getuid().as_raw() {
        return Err(std::io::Error::other("invalid AppImage source"));
    }
    Ok(file)
}

fn secure_directory(path: &Path) -> std::io::Result<File> {
    if !path.is_absolute() {
        return Err(std::io::Error::other("absolute storage required"));
    }
    let mut directory = File::from(rustix::fs::open(
        "/",
        rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )?);
    for component in path.components() {
        let Component::Normal(name) = component else {
            if component == Component::RootDir {
                continue;
            }
            return Err(std::io::Error::other("invalid storage component"));
        };
        let flags = rustix::fs::OFlags::RDONLY
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC;
        match rustix::fs::openat(&directory, name, flags, rustix::fs::Mode::empty()) {
            Ok(fd) => directory = File::from(fd),
            Err(rustix::io::Errno::NOENT) => {
                rustix::fs::mkdirat(&directory, name, rustix::fs::Mode::from_raw_mode(0o700))?;
                directory = File::from(rustix::fs::openat(
                    &directory,
                    name,
                    flags,
                    rustix::fs::Mode::empty(),
                )?);
            }
            Err(e) => return Err(e.into()),
        }
    }
    if directory.metadata()?.uid() != rustix::process::getuid().as_raw() {
        return Err(std::io::Error::other("storage must belong to this user"));
    }
    rustix::fs::fchmod(&directory, rustix::fs::Mode::from_raw_mode(0o700))?;
    Ok(directory)
}

struct Staging<'a> {
    directory: &'a File,
    name: &'a str,
}
impl Drop for Staging<'_> {
    fn drop(&mut self) {
        let _ = rustix::fs::unlinkat(self.directory, self.name, rustix::fs::AtFlags::empty());
    }
}

#[cfg(test)]
pub(crate) mod tests;
