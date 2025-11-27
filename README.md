# airpods-hr-linux

`airpods-hr-linux` is an experimental, unofficial, reverse-engineered project
exploring heart-rate data from AirPods Pro 3 on Linux. It is not affiliated
with or endorsed by Apple. Compatibility is currently limited to the tested
AirPods Pro 3 hardware and should not be assumed for other models or firmware.

This repository packages a working experimental interoperability path and its
independently tested components. One controlled AirPods Pro 3 run completed
discovery, controller handoff, Classic security, AAP setup, heart-rate
activation and sampling, protocol cleanup, disconnection, and BlueZ
restoration. A later controlled run also validated continuous monitoring,
including eight samples, caller-requested stop, protocol cleanup, disconnection,
and BlueZ restoration. The packaged `airpods-hr monitor` command now connects
this path to a Unix signal lifecycle.

Heart-rate output from this project is experimental and is not intended to
provide clinical or medical accuracy guarantees. Do not use it for diagnosis,
treatment, or safety-critical monitoring.

## Project status

Working:

- AAP communication has been reverse-engineered on test hardware.
- A HeartRateService stream has been received using Bumble.
- The heart-rate report parser is known to work with observed packets and is
  covered by hardware-independent tests.
- The controller handoff probe has passed a controlled experiment while
  `bluetoothd` remained running.
- Paired AirPods candidate discovery and BlueZ Classic pairing parsing are
  implemented behind hardware-independent interfaces.
- Classic BR/EDR authentication and encryption have passed a controlled probe.
- A project-owned, Bumble 0.0.234 compatibility layer handles the observed AAP
  L2CAP `FLUSH_TIMEOUT` negotiation without editing `site-packages`.
- A reusable, signaling-only AAP channel session has opened PSM `0x1001` on
  test hardware using the project-owned FLUSH_TIMEOUT compatibility layer.
- A minimal temporary SDP compatibility profile and bounded AAP-handshake layer
  have passed a controlled live validation.
- The complete bounded one-shot path has passed one controlled end-to-end
  AirPods Pro 3 interoperability run.
- A separate continuous-monitor core emits samples without retaining an
  unbounded history and has passed one controlled AirPods Pro 3 run.
- The packaged `airpods-hr monitor` command provides continuous terminal output
  and scoped SIGINT/SIGTERM handling. It has passed one controlled live run on
  the tested AirPods Pro 3 setup, including graceful Ctrl+C cleanup and BlueZ
  restoration.

Not yet implemented:

- Reconnect and recovery handling.
- Suspend and resume recovery.
- Daemon operation and API/SDK integration.
- A user-friendly installer.

## Continuous monitor command

Install the package and its declared runtime dependencies in an isolated
environment:

```console
python3.14 -m pip install -e .
```

Review the no-access plan without querying D-Bus, pairing storage, or Bluetooth:

```console
airpods-hr monitor --dry-run
```

Start continuous monitoring with:

```console
sudo /home/kenan/airpods-hr-linux/.venv/bin/airpods-hr monitor
```

Press `Ctrl+C` to request graceful shutdown. Once heart-rate activation has
completed, the command stops through the monitor's caller-owned event, sends
the existing cleanup commands, disconnects, and restores BlueZ ownership. A
signal received earlier cancels the active session so its existing state-aware
cleanup can unwind. A second signal escalates an in-progress shutdown to task
cancellation.

This packaged command has completed one controlled AirPods Pro 3 run. It
produced continuous BPM output and, after Ctrl+C, reported:

```text
Heart-rate monitoring stopped.
Bluetooth ownership and BlueZ state restored.
```

This is interoperability evidence for the tested setup, not a production or
broad compatibility claim.

To display the unresolved fields and exact validated 18-byte report alongside
each BPM sample, enable diagnostic mode:

```console
sudo /home/kenan/airpods-hr-linux/.venv/bin/airpods-hr monitor --diagnostic
```

Diagnostic events can also be preserved as UTF-8 JSON Lines:

```console
sudo /home/kenan/airpods-hr-linux/.venv/bin/airpods-hr monitor --diagnostic --output /tmp/airpods-hr-task9.3.jsonl
```

The output path must not already exist; this avoids overwriting evidence.
When the monitor runs under `sudo`, normal Unix behavior usually makes the new
file root-owned. Diagnostic capture preserves received values without
filtering, smoothing, startup suppression, or outlier rejection.

The currently tested setup may require elevated privileges to read existing
BlueZ pairing material and acquire direct controller ownership. The program
does not invoke `sudo` or perform privilege escalation. There is no automatic
reconnect after connection loss. Compatibility evidence is limited to the
tested AirPods Pro 3 setup and does not establish broad firmware, controller,
distribution, or AirPods-model support.

