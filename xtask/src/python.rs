use crate::{
    command::{self, COMMAND_TIMEOUT, TEST_TIMEOUT},
    env, paths,
};
use anyhow::{Context, Result, ensure};
use serde_json::Value;
use std::{
    ffi::OsStr,
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    time::Duration,
};

pub struct Python {
    pub path: PathBuf,
    pub facts: Value,
}
impl Python {
    pub fn select(explicit: Option<&Path>, root: &Path) -> Result<Self> {
        let requested = explicit.map(Path::to_path_buf).or_else(|| {
            std::env::var_os("AIRPODS_RELEASE_PYTHON")
                .filter(|v| !v.is_empty())
                .map(PathBuf::from)
        });
        let path = if let Some(requested) = requested {
            if requested.components().count() == 1 {
                paths::which(requested.as_os_str()).with_context(|| {
                    format!("selected Python unavailable: {}", requested.display())
                })?
            } else {
                // Keep the venv symlink path: canonicalizing it would select the base interpreter.
                std::env::current_dir()?.join(requested)
            }
        } else if paths::executable(&root.join(".venv/bin/python")) {
            root.join(".venv/bin/python")
        } else {
            paths::which(OsStr::new("python3.14"))
                .context("python3.14 unavailable; specify --python")?
        };
        ensure!(
            paths::executable(&path),
            "selected Python is not executable: {}",
            path.display()
        );
        let facts: Value = serde_json::from_str(&command::run([
            path.as_os_str(), "-I".as_ref(), "-c".as_ref(),
            "import json,platform,sys; print(json.dumps(dict(python=platform.python_version(),python_implementation=platform.python_implementation(),executable=sys.executable,platform=platform.platform())))".as_ref(),
        ], root, &env::clean(), Duration::from_secs(30), true)?)?;
        println!(
            "release Python: {} {} executable={}",
            facts["python"], facts["python_implementation"], facts["executable"]
        );
        Ok(Self { path, facts })
    }
    pub fn venv(&self, destination: &Path) -> Result<PathBuf> {
        command::run(
            [
                self.path.as_os_str(),
                "-m".as_ref(),
                "venv".as_ref(),
                destination.as_os_str(),
            ],
            destination.parent().context("venv parent")?,
            &env::clean(),
            COMMAND_TIMEOUT,
            false,
        )?;
        Ok(destination.join("bin/python"))
    }
}
pub fn checks(root: &Path, python: &Python) -> Result<()> {
    let environment = env::python(root);
    command::run(
        [
            python.path.as_os_str(),
            "-m".as_ref(),
            "unittest".as_ref(),
            "discover".as_ref(),
            "-s".as_ref(),
            "tests".as_ref(),
            "-v".as_ref(),
        ],
        root,
        &environment,
        TEST_TIMEOUT,
        false,
    )?;
    let temp = tempfile::tempdir()?;
    let mut environment = environment;
    environment.insert(
        "PYTHONPYCACHEPREFIX".into(),
        temp.path().as_os_str().to_owned(),
    );
    command::run(
        [
            python.path.as_os_str(),
            "-m".as_ref(),
            "compileall".as_ref(),
            "-q".as_ref(),
            "src".as_ref(),
            "packages/airpods-client-python/src".as_ref(),
        ],
        root,
        &environment,
        COMMAND_TIMEOUT,
        false,
    )?;
    systemd(root, python)
}
fn systemd(root: &Path, python: &Python) -> Result<()> {
    let Some(analyzer) = paths::which(OsStr::new("systemd-analyze")) else {
        println!("systemd parser: SKIP (systemd-analyze unavailable)");
        return Ok(());
    };
    let temp = tempfile::tempdir()?;
    let runtime = temp.path().join("runtime");
    fs::create_dir_all(runtime.join("systemd"))?;
    for dir in [&runtime, &runtime.join("systemd")] {
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700))?;
    }
    let interpreter = temp.path().join("installed environment/bin/python");
    fs::create_dir_all(interpreter.parent().context("fake interpreter parent")?)?;
    fs::write(&interpreter, "#!/bin/sh\nexit 0\n")?;
    fs::set_permissions(&interpreter, fs::Permissions::from_mode(0o700))?;
    let unit = temp.path().join("airpods-hubd.service");
    command::run([python.path.as_os_str(), "-I".as_ref(), "-c".as_ref(),
        "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from airpods_hr.service_installer import render_unit; Path(sys.argv[3]).write_text(render_unit(Path(sys.argv[2])),encoding='utf-8')".as_ref(),
        root.join("src").as_os_str(), interpreter.as_os_str(), unit.as_os_str()],
        temp.path(), &env::clean(), Duration::from_secs(30), false)?;
    let mut environment = env::clean();
    environment.insert("XDG_RUNTIME_DIR".into(), runtime.into_os_string());
    command::run(
        [
            analyzer.as_os_str(),
            "verify".as_ref(),
            "--user".as_ref(),
            unit.as_os_str(),
        ],
        temp.path(),
        &environment,
        Duration::from_secs(30),
        false,
    )?;
    println!("systemd parser: PASS");
    Ok(())
}
