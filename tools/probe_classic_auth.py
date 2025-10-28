#!/usr/bin/env python3.14
"""Opt-in Classic connection, authentication, and encryption probe."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TextIO

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
            "Experimentally reuse an existing local Classic pairing for a "
            "BR/EDR authentication-only session."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the state-changing authentication experiment",
    )
    return parser


def _progress_output(
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


async def run_live_authentication(output: Callable[[str], None]) -> None:
    discovery_backend = DBusNextManagedObjectsBackend()
    bluez_backend = DBusNextBlueZBackend()
    try:
        await discovery_backend.connect()
        await bluez_backend.connect()
        handoff, transport = create_controller_handoff_transport(bluez_backend)
        await transport.ensure_available()

        session = ClassicAuthenticationSession(
            BlueZDeviceDiscovery(discovery_backend),
            BlueZPairingStore(),
            handoff,
            BumbleClassicRuntimeFactory(),
            progress=_progress_output(output),
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
    live_runner: LiveRunner = run_live_authentication,
) -> int:
    if not execute:
        output("DRY RUN: no Bluetooth state will be changed.")
        output("Planned operations:")
        output("  1. Discover one paired AirPods candidate.")
        output("  2. Load its existing local Classic credentials.")
        output("  3. Hand the powered-down controller from BlueZ to Bumble.")
        output("  4. Connect over BR/EDR, authenticate, and enable encryption.")
        output("  5. Disconnect, release the HCI transport, and restore BlueZ.")
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
    except AdapterRestoreError:
        output("FAIL: BlueZ adapter restoration reported an error.")
    except HandoffError:
        output("FAIL: controller handoff failed; cleanup was attempted.")
    except ClassicAuthenticationError:
        output("FAIL: Classic authentication session failed; cleanup was attempted.")
    except asyncio.CancelledError:
        raise
    except Exception:
        output("FAIL: unexpected authentication-probe error.")
    return 1


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit = print if stream is None else lambda message: print(message, file=stream)
    try:
        return asyncio.run(
            run_probe(
                execute=args.execute,
                output=emit,
            )
        )
    except KeyboardInterrupt:
        emit("FAIL: interrupted; disconnect and restoration were attempted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
