"""Reusable Classic L2CAP transport session for Apple AAP.

This layer opens and closes only the L2CAP channel.  It deliberately has
no application-payload API and installs no SDP records.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import AsyncContextManager, Protocol, TypeVar

from bumble import l2cap

from airpods_hr.authentication import AuthenticatedClassicContext
from airpods_hr.bumble_compat import aap_flush_timeout_compatibility
from airpods_hr.protocol import AAP_PSM


class AAPChannelError(RuntimeError):
    """Base error for the AAP Classic L2CAP transport session."""


class AAPChannelOpenError(AAPChannelError):
    """Raised when the AAP L2CAP channel cannot be opened and configured."""


class AAPChannelOpenTimeoutError(AAPChannelOpenError):
    """Raised when AAP L2CAP creation exceeds its configured deadline."""


class AAPChannelStateError(AAPChannelOpenError):
    """Raised when Bumble returns a channel that is not open in Basic mode."""


class AAPChannelCloseError(AAPChannelError):
    """Raised when an opened AAP L2CAP channel cannot be closed."""


class AAPChannelCloseTimeoutError(AAPChannelCloseError):
    """Raised when AAP L2CAP close exceeds its configured deadline."""


class AAPChannelProgress(StrEnum):
    OPENED = "opened"
    CLOSED = "closed"


class AAPClassicConnection(Protocol):
    @property
    def l2cap_channel_manager(self) -> object: ...

    async def create_l2cap_channel(self, spec: object) -> object: ...


class SecureClassicSession(Protocol):
    def open(self) -> AsyncContextManager[AuthenticatedClassicContext]: ...


CompatibilityFactory = Callable[[object], AbstractContextManager[None]]
WaitFor = Callable[[Awaitable[object], float], Awaitable[object]]
ProtocolTransport = TypeVar("ProtocolTransport")


@dataclass(frozen=True, slots=True)
class AAPChannel:
    """Read-only, non-payload view of an open AAP L2CAP channel."""

    local_mtu: int
    peer_mtu: int
    mode: str = "Basic"
    psm: int = AAP_PSM


AAPProgressCallback = Callable[[AAPChannelProgress, AAPChannel | None], None]


@dataclass(frozen=True, slots=True)
class AAPL2CAPProbeResult:
    display_name: str
    local_mtu: int
    peer_mtu: int
    mode: str
    application_payload_sent: bool
    replacement_key_reported: bool


class AAPChannelSession:
    """Open one configured AAP channel under the scoped Bumble adapter."""

    def __init__(
        self,
        *,
        open_timeout: float = 10.0,
        close_timeout: float = 5.0,
        compatibility: CompatibilityFactory = aap_flush_timeout_compatibility,
        progress: AAPProgressCallback | None = None,
        wait_for: WaitFor = asyncio.wait_for,
    ) -> None:
        if open_timeout <= 0 or close_timeout <= 0:
            raise ValueError("AAP channel timeouts must be positive")
        self._open_timeout = open_timeout
        self._close_timeout = close_timeout
        self._compatibility = compatibility
        self._progress = progress
        self._wait_for = wait_for

    @asynccontextmanager
    async def open(
        self, connection: AAPClassicConnection
    ) -> AsyncIterator[AAPChannel]:
        """Open, verify, yield, and close one Basic-mode AAP channel."""

        async with self._open_raw(connection) as (_, channel):
            yield channel

    @asynccontextmanager
    async def open_protocol(
        self,
        connection: AAPClassicConnection,
        factory: Callable[[object, AAPChannel], ProtocolTransport],
    ) -> AsyncIterator[ProtocolTransport]:
        """Build one deliberate protocol adapter without exposing the channel."""

        async with self._open_raw(connection) as (raw_channel, channel):
            yield factory(raw_channel, channel)

    @asynccontextmanager
    async def _open_raw(
        self, connection: AAPClassicConnection
    ) -> AsyncIterator[tuple[object, AAPChannel]]:
        """Own the raw Bumble channel for a project protocol adapter."""

        raw_channel: object | None = None
        primary_error: BaseException | None = None
        manager = connection.l2cap_channel_manager
        with self._compatibility(manager):
            try:
                spec = l2cap.ClassicChannelSpec(
                    psm=AAP_PSM,
                    mode=l2cap.TransmissionMode.BASIC,
                )
                try:
                    raw_channel = await self._wait_for(
                        connection.create_l2cap_channel(spec),
                        self._open_timeout,
                    )
                except TimeoutError:
                    raise AAPChannelOpenTimeoutError(
                        "AAP L2CAP channel creation timed out"
                    ) from None
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise AAPChannelOpenError(
                        "AAP L2CAP channel creation failed"
                    ) from None

                self._verify_open_channel(raw_channel)
                channel = AAPChannel(
                    local_mtu=int(raw_channel.mtu),
                    peer_mtu=int(raw_channel.peer_mtu),
                )
                self._emit(AAPChannelProgress.OPENED, channel)
                yield raw_channel, channel
            except BaseException as error:
                primary_error = error
                raise
            finally:
                if raw_channel is not None:
                    try:
                        await self._wait_for(
                            raw_channel.disconnect(), self._close_timeout
                        )
                    except TimeoutError as cleanup_error:
                        close_error = AAPChannelCloseTimeoutError(
                            "AAP L2CAP channel close timed out"
                        )
                        if primary_error is not None:
                            primary_error.add_note(str(close_error))
                        else:
                            raise close_error from cleanup_error
                    except asyncio.CancelledError:
                        if primary_error is None:
                            raise
                        primary_error.add_note(
                            "AAP L2CAP channel close was cancelled during cleanup"
                        )
                    except Exception as cleanup_error:
                        close_error = AAPChannelCloseError(
                            "AAP L2CAP channel close failed"
                        )
                        if primary_error is not None:
                            primary_error.add_note(str(close_error))
                        else:
                            raise close_error from cleanup_error
                    else:
                        self._emit(AAPChannelProgress.CLOSED, None)

    @staticmethod
    def _verify_open_channel(channel: object) -> None:
        try:
            is_open = channel.state == l2cap.ClassicChannel.State.OPEN
            is_basic = channel.mode == l2cap.TransmissionMode.BASIC
            is_aap = channel.psm == AAP_PSM
            valid_mtus = int(channel.mtu) > 0 and int(channel.peer_mtu) > 0
        except (AttributeError, TypeError, ValueError):
            raise AAPChannelStateError(
                "Bumble returned an invalid AAP L2CAP channel"
            ) from None
        if not is_open:
            raise AAPChannelStateError("AAP L2CAP channel did not reach OPEN state")
        if not is_basic:
            raise AAPChannelStateError("AAP L2CAP channel is not in Basic mode")
        if not is_aap:
            raise AAPChannelStateError("Bumble returned the wrong L2CAP PSM")
        if not valid_mtus:
            raise AAPChannelStateError("AAP L2CAP channel reported an invalid MTU")

    def _emit(self, event: AAPChannelProgress, channel: AAPChannel | None) -> None:
        if self._progress is not None:
            self._progress(event, channel)


class AAPL2CAPProbeSession:
    """Compose the secure Classic session with one signaling-only AAP channel."""

    def __init__(
        self,
        secure_session: SecureClassicSession,
        channel_session: AAPChannelSession,
    ) -> None:
        self._secure_session = secure_session
        self._channel_session = channel_session

    async def run(self) -> AAPL2CAPProbeResult:
        secure: AuthenticatedClassicContext | None = None
        channel: AAPChannel | None = None
        async with self._secure_session.open() as secure:
            async with self._channel_session.open(secure.connection) as channel:
                # The probe intentionally performs no operation while the channel
                # is held. In particular, no application-data method is exposed.
                pass

        assert secure is not None and channel is not None
        return AAPL2CAPProbeResult(
            display_name=secure.display_name,
            local_mtu=channel.local_mtu,
            peer_mtu=channel.peer_mtu,
            mode=channel.mode,
            application_payload_sent=False,
            replacement_key_reported=secure.replacement_key_reported,
        )
