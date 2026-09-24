"""Schema-2 contracts for the Controller-owned NAS model cache."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from vonk_agent_protocol import canonical_message
from vonk_forge_contracts.model import ModelReference

from .operation_contract import AvailabilityOperationFailure, OperationProgress
from .strict_json import StrictJSONModel

DIGEST_PATTERN = r"^[0-9a-f]{64}$"
ARTIFACT_KEY_PATTERN = r"^[a-z][a-z0-9_.:-]{0,255}$"
REVISION_PATTERN = r"^[0-9a-f]{40,64}$"
UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

Digest = Annotated[str, Field(pattern=DIGEST_PATTERN)]

# One named type per closed set. The contract field and every helper that
# produces or consumes the value share the alias, so the vocabulary cannot
# drift apart.
ModelCacheOperationKind = Literal["download", "repair", "remove"]
ModelCacheOperationState = Literal[
    "queued", "running", "partial", "succeeded", "failed", "cancelled"
]
ModelCacheOperatorState = Literal[
    "accepted",
    "queued",
    "running",
    "partial",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
]
ModelCacheOperatorAction = Literal["download", "remove"]
ModelCacheOperationPhase = Literal[
    "queued",
    "downloading",
    "verifying",
    "reclaiming",
    "cancelling",
    "completed",
    "failed",
]
ModelCacheEntryState = Literal[
    "incomplete", "downloading", "verifying", "cached", "needs-repair", "failed"
]
ModelCacheCoverage = Literal["complete", "incomplete"]


class StrictModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class CacheManifestArtifact(StrictModel):
    key: str
    id: str
    path: str
    kind: str
    repository: str | None
    source: str
    revision: str | None
    sha256: str
    download_bytes: int
    roles: list[str]
    model_content_sha256: str | None


class CacheManifest(StrictModel):
    schema_version: Literal[2]
    source_policy: Literal["nas-first"]
    model_content_sha256: str | None
    recipe_revision_sha256: str | None
    model_definition_ref: ModelReference | None
    model_content_digests: list[str]
    artifacts: list[CacheManifestArtifact]


class ModelCacheObjectReceipt(StrictModel):
    """Managed-storage ownership record for one verified cache object.

    The bytes and this receipt live together under the trusted cache root, and
    together they are the whole of an object's availability. SQL owns set
    membership and the exact artifact-set identity that admission binds; it
    holds no per-object availability flag.
    """

    schema_version: Literal[2]
    sha256: Digest
    storage_key: str = Field(min_length=1, max_length=255)
    expected_bytes: int = Field(ge=0)
    actual_bytes: int = Field(ge=0)
    verified_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _sizes_agree(self) -> ModelCacheObjectReceipt:
        if self.actual_bytes != self.expected_bytes:
            raise ValueError("a verified object receipt has no partial length")
        return self


class ModelCacheRepairCheckpoint(StrictModel):
    transfer_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    completed_objects: list[Digest]


class ModelCacheTransferArtifact(StrictModel):
    baseline_bytes: int = Field(ge=0)
    received_bytes: int = Field(ge=0)
    started_at: str


class ModelCacheTransfer(StrictModel):
    schema_version: Literal[2] = 2
    total_bytes: int = Field(ge=0)
    artifacts: dict[Digest, ModelCacheTransferArtifact]


class ModelCacheRetry(StrictModel):
    automatic_attempts: int = Field(ge=1)
    operator_retries: int = Field(ge=0)
    next_retry_at: str | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0)


class ModelCacheClaim(StrictModel):
    owner: str = Field(min_length=1)
    expires_at: str


class ModelCacheAccessRecheck(StrictModel):
    request_key: str = Field(pattern=UUID_PATTERN)
    checked_at: str
    authorized: bool


class ModelCacheDownloadResult(StrictModel):
    schema_version: Literal[2]
    artifact_set_sha256: Digest
    coverage: Literal["complete"]


class ModelCacheRemovalResult(StrictModel):
    schema_version: Literal[2]
    removed_entries: list[Digest]
    reclaimed_bytes: int = Field(ge=0)
    cancelled_operations: list[str] = Field(default_factory=list, max_length=32)


class ModelCacheCancellation(StrictModel):
    """Durable record of the one accepted cancellation request."""

    request_key: str = Field(pattern=UUID_PATTERN)
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=512)
    requested_at: str = Field(min_length=1, max_length=64)


class _ModelCacheOperationPayload(StrictModel):
    schema_version: Literal[2]
    source_policy: Literal["nas-first"]
    claim: ModelCacheClaim | None = None
    access_recheck: ModelCacheAccessRecheck | None = None
    failure: AvailabilityOperationFailure | None = None
    cancellation: ModelCacheCancellation | None = None
    retry_of: str | None = Field(default=None, pattern=UUID_PATTERN)
    resume_of: str | None = Field(default=None, pattern=UUID_PATTERN)
    operator_action: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_.:-]{0,63}$"
    )
    with_model: bool | None = None
    force_refresh: bool = False


class ModelCacheDownloadPayload(_ModelCacheOperationPayload):
    removal_fence: str | None = Field(default=None, pattern=UUID_PATTERN)
    selector: str | None = Field(default=None, min_length=1, max_length=256)
    artifact_set_sha256: Digest
    manifest: CacheManifest
    plan_digest: Digest
    transfer: ModelCacheTransfer
    retry: ModelCacheRetry
    result: ModelCacheDownloadResult | None = None


class ModelCacheRepairPayload(ModelCacheDownloadPayload):
    repair_checkpoint: ModelCacheRepairCheckpoint


class ModelCacheRemovalPayload(_ModelCacheOperationPayload):
    """Exact, restartable removal plan and its durable effect checkpoint."""

    selector: str = Field(..., min_length=1, max_length=256)
    model_content_sha256: Digest | None = None
    removal_fence: str = Field(..., pattern=UUID_PATTERN)
    selected: list[Digest]
    selected_objects: list[Digest]
    delete_objects: list[Digest]
    object_index: int = Field(ge=0)
    object_pending_bytes: int | None = Field(ge=0)
    reclaimed_bytes: int = Field(ge=0)
    set_index: int = Field(ge=0)
    retry: ModelCacheRetry
    result: ModelCacheRemovalResult | None

    @model_validator(mode="after")
    def removal_checkpoint_is_consistent(self) -> ModelCacheRemovalPayload:
        if (
            self.retry.next_retry_at is not None
            and datetime.fromisoformat(self.retry.next_retry_at).tzinfo is None
        ):
            raise ValueError("model removal retry timestamp must include a timezone")
        if self.operator_action != "remove-model":
            raise ValueError("model removal action is missing")
        if self.object_index > len(self.delete_objects):
            raise ValueError("model removal object checkpoint exceeds its plan")
        if self.set_index > len(self.selected):
            raise ValueError("model removal set checkpoint exceeds its plan")
        if self.object_pending_bytes is not None and self.object_index >= len(
            self.delete_objects
        ):
            raise ValueError("model removal has a byte checkpoint without an object")
        if len(self.selected) != len(set(self.selected)):
            raise ValueError("model removal set identities are duplicated")
        if len(self.selected_objects) != len(set(self.selected_objects)):
            raise ValueError("model removal object identities are duplicated")
        if len(self.delete_objects) != len(set(self.delete_objects)):
            raise ValueError("model removal delete identities are duplicated")
        if not set(self.delete_objects).issubset(self.selected_objects):
            raise ValueError("model removal deletes an unselected object")
        if self.result is not None and (
            self.object_index != len(self.delete_objects)
            or self.set_index != len(self.selected)
        ):
            raise ValueError("model removal result precedes its effects")
        return self


ModelCacheOperationPayload = (
    ModelCacheDownloadPayload | ModelCacheRepairPayload | ModelCacheRemovalPayload
)


def parse_model_cache_payload(
    kind: str, value: object
) -> ModelCacheDownloadPayload | ModelCacheRepairPayload | ModelCacheRemovalPayload:
    """Validate decoded database JSON against the operation-kind envelope."""

    try:
        raw = canonical_message(value)
        if kind == "download":
            parsed = ModelCacheDownloadPayload.model_validate_json(raw)
        elif kind == "repair":
            parsed = ModelCacheRepairPayload.model_validate_json(raw)
        elif kind == "remove":
            parsed = ModelCacheRemovalPayload.model_validate_json(raw)
        else:
            raise ValueError(f"unknown model cache operation kind: {kind}")
        return parsed
    except ValidationError:
        raise
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {kind} model cache operation payload") from error


class ModelCacheDownloadRequest(StrictModel):
    schema_version: Literal[2] = 2
    request_key: str = Field(pattern=UUID_PATTERN)
    plan_digest: Digest
    artifact_set_sha256: Digest | None = None
    model_content_sha256: Digest | None = None
    recipe_revision_sha256: Digest | None = None
    recipe_revision_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_policy: Literal["nas-first"] = "nas-first"

    @model_validator(mode="after")
    def recipe_identity_is_unambiguous(self) -> ModelCacheDownloadRequest:
        if (
            self.recipe_revision_sha256 is not None
            and self.recipe_revision_id is not None
        ):
            raise ValueError("recipe revision digest and ID cannot both be supplied")
        return self


class ModelCacheDownloadPreviewRequest(StrictModel):
    schema_version: Literal[2] = 2
    artifact_set_sha256: Digest | None = None
    model_content_sha256: Digest | None = None
    recipe_revision_sha256: Digest | None = None
    recipe_revision_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_policy: Literal["nas-first"] = "nas-first"

    @model_validator(mode="after")
    def recipe_identity_is_unambiguous(self) -> ModelCacheDownloadPreviewRequest:
        if (
            self.recipe_revision_sha256 is not None
            and self.recipe_revision_id is not None
        ):
            raise ValueError("recipe revision digest and ID cannot both be supplied")
        return self


class ModelCacheRepairPreviewRequest(StrictModel):
    schema_version: Literal[2] = 2
    artifact_set_sha256: Digest


class ModelCacheRepairRequest(StrictModel):
    schema_version: Literal[2] = 2
    request_key: str = Field(pattern=UUID_PATTERN)
    artifact_set_sha256: Digest
    plan_digest: Digest
    source_policy: Literal["nas-first"] = "nas-first"


class ModelCacheRetryRequest(StrictModel):
    schema_version: Literal[2] = 2
    request_key: str = Field(pattern=UUID_PATTERN)


class ModelCacheAccessResumeRequest(StrictModel):
    schema_version: Literal[2] = 2
    request_key: str = Field(pattern=UUID_PATTERN)
    artifact_set_sha256: Digest
    plan_digest: Digest


class ModelCacheOperatorRequest(StrictModel):
    """Body shared by the singular operator model actions."""

    schema_version: Literal[2] = 2
    request_key: str = Field(pattern=UUID_PATTERN)


class ModelCacheRemovalRequest(StrictModel):
    """Exact content identity and request key for a model cache removal."""

    schema_version: Literal[2] = 2
    request_key: str = Field(pattern=UUID_PATTERN)
    model_content_sha256: Digest


class ModelCacheCancellationRequest(StrictModel):
    """Stable identity and operator explanation for one cancellation request."""

    schema_version: Literal[2] = 2
    request_key: str = Field(pattern=UUID_PATTERN)
    reason: str = Field(min_length=1, max_length=512)


class ModelCacheOperatorResponse(StrictModel):
    """CLI-shaped result without exposing an internal plan/digest workflow."""

    schema_version: Literal[2] = 2
    action: ModelCacheOperatorAction
    selector: str = Field(min_length=1, max_length=256)
    request_key: str = Field(pattern=UUID_PATTERN)
    model_content_sha256: Digest | None = None
    operation_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    state: ModelCacheOperatorState
    phase: str = Field(min_length=1, max_length=64)
    progress: OperationProgress
    transferred_bytes: int = Field(ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    eta_seconds: float | None = Field(default=None, ge=0)
    preserved: list[str] = Field(default_factory=list, max_length=32)
    next_actions: list[str] = Field(default_factory=list, max_length=32)
    cancelled_operations: list[str] = Field(default_factory=list, max_length=32)
    cancellation: ModelCacheCancellation | None = None
    result: ModelCacheDownloadResult | ModelCacheRemovalResult | None = None
    failure: AvailabilityOperationFailure | None = None

    @model_validator(mode="after")
    def cancellation_matches_state(self) -> ModelCacheOperatorResponse:
        if self.state == "cancelling" and self.cancellation is None:
            raise ValueError("cancelling model operation requires cancellation intent")
        if self.cancellation is not None and self.state not in {
            "cancelling",
            "cancelled",
        }:
            raise ValueError("model cancellation intent requires a cancelling state")
        return self


class CacheStorageResponse(StrictModel):
    schema_version: Literal[2] = 2
    total_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)
    reserve_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    unique_used_bytes: int = Field(ge=0)
    in_flight_bytes: int = Field(ge=0)
    protected_bytes: int = Field(ge=0)
    reclaimable_bytes: int = Field(ge=0)


class CacheArtifactResponse(StrictModel):
    schema_version: Literal[2] = 2
    key: str = Field(pattern=ARTIFACT_KEY_PATTERN)
    id: str = Field(pattern=ARTIFACT_KEY_PATTERN)
    path: str = Field(min_length=1, max_length=512)
    sha256: Digest
    expected_bytes: int = Field(ge=0)
    actual_bytes: int = Field(ge=0)
    roles: list[str] = Field(min_length=1, max_length=32)
    state: Literal["partial", "verified", "missing", "corrupt"]
    source: str = Field(min_length=1, max_length=2048)

    @field_validator("path")
    @classmethod
    def relative_path_is_safe(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("artifact path must be a normalized relative path")
        return value


class CacheEntryResponse(StrictModel):
    schema_version: Literal[2] = 2
    artifact_set_sha256: Digest
    model_content_sha256: Digest | None
    recipe_revision_sha256: Digest | None
    state: ModelCacheEntryState
    coverage: ModelCacheCoverage
    expected_bytes: int = Field(ge=0)
    verified_bytes: int = Field(ge=0)
    unique_bytes: int = Field(ge=0)
    artifacts: list[CacheArtifactResponse] = Field(max_length=1024)
    protected: bool
    protected_reasons: list[str] = Field(max_length=32)
    update_available: bool
    recipe_update_available: bool
    created_at: str
    updated_at: str
    verified_at: str | None
    last_error: str | None = Field(default=None, max_length=512)


class ModelCacheInventoryResponse(StrictModel):
    schema_version: Literal[2] = 2
    source_policy: Literal["nas-first"] = "nas-first"
    entries: list[CacheEntryResponse] = Field(max_length=100)
    storage: CacheStorageResponse
    total: int = Field(ge=0)
    next_cursor: str | None = Field(default=None, max_length=1024)


class ModelCacheOperationProgress(StrictModel):
    schema_version: Literal[2] = 2
    phase: ModelCacheOperationPhase
    completed_artifacts: int = Field(ge=0)
    total_artifacts: int = Field(ge=0)
    downloaded_bytes: int = Field(ge=0)
    expected_bytes: int | None = Field(default=None, ge=0)
    current_artifact_key: str | None = Field(default=None, pattern=ARTIFACT_KEY_PATTERN)
    total_bytes_known: bool = True
    measurement: OperationProgress

    @model_validator(mode="after")
    def total_known_matches_value(self) -> ModelCacheOperationProgress:
        if self.total_bytes_known != (self.expected_bytes is not None):
            raise ValueError("total_bytes_known must match expected_bytes")
        if (
            self.measurement.completed_bytes,
            self.measurement.total_bytes,
            self.measurement.completed_items,
            self.measurement.total_items,
        ) != (
            self.downloaded_bytes,
            self.expected_bytes,
            self.completed_artifacts,
            self.total_artifacts,
        ):
            raise ValueError("cache counters must match canonical measurement")
        if len(self.model_dump_json().encode("utf-8")) > 1024 * 1024:
            raise ValueError("cache progress exceeds 1 MiB")
        return self


ModelCacheOperationResult = ModelCacheDownloadResult | ModelCacheRemovalResult


def parse_model_cache_result(kind: str, value: object) -> ModelCacheOperationResult:
    """Validate the current result against the operation that produced it."""
    raw = canonical_message(value)
    if kind in {"download", "repair"}:
        return ModelCacheDownloadResult.model_validate_json(raw)
    if kind == "remove":
        return ModelCacheRemovalResult.model_validate_json(raw)
    raise ValueError(f"unknown model cache operation kind: {kind}")


class ModelCacheOperationResponse(StrictModel):
    schema_version: Literal[2] = 2
    id: str = Field(pattern=UUID_PATTERN)
    request_key: str = Field(pattern=UUID_PATTERN)
    kind: ModelCacheOperationKind
    state: ModelCacheOperationState | Literal["cancelling"]
    attempt: int = Field(ge=1)
    artifact_set_sha256: Digest | None
    plan_digest: Digest | None
    progress: ModelCacheOperationProgress
    result: ModelCacheOperationResult | None = None
    failure: AvailabilityOperationFailure | None = None
    cancellation: ModelCacheCancellation | None = None
    created_at: str
    updated_at: str
    completed_at: str | None

    @model_validator(mode="after")
    def result_matches_operation(self) -> ModelCacheOperationResponse:
        if self.result is not None:
            parse_model_cache_result(self.kind, self.result)
        if self.state == "succeeded":
            if self.result is None or self.failure is not None:
                raise ValueError(
                    "succeeded cache operation requires a result and no failure"
                )
        elif self.result is not None:
            raise ValueError("cache result is only available on success")
        if self.state == "failed" and self.failure is None:
            raise ValueError("failed cache operation requires failure evidence")
        if self.state == "running" and self.failure is not None:
            raise ValueError("running cache operation cannot retain failure evidence")
        if self.state == "cancelling" and self.cancellation is None:
            raise ValueError("cancelling cache operation requires cancellation intent")
        if self.cancellation is not None and self.state not in {
            "cancelling",
            "cancelled",
        }:
            raise ValueError("cache cancellation intent requires a cancelling state")
        return self


class ModelCacheOperationsResponse(StrictModel):
    schema_version: Literal[2] = 2
    operations: list[ModelCacheOperationResponse] = Field(max_length=100)
    total: int = Field(ge=0)
    next_cursor: str | None = Field(default=None, max_length=1024)


class ModelCacheRepairPreviewResponse(StrictModel):
    schema_version: Literal[2] = 2
    artifact_set_sha256: Digest
    plan_digest: Digest
    source_policy: Literal["nas-first"] = "nas-first"
    artifact_count: int = Field(ge=0)
    current_state: ModelCacheEntryState
    expected_bytes: int = Field(ge=0)
    verified_bytes: int = Field(ge=0)


class ModelCacheDownloadPreviewResponse(StrictModel):
    schema_version: Literal[2] = 2
    artifact_set_sha256: Digest
    plan_digest: Digest
    source_policy: Literal["nas-first"] = "nas-first"
    artifact_count: int = Field(ge=0)
    expected_bytes: int = Field(ge=0)
    already_cached_bytes: int = Field(ge=0)
    new_bytes: int = Field(ge=0)
    blockers: list[str] = Field(max_length=32)
    warnings: list[str] = Field(max_length=32)


class ModelCacheUpstreamRevision(StrictModel):
    repository: str
    pinned_revision: str = Field(pattern=REVISION_PATTERN)
    latest_revision: str | None = Field(default=None, pattern=REVISION_PATTERN)
    status: Literal["current", "update-available", "check-failed"]
    checked_at: str
    error_code: str | None = None


class ModelCacheUpdateResponse(StrictModel):
    schema_version: Literal[2] = 2
    artifact_set_sha256: Digest
    model_content_sha256: Digest | None
    latest_model_content_sha256: Digest | None
    model_update_from: ModelReference | None = None
    model_update_to: ModelReference | None = None
    upstream_revisions: list[ModelCacheUpstreamRevision] = Field(default_factory=list)
    model_update_ambiguous: bool = False
    model_update_candidates: list[ModelReference] = Field(
        default_factory=list, max_length=16
    )
    recipe_revision_sha256: Digest | None
    latest_recipe_revision_sha256: Digest | None
    model_update_available: bool
    recipe_update_available: bool
    updated_at: str | None = None


class ModelCacheUpdatesResponse(StrictModel):
    schema_version: Literal[2] = 2
    source_policy: Literal["nas-first"] = "nas-first"
    updates: list[ModelCacheUpdateResponse] = Field(max_length=100)
    total: int = Field(ge=0)
    next_cursor: str | None = Field(default=None, max_length=1024)


__all__ = [
    "CacheArtifactResponse",
    "CacheEntryResponse",
    "CacheManifest",
    "CacheManifestArtifact",
    "CacheStorageResponse",
    "ModelCacheAccessRecheck",
    "ModelCacheAccessResumeRequest",
    "ModelCacheCancellation",
    "ModelCacheCancellationRequest",
    "ModelCacheClaim",
    "ModelCacheCoverage",
    "ModelCacheDownloadPayload",
    "ModelCacheDownloadPreviewRequest",
    "ModelCacheDownloadPreviewResponse",
    "ModelCacheDownloadRequest",
    "ModelCacheDownloadResult",
    "ModelCacheEntryState",
    "ModelCacheInventoryResponse",
    "ModelCacheObjectReceipt",
    "ModelCacheOperationKind",
    "ModelCacheOperationPayload",
    "ModelCacheOperationPhase",
    "ModelCacheOperationProgress",
    "ModelCacheOperationResponse",
    "ModelCacheOperationState",
    "ModelCacheOperationsResponse",
    "ModelCacheOperatorAction",
    "ModelCacheOperatorRequest",
    "ModelCacheOperatorResponse",
    "ModelCacheOperatorState",
    "ModelCacheRemovalPayload",
    "ModelCacheRemovalRequest",
    "ModelCacheRemovalResult",
    "ModelCacheRepairPayload",
    "ModelCacheRepairPreviewRequest",
    "ModelCacheRepairPreviewResponse",
    "ModelCacheRepairRequest",
    "ModelCacheRetry",
    "ModelCacheRetryRequest",
    "ModelCacheTransfer",
    "ModelCacheTransferArtifact",
    "ModelCacheUpdateResponse",
    "ModelCacheUpdatesResponse",
    "parse_model_cache_payload",
]
