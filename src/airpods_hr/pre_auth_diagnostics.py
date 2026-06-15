"""Reference-probe-only pre-authentication HCI diagnostics."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from importlib import metadata
from typing import Any, Protocol

from bumble import hci, utils
from bumble.device import Device
from bumble.host import Host

from . import _airpods_aap_core as _native


PRE_AUTH_DELAY_SECONDS = 0.085
DEFAULT_REMOTE_DISCOVERY_TIMEOUT = 2.0
SUPPORTED_BUMBLE_VERSION = "0.0.234"

_STOCK_DEVICE_SEND_COMMAND = Device.send_command
_STOCK_SUPPORTED_FEATURES_HANDLER = (
    Host.on_hci_read_remote_supported_features_complete_event
)
_STOCK_EXTENDED_FEATURES_HANDLER = (
    Host.on_hci_read_remote_extended_features_complete_event
)
_STOCK_REMOTE_NAME_HANDLER = Host.on_hci_remote_name_request_complete_event


class PreAuthSequenceMode(StrEnum):
    """Connect-to-authentication sequence for the private reference probe."""

    PROVEN = "proven"
    DELAY_ONLY = "delay-only"
    BLUEZ_DISCOVERY = "bluez-discovery"


class RemoteDiscoveryResult(StrEnum):
    """Safe result category for one remote-discovery operation."""

    NOT_APPLICABLE = "not-applicable"
    SUCCESS = "success"
    TIMEOUT = "timeout"
    OTHER = "other"


class PreAuthSequenceError(RuntimeError):
    """Raised when the diagnostic pre-authentication sequence fails closed."""


class BumblePreAuthCompatibilityError(PreAuthSequenceError):
    """Raised when Bumble differs from the reviewed diagnostic contract."""


def _parameters(function: object) -> tuple[str, ...]:
    try:
        return tuple(inspect.signature(function).parameters)
    except (TypeError, ValueError) as error:
        raise BumblePreAuthCompatibilityError(
            "Bumble pre-authentication API signatures cannot be inspected"
        ) from error


def _emits(handler: object, *event_names: str) -> bool:
    try:
        source = inspect.getsource(handler)
    except (OSError, TypeError):
        return False
    return all(repr(event_name) in source for event_name in event_names)


def validate_bumble_pre_auth_api(
    *, installed_version: str | None = None
) -> None:
    """Require the exact Bumble 0.0.234 command and event API reviewed here."""

    version = installed_version
    if version is None:
        try:
            version = metadata.version("bumble")
        except metadata.PackageNotFoundError as error:
            raise BumblePreAuthCompatibilityError(
                "Bumble is unavailable for pre-authentication diagnostics"
            ) from error
    if version != SUPPORTED_BUMBLE_VERSION:
        raise BumblePreAuthCompatibilityError(
            f"unsupported Bumble version {version!r} for pre-authentication "
            f"diagnostics; expected {SUPPORTED_BUMBLE_VERSION}"
        )
    if (
        Device.send_command is not _STOCK_DEVICE_SEND_COMMAND
        or Host.on_hci_read_remote_supported_features_complete_event
        is not _STOCK_SUPPORTED_FEATURES_HANDLER
        or Host.on_hci_read_remote_extended_features_complete_event
        is not _STOCK_EXTENDED_FEATURES_HANDLER
        or Host.on_hci_remote_name_request_complete_event
        is not _STOCK_REMOTE_NAME_HANDLER
    ):
        raise BumblePreAuthCompatibilityError(
            "Bumble pre-authentication command or event handlers were replaced"
        )
    if _parameters(_STOCK_DEVICE_SEND_COMMAND) != (
        "self",
        "command",
        "check_result",
    ) or any(
        _parameters(handler) != ("self", "event")
        for handler in (
            _STOCK_SUPPORTED_FEATURES_HANDLER,
            _STOCK_EXTENDED_FEATURES_HANDLER,
            _STOCK_REMOTE_NAME_HANDLER,
        )
    ):
        raise BumblePreAuthCompatibilityError(
            "Bumble pre-authentication method signatures differ from 0.0.234"
        )
    if (
        not _emits(_STOCK_SUPPORTED_FEATURES_HANDLER, "classic_remote_features")
        or not _emits(
            _STOCK_EXTENDED_FEATURES_HANDLER, "classic_remote_features"
        )
        or not _emits(
            _STOCK_REMOTE_NAME_HANDLER, "remote_name", "remote_name_failure"
        )
        or not all(
            issubclass(command_type, hci.HCI_AsyncCommand)
            for command_type in (
                hci.HCI_Read_Remote_Supported_Features_Command,
                hci.HCI_Read_Remote_Extended_Features_Command,
                hci.HCI_Remote_Name_Request_Command,
            )
        )
        or not hasattr(hci.HCI_Command_Status_Event, "fields")
    ):
        raise BumblePreAuthCompatibilityError(
            "Bumble pre-authentication event or command types differ from 0.0.234"
        )


async def _send_accepted_command(device: object, command: object) -> None:
    """Send an async HCI command and require its initial pending status."""

    response = await device.send_command(command)
    if (
        not isinstance(response, hci.HCI_Command_Status_Event)
        or not _native.pre_auth_command_accepted(response.status)
    ):
        raise PreAuthSequenceError("pre-authentication HCI command was rejected")


@dataclass(frozen=True, slots=True)
class PreAuthSequenceObservation:
    """Allowlisted metadata retained for the pre-authentication experiment."""

    mode: PreAuthSequenceMode
    delay_ms: int
    remote_supported_features_request_sent: bool
    remote_supported_features_command_accepted: bool
    remote_supported_features_response_observed: bool
    remote_supported_features_result: RemoteDiscoveryResult
    remote_supported_features_mask: int | None
    remote_extended_features_request_sent: bool
    remote_extended_features_command_accepted: bool
    remote_extended_features_page: int | None
    remote_extended_features_response_observed: bool
    remote_extended_features_result: RemoteDiscoveryResult
    remote_extended_features_max_page: int | None
    remote_extended_features_mask: int | None
    remote_name_request_sent: bool
    remote_name_command_accepted: bool
    remote_name_response_observed: bool
    remote_name_result: RemoteDiscoveryResult
    authentication_attempted: bool


@dataclass(frozen=True, slots=True)
class _FeatureResponse:
    result: RemoteDiscoveryResult
    mask: int | None
    maximum_page: int | None = None


class _RemoteDiscovery(Protocol):
    async def run(
        self, connection: object, strategy: PreAuthSequenceStrategy
    ) -> None: ...


Sleep = Callable[[float], Awaitable[None]]


class PreAuthSequenceStrategy:
    """Run one bounded experiment before the proven authenticate operation."""

    def __init__(
        self,
        mode: PreAuthSequenceMode,
        *,
        sleep: Sleep = asyncio.sleep,
        remote_discovery: _RemoteDiscovery | None = None,
    ) -> None:
        self.mode = PreAuthSequenceMode(mode)
        self._native = _native._PreAuthDiagnosticState(self.mode.value)
        self._sleep = sleep
        self._remote_discovery = remote_discovery or BumbleRemoteDiscovery()

    @property
    def observation(self) -> PreAuthSequenceObservation:
        values = self._native.snapshot()
        values["mode"] = PreAuthSequenceMode(values["mode"])
        for key in (
            "remote_supported_features_result",
            "remote_extended_features_result",
            "remote_name_result",
        ):
            values[key] = RemoteDiscoveryResult(values[key])
        return PreAuthSequenceObservation(**values)

    async def run(self, connection: object) -> None:
        if self.mode is PreAuthSequenceMode.PROVEN:
            return
        if self.mode is PreAuthSequenceMode.DELAY_ONLY:
            await self._sleep(PRE_AUTH_DELAY_SECONDS)
            return
        await self._remote_discovery.run(connection, self)

    async def before_authentication(self, connection: object) -> None:
        """Run the experiment and mark the immediately following auth attempt."""

        await self.run(connection)
        self.mark_authentication_attempted()

    def mark_authentication_attempted(self) -> None:
        self._native.mark_authentication_attempted()

    def _supported_request(self) -> None:
        self._native.supported_request()

    def _supported_accepted(self) -> None:
        self._native.supported_accepted()

    def _supported_response(
        self, *, observed: bool, result: RemoteDiscoveryResult, mask: int | None
    ) -> None:
        self._native.supported_response(observed, result.value, mask)

    def _extended_request(self) -> None:
        self._native.extended_request()

    def _extended_accepted(self) -> None:
        self._native.extended_accepted()

    def _extended_response(
        self,
        *,
        observed: bool,
        result: RemoteDiscoveryResult,
        maximum_page: int | None,
        mask: int | None,
    ) -> None:
        self._native.extended_response(observed, result.value, maximum_page, mask)

    def _name_request(self) -> None:
        self._native.name_request()

    def _name_accepted(self) -> None:
        self._native.name_accepted()

    def _name_response(
        self, *, observed: bool, result: RemoteDiscoveryResult
    ) -> None:
        self._native.name_response(observed, result.value)


class BumbleRemoteDiscovery:
    """Run the observed three-command sequence through Bumble's HCI API."""

    def __init__(
        self, *, operation_timeout: float = DEFAULT_REMOTE_DISCOVERY_TIMEOUT
    ) -> None:
        if operation_timeout <= 0:
            raise ValueError("remote discovery timeout must be positive")
        validate_bumble_pre_auth_api()
        self._operation_timeout = operation_timeout

    async def run(
        self, connection: object, strategy: PreAuthSequenceStrategy
    ) -> None:
        raw_connection, device, host = self._require_bumble_context(connection)
        await self._read_supported_features(
            raw_connection, device, host, strategy
        )
        await self._read_extended_features_page_one(
            raw_connection, device, host, strategy
        )
        await self._request_remote_name(
            raw_connection, device, host, strategy
        )

    async def _read_supported_features(
        self,
        raw_connection: object,
        device: object,
        host: object,
        strategy: PreAuthSequenceStrategy,
    ) -> None:
        strategy._supported_request()

        async def transaction() -> _FeatureResponse:
            future = asyncio.get_running_loop().create_future()
            with closing(utils.EventWatcher()) as watcher:

                @watcher.on(host, "classic_remote_features")
                def observe(
                    handle: int,
                    status: int,
                    features: int,
                    page: int,
                    maximum_page: int,
                ) -> None:
                    del maximum_page
                    if handle != raw_connection.handle or future.done():
                        return
                    result, mask = _native.pre_auth_supported_completion(
                        status, page, features
                    )
                    future.set_result(
                        _FeatureResponse(RemoteDiscoveryResult(result), mask)
                    )

                await _send_accepted_command(
                    device,
                    hci.HCI_Read_Remote_Supported_Features_Command(
                        connection_handle=raw_connection.handle
                    ),
                )
                strategy._supported_accepted()
                return await future

        try:
            result = await asyncio.wait_for(
                transaction(), timeout=self._operation_timeout
            )
        except TimeoutError:
            strategy._supported_response(
                observed=False,
                result=RemoteDiscoveryResult.TIMEOUT,
                mask=None,
            )
            raise PreAuthSequenceError(
                "Read Remote Supported Features timed out"
            ) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            strategy._supported_response(
                observed=False,
                result=RemoteDiscoveryResult.OTHER,
                mask=None,
            )
            raise PreAuthSequenceError(
                "Read Remote Supported Features failed"
            ) from None
        strategy._supported_response(
            observed=True, result=result.result, mask=result.mask
        )
        if result.result is not RemoteDiscoveryResult.SUCCESS:
            raise PreAuthSequenceError(
                "Read Remote Supported Features completion was invalid"
            )

    async def _read_extended_features_page_one(
        self,
        raw_connection: object,
        device: object,
        host: object,
        strategy: PreAuthSequenceStrategy,
    ) -> None:
        strategy._extended_request()

        async def transaction() -> _FeatureResponse:
            future = asyncio.get_running_loop().create_future()
            with closing(utils.EventWatcher()) as watcher:

                @watcher.on(host, "classic_remote_features")
                def observe(
                    handle: int,
                    status: int,
                    features: int,
                    page: int,
                    maximum_page: int,
                ) -> None:
                    if handle != raw_connection.handle or future.done():
                        return
                    result, mask, max_page = _native.pre_auth_extended_completion(
                        status, page, maximum_page, features
                    )
                    future.set_result(
                        _FeatureResponse(RemoteDiscoveryResult(result), mask, max_page)
                    )

                await _send_accepted_command(
                    device,
                    hci.HCI_Read_Remote_Extended_Features_Command(
                        connection_handle=raw_connection.handle,
                        page_number=1,
                    ),
                )
                strategy._extended_accepted()
                return await future

        try:
            result = await asyncio.wait_for(
                transaction(), timeout=self._operation_timeout
            )
        except TimeoutError:
            strategy._extended_response(
                observed=False,
                result=RemoteDiscoveryResult.TIMEOUT,
                maximum_page=None,
                mask=None,
            )
            raise PreAuthSequenceError(
                "Read Remote Extended Features timed out"
            ) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            strategy._extended_response(
                observed=False,
                result=RemoteDiscoveryResult.OTHER,
                maximum_page=None,
                mask=None,
            )
            raise PreAuthSequenceError(
                "Read Remote Extended Features failed"
            ) from None
        strategy._extended_response(
            observed=True,
            result=result.result,
            maximum_page=result.maximum_page,
            mask=result.mask,
        )
        if result.result is not RemoteDiscoveryResult.SUCCESS:
            raise PreAuthSequenceError(
                "Read Remote Extended Features completion was invalid"
            )

    async def _request_remote_name(
        self,
        raw_connection: object,
        device: object,
        host: object,
        strategy: PreAuthSequenceStrategy,
    ) -> None:
        strategy._name_request()

        async def transaction() -> RemoteDiscoveryResult:
            future = asyncio.get_running_loop().create_future()
            with closing(utils.EventWatcher()) as watcher:

                @watcher.on(host, "remote_name")
                def observe_name(address: object, remote_name: bytes) -> None:
                    del remote_name
                    if address == raw_connection.peer_address and not future.done():
                        future.set_result(RemoteDiscoveryResult.SUCCESS)

                @watcher.on(host, "remote_name_failure")
                def observe_failure(address: object, status: int) -> None:
                    del status
                    if address == raw_connection.peer_address and not future.done():
                        future.set_result(RemoteDiscoveryResult.OTHER)

                await _send_accepted_command(
                    device,
                    hci.HCI_Remote_Name_Request_Command(
                        bd_addr=raw_connection.peer_address,
                        page_scan_repetition_mode=(
                            hci.HCI_Remote_Name_Request_Command.R2
                        ),
                        reserved=0,
                        clock_offset=0,
                    ),
                )
                strategy._name_accepted()
                return await future

        try:
            result = await asyncio.wait_for(
                transaction(), timeout=self._operation_timeout
            )
        except TimeoutError:
            strategy._name_response(
                observed=False, result=RemoteDiscoveryResult.TIMEOUT
            )
            raise PreAuthSequenceError(
                "Remote Name Request timed out"
            ) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            strategy._name_response(
                observed=False, result=RemoteDiscoveryResult.OTHER
            )
            raise PreAuthSequenceError("Remote Name Request failed") from None
        strategy._name_response(observed=True, result=result)
        if result is not RemoteDiscoveryResult.SUCCESS:
            raise PreAuthSequenceError(
                "Remote Name Request completion was unsuccessful"
            )

    @staticmethod
    def _require_bumble_context(
        connection: object,
    ) -> tuple[Any, Any, Any]:
        try:
            raw_connection = connection._connection
            device = raw_connection.device
            host = device.host
            handle = raw_connection.handle
            peer_address = raw_connection.peer_address
        except AttributeError:
            raise PreAuthSequenceError(
                "Bumble remote-discovery context is unavailable"
            ) from None
        if (
            not isinstance(handle, int)
            or handle < 0
            or peer_address is None
            or not callable(getattr(device, "send_command", None))
        ):
            raise PreAuthSequenceError(
                "Bumble remote-discovery context is invalid"
            )
        return raw_connection, device, host
