"""The current persisted intent for one recipe cache removal request."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from .model_cache_contract import DIGEST_PATTERN, UUID_PATTERN
from .operation_contract import AvailabilityOperationFailure
from .strict_json import StrictJSONModel

RECIPE_CACHE_REMOVE_KIND = "recipe.cache.remove.v2"
RecipeCacheRemovalKind = Literal["recipe.cache.remove.v2"]
RequestKey = Annotated[str, Field(pattern=UUID_PATTERN)]
Selector = Annotated[str, Field(min_length=1, max_length=256)]
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=DIGEST_PATTERN)]


class RecipeCacheRemovalIntent(StrictJSONModel):
    """Exact accepted removal request stored on its existing Job owner."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2]
    kind: RecipeCacheRemovalKind
    action: Literal["remove"]
    selector: Selector
    actor: Annotated[str, Field(min_length=1, max_length=200)]
    request_key: RequestKey
    recipe_revision_id: Identifier
    with_model: bool
    removal_fence: RequestKey


class RecipeCacheRemovalModelChild(StrictJSONModel):
    """One disjoint model-set removal child accepted with the recipe intent."""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_key: RequestKey
    operation_id: RequestKey
    plan_digest: Digest
    selected_sets: list[Digest] = Field(min_length=1)


class RecipeCacheRemovalPlan(StrictJSONModel):
    """Immutable exact targets bound to the current request owner."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2]
    intent: RecipeCacheRemovalIntent
    image_archives: list[Digest]
    model_children: list[RecipeCacheRemovalModelChild]

    @model_validator(mode="after")
    def targets_are_unique(self) -> RecipeCacheRemovalPlan:
        if len(self.image_archives) != len(set(self.image_archives)):
            raise ValueError("recipe image removal targets are duplicated")
        child_ids = [item.operation_id for item in self.model_children]
        child_keys = [item.request_key for item in self.model_children]
        selected = [
            digest for item in self.model_children for digest in item.selected_sets
        ]
        if len(child_ids) != len(set(child_ids)):
            raise ValueError("recipe model removal owners are duplicated")
        if len(child_keys) != len(set(child_keys)):
            raise ValueError("recipe model removal request keys are duplicated")
        if len(selected) != len(set(selected)):
            raise ValueError("recipe model removal sets appear in multiple children")
        if not self.intent.with_model and self.model_children:
            raise ValueError("model removal children require the accepted model choice")
        return self


class RecipeCacheRemovalCheckpoint(StrictJSONModel):
    """Durable resumable progress for a recipe image and model-cache removal."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2]
    image_index: int = Field(ge=0)
    image_pending_bytes: int | None = Field(ge=0)
    image_reclaimed_bytes: int = Field(ge=0)
    model_index: int = Field(ge=0)
    model_reclaimed_bytes: int = Field(ge=0)
    retry_attempts: int = Field(ge=0)
    failure: AvailabilityOperationFailure | None

    @model_validator(mode="after")
    def retry_state_is_consistent(self) -> RecipeCacheRemovalCheckpoint:
        if self.failure is not None:
            if self.failure.retryable and self.failure.retry_time is None:
                raise ValueError("retryable removal failure requires a retry time")
            if not self.failure.retryable and self.failure.retry_time is not None:
                raise ValueError("terminal removal failure cannot have a retry time")
        return self


class RecipeCacheRemovalOwner(StrictJSONModel):
    """One accepted exact plan plus its canonical durable effect checkpoint."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2]
    plan: RecipeCacheRemovalPlan
    checkpoint: RecipeCacheRemovalCheckpoint

    @model_validator(mode="after")
    def checkpoint_matches_plan(self) -> RecipeCacheRemovalOwner:
        if self.checkpoint.image_index > len(self.plan.image_archives):
            raise ValueError("image removal checkpoint exceeds its plan")
        if self.checkpoint.model_index > len(self.plan.model_children):
            raise ValueError("model removal checkpoint exceeds its plan")
        if (
            self.checkpoint.image_pending_bytes is not None
            and self.checkpoint.image_index >= len(self.plan.image_archives)
        ):
            raise ValueError("image removal bytes have no pending archive")
        return self


class RecipeCacheRemovalResult(StrictJSONModel):
    """Stored terminal summary, bound to the Job's immutable intent."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[2]
    action: Literal["remove"]
    selector: Selector
    request_key: RequestKey
    operation_id: RequestKey
    recipe_revision_id: Identifier
    with_model: bool
    state: Literal["succeeded"]
    reclaimed_bytes: int = Field(ge=0)
    model_removals: list[RequestKey]
    preserved: list[str] = Field(max_length=32)
    cancelled_operations: list[str] = Field(max_length=32)
    cancelled_builds: list[str] = Field(max_length=32)
    next_actions: list[str] = Field(max_length=32)


__all__ = [
    "RECIPE_CACHE_REMOVE_KIND",
    "RecipeCacheRemovalCheckpoint",
    "RecipeCacheRemovalIntent",
    "RecipeCacheRemovalKind",
    "RecipeCacheRemovalModelChild",
    "RecipeCacheRemovalOwner",
    "RecipeCacheRemovalPlan",
    "RecipeCacheRemovalResult",
]
