"""Operator projection api: contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field, model_serializer
from vonk_agent_protocol import (
    EnrollmentGrantState,
    LifecycleState,
    ModelCacheOperatorStatus,
)

from ..agent_api import EnrollmentGrantResponse
from ..enrollment_contract import (
    EnrollmentGrantStatus,
    EnrollmentId,
    EnrollmentObservationOutcome,
    EnrollmentRevocationStatus,
)
from ..strict_json import StrictJSONModel

_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"

LogSource = Literal["client", "monitor", "runtime", "job"]

LogLevel = Literal["debug", "info", "warning", "error"]

_SELECTOR_PATTERN = r"^[^\x00-\x1f\x7f]{1,256}$"

FLEET_OPERATION_IDS = {
    ("get", "/api/fleet"): "getFleetStatus",
    ("get", "/api/fleet/locks"): "getFleetAdmissionLocks",
    ("get", "/api/fleet/{selector}"): "getFleetNode",
    ("get", "/api/fleet/{selector}/loginfo"): "getFleetLogInfo",
    ("post", "/api/fleet/{selector}/rename"): "renameFleetNode",
    ("post", "/api/fleet/enroll"): "enrollFleetNode",
    ("post", "/api/fleet/{selector}/re-enroll"): "reenrollFleetNode",
    ("get", "/api/fleet/enrollments/{grant_id}"): "getFleetEnrollment",
    ("post", "/api/fleet/enrollments/{grant_id}/revoke"): "revokeFleetEnrollment",
    ("post", "/api/fleet/{selector}/remove"): "removeFleetNode",
    ("post", "/api/fleet/upgrade"): "upgradeFleet",
}


class FleetRenameRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    display_name: str = Field(
        min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$"
    )


class FleetEnrollRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")
    request_key: EnrollmentId


class FleetReenrollRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_key: EnrollmentId


class FleetUpgradeRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request_key: EnrollmentId
    selectors: list[str] | None = Field(default=None, min_length=1, max_length=64)
    all: bool = False


class FleetActionResponse(StrictJSONModel):
    action: Literal["enroll", "re-enroll", "remove", "upgrade"]
    state: EnrollmentGrantState | LifecycleState | ModelCacheOperatorStatus
    operation_id: str | None = Field(default=None, max_length=128)
    node_id: str | None = Field(default=None, pattern=_NODE_PATTERN)
    display_name: str | None = Field(default=None, max_length=80)
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    request_key: EnrollmentId | None = None
    observation: EnrollmentObservationOutcome | None = None
    targets: list[str] = Field(default_factory=list, max_length=64)
    grant: EnrollmentGrantResponse | None = None
    grant_status: EnrollmentGrantStatus | None = None
    detail: str | None = Field(default=None, max_length=256)
    revocation: EnrollmentRevocationStatus | None = None

    @model_serializer(mode="wrap")
    def _omit_unused_request_key(self, handler):
        document = handler(self)
        if self.request_key is None:
            document.pop("request_key", None)
        return document


def _fleet_work_state(state: str) -> LifecycleState:
    """Project damaged bookkeeping as observation without replacing job authority."""
    try:
        return LifecycleState(state)
    except ValueError:
        return LifecycleState.OBSERVING


class FleetLockHolder(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    node_id: str | None = Field(default=None, max_length=128)
    namespace: str = Field(max_length=64)
    holder: str = Field(max_length=128)
    state: str | None = Field(default=None, max_length=64)
    transaction_age_seconds: float
    query: str | None = Field(default=None, max_length=256)


class FleetOpenTransaction(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    application_name: str | None = Field(default=None, max_length=128)
    state: str | None = Field(default=None, max_length=64)
    transaction_age_seconds: float
    query: str | None = Field(default=None, max_length=256)


class FleetLocksResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    held: list[FleetLockHolder] = Field(max_length=1_000)
    open_transactions: list[FleetOpenTransaction] = Field(max_length=100)


class FleetLogEntry(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observed_at: datetime
    source: Literal["client", "monitor", "runtime", "job"]
    level: Literal["debug", "info", "warning", "error"]
    message: str = Field(min_length=1, max_length=4_096)
    evidence_id: str = Field(min_length=1, max_length=128)


class FleetLogResponse(StrictJSONModel):
    node_id: str = Field(pattern=_NODE_PATTERN)
    since: datetime | None
    lines: int = Field(ge=1, le=1_000)
    entries: list[FleetLogEntry] = Field(max_length=1_000)
    retained: bool
    follow: bool
