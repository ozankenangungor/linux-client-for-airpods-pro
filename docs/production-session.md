# Persistent production session core

Adds a private production-session core over the proven BlueZ/kernel
coexistence transport. It is implementation scaffolding for hardware validation
and does not freeze a public library API. The module is deliberately absent
from `airpods_hr.__init__`.

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

Session reopen may investigate safe recovery for a new AAP channel on an unchanged
BlueZ ACL. Production session neither implements nor assumes such recovery, and no public
API is frozen here.
