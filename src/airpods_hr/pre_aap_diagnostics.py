"""Reference-probe-only pre-AAP L2CAP sequence diagnostics."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from bumble import l2cap


PRE_AAP_DELAY_SECONDS = 0.020
DEFAULT_INFORMATION_RESPONSE_TIMEOUT = 2.0
_INFORMATION_RESPONSE_HANDLER = "on_l2cap_information_response"
_MISSING = object()


class PreAAPSequenceMode(StrEnum):
    """Pre-AAP sequence available only to the private reference probe."""

    PROVEN = "proven"
    DELAY_ONLY = "delay-only"
    BLUEZ_L2CAP_INFO = "bluez-l2cap-info"


class InformationResponseResult(StrEnum):
    """Safe outcome categories for one L2CAP Information transaction."""

    NOT_APPLICABLE = "not-applicable"
    SUCCESS = "success"
    NOT_SUPPORTED = "not_supported"
    TIMEOUT = "timeout"
    OTHER = "other"


class PreAAPSequenceError(RuntimeError):
    """Raised when the diagnostic pre-AAP sequence cannot finish safely."""


@dataclass(frozen=True, slots=True)
class PreAAPSequenceObservation:
    """Allowlisted metadata for the probe-only pre-AAP sequence."""

    mode: PreAAPSequenceMode
    delay_ms: int
    extended_features_request_sent: bool
    extended_features_response_observed: bool
    extended_features_result: InformationResponseResult
    extended_features_mask: int | None
    fixed_channels_request_sent: bool
    fixed_channels_response_observed: bool
    fixed_channels_result: InformationResponseResult
    fixed_channels_mask: int | None
    aap_open_attempted: bool


@dataclass(frozen=True, slots=True)
class _DecodedInformationResponse:
    result: InformationResponseResult
    mask: int | None


class _InformationExchange(Protocol):
    async def run(
        self, connection: object, strategy: PreAAPSequenceStrategy
    ) -> None: ...


Sleep = Callable[[float], Awaitable[None]]


class PreAAPSequenceStrategy:
    """Execute one private sequence after encryption and before AAP open."""

    def __init__(
        self,
        mode: PreAAPSequenceMode,
        *,
        sleep: Sleep = asyncio.sleep,
        information_exchange: _InformationExchange | None = None,
    ) -> None:
        self.mode = PreAAPSequenceMode(mode)
        self._sleep = sleep
        self._information_exchange = (
            information_exchange or BumbleInformationExchange()
        )
        self._extended_request_sent = False
        self._extended_response_observed = False
        self._extended_result = InformationResponseResult.NOT_APPLICABLE
        self._extended_mask: int | None = None
        self._fixed_request_sent = False
        self._fixed_response_observed = False
        self._fixed_result = InformationResponseResult.NOT_APPLICABLE
        self._fixed_mask: int | None = None
        self._aap_open_attempted = False

    @property
    def observation(self) -> PreAAPSequenceObservation:
        return PreAAPSequenceObservation(
            mode=self.mode,
            delay_ms=(
                round(PRE_AAP_DELAY_SECONDS * 1000)
                if self.mode is PreAAPSequenceMode.DELAY_ONLY
                else 0
            ),
            extended_features_request_sent=self._extended_request_sent,
            extended_features_response_observed=self._extended_response_observed,
            extended_features_result=self._extended_result,
            extended_features_mask=self._extended_mask,
            fixed_channels_request_sent=self._fixed_request_sent,
            fixed_channels_response_observed=self._fixed_response_observed,
            fixed_channels_result=self._fixed_result,
            fixed_channels_mask=self._fixed_mask,
            aap_open_attempted=self._aap_open_attempted,
        )

    async def run(self, connection: object) -> None:
        if self.mode is PreAAPSequenceMode.PROVEN:
            return
        if self.mode is PreAAPSequenceMode.DELAY_ONLY:
            await self._sleep(PRE_AAP_DELAY_SECONDS)
            return
        await self._information_exchange.run(connection, self)

    def mark_aap_open_attempted(self) -> None:
        self._aap_open_attempted = True

    def _request_sent(self, info_type: int) -> None:
        if info_type == 0x0002:
            self._extended_request_sent = True
            self._extended_result = InformationResponseResult.OTHER
        elif info_type == 0x0003:
            self._fixed_request_sent = True
            self._fixed_result = InformationResponseResult.OTHER
        else:
            raise ValueError("unsupported diagnostic Information Type")

    def _response(
        self,
        info_type: int,
        *,
        observed: bool,
        result: InformationResponseResult,
        mask: int | None,
    ) -> None:
        if info_type == 0x0002:
            self._extended_response_observed = observed
            self._extended_result = result
            self._extended_mask = mask
        elif info_type == 0x0003:
            self._fixed_response_observed = observed
            self._fixed_result = result
            self._fixed_mask = mask
        else:
            raise ValueError("unsupported diagnostic Information Type")


class BumbleInformationExchange:
    """Issue two requests through Bumble's Classic signaling manager."""

    def __init__(
        self,
        *,
        response_timeout: float = DEFAULT_INFORMATION_RESPONSE_TIMEOUT,
    ) -> None:
        if response_timeout <= 0:
            raise ValueError("information response timeout must be positive")
        self._response_timeout = response_timeout

    async def run(
        self, connection: object, strategy: PreAAPSequenceStrategy
    ) -> None:
        manager, raw_connection = self._require_bumble_signaling(connection)
        with self._observe_information_responses(
            manager, raw_connection
        ) as pending:
            info_types = l2cap.L2CAP_Information_Request.InfoType
            await self._request(
                manager,
                raw_connection,
                strategy,
                int(info_types.EXTENDED_FEATURES_SUPPORTED),
                pending,
            )
            await self._request(
                manager,
                raw_connection,
                strategy,
                int(info_types.FIXED_CHANNELS_SUPPORTED),
                pending,
            )

    @contextmanager
    def _observe_information_responses(
        self, manager: object, raw_connection: object
    ) -> Iterator[
        dict[tuple[int, int], asyncio.Future[_DecodedInformationResponse]]
    ]:
        previous_handler = getattr(manager, _INFORMATION_RESPONSE_HANDLER, None)
        if previous_handler is not None and not callable(previous_handler):
            raise PreAAPSequenceError(
                "Bumble Information Response handler is invalid"
            )
        previous_instance_handler = getattr(manager, "__dict__", {}).get(
            _INFORMATION_RESPONSE_HANDLER, _MISSING
        )
        pending: dict[
            tuple[int, int], asyncio.Future[_DecodedInformationResponse]
        ] = {}

        def observe_response(
            response_connection: object, cid: int, response: object
        ) -> None:
            if (
                response_connection is raw_connection
                and cid == l2cap.L2CAP_SIGNALING_CID
                and isinstance(response, l2cap.L2CAP_Information_Response)
            ):
                key = (int(response.identifier), int(response.info_type))
                future = pending.get(key)
                if future is not None and not future.done():
                    future.set_result(self._decode_response(response))
                    return
            if previous_handler is not None:
                previous_handler(response_connection, cid, response)

        setattr(manager, _INFORMATION_RESPONSE_HANDLER, observe_response)
        try:
            yield pending
        finally:
            if previous_instance_handler is _MISSING:
                delattr(manager, _INFORMATION_RESPONSE_HANDLER)
            else:
                setattr(
                    manager,
                    _INFORMATION_RESPONSE_HANDLER,
                    previous_instance_handler,
                )

    async def _request(
        self,
        manager: object,
        raw_connection: object,
        strategy: PreAAPSequenceStrategy,
        info_type: int,
        pending: dict[
            tuple[int, int], asyncio.Future[_DecodedInformationResponse]
        ],
    ) -> None:
        identifier = int(manager.next_identifier(raw_connection))
        key = (identifier, info_type)
        future = asyncio.get_running_loop().create_future()
        pending[key] = future
        strategy._request_sent(info_type)
        try:
            manager.send_control_frame(
                raw_connection,
                l2cap.L2CAP_SIGNALING_CID,
                l2cap.L2CAP_Information_Request(
                    identifier=identifier,
                    info_type=info_type,
                ),
            )
            try:
                decoded = await asyncio.wait_for(
                    future, timeout=self._response_timeout
                )
            except TimeoutError:
                strategy._response(
                    info_type,
                    observed=False,
                    result=InformationResponseResult.TIMEOUT,
                    mask=None,
                )
                raise PreAAPSequenceError(
                    "L2CAP Information Response timed out"
                ) from None
            strategy._response(
                info_type,
                observed=True,
                result=decoded.result,
                mask=decoded.mask,
            )
            if decoded.result is not InformationResponseResult.SUCCESS:
                raise PreAAPSequenceError(
                    "L2CAP Information Response was not successful"
                )
        except PreAAPSequenceError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            strategy._response(
                info_type,
                observed=False,
                result=InformationResponseResult.OTHER,
                mask=None,
            )
            raise PreAAPSequenceError(
                "L2CAP Information Request failed"
            ) from None
        finally:
            pending.pop(key, None)

    @staticmethod
    def _decode_response(
        response: l2cap.L2CAP_Information_Response,
    ) -> _DecodedInformationResponse:
        if int(response.result) == int(
            l2cap.L2CAP_Information_Response.Result.NOT_SUPPORTED
        ):
            return _DecodedInformationResponse(
                InformationResponseResult.NOT_SUPPORTED, None
            )
        if int(response.result) != int(
            l2cap.L2CAP_Information_Response.Result.SUCCESS
        ):
            return _DecodedInformationResponse(
                InformationResponseResult.OTHER, None
            )
        expected_length = 4 if int(response.info_type) == 0x0002 else 8
        if len(response.data) != expected_length:
            return _DecodedInformationResponse(
                InformationResponseResult.OTHER, None
            )
        return _DecodedInformationResponse(
            InformationResponseResult.SUCCESS,
            int.from_bytes(response.data, "little"),
        )

    @staticmethod
    def _require_bumble_signaling(connection: object) -> tuple[Any, Any]:
        try:
            manager = connection.l2cap_channel_manager
            raw_connection = connection._connection
            raw_manager = raw_connection.device.l2cap_channel_manager
        except AttributeError:
            raise PreAAPSequenceError(
                "Bumble signaling context is unavailable"
            ) from None
        if manager is not raw_manager:
            raise PreAAPSequenceError(
                "Bumble signaling manager identity mismatch"
            )
        if not callable(getattr(manager, "next_identifier", None)) or not callable(
            getattr(manager, "send_control_frame", None)
        ):
            raise PreAAPSequenceError(
                "Bumble signaling manager API is unavailable"
            )
        return manager, raw_connection


class PreAAPSequenceSecureSession:
    """Run the selected sequence inside the encrypted secure-session scope."""

    def __init__(self, delegate: object, strategy: PreAAPSequenceStrategy):
        self.delegate = delegate
        self.strategy = strategy

    @asynccontextmanager
    async def open(
        self, *, pre_connect_profile: object | None = None
    ) -> AsyncIterator[Any]:
        async with self.delegate.open(
            pre_connect_profile=pre_connect_profile
        ) as context:
            await self.strategy.run(context.connection)
            yield context


class PreAAPAAPChannelSession:
    """Mark the exact AAP-open attempt while delegating channel behavior."""

    def __init__(self, delegate: object, strategy: PreAAPSequenceStrategy):
        self.delegate = delegate
        self.strategy = strategy

    @asynccontextmanager
    async def open_protocol(
        self, connection: object, factory: Callable[[object, object], object]
    ) -> AsyncIterator[Any]:
        self.strategy.mark_aap_open_attempted()
        async with self.delegate.open_protocol(connection, factory) as transport:
            yield transport
