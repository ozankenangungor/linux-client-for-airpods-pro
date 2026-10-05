//! The only module permitted to launch/load the pinned Python oracle.
use crate::{
    archive,
    cli::Scope,
    command::{self, COMMAND_TIMEOUT},
    env, git, manifest, paths,
    python::Python,
};
use anyhow::{Context, Result, ensure};
use nix::fcntl::{RenameFlags, renameat2};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs,
    io::ErrorKind,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    time::Duration,
};

const ORACLE_BLOB: &str = "195292950d9698a1f4b3f6bd5329b315a4168eb2";
const VALIDATOR_STAGE: &str = ".validator-stage";

#[derive(Clone, Copy)]
pub(crate) enum Phase {
    Oracle,
    Shadow,
}

impl Phase {
    fn destination(self) -> &'static str {
        match self {
            Self::Oracle => "python-oracle",
            Self::Shadow => "rust-shadow",
        }
    }
}

fn absent(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error).with_context(|| format!("inspect {}", path.display())),
        Ok(_) => anyhow::bail!("parity path must be absent: {}", path.display()),
    }
}

/// One fixed build-root identity, reused only after a successful directory rename.
/// The second validator creates its own source exports and artifacts from scratch.
pub(crate) struct ValidatorStage {
    root: PathBuf,
}

impl ValidatorStage {
    pub(crate) fn new(root: &Path) -> Result<Self> {
        let stage = Self { root: root.into() };
        stage.directory()?;
        Ok(stage)
    }

    fn directory(&self) -> Result<fs::File> {
        ensure!(
            fs::symlink_metadata(&self.root)?.is_dir() && paths::resolve(&self.root)? == self.root,
            "parity root must remain a resolved directory: {}",
            self.root.display()
        );
        // Anchor the atomic rename to this parent; never follow a substituted root
        // symlink or use a caller-controlled child name.
        Ok(fs::OpenOptions::new()
            .read(true)
            .custom_flags(nix::libc::O_DIRECTORY | nix::libc::O_NOFOLLOW)
            .open(&self.root)?)
    }

    pub(crate) fn prepare(&self, phase: Phase) -> Result<PathBuf> {
        self.directory()?;
        let stage = self.root.join(VALIDATOR_STAGE);
        absent(&stage)?;
        absent(&self.root.join(phase.destination()))?;
        Ok(stage)
    }

    pub(crate) fn finish(&self, phase: Phase) -> Result<PathBuf> {
        let directory = self.directory()?;
        let stage = self.root.join(VALIDATOR_STAGE);
        ensure!(
            fs::symlink_metadata(&stage)?.is_dir() && paths::resolve(&stage)? == stage,
            "validator stage must be a real direct-child directory"
        );
        let destination = self.root.join(phase.destination());
        absent(&destination)?;
        rename_stage(&directory, phase)?;
        absent(&stage)?;
        Ok(destination)
    }
}

pub(crate) fn rename_stage(directory: &fs::File, phase: Phase) -> Result<()> {
    // NOREPLACE rejects even a destination created after the absence check. A
    // failed rename is fatal; there is deliberately no copy fallback.
    renameat2(
        directory,
        VALIDATOR_STAGE,
        directory,
        phase.destination(),
        RenameFlags::RENAME_NOREPLACE,
    )
    .with_context(|| format!("rename {VALIDATOR_STAGE} to {}", phase.destination()))
}

fn equal(field: &str, oracle: &Value, shadow: &Value) -> Result<()> {
    ensure!(
        oracle == shadow,
        "parity mismatch {field}: oracle={oracle} shadow={shadow}"
    );
    Ok(())
}
pub fn compare(
    oracle: &Value,
    shadow: &Value,
    oracle_dir: &Path,
    shadow_dir: &Path,
) -> Result<Value> {
    manifest::validate(oracle)?;
    manifest::validate(shadow)?;
    for field in [
        "schema_version",
        "release_version",
        "source_date_epoch",
        "validation",
    ] {
        equal(field, &oracle[field], &shadow[field])?;
    }
    for field in ["commit", "clean"] {
        equal(
            &format!("git.{field}"),
            &oracle["git"][field],
            &shadow["git"][field],
        )?;
    }
    for field in manifest::TOOLCHAIN_FIELDS {
        equal(
            &format!("toolchain.{field}"),
            &oracle["toolchain"][field],
            &shadow["toolchain"][field],
        )?;
    }
    for field in ["artifact_count", "scope", "reproducible_build_guarantee"] {
        equal(
            &format!("repeat_build_check.{field}"),
            &oracle["repeat_build_check"][field],
            &shadow["repeat_build_check"][field],
        )?;
    }
    let artifacts = |m: &Value| -> Result<BTreeMap<String, Value>> {
        Ok(m["artifacts"]
            .as_array()
            .context("artifacts")?
            .iter()
            .map(|a| {
                (
                    a["filename"].as_str().unwrap_or_default().to_owned(),
                    a.clone(),
                )
            })
            .collect())
    };
    let expected = artifacts(oracle)?;
    let actual = artifacts(shadow)?;
    ensure!(
        expected.keys().eq(actual.keys()),
        "parity artifact filename set differs: oracle={:?} shadow={:?}",
        expected.keys(),
        actual.keys()
    );
    let mut hashes = BTreeMap::new();
    let mut sdists = BTreeMap::new();
    let mut digest_pairs = BTreeMap::new();
    for (name, left) in expected {
        let right = &actual[&name];
        for field in ["kind", "package", "version", "filename"] {
            equal(
                &format!("artifacts.{name}.{field}"),
                &left[field],
                &right[field],
            )?;
        }
        let oracle_path = oracle_dir.join(&name);
        let shadow_path = shadow_dir.join(&name);
        for (path, record) in [(&oracle_path, &left), (&shadow_path, right)] {
            ensure!(
                archive::hash(path)? == record["sha256"].as_str().context("artifact hash")?
                    && fs::metadata(path)?.len()
                        == record["size"].as_u64().context("artifact size")?,
                "artifact differs from its manifest: {name}"
            );
        }
        let (oracle_digest, shadow_digest) = if left["kind"] == "python-sdist" {
            (
                archive::semantic_sdist_digest(&oracle_path)?,
                archive::semantic_sdist_digest(&shadow_path)?,
            )
        } else {
            (archive::hash(&oracle_path)?, archive::hash(&shadow_path)?)
        };
        ensure!(
            oracle_digest == shadow_digest,
            "artifact parity mismatch {name}: oracle digest={oracle_digest} shadow digest={shadow_digest}"
        );
        if left["kind"] == "python-sdist" {
            sdists.insert(name.clone(), true);
        } else {
            hashes.insert(name.clone(), true);
        }
        digest_pairs.insert(
            name,
            json!({"oracle": oracle_digest, "shadow": shadow_digest}),
        );
    }
    Ok(
        json!({"schema_version": 1, "git_commit": oracle["git"]["commit"],
        "python_oracle": "PASS", "rust_shadow": "PASS", "manifest_semantic_parity": true,
        "shadow_accepts_oracle_manifest": true,
        "deterministic_artifact_hash_parity": hashes, "sdist_semantic_content_parity": sdists,
        "compared_digests": digest_pairs, "canonical_switch_authorized": false}),
    )
}
pub fn run(root: &Path, python: &Python, output: &Path) -> Result<()> {
    let commit = git::clean_commit(root)?;
    let result = run_inner(root, python, output, &commit);
    // Check end provenance on errors as well as success; never report parity
    // PASS after a failed comparison or a changed/dirty checkout.
    git::unchanged(root, &commit)?;
    result?;
    println!("xtask release parity PASS; Python validator remains canonical.");
    Ok(())
}

