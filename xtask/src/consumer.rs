use crate :: { archive , command :: { self , COMMAND_TIMEOUT , TEST_TIMEOUT } , env , python :: Python } ;
use anyhow :: { Context , Result , ensure } ;
use std :: { fs , path :: Path , time :: Duration } ;

pub fn production(wheel: &Path, work: &Path, selected: &Python) -> Result<()> {
    let python = selected.venv(work)?;
    let environment = env::clean();
    command::run(
        [
            python.as_os_str(),
            "-m".as_ref(),
            "pip".as_ref(),
            "install".as_ref(),
            wheel.as_os_str(),
        ],
        work,
        &environment,
        TEST_TIMEOUT,
        false,
    )?;
    command::run(
        [
            python.as_os_str(),
            "-m".as_ref(),
            "pip".as_ref(),
            "check".as_ref(),
        ],
        work,
        &environment,
        COMMAND_TIMEOUT,
        false,
    )?;
    command::run(
        [
            python.as_os_str(),
            "-I".as_ref(),
            "-c".as_ref(),
            include_str!("../probes/production.py").as_ref(),
        ],
        work,
        &environment,
        COMMAND_TIMEOUT,
        false,
    )?;
    for name in ["airpods-hr", "airpods-hubd", "airpods-hubd-service"] {
        let executable = work.join("bin").join(name);
        ensure!(
            executable.is_file(),
            "clean production install lacks {name}"
        );
        command::run(
            [executable.as_os_str(), "--help".as_ref()],
            work,
            &environment,
            Duration::from_secs(30),
            true,
        )?;
    }
    let mut isolated = environment.clone();
    for (key, name) in [
        ("HOME", "isolated-home"),
        ("XDG_CONFIG_HOME", "isolated-config"),
        ("XDG_RUNTIME_DIR", "isolated-runtime"),
    ] {
        let path = work.join(name);
        fs::create_dir(&path)?;
        isolated.insert(key.into(), path.into_os_string());
    }
    let result = command::run(
        [
            work.join("bin/airpods-hubd-service").as_os_str(),
            "install".as_ref(),
            "--dry-run".as_ref(),
        ],
        work,
        &isolated,
        COMMAND_TIMEOUT,
        true,
    )?;
    ensure!(
        result.contains(&format!(
            "exec_start=\"{}\" -m airpods_hr._hubd.main",
            python.display()
        )),
        "service dry run did not use installed interpreter"
    );
    ensure!(
        fs::read_dir(work.join("isolated-config"))?.next().is_none(),
        "service dry run mutated isolated configuration"
    );
    command::run(
        [
            python.as_os_str(),
            "-m".as_ref(),
            "compileall".as_ref(),
            "-q".as_ref(),
            work.join("lib").as_os_str(),
        ],
        work,
        &environment,
        COMMAND_TIMEOUT,
        false,
    )?;
    Ok(())
}
pub fn python_client(wheel: &Path, work: &Path, selected: &Python) -> Result<()> {
    let python = selected.venv(work)?;
    let environment = env::clean();
    command::run(
        [
            python.as_os_str(),
            "-m".as_ref(),
            "pip".as_ref(),
            "install".as_ref(),
            "--no-index".as_ref(),
            "--no-deps".as_ref(),
            wheel.as_os_str(),
        ],
        work,
        &environment,
        COMMAND_TIMEOUT,
        false,
    )?;
    command::run(
        [
            python.as_os_str(),
            "-m".as_ref(),
            "pip".as_ref(),
            "check".as_ref(),
        ],
        work,
        &environment,
        COMMAND_TIMEOUT,
        false,
    )?;
    command::run(
        [
            python.as_os_str(),
            "-I".as_ref(),
            "-c".as_ref(),
            include_str!("../probes/python_client.py").as_ref(),
        ],
        work,
        &environment,
        COMMAND_TIMEOUT,
        false,
    )?;
    command::run(
        [
            python.as_os_str(),
            "-m".as_ref(),
            "compileall".as_ref(),
            "-q".as_ref(),
            work.join("lib").as_os_str(),
        ],
        work,
        &environment,
        COMMAND_TIMEOUT,
        false,
    )?;
    Ok(())
}
pub fn production_sdist(
    sdist: &Path,
    build_python: &Path,
    work: &Path,
    selected: &Python,
) -> Result<()> {
    archive::extract_tar(sdist, &work.join("source"), true)?;
    let project = work.join(format!("source/airpods_hr_linux-{}", crate::VERSION));
    let wheels = work.join("wheels");
    command::run(
        [
            build_python.as_os_str(),
            "-m".as_ref(),
            "build".as_ref(),
            "--no-isolation".as_ref(),
            "--wheel".as_ref(),
            "--outdir".as_ref(),
            wheels.as_os_str(),
            project.as_os_str(),
        ],
        work,
        &env::build(build_python)?,
        TEST_TIMEOUT,
        false,
    )?;
    let paths: Vec<_> = fs::read_dir(wheels)?
        .map(|p| p.map(|p| p.path()))
        .collect::<std::io::Result<_>>()?;
    ensure!(
        paths.len() == 1 && paths[0].extension().is_some_and(|s| s == "whl"),
        "sdist rebuild must produce one wheel"
    );
    archive::production_wheel(&paths[0])?;
    production(&paths[0], &work.join("consumer"), selected)
}

