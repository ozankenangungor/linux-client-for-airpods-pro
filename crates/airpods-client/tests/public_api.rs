#![forbid(unsafe_code)]

//! External compile contract for the supported `airpods-client` v0.1 surface.

use airpods_client::{
    AirPodsClient, DaemonState, Error, HeartRateSample, HeartRateSubscription, Hello,
    MAX_FRAME_SIZE, PROTOCOL_VERSION, SourceSide, Status,
};
use std::path::{Path, PathBuf};

async fn frozen_usage(path: &Path) -> Result<(), Error> {
    let client = AirPodsClient::connect_to(path).await?;
    let Hello {
        service,
        experimental,
    } = client.hello().await?;
    let _hello_fields: (String, bool) = (service, experimental);
    client.ping().await?;
    let Status {
        state,
        subscriber_count,
    } = client.status().await?;
    let _status_fields: (DaemonState, u64) = (state, subscriber_count);

    let mut heart_rate: HeartRateSubscription = client.subscribe_heart_rate().await?;
    if let Some(HeartRateSample { bpm, source_side }) = heart_rate.next().await? {
        let _sample_fields: (u8, SourceSide) = (bpm, source_side);
    }
    heart_rate.unsubscribe().await?;
    drop(client);
    Ok(())
}

#[test]
fn v01_public_surface_compiles_from_an_external_test_crate() {
    let _default_socket: fn() -> Result<PathBuf, Error> = AirPodsClient::default_socket_path;
    let _default_connect = AirPodsClient::connect;
    let _usage = frozen_usage;
    let _protocol: u64 = PROTOCOL_VERSION;
    let _frame_limit: usize = MAX_FRAME_SIZE;

    let sides = [SourceSide::Left, SourceSide::Right, SourceSide::Unknown(37)];
    assert_eq!(sides[2].to_string(), "unknown(37)");

    let states = [
        DaemonState::Stopped,
        DaemonState::Starting,
        DaemonState::Ready,
        DaemonState::StartingHeartRate,
        DaemonState::Streaming,
        DaemonState::StoppingHeartRate,
        DaemonState::Failed,
        DaemonState::ShuttingDown,
        DaemonState::Unknown("future".to_owned()),
    ];
    assert_eq!(states.len(), 9);

    let errors = [
        Error::XdgRuntimeDirMissing,
        Error::Connect {
            path: PathBuf::from("socket"),
            kind: std::io::ErrorKind::NotFound,
            message: "missing".to_owned(),
        },
        Error::Io {
            context: "reading",
            kind: std::io::ErrorKind::BrokenPipe,
            message: "closed".to_owned(),
        },
        Error::FrameTooLarge {
            limit: MAX_FRAME_SIZE,
        },
        Error::InvalidJson {
            message: "invalid".to_owned(),
        },
        Error::ProtocolVersion {
            expected: PROTOCOL_VERSION,
            received: Some(2),
        },
        Error::UnexpectedMessage {
            message: "unexpected".to_owned(),
        },
        Error::DaemonError {
            code: "service_unavailable".to_owned(),
            message: "unavailable".to_owned(),
        },
        Error::ConnectionClosed,
        Error::SubscriptionActive,
        Error::EventLagged { skipped: 1 },
    ];
    assert_eq!(errors.len(), 11);
}
