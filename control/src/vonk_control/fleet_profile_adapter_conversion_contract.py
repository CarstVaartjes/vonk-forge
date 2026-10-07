"""Private contracts for the one-time retained profile journal conversion.

These proof inputs and conversion observations cannot authorize execution.
Only the converter consumes retained receipts; workers use the current
FleetProfileSwitchAdapterState after exact accepted-child validation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field
from vonk_agent_protocol import LifecycleState

from .fleet_profile_contract import (
    FleetProfileSwitchChildKind,
    FleetProfileSwitchChildResult,
    UuidId,
)
from .lifecycle.evidence import BookkeepingReason
from .strict_json import StrictModel


class ProfileAdapterConversionOutcome(StrictModel):
    state: Literal["current", "converted", "deferred"]
    reason: BookkeepingReason | None = None
    next_attempt_at: datetime | None = None
    detail: str | None = Field(default=None, max_length=512)


class _RetainedChild(StrictModel):
    operation_id: UuidId
    kind: FleetProfileSwitchChildKind
    state: Literal[
        LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
    ]
    result: FleetProfileSwitchChildResult | None = None


class _AcceptedRequestBinding(StrictModel):
    request_key: UuidId
    retry_of_application_id: UuidId | None
