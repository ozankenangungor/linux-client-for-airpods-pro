# Persistent local sensor daemon foundation

Daemon serviceA establishes the hardware-independent foundation of `airpods-hubd`.
It uses injected fake sessions and never opens Bluetooth. The package lives at
`airpods_hr._hubd`, remains private, and has no installed console script or
public import from `airpods_hr`.

## Daemon serviceA FINAL PASS

Daemon serviceA is accepted at commit
`7239172be6d3be4189f00970756f84cccd6b7e8b`. Hardware-independent validation
proved one persistent injected session, subscriber-arbitrated HR lifecycle,
one report reader with bounded multi-client fan-out, same-UID private Unix IPC,
safe listener ownership, and a full-lifetime cross-process kernel lock. The
accepted implementation performed no Bluetooth access and froze no public API.

## Why one daemon owns the AAP session

Hardware validation found that one long-lived AAP PSM `0x1001` connection can
reuse one descriptor bootstrap across repeated heart-rate START/STOP cycles.
A newly opened AAP channel on the same BlueZ connection can receive the exact
AAP acknowledgement without receiving the descriptor bootstrap again. This
was true even when the first channel never started heart rate and after waits
of both 5 and 60 seconds. A normal BlueZ disconnect and reconnect has restored
bootstrap, and ordinary A2DP audio remains compatible.

Independent applications therefore must not each open and own an AAP channel.
The daemon is the one persistent owner, while local applications subscribe to
its sensor stream:

```text
                         AirPods
                            |
                    BlueZ / kernel L2CAP
                            |
                    one persistent session
                            |
                     +-------------+
                     | airpods-hubd |
                     +-------------+
                            |
                 one report-reader task
                            |
                    bounded fan-out
                    /       |       \
              client A  client B  client C
```

## Python control plane

The first daemon control plane is Python because the hardware-proven
`ProductionHeartRateSession` and its BlueZ/kernel orchestration are Python.
Rewriting that path in Rust would combine an architecture change with a
transport migration. Daemon serviceA instead defines a small injected session
boundary: `open()`, `start()`, `receive_report()`, `stop()`, and `close()`.
Tests and the private probe supply fakes. No daemon module imports or creates
the real production session; Daemon serviceB will add and validate that adapter.

Rust retains its Iteration 9.7 role. `airpods-aap-core` is the portable protocol
core and `_airpods_aap_core` is the private PyO3 parity bridge. The Python
parser remains authoritative, and no production parsing path changes. A
future Rust client crate should speak to the daemon instead of opening another
AAP connection.

## Lifecycle and arbitration

The daemon creates exactly one session object and calls `open()` exactly once
during startup. Successful startup reaches `READY` with the session still
open. Subscriber transitions are serialized by one asynchronous lifecycle
lock:

```text
STOPPED -> STARTING -> READY
                         |
             subscribers 0 -> 1
                         v
                    STARTING_HR
                         |
                         v
                     STREAMING
                         |
             subscribers 1 -> 0
                         v
                    STOPPING_HR
                         |
                         v
                       READY

any session failure -> FAILED
shutdown from any owned state -> SHUTTING_DOWN -> STOPPED
```

The `0 -> 1` transition calls `start()` once. More subscribers reuse the same
stream and reader task. The `1 -> 0` transition calls `stop()` once and returns
to `READY`; it never closes the session. A later subscription starts heart
rate again on the same object. Each connection owns its own idempotent
subscription flag, and disconnect removes only that connection's
subscription.

One daemon task calls `receive_report()`. It forwards each report unchanged to
every current subscriber. It does not smooth, filter, or deduplicate BPM, so
BPM 169 and duplicate reports remain ordinary events.

Shutdown is idempotent and bounds every session operation. If heart rate may
still be active, shutdown attempts STOP before canceling the report reader. It
then closes clients and the listener, closes the one session, removes only the
socket inode it created, and reaches `STOPPED`. No task is intentionally left
running.

## Experimental local IPC and security

Daemon service protocol version 1 is experimental and may change before public
release. It is newline-delimited JSON over a Unix domain socket, with one JSON
object per line. Every request, response, and heart-rate event carries
`protocol_version: 1`. The small command set is `hello`, `status`, `subscribe
heart_rate`, `unsubscribe heart_rate`, and `ping`.

