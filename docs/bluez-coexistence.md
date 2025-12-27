# BlueZ coexistence feasibility

Coexistence probe tests whether the known AAP heart-rate protocol can use a normal Linux
kernel Bluetooth socket while BlueZ retains controller ownership. It is an
isolated feasibility path in `tools/probe_bluez_coexistence.py`. The existing
`airpods-hr monitor` command remains the known-good Bumble research backend and
still uses controller handoff. This task does not define a public transport or
heart-rate library API.

A final hardware PASS. The corrected kernel coexistence path
completed the canonical descriptor handshake and received real heart-rate
reports while BlueZ retained controller ownership and normal AirPods audio
continued without interruption.

## Transport and compatibility identity

The default live probe requires exactly one paired AirPods candidate that is
already connected according to BlueZ. The separate fresh-ACL isolation mode
instead requires that paired candidate to be disconnected before it starts.
Both modes record `Adapter1.Powered` and `Device1.Connected` without changing
either property. The outgoing channel is a Linux `AF_BLUETOOTH`,
`SOCK_SEQPACKET`, `BTPROTO_L2CAP` socket connected to the selected device at
PSM `0x1001`. Before configuring security or connecting,
the client explicitly binds the socket to the selected BlueZ adapter address
with local PSM zero so the kernel assigns the client endpoint. Python supplies
the general Bluetooth constants. This Python build omits `L2CAP_OPTIONS`, so
the Linux-header values `SOL_L2CAP=6` and `L2CAP_OPTIONS=1` provide a narrow
fallback. Before connect, the transport reads the native 12-byte
`struct l2cap_options`, changes only its input MTU (`imtu`) to 2048, writes it
back, and reads it again to verify the new value and every preserved field.
It also requests `BT_SECURITY_MEDIUM` through the host's two-`uint8_t`
`struct bt_security` layout. After connect, the probe reads the local endpoint
with `getsockname()`, normalizes it as a project `BluetoothAddress`, and
verifies that it still matches the selected adapter before sending AAP traffic.
Pairing and encryption remain kernel/BlueZ responsibilities.

The accepted Bumble path exposes four temporary SDP records:
`PnPInformation`, `HandsfreeAudioGateway`, `AudioSource`, and
`A/V RemoteControlTarget`. Their essential attributes are retained in one
canonical SDP module: adapter-derived USB vendor/product/version identity, HFP
AG RFCOMM channel 13, AVDTP PSM `0x0019` and version `0x0103`, and AVRCP target
version `0x0106`. BlueZ assigns its own service-record handles. The probe reads
`Adapter1.UUIDs` and registers only missing service classes with
`ProfileManager1` and a custom `ServiceRecord`. Registration is process-local,
is explicitly unregistered in reverse order, and is also removed by BlueZ if
the D-Bus owner disappears. It does not edit BlueZ configuration.

`Adapter1.UUIDs` establishes only which service-class UUIDs BlueZ advertises.
Matching UUID coverage does not establish that BlueZ's local SDP record
attributes are byte-for-byte or semantically equivalent to the accepted Bumble
records. The probe reports both the UUID coverage count and the number of
temporary profiles it registers so later hardware observations retain that
distinction.

The separate `--audit-sdp` mode is read-only. It compares the four required
service-class UUIDs through `Adapter1.UUIDs`. BlueZ's supported D-Bus API does
not expose the complete attributes of local records owned by BlueZ, and this
host does not provide a justified structured inspection facility. The audit
therefore reports RFCOMM channel, L2CAP PSM, protocol/profile versions, and PnP
identity as `unknown/not-observable`. It never promotes UUID coverage to full
record equivalence.

The kernel socket implements the existing private receive/send protocol used
by `AAPHandshakeSession` and `HeartRateActivationSession`. Those canonical
sessions remain responsible for handshake framing, exact ACK recognition,
the nine `HeartRateCommand` payloads, marker-based parsing, exact 18-byte raw
report retention, activation order, and HR stop commands. BPM zero and other
startup values are retained. There is no validity interpretation or filtering.

