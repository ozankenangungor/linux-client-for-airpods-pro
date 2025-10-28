"""Narrow Bumble compatibility for the observed AirPods AAP L2CAP request.

This module does not implement controller Automatic Flush Timeout behavior.  It
only lets stock Bumble's Classic L2CAP configuration state machine accept and
echo one well-formed ``FLUSH_TIMEOUT`` option for an AAP channel.
"""

from __future__ import annotations

import dataclasses
import inspect
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import metadata
from typing import Iterator

from bumble import l2cap

from airpods_hr.protocol import AAP_PSM


SUPPORTED_BUMBLE_VERSION = "0.0.234"
_HANDLER_ATTRIBUTE = "on_l2cap_configure_request"
_INSTALLATION_ATTRIBUTE = "_airpods_hr_aap_flush_timeout_compatibility"
_MISSING = object()

_STOCK_MANAGER_HANDLER = l2cap.ChannelManager.on_l2cap_configure_request
_STOCK_CHANNEL_HANDLER = l2cap.ClassicChannel.on_configure_request


class BumbleCompatibilityError(RuntimeError):
    """Base error for an unsafe or unsupported Bumble compatibility setup."""


class UnsupportedBumbleVersionError(BumbleCompatibilityError):
    """Raised when the installed Bumble release has not been reviewed."""


class UnsupportedBumbleAPIError(BumbleCompatibilityError):
    """Raised when Bumble's internal L2CAP API differs from the reviewed shape."""


class BumbleCompatibilityActivationError(BumbleCompatibilityError):
    """Raised when the manager-local compatibility adapter cannot be installed."""


@dataclass(frozen=True)
class _ConfigurationOption:
    option_type: int
    value: bytes
    encoded: bytes


@dataclass
class _Installation:
    original_handler: object
    previous_instance_handler: object
    wrapper: object
    references: int = 1


def _method_parameters(method: object) -> tuple[str, ...]:
    try:
        return tuple(inspect.signature(method).parameters)
    except (TypeError, ValueError) as error:
        raise UnsupportedBumbleAPIError(
            "Bumble Classic L2CAP handler signatures could not be inspected"
        ) from error


def validate_bumble_l2cap_api(*, installed_version: str | None = None) -> None:
    """Fail unless the installed Bumble internals match the reviewed release."""

    version = installed_version
    if version is None:
        try:
            version = metadata.version("bumble")
        except metadata.PackageNotFoundError as error:
            raise UnsupportedBumbleVersionError(
                "Bumble is not installed; the AAP compatibility layer requires "
                f"Bumble {SUPPORTED_BUMBLE_VERSION}"
            ) from error

    if version != SUPPORTED_BUMBLE_VERSION:
        raise UnsupportedBumbleVersionError(
            f"unsupported Bumble version {version!r}; expected "
            f"{SUPPORTED_BUMBLE_VERSION}"
        )

    if (
        l2cap.ChannelManager.on_l2cap_configure_request
        is not _STOCK_MANAGER_HANDLER
        or l2cap.ClassicChannel.on_configure_request is not _STOCK_CHANNEL_HANDLER
    ):
        raise UnsupportedBumbleAPIError(
            "Bumble Classic L2CAP handlers have been replaced; refusing to install "
            "the compatibility adapter"
        )

    if _method_parameters(_STOCK_MANAGER_HANDLER) != (
        "self",
        "connection",
        "cid",
        "request",
    ) or _method_parameters(_STOCK_CHANNEL_HANDLER) != ("self", "request"):
        raise UnsupportedBumbleAPIError(
            "Bumble Classic L2CAP handler signatures do not match 0.0.234"
        )

    required_fields = {
        "identifier",
        "destination_cid",
        "flags",
        "options",
    }
    try:
        request_fields = {
            field.name for field in dataclasses.fields(l2cap.L2CAP_Configure_Request)
        }
        flush_type = int(
            l2cap.L2CAP_Configure_Request.ParameterType.FLUSH_TIMEOUT
        )
        success = int(l2cap.L2CAP_Configure_Response.Result.SUCCESS)
    except (AttributeError, TypeError, ValueError) as error:
        raise UnsupportedBumbleAPIError(
            "Bumble L2CAP configuration types do not match 0.0.234"
        ) from error

    if not required_fields.issubset(request_fields) or flush_type != 0x02 or success != 0:
        raise UnsupportedBumbleAPIError(
            "Bumble L2CAP configuration constants do not match 0.0.234"
        )


def _decode_complete_options(
    data: bytes,
) -> tuple[list[_ConfigurationOption], int | None]:
    """Decode complete TLVs and return the type of an incomplete trailing TLV."""

    options: list[_ConfigurationOption] = []
    offset = 0
    while offset < len(data):
        if len(data) - offset < 2:
            return options, data[offset]
        length = data[offset + 1]
        end = offset + 2 + length
        if end > len(data):
            return options, data[offset]
        encoded = data[offset:end]
        options.append(
            _ConfigurationOption(
                option_type=data[offset],
                value=data[offset + 2 : end],
                encoded=encoded,
            )
        )
        offset = end
    return options, None