Inbound frames are limited to 4096 bytes. Invalid JSON, non-object JSON,
missing or unknown operations, invalid streams, oversized frames, and
unsupported versions produce a bounded safe error or connection rejection.
The protocol never decodes pickle data, evaluates code, accepts arbitrary
Python objects, or exposes raw AAP commands.

Heart-rate events contain `bpm` and `source_side`. Controlled-high evidence
maps raw source field 1 to `left` and 2 to `right`. Other values become
`unknown` and retain the byte in the separate experimental
`source_side_raw` field. Events omit `aux`, `flags`, raw report bytes, medical
validity, and speculative timestamp units.

The intended path is `$XDG_RUNTIME_DIR/airpods-hubd.sock`. There is no fallback
to `/tmp`, TCP, HTTP, WebSocket, or another browser-accessible listener. The
runtime directory must be absolute, owned by the daemon UID, and have no group
or other permissions. The socket is changed to mode `0600` immediately after
bind. An existing path is removed only when it is an owned Unix socket inside
that protected directory and a bounded connection probe returns
`ECONNREFUSED`. A successful connection proves another daemon is active and
blocks startup before creating or opening a session. Timeouts and other
ambiguous errors fail closed. The path's type, owner, device, and inode are
checked again after the probe and before stale removal. Clean shutdown checks
the original device and inode before unlinking. On Linux, each accepted peer
is checked with `SO_PEERCRED` and its UID must equal the daemon UID. Unix user
isolation is the Daemon serviceA security boundary.

Daemon ownership has two separate layers. Process ownership uses a persistent
kernel `flock` on the sibling `airpods-hubd.lock` file. The daemon safely opens
that owned regular file without following symlinks, normalizes it to mode
`0600`, and acquires an exclusive nonblocking lock before inspecting or
changing the socket path. It holds the file descriptor throughout startup,
READY and STREAMING operation, failure cleanup, and shutdown. Subscriber count
does not affect this lock. Closing the descriptor after all owned resources
are released lets the kernel release the lock. The regular lock file remains
in the private runtime directory, so no unlink/recreation race or PID-based
stale-lock policy exists.

Socket-path ownership remains a separate defense. A Unix pathname alone is
not an atomic process mutex because another process can observe the interval
between `bind()` and `listen()`. While holding the process lock, the daemon
performs the active/stale checks, creates and manually binds the stream socket,
sets mode `0600`, and starts listening before constructing or opening the
sensor session. A concurrent bind reports ownership failure without another
unlink attempt. Only after the session opens does the daemon hand the
already-bound socket to asyncio with automatic pathname cleanup disabled.
If session open or asyncio handoff fails, the private listener is closed and
only its recorded pathname identity is eligible for cleanup.

Failures before session construction release the flock after cleaning any
listener created by that attempt. Once session construction has been attempted,
the daemon retains the flock in `FAILED` until explicit shutdown completes
best-effort session and socket cleanup. This prevents a new owner while a
possibly live or partially opened sensor session still exists. A failed
session close likewise retains the flock and leaves the daemon `FAILED`; a
later shutdown retry can release it only after close succeeds.

## Backpressure and failure behavior

Each client has a bounded outbound queue of 16 JSON messages. Fan-out uses
non-blocking queue insertion. A full queue deterministically disconnects that
slow client, so it cannot block sensor reads, other clients, or lifecycle
transitions. Other subscribers keep the session streaming; disconnecting the
last subscriber follows the normal `1 -> 0` STOP transition.

A START failure does not activate the requesting subscription or create a
reader task. Unexpected receive failure ends the reader, clears active
subscriptions, enters `FAILED`, and sends connected clients a generic service
error without packet data. A STOP failure enters `FAILED` and never claims
`READY`. Daemon serviceA does not retry, create another session or AAP channel,
reconnect Bluetooth, or define recovery. Shutdown still performs bounded,
best-effort cleanup of resources the daemon owns. Shutdown enters
`SHUTTING_DOWN` and closes the listening server before waiting on HR or client
cleanup, preventing new connections from entering while teardown is in
progress.

This foundation does not freeze a public IPC or client API. Later work may
revise the protocol before release, add a Rust client crate, and define other
client bindings. Daemon serviceB is reserved for the explicit real
`ProductionHeartRateSession` adapter and separately authorized hardware
validation.
