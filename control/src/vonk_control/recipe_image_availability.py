"""Durable preparation of one exact canonical Recipe's runtime image.

The availability operation is deliberately separate from Run/Switch.  It
refreshes catalog metadata before taking a snapshot of the selected Recipe,
then prepares that snapshot without changing a pin or a running workload.  A
``Job`` row is used as the restart-safe operation record so the worker and the
API can observe the same status without a second operation database.

This module owns image preparation only.  Model file transfers remain owned by
``model_cache`` and can be linked by the caller through ``model_digest``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased, sessionmaker
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    AssetAvailability,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    ModelCacheCode,
    OperationCheckpoint,
    OperationMemberProgress,
    OperationProgress,
    ProgressPhase,
    RecipeBuildCode,
    RecipeImageCode,
    RecipePackageCode,
    RecipeUpdateCode,
    RuntimeImageCode,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)
from vonk_forge_contracts import RecipeDefinition, document_sha256, read_recipe

from . import job_states, model_cache_states
from .admission_locking import is_admission_contention
from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    RemovalOwnerKind,
    check_removal_fence_nowait,
    clear_removal,
    dead_removal_identities,
    has_pending_removal,
    lock_reference_gates,
    lock_removal_fences,
    release_dead_removal_nowait,
    require_reference_open,
    reserve_removal_owners,
    retryable_artifact_database_error,
    supersede_removal_nowait,
)
from .artifact_reference_scan import (
    MAX_ARTIFACT_OWNER_SCAN_BYTES,
    ArtifactReferenceFinding,
    model_set_reference_findings,
    runtime_image_reference_findings,
    runtime_image_reference_reasons,
)
from .cache_removal_review import (
    ArtifactKind,
    AssetDisposition,
    CacheRemovalAsset,
    CacheRemovalBlocker,
    CacheRemovalFinding,
    CacheRemovalReview,
    CacheRemovalReviewContent,
    refusing_removal_blockers,
    seal_cache_removal_review,
)
from .catalog_queries import active_head_revision
from .categorized_errors import InvalidValue, MissingRecord
from .categorized_faults import security_reason
from .content_identity import ImageContent, same_image
from .failure_classification import is_redownload, is_security_failure
from .job_documents import (
    AvailabilityJobPayload,
    AvailabilityJobResult,
    AvailabilityModelChild,
    AvailabilityRetry,
    AvailabilityRuntime,
    AvailabilitySupersession,
    RecipeBuildParent,
)
from .lifecycle.core import STOP_BUDGET
from .lifecycle.evidence import BookkeepingReason, retire_as_unknown
from .lifecycle.image_availability import ImageAvailabilityAdapter
from .lifecycle.types import State
from .logging import log_event
from .model_cache import (
    ModelCacheConflict,
    ModelCacheError,
    ModelCacheNotFound,
    ModelCacheRemovalScope,
    model_cache_failure_is_terminal,
)
from .model_cache_contract import (
    CacheManifest,
    ModelCacheOperationProgress,
    ModelCacheRemovalResult,
    ModelCacheRepairPreviewResponse,
)
from .model_cache_progress import project_cache_progress
from .models import (
    ArtifactLifecycleGate,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    Job,
    ModelCacheOperation,
    RecipeBuild,
)
from .operation_blockers import (
    OperationBlocker,
    bound_blockers,
    make_blocker,
)
from .operation_contract import (
    AvailabilityOperationFailure,
    AvailabilityRecoveryAction,
    sanitize_failure_evidence,
)
from .operation_progress import aggregate_progress
from .recipe_availability_intent import (
    RecipeAvailabilityIntent,
    RecipeRetryIntent,
    RecipeRevisionIntent,
    RecipeSelectorIntent,
)
from .recipe_build_cancellation import (
    BuildConsumerError,
    current_build_consumers,
    lock_availability_build_dependency,
    lock_build_dependency,
    request_build_cancellation,
)
from .recipe_image_availability_clocks_contract import StoredAvailabilityClocks
from .recipe_image_availability_contract import (
    AvailabilityBuildReceipt,
    RecipeImageAvailabilityArtifact,
)
from .recipe_image_availability_reader_contract import (
    AvailabilityDownloadPreview,
    RecoveredAvailabilityPayload,
    StoredAvailabilityIdentity,
    StoredModelChildCancellation,
)
from .recipe_image_availability_view_contract import (
    RecipeCacheRemovalStatus,
    RecipeImageAvailabilityView,
)
from .recipe_image_removal_contract import (
    RECIPE_CACHE_REMOVE_KIND,
    RecipeCacheRemovalCheckpoint,
    RecipeCacheRemovalIntent,
    RecipeCacheRemovalModelChild,
    RecipeCacheRemovalOwner,
    RecipeCacheRemovalPlan,
    RecipeCacheRemovalResult,
)
from .recipe_lifecycle_contract import RecipeOperationCancellationResult
from .recipe_update_contract import UPDATE_KIND, RecipeUpdateResponse
from .revision_images import revision_archives, revision_images
from .runtime_image_preparation import (
    OCIImageTransport,
    RuntimeImagePreparationError,
    RuntimeImagePreparationRefused,
    RuntimeImagePreparationUnknown,
    RuntimeImageReceipt,
    RuntimeImageReferenceIntent,
    RuntimeImageStorage,
    prepare_runtime_image,
)
from .stored_json import JsonColumn, Residue, read_column, read_row_column
from .strict_json import read_stored_model, serialize_json_value
from .worker_memory_contract import WorkerMemoryComponent

if TYPE_CHECKING:
    from .recipe_update_batches import RecipeUpdateClaim

_LOGGER = logging.getLogger(__name__)
#: The progress ``activity`` of an operation: working now, or waiting for its turn.
_ACTIVE = "active"
_WAITING = "waiting"
_PREPARATION_RETRY_QUIET = timedelta(minutes=15)
#: A finished preparation the plan still reports missing is asked again after this.
_PREPARATION_RECHECK_QUIET = timedelta(minutes=2)
#: Longest chain of re-asks one revision keeps (failures and stale successes).
_PREPARATION_CHAIN_LIMIT = 64
_SUCCEEDED = job_states.words(LifecycleState.SUCCEEDED)
SCHEMA_VERSION = 2
OPERATION_KIND = "recipe.image.availability.v2"
REMOVE_OPERATION_KIND = RECIPE_CACHE_REMOVE_KIND
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANCELLATION_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
# Only an invalid recipe/runtime contract, a withdrawn revision, a revoked
# authority, an untrusted source or redirect, an invalid build source or
# security envelope, or a conflicting registry/build identity for the same
# bytes is terminal (as is any availability error explicitly marked
# non-retryable, such as a build whose identity changed). Every other failure, including
# integrity mismatches (whose bytes are then downloaded or built again) and
# malformed records of our own, retries with capped backoff while the
# operation remains the current intent.
_TERMINAL_FAILURE_CODES = frozenset(
    {
        RecipeBuildCode.SECURITY_INVALID,
        RecipeBuildCode.SOURCE_INVALID,
        RecipeImageCode.RECIPE_INVALID,
        RecipeImageCode.RECIPE_UNAVAILABLE,
        RecipeImageCode.RUNTIME_INVALID,
        RuntimeImageCode.DESTINATION_FORBIDDEN,
        RuntimeImageCode.REDIRECT_FORBIDDEN,
        RuntimeImageCode.IMAGE_UNPINNED,
        RuntimeImageCode.RECEIPT_IDENTITY_CONFLICT,
        RuntimeImageCode.SOURCE_MISMATCH,
    }
)


def _removal_retry_is_due(
    failure: AvailabilityOperationFailure | None, now: datetime
) -> bool:
    if failure is None:
        return True
    if not failure.retryable or failure.retry_time is None:
        return False
    try:
        retry_time = datetime.fromisoformat(failure.retry_time)
    except ValueError as error:
        # A damaged stored retry time is no schedule: the removal is due now (it
        # re-derives its next attempt from the core's clock) and the damage is
        # recorded instead of wedging the removal.
        retire_as_unknown(
            "recipe-image.removal-retry-time",
            failure.code,
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            f"{type(error).__name__}: {error}",
        )
        return True
    retry_time = (
        retry_time if retry_time.tzinfo is not None else retry_time.replace(tzinfo=UTC)
    )
    return now >= retry_time


_CAPACITY_FAILURE_CODES = frozenset(
    {
        RecipeBuildCode.INSUFFICIENT_DISK,
        RecipeBuildCode.INSUFFICIENT_MEMORY,
        RecipeImageCode.INSUFFICIENT_DISK,
        RecipeImageCode.INSUFFICIENT_MEMORY,
        RuntimeImageCode.INSUFFICIENT_DISK,
    }
)
_INTEGRITY_FAILURE_CODES = frozenset(
    {
        RuntimeImageCode.DIGEST_MISMATCH,
        RecipePackageCode.DIGEST_MISMATCH,
        RuntimeImageCode.DIGEST_MISMATCH_,
        RuntimeImageCode.ARCHIVE_MISMATCH,
        RuntimeImageCode.ARCHIVE_CONFLICT,
        RuntimeImageCode.EVIDENCE_INVALID,
    }
)
# Another transaction held a record this one needed for an instant (a model
# download writing its progress, say). That is a wait to retry, never a failure
# and never a raw database message.
DATABASE_BUSY_CODE = RecipeImageCode.DATABASE_BUSY
DATABASE_BUSY_DETAIL = (
    "Another operation was changing the same record; this retries automatically."
)
_DEPENDENCY_WAIT_CODES = frozenset(
    {
        DATABASE_BUSY_CODE,
        RecipeImageCode.BUILD_CAPACITY_WAIT,
        RuntimeImageCode.TRANSFER_CONTENDED,
        RuntimeImageCode.PUBLICATION_CONTENDED,
        RecipeBuildCode.CONSUMER_BUSY,
        RecipeBuildCode.CANCELLATION_PENDING,
        RecipeImageCode.BUILD_WAIT,
    }
)
# A newer preparation for the same recipe supersedes an older one that has not
# started.  The cancellation is recorded on the operation itself (terminal
# state, typed failure evidence, and a bounded status reason) so newer intent
# wins visibly and the older attempt never consumes a builder or a queue slot.
SUPERSEDED_PREPARATION_CODE = RecipeImageCode.SUPERSEDED_BY_NEWER_REVISION
# Verified cache bytes can disappear (NAS restore, eviction, partial cleanup).
# That is ordinary cache loss, not corruption: it must re-prepare, never ask an
# operator to inspect a terminal failure.
_RECOVERABLE_MISS_CODES = frozenset({RuntimeImageCode.CACHE_MISSING})


# A recipe whose stored build source the Controller's source policy refuses; the
# refusal is final for that source and names the file and line of each finding.
SOURCE_POLICY_REFUSED_CODE = RecipeImageCode.SOURCE_POLICY_REFUSED


_CLAIM_SCAN_WINDOW = 256
_MODEL_WAIT_POLL_SECONDS = 5


class RecipeImageAvailabilityError(RuntimeError):
    """A bounded operator-facing availability failure."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        # None leaves classification to the existing transport/code heuristics;
        # either bool is an explicit owner decision and must be preserved.
        retryable: bool | None = None,
        retry_after_seconds: int | None = None,
        retry_time: str | None = None,
        recovery_actions: Sequence[str] = (),
        log_excerpt: str | None = None,
        step: str | None = None,
        settled_build_operation_id: str | None = None,
        required_bytes: int | None = None,
        free_bytes: int | None = None,
        shortfall_bytes: int | None = None,
        blockers: Sequence[OperationBlocker] = (),
    ) -> None:
        self.code = code
        self.detail = detail
        self.blockers = tuple(blockers)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.retry_time = retry_time
        self.recovery_actions = tuple(recovery_actions)
        self.log_excerpt = log_excerpt
        self.step = step
        self.settled_build_operation_id = settled_build_operation_id
        self.required_bytes = required_bytes
        self.free_bytes = free_bytes
        self.shortfall_bytes = shortfall_bytes
        super().__init__(detail)


class RecipeImageAvailabilityRefused(
    SecurityRefusalError, RecipeImageAvailabilityError
):
    """A refusal at a security boundary: review, scope or source authority that does not match."""

    def __init__(
        self,
        *args: Any,
        reason: SecurityRefusalReason | None = None,
        **fields: Any,
    ) -> None:
        RecipeImageAvailabilityError.__init__(self, *args, **fields)
        self.typed_reason = (
            reason if reason is not None else security_reason(args[0] if args else None)
        )


class RecipeImageAvailabilityInvalid(InvalidRequestError, RecipeImageAvailabilityError):
    """A malformed, stale or out-of-contract request: rejected before effects."""

    def __init__(
        self,
        *args: Any,
        reason: InvalidRequestReason | None = InvalidRequestReason.MALFORMED,
        field: str | None = None,
        **fields: Any,
    ) -> None:
        RecipeImageAvailabilityError.__init__(self, *args, **fields)
        self.typed_reason = reason
        self.typed_field = field


class RecipeImageAvailabilityUnknown(UnknownOutcomeError, RecipeImageAvailabilityError):
    """Unconfirmed bookkeeping, a busy owner or a transient failure: observed and retried, never a refusal."""

    def __init__(
        self,
        *args: Any,
        reason: WaitReason | None = None,
        **fields: Any,
    ) -> None:
        RecipeImageAvailabilityError.__init__(self, *args, **fields)
        self.typed_reason = reason


@dataclass(frozen=True, slots=True)
class BuildUnsettled:
    """A build observation that cannot settle yet: an unknown outcome, returned.

    The builder hands it back instead of raising: the build is still queued or
    running, its evidence is missing or changed, or its outcome is unconfirmed.
    The claim runner records it through the lifecycle core (``_fail``), which
    schedules the next attempt on its bounded backoff; ``retryable=False`` is
    the one explicit owner decision that ends the operation.  The fields are the
    ones ``_fail`` reads from any failure.
    """

    code: str
    detail: str
    reason: WaitReason
    retryable: bool | None = None
    retry_after_seconds: int | None = None
    retry_time: str | None = None
    recovery_actions: tuple[str, ...] = ()
    log_excerpt: str | None = None
    step: str | None = None
    settled_build_operation_id: str | None = None
    blockers: tuple[OperationBlocker, ...] = ()

    def __str__(self) -> str:
        return self.detail


class _ModelQueueFailed(RecipeImageAvailabilityUnknown):
    """A Model cache call raised: the availability error carries its cause."""

    def __init__(self, error: BaseException, action: str) -> None:
        if _is_database_busy(error):
            super().__init__(
                DATABASE_BUSY_CODE,
                DATABASE_BUSY_DETAIL,
                retryable=True,
                recovery_actions=("retry",),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        else:
            super().__init__(
                RecipeImageCode.MODEL_CACHE_UNAVAILABLE,
                f"{action} ({_failure_detail(error)[:200]})",
                retryable=True,
                recovery_actions=("retry",),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )


class RecipeAuthorityResolver(Protocol):
    """Refresh and resolve the selected canonical Recipe in one operation."""

    def __call__(
        self, recipe_revision_id: str, *, force: bool = False
    ) -> tuple[RecipeDefinition, AvailabilityRuntime]: ...


class RecipeImageBuilder(Protocol):
    """Build the exact source recipe and report bounded progress."""

    def __call__(
        self,
        recipe: RecipeDefinition,
        runtime: AvailabilityRuntime,
        *,
        claim: RecipeImageAvailabilityClaim,
        build_input_sha256: str,
        force: bool,
        progress: Callable[[object], None],
    ) -> object | BuildUnsettled: ...


class RuntimeImageCacheStorage(RuntimeImageStorage, Protocol):
    """Verified OCI storage that also exposes its archive namespace root.

    The cache-removal path must unlink an archive and its receipt by name, so
    it needs the namespace root that :class:`RuntimeImageStorage` leaves
    implicit. Naming that requirement here keeps the narrowed contract local
    to the consumer instead of widening every storage implementation.
    """

    root: Path

    def published_archive_bytes(self, archive_sha256: str) -> int: ...

    def remove_published(self, archive_sha256: str) -> int: ...

    def build_archive_available(
        self, archive_sha256: str, expected_bytes: int
    ) -> bool: ...


class ModelCacheOperationHandle(Protocol):
    """The bounded view a durable ModelCache operation exposes to its caller."""

    id: str
    kind: str
    request_key: str
    state: str
    progress: ModelCacheOperationProgress
    artifact_set_sha256: str | None
    plan_digest: str | None
    failure: AvailabilityOperationFailure | None
    result: object | None


class ModelCacheRemovalCoordinator(Protocol):
    """Exact model-cache scope/owner seam used by recipe removal."""

    def recipe_removal_scope_in_session(
        self, session: Session, *, recipe_revision_id: str
    ) -> ModelCacheRemovalScope | None: ...

    def removal_owner_findings_in_session(
        self, session: Session, scope: ModelCacheRemovalScope
    ) -> tuple[CacheRemovalFinding, ...]: ...

    def removal_asset_status(
        self, scope: ModelCacheRemovalScope
    ) -> tuple[CacheRemovalAsset, ...]: ...

    def retained_model_object_findings(
        self, scope: ModelCacheRemovalScope
    ) -> tuple[CacheRemovalFinding, ...]: ...

    def accept_recipe_removal_child_in_session(
        self,
        session: Session,
        *,
        actor: str,
        request_key: str,
        recipe_revision_id: str,
        operation_id: str,
        removal_fence: str,
        scope: ModelCacheRemovalScope,
    ) -> tuple[str, str, tuple[str, ...], str] | None: ...

    def get_operation(self, operation_id: str) -> ModelCacheOperationHandle: ...


class ModelCacheCancellationOwner(Protocol):
    """Required ModelCache owner seam for exact-child cancellation."""

    def cancel_operation_in_session(
        self,
        session: Session,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
        reason: str,
    ) -> bool: ...

    def signal_cancelled_operation(self, operation_id: str) -> None: ...

    def get_operation(self, operation_id: str) -> ModelCacheOperationHandle: ...


@dataclass(frozen=True, slots=True)
class RecipeImageAvailabilityClaim:
    """Small scheduler hook returned for execution outside the worker tick."""

    operation_id: str
    recipe_revision_id: str
    build_input_sha256: str | None
    claim_owner: str
    execution_attempt: int


@dataclass(frozen=True, slots=True)
class _RecipeRemovalSelection:
    """One read of the current immutable recipe and its exact cache targets."""

    revision_id: str
    revision_content_sha256: str
    image_archives: tuple[str, ...]
    image_expected_bytes: tuple[tuple[str, int], ...]
    model_scope: ModelCacheRemovalScope | None


class _AvailabilityClaimLost(RuntimeImagePreparationUnknown):
    """Stop an executor whose durable attempt can no longer accept writes."""

    def __init__(self) -> None:
        # Preserve this control outcome through image preparation's typed
        # exception boundary; contention must not become a transfer failure.
        super().__init__(
            RecipeImageCode.CLAIM_LOST, "availability execution claim is unavailable"
        )


def _iso(value: datetime) -> str:
    value = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.IDENTITY_INVALID,
            f"{field} must be a lowercase SHA-256 digest",
        )
    return value


def _optional_digest(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _digest(value, field=field)


def _canonical_cancellation_id(value: object) -> str:
    if not isinstance(value, str) or not _CANCELLATION_UUID.fullmatch(value):
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.CANCELLATION_INVALID,
            "cancellation request identity must be a canonical UUID",
        )
    try:
        if str(uuid.UUID(value)) != value:
            raise InvalidValue("noncanonical UUID")
    except ValueError as error:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.CANCELLATION_INVALID,
            "cancellation request identity must be a canonical UUID",
        ) from error
    return value


def _canonical_recipe(value: object) -> RecipeDefinition:
    if isinstance(value, RecipeDefinition):
        return value
    if not isinstance(value, Mapping):
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.RECIPE_INVALID,
            "selected recipe is not a canonical RecipeDefinition",
        )
    try:
        return read_recipe(value)
    except Exception as error:
        raise RecipeImageAvailabilityInvalid(
            RecipeImageCode.RECIPE_INVALID,
            "selected recipe is not a canonical RecipeDefinition",
        ) from error


def _known_total(runtime: AvailabilityRuntime) -> int | None:
    return (
        runtime.image_bytes if runtime.image_bytes and runtime.image_bytes > 0 else None
    )


def _progress(
    phase: str,
    *,
    completed_bytes: int = 0,
    total_bytes: int | None = None,
    bytes_per_second: float | None = None,
    eta_seconds: float | None = None,
    checkpoint: OperationCheckpoint | None = None,
) -> OperationProgress:
    return OperationProgress(
        phase=phase,
        completed_bytes=max(0, completed_bytes),
        total_bytes=total_bytes,
        total_bytes_known=total_bytes is not None,
        bytes_per_second=bytes_per_second,
        eta_seconds=eta_seconds,
        checkpoint=checkpoint,
    )


def _read[T](model: type[T], value: object, *, subject: str = "?") -> T | None:
    """A typed producer/consumer seam; damaged bookkeeping is a residue."""
    if isinstance(value, model):
        return value
    result = read_column(
        JsonColumn("jobs", "projection", {None: model}), value, subject=subject
    )
    return result if isinstance(result, model) else None


def _retryable(error: BaseException | BuildUnsettled) -> bool:
    """Classify by typed code; unknown failures retry with capped backoff."""

    code = getattr(error, "code", None)
    if isinstance(code, str) and is_security_failure(code):
        return False
    if isinstance(error, BuildUnsettled):
        return error.retryable is not False
    if (
        isinstance(error, UnknownOutcomeError)
        and not isinstance(error, ModelCacheError)
        and getattr(error, "retryable", None) is not False
    ):
        # An unknown outcome is not terminal on its own word: the next attempt
        # reads the evidence again, on the core's bounded clock.  Only an
        # explicit owner decision (``retryable=False``) ends it.
        return True
    if isinstance(code, str) and code in _TERMINAL_FAILURE_CODES:
        return False
    if isinstance(error, ModelCacheError):
        return not model_cache_failure_is_terminal(code)
    return not (
        isinstance(error, RecipeImageAvailabilityError) and error.retryable is False
    )


def _is_database_busy(error: BaseException | None) -> bool:
    """Whether PostgreSQL refused a lock this error (or its cause) waited for."""

    seen = 0
    while error is not None and seen < 8:
        if isinstance(error, DBAPIError) and is_admission_contention(error):
            return True
        # Only an explicit cause: __context__ is whatever was being handled
        # when this was raised, which says nothing about this failure.
        error = error.__cause__
        seen += 1
    return False


def _failure_code(error: BaseException | BuildUnsettled) -> str:
    """Return the stable operation failure code for an exception.

    Only the repository's own operation failures may contribute ``code``.
    A library exception can carry an unrelated attribute of the same name --
    ``sqlalchemy.exc.IntegrityError.code`` is the ``gkpj`` documentation slug --
    and copying it hides the failure class behind an opaque token that matches
    no recovery action and no operator instruction. Anything the database
    layer raises is therefore reported by its exception class name.
    """

    if isinstance(error, DBAPIError) and _is_database_busy(error):
        return DATABASE_BUSY_CODE
    code = getattr(error, "code", None)
    if isinstance(error, SQLAlchemyError) or not isinstance(code, str) or not code:
        return type(error).__name__.lower()
    return code


