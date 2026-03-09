//! Repository-only cross-language probe for the real Python hub daemon tests.
//!
//! This is not a stable user CLI. It receives an explicit Unix socket path and
//! exercises only the public experimental `airpods-client` API.

use airpods_client::{
    AirPodsClient, DaemonState, Error, HeartRateSample, PROTOCOL_VERSION, SourceSide,
};
use serde_json::{Value, json};
use std::error::Error as StdError;
use std::io::{self, Write};
use std::path::PathBuf;

type ProbeResult<T = ()> = Result<T, Box<dyn StdError>>;

#[tokio::main(flavor = "current_thread")]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("integration_probe_error={error}");
        std::process::exit(1);
    }
}

async fn run() -> ProbeResult {
    let mut arguments = std::env::args_os().skip(1);
    let mode = arguments
        .next()
        .and_then(|value| value.into_string().ok())
        .ok_or_else(|| invalid("expected a probe mode"))?;
    let socket_path = arguments
        .next()
        .map(PathBuf::from)
        .ok_or_else(|| invalid("expected an explicit Unix socket path"))?;
    require(arguments.next().is_none(), "unexpected extra argument")?;
    require(PROTOCOL_VERSION == 1, "probe requires protocol version 1")?;

    match mode.as_str() {
        "basic" => basic(&socket_path).await,
        "two-clients" => two_clients(&socket_path).await,
        "drop-resubscribe" => drop_resubscribe(&socket_path).await,
        "disconnect" => disconnect(&socket_path).await,
        "mixed" => mixed(&socket_path).await,
        _ => Err(invalid("unknown probe mode").into()),
    }
}

async fn mixed(socket_path: &PathBuf) -> ProbeResult {
    let client = AirPodsClient::connect_to(socket_path).await?;
    let mut subscription = client.subscribe_heart_rate().await?;
    emit(json!({"phase": "rust_subscribed"}))?;
    emit(json!({"phase": "shared_events_ready"}))?;

    let first = required_sample(subscription.next().await?)?;
    let second = required_sample(subscription.next().await?)?;
    require_sample(first, 169, SourceSide::Left)?;
    require_sample(second, 88, SourceSide::Right)?;
    subscription.unsubscribe().await?;
    require_status(&client, DaemonState::Streaming, 1).await?;
    emit(json!({
        "phase": "rust_unsubscribed",
        "bpm": [first.bpm, second.bpm]
    }))?;

    wait_for_go()?;
    require_status(&client, DaemonState::Ready, 0).await?;
    drop(client);
    emit(json!({
        "phase": "pass",
        "scenario": "mixed",
        "event_count": 2
    }))
}

async fn basic(socket_path: &PathBuf) -> ProbeResult {
    let client = AirPodsClient::connect_to(socket_path).await?;
    let hello = client.hello().await?;
    require(hello.service == "airpods-hubd", "unexpected daemon service")?;
    require(
        hello.experimental,
        "daemon did not report experimental=true",
    )?;
    client.ping().await?;
    require_status(&client, DaemonState::Ready, 0).await?;

    let mut subscription = client.subscribe_heart_rate().await?;
    emit(json!({"phase": "subscribed"}))?;
    emit(json!({"phase": "interleave_ready"}))?;
    wait_for_go()?;

    let (status, first) = tokio::join!(client.status(), subscription.next());
    let status = status?;
    require(
        status.state == DaemonState::Streaming && status.subscriber_count == 1,
        "interleaved status response was invalid",
    )?;
    let first = required_sample(first?)?;
    require_sample(first, 169, SourceSide::Left)?;

    emit(json!({"phase": "remaining_events_ready"}))?;
    let mut samples = vec![first];
    for _ in 0..3 {
        samples.push(required_sample(subscription.next().await?)?);
    }
    let expected = [
        HeartRateSample {
            bpm: 169,
            source_side: SourceSide::Left,
        },
        HeartRateSample {
            bpm: 88,
            source_side: SourceSide::Right,
        },
        HeartRateSample {
            bpm: 88,
            source_side: SourceSide::Right,
        },
        HeartRateSample {
            bpm: 73,
            source_side: SourceSide::Unknown(37),
        },
    ];
    require(samples == expected, "heart-rate sequence did not match")?;

    subscription.unsubscribe().await?;
    require_status(&client, DaemonState::Ready, 0).await?;
    drop(client);
    emit(json!({
        "phase": "pass",
        "scenario": "basic",
        "event_count": samples.len(),
        "bpm": samples.iter().map(|sample| sample.bpm).collect::<Vec<_>>()
    }))
}

