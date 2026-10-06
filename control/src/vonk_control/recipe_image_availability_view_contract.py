"""Typed read projections of recipe-image preparation and removal."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field
from vonk_agent_protocol import OperationProgress

from .job_documents import AvailabilityJobResult, AvailabilityModelChild
from .operation_blockers import OperationBlocker
from .operation_contract import AvailabilityOperationFailure
from .recipe_availability_intent import RecipeAvailabilityIntent
from .recipe_lifecycle_contract import RecipeOperationCancellationResult
from .stored_json import Residue
from .strict_json import StrictJSONModel


class RecipeImageAvailabilityView(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    request_id: str
    request: RecipeAvailabilityIntent | None
    kind: str
    state: str
    attempt: int
    recipe_revision_id: str | None
    recipe_content_sha256: str | None
    model_digest: str | None
    build_input_sha256: str | None
    measurement: OperationProgress = Field(alias="progress")
    image_measurement: OperationProgress | None = Field(alias="image_progress")
    artifact: AvailabilityJobResult | None = Field(alias="result")
    failure_evidence: AvailabilityOperationFailure | None = Field(alias="failure")
    supported_actions: tuple[str, ...]
    created_at: str
    updated_at: str
    model_preparation: AvailabilityModelChild | None = Field(
        alias="model_child", default=None
    )
    image_state: str | None = None
    image_failure_evidence: AvailabilityOperationFailure | None = Field(
        alias="image_failure", default=None
    )
    cancellation: RecipeOperationCancellationResult | None = None
    blockers: tuple[OperationBlocker, ...] = ()
    next_attempt_at: str | None = None
    residue: Residue | None = None

    def document(self):
        """The existing public JSON projection, derived from typed owner fields."""
        document = self.model_dump(
            mode="json",
            by_alias=True,
            exclude={
                "model_preparation",
                "image_state",
                "image_failure_evidence",
                "residue",
            },
        )
        return document | {
            "schema_version": 2,
            "children": []
            if self.model_preparation is None
            else [self.model_preparation.model_dump(mode="json")],
        }

    @property
    def progress(self):
        return self.measurement.model_dump(mode="json", exclude_none=True)

    @property
    def image_progress(self):
        return (
            self.image_measurement.model_dump(mode="json", exclude_none=True)
            if self.image_measurement
            else None
        )

    @property
    def result(self):
        return (
            self.artifact.model_dump(mode="json", exclude_none=True)
            if self.artifact
            else None
        )

    @property
    def failure(self):
        return (
            self.failure_evidence.model_dump(mode="json", exclude_none=True)
            if self.failure_evidence
            else None
        )

    @property
    def model_child(self):
        return (
            self.model_preparation.model_dump(mode="json", exclude_none=True)
            if self.model_preparation
            else None
        )

    @property
    def image_failure(self):
        return (
            self.image_failure_evidence.model_dump(mode="json", exclude_none=True)
            if self.image_failure_evidence
            else None
        )


class RecipeCacheRemovalStatus(StrictJSONModel):
    """Removal progress derived from its accepted plan and checkpoint."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[2]
    action: Literal["remove"]
    selector: str
    request_key: str
    operation_id: str
    recipe_revision_id: str
    review_digest: str
    with_model: bool
    state: str
    progress: OperationProgress
    reclaimed_bytes: int = Field(ge=0)
    preserved: list[str]
    failure: AvailabilityOperationFailure | None = None
    next_actions: list[str]
    cancelled_operations: list[str]
    cancelled_builds: list[str]
    model_removals: list[str]
