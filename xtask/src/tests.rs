use crate::{archive, command, env, git, manifest, paths, static_policy};
use serde_json::{Value, json};
use std::{
    ffi::OsString,
    fs,
    io::Write,
    path::Path,
    time::{Duration, Instant},
};

fn fixture_manifest() -> Value {
    let artifacts: Vec<_> = manifest::ARTIFACT_SET
        .iter()
        .zip([
            "production.whl",
            "production.tar.gz",
            "client.whl",
            "client.tar.gz",
            "client.crate",
        ])
        .map(|((kind, package), name)| {
            json!({"kind": kind, "package": package, "version": crate::VERSION,
        "filename": name, "sha256": "a".repeat(64), "size": 1})
        })
        .collect();
    let repeated: Vec<_> = artifacts
        .iter()
        .map(|a| json!({"filename": a["filename"], "byte_for_byte_equal": true}))
        .collect();
    json!({"schema_version": 1, "release_version": crate::VERSION,
        "git": {"commit": "a".repeat(40), "clean": true}, "source_date_epoch": 1,
        "toolchain": {"python": "3.14.7", "python_implementation": "CPython", "python_build_frontend": "build 1.3.0",
            "setuptools": "84.0.0", "wheel": "0.48.0", "maturin": "1.15.0", "rustc": "rustc test", "cargo": "cargo test", "platform": "Linux-test"},
        "artifacts": artifacts,
        "repeat_build_check": {"source_date_epoch": 1, "artifact_count": 5, "matched_artifact_count": 5,
            "byte_for_byte_equal": true, "artifacts": repeated, "scope": manifest::REPEAT_SCOPE, "reproducible_build_guarantee": false},
        "validation": {"bluetooth_hardware_used": false, "production_daemon_started": false, "published": false}})
}

// Use this test executable as the deterministic child; no shell, hardware or network.
#[test]
fn runner_child() {
    match std::env::var("XTASK_TEST_CHILD_ACTION").as_deref() {
        Ok("wait") => loop {
            std::thread::park();
        },
        Ok("fail") => panic!("synthetic child failure diagnostic"),
        Ok("print") => {
            println!("argv: {:?}", std::env::args_os().collect::<Vec<_>>());
            println!(
                "literal: {}",
                std::env::var("XTASK_TEST_CHILD_PAYLOAD").unwrap_or_default()
            );
        }
        Ok("pipes") => {
            let data = "x".repeat(100_000);
            print!("{data}");
            eprint!("{data}");
        }
        Ok("env") => {
            for key in env::CREDENTIALS {
                assert!(std::env::var_os(key).is_none());
            }
        }
        _ => (),
    }
}
fn child(action: &str, timeout: Duration, extra: &[(&str, &str)]) -> anyhow::Result<String> {
    let temp = tempfile::tempdir()?;
    let mut environment = env::clean();
    environment.insert("XTASK_TEST_CHILD_ACTION".into(), action.into());
    for (key, value) in extra {
        environment.insert((*key).into(), (*value).into());
    }
    command::run(
        [
            std::env::current_exe()?.as_os_str(),
            "--exact".as_ref(),
            "tests::runner_child".as_ref(),
            "--nocapture".as_ref(),
            "--skip".as_ref(),
            extra
                .iter()
                .find(|(key, _)| *key == "XTASK_TEST_CHILD_PAYLOAD")
                .map_or("unused-filter", |(_, value)| *value)
                .as_ref(),
        ],
        temp.path(),
        &environment,
        timeout,
        true,
    )
}
#[test]
fn command_success() {
    child("print", Duration::from_secs(5), &[]).unwrap();
}
#[test]
fn command_nonzero_has_diagnostic() {
    let error = child("fail", Duration::from_secs(5), &[])
        .unwrap_err()
        .to_string();
    assert!(
        error.contains("command exited") && error.contains("synthetic child failure diagnostic")
    );
}
#[test]
fn command_captures_stdout() {
    assert!(
        child(
            "print",
            Duration::from_secs(5),
            &[("XTASK_TEST_CHILD_PAYLOAD", "captured text")]
        )
        .unwrap()
        .contains("captured text")
    );
}
#[test]
fn command_large_dual_pipes_do_not_deadlock() {
    assert!(child("pipes", Duration::from_secs(5), &[]).unwrap().len() > 100_000);
}
#[test]
fn command_timeout_is_finite() {
    let start = Instant::now();
    let error = child("wait", Duration::from_millis(100), &[])
        .unwrap_err()
        .to_string();
    assert!(error.contains("timed out"));
    assert!(start.elapsed() < Duration::from_secs(3));
}
#[test]
fn command_has_no_shell_expansion() {
    let payload = "$(touch sentinel); `exit 7` $HOME *";
    assert!(
        child(
            "print",
            Duration::from_secs(5),
            &[("XTASK_TEST_CHILD_PAYLOAD", payload)]
        )
        .unwrap()
        .contains(payload)
    );
}
#[test]
fn runner_strips_credentials_even_when_supplied() {
    let pairs: Vec<_> = env::CREDENTIALS
        .iter()
        .map(|key| (*key, "synthetic-secret"))
        .collect();
    child("env", Duration::from_secs(5), &pairs).unwrap();
}
#[test]
fn synthetic_environment_sanitization() {
    let mut map = env::Environment::new();
    for key in env::CREDENTIALS {
        map.insert((*key).into(), "synthetic".into());
    }
    map.insert("CARGO_REGISTRIES_PRIVATE_TOKEN".into(), "synthetic".into());
    map.insert("KEEP".into(), "public".into());
    let clean = env::sanitize(map);
    assert_eq!(clean.len(), 1);
    assert_eq!(clean[&OsString::from("KEEP")], "public");
}

