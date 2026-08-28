use airpods_client::{AirPodsClient, Error, MAX_FRAME_SIZE, PROTOCOL_VERSION};
use std::path::Path;

async fn api() -> Result<(), Error> {
    let client = AirPodsClient::connect_to(Path::new("/tmp/not-opened")).await?;
    let _ = client.hello().await?;
    let _ = client.ping().await?;
    let _ = client.status().await?;
    let mut subscription = client.subscribe_heart_rate().await?;
    let _ = subscription.next().await?;
    subscription.unsubscribe().await?;
    Ok(())
}

fn main() {
    let _ = (PROTOCOL_VERSION, MAX_FRAME_SIZE, api);
}
