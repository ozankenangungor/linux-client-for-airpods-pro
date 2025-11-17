# Classic authentication-only probe

Classic auth composes the existing read-only discovery and credential preparation
with the reviewed controller handoff. The live path is designed to connect to
one paired candidate over BR/EDR, authenticate with the imported in-memory
LinkKey, enable encryption, disconnect, release the HCI transport, and let the
controller handoff restore BlueZ. It does not open an L2CAP application channel,
install SDP records, send AAP data, or start heart-rate streaming.

The diagnostic is a dry run unless `--execute` is explicitly supplied. The
project does not invoke privilege-escalation tools; any privileges required by
the eventual controlled experiment must be arranged externally.

## Bumble 0.0.234 logging inspection

The installed Bumble source has several paths relevant to Classic key secrecy:

- `bumble.host` logs complete controller-to-host and host-to-controller HCI
  objects at DEBUG. A Link Key Notification event and a Link Key Request Reply
  command contain the raw key and can therefore be rendered by these messages.
- `bumble.host.Host.on_hci_link_key_notification_event()` has a separate DEBUG
  message that formats the notified LinkKey as hexadecimal.
- `bumble.hci` includes the entire raw HCI packet as hexadecimal in an ERROR
  message if packet parsing fails. A malformed packet could contain key bytes.
- `bumble.device.Device.get_link_key()` logs only key presence or absence at
  DEBUG/WARNING. It does not format the key.
- `bumble.device.Device.on_link_key()` passes key material to the keystore but
  does not itself log the value. `Device.update_keys()` logs an exception at
  ERROR if storage fails; the project's read-only error contains no key value.
- Bumble's generic pairing-key printing and dictionary conversion can expose
  key values, but the project's in-memory key type disables both paths.

No inspected Bumble INFO path formatted a Classic LinkKey. The ERROR-level HCI
packet parsing path means an INFO- or WARNING-only policy would still be
insufficient.

Before creating or powering a Bumble Device, `harden_bumble_logging()` sets the
`bumble` logger and the inspected `bumble.host`, `bumble.hci`, `bumble.device`,
`bumble.keys`, and `bumble.transport` logger families to a level above CRITICAL.
It disables those loggers and applies the same controls to already-created
`bumble.*` loggers. The setting is process-wide and is intentionally not
restored after authentication.

## Authentication and cleanup order

The session performs all BlueZ-dependent reads before handoff:

1. Discover candidates and require exactly one.
2. Load its Classic credentials from the configured BlueZ pairing store.
3. Construct the read-only in-memory Bumble keystore.
4. Harden Bumble logging.
5. Enter `ControllerHandoff` and obtain the active transport through a small
   wrapper around the accepted transport backend.
6. Create a Bumble `Device` with Classic enabled and LE disabled, attach the
   in-memory keystore, and power it on.
7. Construct the peer using Bumble's BR/EDR address API and connect using
   `PhysicalTransport.BR_EDR`.
8. Authenticate and verify `connection.authenticated`.
9. Enable encryption and verify the connection's encryption state.
10. Disconnect in a `finally` block.
11. Power off the temporary Bumble Device, release the HCI transport, and let
    `ControllerHandoff` restore the original BlueZ `Powered` state.

The real Bumble adapter bounds connection and security operations to 20
seconds, disconnect to 5 seconds, and Device power transitions to 10 seconds.
These values are configurable at the runtime-factory boundary.

Connection, authentication, encryption, and ordinary asyncio-cancellation
paths all pass through the same cleanup contexts. Process termination such as
SIGKILL or sudden power loss still cannot guarantee restoration.

## Replacement-key policy

Existing-key authentication handles an HCI Link Key Request through
`Device.get_link_key()` and performs only a keystore read. Bumble calls
`KeyStore.update()` if the controller separately emits an HCI Link Key
Notification. The in-memory store reports this event through a callback that
receives no address or key value, then rejects the update. The probe can display
a non-secret replacement-key diagnostic, but it never persists the replacement
or modifies BlueZ pairing storage.
