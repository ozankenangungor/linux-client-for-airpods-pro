# airpods-client

`airpods-client` is an experimental Rust client for the local
`airpods-hubd` Unix JSONL interface. It does not open Bluetooth, start the
daemon, or reconnect automatically. The API and protocol compatibility are
not yet stable, and the crate is not published.

```rust,no_run
use airpods_client::AirPodsClient;

# async fn example() -> Result<(), airpods_client::Error> {
let client = AirPodsClient::connect().await?;
let status = client.status().await?;
println!("{:?}", status.state);

let mut heart_rate = client.subscribe_heart_rate().await?;
if let Some(sample) = heart_rate.next().await? {
    println!("{} bpm ({})", sample.bpm, sample.source_side);
}
heart_rate.unsubscribe().await?;
# Ok(())
# }
```

The default socket is `$XDG_RUNTIME_DIR/airpods-hubd.sock`. Use
`AirPodsClient::connect_to` to select an explicit socket in tests or during
development. Missing `XDG_RUNTIME_DIR` and an absent daemon are returned as
errors; the client has no `/tmp`, TCP, or service-start fallback.