pub(crate) fn require_oracle_blob(blob: &str) -> Result<()> {
    ensure!(
        blob == ORACLE_BLOB,
        "parity requires the pinned validator blob"
    );
    Ok(())
}

fn run_inner(root: &Path, python: &Python, output: &Path, commit: &str) -> Result<()> {
    require_oracle_blob(&command::output(
        ["git", "rev-parse", "HEAD:tools/validate_release.py"],
        root,
    )?)?;
    let output = paths::output_dir(output, root)?;
    fs::create_dir_all(&output)?;
    let staging = ValidatorStage::new(&output)?;
    let oracle_stage = staging.prepare(Phase::Oracle)?;
    // The outer deadline covers the full sequential oracle gate, whose children have
    // 600/1200s deadlines; it cannot truncate a legitimately bounded all-scope run.
    command::run(
        [
            python.path.as_os_str(),
            root.join("tools/validate_release.py").as_os_str(),
            "--scope".as_ref(),
            "all".as_ref(),
            "--output-dir".as_ref(),
            oracle_stage.as_os_str(),
        ],
        root,
        &env::clean(),
        Duration::from_secs(14_400),
        false,
    )?;
    git::unchanged(root, commit)?;
    let oracle_dir = staging.finish(Phase::Oracle)?;
    let shadow_stage = staging.prepare(Phase::Shadow)?;
    ensure!(oracle_stage == shadow_stage, "validator build roots differ");
    crate::validate(root, python, Scope::All, Some(&shadow_stage))?;
    git::unchanged(root, commit)?;
    let shadow_dir = staging.finish(Phase::Shadow)?;
    let oracle: Value =
        serde_json::from_slice(&fs::read(oracle_dir.join("release-manifest.json"))?)?;
    let shadow: Value =
        serde_json::from_slice(&fs::read(shadow_dir.join("release-manifest.json"))?)?;
    manifest::validate(&shadow)?;
    manifest::validate(&oracle)?;
    println!("shadow accepts oracle manifest: PASS");
    // Ordinary shadow validation has already completed its own native schema gate.
    // Only parity permits this one-way call; no oracle build/audit functions called.
    command::run([python.path.as_os_str(), "-I".as_ref(), "-c".as_ref(),
        "import importlib.util,json,sys; spec=importlib.util.spec_from_file_location('frozen_release_oracle',sys.argv[1]); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); module.validate_manifest(json.load(open(sys.argv[2],encoding='utf-8'))); print('oracle accepts shadow manifest: PASS')".as_ref(),
        root.join("tools/validate_release.py").as_os_str(), shadow_dir.join("release-manifest.json").as_os_str()],
        root, &env::clean(), COMMAND_TIMEOUT, false)?;
    let mut report = compare(&oracle, &shadow, &oracle_dir, &shadow_dir)?;
    report["oracle_accepts_shadow_manifest"] = json!(true);
    git::unchanged(root, commit)?;
    manifest::write_json(&output.join("release-parity.json"), &report)?;
    fs::write(
        output.join("release-parity.txt"),
        format!(
            "Release parity PASS\nGit commit: {commit}\nPython oracle: PASS\nRust shadow: PASS\nManifest semantic parity: true\nOracle accepts shadow manifest: true\nShadow accepts oracle manifest: true\nTwo wheel SHA-256 hashes and Rust crate SHA-256 match.\nBoth sdist semantic content digests match (sorted safe paths and regular-file bytes; archive times, ownership, gzip metadata and directory entries ignored).\nIndependent repeated-build matched counts are observations and need not match.\ncanonical_switch_authorized=false\nPython validator remains canonical.\nThis parity output is not a release publication bundle. Nothing published.\n"
        ),
    )?;
    Ok(())
}
