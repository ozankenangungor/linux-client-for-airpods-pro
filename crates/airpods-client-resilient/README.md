# airpods-client-resilient 0.1.0

This experimental, unpublished Rust crate provides an explicit opt-in heart-rate stream for applications that want finite reconnect to an already running `airpods-hubd` Unix socket. The base `airpods-client` v0.1 API remains terminal on disconnect.

`ResilientHeartRateStream::default_socket(policy)` uses the base client's default socket resolution. `explicit_socket(path, policy)` selects a path. The daemon must be started and managed externally. This crate does not start or restart it, use systemd, or own Bluetooth resources.

The default `ReconnectPolicy` retries after 1, 2, 5, 10, and 10 seconds, without jitter. Custom schedules must contain 1 to 16 positive delays. Exhaustion returns typed `Error::RetryExhausted` with the last low-level error. Only specified connection failures are retried; protocol, configuration, daemon rejection, and consumer lag errors terminate the stream.

`next()` yields unchanged `Sample` values and explicit `Reconnecting` and `Reconnected` events. `Reconnected` means both a replacement connection and heart-rate subscription succeeded. These events mark a gap; BPM continuity across daemon outages is not implied. Values, duplicates, event order within a live connection, and unknown source bytes are preserved.

`close()` permanently disables reconnect and awaits confirmed unsubscribe if active. During backoff it returns without connecting. The wrapper has no background reconnect task; dropping it performs no blocking I/O. The base client's subscription Drop behavior applies.
