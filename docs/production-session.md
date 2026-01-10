# Persistent production session core

Iteration 9.6 is a final hardware pass. The production architecture is one
long-lived AAP channel, one descriptor bootstrap, and many HR START/STOP
cycles. This is an accepted lifecycle constraint, not an automatic recovery
policy.

Adds a private production-session core over the proven BlueZ/kernel
coexistence transport. It is implementation scaffolding for hardware validation
and does not freeze a public library API. The module is deliberately absent
from `airpods_hr.__init__`.

## Production session hardware result

Production session is a final hardware pass on AirPods Pro 3. One production session
opened through the BlueZ/kernel coexistence path, completed one descriptor
handshake, and then completed three HR activation/stop cycles on that same AAP
PSM `0x1001` channel. Each cycle returned five canonical reports. The final
counters were one transport open, one descriptor handshake, three HR
activations, three HR stops, and 15 reports.

BlueZ remained reachable, the adapter remained powered, and
`Device1.Connected` remained true after cleanup. Testing confirmed that
ordinary A2DP music played continuously without interruption throughout the
test. This proves persistent in-session reuse on real hardware.

## Why the AAP channel is long-lived

Coexistence probe proved that a kernel-managed Classic L2CAP channel to AAP PSM `0x1001`
can receive canonical heart-rate reports while BlueZ owns the controller and
ordinary AirPods audio continues. The socket must advertise a local receive
MTU (`imtu`) of 2048 through `SOL_L2CAP` / `L2CAP_OPTIONS` before connect. The
transport binds to the selected BlueZ adapter, requests `BT_SECURITY_MEDIUM`,
reads back the L2CAP options, verifies only `imtu` changed, connects to the
AirPods, and verifies the selected local route.

Semantics capture then proved repeated HR activation/stop cycles on one continuously
open AAP channel. Closing a successful AAP channel and immediately opening a
new channel on the same BlueZ ACL can sometimes yield the exact AAP ACK without
the descriptor catalog. A normal BlueZ disconnect/reconnect has restored the
catalog, but Production session does not treat that observation as a recovery policy.

The current production architecture therefore has two nested lifetimes:

```text
one persistent AAP session
  one kernel L2CAP PSM 0x1001 open
  one canonical descriptor handshake
  HR activation 1 -> reports -> canonical stop -> ready
  HR activation 2 -> reports -> canonical stop -> ready
  ...
  final session close
```

Every successful `open()` performs BlueZ preflight, temporary compatibility
registration when required, one corrected kernel transport open, one exact AAP
handshake, and normal descriptor-gated completion. It reaches `READY` without
activating HR. `start()` is accepted only from `READY` and reaches `STREAMING`
only after the canonical activation ACK. `stop()` uses canonical `STOP_HR`, its
acknowledgement, and `HR_OFF`, then returns to `READY` without closing the
transport or repeating descriptors. Final `close()` releases only session-owned
resources and is idempotent.

The audited active close order is canonical `STOP_HR`, stop acknowledgement,
`HR_OFF`, activation collector shutdown, L2CAP collection shutdown, kernel
socket close, temporary compatibility-profile cleanup, final BlueZ state
check, and D-Bus client cleanup. Closing from `READY` starts at the collection
and owned-resource cleanup steps because HR is already stopped. Production session
therefore performs every known canonical protocol shutdown action; Session reopen
does not invent another teardown frame.

The internal state machine is:

```text
CLOSED -> OPENING -> READY -> STARTING -> STREAMING
                         ^                    |
                         |------ STOPPING <---|

Any failed active operation -> FAILED
READY or STREAMING -> close() -> CLOSED
```

An object whose open or active operation failed may be closed for deterministic
cleanup, but it is not reopened. Callers construct a new internal object for a
new AAP channel. That new channel remains subject to the same-ACL descriptor
bootstrap limitation.

## Report and concurrency contract

The private `receive_report()` mechanism returns the existing
`HeartRateReport` object produced by the frozen canonical parser. It preserves
arrival order, exact 18-byte raw reports, duplicates, startup reports, and BPM
169. It performs no filtering, smoothing, suppression, field renaming, flag
interpretation, or source-side conversion.

One report consumer is supported at a time. A competing receive call fails
clearly instead of racing the stream. Cancellation removes its pending queue
wait, while the canonical activation task remains the sole consumer of the AAP
transport. Canonical receive polling and bounded stop timeouts prevent a
background socket worker from remaining indefinitely blocked during stop or
close.

