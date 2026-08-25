//! Archives are inspected as data. Extraction accepts only safe regular files/directories.
use anyhow::{Context, Result, ensure};
use flate2::read::GzDecoder;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{Read, Write},
    os::unix::fs::PermissionsExt,
    path::Path,
};

const MAX_MEMBER: u64 = 128 * 1024 * 1024;
const MAX_ARCHIVE: usize = 512 * 1024 * 1024;
pub type Members = BTreeMap<String, Option<Vec<u8>>>;

pub fn safe_name(name: &str) -> Result<String> {
    ensure!(
        !name.is_empty()
            && name.len() <= 4096
            && !name.starts_with('/')
            && !name.contains('\\')
            && !name.contains('\0'),
        "unsafe archive path: {name:?}"
    );
    let mut parts = Vec::new();
    for part in name.split('/') {
        ensure!(
            part != ".." && !part.contains(':'),
            "unsafe archive path: {name:?}"
        );
        if !part.is_empty() && part != "." {
            parts.push(part);
        }
    }
    ensure!(!parts.is_empty(), "empty normalized archive path");
    Ok(parts.join("/"))
}

fn bounded_read(reader: impl Read, size: u64) -> Result<Vec<u8>> {
    ensure!(size <= MAX_MEMBER, "archive member exceeds size bound");
    let mut data = Vec::new();
    reader.take(MAX_MEMBER + 1).read_to_end(&mut data)?;
    ensure!(
        data.len() as u64 <= MAX_MEMBER,
        "archive member exceeds size bound"
    );
    Ok(data)
}
fn insert(
    members: &mut Members,
    name: String,
    data: Option<Vec<u8>>,
    total: &mut usize,
) -> Result<()> {
    ensure!(
        members.len() < 100_000,
        "archive exceeds member-count bound"
    );
    *total += data.as_ref().map_or(0, Vec::len);
    ensure!(*total <= MAX_ARCHIVE, "archive exceeds total size bound");
    ensure!(
        members.insert(name.clone(), data).is_none(),
        "duplicate normalized archive member: {name}"
    );
    Ok(())
}

pub fn zip(path: &Path) -> Result<Members> {
    let mut archive = zip::ZipArchive::new(File::open(path)?)?;
    let mut members = Members::new();
    let mut total = 0;
    for index in 0..archive.len() {
        let member = archive.by_index(index)?;
        let name = safe_name(member.name())?;
        let mode = member.unix_mode().unwrap_or(0) & 0o170000;
        ensure!(
            mode == 0 || mode == 0o100000 || mode == 0o040000,
            "unsafe ZIP link/type: {name}"
        );
        let data = if member.is_dir() {
            None
        } else {
            let size = member.size();
            Some(bounded_read(member, size)?)
        };
        insert(&mut members, name, data, &mut total)?;
    }
    Ok(members)
}
pub fn tar(path: &Path, gzip: bool) -> Result<Members> {
    let file = File::open(path)?;
    let reader: Box<dyn Read> = if gzip {
        Box::new(GzDecoder::new(file))
    } else {
        Box::new(file)
    };
    let mut archive = tar::Archive::new(reader);
    let mut members = Members::new();
    let mut total = 0;
    for member in archive.entries()? {
        let mut member = member?;
        let raw = member.path_bytes();
        let name = safe_name(std::str::from_utf8(&raw)?)?;
        let kind = member.header().entry_type();
        if kind.is_pax_global_extensions() {
            ensure!(member.size() <= 65_536, "oversized global PAX metadata");
            for extension in member
                .pax_extensions()?
                .context("missing global PAX metadata")?
            {
                let extension = extension?;
                let key = extension.key()?;
                ensure!(
                    [
                        "comment", "mtime", "atime", "ctime", "uid", "gid", "uname", "gname",
                        "charset"
                    ]
                    .contains(&key),
                    "unsafe global PAX key: {key}"
                );
            }
            // Git's commit comment and archive times/owners are not source members.
            continue;
        }
        ensure!(
            kind.is_file() || kind.is_dir(),
            "unsafe tar link/type: {name}"
        );
        let data = if kind.is_dir() {
            None
        } else {
            let size = member.size();
            Some(bounded_read(member, size)?)
        };
        insert(&mut members, name, data, &mut total)?;
    }
    ensure!(!members.is_empty(), "empty tar archive");
    Ok(members)
}

