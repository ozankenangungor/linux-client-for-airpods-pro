#!/usr/bin/env python3.14
"""Safe diagnostic for read-only paired AirPods candidate discovery."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TextIO

from airpods_hr.discovery import (
    BlueZDeviceDiscovery,
    DBusNextManagedObjectsBackend,
    DeviceDiscoveryError,
)

BackendFactory = Callable[[], Awaitable[DBusNextManagedObjectsBackend]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "List paired AirPods candidates from public BlueZ Device1 metadata. "
            "This tool never reads pairing storage."
        )
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="perform read-only BlueZ Device1 discovery",
    )
    return parser


async def create_backend() -> DBusNextManagedObjectsBackend:
    backend = DBusNextManagedObjectsBackend()
    await backend.connect()
    return backend


async def run_probe(
    *,
    discover: bool,
    output: Callable[[str], None] = print,
    backend_factory: BackendFactory = create_backend,
) -> int:
    if not discover:
        output("DRY RUN: no D-Bus query or pairing-storage read will occur.")
        output("Planned operation: list paired AirPods candidates from Device1.")
        output("Use --discover to perform the read-only D-Bus query.")
        return 0

    backend: DBusNextManagedObjectsBackend | None = None
    try:
        backend = await backend_factory()
        candidates = await BlueZDeviceDiscovery(backend).discover_candidates()
        if not candidates:
            output("No paired AirPods candidates found.")
            return 0

        output(f"Paired AirPods candidates: {len(candidates)}")
        for index, candidate in enumerate(candidates, start=1):
            output(
                f"  {index}. {candidate.display_name} "
                f"(paired=yes, adapter={candidate.adapter_name})"
            )
        if len(candidates) > 1:
            output("Multiple candidates found; no device was selected.")
        return 0
    except DeviceDiscoveryError as error:
        output(f"FAIL: {error}")
        return 1
    finally:
        if backend is not None:
            backend.close()


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit = print if stream is None else lambda message: print(message, file=stream)
    return asyncio.run(run_probe(discover=args.discover, output=emit))


if __name__ == "__main__":
    raise SystemExit(main())
