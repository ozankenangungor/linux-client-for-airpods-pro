# Local pairing discovery and credential preparation

Discovery layer prepares an existing local BlueZ pairing for later use by Bumble. It
does not pair, connect, authenticate, open AAP, or start heart-rate streaming.
The device must already belong to, or be controlled by, the user and must have
been paired through BlueZ.

## Device discovery

Discovery reads `org.bluez.Device1` and the associated `org.bluez.Adapter1`
objects from BlueZ's D-Bus ObjectManager. The initial matching rule accepts only
paired devices whose `Name` or `Alias` contains `AirPods` as a distinct token.
This is a candidate rule rather than a compatibility claim.

No match produces a clear error. One match can be selected automatically.
Multiple matches remain an explicit list for a future user-choice flow; the
library does not silently choose among them. The diagnostic tool displays only
the device display name, paired status, and adapter name. It does not display
Bluetooth addresses.

## Address and path safety

Both adapter and device addresses must be canonical six-octet hexadecimal
Bluetooth addresses. They are normalized to uppercase before use. Malformed
values, path separators, and traversal strings are rejected before any
address-derived filesystem path is built.

The pairing parser accepts a configurable storage root. Its production default
is the standard BlueZ root, `/var/lib/bluetooth`, with this layout:

```text
<storage-root>/<validated-adapter-address>/<validated-device-address>/info
```

The parser reads the Classic `[LinkKey]` section and retains `Key`, `Type`, and
optional `PINLength`. A device can be paired without having a Classic LinkKey,
which is reported separately from missing files, invalid data, and permission
failures. Reading the real BlueZ pairing store normally requires elevated read
permission. This project does not invoke `sudo`, install a helper, or alter file
permissions.

## Secret boundary

A Bluetooth LinkKey is a secret. The `BluetoothLinkKey` type redacts both
`str()` and `repr()`. Errors never include key text, and the library does not
log, upload, or persist imported keys. The raw bytes cross only the intentionally
named internal conversion boundary that constructs Bumble pairing objects.

Python byte objects cannot be reliably zeroized and this design does not claim
hardened secret memory. Its goal is to prevent accidental display and disk
persistence while keeping the credential local to the process.

## Bumble representation

`InMemoryBumbleKeyStore` implements Bumble 0.0.234's asynchronous `KeyStore`
interface. `get(peer_address)` returns a `PairingKeys` object whose `link_key`
is a `PairingKeys.Key`, while `link_key_type` preserves BlueZ's numeric type.
The `authenticated` flag is retained for authenticated P-192 and P-256
combination-key types. Bumble's Classic key lookup then consumes the key from
memory by peer address. The adapter disables Bumble's inherited key-printing
and dictionary-serialization paths so an imported key is not accidentally
displayed or passed to a persistent keystore.

Bumble 0.0.234 formats public addresses with a `/P` suffix and passes that
qualified string to `KeyStore.get()` for a Classic LinkKey lookup. The adapter
accepts exactly the qualified public form, removes the suffix, and then applies
the project's strict address validation. Unqualified, random-qualified,
malformed, and traversal-containing names are rejected. `get_all()` returns the
same `/P` public form expected by Bumble. The filesystem-safe
`BluetoothAddress` type itself remains unqualified and does not accept `/P`.

During existing-key authentication, Bumble handles an HCI Link Key Request by
calling `Device.get_link_key()`, which only reads the keystore. Bumble calls
`KeyStore.update()` when the controller emits an HCI Link Key Notification,
which represents receipt of key material to store. This imported keystore keeps
its read-only rejection behavior: it will not persist or silently adopt a newly
generated replacement key that could diverge from BlueZ's pairing state.

BlueZ's optional `PINLength` is preserved in the project's internal
`ClassicPairingCredentials`. Bumble 0.0.234's `PairingKeys` has no corresponding
Classic PIN-length field, so it is not placed into the Bumble object.

At Discovery layer, this layer was not yet connected to the packaged CLI or to a Bumble
`Device`; the final one-command experience was future work. Later tasks
integrated it into the hardware-proven research monitor path.