Classic auth adds a safe-by-default diagnostic for a future Classic BR/EDR
connection, authentication, and encryption experiment. It composes the
reviewed discovery, local credential, and controller-handoff layers, but it
does not open AAP or install SDP records:

```console
PYTHONPATH=src python3.14 tools/probe_classic_auth.py
```

The default invocation only prints the plan. See
[docs/classic-auth.md](docs/classic-auth.md) for the cleanup and logging-safety
boundaries before considering an explicitly enabled live experiment.

Adds a separate dry-run probe for opening and closing the AAP Classic
L2CAP transport without sending application payload or installing SDP records:

```console
PYTHONPATH=src python3.14 tools/probe_aap_l2cap.py
```

See [docs/aap-channel.md](docs/aap-channel.md) for the channel state, MTU,
timeout, compatibility-lifetime, and cleanup boundaries.

Adds another safe-by-default probe for four temporary SDP compatibility
records, one known AAP handshake request, and bounded descriptor observation:

```console
PYTHONPATH=src python3.14 tools/probe_aap_handshake.py
```

It sends no heart-rate control command. See
[docs/aap-handshake.md](docs/aap-handshake.md) for the exact scope and the
reviewed protocol and cleanup boundaries.

Adds a bounded dry-run probe for the proven heart-rate command sequence,
with five reports and a 12-second stream window as its defaults:

```console
PYTHONPATH=src python3.14 tools/probe_heart_rate.py
```

It reuses the existing marker-based report parser and does not include the
unconfirmed workout command. See
[docs/heart-rate-session.md](docs/heart-rate-session.md) for the command,
timing, parser, and cleanup boundaries.

## Experimental controller handoff probe

Adds a diagnostic probe for testing whether `bluetoothd` can remain
running while an adapter is temporarily handed to Bumble through Linux's
exclusive HCI user channel. It does not connect to AirPods or perform any AAP
communication.

The probe is a dry run unless `--execute` is explicitly supplied:

```console
PYTHONPATH=src python3.14 tools/probe_controller_handoff.py --adapter hci0
```

The required runtime dependencies are installed with the project:

```console
python3.14 -m pip install -e .
```

`dbus-next` provides pure-Python access to BlueZ's D-Bus APIs. Bumble provides
only the `hci-socket` transport boundary in this task; no Bumble Device or
AirPods connection is created.

During an executed probe, the selected adapter is powered down through BlueZ,
so active connections on that adapter may drop. The original `Powered` state is
restored after normal completion, handled exceptions, and ordinary asyncio
cancellation. HCI ownership is released when the transport closes. Restoration
of the BlueZ property cannot be guaranteed after `SIGKILL`, sudden power loss,
or another termination that prevents Python cleanup from running.

See [docs/controller-handoff.md](docs/controller-handoff.md) before considering
a live experiment.

## Local pairing credential preparation

Adds a read-only discovery and credential-preparation layer. It finds
paired AirPods candidates from BlueZ `Device1` metadata, validates the adapter
and device Bluetooth addresses used for storage lookup, and parses an existing
Classic LinkKey into a read-only, in-memory Bumble `KeyStore`. Multiple matching
devices are returned for an eventual user choice; the library does not silently
select one.

This functionality is for a device the user owns or controls and has already
paired locally. Bluetooth LinkKeys are secrets: they must remain on the local
machine and must never be uploaded, printed, or logged. Reading the real BlueZ
pairing store at `/var/lib/bluetooth` usually requires elevated read permission;
the library reports permission failures and never invokes privilege-escalation
tools. Python cannot provide hardened or reliably zeroized secret memory, so the
implemented boundary focuses on avoiding accidental display and persistence.

The new diagnostic is a no-access dry run by default:

```console
PYTHONPATH=src python3.14 tools/probe_device_discovery.py
```

Its explicit `--discover` mode reads only public BlueZ D-Bus device metadata.
The tool has no mode that reads pairing storage, connects to a device, or changes
Bluetooth state. See [docs/pairing.md](docs/pairing.md) for the security and API
boundaries.

## Development

The project targets Python 3.14 and uses a `src/` package layout. The test suite
does not require Bluetooth hardware or access to live system services. Install
the project into an isolated environment before running it:

```console
python3.14 -m pip install -e .
PYTHONPATH=src python -m unittest discover -s tests -v
```

After installation, command help is available without Bluetooth access:

```console
airpods-hr
```

See [docs/protocol.md](docs/protocol.md) for the verified protocol observations
and current integration limitations. The scoped L2CAP negotiation adapter is
documented in [docs/l2cap-compat.md](docs/l2cap-compat.md).

## License

Original code in this repository is available under the MIT License. See
[LICENSE](LICENSE).
