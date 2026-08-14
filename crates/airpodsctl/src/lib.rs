#![forbid(unsafe_code)]

mod top;
use airpods_client::{AirPodsClient, DaemonState, HeartRateSample, Hello, SourceSide, Status};
use airpods_client_resilient::{
    ReconnectPolicy, ResilientHeartRateEvent, ResilientHeartRateStream,
};
use clap::{Parser, Subcommand};
use serde_json::json;
use std::io::{self, Write};
use std::num::NonZeroU64;
use std::path::PathBuf;

#[derive(Debug, Parser)]
#[command(name = "airpodsctl", version, about = "Inspect a running airpods-hubd")]
pub struct Cli {
    /// Explicit daemon Unix socket path
    #[arg(long, global = true, value_name = "PATH")]
    pub socket: Option<PathBuf>,
    /// Emit one compact JSON object per result or event
    #[arg(long, global = true)]
    pub json: bool,
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    Hello,
    Ping,
    Status,
    Watch {
        /// Stop after N samples and confirm unsubscribe
        #[arg(long, value_name = "N")]
        count: Option<NonZeroU64>,
        /// Retry a lost daemon connection with a finite schedule
        #[arg(long)]
        reconnect: bool,
    },
}

fn state_text(state: &DaemonState) -> &str {
    match state {
        DaemonState::Stopped => "stopped",
        DaemonState::Starting => "starting",
        DaemonState::Ready => "ready",
        DaemonState::StartingHeartRate => "starting_hr",
        DaemonState::Streaming => "streaming",
        DaemonState::StoppingHeartRate => "stopping_hr",
        DaemonState::Failed => "failed",
        DaemonState::ShuttingDown => "shutting_down",
        DaemonState::Unknown(raw) => raw,
        _ => "unsupported",
    }
}

#[cfg(test)]
mod tests;

fn render_hello(value: &Hello, json_mode: bool) -> String {
    if json_mode {
        json!({"service": value.service, "experimental": value.experimental}).to_string()
    } else {
        format!(
            "service: {}\nexperimental: {}",
            value.service, value.experimental
        )
    }
}

fn render_ping(json_mode: bool) -> String {
    if json_mode {
        json!({"pong": true}).to_string()
    } else {
        "pong".to_owned()
    }
}

fn render_status(value: &Status, json_mode: bool) -> String {
    if json_mode {
        json!({"state": state_text(&value.state), "subscriber_count": value.subscriber_count})
            .to_string()
    } else {
        format!(
            "state: {}\nsubscribers: {}",
            state_text(&value.state),
            value.subscriber_count
        )
    }
}

fn render_sample(value: HeartRateSample, json_mode: bool) -> String {
    if json_mode {
        match value.source_side {
            SourceSide::Left => json!({"bpm": value.bpm, "source_side": "left"}).to_string(),
            SourceSide::Right => json!({"bpm": value.bpm, "source_side": "right"}).to_string(),
            SourceSide::Unknown(raw) => {
                json!({"bpm": value.bpm, "source_side": "unknown", "source_side_raw": raw})
                    .to_string()
            }
        }
    } else {
        format!("{} bpm ({})", value.bpm, value.source_side)
    }
}

fn output(line: &str) -> Result<(), String> {
    let mut stdout = io::stdout().lock();
    writeln!(stdout, "{line}").map_err(|error| format!("writing stdout: {error}"))
}

fn client_error(error: airpods_client::Error, explicit_socket: bool) -> String {
    match error {
        airpods_client::Error::Connect { message, .. } if !explicit_socket => {
            format!("could not connect to default airpods-hubd socket: {message}")
        }
        other => other.to_string(),
    }
}

/// Execute one CLI command. Returns the process exit code on non-error outcomes.
pub async fn run(cli: Cli) -> Result<u8, String> {
    let explicit_socket = cli.socket.is_some();
    if let Command::Watch { count, reconnect } = cli.command {
        return if reconnect {
            watch_resilient(cli.socket, cli.json, count, explicit_socket).await
        } else {
            watch(cli.socket, cli.json, count, explicit_socket).await
        };
    }
    let client = match cli.socket {
        Some(path) => AirPodsClient::connect_to(path).await,
        None => AirPodsClient::connect().await,
    }
    .map_err(|error| client_error(error, explicit_socket))?;
    let line = match cli.command {
        Command::Hello => render_hello(
            &client
                .hello()
                .await
                .map_err(|error| client_error(error, explicit_socket))?,
            cli.json,
        ),
        Command::Ping => {
            client
                .ping()
                .await
                .map_err(|error| client_error(error, explicit_socket))?;
            render_ping(cli.json)
        }
        Command::Status => render_status(
            &client
                .status()
                .await
                .map_err(|error| client_error(error, explicit_socket))?,
            cli.json,
        ),
        Command::Watch { .. } => unreachable!("watch dispatched above"),
    };
    output(&line)?;
    Ok(0)
}

