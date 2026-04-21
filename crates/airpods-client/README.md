# airpods-client

`airpods-client` 0.1 is the Rust SDK for Linux Client for AirPods Pro. It uses
the local `airpods-hubd` Unix JSONL interface. The daemon must already be running.
The crate does not open Bluetooth, start the daemon, call systemd, or reconnect
automatically. The crate is unpublished; use `crates/airpods-client` as a local
path dependency until publication.

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

Heart-rate subscription transitions are serialized. Cancelling subscribe or
unsubscribe cannot release its generation for reuse until cleanup completes.
Dropping an active subscription inside a Tokio runtime schedules a nonblocking
best-effort unsubscribe. If it is dropped outside a current Tokio context, the
client closes and becomes unusable so it cannot retain a silently orphaned
subscription. Call `unsubscribe().await` when confirmed cleanup is required.

Patch releases in the v0.1 line should not intentionally break the documented
surface. A future v0.2 may make deliberate breaking changes. Protocol version
1 remains experimental and may evolve through coordinated daemon and SDK
versioning; compatibility with arbitrary future daemon versions is not
promised.
