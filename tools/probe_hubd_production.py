#!/usr/bin/env python3.14
"""Private opt-in end-to-end probe for hubd and the production session."""

from __future__ import annotations


import asyncio
import json

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


from airpods_hr._hubd.protocol import MAX_FRAME_SIZE, PROTOCOL_VERSION


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