fn resilient_error(error: airpods_client_resilient::Error, explicit_socket: bool) -> String {
    match error {
        airpods_client_resilient::Error::Client(error) => client_error(error, explicit_socket),
        airpods_client_resilient::Error::RetryExhausted {
            attempts,
            last_error,
        } => format!(
            "daemon reconnect exhausted after {attempts} {}: {}",
            if attempts == 1 { "attempt" } else { "attempts" },
            client_error(last_error, explicit_socket),
        ),
    }
}

async fn watch_resilient(
    socket: Option<PathBuf>,
    json_mode: bool,
    count: Option<NonZeroU64>,
    explicit_socket: bool,
) -> Result<u8, String> {
    watch_resilient_with_policy(
        socket,
        json_mode,
        count,
        explicit_socket,
        ReconnectPolicy::default(),
    )
    .await
}

async fn watch_resilient_with_policy(
    socket: Option<PathBuf>,
    json_mode: bool,
    count: Option<NonZeroU64>,
    explicit_socket: bool,
    policy: ReconnectPolicy,
) -> Result<u8, String> {
    let mut stream = match socket {
        Some(path) => ResilientHeartRateStream::explicit_socket(path, policy),
        None => ResilientHeartRateStream::default_socket(policy),
    };
    let interrupt = tokio::signal::ctrl_c();
    tokio::pin!(interrupt);
    let mut received = 0_u64;
    loop {
        tokio::select! {
            biased;
            result = &mut interrupt => {
                result.map_err(|error| format!("waiting for Ctrl-C: {error}"))?;
                stream.close().await.map_err(|error| resilient_error(error, explicit_socket))?;
                return Ok(130);
            }
            result = stream.next() => {
                match result.map_err(|error| resilient_error(error, explicit_socket))? {
                    Some(ResilientHeartRateEvent::Sample(sample)) => {
                        output(&render_sample(sample, json_mode))?;
                        received += 1;
                        if count.is_some_and(|limit| received == limit.get()) {
                            stream.close().await.map_err(|error| resilient_error(error, explicit_socket))?;
                            return Ok(0);
                        }
                    }
                    Some(ResilientHeartRateEvent::Reconnecting { attempt, delay }) => {
                        if json_mode {
                            output(&json!({"event":"reconnecting","attempt":attempt,"delay_ms":delay.as_millis()}).to_string())?;
                        } else {
                            eprintln!("reconnecting to airpods-hubd: attempt {attempt} in {}s", delay.as_secs());
                        }
                    }
                    Some(ResilientHeartRateEvent::Reconnected { attempts }) => {
                        if json_mode {
                            output(&json!({"event":"reconnected","attempts":attempts}).to_string())?;
                        } else {
                            eprintln!("reconnected to airpods-hubd after {attempts} {}", if attempts == 1 { "attempt" } else { "attempts" });
                        }
                    }
                    None => {
                        stream.close().await.map_err(|error| resilient_error(error, explicit_socket))?;
                        return Ok(0);
                    }
                }
            }
        }
    }
}

async fn watch(
    socket: Option<PathBuf>,
    json_mode: bool,
    count: Option<NonZeroU64>,
    explicit_socket: bool,
) -> Result<u8, String> {
    let interrupt = tokio::signal::ctrl_c();
    tokio::pin!(interrupt);
    let client = tokio::select! {
        biased;
        result = &mut interrupt => return result.map(|()| 130).map_err(|error| format!("waiting for Ctrl-C: {error}")),
        result = async {
            match socket {
                Some(path) => AirPodsClient::connect_to(path).await,
                None => AirPodsClient::connect().await,
            }
        } => result.map_err(|error| client_error(error, explicit_socket))?,
    };
    let mut subscription = tokio::select! {
        biased;
        result = &mut interrupt => return result.map(|()| 130).map_err(|error| format!("waiting for Ctrl-C: {error}")),
        result = client.subscribe_heart_rate() => result.map_err(|error| client_error(error, explicit_socket))?,
    };
    let mut received = 0_u64;
    loop {
        if count.is_some_and(|limit| received == limit.get()) {
            subscription
                .unsubscribe()
                .await
                .map_err(|error| client_error(error, explicit_socket))?;
            return Ok(0);
        }
        tokio::select! {
            biased;
            result = &mut interrupt => {
                result.map_err(|error| format!("waiting for Ctrl-C: {error}"))?;
                subscription.unsubscribe().await.map_err(|error| client_error(error, explicit_socket))?;
                return Ok(130);
            }
            result = subscription.next() => {
                match result.map_err(|error| client_error(error, explicit_socket))? {
                    Some(sample) => {
                        output(&render_sample(sample, json_mode))?;
                        received += 1;
                    }
                    None => {
                        subscription.unsubscribe().await.map_err(|error| client_error(error, explicit_socket))?;
                        return Ok(0);
                    }
                }
            }
        }
    }
}
