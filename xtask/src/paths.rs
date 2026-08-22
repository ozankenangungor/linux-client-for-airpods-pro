use anyhow::{Context, Result, bail};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Component, Path, PathBuf},
};

/// Resolve existing symlinks before processing subsequent `..`, including missing leaves.
pub fn resolve(path: &Path) -> Result<PathBuf> {
    let path = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut resolved = PathBuf::new();
    for part in path.components() {
        match part {
            Component::RootDir => resolved.push("/"),
            Component::CurDir => (),
            Component::ParentDir => {
                resolved.pop();
            }
            Component::Normal(name) => {
                resolved.push(name);
                match fs::symlink_metadata(&resolved) {
                    Ok(_) => {
                        resolved = fs::canonicalize(&resolved)
                            .with_context(|| format!("resolve {}", resolved.display()))?;
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
                    Err(e) => return Err(e.into()),
                }
            }
            Component::Prefix(_) => bail!("unsupported path prefix"),
        }
    }
    Ok(resolved)
}

pub fn output_dir(path: &Path, repo: &Path) -> Result<PathBuf> {
    let path = if path.starts_with("~") {
        PathBuf::from(std::env::var_os("HOME").context("HOME missing for ~ expansion")?)
            .join(path.strip_prefix("~")?)
    } else {
        path.to_owned()
    };
    let resolved = resolve(&path)?;
    if resolved.starts_with(resolve(repo)?) {
        bail!("release output must be outside the repository");
    }
    if resolved.exists() && (!resolved.is_dir() || fs::read_dir(&resolved)?.next().is_some()) {
        bail!(
            "output directory must be absent or empty: {}",
            resolved.display()
        );
    }
    Ok(resolved)
}

pub fn executable(path: &Path) -> bool {
    fs::metadata(path).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

pub fn which(name: &std::ffi::OsStr) -> Option<PathBuf> {
    std::env::split_paths(&std::env::var_os("PATH")?)
        .map(|dir| dir.join(name))
        .find(|path| executable(path))
}
