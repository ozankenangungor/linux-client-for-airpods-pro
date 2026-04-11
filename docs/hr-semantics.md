# Heart-rate report semantics

Semantics captureA added private evidence-capture infrastructure, and its baseline and
same-AAP activation-restart scenarios passed on real AirPods Pro 3 hardware.
Semantics captureB consolidates that evidence without changing the parser, assigning
medical validity, or defining a public library API. The tool uses the Coexistence probe
BlueZ/kernel coexistence transport, including the verified Classic L2CAP local
receive MTU of 2048, selected-adapter routing, the canonical descriptor-gated
AAP handshake, canonical HR activation commands, and the existing parser.

## Evidence entering Semantics capture

The canonical report is exactly 18 bytes and has report ID `0x01`. BPM is the
literal value decoded by the accepted parser. Real captures have repeatedly
started with a report containing BPM 169 and then transitioned to other BPM
values. Sequence values have been observed incrementing 0, 1, 2, 3, 4, and so
on. `field_5` and `flags` have changed in real captures.

These observations do not establish medical meanings. In particular, BPM 169
must not be treated as invalid merely because it has often appeared first. The
capture path retains it, every other startup value, and duplicate reports in
arrival order without filtering or smoothing. Controlled Semantics captureB evidence
strongly establishes `field_5` as a source-side identifier while preserving
that neutral field name in the frozen wire parser. The exact meanings of
`aux`, the timestamp epoch and unit contract, and individual `flags` bits
remain unassigned.

Semantics capture separates transport and ordering mechanics, behavior across HR
activation cycles, and evidence about unresolved state fields before any
public API names are frozen.

## Evidence levels

The following labels describe the strength and limits of the current evidence.
They are not API-stability levels.

| Level | Meaning |
| --- | --- |
| `PROVEN-WIRE` | Directly established by canonical parsing or exact capture. It describes bytes, integers, ordering, or timing without assigning a cause or product meaning. |
| `CONTROLLED-HIGH` | Repeated controlled changes, including a reversed-order control, support one narrow interpretation and remove the tested ordering explanation. The result remains scoped to the tested hardware and conditions. |
| `ASSOCIATED` | A value or bit changes consistently with an experimental phase or topology, but the underlying device concept has not been isolated. |
| `UNKNOWN` | No controlled evidence supports a semantic interpretation. Observed values may still be recorded mechanically. |

## Consolidated evidence

| Level | Field or behavior | Supported statement | Interpretation boundary |
| --- | --- | --- | --- |
| `PROVEN-WIRE` | Report framing | A canonical report is 18 bytes, has report ID `0x01`, and preserves its exact bytes. | No validity or quality conclusion follows from framing. |
| `PROVEN-WIRE` | `bpm` | The accepted parser decodes BPM directly from its existing byte position. Every value remains in arrival order. | No value, including 169, is classified as valid or invalid here. |
| `CONTROLLED-HIGH` | `field_5` | `1` identifies the left source side and `2` identifies the right source side. This is a strongly established source-side identifier for the report. | It does not merely say which earbud is connected or worn; both-ear captures can switch source side during one activation. The parser field remains named `field_5`. |
| `CONTROLLED-HIGH` | `sequence` lifecycle | It increments by one in the observed steady captures and resets to 0 at every new HR activation, including a restart on the same AAP channel. | A non-unit delta is not yet defined as packet loss. |
| `ASSOCIATED` | First BPM | BPM 169 repeatedly occurs at activation start, including both activations on one AAP channel. | This is a deterministic/repeated activation-start BPM observation, not a sentinel or discard rule. |
| `ASSOCIATED` | `timestamp_ticks` | It remains monotonic across same-channel HR stop/restart and advances by approximately 1,000,000,000 numeric units per approximately one-second report interval. | The scale is numerically consistent with nanoseconds, but epoch, origin, and exact unit contract are unproven; the name remains `timestamp_ticks`. |
| `ASSOCIATED` | `flags` bits 12, 13, and 24 | Controlled same-channel topology changes associate these bit positions with single-versus-dual-ear topology. | No bit is named as presence, worn state, contact, connection, or sensor validity. |
| `ASSOCIATED` | `flags` bits 0, 17, 23, and 31 | Their timing repeats around activation: bits 0 and 23 occur only in sample 1; bits 17 and 31 occur in samples 1 through 4 in the applicable captured patterns. | They are mechanically associated with activation/bootstrap phase only. |
| `UNKNOWN` | `aux` | Early reports can repeat a value such as 20, and later reports can change substantially around the broad startup transition. | Confidence, quality, signal strength, contact, motion, and validity meanings are unsupported. |

## Controlled capture results

The validated baseline produced 30 of 30 canonical reports. Its 32
independently valid JSONL lines comprised one header, 30 samples, and one final
summary. Sequence ran from 0 through 29. The 29 receive intervals had a minimum
of 906.842930 ms, median of 996.748491 ms, and maximum of 1071.940923 ms. The
first BPM was 169, and `timestamp_ticks` remained monotonic. `field_5` was 2 for
samples 1 through 4 and 1 from sample 5 onward. The exact flags progression
was:

| Baseline samples | `flags` |
| --- | --- |
| S1 | `0x81821001` |
| S2-S4 | `0x81021000` |
| S5 onward | `0x00001000` |

