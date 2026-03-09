"""Bounded protocol-v1 Unix client for airpods-hubd.

The API is experimental. This module owns no Bluetooth resources and never
starts or reconnects the daemon.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
from typing import Any, Self


PROTOCOL_VERSION = 1
MAX_FRAME_SIZE = 4096
DEFAULT_SOCKET_NAME = "airpods-hubd.sock"
EVENT_BUFFER_SIZE = 32


class AirPodsClientError(Exception):
    """Base class for errors at the local daemon-client boundary."""


class XdgRuntimeDirMissing(AirPodsClientError):
    """The default daemon socket cannot be resolved."""


class ConnectionFailed(AirPodsClientError):
    """The Unix connection could not be opened."""

    def __init__(self, path: Path, cause: OSError) -> None:
        super().__init__(f"could not connect to {path}: {cause}")
        self.path = path
        self.cause = cause


class ConnectionClosed(AirPodsClientError):
    """The connection is closed and cannot be reused."""


class FrameTooLarge(AirPodsClientError):
    """An inbound JSON payload exceeded the daemon's frame limit."""

    def __init__(self, limit: int = MAX_FRAME_SIZE) -> None:
        super().__init__(f"daemon frame exceeds the {limit}-byte limit")
        self.limit = limit


class InvalidMessage(AirPodsClientError):
    """A daemon frame is not valid protocol-v1 JSON."""


class ProtocolVersionError(AirPodsClientError):
    """A daemon frame has a missing, invalid, or unsupported version."""

    def __init__(self, received: object) -> None:
        super().__init__(
            f"unsupported daemon protocol version {received!r}; "
            f"expected {PROTOCOL_VERSION}"
        )
        self.expected = PROTOCOL_VERSION
        self.received = received


