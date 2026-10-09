"""Fleet profile contract: switch state."""

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
    ProfileSwitchChildKind,
)

from ..run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchProfileStopScope,
)
from ..strict_json import StrictModel
from .definitions import FleetProfileAssignment
from .effects import FleetProfileChildProgress
from .vocabulary import FleetProfileOperationState, NodeId, UuidId

if TYPE_CHECKING:
    FleetProfileSwitchChildKind = Literal["install", "run", "stop", "cleanup"]
else:
    FleetProfileSwitchChildKind = Literal[
        tuple(member.value for member in ProfileSwitchChildKind)
    ]


class FleetProfileSwitchQueueItem(StrictModel):
    """One durable Run/Switch child in the profile reconciliation queue."""

    kind: FleetProfileSwitchChildKind
    id: UuidId
    profile_stop_scope: RunSwitchProfileStopScope | None = None

    @model_validator(mode="after")
    def partial_stop_is_a_stop_item(self) -> FleetProfileSwitchQueueItem:
        if self.profile_stop_scope is not None and self.kind != "stop":
            raise ValueError("profile Stop scope requires a Stop queue item")
        return self


class FleetProfileSwitchPendingChild(StrictModel):
    """An issued queue child whose exact outcome remains to be observed."""

    queue_index: int = Field(ge=0)
    operation_id: UuidId
    original_operation_id: UuidId | None = None
    kind: FleetProfileSwitchChildKind


class FleetProfileSwitchChildState(StrictModel):
    """Terminal receipt for a child already completed by the adapter."""

    queue_index: int = Field(ge=0)
    operation_id: UuidId
    original_operation_id: UuidId | None = None
    kind: FleetProfileSwitchChildKind
    state: Literal[
        LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
    ]
    result: FleetProfileSwitchChildResult | None = None


class FleetProfileAssignmentFailure(StrictModel):
    queue_index: int | None = Field(default=None, ge=0)
    assignment_id: UuidId | None = None
    operation_id: UuidId | None = None
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    terminal: bool = False


class FleetProfileSwitchAdapterResult(StrictModel):
    """Complete result of reconciling every assignment in a profile scope."""

    children: list[FleetProfileSwitchChildState] = Field(max_length=128)
    assignment_ids: list[UuidId] = Field(max_length=64)

    @model_validator(mode="after")
    def assignment_ids_are_unique(self) -> FleetProfileSwitchAdapterResult:
        if len(self.assignment_ids) != len(set(self.assignment_ids)):
            raise ValueError("profile switch assignment IDs must be unique")
        return self


class FleetProfileSwitchAdapterState(StrictModel):
    """Persisted state used to resume a profile switch after restart."""

    schema_version: Literal[2] = 2
    child_id: UuidId
    scope_node_ids: list[NodeId] = Field(max_length=32)
    assignment_ids: list[UuidId] = Field(max_length=64)
    assignments: list[FleetProfileAssignment] = Field(max_length=64)
    queue: list[FleetProfileSwitchQueueItem] = Field(max_length=128)
    position: int = Field(default=0, ge=0, le=128)
    pending_children: list[FleetProfileSwitchPendingChild] = Field(default_factory=list)
    skipped_indices: list[int] = Field(default_factory=list)
    children: list[FleetProfileSwitchChildState] = Field(
        default_factory=list, max_length=128
    )
    assignment_failures: list[FleetProfileAssignmentFailure] = Field(
        default_factory=list, max_length=128
    )
    actor: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    request_id: UuidId
    state: FleetProfileOperationState = LifecycleState.QUEUED
    child_progress: FleetProfileChildProgress | None = None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    observation_due_at: datetime | None = None
    observation_deadline_at: datetime | None = None
    pending_operation_ids: list[UuidId] = Field(default_factory=list, max_length=128)
    stop_reissue_attempt: int = Field(default=0, ge=0, le=32)
    result: FleetProfileSwitchAdapterResult | None = None

    @model_validator(mode="after")
    def identities_are_consistent(self) -> FleetProfileSwitchAdapterState:
        if self.scope_node_ids != sorted(set(self.scope_node_ids)):
            raise ValueError("profile switch scope node IDs must be sorted and unique")
        if self.assignment_ids != [assignment.id for assignment in self.assignments]:
            raise ValueError("profile switch assignments must match assignment IDs")
        indices = [
            child.queue_index for child in (*self.pending_children, *self.children)
        ]
        indices.extend(self.skipped_indices)
        if len(indices) != len(set(indices)):
            raise ValueError("queue child identities must be unique")
        operation_ids = [
            child.operation_id for child in (*self.pending_children, *self.children)
        ]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("one operation cannot own multiple queue effects")
        if any(
            child.result is not None
            and child.result.run_switch_operation_id != child.operation_id
            for child in self.children
        ):
            raise ValueError("closed queue receipt differs from its operation identity")
        if self.position > len(self.queue) or any(
            index < 0 or index >= len(self.queue) for index in indices
        ):
            raise ValueError("queue child index is outside the immutable queue")
        if any(
            failure.queue_index is not None and failure.queue_index >= len(self.queue)
            for failure in self.assignment_failures
        ):
            raise ValueError("failure index is outside the immutable queue")
        for child in (*self.pending_children, *self.children):
            if child.kind != self.queue[child.queue_index].kind:
                raise ValueError("queue child kind differs from its reviewed item")
        return self


class FleetProfileSwitchChildResult(StrictModel):
    """Profile child receipt containing the public Run/Switch result tree."""

    run_switch_operation_id: UuidId
    run_switch: RunSwitchOperationResult


# The terminal child receipt is declared above its own type so a completed
# child can carry it; resolve that forward reference once both exist.
FleetProfileSwitchChildState.model_rebuild()


class FleetProfileVerificationResult(StrictModel):
    """Small result used by profile switch adapters that verify directly."""

    verified: bool


FleetProfileChildResult = (
    FleetProfileSwitchChildResult
    | FleetProfileSwitchAdapterResult
    | FleetProfileVerificationResult
)


class FleetProfileStepResult(StrictModel):
    """Result receipt for one completed profile plan step."""

    operation_id: UuidId
    kind: Literal["switch"]
    result: FleetProfileChildResult | None = None
