//! Argument-only subprocess execution with bounded capture and owned process groups.

use std::ffi::OsString;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;
use tokio::sync::watch;

const OUTPUT_LIMIT: usize = 4096;

#[derive(Clone, Debug)]
pub(super) struct CommandSpec {
    pub program: OsString,
    pub args: Vec<OsString>,
    pub timeout: Duration,
}

impl CommandSpec {
    pub fn new(
        program: impl Into<OsString>,
        args: impl IntoIterator<Item = impl Into<OsString>>,
        timeout: Duration,
    ) -> Self {
        Self {
            program: program.into(),
            args: args.into_iter().map(Into::into).collect(),
            timeout,
        }
    }
}

#[derive(Debug)]
pub(super) struct CommandResult {
    pub success: bool,
    pub stdout: Vec<u8>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum ProcessError {
    Cancelled,
    TimedOut,
    Unavailable,
}

struct ProcessGroup(Option<rustix::process::Pid>);

impl ProcessGroup {
    fn kill(&self) {
        if let Some(pid) = self.0 {
            let _ = rustix::process::kill_process_group(pid, rustix::process::Signal::KILL);
        }
    }
}

impl Drop for ProcessGroup {
    fn drop(&mut self) {
        self.kill();
    }
}

async fn capture(mut pipe: impl AsyncRead + Unpin) -> std::io::Result<Vec<u8>> {
    let mut output = Vec::with_capacity(OUTPUT_LIMIT);
    let mut buffer = [0; 4096];
    loop {
        let count = pipe.read(&mut buffer).await?;
        if count == 0 {
            return Ok(output);
        }
        let keep = count.min(OUTPUT_LIMIT.saturating_sub(output.len()));
        output.extend_from_slice(&buffer[..keep]);
    }
}

pub(super) async fn run(
    spec: CommandSpec,
    stop: &mut watch::Receiver<bool>,
) -> Result<CommandResult, ProcessError> {
    if *stop.borrow() {
        return Err(ProcessError::Cancelled);
    }
    let mut command = Command::new(spec.program);
    command
        .args(spec.args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .process_group(0)
        .env_remove("PYTHONPATH")
        .env_remove("PYTHONHOME")
        .env_remove("VIRTUAL_ENV");
    let mut child = command.spawn().map_err(|_| ProcessError::Unavailable)?;
    let pid = child
        .id()
        .filter(|pid| *pid > 1)
        .and_then(|pid| i32::try_from(pid).ok())
        .and_then(rustix::process::Pid::from_raw)
        .ok_or(ProcessError::Unavailable)?;
    let mut group = ProcessGroup(Some(pid));
    // A child ID must never turn into kill(0) or kill(-1).
    let stdout = child.stdout.take().ok_or(ProcessError::Unavailable)?;
    let stderr = child.stderr.take().ok_or(ProcessError::Unavailable)?;
    let deadline = tokio::time::Instant::now() + spec.timeout;
    let result = tokio::select! {
        biased;
        _ = stop.changed() => Err(ProcessError::Cancelled),
        _ = tokio::time::sleep_until(deadline) => Err(ProcessError::TimedOut),
        result = async {
            let (status, output, errors) = tokio::join!(child.wait(), capture(stdout), capture(stderr));
            let status = status.map_err(|_| ProcessError::Unavailable)?;
            let output = output.map_err(|_| ProcessError::Unavailable)?;
            errors.map_err(|_| ProcessError::Unavailable)?;
            Ok(CommandResult { success: status.success(), stdout: output })
        } => result,
    };
    if result.is_err() {
        // Kill the whole owned group before reaping its leader. A build helper
        // that ignores SIGTERM must not outlive a cancelled installation.
        group.kill();
        let _ = child.start_kill();
        let _ = tokio::time::timeout(Duration::from_millis(500), child.wait()).await;
    }
    group.0 = None;
    result
}

#[cfg(test)]
mod tests;
