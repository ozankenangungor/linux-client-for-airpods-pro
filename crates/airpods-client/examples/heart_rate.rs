use airpods_client::AirPodsClient;

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = AirPodsClient::connect().await?;
    let status = client.status().await?;
    println!("daemon state: {:?}", status.state);

    let mut heart_rate = client.subscribe_heart_rate().await?;
    for _ in 0..10 {
        let Some(sample) = heart_rate.next().await? else {
            break;
        };
        println!("{} bpm ({})", sample.bpm, sample.source_side);
    }
    heart_rate.unsubscribe().await?;
    Ok(())
}
