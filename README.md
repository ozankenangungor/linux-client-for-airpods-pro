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
- A persistent `airpods-hubd` user service owns one production session and
  fans heart-rate events out over private Unix JSONL IPC. Its repeated
  START/STOP and graceful systemd lifecycle have passed real hardware tests.
- The unpublished v0.1 Rust and Python `airpods-client` release candidates
  consume protocol version 1 without opening Bluetooth or starting the daemon.
  Both clients have passed real AirPods Pro 3 validation through the production
  daemon while A2DP audio remained uninterrupted.
- The production distribution includes a relocatable, project-owned systemd
  user-service installer. It derives the daemon interpreter from the installed
  Python environment and does not depend on a repository checkout. The
  installed distribution and real Rust SDK path have passed their SDK packaging
  hardware gate while A2DP audio remained uninterrupted.
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
- Publication of the Rust and Python v0.1 SDK release candidates; neither is
  published yet, and v0.1 is not a 1.0 stability promise.

## Experimental BlueZ coexistence probe

Adds a separate production-candidate transport experiment. The
existing `airpods-hr monitor` command continues to use the known-good Bumble
research backend, which temporarily takes direct controller ownership. Its
backend and lifecycle have not changed.

The coexistence probe instead leaves BlueZ active and opens an outgoing Linux
kernel `AF_BLUETOOTH` / `SOCK_SEQPACKET` / `BTPROTO_L2CAP` socket to AAP PSM
`0x1001` on the already paired and connected link. It asks the kernel for
medium Bluetooth security and never reads a LinkKey. Before opening the socket,
it compares the four compatibility service UUIDs from the accepted Bumble SDP
identity with `Adapter1.UUIDs`; only missing records are temporarily registered
through BlueZ `ProfileManager1`. All probe-owned profiles and the L2CAP socket
are removed during cleanup. The probe does not disconnect the AirPods, power
the adapter off, acquire an HCI user channel, or fall back to Bumble.

Review the deterministic no-access plan:

```console
PYTHONPATH=src .venv/bin/python tools/probe_bluez_coexistence.py
```

You can run the opt-in experiment, without `sudo` first, while the
AirPods are already connected through BlueZ and continuous audio is playing:

```console
PYTHONPATH=src .venv/bin/python tools/probe_bluez_coexistence.py --execute --samples 5
```

Only if that command fails solely with an access or capability error should
retry the same operation with elevated privileges:

```console
sudo env PYTHONPATH=src .venv/bin/python tools/probe_bluez_coexistence.py --execute --samples 5 --verbose
```

The probe reports BlueZ reachability, adapter power, and
`Device1.Connected` at every phase and after cleanup. Controlled AirPods Pro 3
runs have received canonical heart-rate reports while retaining the BlueZ
connection at every checkpoint and leaving A2DP audio uninterrupted. This is
feasibility evidence for the tested hardware and firmware, not a broad
compatibility guarantee. No public coexistence or heart-rate library API is
frozen by this experiment. See
[docs/bluez-coexistence.md](docs/bluez-coexistence.md) for the complete owner
procedure, evidence, and remaining limitations.

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
sudo .venv/bin/airpods-hr monitor
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
sudo .venv/bin/airpods-hr monitor --diagnostic
```

Diagnostic events can also be preserved as UTF-8 JSON Lines:

```console
sudo .venv/bin/airpods-hr monitor --diagnostic --output /tmp/airpods-hr-task9.3.jsonl
```

The output path must not already exist; this avoids overwriting evidence.
When the monitor runs under `sudo`, normal Unix behavior usually makes the new
file root-owned. Diagnostic capture preserves received values without
filtering, smoothing, startup suppression, or outlier rejection.

Adds a separate private BlueZ-coexistence semantics capture tool for
ordered baseline and same-AAP-channel restart experiments. It does not change
the monitor or assign meanings to unresolved report fields. See
[docs/hr-semantics.md](docs/hr-semantics.md).

Adds a private persistent-session core that performs one descriptor
handshake and supports repeated canonical HR activation/stop cycles on the same
AAP channel. Its interface is not public or frozen. See
[docs/production-session.md](docs/production-session.md).

Adds a standard-library-only Rust protocol core and a shared safe
golden corpus. Python remains authoritative and no production path calls Rust.
Adds a separate private development binding for real FFI parity
testing without changing the setuptools package or production parser.
See [docs/rust-core-architecture.md](docs/rust-core-architecture.md).

Adds the persistent local `airpods-hubd` service. Adds the
v0.1 Rust and Python client SDK release candidates over its Unix IPC, and Iteration 9.10A adds the relocatable user-service installer. Applications do not acquire
Bluetooth through either client boundary. See
[docs/hubd-architecture.md](docs/hubd-architecture.md) and
[docs/client-sdk-architecture.md](docs/client-sdk-architecture.md). Installation
instructions are in [docs/daemon-installation.md](docs/daemon-installation.md).
The local release build, manifest, CI, and manual release gates are documented
in [docs/release-process.md](docs/release-process.md).

Legacy direct-controller research probes may require elevated privileges to
read existing BlueZ pairing material and acquire HCI ownership. The production
hubd path uses the hardware-proven BlueZ coexistence transport and does not take
direct controller ownership. No command invokes `sudo` or performs privilege
escalation. There is no automatic reconnect after connection loss.
Compatibility evidence is limited to the tested AirPods Pro 3 setup and does
not establish broad firmware, controller, distribution, or AirPods-model
support.

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
