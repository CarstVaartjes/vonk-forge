"""Fleet profile contract: review."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)
from vonk_agent_protocol import (
    LifecycleState,
    ProfileReasonCode,
    SupersedeCode,
)

from ..integer_domains import MAX_DATABASE_INTEGER
from ..operation_blockers import OperationBlocker
from ..strict_json import StrictModel
from .applications import (
    FleetProfileApplicationCancellationView,
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
)
from .assessment import (
    FleetProfileAdmissionDecision,
    FleetProfileAssignmentAssessment,
    FleetProfileAssignmentPreparation,
    FleetProfileAssignmentPreview,
    FleetProfilePlanStep,
    FleetProfilePlanSummary,
    FleetProfilePreparationDecision,
    FleetProfileReason,
    FleetProfileScopePreview,
)
from .definitions import FleetProfileAssignment, FleetProfileDefinition
from .effects import FleetProfileEffects
from .vocabulary import (
    Digest,
    FleetProfileOperationState,
    FleetProfileSupersedeCode,
    Name,
    UuidId,
)


class FleetProfileReviewedDecision(StrictModel):
    """The reviewable semantic decision that owns the reconciliation digest."""

    schema_version: Literal[2] = 2
    profile_id: UuidId
    profile_name: Name
    profile_digest: Digest
    profile_revision: int | None = Field(le=MAX_DATABASE_INTEGER, ge=1)
    profile_definition: FleetProfileDefinition | None
    allowed: bool
    scope: FleetProfileScopePreview
    summary: FleetProfilePlanSummary
    assignments: list[FleetProfileAssignmentPreview] = Field(max_length=64)
    resolved_assignments: list[FleetProfileAssignment] = Field(max_length=64)
    admission_decisions: list[FleetProfileAdmissionDecision] = Field(max_length=64)
    preparation_decisions: list[FleetProfilePreparationDecision] = Field(max_length=64)
    effects: FleetProfileEffects
    steps: list[FleetProfilePlanStep] = Field(max_length=1024)
    #: What the Controller prepares by itself, in order, when this load is
    #: accepted: the model download and the runtime image build.
    preparation_steps: list[FleetProfilePlanStep] = Field(
        default_factory=list, max_length=128
    )
    #: True when preparation is all that stands between this plan and admission.
    waits_for_preparation: bool = False
    reasons: list[FleetProfileReason] = Field(max_length=128)


class FleetProfilePreview(FleetProfileReviewedDecision):
    generated_at: datetime
    assessments: list[FleetProfileAssignmentAssessment] = Field(max_length=64)
    preparations: list[FleetProfileAssignmentPreparation] = Field(
        default_factory=list, max_length=64
    )
    plan_digest: Digest
    #: Every current preview binds the displayed effects, excluding readiness
    #: counters and timestamps. Admission compares the freshly planned effects.
    effects_digest: Digest

    def reviewed_decision(self) -> FleetProfileReviewedDecision:
        return FleetProfileReviewedDecision.model_validate(
            {
                name: getattr(self, name)
                for name in FleetProfileReviewedDecision.model_fields
            }
        )


class FleetProfileLoadReview(StrictModel):
    effects_digest: Digest


class FleetProfileLoadRequest(StrictModel):
    request_key: UuidId
    #: Omission authorizes the current plan; a review always requires a binding.
    review: FleetProfileLoadReview | None = None


class FleetProfileApplicationCancelRequest(StrictModel):
    profile_number: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    request_key: UuidId


class FleetProfileApplicationProjectionIssue(StrictModel):
    """Historical state is retained while its metadata cannot be verified."""

    code: Literal[ProfileReasonCode.APPLICATION_INTENT_INVALID]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    observation: Literal["unknown"] = "unknown"


class FleetProfileApplicationView(StrictModel):
    id: UuidId
    request_key: UuidId
    profile_id: UuidId
    profile_digest: Digest
    plan_digest: Digest
    attempt: int = Field(default=1, ge=1)
    retry_of_application_id: UuidId | None = None
    #: The application that continues this one's work once it ended ``superseded``;
    #: follow the chain to the live application.
    superseded_by: UuidId | None = None
    #: Typed reason of a ``superseded`` end.
    reason_code: FleetProfileSupersedeCode | None = None
    state: FleetProfileOperationState
    current_step: int = Field(ge=0, le=1024)
    total_steps: int = Field(ge=0, le=1024)
    current_operation_id: UuidId | None
    status_reason: Annotated[str, StringConstraints(max_length=512)] | None
    progress: FleetProfileApplicationProgress
    cancellation: FleetProfileApplicationCancellationView | None = None
    result: FleetProfileApplicationResult | None
    projection_issue: FleetProfileApplicationProjectionIssue | None = None
    #: What a queued or failed application is waiting for; empty once it runs.
    blockers: list[OperationBlocker] = Field(default_factory=list, max_length=16)
    #: When the Controller will check again; an application that will retry is
    #: reported as ``queued`` with this time, never as ``failed``.
    next_attempt_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def application_state_is_consistent(self) -> FleetProfileApplicationView:
        if self.current_step > self.total_steps:
            raise ValueError("application step exceeds total steps")
        if (
            self.attempt != self.progress.attempt
            or self.retry_of_application_id != self.progress.retry_of_application_id
        ):
            raise ValueError(
                "application recovery identity disagrees with persisted progress"
            )
        if self.state == "succeeded" and self.projection_issue is None:
            if self.result is None or self.status_reason is not None:
                raise ValueError(
                    "successful application requires a result and no failure reason"
                )
            if (
                self.current_step != self.total_steps
                or self.result.completed_steps != self.total_steps
            ):
                raise ValueError(
                    "successful application must complete every planned step"
                )
        if (
            self.state in {LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR}
            and not (self.status_reason or "").strip()
        ):
            raise ValueError("failed or waiting application requires a failure reason")
        if self.state == "superseded":
            if self.reason_code is None and self.projection_issue is None:
                raise ValueError("superseded application requires a reason code")
            if (
                self.reason_code == SupersedeCode.SUPERSEDED_BY_RETRY
                and self.superseded_by is None
            ):
                raise ValueError("a retry supersession names its successor")
        elif self.superseded_by is not None or self.reason_code is not None:
            raise ValueError("only a superseded application names a successor")
        return self
