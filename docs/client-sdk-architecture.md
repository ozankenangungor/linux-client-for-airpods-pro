# Experimental Rust client SDK foundation

Client SDKA adds the unpublished `airpods-client` crate as the first reusable
application boundary for `airpods-hubd`. Its API and IPC compatibility remain
experimental. This task does not promise semantic-version stability, a 1.0
release, or permanent protocol version 1 support.

## Client SDKA FINAL PASS

Client SDKA is accepted at commit
`e71a253b931e555bae47e0917975ce9e2d9410b3`. Hardware-independent validation
proved bounded protocol-v1 framing, response/event multiplexing, exact event
ordering and duplicates, typed failures, and a cancellation-safe generated
subscription lifecycle. Cleanup of one generation completes or invalidates the
connection before a replacement can activate. The Rust workspace passed 41
tests, including 28 client tests, and the cancellation suite passed ten
consecutive runs. The complete 692-test Python suite also passed. The crate
remains experimental and unpublished.

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

## Client SDKB real daemon integration

Client SDKB adds a repository-only cross-language gate. Python instantiates the
actual `AirPodsHubDaemon` with one injected `FakeSensorSession` in a temporary
private directory, builds the Rust probe once, and launches it with an explicit
Unix socket path. The probe at
`crates/airpods-client/examples/integration_probe.rs` uses only the public
experimental client API. It is test infrastructure, not a stable user CLI.

The basic scenario sends `hello`, `ping`, `status`, `subscribe`, and
`unsubscribe` through the real daemon. Fake `HeartRateReport` objects pass
through the daemon's existing event encoder and arrive in Rust as 169 left, 88
right, a duplicate 88 right, and 73 unknown with raw source 37. A test-only
subclass wraps the real status dispatch with asyncio barriers and observes the
real outbound enqueue operation. This makes an event precede an in-flight
status response deterministically and proves that the Rust reader loses or
misclassifies neither message across the language boundary. The subclass calls
the accepted daemon implementation for framing, dispatch, encoding, and client
lifecycle behavior; it does not implement alternate protocol semantics.

A second scenario connects two independent Rust clients to the same daemon.
Both subscriptions share one fake-session START and receive the same reports.
Removing client A leaves client B streaming without STOP; removing B performs
the single STOP and returns the daemon to `READY` while the fake session remains
open. Additional scenarios prove that Rust's nonblocking Drop cleanup permits a
later generation on the same connection and that an active outside-runtime
Drop closes the connection, causing the real daemon to remove the last
subscriber and stop HR without closing the sensor session.

All subprocesses use explicit argument arrays, bounded waits and output, and
failure cleanup. The harness never constructs the production session, calls
BlueZ or systemd, or opens Bluetooth. The accepted Client SDKA external types did
not expose an ownership blocker during this review and remain unchanged. The
crate and IPC are still experimental. Real AirPods validation of the Rust
client remains pending after source review.

### Client SDKB FINAL PASS

The hardware-independent cross-language suite passed three consecutive runs.
It proved one real daemon and one fake session across the basic, two-client,
Drop/resubscribe, and active-disconnect scenarios, including deterministic
event/response interleaving. The complete Python suite passed 698 tests and the
Rust workspace retained its 41 passing tests.

Testing subsequently built the real `heart_rate` Rust example and ran it
against the production daemon through the systemd user service with real
AirPods Pro 3. The service reached `READY` after BlueZ preflight, profile
registration, transport open, and descriptor handshake while
`Device1.Connected` remained true. The Rust client reported daemon state
`Ready` and received 169, 137, 93, 75, 72, 71, 71, 71, 71, and 70 BPM, all
from the right source, before exiting successfully. This preserved BPM 169 and
the repeated 71 BPM samples without filtering or deduplication.

Stopping the user service delivered `SIGTERM`, completed production cleanup,
removed the socket, released the process lock, and left BlueZ reachable, the
adapter powered, and `Device1.Connected` true. The human operator confirmed
that normal A2DP music remained uninterrupted throughout. Client SDKB is
therefore FINAL PASS across the real Rust application, Rust client, Unix IPC,
systemd daemon, production session, and AirPods hardware stack.

This evidence does not freeze the Rust API or IPC version 1, publish the crate,
or add automatic daemon reconnect. Those surfaces remain experimental.
