"""Views."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Future
from dataclasses import dataclass, field

from ..model_cache_contract import ModelCacheOperationResult
from ..operation_blockers import OperationBlocker
from .artifacts import ArtifactSetManifest, ArtifactSpec
from .constants import SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ModelCacheRemovalScope:
    """Exact SQL membership and unshared bytes for one accepted removal."""

    selected_sets: tuple[str, ...]
    memberships: tuple[tuple[str, tuple[str, ...]], ...]
    selected_objects: tuple[str, ...]
    delete_objects: tuple[str, ...]
    shared_memberships: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class StorageSummary:
    total_bytes: int
    free_bytes: int
    reserve_bytes: int
    available_bytes: int
    unique_used_bytes: int
    in_flight_bytes: int
    protected_bytes: int
    reclaimable_bytes: int

    def document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "reserve_bytes": self.reserve_bytes,
            "available_bytes": self.available_bytes,
            "unique_used_bytes": self.unique_used_bytes,
            "in_flight_bytes": self.in_flight_bytes,
            "protected_bytes": self.protected_bytes,
            "reclaimable_bytes": self.reclaimable_bytes,
        }


@dataclass(frozen=True, slots=True)
class CacheOperationView:
    id: str
    request_key: str
    kind: str
    state: str
    attempt: int
    model_content_sha256: str | None
    artifact_set_sha256: str | None
    plan_digest: str | None
    review_digest: str | None
    progress: Mapping[str, object]
    result: ModelCacheOperationResult | None
    last_error: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    retryable: bool = False
    failure: Mapping[str, object] | None = None
    cancellation: Mapping[str, object] | None = None
    #: What a queued or interrupted operation waits for, and when it retries.
    blockers: tuple[OperationBlocker, ...] = ()
    next_attempt_at: str | None = None


@dataclass(slots=True)
class _BackgroundTransfer:
    """One download or repair this process is transferring on the shared pool."""

    set_digest: str
    manifest: ArtifactSetManifest
    force: bool
    specs: list[ArtifactSpec]
    planned_total: int
    transfer_attempt: int
    next_index: int = 0
    futures: list[Future[None]] = field(default_factory=list)
    future_specs: dict[Future[None], str] = field(default_factory=dict)
    failure: BaseException | None = None
    failure_artifact_key: str | None = None

    def pending(self) -> int:
        """Transfers submitted to the pool that have not finished."""

        return sum(1 for future in self.futures if not future.done())