async fn two_clients(socket_path: &PathBuf) -> ProbeResult {
    let client_a = AirPodsClient::connect_to(socket_path).await?;
    let client_b = AirPodsClient::connect_to(socket_path).await?;
    let mut subscription_a = client_a.subscribe_heart_rate().await?;
    emit(json!({"phase": "client_a_subscribed"}))?;
    let mut subscription_b = client_b.subscribe_heart_rate().await?;
    emit(json!({"phase": "both_subscribed"}))?;
    emit(json!({"phase": "shared_events_ready"}))?;

    let expected = [
        HeartRateSample {
            bpm: 101,
            source_side: SourceSide::Left,
        },
        HeartRateSample {
            bpm: 102,
            source_side: SourceSide::Right,
        },
    ];
    for expected_sample in expected {
        require(
            required_sample(subscription_a.next().await?)? == expected_sample,
            "client A shared event mismatch",
        )?;
        require(
            required_sample(subscription_b.next().await?)? == expected_sample,
            "client B shared event mismatch",
        )?;
    }

    subscription_a.unsubscribe().await?;
    require_status(&client_a, DaemonState::Streaming, 1).await?;
    emit(json!({"phase": "client_a_unsubscribed"}))?;
    emit(json!({"phase": "client_b_event_ready"}))?;
    require_sample(
        required_sample(subscription_b.next().await?)?,
        83,
        SourceSide::Unknown(37),
    )?;

    subscription_b.unsubscribe().await?;
    require_status(&client_b, DaemonState::Ready, 0).await?;
    drop((client_a, client_b));
    emit(json!({
        "phase": "pass",
        "scenario": "two-clients",
        "shared_event_count": 2,
        "remaining_event_count": 1
    }))
}

async fn drop_resubscribe(socket_path: &PathBuf) -> ProbeResult {
    let client = AirPodsClient::connect_to(socket_path).await?;
    let subscription = client.subscribe_heart_rate().await?;
    emit(json!({"phase": "first_subscribed"}))?;
    wait_for_go()?;
    drop(subscription);

    let mut replacement = client.subscribe_heart_rate().await?;
    emit(json!({"phase": "replacement_subscribed"}))?;
    emit(json!({"phase": "replacement_event_ready"}))?;
    require_sample(
        required_sample(replacement.next().await?)?,
        90,
        SourceSide::Left,
    )?;
    replacement.unsubscribe().await?;
    require_status(&client, DaemonState::Ready, 0).await?;
    drop(client);
    emit(json!({
        "phase": "pass",
        "scenario": "drop-resubscribe",
        "event_count": 1
    }))
}

async fn disconnect(socket_path: &PathBuf) -> ProbeResult {
    let client = AirPodsClient::connect_to(socket_path).await?;
    let subscription = client.subscribe_heart_rate().await?;
    emit(json!({"phase": "subscribed"}))?;
    std::thread::spawn(move || drop(subscription))
        .join()
        .map_err(|_| invalid("subscription drop thread panicked"))?;
    require(
        matches!(client.status().await, Err(Error::ConnectionClosed)),
        "outside-runtime drop did not close the client",
    )?;
    drop(client);
    emit(json!({"phase": "pass", "scenario": "disconnect"}))
}

async fn require_status(
    client: &AirPodsClient,
    expected_state: DaemonState,
    expected_subscribers: u64,
) -> ProbeResult {
    let status = client.status().await?;
    require(
        status.state == expected_state && status.subscriber_count == expected_subscribers,
        "daemon status did not match",
    )
}

fn required_sample(sample: Option<HeartRateSample>) -> ProbeResult<HeartRateSample> {
    sample.ok_or_else(|| invalid("heart-rate subscription ended unexpectedly").into())
}

fn require_sample(sample: HeartRateSample, bpm: u8, source_side: SourceSide) -> ProbeResult {
    require(
        sample == HeartRateSample { bpm, source_side },
        "heart-rate sample did not match",
    )
}

fn wait_for_go() -> ProbeResult {
    let mut command = String::new();
    io::stdin().read_line(&mut command)?;
    require(command.trim() == "go", "expected go barrier command")
}

fn emit(value: Value) -> ProbeResult {
    let stdout = io::stdout();
    let mut stdout = stdout.lock();
    serde_json::to_writer(&mut stdout, &value)?;
    stdout.write_all(b"\n")?;
    stdout.flush()?;
    Ok(())
}

fn require(condition: bool, message: &'static str) -> ProbeResult {
    if condition {
        Ok(())
    } else {
        Err(invalid(message).into())
    }
}

fn invalid(message: &'static str) -> io::Error {
    io::Error::other(message)
}