## Safety and failure behavior

The default invocation only prints its plan:

```console
cd /home/kenan/airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_bluez_coexistence.py
```

Dry-run does not connect to D-Bus, register a profile, create an L2CAP socket,
or send AAP data. Live mode is bounded by configurable D-Bus, L2CAP connect,
AAP ACK, descriptor, control ACK, stop ACK, and sample-collection timeouts.
The `--descriptor-timeout SECONDS` experiment accepts 1 through 30 seconds and
defaults to the canonical three-second post-ACK observation window. A
descriptor timeout remains a failure and does not advance to HR activation.
Failures report a phase and a safe category. `--verbose` may additionally show
an errno name, D-Bus error name, Python exception class, or bounded canonical
handshake observation summaries; it never prints pairing credentials, raw AAP
frames, arbitrary descriptor strings, or hidden six-byte unit values.

On failure or Ctrl+C, the canonical HR cleanup runs if activation reached that
state. The probe then closes its L2CAP socket, unregisters its temporary BlueZ
profiles, checks BlueZ connection state again, and closes its D-Bus connection.
It does not disconnect AirPods as cleanup, power-cycle the adapter, reconnect a
dropped device, invoke controller handoff, or fall back to Bumble.

`--experimental-ack-only-hr` is an explicit probe-only feasibility mode. It
does not change `AAPHandshakeSession`, the normal monitor, or the default
coexistence policy. Continuation is allowed only for
`AAPDescriptorObservationTimeoutError` after the exact ACK, with zero receive
frame drops and a still-connected BlueZ device. Missing or malformed ACKs,
generic handshake failures, route or security failures, and connection loss
remain terminal. The result continues to label the descriptor handshake
incomplete even if the canonical HR activation and sampling sequence succeeds.

`--experimental-fresh-bluez-acl` is a separate probe-only isolation mode and
is mutually exclusive with the ACK-only experiment. It requires a paired,
powered candidate with `Device1.Connected=false`, then uses the same selected
adapter bind, `BT_SECURITY_MEDIUM`, kernel L2CAP connect, route verification,
and canonical descriptor-gated handshake. The probe never calls BlueZ
`Connect`, `Disconnect`, or `ConnectProfile`; testing performs the initial
manual disconnect. The probe records whether `Device1.Connected` changes after
the L2CAP connect, after the exact ACK, after the descriptor phase, and during
cleanup. A descriptor timeout remains terminal and cannot reach HR in this
mode.

## Owner hardware validation

This sequence is owner-only. First connect the AirPods normally through BlueZ
and start continuous audio playback through them. The following command can
confirm locally that an AirPods device appears among BlueZ's connected devices;
its address does not need to be copied into a report or chat:

```console
bluetoothctl devices Connected | sed -n '/AirPods/p'
```

Confirm the dry-run, then try the live operation as the normal user:

```console
cd /home/kenan/airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_bluez_coexistence.py
PYTHONPATH=src .venv/bin/python tools/probe_bluez_coexistence.py \
  --execute --samples 5 --descriptor-timeout 30 --verbose
```

Audit the locally advertised compatibility identity without registering a
profile or opening an L2CAP socket:

```console
PYTHONPATH=src .venv/bin/python tools/probe_bluez_coexistence.py --audit-sdp
```

Testing-only HR feasibility experiment and future diagnostic reruns use:

```console
PYTHONPATH=src .venv/bin/python tools/probe_bluez_coexistence.py \
  --execute \
  --samples 5 \
  --verbose \
  --experimental-ack-only-hr
```

The fresh BlueZ-managed ACL isolation experiment is owner-only. Ensure normal
`bluetoothd` is running without a temporary compatibility mode, keep the
AirPods paired, manually disconnect them through BlueZ, and confirm that they
are no longer listed as connected. Do not reconnect them during the run:

