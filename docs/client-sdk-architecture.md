# Experimental Rust client SDK foundation

Client SDKA adds the unpublished `airpods-client` crate as the first reusable
application boundary for `airpods-hubd`. Its API and IPC compatibility remain
experimental. This task does not promise semantic-version stability, a 1.0
release, or permanent protocol version 1 support.

## Boundary and ownership

Applications talk to the daemon because the proven AirPods path depends on one
persistent production session, one AAP channel, and one sensor reader. If each
application opened Bluetooth independently, it could compete for the same
transport and descriptor bootstrap. The client crate therefore has no BlueZ,
D-Bus, Bluetooth socket, Bumble, PyO3, controller-handoff, or production-session
dependency.

```text
application / game
        |
        v
 airpods-client             future Python / Unity / C# / JS clients
        |                                  |
        +---------- Unix JSONL ------------+
                           |
                           v
                    +-------------+
                    | airpods-hubd |
                    +-------------+
                           |
                 one persistent AirPods session
                           |
                           v
                        AirPods
```

Future language bindings should use the same daemon boundary instead of
linking to Bluetooth ownership code. A Python client can be added later, and
Unity, C#, or JavaScript consumers can implement or wrap the local IPC after a
supported SDK surface is selected.

## Connection lifecycle

`AirPodsClient::connect()` resolves exactly
`$XDG_RUNTIME_DIR/airpods-hubd.sock`. Missing `XDG_RUNTIME_DIR` is a typed
error. `AirPodsClient::connect_to()` accepts an explicit Unix path for tests and
development. The crate never falls back to `/tmp`, TCP, or a localhost port.
It does not launch, restart, or retry the daemon, call `systemctl`, or touch
Bluetooth. An absent daemon produces a connection error.

The client uses Tokio only for Unix socket I/O, tasks, locks, and channels.
`serde_json` handles JSON values. There is no HTTP, WebSocket, D-Bus, BlueZ, or
unrelated application framework.

Disconnect is terminal in Client SDKA. It completes a pending request with a
deterministic error and ends an active subscription with an error. It does not
reconnect or create a replacement daemon or sensor session.

## Protocol and multiplexing

The client implements experimental protocol version 1 operations `hello`,
`status`, `ping`, `subscribe heart_rate`, and `unsubscribe heart_rate`. Every
inbound message must be one JSON object terminated by a newline and carry the
supported protocol version. The reader accumulates at most the daemon's
4096-byte payload limit. It rejects an oversized frame, malformed JSON,
non-object JSON, unsupported versions, invalid fields, and messages that are
neither a response nor a heart-rate event.

One task is the sole socket reader. It validates each frame and routes it to
either the pending request or the bounded heart-rate event channel:

```text
                         one Unix stream
                               |
                         bounded reader task
                          /              \
                  request response    heart_rate event
                         |                   |
                  pending request      subscription
```

Protocol version 1 has no request identifier. The client does not invent one:
it permits one in-flight request per connection and serializes later requests.
The serialization guard stays with the pending response even if the caller
cancels its request future, so a late response cannot be correlated with a
newer request. Events may arrive before, between, or after responses without
being consumed as responses or reordered.

Subscription transitions have a separate serialized lifecycle and monotonic
local generations. A generation installs its event route while holding the
lifecycle gate. Successful activation releases the gate but retains generation
ownership in `HeartRateSubscription`. Teardown marks that generation as
cleaning before it sends `unsubscribe`, and a later generation waits until the
response is consumed and the old event route is removed. Cleanup from an old
generation therefore cannot unsubscribe a newer subscription or deliver its
queued events through the newer route.

Cancelling subscribe while it waits for request serialization, or after its
request is sent, drops a provisional generation owner. That owner transfers
the lifecycle gate to a cleanup task. An idempotent unsubscribe waits behind
the cancelled request, so the late subscribe response is consumed first.
Cancelling explicit unsubscribe follows the same rule: the subscription keeps
cleanup ownership across the await and transfers it on cancellation. A
fail-safe guard invalidates the connection if an internally scheduled cleanup
task is cancelled before it can establish a deterministic remote state.

## Heart-rate subscription and data

`subscribe_heart_rate()` creates one `HeartRateSubscription` on the connection.
`next()` yields events in daemon order, including duplicates and BPM 169.
`HeartRateSample` exposes only `bpm` and `source_side`, the fields present in
the current daemon event. `SourceSide` maps `left`, `right`, and the daemon's
`unknown` plus `source_side_raw` encoding. It makes no claim about which bud is
worn, connected, or touching skin.

The client adds no smoothing, filtering, deduplication, medical validity,
signal quality, contact confidence, timestamp units, auxiliary-field meaning,
or flag meaning. Its event queue is bounded. A lagging consumer receives a
typed lag error instead of allowing memory to grow without limit.

`HeartRateSubscription::unsubscribe()` is the reliable lifecycle endpoint: it
waits for the daemon response and releases the local event route. `Drop` never
performs blocking network I/O. Inside a current Tokio context it schedules a
best-effort unsubscribe and keeps the generation in its cleaning state until
that operation finishes. A replacement subscription waits for this teardown.

Outside a current Tokio context, `Drop` cannot guarantee that an async cleanup
task will run. It therefore marks the whole client connection closed and calls
nonblocking Unix `shutdown` on a cloned socket handle. All later client methods
return `ConnectionClosed`, the daemon observes disconnection and performs its
normal subscriber cleanup, and the client never appears reusable with an
orphaned subscription. Applications that require a confirmed unsubscribe
response must call the async method explicitly. No subscription operation opens
a second socket or sensor path.

## Error model

The non-exhaustive `Error` enum separates missing runtime configuration,
connect and I/O failures, oversized frames, malformed JSON, unsupported
protocol versions, unexpected message shapes, structured daemon errors,
connection closure, an already-active subscription, and bounded-channel lag.
Daemon error codes and safe messages remain available without exposing daemon
session or Bluetooth implementation objects.
