use crate::{
    archive,
    command::{self, COMMAND_TIMEOUT, TEST_TIMEOUT},
    consumer, env, git, manifest,
    python::Python,
};
use anyhow::{Context, Result, ensure};
use serde_json::{Value, json};
use std::{
    ffi::OsString,
    fs,
    path::{Path, PathBuf},
};

fn build_venv(selected: &Python, destination: &Path) -> Result<PathBuf> {
    let python = selected.venv(destination)?;
    command::run(
        [
            python.as_os_str(),
            "-m".as_ref(),
            "pip".as_ref(),
            "install".as_ref(),
            "build==1.3.0".as_ref(),
            "setuptools==84.0.0".as_ref(),
            "wheel==0.48.0".as_ref(),
            "maturin==1.15.0".as_ref(),
        ],
        destination.parent().context("build venv parent")?,
        &env::clean(),
        COMMAND_TIMEOUT,
        false,
    )?;
    Ok(python)
}
fn python_package(
    python: &Path,
    source: &Path,
    destination: &Path,
    epoch: u64,
) -> Result<Vec<PathBuf>> {
    fs::create_dir_all(destination)?;
    let mut environment = env::build(python)?;
    environment.insert("SOURCE_DATE_EPOCH".into(), epoch.to_string().into());
    let config: toml::Value = toml::from_str(&fs::read_to_string(source.join("pyproject.toml"))?)?;
    let mut args = vec![
        python.as_os_str().to_owned(),
        "-m".into(),
        "build".into(),
        "--no-isolation".into(),
        "--wheel".into(),
        "--sdist".into(),
    ];
    if config["build-system"]["build-backend"].as_str() == Some("maturin") {
        args.push("--config-setting=build-args=--locked".into());
    }
    args.extend([
        OsString::from("--outdir"),
        destination.as_os_str().to_owned(),
        source.as_os_str().to_owned(),
    ]);
    command::run(
        args,
        destination.parent().context("build destination parent")?,
        &environment,
        TEST_TIMEOUT,
        false,
    )?;
    let mut paths: Vec<_> = fs::read_dir(destination)?
        .map(|p| p.map(|p| p.path()))
        .collect::<std::io::Result<_>>()?;
    paths.sort();
    ensure!(
        paths.len() == 2
            && paths
                .iter()
                .filter(|p| p.extension().is_some_and(|s| s == "whl"))
                .count()
                == 1
            && paths
                .iter()
                .filter(|p| p.to_string_lossy().ends_with(".tar.gz"))
                .count()
                == 1,
        "expected exactly one wheel and one sdist: {paths:?}"
    );
    Ok(paths)
}
fn rust_package(source: &Path, destination: &Path, epoch: u64) -> Result<PathBuf> {
    let target = destination.join("cargo-target");
    let mut environment = env::clean();
    environment.insert("SOURCE_DATE_EPOCH".into(), epoch.to_string().into());
    command::run(
        [
            "cargo".as_ref(),
            "package".as_ref(),
            "--locked".as_ref(),
            "--package".as_ref(),
            "airpods-client".as_ref(),
            "--target-dir".as_ref(),
            target.as_os_str(),
        ],
        source,
        &environment,
        TEST_TIMEOUT,
        false,
    )?;
    let path = target.join(format!("package/airpods-client-{}.crate", crate::VERSION));
    ensure!(
        path.is_file(),
        "cargo package did not produce {}",
        path.display()
    );
    Ok(path)
}
fn round(python: &Path, source: &Path, work: &Path, epoch: u64) -> Result<Vec<PathBuf>> {
    let mut packages = python_package(python, source, &work.join("production"), epoch)?;
    packages.extend(python_package(
        python,
        &source.join("packages/airpods-client-python"),
        &work.join("python-client"),
        epoch,
    )?);
    packages.push(rust_package(source, &work.join("rust-client"), epoch)?);
    Ok(packages)
}
pub fn validate(root: &Path, selected: &Python, output: &Path, commit: &str) -> Result<Value> {
    fs::create_dir_all(output)?;
    let work = output.join(".work");
    fs::create_dir(&work)?;
    println!(
        "artifact work directory (retained on failure): {}",
        work.display()
    );
    let epoch = git::epoch(root)?;
    let first_source = work.join("source-first");
    let second_source = work.join("source-second");
    git::export(root, commit, &first_source)?;
    git::export(root, commit, &second_source)?;
    let build_python = build_venv(selected, &work.join("build-venv"))?;
    let first = round(&build_python, &first_source, &work.join("first"), epoch)?;
    let second = round(&build_python, &second_source, &work.join("second"), epoch)?;
    let mut repeated = Vec::new();
    let mut records = Vec::new();
    let mut copied = Vec::new();
    for (left, right) in first.iter().zip(&second) {
        let filename = left.file_name().context("artifact basename")?;
        repeated.push(json!({"filename": filename.to_str().context("UTF-8 artifact basename")?,
            "byte_for_byte_equal": left.file_name() == right.file_name() && archive::hash(left)? == archive::hash(right)?}));
        let path = output.join(filename);
        fs::copy(left, &path)?;
        let (kind, package) = if path.extension().is_some_and(|s| s == "crate") {
            ("rust-crate", "airpods-client")
        } else {
            (
                if path.extension().is_some_and(|s| s == "whl") {
                    "python-wheel"
                } else {
                    "python-sdist"
                },
                if filename.to_string_lossy().starts_with("airpods_hr_linux-") {
                    "airpods-hr-linux"
                } else {
                    "airpods-client"
                },
            )
        };
        match (kind, package) {
            ("python-wheel", "airpods-hr-linux") => {
                archive::production_wheel(&path)?;
                consumer::production(&path, &work.join("production-consumer"), selected)?;
            }
            ("python-wheel", _) => {
                archive::client_wheel(&path)?;
                consumer::python_client(&path, &work.join("python-client-consumer"), selected)?;
            }
            ("python-sdist", package) => {
                archive::sdist(&path, package)?;
                if package == "airpods-hr-linux" {
                    consumer::production_sdist(
                        &path,
                        &build_python,
                        &work.join("production-sdist"),
                        selected,
                    )?;
                }
            }
            _ => {
                archive::rust_crate(&path, root)?;
                consumer::rust(&path, &work.join("rust-consumer"))?;
            }
        }
        records.push(manifest::record(&path, kind, package)?);
        copied.push(path);
    }
    records.sort_by_key(|a| {
        (
            a["package"].as_str().unwrap_or_default().to_owned(),
            a["kind"].as_str().unwrap_or_default().to_owned(),
            a["filename"].as_str().unwrap_or_default().to_owned(),
        )
    });
    let matched = repeated
        .iter()
        .filter(|r| r["byte_for_byte_equal"] == true)
        .count();
    let m = json!({"schema_version": 1, "release_version": crate::VERSION,
        "git": {"commit": commit, "clean": true}, "source_date_epoch": epoch,
        "toolchain": {"python": selected.facts["python"], "python_implementation": selected.facts["python_implementation"],
            "python_build_frontend": "build 1.3.0", "setuptools": "84.0.0", "wheel": "0.48.0", "maturin": "1.15.0",
            "rustc": command::output(["rustc", "--version"], root)?, "cargo": command::output(["cargo", "--version"], root)?, "platform": selected.facts["platform"]},
        "artifacts": records, "repeat_build_check": {"source_date_epoch": epoch, "artifact_count": 5,
            "matched_artifact_count": matched, "byte_for_byte_equal": matched == 5, "artifacts": repeated,
            "scope": manifest::REPEAT_SCOPE, "reproducible_build_guarantee": false},
        "validation": {"bluetooth_hardware_used": false, "production_daemon_started": false, "published": false}});
    manifest::validate(&m)?;
    git::unchanged(root, commit)?;
    manifest::write_json(&output.join("release-manifest.json"), &m)?;
    manifest::summary(&output.join("release-summary.txt"), &m)?;
    fs::remove_dir_all(&work)?;
    ensure!(
        fs::read_dir(output)?.count() == 7 && copied.iter().all(|p| p.is_file()),
        "unexpected successful output contents"
    );
    println!("same-host repeated builds: {matched}/5; reproducible-build guarantee: false");
    Ok(m)
}