def _failure_detail(error: BaseException | BuildUnsettled) -> str:
    """Return operator-facing failure text, never a non-string attribute.

    ``sqlalchemy.exc.StatementError`` initialises ``detail`` to an empty list,
    so trusting the attribute records ``[]`` and discards the message, the
    statement and the violated constraint. A driver error is reported from its
    ``orig`` message, which names the constraint without the statement and
    bound parameters that ``str(error)`` would bury it under.
    """

    detail = getattr(error, "detail", None)
    if isinstance(detail, str) and detail.strip():
        return detail
    if isinstance(error, DBAPIError) and _is_database_busy(error):
        return DATABASE_BUSY_DETAIL
    message: str | None = None
    if isinstance(error, DBAPIError):
        origin = getattr(error, "orig", None)
        if origin is not None:
            message = str(origin).strip() or None
    if message is None:
        message = str(error)
    # Keep the class name: an opaque library message must never hide which
    # failure was raised.
    return f"{type(error).__name__}: {message}"


def _retry_after(error: BaseException | BuildUnsettled) -> int | None:
    value = getattr(error, "retry_after_seconds", None)
    if type(value) is int and 0 <= value <= 86_400:
        return value
    return None


def _log_excerpt(error: BaseException | BuildUnsettled) -> str | None:
    value = getattr(error, "log_excerpt", None)
    if not isinstance(value, str) or not value.strip():
        value = getattr(error, "detail", None)
    if not isinstance(value, str) or not value.strip():
        value = _failure_detail(error)
    if not isinstance(value, str) or not value.strip():
        return None
    return value[:1024]


def _recovery_actions(code: str, retryable: bool) -> list[str]:
    """Map stable failure classes to UI action identifiers."""

    if code in _CAPACITY_FAILURE_CODES:
        return ["free_space"]
    if code in _INTEGRITY_FAILURE_CODES or code in _RECOVERABLE_MISS_CODES:
        return ["force_rebuild"]
    if retryable:
        return ["retry"]
    return ["inspect"]


