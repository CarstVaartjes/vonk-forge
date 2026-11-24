"""Wire documents of recipe image availability requests and their operator answers."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from vonk_agent_protocol.agent_words import ProgressPhase
from vonk_agent_protocol.integer_domains import MAX_DATABASE_INTEGER
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.wire_model import OperationProgress
from vonk_control.lifecycle.evidence import Residue
from vonk_control.model_cache.contract import Digest
from vonk_control.operations.blockers import OperationBlocker
from vonk_control.operations.contract import (
    AvailabilityOperationFailure,
    AvailabilityRecoveryAction,
)
from vonk_control.recipe_image_availability.availability_contract import (
    RecipeImageAvailabilityArtifact,
    RecipeImageAvailabilityState,
)
from vonk_control.recipe_operations.availability_intent import RecipeAvailabilityIntent
from vonk_control.recipe_operations.lifecycle_contract import (
    RecipeOperationCancellationResult,
)
from vonk_control.strict_json import StrictJSONModel

RecipeImageAvailabilityKind = Literal["recipe.image.availability.v2"]
RecipeImageAvailabilityChildKind = Literal["model-cache", "runtime-image"]
RecipeOperatorState = Literal[
    "accepted",
    LifecycleState.QUEUED,
    LifecycleState.RUNNING,
    LifecycleState.BACKOFF,
    LifecycleState.SUCCEEDED,
    LifecycleState.FAILED,
    LifecycleState.CANCELLED,
]
RecipeOperatorAction = Literal["remove"]


class RecipeImageAvailabilityChild(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    kind: RecipeImageAvailabilityChildKind
    id: str
    request_key: str | None = None
    state: RecipeImageAvailabilityState
    artifact_set_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_content_digests: list[Digest]
    artifacts: list[RecipeImageAvailabilityArtifact] = Field(default_factory=list)
    progress: OperationProgress
    failure: AvailabilityOperationFailure | None = None


class RecipeImageAvailabilityAction(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: AvailabilityRecoveryAction

    @field_validator("key", mode="before")
    @classmethod
    def parse_key(cls, value: object) -> object:
        if isinstance(value, AvailabilityRecoveryAction):
            return value
        try:
            return AvailabilityRecoveryAction(value)
        except (TypeError, ValueError) as error:
            raise ValueError("key contains an invalid action") from error


class RecipeImageAvailabilityResult(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2] = 2
    recipe_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_digest: str | None = None
    model_child_id: str | None = None
    artifact_set_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_content_digests: list[Digest]
    build_input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    image_digest: str = Field(min_length=1, max_length=256)
    local_image_config_id: str | None = None
    oci_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_bytes: int = Field(ge=1)
    build_id: str | None = None


class RecipeImageAvailabilityResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    request: RecipeAvailabilityIntent | None
    kind: RecipeImageAvailabilityKind
    state: RecipeImageAvailabilityState
    attempt: int = Field(le=MAX_DATABASE_INTEGER, ge=0)
    recipe_revision_id: str | None
    recipe_content_sha256: str | None
    progress: OperationProgress | None
    children: list[RecipeImageAvailabilityChild] = Field(default_factory=list)
    result: RecipeImageAvailabilityResult | None = None
    failure: AvailabilityOperationFailure | None = None
    cancellation: RecipeOperationCancellationResult | None = None
    actions: list[RecipeImageAvailabilityAction] = Field(default_factory=list)
    #: What a queued or blocked preparation is waiting for, as of its latest check.
    blockers: list[OperationBlocker] = Field(default_factory=list, max_length=16)
    next_attempt_at: str | None = None
    created_at: str
    updated_at: str
    residue: Residue | None = None

    @model_validator(mode="after")
    def terminal_evidence_is_consistent(self) -> RecipeImageAvailabilityResponse:
        if (
            self.state == "succeeded"
            and self.residue is None
            and (self.result is None or self.failure is not None)
        ):
            raise ValueError(
                "successful image availability requires a result and no failure"
            )
        if self.state == "failed" and self.failure is None and self.residue is None:
            raise ValueError("failed image availability requires failure evidence")
        if self.state != "succeeded" and self.result is not None:
            raise ValueError("image availability result requires success")
        return self


class RecipeOperatorResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: RecipeOperatorAction
    selector: str = Field(min_length=1, max_length=256)
    request_key: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=128)
    recipe_revision_id: str = Field(min_length=1, max_length=128)
    with_model: bool
    state: RecipeOperatorState
    progress: OperationProgress
    reclaimed_bytes: int = Field(ge=0)
    preserved: list[str] = Field(default_factory=list, max_length=32)
    next_actions: list[str] = Field(default_factory=list, max_length=32)
    cancelled_operations: list[str] = Field(default_factory=list, max_length=32)
    cancelled_builds: list[str] = Field(default_factory=list, max_length=32)
    model_removals: list[str] = Field(default_factory=list, max_length=32)
    failure: AvailabilityOperationFailure | None = None

    @model_validator(mode="after")
    def terminal_removal_evidence_is_consistent(self) -> RecipeOperatorResponse:
        if self.state == "succeeded":
            if (
                self.failure is not None
                or self.progress.phase != ProgressPhase.COMPLETED
            ):
                raise ValueError(
                    "successful recipe removal requires completed progress and no failure"
                )
        elif self.progress.phase == ProgressPhase.COMPLETED:
            raise ValueError(
                "unfinished recipe removal cannot report completed progress"
            )
        if self.state == "failed" and self.failure is None:
            raise ValueError("failed recipe removal requires failure evidence")
        return self


# One named type per closed set, shared by the contract field and every
# helper that produces the value, so the vocabulary cannot drift apart.