```console
bluetoothctl devices Connected | sed -n '/AirPods/p'
PYTHONPATH=src .venv/bin/python tools/probe_bluez_coexistence.py \
  --execute \
  --samples 5 \
  --descriptor-timeout 30 \
  --verbose \
  --experimental-fresh-bluez-acl
```

The private reference-backend handshake diagnostic is dry-run safe by default:

```console
PYTHONPATH=src .venv/bin/python tools/probe_reference_handshake.py
```

Its owner-only live command uses the existing controller-handoff/Bumble path,
which normally requires the same privileges as the reference monitor:

```console
sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose
```

Reference descriptor timing can be compared with explicit ten-second and
thirty-second probe-only windows:

```console
sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 10

sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response proven
```

Coexistence probe.7 compares that proven response with the experimental Linux-kernel
wire shape while keeping the reference transport and canonical handshake the
same:

```console
sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response kernel-mtu-only
```

It performs only the canonical descriptor-gated handshake. It does not run HR
activation after a descriptor timeout or other handshake failure.

Coexistence probe.8 holds the proven AAP Configure Response constant and compares the
exact four-record reference footprint with a bounded, expanded extra-record
footprint based on testing's BlueZ audit:

```console
sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response proven \
  --sdp-footprint proven

sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response proven \
  --sdp-footprint bluez-like
```

The experimental footprint retains the four proven records unchanged and adds
deterministic records for the observed extra classic profiles and standard
GATT service classes over ATT PSM `0x001f`. It also includes the audited Message
Notification, Message Access, Phone Book Access Server, IrMC Synchronization,
OBEX File Transfer, OBEX Object Push, and Nokia OBEX PC Suite records with their
observed RFCOMM, OBEX, and profile descriptors. Machine-specific ATT handle
ranges and other unaudited vendor attributes remain omitted. This experiment
does not mutate the system BlueZ SDP database.

This is an extra-record and expanded-footprint causality experiment, not a
byte-for-byte BlueZ SDP reproduction. In particular, the frozen proven AVRCP
Target remains unchanged and therefore lacks the L2CAP/AVCTP descriptors seen
on testing's BlueZ AVRCP Target. A passing `bluez-like` run would weaken the
added-record footprint hypothesis; it would not eliminate every possible SDP
identity difference.

Coexistence probe.9 keeps the proven AAP Configure Response and proven SDP footprint
fixed while changing only the interval after encryption and before AAP opens.
The three owner-only reference controls are:

```console
sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response proven \
  --sdp-footprint proven \
  --pre-aap-sequence proven

sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response proven \
  --sdp-footprint proven \
  --pre-aap-sequence delay-only

sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response proven \
  --sdp-footprint proven \
  --pre-aap-sequence bluez-l2cap-info
```

`delay-only` waits 20 ms without sending added signaling.
`bluez-l2cap-info` uses Bumble's Classic signaling manager to request Extended
Features (`0x0002`) and then Fixed Channels (`0x0003`), requiring each bounded
response before AAP can open. The observer records only response categories
and decoded capability masks and is removed before AAP negotiation begins.

Coexistence probe.10 holds the proven post-authentication sequence fixed and varies only
the reference interval after Connect Complete and before Authentication
Requested:

```console
sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response proven \
  --sdp-footprint proven \
  --pre-aap-sequence proven \
  --pre-auth-sequence proven

sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response proven \
  --sdp-footprint proven \
  --pre-aap-sequence proven \
  --pre-auth-sequence delay-only

sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response proven \
  --sdp-footprint proven \
  --pre-aap-sequence proven \
  --pre-auth-sequence bluez-discovery
```

`delay-only` waits 85 ms without sending discovery commands.
`bluez-discovery` uses Bumble's HCI command and event abstractions to read
Remote Supported Features, read Remote Extended Features page 1, and request
the remote name with R2, mandatory page scan mode, and zero clock offset. Each
bounded, matching completion is required before the existing authentication
operation can begin. The observation retains decoded feature masks and status
metadata but no name or raw HCI payload.

