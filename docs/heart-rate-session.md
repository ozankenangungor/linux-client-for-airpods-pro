# Heart-rate sessions

Adds a bounded experimental session on top of the live-proven AAP
handshake and descriptor path. The probe is a dry run unless `--execute` is
supplied. One controlled end-to-end AirPods Pro 3 interoperability run has
validated this bounded path, including activation, sample observation, HR
cleanup, disconnection, and BlueZ restoration. This is narrow compatibility
evidence rather than a general firmware, controller, or model guarantee.

## Observed command sequence

The session transmits only commands copied byte-for-byte from the previously
working private proof of concept. Their names are operational labels rather
than Apple-documented protocol terminology. Activation uses this fixed order:

1. `STOP_HEAD`, followed by the observed service-`0x0e` acknowledgement.
2. `CONNECT0`, `CAPS0`, and `CONNECT4`, followed by the observed connect
   acknowledgement.
3. `CAPS4`, `HR_ON`, and `START_HR`, followed by the observed service-`0x13`
   acknowledgement.

If `START_HR` was sent, cleanup attempts `STOP_HR` and waits briefly for the
observed service-`0x13` acknowledgement. If `HR_ON` was sent, cleanup then
sends `HR_OFF`. A missing stop acknowledgement does not prevent `HR_OFF` or
the outer AAP, Classic, HCI, and BlueZ cleanup.

The unconfirmed workout packet containing `0x44` is not part of the command
set and is not required by this implementation.

## Timing and bounds

The working proof of concept allowed a 1.5-second capability/bootstrap period
from transmission of the AAP handshake request. This duration is an observed
compatibility value, not an official protocol requirement. Heart-rate session records a
monotonic handshake-send timestamp. If descriptor discovery finishes early,
it waits only until that 1.5-second minimum has elapsed; if discovery already
took at least 1.5 seconds, it adds no delay.

The probe targets five valid reports by default and limits stream observation
to 12 seconds. It stops at the first bound. Zero valid samples is a failure;
one or more samples below the target is a bounded partial result; reaching the
target is a pass. Control acknowledgement waits and stop acknowledgement waits
are independently bounded.

## Receive and parsing behavior

One bounded receive collector remains installed across the handshake,
descriptor observation, control acknowledgements, sample stream, and stop
acknowledgement. The collector restores the raw Bumble channel sink once when
the complete AAP/heart-rate operation exits.

Service acknowledgements are recognized from stable observed envelope bytes,
one observed fixed header word, an internally bounded trailing-length field, a
canonical one-byte or two-byte varying identifier, and the stable
service-specific suffix. These checks treat the frame as an observed byte
structure and do not assign official meanings to its fields. The `CONNECT4`
acknowledgement uses the exact stable frame observed in the proven runs.

Historical successful sessions placed `10 01` after the varying identifier;
a controlled project run later observed `10 03` with the same exact
service-specific and bootstrap remainders. Both are accepted as observed
control-prefix variants. Their semantic meaning is unknown. The classifiers
continue to require the exact envelope, lengths, one/two-byte identifier,
allowlisted prefix, and complete expected remainder.

When the first `STOP_HEAD` acknowledgement is absent, the probe retains at
most eight structural summaries of frames consumed by that bounded wait. Each
summary contains only byte length, allowlisted fixed-offset 16-bit values,
envelope/length/tag booleans, acknowledgement-suffix booleans, a candidate
identifier termination/octet-count/canonical summary when safely derivable,
neutral post-identifier lengths and fixed-position byte values, terminal-tag
metadata, a bounded count and numeric terminal values for the observed
`62 02 08 XX` structure, known bootstrap-tail suffix/classifier booleans, the
heart-rate-marker boolean, and a monotonic consumption time relative to
`STOP_HEAD`. The identifier parser accepts at most five octets for diagnostics
and never retains or reports the identifier value. It stores no frame bytes or
decoded payload strings.

When at least two post-identifier octets exist, the summary retains those two
octets only as separate bounded integers. It compares the bytes after them
against four exact historical remainder shapes and records at most four numeric
offsets for observed `62 02 08 XX` groups. These comparisons remain diagnostic:
they do not make a changed-prefix frame an acknowledgement or bootstrap-tail
match, and no post-identifier byte sequence is retained.

For the observed bootstrap-tail frames, the diagnostic requires the 16-bit
word at offsets 10–11 to equal the total frame length minus 12. This is only an
evidence-backed structural check; the field's protocol meaning remains unknown.

