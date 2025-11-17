# Classic runtime parity audit (AAP handshake.4)

This document compares the reviewed project with the previously proven Bumble
PoC. It contains no controller address, peer address, pairing value, or raw
packet content. Bumble 0.0.234 was inspected in both environments. Their
`device.py`, `host.py`, `sdp.py`, and driver package are byte-identical. The
only Bumble source difference is the already-known local FLUSH_TIMEOUT case in
the old `l2cap.py`.

## DeviceConfiguration parity

Both runtimes construct the same Bumble 0.0.234 `DeviceConfiguration` defaults
except where shown below.

| Field or behavior | Proven PoC | Project default | Classification |
| --- | --- | --- | --- |
| `name` | legacy PoC name | project diagnostic name | **PLAUSIBLE LIVE A/B VARIABLE** |
| `classic_enabled` | `True` | `True` | **ELIMINATED BY SOURCE/TEST** |
| `le_enabled` | `False` | `False` | **ELIMINATED BY SOURCE/TEST** |
| configured address | Bumble default; controller public address read after reset | same | **ELIMINATED BY SOURCE/TEST** |
| `class_of_device` | default `0` | default `0` | **ELIMINATED BY SOURCE/TEST** |
| Classic Secure Connections | default enabled | default enabled | **ELIMINATED BY SOURCE/TEST** |
| Secure Simple Pairing | default enabled | default enabled | **ELIMINATED BY SOURCE/TEST** |
| Classic SMP | default enabled | default enabled | **ELIMINATED BY SOURCE/TEST** |
| accept incoming Classic connections | default enabled | default enabled | **ELIMINATED BY SOURCE/TEST** |
| interlaced Classic scanning | default enabled | default enabled | **ELIMINATED BY SOURCE/TEST** |
| connectable | default `True` | default `True` | **ELIMINATED BY SOURCE/TEST** |
| discoverable | default `True` | default `True` | **ELIMINATED BY SOURCE/TEST** |
| IO capability | default no-input/no-output | same | **ELIMINATED BY SOURCE/TEST** |
| authentication-enable setting | not explicitly configured | same | **UNKNOWN controller default** |
| local-name/EIR source | legacy PoC name | project diagnostic name | **PLAUSIBLE LIVE A/B VARIABLE** |
| inquiry/page scan state | discoverable/connectable with interlaced scan when supported | same | **ELIMINATED BY SOURCE/TEST** |
| page timeout and scan activity | not explicitly configured by Device | same | **UNKNOWN controller default** |
| outgoing role/link policy | BR/EDR central request, role switch allowed; no explicit default link-policy write | same | **ELIMINATED for request path; UNKNOWN controller default** |
| GAP/GATT defaults | enabled but LE is disabled | same | **ELIMINATED BY SOURCE/TEST** |
| L2CAP feature configuration | Bumble defaults | same | **ELIMINATED BY SOURCE/TEST** |
| keystore | configured for creation during `power_on()` | in-memory store assigned before `power_on()` | **ELIMINATED BY SOURCE/TEST** |

The local name remains a plausible variable because `Device.power_on()` sends
`HCI_Write_Local_Name`, and `Device.set_discoverable()` builds Extended Inquiry
Response data containing that name. The peer can therefore observe it through
normal Classic procedures. The project keeps its existing name by default and
offers the reviewed `legacy-poc` profile only as an explicit probe option.

Keystore timing does not change the provider path. `Device.host` installs the
bound method `Device.get_link_key` as `Host.link_key_provider`. Each invocation
of that method reads the current `device.keystore`; no store object or LinkKey
is captured during construction. Both paths have a store available before
`power_on()` finishes and before authentication begins.

## Device and Host power-on audit

`Device.power_on()` first calls `Host.reset()`. In stock Bumble 0.0.234,
`Host.reset()` performs controller/driver initialization, normally including
`HCI_Reset`, reads supported commands, version, features, and buffer sizes,
then programs event masks. For the Classic configuration, `Device.power_on()`
then writes:

