use crate::{archive, command};
use anyhow::{Result, bail, ensure};
use std::{
    fs,
    path::{Path, PathBuf},
};

pub fn root() -> Result<PathBuf> {
    Ok(PathBuf::from(command::output(
        ["git", "rev-parse", "--show-toplevel"],
        &std::env::current_dir()?,
    )?))
}
pub fn valid_commit(value: &str) -> bool {
    value.len() == 40
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn clean_commit(root: &Path) -> Result<String> {
    ensure!(
        command::output(["git", "status", "--porcelain=v1"], root)?.is_empty(),
        "release validation requires a clean git tree"
    );
    let commit = command::output(["git", "rev-parse", "HEAD"], root)?;
    ensure!(valid_commit(&commit), "unexpected git commit: {commit:?}");
    Ok(commit)
}
pub fn unchanged(root: &Path, commit: &str) -> Result<()> {
    ensure!(
        clean_commit(root)? == commit,
        "HEAD changed during release validation"
    );
    Ok(())
}
pub fn epoch(root: &Path) -> Result<u64> {
    let value: u64 =
        command::output(["git", "show", "-s", "--format=%ct", "HEAD"], root)?.parse()?;
    ensure!(value > 0, "commit timestamp must be positive");
    Ok(value)
}
pub fn export(root: &Path, commit: &str, destination: &Path) -> Result<()> {
    unchanged(root, commit)?;
    fs::create_dir_all(destination)?;
    let tar = destination.with_extension("tar");
    command::output(
        [
            std::ffi::OsStr::new("git"),
            "archive".as_ref(),
            "--format=tar".as_ref(),
            "--output".as_ref(),
            tar.as_os_str(),
            commit.as_ref(),
        ],
        root,
    )?;
    archive::extract_tar(&tar, destination, false)?;
    fs::remove_file(tar)?;
    if destination.join(".git").exists() {
        bail!("source export contains .git");
    }
    Ok(())
}
