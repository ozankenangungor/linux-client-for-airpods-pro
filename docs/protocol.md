# Experimentally observed protocol details

This document records only behavior verified on the current AirPods Pro 3 test
setup. It does not claim that these details are stable across models or firmware.

## Verified

- Apple AAP communication uses Classic L2CAP PSM `0x1001`.
- The heart-rate service ID is `0x13`.
- Heart-rate reports are located by searching the outer AAP packet for the byte
  marker `3a 16 08 13 1a 12` and taking exactly 18 bytes after it.
- The report ID at byte 0 is `0x01`.
- Byte 1 contains BPM. This mapping has been strongly validated experimentally.
- Bytes 3–4 are a little-endian, monotonically increasing sequence counter.
- Bytes 6–13 form a little-endian 64-bit counter or timestamp-like value.
- Bytes 14–17 form a little-endian 32-bit flags or status field.
- The outer message contains a variable-length protobuf varint sequence/message
  ID. Equivalent outer packets can therefore grow from 40 to 41 bytes when the
  value no longer fits in a one-byte varint. Parsing by a fixed outer length or
  offset is unsafe; marker-based parsing handles both forms.
- Heart-rate streaming has been received with Bumble on the tested setup.
- Four BlueZ-compatible SDP record roles were required on that working setup:
  `PnPInformation`, `HandsfreeAudioGateway`, `AudioSource`, and
  `A/V RemoteControlTarget`.
- The observed AAP handshake request is
  `00 00 04 00 01 00 02 00 00 00 00 00 00 00 00 00`. Its observed ACK is
  `01 00 04 00 00 00 01 00 03 00 00 00 00 00 00 00 00 00`.

The observed 18-byte report layout is:

| Bytes | Decoding | Status |
| --- | --- | --- |
| `0` | Report ID (`0x01`) | Verified |
| `1` | BPM | Strongly validated experimentally |
| `2` | `aux` byte | Semantics unknown |
| `3:5` | Little-endian 16-bit sequence | Verified behavior |
| `5` | Unknown 8-bit field | Semantics unknown |
| `6:14` | Little-endian 64-bit increasing value | Official unit unknown |
| `14:18` | Little-endian 32-bit field | Flag meanings unknown |

## Observed but semantics unknown

- `report[2]` is retained as a neutral auxiliary byte. No confidence, quality,
  or signal interpretation has been established.
- `report[5]` is retained as an unknown 8-bit field.
- `report[6:14]` increases by approximately 1,000,000,000 per one-second sample,
  but its official unit has not been independently proven.
- `report[14:18]` is retained as a flags/status value, but the meanings of its
  bits have not been established.

## Known integration limitations

- With BlueZ, the same observed start-heart-rate sequence receives an
  acknowledgement but no periodic heart-rate samples.
- With Bumble, the stream succeeds on the tested setup.
- Bumble 0.0.234 requires compatibility behavior for an incoming L2CAP
  Configure Request containing `FLUSH_TIMEOUT`. The option is accepted and
  echoed in the reply rather than rejected as unsupported. The project now
  provides a version-guarded, manager-scoped negotiation adapter without
  modifying Bumble. It has hardware-independent coverage; composing it with an
  AAP connection remains future work.
- The reviewed controller handoff and local pairing-credential preparation are
  not yet composed with an AirPods connection. Reconnect/recovery and a
  user-friendly installation flow remain unimplemented.
- This stage does not implement server behavior for additional Bluetooth PSMs.
- Post-handshake traffic has contained the strings `AccessoryService`,
  `devmotion6`, `MaxReportSize`, `ReportDescriptor`, `HeartRateService`,
  `HeartRate`, and `com.apple.hid.heartrate-access`. These are descriptor
  evidence only; the complete descriptor format and semantics are not claimed.