- local name;
- Class of Device;
- Simple Pairing mode;
- Secure Connections host support;
- connectable/discoverable scan enable;
- Extended Inquiry Response containing the configured local name;
- interlaced page/inquiry scan type when supported.

Bumble does not explicitly write page timeout, authentication-enable,
connection-accept timeout, page/inquiry scan activity, default link policy, or
voice setting in this path. `HCI_Reset` may establish controller-defined
defaults, but Bumble does not read and compare all of those settings. Any
vendor or controller state surviving the different BlueZ ownership path is
therefore **UNKNOWN** until observed. The probe offers best-effort HCI Read
diagnostics only for commands modeled by Bumble 0.0.234. This snapshot is
disabled by default because the extra controller transactions and their delay
made the AAP handshake.4 live baseline differ from the AAP handshake.3 pre-connect path. It
must be enabled explicitly with `--classic-host-snapshot`. Unsupported reads
are reported as unavailable rather than implemented with raw commands.

`HCI_Read_Local_Name_ReturnParameters.local_name` is a 248-octet `bytes` field
in Bumble 0.0.234. Its diagnostic mapper displays only the bytes before the
first NUL, but the value returned to the observer remains fixed-width. The
observer now applies the same NUL boundary before strict UTF-8 decoding and
comparison. It immediately reduces the result to yes, no, or unavailable; no
raw or normalized name is retained.

## SDP before power_on

This candidate is **ELIMINATED BY SOURCE/TEST**. Neither `Device.power_on()`,
`Host.reset()`, `Device.set_connectable()`, nor `Device.set_discoverable()`
reads `sdp_service_records` or the SDP server. EIR synthesis uses only the
local name. The SDP server is registered with the L2CAP manager during Device
construction, and the `Device.sdp_service_records` property directly replaces
`sdp_server.service_records`. Existing real-server replay tests prove the
active server observes the temporary mapping before the BR/EDR connection.

## L2CAP Configure Response parity

For the observed MTU, FLUSH_TIMEOUT, and Basic-mode option sequence, the old
patch appended every accepted option in request order. The project shim lets
stock parsing update the peer MTU and Basic-mode state, then emits a successful
Configure Response with the exact original option sequence. A test serializes
the real Bumble response and compares it byte-for-byte with that proven-patch
response model, including identifier, source CID, flags, result, option order,
and option values. This candidate is **ELIMINATED BY SOURCE/TEST**.

## Basic-mode send parity

In Bumble 0.0.234, `ClassicChannel.write(sdu)` calls
`Processor.send_sdu(sdu)`. The Basic-mode `Processor.send_sdu` immediately
calls `ClassicChannel.send_pdu(sdu)`. Both paths therefore call
`ChannelManager.send_pdu` with the same connection, destination CID, SDU bytes,
and FCS setting. A real-class spy test verifies this. This candidate is
**ELIMINATED BY SOURCE/TEST**.

## Pre-connect diagnostic parity

Compared with AAP handshake.3, AAP handshake.4 added one controller-visible operation between
`Device.power_on()` and `runtime.connect()`: the host-state observer issued its
allowlisted HCI Read commands. Those reads did not request state changes, but
they added controller traffic and timing. Runtime-name profile selection and
snapshot/result plumbing are in-memory operations; the project-default profile
resolves to the same runtime name AAP handshake.3 already used. The SDP profile and its
diagnostic hooks were already part of AAP handshake.3. With the snapshot disabled, the
AAP handshake.4.1 interval adds only in-memory bookkeeping.

## Next controlled variable

No proven runtime defect was found. The smallest next experiment is to run the
handshake probe first with the project-default name and the host-state snapshot
disabled. Only after that non-invasive baseline is reviewed should the opt-in
runtime-name profile be changed to `legacy-poc`. The timeline records only
monotonic offsets for handshake send, exact ACK, the first post-ACK frame, an
optional first 357-byte frame, and peer-initiated PSM request classes. It never
records raw packets or peer identity.
