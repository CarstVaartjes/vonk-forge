"""Fleet profile contract: applications."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)
from vonk_agent_protocol import (
    LifecycleState,
)
from vonk_agent_protocol.agent_words import (
    ProfileEffectState,
)

from ..integer_domains import MAX_DATABASE_INTEGER
from ..operation_blockers import OperationBlocker
from ..run_switch_contract import (
    RunSwitchProgress,
)
from ..strict_json import StrictModel
from .definitions import FleetProfileAssignment, FleetProfileScope
from .effects import FleetProfileChildProgress, FleetProfileRunEffect
from .switch_state import (
    FleetProfileChildResult,
    FleetProfileStepResult,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildKind,
    FleetProfileSwitchChildResult,
)
from .vocabulary import (
    Digest,
    FleetProfileCancellationState,
    FleetProfileInstallationPolicy,
    FleetProfileOperationKind,
    FleetProfileOperationState,
    FleetProfileSupersedeCode,
    NodeId,
    UuidId,
)


class FleetProfileIntendedConfiguration(StrictModel):
    """Immutable desired configuration captured when execution is admitted."""

    profile_digest: Digest
    reviewed_plan_digest: Digest
    reviewed_application_id: UuidId
    installation_policy: FleetProfileInstallationPolicy
    scope: FleetProfileScope
    assignments: list[FleetProfileAssignment] = Field(max_length=64)


class FleetProfileApplicationCancellationIntent(StrictModel):
    """Durable identity and authority for an explicit or superseding cancel."""

    request_key: UuidId
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    requested_at: datetime
    state: FleetProfileCancellationState = LifecycleState.OBSERVING
    cause: Literal["operator", "superseded"]
    successor_application_id: UuidId | None = None
    workload_intent_ordinal: int | None = Field(
        le=MAX_DATABASE_INTEGER, default=None, ge=1
    )
    pending_operation_ids: list[UuidId] = Field(default_factory=list, max_length=128)
    observation_due_at: datetime | None = None
    observation_deadline_at: datetime | None = None


class FleetProfileApplicationEffect(StrictModel):
    """One exact profile or child effect in the cancellation receipt."""

    effect_id: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    kind: Literal[
        "profile-step", "run", "install", "stop", "cleanup", "agent-operation"
    ]
    label: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    operation_id: UuidId | None = None
    outcome: Literal["succeeded", "failed", "cancelled", "pending", "not-issued"]


class FleetProfileApplicationCancellationView(StrictModel):
    """Live projection of completed, pending, and unissued cancelled effects."""

    request_key: UuidId
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    requested_at: datetime
    state: FleetProfileCancellationState
    cause: Literal["operator", "superseded"]
    completed_effects: list[FleetProfileApplicationEffect] = Field(max_length=1024)
    pending_effects: list[FleetProfileApplicationEffect] = Field(max_length=1024)
    cancelled_effects: list[FleetProfileApplicationEffect] = Field(max_length=1024)
    owner: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    dependency: UuidId | None = None
    deadline_at: datetime | None = None


if TYPE_CHECKING:
    FleetProfileEffectState = Literal[
        "not-issued", "pending", "succeeded", "failed", "cancelled", "unknown"
    ]
else:
    FleetProfileEffectState = Literal[
        tuple(member.value for member in ProfileEffectState)
    ]


class FleetProfileEffectProgress(StrictModel):
    """Observational receipt for one immutable accepted queue effect."""

    effect_id: str
    application_id: UuidId
    plan_digest: Digest
    workload_intent_ordinal: int = Field(ge=1)
    queue_index: int = Field(ge=0)
    kind: FleetProfileSwitchChildKind
    target_id: UuidId
    node_ids: list[NodeId]
    request_key: UuidId
    operation_id: UuidId | None = None
    original_operation_id: UuidId | None = None
    state: FleetProfileEffectState
    result: FleetProfileSwitchChildResult | None = None
    progress: RunSwitchProgress | None = None
    stop_effect: FleetProfileRunEffect | None = None


class FleetProfileApplicationProgress(StrictModel):
    """Typed progress tree persisted with every profile application."""

    effects: list[FleetProfileEffectProgress] = Field(default_factory=list)
    attempt: int = Field(default=1, ge=1)
    retry_due_at: datetime | None = None
    retry_of_application_id: UuidId | None = None
    admission_pending: bool = False
    admission_attempt: int = Field(default=0, ge=0)
    admission_retry_at: datetime | None = None
    #: When the admission began waiting for disk it asked the collector to free;
    #: the wait is bounded, then the load ends with a typed refusal.
    storage_wait_since: datetime | None = None
    intended_profile: FleetProfileIntendedConfiguration | None = None
    workload_intent_ordinal: int | None = Field(
        le=MAX_DATABASE_INTEGER, default=None, ge=1
    )
    operation_kind: FleetProfileOperationKind | None = None
    completed_steps: int = Field(default=0, ge=0, le=1024)
    total_steps: int = Field(default=0, ge=0, le=1024)
    current_label: Annotated[str, StringConstraints(max_length=240)] | None = None
    child_source: Literal["switch-adapter"] | None = None
    child_progress: FleetProfileChildProgress | None = None
    step_results: dict[str, FleetProfileStepResult] = Field(default_factory=dict)
    switch_adapter: FleetProfileSwitchAdapterState | None = None
    cancellation: FleetProfileApplicationCancellationIntent | None = None
    #: The application that took over this one's work (set when it ended
    #: ``superseded`` and a successor exists), with the typed reason.
    superseded_by: UuidId | None = None
    supersede_code: FleetProfileSupersedeCode | None = None
    #: What the application is waiting for, as of its latest check.
    blockers: list[OperationBlocker] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def progress_is_consistent(self) -> FleetProfileApplicationProgress:
        if self.total_steps < self.completed_steps:
            raise ValueError("profile progress completed steps exceed total steps")
        return self


class FleetProfileApplicationResult(StrictModel):
    """Terminal result for one profile application."""

    changed: bool
    completed_steps: int = Field(ge=0, le=1024)


class FleetProfileChildOperation(StrictModel):
    """Stable child operation envelope independent of the Run service module."""

    id: UuidId
    state: FleetProfileOperationState
    progress: FleetProfileChildProgress | None = None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    result: FleetProfileChildResult | None = None
    #: A phase the child keeps retrying; empty while it makes progress.
    stalls: list[OperationBlocker] = Field(default_factory=list, max_length=16)
