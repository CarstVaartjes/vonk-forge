"""Read-only adoption and recovery projections for availability bookkeeping."""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationInfo,
    field_validator,
)
from vonk_agent_protocol import OperationProgress
from vonk_forge_contracts import RecipeDefinition

from .job_documents import AvailabilityJobPayload
from .model_cache_contract import (
    ModelCacheCancellation,
    ModelCacheDownloadPreviewResponse,
)
from .recipe_availability_intent import RecipeAvailabilityIntent
from .recipe_image_availability_clocks_contract import readable_or_none
from .recipe_lifecycle_contract import RecipeOperationCancellationResult
from .strict_json import read_stored_document


class StoredAvailabilityIdentity(BaseModel):
    """Readable immutable identity, independent of damaged optional bookkeeping."""

    model_config = ConfigDict(extra="ignore", strict=True)
    recipe_revision_id: Annotated[str | None, readable_or_none(TypeAdapter(str))] = None
    recipe_content_sha256: Annotated[
        str | None,
        readable_or_none(TypeAdapter(Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")])),
    ] = None
    recipe: Annotated[
        RecipeDefinition | None, readable_or_none(TypeAdapter(RecipeDefinition))
    ] = None
    request: Annotated[
        RecipeAvailabilityIntent | None,
        readable_or_none(TypeAdapter(RecipeAvailabilityIntent)),
    ] = None
    progress: Annotated[
        OperationProgress | None, readable_or_none(TypeAdapter(OperationProgress))
    ] = None

    cancellation: Annotated[
        RecipeOperationCancellationResult | None,
        readable_or_none(TypeAdapter(RecipeOperationCancellationResult)),
    ] = None


class RecoveredAvailabilityPayload(AvailabilityJobPayload):
    """Adopt readable current fields after recording damage with the column reader.

    Required execution identity remains strict. Optional progress, child and
    receipt observations can be reconstructed by their owners; unreadable copies
    are discarded so the normal worker refreshes them. Claim and cancel clocks
    are observed separately by the lifecycle adapter before any repair.
    """

    @field_validator(
        "claim_until",
        "retry_after_at",
        "image_result",
        "model_child",
        "failure",
        "blockers",
        "build_dependency",
        "cancellation",
        "supersession",
        mode="before",
    )
    @classmethod
    def observation_or_none(cls, value: object, info: ValidationInfo) -> object:
        assert info.field_name is not None
        field = AvailabilityJobPayload.model_fields[info.field_name]
        adapter = TypeAdapter(field.annotation)
        try:
            return read_stored_document(
                lambda document: adapter.validate_json(
                    json.dumps(document, allow_nan=False)
                ),
                value,
            )
        except (TypeError, ValueError):
            return None


class AvailabilityDownloadPreview(ModelCacheDownloadPreviewResponse):
    """The cache decision, without its private in-process execution handles.

    The public owner's fields contain no JSON-only scalars or tuple conversion.
    Python validation discards the private manifest/transfer objects before JSON
    serialization, and retains the canonical strict decision fields.
    """

    model_config = ConfigDict(extra="ignore", strict=True)

    @classmethod
    def read(cls, value: object) -> AvailabilityDownloadPreview | None:
        try:
            return cls.model_validate(value)
        except (TypeError, ValueError):
            return None


class StoredModelChildCancellation(BaseModel):
    """Accepted cancellation remains readable independently of download damage."""

    model_config = ConfigDict(extra="ignore", strict=True)
    cancellation: ModelCacheCancellation | None = None