class DaemonError(AirPodsClientError):
    """A structured error response returned by airpods-hubd."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"daemon error {code}: {message}")
        self.code = code
        self.message = message


class SubscriptionActive(AirPodsClientError):
    """This connection already owns a heart-rate subscription generation."""


class EventBufferFull(AirPodsClientError):
    """A subscriber did not consume its bounded event buffer in time."""


class DaemonState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    STARTING_HEART_RATE = "starting_hr"
    STREAMING = "streaming"
    STOPPING_HEART_RATE = "stopping_hr"
    FAILED = "failed"
    SHUTTING_DOWN = "shutting_down"


class SourceSide(str, Enum):
    LEFT = "left"
    RIGHT = "right"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Hello:
    service: str
    experimental: bool


@dataclass(frozen=True, slots=True)
class Status:
    state: DaemonState
    subscriber_count: int


@dataclass(frozen=True, slots=True)
class HeartRateSample:
    bpm: int
    source_side: SourceSide
    source_side_raw: int | None = None


class _SubscriptionPhase(Enum):
    IDLE = "idle"
    SUBSCRIBING = "subscribing"
    ACTIVE = "active"
    CLEANING = "cleaning"


@dataclass(slots=True)
class _EventRoute:
    generation: int
    queue: asyncio.Queue[HeartRateSample | BaseException]


class AirPodsClient:
    """One experimental asyncio connection to a running airpods-hubd."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._request_lock = asyncio.Lock()
        self._pending: asyncio.Future[dict[str, Any]] | None = None
        self._subscription_lock = asyncio.Lock()
        self._subscription_changed = asyncio.Event()
        self._subscription_changed.set()
        self._subscription_phase = _SubscriptionPhase.IDLE
        self._subscription_generation: int | None = None
        self._next_generation = 1
        self._event_route: _EventRoute | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._failure: AirPodsClientError | None = None
        self._reader_task = asyncio.create_task(self._read_loop())

    @staticmethod
    def default_socket_path() -> Path:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            raise XdgRuntimeDirMissing(
                "XDG_RUNTIME_DIR is required to locate airpods-hubd"
            )
        return Path(runtime) / DEFAULT_SOCKET_NAME

    @classmethod
    async def connect(cls) -> Self:
        """Connect to `$XDG_RUNTIME_DIR/airpods-hubd.sock`."""

        return await cls.connect_to(cls.default_socket_path())

    @classmethod
    async def connect_to(cls, socket_path: str | os.PathLike[str]) -> Self:
        """Connect to an explicit Unix socket without starting the daemon."""

        path = Path(socket_path)
        try:
            reader, writer = await asyncio.open_unix_connection(
                path,
                limit=MAX_FRAME_SIZE + 1,
            )
        except OSError as error:
            raise ConnectionFailed(path, error) from error
        return cls(reader, writer)

    async def __aenter__(self) -> Self:
        self._ensure_open()
        return self

    async def __aexit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object,
    ) -> None:
        await self.close()

    async def hello(self) -> Hello:
        response = await self._request("hello")
        message = _success(response, "hello")
        return Hello(
            service=_string(message, "service"),
            experimental=_boolean(message, "experimental"),
        )

    async def ping(self) -> None:
        response = await self._request("ping")
        message = _success(response, "ping")
        if _boolean(message, "pong") is not True:
            raise InvalidMessage("ping response must contain pong=true")

    async def status(self) -> Status:
        response = await self._request("status")
        message = _success(response, "status")
        state_value = _string(message, "state")
        try:
            state = DaemonState(state_value)
        except ValueError:
            raise InvalidMessage(f"unknown daemon state: {state_value}") from None
        return Status(
            state=state,
            subscriber_count=_non_negative_integer(message, "subscriber_count"),
        )

    async def subscribe_heart_rate(self) -> HeartRateSubscription:
        """Subscribe, retaining cancellation cleanup until this generation ends."""

        while True:
            await self._subscription_lock.acquire()
            if self._subscription_phase is not _SubscriptionPhase.CLEANING:
                break
            changed = self._subscription_changed
            self._subscription_lock.release()
            await changed.wait()

        generation: int | None = None
        try:
            self._ensure_open()
            if self._subscription_phase is not _SubscriptionPhase.IDLE:
                raise SubscriptionActive(
                    "this client already has a heart-rate subscription"
                )
            generation = self._next_generation
            self._next_generation += 1
            self._subscription_phase = _SubscriptionPhase.SUBSCRIBING
            self._subscription_generation = generation
            self._subscription_changed.clear()
            route = _EventRoute(
                generation,
                asyncio.Queue(maxsize=EVENT_BUFFER_SIZE),
            )
            self._event_route = route

            try:
                response = await self._request(
                    "subscribe",
                    stream="heart_rate",
                )
                _validate_subscription_response(response, "subscribe", True)
            except BaseException:
                self._subscription_phase = _SubscriptionPhase.CLEANING
                self._schedule_cleanup(generation)
                raise

            self._subscription_phase = _SubscriptionPhase.ACTIVE
            return HeartRateSubscription(self, generation, route.queue)
        finally:
            self._subscription_lock.release()

    async def close(self) -> None:
        """Close this connection. Repeated calls are safe."""

        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_connection())
        await asyncio.shield(self._close_task)

    async def _close_connection(self) -> None:
        self._fail(ConnectionClosed("airpods-hubd connection is closed"))
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        if self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        tasks = tuple(self._background_tasks)
        for task in tasks:
            if task is not asyncio.current_task():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _request(self, operation: str, **fields: object) -> dict[str, Any]:
        task = self._spawn(self._request_transaction(operation, fields))
        return await asyncio.shield(task)

    async def _request_transaction(
        self,
        operation: str,
        fields: dict[str, object],
    ) -> dict[str, Any]:
        async with self._request_lock:
            self._ensure_open()
            message = {
                "protocol_version": PROTOCOL_VERSION,
                "operation": operation,
                **fields,
            }
            frame = json.dumps(
                message,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if len(frame) > MAX_FRAME_SIZE:
                raise FrameTooLarge()
            frame += b"\n"

            loop = asyncio.get_running_loop()
            response: asyncio.Future[dict[str, Any]] = loop.create_future()
            self._pending = response
            try:
                self._writer.write(frame)
                await self._writer.drain()
                return await response
            except (ConnectionError, OSError) as error:
                failure = ConnectionClosed(f"daemon I/O failed: {error}")
                self._fail(failure)
                raise failure from error
            finally:
                if self._pending is response:
                    self._pending = None

    async def _read_loop(self) -> None:
        try:
            while True:
                message = await self._read_message()
                if "event" in message:
                    self._route_event(_heart_rate_sample(message))
                    continue
                pending = self._pending
                if pending is None or pending.done():
                    if message.get("ok") is False:
                        self._fail(_daemon_error(message))
                        return
                    raise InvalidMessage(
                        "response arrived without a pending request"
                    )
                pending.set_result(message)
        except asyncio.CancelledError:
            raise
        except AirPodsClientError as error:
            self._fail(error)
        except (ConnectionError, OSError) as error:
            self._fail(ConnectionClosed(f"daemon I/O failed: {error}"))

    async def _read_message(self) -> dict[str, Any]:
        try:
            frame = await self._reader.readline()
        except ValueError:
            raise FrameTooLarge() from None
        if not frame:
            raise ConnectionClosed("airpods-hubd closed the connection")
        if not frame.endswith(b"\n"):
            if len(frame) > MAX_FRAME_SIZE:
                raise FrameTooLarge()
            raise InvalidMessage("daemon frame is not newline terminated")
        if len(frame) - 1 > MAX_FRAME_SIZE:
            raise FrameTooLarge()
        try:
            value = json.loads(frame[:-1])
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidMessage(f"invalid daemon JSON: {error}") from None
        if not isinstance(value, dict):
            raise InvalidMessage("daemon message must be a JSON object")
        version = value.get("protocol_version")
        if type(version) is not int or version != PROTOCOL_VERSION:
            raise ProtocolVersionError(version)
        has_event = "event" in value
        has_response = "ok" in value
        if has_event == has_response:
            raise InvalidMessage(
                "message must be exactly one response or heart-rate event"
            )
        if has_event:
            if value.get("event") != "heart_rate":
                raise InvalidMessage("unsupported daemon event")
        elif type(value.get("ok")) is not bool:
            raise InvalidMessage("response ok field must be boolean")
        return value

    def _route_event(self, sample: HeartRateSample) -> None:
        route = self._event_route
        if route is None:
            raise InvalidMessage("heart-rate event arrived without a subscription")
        try:
            route.queue.put_nowait(sample)
        except asyncio.QueueFull:
            self._fail(EventBufferFull("heart-rate event buffer is full"))

    def _schedule_cleanup(self, generation: int) -> asyncio.Task[None]:
        return self._spawn(self._cleanup_subscription(generation))

    async def _cleanup_subscription(self, generation: int) -> None:
        async with self._subscription_lock:
            if self._subscription_generation != generation:
                return
            self._subscription_phase = _SubscriptionPhase.CLEANING
            self._subscription_changed.clear()
            if not self._closed:
                try:
                    response = await self._request(
                        "unsubscribe",
                        stream="heart_rate",
                    )
                    _validate_subscription_response(
                        response,
                        "unsubscribe",
                        False,
                    )
                except asyncio.CancelledError:
                    self._fail(
                        ConnectionClosed(
                            "subscription cleanup was cancelled"
                        )
                    )
                    raise
                except AirPodsClientError as error:
                    self._fail(error)
                    raise
            if (
                self._event_route is not None
                and self._event_route.generation == generation
            ):
                if self._event_route.queue.full():
                    self._event_route.queue.get_nowait()
                self._event_route.queue.put_nowait(StopAsyncIteration())
                self._event_route = None
            if self._subscription_generation == generation:
                self._subscription_generation = None
                self._subscription_phase = _SubscriptionPhase.IDLE
                self._subscription_changed.set()

    def _spawn(self, awaitable: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)
        return task

    def _background_task_done(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _fail(self, error: AirPodsClientError) -> None:
        if self._closed:
            return
        self._closed = True
        self._failure = error
        self._writer.close()
        pending = self._pending
        if pending is not None and not pending.done():
            pending.set_exception(error)
        route = self._event_route
        if route is not None:
            if route.queue.full():
                route.queue.get_nowait()
            route.queue.put_nowait(error)
        self._event_route = None
        self._subscription_generation = None
        self._subscription_phase = _SubscriptionPhase.IDLE
        self._subscription_changed.set()

    def _ensure_open(self) -> None:
        if self._closed:
            raise self._failure or ConnectionClosed(
                "airpods-hubd connection is closed"
            )


class HeartRateSubscription(AsyncIterator[HeartRateSample]):
    """One generation-scoped heart-rate event subscription."""

    def __init__(
        self,
        client: AirPodsClient,
        generation: int,
        queue: asyncio.Queue[HeartRateSample | BaseException],
    ) -> None:
        self._client = client
        self._generation = generation
        self._queue = queue
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: object,
    ) -> None:
        await self.close()

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> HeartRateSample:
        if self._closed:
            raise StopAsyncIteration
        value = await self._queue.get()
        if isinstance(value, BaseException):
            self._closed = True
            raise value
        return value

    async def next(self) -> HeartRateSample | None:
        """Return the next sample, or `None` after explicit close."""

        try:
            return await self.__anext__()
        except StopAsyncIteration:
            return None

    async def unsubscribe(self) -> None:
        await self.close()

    async def close(self) -> None:
        """Idempotently unsubscribe; cancellation leaves cleanup running."""

        if self._cleanup_task is None:
            self._closed = True
            self._cleanup_task = self._client._schedule_cleanup(
                self._generation
            )
        await asyncio.shield(self._cleanup_task)


def _success(
    response: dict[str, Any],
    expected_operation: str,
) -> dict[str, Any]:
    ok = response.get("ok")
    if ok is False:
        raise _daemon_error(response)
    if ok is not True:
        raise InvalidMessage("response ok field must be boolean")
    if _string(response, "operation") != expected_operation:
        raise InvalidMessage("response operation does not match request")
    return response


def _daemon_error(response: dict[str, Any]) -> DaemonError:
    error = response.get("error")
    if not isinstance(error, dict):
        raise InvalidMessage("daemon error response has no error object")
    return DaemonError(
        _string(error, "code"),
        _string(error, "message"),
    )


def _validate_subscription_response(
    response: dict[str, Any],
    operation: str,
    subscribed: bool,
) -> None:
    message = _success(response, operation)
    if _string(message, "stream") != "heart_rate":
        raise InvalidMessage("subscription response stream is invalid")
    if _boolean(message, "subscribed") is not subscribed:
        raise InvalidMessage("subscription response state is invalid")
    idempotence_field = (
        "already_subscribed" if subscribed else "already_unsubscribed"
    )
    _boolean(message, idempotence_field)


def _heart_rate_sample(message: dict[str, Any]) -> HeartRateSample:
    bpm = _byte(message, "bpm")
    try:
        source_side = SourceSide(_string(message, "source_side"))
    except ValueError:
        raise InvalidMessage("source_side is invalid") from None
    raw: int | None = None
    if source_side is SourceSide.UNKNOWN:
        raw = _byte(message, "source_side_raw")
    elif "source_side_raw" in message:
        raise InvalidMessage("known source_side must not include source_side_raw")
    return HeartRateSample(bpm, source_side, raw)


def _string(message: dict[str, Any], field: str) -> str:
    value = message.get(field)
    if not isinstance(value, str):
        raise InvalidMessage(f"{field} must be a string")
    return value


def _boolean(message: dict[str, Any], field: str) -> bool:
    value = message.get(field)
    if type(value) is not bool:
        raise InvalidMessage(f"{field} must be a boolean")
    return value


def _non_negative_integer(message: dict[str, Any], field: str) -> int:
    value = message.get(field)
    if type(value) is not int or value < 0:
        raise InvalidMessage(f"{field} must be a non-negative integer")
    return value


def _byte(message: dict[str, Any], field: str) -> int:
    value = _non_negative_integer(message, field)
    if value > 255:
        raise InvalidMessage(f"{field} must fit in one byte")
    return value