pub fn extract_tar(path: &Path, destination: &Path, gzip: bool) -> Result<()> {
    fs::create_dir_all(destination)?;
    let destination = fs::canonicalize(destination)?;
    for (name, data) in tar(path, gzip)? {
        let target = destination.join(&name);
        // Newly created trees only, but reject preexisting symlinks as defense in depth.
        let resolved = crate::paths::resolve(&target)?;
        ensure!(
            resolved.starts_with(&destination),
            "extraction escapes destination: {name}"
        );
        if let Some(data) = data {
            fs::create_dir_all(target.parent().context("member parent")?)?;
            File::options()
                .write(true)
                .create_new(true)
                .open(target)?
                .write_all(&data)?;
        } else {
            fs::create_dir_all(target)?;
        }
    }
    // Match Python's tar data filter: clear special/group/other write bits,
    // clear all execute bits when the owner cannot execute, and ensure owner rw.
    let file = File::open(path)?;
    let reader: Box<dyn Read> = if gzip {
        Box::new(GzDecoder::new(file))
    } else {
        Box::new(file)
    };
    for entry in tar::Archive::new(reader).entries()? {
        let entry = entry?;
        if entry.header().entry_type().is_file() {
            let name = safe_name(std::str::from_utf8(&entry.path_bytes())?)?;
            let mut mode = entry.header().mode()? & 0o755;
            if mode & 0o100 == 0 {
                mode &= !0o111;
            }
            mode |= 0o600;
            fs::set_permissions(destination.join(name), fs::Permissions::from_mode(mode))?;
        }
    }
    Ok(())
}

