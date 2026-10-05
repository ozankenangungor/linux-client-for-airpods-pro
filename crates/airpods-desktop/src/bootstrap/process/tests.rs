use super::*;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_FILE: AtomicU64 = AtomicU64::new(0);

struct ReadyFile(PathBuf);
impl ReadyFile {
    fn new() -> Self {
        Self(std::env::temp_dir().join(format!(
            "desktop-child-{}-{}.ready",
            std::process::id(),
            NEXT_FILE.fetch_add(1, Ordering::Relaxed)
        )))
    }
}
impl Drop for ReadyFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

async fn await_ready(
    path: &Path,
    expected_pids: usize,
    process: &mut std::pin::Pin<
        Box<impl std::future::Future<Output = Result<CommandResult, ProcessError>>>,
    >,
) -> Vec<u32> {
    tokio::time::timeout(Duration::from_secs(3), async {
        loop {
            tokio::select! {
                result = &mut *process => panic!("child ended before cancellation: {result:?}"),
                _ = tokio::time::sleep(Duration::from_millis(5)) => {
                    if let Ok(contents) = tokio::fs::read_to_string(path).await {
                        let pids: Vec<_> = contents.split_whitespace().map(|pid| pid.parse().unwrap()).collect();
                        if pids.len() == expected_pids {
                            return pids;
                        }
                    }
                },
            }
        }
    }).await.expect("fake bootstrap child did not start")
}

fn alive(pid: u32) -> bool {
    std::fs::read_to_string(format!("/proc/{pid}/stat"))
        .ok()
        .is_some_and(|stat| !stat.split_once(") ").unwrap().1.starts_with('Z'))
}

async fn await_dead(pid: u32) {
    // Group SIGKILL may still be pending in a descendant after the leader is reaped.
    tokio::time::timeout(Duration::from_millis(100), async {
        while alive(pid) {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("owned child {pid} remained alive after cancellation"));
}

#[tokio::test]
async fn readiness_waits_for_all_owned_pids() {
    let ready = ReadyFile::new();
    std::fs::write(&ready.0, "").unwrap();
    let mut process = Box::pin(std::future::pending::<Result<CommandResult, ProcessError>>());
    let mut waiting = Box::pin(await_ready(&ready.0, 2, &mut process));
    assert!(
        tokio::time::timeout(Duration::from_millis(20), &mut waiting)
            .await
            .is_err()
    );
    std::fs::write(&ready.0, "12345").unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(20), &mut waiting)
            .await
            .is_err()
    );
    std::fs::write(&ready.0, "12345 67890").unwrap();
    assert_eq!(
        tokio::time::timeout(Duration::from_millis(100), waiting)
            .await
            .unwrap(),
        [12345, 67890]
    );
}

#[tokio::test]
async fn cancellation_terminates_only_bootstrap_process_group_and_reaps_child() {
    let ready = ReadyFile::new();
    // An unrelated process remains alive throughout bootstrap cancellation.
    let mut unrelated = Command::new("/bin/sleep")
        .arg("30")
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let script = "import os, pathlib, signal, subprocess, sys, time\nchild = subprocess.Popen(['/bin/sleep', '30'])\ndef stop(*args):\n child.wait()\n sys.exit(0)\nsignal.signal(signal.SIGTERM, stop)\npathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}')\ntime.sleep(30)";
    let spec = CommandSpec::new(
        "/usr/bin/python3",
        [
            OsString::from("-I"),
            OsString::from("-c"),
            OsString::from(script),
            ready.0.as_os_str().into(),
        ],
        Duration::from_secs(20),
    );
    let (stop, mut stopped) = watch::channel(false);
    let mut running = Box::pin(run(spec, &mut stopped));
    let pids = await_ready(&ready.0, 2, &mut running).await;
    let before = std::time::Instant::now();
    stop.send(true).unwrap();
    assert_eq!(running.await.unwrap_err(), ProcessError::Cancelled);
    assert!(before.elapsed() < Duration::from_secs(2));
    for pid in pids {
        await_dead(pid).await;
    }
    assert!(unrelated.try_wait().unwrap().is_none());
    unrelated.kill().await.unwrap();
    unrelated.wait().await.unwrap();
}

#[tokio::test]
async fn cancellation_force_kills_a_child_that_ignores_termination() {
    let ready = ReadyFile::new();
    let script = "import os, pathlib, signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(30)";
    let spec = CommandSpec::new(
        "/usr/bin/python3",
        [
            OsString::from("-I"),
            OsString::from("-c"),
            OsString::from(script),
            ready.0.as_os_str().into(),
        ],
        Duration::from_secs(20),
    );
    let (stop, mut stopped) = watch::channel(false);
    let mut running = Box::pin(run(spec, &mut stopped));
    let pids = await_ready(&ready.0, 1, &mut running).await;
    let before = std::time::Instant::now();
    stop.send(true).unwrap();
    assert_eq!(running.await.unwrap_err(), ProcessError::Cancelled);
    assert!(before.elapsed() < Duration::from_secs(2));
    assert!(!alive(pids[0]));
}

#[tokio::test]
async fn command_timeout_is_enforced_and_output_is_bounded() {
    let (_stop, mut stopped) = watch::channel(false);
    let spec = CommandSpec::new("/bin/sleep", ["30"], Duration::from_millis(30));
    assert_eq!(
        run(spec, &mut stopped).await.unwrap_err(),
        ProcessError::TimedOut
    );
    let spec = CommandSpec::new(
        "/usr/bin/python3",
        [
            "-I",
            "-c",
            "import sys; print('x' * 50000); sys.stderr.write('y' * 50000)",
        ],
        Duration::from_secs(3),
    );
    let result = run(spec, &mut stopped).await.unwrap();
    assert!(result.success);
    assert_eq!(result.stdout.len(), OUTPUT_LIMIT);
}

#[tokio::test]
async fn paths_and_shell_metacharacters_are_only_arguments() {
    let (_stop, mut stopped) = watch::channel(false);
    let value = "path with spaces/'quotes'/$HOME/$(touch forbidden)/;\nnext line";
    let spec = CommandSpec::new(
        "/usr/bin/python3",
        [
            "-I",
            "-c",
            "import sys; sys.stdout.write(sys.argv[1])",
            value,
        ],
        Duration::from_secs(3),
    );
    let result = run(spec, &mut stopped).await.unwrap();
    assert!(result.success);
    assert_eq!(result.stdout, value.as_bytes());
}

#[tokio::test]
async fn pre_cancelled_command_never_spawns_a_process() {
    let (stop, mut stopped) = watch::channel(false);
    stop.send(true).unwrap();
    let spec = CommandSpec::new("does-not-exist", ["anything"], Duration::from_secs(1));
    assert_eq!(
        run(spec, &mut stopped).await.unwrap_err(),
        ProcessError::Cancelled
    );
}
