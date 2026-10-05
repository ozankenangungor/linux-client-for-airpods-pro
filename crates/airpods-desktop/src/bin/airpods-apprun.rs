//! AppImage entrypoint. Internal modes never reach the desktop CLI.
#![forbid(unsafe_code)]

use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;

const IDENTITY: &str = concat!(
    "airpods-hr-linux AppImage v1\n",
    env!("CARGO_PKG_VERSION"),
    "\n"
);

fn command(root: &Path, args: &[std::ffi::OsString]) -> std::io::Result<(Command, bool)> {
    if std::fs::read_to_string(root.join("airpods-distribution"))? != IDENTITY {
        return Err(std::io::Error::other("invalid application payload"));
    }
    let daemon = args
        .first()
        .is_some_and(|a| a == "--internal-daemon-service");
    let mut command = if daemon {
        if args.len() != 1 {
            return Err(std::io::Error::other("unexpected internal arguments"));
        }
        let mut c = Command::new(root.join("usr/lib/airpods-hr-linux/python/bin/python3.14"));
        c.args(["-I", "-m", "airpods_hr._hubd.main"]);
        c
    } else {
        let mut c = Command::new(root.join("usr/bin/airpods-desktop"));
        c.args(args)
            .env("AIRPODS_APPIMAGE", "1")
            .env("APPDIR", root);
        c
    };
    command
        .env("LD_LIBRARY_PATH", root.join("usr/lib"))
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .env_remove("VIRTUAL_ENV");
    Ok((command, daemon))
}

fn launch() -> std::io::Result<()> {
    let executable = std::env::current_exe()?.canonicalize()?;
    let root: PathBuf = executable
        .parent()
        .ok_or_else(|| std::io::Error::other("missing AppDir"))?
        .into();
    let (mut command, daemon) = command(&root, &std::env::args_os().skip(1).collect::<Vec<_>>())?;
    if daemon {
        // The runtime has completed its FUSE mount. Restore the source service's
        // privilege restriction before the unchanged Python daemon runs.
        rustix::thread::set_no_new_privs(true)?;
    }
    Err(command.exec())
}

fn main() {
    if launch().is_err() {
        eprintln!("AirPods HR: the bundled application could not start.");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn packaged_entrypoint_requires_identity() {
        assert!(command(Path::new("/nonexistent"), &[]).is_err());
    }
    #[test]
    fn internal_mode_has_one_fixed_bundled_command_and_demo_runs_only_gui() {
        let root = std::env::temp_dir().join(format!("apprun-test-{}", std::process::id()));
        std::fs::create_dir(&root).unwrap();
        std::fs::write(root.join("airpods-distribution"), IDENTITY).unwrap();
        let (daemon, is_daemon) = command(&root, &["--internal-daemon-service".into()]).unwrap();
        assert!(is_daemon);
        assert_eq!(
            daemon.get_program(),
            root.join("usr/lib/airpods-hr-linux/python/bin/python3.14")
        );
        assert_eq!(
            daemon.get_args().collect::<Vec<_>>(),
            ["-I", "-m", "airpods_hr._hubd.main"].map(std::ffi::OsStr::new)
        );
        assert!(
            command(
                &root,
                &["--internal-daemon-service".into(), "injected".into()]
            )
            .is_err()
        );
        let (demo, is_daemon) = command(&root, &["--demo".into()]).unwrap();
        assert!(!is_daemon);
        assert_eq!(demo.get_program(), root.join("usr/bin/airpods-desktop"));
        assert_eq!(
            demo.get_args().collect::<Vec<_>>(),
            [std::ffi::OsStr::new("--demo")]
        );
        std::fs::remove_dir_all(root).unwrap();
    }
}