receive-MTU experiment holds all earlier reference variables on their proven settings and
changes only the initial host-originated AAP Configure Request:

```console
sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response proven \
  --sdp-footprint proven \
  --pre-aap-sequence proven \
  --pre-auth-sequence proven \
  --aap-local-rx-profile proven

sudo env PYTHONPATH=src .venv/bin/python \
  tools/probe_reference_handshake.py --execute --verbose \
  --descriptor-timeout 30 \
  --aap-config-response proven \
  --sdp-footprint proven \
  --pre-aap-sequence proven \
  --pre-auth-sequence proven \
  --aap-local-rx-profile kernel-default
```

`proven` retains Bumble's MTU 2048 option. `kernel-default` removes only that
wire option for AAP PSM `0x1001`, leaving Bumble's internal channel receive MTU
at 2048. This is independent from `--aap-config-response`, which controls the
opposite Configure Request/Response direction.

Normal-user execution is the preferred test. Whether it succeeds depends on
the host's system D-Bus policy and Bluetooth socket permissions. If, and only
if, the first live attempt fails solely with `EACCES`, `EPERM`, a D-Bus access
denial, or an equivalent permission/capability category, retry:

```console
sudo env PYTHONPATH=src .venv/bin/python tools/probe_bluez_coexistence.py --execute --samples 5 --verbose
```

Keep audio playing throughout either live attempt. The probe itself prints the
connection invariant at preflight, after profile registration, after L2CAP
connect, after AAP ACK/descriptor handling, after HR activation, after HR
reception, and after cleanup. After the probe exits, confirm audio is still
playing and re-run the local connected device check:

```console
bluetoothctl devices Connected | sed -n '/AirPods/p'
```

Hardware feasibility passes only if BlueZ stays reachable, the adapter remains
powered, every connection checkpoint remains true, PSM `0x1001` opens, the AAP
handshake and HR activation succeed, at least three canonical reports are
received (five preferred), cleanup completes, the device remains connected,
and audio has no observable interruption. A run where audio or the BlueZ
connection drops is a failure even if HR data appeared.

## Real-hardware diagnostic milestone

Owner coexistence runs proved that BlueZ can retain controller ownership
while a kernel-managed L2CAP channel opens to PSM `0x1001` through the
explicitly selected and verified adapter. `BT_SECURITY_MEDIUM` reached the peer,
the canonical AAP request received the canonical exact ACK, and cleanup left
BlueZ reachable, the adapter powered, and `Device1.Connected` true. Testing
observed uninterrupted AirPods audio during the audio-active runs.

The three-second and ten-second descriptor windows produced effectively the
same result: 26 post-ACK frames, zero observed receive-frame drops, and none of
the four canonical descriptor evidence markers. The stream included a
structurally valid type-`0x002B` frame and other canonical AAP frame classes.
This proves a real non-empty post-ACK stream and makes simple delayed delivery
less likely; it does not establish a cause.

The experimental ACK-only HR path was then run once with audio active and once
with the AirPods connected but no audio playing. Both runs reproduced the same
protocol result: the exact AAP ACK was observed, descriptor evidence remained
absent, canonical HR activation reached the service-`0x13` START_HR ACK, no
valid HR report arrived before the stream timeout, and canonical cleanup
observed the STOP_HR ACK before sending HR_OFF. BlueZ remained connected in
both cases, and the audio-active run had no audible interruption. The no-audio
controlled run therefore did not change the missing-HR result; active audio
playback is not currently implicated by this evidence.

Coexistence probe.3 adds bounded post-START_HR diagnostics to distinguish an empty
stream window from non-HR traffic or marker-bearing frames that fail canonical
parsing. Observation starts only after the canonical START_HR acknowledgement,
uses the same frames consumed by `HeartRateActivationSession`, and stops before
STOP_HR is sent. It records only existing allowlisted `ControlFrameSummary`
metadata and numeric canonical parser counters. It does not retain or print
arbitrary frame payloads, start another receive loop, change commands, or alter
parsing semantics.

