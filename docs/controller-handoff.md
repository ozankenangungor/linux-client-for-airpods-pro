# Experimental controller handoff probe

This Daemon probe probe isolates one question: can a Linux Bluetooth controller be
temporarily handed from BlueZ to Bumble's HCI user channel while `bluetoothd`
continues running? It does not connect to AirPods, import pairing credentials,
or perform AAP communication.

## Proposed experiment

The probe reads the requested BlueZ `org.bluez.Adapter1` object through the
system D-Bus, records its `Powered` property, and asks BlueZ to power it down.
After observing `Powered=false`, it opens Bumble's
`hci-socket:<controller-index>` transport for about two seconds and then closes
it. It repeatedly queries BlueZ's ObjectManager for a newly exposed adapter
object and restores the original `Powered` value.

`bluetoothd` is intended to remain running throughout this experiment. The
probe does not manage the service or invoke command-line Bluetooth management
tools.

HCI user-channel access is exclusive. Powering down the adapter may disconnect
active Bluetooth devices using it. The probe displays only the requested
adapter name and its boolean `Powered` state; it does not display adapter
addresses or enumerate devices.

## Safety and restoration boundary

The handoff uses nested `try`/`finally` cleanup through async context managers.
On normal completion, transport acquisition failure, exceptions while held, or
ordinary asyncio cancellation, it releases the Bumble transport and attempts
to rediscover the adapter before restoring and verifying its original state.
An adapter that started off is kept off. Rediscovery and state transitions have
bounded timeouts, and restoration failures use dedicated errors.

Closing the process file descriptor releases kernel ownership of the HCI user
channel. Restoring BlueZ's previous `Powered` property is a separate userspace
cleanup operation. `SIGKILL`, sudden power loss, interpreter failure, or a
second interruption during cleanup can prevent property restoration. This is
an experimental probe, not crash-proof product recovery.

## Usage

Safe dry run:

```console
PYTHONPATH=src python3.14 tools/probe_controller_handoff.py --adapter hci0
```

The probe prints its plan and exits without loading the live backends or
changing adapter state. A live run requires `--execute`, suitable permissions
for HCI user-channel access, and installation of the optional
`handoff-probe` dependencies. Live execution should occur only after code
review and while temporary disconnection of adapter users is acceptable.
