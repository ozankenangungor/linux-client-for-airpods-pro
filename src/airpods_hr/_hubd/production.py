"""Private composition boundary between hubd and the production session."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from airpods_hr._hubd.server import AirPodsHubDaemon
from airpods_hr.production_session import (
    DEFAULT_DBUS_TIMEOUT,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_L2CAP_CONNECT_TIMEOUT,
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    InternalProductionSession,
    ProductionSessionError,
    ProductionSessionState,
    create_production_session,
)


DEFAULT_DESCRIPTOR_TIMEOUT = 30.0
DEFAULT_DAEMON_OPERATION_TIMEOUT = 150.0
_MAX_PROFILE_REGISTRATIONS = 4
_OPEN_DBUS_WINDOWS = 2 + _MAX_PROFILE_REGISTRATIONS + 3
_OUTER_TIMEOUT_MARGIN = 10.0

ProductionSessionBuilder = Callable[..., InternalProductionSession]


def minimum_daemon_operation_timeout(
    *,
    descriptor_timeout: float,
    dbus_timeout: float,
    connect_timeout: float,
    handshake_timeout: float,
    start_timeout: float,
    stop_timeout: float,
) -> float:
    """Return a conservative floor for the outer production open timeout."""

    open_window = (
        _OPEN_DBUS_WINDOWS * dbus_timeout
        + connect_timeout
        + handshake_timeout
        + descriptor_timeout
    )
    cleanup_window = stop_timeout + 5 * dbus_timeout
    return max(
        open_window + cleanup_window,
        start_timeout + cleanup_window,
        stop_timeout + cleanup_window,
        cleanup_window,
    ) + _OUTER_TIMEOUT_MARGIN


@dataclass(frozen=True, slots=True)
class ProductionHubConfig:
    """Private, bounded production composition settings."""

    descriptor_timeout: float = DEFAULT_DESCRIPTOR_TIMEOUT
    dbus_timeout: float = DEFAULT_DBUS_TIMEOUT
    connect_timeout: float = DEFAULT_L2CAP_CONNECT_TIMEOUT
    handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT
    start_timeout: float = DEFAULT_START_TIMEOUT
    stop_timeout: float = DEFAULT_STOP_TIMEOUT
    daemon_operation_timeout: float = DEFAULT_DAEMON_OPERATION_TIMEOUT

    def __post_init__(self) -> None:
        values = (
            self.descriptor_timeout,
            self.dbus_timeout,
            self.connect_timeout,
            self.handshake_timeout,
            self.start_timeout,
            self.stop_timeout,
            self.daemon_operation_timeout,
        )
        if min(values) <= 0:
            raise ValueError("production hub timeouts must be positive")
        minimum = minimum_daemon_operation_timeout(
            descriptor_timeout=self.descriptor_timeout,
            dbus_timeout=self.dbus_timeout,
            connect_timeout=self.connect_timeout,
            handshake_timeout=self.handshake_timeout,
            start_timeout=self.start_timeout,
            stop_timeout=self.stop_timeout,
        )
        if self.daemon_operation_timeout < minimum:
            raise ValueError(
                "daemon operation timeout is below the production open window "
                f"({minimum:g}s minimum)"
            )


class ProductionSessionFactory:
    """Construct a fresh authoritative production session for each attempt."""

    def __init__(
        self,
        config: ProductionHubConfig,
        *,
        output: Callable[[str], None] = print,
        builder: ProductionSessionBuilder | None = None,
    ) -> None:
        self.config = config
        self._output = output
        self._builder = builder
        self.calls = 0
        self.session: InternalProductionSession | None = None

    def __call__(self) -> InternalProductionSession:
        self.calls += 1
        builder = self._builder or create_production_session
        session = builder(
            descriptor_timeout=self.config.descriptor_timeout,
            dbus_timeout=self.config.dbus_timeout,
            connect_timeout=self.config.connect_timeout,
            handshake_timeout=self.config.handshake_timeout,
            start_timeout=self.config.start_timeout,
            stop_timeout=self.config.stop_timeout,
            output=self._output,
        )
        self.session = session
        return session


def _is_recoverable_production_error(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    return isinstance(error, ProductionSessionError) and error.recoverable


def _production_cleanup_completed(
    session: object, _error: BaseException
) -> bool:
    return (
        getattr(session, "state", None) is ProductionSessionState.CLOSED
        and getattr(session, "cleanup_complete", False) is True
    )


@dataclass(frozen=True, slots=True)
class ProductionHub:
    """The private daemon and its observable production session factory."""

    daemon: AirPodsHubDaemon
    factory: ProductionSessionFactory


def create_production_hub(
    socket_path: Path | str,
    *,
    config: ProductionHubConfig | None = None,
    output: Callable[[str], None] = print,
    builder: ProductionSessionBuilder | None = None,
) -> ProductionHub:
    """Compose hubd around the existing production session without wrapping it."""

    selected = config or ProductionHubConfig()
    factory = ProductionSessionFactory(selected, output=output, builder=builder)
    daemon = AirPodsHubDaemon(
        factory,
        socket_path,
        operation_timeout=selected.daemon_operation_timeout,
        session_error_is_recoverable=_is_recoverable_production_error,
        session_cleanup_completed=_production_cleanup_completed,
        lifecycle_output=output,
    )
    return ProductionHub(daemon=daemon, factory=factory)


__all__: list[str] = []
