"""One current contract for durable recipe-update intent and observation."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator
from vonk_agent_protocol import OperationProgress

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from .recipe_lifecycle_contract import RecipeOperationCancellationResult
from .strict_json import StrictJSONModel

UPDATE_KIND = "recipe.cache.update.v2"
Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RequestKey = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
Selector = Annotated[str, Field(min_length=1, max_length=256)]
UpdateState = Literal[
    "queued", "running", "cancelling", "succeeded", "partial", "failed", "cancelled"
]


class RecipeUpdateScope(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    all: bool = False
    selectors: list[Selector] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit_scope(self) -> RecipeUpdateScope:
        if self.all == bool(self.selectors):
            raise ValueError("specify selectors or all, but not both")
        if any(not selector.strip() for selector in self.selectors):
            raise ValueError("recipe selectors must not be blank")
        return self


class RecipeUpdateRequest(RecipeUpdateScope):
    schema_version: Literal[2] = 2
    request_key: RequestKey


class RecipeUpdateFailure(StrictJSONModel):
    """Bounded admission failure or observation of the referenced child's failure."""

    code: str = Field(pattern=r"^[a-z][a-z0-9_.:-]{0,95}$")
    detail: str = Field(max_length=512)
    retryable: bool


class RecipeUpdateIdentity(StrictJSONModel):
    recipe_revision_id: Identifier
    recipe_content_sha256: Digest
    effective_execution_key: Digest
    recipe_name: str = Field(min_length=1, max_length=512)
    request_key: RequestKey


class RecipeUpdateBinding(StrictJSONModel):
    request: RecipeUpdateScope
    children: list[RecipeUpdateIdentity]


class RecipeUpdateChild(RecipeUpdateIdentity):
    """Frozen identity plus a rebuildable observation; child jobs own execution."""

    operation_id: Identifier | None = None
    state: Literal[
        "pending",
        "queued",
        "running",
        "cancelling",
        "succeeded",
        "partial",
        "failed",
        "cancelled",
    ] = "pending"
    failure: RecipeUpdateFailure | None = None
    observed_at: AwareDatetime | None = None
    retry_at: AwareDatetime | None = None

    def identity(self) -> RecipeUpdateIdentity:
        return RecipeUpdateIdentity.model_validate(
            {name: getattr(self, name) for name in RecipeUpdateIdentity.model_fields}
        )


class RecipeUpdateDocument(StrictJSONModel):
    schema_version: Literal[2] = 2
    kind: Literal["recipe.cache.update.v2"] = UPDATE_KIND
    request: RecipeUpdateScope
    children: list[RecipeUpdateChild]
    cancellation: RecipeOperationCancellationResult | None = None
    next_child: int = Field(default=0, ge=0)
    claim_owner: str | None = Field(default=None, max_length=128)
    claim_until: AwareDatetime | None = None
    next_attempt_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def bounded_acyclic_scope(self) -> RecipeUpdateDocument:
        if self.children and self.next_child >= len(self.children):
            raise ValueError("update cursor is outside its frozen scope")
        if not self.children and self.next_child != 0:
            raise ValueError("empty update has no child cursor")
        for field in ("recipe_revision_id", "request_key"):
            values = [getattr(child, field) for child in self.children]
            if len(values) != len(set(values)):
                raise ValueError("update scope contains duplicate child identities")
        operations = [
            child.operation_id
            for child in self.children
            if child.operation_id is not None
        ]
        if len(operations) != len(set(operations)):
            raise ValueError("update scope contains duplicate child operations")
        if (self.claim_owner is None) != (self.claim_until is None):
            raise ValueError("update claim requires both an owner and expiry")
        return self


class RecipeUpdateResponse(StrictJSONModel):
    schema_version: Literal[2] = 2
    kind: Literal["recipe.cache.update.v2"] = UPDATE_KIND
    action: Literal["update"] = "update"
    id: Identifier
    request_id: RequestKey
    request: RecipeUpdateScope
    state: UpdateState
    attempt: int = Field(ge=0)
    children: list[RecipeUpdateChild]
    cancellation: RecipeOperationCancellationResult | None = None
    progress: OperationProgress
    waiting_on: str | None = Field(default=None, max_length=256)
    wait_owner: Literal["recipe-image-availability"] | None = None
    next_attempt_at: datetime | None = None
    resume_condition: str | None = Field(default=None, max_length=256)
    created_at: datetime
    updated_at: datetime


def read_update_document(value: object) -> RecipeUpdateDocument:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    if len(encoded) > MAX_CONTROL_DOCUMENT_BYTES:
        raise ValueError(
            f"update document has {len(encoded)} bytes; limit is {MAX_CONTROL_DOCUMENT_BYTES} bytes"
        )
    return RecipeUpdateDocument.model_validate_json(encoded)