## Failure and ownership boundaries

The core fails closed. Descriptor timeout remains a failed `open()` even when
the exact ACK was observed. It does not use ACK-only continuation, cache
descriptors across channels, open a replacement AAP channel, reconnect or
disconnect the BlueZ device, or fall back to Bumble. It never takes controller
ownership, reads LinkKeys, or reads `/var/lib/bluetooth`.

BlueZ remains controller owner. This layer does not select audio profiles,
pause media, control A2DP, open a microphone, or switch to HFP. Audio continuity
is external to the session core and remains an owner hardware observation.

## Private probe

The probe is a deterministic dry run unless `--execute` is present:

```console
cd /home/kenan/airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_production_session.py
```

The future owner validation command is:

```console
cd /home/kenan/airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_production_session.py \
  --execute \
  --cycles 3 \
  --samples-per-cycle 5 \
  --restart-delay 5 \
  --descriptor-timeout 30
```

Testing should begin with a fresh normal BlueZ reconnect and play ordinary
A2DP audio throughout the test. A passing probe reports one transport open,
one descriptor handshake, three HR activations, three canonical HR stops, and
15 reports. The probe does not print addresses, credentials, or raw HCI
security material.

## Session reopen: same-ACL new-channel characterization

Persistent reuse and new-channel reuse are separate lifecycle questions.
Session reopen adds a private, safe-by-default diagnostic that constructs two
independent production sessions in one process while deliberately preserving
the existing BlueZ ACL. Session 1 opens, completes descriptors, collects HR,
stops, and fully closes. Only then does session 2 use a newly constructed
session object and a new kernel AAP channel.

Session reopen passed its hardware characterization by cleanly reproducing the
limitation. Session 1 used a new production dependency graph, verified kernel
`imtu=2048` and the selected adapter route, completed the exact AAP ACK and
descriptor handshake, received five canonical HR reports, performed the
canonical stop and `HR_OFF` lifecycle, and fully closed. BlueZ remained
reachable, powered, and connected. After five seconds, session 2 used another
new dependency graph and socket on that unchanged BlueZ connection. Its
`imtu=2048` and route verification passed and its exact AAP ACK arrived, but
descriptor observation timed out after 27 post-ACK frames with zero receive
drops and none of the four required descriptor markers.

The measured totals were two session objects, transport opens and transport
closes; two descriptor-handshake attempts with one completion; two exact ACKs;
one HR activation and stop; five reports from session 1 and none from session
2. The result was `SESSION_2_EXACT_ACK_DESCRIPTOR_TIMEOUT`. Testing observed
uninterrupted A2DP audio throughout. This proves the new-channel phenomenon is
reproducible after eliminating local session, socket, collector, descriptor,
activation, and dependency-graph reuse. It does not prove that descriptors are
sent only once per ACL.

Every session owns fresh instances of its BlueZ client, compatibility
registration, kernel transport and socket, receive collector, canonical AAP
handshake and descriptor tracker, HR activation state, report queue, and
counters. No module-level or class-level descriptor completion state exists in
the production core. Descriptor evidence belongs to the AAP channel on which it
was observed and is never copied to the next channel.

The diagnostic records BlueZ reachability, adapter power, and
`Device1.Connected` before session 1, after session 1 closes, and immediately
before session 2 opens. It also records the verified local receive MTU and
selected-adapter route for each successfully opened transport. If the second
channel sees the exact ACK but no descriptor catalog, it reports
`SESSION_2_EXACT_ACK_DESCRIPTOR_TIMEOUT`, cleans up, and stops. Missing ACK,
transport failure, changed BlueZ state, and other failures remain distinct.

The probe does not retry, create a third channel, continue on ACK alone,
disconnect or reconnect BlueZ, or fall back to Bumble. It does not cache the
first session's descriptor result. Current production behavior therefore
continues to fail closed on a new channel whose descriptor bootstrap is
incomplete. Session reopen characterizes this behavior and does not claim its root
cause or install an automatic Bluetooth reconnect workaround.

The dry run performs no D-Bus or Bluetooth operations:

```console
cd /home/kenan/airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_session_reopen.py
```

The Session reopen owner characterization command was:

```console
cd /home/kenan/airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_session_reopen.py \
  --execute \
  --samples-per-session 5 \
  --reopen-delay 5 \
  --descriptor-timeout 30 \
  --verbose
```

No public API was frozen in Session reopen.

## Iteration 9.6C: descriptor-bootstrap state isolation

