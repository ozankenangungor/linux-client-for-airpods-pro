# AAP Classic L2CAP channel probe

Adds a reusable transport-only session for Apple AAP and a separate,
safe-by-default diagnostic. The layer opens Classic L2CAP PSM `0x1001`, checks
that Bumble reports an open Basic-mode channel, exposes negotiated MTU values,
and closes the channel. It does not perform an AAP handshake or send any AAP
application payload.

## Bumble 0.0.234 behavior

An authenticated Bumble `Connection` exposes
`create_l2cap_channel(ClassicChannelSpec(...))`. That delegates through the
owning `Device.l2cap_channel_manager` to
`ChannelManager.create_classic_channel()`. The manager creates a
`ClassicChannel` and awaits `ClassicChannel.connect()`. Its connection future
is completed only when Bumble's state machine reaches `ClassicChannel.State.OPEN`
after successful configuration in both directions, so a successful create call
returns a fully configured channel.

`ClassicChannel.disconnect()` sends an L2CAP Disconnection Request and awaits
the matching response. AAP channel bounds both creation and close with independent,
configurable timeouts.

`ClassicChannel.mtu` is Bumble's local receive MTU, advertised to the peer.
`ClassicChannel.peer_mtu` is the peer's receive MTU and limits outbound data.
The diagnostic reports both observed properties. It does not require or
hardcode a particular peer MTU.

## Compatibility and mode lifetime

`AAPChannelSession` enters the accepted
`aap_flush_timeout_compatibility()` context on the connection's actual
`ChannelManager` before channel creation begins. It creates
`ClassicChannelSpec(psm=0x1001, mode=TransmissionMode.BASIC)`, verifies the
returned PSM, state, mode, and MTUs, and keeps compatibility active throughout
the held channel lifetime. Cleanup closes the L2CAP channel before restoring
the manager's original handler.

The outer secure-session context then disconnects BR/EDR, powers off the
temporary Bumble Device, releases HCI ownership, and lets the controller
handoff restore BlueZ. If opening, negotiation, timeout, or cancellation fails,
the same nested contexts unwind. A primary operation error is retained when a
secondary channel-close failure also occurs.

## Probe boundary

Run the diagnostic without flags to inspect its plan:

```console
PYTHONPATH=src python3.14 tools/probe_aap_l2cap.py
```

The default path constructs no D-Bus or Bumble backend. At source review, the
planned `--execute` run was limited to security and L2CAP signaling. Its
channel body is intentionally empty, and the project-owned channel view has no
payload send API. The probe also installs no SDP service records. The
subsequently accepted signaling-only run opened the real AAP PSM on test
hardware; SDP and AAP application behavior are handled by a separate layer.
