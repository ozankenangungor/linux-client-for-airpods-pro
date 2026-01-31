#!/usr/bin/env python3.14
"""Exercise the private daemon foundation with fake sessions and local IPC."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from airpods_hr._hubd.protocol import PROTOCOL_VERSION
from airpods_hr._hubd.server import AirPodsHubDaemon
from airpods_hr.heartrate import HeartRateReport


class ProbeSession:
    def __init__(self) -> None:
        self.opens = 0
        self.starts = 0
        self.reports_read = 0
        self.stops = 0
        self.closes = 0
        self.reports: asyncio.Queue[HeartRateReport] = asyncio.Queue()

    async def open(self) -> None:
        self.opens += 1

    async def start(self) -> None:
        self.starts += 1

    async def receive_report(self) -> HeartRateReport:
        report = await self.reports.get()
        self.reports_read += 1
        return report

    async def stop(self) -> None:
        self.stops += 1

    async def close(self) -> None:
        self.closes += 1


class ProbeFactory:
    def __init__(self) -> None:
        self.created = 0
        self.session: ProbeSession | None = None

    def __call__(self) -> ProbeSession:
        self.created += 1
        self.session = ProbeSession()
        return self.session


class ProbeClient:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, path: Path) -> ProbeClient:
        return cls(*(await asyncio.open_unix_connection(path)))

    async def request(self, operation: str, **fields: Any) -> dict[str, Any]:
        self.writer.write(
            json.dumps(
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "operation": operation,
                    **fields,
                }
            ).encode()
            + b"\n"
        )
        await self.writer.drain()
        return json.loads(await self.reader.readline())

    async def event(self) -> dict[str, Any]:
        return json.loads(await self.reader.readline())

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


def _sample(index: int) -> HeartRateReport:
    return HeartRateReport(
        bpm=169 if index == 0 else 80 + index,
        aux=20,
        sequence=index,
        field_5=1 if index % 2 == 0 else 2,
        timestamp_ticks=100 + index,
        flags=0x1000,
    )


async def run_probe(*, samples: int, output=print) -> int:
    factory = ProbeFactory()
    with tempfile.TemporaryDirectory(prefix="airpods-hubd-") as directory:
        socket_path = Path(directory) / "hubd.sock"
        daemon = AirPodsHubDaemon(factory, socket_path)
        clients: list[ProbeClient] = []
        client_reports = [0, 0]
        try:
            await daemon.start()
            clients = [
                await ProbeClient.connect(socket_path),
                await ProbeClient.connect(socket_path),
            ]
            for client in clients:
                reply = await client.request("subscribe", stream="heart_rate")
                if not reply.get("ok"):
                    return 1
            assert factory.session is not None
            for index in range(samples):
                factory.session.reports.put_nowait(_sample(index))
            for client_index, client in enumerate(clients):
                for _ in range(samples):
                    event = await client.event()
                    if event.get("event") != "heart_rate":
                        return 1
                    client_reports[client_index] += 1
            for client in clients:
                reply = await client.request("unsubscribe", stream="heart_rate")
                if not reply.get("ok"):
                    return 1
        finally:
            for client in clients:
                await client.close()
            await daemon.shutdown()

    session = factory.session
    assert session is not None
    output(f"session_objects_created={factory.created}")
    output(f"session_opens={session.opens}")
    output(f"session_starts={session.starts}")
    output(f"reports_read={session.reports_read}")
    output(f"client_a_reports={client_reports[0]}")
    output(f"client_b_reports={client_reports[1]}")
    output(f"session_stops={session.stops}")
    output(f"session_closes={session.closes}")
    passed = (
        factory.created == 1
        and session.opens == 1
        and session.starts == 1
        and session.reports_read == samples
        and client_reports == [samples, samples]
        and session.stops == 1
        and session.closes == 1
    )
    output("HUBD PRIVATE PROBE PASS" if passed else "HUBD PRIVATE PROBE FAIL")
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run hardware-independent airpods-hubd fake-session proof."
    )
    parser.add_argument("--samples", type=int, choices=range(1, 17), default=3)
    return parser


def main(argv: Sequence[str] | None = None, *, stream: TextIO | None = None) -> int:
    args = build_parser().parse_args(argv)

    def emit(message: str) -> None:
        print(message, file=stream)

    return asyncio.run(run_probe(samples=args.samples, output=emit))


if __name__ == "__main__":
    raise SystemExit(main())