pub fn hash(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0; 65536];
    loop {
        let size = file.read(&mut buffer)?;
        if size == 0 {
            break;
        }
        digest.update(&buffer[..size]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

/// Sorted normalized paths + length framed regular-file bytes; directory entries ignored.
pub fn semantic_sdist_digest(path: &Path) -> Result<String> {
    let mut digest = Sha256::new();
    digest.update(b"airpods-sdist-content-v1\0");
    for (name, data) in tar(path, true)? {
        if let Some(data) = data {
            digest.update((name.len() as u64).to_be_bytes());
            digest.update(name.as_bytes());
            digest.update((data.len() as u64).to_be_bytes());
            digest.update(data);
        }
    }
    Ok(format!("{:x}", digest.finalize()))
}

pub fn policy(name: &str) -> Result<()> {
    member_policy(name, false)
}

fn member_policy(name: &str, packaged_rust_tests: bool) -> Result<()> {
    let lower = name.to_lowercase();
    for marker in [
        "airpods-client-c",
        "airpods_client_c",
        "airpods_client.h",
        "c_ffi",
        "c-probe",
        "/ffi.rs",
    ] {
        ensure!(
            !lower.contains(marker),
            "repository-only C SDK material: {name}"
        );
    }
    let parts: Vec<_> = lower.split('/').collect();
    ensure!(
        !parts.contains(&"xtask")
            && !lower.ends_with(".cargo/config.toml")
            && !parts
                .iter()
                .any(|p| p.starts_with("release-parity") || p.starts_with("release-shadow")),
        "repository-only xtask/release tooling: {name}"
    );
    ensure!(
        !lower.contains("airpodsctl") && !lower.contains("airpods-client-resilient"),
        "independent application material: {name}"
    );
    ensure!(
        !parts.iter().any(|p| [
            "captures",
            "dumps",
            "target",
            "__pycache__",
            "secrets",
            "credentials"
        ]
        .contains(p)
            || (!packaged_rust_tests && *p == "tests")),
        "generated/private material: {name}"
    );
    ensure!(
        !parts.iter().any(|p| [
            ".log", ".pcap", ".pcapng", ".btsnoop", ".pem", ".key", ".pyc"
        ]
        .iter()
        .any(|suffix| p.ends_with(suffix))
            || p.contains("linkkey")
            || p.contains("link-key")),
        "generated/credential-like material: {name}"
    );
    Ok(())
}
pub fn audit_members(members: &Members) -> Result<()> {
    for name in members.keys() {
        policy(name)?;
    }
    Ok(())
}

type Metadata = BTreeMap<String, Vec<String>>;
type Entries = BTreeMap<String, String>;
pub fn wheel_metadata(members: &Members) -> Result<(Metadata, Entries)> {
    let metadata: Vec<_> = members
        .iter()
        .filter(|(name, _)| name.ends_with(".dist-info/METADATA"))
        .collect();
    ensure!(metadata.len() == 1, "expected one METADATA file");
    let mut fields: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let text = std::str::from_utf8(metadata[0].1.as_ref().context("METADATA is not regular")?)?;
    let mut last: Option<String> = None;
    for line in text.lines() {
        if line.is_empty() {
            break;
        }
        if line.starts_with([' ', '\t']) {
            let key = last.as_ref().context("invalid metadata continuation")?;
            fields
                .get_mut(key)
                .context("metadata field")?
                .last_mut()
                .context("metadata value")?
                .push_str(line.trim());
        } else if let Some((key, value)) = line.split_once(':') {
            last = Some(key.to_owned());
            fields
                .entry(key.to_owned())
                .or_default()
                .push(value.trim().to_owned());
        }
    }
    let mut entries = BTreeMap::new();
    if let Some((_, Some(data))) = members
        .iter()
        .find(|(n, _)| n.ends_with(".dist-info/entry_points.txt"))
    {
        let mut console = false;
        for line in std::str::from_utf8(data)?.lines().map(str::trim) {
            if line.starts_with('[') {
                console = line == "[console_scripts]";
            } else if console && let Some((key, value)) = line.split_once('=') {
                entries.insert(key.trim().to_owned(), value.trim().to_owned());
            }
        }
    }
    Ok((fields, entries))
}

pub fn production_wheel(path: &Path) -> Result<()> {
    let members = zip(path)?;
    audit_members(&members)?;
    ensure!(
        members.keys().any(|n| n.starts_with("airpods_hr/")),
        "production wheel lacks airpods_hr"
    );
    ensure!(
        members.contains_key("airpods_hr/service_installer.py"),
        "production wheel lacks service installer"
    );
    ensure!(
        members
            .keys()
            .filter(|n| n.starts_with("airpods_hr/_airpods_aap_core.") && n.ends_with(".so"))
            .count()
            == 1,
        "production wheel must contain exactly one native Rust extension"
    );
    ensure!(
        !members.keys().any(|n| n.starts_with("airpods_client/")),
        "production wheel contains standalone client"
    );
    let (fields, entries) = wheel_metadata(&members)?;
    identity(&members, &fields, "airpods-hr-linux")?;
    let dependencies: std::collections::BTreeSet<_> = fields
        .get("Requires-Dist")
        .into_iter()
        .flatten()
        .map(|v| v.split(';').next().unwrap_or_default().trim())
        .collect();
    ensure!(
        dependencies
            == ["bumble==0.0.234", "dbus-next>=0.2.3"]
                .into_iter()
                .collect(),
        "production dependency metadata mismatch: {dependencies:?}"
    );
    ensure!(
        entries.keys().map(String::as_str).collect::<Vec<_>>()
            == ["airpods-hr", "airpods-hubd", "airpods-hubd-service"],
        "production console entrypoints mismatch"
    );
    Ok(())
}
fn identity(
    members: &Members,
    fields: &BTreeMap<String, Vec<String>>,
    package: &str,
) -> Result<()> {
    for (field, expected) in [
        ("Name", package),
        ("Version", crate::VERSION),
        ("License-Expression", "MIT"),
    ] {
        ensure!(
            fields
                .get(field)
                .and_then(|v| v.first())
                .map(String::as_str)
                == Some(expected),
            "wheel {field} mismatch"
        );
    }
    ensure!(
        members
            .keys()
            .any(|n| n.ends_with(".dist-info/licenses/LICENSE")),
        "wheel lacks packaged license"
    );
    Ok(())
}
pub fn client_wheel(path: &Path) -> Result<()> {
    let members = zip(path)?;
    audit_members(&members)?;
    ensure!(
        members.keys().any(|n| n.starts_with("airpods_client/")),
        "client wheel lacks airpods_client"
    );
    for name in members.keys() {
        let root = name.split('/').next().unwrap_or_default();
        ensure!(
            root == "airpods_client" || root.ends_with(".dist-info"),
            "unexpected client wheel root: {root}"
        );
        ensure!(
            !name.contains("airpods_hr"),
            "client wheel contains daemon material"
        );
    }
    let (fields, entries) = wheel_metadata(&members)?;
    identity(&members, &fields, "airpods-client")?;
    ensure!(
        !fields.contains_key("Requires-Dist") && entries.is_empty(),
        "client wheel dependencies/entrypoints must be empty"
    );
    Ok(())
}
pub const PRODUCTION_SOURCES: &[&str] = &[
    "Cargo.toml",
    "Cargo.lock",
    "pyproject.toml",
    "crates/airpods-aap-core/Cargo.toml",
    "crates/airpods-aap-core/src/lib.rs",
    "crates/airpods-aap-core/src/heart_rate.rs",
    "crates/airpods-aap-core/src/production.rs",
    "crates/airpods-aap-core/src/recovery.rs",
    "crates/airpods-aap-py/Cargo.toml",
    "crates/airpods-aap-py/src/lib.rs",
    "crates/airpods-aap-py/src/app_runtime_policy_bridge.rs",
    "crates/airpods-app-core/Cargo.toml",
    "crates/airpods-app-core/src/lib.rs",
    "crates/airpods-app-core/src/daemon.rs",
    "crates/airpods-app-core/src/production_config.rs",
    "crates/airpods-app-core/src/runner.rs",
    "crates/airpods-app-core/src/monitor.rs",
    "crates/airpods-app-core/src/service.rs",
    "crates/airpods-app-core/src/path_policy.rs",
];
pub fn sdist(path: &Path, package: &str) -> Result<()> {
    let members = tar(path, true)?;
    audit_members(&members)?;
    let production = package == "airpods-hr-linux";
    let required = if production {
        "src/airpods_hr/"
    } else {
        "src/airpods_client/"
    };
    ensure!(
        members.keys().any(|n| n.contains(required)),
        "sdist lacks {required}"
    );
    ensure!(
        members.keys().any(|n| n.ends_with("/LICENSE")),
        "sdist lacks packaged license"
    );
    if production {
        ensure!(
            !members.keys().any(|n| n.contains("/src/airpods_client/")),
            "production sdist contains standalone client"
        );
        let root = members
            .keys()
            .next()
            .context("sdist root")?
            .split('/')
            .next()
            .context("sdist root name")?;
        for source in PRODUCTION_SOURCES {
            ensure!(
                members
                    .get(&format!("{root}/{source}"))
                    .is_some_and(Option::is_some),
                "production sdist lacks {source}"
            );
        }
        ensure!(
            !members
                .keys()
                .any(|n| n.ends_with(".so") || n.ends_with(".pyd")),
            "sdist contains prebuilt native extension"
        );
    } else {
        ensure!(
            !members
                .keys()
                .any(|n| n.contains("airpods_hr") || n.contains("/crates/")),
            "client sdist contains daemon/Rust material"
        );
    }
    Ok(())
}
pub fn rust_crate(path: &Path, root: &Path) -> Result<()> {
    let members = tar(path, true)?;
    // The public crate carries its own tests; repository integration probes
    // are excluded below. Wheels and sdists still exclude all test trees.
    for name in members.keys() {
        member_policy(name, true)?;
    }
    let license = format!("airpods-client-{}/LICENSE", crate::VERSION);
    ensure!(
        members.get(&license).and_then(Option::as_ref) == Some(&fs::read(root.join("LICENSE"))?),
        "Rust crate license differs"
    );
    let (_, data) = members
        .iter()
        .find(|(n, _)| n.ends_with("/Cargo.toml"))
        .context("crate lacks Cargo.toml")?;
    let manifest: toml::Value = toml::from_str(std::str::from_utf8(
        data.as_ref().context("crate manifest not regular")?,
    )?)?;
    ensure!(
        manifest["package"]["license"].as_str() == Some("MIT"),
        "Rust crate license metadata mismatch"
    );
    for name in members.keys() {
        ensure!(
            ![
                "integration_probe.rs",
                "/target/",
                ".py",
                "airpods_hr",
                "airpods-client-python"
            ]
            .iter()
            .any(|m| name.contains(m)),
            "Rust crate contains repository-only material: {name}"
        );
    }
    ensure!(
        members.keys().any(|n| n.ends_with("/src/lib.rs")),
        "Rust crate lacks src/lib.rs"
    );
    Ok(())
}