class RecipeImageAvailabilityService:
    """Persist and execute exact recipe-image availability operations.

    ``authority`` must perform the latest metadata refresh and return the
    selected revision's canonical recipe plus its compiled runtime projection.
    ``builder`` builds the recipe image; :func:`prepare_runtime_image` then
    verifies the stored archive with the OCI ``transport``.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        storage: RuntimeImageCacheStorage,
        authority: Callable[..., tuple[RecipeDefinition, object]],
        transport: OCIImageTransport | None = None,
        builder: Callable[..., object] | None = None,
        clock: Callable[[], datetime],
        model_cache: Any | None = None,
        max_parallel: int = 4,
        builder_admission: Callable[..., None] | None = None,
        claim_lease_seconds: int = 120,
    ) -> None:
        if not 1 <= max_parallel <= 16:
            raise InvalidValue("availability parallelism is invalid")
        if not 10 <= claim_lease_seconds <= 3_600:
            raise InvalidValue("availability claim lease is invalid")
        self._sessions = sessions
        self._storage = storage
        self._authority = authority
        self._transport = transport
        self._builder = builder
        self._clock = clock
        self._removal_gate_after: tuple[str, str] | None = None
        self._removal_request_after: str | None = None
        self._lifecycle = ImageAvailabilityAdapter(clock=clock)
        self._model_cache = model_cache
        self._max_parallel = max_parallel
        self._builder_admission = builder_admission
        self._claim_lease_seconds = claim_lease_seconds
        self._identity_locks: dict[str, threading.Lock] = {}
        self._identity_locks_guard = threading.Lock()
        self._removal_lock = threading.RLock()
        from .recipe_update_batches import RecipeUpdateBatches

        self._updates = RecipeUpdateBatches(self, sessions)

    def _payload(self, operation: Job) -> AvailabilityJobPayload | Residue:
        value = read_row_column(operation, "payload")
        if isinstance(value, AvailabilityJobPayload):
            return value
        identity = _read(
            StoredAvailabilityIdentity, operation.payload, subject=operation.id
        )
        if identity is not None:
            try:
                recipe = self._stored_recipe(identity)
                if isinstance(recipe, Residue):
                    return recipe
                # Repair is confined to the JSON decoding boundary, then the
                # complete current contract must validate again.
                document = json.loads(canonical_message(operation.payload))
                if isinstance(document, dict):
                    document["recipe"] = recipe.model_dump(mode="json")
                    recovered = _read(
                        RecoveredAvailabilityPayload,
                        document,
                        subject=operation.id,
                    )
                    if recovered is not None:
                        return AvailabilityJobPayload.model_validate_json(
                            canonical_message(recovered)
                        )
            except (RecipeImageAvailabilityError, TypeError, ValueError):
                pass
        if isinstance(value, Residue):
            return value
        return retire_as_unknown(
            "jobs.payload", operation.id, BookkeepingReason.ROW_INCOMPLETE
        )

    def _recipe_removal_selection_in_session(
        self,
        session: Session,
        selector: str,
        *,
        with_model: bool,
    ) -> _RecipeRemovalSelection:
        revision_id = self._resolve_recipe_selector_in_session(session, selector)
        revision = session.get(
            CatalogDocumentRevision,
            revision_id,
            populate_existing=True,
        )
        if (
            revision is None
            or revision.kind != "recipe"
            or revision.state != "active"
            or not isinstance(revision.content_digest, str)
        ):
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.SELECTOR_MISSING,
                "selected recipe revision is not active",
                reason=InvalidRequestReason.NOT_FOUND,
            )
        # The recipe document itself is not read: its cache is selected by the
        # revision identity, so a revision whose stored document is damaged can
        # still have its cache reviewed and removed.

        # The images the recipe's builds produced are the ones its removal
        # selects; the reference scan keeps whatever another owner still uses.
        image_sizes: dict[str, int] = {}
        for image in revision_images(session, [revision_id]).get(revision_id, ()):
            if not _is_digest(image.archive_sha256):
                # A stored archive digest that is not a digest names nothing that
                # can be removed: the damage is recorded and the image skipped.
                retire_as_unknown(
                    "recipe-image.removal-archive",
                    revision_id,
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "stored runtime image archive digest is not a SHA-256 digest",
                )
                continue
            image_sizes.setdefault(image.archive_sha256, image.image_bytes)

        model_scope: ModelCacheRemovalScope | None = None
        if with_model:
            if self._model_cache is None:
                raise RecipeImageAvailabilityInvalid(
                    ModelCacheCode.UNAVAILABLE,
                    "model cache removal is unavailable",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            model_scope = cast(
                ModelCacheRemovalCoordinator, self._model_cache
            ).recipe_removal_scope_in_session(session, recipe_revision_id=revision_id)
        return _RecipeRemovalSelection(
            revision_id=revision_id,
            revision_content_sha256=revision.content_digest,
            image_archives=tuple(sorted(image_sizes)),
            image_expected_bytes=tuple(sorted(image_sizes.items())),
            model_scope=model_scope,
        )

    def _resolve_recipe_selector(self, selector: str) -> str:
        """Resolve logical selectors to the current head, retaining exact pins."""

        if not isinstance(selector, str) or not 1 <= len(selector.strip()) <= 256:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.SELECTOR_INVALID, "recipe selector is required"
            )
        selector = selector.strip().casefold()
        with self._sessions() as session:
            return self._resolve_recipe_selector_in_session(session, selector)

    @staticmethod
    def _resolve_recipe_selector_in_session(session: Session, selector: str) -> str:
        """Resolve one already-normalized recipe selector in its SQL snapshot."""

        if _SHA256.fullmatch(selector):
            rows = list(
                session.scalars(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                        CatalogDocumentRevision.content_digest == selector,
                    )
                )
            )
        elif re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            selector,
        ):
            rows = list(
                session.scalars(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.kind == "recipe",
                        CatalogDocumentRevision.state == "active",
                        (CatalogDocumentRevision.id == selector)
                        | (
                            (CatalogDocumentRevision.document_id == selector)
                            & active_head_revision()
                        ),
                    )
                )
            )
        else:
            if "/" in selector:
                publisher, slug = selector.split("/", 1)
                query = select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                    CatalogDocumentRevision.publisher == publisher,
                    CatalogDocumentRevision.slug == slug,
                )
            else:
                query = select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                    CatalogDocumentRevision.slug == selector,
                )
            rows = list(session.scalars(query.where(active_head_revision())))
        if not rows:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.SELECTOR_MISSING,
                "recipe selector was not found",
                reason=InvalidRequestReason.NOT_FOUND,
            )
        if len(rows) != 1:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.SELECTOR_AMBIGUOUS,
                "recipe selector matches multiple recipes",
                reason=InvalidRequestReason.CONFLICT,
            )
        return rows[0].id

    def start_selector(
        self,
        selector: str,
        *,
        actor: str,
        request_id: str,
        force: bool = False,
    ) -> RecipeImageAvailabilityView:
        """Bind a selected recipe once under the caller's request identity."""

        intent = RecipeSelectorIntent(selector=selector, force=force)
        return self._start_request(intent, actor=actor, request_id=request_id)

    @staticmethod
    def _removal_payload_digest(payload: RecipeCacheRemovalPlan) -> str:
        return hashlib.sha256(canonical_message(payload)).hexdigest()

    def _read_removal_owner(self, operation: Job) -> RecipeCacheRemovalOwner:
        try:
            encoded = canonical_message(operation.payload)
            if len(encoded) > MAX_ARTIFACT_OWNER_SCAN_BYTES:
                raise InvalidValue(
                    "stored recipe removal owner exceeds the scan byte budget",
                    reason=InvalidRequestReason.LIMIT_EXCEEDED,
                )
            owner = read_row_column(operation, "payload")
        except (TypeError, ValueError):
            owner = None
        if not isinstance(owner, RecipeCacheRemovalOwner):
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.OPERATION_INVALID,
                "stored recipe removal owner is malformed",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        plan = owner.plan
        intent = plan.intent
        payload = plan
        if (
            operation.kind != REMOVE_OPERATION_KIND
            or operation.state
            not in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.BACKOFF,
                LifecycleState.SUCCEEDED,
                LifecycleState.FAILED,
                LifecycleState.CANCELLED,
            )
            or operation.actor != intent.actor
            or operation.request_id != intent.request_key
            or operation.authority_revision != intent.recipe_revision_id
            or operation.payload_digest != self._removal_payload_digest(payload)
            or operation.targets != []
        ):
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.OPERATION_INVALID,
                "stored recipe removal plan does not match its Job owner",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        if operation.state in job_states.words(
            LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF
        ) and (
            owner.checkpoint.failure is not None
            and not owner.checkpoint.failure.retryable
        ):
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.OPERATION_INVALID,
                "active recipe removal has a terminal failure checkpoint",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        if operation.state == "failed" and (
            owner.checkpoint.failure is None or owner.checkpoint.failure.retryable
        ):
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.OPERATION_INVALID,
                "failed recipe removal has no terminal failure checkpoint",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        return owner

    def _read_removal_intent(self, operation: Job) -> RecipeCacheRemovalIntent:
        return self._read_removal_owner(operation).plan.intent

    @staticmethod
    def _removal_progress_document(
        operation: Job,
        owner: RecipeCacheRemovalOwner,
    ):
        checkpoint = owner.checkpoint
        total_items = len(owner.plan.image_archives) + len(owner.plan.model_children)
        completed_items = checkpoint.image_index + checkpoint.model_index
        reclaimed_bytes = (
            checkpoint.image_reclaimed_bytes + checkpoint.model_reclaimed_bytes
        )
        successful = operation.state == "succeeded"
        progress = OperationProgress(
            phase=(
                ProgressPhase.COMPLETED
                if successful
                else ProgressPhase.FAILED
                if operation.state in {"failed", "cancelled"}
                else ProgressPhase.RECLAIMING
            ),
            completed_bytes=reclaimed_bytes,
            total_bytes=reclaimed_bytes if successful else None,
            total_bytes_known=successful,
            completed_items=completed_items,
            total_items=total_items,
            observed_at=_iso(operation.updated_at),
            activity=_ACTIVE if operation.state == "running" else _WAITING,
        )
        return progress

    def _read_removal_result(self, operation: Job, intent: RecipeCacheRemovalIntent):
        """Rebuild the read projection from the authoritative plan and checkpoint."""
        owner = self._read_removal_owner(operation)
        intent = owner.plan.intent
        if operation.state == "succeeded":
            self._stored_removal_result(operation, intent, owner)
        status = RecipeCacheRemovalStatus(
            schema_version=SCHEMA_VERSION,
            action=intent.action,
            selector=intent.selector,
            request_key=intent.request_key,
            operation_id=operation.id,
            recipe_revision_id=intent.recipe_revision_id,
            review_digest=intent.review_digest,
            with_model=intent.with_model,
            state=operation.state,
            progress=self._removal_progress_document(operation, owner),
            reclaimed_bytes=owner.checkpoint.image_reclaimed_bytes
            + owner.checkpoint.model_reclaimed_bytes,
            preserved=["profile-assignments", "spark-local-copies"]
            + ([] if intent.with_model else ["model-download"]),
            failure=owner.checkpoint.failure,
            next_actions=[]
            if owner.checkpoint.failure is None
            else [action.value for action in owner.checkpoint.failure.recovery_actions],
            cancelled_operations=[],
            cancelled_builds=[],
            model_removals=[child.operation_id for child in owner.plan.model_children],
        )
        return status.model_dump(mode="json", exclude_none=True)

    @staticmethod
    def _stored_removal_result(
        operation: Job,
        intent: RecipeCacheRemovalIntent,
        owner: RecipeCacheRemovalOwner,
    ) -> RecipeCacheRemovalResult | None:
        """The stored result of a finished removal, or ``None`` when it is
        missing, malformed or disagrees with the accepted intent."""

        result = read_row_column(operation, "result")
        if not isinstance(result, RecipeCacheRemovalResult):
            return None
        if (
            result.action != intent.action
            or result.selector != intent.selector
            or result.request_key != intent.request_key
            or result.operation_id != operation.id
            or result.recipe_revision_id != intent.recipe_revision_id
            or result.review_digest != intent.review_digest
            or result.with_model is not intent.with_model
            or result.reclaimed_bytes
            != owner.checkpoint.image_reclaimed_bytes
            + owner.checkpoint.model_reclaimed_bytes
            or result.model_removals
            != [item.operation_id for item in owner.plan.model_children]
            or owner.checkpoint.image_index != len(owner.plan.image_archives)
            or owner.checkpoint.model_index != len(owner.plan.model_children)
            or owner.checkpoint.failure is not None
        ):
            return None
        return result

    def _replay_removal(
        self,
        operation: Job,
        *,
        selector: str,
        actor: str,
        request_id: str,
        with_model: bool,
    ):
        if operation.kind != REMOVE_OPERATION_KIND:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.REQUEST_KEY_REUSED,
                "request key was already used for another operation",
                reason=InvalidRequestReason.CONFLICT,
            )
        owner = self._read_removal_owner(operation)
        intent = owner.plan.intent
        if (
            intent.selector != selector
            or intent.actor != actor
            or intent.request_key != request_id
            or intent.with_model is not with_model
        ):
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.REQUEST_KEY_REUSED,
                "request key was already used for another removal intent",
                reason=InvalidRequestReason.CONFLICT,
            )
        return self._read_removal_result(operation, intent)

    @staticmethod
    def _cache_removal_finding(
        finding: ArtifactReferenceFinding,
    ) -> CacheRemovalFinding:
        return CacheRemovalFinding(
            classification=finding.classification,
            asset_kind=finding.asset.kind,
            asset_sha256=finding.asset.sha256,
            owner_kind=finding.owner_kind,
            owner_id=finding.owner_id,
            state=finding.state,
            detail=finding.detail,
            reason=finding.reason,
        )

    def _recipe_removal_impact_in_session(
        self,
        session: Session,
        selector: str,
        *,
        with_model: bool,
        own_assignments: Sequence[
            tuple[ArtifactIdentity, RemovalOwnerKind, str, str]
        ] = (),
    ) -> tuple[
        _RecipeRemovalSelection,
        tuple[CacheRemovalFinding, ...],
        tuple[CacheRemovalFinding, ...],
        tuple[CacheRemovalBlocker, ...],
    ]:
        selection = self._recipe_removal_selection_in_session(
            session, selector, with_model=with_model
        )
        expected_owners = {
            identity: (owner_kind, owner_id, fence)
            for identity, owner_kind, owner_id, fence in own_assignments
        }
        image_findings: dict[str, tuple[ArtifactReferenceFinding, ...]] = {}
        model_findings: dict[str, tuple[ArtifactReferenceFinding, ...]] = {}
        model_owner_findings: tuple[CacheRemovalFinding, ...] = ()
        retained_model_findings: tuple[CacheRemovalFinding, ...] = ()
        scan_blockers: list[CacheRemovalBlocker] = []
        # What the model scan needs is known before it runs: an evidence gap is a
        # retryable blocker of this review (the caller observes and asks again),
        # decided here instead of raised out of the scan and caught below.
        model_identities: set[ArtifactIdentity] = set()
        own_model_identities: set[ArtifactIdentity] = set()
        precondition: CacheRemovalBlocker | None = None
        if selection.model_scope is not None:
            model_identities = {
                ArtifactIdentity("model-set", digest)
                for digest in selection.model_scope.selected_sets
            } | {
                ArtifactIdentity("model-object", digest)
                for digest in selection.model_scope.delete_objects
            }
            own_model_identities = {
                identity
                for identity in expected_owners
                if identity.kind in {"model-set", "model-object"}
            }
            # A model scope exists only when the ModelCache is wired (the selection
            # refuses a with-model review without it).
            if own_model_identities and not model_identities <= own_model_identities:
                precondition = CacheRemovalBlocker(
                    code=ArtifactLifecycleCode.REMOVAL_OWNER_UNRESOLVED,
                    detail="recipe removal holds an incomplete model deletion fence",
                    retryable=True,
                    recovery_actions=[AvailabilityRecoveryAction.RETRY],
                )
        try:
            if precondition is not None:
                scan_blockers.append(precondition)
            else:
                # Caught owner-scan errors must roll back their PostgreSQL
                # subtransaction before the independent lifecycle-gate scan runs.
                with session.begin_nested():
                    image_findings = runtime_image_reference_findings(
                        session, selection.image_archives
                    )
                    model_findings = (
                        {}
                        if selection.model_scope is None
                        else model_set_reference_findings(
                            session, selection.model_scope.selected_sets
                        )
                    )
                    if selection.model_scope is not None:
                        model_coordinator = cast(
                            ModelCacheRemovalCoordinator, self._model_cache
                        )
                        retained_model_findings = (
                            model_coordinator.retained_model_object_findings(
                                selection.model_scope
                            )
                        )
                        if not model_identities <= own_model_identities:
                            model_owner_findings = (
                                model_coordinator.removal_owner_findings_in_session(
                                    session, selection.model_scope
                                )
                            )
        except ArtifactLifecycleError as error:
            image_findings = {}
            model_findings = {}
            model_owner_findings = ()
            retained_model_findings = ()
            scan_blockers.append(
                CacheRemovalBlocker(
                    code=error.code,
                    detail=error.detail,
                    retryable=error.retryable,
                    recovery_actions=(["retry"] if error.retryable else ["inspect"]),
                )
            )
        except DBAPIError as error:
            image_findings = {}
            model_findings = {}
            model_owner_findings = ()
            retained_model_findings = ()
            translated = retryable_artifact_database_error(error)
            scan_blockers.append(
                CacheRemovalBlocker(
                    code=(
                        translated.code
                        if translated is not None
                        else ArtifactLifecycleCode.REFERENCE_SCAN_FAILED
                    ),
                    detail=(
                        translated.detail
                        if translated is not None
                        else "recipe cache reference scan could not be completed; removal was deferred"
                    ),
                    retryable=True,
                    recovery_actions=[AvailabilityRecoveryAction.RETRY],
                )
            )

        all_findings = (
            tuple(
                self._cache_removal_finding(finding)
                for mapping_by_asset in (image_findings, model_findings)
                for findings in mapping_by_asset.values()
                for finding in findings
            )
            + model_owner_findings
            + retained_model_findings
        )
        references = tuple(
            finding
            for finding in all_findings
            if finding.classification == "saved-reference"
        )
        active_work = tuple(
            finding
            for finding in all_findings
            if finding.classification == "active-work"
        )
        blockers: list[CacheRemovalBlocker] = []
        blockers.extend(
            CacheRemovalBlocker(
                code=ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                detail=finding.reason,
                retryable=True,
                recovery_actions=["observe_removal_operation"],
            )
            for finding in model_owner_findings
        )
        for kind, findings_by_asset, label, code in (
            (
                "runtime-image",
                image_findings,
                "runtime image",
                RecipeImageCode.REMOVAL_REFERENCED,
            ),
            (
                "model-set",
                model_findings,
                "model cache set",
                ModelCacheCode.REMOVAL_REFERENCED,
            ),
        ):
            reasons = [
                finding.reason
                for values in findings_by_asset.values()
                for finding in values
            ]
            if reasons:
                unique_reasons = sorted(set(reasons))
                blockers.append(
                    CacheRemovalBlocker(
                        code=code,
                        detail=(
                            f"{label} references prevent removal "
                            f"({len(unique_reasons)} owner(s)): "
                            + "; ".join(unique_reasons[:4])
                        )[:512],
                        retryable=True,
                        recovery_actions=(
                            ["inspect"]
                            if any(
                                finding.classification == "saved-reference"
                                for values in findings_by_asset.values()
                                for finding in values
                            )
                            else ["retry"]
                        ),
                    )
                )

        identity_groups: dict[str, set[str]] = {}
        if selection.image_archives:
            identity_groups["runtime-image"] = set(selection.image_archives)
        gate_conditions = [
            and_(
                ArtifactLifecycleGate.artifact_kind == kind,
                ArtifactLifecycleGate.artifact_sha256.in_(digests),
            )
            for kind, digests in identity_groups.items()
        ]
        gate_owners: list[str] = []
        try:
            with session.begin_nested():
                if gate_conditions:
                    for row in session.scalars(
                        select(ArtifactLifecycleGate)
                        .where(or_(*gate_conditions))
                        .order_by(
                            ArtifactLifecycleGate.artifact_kind,
                            ArtifactLifecycleGate.artifact_sha256,
                        )
                    ):
                        if row.removal_owner_id is None:
                            continue
                        identity = ArtifactIdentity(
                            cast(ArtifactKind, row.artifact_kind), row.artifact_sha256
                        )
                        expected = expected_owners.get(identity)
                        actual = (
                            row.removal_owner_kind,
                            row.removal_owner_id,
                            row.removal_fence,
                        )
                        if expected != actual:
                            gate_owners.append(
                                f"{row.artifact_kind} {row.artifact_sha256}: "
                                f"{row.removal_owner_kind} {row.removal_owner_id}"
                            )
        except DBAPIError as error:
            gate_owners = []
            translated = retryable_artifact_database_error(error)
            scan_blockers.append(
                CacheRemovalBlocker(
                    code=(
                        translated.code
                        if translated is not None
                        else ArtifactLifecycleCode.REFERENCE_SCAN_FAILED
                    ),
                    detail=(
                        translated.detail
                        if translated is not None
                        else "recipe removal-owner scan could not be completed; removal was deferred"
                    ),
                    retryable=True,
                    recovery_actions=[AvailabilityRecoveryAction.RETRY],
                )
            )
        blockers.extend(scan_blockers)
        if gate_owners:
            blockers.append(
                CacheRemovalBlocker(
                    code=ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                    detail=(
                        f"{len(gate_owners)} cache identity/identities are reserved "
                        "for another removal: " + "; ".join(sorted(gate_owners)[:4])
                    )[:512],
                    retryable=True,
                    recovery_actions=[AvailabilityRecoveryAction.RETRY],
                )
            )
        return selection, references, active_work, tuple(blockers)

    def _runtime_image_removal_assets(
        self, selection: _RecipeRemovalSelection
    ) -> tuple[tuple[CacheRemovalAsset, ...], tuple[CacheRemovalBlocker, ...]]:
        assets: list[CacheRemovalAsset] = []
        blockers: list[CacheRemovalBlocker] = []
        for archive, expected_bytes in selection.image_expected_bytes:
            availability: AssetAvailability = AssetAvailability.UNKNOWN
            available_bytes: int | None = None
            try:
                with self._storage.publication_lock(archive):
                    observed_bytes = self._storage.published_archive_bytes(archive)
                    if observed_bytes == 0:
                        availability = AssetAvailability.MISSING
                        available_bytes = 0
                    elif observed_bytes < expected_bytes:
                        availability = AssetAvailability.PARTIAL
                        available_bytes = observed_bytes
                    elif observed_bytes > expected_bytes:
                        blockers.append(
                            CacheRemovalBlocker(
                                code=RuntimeImageCode.ARCHIVE_SIZE_MISMATCH,
                                detail=(
                                    f"runtime image {archive} has {observed_bytes} bytes; "
                                    f"the authorized size is {expected_bytes}"
                                ),
                                retryable=False,
                                recovery_actions=["inspect"],
                            )
                        )
                    else:
                        receipt = self._storage.read_receipt(archive)
                        if same_image(
                            receipt,
                            ImageContent(
                                archive_sha256=archive, image_bytes=expected_bytes
                            ),
                        ):
                            # Managed publication records only verified bytes;
                            # the exact receipt plus regular-file size is the
                            # cheap readiness check for this operator review.
                            availability = AssetAvailability.VERIFIED
                            available_bytes = expected_bytes
                        else:
                            blockers.append(
                                CacheRemovalBlocker(
                                    code=RuntimeImageCode.RECEIPT_IDENTITY_CONFLICT,
                                    detail=(
                                        f"runtime image {archive} receipt does not "
                                        "match the authorized identity"
                                    ),
                                    retryable=False,
                                    recovery_actions=["inspect"],
                                )
                            )
            except RuntimeImagePreparationError as error:
                blockers.append(
                    CacheRemovalBlocker(
                        code=error.code,
                        detail=f"runtime image {archive}: {error.detail}",
                        retryable=error.retryable,
                        recovery_actions=list(error.recovery_actions)
                        or (["retry"] if error.retryable else ["inspect"]),
                    )
                )
            assets.append(
                CacheRemovalAsset(
                    kind="runtime-image",
                    sha256=archive,
                    expected_bytes=expected_bytes,
                    availability=availability,
                    available_bytes=available_bytes,
                    disposition="remove",
                )
            )
        return tuple(assets), tuple(blockers)

    def _model_removal_assets(
        self, scope: ModelCacheRemovalScope | None
    ) -> tuple[CacheRemovalAsset, ...]:
        if scope is None:
            return ()
        if self._model_cache is None:
            raise RecipeImageAvailabilityInvalid(
                ModelCacheCode.UNAVAILABLE,
                "model cache removal is unavailable",
                reason=InvalidRequestReason.NOT_FOUND,
            )
        assets = cast(
            ModelCacheRemovalCoordinator, self._model_cache
        ).removal_asset_status(scope)
        expected: dict[tuple[str, str], str] = {
            ("model-set", digest): "remove" for digest in scope.selected_sets
        }
        expected.update(
            {
                ("model-object", digest): (
                    "remove" if digest in scope.delete_objects else "retain-shared"
                )
                for digest in scope.selected_objects
            }
        )
        observed: dict[tuple[str, str], CacheRemovalAsset] = {}
        for asset in assets:
            if not isinstance(asset, CacheRemovalAsset):
                raise RecipeImageAvailabilityRefused(
                    ModelCacheCode.REVIEW_INVALID,
                    "ModelCache returned an invalid typed asset status",
                )
            identity = (asset.kind, asset.sha256)
            if identity in observed:
                raise RecipeImageAvailabilityRefused(
                    ModelCacheCode.REVIEW_INVALID,
                    "ModelCache returned duplicate reviewed asset identities",
                )
            if expected.get(identity) != asset.disposition:
                raise RecipeImageAvailabilityRefused(
                    ModelCacheCode.REVIEW_INVALID,
                    "ModelCache asset status does not match the exact removal scope",
                )
            observed[identity] = asset
        if set(observed) != set(expected):
            raise RecipeImageAvailabilityRefused(
                ModelCacheCode.REVIEW_INVALID,
                "ModelCache asset status is incomplete for the exact removal scope",
            )
        return tuple(observed[key] for key in sorted(observed))

    @staticmethod
    def _review_assets_for_selection(
        selection: _RecipeRemovalSelection,
        observed_assets: Sequence[CacheRemovalAsset],
    ) -> tuple[CacheRemovalAsset, ...]:
        by_identity = {(asset.kind, asset.sha256): asset for asset in observed_assets}
        expected: dict[
            tuple[ArtifactKind, str], tuple[int | None, AssetDisposition]
        ] = {
            ("runtime-image", digest): (size, "remove")
            for digest, size in selection.image_expected_bytes
        }
        if selection.model_scope is not None:
            expected.update(
                {
                    ("model-set", digest): (None, "remove")
                    for digest in selection.model_scope.selected_sets
                }
            )
            expected.update(
                {
                    ("model-object", digest): (
                        None,
                        "remove"
                        if digest in selection.model_scope.delete_objects
                        else "retain-shared",
                    )
                    for digest in selection.model_scope.selected_objects
                }
            )
        assets: list[CacheRemovalAsset] = []
        for identity, (expected_bytes, disposition) in sorted(expected.items()):
            observed = by_identity.get(identity)
            if observed is None or observed.disposition != disposition:
                assets.append(
                    CacheRemovalAsset(
                        kind=identity[0],
                        sha256=identity[1],
                        expected_bytes=expected_bytes,
                        availability=AssetAvailability.UNKNOWN,
                        available_bytes=None,
                        disposition=disposition,
                    )
                )
            else:
                if (
                    expected_bytes is not None
                    and observed.expected_bytes != expected_bytes
                ):
                    assets.append(
                        CacheRemovalAsset(
                            kind=identity[0],
                            sha256=identity[1],
                            expected_bytes=expected_bytes,
                            availability=AssetAvailability.UNKNOWN,
                            available_bytes=None,
                            disposition=disposition,
                        )
                    )
                else:
                    assets.append(observed)
        return tuple(assets)

    def _sealed_recipe_removal_review(
        self,
        *,
        selector: str,
        with_model: bool,
        selection: _RecipeRemovalSelection,
        references: Sequence[CacheRemovalFinding],
        active_work: Sequence[CacheRemovalFinding],
        blockers: Sequence[CacheRemovalBlocker],
        assets: Sequence[CacheRemovalAsset],
        now: datetime,
    ) -> CacheRemovalReview:
        known_asset_blockers = {
            asset.sha256
            for asset in assets
            if any(asset.sha256 in blocker.detail for blocker in blockers)
        }
        asset_blockers = [
            CacheRemovalBlocker(
                code=ArtifactLifecycleCode.ASSET_AVAILABILITY_UNKNOWN,
                detail=(f"{asset.kind} {asset.sha256} storage readiness is unknown"),
                retryable=True,
                recovery_actions=[AvailabilityRecoveryAction.RETRY],
            )
            for asset in assets
            if asset.availability == AssetAvailability.UNKNOWN
            and asset.sha256 not in known_asset_blockers
        ]
        blockers_by_identity = {
            (blocker.code, blocker.detail): blocker
            for blocker in (*blockers, *asset_blockers)
        }
        return seal_cache_removal_review(
            CacheRemovalReviewContent(
                schema_version=SCHEMA_VERSION,
                action="remove",
                resource_kind="recipe",
                selector=selector,
                target_identity=selection.revision_id,
                with_model=with_model,
                assets=list(assets),
                references=list(references),
                active_work=list(active_work),
                blockers=list(blockers_by_identity.values()),
                observed_at=_iso(now),
            )
        )

    def review_removal(self, selector: str, *, with_model: bool) -> CacheRemovalReview:
        """Return one complete read-only review of exact recipe cache effects."""

        if not isinstance(selector, str) or not 1 <= len(selector.strip()) <= 256:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.SELECTOR_INVALID, "recipe selector is required"
            )
        if type(with_model) is not bool:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.REMOVAL_CHOICE_INVALID,
                "with_model must be an explicit boolean",
            )
        normalized = selector.strip().casefold()
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        try:
            with self._sessions.begin() as session:
                selection, references, active_work, blockers = (
                    self._recipe_removal_impact_in_session(
                        session,
                        normalized,
                        with_model=with_model,
                    )
                )
        except ArtifactLifecycleError as error:
            raise RecipeImageAvailabilityRefused(
                error.code,
                error.detail,
                retryable=error.retryable,
                recovery_actions=("retry",) if error.retryable else ("inspect",),
            ) from error
        image_assets, image_blockers = self._runtime_image_removal_assets(selection)
        model_assets = self._model_removal_assets(selection.model_scope)
        return self._sealed_recipe_removal_review(
            selector=normalized,
            with_model=with_model,
            selection=selection,
            references=references,
            active_work=active_work,
            blockers=tuple(blockers) + image_blockers,
            assets=image_assets + model_assets,
            now=now,
        )

    def remove_selector(
        self,
        selector: str,
        *,
        actor: str,
        request_id: str,
        with_model: bool = False,
    ):
        """Accept a restart-safe removal of the named recipe against current state.

        Assets still in use are fenced against new consumers and the removal
        waits for their owners.
        """

        if not isinstance(selector, str) or not 1 <= len(selector.strip()) <= 256:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.SELECTOR_INVALID, "recipe selector is required"
            )
        selector = selector.strip().casefold()
        with self._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is not None:
                return self._replay_removal(
                    existing,
                    selector=selector,
                    actor=actor,
                    request_id=request_id,
                    with_model=with_model,
                )

        observed_review = self.review_removal(selector, with_model=with_model)
        observed_blockers = refusing_removal_blockers(observed_review)
        if observed_blockers:
            first_blocker = observed_blockers[0]
            raise RecipeImageAvailabilityRefused(
                first_blocker.code,
                first_blocker.detail,
                retryable=first_blocker.retryable,
                recovery_actions=first_blocker.recovery_actions,
            )

        operation_id = str(uuid.uuid4())
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(Job).where(Job.request_id == request_id)
                )
                if existing is not None:
                    return self._replay_removal(
                        existing,
                        selector=selector,
                        actor=actor,
                        request_id=request_id,
                        with_model=with_model,
                    )

                selection = self._recipe_removal_selection_in_session(
                    session, selector, with_model=with_model
                )
                revision_id = selection.revision_id
                image_archives = selection.image_archives
                model_scope = selection.model_scope

                removal_fence = str(uuid.uuid4())
                model_operation_id = (
                    str(uuid.uuid4()) if model_scope is not None else None
                )
                model_removal_fence = (
                    str(uuid.uuid4()) if model_scope is not None else None
                )
                image_owner_kind: RemovalOwnerKind = "recipe-image-job"
                assignments: list[
                    tuple[ArtifactIdentity, RemovalOwnerKind, str, str]
                ] = [
                    (
                        ArtifactIdentity("runtime-image", archive),
                        image_owner_kind,
                        operation_id,
                        removal_fence,
                    )
                    for archive in image_archives
                ]
                if model_scope is not None:
                    if model_operation_id is None or model_removal_fence is None:
                        raise AssertionError("model removal owner identity is missing")
                    model_owner_kind: RemovalOwnerKind = "model-cache-operation"
                    assignments.extend(
                        (
                            ArtifactIdentity("model-set", set_digest),
                            model_owner_kind,
                            model_operation_id,
                            model_removal_fence,
                        )
                        for set_digest in model_scope.selected_sets
                    )
                    assignments.extend(
                        (
                            ArtifactIdentity("model-object", object_digest),
                            "model-cache-operation",
                            model_operation_id,
                            model_removal_fence,
                        )
                        for object_digest in model_scope.delete_objects
                    )
                now = self._clock()
                now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
                reserve_removal_owners(session, assignments, now=now)
                (
                    current_selection,
                    references,
                    active_work,
                    current_blockers,
                ) = self._recipe_removal_impact_in_session(
                    session,
                    selector,
                    with_model=with_model,
                    own_assignments=assignments,
                )
                current_assets = self._review_assets_for_selection(
                    current_selection, observed_review.assets
                )
                current_review = self._sealed_recipe_removal_review(
                    selector=selector,
                    with_model=with_model,
                    selection=current_selection,
                    references=references,
                    active_work=active_work,
                    blockers=current_blockers,
                    assets=current_assets,
                    now=now,
                )
                current_blockers = refusing_removal_blockers(current_review)
                if current_blockers:
                    first_blocker = current_blockers[0]
                    raise RecipeImageAvailabilityRefused(
                        first_blocker.code,
                        first_blocker.detail,
                        retryable=first_blocker.retryable,
                        recovery_actions=first_blocker.recovery_actions,
                    )

                model_children: list[RecipeCacheRemovalModelChild] = []
                if model_scope is not None:
                    assert model_operation_id is not None
                    assert model_removal_fence is not None
                    child_request_key = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"vonk:recipe-remove-model:{request_id}:{revision_id}",
                        )
                    )
                    accepted_child = cast(
                        ModelCacheRemovalCoordinator, self._model_cache
                    ).accept_recipe_removal_child_in_session(
                        session,
                        actor=actor,
                        request_key=child_request_key,
                        recipe_revision_id=revision_id,
                        operation_id=model_operation_id,
                        removal_fence=model_removal_fence,
                        scope=model_scope,
                    )
                    if accepted_child is None:
                        raise RecipeImageAvailabilityRefused(
                            ModelCacheCode.REMOVAL_SCOPE_CHANGED,
                            "model cache scope changed before the recipe removal was accepted",
                        )
                    child_id, accepted_key, selected_sets, plan_digest = accepted_child
                    if (
                        child_id != model_operation_id
                        or accepted_key != child_request_key
                        or selected_sets != model_scope.selected_sets
                    ):
                        raise RecipeImageAvailabilityRefused(
                            ModelCacheCode.REMOVAL_SCOPE_CHANGED,
                            "accepted model removal does not match the reviewed recipe scope",
                        )
                    model_children.append(
                        RecipeCacheRemovalModelChild(
                            request_key=accepted_key,
                            operation_id=child_id,
                            plan_digest=plan_digest,
                            selected_sets=list(selected_sets),
                        )
                    )

                intent = RecipeCacheRemovalIntent(
                    schema_version=SCHEMA_VERSION,
                    kind=REMOVE_OPERATION_KIND,
                    action="remove",
                    selector=selector,
                    actor=actor,
                    request_key=request_id,
                    review_digest=current_review.review_digest,
                    recipe_revision_id=revision_id,
                    with_model=with_model,
                    removal_fence=removal_fence,
                )
                plan = RecipeCacheRemovalPlan(
                    schema_version=SCHEMA_VERSION,
                    intent=intent,
                    image_archives=list(image_archives),
                    model_children=model_children,
                )
                checkpoint = RecipeCacheRemovalCheckpoint(
                    schema_version=SCHEMA_VERSION,
                    image_index=0,
                    image_pending_bytes=None,
                    image_reclaimed_bytes=0,
                    model_index=0,
                    model_reclaimed_bytes=0,
                    retry_attempts=0,
                    failure=None,
                )
                owner = RecipeCacheRemovalOwner(
                    schema_version=SCHEMA_VERSION,
                    plan=plan,
                    checkpoint=checkpoint,
                )
                owner_bytes = len(canonical_message(owner.model_dump(mode="json")))
                if owner_bytes > MAX_ARTIFACT_OWNER_SCAN_BYTES:
                    raise RecipeImageAvailabilityInvalid(
                        RecipeImageCode.REMOVAL_SCOPE_LIMITED,
                        "recipe removal owner is "
                        f"{owner_bytes} bytes; limit is {MAX_ARTIFACT_OWNER_SCAN_BYTES} bytes",
                        reason=InvalidRequestReason.LIMIT_EXCEEDED,
                    )
                now = self._clock()
                now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
                operation = self._lifecycle.new_job(
                    id=operation_id,
                    request_id=request_id,
                    kind=REMOVE_OPERATION_KIND,
                    actor=actor,
                    authority_revision=revision_id,
                    targets=[],
                    payload_digest=self._removal_payload_digest(plan),
                    payload=serialize_json_value(owner),
                    result=None,
                    current_attempt=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(operation)
                session.flush()
        except IntegrityError:
            with self._sessions() as session:
                existing = session.scalar(
                    select(Job).where(Job.request_id == request_id)
                )
                if existing is None:
                    raise
                return self._replay_removal(
                    existing,
                    selector=selector,
                    actor=actor,
                    request_id=request_id,
                    with_model=with_model,
                )
        except ArtifactLifecycleError as error:
            raise RecipeImageAvailabilityRefused(
                error.code,
                error.detail,
                retryable=error.retryable,
                recovery_actions=("retry",) if error.retryable else (),
            ) from error
        except ModelCacheConflict as error:
            raise RecipeImageAvailabilityRefused(
                error.code,
                error.detail,
                retryable=error.recovery == "retry",
                retry_after_seconds=error.retry_after_seconds,
                recovery_actions=("retry",) if error.recovery == "retry" else (),
            ) from error

        with self._sessions() as session:
            operation = session.get(Job, operation_id)
            if operation is None:
                raise RecipeImageAvailabilityUnknown(
                    RecipeImageCode.OPERATION_MISSING,
                    "accepted recipe removal owner could not be read",
                    retryable=True,
                    recovery_actions=("retry",),
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            intent = self._read_removal_intent(operation)
            return self._read_removal_result(operation, intent)

    def reconcile_requested_removals(self, *, limit: int = 64) -> int:
        """Reconcile only the exact deletion dependencies bound at acceptance."""
        # This stored contract also imports the API response types. Resolve it
        # after the service module has loaded, keeping that import seam acyclic.
        from .job_documents import AvailabilityJobPayload

        with self._sessions() as session:
            accepted_requests = []
            observed_request = False
            for operation in session.scalars(
                select(Job)
                .where(
                    Job.kind == OPERATION_KIND,
                    Job.state.in_(
                        job_states.words(LifecycleState.QUEUED, LifecycleState.BACKOFF)
                    ),
                )
                .where(
                    Job.id > self._removal_request_after
                    if self._removal_request_after is not None
                    else true()
                )
                .order_by(Job.id)
                .limit(limit)
            ):
                observed_request = True
                self._removal_request_after = operation.id
                try:
                    payload = read_stored_model(
                        AvailabilityJobPayload,
                        canonical_message(operation.payload),
                        from_json=True,
                    )
                except (TypeError, ValueError):
                    continue
                if payload.cancellation is not None:
                    continue
                for gate in session.scalars(
                    select(ArtifactLifecycleGate).where(
                        ArtifactLifecycleGate.artifact_kind == "runtime-image",
                        ArtifactLifecycleGate.artifact_sha256.in_(
                            payload.removal_archives or ()
                        ),
                        ArtifactLifecycleGate.removal_owner_kind == "recipe-image-job",
                        ArtifactLifecycleGate.removal_owner_id.is_not(None),
                    )
                ):
                    accepted_requests.append(
                        (
                            operation.id,
                            ArtifactIdentity("runtime-image", gate.artifact_sha256),
                        )
                    )
            if not observed_request:
                self._removal_request_after = None
        changed = 0
        deadline = time.monotonic() + 0.25
        for request_id, identity in accepted_requests[:limit]:
            if time.monotonic() >= deadline:
                break

            def validate(
                requester: ModelCacheOperation | Job,
                remover: ModelCacheOperation | Job,
                fence: str,
                identity: ArtifactIdentity = identity,
            ) -> bool:
                if not isinstance(requester, Job) or not isinstance(remover, Job):
                    return False
                if (
                    requester.kind != OPERATION_KIND
                    or remover.kind != REMOVE_OPERATION_KIND
                ):
                    return False
                try:
                    payload = read_stored_model(
                        AvailabilityJobPayload,
                        canonical_message(requester.payload),
                        from_json=True,
                    )
                    owner = self._read_removal_owner(remover)
                except (TypeError, ValueError, RecipeImageAvailabilityError):
                    return False
                return (
                    payload.cancellation is None
                    and requester.authority_revision == payload.recipe_revision_id
                    and requester.targets == [payload.recipe_revision_id]
                    and payload.recipe_content_sha256
                    == document_sha256(payload.recipe.model_dump(mode="json"))
                    and identity.sha256 in (payload.removal_archives or ())
                    and identity.sha256 in owner.plan.image_archives
                    and owner.plan.intent.removal_fence == fence
                )

            def cancel(remover: ModelCacheOperation | Job, accepted_id: str) -> None:
                assert isinstance(remover, Job)
                self._lifecycle.supersede_removal(remover, accepted_id, self._clock())

            try:
                with (
                    self._storage.publication_lock(identity.sha256),
                    self._sessions.begin() as session,
                ):
                    changed += int(
                        supersede_removal_nowait(
                            session,
                            identity,
                            owner_kind="recipe-image-job",
                            request_id=request_id,
                            validate=validate,
                            cancel=cancel,
                            now=self._clock(),
                        )
                    )
            except (RuntimeImagePreparationError, ArtifactLifecycleError, OSError):
                continue
        return changed

    def reconcile_removal_gates(self, *, limit: int = 64) -> int:
        with self._sessions() as session:
            identities = dead_removal_identities(
                session,
                owner_kind="recipe-image-job",
                limit=limit,
                after=self._removal_gate_after,
            )
        if not identities:
            self._removal_gate_after = None
        released = 0
        deadline = time.monotonic() + 0.25
        for identity in identities:
            if time.monotonic() >= deadline:
                break
            self._removal_gate_after = (identity.kind, identity.sha256)
            if identity.kind != "runtime-image":
                continue
            try:
                with (
                    self._storage.publication_lock(identity.sha256),
                    self._sessions.begin() as session,
                ):
                    changed = release_dead_removal_nowait(
                        session,
                        identity,
                        owner_kind="recipe-image-job",
                        now=self._clock(),
                    )
                if changed:
                    released += 1
                    log_event(
                        _LOGGER,
                        "artifact.removal_gate_reconciled",
                        service="controller",
                        artifact_kind=identity.kind,
                        artifact_sha256=identity.sha256,
                    )
            except (
                RuntimeImagePreparationError,
                ArtifactLifecycleError,
                OSError,
            ) as error:
                log_event(
                    _LOGGER,
                    "artifact.removal_gate_deferred",
                    service="controller",
                    artifact_kind=identity.kind,
                    artifact_sha256=identity.sha256,
                    code=getattr(error, "code", type(error).__name__),
                )
                continue
        return released

    def advance_removals(self, *, limit: int = 1) -> int:
        """Advance bounded, durable cache removals without claiming image slots."""

        if not 1 <= limit <= 16:
            raise InvalidValue(
                "recipe removal batch limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        self.reconcile_requested_removals()
        self.reconcile_removal_gates()
        advanced = 0
        boundary: tuple[datetime, str] | None = None
        while advanced < limit:
            with self._sessions() as session:
                statement = (
                    select(Job.id, Job.updated_at)
                    .where(
                        Job.kind == REMOVE_OPERATION_KIND,
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.BACKOFF,
                            )
                        ),
                    )
                    .order_by(Job.updated_at, Job.id)
                    .limit(64)
                )
                if boundary is not None:
                    boundary_time, boundary_id = boundary
                    statement = statement.where(
                        or_(
                            Job.updated_at > boundary_time,
                            and_(
                                Job.updated_at == boundary_time,
                                Job.id > boundary_id,
                            ),
                        )
                    )
                rows = tuple(session.execute(statement))
            if not rows:
                break
            for operation_id, _updated_at in rows:
                try:
                    advanced += int(self._advance_recipe_removal(operation_id))
                except RecipeImageAvailabilityError as error:
                    if error.code == RecipeImageCode.OPERATION_INVALID:
                        # Preserve damaged intent and fence the executor by ending
                        # the owner. The storage-lock reconciler releases its gates.
                        with self._sessions.begin() as session:
                            damaged = session.scalar(
                                select(Job)
                                .where(Job.id == operation_id)
                                .with_for_update(skip_locked=True)
                            )
                            if (
                                damaged is not None
                                and damaged.state
                                in job_states.words(
                                    LifecycleState.QUEUED,
                                    LifecycleState.RUNNING,
                                    LifecycleState.BACKOFF,
                                )
                            ):
                                self._lifecycle.fail(
                                    damaged,
                                    self._clock(),
                                    retryable=False,
                                    reason=f"{error.code}: {error.detail}",
                                )
                    continue
                if advanced >= limit:
                    break
            last_updated, last_id = rows[-1][1], rows[-1][0]
            boundary = (last_updated, last_id)
        return advanced

    def _advance_recipe_removal(self, operation_id: str) -> bool:
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        with self._sessions() as session:
            operation = session.get(Job, operation_id, populate_existing=True)
            if (
                operation is None
                or operation.kind != REMOVE_OPERATION_KIND
                or operation.state
                not in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.BACKOFF,
                )
            ):
                return False
            owner = self._read_removal_owner(operation)
        if not _removal_retry_is_due(owner.checkpoint.failure, now):
            return False

        if owner.checkpoint.image_index < len(owner.plan.image_archives):
            archive_sha256 = owner.plan.image_archives[owner.checkpoint.image_index]
            return self._advance_recipe_image_removal(
                operation_id, owner, archive_sha256, now=now
            )
        if owner.checkpoint.model_index < len(owner.plan.model_children):
            return self._advance_recipe_model_child(
                operation_id,
                owner,
                owner.plan.model_children[owner.checkpoint.model_index],
                now=now,
            )
        return self._finish_recipe_removal(operation_id, owner, now=now)

    def _advance_recipe_image_removal(
        self,
        operation_id: str,
        observed_owner: RecipeCacheRemovalOwner,
        archive_sha256: str,
        *,
        now: datetime,
    ) -> bool:
        identity = ArtifactIdentity("runtime-image", archive_sha256)
        try:
            with self._storage.publication_lock(archive_sha256):
                observed_bytes = self._storage.published_archive_bytes(archive_sha256)
                pending_bytes = self._checkpoint_recipe_image_removal(
                    operation_id,
                    observed_owner,
                    identity=identity,
                    observed_bytes=observed_bytes,
                    now=now,
                )
                if pending_bytes is None:
                    return False
                reclaimed_now = self._storage.remove_published(archive_sha256)
                if reclaimed_now not in {0, pending_bytes}:
                    raise RuntimeImagePreparationUnknown(
                        RuntimeImageCode.ARCHIVE_MISMATCH,
                        "published image length changed during the fenced removal",
                    )
                return self._complete_recipe_image_removal(
                    operation_id,
                    observed_owner,
                    identity=identity,
                    pending_bytes=pending_bytes,
                    now=self._clock(),
                )
        except RuntimeImagePreparationError as error:
            return self._record_recipe_removal_failure(
                operation_id,
                code=error.code,
                detail=error.detail,
                retryable=error.retryable,
                retry_after_seconds=5,
            )
        except ArtifactLifecycleError as error:
            return self._record_recipe_removal_failure(
                operation_id,
                code=error.code,
                detail=error.detail,
                retryable=error.retryable,
                retry_after_seconds=5,
            )
        except RecipeImageAvailabilityError as error:
            return self._record_recipe_removal_failure(
                operation_id,
                code=error.code,
                detail=error.detail,
                retryable=_retryable(error),
                retry_after_seconds=error.retry_after_seconds or 5,
            )
        except DBAPIError as error:
            translated = retryable_artifact_database_error(error)
            if translated is None:
                raise
            return self._record_recipe_removal_failure(
                operation_id,
                code=translated.code,
                detail=translated.detail,
                retryable=True,
                retry_after_seconds=5,
            )

    def _checkpoint_recipe_image_removal(
        self,
        operation_id: str,
        observed_owner: RecipeCacheRemovalOwner,
        *,
        identity: ArtifactIdentity,
        observed_bytes: int,
        now: datetime,
    ) -> int | None:
        intent = observed_owner.plan.intent
        with self._sessions.begin() as session:
            if not check_removal_fence_nowait(
                session,
                identity,
                owner_kind="recipe-image-job",
                owner_id=operation_id,
                fence=intent.removal_fence,
            ):
                return None
            operation = session.scalar(
                select(Job)
                .where(Job.id == operation_id, Job.kind == REMOVE_OPERATION_KIND)
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
            if operation is None or operation.state not in job_states.words(
                LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF
            ):
                return None
            owner = self._read_removal_owner(operation)
            checkpoint = owner.checkpoint
            if (
                owner.plan.intent != intent
                or checkpoint.image_index != observed_owner.checkpoint.image_index
            ):
                return None
            references = runtime_image_reference_reasons(session, (identity.sha256,))
            if references[identity.sha256]:
                raise RecipeImageAvailabilityUnknown(
                    ArtifactLifecycleCode.REFERENCE_CHANGED,
                    "runtime image acquired an active reference while removal was reserved",
                    retryable=True,
                    recovery_actions=("retry",),
                    reason=WaitReason.SCOPE_CHANGED,
                )
            pending_bytes = checkpoint.image_pending_bytes
            if pending_bytes is None:
                pending_bytes = observed_bytes
            elif observed_bytes not in {0, pending_bytes}:
                # Our own checkpoint is stale; remove what is stored now.
                pending_bytes = observed_bytes
            updated_checkpoint = checkpoint.model_copy(
                update={"image_pending_bytes": pending_bytes, "failure": None}
            )
            updated_owner = owner.model_copy(update={"checkpoint": updated_checkpoint})
            operation.payload = serialize_json_value(updated_owner)
            self._lifecycle.advance_removal(operation, now)
            return pending_bytes

    def _complete_recipe_image_removal(
        self,
        operation_id: str,
        observed_owner: RecipeCacheRemovalOwner,
        *,
        identity: ArtifactIdentity,
        pending_bytes: int,
        now: datetime,
    ) -> bool:
        intent = observed_owner.plan.intent
        with self._sessions.begin() as session:
            if not check_removal_fence_nowait(
                session,
                identity,
                owner_kind="recipe-image-job",
                owner_id=operation_id,
                fence=intent.removal_fence,
            ):
                return False
            operation = session.scalar(
                select(Job)
                .where(Job.id == operation_id, Job.kind == REMOVE_OPERATION_KIND)
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
            if operation is None or operation.state not in job_states.words(
                LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF
            ):
                return False
            owner = self._read_removal_owner(operation)
            checkpoint = owner.checkpoint
            if (
                owner.plan.intent != intent
                or checkpoint.image_index != observed_owner.checkpoint.image_index
                or checkpoint.image_pending_bytes != pending_bytes
            ):
                return False
            updated_checkpoint = checkpoint.model_copy(
                update={
                    "image_index": checkpoint.image_index + 1,
                    "image_pending_bytes": None,
                    "image_reclaimed_bytes": checkpoint.image_reclaimed_bytes
                    + pending_bytes,
                    "failure": None,
                }
            )
            updated_owner = owner.model_copy(update={"checkpoint": updated_checkpoint})
            operation.payload = serialize_json_value(updated_owner)
            self._lifecycle.advance_removal(operation, now)
            return True

    def _advance_recipe_model_child(
        self,
        operation_id: str,
        owner: RecipeCacheRemovalOwner,
        child: RecipeCacheRemovalModelChild,
        *,
        now: datetime,
    ) -> bool:
        if self._model_cache is None:
            return self._record_recipe_removal_failure(
                operation_id,
                code=ModelCacheCode.UNAVAILABLE,
                detail="model cache child status is unavailable",
                retryable=True,
                retry_after_seconds=5,
            )
        cache = cast(ModelCacheRemovalCoordinator, self._model_cache)
        try:
            operation = cache.get_operation(child.operation_id)
        except ModelCacheNotFound:
            return self._record_recipe_removal_failure(
                operation_id,
                code=ModelCacheCode.REMOVAL_CHILD_MISSING,
                detail="accepted model-removal child is unavailable",
                retryable=False,
            )
        except ModelCacheError as error:
            return self._record_recipe_removal_failure(
                operation_id,
                code=ModelCacheCode.REMOVAL_CHILD_INVALID,
                detail=error.detail,
                retryable=False,
            )
        except DBAPIError as error:
            translated = retryable_artifact_database_error(error)
            if translated is None:
                raise
            return self._record_recipe_removal_failure(
                operation_id,
                code=translated.code,
                detail=translated.detail,
                retryable=True,
                retry_after_seconds=5,
            )
        if (
            operation.id != child.operation_id
            or operation.request_key != child.request_key
            or operation.kind != "remove"
            or operation.plan_digest != child.plan_digest
        ):
            return self._record_recipe_removal_failure(
                operation_id,
                code=ModelCacheCode.REMOVAL_CHILD_MISMATCH,
                detail="model-removal child identity does not match its accepted recipe owner",
                retryable=False,
            )
        if operation.state in model_cache_states.LIVE:
            return self._record_recipe_removal_failure(
                operation_id,
                code=ModelCacheCode.REMOVAL_CHILD_PENDING,
                detail="waiting for the accepted model-removal child to finish",
                retryable=True,
                retry_after_seconds=5,
            )
        if operation.state != "succeeded":
            return self._record_recipe_removal_failure(
                operation_id,
                code=ModelCacheCode.REMOVAL_CHILD_FAILED,
                detail="accepted model-removal child did not complete successfully",
                retryable=False,
            )
        result = (
            operation.result
            if isinstance(operation.result, ModelCacheRemovalResult)
            else read_row_column(operation, "result")
        )
        if not isinstance(result, ModelCacheRemovalResult):
            return self._record_recipe_removal_failure(
                operation_id,
                code=ModelCacheCode.REMOVAL_CHILD_INVALID,
                detail="successful model-removal child has an invalid result",
                retryable=False,
            )
        if result.removed_entries != child.selected_sets or result.cancelled_operations:
            return self._record_recipe_removal_failure(
                operation_id,
                code=ModelCacheCode.REMOVAL_CHILD_MISMATCH,
                detail="model-removal child result does not match its accepted scope",
                retryable=False,
            )
        try:
            return self._complete_recipe_model_child(
                operation_id,
                owner,
                child,
                reclaimed_bytes=result.reclaimed_bytes,
                now=now,
            )
        except DBAPIError as error:
            translated = retryable_artifact_database_error(error)
            if translated is None:
                raise
            return self._record_recipe_removal_failure(
                operation_id,
                code=translated.code,
                detail=translated.detail,
                retryable=True,
                retry_after_seconds=5,
            )

    def _complete_recipe_model_child(
        self,
        operation_id: str,
        observed_owner: RecipeCacheRemovalOwner,
        child: RecipeCacheRemovalModelChild,
        *,
        reclaimed_bytes: int,
        now: datetime,
    ) -> bool:
        with self._sessions.begin() as session:
            operation = session.scalar(
                select(Job)
                .where(Job.id == operation_id, Job.kind == REMOVE_OPERATION_KIND)
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
            if operation is None or operation.state not in job_states.words(
                LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF
            ):
                return False
            owner = self._read_removal_owner(operation)
            checkpoint = owner.checkpoint
            if (
                owner.plan.intent != observed_owner.plan.intent
                or checkpoint.model_index != observed_owner.checkpoint.model_index
                or owner.plan.model_children[checkpoint.model_index] != child
                or checkpoint.image_index != len(owner.plan.image_archives)
            ):
                return False
            updated_checkpoint = checkpoint.model_copy(
                update={
                    "model_index": checkpoint.model_index + 1,
                    "model_reclaimed_bytes": checkpoint.model_reclaimed_bytes
                    + reclaimed_bytes,
                    "failure": None,
                }
            )
            updated_owner = owner.model_copy(update={"checkpoint": updated_checkpoint})
            operation.payload = serialize_json_value(updated_owner)
            self._lifecycle.advance_removal(operation, now)
            return True

    def _record_recipe_removal_failure(
        self,
        operation_id: str,
        *,
        code: str,
        detail: str,
        retryable: bool,
        retry_after_seconds: int = 5,
    ) -> bool:
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        try:
            with self._sessions.begin() as session:
                operation = session.scalar(
                    select(Job)
                    .where(Job.id == operation_id, Job.kind == REMOVE_OPERATION_KIND)
                    .execution_options(populate_existing=True)
                    .with_for_update(nowait=True)
                )
                if operation is None or operation.state not in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.BACKOFF,
                ):
                    return False
                owner = self._read_removal_owner(operation)
                checkpoint = owner.checkpoint
                retry_attempts = checkpoint.retry_attempts
                retry_time: str | None = None
                delay: int | None = None
                if retryable:
                    retry_attempts += 1
                    # The core's bounded backoff decides when; the owner's
                    # delay is only the floor (one retry clock).
                    ended = self._lifecycle.fail(
                        operation,
                        now,
                        retryable=True,
                        reason="recipe removal will be retried",
                        retry_after=now + timedelta(seconds=retry_after_seconds),
                        count=retry_attempts - 1,
                    )
                    due = ended.next_action_at or now
                    delay = max(0, int((due - now).total_seconds() + 0.999))
                    retry_time = _iso(due)
                safe = sanitize_failure_evidence({"code": code, "detail": detail})
                failure = read_stored_model(
                    AvailabilityOperationFailure,
                    {
                        "code": safe.get("code", RecipeImageCode.REMOVAL_FAILED),
                        "detail": safe.get("detail", "recipe removal did not complete"),
                        "recovery_actions": [] if retryable else ["inspect"],
                        "retryable": retryable,
                        "retry_time": retry_time,
                        "retry_after_seconds": delay,
                    },
                )
                updated_checkpoint = checkpoint.model_copy(
                    update={"retry_attempts": retry_attempts, "failure": failure}
                )
                operation.payload = serialize_json_value(
                    owner.model_copy(update={"checkpoint": updated_checkpoint})
                )
                if not retryable:
                    self._lifecycle.fail(
                        operation,
                        now,
                        retryable=False,
                        reason=failure.detail,
                        count=retry_attempts,
                    )
                operation.status_reason = failure.detail
                return True
        except DBAPIError as error:
            translated = retryable_artifact_database_error(error)
            if translated is None:
                raise
            return False

    def _finish_recipe_removal(
        self,
        operation_id: str,
        observed_owner: RecipeCacheRemovalOwner,
        *,
        now: datetime,
    ) -> bool:
        intent = observed_owner.plan.intent
        identities = tuple(
            ArtifactIdentity("runtime-image", digest)
            for digest in observed_owner.plan.image_archives
        )
        try:
            with self._sessions.begin() as session:
                if identities and not lock_removal_fences(
                    session,
                    identities,
                    owner_kind="recipe-image-job",
                    owner_id=operation_id,
                    fence=intent.removal_fence,
                    now=now,
                ):
                    return False
                operation = session.scalar(
                    select(Job)
                    .where(Job.id == operation_id, Job.kind == REMOVE_OPERATION_KIND)
                    .execution_options(populate_existing=True)
                    .with_for_update(nowait=True)
                )
                if operation is None or operation.state not in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.BACKOFF,
                ):
                    return False
                owner = self._read_removal_owner(operation)
                checkpoint = owner.checkpoint
                if (
                    owner.plan.intent != intent
                    or checkpoint.image_index != len(owner.plan.image_archives)
                    or checkpoint.image_pending_bytes is not None
                    or checkpoint.model_index != len(owner.plan.model_children)
                    or not _removal_retry_is_due(checkpoint.failure, now)
                ):
                    return False
                intent_archives = tuple(owner.plan.image_archives)
                references = runtime_image_reference_reasons(session, intent_archives)
                if any(references[digest] for digest in intent_archives):
                    raise RecipeImageAvailabilityUnknown(
                        ArtifactLifecycleCode.REFERENCE_CHANGED,
                        "a runtime image reference remains after the removal effects",
                        retryable=True,
                        recovery_actions=("retry",),
                        reason=WaitReason.SCOPE_CHANGED,
                    )
                if identities:
                    clear_removal(
                        session,
                        identities,
                        owner_kind="recipe-image-job",
                        owner_id=operation_id,
                        fence=intent.removal_fence,
                        now=now,
                    )
                result = RecipeCacheRemovalResult(
                    schema_version=SCHEMA_VERSION,
                    action="remove",
                    selector=intent.selector,
                    request_key=intent.request_key,
                    review_digest=intent.review_digest,
                    operation_id=operation.id,
                    recipe_revision_id=intent.recipe_revision_id,
                    with_model=intent.with_model,
                    state="succeeded",
                    reclaimed_bytes=(
                        checkpoint.image_reclaimed_bytes
                        + checkpoint.model_reclaimed_bytes
                    ),
                    model_removals=[
                        child.operation_id for child in owner.plan.model_children
                    ],
                    preserved=["profile-assignments", "spark-local-copies"]
                    + ([] if intent.with_model else ["model-download"]),
                    cancelled_operations=[],
                    cancelled_builds=[],
                    next_actions=[],
                )
                operation.result = serialize_json_value(result)
                self._lifecycle.succeed(operation, now)
                updated_owner = owner.model_copy(
                    update={
                        "checkpoint": checkpoint.model_copy(update={"failure": None})
                    }
                )
                operation.payload = serialize_json_value(updated_owner)
                return True
        except ArtifactLifecycleError as error:
            return self._record_recipe_removal_failure(
                operation_id,
                code=error.code,
                detail=error.detail,
                retryable=error.retryable,
                retry_after_seconds=5,
            )
        except RecipeImageAvailabilityError as error:
            return self._record_recipe_removal_failure(
                operation_id,
                code=error.code,
                detail=error.detail,
                retryable=_retryable(error),
                retry_after_seconds=error.retry_after_seconds or 5,
            )
        except DBAPIError as error:
            translated = retryable_artifact_database_error(error)
            if translated is None:
                raise
            return self._record_recipe_removal_failure(
                operation_id,
                code=translated.code,
                detail=translated.detail,
                retryable=True,
                retry_after_seconds=5,
            )

    def update(
        self,
        *,
        actor: str,
        request_id: str,
        selectors: list[str] | None,
        all: bool,
    ) -> RecipeUpdateResponse:
        """Accept one durable exact update scope before issuing child work."""
        return self._updates.start(
            actor=actor, request_id=request_id, selectors=selectors, all=all
        )

    def cancel(
        self,
        operation_id: str,
        *,
        actor: str,
        request_id: str,
        reason: str,
    ) -> RecipeImageAvailabilityView | RecipeUpdateResponse:
        """Persist a current, authorized cancellation request for one Recipe job."""

        with self._sessions() as session:
            operation = session.get(Job, operation_id)
            if operation is None:
                raise MissingRecord(operation_id)
            kind = operation.kind
        if kind == UPDATE_KIND:
            try:
                return self._updates.cancel(
                    operation_id, actor=actor, request_id=request_id, reason=reason
                )
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "55P03":
                    raise
                raise RecipeImageAvailabilityUnknown(
                    RecipeImageCode.CANCEL_BUSY,
                    "recipe cancellation is changing; retry with the same request key",
                    retryable=True,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
        if kind != OPERATION_KIND:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.NOT_CANCELLABLE,
                "operation is not a current recipe preparation",
            )
        try:
            with self._sessions.begin() as session:
                job = session.scalar(
                    select(Job)
                    .where(Job.id == operation_id, Job.kind == OPERATION_KIND)
                    .with_for_update(nowait=True)
                )
                if job is None:
                    raise MissingRecord(operation_id)
                self._request_cancellation(
                    session,
                    job,
                    actor=actor,
                    request_id=request_id,
                    reason=reason,
                    authorize=True,
                )
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "55P03":
                raise
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.CANCEL_BUSY,
                "recipe cancellation is changing; retry with the same request key",
                retryable=True,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        return self.get(operation_id)

    def _request_cancellation(
        self,
        session: Session,
        job: Job,
        *,
        actor: str,
        request_id: str,
        reason: str,
        authorize: bool,
    ) -> RecipeOperationCancellationResult | Residue:
        if job.kind not in {OPERATION_KIND, UPDATE_KIND}:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.NOT_CANCELLABLE,
                "operation is not a current recipe preparation",
            )
        if authorize:
            self._updates._authorize(session, actor)
        cancellation_id = _canonical_cancellation_id(request_id)
        normalized_reason = " ".join(reason.split()) if isinstance(reason, str) else ""
        if not normalized_reason or len(normalized_reason) > 512:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.CANCELLATION_INVALID,
                "cancellation reason must contain 1 to 512 normalized characters",
            )
        current = self._stored_cancellation(job)
        if current is not None:
            if (
                current.cancel_request_id == cancellation_id
                and current.cancel_actor == actor
                and current.reason == normalized_reason
            ):
                return current
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.CANCEL_REQUEST_KEY_REUSED,
                "operation already has a different cancellation request",
                reason=InvalidRequestReason.CONFLICT,
            )
        if job.state not in job_states.words(
            LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF
        ):
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.NOT_CANCELLABLE,
                "recipe operation is no longer active",
            )
        used = session.scalar(
            select(Job.id)
            .where(
                Job.id != job.id,
                or_(
                    Job.request_id == cancellation_id,
                    Job.payload["cancellation"]["cancel_request_id"].as_string()
                    == cancellation_id,
                ),
            )
            .limit(1)
        )
        if used is not None:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.CANCEL_REQUEST_KEY_REUSED,
                "cancellation request key was already used",
                reason=InvalidRequestReason.CONFLICT,
            )
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        cancellation = RecipeOperationCancellationResult(
            cancel_requested=True,
            cancel_requested_at=now,
            cancel_request_id=cancellation_id,
            cancel_actor=actor,
            reason=normalized_reason,
        )
        if job.kind == UPDATE_KIND:
            document = self._updates._document(job).model_copy(
                update={"cancellation": cancellation}
            )
            job.payload = serialize_json_value(document)
        else:
            payload = self._payload(job)
            if isinstance(payload, Residue):
                return payload
            payload = payload.model_copy(update={"cancellation": cancellation})
            job.payload = serialize_json_value(payload)
        # Rule 4 through the core: work that never ran and holds nothing ends
        # ``cancelled`` at once; anything else is ``cancelling`` until the
        # reconcile pass has released it (or its budget is spent).
        self._lifecycle.request_cancel(job, cancellation_id, normalized_reason, now)
        return cancellation

    def _stored_cancellation(
        self, job: Job
    ) -> RecipeOperationCancellationResult | None:
        if job.kind == UPDATE_KIND:
            return self._updates._document(job).cancellation
        payload = self._payload(job)
        if isinstance(payload, AvailabilityJobPayload):
            return payload.cancellation
        # Unreadable unrelated fields must not erase a valid cancellation.
        identity = _read(StoredAvailabilityIdentity, job.payload, subject=job.id)
        return identity.cancellation if identity else None

    def _cancel_update_child(
        self, operation_id: str, cancellation: RecipeOperationCancellationResult
    ) -> RecipeImageAvailabilityView | None:
        """Apply an update parent's already-authorized intent to one child."""

        with self._sessions.begin() as session:
            job = session.scalar(
                select(Job)
                .where(Job.id == operation_id, Job.kind == OPERATION_KIND)
                .with_for_update(nowait=True)
            )
            if job is None:
                return None
            current = self._stored_cancellation(job)
            if current is None:
                if job.state not in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.BACKOFF,
                ):
                    return self._view(job)
                self._request_cancellation(
                    session,
                    job,
                    actor=cancellation.cancel_actor,
                    request_id=cancellation.cancel_request_id,
                    reason=cancellation.reason,
                    authorize=False,
                )
            elif (
                current.cancel_request_id != cancellation.cancel_request_id
                and job.state != "cancelled"
            ):
                # A separately accepted child cancellation owns this child.
                # Observe it and let the parent's durable reconciliation wait.
                return self._view(job)
        return self.get(operation_id)

    def reconcile_cancellations(self, *, limit: int = 8) -> int:
        """Reconcile accepted cancellation fences without claiming image slots."""

        if not 1 <= limit <= 100:
            raise InvalidValue(
                "cancellation reconciliation limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        progressed = self._updates.reconcile_cancellations(limit=limit)
        with self._sessions() as session:
            operation_ids = tuple(
                session.scalars(
                    select(Job.id)
                    .where(
                        Job.kind == OPERATION_KIND,
                        Job.state.in_(job_states.words(LifecycleState.OBSERVING)),
                    )
                    .order_by(Job.updated_at, Job.id)
                    .limit(limit)
                )
            )
        for operation_id in operation_ids:
            progressed += int(self._reconcile_availability_cancellation(operation_id))
        return progressed

    def _model_child_cancellation_pending(
        self,
        operation_id: str,
        payload: AvailabilityJobPayload,
        request_id: str,
        cancellation: RecipeOperationCancellationResult,
    ) -> tuple[bool, bool]:
        """Stop only a ModelCache child whose exact request belongs here.

        The ModelCache operation row serializes consumer registration and
        detachment. A concurrent parent must register under the same row lock
        before it relies on the child; if cancellation wins, that parent sees
        the child fence and starts its own fresh request.
        """

        if self._model_cache is None:
            return False, False
        child = payload.model_child
        child_id = child.id if child else None
        recipe_revision_id = payload.recipe_revision_id
        request_keys = {
            str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"vonk:recipe-availability-model:{recipe_revision_id}:{request_id}",
                )
            )
        }
        artifact_set = child.artifact_set_sha256 if child else None
        if isinstance(artifact_set, str):
            request_keys.add(
                str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"vonk:recipe-availability-model-repair:{artifact_set}:{request_id}",
                    )
                )
            )
        if isinstance(child_id, str):
            request_keys.add(
                str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"vonk:recipe-availability-model-retry:{child_id}:{request_id}",
                    )
                )
            )
        cancelled_child_id: str | None = None
        try:
            with self._sessions.begin() as session:
                # Prefer the exact child already checkpointed by this parent.
                # A linked child may be a transfer owned by another request;
                # in that case still look for an exact request key that this
                # parent issued before a crash but had not linked yet.
                filters: list[ColumnElement[bool]] = [
                    ModelCacheOperation.request_key.in_(sorted(request_keys))
                ]
                if isinstance(child_id, str):
                    filters.append(ModelCacheOperation.id == child_id)
                candidates = list(
                    session.scalars(
                        select(ModelCacheOperation)
                        .where(or_(*filters))
                        .order_by(ModelCacheOperation.id)
                    )
                )
                candidate = next(
                    (
                        item
                        for item in candidates
                        if item.id == child_id
                        and item.request_key in request_keys
                        and item.state in model_cache_states.ACTIVE
                    ),
                    None,
                )
                if candidate is None:
                    candidate = next(
                        (
                            item
                            for item in candidates
                            if item.request_key in request_keys
                            and item.state in model_cache_states.ACTIVE
                        ),
                        None,
                    )
                if candidate is None:
                    return False, False
                model_child = session.scalar(
                    select(ModelCacheOperation)
                    .where(ModelCacheOperation.id == candidate.id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
                if (
                    model_child is None
                    or model_child.request_key not in request_keys
                    or model_child.state not in model_cache_states.ACTIVE
                ):
                    return False, False
                shared = session.scalar(
                    select(Job.id)
                    .where(
                        Job.id != operation_id,
                        Job.kind == OPERATION_KIND,
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.BACKOFF,
                            )
                        ),
                        Job.payload["model_child"]["id"].as_string() == model_child.id,
                    )
                    .limit(1)
                )
                if shared is not None:
                    return False, False
                if model_child.kind != "download":
                    # The current ModelCache cancellation contract owns
                    # downloads. Keep the parent pending until another
                    # supported owner effect settles instead of inventing a
                    # repair-cancellation path here.
                    return True, False
                cache = cast(ModelCacheCancellationOwner, self._model_cache)
                child_cancel_request_key = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "vonk:recipe-availability-model-cancel:"
                        f"{operation_id}:{cancellation.cancel_request_id}:"
                        f"{model_child.id}",
                    )
                )
                changed = cache.cancel_operation_in_session(
                    session,
                    model_child.id,
                    actor=cancellation.cancel_actor,
                    request_key=child_cancel_request_key,
                    reason=cancellation.reason,
                )
                cancelled_child_id = model_child.id
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "55P03":
                return True, False
            raise
        if cancelled_child_id is not None:
            cache = cast(ModelCacheCancellationOwner, self._model_cache)
            cache.signal_cancelled_operation(cancelled_child_id)
            # The child may still hold an artifact lock in another process.
            # Its durable W11 cancellation remains pending until that exact
            # effect releases the lock; the recipe parent must wait too.
            child = cache.get_operation(cancelled_child_id)
            return child.state != "cancelled", changed
        return False, changed

    def _build_child_cancellation_pending(
        self,
        operation_id: str,
        payload: AvailabilityJobPayload,
        current_attempt: int,
        cancellation: RecipeOperationCancellationResult,
    ) -> bool:
        dependency = payload.build_dependency
        request_key = str(dependency.request_key) if dependency else None
        child_operation_id = (
            str(dependency.operation_id)
            if dependency and dependency.operation_id
            else None
        )
        if not isinstance(request_key, str):
            request_key = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"vonk:recipe-image-build:{operation_id}:{current_attempt}",
                )
            )
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        try:
            with self._sessions.begin() as session:
                child_query = select(Job).where(Job.kind == "recipe.build.v1")
                if isinstance(child_operation_id, str):
                    child_query = child_query.where(Job.id == child_operation_id)
                else:
                    child_query = child_query.where(Job.request_id == request_key)
                child = session.scalar(child_query)
                if child is None or child.state not in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.OBSERVING,
                    LifecycleState.BACKOFF,
                    LifecycleState.NEEDS_OPERATOR,
                ):
                    return False
                child_payload = read_row_column(child, "payload")
                owner_id = (
                    child_payload.owner_id
                    if isinstance(child_payload, RecipeBuildParent)
                    else None
                )
                if not isinstance(owner_id, str):
                    return True
                build = session.get(RecipeBuild, owner_id)
                if build is None:
                    return True
                recipe_revision_id = payload.recipe_revision_id
                build_input_sha256 = payload.build_input_sha256
                try:
                    locked = lock_build_dependency(
                        session,
                        recipe_revision_id=recipe_revision_id
                        if isinstance(recipe_revision_id, str)
                        else None,
                        builder_node_id=build.builder_node_id,
                        build_input_sha256=build_input_sha256
                        if isinstance(build_input_sha256, str)
                        else None,
                        build_id=build.id,
                        allow_cancelling=True,
                    )
                    if locked is None:
                        return True
                    consumers = current_build_consumers(session, locked)
                except BuildConsumerError:
                    # Unknown ownership cannot authorize releasing an issued build.
                    return True
                if consumers:
                    # Another accepted operation still depends on this exact
                    # build; detaching this parent must leave that work alone.
                    return False
                child = session.scalar(
                    select(Job)
                    .where(
                        Job.id == child.id,
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.OBSERVING,
                                LifecycleState.BACKOFF,
                                LifecycleState.NEEDS_OPERATOR,
                            )
                        ),
                    )
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
                if child is None:
                    return False
                child_payload = read_row_column(child, "payload")
                if (
                    not isinstance(child_payload, RecipeBuildParent)
                    or child_payload.owner_id != locked.id
                    or (
                        not isinstance(operation_id, str)
                        and child.request_id != request_key
                    )
                ):
                    return True
                request_build_cancellation(
                    child,
                    actor=cancellation.cancel_actor,
                    request_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            "vonk:recipe-availability-build-cancel:"
                            f"{operation_id}:{cancellation.cancel_request_id}:{request_key}",
                        )
                    ),
                    reason=cancellation.reason,
                    now=now,
                )
                return True
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "55P03":
                return True
            raise

    def _release_cancelled_claim(self, claim: RecipeImageAvailabilityClaim) -> bool:
        with self._sessions() as session:
            operation = session.get(Job, claim.operation_id)
            payload = self._payload(operation) if operation is not None else None
            if (
                operation is None
                or not isinstance(payload, AvailabilityJobPayload)
                or operation.kind != OPERATION_KIND
                or operation.state not in job_states.words(LifecycleState.OBSERVING)
                or operation.current_attempt != claim.execution_attempt
                or not isinstance(operation.payload, Mapping)
                or payload.claim_owner != claim.claim_owner
                or self._stored_cancellation(operation) is None
                or payload.removal_fence is not None
            ):
                return False
            snapshot = self._payload(operation)
            if isinstance(snapshot, Residue):
                return False
        try:
            reference = self._image_reference_intent_for_claim(snapshot, claim)
        except _AvailabilityClaimLost:
            return False
        lock = (
            self._storage.publication_lock(reference.oci_archive_sha256)
            if reference is not None
            else nullcontext()
        )
        try:
            with lock, self._sessions.begin() as session:
                operation = session.scalar(
                    select(Job)
                    .where(Job.id == claim.operation_id, Job.kind == OPERATION_KIND)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
                payload = self._payload(operation) if operation is not None else None
                if (
                    operation is None
                    or not isinstance(payload, AvailabilityJobPayload)
                    or operation.state not in job_states.words(LifecycleState.OBSERVING)
                    or operation.current_attempt != claim.execution_attempt
                    or not isinstance(operation.payload, Mapping)
                    or payload.claim_owner != claim.claim_owner
                    or payload.removal_fence is not None
                    or self._stored_cancellation(operation) is None
                ):
                    return False
                payload = self._payload(operation)
                if isinstance(payload, Residue):
                    return False
                try:
                    latest_reference = self._image_reference_intent_for_claim(
                        payload, claim
                    )
                except _AvailabilityClaimLost:
                    return False
                if latest_reference != reference:
                    # A publisher may have entered its guarded callback
                    # after our snapshot. Let the next pass claim its exact
                    # archive lock before releasing the owner.
                    return False
                payload = payload.model_copy(update={"image_reference_intent": None})
                payload = payload.model_copy(update={"claim_owner": None})
                payload = payload.model_copy(update={"claim_until": None})
                operation.payload = serialize_json_value(payload)
                operation.updated_at = self._clock()
                return True
        except RuntimeImagePreparationError as error:
            if error.code == RuntimeImageCode.PUBLICATION_CONTENDED:
                return False
            raise
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "55P03":
                return False
            raise

    def _reconcile_availability_cancellation(self, operation_id: str) -> bool:
        changed = self._reconcile_cancellation_pass(operation_id)
        return self._end_spent_cancellation(operation_id) or changed

    def _end_spent_cancellation(self, operation_id: str) -> bool:
        """Rule 4: a cancel whose budget is spent ends ``cancelled``, effect unknown.

        Whatever is still outstanding (a lease, a child that does not stop) keeps
        its own lifecycle; the claim is fenced by the ended state.
        """

        now = self._clock()
        try:
            with self._sessions.begin() as session:
                current = session.scalar(
                    select(Job)
                    .where(Job.id == operation_id, Job.kind == OPERATION_KIND)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
                if current is None or current.state not in job_states.words(
                    LifecycleState.OBSERVING
                ):
                    return False
                row = self._lifecycle.lifecycle(current, now)
                if row.observe_count < STOP_BUDGET:
                    return False
                ended = self._lifecycle.settle_cancel(current, now, outstanding=True)
                return ended.terminal
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "55P03":
                return False
            raise

    def _reconcile_cancellation_pass(self, operation_id: str) -> bool:
        with self._sessions() as session:
            operation = session.get(Job, operation_id)
            if (
                operation is None
                or operation.kind != OPERATION_KIND
                or operation.state not in job_states.words(LifecycleState.OBSERVING)
            ):
                return False
            cancellation = self._stored_cancellation(operation)
            payload = self._payload(operation)
            if isinstance(payload, Residue):
                return False
            request_id = operation.request_id
            current_attempt = int(operation.current_attempt)
        if cancellation is None:
            return False
        model_pending, child_changed = self._model_child_cancellation_pending(
            operation_id, payload, request_id, cancellation
        )
        build_pending = self._build_child_cancellation_pending(
            operation_id, payload, current_attempt, cancellation
        )
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        owner = payload.claim_owner
        if owner is not None:
            if not isinstance(owner, str):
                return child_changed
            if payload.image_reference_intent is None:
                until = payload.claim_until
                if until is None:
                    return child_changed
                if until > now:
                    # A valid lease may still be transferring bytes. Give its
                    # exact owner the chance to retain a verified receipt under
                    # the archive lock; expiry later fences callbacks in SQL.
                    return child_changed
            recipe_revision_id = operation.authority_revision
            if not isinstance(recipe_revision_id, str):
                return child_changed
            build_input_sha256 = payload.build_input_sha256
            claim = RecipeImageAvailabilityClaim(
                operation_id=operation_id,
                recipe_revision_id=recipe_revision_id,
                build_input_sha256=(
                    build_input_sha256 if isinstance(build_input_sha256, str) else None
                ),
                claim_owner=owner,
                execution_attempt=current_attempt,
            )
            if not self._release_cancelled_claim(claim):
                return child_changed
        elif payload.image_reference_intent is not None:
            # An intent without its exact claim owner is malformed and cannot
            # be treated as a stale, harmless record.
            return child_changed
        if model_pending or build_pending:
            return child_changed
        with self._sessions.begin() as session:
            current = session.scalar(
                select(Job)
                .where(Job.id == operation_id, Job.kind == OPERATION_KIND)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
            if current is None or current.state not in job_states.words(
                LifecycleState.OBSERVING
            ):
                return child_changed
            current_cancellation = self._stored_cancellation(current)
            if (
                current_cancellation is None
                or current_cancellation.cancel_request_id
                != cancellation.cancel_request_id
            ):
                return child_changed
            current_payload = self._payload(current)
            if isinstance(current_payload, Residue):
                return False
            if (
                current_payload.claim_owner is not None
                or current_payload.image_reference_intent is not None
                or current_payload.removal_fence is not None
            ):
                return child_changed
            self._lifecycle.settle_cancel(current, now, outstanding=False)
            current.status_reason = cancellation.reason
            return True

    def claim_update(self, owner: str) -> RecipeUpdateClaim | None:
        return self._updates.claim(owner)

    def run_update_claim(self, claim: RecipeUpdateClaim) -> None:
        self._updates.run(claim)

    def update_activity_provider(self):
        return self._updates.activity_provider()

    def start(
        self,
        recipe_revision_id: str,
        *,
        actor: str,
        request_id: str,
        model_digest: str | None = None,
        build_input_sha256: str | None = None,
        effective_execution_key: str | None = None,
        force: bool = False,
        force_rebuild: bool = False,
    ) -> RecipeImageAvailabilityView:
        """Refresh metadata and queue one exact selected Recipe operation."""

        if not isinstance(recipe_revision_id, str) or not recipe_revision_id.strip():
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.RECIPE_INVALID, "recipe revision is required"
            )
        if force and force_rebuild:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.ACTION_INVALID,
                "force cannot be combined with an explicit image action",
            )
        model_digest = _optional_digest(model_digest, field="model_digest")
        build_input_sha256 = _optional_digest(
            build_input_sha256, field="build_input_sha256"
        )
        effective_execution_key = _optional_digest(
            effective_execution_key, field="effective_execution_key"
        )
        intent = RecipeRevisionIntent(
            recipe_revision_id=recipe_revision_id,
            force=force,
            force_rebuild=force_rebuild,
            model_digest=model_digest,
            build_input_sha256=build_input_sha256,
            effective_execution_key=effective_execution_key,
        )
        return self._start_request(intent, actor=actor, request_id=request_id)

    def cancel_profile_preparation(
        self,
        recipe_revision_id: str,
        *,
        actor: str,
        reason: str,
        application_id: str | None = None,
    ) -> tuple[str, ...]:
        """Cancel the pending preparation a profile load asked for, if any.

        Only the chain of deterministic requests ``ensure_preparation`` makes
        is touched. The build layer still refuses to cancel an image another
        accepted consumer needs.
        """

        namespace = uuid.NAMESPACE_URL
        request_id = str(
            uuid.uuid5(
                namespace,
                f"vonk-forge:profile-preparation:{recipe_revision_id}"
                + (f":{application_id}" if application_id else ""),
            )
        )
        cancelled: list[str] = []
        for _ in range(_PREPARATION_CHAIN_LIMIT):
            with self._sessions.begin() as session:
                job = session.scalar(
                    select(Job)
                    .where(Job.kind == OPERATION_KIND, Job.request_id == request_id)
                    .with_for_update(nowait=True)
                )
                if job is None:
                    break
                if job.state in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.BACKOFF,
                ):
                    self._request_cancellation(
                        session,
                        job,
                        actor=actor,
                        request_id=str(
                            uuid.uuid5(
                                namespace,
                                f"vonk-forge:profile-preparation-cancel:{job.id}",
                            )
                        ),
                        reason=reason,
                        authorize=True,
                    )
                    cancelled.append(job.id)
                    break
                if job.state not in {"failed", "cancelled", *_SUCCEEDED}:
                    break
                request_id = str(
                    uuid.uuid5(
                        namespace,
                        f"vonk-forge:profile-preparation:{recipe_revision_id}:{job.id}",
                    )
                )
        return tuple(cancelled)

    def ensure_preparation(
        self, recipe_revision_id: str, *, actor: str, application_id: str | None = None
    ) -> tuple[OperationBlocker, ...]:
        """Start, or find, the preparation a load asks for; say what it waits on.

        One deterministic request identity per revision keeps repeated asks on
        the same durable operation. A preparation that ended for good is asked
        again only after a quiet period, so a hopeless one cannot spin.
        """

        namespace = uuid.NAMESPACE_URL
        request_id = str(
            uuid.uuid5(
                namespace,
                f"vonk-forge:profile-preparation:{recipe_revision_id}"
                + (f":{application_id}" if application_id else ""),
            )
        )
        for _ in range(_PREPARATION_CHAIN_LIMIT):
            view = self.start(recipe_revision_id, actor=actor, request_id=request_id)
            if view.state in _SUCCEEDED:
                # The caller asks only because its plan still lacks the image
                # or model, so an earlier success is stale (the image was
                # removed or the revision's build changed). Replaying it would
                # park the load for ever; ask again under a new identity,
                # paced so an evidence mismatch cannot spin.
                updated = datetime.fromisoformat(view.updated_at)
                updated = updated if updated.tzinfo else updated.replace(tzinfo=UTC)
                now = self._clock()
                now = now if now.tzinfo else now.replace(tzinfo=UTC)
                if now < updated + _PREPARATION_RECHECK_QUIET:
                    return (
                        make_blocker(
                            RecipeImageCode.PREPARING,
                            "Preparing finished a moment ago; checking that "
                            "the model and runtime image are in place.",
                            severity="info",
                        ),
                    )
                request_id = str(
                    uuid.uuid5(
                        namespace,
                        f"vonk-forge:profile-preparation:{recipe_revision_id}:{view.id}",
                    )
                )
                continue
            if view.state not in {"failed", "cancelled"}:
                break
            updated = datetime.fromisoformat(view.updated_at)
            updated = updated if updated.tzinfo else updated.replace(tzinfo=UTC)
            retry_at = updated + _PREPARATION_RETRY_QUIET
            now = self._clock()
            now = now if now.tzinfo else now.replace(tzinfo=UTC)
            if now < retry_at:
                failure = view.failure_evidence
                return (
                    make_blocker(
                        failure.code if failure else RecipeImageCode.PREPARATION_FAILED,
                        f"Preparing this recipe failed: "
                        f"{failure.detail if failure else view.state}. "
                        f"The Controller asks again after {retry_at.isoformat()}.",
                        severity="error",
                    ),
                )
            request_id = str(
                uuid.uuid5(
                    namespace,
                    f"vonk-forge:profile-preparation:{recipe_revision_id}:{view.id}",
                )
            )
        else:
            return (
                make_blocker(
                    RecipeImageCode.PREPARATION_EXHAUSTED,
                    "The bounded preparation attempts ended without the required assets.",
                    severity="error",
                ),
            )
        if view.state == "succeeded":
            return ()
        return view.blockers or (
            make_blocker(
                RecipeImageCode.PREPARING,
                f"Preparing the model and runtime image ({view.measurement.phase}).",
                severity="info",
            ),
        )

    def _refresh_authority(
        self, recipe_revision_id: str, *, force: bool
    ) -> tuple[RecipeDefinition, AvailabilityRuntime | None]:
        assert self._authority is not None
        try:
            recipe, runtime = self._authority(recipe_revision_id, force=force)
            parsed = _read(AvailabilityRuntime, runtime, subject=recipe_revision_id)
            return _canonical_recipe(recipe), parsed
        except RecipeImageAvailabilityError:
            raise
        except Exception as error:
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.METADATA_REFRESH_FAILED,
                "latest recipe metadata could not be refreshed",
                retryable=_retryable(error),
                retry_after_seconds=_retry_after(error),
                recovery_actions=("retry",) if _retryable(error) else ("inspect",),
            ) from error

    def _start_request(
        self,
        intent: RecipeSelectorIntent | RecipeRevisionIntent,
        *,
        actor: str,
        request_id: str,
        update_claim: RecipeUpdateClaim | None = None,
    ) -> RecipeImageAvailabilityView:
        existing = self._request_replay(request_id, actor=actor, intent=intent)
        if existing is not None:
            return existing
        if isinstance(intent, RecipeSelectorIntent):
            recipe_revision_id = self._resolve_recipe_selector(intent.selector)
            model_digest = build_input_sha256 = effective_execution_key = None
            force_rebuild = False
        else:
            recipe_revision_id = intent.recipe_revision_id
            model_digest = intent.model_digest
            build_input_sha256 = intent.build_input_sha256
            effective_execution_key = intent.effective_execution_key
            force_rebuild = intent.force_rebuild
        force = intent.force
        if self._authority is None:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.METADATA_REFRESH_UNAVAILABLE,
                "latest recipe metadata could not be refreshed",
                reason=InvalidRequestReason.NOT_FOUND,
            )
        raw_recipe, runtime = self._refresh_authority(recipe_revision_id, force=force)
        if isinstance(intent, RecipeSelectorIntent):
            # The refresh may have published a newer head; the selector means
            # the current revision, so follow it instead of reporting staleness.
            current_revision_id = self._resolve_recipe_selector(intent.selector)
            if current_revision_id != recipe_revision_id:
                recipe_revision_id = current_revision_id
                raw_recipe, runtime = self._refresh_authority(
                    recipe_revision_id, force=force
                )
        _canonical_recipe(raw_recipe)
        if runtime is None:
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.RUNTIME_INVALID,
                "selected recipe runtime projection is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        try:
            with self._sessions.begin() as session:
                if update_claim is not None:
                    if not isinstance(intent, RecipeRevisionIntent):
                        raise RecipeImageAvailabilityInvalid(
                            RecipeUpdateCode.OPERATION_INVALID,
                            "update child requires an exact revision",
                        )
                    self._updates.authorize_child(
                        session, update_claim, actor, request_id, intent
                    )
                revision = session.get(CatalogDocumentRevision, recipe_revision_id)
                if (
                    revision is None
                    or revision.kind != "recipe"
                    or revision.state != "active"
                ):
                    raise RecipeImageAvailabilityInvalid(
                        RecipeImageCode.RECIPE_UNAVAILABLE,
                        "selected recipe revision is unavailable or inactive",
                        reason=InvalidRequestReason.NOT_FOUND,
                    )
                if effective_execution_key is None:
                    effective_execution_key = revision.execution_key
                if effective_execution_key != revision.execution_key:
                    raise RecipeImageAvailabilityInvalid(
                        RecipeImageCode.IDENTITY_CONFLICT,
                        "selected recipe execution identity changed",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                if force:
                    force_rebuild = True
                runtime_build_input = runtime.build_input_sha256
                provisional_intent = runtime.input_intent_sha256
                if not isinstance(runtime_build_input, str) and not isinstance(
                    provisional_intent, str
                ):
                    raise RecipeImageAvailabilityUnknown(
                        RecipeImageCode.BUILD_INPUT_MISSING,
                        "authoritative runtime projection lacks the exact build input digest",
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                if isinstance(runtime_build_input, str):
                    runtime_build_input = _digest(
                        runtime_build_input, field="build_input_sha256"
                    )
                    if build_input_sha256 is None:
                        build_input_sha256 = runtime_build_input
                    elif build_input_sha256 != runtime_build_input:
                        raise RecipeImageAvailabilityInvalid(
                            RecipeImageCode.IDENTITY_CONFLICT,
                            "submitted build input does not match authoritative runtime metadata",
                            reason=InvalidRequestReason.CONFLICT,
                        )
                identity_key = build_input_sha256
                recipe = _canonical_recipe(revision.document)
                payload = AvailabilityJobPayload(
                    schema_version=SCHEMA_VERSION,
                    kind=OPERATION_KIND,
                    request=intent,
                    recipe_revision_id=recipe_revision_id,
                    recipe_content_sha256=revision.content_digest,
                    effective_execution_key=effective_execution_key,
                    model_digest=model_digest,
                    build_input_sha256=build_input_sha256,
                    identity_key=identity_key,
                    recipe=recipe,
                    runtime=runtime,
                    force_rebuild=force_rebuild,
                    progress=_progress("prepare", total_bytes=_known_total(runtime)),
                    retry=AvailabilityRetry(automatic_attempts=0, operator_retries=0),
                )
                encoded = canonical_message(payload)
                existing = session.scalar(
                    select(Job).where(Job.request_id == request_id)
                )
                if existing is not None:
                    return self._matching_request(existing, actor=actor, intent=intent)
                current_archives = tuple(
                    revision_archives(session, [recipe_revision_id])
                )
                # This accepted request owns a durable wait, not available bytes.
                # Gates still fence storage effects until the worker reconciles them.
                lock_reference_gates(
                    session,
                    (
                        ArtifactIdentity("runtime-image", archive)
                        for archive in current_archives
                    ),
                    now=self._clock(),
                )
                payload = payload.model_copy(
                    update={"removal_archives": list(current_archives)}
                )
                if has_pending_removal(
                    session,
                    (
                        ArtifactIdentity("runtime-image", archive)
                        for archive in current_archives
                    ),
                ):
                    payload = payload.model_copy(
                        update={
                            "blockers": [
                                make_blocker(
                                    ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                                    "Waiting for the prior image removal fence to settle",
                                    severity="info",
                                )
                            ]
                        }
                    )
                encoded = canonical_message(payload)
                self._lock_build_consumer(session, payload)
                now = self._clock()
                operation = self._lifecycle.new_job(
                    id=str(uuid.uuid4()),
                    request_id=request_id,
                    kind=OPERATION_KIND,
                    actor=actor,
                    authority_revision=recipe_revision_id,
                    targets=[recipe_revision_id],
                    payload_digest=hashlib.sha256(encoded).hexdigest(),
                    payload=serialize_json_value(payload),
                    result=None,
                    current_attempt=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(operation)
                session.flush()
                self._cancel_older_preparations(
                    session, newer_revision=revision, now=now
                )
                return self._view(operation)
        except IntegrityError:
            replay = self._request_replay(request_id, actor=actor, intent=intent)
            if replay is None:
                raise
            return replay

    @staticmethod
    def _lock_build_consumer(session: Session, payload: AvailabilityJobPayload) -> None:
        try:
            lock_availability_build_dependency(session, payload.model_dump(mode="json"))
        except BuildConsumerError as error:
            raise RecipeImageAvailabilityUnknown(
                error.code,
                str(error),
                retryable=error.retryable,
                recovery_actions=("retry",) if error.retryable else (),
            ) from error

    def _model_progress(self, value: object) -> OperationProgress:
        parsed = _read(ModelCacheOperationProgress, value)
        if parsed is None:
            return _progress(ProgressPhase.WAITING)
        result = _read(
            OperationProgress,
            project_cache_progress(parsed.model_dump(mode="json"), self._clock()),
        )
        return result if result is not None else _progress(ProgressPhase.WAITING)

    def _refresh_model_observation(
        self, child: AvailabilityModelChild, operation: ModelCacheOperationHandle
    ) -> AvailabilityModelChild:
        failure = (
            _read(AvailabilityOperationFailure, operation.failure)
            if operation.failure is not None
            else None
        )
        return AvailabilityModelChild.model_validate_json(
            canonical_message(
                child.model_dump(mode="json")
                | {
                    "id": operation.id,
                    "state": operation.state,
                    "progress": self._model_progress(operation.progress).model_dump(
                        mode="json"
                    ),
                    "artifact_set_sha256": operation.artifact_set_sha256,
                    "plan_digest": operation.plan_digest,
                    "failure": failure.model_dump(mode="json")
                    if failure is not None
                    else None,
                }
            )
        )

    def _ensure_model_child(
        self,
        recipe_revision_id: str,
        *,
        actor: str,
        parent_request_key: str,
    ) -> AvailabilityModelChild | None:
        """Queue one exact durable ModelCache child for the complete model set."""

        if self._model_cache is None:
            return None
        child_request_key = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"vonk:recipe-availability-model:{recipe_revision_id}:{parent_request_key}",
            )
        )
        try:
            preview = AvailabilityDownloadPreview.read(
                self._model_cache.download_preview(
                    recipe_revision_id=recipe_revision_id
                )
            )
            if preview is None:
                raise RecipeImageAvailabilityUnknown(
                    RecipeImageCode.MODEL_CACHE_INVALID,
                    "ModelCache returned an incomplete exact artifact plan",
                    retryable=True,
                    recovery_actions=("retry",),
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            plan_digest = preview.plan_digest
            artifact_set_sha256 = preview.artifact_set_sha256
            new_bytes = preview.new_bytes
            manifest = self._model_cache.resolve_artifact_set(
                recipe_revision_id=recipe_revision_id
            )
            manifest_document = _read(CacheManifest, manifest.document())
            if manifest_document is None:
                raise RecipeImageAvailabilityUnknown(
                    RecipeImageCode.MODEL_CACHE_INVALID,
                    "ModelCache returned an incomplete artifact manifest",
                    retryable=True,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            if manifest.digest != artifact_set_sha256:
                raise RecipeImageAvailabilityUnknown(
                    RecipeImageCode.MODEL_CACHE_INVALID,
                    "resolved model artifact identity changed during planning",
                    retryable=True,
                    recovery_actions=("retry",),
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            operation = None
            list_operations = getattr(self._model_cache, "list_operations", None)
            if list_operations is not None:
                candidates = [
                    candidate
                    for candidate in list_operations(limit=100)
                    if (
                        candidate.artifact_set_sha256 == artifact_set_sha256
                        and candidate.state
                        in {*model_cache_states.ACTIVE, "succeeded", "failed"}
                        and not (candidate.state == "succeeded" and new_bytes > 0)
                    )
                ]
                state_rank = {
                    "succeeded": 0,
                    "queued": 1,
                    "running": 1,
                    model_cache_states.BACKOFF: 1,
                    "failed": 2,
                }
                operation = min(
                    candidates,
                    key=lambda candidate: (
                        state_rank.get(candidate.state, 3),
                        str(candidate.id),
                    ),
                    default=None,
                )
                if operation is not None and operation.state == "failed":
                    failure = _read(AvailabilityOperationFailure, operation.failure)
                    actions = failure.recovery_actions if failure else []
                    if "download_again" in actions:
                        operation = self._start_model_repair(
                            operation,
                            actor=actor,
                            parent_request_key=parent_request_key,
                        )
            if operation is None:
                operation = self._model_cache.start_download(
                    actor=actor,
                    request_key=child_request_key,
                    plan_digest=plan_digest,
                    recipe_revision_id=recipe_revision_id,
                )
        except RecipeImageAvailabilityError:
            raise
        except Exception as error:
            raise _ModelQueueFailed(
                error, "exact Model artifact preparation could not be queued"
            ) from error
        return AvailabilityModelChild.model_validate_json(
            canonical_message(
                {
                    "id": operation.id,
                    "request_key": str(
                        getattr(operation, "request_key", child_request_key)
                    ),
                    "state": operation.state,
                    "artifact_set_sha256": artifact_set_sha256,
                    "plan_digest": plan_digest,
                    "model_content_digests": manifest_document.model_content_digests,
                    "artifacts": [
                        artifact.model_dump(mode="json")
                        for item in manifest_document.artifacts
                        if (
                            artifact := _read(
                                RecipeImageAvailabilityArtifact,
                                item.model_dump(mode="json"),
                            )
                        )
                        is not None
                    ],
                    "progress": self._model_progress(operation.progress).model_dump(
                        mode="json"
                    ),
                }
            )
        )

    def _start_model_repair(
        self,
        operation: object,
        *,
        actor: str,
        parent_request_key: str,
    ) -> ModelCacheOperationHandle:
        """Start a fresh content-addressed repair without deleting valid bytes."""

        artifact_set_sha256 = getattr(operation, "artifact_set_sha256", None)
        if not isinstance(artifact_set_sha256, str):
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.MODEL_CACHE_INVALID,
                "integrity failure did not retain an artifact-set identity",
                retryable=True,
                recovery_actions=("retry",),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        repair_preview = getattr(self._model_cache, "repair_preview", None)
        start_repair = getattr(self._model_cache, "start_repair", None)
        if repair_preview is None or start_repair is None:
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.MODEL_CACHE_UNAVAILABLE,
                "ModelCache does not expose the canonical repair workflow",
                retryable=True,
                recovery_actions=("retry",),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        preview = _read(
            ModelCacheRepairPreviewResponse, repair_preview(artifact_set_sha256)
        )
        plan_digest = preview.plan_digest if preview else None
        if not isinstance(plan_digest, str):
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.MODEL_CACHE_INVALID,
                "ModelCache returned an incomplete repair plan",
                retryable=True,
                recovery_actions=("retry",),
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        request_key = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"vonk:recipe-availability-model-repair:{artifact_set_sha256}:{parent_request_key}",
            )
        )
        return start_repair(
            actor=actor,
            request_key=request_key,
            artifact_set_sha256=artifact_set_sha256,
            plan_digest=plan_digest,
        )

    def _resume_model_child(
        self,
        child: AvailabilityModelChild | None,
        *,
        actor: str,
        parent_request_key: str,
    ) -> AvailabilityModelChild | None:
        if self._model_cache is None or child is None:
            return child
        child_id = child.id
        try:
            operation = self._model_cache.get_operation(child_id)
            if operation.state == "failed":
                reused = None
                list_operations = getattr(self._model_cache, "list_operations", None)
                if list_operations is not None:
                    candidates = [
                        candidate
                        for candidate in list_operations(limit=100)
                        if (
                            candidate.id != child_id
                            and candidate.artifact_set_sha256
                            == operation.artifact_set_sha256
                            and candidate.state
                            in {*model_cache_states.ACTIVE, "succeeded"}
                        )
                    ]
                    state_rank = {
                        "succeeded": 0,
                        "queued": 1,
                        "running": 1,
                        model_cache_states.BACKOFF: 1,
                    }
                    reused = min(
                        candidates,
                        key=lambda candidate: (
                            state_rank.get(candidate.state, 2),
                            str(candidate.id),
                        ),
                        default=None,
                    )
                if reused is not None:
                    operation = reused
                else:
                    retry_key = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"vonk:recipe-availability-model-retry:{child_id}:{parent_request_key}",
                        )
                    )
                    failure = _read(AvailabilityOperationFailure, operation.failure)
                    actions = failure.recovery_actions if failure else []
                    if "download_again" in actions and isinstance(
                        operation.artifact_set_sha256, str
                    ):
                        operation = self._start_model_repair(
                            operation,
                            actor=actor,
                            parent_request_key=parent_request_key,
                        )
                    elif (
                        "check_access_and_resume" in actions
                        and isinstance(operation.artifact_set_sha256, str)
                        and isinstance(operation.plan_digest, str)
                        and callable(
                            getattr(self._model_cache, "check_access_and_resume", None)
                        )
                    ):
                        operation = self._model_cache.check_access_and_resume(
                            child_id,
                            actor=actor,
                            request_key=retry_key,
                            artifact_set_sha256=operation.artifact_set_sha256,
                            plan_digest=operation.plan_digest,
                        )
                    else:
                        operation = self._model_cache.retry(
                            child_id, actor=actor, request_key=retry_key
                        )
            return self._refresh_model_observation(child, operation)
        except Exception as error:
            raise _ModelQueueFailed(
                error, "Model artifact operation could not be resumed"
            ) from error

    def _matching_request(
        self,
        existing: Job,
        *,
        actor: str,
        intent: RecipeAvailabilityIntent,
    ) -> RecipeImageAvailabilityView:
        if existing.kind != OPERATION_KIND or existing.actor != actor:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.REQUEST_KEY_REUSED,
                "request key was already used for another operation",
                reason=InvalidRequestReason.CONFLICT,
            )
        try:
            payload = self._payload(existing)
            stored = (
                payload.request if isinstance(payload, AvailabilityJobPayload) else None
            )
        except (TypeError, ValueError, ValidationError) as error:
            # A stored request that does not parse cannot be the caller's request:
            # it is recorded and the key reads as used by another operation.
            retire_as_unknown(
                "recipe-image.request",
                existing.id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"{type(error).__name__}: {error}",
            )
            stored = None
        if stored != intent:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.REQUEST_KEY_REUSED,
                "request key was already used for another operation",
                reason=InvalidRequestReason.CONFLICT,
            )
        return self._view(existing)

    def _request_replay(
        self,
        request_id: str,
        *,
        actor: str,
        intent: RecipeAvailabilityIntent,
    ) -> RecipeImageAvailabilityView | None:
        with self._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is None:
                return None
            return self._matching_request(existing, actor=actor, intent=intent)

    def get(self, operation_id: str) -> RecipeImageAvailabilityView:
        with self._sessions() as session:
            operation = session.get(Job, operation_id)
            if operation is None or operation.kind != OPERATION_KIND:
                raise MissingRecord(operation_id)
            return self._view(operation)

    def get_operator_operation(self, operation_id: str):
        """Observe either current recipe preparation or durable cache removal."""

        with self._sessions() as session:
            operation = session.get(Job, operation_id)
            if operation is None:
                raise MissingRecord(operation_id)
            if operation.kind == OPERATION_KIND:
                return self._view(operation)
            if operation.kind == REMOVE_OPERATION_KIND:
                intent = self._read_removal_intent(operation)
                return self._read_removal_result(operation, intent)
            if operation.kind != UPDATE_KIND:
                raise MissingRecord(operation_id)
        return self._updates.get(operation_id)

    def get_operator_request(self, request_key: str, *, actor: str):
        """Correlate only this issuer's request within the recipe family."""

        with self._sessions() as session:
            operation = session.scalar(
                select(Job).where(Job.request_id == request_key, Job.actor == actor)
            )
            if operation is None or operation.kind not in {
                OPERATION_KIND,
                REMOVE_OPERATION_KIND,
                UPDATE_KIND,
            }:
                raise MissingRecord(request_key)
            operation_id = operation.id
        return self.get_operator_operation(operation_id)

    def list_page(
        self,
        *,
        recipe_revision_id: str | None = None,
        state: str | None = None,
        limit: int = 50,
        boundary: tuple[str, str] | None = None,
    ) -> tuple[tuple[RecipeImageAvailabilityView, ...], int, tuple[str, str] | None]:
        if not 1 <= limit <= 100:
            raise InvalidValue(
                "availability list limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        with self._sessions() as session:
            query = select(Job).where(Job.kind == OPERATION_KIND)
            count_query = (
                select(func.count()).select_from(Job).where(Job.kind == OPERATION_KIND)
            )
            if recipe_revision_id is not None:
                query = query.where(Job.authority_revision == recipe_revision_id)
                count_query = count_query.where(
                    Job.authority_revision == recipe_revision_id
                )
            if state is not None:
                query = query.where(Job.state == state)
                count_query = count_query.where(Job.state == state)
            if boundary is not None:
                boundary_time = datetime.fromisoformat(boundary[0])
                query = query.where(
                    or_(
                        Job.created_at < boundary_time,
                        (Job.created_at == boundary_time) & (Job.id < boundary[1]),
                    )
                )
            total = int(session.scalar(count_query) or 0)
            rows = tuple(
                session.scalars(
                    query.order_by(Job.created_at.desc(), Job.id.desc()).limit(
                        limit + 1
                    )
                )
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
        next_boundary = None
        if has_more and rows:
            last = rows[-1]
            next_boundary = (_iso(last.created_at), last.id)
        return tuple(self._view(row) for row in rows), total, next_boundary

    def retry(
        self, operation_id: str, *, actor: str, request_id: str
    ) -> RecipeImageAvailabilityView:
        intent = RecipeRetryIntent(operation_id=operation_id)
        existing = self._request_replay(request_id, actor=actor, intent=intent)
        if existing is not None:
            return existing
        with self._sessions() as session:
            previous = session.get(Job, operation_id)
            if previous is None or previous.kind != OPERATION_KIND:
                raise MissingRecord(operation_id)
            if previous.state != "failed":
                raise RecipeImageAvailabilityInvalid(
                    RecipeImageCode.NOT_RETRYABLE, "operation is not failed"
                )
            previous_payload = self._payload(previous)
            if isinstance(previous_payload, Residue):
                return self._unknown_view(previous, previous_payload)
            retry_count = previous_payload.retry.operator_retries
            previous_authority = previous.authority_revision
            previous_targets = list(previous.targets)
            payload = previous_payload
        payload = payload.model_copy(update={"request": intent})
        with self._sessions.begin() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is not None:
                return self._matching_request(existing, actor=actor, intent=intent)
            payload = payload.model_copy(
                update={
                    "retry": AvailabilityRetry(
                        automatic_attempts=0, operator_retries=retry_count + 1
                    )
                }
            )
            payload = payload.model_copy(update={"retry_after_at": None})
            payload = payload.model_copy(update={"failure": None})
            payload = payload.model_copy(update={"build_dependency": None})
            self._lock_build_consumer(session, payload)
            now = self._clock()
            encoded = canonical_message(payload)
            operation = self._lifecycle.new_job(
                id=str(uuid.uuid4()),
                request_id=request_id,
                kind=OPERATION_KIND,
                actor=actor,
                authority_revision=previous_authority,
                targets=previous_targets,
                payload_digest=hashlib.sha256(encoded).hexdigest(),
                payload=serialize_json_value(payload),
                result=None,
                current_attempt=0,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            return self._view(operation)

    def resume_operations(self, *, limit: int = 16) -> int:
        if not 1 <= limit <= 100:
            raise InvalidValue(
                "availability operation limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        with self._sessions() as session:
            # SQLAlchemy's scalar count expression is portable across the
            # SQLite fixtures and PostgreSQL deployment.
            count = int(
                session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(
                        Job.kind.in_((OPERATION_KIND, REMOVE_OPERATION_KIND)),
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.BACKOFF,
                                LifecycleState.OBSERVING,
                            )
                        ),
                    )
                )
                or 0
            )
            return min(count, limit)

    def run_pending(self, *, limit: int = 1) -> int:
        if not 1 <= limit <= 16:
            raise InvalidValue(
                "availability worker batch limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        removals = self.advance_removals(limit=1)
        self.reconcile_cancellations(limit=limit)
        update_claim = self.claim_update(f"update-{uuid.uuid4().hex}")
        if update_claim is not None:
            self.run_update_claim(update_claim)
        claims = self.claim_pending(limit=min(limit, self._max_parallel))
        for claim in claims:
            self.run_claim(claim)
        return len(claims) + int(update_claim is not None) + removals

    def claim_pending(
        self, *, limit: int = 4, owner_id: str | None = None
    ) -> tuple[RecipeImageAvailabilityClaim, ...]:
        """Return independent durable claims for an external worker scheduler.

        The caller may dispatch each claim on its own bounded executor.  The
        claims carry no network handle, so a process restart can safely find
        the same rows through :meth:`resume_operations`.
        """

        if not 1 <= limit <= self._max_parallel:
            raise InvalidValue(
                "availability claim limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        self.reconcile_requested_removals()
        owner_id = owner_id or str(uuid.uuid4())
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        claims: list[RecipeImageAvailabilityClaim] = []
        with self._sessions.begin() as session:
            # The dispatch boundary.  A queued preparation whose recipe has
            # advanced to a newer active revision is cancelled here, before any
            # claim is handed to a builder executor, so a superseded operation
            # can never occupy the Spark the current revision needs.  Only
            # not-yet-started operations are eligible; a running attempt with a
            # live lease is left alone because the active revision may reuse its
            # shared build inputs.
            self._cancel_superseded_by_active_head(session, now=now)
            candidate_ids = list(
                session.scalars(
                    select(Job.id)
                    .where(
                        Job.kind == OPERATION_KIND,
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.BACKOFF,
                            )
                        ),
                    )
                    .order_by(Job.updated_at, Job.id)
                    .limit(max(limit * 8, _CLAIM_SCAN_WINDOW))
                )
            )
            for operation_id in candidate_ids:
                operation = session.scalar(
                    select(Job)
                    .where(
                        Job.id == operation_id,
                        Job.kind == OPERATION_KIND,
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.BACKOFF,
                            )
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
                if operation is None:
                    continue
                payload = self._payload(operation)
                if isinstance(payload, Residue):
                    continue
                if not self._retry_due(payload, now):
                    continue
                if (
                    operation.state == "running"
                    and payload.claim_until is not None
                    and now < payload.claim_until
                ):
                    continue
                if has_pending_removal(
                    session,
                    (
                        ArtifactIdentity("runtime-image", archive)
                        for archive in payload.removal_archives or ()
                    ),
                ):
                    updated = self._record_blockers(
                        operation,
                        payload,
                        [
                            make_blocker(
                                ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                                "Waiting for the prior image removal fence to settle",
                                severity="info",
                            )
                        ],
                    )
                    updated = self._lifecycle.defer(
                        operation, now, now + timedelta(seconds=5), payload=updated
                    )
                    operation.payload = serialize_json_value(updated)
                    continue
                if operation.state in job_states.words(
                    LifecycleState.BACKOFF
                ) and self._park_for_model(operation, payload, now):
                    # Only the model download is outstanding: no worker slot is
                    # spent polling it, so ready work is never queued behind it.
                    continue
                self._lifecycle.claim(
                    operation,
                    owner_id,
                    now + timedelta(seconds=self._claim_lease_seconds),
                    now,
                )
                operation.payload = serialize_json_value(
                    payload.model_copy(
                        update={
                            "claim_owner": owner_id,
                            "claim_until": now
                            + timedelta(seconds=self._claim_lease_seconds),
                        }
                    )
                )
                claims.append(
                    RecipeImageAvailabilityClaim(
                        operation_id=operation.id,
                        recipe_revision_id=str(payload.recipe_revision_id),
                        build_input_sha256=(
                            str(payload.build_input_sha256)
                            if payload.build_input_sha256 is not None
                            else None
                        ),
                        claim_owner=owner_id,
                        execution_attempt=operation.current_attempt,
                    )
                )
                if len(claims) >= limit:
                    break
        return tuple(claims)

    def _park_for_model(
        self, operation: Job, payload: AvailabilityJobPayload, now: datetime
    ) -> bool:
        """Wait without a worker slot when only the exact model child is active."""
        child = payload.model_child
        if self._model_cache is None or child is None or payload.image_result is None:
            return False
        try:
            state = self._model_cache.get_operation(child.id).state
        except Exception:  # noqa: BLE001 - a normal claim refreshes unclear evidence
            return False
        if state not in model_cache_states.ACTIVE:
            return False
        updated = payload.model_copy(
            update={"retry_after_at": now + timedelta(seconds=_MODEL_WAIT_POLL_SECONDS)}
        )
        if not any(
            item.code == RecipeImageCode.WAITING_FOR_MODEL
            for item in payload.blockers or []
        ):
            updated = self._record_blockers(
                operation,
                updated,
                [
                    make_blocker(
                        RecipeImageCode.WAITING_FOR_MODEL,
                        "Waiting for the model download to finish; the runtime image is already prepared.",
                        severity="info",
                    )
                ],
            )
        operation.payload = serialize_json_value(updated)
        operation.updated_at = now
        return True

    @staticmethod
    def _holds_live_lease(payload: AvailabilityJobPayload, now: datetime) -> bool:
        return bool(payload.claim_owner) and (
            payload.claim_until is None or now < payload.claim_until
        )

    def _cancel_superseded_operation(
        self,
        operation: Job,
        newer_revision_id: str,
        *,
        now: datetime,
    ) -> bool:
        """Cancel one not-yet-started operation superseded by newer intent.

        The operation record is rewritten to a terminal ``cancelled`` state with
        typed failure evidence that names the replacement, never to a success.
        An operation that already holds a durable image result or a live lease
        is left alone, and a state other than ``queued`` is never touched: a
        running build may still produce an image the active revision reuses.
        """
        if operation.state != "queued":
            return False
        payload = self._payload(operation)
        if (
            isinstance(payload, Residue)
            or payload.image_result is not None
            or self._holds_live_lease(payload, now)
        ):
            return False
        detail = f"superseded by newer recipe revision {newer_revision_id}"
        failure = AvailabilityOperationFailure(
            code=SUPERSEDED_PREPARATION_CODE,
            detail=detail,
            recovery_actions=[AvailabilityRecoveryAction.DOWNLOAD_AGAIN],
            retryable=False,
        )
        updated = payload.model_copy(
            update={
                "claim_owner": None,
                "claim_until": None,
                "retry_after_at": None,
                "failure": failure,
                "supersession": AvailabilitySupersession(
                    code=SUPERSEDED_PREPARATION_CODE,
                    recipe_revision_id=newer_revision_id,
                    superseded_at=now,
                ),
            }
        )
        operation.payload = serialize_json_value(updated)
        operation.result = None
        self._lifecycle.supersede(operation, detail[:512], now)
        return True

    def _cancel_older_preparations(
        self,
        session: Session,
        *,
        newer_revision: CatalogDocumentRevision,
        now: datetime,
        limit: int = 64,
    ) -> tuple[str, ...]:
        """Cancel queued preparations older than a newer intent for the recipe.

        ``newer_revision`` is the revision the caller is recording an intent
        for.  Revisions are ordered by the monotonic ``revision_number`` within
        one canonical recipe document, so a retained older digest that becomes
        the head again does not fall into this older-than comparison.
        """
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        candidates = tuple(
            session.scalars(
                select(Job)
                .join(
                    CatalogDocumentRevision,
                    CatalogDocumentRevision.id == Job.authority_revision,
                )
                .where(
                    Job.kind == OPERATION_KIND,
                    Job.state == "queued",
                    Job.authority_revision != newer_revision.id,
                    CatalogDocumentRevision.document_id == newer_revision.document_id,
                    CatalogDocumentRevision.revision_number
                    < newer_revision.revision_number,
                )
                .order_by(Job.created_at, Job.id)
                .limit(limit)
                .with_for_update(of=Job, skip_locked=True)
            )
        )
        return tuple(
            operation.id
            for operation in candidates
            if self._cancel_superseded_operation(operation, newer_revision.id, now=now)
        )

    def _cancel_superseded_by_active_head(
        self, session: Session, *, now: datetime, limit: int = 64
    ) -> tuple[str, ...]:
        """Cancel queued preparations older than their recipe's active head.

        This is the durable counterpart to the intent-time cancellation: it
        catches a head that advanced without a fresh preparation request (for
        example a managed-recipes sync).  Only strictly older revisions of the
        same recipe are eligible, and only not-yet-started ones, so a running
        build whose shared inputs the active revision can reuse is untouched.
        """
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        operation_revision = aliased(CatalogDocumentRevision)
        head_revision = aliased(CatalogDocumentRevision)
        rows = session.execute(
            select(Job, head_revision.id)
            .join(operation_revision, operation_revision.id == Job.authority_revision)
            .join(
                CatalogDocumentHead,
                (CatalogDocumentHead.kind == operation_revision.kind)
                & (CatalogDocumentHead.publisher == operation_revision.publisher)
                & (CatalogDocumentHead.slug == operation_revision.slug),
            )
            .join(
                head_revision,
                head_revision.id == CatalogDocumentHead.active_revision_id,
            )
            .where(
                Job.kind == OPERATION_KIND,
                Job.state == "queued",
                operation_revision.kind == "recipe",
                head_revision.id != operation_revision.id,
                head_revision.revision_number > operation_revision.revision_number,
            )
            .order_by(Job.created_at, Job.id)
            .limit(limit)
            .with_for_update(of=Job, skip_locked=True)
        ).all()
        return tuple(
            operation.id
            for operation, head_revision_id in rows
            if self._cancel_superseded_operation(
                operation, str(head_revision_id), now=now
            )
        )

    def run_claim(self, claim: RecipeImageAvailabilityClaim) -> None:
        """Execute one claim; callers may run claims in their own bounded pool."""

        self._run(claim)

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        with self._identity_locks_guard:
            return {
                WorkerMemoryComponent.IMAGE_PREPARATION_IDENTITY_LOCKS: len(
                    self._identity_locks
                )
            }

    def _identity_lock(self, identity_key: str | None) -> threading.Lock:
        if not identity_key:
            return threading.Lock()
        with self._identity_locks_guard:
            lock = self._identity_locks.get(identity_key)
            if lock is None:
                lock = threading.Lock()
                self._identity_locks[identity_key] = lock
            return lock

    def _eligible(self, operation_id: str) -> bool:
        with self._sessions() as session:
            operation = session.get(Job, operation_id)
            if operation is None:
                return False
            clocks = StoredAvailabilityClocks.read(operation.payload)
            now = self._clock()
            now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            return clocks.retry_after_at is None or now >= clocks.retry_after_at

    @staticmethod
    def _retry_due(payload: AvailabilityJobPayload, now: datetime) -> bool:
        return payload.retry_after_at is None or now >= payload.retry_after_at

    def _claim_operation(
        self,
        session: Session,
        claim: RecipeImageAvailabilityClaim,
        *,
        allowed_states: tuple[str, ...] = ("running",),
    ) -> Job | None:
        # Progress can arrive under the managed artifact lock. Never wait for
        # SQL ownership there. A contended claim remains visible until its
        # existing lease expires; the scheduler can then resume its exact work.
        operation = session.scalar(
            select(Job)
            .where(Job.id == claim.operation_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        payload = self._payload(operation) if operation is not None else None
        if (
            operation is None
            or operation.kind != OPERATION_KIND
            or operation.state not in allowed_states
            or operation.current_attempt != claim.execution_attempt
            or not isinstance(operation.payload, Mapping)
            or not isinstance(payload, AvailabilityJobPayload)
            or payload.claim_owner != claim.claim_owner
            or payload.removal_fence is not None
        ):
            return None
        if (
            operation.state in job_states.words(LifecycleState.OBSERVING)
            and self._stored_cancellation(operation) is None
        ):
            return None
        until = payload.claim_until
        if until is None:
            return None
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        if now >= until:
            return None
        return operation

    def _require_claim(
        self,
        session: Session,
        claim: RecipeImageAvailabilityClaim,
        *,
        allowed_states: tuple[str, ...] = ("running",),
    ) -> Job:
        operation = self._claim_operation(session, claim, allowed_states=allowed_states)
        if operation is None:
            raise _AvailabilityClaimLost
        return operation

    def _run(self, claim: RecipeImageAvailabilityClaim) -> None:
        operation_id = claim.operation_id
        with self._sessions.begin() as session:
            operation = self._claim_operation(session, claim)
            if operation is not None:
                payload = self._payload(operation)
                if isinstance(payload, Residue):
                    return
                operation_actor = operation.actor
                operation_request_id = operation.request_id
                operation.updated_at = self._clock()
                self._set_progress(
                    operation,
                    "prepare",
                    total_bytes=_known_total(payload.runtime),
                )
        if operation is None:
            self._release_cancelled_claim(claim)
            self._reconcile_availability_cancellation(operation_id)
            return
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._renew_claim_loop,
            args=(claim, heartbeat_stop),
            name=f"recipe-image-lease-{operation_id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            recipe = payload.recipe
            runtime = payload.runtime
            request_intent = payload.request
            if (
                isinstance(request_intent, RecipeRetryIntent)
                and payload.model_child is not None
            ):
                model_child = self._resume_model_child(
                    payload.model_child,
                    actor=operation_actor,
                    parent_request_key=operation_request_id,
                )
            else:
                model_child = self._current_model_child(
                    payload,
                    actor=operation_actor,
                    parent_request_key=operation_request_id,
                )
            model_pending = False
            model_failure: AvailabilityOperationFailure | None = None
            if model_child is not None:
                child_state = model_child.state
                if not self._update_model_progress(claim, model_child):
                    raise _AvailabilityClaimLost()
                if child_state in model_cache_states.ACTIVE:
                    model_pending = True
                elif child_state != "succeeded":
                    model_failure = model_child.failure
            identity_key = payload.identity_key
            identity = identity_key if isinstance(identity_key, str) else None
            with self._identity_lock(identity):
                receipt = payload.image_result
                if receipt is None or not self._storage.build_archive_available(
                    receipt.oci_archive_sha256, receipt.image_bytes
                ):
                    prepared = self._prepare_claimed_image(
                        claim, payload, recipe, runtime
                    )
                    if isinstance(prepared, BuildUnsettled):
                        # Unknown, not failed: the outcome is recorded and the
                        # core schedules the next attempt on its bounded backoff.
                        self._fail(claim, prepared)
                        return
                    receipt = prepared
                    # Removal holds the same lock and commits a durable fence
                    # before deleting Controller image bytes. Late verified
                    # bytes may remain reusable, but stale work cannot accept
                    # a current authorization or operation result.
                    with self._removal_lock:
                        self._persist_receipt(claim, receipt)
            with self._sessions() as session:
                latest = session.get(Job, operation_id)
                if latest is not None:
                    payload = self._payload(latest)
                    if isinstance(payload, Residue):
                        return
            if model_pending:
                self._defer_for_model(claim)
                return
            if model_failure is not None:
                raise RecipeImageAvailabilityUnknown(
                    model_failure.code,
                    model_failure.detail,
                    retryable=model_failure.retryable,
                    retry_after_seconds=model_failure.retry_after_seconds,
                    retry_time=model_failure.retry_time,
                    recovery_actions=tuple(
                        action.value for action in model_failure.recovery_actions
                    ),
                    log_excerpt=model_failure.log_excerpt,
                    required_bytes=model_failure.required_bytes,
                    free_bytes=model_failure.free_bytes,
                    shortfall_bytes=model_failure.shortfall_bytes,
                )
            result = AvailabilityJobResult(
                schema_version=SCHEMA_VERSION,
                recipe_content_sha256=payload.recipe_content_sha256,
                model_digest=payload.model_digest,
                build_input_sha256=payload.build_input_sha256,
                image_digest=receipt.image_digest,
                local_image_config_id=receipt.local_image_config_id,
                oci_archive_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                build_id=receipt.build_id,
                model_child=model_child,
            )
            with self._removal_lock, self._sessions.begin() as session:
                operation = self._require_claim(session, claim)
                operation.result = serialize_json_value(result)
                self._lifecycle.succeed(operation, self._clock())
                completed_payload = self._payload(operation)
                if isinstance(completed_payload, Residue):
                    return
                completed_payload = completed_payload.model_copy(
                    update={"failure": None}
                )
                completed_payload = completed_payload.model_copy(
                    update={"retry_after_at": None}
                )
                completed_payload = completed_payload.model_copy(
                    update={"blockers": None}
                )
                operation.payload = serialize_json_value(
                    completed_payload.model_copy(
                        update={
                            "stage": "available",
                            "claim_owner": None,
                            "claim_until": None,
                        }
                    )
                )
                operation.current_attempt = int(operation.current_attempt)
                self._set_progress(
                    operation,
                    "available",
                    total_bytes=receipt.image_bytes,
                    completed_bytes=receipt.image_bytes,
                )
        except _AvailabilityClaimLost:
            return
        except UnknownOutcomeError as error:
            # An unknown outcome is never a refusal: the failure is recorded and
            # the lifecycle core schedules the next attempt on its bounded clock.
            self._fail(claim, error)
        except Exception as error:  # noqa: BLE001 - persist failures at the background job boundary
            self._fail(claim, error)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=max(1.0, self._claim_lease_seconds / 2))
            self._release_cancelled_claim(claim)
            self._reconcile_availability_cancellation(operation_id)

    def _stored_recipe(
        self, payload: AvailabilityJobPayload | StoredAvailabilityIdentity | object
    ) -> RecipeDefinition | Residue:
        """Rebuild a damaged recipe only from its exact accepted catalog digest."""
        if isinstance(payload, AvailabilityJobPayload):
            return payload.recipe
        identity = (
            payload
            if isinstance(payload, StoredAvailabilityIdentity)
            else _read(StoredAvailabilityIdentity, payload)
        )
        if identity is not None:
            if identity.recipe is not None:
                return identity.recipe
            if (
                identity.recipe_revision_id is not None
                and identity.recipe_content_sha256 is not None
            ):
                with self._sessions() as session:
                    revision = session.get(
                        CatalogDocumentRevision, identity.recipe_revision_id
                    )
                    if (
                        revision is not None
                        and revision.kind == "recipe"
                        and revision.content_digest == identity.recipe_content_sha256
                    ):
                        document = read_row_column(revision, "document")
                        if isinstance(document, RecipeDefinition):
                            return document
        return retire_as_unknown(
            "recipe-image.recipe",
            identity.recipe_revision_id
            if identity and identity.recipe_revision_id
            else "?",
            BookkeepingReason.EVIDENCE_UNAVAILABLE,
        )

    def _current_model_child(
        self,
        payload: AvailabilityJobPayload,
        *,
        actor: str | None = None,
        parent_request_key: str | None = None,
    ) -> AvailabilityModelChild | None:
        child = payload.model_child
        if child is None or self._model_cache is None:
            if (
                child is None
                and self._model_cache is not None
                and actor is not None
                and parent_request_key is not None
                and payload.recipe.models
            ):
                return self._ensure_model_child(
                    payload.recipe_revision_id,
                    actor=actor,
                    parent_request_key=parent_request_key,
                )
            return child
        child_id = child.id
        try:
            operation = self._model_cache.get_operation(child_id)
            if (
                operation.state == "succeeded"
                and actor is not None
                and parent_request_key is not None
                and isinstance(operation.artifact_set_sha256, str)
            ):
                recipe_revision_id = payload.recipe_revision_id
                if not isinstance(recipe_revision_id, str):
                    raise RecipeImageAvailabilityUnknown(
                        RecipeImageCode.MODEL_CACHE_INVALID,
                        "availability operation lacks its exact recipe revision",
                        retryable=True,
                        recovery_actions=("retry",),
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                preview = AvailabilityDownloadPreview.read(
                    self._model_cache.download_preview(
                        recipe_revision_id=recipe_revision_id
                    ),
                )
                if (
                    preview is None
                    or preview.artifact_set_sha256 != operation.artifact_set_sha256
                ):
                    raise RecipeImageAvailabilityUnknown(
                        RecipeImageCode.MODEL_CACHE_INVALID,
                        "ModelCache returned an incomplete exact artifact plan",
                        retryable=True,
                        recovery_actions=("retry",),
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                preview_plan = preview.plan_digest
                new_bytes = preview.new_bytes
                if new_bytes > 0:
                    return self._ensure_model_child(
                        recipe_revision_id,
                        actor=actor,
                        parent_request_key=(
                            f"{parent_request_key}:restore:{child_id}:{preview_plan}"
                        ),
                    )
        except ModelCacheNotFound:
            return child.model_copy(
                update={
                    "state": LifecycleState.FAILED,
                    "failure": AvailabilityOperationFailure(
                        code=RecipeImageCode.MODEL_CHILD_MISSING,
                        detail="durable ModelCache child operation is unavailable",
                        retryable=True,
                        recovery_actions=[AvailabilityRecoveryAction.RETRY],
                    ),
                }
            )
        return self._refresh_model_observation(child, operation)

    def _update_model_progress(
        self, claim: RecipeImageAvailabilityClaim, child: AvailabilityModelChild
    ) -> bool:
        child_id = child.id
        with self._sessions.begin() as session:
            model_operation = None
            if isinstance(child_id, str):
                model_operation = session.scalar(
                    select(ModelCacheOperation)
                    .where(ModelCacheOperation.id == child_id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
                if model_operation is None and self._model_cache is not None:
                    raise RecipeImageAvailabilityUnknown(
                        RecipeImageCode.MODEL_CHILD_MISSING,
                        "durable ModelCache child operation is unavailable",
                        retryable=True,
                        recovery_actions=("retry",),
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
            operation = session.scalar(
                select(Job)
                .where(Job.id == claim.operation_id, Job.kind == OPERATION_KIND)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
            payload = self._payload(operation) if operation is not None else None
            if (
                operation is None
                or not isinstance(payload, AvailabilityJobPayload)
                or operation.current_attempt != claim.execution_attempt
                or not isinstance(operation.payload, Mapping)
                or payload.claim_owner != claim.claim_owner
            ):
                raise _AvailabilityClaimLost()
            cancelling = operation.state in job_states.words(LifecycleState.OBSERVING)
            if not cancelling and operation.state != "running":
                raise _AvailabilityClaimLost()
            if (
                not cancelling
                and model_operation is not None
                and (
                    model_operation.state == "cancelled"
                    or self._model_child_has_cancel_intent(model_operation)
                )
            ):
                raise RecipeImageAvailabilityUnknown(
                    RecipeImageCode.MODEL_CHILD_CANCELLED,
                    "ModelCache child was cancelled while joining the operation",
                    retryable=True,
                    recovery_actions=("resume", "retry"),
                )
            payload = self._payload(operation)
            if isinstance(payload, Residue):
                return False
            payload = payload.model_copy(update={"model_child": child})
            operation.payload = serialize_json_value(payload)
            operation.updated_at = self._clock()
            # Keep image progress separate. The view aggregates the two
            # durable members exactly once.
            return not cancelling

    @staticmethod
    def _model_child_has_cancel_intent(operation: ModelCacheOperation) -> bool:
        payload = read_row_column(operation, "payload")
        if isinstance(payload, Residue):
            cancellation = _read(
                StoredModelChildCancellation, operation.payload, subject=operation.id
            )
            return cancellation is not None and cancellation.cancellation is not None
        if payload is None:
            return False
        return payload.cancellation is not None

    def _defer_for_model(self, claim: RecipeImageAvailabilityClaim) -> None:
        with self._sessions.begin() as session:
            operation = self._require_claim(session, claim)
            now = self._clock()
            payload = self._payload(operation)
            if isinstance(payload, Residue):
                return
            payload = payload.model_copy(
                update={"claim_owner": None, "claim_until": None}
            )
            payload = self._lifecycle.defer(
                operation, now, now + timedelta(seconds=1), payload=payload
            )
            payload = self._record_blockers(
                operation,
                payload,
                [
                    make_blocker(
                        RecipeImageCode.WAITING_FOR_MODEL,
                        "Waiting for the model download to finish.",
                        severity="info",
                    )
                ],
            )
            operation.payload = serialize_json_value(payload)

    def _prepare_claimed_image(
        self,
        claim: RecipeImageAvailabilityClaim,
        payload: AvailabilityJobPayload,
        recipe: RecipeDefinition,
        runtime: AvailabilityRuntime,
    ) -> RuntimeImageReceipt | BuildUnsettled:
        force_rebuild = payload.force_rebuild is True

        def persist_provisional_reference(
            receipt: RuntimeImageReceipt,
        ) -> None:
            self._persist_provisional_image_reference(
                claim,
                receipt=receipt,
            )

        if self._builder is None:
            raise RecipeImageAvailabilityInvalid(
                RecipeImageCode.BUILD_UNAVAILABLE,
                "no canonical recipe build executor is configured",
                reason=InvalidRequestReason.NOT_FOUND,
            )
        build_input_sha256 = payload.build_input_sha256
        dispatch_identity_missing = not isinstance(build_input_sha256, str)
        if dispatch_identity_missing:
            build_input_sha256 = ""
        if self._builder_admission is not None:
            self._builder_admission(recipe, runtime.model_dump(mode="json"))
        self._update_progress(claim, "build", total_bytes=None)

        def report(value: object) -> None:
            progress = _read(OperationProgress, value, subject=claim.operation_id)
            if progress is not None:
                self._update_progress(claim, progress.phase, detail=progress)

        # The builder re-resolves the exact executable identity and reuses
        # a verified filesystem receipt itself, so queue-time and
        # dispatch-time cache hits take the same path.
        build_receipt = self._builder(
            recipe,
            runtime.model_dump(mode="json"),
            claim=claim,
            build_input_sha256=build_input_sha256,
            force=force_rebuild,
            progress=report,
        )
        if isinstance(build_receipt, BuildUnsettled):
            return build_receipt
        receipt = _read(
            AvailabilityBuildReceipt, build_receipt, subject=claim.operation_id
        )
        if receipt is None:
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.BUILD_INVALID,
                "builder returned no receipt",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        if dispatch_identity_missing:
            resolved_input = receipt.build_input_sha256
            if resolved_input is None:
                raise RecipeImageAvailabilityUnknown(
                    RecipeImageCode.BUILD_INPUT_MISSING,
                    "dispatch did not bind an exact build input identity",
                    retryable=True,
                    recovery_actions=("retry",),
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            with self._sessions.begin() as session:
                operation = self._require_claim(session, claim)
                assigned = self._payload(operation)
                if isinstance(assigned, Residue):
                    return BuildUnsettled(
                        RecipeImageCode.OPERATION_INVALID,
                        "availability bookkeeping is unknown",
                        WaitReason.OBSERVATION_UNAVAILABLE,
                        retryable=True,
                    )
                runtime_update = {"build_input_sha256": resolved_input}
                if receipt.builder_node_id is not None:
                    runtime_update["builder_node_id"] = receipt.builder_node_id
                assigned_runtime = assigned.runtime.model_copy(update=runtime_update)
                operation.payload = serialize_json_value(
                    assigned.model_copy(
                        update={
                            "build_input_sha256": resolved_input,
                            "identity_key": resolved_input,
                            "runtime": assigned_runtime,
                        }
                    )
                )
        self._update_progress(claim, "verify")
        return prepare_runtime_image(
            recipe.model_dump(mode="json"),
            runtime=runtime.model_dump(mode="json"),
            storage=self._storage,
            transport=self._transport,
            build_receipt=receipt.model_dump(mode="json"),
            now=self._clock(),
            before_publish=persist_provisional_reference,
        )

    def _renew_claim_loop(
        self, claim: RecipeImageAvailabilityClaim, stop: threading.Event
    ) -> None:
        interval = max(1.0, self._claim_lease_seconds / 3)
        while not stop.wait(interval):
            if not self._renew_claim(claim):
                return

    def _renew_claim(self, claim: RecipeImageAvailabilityClaim) -> bool:
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        with self._sessions.begin() as session:
            operation = self._claim_operation(
                session,
                claim,
                allowed_states=job_states.words(
                    LifecycleState.RUNNING, LifecycleState.OBSERVING
                ),
            )
            if operation is None:
                return False
            payload = self._payload(operation)
            if isinstance(payload, Residue):
                return False
            operation.payload = serialize_json_value(
                payload.model_copy(
                    update={
                        "claim_until": now
                        + timedelta(seconds=self._claim_lease_seconds),
                    }
                )
            )
            operation.updated_at = now
            return True

    def _persist_receipt(
        self,
        claim: RecipeImageAvailabilityClaim,
        receipt: RuntimeImageReceipt,
    ) -> None:
        with self._sessions.begin() as session:
            operation = self._require_claim(session, claim)
            operation_payload = self._payload(operation)
            if isinstance(operation_payload, Residue):
                return
            reference = self._image_reference_intent_for_claim(operation_payload, claim)
            if (
                operation_payload.image_reference_intent is not None
                and reference is None
            ):
                raise _AvailabilityClaimLost()
            try:
                require_reference_open(
                    session,
                    (ArtifactIdentity("runtime-image", receipt.oci_archive_sha256),),
                    now=self._clock(),
                )
            except ArtifactLifecycleError as error:
                raise RuntimeImagePreparationRefused(
                    error.code, error.detail, retryable=error.retryable
                ) from error
            operation.payload = serialize_json_value(
                operation_payload.model_copy(
                    update={"image_result": receipt, "image_reference_intent": None}
                )
            )
            self._set_progress(
                operation,
                "available",
                total_bytes=receipt.image_bytes,
                completed_bytes=receipt.image_bytes,
            )
            operation.updated_at = self._clock()

    def _persist_provisional_image_reference(
        self,
        claim: RecipeImageAvailabilityClaim,
        *,
        receipt: RuntimeImageReceipt,
    ) -> None:
        """Bind exact output identity before managed storage publication."""

        now = self._clock()
        reference = RuntimeImageReferenceIntent(
            schema_version=SCHEMA_VERSION,
            recipe_revision_id=claim.recipe_revision_id,
            oci_archive_sha256=receipt.oci_archive_sha256,
            image_digest=receipt.image_digest,
            image_bytes=receipt.image_bytes,
            operation_id=claim.operation_id,
            attempt=claim.execution_attempt,
            claim_owner=claim.claim_owner,
        )
        with self._sessions.begin() as session:
            operation = self._require_claim(
                session,
                claim,
                allowed_states=job_states.words(
                    LifecycleState.RUNNING, LifecycleState.OBSERVING
                ),
            )
            try:
                require_reference_open(
                    session,
                    (ArtifactIdentity("runtime-image", receipt.oci_archive_sha256),),
                    now=now,
                )
            except ArtifactLifecycleError as error:
                raise RuntimeImagePreparationRefused(
                    error.code, error.detail, retryable=error.retryable
                ) from error
            payload = self._payload(operation)
            if isinstance(payload, Residue):
                return
            stored = serialize_json_value(payload)
            if stored != operation.payload:
                operation.payload = stored
                operation.updated_at = now
            existing = payload.image_reference_intent
            if existing is None:
                payload = payload.model_copy(
                    update={"image_reference_intent": reference}
                )
                operation.payload = serialize_json_value(payload)
                operation.updated_at = now
            else:
                existing_reference = existing
                if existing_reference != reference:
                    if not same_image(existing_reference, reference):
                        _LOGGER.warning(
                            "availability operation %s now publishes archive %s "
                            "instead of %s",
                            reference.operation_id,
                            reference.oci_archive_sha256,
                            existing_reference.oci_archive_sha256,
                        )
                    # The current attempt's output leads (only the current
                    # claim reaches here). This callback runs under the exact
                    # archive publication lock. The prior attempt can no
                    # longer commit after this owner transfer; the exact bytes
                    # remain protected without opening a gap between
                    # provisional references.
                    payload = payload.model_copy(
                        update={"image_reference_intent": reference}
                    )
                    operation.payload = serialize_json_value(payload)
                    operation.updated_at = now

    @staticmethod
    def _image_reference_intent_for_claim(
        payload: AvailabilityJobPayload, claim: RecipeImageAvailabilityClaim
    ) -> RuntimeImageReferenceIntent | None:
        raw_reference = payload.image_reference_intent
        if raw_reference is None:
            return None
        reference = raw_reference
        if not reference.belongs_to(
            operation_id=claim.operation_id,
            recipe_revision_id=claim.recipe_revision_id,
            attempt=claim.execution_attempt,
            claim_owner=claim.claim_owner,
        ):
            raise _AvailabilityClaimLost()
        return reference

    def _set_progress(
        self,
        operation: Job,
        phase: str,
        *,
        total_bytes: int | None = None,
        completed_bytes: int = 0,
        bytes_per_second: float | None = None,
        eta_seconds: float | None = None,
        detail: OperationProgress | None = None,
    ) -> None:
        progress = _progress(
            phase,
            total_bytes=total_bytes,
            completed_bytes=completed_bytes,
            bytes_per_second=bytes_per_second,
            eta_seconds=eta_seconds,
        )
        payload = self._payload(operation)
        if isinstance(payload, Residue):
            return
        payload = payload.model_copy(update={"progress": progress})
        operation.payload = serialize_json_value(payload)

    def _update_progress(
        self,
        claim: RecipeImageAvailabilityClaim,
        phase: str,
        *,
        total_bytes: int | None = None,
        completed_bytes: int = 0,
        detail: OperationProgress | None = None,
    ) -> None:
        with self._sessions.begin() as session:
            operation = self._require_claim(session, claim)
            if detail is not None:
                completed_bytes = detail.completed_bytes
                total_bytes = detail.total_bytes
            payload = self._payload(operation)
            if isinstance(payload, Residue):
                return
            previous = payload.progress
            self._set_progress(
                operation,
                phase,
                total_bytes=total_bytes,
                completed_bytes=max(previous.completed_bytes, completed_bytes),
                bytes_per_second=detail.bytes_per_second if detail else None,
                eta_seconds=detail.eta_seconds if detail else None,
            )
            operation.updated_at = self._clock()

    @staticmethod
    def _record_blockers(
        operation: Job,
        payload: AvailabilityJobPayload,
        blockers: Sequence[OperationBlocker],
    ) -> AvailabilityJobPayload:
        """Return the typed payload; log wait reasons only when they change."""
        before = {(item.code, tuple(item.node_ids)) for item in payload.blockers or []}
        stored = bound_blockers(blockers)
        after = {(item.code, tuple(item.node_ids)) for item in stored}
        if before != after and stored:
            _LOGGER.log(
                logging.WARNING
                if any(item.severity == "error" for item in stored)
                else logging.INFO,
                "recipe image preparation %s is waiting: %s",
                operation.id,
                "; ".join(f"{item.code}: {item.detail}" for item in stored[:4]),
            )
        return payload.model_copy(update={"blockers": stored})

    def note_waiting_for_worker(self, busy: int) -> None:
        """Say why queued preparations are not running: every worker is busy."""

        with self._sessions.begin() as session:
            for operation in session.scalars(
                select(Job)
                .where(
                    Job.kind == OPERATION_KIND,
                    Job.state == "queued",
                    Job.current_attempt == 0,
                )
                .order_by(Job.created_at, Job.id)
                .limit(16)
                .with_for_update(skip_locked=True)
            ):
                payload = self._payload(operation)
                if isinstance(payload, Residue) or payload.blockers:
                    continue
                payload = self._record_blockers(
                    operation,
                    payload,
                    [
                        make_blocker(
                            RecipeImageCode.WAITING_FOR_WORKER,
                            f"All {busy} image preparation workers are busy preparing "
                            "other runtime images; this starts when one is free.",
                            severity="info",
                        )
                    ],
                )
                operation.payload = serialize_json_value(payload)

    def _fail(
        self,
        claim: RecipeImageAvailabilityClaim,
        error: BaseException | BuildUnsettled,
    ) -> None:
        retryable = _retryable(error)
        code = _failure_code(error)
        detail = _failure_detail(error)
        step = getattr(error, "step", None)
        retry_after = _retry_after(error)
        preserved_retry_time = getattr(error, "retry_time", None)
        if isinstance(step, str) and step.strip():
            detail = f"{step.strip()}: {detail}"
        excerpt = _log_excerpt(error)
        with self._sessions.begin() as session:
            operation = self._claim_operation(session, claim)
            if operation is None:
                _LOGGER.warning(
                    "recipe image preparation %s failed after its claim was lost "
                    "(%s); its next claim will retry",
                    claim.operation_id,
                    _failure_code(error),
                )
                return
            payload = self._payload(operation)
            if isinstance(payload, Residue):
                return
            retry = payload.retry
            automatic_attempts = retry.automatic_attempts
            dependency_wait = str(code) in _DEPENDENCY_WAIT_CODES
            bounded = retryable
            retry = retry.model_copy(
                update={
                    "automatic_attempts": automatic_attempts + int(not dependency_wait)
                }
            )
            now = self._clock()
            now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            # The core decides the retry (rule 1) on its one bounded, jittered
            # clock; the error's own delay (``Retry-After``) is only the floor.
            floor = (
                now + timedelta(seconds=retry_after)
                if retry_after is not None
                else None
            )
            if isinstance(preserved_retry_time, str):
                try:
                    preserved = datetime.fromisoformat(preserved_retry_time)
                except ValueError:
                    preserved = None
                if preserved is not None:
                    preserved = (
                        preserved
                        if preserved.tzinfo is not None
                        else preserved.replace(tzinfo=UTC)
                    )
                    floor = preserved if floor is None else max(floor, preserved)
            decided = self._lifecycle.plan_failure(
                operation,
                now,
                retryable=retryable,
                retry_after=floor,
                count=automatic_attempts,
            )
            if decided.state is State.BACKOFF and decided.next_action_at is not None:
                retry_after = max(
                    0, int((decided.next_action_at - now).total_seconds() + 0.999)
                )
                preserved_retry_time = _iso(decided.next_action_at)
            # A definite end keeps whatever the error itself stated.
            required_bytes = getattr(error, "required_bytes", None)
            free_bytes = getattr(error, "free_bytes", None)
            shortfall_bytes = getattr(error, "shortfall_bytes", None)
            if required_bytes is None:
                required_bytes = getattr(error, "disk_required_bytes", None)
            if free_bytes is None:
                free_bytes = getattr(error, "disk_free_bytes", None)
            if (
                shortfall_bytes is None
                and type(required_bytes) is int
                and type(free_bytes) is int
            ):
                shortfall_bytes = max(0, required_bytes - free_bytes)
            failure = AvailabilityOperationFailure.model_validate(
                sanitize_failure_evidence(
                    {
                        "code": str(code)[:64],
                        "detail": str(detail)[:512],
                        "recovery_actions": list(getattr(error, "recovery_actions", ()))
                        or _recovery_actions(str(code), retryable),
                        "retryable": retryable,
                        "retry_time": preserved_retry_time
                        if isinstance(preserved_retry_time, str)
                        else (
                            _iso(now + timedelta(seconds=retry_after))
                            if retry_after is not None
                            else None
                        ),
                        "retry_after_seconds": retry_after,
                        "log_excerpt": excerpt,
                        "required_bytes": required_bytes
                        if type(required_bytes) is int and required_bytes >= 0
                        else None,
                        "free_bytes": free_bytes
                        if type(free_bytes) is int and free_bytes >= 0
                        else None,
                        "shortfall_bytes": shortfall_bytes
                        if type(shortfall_bytes) is int and shortfall_bytes >= 0
                        else None,
                    }
                )
            )
            operation.result = None
            payload = self._payload(operation)
            if isinstance(payload, Residue):
                return
            reference = self._image_reference_intent_for_claim(payload, claim)
            if payload.image_reference_intent is not None and reference is None:
                return
            payload = payload.model_copy(update={"image_reference_intent": None})
            payload = payload.model_copy(update={"retry": retry, "failure": failure})
            blockers = list(getattr(error, "blockers", ())) or [
                make_blocker(
                    str(code),
                    str(detail),
                    severity="warning" if bounded else "error",
                )
            ]
            payload = self._record_blockers(operation, payload, blockers)
            if bounded and (
                str(code) in _INTEGRITY_FAILURE_CODES or is_redownload(str(code))
            ):
                # Never reuse bytes that failed verification: the automatic
                # retry downloads or builds them again.
                payload = payload.model_copy(update={"force_rebuild": True})
            dependency = payload.build_dependency
            settled_build_id = getattr(error, "settled_build_operation_id", None)
            exact_build_settled = (
                isinstance(settled_build_id, str)
                and dependency is not None
                and str(dependency.operation_id) == settled_build_id
            )
            if exact_build_settled or str(code) == RuntimeImageCode.CACHE_MISSING:
                # The failed effect is settled; a later execution claim may
                # create a new child. Observation waits retain the exact child.
                payload = payload.model_copy(update={"build_dependency": None})
            payload = payload.model_copy(update={"retry_after_at": None})
            payload = payload.model_copy(update={"claim_owner": None})
            payload = payload.model_copy(update={"claim_until": None})
            updated = self._lifecycle.commit(
                operation, decided, now, reason=str(detail), payload=payload
            )
            assert updated is not None
            operation.payload = serialize_json_value(updated)

    def _unknown_view(
        self, operation: Job, residue: Residue
    ) -> RecipeImageAvailabilityView:
        identity = _read(
            StoredAvailabilityIdentity, operation.payload, subject=operation.id
        )
        return RecipeImageAvailabilityView(
            id=operation.id,
            request_id=operation.request_id,
            request=identity.request if identity else None,
            kind=operation.kind,
            state=LifecycleState.BACKOFF,
            attempt=int(operation.current_attempt or 0),
            recipe_revision_id=identity.recipe_revision_id
            if identity
            else operation.authority_revision,
            recipe_content_sha256=identity.recipe_content_sha256 if identity else None,
            model_digest=None,
            build_input_sha256=None,
            progress=_progress(ProgressPhase.WAITING),
            image_progress=None,
            result=None,
            failure=None,
            supported_actions=(),
            created_at=_iso(operation.created_at),
            updated_at=_iso(operation.updated_at),
            cancellation=self._stored_cancellation(operation),
            residue=residue,
        )

    def _view(self, operation: Job) -> RecipeImageAvailabilityView:
        payload = self._payload(operation)
        if isinstance(payload, Residue):
            return self._unknown_view(operation, payload)
        stored_result = read_row_column(operation, "result")
        result = (
            stored_result if isinstance(stored_result, AvailabilityJobResult) else None
        )
        model_child = self._current_model_child(payload)
        image_progress = payload.progress
        image_result = payload.image_result
        image_ready = image_result is not None
        image_state = "succeeded" if image_ready else operation.state
        failure = payload.failure
        state = operation.state
        residue = None
        if (
            state == "succeeded"
            and (result is None or failure is not None)
            or state == "failed"
            and failure is None
        ):
            # Missing terminal evidence is typed unknown, not confirmed success
            # or an assertion that this terminal row is executing again.
            residue = retire_as_unknown(
                "recipe-image.result", operation.id, BookkeepingReason.EVIDENCE_MISMATCH
            )
            state = LifecycleState.BACKOFF
            result = None
        if state != "succeeded":
            result = None
        if image_result is not None:
            image_progress = image_progress.model_copy(
                update={
                    "phase": "available",
                    "completed_bytes": image_result.image_bytes,
                    "total_bytes": image_result.image_bytes,
                    "total_bytes_known": True,
                    "bytes_per_second": None,
                    "eta_seconds": None,
                }
            )
        shared = (
            OperationProgress.model_fields.keys()
            & OperationMemberProgress.model_fields.keys()
        )
        members = [
            OperationMemberProgress.model_validate_json(
                canonical_message(
                    image_progress.model_dump(mode="json", include=shared)
                    | {"member_id": "runtime-image", "state": image_state}
                )
            )
        ]
        if model_child is not None and model_child.progress is not None:
            members.append(
                OperationMemberProgress.model_validate_json(
                    canonical_message(
                        model_child.progress.model_dump(mode="json", include=shared)
                        | {"member_id": "model-cache", "state": model_child.state}
                    )
                )
            )
        progress = (
            aggregate_progress(members)
            if len(members) > 1
            else image_progress.model_copy(update={"members": members})
        )
        next_attempt_at = (
            _iso(payload.retry_after_at)
            if payload.retry_after_at is not None
            and state in job_states.words(LifecycleState.QUEUED, LifecycleState.BACKOFF)
            else None
        )
        return RecipeImageAvailabilityView(
            id=operation.id,
            request_id=operation.request_id,
            request=payload.request,
            kind=operation.kind,
            state=state,
            attempt=int(operation.current_attempt),
            recipe_revision_id=payload.recipe_revision_id,
            recipe_content_sha256=payload.recipe_content_sha256,
            model_digest=payload.model_digest,
            build_input_sha256=payload.build_input_sha256,
            progress=progress,
            image_progress=image_progress,
            image_state=image_state,
            image_failure=None if image_ready else failure,
            result=result,
            failure=failure,
            supported_actions=tuple(action.value for action in failure.recovery_actions)
            if failure
            else (),
            created_at=_iso(operation.created_at),
            updated_at=_iso(operation.updated_at),
            model_child=model_child,
            cancellation=self._stored_cancellation(operation),
            blockers=tuple(payload.blockers or [])
            if state
            in job_states.words(
                LifecycleState.QUEUED, LifecycleState.BACKOFF, LifecycleState.FAILED
            )
            else (),
            next_attempt_at=next_attempt_at,
            residue=residue,
        )


__all__ = [
    "OPERATION_KIND",
    "REMOVE_OPERATION_KIND",
    "SUPERSEDED_PREPARATION_CODE",
    "RecipeAuthorityResolver",
    "RecipeImageAvailabilityClaim",
    "RecipeImageAvailabilityError",
    "RecipeImageAvailabilityService",
    "RecipeImageAvailabilityView",
    "RecipeImageBuilder",
]
