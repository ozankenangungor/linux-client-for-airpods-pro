"""Hardware-independent Unix-socket control plane for one sensor session."""

from __future__ import annotations

import asyncio
import errno
import os
import socket
import stat
import struct
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from airpods_hr._hubd.protocol import (
    MAX_FRAME_SIZE,
    OUTBOUND_QUEUE_SIZE,
    PROTOCOL_VERSION,
    RequestError,
    decode_request,
    encode_message,
    error_response,
    heart_rate_event,
    response,
)
from airpods_hr._hubd.session import SensorSession, SessionFactory


DEFAULT_SOCKET_NAME = "airpods-hubd.sock"
DEFAULT_OPERATION_TIMEOUT = 10.0
_UNIX_PATH_MAX_BYTES = 107


class DaemonState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    STARTING_HR = "starting_hr"
    STREAMING = "streaming"
    STOPPING_HR = "stopping_hr"
    FAILED = "failed"
    SHUTTING_DOWN = "shutting_down"


class HubDaemonError(RuntimeError):
    """Base class for private daemon failures."""


class UnsafeSocketPathError(HubDaemonError):
    """The requested local socket path cannot be managed safely."""


class SessionOperationError(HubDaemonError):
    """The injected session failed during a daemon lifecycle operation."""


class DaemonAlreadyRunningError(HubDaemonError):
    """An active daemon already owns the requested Unix socket."""


PeerUidProvider = Callable[[Any], int]


def socket_path_from_environment() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise UnsafeSocketPathError("XDG_RUNTIME_DIR is required")
    return Path(runtime) / DEFAULT_SOCKET_NAME


def linux_peer_uid(peer_socket: Any) -> int:
    if not hasattr(socket, "SO_PEERCRED"):
        raise HubDaemonError("SO_PEERCRED is unavailable")
    credentials = peer_socket.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    _pid, uid, _gid = struct.unpack("3i", credentials)
    return uid


def _validate_socket_path(path: Path) -> None:
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise UnsafeSocketPathError("socket path must be an absolute file path")
    if len(os.fsencode(path)) > _UNIX_PATH_MAX_BYTES:
        raise UnsafeSocketPathError("socket path is too long")
    parent_path = path.parent
    try:
        if parent_path.resolve(strict=True) != parent_path:
            raise UnsafeSocketPathError("socket parent path must be canonical")
        parent = parent_path.stat()
    except OSError as error:
        raise UnsafeSocketPathError("socket parent directory is unavailable") from error
    if not stat.S_ISDIR(parent.st_mode):
        raise UnsafeSocketPathError("socket parent must be a directory")
    if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise UnsafeSocketPathError(
            "socket parent must be owned by the daemon user with mode 0700"
        )


def _probe_existing_socket(path: Path) -> None:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.25)
    try:
        probe.connect(str(path))
    except OSError as error:
        if error.errno == errno.ECONNREFUSED:
            return
        raise UnsafeSocketPathError(
            "existing socket state could not be proven stale"
        ) from error
    finally:
        probe.close()
    raise DaemonAlreadyRunningError("an active daemon already owns the socket")


async def _remove_safe_stale_socket(path: Path) -> None:
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise UnsafeSocketPathError("cannot inspect existing socket path") from error
    if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
        raise UnsafeSocketPathError("existing socket path is not an owned socket")
    identity = (existing.st_dev, existing.st_ino)
    await asyncio.to_thread(_probe_existing_socket, path)
    try:
        current = path.lstat()
    except OSError as error:
        raise UnsafeSocketPathError(
            "existing socket path changed during stale check"
        ) from error
    if (
        not stat.S_ISSOCK(current.st_mode)
        or current.st_uid != os.geteuid()
        or (current.st_dev, current.st_ino) != identity
    ):
        raise UnsafeSocketPathError(
            "existing socket path changed during stale check"
        )
    path.unlink()


@dataclass(eq=False, slots=True)
class _Client:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    outbound: asyncio.Queue[bytes] = field(
        default_factory=lambda: asyncio.Queue(maxsize=OUTBOUND_QUEUE_SIZE)
    )
    subscribed: bool = False
    closing: bool = False
    writer_task: asyncio.Task[None] | None = None