After these coexistence experiments, testing reran the previously proven
Bumble/reference monitor and it failed at its generic AAP handshake/descriptor
stage. The same result followed an AirPods case cycle. Testing then rebooted
the host, reconnected the AirPods, and reproduced the reference failure before
running another coexistence probe. The missing HR stream therefore cannot yet
be attributed solely to the coexistence architecture.

Coexistence probe.4 adds the separate private reference handshake diagnostic above. It
reuses the existing paired-device discovery, local Classic credentials,
controller handoff, Bumble authentication and encryption, accepted four-record
SDP identity, Bumble L2CAP transport, and canonical `AAPHandshakeSession`. Its
bounded output reports exact-ACK state, pre/post-ACK frame counts, descriptor
evidence booleans, and canonical safe structural summaries for direct
comparison with coexistence observations. It does not print credential or raw
frame material and does not speculate about the cause. Current evidence does
not establish that the AirPods are broken or that any persistent protocol
state exists.

Testing subsequently performed a Linux re-pair and then an AirPods factory
reset with fresh pairing. Neither restored canonical descriptor completion at
the three-second window. Repeating device or pairing resets is therefore not a
useful next experiment based on current evidence. Coexistence probe.5 added bounded
reference timeout control, and the reference/Bumble path then completed the
canonical descriptor evidence at 30 seconds: its fresh BR/EDR connection
authenticated, enabled encryption, opened PSM `0x1001`, observed the exact ACK,
and found all four markers among 29 post-ACK frames.

The existing-ACL BlueZ coexistence path did not complete the same descriptor
evidence with a 30-second window, either with active audio or without audio.
It observed the exact ACK and about 30 post-ACK frames with no receive drops.
The subsequent ACK-only HR experiment received the canonical activation ACK
but no HR stream. Active A2DP playback is therefore not implicated by these
controlled observations.

Testing also inspected the actual local BlueZ SDP database. The critical
compatibility attributes were present: PnP vendor `0x1D6B`, product `0x0246`,
version `0x0557`, and source `0x0002`; HFP AG RFCOMM channel 13; Audio Source
PSM `0x0019` with AVDTP `0x0103`; and AVRCP Target `0x0106`. Record handles and
additional BlueZ attributes naturally differ, so this evidence does not claim
byte-for-byte or complete semantic SDP equality.

Coexistence probe.6 isolates the leading remaining transport variable: the successful
reference runtime creates a fresh BR/EDR ACL, whereas normal coexistence adds
PSM `0x1001` to a pre-existing BlueZ ACL. The explicit fresh-ACL mode starts
from a manually disconnected but paired device and lets only the kernel L2CAP
connect create or use a new BlueZ-managed ACL. It reports observations and
does not assign a root cause.

Testing also ran the fresh BlueZ-managed ACL experiment with WirePlumber
stopped and inactive. It began with `Device1.Connected=false`; kernel L2CAP PSM
`0x1001` connected, the exact AAP ACK arrived, and 30 post-ACK frames were
observed. Only `sensor_framework` appeared; `heart_rate_service`, `heart_rate`,
and `heartrate_access` remained absent, so canonical descriptor completion
timed out. WirePlumber was restored afterward. This shows that the failure
persists without WirePlumber policy and audio orchestration, making
WirePlumber itself an unlikely leading explanation. It does not rule out every
possible interaction with concurrent BlueZ profiles.

A subsequent `btmon` capture exposed a concrete L2CAP-level difference on AAP
PSM `0x1001`. The AirPods Configure Request contained MTU 2582 and Flush
Timeout 30. Linux accepted the request but its successful Configure Response
contained only the MTU option (`01 02 16 0a`); it omitted the peer's Flush
Timeout option (`02 02 1e 00`). The same general Linux behavior was observed
on ordinary AVDTP PSM `0x0019` channels, so the capture alone does not prove
that the omission causes the AAP descriptor difference.

