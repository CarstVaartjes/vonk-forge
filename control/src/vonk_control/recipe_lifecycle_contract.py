"""Typed response contracts for recipe lifecycle operations.

The durable ``Job.result`` column is a JSON document, but the lifecycle API
has a small, known set of result shapes.  Keep the persisted projection as a
mapping for the orchestration code while validating the HTTP boundary against
these concrete models.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator
from vonk_agent_protocol import (
    AgentFailureResult,
    AgentInstallResult,
    RecipeBuildEvidence,
    RecipeImageImportEvidence,
    RecipeJobRunResult,
    RecipeModelCleanupResult,
    RecipeStartCollectiveReadinessEvidence,
    RecipeStartRankLaunchEvidence,
    RecipeStartSingleEvidence,
    RecipeStopResult,
    RecipeUninstallResult,
)

from .library_contract import Digest, ImageDigest, NodeId, UuidId
from .strict_json import StrictJSONModel


class LifecycleModel(StrictJSONModel):
    """Strict response model shared by recipe lifecycle result projections."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class RemovedRecipeNodeResult(LifecycleModel):
    """Result emitted by the Controller's logical uninstall projection."""

    removed: Literal[True]


class LifecycleCodeFailureResult(LifecycleModel):
    """Bounded failure marker used by the Controller's node projector."""

    code: str = Field(min_length=1, max_length=128)
    detail: str | None = Field(default=None, min_length=1, max_length=512)


class TensorParallelStartEvidence(LifecycleModel):
    """Endpoint evidence emitted by the phase-free tensor-parallel path.

    Tensor-parallel recipes use the current exact run inspection fields while
    keeping the single start phase.  The protocol's phased start variants do
    not model this flat projection, so this Controller projection keeps the
    actual producer shape typed without weakening any required field.
    """

    recipe_revision_id: UuidId
    recipe_content_sha256: Digest
    image_digest: ImageDigest
    artifact_set_digest: Digest
    model_identity: str | None = Field(default=None, max_length=1024)
    rank: int = Field(ge=0)
    world_size: int = Field(ge=1)
    memory_reservation_bytes: int = Field(ge=1)
    evidence_digest: Digest
    endpoint: str
    ready: Literal[True]
    run_generation: int = Field(ge=1)
    runtime_arguments_sha256: Digest
    local_address: str
    master_address: str
    master_port: int = Field(ge=1024, le=65535)


LifecycleNodeResult = Annotated[
    AgentInstallResult
    | RecipeBuildEvidence
    | RecipeImageImportEvidence
    | RecipeStartSingleEvidence
    | RecipeStartRankLaunchEvidence
    | RecipeStartCollectiveReadinessEvidence
    | TensorParallelStartEvidence
    | RecipeStopResult
    | RecipeUninstallResult
    | RemovedRecipeNodeResult
    | RecipeModelCleanupResult
    | AgentFailureResult
    | LifecycleCodeFailureResult,
    Field(discriminator=None),
]


class RecipeOperationProgressResult(LifecycleModel):
    """Partial evidence retained while a multi-node operation is running."""

    node_evidence: dict[NodeId, LifecycleNodeResult] | None = None
    launch_evidence: dict[NodeId, LifecycleNodeResult] | None = None

    @model_validator(mode="after")
    def has_evidence(self) -> RecipeOperationProgressResult:
        if self.node_evidence is None and self.launch_evidence is None:
            raise ValueError("recipe operation progress has no evidence")
        return self


class RecipeOperationResult(LifecycleModel):
    """Terminal aggregate emitted by the durable lifecycle job projector."""

    successful_nodes: list[NodeId] = Field(max_length=1024)
    failed_nodes: list[NodeId] = Field(max_length=1024)
    node_evidence: dict[NodeId, LifecycleNodeResult]
    launch_evidence: dict[NodeId, LifecycleNodeResult] | None = None
    recovery_error: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def nodes_are_disjoint(self) -> RecipeOperationResult:
        if set(self.successful_nodes) & set(self.failed_nodes):
            raise ValueError("successful and failed nodes overlap")
        return self


