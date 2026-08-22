use crate :: { command , env , paths } ;

use std :: { ffi :: OsString , fs , time :: { Duration , Instant } } ;



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




















































