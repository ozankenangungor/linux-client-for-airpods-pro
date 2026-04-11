# Rust core architecture

Protocol core introduced `airpods-aap-core` as a platform-independent protocol
core. Rust provides explicit integer widths, exhaustive result handling, and a
small typed boundary shared by later SDK work. That task established byte
parity and package structure. It did not replace the hardware-proven Python
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

At Protocol core, the daemon, SDKs, and IPC layer were future components. That task
implemented only the pure core crate shown below.

```text
airpods-aap-core (Protocol core)
  proven protocol constants and neutral wire types
  canonical report parsing
  mechanically derived semantics
  no operating-system I/O

future Linux layers as planned at Protocol core
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
`production_session.py`, `bluez_coexistence.py`, `heart_rate_session.py`,
`monitor_cli.py`, `hr_semantics.py`, and the transport/session code they
compose do not call Rust. Adds an isolated development FFI binding,
but there is no binding import, parser selection, daemon, or Rust transport API
in any production path.

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

## Protocol core planned crate layout

The intended separation is:

| Crate | Intended responsibility | Protocol core status |
| --- | --- | --- |
| `airpods-aap-core` | Portable protocol constants, parsers, neutral types, and I/O-free primitives. | Initial crate implemented. |
| `airpods-linux` | BlueZ, D-Bus, kernel L2CAP, and Linux lifecycle integration. | Future design only. |
| `airpods-client` | Client-side API for a local service. | Future design only. |
| `airpods-hubd` | Persistent session owner and local multi-consumer service. | Future design only. |

This layout is directional and does not freeze crate APIs or the final
end-user interface.

## Private Python binding

Adds `crates/airpods-aap-py` as a separate PyO3 crate. The dependency
direction is one-way: `airpods-aap-py` depends on `airpods-aap-core`, while the
core manifest remains empty of dependencies and its source remains free of
PyO3 and Python runtime knowledge. Both crates are unpublished.

The binding exposes the top-level private module `_airpods_aap_core` for
development and parity testing:

```text
Python BlueZ / D-Bus / Linux orchestration
                    |
                    v
      _airpods_aap_core (private PyO3)
                    |
                    v
            airpods-aap-core
```

The module name begins with an underscore and is not part of
`airpods_hr.__all__`. Keeping it top-level avoids modifying the existing
setuptools build, the `airpods_hr` package contents, or normal installation.
The binding has its own `pyproject.toml`; the root `pyproject.toml` continues to
use setuptools. This is a development-only packaging boundary, not final
end-user installation or API design.

The private function `_airpods_aap_core.parse_heart_rate_packet()` accepts
Python `bytes` only. PyO3 extracts `PyBytes` directly, so mutable `bytearray`,
`memoryview`, strings, and other objects raise `TypeError`. The returned frozen
private object exposes the six neutral fields plus exact `raw_report` bytes.
Its `source_side()` method returns `("left", None)`, `("right", None)`, or
`("unknown", raw)` while preserving `field_5`. It does not interpret `aux`,
name flags, rename `timestamp_ticks`, filter BPM 169, or deduplicate reports.

Rust parse failures map to three private Python exception types:
`MarkerNotFoundError`, `TruncatedReportError`, and `InvalidReportIdError`. These
categories support parity testing without replacing the existing public Python
exceptions. No parser falls back to the other on failure.

FFI binding uses PyO3 `0.29.2` and maturin `1.15.0`. They build against the tested
CPython `3.14.6` and Rust/Cargo `1.97.0` environment. PyO3 is pinned exactly in
the binding manifest because this private native boundary is compiler-facing;
maturin is pinned exactly in the binding-only build configuration so developer
rebuilds use the validated frontend. Neither pin affects the root Python
package or the core crate.

Install the development build and run mandatory real-FFI parity tests from the
repository root:

```console
.venv/bin/python -m pip install maturin==1.15.0
cd crates/airpods-aap-py
../../.venv/bin/maturin develop --release --locked
cd ../..
PYTHONPATH=src .venv/bin/python -m unittest -v tests.test_rust_ffi_parity
```

The test module imports `_airpods_aap_core` unconditionally. A missing or
unbuildable extension fails FFI binding instead of turning all FFI checks into
skips. The tests compare every golden vector and a bounded deterministic
synthetic corpus through the real native module, including failures, integer
widths, trailing frame bytes, raw report bytes, source side, BPM 169, and
duplicates.

To remove the development installation and its targeted build output:

```console
.venv/bin/python -m pip uninstall -y airpods-aap-py
cargo clean -p airpods-aap-py
```

Run the install commands again for a clean rebuild. Generated extension
libraries, wheels, and `target/` contents are build artifacts and are not
source-controlled.

`tools/rust_shadow.py` is an explicit hardware-independent comparison helper.
Given caller-supplied bytes, it invokes both parsers and compares all fields,
raw bytes, failure category, and source-side derivation. It neither opens or
intercepts Bluetooth traffic nor logs packet data, chooses a production parser,
or falls back between implementations. Production code never imports it.

## Protocol core persistent daemon plan

Iteration 9.6 hardware evidence favored a daemon because the reliable lifecycle is
one continuously open AAP session with one descriptor bootstrap and repeated
HR START/STOP cycles. The planned daemon would own that session while multiple
local consumers came and went:

```text
subscriber_count 0 -> 1:  START HR
subscriber_count 1 -> 0:  STOP HR; keep AAP channel READY
application exits:        daemon and READY AAP session remain alive
```

This design avoided treating each application lifetime as a new AAP channel.
Protocol core did not implement the daemon, subscriber accounting, IPC, or service
management. Daemon service subsequently implemented and validated that persistent
daemon design.

## Authority and migration gates

The Python parser and protocol constants remain authoritative for wire
behavior. `production_session.py`, `bluez_coexistence.py`, `monitor_cli.py`,
`hr_semantics.py`, and their existing tests remain authoritative for current
production behavior and evidence handling.

Before Rust can replace any Python path, a later task must:

1. Keep every safe golden vector passing through both parsers and expand the
   corpus for any newly proven structure.
2. Review the private binding boundary before defining any end-user API.
3. Design supported-platform wheel and source-build behavior without changing
   the current setuptools installation until that packaging is accepted.
4. Define an explicit production integration and rollback plan; the FFI binding
   shadow helper is manual and is not a runtime selection mechanism.
5. Keep all hardware-independent Python, core, binding, and FFI parity checks
   mandatory on supported development environments.
6. Perform separately authorized hardware validation of persistent-session,
   descriptor gating, cleanup, BlueZ ownership, and audio coexistence.
7. Stage any production switch so the proven Python path remains available
   until parity, rollback, and failure behavior are accepted.

Successful FFI binding FFI parity establishes a private native bridge. It does
not authorize a production parser switch or satisfy the hardware migration
gates.

## Iteration 9.7 final pass

Iteration 9.7 is accepted as a final pass. Protocol core established the std-only,
unsafe-free `airpods-aap-core` parity foundation and its shared safe golden
corpus. FFI binding added the separate private `_airpods_aap_core` PyO3 bridge
for real native FFI parity testing. The Python parser remains authoritative;
no production parser selection, Bluetooth behavior, public API, or runtime
dependency changed.

Testing environment validated Python 3.14.6, rustc and Cargo 1.97.0, PyO3
0.29.2, and maturin 1.15.0. The final accepted results were 601 Python tests
passed with zero failures and zero skips, and 13 Rust tests passed with zero
failures.
