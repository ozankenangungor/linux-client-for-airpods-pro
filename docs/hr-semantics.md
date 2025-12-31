# Heart-rate report semantics capture

Adds private evidence-capture infrastructure. It does not assign
meanings to unresolved AirPods fields and does not define a public library API.
The tool uses the Coexistence probe BlueZ/kernel coexistence transport, including the
verified Classic L2CAP local receive MTU of 2048, selected-adapter routing, the
canonical descriptor-gated AAP handshake, canonical HR activation commands,
and the existing parser.

## Evidence entering Semantics capture

The canonical report is exactly 18 bytes and has report ID `0x01`. BPM is the
literal value decoded by the accepted parser. Real captures have repeatedly
started with a report containing BPM 169 and then transitioned to other BPM
values. Sequence values have been observed incrementing 0, 1, 2, 3, 4, and so
on. `field_5` and `flags` have changed in real captures.

These observations do not establish semantic meanings. In particular, BPM 169
must not be treated as invalid merely because it has often appeared first. The
capture path retains it, every other startup value, and duplicate reports in
arrival order without filtering or smoothing. The meanings of `aux`,
`field_5`, `timestamp_ticks`, and `flags` remain unassigned.

Semantics capture separates transport and ordering mechanics, behavior across HR
activation cycles, and evidence about unresolved state fields before any
public API names are frozen.

## Private capture tool

The default invocation is a dry run and performs no Bluetooth work:

```console
cd /home/kenan/airpods-hr-linux
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
cd /home/kenan/airpods-hr-linux
PYTHONPATH=src .venv/bin/python tools/probe_hr_semantics.py \
  --execute \
  --scenario baseline \
  --samples 30 \
  --output /tmp/airpods-hr-semantics-baseline.jsonl
```

Same-session activation restart:

```console
cd /home/kenan/airpods-hr-linux
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