fn path_fixture() -> (tempfile::TempDir, std::path::PathBuf) {
    let temp = tempfile::tempdir().unwrap();
    let repo = temp.path().join("repo");
    fs::create_dir(&repo).unwrap();
    (temp, repo)
}
#[test]
fn output_outside_repo() {
    let (t, r) = path_fixture();
    assert!(paths::output_dir(&t.path().join("release"), &r).is_ok());
}
#[test]
fn output_repo_root_rejected() {
    let (_t, r) = path_fixture();
    assert!(paths::output_dir(&r, &r).is_err());
}
#[test]
fn output_repo_child_rejected() {
    let (_t, r) = path_fixture();
    assert!(paths::output_dir(&r.join("release"), &r).is_err());
}
#[test]
fn output_parent_normalization_rejected() {
    let (t, r) = path_fixture();
    assert!(paths::output_dir(&t.path().join("missing/../repo/release"), &r).is_err());
}
#[test]
fn output_symlink_into_repo_rejected() {
    let (t, r) = path_fixture();
    let link = t.path().join("outside");
    std::os::unix::fs::symlink(&r, &link).unwrap();
    assert!(paths::output_dir(&link.join("release"), &r).is_err());
}
#[test]
fn output_symlink_parent_resolved_before_dotdot() {
    let (t, r) = path_fixture();
    fs::create_dir(r.join("child")).unwrap();
    let link = t.path().join("outside");
    std::os::unix::fs::symlink(r.join("child"), &link).unwrap();
    assert!(paths::output_dir(&link.join("../release"), &r).is_err());
}
#[test]
fn nonempty_output_rejected() {
    let (t, r) = path_fixture();
    let out = t.path().join("out");
    fs::create_dir(&out).unwrap();
    fs::write(out.join("sentinel"), b"x").unwrap();
    assert!(paths::output_dir(&out, &r).is_err());
}
#[test]
fn empty_output_accepted() {
    let (t, r) = path_fixture();
    let out = t.path().join("out");
    fs::create_dir(&out).unwrap();
    assert!(paths::output_dir(&out, &r).is_ok());
}
#[test]
fn dangling_symlink_output_rejected() {
    let (t, r) = path_fixture();
    let out = t.path().join("out");
    std::os::unix::fs::symlink(r.join("missing"), &out).unwrap();
    assert!(paths::output_dir(&out, &r).is_err());
}

