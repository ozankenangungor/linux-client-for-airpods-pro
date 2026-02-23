#!/usr/bin/env python3.14
"""Hardware-independent lifecycle proof for the installed daemon runner."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from airpods_hr._hubd.main import run_daemon
from airpods_hr._hubd.production import ProductionHubConfig, create_production_hub
from airpods_hr._hubd.protocol import PROTOCOL_VERSION
from airpods_hr.heartrate import HeartRateReport


class ProbeSession:
    def __init__(self) -> None:
        self.opens = 0
        self.closes = 0
        self.reports: asyncio.Queue[HeartRateReport] = asyncio.Queue()

    async def open(self) -> None:
        self.opens += 1

    async def start(self) -> None:
        pass

    async def receive_report(self) -> HeartRateReport:
        return await self.reports.get()

    async def stop(self) -> None:
        pass

    async def close(self) -> None:
        self.closes += 1


class ProbeBuilder:
    def __init__(self) -> None:
        self.calls = 0
        self.session = ProbeSession()

    def __call__(self, **_kwargs: Any) -> ProbeSession:
        self.calls += 1
        return self.session


class NoopSignalRegistrar:
    def add(self, _signum: Any, _callback: Any) -> None:
        pass

    def remove(self, _signum: Any) -> None:
        pass


class QuietLogger:
    def info(self, _message: str, *_args: Any) -> None:
        pass

    def error(self, _message: str, *_args: Any) -> None:
        pass

    def debug(self, _message: str, *_args: Any) -> None:
        pass


async def _connect(path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    deadline = asyncio.get_running_loop().time() + 2.0
    while True:
        try:
            return await asyncio.open_unix_connection(path)
        except (FileNotFoundError, ConnectionRefusedError):
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("runner probe socket did not become ready")
            await asyncio.sleep(0.01)


async def _request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    operation: str,
) -> dict[str, Any]:
    writer.write(
        json.dumps(
            {"protocol_version": PROTOCOL_VERSION, "operation": operation}
        ).encode()
        + b"\n"
    )
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), timeout=1.0))


def _lock_is_released(path: Path) -> bool:
    descriptor = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    finally:
        os.close(descriptor)


async def run_probe(*, output=print) -> int:
    builder = ProbeBuilder()
    with tempfile.TemporaryDirectory(prefix="airpods-hubd-runner-") as directory:
        socket_path = Path(directory) / "airpods-hubd.sock"
        lock_path = socket_path.with_suffix(".lock")
        stop = asyncio.Event()

        def build_hub(path: Path, **kwargs: Any) -> Any:
            return create_production_hub(
                path,
                config=kwargs["config"],
                output=kwargs["output"],
                builder=builder,
            )

        runner = asyncio.create_task(
            run_daemon(
                socket_path,
                ProductionHubConfig(),
                shutdown_event=stop,
                registrar=NoopSignalRegistrar(),
                hub_builder=build_hub,
                logger=QuietLogger(),
            )
        )
        reader, writer = await _connect(socket_path)
        try:
            ping = await _request(reader, writer, "ping")
            status = await _request(reader, writer, "status")
        finally:
            writer.close()
            await writer.wait_closed()
        stop.set()
        exit_code = await asyncio.wait_for(runner, timeout=2.0)
        socket_removed = not socket_path.exists()
        lock_released = lock_path.exists() and _lock_is_released(lock_path)

    output(f"runner_exit={exit_code}")
    output(f"factory_calls={builder.calls}")
    output(f"session_opens={builder.session.opens}")
    output(f"ping_ok={str(ping.get('pong') is True).lower()}")
    output(f"status_state={status.get('state')}")
    output(f"session_closes={builder.session.closes}")
    output(f"socket_removed={str(socket_removed).lower()}")
    output(f"lock_released={str(lock_released).lower()}")
    passed = (
        exit_code == 0
        and builder.calls == 1
        and builder.session.opens == 1
        and ping.get("pong") is True
        and status.get("state") == "ready"
        and builder.session.closes == 1
        and socket_removed
        and lock_released
    )
    output("HUBD RUNNER FAKE PROBE PASS" if passed else "HUBD RUNNER FAKE PROBE FAIL")
    return 0 if passed else 1


def main() -> int:
    return asyncio.run(run_probe())


if __name__ == "__main__":
    raise SystemExit(main())
