#!/usr/bin/env python3.14
"""Experimental, opt-in BlueZ-to-Bumble controller handoff probe."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TextIO

from airpods_hr.bluetooth import (
    AdapterRestoreError,
    AdapterState,
    BumbleHCITransportBackend,
    ControllerHandoff,
    DBusNextBlueZBackend,
    HandoffError,
)

BackendFactory = Callable[
    [], Awaitable[tuple[DBusNextBlueZBackend, BumbleHCITransportBackend]]
]
HandoffFactory = Callable[
    [DBusNextBlueZBackend, BumbleHCITransportBackend], ControllerHandoff
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Experimentally hand one powered-down BlueZ adapter to Bumble's "
            "HCI user channel without managing bluetooth.service."
        )
    )
    parser.add_argument(
        "--adapter",
        default="hci0",
        help="controller name such as hci0 (default: hci0)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the state-changing handoff experiment",
    )
    return parser


async def create_runtime_backends(
) -> tuple[DBusNextBlueZBackend, BumbleHCITransportBackend]:
    bluez = DBusNextBlueZBackend()
    await bluez.connect()
    return bluez, BumbleHCITransportBackend()


async def run_probe(
    *,
    adapter_name: str,
    execute: bool,
    output: Callable[[str], None] = print,
    backend_factory: BackendFactory = create_runtime_backends,
    handoff_factory: HandoffFactory = ControllerHandoff,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    if not execute:
        output("DRY RUN: no Bluetooth state will be changed.")
        output(f"Adapter: {adapter_name}")
        output("Planned operations:")
        output("  1. Verify org.bluez and read the adapter Powered state.")
        output("  2. Set Powered=false through BlueZ D-Bus and verify it.")
        output("  3. Acquire Bumble's exclusive HCI user-channel transport.")
        output("  4. Hold for approximately 2 seconds, then release it.")
        output("  5. Rediscover the adapter and restore its original state.")
        output("Use --execute only after reviewing the experiment.")
        return 0

    bluez: DBusNextBlueZBackend | None = None
    original: AdapterState | None = None
    try:
        bluez, transport = await backend_factory()
        await bluez.ensure_available()
        await transport.ensure_available()

        handoff = handoff_factory(bluez, transport)
        original = await handoff.inspect(adapter_name)
        output(f"Adapter: {original.name}")
        output(f"Original Powered state: {original.powered}")
        output(
            "WARNING: active Bluetooth connections on this adapter may "
            "temporarily disconnect."
        )

        async with handoff.handoff(original):
            output("HCI user-channel transport acquired; holding for 2 seconds.")
            await sleep(2.0)

        output("PASS: transport released and original Powered state restored.")
        return 0
    except AdapterRestoreError as error:
        output(f"FAIL: restoration operation reported an error: {error}")
        if bluez is not None and original is not None:
            try:
                final_state = await bluez.get_adapter(original.name)
            except Exception:
                output("Final adapter state could not be read.")
            else:
                output(
                    "Final observed adapter state: "
                    f"Powered={final_state.powered}"
                )
        return 1
    except HandoffError as error:
        output(f"FAIL: {error}")
        return 1
    finally:
        if bluez is not None:
            bluez.close()


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit = print if stream is None else lambda message: print(message, file=stream)
    try:
        return asyncio.run(
            run_probe(
                adapter_name=args.adapter,
                execute=args.execute,
                output=emit,
            )
        )
    except KeyboardInterrupt:
        emit("FAIL: interrupted; cleanup was attempted before exit.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
