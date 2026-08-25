use crate::command;
use anyhow::{Result, ensure};
use std::{fs, path::Path};

pub const STALE: &[&str] = &[
    "Real AirPods validation of the Rust client remains pending",
    "A future Rust client crate should speak to the daemon",
    "future Python / Unity / C# / JS clients",
    "The daemon, SDKs, and IPC layer are future components",
    "The future execution uses",
    "Before a future owner run",
];
pub fn version(value: &str) -> Result<()> {
    ensure!(
        value == crate::VERSION,
        "coordinated release version differs: {value}"
    );
    Ok(())
}
pub fn version_document(text: &str, section: &str) -> Result<()> {
    let manifest: toml::Value = toml::from_str(text)?;
    version(
        manifest
            .get(section)
            .and_then(|v| v.get("version"))
            .and_then(toml::Value::as_str)
            .unwrap_or_default(),
    )
}
pub fn sensitive(path: &str) -> Result<()> {
    let lower = path.to_lowercase();
    let parts: Vec<_> = lower.split('/').collect();
    ensure!(
        !parts.iter().any(|p| [
            "captures",
            "dumps",
            "secrets",
            "credentials",
            "target",
            "dist"
        ]
        .contains(p)),
        "sensitive/generated directory is tracked: {path}"
    );
    let file = parts.last().copied().unwrap_or_default();
    ensure!(
        ![".btsnoop", ".log", ".pcap", ".pcapng", ".pem", ".key"]
            .iter()
            .any(|s| file.ends_with(s)),
        "sensitive/generated file is tracked: {path}"
    );
    ensure!(
        !file.contains("linkkey") && !file.contains("link-key"),
        "credential-like filename: {path}"
    );
    Ok(())
}
pub fn documentation(text: &str) -> Result<()> {
    for marker in ["/home/kenan/", "~/airpods-hr-linux"] {
        ensure!(
            !text.contains(marker),
            "personal checkout path in public documentation"
        );
    }
    for marker in STALE {
        ensure!(
            !text.contains(marker),
            "stale current-state documentation: {marker}"
        );
    }
    Ok(())
}
pub fn validate(root: &Path) -> Result<()> {
    for (file, section) in [
        ("pyproject.toml", "project"),
        ("packages/airpods-client-python/pyproject.toml", "project"),
        ("crates/airpods-client/Cargo.toml", "package"),
    ] {
        version_document(&fs::read_to_string(root.join(file))?, section)?;
    }
    for (file, reference) in [
        (
            "packages/airpods-client-python/README.md",
            "`airpods-client` 0.1",
        ),
        ("crates/airpods-client/README.md", "`airpods-client` 0.1"),
    ] {
        ensure!(
            fs::read_to_string(root.join(file))?.contains(reference),
            "current version reference missing: {file}"
        );
    }
    let tracked = command::output(["git", "ls-files", "-z"], root)?;
    for file in tracked.split('\0').filter(|s| !s.is_empty()) {
        sensitive(file)?;
        if file.ends_with(".md") {
            documentation(&fs::read_to_string(root.join(file))?)?;
        }
    }
    command::output(["git", "diff", "--check"], root)?;
    println!("static policy: PASS");
    Ok(())
}
