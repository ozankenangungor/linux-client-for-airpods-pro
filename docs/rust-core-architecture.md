# Rust core architecture

Protocol core introduces `airpods-aap-core` as a platform-independent protocol
core. Rust provides explicit integer widths, exhaustive result handling, and a
small typed boundary that future SDKs can share. This task establishes byte
parity and package structure. It does not replace the hardware-proven Python
production runtime or freeze the final end-user API.

## Target architecture

```text
                         AirPods
                            |
                    BlueZ / kernel L2CAP
                            |
                      persistent daemon
                       airpods-hubd
                            |
                +-----------+-----------+
                |           |           |
             Rust SDK    Python SDK   local IPC
                |                       |
            native apps              games / OBS /
                                     Unity / others
```

The daemon, SDKs, and IPC layer are future components. Protocol core implements
only the pure core crate shown below.

```text
airpods-aap-core (Protocol core)
  proven protocol constants and neutral wire types
  canonical report parsing
  mechanically derived semantics
  no operating-system I/O

future Linux layers
  BlueZ and D-Bus orchestration
  kernel Bluetooth sockets and persistent channel lifecycle
  audio coexistence and controller ownership policy
```

## Core boundary

The core may own protocol facts that do not depend on an operating system:
wire constants and neutral types, canonical report parsing, structurally
proven descriptor/message parsing, I/O-free state-machine primitives, and
mechanical or evidence-qualified derivations. It must not depend on BlueZ,
D-Bus, Linux Bluetooth sockets, PipeWire, HCI controller ownership, filesystem
LinkKeys, or async Linux transport lifecycle.

The initial crate is standard-library only and forbids unsafe code. Its parser
copies the exact 18-byte report into a fixed-size array, so callers can inspect
or compare the wire data without an allocation in the report model. The small
copy favors straightforward parity over premature optimization.

The proven Linux transport remains in Python. In particular,
`production_session.py`, `bluez_coexistence.py`, `monitor_cli.py`, and the
transport/session code they compose do not call Rust. There is no FFI, PyO3,
maturin runtime dependency, daemon, or Rust transport API in Protocol core.

## Heart-rate parity contract

The frozen Python `parse_heart_rate_packet()` implementation in
`src/airpods_hr/heartrate.py` is authoritative. Both implementations consume
the safe shared corpus at `testdata/hr_report_golden.tsv`. For every vector,
the Python result must equal the Rust result for acceptance or rejection and
for every decoded field.

The exact report layout is:

| Byte range | Rust type | Byte order | Preserved name |
| --- | --- | --- | --- |
| `0` | `u8` | single byte | report ID (`0x01`) |
| `1` | `u8` | single byte | `bpm` |
| `2` | `u8` | single byte | `aux` |
| `3..5` | `u16` | little-endian | `sequence` |
| `5` | `u8` | single byte | `field_5` |
| `6..14` | `u64` | little-endian | `timestamp_ticks` |
| `14..18` | `u32` | little-endian | `flags` |

The parser follows the Python outer-packet contract exactly: find the first
verified marker, require at least 18 following bytes, validate the report ID,
decode the first 18-byte report, and ignore later frame bytes. Its explicit
errors correspond only to currently enforced rules: marker missing, report
truncated, and unexpected report ID. Rust adds no speculative validation.

`field_5` remains present with its raw name and value. Controlled-high Semantics capture
evidence supports a separate `source_side()` derivation: `1` maps to `Left`,
`2` maps to `Right`, and every other byte remains representable as
`Unknown(raw)`. This derived view does not alter the wire struct.

The corpus uses synthetic protocol vectors and contains no Bluetooth address,
LinkKey, encryption material, HCI capture, or personal data. It covers both
known source-side values, an unknown value, the actual `u16` sequence and `u64`
timestamp edges, duplicates, trailing outer-frame data, malformed inputs, BPM
169, and the six representative raw flags values from Semantics capture. No command
payload collection is duplicated in Rust during this task.

## Semantics boundary

Protocol core carries evidence forward without increasing confidence:

| Evidence | Supported use | Boundary |
| --- | --- | --- |
| `CONTROLLED-HIGH` | `field_5=1` derives left source side; `field_5=2` derives right source side. | The raw `field_5` stays available. |
| `ASSOCIATED` | Flags bits 0, 17, 23, and 31 vary with activation/bootstrap phase. | No bit is assigned a semantic name. |
| `ASSOCIATED` | Flags bits 12, 13, and 24 vary with tested single-versus-dual topology. | No presence, wear, contact, or validity meaning is assigned. |
| `UNKNOWN` | `aux`, exact flags meanings, timestamp unit/epoch, and repeated activation-start BPM 169. | Values are preserved without interpretation. |

BPM 169 is parsed as the integer 169. It is not filtered, treated as a
sentinel, dropped at startup, smoothed, or assigned lower validity. Duplicate
reports likewise remain ordinary data.

## Future crate layout

The intended separation is:

| Crate | Intended responsibility | Protocol core status |
| --- | --- | --- |
| `airpods-aap-core` | Portable protocol constants, parsers, neutral types, and I/O-free primitives. | Initial crate implemented. |
| `airpods-linux` | BlueZ, D-Bus, kernel L2CAP, and Linux lifecycle integration. | Future design only. |
| `airpods-client` | Client-side API for a local service. | Future design only. |
| `airpods-hubd` | Persistent session owner and local multi-consumer service. | Future design only. |

This layout is directional and does not freeze crate APIs or the final
end-user interface.

## Future Python binding

A later internal extension can expose the portable parser through PyO3 and be
packaged with maturin while Python continues to own BlueZ, D-Bus, and Linux
orchestration:

```text
Python BlueZ / D-Bus / Linux orchestration
                    |
                    v
       internal Rust protocol extension
```

The package-level experience should remain `from airpods_hr import ...`; users
should not need to know which internal parser is active. Adds no
binding code, Python dependency, import change, or packaging hook.

## Future persistent daemon

Iteration 9.6 hardware evidence favors a daemon because the reliable lifecycle is
one continuously open AAP session with one descriptor bootstrap and repeated
HR START/STOP cycles. A later daemon can own that session while multiple local
consumers come and go:

```text
subscriber_count 0 -> 1:  START HR
subscriber_count 1 -> 0:  STOP HR; keep AAP channel READY
application exits:        daemon and READY AAP session remain alive
```

This would avoid treating each application lifetime as a new AAP channel.
Protocol core does not implement the daemon, subscriber accounting, IPC, or service
management.

## Authority and migration gates

The Python parser and protocol constants remain authoritative for wire
behavior. `production_session.py`, `bluez_coexistence.py`, `monitor_cli.py`,
`hr_semantics.py`, and their existing tests remain authoritative for current
production behavior and evidence handling.

Before Rust can replace any Python path, a later task must:

1. Keep every safe golden vector passing through both parsers and expand the
   corpus for any newly proven structure.
2. Review an internal binding API without freezing the end-user API.
3. Add PyO3/maturin packaging and import behavior with explicit fallback and
   failure contracts.
4. Prove Python package, wheel, and supported-platform behavior in CI.
5. Re-run all hardware-independent Python and Rust validation with the binding
   enabled.
6. Perform separately authorized hardware validation of persistent-session,
   descriptor gating, cleanup, BlueZ ownership, and audio coexistence.
7. Stage any production switch so the proven Python path remains available
   until parity, rollback, and failure behavior are accepted.

No migration gate is satisfied merely by creating this crate.
