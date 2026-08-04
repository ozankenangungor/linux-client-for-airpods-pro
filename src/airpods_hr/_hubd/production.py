"""Private composition boundary between hubd and the production session."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from airpods_hr import _airpods_aap_core as _native

from airpods_hr._hubd.server import AirPodsHubDaemon
from airpods_hr.bluez_coexistence import BlueZConnectionEpochRefresher
from airpods_hr.production_session import (
    DEFAULT_DBUS_TIMEOUT,
    DEFAULT_HANDSHAKE_TIMEOUT,
    DEFAULT_L2CAP_CONNECT_TIMEOUT,
    DEFAULT_START_TIMEOUT,
    DEFAULT_STOP_TIMEOUT,
    InternalProductionSession,
    ProductionSessionCategory,
    ProductionSessionError,
    ProductionSessionState,
    create_production_session,
)


DEFAULT_DESCRIPTOR_TIMEOUT, DEFAULT_DAEMON_OPERATION_TIMEOUT = (
    _native.app_production_defaults()
)

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

    return _native.app_production_minimum(
        descriptor_timeout, dbus_timeout, connect_timeout,
        handshake_timeout, start_timeout, stop_timeout,
    )


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
        try:
            _native.app_production_validate(
                self.descriptor_timeout, self.dbus_timeout, self.connect_timeout,
                self.handshake_timeout, self.start_timeout, self.stop_timeout,
                self.daemon_operation_timeout,
            )
        except ValueError as error:
            if "below the production open window" in str(error):
                minimum = minimum_daemon_operation_timeout(
                    descriptor_timeout=self.descriptor_timeout,
                    dbus_timeout=self.dbus_timeout,
                    connect_timeout=self.connect_timeout,
                    handshake_timeout=self.handshake_timeout,
                    start_timeout=self.start_timeout,
                    stop_timeout=self.stop_timeout,
                )
                raise ValueError(f"{error} ({minimum:g}s minimum)") from error
            raise


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


def _production_epoch_refresh_is_eligible(error: BaseException) -> bool:
    return (
        isinstance(error, ProductionSessionError)
        and error.recoverable is True
        and error.category is ProductionSessionCategory.AAP_DESCRIPTOR_TIMEOUT
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
    epoch_refresher = BlueZConnectionEpochRefresher(
        dbus_timeout=selected.dbus_timeout,
        state_timeout=selected.connect_timeout,
    )
    daemon = AirPodsHubDaemon(
        factory,
        socket_path,
        operation_timeout=selected.daemon_operation_timeout,
        session_error_is_recoverable=_is_recoverable_production_error,
        session_cleanup_completed=_production_cleanup_completed,
        epoch_refresh_is_eligible=_production_epoch_refresh_is_eligible,
        connection_epoch_refresher=epoch_refresher,
        lifecycle_output=output,
    )
    return ProductionHub(daemon=daemon, factory=factory)


__all__: list[str] = []
