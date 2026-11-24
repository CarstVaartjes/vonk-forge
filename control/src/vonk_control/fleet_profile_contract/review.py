"""Fleet profile contract: review."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)

from vonk_agent_protocol.agent_words import ProfileAction
from vonk_agent_protocol.identifiers import Digest
from vonk_agent_protocol.integer_domains import MAX_DATABASE_INTEGER
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.reason_codes.profiles import ProfileReasonCode
from vonk_agent_protocol.reason_codes.runs import SupersedeCode
from vonk_control.fleet_profile_contract.applications import (
    FleetProfileApplicationCancellationView,
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
)
from vonk_control.fleet_profile_contract.assessment import (
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
from vonk_control.fleet_profile_contract.definitions import (
    FleetProfileAssignment,
    FleetProfileDefinition,
)
from vonk_control.fleet_profile_contract.effects import FleetProfileEffects
from vonk_control.fleet_profile_contract.vocabulary import (
    FleetProfileOperationState,
    FleetProfileSupersedeCode,
    Name,
)
from vonk_control.identifiers import UuidId
from vonk_control.operations.blockers import OperationBlocker
from vonk_control.strict_json import StrictModel
from vonk_control.waits.contract import QueueWait


class ProfileLoadAction(StrEnum):
    """What a load of the reviewed plan would do, decided once by the Controller."""

    #: Applies changes and stops no workload.
    LOAD = "load"
    #: Applies changes that stop serving workloads: confirm them first.
    LOAD_STOPPING = "load_stopping"
    #: Everything already matches the saved profile.
    NOTHING_TO_CHANGE = "nothing_to_change"
    #: Cannot be loaded now; the reasons say why.
    BLOCKED = "blocked"


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


def _stops_a_workload(decision: FleetProfileReviewedDecision) -> bool:
    """Whether loading stops a serving workload: a run stop, or an adopted one."""

    return bool(decision.summary.stops) or any(
        application.stops for application in decision.effects.adopted
    )


def _changes_something(decision: FleetProfileReviewedDecision) -> bool:
    """Whether loading does anything beyond keeping what already runs."""

    effects = decision.effects
    summary = decision.summary
    return bool(
        decision.steps
        or decision.preparation_steps
        or effects.gpu_clocks
        or effects.adopted
        or any(item.action != ProfileAction.KEEP for item in effects.installations)
        or any(
            item.action != ProfileAction.KEEP or item.previous_alias is not None
            for item in effects.runs
        )
        or summary.starts
        or summary.installs
        or summary.uninstalls
        or _stops_a_workload(decision)
    )


def profile_load_action(decision: FleetProfileReviewedDecision) -> ProfileLoadAction:
    """The one rule for offering, confirming and describing a load.

    A plan loads when it can be admitted now or once the Controller has prepared
    what it lacks, and it changes something. It needs a confirmation exactly
    when it stops a workload.
    """

    if not (decision.allowed or decision.waits_for_preparation):
        return ProfileLoadAction.BLOCKED
    if not _changes_something(decision):
        return ProfileLoadAction.NOTHING_TO_CHANGE
    return (
        ProfileLoadAction.LOAD_STOPPING
        if _stops_a_workload(decision)
        else ProfileLoadAction.LOAD
    )


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


class FleetProfilePreviewResponse(FleetProfilePreview):
    """What the review endpoint answers: the stored plan and what loading it does."""

    #: The published review document's own version; the stored plan keeps its own.
    schema_version: Literal[3] = 3  # type: ignore[assignment]
    next_action: ProfileLoadAction

    @classmethod
    def of(cls, preview: FleetProfilePreview) -> FleetProfilePreviewResponse:
        # The stored plan is already valid: copy its fields, add the derived answer.
        return cls.model_construct(
            **{
                name: getattr(preview, name)
                for name in FleetProfilePreview.model_fields
                if name != "schema_version"
            },
            schema_version=3,
            next_action=profile_load_action(preview),
        )


class FleetProfileLoadReview(StrictModel):
    effects_digest: Digest


class FleetProfilePreviewRequest(StrictModel):
    """An unsaved definition to review as the profile a save would make."""

    definition: FleetProfileDefinition


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
    wait: QueueWait | None = None
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
            self.state == LifecycleState.FAILED
            and not (self.status_reason or "").strip()
        ):
            raise ValueError("failed application requires a failure reason")
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