The historical reviewed AAP request sent to Bumble also included an RFC Basic
option. The frozen `aap_flush_timeout_compatibility()` behavior accepts the
reviewed Flush Timeout while preserving the peer request options in its
successful response. Coexistence probe.7 tested that response-shape hypothesis on real
hardware. In proven mode the peer sent option types `0x01,0x02`, MTU 2582, and
Flush Timeout 30; the response retained option types `0x01,0x02` and included
Flush Timeout. In `kernel-mtu-only` mode the same request received an MTU-only
`0x01` response with MTU 2582 and no Flush Timeout option.

Both response modes produced the exact AAP ACK and 29 post-ACK frames.
`sensor_framework`, `heart_rate_service`, `heart_rate`, and
`heartrate_access` were all present, yielding `descriptor_complete=yes` and
`REFERENCE HANDSHAKE PASS`. Omitting Flush Timeout from the successful response
therefore did not reproduce the coexistence descriptor failure, so the Flush
Timeout echo difference is no longer a leading causal hypothesis.

The extracted HCI teardown evidence showed host-issued disconnect commands and
the reason `Connection Terminated By Local Host`. It does not support claiming
that the AirPods initiated those disconnects.

The coexistence `btmon` capture also showed the AirPods issue an SDP
`ServiceSearchAttributeRequest` with search pattern L2CAP UUID `0x0100`, maximum
attribute byte count 65535, and full attribute range `0x0000ffff`. The BlueZ
response was paginated across multiple roughly 250-byte continuation pages.
This proves the peer inspects a broad L2CAP-visible part of the host SDP
capability footprint rather than querying only Public Browse Group `0x1002`.

The private reference diagnostic now observes the same allowlisted query
metadata and compares its exact four-record footprint with a bounded expanded
set. A dedicated slot always retains the L2CAP `0x0100` full-attribute query,
even after the general summary limit or across continuation pages. It reports
UUIDs, attribute ranges, maximum byte count, continuation use, match count, and
computed response size without retaining SDP packet payloads. The footprint
remains an extra-record experiment rather than a complete BlueZ SDP clone.
Clean targeted request/response decoding subsequently matched between a
reference descriptor PASS and a BlueZ descriptor FAIL, and the expanded
`bluez-like` reference experiment also completed all descriptors. These results
make the added SDP footprint unlikely to explain the current difference.

The same clean captures showed identical HCI Create Connection parameters:
packet type `0xcc18`, page scan repetition mode R2, mandatory page scan mode,
clock offset `0x0000`, and peripheral role switch allowed. Neither relevant
pre-AAP interval contained an actual Role Change, Sniff Mode transition, or
Link Policy change. The longer Create Connection latency in the BlueZ sample
is not treated as causal because the parameters matched and paging latency can
vary.

The leading concrete peer-visible difference is now the initialization between
encryption and AAP channel creation. The reference path opened AAP PSM `0x1001`
almost immediately after encryption. BlueZ read the encryption key size, sent
an L2CAP Information Request for Extended Features (`0x0002`), received its
response, sent an L2CAP Information Request for Fixed Channels (`0x0003`),
received its response, and then opened AAP. Coexistence probe.9 isolates only those two
L2CAP Information exchanges in the proven reference environment. It does not
emulate the remote feature, remote name, or encryption-key-size HCI reads.

Testing completed all three Coexistence probe.9 reference runs. Proven mode,
`delay-only`, and `bluez-l2cap-info` all observed the exact ACK and completed
all descriptor evidence. The information experiment returned Extended Features
mask `0x00000280` and Fixed Channels mask `0x0401000000000040`, matching the
clean BlueZ evidence. The 20 ms timing control also passed. BlueZ's pre-AAP
L2CAP Information exchange and that short post-encryption delay therefore did
not reproduce the coexistence failure under the proven reference transport and
are no longer leading causal hypotheses.