#[test]
fn static_version_mismatch() {
    assert!(static_policy::version("0.2.0").is_err());
}
#[test]
fn static_version_documents() {
    for section in ["project", "package"] {
        static_policy::version_document(
            &format!("[{section}]\nname='fixture'\nversion='0.1.0'\n"),
            section,
        )
        .unwrap();
        assert!(
            static_policy::version_document(&format!("[{section}]\nversion='0.2.0'\n"), section)
                .is_err()
        );
        assert!(
            static_policy::version_document(&format!("[{section}]\nname='fixture'\n"), section)
                .is_err()
        );
    }
}
#[test]
fn static_sensitive_paths() {
    for p in [
        "captures/file",
        "dump.PCAP",
        "keys/LinkKey.json",
        "target/a",
        "secret.pem",
    ] {
        assert!(static_policy::sensitive(p).is_err(), "{p}");
    }
}
#[test]
fn static_private_docs_paths() {
    for text in [
        "checkout /home/kenan/project",
        "checkout ~/airpods-hr-linux",
    ] {
        assert!(static_policy::documentation(text).is_err());
    }
}
#[test]
fn static_stale_docs() {
    for marker in static_policy::STALE {
        assert!(static_policy::documentation(marker).is_err());
    }
}
#[test]
fn static_safe_examples() {
    static_policy::version(crate::VERSION).unwrap();
    static_policy::sensitive("xtask/src/paths.rs").unwrap();
    static_policy::documentation("cargo xtask release parity; Python remains canonical").unwrap();
}
#[test]
fn commit_sha_policy() {
    assert!(git::valid_commit(&"a".repeat(40)));
    assert!(!git::valid_commit(&"A".repeat(40)));
    assert!(!git::valid_commit(&"g".repeat(40)));
}
#[test]
fn committed_source_export_handles_git_pax_metadata() {
    let t = tempfile::tempdir().unwrap();
    let repo = t.path().join("repo");
    fs::create_dir(&repo).unwrap();
    command::output(["git", "init", "--quiet"], &repo).unwrap();
    fs::write(repo.join("committed.txt"), "committed bytes\n").unwrap();
    command::output(["git", "add", "committed.txt"], &repo).unwrap();
    command::output(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        &repo,
    )
    .unwrap();
    let commit = git::clean_commit(&repo).unwrap();
    let destination = t.path().join("export");
    git::export(&repo, &commit, &destination).unwrap();
    assert_eq!(
        fs::read(destination.join("committed.txt")).unwrap(),
        b"committed bytes\n"
    );
    assert!(!destination.join(".git").exists());
    assert!(!destination.join("pax_global_header").exists());
    assert!(git::unchanged(&repo, &"b".repeat(40)).is_err());
    fs::write(repo.join("untracked.txt"), b"dirty fixture").unwrap();
    assert!(git::clean_commit(&repo).is_err());
}

