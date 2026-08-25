use crate::{archive, git};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{collections::BTreeSet, fs, path::Path};

pub const REPEAT_SCOPE: &str = "same source, host, toolchain, and SOURCE_DATE_EPOCH";
pub const TOOLCHAIN_FIELDS: &[&str] = &[
    "python",
    "python_implementation",
    "python_build_frontend",
    "setuptools",
    "wheel",
    "maturin",
    "rustc",
    "cargo",
    "platform",
];
pub const ARTIFACT_SET: &[(&str, &str)] = &[
    ("python-wheel", "airpods-hr-linux"),
    ("python-sdist", "airpods-hr-linux"),
    ("python-wheel", "airpods-client"),
    ("python-sdist", "airpods-client"),
    ("rust-crate", "airpods-client"),
];
fn fields(value: &Value, expected: &[&str], label: &str) -> Result<()> {
    let object = value
        .as_object()
        .with_context(|| format!("{label} must be object"))?;
    ensure!(
        object.keys().map(String::as_str).collect::<BTreeSet<_>>()
            == expected.iter().copied().collect(),
        "{label} fields differ"
    );
    Ok(())
}
fn hex(value: &str, len: usize) -> bool {
    value.len() == len
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn validate(m: &Value) -> Result<()> {
    fields(
        m,
        &[
            "schema_version",
            "release_version",
            "git",
            "source_date_epoch",
            "toolchain",
            "artifacts",
            "repeat_build_check",
            "validation",
        ],
        "manifest top-level",
    )?;
    ensure!(
        m["schema_version"].as_u64() == Some(1),
        "manifest schema version mismatch"
    );
    ensure!(
        m["release_version"] == crate::VERSION,
        "manifest release version mismatch"
    );
    fields(&m["git"], &["commit", "clean"], "git provenance")?;
    ensure!(
        m["git"]["clean"] == true && m["git"]["commit"].as_str().is_some_and(git::valid_commit),
        "manifest git provenance invalid"
    );
    ensure!(
        m["source_date_epoch"].as_u64().is_some_and(|e| e > 0),
        "nonpositive SOURCE_DATE_EPOCH"
    );
    fields(
        &m["validation"],
        &[
            "bluetooth_hardware_used",
            "production_daemon_started",
            "published",
        ],
        "validation",
    )?;
    ensure!(
        m["validation"]
            .as_object()
            .context("validation object")?
            .values()
            .all(|v| v == false),
        "wrong validation booleans"
    );
    for field in TOOLCHAIN_FIELDS {
        ensure!(
            m["toolchain"][field]
                .as_str()
                .is_some_and(|s| !s.is_empty()),
            "missing toolchain fact: {field}"
        );
    }
    let artifacts = m["artifacts"]
        .as_array()
        .context("artifacts must be array")?;
    ensure!(
        artifacts.len() == 5,
        "manifest must describe five artifacts"
    );
    let mut names = BTreeSet::new();
    let mut identities = BTreeSet::new();
    for artifact in artifacts {
        fields(
            artifact,
            &["kind", "package", "version", "filename", "sha256", "size"],
            "artifact",
        )?;
        ensure!(
            artifact["version"] == crate::VERSION,
            "artifact version mismatch"
        );
        let name = artifact["filename"]
            .as_str()
            .context("filename must be string")?;
        ensure!(
            !name.is_empty()
                && name != "."
                && name != ".."
                && !name.contains(['/', '\\', '\0', ':'])
                && names.insert(name),
            "artifact filenames must be unique basenames"
        );
        ensure!(
            ![
                "airpods-client-c",
                "airpods_client_c",
                "airpodsctl",
                "resilient",
                "ffi",
                "xtask"
            ]
            .iter()
            .any(|s| name.to_lowercase().contains(s)),
            "unpublished application artifact"
        );
        ensure!(
            artifact["sha256"].as_str().is_some_and(|s| hex(s, 64))
                && artifact["size"].as_u64().is_some_and(|s| s > 0),
            "invalid artifact hash/size"
        );
        identities.insert((
            artifact["kind"].as_str().context("kind string")?,
            artifact["package"].as_str().context("package string")?,
        ));
    }
    ensure!(
        identities == ARTIFACT_SET.iter().copied().collect(),
        "manifest canonical package/kind set differs"
    );
    let repeat = &m["repeat_build_check"];
    ensure!(
        repeat["source_date_epoch"] == m["source_date_epoch"],
        "repeat SOURCE_DATE_EPOCH mismatch"
    );
    ensure!(
        repeat["artifact_count"].as_u64() == Some(5)
            && repeat["matched_artifact_count"]
                .as_u64()
                .is_some_and(|n| n <= 5),
        "invalid repeated-build counts"
    );
    ensure!(
        repeat["reproducible_build_guarantee"] == false,
        "reproducible_build_guarantee must be false"
    );
    ensure!(
        repeat["scope"].as_str().is_some_and(|s| !s.is_empty()),
        "repeat scope missing"
    );
    let results = repeat["artifacts"]
        .as_array()
        .context("repeat artifacts array")?;
    ensure!(results.len() == 5, "repeat requires five results");
    let mut repeated = BTreeSet::new();
    let mut matched = 0;
    for result in results {
        fields(
            result,
            &["filename", "byte_for_byte_equal"],
            "repeat artifact",
        )?;
        ensure!(
            repeated.insert(
                result["filename"]
                    .as_str()
                    .context("repeat filename string")?
            ),
            "duplicate repeat filename"
        );
        if result["byte_for_byte_equal"]
            .as_bool()
            .context("repeat result boolean")?
        {
            matched += 1;
        }
    }
    ensure!(
        repeated == names,
        "repeat filenames differ from canonical artifact filenames"
    );
    ensure!(
        repeat["matched_artifact_count"] == matched
            && repeat["byte_for_byte_equal"] == (matched == 5),
        "inconsistent repeat observation"
    );
    Ok(())
}
pub fn record(path: &Path, kind: &str, package: &str) -> Result<Value> {
    Ok(
        json!({"kind": kind, "package": package, "version": crate::VERSION,
        "filename": path.file_name().context("artifact basename")?.to_str().context("artifact UTF-8 filename")?,
        "sha256": archive::hash(path)?, "size": fs::metadata(path)?.len()}),
    )
}
pub fn write_json(path: &Path, value: &Value) -> Result<()> {
    fs::write(path, format!("{}\n", serde_json::to_string_pretty(value)?))?;
    Ok(())
}
pub fn summary(path: &Path, m: &Value) -> Result<()> {
    let mut lines = vec![
        format!(
            "AirPods HR Linux release candidate {} (Rust shadow)",
            crate::VERSION
        ),
        format!(
            "Git commit: {}",
            m["git"]["commit"].as_str().context("commit string")?
        ),
        "Git tree clean: yes".into(),
    ];
    for (label, field) in [
        ("Python", "python"),
        ("Rust", "rustc"),
        ("Cargo", "cargo"),
        ("Platform", "platform"),
    ] {
        lines.push(format!(
            "{label}: {}",
            m["toolchain"][field].as_str().context("toolchain string")?
        ));
    }
    lines.push(format!("SOURCE_DATE_EPOCH: {}", m["source_date_epoch"]));
    lines.push(format!(
        "Same-host repeated builds matched: {}/5",
        m["repeat_build_check"]["matched_artifact_count"]
    ));
    lines.push(
        "Reproducible-build guarantee: no; this is one same-host repeated-build observation."
            .into(),
    );
    lines.push("Artifacts:".into());
    for a in m["artifacts"].as_array().context("artifacts array")? {
        lines.push(format!(
            "- {} | {} {} | {} bytes | sha256 {}",
            a["filename"].as_str().context("filename")?,
            a["package"].as_str().context("package")?,
            crate::VERSION,
            a["size"],
            a["sha256"].as_str().context("hash")?
        ));
    }
    let differing: Vec<_> = m["repeat_build_check"]["artifacts"]
        .as_array()
        .context("repeat array")?
        .iter()
        .filter(|r| r["byte_for_byte_equal"] == false)
        .filter_map(|r| r["filename"].as_str())
        .collect();
    if !differing.is_empty() {
        lines.push(format!(
            "Repeated-build byte differences: {}",
            differing.join(", ")
        ));
    }
    lines.push("Validation used no Bluetooth hardware, did not start the production daemon, used no publication credentials, and published nothing.".into());
    fs::write(path, lines.join("\n") + "\n")?;
    Ok(())
}
