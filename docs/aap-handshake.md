# Minimal SDP compatibility and AAP handshake

Adds the first bounded AAP application exchange. Its diagnostic remains
safe by default. A controlled hardware run with the public project-default
runtime profile and host-state snapshot disabled successfully installed four
temporary SDP records, opened Classic L2CAP PSM `0x1001`, sent one known
handshake request, recognized its exact acknowledgement, and observed the
sensor-framework and HeartRateService descriptor evidence.

## Temporary SDP compatibility profile

Earlier experiments found that an empty local Bumble SDP database led to the
ACL connection disappearing after peer SDP queries. Four minimal records kept
that session alive: `PnPInformation`, `HandsfreeAudioGateway`, `AudioSource`,
and `A/V RemoteControlTarget`. The AAP handshake profile contains exactly these four.
They reproduce an interoperability profile and do not claim that this project
implements complete HFP, A2DP, or AVRCP endpoints.

PnP vendor, product, and version values come from a strictly parsed BlueZ
`Adapter1.Modalias` with the form `usb:vVVVVpPPPPdDDDD`. Missing, malformed, or
non-USB values fail explicitly. The project does not invent fallback hardware
IDs. The observed PnP vendor source value is USB. The HFP AG record uses RFCOMM
channel 13; AudioSource identifies L2CAP/AVDTP PSM `0x0019` and AVDTP version
`0x0103`; the AVRCP Target profile descriptor identifies AVRCP version
`0x0106`.

The adapter Modalias is captured with the selected candidate and parsed before
controller handoff. After the temporary Bumble Device is powered, the records
replace its in-memory SDP mapping before `runtime.connect()` begins. They stay
installed through connection, authentication, encryption, AAP channel setup,
handshake, and descriptor observation, then the previous mapping is restored
before BR/EDR disconnect. Bumble's already-registered SDP server answers queries
from that mapping. This does not modify BlueZ SDP state, persist records, or
register application servers for PSM 3, 23, or 25.

### Bumble 0.0.234 SDP wiring and diagnostics

`bumble.device.Device.__init__()` constructs one `sdp.Server` and immediately
registers its `on_connection` handler as the Classic L2CAP PSM 1 server on the
Device's `ChannelManager`. The `Device.sdp_service_records` property reads and
writes that same server's `service_records` attribute. Each
`sdp.Server.match_services()` call reads the current attribute, so replacing
`device.sdp_service_records` updates the active server; it does not leave the
server using an earlier record dictionary.

Hardware-independent replay tests pass real serialized ServiceSearchAttribute
requests through the Device's registered PSM 1 handler and
`sdp.Server.on_pdu()`. They verify the four expected responses and an empty
response for an unknown service UUID.

For the next controlled run, an instance-scoped observer records only counters
and booleans. It reports whether an SDP channel was accepted, counts known and
unknown SDP queries, notes whether each expected record matched, and counts
peer-initiated connection requests for PSM 1, 3, 23, 25, and other PSMs. The
observer delegates to Bumble without changing acceptance or response behavior
and restores its instance hooks after the temporary profile exits. It is gated
to Bumble 0.0.234.

## Handshake and observation

The only AAP handshake AAP payload is this 16-byte request:

```text
00 00 04 00 01 00 02 00 00 00 00 00 00 00 00 00
```

Acknowledgement succeeds only when one complete received SDU exactly equals
the experimentally observed 18-byte value:

```text
01 00 04 00 00 00 01 00 03 00 00 00 00 00 00 00 00 00
```

Every non-ACK frame received after sending the request can contribute descriptor
evidence, including frames that arrive before the exact ACK. After the ACK, a
separate bounded window continues checking received frames for known non-secret
strings. `AccessoryService`, `devmotion6`, `MaxReportSize`, or
`ReportDescriptor` count as sensor-framework evidence. The implementation also
tracks `HeartRateService`, a distinct `HeartRate` token, and
`com.apple.hid.heartrate-access`. These are observed markers; the surrounding
descriptor format and every field's meaning are not asserted.

