"""Reference-probe-only pre-authentication HCI diagnostics."""

from __future__ import annotations


import inspect
from collections.abc import Awaitable, Callable


from importlib import metadata


from bumble import hci
from bumble.device import Device
from bumble.host import Host


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


Sleep = Callable[[float], Awaitable[None]]


