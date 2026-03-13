# Client SDK v0.1 API contract

This document freezes the first supported application-facing surface for the
Rust `airpods-client` crate and Python `airpods-client` distribution. Patch
releases in the v0.1 line should not intentionally break this surface. A future
v0.2 may contain deliberate breaking changes when they are documented and
versioned.

This is not a 1.0 or permanent-compatibility promise. IPC protocol version 1
remains experimental inside the coordinated SDK and `airpods-hubd` ecosystem.
The SDKs validate the daemon's protocol version and do not promise
compatibility with arbitrary future daemon versions.

## Shared behavior

Both clients require an already-running `airpods-hubd`. They communicate only
through a Unix socket and never start, stop, restart, or reconnect the daemon.
They do not call systemd or own Bluetooth, BlueZ, AAP, pairing, or controller
handoff resources.

The default connection resolves exactly
`$XDG_RUNTIME_DIR/airpods-hubd.sock`. Missing `XDG_RUNTIME_DIR` is a typed
error. Each SDK also accepts an explicit Unix socket path and has no `/tmp`,
TCP, or localhost fallback.

The supported operations are `hello`, `ping`, `status`, heart-rate subscribe,
heart-rate iteration, and explicit unsubscribe. One reader separates events
from serialized request responses. Both clients preserve BPM values, event
order, and duplicates without filtering. Source side means only the daemon
field: `left`, `right`, or `unknown` with its raw byte. It does not mean worn
side, connected side, or skin contact.

Daemon disconnect is terminal for a connection. Pending operations and active
subscriptions end with a typed connection error. There is no automatic
reconnect or request-timeout policy in v0.1.

## Rust surface

The supported crate root is `airpods_client`. These public constants are part
of v0.1:

- `PROTOCOL_VERSION: u64`
- `MAX_FRAME_SIZE: usize`

`AirPodsClient` supports:

- `AirPodsClient::default_socket_path()`
- `AirPodsClient::connect().await`
- `AirPodsClient::connect_to(path).await`
- `hello().await`
- `ping().await`
- `status().await`
- `subscribe_heart_rate().await`

Dropping the final `AirPodsClient` handle closes its Unix connection. Rust does
not expose a separate asynchronous client-close method in v0.1.

`HeartRateSubscription` supports `next().await` and the consuming
`unsubscribe().await`. `next()` returns `Result<Option<HeartRateSample>,
Error>`. Explicit unsubscribe is the confirmed cleanup path; Drop is
nonblocking and follows the documented best-effort/connection-invalidation
lifecycle.

The supported Rust models and fields are:

- `Hello { service: String, experimental: bool }`
- `Status { state: DaemonState, subscriber_count: u64 }`
- `HeartRateSample { bpm: u8, source_side: SourceSide }`
- `SourceSide::{Left, Right, Unknown(u8)}`
- `DaemonState::{Stopped, Starting, Ready, StartingHeartRate, Streaming,
  StoppingHeartRate, Failed, ShuttingDown, Unknown(String)}`

`Error` is non-exhaustive and retains the v0.1 typed categories
`XdgRuntimeDirMissing`, `Connect`, `Io`, `FrameTooLarge`, `InvalidJson`,
`ProtocolVersion`, `UnexpectedMessage`, `DaemonError`, `ConnectionClosed`,
`SubscriptionActive`, and `EventLagged`. Their currently public named fields
remain available. Consumers must retain a wildcard match because new error
categories may be added without breaking the v0.1 contract.

## Python surface

The supported import root is `airpods_client`. Its explicit `__all__` contains:

- `AirPodsClient`
- `AirPodsClientError`
- `ConnectionClosed`
- `ConnectionFailed`
- `DaemonError`
- `DaemonState`
- `EventBufferFull`
- `FrameTooLarge`
- `HeartRateSample`
- `HeartRateSubscription`
- `Hello`
- `InvalidMessage`
- `ProtocolVersionError`
- `SourceSide`
- `Status`
- `SubscriptionActive`
- `XdgRuntimeDirMissing`

`AirPodsClient` supports the async class methods `connect()` and
`connect_to(path)`, the async methods `hello()`, `ping()`, `status()`,
`subscribe_heart_rate()`, and idempotent `close()`, plus the async context
manager protocol. Because connection construction is asynchronous, the v0.1
context form is:

```python
async with await AirPodsClient.connect() as client:
    ...
```

`HeartRateSubscription` is an async iterator and async context manager. It
supports `next()`, `unsubscribe()`, and idempotent `close()`. Explicit cleanup
is the supported lifecycle boundary; Python finalizers perform no network I/O.

The frozen dataclass fields are:

- `Hello(service: str, experimental: bool)`
- `Status(state: DaemonState, subscriber_count: int)`
- `HeartRateSample(bpm: int, source_side: SourceSide,
  source_side_raw: int | None = None)`

`SourceSide` exports `LEFT`, `RIGHT`, and `UNKNOWN`. For `UNKNOWN`,
`HeartRateSample.source_side_raw` retains the daemon byte. `DaemonState`
exports `STOPPED`, `STARTING`, `READY`, `STARTING_HEART_RATE`, `STREAMING`,
`STOPPING_HEART_RATE`, `FAILED`, and `SHUTTING_DOWN` with the protocol-v1
string values.

All exported exception types derive from `AirPodsClientError`. `DaemonError`
retains `code` and `message`; `ProtocolVersionError` retains `expected` and
`received`; `ConnectionFailed` retains `path` and `cause`; and
`FrameTooLarge` retains `limit`.

## Outside the contract

The following are not part of the v0.1 SDK contract:

- crate-private items and Python implementation modules or underscored names
- daemon-private Python modules and production session objects
- Bluetooth, BlueZ, D-Bus, AAP, controller-handoff, or pairing behavior
- repository test probes and integration-probe command syntax
- raw Bluetooth packets, AAP frames, or parser implementation details
- meanings for flags, auxiliary fields, or timestamp units
- smoothing, deduplication, medical validity, or signal-quality claims
- automatic reconnect, daemon autostart, or systemd control
- daemon installation paths or production systemd packaging
- external registry-name availability or publication policy

The Python distribution and Rust crate are release candidates built locally in
Client SDKD. Publication, public daemon distribution, and coordinated future IPC
versioning remain separate gates.