class RecipeOperationCancellationResult(LifecycleModel):
    """Cancellation metadata merged into a pending lifecycle result."""

    cancel_requested: Literal[True]
    cancel_request_id: UuidId
    cancel_actor: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=512)
    cancelled: Literal[True] | None = None
    recovery: Literal["retry creates a new operation"] | None = None
    node_evidence: dict[NodeId, LifecycleNodeResult] | None = None
    launch_evidence: dict[NodeId, LifecycleNodeResult] | None = None


class RecipeOperationActivatedResult(LifecycleModel):
    activated: Literal[True]


class RecipeOperationStoppedResult(LifecycleModel):
    stopped: Literal[True]


RecipeLifecycleResult = (
    RecipeOperationResult
    | RecipeOperationProgressResult
    | RecipeOperationCancellationResult
    | RecipeOperationActivatedResult
    | RecipeOperationStoppedResult
    | RecipeJobRunResult
)


def parse_recipe_lifecycle_result(kind: str, value: object) -> object:
    """Validate one persisted lifecycle result against its operation kind."""

    if kind == "recipe.job.activate.v1":
        models = (RecipeOperationActivatedResult,)
    elif kind == "recipe.stop":
        models = (
            RecipeOperationStoppedResult,
            RecipeOperationResult,
            RecipeOperationProgressResult,
            RecipeOperationCancellationResult,
        )
    elif kind == "recipe.job.run.v1":
        models = (RecipeJobRunResult, RecipeOperationCancellationResult)
    elif kind.startswith("recipe."):
        models = (
            RecipeOperationResult,
            RecipeOperationProgressResult,
            RecipeOperationCancellationResult,
        )
    else:
        raise ValueError("recipe operation kind has no result contract")
    _validate_evidence_for_kind(kind, value)
    for model in models:
        try:
            return model.model_validate(value)
        except (TypeError, ValueError):
            continue
    raise ValueError("recipe operation result does not match its kind")


def _validate_evidence_for_kind(kind: str, value: object) -> None:
    """Reject evidence belonging to a different lifecycle operation."""

    if not isinstance(value, Mapping):
        return
    if kind in {"recipe.build.v1"}:
        evidence_models = (RecipeBuildEvidence,)
    elif kind in {"recipe.image.import.v1", "recipe.image.distribute"}:
        evidence_models = (RecipeImageImportEvidence,)
    elif kind == "recipe.install":
        evidence_models = (AgentInstallResult,)
    elif kind == "recipe.start":
        evidence_models = (
            RecipeStartSingleEvidence,
            RecipeStartCollectiveReadinessEvidence,
            RecipeStartRankLaunchEvidence,
            TensorParallelStartEvidence,
        )
    elif kind == "recipe.stop":
        evidence_models = (RecipeStopResult,)
    elif kind == "recipe.uninstall":
        evidence_models = (RecipeUninstallResult, RemovedRecipeNodeResult)
    elif kind == "recipe.model-uninstall.v1":
        evidence_models = (RecipeModelCleanupResult,)
    else:
        return
    evidence_models = (*evidence_models, AgentFailureResult, LifecycleCodeFailureResult)
    for field_name in ("node_evidence", "launch_evidence"):
        evidence = value.get(field_name)
        if evidence is None:
            continue
        if not isinstance(evidence, Mapping):
            raise TypeError("recipe operation evidence is invalid")
        for item in evidence.values():
            if not isinstance(item, Mapping):
                raise TypeError("recipe operation evidence is invalid")
            if not any(_model_accepts(model, item) for model in evidence_models):
                raise ValueError("recipe operation evidence kind is invalid")


def _model_accepts(model: type[LifecycleModel], value: Mapping[str, object]) -> bool:
    try:
        model.model_validate(value)
    except (TypeError, ValueError):
        return False
    return True


class RecipeOperationConflictResponse(LifecycleModel):
    code: Literal["recipe.operation_conflict"]
    detail: str = Field(min_length=1, max_length=256)
    request_id: UuidId


__all__ = [
    "LifecycleCodeFailureResult",
    "LifecycleNodeResult",
    "RecipeLifecycleResult",
    "RecipeOperationActivatedResult",
    "RecipeOperationCancellationResult",
    "RecipeOperationConflictResponse",
    "RecipeOperationProgressResult",
    "RecipeOperationResult",
    "RecipeOperationStoppedResult",
    "RemovedRecipeNodeResult",
    "TensorParallelStartEvidence",
    "parse_recipe_lifecycle_result",
]
