//! Every child has a deadline, sanitized environment, and joined output readers.
use crate::env::{self, Environment};
use anyhow::{Context, Result, bail};
use nix::{
    sys::signal::{Signal, killpg},
    unistd::Pid,
};
use std::{
    ffi::{OsStr, OsString},
    io::{Read, Write},
    os::unix::process::CommandExt,
    path::Path,
    process::{Command, Stdio},
    thread,
    time::Duration,
};
use wait_timeout::ChildExt;

pub const COMMAND_TIMEOUT: Duration = Duration::from_secs(600);
pub const TEST_TIMEOUT: Duration = Duration::from_secs(1200);
const TAIL: usize = 8_000;
const CAPTURE_LIMIT: usize = 16 * 1024 * 1024;

struct Readout {
    bytes: Vec<u8>,
    tail: Vec<u8>,
    overflow: bool,
}

fn drain(mut pipe: impl Read, capture: bool, stderr: bool) -> std::io::Result<Readout> {
    let mut result = Readout {
        bytes: Vec::new(),
        tail: Vec::new(),
        overflow: false,
    };
    let mut buffer = [0; 8192];
    loop {
        let n = pipe.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        let chunk = &buffer[..n];
        if capture {
            if result.bytes.len() + n <= CAPTURE_LIMIT {
                result.bytes.extend_from_slice(chunk);
            } else {
                result.overflow = true;
            }
        } else if stderr {
            let _ = std::io::stderr().lock().write_all(chunk);
        } else {
            let _ = std::io::stdout().lock().write_all(chunk);
        }
        result.tail.extend_from_slice(chunk);
        if result.tail.len() > TAIL {
            result.tail.drain(..result.tail.len() - TAIL);
        }
    }
    Ok(result)
}

pub fn run<I, S>(
    argv: I,
    cwd: &Path,
    environment: &Environment,
    timeout: Duration,
    capture: bool,
) -> Result<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    let argv: Vec<OsString> = argv.into_iter().map(|s| s.as_ref().to_owned()).collect();
    let (program, args) = argv.split_first().context("empty argv")?;
    // Probe program bodies are intentionally omitted from logs; review them in probes/.
    let display: Vec<_> = argv
        .iter()
        .map(|s| {
            let s = s.to_string_lossy();
            if s.contains('\n') {
                "<probe program>".into()
            } else {
                s.into_owned()
            }
        })
        .collect();
    println!("+ {display:?}");
    let mut child = Command::new(program)
        .args(args)
        .current_dir(cwd)
        .env_clear()
        .envs(env::sanitize(environment.clone()))
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()
        .with_context(|| format!("spawn {display:?}"))?;
    let pid = Pid::from_raw(i32::try_from(child.id())?);
    let stdout = child.stdout.take().context("stdout pipe")?;
    let stderr = child.stderr.take().context("stderr pipe")?;
    let out_reader = thread::spawn(move || drain(stdout, capture, false));
    let err_reader = thread::spawn(move || drain(stderr, capture, true));
    let status = child.wait_timeout(timeout);
    // Terminate descendants too, including any retaining captured pipe handles.
    // This is also done on normal completion: no child may be detached by a gate.
    let _ = killpg(pid, Signal::SIGKILL);
    let reaped = child.wait();
    let out = out_reader
        .join()
        .map_err(|_| anyhow::anyhow!("stdout reader panicked"))??;
    let err = err_reader
        .join()
        .map_err(|_| anyhow::anyhow!("stderr reader panicked"))??;
    reaped.context("reap child")?;
    let diagnostic = format!(
        "{}\n{}",
        String::from_utf8_lossy(&out.tail),
        String::from_utf8_lossy(&err.tail)
    );
    let Some(status) = status.context("wait for child")? else {
        bail!("command timed out after {timeout:?}: {display:?}\n{diagnostic}");
    };
    if !status.success() {
        bail!("command exited {status}: {display:?}\n{diagnostic}");
    }
    if out.overflow || err.overflow {
        bail!("captured output exceeded bound: {display:?}");
    }
    String::from_utf8(out.bytes).context("command stdout is not UTF-8")
}

pub fn output<I, S>(argv: I, cwd: &Path) -> Result<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    Ok(run(argv, cwd, &env::clean(), COMMAND_TIMEOUT, true)?
        .trim()
        .to_owned())
}