Normal output contains booleans derived from these markers. A descriptor
timeout also reports only pre-ACK/post-ACK frame counts and the receive drop
count. Raw descriptors, device-specific values, and complete packets are not
printed. Missing ACK and missing required descriptor evidence have distinct
errors. The ACK wait remains five seconds and the post-ACK descriptor window
remains three seconds by default; preserving early evidence fixes the observed
ordering loss without adding an arbitrary sleep.

The timeout diagnostic retains at most 64 safe frame summaries across both
receive phases. Each summary contains only byte length and the complete
little-endian 16-bit values available at fixed header byte pairs 2:4 and 4:6.
They are reported neutrally as `header_u16_2_3` and `header_u16_4_5`; no protocol
meaning is assigned. Short frames omit unavailable fields. No payload bytes or
arbitrary strings are retained or printed. The bounded monotonic timeline also
records the first post-ACK frame regardless of its length, while retaining the
357-byte event as a separate compatibility observation.

Post-ACK frames with `header_u16_4_5 == 0x002B` are an observed frame family
whose lengths have varied between sessions. Historical successful evidence
included a 357-byte example, while recent stalled sessions included both
357-byte and 51-byte examples. Frame length alone therefore does not determine
whether the handshake will progress to descriptor evidence.

For this frame family, the diagnostic interprets bytes 7:9 only as a neutral
observed little-endian value and compares it with the number of bytes after
offset 17. When those lengths agree and the body is exactly divisible into
17-byte units, it reports the unit count and a histogram of only the final
three bytes of each unit. The histogram uses neutral suffix-field names, sorts
entries deterministically, retains at most eight entries, and separately
reports the complete distinct-pair count. It may also report whether bytes
8..13 were uniform across all complete units, but never exposes or hashes that
six-byte value. The diagnostic does not claim that the length value or the
17-byte units are official protocol fields or records, and their semantic
meaning remains unknown. Unit bytes 0..13 and all other raw frame content are
intentionally neither retained nor logged because they may contain identifying
or address-like data.

The proven PoC and this implementation use the same Classic/LE flags and other
DeviceConfiguration defaults, the same four SDP record shapes, Basic-mode PSM
`0x1001`, a receive sink installed before the handshake, and the same handshake
bytes. Their configured local names differ. The PoC assigned records before
`power_on()` while this project assigns them after `power_on()` and before
`runtime.connect()`; source inspection and real-server replay show that SDP
state is not consulted during power-on and both assignments reach the same
server. The PoC used a persistent JSON keystore while this project uses the
reviewed in-memory keystore. In Basic mode, a real-class spy confirms the PoC's
`send_pdu()` and the project's `write()` reach the same Bumble channel send
path.

## Receive and cleanup boundaries

Bumble 0.0.234 delivers Classic application SDUs synchronously through
`ClassicChannel.sink` from `ClassicChannel.on_sdu()`. `BumbleAAPTransport`
temporarily replaces that sink with an `asyncio.Queue` callback, caps both the
queue length and frame size, drops the oldest queued frame when full, and
restores the previous sink on normal exit, failure, or cancellation. Receives
wait asynchronously with bounded timeouts and do not busy-poll.

The FLUSH_TIMEOUT compatibility context covers channel creation, handshake,
descriptor observation, and channel close. Cleanup then restores the channel
handler, removes the temporary SDP record mapping, disconnects BR/EDR, powers
off the temporary Bumble Device, releases the HCI transport, and restores the
original BlueZ power state.

AAP handshake sends no heart-rate control command, does not use workout opcode `0x44`,
and does not parse BPM reports. AAP handshake.3 verified that all four expected SDP
matches were served. AAP handshake.4 added a bounded monotonic timeline and a
best-effort allowlisted host-state snapshot. AAP handshake.4.1 makes that snapshot
explicitly opt-in so its HCI Read commands do not perturb the default
pre-connect baseline and corrects NUL-padded local-name comparison. The final
controlled AAP handshake run succeeded with the project-default name and the snapshot
disabled; the legacy-name experiment is unnecessary.
