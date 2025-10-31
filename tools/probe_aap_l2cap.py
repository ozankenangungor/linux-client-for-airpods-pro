#!/usr/bin/env python3.14
"""Opt-in signaling-only probe for the Classic AAP L2CAP channel."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TextIO

from airpods_hr.aap_channel import (
    AAPChannel,
    AAPChannelError,
    AAPChannelProgress,
    AAPChannelSession,
    AAPL2CAPProbeSession,
)
from airpods_hr.authentication import (
    AuthenticationProgress,
    BumbleClassicRuntimeFactory,
    ClassicAuthenticationError,
    ClassicAuthenticationSession,
    create_controller_handoff_transport,
)
from airpods_hr.bluetooth import (
    AdapterRestoreError,
    DBusNextBlueZBackend,
    HandoffError,
)
from airpods_hr.discovery import (
    BlueZDeviceDiscovery,
    DBusNextManagedObjectsBackend,
    MultipleAirPodsCandidatesError,
    NoAirPodsCandidatesError,
)
from airpods_hr.pairing import BlueZPairingStore, PairingStoreError


LiveRunner = Callable[[Callable[[str], None]], Awaitable[None]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Experimentally open and close AAP Classic L2CAP PSM 0x1001 "
            "without sending application payload."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the state-changing AAP L2CAP open experiment",
    )
    return parser


def _authentication_progress(
    output: Callable[[str], None],
) -> Callable[[AuthenticationProgress, str | None], None]:
    def emit(event: AuthenticationProgress, detail: str | None) -> None:
        if event is AuthenticationProgress.DEVICE_SELECTED:
            output(f"Device: {detail or 'AirPods'}")
        elif event is AuthenticationProgress.CONNECTED:
            output("BR/EDR connection: OK")
        elif event is AuthenticationProgress.AUTHENTICATED:
            output("Authentication: OK")
        elif event is AuthenticationProgress.ENCRYPTED:
            output("Encryption: OK")
        elif event is AuthenticationProgress.DISCONNECTED:
            output("Disconnect: OK")
        elif event is AuthenticationProgress.REPLACEMENT_KEY_REPORTED:
            output("Controller reported a new/replacement link key.")

    return emit


def _channel_progress(
    output: Callable[[str], None],
) -> Callable[[AAPChannelProgress, AAPChannel | None], None]:
    def emit(event: AAPChannelProgress, channel: AAPChannel | None) -> None:
        if event is AAPChannelProgress.OPENED and channel is not None:
            output("AAP L2CAP channel: OPEN")
            output(f"Mode: {channel.mode}")
            output(f"Local MTU: {channel.local_mtu}")
            output(f"Peer MTU: {channel.peer_mtu}")
            output("Application payload sent: no")
        elif event is AAPChannelProgress.CLOSED:
            output("AAP L2CAP close: OK")

    return emit


async def run_live_probe(output: Callable[[str], None]) -> None:
    discovery_backend = DBusNextManagedObjectsBackend()
    bluez_backend = DBusNextBlueZBackend()
    try:
        await discovery_backend.connect()
        await bluez_backend.connect()
        handoff, transport = create_controller_handoff_transport(bluez_backend)
        await transport.ensure_available()

        secure_session = ClassicAuthenticationSession(
            BlueZDeviceDiscovery(discovery_backend),
            BlueZPairingStore(),
            handoff,
            BumbleClassicRuntimeFactory(),
            progress=_authentication_progress(output),
        )
        session = AAPL2CAPProbeSession(
            secure_session,
            AAPChannelSession(progress=_channel_progress(output)),
        )
        await session.run()
        output("BlueZ restoration: OK")
    finally:
        discovery_backend.close()
        bluez_backend.close()


async def run_probe(
    *,
    execute: bool,
    output: Callable[[str], None] = print,
    live_runner: LiveRunner = run_live_probe,
) -> int:
    if not execute:
        output("DRY RUN: no Bluetooth state will be changed.")
        output("Planned operations:")
        output("  1. Discover one paired AirPods candidate.")
        output("  2. Load its existing local Classic credentials.")
        output("  3. Hand the powered-down controller from BlueZ to Bumble.")
        output("  4. Connect over BR/EDR, authenticate, and enable encryption.")
        output("  5. Enable FLUSH_TIMEOUT compatibility and open AAP PSM 0x1001.")
        output("  6. Send no AAP application payload.")
        output("  7. Close, disconnect, release the controller, and restore BlueZ.")
        output("Use --execute only after reviewing the experiment.")
        return 0

    try:
        await live_runner(output)
        return 0
    except NoAirPodsCandidatesError:
        output("FAIL: no paired AirPods candidate was found.")
    except MultipleAirPodsCandidatesError:
        output("FAIL: multiple paired AirPods candidates require selection.")
    except PairingStoreError:
        output("FAIL: existing local Classic credentials could not be loaded.")
    except AAPChannelError:
        output("FAIL: AAP L2CAP channel probe failed; cleanup was attempted.")
    except AdapterRestoreError:
        output("FAIL: BlueZ adapter restoration reported an error.")
    except HandoffError:
        output("FAIL: controller handoff failed; cleanup was attempted.")
    except ClassicAuthenticationError:
        output("FAIL: Classic security session failed; cleanup was attempted.")
    except asyncio.CancelledError:
        raise
    except Exception:
        output("FAIL: unexpected AAP L2CAP probe error.")
    return 1


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit = print if stream is None else lambda message: print(message, file=stream)
    try:
        return asyncio.run(run_probe(execute=args.execute, output=emit))
    except KeyboardInterrupt:
        emit("FAIL: interrupted; channel close and restoration were attempted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