Coexistence probe.10 tested the earlier peer-visible difference. The reference path
authenticated about 1 ms after Connect Complete, while the observed BlueZ path
spent about 82 ms reading Remote Supported Features, Remote Extended Features
page 1, and the remote name before Authentication Requested. The proven,
85 ms delay-only, and BlueZ-style discovery reference runs all completed the
canonical descriptor evidence with 29, 29, and 30 post-ACK frames respectively.
The discovery run read Supported Features `0x877BFFDBFE2DFEBF`, Extended
Features page 1 `0x000000000000000B` with maximum page 2, and the remote name
successfully. Neither the delay nor that discovery sequence reproduced the
coexistence failure, so they are no longer leading hypotheses.

The clean reference and BlueZ AAP transcripts contain the same 16-byte
canonical handshake request and the same 18-byte exact ACK. Immediately before
the post-ACK stream, the reference host's Configure Request advertises MTU
2048, while the Linux kernel sends no MTU option, implying the Classic default
receive MTU of 672. This host-originated request describes the host receive
capability for AirPods-to-host traffic. The AirPods-originated request
advertising MTU 2582 describes the opposite, host-to-AirPods direction.

receive-MTU experiment causally reproduced the descriptor failure in the proven reference
stack. With an explicit host receive MTU of 2048, all descriptor evidence
completed and a 996-byte type-`0x0017` SDU arrived. Removing only the MTU option
left Bumble's internal receive MTU at 2048 but made the peer respond with MTU
672; the exact AAP ACK still arrived, only `sensor_framework` evidence
remained, descriptor completion timed out, and the 996-byte SDU disappeared.
This establishes the advertised local receive MTU as the leading causal root
cause.

The first post-ACK type-`0x002B` frame was 357 bytes in both receive-MTU experiment runs,
so historical `0x002B` length variation is not treated as causal. coexistence validation
applies the proven requirement to the kernel BR/EDR socket by setting
`imtu=2048` through `SOL_L2CAP/L2CAP_OPTIONS` before connect while preserving
all other `l2cap_options` fields.

A secondary untested transcript difference remains in the host L2CAP Extended
Features response: reference reported `0x000000A8`, while BlueZ reported
`0x000002B8`. receive-MTU experiment does not alter or interpret that mask.

## Final hardware result

Validated on real AirPods Pro hardware. The kernel local
receive summary reported `before_imtu=672`, `after_imtu=2048`, every other
`l2cap_options` field preserved, and `verified=yes`. The selected local adapter
route was confirmed, the exact canonical AAP ACK arrived, the canonical
descriptor handshake completed, and canonical HR activation was acknowledged.
The probe received five of five requested reports, including BPM values 169,
137, 112, 85, and 85, with sequences 0 through 4 and exact 18-byte raw reports.

A later validation after a normal BlueZ disconnect/reconnect again completed
the descriptor handshake and received five of five reports, including BPM
values 169, 145, 120, 101, and 106. During the successful runs, BlueZ remained
reachable, the adapter remained powered, `Device1.Connected` remained true,
and observed uninterrupted normal AirPods audio. Cleanup preserved
the BlueZ connection and audio connectivity. BlueZ retained controller
ownership throughout, so A final hardware PASS.

One lifecycle nuance remains. After a successful coexistence session,
immediately starting a new probe process on the same BlueZ connection can
receive the exact AAP ACK but time out waiting for descriptors. A normal BlueZ
disconnect/reconnect restored full descriptor completion on the same corrected
`imtu=2048` path. This suggests an AAP descriptor-bootstrap or channel-lifecycle
state issue across separate channel opens; it does not invalidate the proven
coexistence result. telemetry semantics captures should begin after a fresh
normal BlueZ reconnect. Experiments that compare multiple HR activations should
keep one AAP PSM `0x1001` channel open instead of reopening it between cycles.

The read-only SDP audit continues to preserve attributes unavailable through
its chosen API as unknown. Separate owner inspection supplies the observed
critical values without claiming complete record equality. No public transport
or heart-rate API is frozen by this feasibility result.