The transport also reports only its pending receive-queue count. The session
captures that queue snapshot immediately before sending `STOP_HEAD` without
draining or otherwise changing the queue. If the snapshot is `N`, the first
`N` frames subsequently consumed by the acknowledgement wait were already
locally queued before `STOP_HEAD`. Their relative times describe when the wait
consumed them, not when the receive sink originally observed them. These
diagnostics do not change the accepted acknowledgement classifier, the
1.5-second bootstrap rule, retries, or command ordering.

Samples are parsed only by
`airpods_hr.heartrate.parse_heart_rate_packet()`. The parser searches for the
heart-rate marker, so it does not assume a fixed outer AAP packet size or
offset. Marker-free frames are counted as non-heart-rate traffic. A frame that
contains the marker but has a malformed report is counted separately and does
not terminate the bounded observation window.

Only BPM is displayed by the bounded probe. The report ID and BPM mapping are
known. `aux`, `field_5`, and `flags` retain unresolved semantics. `sequence` is
the raw observed little-endian sequence/counter field; its wrap behavior is not
established. The 64-bit increasing value is exposed as `timestamp_ticks`; its
official tick period has not been independently established. The parser also
retains `raw_report`, the exact validated 18 bytes following the verified
heart-rate marker.

## Continuous monitor core

Iteration 9.1 adds a separate hardware-independent continuous-monitor core without
changing the bounded Heart-rate session session. After the same handshake, descriptors,
1.5-second minimum bootstrap interval, command order, and strict
acknowledgement checks, `HeartRateMonitorActivationSession` observes reports
until a caller-owned `asyncio.Event` is set. It has no sample target and no
overall stream deadline.

The monitor calls `receive()` with a 0.5-second timeout by default. An idle
timeout is treated as an empty poll, so the stream continues while stop-event
latency remains bounded when no packets arrive. If the event is already set
when activation completes, the monitor skips observation and performs normal
cleanup. An explicit stop with zero samples is a successful continuous result.

Each valid report is delivered immediately through the existing synchronous
`HeartRateProgress.SAMPLE` callback. The result retains only a sample count,
acknowledgement status, payload and frame counters, and malformed/non-HR frame
counts. It does not retain reports or raw received frames, so storage does not
grow with monitoring duration.

Normal monitor cleanup sends `STOP_HR`, performs the same bounded optional
service-`0x13` acknowledgement wait, and then sends `HR_OFF`. A missing stop
acknowledgement is reported without preventing `HR_OFF` or outer cleanup.
Transport failures and progress-callback failures remain authoritative after
state-aware cleanup. `asyncio.CancelledError` likewise propagates after
best-effort cleanup, including cancellation during the optional stop-ACK wait.

`HeartRateMonitorSession` composes this continuous path with the same secure
Classic session, SDP profile, AAP channel, handshake, and single receive
collector as the bounded path. Iteration 9.1 does not add reconnect or retry policy.
One controlled AirPods Pro 3 run validated the continuous path through eight
samples, caller-owned event stop, protocol cleanup, disconnection, and BlueZ
restoration. This remains narrow interoperability evidence rather than a broad
compatibility claim.

### Continuous-monitor validation probe

Iteration 9.1.1 adds `tools/probe_heart_rate_monitor.py` as a safe-by-default,
finite validation harness around the Iteration 9.1 continuous core. The harness
owns the caller-side `asyncio.Event`. It sets that event when its validation
sample target is reached or when a safety window expires after
`START_HR` has been acknowledged. Both cases use the monitor's normal
event-driven `STOP_HR`, optional acknowledgement, and `HR_OFF` cleanup path.

These external validation bounds do not add a sample target or overall stream
deadline to `HeartRateMonitorActivationSession`. The continuous core remains
event-driven. The probe performs no Bluetooth access unless `--execute` is
explicitly supplied. It remains a finite validation harness and is separate
from the continuous product command.

## Product CLI signal lifecycle

Iteration 9.2 connects the existing continuous session to `airpods-hr monitor`.
Samples are written to standard output as BPM-only lines. Connection,
readiness, shutdown, warning, and error messages are written to standard error.
`airpods-hr monitor --dry-run` prints its plan without accessing D-Bus, pairing
storage, HCI, or Bluetooth.