#[test]
fn manifest_valid() {
    manifest::validate(&fixture_manifest()).unwrap();
}
macro_rules! bad_manifest {
    ($name:ident, $value:ident, $change:block) => { #[test] fn $name() { let mut $value = fixture_manifest(); $change assert!(manifest::validate(&$value).is_err()); } };
}
bad_manifest!(manifest_missing_field, m, {
    m.as_object_mut().unwrap().remove("source_date_epoch");
});
bad_manifest!(manifest_extra_field, m, {
    m["extra"] = json!(1);
});
bad_manifest!(manifest_duplicate_filename, m, {
    m["artifacts"][1]["filename"] = m["artifacts"][0]["filename"].clone();
});
bad_manifest!(manifest_private_filename, m, {
    m["artifacts"][0]["filename"] = json!("/private/production.whl");
});
bad_manifest!(manifest_relative_filename, m, {
    m["artifacts"][0]["filename"] = json!("../production.whl");
});
bad_manifest!(manifest_sixth_c_artifact, m, {
    let mut extra = m["artifacts"][4].clone();
    extra["package"] = json!("airpods-client-c");
    m["artifacts"].as_array_mut().unwrap().push(extra);
});
bad_manifest!(manifest_c_artifact_replacement, m, {
    m["artifacts"][4]["package"] = json!("airpods-client-c");
});
bad_manifest!(manifest_ffi_filename, m, {
    m["artifacts"][4]["filename"] = json!("ffi.crate");
});
bad_manifest!(manifest_xtask_artifact, m, {
    m["artifacts"][4]["package"] = json!("xtask");
});
bad_manifest!(manifest_xtask_filename, m, {
    m["artifacts"][4]["filename"] = json!("xtask.crate");
});
bad_manifest!(manifest_validation_boolean, m, {
    m["validation"]["published"] = json!(true);
});
bad_manifest!(manifest_repeat_names, m, {
    m["repeat_build_check"]["artifacts"][0]["filename"] = json!("other.whl");
});
bad_manifest!(manifest_reproducibility_claim, m, {
    m["repeat_build_check"]["reproducible_build_guarantee"] = json!(true);
});
bad_manifest!(manifest_dirty_git, m, {
    m["git"]["clean"] = json!(false);
});
bad_manifest!(manifest_bad_commit, m, {
    m["git"]["commit"] = json!("z".repeat(40));
});
bad_manifest!(manifest_bad_hash, m, {
    m["artifacts"][0]["sha256"] = json!("z".repeat(64));
});
bad_manifest!(manifest_zero_size, m, {
    m["artifacts"][0]["size"] = json!(0);
});
bad_manifest!(manifest_nonpositive_epoch, m, {
    m["source_date_epoch"] = json!(0);
});
bad_manifest!(manifest_bad_repeat_count, m, {
    m["repeat_build_check"]["matched_artifact_count"] = json!(6);
});
bad_manifest!(manifest_inconsistent_repeat_count, m, {
    m["repeat_build_check"]["matched_artifact_count"] = json!(3);
});
bad_manifest!(manifest_wrong_version, m, {
    m["release_version"] = json!("0.2.0");
});
bad_manifest!(manifest_wrong_schema, m, {
    m["schema_version"] = json!(2);
});

fn write_zip(path: &Path, members: &[(&str, &[u8])]) {
    let mut archive = zip::ZipWriter::new(fs::File::create(path).unwrap());
    for (name, data) in members {
        archive
            .start_file(*name, zip::write::SimpleFileOptions::default())
            .unwrap();
        archive.write_all(data).unwrap();
    }
    archive.finish().unwrap();
}
fn write_tar(path: &Path, members: &[(&str, &[u8])], timestamp: u32) {
    let encoder = flate2::GzBuilder::new().mtime(timestamp).write(
        fs::File::create(path).unwrap(),
        flate2::Compression::default(),
    );
    let mut archive = tar::Builder::new(encoder);
    for (name, data) in members {
        let mut header = tar::Header::new_gnu();
        header.set_size(data.len() as u64);
        header.set_mode(0o644);
        header.set_mtime(timestamp.into());
        header.set_uid(timestamp.into());
        // Raw name allows malicious traversal fixtures that Builder's normal API rejects.
        let bytes = header.as_mut_bytes();
        bytes[..100].fill(0);
        bytes[..name.len()].copy_from_slice(name.as_bytes());
        header.set_cksum();
        archive.append(&header, *data).unwrap();
    }
    archive.into_inner().unwrap().finish().unwrap();
}
#[test]
fn zip_traversal_rejected() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path().join("x.whl");
    for name in [
        "../escape",
        "/absolute",
        "a/../../escape",
        "C:/escape",
        "a\\..\\escape",
    ] {
        write_zip(&p, &[(name, b"x")]);
        assert!(archive::zip(&p).is_err(), "{name}");
    }
}
#[test]
fn tar_traversal_rejected() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path().join("x.tar.gz");
    for name in ["../escape", "/absolute", "a/../../escape"] {
        write_tar(&p, &[(name, b"x")], 1);
        assert!(archive::tar(&p, true).is_err(), "{name}");
    }
}
#[test]
fn tar_symlink_rejected() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path().join("link.tar");
    let mut builder = tar::Builder::new(fs::File::create(&p).unwrap());
    let mut header = tar::Header::new_gnu();
    header.set_entry_type(tar::EntryType::Symlink);
    header.set_size(0);
    header.set_mode(0o777);
    header.set_cksum();
    builder
        .append_link(&mut header, "link", "../escape")
        .unwrap();
    builder.finish().unwrap();
    assert!(archive::tar(&p, false).is_err());
}
#[test]
fn archive_c_sdk_contamination() {
    for name in [
        "crates/airpods-client-c/src/lib.rs",
        "include/airpods_client.h",
        "libairpods_client_c.so",
        "libairpods_client_c.a",
        "tests/c_ffi_probe.c",
        "debug/c-probe",
        "src/ffi.rs",
    ] {
        assert!(archive::policy(name).is_err(), "{name}");
    }
}
#[test]
fn archive_xtask_contamination() {
    for name in [
        "root/xtask/src/main.rs",
        "root/.cargo/config.toml",
        "root/release-shadow-debug.txt",
    ] {
        assert!(archive::policy(name).is_err(), "{name}");
    }
}
#[test]
fn archive_generated_contamination() {
    for name in [
        "root/tests/x.py",
        "root/target/x",
        "root/captures/x",
        "root/__pycache__/x",
        "root/a.log",
        "root/LinkKey.json",
    ] {
        assert!(archive::policy(name).is_err(), "{name}");
    }
}
#[test]
fn wheel_metadata_extraction() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path().join("x.whl");
    write_zip(&p, &[("p.dist-info/METADATA", b"Name: package\nVersion: 0.1.0\nRequires-Dist: first\nRequires-Dist: second\n\nbody\n"),
        ("p.dist-info/entry_points.txt", b"[console_scripts]\nthing = package:main\n[other]\nignored = x\n")]);
    let (metadata, entries) = archive::wheel_metadata(&archive::zip(&p).unwrap()).unwrap();
    assert_eq!(metadata["Name"], ["package"]);
    assert_eq!(metadata["Requires-Dist"], ["first", "second"]);
    assert_eq!(entries.len(), 1);
    assert_eq!(entries["thing"], "package:main");
}
#[test]
fn sdist_semantic_digest_ignores_metadata() {
    let t = tempfile::tempdir().unwrap();
    let a = t.path().join("a.tar.gz");
    let b = t.path().join("b.tar.gz");
    write_tar(&a, &[("root/a", b"one"), ("root/b", b"two")], 1);
    write_tar(&b, &[("root/b", b"two"), ("root/./a", b"one")], 2);
    assert_ne!(archive::hash(&a).unwrap(), archive::hash(&b).unwrap());
    assert_eq!(
        archive::semantic_sdist_digest(&a).unwrap(),
        archive::semantic_sdist_digest(&b).unwrap()
    );
}
#[test]
fn sdist_semantic_digest_tracks_content() {
    let t = tempfile::tempdir().unwrap();
    let a = t.path().join("a.tar.gz");
    let b = t.path().join("b.tar.gz");
    write_tar(&a, &[("root/a", b"one")], 1);
    write_tar(&b, &[("root/a", b"changed")], 1);
    assert_ne!(
        archive::semantic_sdist_digest(&a).unwrap(),
        archive::semantic_sdist_digest(&b).unwrap()
    );
}
#[test]
fn extraction_cannot_follow_existing_symlink() {
    let t = tempfile::tempdir().unwrap();
    let out = t.path().join("out");
    let elsewhere = t.path().join("elsewhere");
    fs::create_dir(&out).unwrap();
    fs::create_dir(&elsewhere).unwrap();
    std::os::unix::fs::symlink(&elsewhere, out.join("link")).unwrap();
    let p = t.path().join("x.tar.gz");
    write_tar(&p, &[("link/file", b"x")], 1);
    assert!(archive::extract_tar(&p, &out, true).is_err());
    assert!(!elsewhere.join("file").exists());
}
#[test]
fn source_export_matches_data_filter_file_permissions() {
    use std::os::unix::fs::PermissionsExt;
    let t = tempfile::tempdir().unwrap();
    let path = t.path().join("modes.tar");
    let mut builder = tar::Builder::new(fs::File::create(&path).unwrap());
    for (name, mode) in [
        ("data", 0o664),
        ("non_owner_exec", 0o665),
        ("executable", 0o775),
    ] {
        let mut header = tar::Header::new_gnu();
        header.set_size(1);
        header.set_mode(mode);
        header.set_cksum();
        builder
            .append_data(&mut header, name, b"x".as_slice())
            .unwrap();
    }
    builder.finish().unwrap();
    let out = t.path().join("out");
    archive::extract_tar(&path, &out, false).unwrap();
    for (name, expected) in [
        ("data", 0o644),
        ("non_owner_exec", 0o644),
        ("executable", 0o755),
    ] {
        assert_eq!(
            fs::metadata(out.join(name)).unwrap().permissions().mode() & 0o777,
            expected
        );
    }
}
#[test]
fn production_sdist_requires_full_build_sources() {
    let t = tempfile::tempdir().unwrap();
    let p = t.path().join("p.tar.gz");
    let mut names = vec![
        "root/LICENSE".to_owned(),
        "root/src/airpods_hr/__init__.py".to_owned(),
    ];
    names.extend(
        archive::PRODUCTION_SOURCES
            .iter()
            .map(|s| format!("root/{s}")),
    );
    let all: Vec<_> = names
        .iter()
        .map(|s| (s.as_str(), b"fixture".as_slice()))
        .collect();
    write_tar(&p, &all, 1);
    archive::sdist(&p, "airpods-hr-linux").unwrap();
    for missing in archive::PRODUCTION_SOURCES {
        let subset: Vec<_> = all
            .iter()
            .copied()
            .filter(|(s, _)| *s != format!("root/{missing}"))
            .collect();
        write_tar(&p, &subset, 1);
        assert!(archive::sdist(&p, "airpods-hr-linux").is_err(), "{missing}");
    }
}
#[test]
fn rust_crate_manifest_document_audit() {
    let t = tempfile::tempdir().unwrap();
    fs::write(t.path().join("LICENSE"), b"fixture license").unwrap();
    let path = t.path().join("fixture.crate");
    write_tar(
        &path,
        &[
            ("airpods-client-0.1.0/LICENSE", b"fixture license"),
            (
                "airpods-client-0.1.0/tests/client.rs",
                b"// public crate contract tests",
            ),
            (
                "airpods-client-0.1.0/src/lib.rs",
                b"pub struct AirPodsClient;",
            ),
            (
                "airpods-client-0.1.0/Cargo.toml",
                b"[package]\nname='airpods-client'\nversion='0.1.0'\nlicense='MIT'\n",
            ),
        ],
        1,
    );
    archive::rust_crate(&path, t.path()).unwrap();
}
#[test]
fn all_audits_reject_xtask_and_c_sdk_first() {
    let t = tempfile::tempdir().unwrap();
    for name in ["xtask/src/main.rs", "crates/airpods-client-c/src/lib.rs"] {
        let wheel = t.path().join("x.whl");
        write_zip(&wheel, &[(name, b"x")]);
        assert!(archive::production_wheel(&wheel).is_err());
        assert!(archive::client_wheel(&wheel).is_err());
        let tar = t.path().join("x.tar.gz");
        write_tar(&tar, &[(name, b"x")], 1);
        assert!(archive::sdist(&tar, "airpods-hr-linux").is_err());
        assert!(archive::sdist(&tar, "airpods-client").is_err());
        assert!(archive::rust_crate(&tar, t.path()).is_err());
    }
}

