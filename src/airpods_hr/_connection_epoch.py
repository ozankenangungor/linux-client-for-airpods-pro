"""Private safe values shared by connection-epoch recovery boundaries."""

from __future__ import annotations

from enum import StrEnum


class ConnectionEpochRefreshOutcome(StrEnum):
    """Safe result from one finite target-device connection replacement."""

    REFRESHED = "refreshed"
    ALREADY_DISCONNECTED = "already_disconnected"


class ConnectionEpochRefreshStage(StrEnum):
    """Safe stage labels for expected connection-refresh failures."""

    DISCOVERY = "discovery"
    DISCONNECT_REQUEST = "disconnect_request"
    DISCONNECTED_STATE_PROOF = "disconnected_state_proof"
    CONNECT_REQUEST = "connect_request"
    CONNECTED_STATE_PROOF = "connected_state_proof"
    DBUS_CLEANUP = "dbus_cleanup"


class ConnectionEpochRefreshError(RuntimeError):
    """Expected operational failure containing no target-device secrets."""

    def __init__(self, stage: ConnectionEpochRefreshStage) -> None:
        self.stage = stage
        super().__init__(f"connection epoch refresh failed at {stage.value}")