Iteration 9.6C varies only session 1 behavior and the same-ACL idle delay. The
default `hr-cycle` mode preserves Session reopen exactly: session 1 opens, completes
descriptors, starts HR, receives the requested reports, performs canonical
stop and `HR_OFF`, then closes. The `descriptor-only` mode opens session 1
through normal descriptor-gated READY and closes it without starting HR,
receiving reports, sending `STOP_HR`, or sending `HR_OFF`. Session 2 is normal
and identical in both modes: it uses fresh dependencies and a new AAP channel,
requires its own exact handshake and descriptor evidence, and validates HR only
if READY is reached.

Iteration 9.6C passed its hardware characterization. From a fresh normal BlueZ
connection, session 1 ran in `descriptor-only` mode: it opened a new production
session, received the exact AAP acknowledgement, completed the descriptor
catalog, reached READY, and closed. It did not start HR, receive HR reports,
send `STOP_HR`, or send `HR_OFF`.

After a five-second delay, session 2 used a completely fresh session graph and
new transport/socket on the unchanged BlueZ connection. It verified
`imtu=2048` and received the exact AAP acknowledgement, followed by 27 frames
with zero receive drops. None contained the sensor framework, heart-rate
service, heart-rate, or heartrate-access descriptor markers, so descriptor
gating timed out as designed. The result was
`SESSION_2_EXACT_ACK_DESCRIPTOR_TIMEOUT`.

After another fresh normal BlueZ reconnect, the descriptor-only experiment was
repeated with a 60-second reopen delay. Session 2 again received the exact AAP
acknowledgement and 27 post-ACK frames with zero receive drops, but none of the
four required descriptor markers. The result was again
`SESSION_2_EXACT_ACK_DESCRIPTOR_TIMEOUT`. Ordinary A2DP audio remained
uninterrupted during both tests.

Together with Production session and Session reopen, the final evidence is:

**Proven:**

- Three repeated HR activation/stop cycles work on one continuously open AAP
  PSM `0x1001` channel after one descriptor handshake. The measured totals were
  `transport_opens=1`, `descriptor_handshakes=1`, `hr_activations=3`,
  `hr_stops=3`, and `reports_received=15`.
- BlueZ remains controller owner, `Device1.Connected` remains true, and
  ordinary A2DP audio can continue without interruption during that persistent
  session.
- A new AAP channel on the same BlueZ connection can receive the exact AAP ACK
  yet omit the required descriptor catalog. In the characterized failures it
  delivered 27 post-ACK frames with zero receive drops and none of the sensor
  framework, heart-rate service, heart-rate, or heartrate-access markers.
- HR activation, HR report reception, `STOP_HR`, and `HR_OFF` are not necessary
  to trigger that behavior: a descriptor-only first session reproduced it.
- A 60-second idle delay between channels was insufficient to restore
  descriptor bootstrap in the tested case.
- No production session, transport, collector, descriptor, activation, or
  dependency-graph state was reused by the new channel.

**Not proven:**

- The exact firmware-internal cause.
- The exact lifetime of the remote state.
- Strictly ACL-scoped descriptor semantics.
- The exact Apple teardown or reset mechanism.

The safe conclusion is that descriptor-bootstrap behavior is affected by
AirPods/accessory remote state whose lifetime exceeds an individual AAP L2CAP
channel and which has previously been observed to reset after a normal BlueZ
disconnect/reconnect. The evidence does not establish that descriptors are
strictly once per ACL.

The production response remains one persistent AAP channel, one descriptor
bootstrap, and repeated HR START/STOP cycles. Production does not cache
descriptors across channels, bypass descriptor gating, continue from an ACK
alone, or automatically reconnect Bluetooth.

The completed five-second descriptor-only characterization used:

```console
cd /home/kenan/airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_session_reopen.py \
  --execute \
  --session-1-mode descriptor-only \
  --samples-per-session 5 \
  --reopen-delay 5 \
  --descriptor-timeout 30 \
  --verbose
```

The completed 60-second descriptor-only characterization used a fresh normal
BlueZ reconnect before the independent run:

```console
cd /home/kenan/airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_session_reopen.py \
  --execute \
  --session-1-mode descriptor-only \
  --samples-per-session 5 \
  --reopen-delay 60 \
  --descriptor-timeout 30 \
  --verbose
```

These commands record the completed owner-run hardware procedure. They are not
part of routine software validation and must never be invoked by automated
tests.