The same-AAP activation-restart capture kept one PSM `0x1001` channel open,
completed one descriptor handshake, and performed two successful activation
cycles separated by five seconds. Each cycle started with BPM 169 and sequence
0, then sequence advanced through 9. Sequence therefore resets with HR
activation rather than AAP channel creation. `timestamp_ticks` continued
across the stop/restart. Both cycles repeated the same flags progression shown
above.

Two order-reversed source experiments established the `field_5` mapping:

| Experiment | Cycle | Controlled source | Observed `field_5` |
| --- | --- | --- | --- |
| A | 1 | Left only; right in case | `1` for 12/12 reports |
| A | 2 | Right only; left in case | `2` for 12/12 reports |
| B, reversed | 1 | Right only; left in case | `2` for 12/12 reports |
| B, reversed | 2 | Left only; right in case | `1` for 12/12 reports |

The reverse order removes cycle order as an explanation. In both-ear captures,
`field_5` can change within an activation, so the supported interpretation is
the source side of each report rather than general connection or wear state.

Two same-channel topology experiments also changed both directions:

| Direction | Both-ear cycle | Single-ear cycle |
| --- | --- | --- |
| Both to left only | Both first, then left only | Left-only `field_5=1` for 12/12 reports |
| Left only to both | Both second; `field_5=1` for S1-S4 and `2` from S5 | Left-only `field_5=1` for 12/12 reports |

The flags changed with topology in both causal directions:

| Phase | Dual-ear flags | Dual-ear bits set | Single-ear flags | Single-ear bits set |
| --- | --- | --- | --- | --- |
| S1 | `0x81821001` | 0, 12, 17, 23, 24, 31 | `0x80822001` | 0, 13, 17, 23, 31 |
| S2-S4 | `0x81021000` | 12, 17, 24, 31 | `0x80022000` | 13, 17, 31 |
| S5 onward | `0x00001000` | 12 | `0x00002000` | 13 |

Left-only and right-only runs produced the same single-ear flags pattern, so
that pattern is not source-side specific. These tables preserve exact integers
and mechanically derived bit indexes; they deliberately do not assign names to
individual bits.

All hardware captures used the production BlueZ/kernel coexistence path. BlueZ
retained controller ownership, and ordinary AirPods audio remained compatible
with the capture path. Audio continuity is an owner observation rather than a
field inferred by the tool.

## Private capture tool

The default invocation is a dry run and performs no Bluetooth work:

```console
cd airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_hr_semantics.py
```

`baseline` opens one AAP channel, completes one descriptor handshake, performs
one canonical HR activation, records the requested reports, and runs canonical
STOP_HR and HR_OFF cleanup.

`activation-restart` opens one AAP channel and completes one descriptor
handshake. It then performs two complete canonical HR activation/stop cycles on
that same open channel, with a configurable bounded delay between them. It does
not reopen PSM `0x1001` or repeat descriptor discovery between cycles.

Each live run writes JSONL. If `--output` is omitted, the tool creates a unique
`/tmp/airpods-hr-semantics-<timestamp>-<pid>.jsonl` path and prints it. The file
is opened without overwriting an existing path, and every complete JSON object
is flushed immediately so evidence from an earlier cycle survives a later
failure.

The file contains one `capture_header`, one `sample` record for every canonical
report, and one `capture_summary`. Sample records include monotonic receive
time, arrival and activation-relative deltas, all six neutral parsed fields,
mechanical flag bit indexes, modulo deltas, duplicate status, exact raw-report
length, and the exact 18-byte report as lowercase hex. Raw report hex is
included by default because this is a private reverse-engineering artifact; it
is not printed on the console.

The parser-derived widths are 16 bits for `sequence`, 64 bits for
`timestamp_ticks`, and 32 bits for `flags`. Modulo deltas use those widths and
do not imply packet loss. Summary reset fields are mechanical observations:
zero after a prior nonzero value is `yes`, a nondecreasing transition is `no`,
and other or missing evidence is `unknown`. These labels do not assign a
protocol meaning to either counter.

The JSONL contains no Bluetooth address, LinkKey, encryption key, or arbitrary
HCI material. Console connection checkpoints attest only BlueZ reachability,
adapter power, and `Device1.Connected`; audio continuity remains an owner
observation.

## Owner capture procedure

Because immediate new AAP processes on an unchanged BlueZ connection can
occasionally miss descriptor bootstrap, perform a fresh normal BlueZ
disconnect/reconnect before each new capture process. Keep ordinary AirPods
audio playing during capture. Do not disconnect or reconnect between cycles of
the activation-restart scenario.

Baseline capture:

```console
cd airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_hr_semantics.py \
  --execute \
  --scenario baseline \
  --samples 30 \
  --output /tmp/airpods-hr-semantics-baseline.jsonl
```

Same-session activation restart:

```console
cd airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_hr_semantics.py \
  --execute \
  --scenario activation-restart \
  --samples-per-cycle 10 \
  --restart-delay 5 \
  --output /tmp/airpods-hr-semantics-activation-restart.jsonl
```

Both paths are descriptor-gated and have no Bumble fallback. They do not read
pairing credentials, take controller ownership, manage A2DP, or intentionally
disconnect the AirPods. Testing should preserve the resulting JSONL files
for architecture and semantics review without interpreting unresolved fields.
