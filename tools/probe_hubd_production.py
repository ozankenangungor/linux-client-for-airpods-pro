#!/usr/bin/env python3.14
"""Private opt-in end-to-end probe for hubd and the production session."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from airpods_hr._hubd.production import (
    DEFAULT_DAEMON_OPERATION_TIMEOUT,
    DEFAULT_DESCRIPTOR_TIMEOUT,
    ProductionHub,
    ProductionHubConfig,
    ProductionSessionBuilder,
    create_production_hub,
)
from airpods_hr._hubd.protocol import MAX_FRAME_SIZE, PROTOCOL_VERSION
from airpods_hr._hubd.server import DaemonState
from airpods_hr.production_session import (
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
)


DEFAULT_SAMPLE_TARGET = 5
DEFAULT_RESTART_DELAY = 5.0
DEFAULT_CLIENT_TIMEOUT = 15.0
PROBE_SOCKET_NAME = "airpods-hubd-probe.sock"
MAX_BUFFERED_EVENTS = 32

Output = Callable[[str], None]
Sleeper = Callable[[float], Awaitable[None]]
CycleReady = Callable[[int, Any], Awaitable[None]]


class ProbeFailure(RuntimeError):
    """A safe probe failure category without hardware details."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _bounded_int(minimum: int, maximum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        parsed = int(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"value must be between {minimum} and {maximum}"
            )
        return parsed

    return parse


def _bounded_float(minimum: float, maximum: float) -> Callable[[str], float]:
    def parse(value: str) -> float:
        parsed = float(value)
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"value must be between {minimum:g} and {maximum:g}"
            )
        return parsed

    return parse


def _probe_socket_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise ProbeFailure("safe_runtime_directory_unavailable")
    return Path(runtime) / PROBE_SOCKET_NAME


