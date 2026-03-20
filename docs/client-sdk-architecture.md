# Experimental client SDK architecture

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

Language SDKs use the same daemon boundary instead of linking to Bluetooth
ownership code. Client SDKC adds the first Python client beside the Rust crate.
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

## Client SDKC Python SDK foundation

Client SDKC adds the separate `airpods_client` Python package. It is a
stdlib-only asyncio client for the daemon's Unix JSONL socket. It does not
import `airpods_hr`, hubd implementation modules, Bumble, D-Bus, BlueZ, or the
production session, and it never starts or reconnects the daemon. The default
socket is exactly `$XDG_RUNTIME_DIR/airpods-hubd.sock`; tests and development
can provide an explicit Unix path. Missing runtime configuration and an absent
daemon produce distinct client exceptions without `/tmp` or TCP fallback.

```python
from airpods_client import AirPodsClient

async with await AirPodsClient.connect() as client:
    async with await client.subscribe_heart_rate() as heart_rate:
        async for sample in heart_rate:
            print(sample.bpm, sample.source_side.value)
```

The Python connection has one bounded reader task and one serialized request
path. Protocol version 1 has no request IDs, so a shielded request transaction
retains the request lock until its response arrives even when the calling
coroutine is cancelled. Heart-rate events use a separate bounded route and can
arrive before or between responses without satisfying a request. JSON payloads
are limited to 4096 bytes and validated as UTF-8 protocol-v1 objects before
typed models are constructed.

Python subscriptions use the same externally visible lifecycle rule as Rust:

```text
IDLE -> SUBSCRIBING(generation) -> ACTIVE(generation)
                                  |
                                  v
                         CLEANING(generation) -> IDLE
```

Cancelling subscribe transfers its provisional generation to an asyncio
cleanup task. Cancelling an explicit close does not cancel that task. A later
subscription waits for the old subscribe response, idempotent unsubscribe
response, and event-route removal. If confirmed cleanup fails, the client
connection becomes terminal. An old unsubscribe therefore cannot affect a new
generation, and events queued for a cancelled generation cannot appear in its
replacement. `HeartRateSubscription.close()` and `unsubscribe()` are explicit,
idempotent async cleanup methods; the async context managers call them without
depending on Python finalizers. Closing the client with an active subscription
closes the socket, allowing hubd to remove that client and stop HR when it was
the final subscriber.

`DaemonState`, `Hello`, `Status`, `SourceSide`, and `HeartRateSample` preserve
the same protocol meanings as the Rust SDK. Both keep event order and
duplicates, accept BPM 169 as ordinary data, map left and right exactly, and
retain raw value 37 for an unknown source side. Both report structured daemon
errors and terminal disconnects through client-specific typed errors. The
internal task and ownership structures differ because Rust and asyncio have
different cancellation models; behavioral parity applies to these observable
results.

Repository integration tests connect Python clients to the actual
`AirPodsHubDaemon` with one injected `FakeSensorSession`. They cover protocol
operations, real daemon event encoding, response/event interleaving,
disconnect cleanup, and two Python clients sharing one sensor START. A mixed
scenario connects one Rust client and one Python client to that same daemon.
Both receive the same ordered reports; removing Rust leaves Python streaming,
and removing Python performs the single final STOP while the fake session stays
open until daemon shutdown. These tests construct no production session and do
no Bluetooth or systemd work.

The Rust and Python SDK APIs and IPC version 1 remain experimental and
unpublished. Automatic reconnect is still deferred. Client SDKC is a source and
fake-session integration gate; a later gate must review and validate the SDK
surface before any public API freeze.

### Client SDKC FINAL PASS

Testing installed the standalone experimental Python SDK in the development
environment and ran `examples/python_heart_rate.py` against the production
daemon through the systemd user service with real AirPods Pro 3. The daemon
reached `READY` after BlueZ preflight, profile registration, transport open,
and descriptor handshake while `Device1.Connected` remained true. The Python
client reported daemon state `ready` and received 169, 125, 65, 66, 66, 65,
65, 65, 65, and 64 BPM from the left source before exiting successfully. BPM
169 and duplicate samples remained ordinary unfiltered data.

