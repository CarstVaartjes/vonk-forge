"""Schema-2 contracts for the Controller-owned NAS model cache."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DIGEST_PATTERN = r"^[0-9a-f]{64}$"
ARTIFACT_KEY_PATTERN = r"^[a-z][a-z0-9_.:-]{0,255}$"
REVISION_PATTERN = r"^[0-9a-f]{40,64}$"
UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

Digest = Annotated[str, Field(pattern=DIGEST_PATTERN)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ModelCacheDownloadRequest(StrictModel):
    schema_version: Literal[2] = 2
    request_key: str = Field(pattern=UUID_PATTERN)
    plan_digest: Digest
    artifact_set_sha256: Digest | None = None
    model_version_sha256: Digest | None = None
    recipe_revision_sha256: Digest | None = None
    recipe_revision_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_policy: Literal["nas-first"] = "nas-first"

    @model_validator(mode="after")
    def recipe_identity_is_unambiguous(self) -> ModelCacheDownloadRequest:
        if self.recipe_revision_sha256 is not None and self.recipe_revision_id is not None:
            raise ValueError("recipe revision digest and ID cannot both be supplied")
        return self


class ModelCacheDownloadPreviewRequest(StrictModel):
    schema_version: Literal[2] = 2
    artifact_set_sha256: Digest | None = None
    model_version_sha256: Digest | None = None
    recipe_revision_sha256: Digest | None = None
    recipe_revision_id: str | None = Field(default=None, min_length=1, max_length=128)
    source_policy: Literal["nas-first"] = "nas-first"

    @model_validator(mode="after")
    def recipe_identity_is_unambiguous(self) -> ModelCacheDownloadPreviewRequest:
        if self.recipe_revision_sha256 is not None and self.recipe_revision_id is not None:
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


class ModelCacheEvictionPreviewRequest(StrictModel):
    schema_version: Literal[2] = 2
    target_bytes: int = Field(gt=0)


class ModelCacheEvictRequest(StrictModel):
    schema_version: Literal[2] = 2
    request_key: str = Field(pattern=UUID_PATTERN)
    plan_digest: Digest
    target_bytes: int = Field(gt=0)


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
    model_version_sha256: Digest | None
    recipe_revision_sha256: Digest | None
    state: Literal[
        "incomplete", "downloading", "verifying", "cached", "needs-repair", "failed"
    ]
    coverage: Literal["complete", "incomplete"]
    expected_bytes: int = Field(ge=0)
    verified_bytes: int = Field(ge=0)
    unique_bytes: int = Field(ge=0)
    artifacts: list[CacheArtifactResponse] = Field(max_length=128)
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
    phase: Literal["queued", "downloading", "verifying", "reclaiming", "completed", "failed"]
    completed_artifacts: int = Field(ge=0)
    total_artifacts: int = Field(ge=0)
    downloaded_bytes: int = Field(ge=0)
    expected_bytes: int = Field(ge=0)
    current_artifact_key: str | None = Field(default=None, pattern=ARTIFACT_KEY_PATTERN)


class ModelCacheOperationResponse(StrictModel):
    schema_version: Literal[2] = 2
    id: str = Field(pattern=UUID_PATTERN)
    request_key: str = Field(pattern=UUID_PATTERN)
    kind: Literal["download", "repair", "evict"]
    state: Literal["queued", "running", "partial", "succeeded", "failed", "cancelled"]
    attempt: int = Field(ge=1)
    artifact_set_sha256: Digest | None
    plan_digest: Digest | None
    progress: ModelCacheOperationProgress
    result: dict[str, object] | None = None
    last_error: str | None = Field(default=None, max_length=512)
    created_at: str
    updated_at: str
    completed_at: str | None


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
    current_state: Literal[
        "incomplete", "downloading", "verifying", "cached", "needs-repair", "failed"
    ]
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


class ModelCacheEvictionEntry(StrictModel):
    schema_version: Literal[2] = 2
    artifact_set_sha256: Digest
    reclaimable_bytes: int = Field(ge=0)
    protected: bool
    protected_reasons: list[str] = Field(max_length=32)
    last_accessed_at: str


class ModelCacheEvictionPreviewResponse(StrictModel):
    schema_version: Literal[2] = 2
    plan_digest: Digest
    target_bytes: int = Field(gt=0)
    selected: list[ModelCacheEvictionEntry] = Field(max_length=100)
    protected_entries: list[ModelCacheEvictionEntry] = Field(max_length=100)
    reclaimable_bytes: int = Field(ge=0)
    selected_bytes: int = Field(ge=0)
    storage_before: CacheStorageResponse
    storage_after: CacheStorageResponse
    blockers: list[str] = Field(max_length=32)


class ModelCacheUpdateResponse(StrictModel):
    schema_version: Literal[2] = 2
    artifact_set_sha256: Digest
    model_version_sha256: Digest | None
    latest_model_version_sha256: Digest | None
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
    "CacheStorageResponse",
    "ModelCacheDownloadPreviewRequest",
    "ModelCacheDownloadPreviewResponse",
    "ModelCacheDownloadRequest",
    "ModelCacheEvictRequest",
    "ModelCacheEvictionEntry",
    "ModelCacheEvictionPreviewRequest",
    "ModelCacheEvictionPreviewResponse",
    "ModelCacheInventoryResponse",
    "ModelCacheOperationProgress",
    "ModelCacheOperationResponse",
    "ModelCacheOperationsResponse",
    "ModelCacheRepairPreviewRequest",
    "ModelCacheRepairPreviewResponse",
    "ModelCacheRepairRequest",
    "ModelCacheUpdateResponse",
    "ModelCacheUpdatesResponse",
]