class AirPodsHubDaemon:
    """Own one injected session and fan its reports out over local JSONL IPC."""

    def __init__(
        self,
        session_factory: SessionFactory,
        socket_path: Path | str | None = None,
        *,
        peer_uid_provider: PeerUidProvider = linux_peer_uid,
        operation_timeout: float = DEFAULT_OPERATION_TIMEOUT,
    ) -> None:
        if operation_timeout <= 0:
            raise ValueError("operation_timeout must be positive")
        self.socket_path = (
            Path(socket_path)
            if socket_path is not None
            else socket_path_from_environment()
        )
        self._session_factory = session_factory
        self._peer_uid_provider = peer_uid_provider
        self._operation_timeout = operation_timeout
        self._lifecycle_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._session: SensorSession | None = None
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[_Client] = set()
        self._reader_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._owned_socket_identity: tuple[int, int] | None = None
        self._hr_may_be_active = False
        self._start_attempted = False
        self.state = DaemonState.STOPPED

    @property
    def session(self) -> SensorSession | None:
        return self._session

    @property
    def subscriber_count(self) -> int:
        return sum(client.subscribed for client in self._clients)

    @property
    def report_reader_active(self) -> bool:
        return self._reader_task is not None and not self._reader_task.done()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.state is not DaemonState.STOPPED:
                raise HubDaemonError(f"cannot start daemon from {self.state.value}")
            if self._start_attempted:
                raise HubDaemonError("daemon objects are single-use")
            self._start_attempted = True
            self.state = DaemonState.STARTING
            owned_listener: socket.socket | None = None
            try:
                _validate_socket_path(self.socket_path)
                await _remove_safe_stale_socket(self.socket_path)
                owned_listener = self._acquire_listener()
                self._session = self._session_factory()
                await self._bounded(self._session.open())
                self._server = await asyncio.start_unix_server(
                    self._handle_client,
                    sock=owned_listener,
                    limit=MAX_FRAME_SIZE + 1,
                    cleanup_socket=False,
                )
                owned_listener = None
            except BaseException as error:
                self.state = DaemonState.FAILED
                if owned_listener is not None:
                    owned_listener.close()
                self._remove_owned_socket()
                if isinstance(error, asyncio.CancelledError):
                    raise
                if isinstance(
                    error, (DaemonAlreadyRunningError, UnsafeSocketPathError)
                ):
                    raise
                raise SessionOperationError("daemon startup failed") from error
            self.state = DaemonState.READY

    def _acquire_listener(self) -> socket.socket:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
        except OSError as error:
            listener.close()
            if error.errno == errno.EADDRINUSE:
                raise DaemonAlreadyRunningError(
                    "another daemon acquired the socket during startup"
                ) from error
            raise UnsafeSocketPathError("could not bind daemon socket") from error

        try:
            socket_stat = self.socket_path.lstat()
            if (
                not stat.S_ISSOCK(socket_stat.st_mode)
                or socket_stat.st_uid != os.geteuid()
            ):
                raise UnsafeSocketPathError("bound path is not an owned Unix socket")
            self._owned_socket_identity = (
                socket_stat.st_dev,
                socket_stat.st_ino,
            )
            os.chmod(self.socket_path, 0o600)
            current = self.socket_path.lstat()
            if (
                not stat.S_ISSOCK(current.st_mode)
                or current.st_uid != os.geteuid()
                or (current.st_dev, current.st_ino)
                != self._owned_socket_identity
                or stat.S_IMODE(current.st_mode) != 0o600
            ):
                raise UnsafeSocketPathError(
                    "daemon socket path changed during listener setup"
                )
            listener.listen(socket.SOMAXCONN)
            listener.setblocking(False)
            return listener
        except BaseException:
            listener.close()
            self._remove_owned_socket()
            raise

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self.state is DaemonState.STOPPED:
                return
            async with self._lifecycle_lock:
                self.state = DaemonState.SHUTTING_DOWN
                server = self._server
                self._server = None
                if server is not None:
                    server.close()
                if self._hr_may_be_active and self._session is not None:
                    try:
                        await self._bounded(self._session.stop())
                        self._hr_may_be_active = False
                    except BaseException:
                        pass
                reader_task = self._reader_task
                self._reader_task = None
                if (
                    reader_task is not None
                    and reader_task is not asyncio.current_task()
                ):
                    reader_task.cancel()
                    await asyncio.gather(reader_task, return_exceptions=True)
                clients = tuple(self._clients)
                for client in clients:
                    client.subscribed = False
                    client.closing = True
                self._clients.clear()

            for client in clients:
                await self._close_client_transport(client)
            handler_tasks = tuple(self._handler_tasks)
            for task in handler_tasks:
                task.cancel()
            if handler_tasks:
                await asyncio.gather(*handler_tasks, return_exceptions=True)
                self._handler_tasks.difference_update(handler_tasks)
            if server is not None:
                await server.wait_closed()
            tasks = tuple(self._background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
                self._background_tasks.difference_update(tasks)
            if self._session is not None:
                try:
                    await self._bounded(self._session.close())
                except BaseException:
                    pass
            self._remove_owned_socket()
            self.state = DaemonState.STOPPED

    async def _bounded(self, operation: Any) -> Any:
        return await asyncio.wait_for(operation, timeout=self._operation_timeout)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        handler_task = asyncio.current_task()
        if handler_task is not None:
            self._handler_tasks.add(handler_task)
            handler_task.add_done_callback(self._handler_tasks.discard)
        peer_socket = writer.get_extra_info("socket")
        try:
            peer_uid = self._peer_uid_provider(peer_socket)
        except BaseException:
            writer.close()
            await writer.wait_closed()
            return
        if peer_uid != os.geteuid():
            writer.close()
            await writer.wait_closed()
            return

        client = _Client(reader, writer)
        self._clients.add(client)
        client.writer_task = asyncio.create_task(self._write_client(client))
        try:
            while not client.closing:
                try:
                    frame = await reader.readline()
                except ValueError:
                    self._enqueue(
                        client,
                        error_response(
                            RequestError(
                                "frame_too_large", "request frame exceeds limit"
                            )
                        ),
                    )
                    break
                if not frame:
                    break
                if not frame.endswith(b"\n") or len(frame) - 1 > MAX_FRAME_SIZE:
                    self._enqueue(
                        client,
                        error_response(
                            RequestError(
                                "frame_too_large", "request frame exceeds limit"
                            )
                        ),
                    )
                    break
                try:
                    request = decode_request(frame[:-1])
                    reply = await self._dispatch(client, request)
                except RequestError as error:
                    reply = error_response(error)
                if not self._enqueue(client, reply):
                    break
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            await self._disconnect_client(client)

    async def _write_client(self, client: _Client) -> None:
        try:
            while True:
                message = await client.outbound.get()
                client.writer.write(message)
                await client.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if not client.closing:
                self._spawn_background(self._disconnect_client(client))

    async def _dispatch(
        self, client: _Client, request: dict[str, Any]
    ) -> dict[str, Any]:
        operation = request["operation"]
        if operation == "hello":
            return response(
                operation,
                service="airpods-hubd",
                experimental=True,
            )
        if operation == "ping":
            return response(operation, pong=True)
        if operation == "status":
            return response(
                operation,
                state=self.state.value,
                subscriber_count=self.subscriber_count,
            )
        if operation == "subscribe":
            already_subscribed = await self._subscribe(client)
            return response(
                operation,
                stream="heart_rate",
                subscribed=True,
                already_subscribed=already_subscribed,
            )
        already_unsubscribed = await self._unsubscribe(client)
        return response(
            operation,
            stream="heart_rate",
            subscribed=False,
            already_unsubscribed=already_unsubscribed,
        )

    async def _subscribe(self, client: _Client) -> bool:
        async with self._lifecycle_lock:
            if client.subscribed:
                return True
            if client.closing:
                raise RequestError("connection_closing", "connection is closing")
            if self.state is DaemonState.FAILED:
                raise RequestError("service_failed", "sensor service is unavailable")
            if (
                self.state is not DaemonState.READY
                and self.state is not DaemonState.STREAMING
            ):
                raise RequestError(
                    "service_unavailable", "sensor service is unavailable"
                )
            if self.subscriber_count == 0:
                if self._session is None:
                    raise RequestError(
                        "service_unavailable", "sensor service is unavailable"
                    )
                self.state = DaemonState.STARTING_HR
                self._hr_may_be_active = True
                try:
                    await self._bounded(self._session.start())
                except BaseException as error:
                    self.state = DaemonState.FAILED
                    await self._notify_service_failure_locked(exclude=client)
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    raise RequestError(
                        "session_start_failed", "sensor service is unavailable"
                    ) from error
                client.subscribed = True
                self.state = DaemonState.STREAMING
                self._reader_task = asyncio.create_task(self._read_reports())
                return False
            client.subscribed = True
            return False

    async def _unsubscribe(self, client: _Client) -> bool:
        reader_task: asyncio.Task[None] | None = None
        stop_error: RequestError | None = None
        stop_cause: BaseException | None = None
        async with self._lifecycle_lock:
            if not client.subscribed:
                return True
            client.subscribed = False
            if self.subscriber_count != 0:
                return False
            if self.state is DaemonState.STREAMING and self._session is not None:
                self.state = DaemonState.STOPPING_HR
                try:
                    await self._bounded(self._session.stop())
                    self._hr_may_be_active = False
                    self.state = DaemonState.READY
                except BaseException as error:
                    self.state = DaemonState.FAILED
                    await self._notify_service_failure_locked(exclude=client)
                    if isinstance(error, asyncio.CancelledError):
                        raise
                    stop_error = RequestError(
                        "session_stop_failed", "sensor service is unavailable"
                    )
                    stop_cause = error
                finally:
                    reader_task = self._reader_task
                    self._reader_task = None
        if reader_task is not None and reader_task is not asyncio.current_task():
            reader_task.cancel()
            await asyncio.gather(reader_task, return_exceptions=True)
        if stop_error is not None:
            raise stop_error from stop_cause
        return False

    async def _read_reports(self) -> None:
        try:
            while True:
                session = self._session
                if session is None:
                    return
                report = await session.receive_report()
                event = heart_rate_event(report)
                for client in tuple(self._clients):
                    if client.subscribed and not self._enqueue(client, event):
                        self._spawn_background(self._disconnect_client(client))
        except asyncio.CancelledError:
            raise
        except BaseException:
            await self._receive_failed()

    async def _receive_failed(self) -> None:
        async with self._lifecycle_lock:
            if self.state is not DaemonState.STREAMING:
                return
            self.state = DaemonState.FAILED
            for client in self._clients:
                client.subscribed = False
            await self._notify_service_failure_locked()

    async def _notify_service_failure_locked(
        self, *, exclude: _Client | None = None
    ) -> None:
        message = error_response("service_failed")
        for client in tuple(self._clients):
            if client is not exclude:
                self._enqueue(client, message)

    def _enqueue(self, client: _Client, message: dict[str, Any]) -> bool:
        if client.closing:
            return False
        try:
            client.outbound.put_nowait(encode_message(message))
        except asyncio.QueueFull:
            return False
        return True

    def _spawn_background(self, operation: Any) -> None:
        task = asyncio.create_task(operation)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _disconnect_client(self, client: _Client) -> None:
        if client.closing:
            return
        client.closing = True
        try:
            await self._unsubscribe(client)
        except RequestError:
            pass
        self._clients.discard(client)
        await self._close_client_transport(client)

    async def _close_client_transport(self, client: _Client) -> None:
        writer_task = client.writer_task
        if writer_task is not None and writer_task is not asyncio.current_task():
            writer_task.cancel()
            await asyncio.gather(writer_task, return_exceptions=True)
        client.writer.close()
        try:
            await client.writer.wait_closed()
        except (ConnectionError, asyncio.CancelledError):
            pass

    def _remove_owned_socket(self) -> None:
        identity = self._owned_socket_identity
        self._owned_socket_identity = None
        if identity is None:
            return
        try:
            current = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if (
            stat.S_ISSOCK(current.st_mode)
            and current.st_uid == os.geteuid()
            and (current.st_dev, current.st_ino) == identity
        ):
            self.socket_path.unlink()


__all__ = [
    "AirPodsHubDaemon",
    "DaemonAlreadyRunningError",
    "DaemonState",
    "HubDaemonError",
    "SessionOperationError",
    "UnsafeSocketPathError",
    "linux_peer_uid",
    "socket_path_from_environment",
]