Stopping the service delivered `SIGTERM`, completed production cleanup,
removed the socket, released the process lock, and left BlueZ reachable, the
adapter powered, and `Device1.Connected` true. The human operator confirmed
that A2DP music remained uninterrupted throughout. Client SDKC is therefore
FINAL PASS across the real Python application, standalone Python client, Unix
IPC, systemd daemon, production session, and AirPods hardware stack.

This evidence does not publish or freeze a 1.0 API, promise permanent IPC
version 1 compatibility, add automatic reconnect, or make the SDK responsible
for starting the daemon.

## Client SDKD v0.1 SDK surface and packaging

Client SDKD freezes the supported first client surface documented in
`docs/sdk-v0.1-api.md`. The existing hardware-proven connection, request,
event, error, and subscription behavior remains intact. Patch releases in the
v0.1 line should not intentionally break that documented surface. A future
v0.2 may make deliberate breaking changes. This is not a 1.0 promise, and IPC
protocol version 1 remains experimental and subject to coordinated daemon/SDK
versioning.

The authoritative Python source moves to
`packages/airpods-client-python/src/airpods_client`. Its independent
`airpods-client` distribution is version 0.1.0, uses setuptools packaging, and
has no runtime dependencies. The root `airpods-hr-linux` production
distribution continues to discover `src/airpods_hr` and retain its daemon and
Bluetooth dependencies; it does not package a second client copy. Repository
tests add the standalone source directory explicitly rather than relying on
the production distribution.

The Rust `airpods-client` crate remains version 0.1.0 with the accepted public
API and dependency set. Its package metadata now identifies its README and
excludes the repository-only cross-language integration probe. Local Cargo
packaging and an external consumer compile validate it without publishing.

Both release-candidate SDK packages require `airpods-hubd` to be running and
own no Bluetooth or daemon lifecycle. Their hardware evidence is already
complete: Client SDKB proved the Rust SDK through the real production stack, and
Client SDKC proved the Python SDK through the same boundary while A2DP remained
uninterrupted. Client SDKD performs no additional hardware or production-service
run.

Before public release, maintainers must separately choose and verify registry
names, perform the publication gate, and package the daemon and production
systemd paths. Automatic reconnect, a deliberate request-timeout policy, and
future IPC compatibility remain outside this SDK packaging gate.

### Client SDKD and Client SDK FINAL PASS

Client SDKD is accepted at repository commit
`1514a01d2b2234569d5f80d80f491ef72f4a96b7`. The accepted repository ZIP has
SHA-256 `e3cb874f93a756adbc10e3596f5713ebf9f487bca62177d9f9eebf7bcdff1ea2`.

The standalone Python `airpods-client` 0.1.0 release candidate has no runtime
dependencies. Its accepted wheel SHA-256 is
`7beaf02ac14fda947a1cf308886b9cde4a8d174987955b4bd44291a9c2274cac`, and its
accepted sdist SHA-256 is
`45313422fe09d3aefe2e44aeb01e8cefbfbcbd406e1843a11c88cfb223b199b1`. The
accepted Rust `airpods-client` 0.1.0 crate SHA-256 is
`33bb9f5015042363050e587a8ff339cebce13542fa92e0d4f1045903226b872a`.

Tasks 9.9A, 9.9B, 9.9C, and 9.9D, and therefore Client SDK as a whole, are FINAL
PASS. The Rust and Python SDK release candidates remain unpublished. This
closure freezes the documented v0.1 application surface without making a 1.0
stability promise, stabilizing IPC protocol version 1, or adding automatic
reconnect, Bluetooth ownership, daemon startup, or systemd control to either
SDK.