class ProbeClient:
    """Probe-only JSONL client with bounded reads and event interleaving."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        read_timeout: float,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._read_timeout = read_timeout
        self._events: list[dict[str, Any]] = []

    @classmethod
    async def connect(
        cls, path: Path, *, timeout: float
    ) -> ProbeClient:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    path,
                    limit=MAX_FRAME_SIZE + 1,
                ),
                timeout=timeout,
            )
        except TimeoutError as error:
            raise ProbeFailure("ipc_connect_timeout") from error
        except OSError as error:
            raise ProbeFailure("ipc_connect_failed") from error
        return cls(reader, writer, read_timeout=timeout)

    async def _read_object(self) -> dict[str, Any]:
        try:
            frame = await asyncio.wait_for(
                self._reader.readline(), timeout=self._read_timeout
            )
        except TimeoutError as error:
            raise ProbeFailure("ipc_read_timeout") from error
        except ValueError as error:
            raise ProbeFailure("ipc_invalid_frame") from error
        if not frame:
            raise ProbeFailure("ipc_connection_closed")
        if len(frame) > MAX_FRAME_SIZE + 1 or not frame.endswith(b"\n"):
            raise ProbeFailure("ipc_invalid_frame")
        try:
            message = json.loads(frame)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProbeFailure("ipc_invalid_json") from error
        if not isinstance(message, dict):
            raise ProbeFailure("ipc_non_object_message")
        if message.get("protocol_version") != PROTOCOL_VERSION:
            raise ProbeFailure("ipc_protocol_mismatch")
        return message

    async def request(self, operation: str, **fields: Any) -> dict[str, Any]:
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "operation": operation,
            **fields,
        }
        self._writer.write(
            json.dumps(request, separators=(",", ":")).encode() + b"\n"
        )
        try:
            await asyncio.wait_for(
                self._writer.drain(), timeout=self._read_timeout
            )
        except TimeoutError as error:
            raise ProbeFailure("ipc_write_timeout") from error
        while True:
            message = await self._read_object()
            if message.get("event") == "heart_rate":
                if len(self._events) >= MAX_BUFFERED_EVENTS:
                    raise ProbeFailure("ipc_event_buffer_full")
                self._events.append(message)
                continue
            if message.get("ok") is True and message.get("operation") != operation:
                raise ProbeFailure("ipc_response_mismatch")
            return message

    async def heart_rate_events(self, count: int) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while len(events) < count:
            if self._events:
                message = self._events.pop(0)
            else:
                message = await self._read_object()
            if message.get("event") != "heart_rate":
                raise ProbeFailure("ipc_expected_heart_rate_event")
            bpm = message.get("bpm")
            side = message.get("source_side")
            if type(bpm) is not int or side not in {"left", "right", "unknown"}:
                raise ProbeFailure("ipc_invalid_heart_rate_event")
            events.append(message)
        return events

    def discard_buffered_events(self) -> None:
        self._events.clear()

    async def close(self) -> None:
        self._writer.close()
        try:
            await asyncio.wait_for(
                self._writer.wait_closed(), timeout=self._read_timeout
            )
        except (ConnectionError, TimeoutError, asyncio.CancelledError):
            pass


def _require_ok(reply: dict[str, Any], operation: str) -> None:
    if reply.get("ok") is not True or reply.get("operation") != operation:
        raise ProbeFailure(f"ipc_{operation}_failed")


def _require_status(
    reply: dict[str, Any], *, state: DaemonState, subscriber_count: int
) -> None:
    _require_ok(reply, "status")
    if (
        reply.get("state") != state.value
        or reply.get("subscriber_count") != subscriber_count
    ):
        raise ProbeFailure("daemon_status_mismatch")


def _safe_failure_category(error: BaseException) -> str:
    if isinstance(error, ProbeFailure):
        return error.category
    category = getattr(error, "category", None)
    if category is not None:
        return getattr(category, "value", "production_session_failed")
    return type(error).__name__


async def _cycle_ready(_cycle: int, _session: Any) -> None:
    """Default no-op injection point used only by hardware-independent tests."""


def _render_events(label: str, events: list[dict[str, Any]], output: Output) -> None:
    for index, event in enumerate(events, 1):
        output(
            f"{label} event={index} bpm={event['bpm']} "
            f"source_side={event['source_side']}"
        )


async def run_probe(
    *,
    execute: bool,
    socket_path: Path | None = None,
    sample_target: int = DEFAULT_SAMPLE_TARGET,
    restart_delay: float = DEFAULT_RESTART_DELAY,
    client_timeout: float = DEFAULT_CLIENT_TIMEOUT,
    descriptor_timeout: float = DEFAULT_DESCRIPTOR_TIMEOUT,
    start_timeout: float = DEFAULT_START_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    daemon_operation_timeout: float = DEFAULT_DAEMON_OPERATION_TIMEOUT,
    output: Output = print,
    session_builder: ProductionSessionBuilder | None = None,
    sleep: Sleeper = asyncio.sleep,
    cycle_ready: CycleReady = _cycle_ready,
) -> int:
    """Run the private lifecycle once, or describe it without hardware access."""

    if sample_target <= 0 or restart_delay < 0 or client_timeout <= 0:
        output("HUBD PRODUCTION PROBE FAIL category=invalid_probe_configuration")
        return 2
    try:
        config = ProductionHubConfig(
            descriptor_timeout=descriptor_timeout,
            start_timeout=start_timeout,
            stop_timeout=stop_timeout,
            daemon_operation_timeout=daemon_operation_timeout,
        )
    except ValueError:
        output("HUBD PRODUCTION PROBE FAIL category=invalid_timeout_configuration")
        return 2

    if not execute:
        output("DRY RUN: no Bluetooth, BlueZ, or production session access.")
        output("future execution requires explicit --execute")
        output("session_objects=1; session_opens=1; AAP_channels=1")
        output("cycles=2; subscribers=2_then_1; one_daemon_reader=yes")
        output(
            f"samples_per_client={sample_target}; restart_delay={restart_delay:g}s"
        )
        output(
            f"descriptor_timeout={descriptor_timeout:g}s; "
            f"daemon_operation_timeout={daemon_operation_timeout:g}s"
        )
        output(
            "operator_precondition=normal BlueZ ownership; fresh normal "
            "disconnect/reconnect; AirPods connected; A2DP music playing"
        )
        output("probe_performs_disconnect_or_reconnect=no")
        return 0

    hub: ProductionHub | None = None
    clients: list[ProbeClient] = []
    failure: BaseException | None = None
    cleanup_failure: BaseException | None = None
    selected_path: Path | None = None
    cycle_events: list[list[dict[str, Any]]] = []
    try:
        selected_path = _probe_socket_path(socket_path)
        hub = create_production_hub(
            selected_path,
            config=config,
            output=output,
            builder=session_builder,
        )
        output("PHASE 1: daemon start")
        await hub.daemon.start()
        if hub.daemon.state is not DaemonState.READY:
            raise ProbeFailure("daemon_not_ready")
        session = hub.daemon.session
        if session is None or hub.factory.session is not session:
            raise ProbeFailure("production_session_identity_mismatch")

        output("PHASE 2: two Unix IPC clients")
        clients = [
            await ProbeClient.connect(selected_path, timeout=client_timeout),
            await ProbeClient.connect(selected_path, timeout=client_timeout),
        ]
        for client in clients:
            _require_ok(await client.request("ping"), "ping")
            _require_status(
                await client.request("status"),
                state=DaemonState.READY,
                subscriber_count=0,
            )

        output("PHASE 3: first HR cycle")
        _require_ok(
            await clients[0].request("subscribe", stream="heart_rate"),
            "subscribe",
        )
        _require_ok(
            await clients[1].request("subscribe", stream="heart_rate"),
            "subscribe",
        )
        await cycle_ready(1, session)
        first_a, first_b = await asyncio.gather(
            clients[0].heart_rate_events(sample_target),
            clients[1].heart_rate_events(sample_target),
        )
        cycle_events.extend((first_a, first_b))
        _render_events("client_a cycle=1", first_a, output)
        _render_events("client_b cycle=1", first_b, output)

        output("PHASE 4: subscriber arbitration")
        _require_ok(
            await clients[0].request("unsubscribe", stream="heart_rate"),
            "unsubscribe",
        )
        _require_status(
            await clients[1].request("status"),
            state=DaemonState.STREAMING,
            subscriber_count=1,
        )
        if session.counters.hr_stops != 0:
            raise ProbeFailure("early_production_stop")
        _require_ok(
            await clients[1].request("unsubscribe", stream="heart_rate"),
            "unsubscribe",
        )
        _require_status(
            await clients[0].request("status"),
            state=DaemonState.READY,
            subscriber_count=0,
        )
        if session.counters.hr_stops != 1 or hub.daemon._lock_fd is None:
            raise ProbeFailure("first_cycle_cleanup_mismatch")
        for client in clients:
            _require_ok(await client.request("ping"), "ping")
            client.discard_buffered_events()

        output("PHASE 5: same-session restart")
        await sleep(restart_delay)
        _require_ok(
            await clients[0].request("subscribe", stream="heart_rate"),
            "subscribe",
        )
        if hub.daemon.session is not session or hub.factory.calls != 1:
            raise ProbeFailure("production_session_was_replaced")
        await cycle_ready(2, session)
        second = await clients[0].heart_rate_events(sample_target)
        cycle_events.append(second)
        _render_events("client_a cycle=2", second, output)
        _require_ok(
            await clients[0].request("unsubscribe", stream="heart_rate"),
            "unsubscribe",
        )
        _require_status(
            await clients[0].request("status"),
            state=DaemonState.READY,
            subscriber_count=0,
        )
        if hub.daemon.session is not session:
            raise ProbeFailure("production_session_was_replaced")
    except BaseException as error:
        failure = error
    finally:
        for client in clients:
            try:
                await client.close()
            except BaseException as error:
                if cleanup_failure is None:
                    cleanup_failure = error
        if hub is not None:
            try:
                await asyncio.wait_for(
                    hub.daemon.shutdown(),
                    timeout=2 * config.daemon_operation_timeout,
                )
            except BaseException as error:
                cleanup_failure = cleanup_failure or error

    session = None if hub is None else hub.factory.session
    counters = None if session is None else session.counters
    factory_calls = 0 if hub is None else hub.factory.calls
    output(f"factory_calls={factory_calls}")
    output(f"production_session_objects={1 if session is not None else 0}")
    if counters is not None:
        output("PRODUCTION COUNTERS")
        output(f"  transport_opens={counters.transport_opens}")
        output(f"  descriptor_handshakes={counters.descriptor_handshakes}")
        output(f"  hr_activations={counters.hr_activations}")
        output(f"  hr_stops={counters.hr_stops}")
        output(f"  reports_received={counters.reports_received}")
    event_counts = ",".join(str(len(events)) for events in cycle_events)
    output(f"client_event_counts={event_counts}")
    if hub is not None:
        output(f"daemon_state={hub.daemon.state.value}")
        lock_released = hub.daemon._lock_fd is None
        output(f"process_lock_released={'yes' if lock_released else 'no'}")
    socket_removed = selected_path is not None and not selected_path.exists()
    output(f"socket_removed={'yes' if socket_removed else 'no'}")

    if isinstance(failure, (KeyboardInterrupt, asyncio.CancelledError)):
        raise failure
    if failure is not None:
        output(
            "HUBD PRODUCTION PROBE FAIL "
            f"category={_safe_failure_category(failure)}"
        )
        if cleanup_failure is not None:
            output("HUBD PRODUCTION CLEANUP FAIL category=shutdown_failed")
        return 1
    if cleanup_failure is not None:
        output("HUBD PRODUCTION PROBE FAIL category=shutdown_failed")
        return 1
    assert hub is not None
    assert counters is not None
    minimum_reports = 2 * sample_target
    passed = (
        hub.factory.calls == 1
        and counters.transport_opens == 1
        and counters.descriptor_handshakes == 1
        and counters.hr_activations == 2
        and counters.hr_stops == 2
        and counters.reports_received >= minimum_reports
        and [len(events) for events in cycle_events]
        == [sample_target, sample_target, sample_target]
        and hub.daemon.state is DaemonState.STOPPED
        and hub.daemon._lock_fd is None
        and socket_removed
    )
    output(
        "HUBD PRODUCTION PROBE PASS"
        if passed
        else "HUBD PRODUCTION PROBE FAIL category=counters"
    )
    return 0 if passed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or explicitly execute the private production-session hubd probe."
        )
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform the opt-in Bluetooth hardware lifecycle",
    )
    parser.add_argument("--socket-path", type=Path)
    parser.add_argument(
        "--samples-per-client",
        type=_bounded_int(1, 20),
        default=DEFAULT_SAMPLE_TARGET,
    )
    parser.add_argument(
        "--restart-delay",
        type=_bounded_float(0.0, 30.0),
        default=DEFAULT_RESTART_DELAY,
    )
    parser.add_argument(
        "--client-timeout",
        type=_bounded_float(1.0, 60.0),
        default=DEFAULT_CLIENT_TIMEOUT,
    )
    parser.add_argument(
        "--descriptor-timeout",
        type=_bounded_float(1.0, 60.0),
        default=DEFAULT_DESCRIPTOR_TIMEOUT,
    )
    parser.add_argument(
        "--start-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_START_TIMEOUT,
    )
    parser.add_argument(
        "--stop-timeout",
        type=_bounded_float(1.0, 30.0),
        default=DEFAULT_STOP_TIMEOUT,
    )
    parser.add_argument(
        "--daemon-operation-timeout",
        type=_bounded_float(1.0, 300.0),
        default=DEFAULT_DAEMON_OPERATION_TIMEOUT,
    )
    return parser


def main(
    argv: Sequence[str] | None = None, *, stream: TextIO | None = None
) -> int:
    args = build_parser().parse_args(argv)

    def emit(message: str) -> None:
        print(message, file=stream)

    try:
        return asyncio.run(
            run_probe(
                execute=args.execute,
                socket_path=args.socket_path,
                sample_target=args.samples_per_client,
                restart_delay=args.restart_delay,
                client_timeout=args.client_timeout,
                descriptor_timeout=args.descriptor_timeout,
                start_timeout=args.start_timeout,
                stop_timeout=args.stop_timeout,
                daemon_operation_timeout=args.daemon_operation_timeout,
                output=emit,
            )
        )
    except KeyboardInterrupt:
        emit("HUBD PRODUCTION PROBE INTERRUPTED")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