def _reject_malformed_flush_timeout(
    channel: l2cap.ClassicChannel,
    request: l2cap.L2CAP_Configure_Request,
) -> None:
    channel.send_control_frame(
        l2cap.L2CAP_Configure_Response(
            identifier=request.identifier,
            source_cid=channel.destination_cid,
            flags=0,
            result=l2cap.L2CAP_Configure_Response.Result.FAILURE_UNKNOWN_OPTIONS,
            options=b"",
        )
    )


def _handle_aap_configure_request(
    manager: l2cap.ChannelManager,
    original_manager_handler: object,
    connection: object,
    cid: int,
    request: l2cap.L2CAP_Configure_Request,
) -> None:
    """Handle the one reviewed option while delegating state logic to Bumble."""

    channel = manager.find_channel(connection.handle, request.destination_cid)
    if channel is None or channel.psm != AAP_PSM or request.flags != 0:
        original_manager_handler(connection, cid, request)  # type: ignore[operator]
        return

    options, incomplete_type = _decode_complete_options(request.options)
    if incomplete_type == 0x02:
        _reject_malformed_flush_timeout(channel, request)
        return
    if incomplete_type is not None:
        original_manager_handler(connection, cid, request)  # type: ignore[operator]
        return

    flush_options = [option for option in options if option.option_type == 0x02]
    if len(flush_options) != 1 or len(flush_options[0].value) != 2:
        original_manager_handler(connection, cid, request)  # type: ignore[operator]
        return

    filtered_options = b"".join(
        option.encoded for option in options if option.option_type != 0x02
    )
    filtered_request = l2cap.L2CAP_Configure_Request(
        identifier=request.identifier,
        destination_cid=request.destination_cid,
        flags=request.flags,
        options=filtered_options,
    )

    previous_send = channel.__dict__.get("send_control_frame", _MISSING)
    original_send = channel.send_control_frame

    def send_control_frame(frame: l2cap.L2CAP_Control_Frame) -> None:
        if (
            isinstance(frame, l2cap.L2CAP_Configure_Response)
            and frame.identifier == request.identifier
            and frame.result == l2cap.L2CAP_Configure_Response.Result.SUCCESS
        ):
            frame = l2cap.L2CAP_Configure_Response(
                identifier=frame.identifier,
                source_cid=frame.source_cid,
                flags=frame.flags,
                result=frame.result,
                options=request.options,
            )
        original_send(frame)

    channel.send_control_frame = send_control_frame
    try:
        channel.on_configure_request(filtered_request)
    finally:
        if previous_send is _MISSING:
            del channel.send_control_frame
        else:
            channel.send_control_frame = previous_send  # type: ignore[method-assign]


@contextmanager
def aap_flush_timeout_compatibility(
    manager: l2cap.ChannelManager,
) -> Iterator[None]:
    """Temporarily enable FLUSH_TIMEOUT negotiation for AAP on one manager.

    Nested activation for the same manager is idempotent.  The adapter is
    removed when the outermost context exits.  Other managers, other PSMs, and
    every option other than one two-byte FLUSH_TIMEOUT retain stock behavior.
    """

    validate_bumble_l2cap_api()
    if not isinstance(manager, l2cap.ChannelManager):
        raise BumbleCompatibilityActivationError(
            "compatibility requires a Bumble 0.0.234 ChannelManager instance"
        )

    existing = manager.__dict__.get(_INSTALLATION_ATTRIBUTE, _MISSING)
    if existing is not _MISSING:
        if (
            not isinstance(existing, _Installation)
            or manager.__dict__.get(_HANDLER_ATTRIBUTE) is not existing.wrapper
        ):
            raise BumbleCompatibilityActivationError(
                "the Bumble ChannelManager compatibility state is inconsistent"
            )
        existing.references += 1
        installation = existing
    else:
        previous_instance_handler = manager.__dict__.get(
            _HANDLER_ATTRIBUTE, _MISSING
        )
        original_handler = manager.on_l2cap_configure_request

        def wrapper(connection: object, cid: int, request: object) -> None:
            if not isinstance(request, l2cap.L2CAP_Configure_Request):
                original_handler(connection, cid, request)  # type: ignore[arg-type]
                return
            _handle_aap_configure_request(
                manager, original_handler, connection, cid, request
            )

        installation = _Installation(
            original_handler=original_handler,
            previous_instance_handler=previous_instance_handler,
            wrapper=wrapper,
        )
        try:
            setattr(manager, _HANDLER_ATTRIBUTE, wrapper)
            setattr(manager, _INSTALLATION_ATTRIBUTE, installation)
        except (AttributeError, TypeError) as error:
            raise BumbleCompatibilityActivationError(
                "the Bumble ChannelManager instance cannot be adapted safely"
            ) from error

    try:
        yield
    finally:
        installation.references -= 1
        if installation.references == 0:
            if manager.__dict__.get(_HANDLER_ATTRIBUTE) is not installation.wrapper:
                raise BumbleCompatibilityActivationError(
                    "the Bumble ChannelManager handler changed while compatibility "
                    "was active"
                )
            if installation.previous_instance_handler is _MISSING:
                delattr(manager, _HANDLER_ATTRIBUTE)
            else:
                setattr(
                    manager,
                    _HANDLER_ATTRIBUTE,
                    installation.previous_instance_handler,
                )
            delattr(manager, _INSTALLATION_ATTRIBUTE)
