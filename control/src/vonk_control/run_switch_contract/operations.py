"""Run switch contract: operations."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)
from vonk_agent_protocol import (
    OperationProgress,
)

from ..integer_domains import MAX_DATABASE_INTEGER
from ..lifecycle_preflight import LifecyclePreflightCheckpoint
from ..operation_blockers import OperationBlocker
from ..run_switch_identity_contract import NodeId, RunSwitchCancellation, UuidId
from ..strict_json import StrictModel
from .phase_results import RunSwitchPhaseResult
from .progress import RunSwitchMemberReceipt, RunSwitchProgress
from .vocabulary import (
    Digest,
    RunSwitchAction,
    RunSwitchOperationKind,
    RunSwitchPhaseKind,
    RunSwitchProgressState,
    RunSwitchSubphase,
)


class RunSwitchRuntimeImageReferenceIntent(StrictModel):
    """Exact image bytes provisionally protected by a current RunSwitch job."""

    schema_version: Literal[2] = 2
    owner_kind: Literal["run-switch-job"]
    operation_id: UuidId
    request_key: UuidId
    actor: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    plan_digest: Digest
    phase_index: int = Field(ge=0, le=31)
    item_index: int = Field(ge=0, le=31)
    workload_intent_ordinal: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    recipe_revision_id: UuidId
    profile_application_id: UuidId | None = None
    execution_keys: list[Digest] = Field(min_length=1, max_length=32)
    image_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    archive_sha256: Digest
    image_bytes: int = Field(strict=True, ge=1, le=16 * 1024**4)
    build_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    build_input_sha256: Digest | None = None

    @model_validator(mode="after")
    def reference_identity_is_consistent(
        self,
    ) -> RunSwitchRuntimeImageReferenceIntent:
        if self.execution_keys != sorted(set(self.execution_keys)):
            raise ValueError("RunSwitch runtime execution keys are not canonical")
        return self


class RunSwitchOperationResult(StrictModel):
    """Exact durable result tree stored in ``Job.result``."""

    phase_index: int = Field(default=0, ge=0, le=31)
    workload_intent_ordinal: int | None = Field(
        le=MAX_DATABASE_INTEGER, default=None, ge=1
    )
    profile_application_id: UuidId | None = None
    item_index: int = Field(default=0, ge=0, le=31)
    phase: RunSwitchPhaseKind | None = None
    subphase: RunSwitchSubphase | None = None
    completed_phases: list[RunSwitchPhaseKind] = Field(
        default_factory=list, max_length=16
    )
    child_operation_id: UuidId | None = None
    runtime_image_reference_intent: RunSwitchRuntimeImageReferenceIntent | None = None
    phase_results: list[RunSwitchPhaseResult] = Field(default_factory=list)
    operation_phase_index: int | None = Field(default=None, ge=0, le=31)
    preflight: LifecyclePreflightCheckpoint | None = None
    cancellation: RunSwitchCancellation | None = None
    operation: OperationProgress | None = None
    completed_bytes: int = Field(default=0, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    total_bytes_known: bool = False
    members: list[RunSwitchMemberReceipt] = Field(default_factory=list, max_length=32)
    retryable: bool = False
    phase_retry_generation: int = Field(default=0, ge=0)
    force_replan: bool = False
    failure_code: (
        Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.:-]{0,95}$")] | None
    ) = None
    retry_attempt: int | None = Field(default=None, ge=2)
    retry_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    observation_due_at: datetime | None = None
    observation_deadline_at: datetime | None = None
    recovery_deadline_at: datetime | None = None
    recovery_child_operation_id: UuidId | None = None
    startup_budget_seconds: int | None = Field(default=None, ge=1)
    start_deadline: datetime | None = None
    failed_phase: RunSwitchPhaseKind | None = None
    final_verify_started_at: float | None = Field(default=None, ge=0)
    final_observation: RunSwitchPhaseResult | None = None
    #: What the operation waits for as of the Controller's latest check.
    blockers: list[OperationBlocker] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def total_bytes_state_is_consistent(self) -> RunSwitchOperationResult:
        if self.total_bytes_known != (self.total_bytes is not None):
            raise ValueError("operation total byte knowledge is inconsistent")
        if self.total_bytes is not None and self.completed_bytes > self.total_bytes:
            raise ValueError("operation completed bytes exceed total bytes")
        return self


class RunSwitchOperation(StrictModel):
    operation_id: UuidId
    kind: RunSwitchOperationKind
    action: RunSwitchAction
    state: RunSwitchProgressState
    plan_digest: Digest
    request_key: UuidId
    cleanup_mode: Literal["uninstall", "reconcile"] | None = None
    installation_id: UuidId | None = None
    node_ids: list[NodeId] = Field(min_length=1, max_length=32)
    current_phase: RunSwitchPhaseKind | None = None
    completed_phases: list[RunSwitchPhaseKind] = Field(max_length=16)
    progress: RunSwitchProgress
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    result: RunSwitchOperationResult | None = None
    #: What a waiting or retrying operation waits for; empty once it settles.
    blockers: list[OperationBlocker] = Field(default_factory=list, max_length=16)
    #: When the Controller checks again; a retry is waiting, never failed.
    next_attempt_at: datetime | None = None

    @model_validator(mode="after")
    def terminal_evidence_is_consistent(self) -> RunSwitchOperation:
        if self.action == "cleanup":
            if self.cleanup_mode is None or self.installation_id is None:
                raise ValueError("cleanup operation requires its reviewed identity")
        elif self.cleanup_mode is not None or self.installation_id is not None:
            raise ValueError("non-cleanup operation cannot carry cleanup identity")
        if self.state == "succeeded":
            if self.result is None or not self.result.completed_phases:
                raise ValueError(
                    "succeeded run-switch requires completed phase evidence"
                )
            if self.status_reason is not None or self.result.failed_phase is not None:
                raise ValueError("succeeded run-switch cannot retain failure evidence")
            if self.result.retryable or self.result.child_operation_id is not None:
                raise ValueError(
                    "succeeded run-switch cannot retain pending recovery or child work"
                )
            if self.result.failure_code is not None:
                raise ValueError("succeeded run-switch cannot retain failure evidence")
        if self.state == "failed" and not (self.status_reason or "").strip():
            raise ValueError("failed run-switch requires a status reason")
        return self


class RunSwitchCancelRequest(StrictModel):
    schema_version: Literal[2] = 2
    request_key: UuidId
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class RunSwitchRetryRequest(StrictModel):
    schema_version: Literal[2] = 2
    request_key: UuidId
