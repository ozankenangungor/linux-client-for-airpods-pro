"""Installed process entrypoint for the private production hub daemon."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from airpods_hr import _airpods_aap_core as _native

from airpods_hr._hubd.production import (
    DEFAULT_DAEMON_OPERATION_TIMEOUT,
    ProductionHub,
    ProductionHubConfig,
    create_production_hub,
)
from airpods_hr._hubd.server import (
    DaemonAlreadyRunningError,
    DaemonState,
    HubDaemonError,
    SessionOperationError,
    UnsafeSocketPathError,
    socket_path_from_environment,
)


EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_CONFIGURATION = 2
LOGGER_NAME = "airpods_hr.hubd"

ProductionHubBuilder = Callable[..., ProductionHub]


class SignalRegistrar(Protocol):
    """The small signal boundary used by the runner and its unit tests."""

    def add(self, signum: signal.Signals, callback: Callable[[], None]) -> None: ...

    def remove(self, signum: signal.Signals) -> None: ...


class AsyncioSignalRegistrar:
    """Install callbacks on one running asyncio event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add(self, signum: signal.Signals, callback: Callable[[], None]) -> None:
        self._loop.add_signal_handler(signum, callback)

    def remove(self, signum: signal.Signals) -> None:
        self._loop.remove_signal_handler(signum)


def _safe_failure_name(error: BaseException) -> str:
    if isinstance(error, DaemonAlreadyRunningError):
        return _native.app_runner_safe_failure("already_running")
    if isinstance(error, UnsafeSocketPathError):
        return _native.app_runner_safe_failure("unsafe_socket_path")
    if isinstance(error, ValueError):
        return _native.app_runner_safe_failure("invalid_configuration")
    category = getattr(error, "category", None)
    if category is not None:
        value = getattr(category, "value", None)
        if isinstance(value, str):
            try:
                return _native.app_runner_production_category(value)
            except ValueError:
                pass
        return _native.app_runner_safe_failure("production_session")
    if isinstance(error, SessionOperationError):
        return _native.app_runner_safe_failure("session_operation")
    if isinstance(error, HubDaemonError):
        return _native.app_runner_safe_failure("hub_daemon")
    return _native.app_runner_safe_failure("unexpected")


async def run_daemon(
    socket_path: Path,
    config: ProductionHubConfig,
    *,
    shutdown_event: asyncio.Event | None = None,
    registrar: SignalRegistrar | None = None,
    hub_builder: ProductionHubBuilder | None = None,
    logger: logging.Logger | None = None,
    verbose: bool = False,
) -> int:
    """Run one production hub until a signal or injected stop request."""

    log = logger or logging.getLogger(LOGGER_NAME)
    stop_requested = shutdown_event or asyncio.Event()
    signal_registrar = registrar or AsyncioSignalRegistrar(
        asyncio.get_running_loop()
    )
    build_hub = hub_builder or create_production_hub
    first_signal: signal.Signals | None = None
    installed: list[signal.Signals] = []
    hub: ProductionHub | None = None
    startup_task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[bool] | None = None
    exit_code = EXIT_SUCCESS

    def request_shutdown(signum: signal.Signals) -> None:
        nonlocal first_signal
        if first_signal is None:
            first_signal = signum
            log.info("shutdown requested: %s", signum.name)
            stop_requested.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal_registrar.add(
                signum,
                lambda signum=signum: request_shutdown(signum),
            )
            installed.append(signum)

        log.info("daemon starting")
        hub = build_hub(
            socket_path,
            config=config,
            output=lambda message: log.info("production session: %s", message),
        )
        startup_task = asyncio.create_task(
            hub.daemon.start(), name="airpods-hubd-startup"
        )
        stop_task = asyncio.create_task(
            stop_requested.wait(), name="airpods-hubd-stop-request"
        )
        done, _ = await asyncio.wait(
            {startup_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done and not startup_task.done():
            startup_task.cancel()
        try:
            await startup_task
        except asyncio.CancelledError:
            if first_signal is None and not stop_requested.is_set():
                raise
            log.info("daemon startup interrupted")
        else:
            if hub.daemon.state is not DaemonState.READY:
                raise HubDaemonError("daemon did not reach READY")
            log.info("daemon ready")
            if not stop_requested.is_set():
                await stop_task

        if first_signal is not None:
            exit_code = _native.app_runner_outcome(None, int(first_signal), True)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        exit_code = _native.app_runner_outcome("unexpected", None, True)
        failure_name = _safe_failure_name(error)
        log.error("daemon failed: %s", failure_name)
        if verbose:
            log.debug("safe failure type: %s", type(error).__name__)
    finally:
        if stop_task is not None and not stop_task.done():
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        if startup_task is not None and not startup_task.done():
            startup_task.cancel()
            await asyncio.gather(startup_task, return_exceptions=True)
        if hub is not None:
            log.info("daemon shutting down")
            try:
                await hub.daemon.shutdown()
            except BaseException as error:
                exit_code = _native.app_runner_outcome(None, None, False)
                log.error("daemon cleanup failed: %s", _safe_failure_name(error))
            if hub.daemon.state is not DaemonState.STOPPED:
                exit_code = _native.app_runner_outcome(None, None, False)
                log.error("daemon cleanup failed: incomplete_shutdown")
            else:
                log.info("daemon stopped")
        for signum in reversed(installed):
            signal_registrar.remove(signum)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airpods-hubd",
        description="Run the private local AirPods sensor daemon.",
    )
    parser.add_argument(
        "--socket-path",
        type=Path,
        help="private Unix socket path (default: $XDG_RUNTIME_DIR/airpods-hubd.sock)",
    )
    parser.add_argument(
        "--operation-timeout",
        type=float,
        default=DEFAULT_DAEMON_OPERATION_TIMEOUT,
        help="bounded outer production operation timeout in seconds",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="include safe failure type details",
    )
    return parser


def _configure_logging(verbose: bool) -> logging.Logger:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s airpods-hubd: %(message)s",
    )
    return logging.getLogger(LOGGER_NAME)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logger = _configure_logging(args.verbose)
    try:
        config = ProductionHubConfig(
            daemon_operation_timeout=args.operation_timeout
        )
        socket_path = args.socket_path or socket_path_from_environment()
    except (ValueError, UnsafeSocketPathError) as error:
        logger.error("configuration failed: %s", _safe_failure_name(error))
        return _native.app_runner_outcome("invalid_configuration", None, True)
    try:
        return asyncio.run(
            run_daemon(
                socket_path,
                config,
                logger=logger,
                verbose=args.verbose,
            )
        )
    except KeyboardInterrupt:
        return _native.app_runner_outcome(None, int(signal.SIGINT), True)


__all__: list[str] = []


if __name__ == "__main__":
    raise SystemExit(main())
