"""Interfaces for digest-bound recipe operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    LifecycleState,
    OperationProgress,
)

from .. import job_states
from ..lifecycle.recipe_operation import RecipeOperationAdapter
from ..models import (
    AgentOperation,
)
from ..recipe_lifecycle_contract import (
    RecipeLifecycleResult,
)
from ..recovery_policy import FailureKind
from ..strict_json import serialize_json_value


class AgentJobQueue(Protocol):
    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: object,
        *,
        operation_id: str,
    ) -> AgentOperation: ...

    def notify_available(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RecipeOperationView:
    id: str
    kind: str
    owner_id: str
    state: str
    plan_digest: str
    nodes: tuple[str, ...]
    lifecycle_result: RecipeLifecycleResult | None
    retry_due_at: datetime | None = None
    status_reason: str | None = None
    progress: OperationProgress | None = None

    @property
    def result(self) -> object:
        """Serialize the canonical result for the public JSON response."""
        return (
            None
            if self.lifecycle_result is None
            else serialize_json_value(self.lifecycle_result)
        )


@dataclass(frozen=True, slots=True)
class IssuedWorkloadReconciliation:
    job_id: str
    kind: str
    owner_id: str
    plan_digest: str
    payload_digests: tuple[str, ...]
    failure_kind: FailureKind
    observe_due_at: datetime
    observation_deadline: datetime


@dataclass(frozen=True, slots=True)
class RecipeRunRankStatus:
    node_id: str
    rank: int
    role: str
    state: str
    observed_at: datetime
    age_seconds: float
    fresh: bool


@dataclass(frozen=True, slots=True)
class RecipeRunRecoveryOwner:
    operation_id: str
    kind: Literal[WireAgentOperation.RECIPE_START, WireAgentOperation.RECIPE_STOP]
    state: str  # a word of the core vocabulary


@dataclass(frozen=True, slots=True)
class RecipeRunStatus:
    id: str
    alias: str
    state: str
    route_state: str
    healthy: bool
    ranks: tuple[RecipeRunRankStatus, ...]
    run_generation: int
    observation_deadline_at: datetime | None
    route_error: str | None
    route_next_attempt_at: datetime | None
    route_recovery_pending: bool
    recovery_owners: tuple[RecipeRunRecoveryOwner, ...]


new_recipe_job = RecipeOperationAdapter.new_job


_TERMINAL_JOB_STATES = frozenset(
    job_states.words(
        LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
    )
)
