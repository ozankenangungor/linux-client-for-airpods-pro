# AirPods AAP L2CAP compatibility

This document describes a narrow interoperability behavior observed on the
tested AirPods Pro 3 setup. It is not a general description of every model or
firmware.

## Observed request and stock behavior

After accepting a Classic L2CAP connection for Apple AAP PSM `0x1001`, the
tested peer sent this Configure Request option sequence:

```text
01 02 16 0a
02 02 1e 00
04 09 00 00 00 00 00 00 00 00 00
```

The options are an MTU of 2582, a two-byte `FLUSH_TIMEOUT` value whose raw
bytes are `1e 00`, and a retransmission/flow-control option selecting Basic
mode. Bumble 0.0.234 handles the MTU and Basic-mode options but has no
`FLUSH_TIMEOUT` branch in `ClassicChannel.on_configure_request`. It therefore
uses its unknown-option path, sends `FAILURE_UNKNOWN_OPTIONS`, and echoes only
the rejected option.

The earlier hardware experiment established that echoing the peer's
`FLUSH_TIMEOUT` option in a successful Configure Response was sufficient for
this AAP channel to open. That observation does not establish broader device
or firmware compatibility.

## Project compatibility mechanism

`airpods_hr.bumble_compat.aap_flush_timeout_compatibility()` is a scoped
context manager for one Bumble `ChannelManager`. Bumble 0.0.234 has no public
configure-option hook or Classic channel factory, so the context temporarily
installs a handler on that manager instance. It leaves Bumble's classes and
other manager instances unchanged.

For PSM `0x1001`, the handler recognizes exactly one complete type `0x02`
option with a two-byte value. It removes that option while delegating MTU,
Basic-mode retransmission/flow-control, state transitions, and all other
validation to stock Bumble. If Bumble's delegated response succeeds, the
handler restores the exact peer option sequence in the response. Other PSMs,
malformed or duplicate `FLUSH_TIMEOUT` options, and unrelated unknown options
retain stock Bumble behavior. Nested activation on the same manager is
idempotent, and the original instance handler is restored when the outermost
context exits.

The adapter is guarded for Bumble 0.0.234 and the reviewed internal API shape.
It raises a clear compatibility error for another version or replaced handler
instead of modifying an unknown implementation. The optional dependency is
therefore pinned to that exact Bumble release.

## Deliberate limits

The compatibility layer changes only L2CAP configuration negotiation. It does
not interpret the `1e 00` value, issue an HCI Write Automatic Flush Timeout
command, configure controller flush behavior, open PSM `0x1001`, implement AAP,
or connect to a device. It never edits Bumble or requires a manual
`site-packages` patch.