The CLI owns the same `asyncio.Event` passed to the continuous session and
tracks `HeartRateProgress.START_ACKNOWLEDGED`. The first SIGINT or SIGTERM
before that acknowledgement cancels the active composed-session task because
the stop event does not interrupt deterministic activation. The existing
monitor cancellation path then performs state-aware cleanup and unwinds the
AAP, Classic, HCI, and BlueZ contexts. The conventional resulting exit codes
are 130 for SIGINT and 143 for SIGTERM.

After the start acknowledgement, the first signal sets the caller-owned stop
event without initially cancelling the task. This uses the normal `STOP_HR`,
bounded optional stop-acknowledgement wait, `HR_OFF`, and outer restoration
path. A second SIGINT or SIGTERM during either shutdown path cancels the same
active task and relies on the monitor core's cancellation rules. It does not
directly exit the process or create another cleanup operation.

Signal handlers are installed only around a live monitor invocation and are
removed after its task completes. Help and dry-run paths install none. Iteration 9.2
adds no retry, reconnect, suspend/resume hook, daemon behavior, or
machine-readable output. The packaged command later completed one controlled
AirPods Pro 3 run using
`sudo /home/kenan/airpods-hr-linux/.venv/bin/airpods-hr monitor`. It produced
continuous samples and, after Ctrl+C, reported that monitoring stopped and
Bluetooth ownership and BlueZ state were restored. This is evidence for the
tested setup only.

## Measurement-integrity diagnostics

CLI diagnostics adds `airpods-hr monitor --diagnostic` at the existing product CLI
boundary. `HeartRateMonitorActivationSession` remains the sole producer of
parsed `HeartRateReport` objects. The ordinary BPM display and diagnostic
capture branch only after `parse_heart_rate_packet()` has validated and decoded
the report. There is no second parser, receive loop, Bluetooth session, or
protocol state machine.

Diagnostic terminal output contains the host monotonic observation time,
elapsed session milliseconds, BPM, `aux`, `sequence`, `field_5`,
`timestamp_ticks`, `flags`, and lowercase hexadecimal for the exact original
18-byte report. It does not reinterpret the unresolved fields or convert the
device timestamp to a guessed unit. No filtering, smoothing, startup
suppression, or outlier rejection is applied.

`--diagnostic --output PATH` writes UTF-8 JSON Lines using `schema_version` 1.
The file contains one `session_start` event, one `heart_rate_sample` event for
every successfully emitted parsed report, and one `session_stop` event when it
can be finalized safely. Each sample includes `host_monotonic_ns`, elapsed
milliseconds, the six decoded fields, and `raw_report_hex`. Session-stop data
includes duration, emitted-report count, and only a termination reason the CLI
can distinguish. The path is created exclusively and is never overwritten.
Running through `sudo` follows ordinary Unix ownership rules, so a newly
created capture is normally root-owned.

The event keys are deliberately allowlisted:

- `session_start`: `schema_version`, `event`,
  `host_monotonic_reference_ns`, and `wall_clock_utc`.
- `heart_rate_sample`: `schema_version`, `event`, `host_monotonic_ns`,
  `elapsed_ms`, `bpm`, `aux`, `sequence`, `field_5`, `timestamp_ticks`,
  `flags`, and `raw_report_hex`.
- `session_stop`: `schema_version`, `event`, `host_monotonic_ns`,
  `elapsed_ms`, `heart_rate_samples_emitted`, and `termination_reason`.

Numeric fields remain JSON numbers. `raw_report_hex` uses deterministic
lowercase hexadecimal with two characters per report byte. `--output` without
`--diagnostic` is rejected as an invalid argument combination.

The recorder opens the requested output and writes `session_start` before live
Bluetooth composition begins. A write failure raised from the existing sample
callback follows the monitor core's state-aware HR cleanup, AAP/Classic/HCI
unwind, BlueZ restoration, and close-once D-Bus backend ownership. JSONL
finalization then occurs after the composed monitor has unwound. A protocol or
adapter-restoration failure remains authoritative if diagnostic finalization
also fails.

## Safety scope

The transport exposes separate handshake transmission and a closed
`HeartRateCommand` operation. It does not expose a generic application-byte
send method. A complete normal session sends ten AAP application payloads:
one handshake, seven activation commands, and two cleanup commands.

After the authenticated-session context exits, Heart-rate session can report successful
BlueZ restoration even when an inner protocol error is re-raised. A dedicated
`AdapterRestoreError` remains authoritative and suppresses that success line;
the diagnostic callback neither repeats nor performs restoration.

Heart-rate output is experimental and provides no clinical or medical
accuracy guarantee. It must not be used for diagnosis, treatment, or
safety-critical monitoring.
