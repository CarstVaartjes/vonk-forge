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
import re
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, aliased, sessionmaker
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import (
    OperationMemberProgress,
    OperationProgress,
    canonical_message,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    RemovalOwnerKind,
    check_removal_fence_nowait,
    clear_removal,
    lock_removal_fences,
    require_reference_open,
    reserve_removal_owners,
    retryable_artifact_database_error,
)
from .artifact_reference_scan import (
    MAX_ARTIFACT_OWNER_SCAN_BYTES,
    runtime_image_reference_reasons,
)
from .bounded_json import mapping, require_mapping, require_sequence
from .catalog_queries import active_head_revision
from .catalog_revision_contract import read_catalog_document
from .model_cache import (
    ModelCacheConflict,
    ModelCacheError,
    ModelCacheNotFound,
    ModelCacheRemovalScope,
)
from .model_cache_contract import ModelCacheCancellation, ModelCacheRemovalResult
from .model_cache_progress import project_cache_progress
from .models import (
    CatalogDocumentHead,
    CatalogDocumentRevision,
    Job,
    ModelCacheOperation,
    RecipeBuild,
    RuntimeImageAuthorization,
)
from .operation_contract import (
    AvailabilityOperationFailure,
    normalize_operation_progress,
    sanitize_failure_evidence,
)
from .operation_progress import aggregate_progress
from .recipe_availability_intent import (
    RecipeAvailabilityIntent,
    RecipeRetryIntent,
    RecipeRevisionIntent,
    RecipeSelectorIntent,
    read_availability_intent,
)
from .recipe_build_cancellation import (
    BuildConsumerError,
    current_build_consumers,
    lock_availability_build_dependency,
    lock_build_dependency,
    request_build_cancellation,
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
from .runtime_image_preparation import (
    OCIImageTransport,
    RuntimeImagePreparationError,
    RuntimeImageReceipt,
    RuntimeImageReferenceIntent,
    RuntimeImageStorage,
    persist_runtime_image_receipt,
    prepare_runtime_image,
    read_runtime_image_reference_intent,
)
from .strict_json import serialize_json_value

if TYPE_CHECKING:
    from .recipe_update_batches import RecipeUpdateClaim

SCHEMA_VERSION = 2
OPERATION_KIND = "recipe.image.availability.v2"
REMOVE_OPERATION_KIND = RECIPE_CACHE_REMOVE_KIND
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANCELLATION_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_MAX_AUTOMATIC_ATTEMPTS = 3
_MAX_OPERATOR_RETRIES = 3
_TERMINAL_FAILURE_CODES = frozenset(
    {
        "recipe_image.identity_conflict",
        "recipe_image.metadata_stale",
        "recipe_image.recipe_invalid",
        "recipe_image.recipe_unavailable",
        "recipe_image.runtime_invalid",
        "runtime_image.digest_mismatch",
        "runtime_image.archive_mismatch",
        "runtime_image.archive_conflict",
        "runtime_image.receipt_identity_conflict",
        "runtime_image.authorization_invalid",
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
        raise RecipeImageAvailabilityError(
            "recipe_image.operation_invalid",
            "stored removal retry time is malformed",
        ) from error
    retry_time = (
        retry_time if retry_time.tzinfo is not None else retry_time.replace(tzinfo=UTC)
    )
    return now >= retry_time


_CAPACITY_FAILURE_CODES = frozenset(
    {
        "build.insufficient_disk",
        "build.insufficient_memory",
        "recipe_image.insufficient_disk",
        "recipe_image.insufficient_memory",
        "runtime_image.insufficient_disk",
    }
)
_INTEGRITY_FAILURE_CODES = frozenset(
    {
        "registry.digest_mismatch",
        "recipe_package.digest_mismatch",
        "runtime_image.digest_mismatch",
        "runtime_image.archive_mismatch",
        "runtime_image.archive_conflict",
        "runtime_image.evidence_invalid",
    }
)
_DEPENDENCY_WAIT_CODES = frozenset(
    {
        "recipe_image.build_capacity_wait",
        "runtime_image.transfer_contended",
        "runtime_image.publication_contended",
        "build.consumer_busy",
        "build.cancellation_pending",
        "recipe_image.build_wait",
    }
)
# A newer preparation for the same recipe supersedes an older one that has not
# started.  The cancellation is recorded on the operation itself (terminal
# state, typed failure evidence, and a bounded status reason) so newer intent
# wins visibly and the older attempt never consumes a builder or a queue slot.
SUPERSEDED_PREPARATION_CODE = "recipe_image.superseded_by_newer_revision"
# Verified cache bytes can disappear (NAS restore, eviction, partial cleanup).
# That is ordinary cache loss, not corruption: it must re-prepare, never ask an
# operator to inspect a terminal failure.
_RECOVERABLE_MISS_CODES = frozenset({"runtime_image.cache_missing"})


class RecipeImageAvailabilityError(RuntimeError):
    """A bounded operator-facing availability failure."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        retry_time: str | None = None,
        recovery_actions: Sequence[str] = (),
        log_excerpt: str | None = None,
        step: str | None = None,
        required_bytes: int | None = None,
        free_bytes: int | None = None,
        shortfall_bytes: int | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.retry_time = retry_time
        self.recovery_actions = tuple(recovery_actions)
        self.log_excerpt = log_excerpt
        self.step = step
        self.required_bytes = required_bytes
        self.free_bytes = free_bytes
        self.shortfall_bytes = shortfall_bytes
        super().__init__(detail)


class RecipeAuthorityResolver(Protocol):
    """Refresh and resolve the selected canonical Recipe in one operation."""

    def __call__(
        self, recipe_revision_id: str, *, force: bool = False
    ) -> tuple[RecipeDefinition | Mapping[str, object], Mapping[str, object]]: ...


class RecipeImageBuilder(Protocol):
    """Build the exact source recipe and report bounded progress."""

    def __call__(
        self,
        recipe: RecipeDefinition,
        runtime: Mapping[str, object],
        *,
        claim: RecipeImageAvailabilityClaim,
        build_input_sha256: str,
        force: bool,
        progress: Callable[[Mapping[str, object]], None],
    ) -> Mapping[str, object]: ...


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
    progress: Mapping[str, object]
    artifact_set_sha256: str | None
    plan_digest: str | None
    failure: Mapping[str, object] | None
    result: object | None


class ModelCacheRemovalCoordinator(Protocol):
    """Exact model-cache scope/owner seam used by recipe removal."""

    def recipe_removal_scope_in_session(
        self, session: Session, *, recipe_revision_id: str
    ) -> ModelCacheRemovalScope | None: ...

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
class RecipeImageAvailabilityView:
    id: str
    request_id: str
    request: RecipeAvailabilityIntent
    kind: str
    state: str
    attempt: int
    recipe_revision_id: str
    recipe_content_sha256: str
    model_digest: str | None
    build_input_sha256: str | None
    progress: Mapping[str, object]
    image_progress: Mapping[str, object] | None
    result: Mapping[str, object] | None
    failure: Mapping[str, object] | None
    supported_actions: tuple[str, ...]
    created_at: str
    updated_at: str
    model_child: Mapping[str, object] | None = None
    image_state: str | None = None
    image_failure: Mapping[str, object] | None = None
    cancellation: RecipeOperationCancellationResult | None = None

    def document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "request_id": self.request_id,
            "request": serialize_json_value(self.request),
            "kind": self.kind,
            "state": self.state,
            "attempt": self.attempt,
            "recipe_revision_id": self.recipe_revision_id,
            "recipe_content_sha256": self.recipe_content_sha256,
            "model_digest": self.model_digest,
            "build_input_sha256": self.build_input_sha256,
            "progress": dict(self.progress),
            "image_progress": None
            if self.image_progress is None
            else dict(self.image_progress),
            "result": None if self.result is None else dict(self.result),
            "failure": None if self.failure is None else dict(self.failure),
            "cancellation": (
                None
                if self.cancellation is None
                else self.cancellation.model_dump(mode="json", exclude_none=True)
            ),
            "supported_actions": list(self.supported_actions),
            "children": ([] if self.model_child is None else [dict(self.model_child)]),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class RecipeImageAvailabilityClaim:
    """Small scheduler hook returned for execution outside the worker tick."""

    operation_id: str
    recipe_revision_id: str
    image_identity: str | None
    build_input_sha256: str | None
    claim_owner: str
    execution_attempt: int


class _AvailabilityClaimLost(RuntimeImagePreparationError):
    """Stop an executor whose durable attempt can no longer accept writes."""

    def __init__(self) -> None:
        # Preserve this control outcome through image preparation's typed
        # exception boundary; contention must not become a transfer failure.
        super().__init__(
            "recipe_image.claim_lost", "availability execution claim is unavailable"
        )


def _iso(value: datetime) -> str:
    value = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _digest(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise RecipeImageAvailabilityError(
            "recipe_image.identity_invalid",
            f"{field} must be a lowercase SHA-256 digest",
        )
    return value


def _optional_digest(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return _digest(value, field=field)


def _canonical_cancellation_id(value: object) -> str:
    if not isinstance(value, str) or not _CANCELLATION_UUID.fullmatch(value):
        raise RecipeImageAvailabilityError(
            "recipe_image.cancellation_invalid",
            "cancellation request identity must be a canonical UUID",
        )
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError("noncanonical UUID")
    except ValueError as error:
        raise RecipeImageAvailabilityError(
            "recipe_image.cancellation_invalid",
            "cancellation request identity must be a canonical UUID",
        ) from error
    return value


def _canonical_recipe(value: object) -> RecipeDefinition:
    if isinstance(value, RecipeDefinition):
        return value
    if not isinstance(value, Mapping):
        raise RecipeImageAvailabilityError(
            "recipe_image.recipe_invalid",
            "selected recipe is not a canonical RecipeDefinition",
        )
    try:
        return RecipeDefinition.model_validate(value)
    except Exception as error:
        raise RecipeImageAvailabilityError(
            "recipe_image.recipe_invalid",
            "selected recipe is not a canonical RecipeDefinition",
        ) from error


def _image_identity(recipe: RecipeDefinition) -> str | None:
    if recipe.execution.mode != "image" or recipe.execution.image is None:
        return None
    return f"sha256:{recipe.execution.image.digest}"


def _known_total(runtime: Mapping[str, object]) -> int | None:
    for key in ("image_bytes", "expected_bytes", "total_bytes"):
        value = runtime.get(key)
        if type(value) is int and value > 0:
            return value
    return None


def _progress(
    phase: str,
    *,
    completed_bytes: int = 0,
    total_bytes: int | None = None,
    bytes_per_second: float | None = None,
    eta_seconds: float | None = None,
    checkpoint: Mapping[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "phase": phase,
        "completed_bytes": max(0, completed_bytes),
        "total_bytes_known": total_bytes is not None,
    }
    if total_bytes is not None:
        value["total_bytes"] = total_bytes
    if bytes_per_second is not None:
        value["bytes_per_second"] = bytes_per_second
    if eta_seconds is not None:
        value["eta_seconds"] = eta_seconds
    if checkpoint is not None:
        value["checkpoint"] = dict(checkpoint)
    return normalize_operation_progress(value)


def _retryable(error: BaseException) -> bool:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in _TERMINAL_FAILURE_CODES:
        return False
    if isinstance(code, str) and code in _RECOVERABLE_MISS_CODES:
        return True
    if getattr(error, "retryable", False) is True:
        return True
    status = getattr(error, "status_code", None)
    if type(status) is int:
        return status == 429 or status >= 500
    text = f"{getattr(error, 'code', '')} {getattr(error, 'detail', str(error))}".casefold()
    return isinstance(error, (OSError, TimeoutError, ConnectionError)) or any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "connection",
            "network",
            "transport",
            "temporarily",
            "copy",
        )
    )


def _failure_code(error: BaseException) -> str:
    """Return the stable operation failure code for an exception.

    Only the repository's own operation failures may contribute ``code``.
    A library exception can carry an unrelated attribute of the same name --
    ``sqlalchemy.exc.IntegrityError.code`` is the ``gkpj`` documentation slug --
    and copying it hides the failure class behind an opaque token that matches
    no recovery action and no operator instruction. Anything the database
    layer raises is therefore reported by its exception class name.
    """

    code = getattr(error, "code", None)
    if isinstance(error, SQLAlchemyError) or not isinstance(code, str) or not code:
        return type(error).__name__.lower()
    return code


def _failure_detail(error: BaseException) -> str:
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


def _retry_after(error: BaseException) -> int | None:
    value = getattr(error, "retry_after_seconds", None)
    if type(value) is int and 0 <= value <= 86_400:
        return value
    return None


def _log_excerpt(error: BaseException) -> str | None:
    value = getattr(error, "log_excerpt", None)
    if not isinstance(value, str) or not value.strip():
        value = getattr(error, "detail", None)
    if not isinstance(value, str) or not value.strip():
        value = _failure_detail(error)
    if not isinstance(value, str) or not value.strip():
        return None
    return value[:1024]


def _recovery_actions(
    payload: Mapping[str, object], code: str, retryable: bool
) -> list[str]:
    """Map stable failure classes to UI action identifiers."""

    mode = payload.get("execution_mode")
    if code in _CAPACITY_FAILURE_CODES:
        return ["free_space"]
    if code in _INTEGRITY_FAILURE_CODES:
        return ["download_again"] if mode == "image" else ["force_rebuild"]
    if code in _RECOVERABLE_MISS_CODES:
        return ["download_again"] if mode == "image" else ["force_rebuild"]
    if retryable:
        resumable = mode == "image" and (
            code.startswith(("registry.", "runtime_image.transport"))
            or code
            in {"recipe_image.download_interrupted", "recipe_image.network_error"}
        )
        return ["resume", "retry"] if resumable else ["retry"]
    return ["inspect"]


class RecipeImageAvailabilityService:
    """Persist and execute exact recipe-image availability operations.

    ``authority`` must perform the latest metadata refresh and return the
    selected revision's canonical recipe plus its compiled runtime projection.
    ``builder`` is called only for source-build recipes; direct-image recipes
    use :func:`prepare_runtime_image` and the existing OCI transport.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        storage: RuntimeImageCacheStorage,
        authority: RecipeAuthorityResolver,
        transport: OCIImageTransport | None = None,
        builder: RecipeImageBuilder | None = None,
        clock: Callable[[], datetime],
        receipt_writer: Callable[[Session, str, str, str, RuntimeImageReceipt], object]
        | None = None,
        model_cache: Any | None = None,
        automatic_attempt_limit: int = _MAX_AUTOMATIC_ATTEMPTS,
        operator_retry_limit: int = _MAX_OPERATOR_RETRIES,
        max_parallel: int = 4,
        max_parallel_builds: int = 1,
        builder_admission: Callable[[RecipeDefinition, Mapping[str, object]], None]
        | None = None,
        claim_lease_seconds: int = 120,
    ) -> None:
        if not 1 <= automatic_attempt_limit <= 8:
            raise ValueError("automatic attempt limit is invalid")
        if not 0 <= operator_retry_limit <= 8:
            raise ValueError("operator retry limit is invalid")
        if not 1 <= max_parallel <= 16:
            raise ValueError("availability parallelism is invalid")
        if not 1 <= max_parallel_builds <= max_parallel:
            raise ValueError("availability build parallelism is invalid")
        if not 10 <= claim_lease_seconds <= 3_600:
            raise ValueError("availability claim lease is invalid")
        self._sessions = sessions
        self._storage = storage
        self._authority = authority
        self._transport = transport
        self._builder = builder
        self._clock = clock
        self._receipt_writer = receipt_writer
        self._model_cache = model_cache
        self._automatic_attempt_limit = automatic_attempt_limit
        self._operator_retry_limit = operator_retry_limit
        self._max_parallel = max_parallel
        self._max_parallel_builds = max_parallel_builds
        self._builder_admission = builder_admission
        self._claim_lease_seconds = claim_lease_seconds
        self._identity_locks: dict[str, threading.Lock] = {}
        self._identity_locks_guard = threading.Lock()
        self._removal_lock = threading.RLock()
        from .recipe_update_batches import RecipeUpdateBatches

        self._updates = RecipeUpdateBatches(self, sessions)

    def _resolve_recipe_selector(self, selector: str) -> str:
        """Resolve logical selectors to the current head, retaining exact pins."""

        if not isinstance(selector, str) or not 1 <= len(selector.strip()) <= 256:
            raise RecipeImageAvailabilityError(
                "recipe_image.selector_invalid", "recipe selector is required"
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
            raise RecipeImageAvailabilityError(
                "recipe_image.selector_missing", "recipe selector was not found"
            )
        if len(rows) != 1:
            raise RecipeImageAvailabilityError(
                "recipe_image.selector_ambiguous",
                "recipe selector matches multiple recipes",
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
    def _removal_payload_digest(payload: Mapping[str, object]) -> str:
        encoded = json.dumps(
            serialize_json_value(dict(payload)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _read_removal_owner(self, operation: Job) -> RecipeCacheRemovalOwner:
        try:
            encoded = canonical_message(operation.payload)
            if len(encoded) > MAX_ARTIFACT_OWNER_SCAN_BYTES:
                raise ValueError(
                    "stored recipe removal owner exceeds the scan byte budget"
                )
            owner = RecipeCacheRemovalOwner.model_validate_json(encoded)
        except (TypeError, ValueError) as error:
            raise RecipeImageAvailabilityError(
                "recipe_image.operation_invalid",
                "stored recipe removal owner is malformed",
            ) from error
        plan = owner.plan
        intent = plan.intent
        payload = plan.model_dump(mode="json")
        if (
            operation.kind != REMOVE_OPERATION_KIND
            or operation.state
            not in {"queued", "running", "partial", "succeeded", "failed", "cancelled"}
            or operation.actor != intent.actor
            or operation.request_id != intent.request_key
            or operation.authority_revision != intent.recipe_revision_id
            or operation.payload_digest != self._removal_payload_digest(payload)
            or operation.targets != []
        ):
            raise RecipeImageAvailabilityError(
                "recipe_image.operation_invalid",
                "stored recipe removal plan does not match its Job owner",
            )
        if operation.state in {"queued", "running", "partial"} and (
            owner.checkpoint.failure is not None
            and not owner.checkpoint.failure.retryable
        ):
            raise RecipeImageAvailabilityError(
                "recipe_image.operation_invalid",
                "active recipe removal has a terminal failure checkpoint",
            )
        if operation.state == "failed" and (
            owner.checkpoint.failure is None or owner.checkpoint.failure.retryable
        ):
            raise RecipeImageAvailabilityError(
                "recipe_image.operation_invalid",
                "failed recipe removal has no terminal failure checkpoint",
            )
        return owner

    def _read_removal_intent(self, operation: Job) -> RecipeCacheRemovalIntent:
        return self._read_removal_owner(operation).plan.intent

    @staticmethod
    def _removal_progress_document(
        operation: Job,
        owner: RecipeCacheRemovalOwner,
    ) -> dict[str, object]:
        checkpoint = owner.checkpoint
        total_items = len(owner.plan.image_archives) + len(owner.plan.model_children)
        completed_items = checkpoint.image_index + checkpoint.model_index
        reclaimed_bytes = (
            checkpoint.image_reclaimed_bytes + checkpoint.model_reclaimed_bytes
        )
        successful = operation.state == "succeeded"
        progress = OperationProgress(
            phase=(
                "completed"
                if successful
                else "failed"
                if operation.state in {"failed", "cancelled"}
                else "reclaiming"
            ),
            completed_bytes=reclaimed_bytes,
            total_bytes=reclaimed_bytes if successful else None,
            total_bytes_known=successful,
            completed_items=completed_items,
            total_items=total_items,
            observed_at=_iso(operation.updated_at),
            activity="active" if operation.state == "running" else "waiting",
        )
        return progress.model_dump(mode="json", exclude_none=True)

    def _read_removal_result(
        self, operation: Job, intent: RecipeCacheRemovalIntent
    ) -> dict[str, object]:
        owner = self._read_removal_owner(operation)
        if owner.plan.intent != intent:
            raise RecipeImageAvailabilityError(
                "recipe_image.operation_invalid",
                "stored removal projection intent changed",
            )
        if operation.state == "succeeded":
            if not isinstance(operation.result, Mapping):
                raise RecipeImageAvailabilityError(
                    "recipe_image.operation_invalid",
                    "successful removal has no stored result",
                )
            try:
                result = RecipeCacheRemovalResult.model_validate_json(
                    json.dumps(
                        serialize_json_value(operation.result),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            except (TypeError, ValidationError) as error:
                raise RecipeImageAvailabilityError(
                    "recipe_image.operation_invalid",
                    "stored recipe removal result is malformed",
                ) from error
            if (
                result.action != intent.action
                or result.selector != intent.selector
                or result.request_key != intent.request_key
                or result.operation_id != operation.id
                or result.recipe_revision_id != intent.recipe_revision_id
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
                raise RecipeImageAvailabilityError(
                    "recipe_image.operation_invalid",
                    "stored removal result does not match its accepted intent",
                )
            return result.model_dump(mode="json") | {
                "progress": self._removal_progress_document(operation, owner),
            }
        if operation.result is not None:
            raise RecipeImageAvailabilityError(
                "recipe_image.operation_invalid",
                "unfinished recipe removal has a terminal result",
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "action": intent.action,
            "selector": intent.selector,
            "request_key": intent.request_key,
            "operation_id": operation.id,
            "recipe_revision_id": intent.recipe_revision_id,
            "with_model": intent.with_model,
            "state": operation.state,
            "progress": self._removal_progress_document(operation, owner),
            "reclaimed_bytes": (
                owner.checkpoint.image_reclaimed_bytes
                + owner.checkpoint.model_reclaimed_bytes
            ),
            "preserved": ["profile-assignments", "spark-local-copies"]
            + ([] if intent.with_model else ["model-download"]),
            "failure": (
                None
                if owner.checkpoint.failure is None
                else owner.checkpoint.failure.model_dump(mode="json")
            ),
            "next_actions": (
                []
                if owner.checkpoint.failure is None
                else [
                    action.value for action in owner.checkpoint.failure.recovery_actions
                ]
            ),
            "cancelled_operations": [],
            "cancelled_builds": [],
            "model_removals": [
                child.operation_id for child in owner.plan.model_children
            ],
        }

    def _replay_removal(
        self,
        operation: Job,
        *,
        selector: str,
        actor: str,
        request_id: str,
        with_model: bool,
    ) -> dict[str, object]:
        if operation.kind != REMOVE_OPERATION_KIND:
            raise RecipeImageAvailabilityError(
                "recipe_image.request_key_reused",
                "request key was already used for another operation",
            )
        owner = self._read_removal_owner(operation)
        intent = owner.plan.intent
        if (
            intent.selector != selector
            or intent.actor != actor
            or intent.request_key != request_id
            or intent.with_model is not with_model
        ):
            raise RecipeImageAvailabilityError(
                "recipe_image.request_key_reused",
                "request key was already used for another removal intent",
            )
        return self._read_removal_result(operation, intent)

    def remove_selector(
        self,
        selector: str,
        *,
        actor: str,
        request_id: str,
        with_model: bool = False,
    ) -> dict[str, object]:
        """Accept one exact, restart-safe cache removal before any byte effect."""

        if not isinstance(selector, str) or not 1 <= len(selector.strip()) <= 256:
            raise RecipeImageAvailabilityError(
                "recipe_image.selector_invalid", "recipe selector is required"
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

                revision_id = self._resolve_recipe_selector_in_session(
                    session, selector
                )
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
                    raise RecipeImageAvailabilityError(
                        "recipe_image.selector_missing",
                        "selected recipe revision is not active",
                    )
                try:
                    recipe = read_catalog_document(revision)
                    if not isinstance(recipe, RecipeDefinition):
                        raise TypeError("catalog revision is not a Recipe")
                except (TypeError, ValueError) as error:
                    raise RecipeImageAvailabilityError(
                        "recipe_image.recipe_invalid",
                        "selected canonical recipe revision is invalid",
                    ) from error

                authorizations = tuple(
                    session.scalars(
                        select(RuntimeImageAuthorization)
                        .where(
                            RuntimeImageAuthorization.recipe_revision_id == revision_id
                        )
                        .order_by(
                            RuntimeImageAuthorization.oci_archive_sha256,
                            RuntimeImageAuthorization.id,
                        )
                    )
                )
                image_archives = tuple(
                    sorted(
                        {
                            _digest(
                                authorization.oci_archive_sha256,
                                field="runtime image archive digest",
                            )
                            for authorization in authorizations
                        }
                    )
                )
                for authorization in authorizations:
                    if (
                        authorization.original_content_digest != revision.content_digest
                        or authorization.effective_execution_key
                        != revision.execution_key
                        or authorization.state not in {"authorized", "revoked"}
                    ):
                        raise RecipeImageAvailabilityError(
                            "runtime_image.authorization_invalid",
                            "stored recipe image authorization does not match its exact revision",
                        )

                model_scope: ModelCacheRemovalScope | None = None
                if with_model:
                    if self._model_cache is None:
                        raise RecipeImageAvailabilityError(
                            "model_cache.unavailable",
                            "model cache removal is unavailable",
                        )
                    model_scope = cast(
                        ModelCacheRemovalCoordinator, self._model_cache
                    ).recipe_removal_scope_in_session(
                        session, recipe_revision_id=revision_id
                    )

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

                try:
                    references = runtime_image_reference_reasons(
                        session, image_archives
                    )
                except ArtifactLifecycleError as error:
                    raise RecipeImageAvailabilityError(
                        error.code,
                        error.detail,
                        retryable=error.retryable,
                        recovery_actions=("retry",) if error.retryable else (),
                    ) from error
                blocked = {
                    archive: reasons
                    for archive, reasons in references.items()
                    if reasons
                }
                if blocked:
                    archive = min(blocked)
                    raise RecipeImageAvailabilityError(
                        "recipe_image.removal_referenced",
                        f"runtime image {archive} is still referenced: "
                        + ", ".join(blocked[archive][:4]),
                        retryable=True,
                        recovery_actions=("retry",),
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
                        raise RecipeImageAvailabilityError(
                            "model_cache.removal_scope_changed",
                            "model cache scope changed before the recipe removal was accepted",
                        )
                    child_id, accepted_key, selected_sets, plan_digest = accepted_child
                    if (
                        child_id != model_operation_id
                        or accepted_key != child_request_key
                        or selected_sets != model_scope.selected_sets
                    ):
                        raise RecipeImageAvailabilityError(
                            "model_cache.removal_scope_changed",
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
                    raise RecipeImageAvailabilityError(
                        "recipe_image.removal_scope_limited",
                        "recipe removal owner is "
                        f"{owner_bytes} bytes; limit is {MAX_ARTIFACT_OWNER_SCAN_BYTES} bytes",
                    )
                now = self._clock()
                now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
                operation = Job(
                    id=operation_id,
                    request_id=request_id,
                    kind=REMOVE_OPERATION_KIND,
                    state="queued",
                    actor=actor,
                    authority_revision=revision_id,
                    targets=[],
                    payload_digest=self._removal_payload_digest(
                        plan.model_dump(mode="json")
                    ),
                    payload=owner.model_dump(mode="json"),
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
            raise RecipeImageAvailabilityError(
                error.code,
                error.detail,
                retryable=error.retryable,
                recovery_actions=("retry",) if error.retryable else (),
            ) from error
        except ModelCacheConflict as error:
            raise RecipeImageAvailabilityError(
                error.code,
                error.detail,
                retryable=error.recovery == "retry",
                retry_after_seconds=error.retry_after_seconds,
                recovery_actions=("retry",) if error.recovery == "retry" else (),
            ) from error

        with self._sessions() as session:
            operation = session.get(Job, operation_id)
            if operation is None:
                raise RecipeImageAvailabilityError(
                    "recipe_image.operation_missing",
                    "accepted recipe removal owner could not be read",
                    retryable=True,
                    recovery_actions=("retry",),
                )
            intent = self._read_removal_intent(operation)
            return self._read_removal_result(operation, intent)

    def advance_removals(self, *, limit: int = 1) -> int:
        """Advance bounded, durable cache removals without claiming image slots."""

        if not 1 <= limit <= 16:
            raise ValueError("recipe removal batch limit is invalid")
        advanced = 0
        boundary: tuple[datetime, str] | None = None
        while advanced < limit:
            with self._sessions() as session:
                statement = (
                    select(Job.id, Job.updated_at)
                    .where(
                        Job.kind == REMOVE_OPERATION_KIND,
                        Job.state.in_(("queued", "running", "partial")),
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
                except RecipeImageAvailabilityError:
                    # A malformed owner is isolated to its own request. It
                    # remains fail-closed and cannot hold up later rows.
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
                or operation.state not in {"queued", "running", "partial"}
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
                    raise RuntimeImagePreparationError(
                        "runtime_image.archive_mismatch",
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
                retryable=error.retryable,
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
            if operation is None or operation.state not in {
                "queued",
                "running",
                "partial",
            }:
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
                raise RecipeImageAvailabilityError(
                    "artifact.reference_changed",
                    "runtime image acquired an active reference while removal was reserved",
                    retryable=True,
                    recovery_actions=("retry",),
                )
            pending_bytes = checkpoint.image_pending_bytes
            if pending_bytes is None:
                pending_bytes = observed_bytes
            elif observed_bytes not in {0, pending_bytes}:
                raise RuntimeImagePreparationError(
                    "runtime_image.archive_mismatch",
                    "published image length disagrees with its durable removal checkpoint",
                )
            updated_checkpoint = checkpoint.model_copy(
                update={"image_pending_bytes": pending_bytes, "failure": None}
            )
            updated_owner = owner.model_copy(update={"checkpoint": updated_checkpoint})
            operation.payload = updated_owner.model_dump(mode="json")
            operation.state = "running"
            operation.status_reason = None
            operation.updated_at = now
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
            if operation is None or operation.state not in {
                "queued",
                "running",
                "partial",
            }:
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
            operation.payload = updated_owner.model_dump(mode="json")
            operation.state = "running"
            operation.status_reason = None
            operation.updated_at = now
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
                code="model_cache.unavailable",
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
                code="model_cache.removal_child_missing",
                detail="accepted model-removal child is unavailable",
                retryable=False,
            )
        except ModelCacheError as error:
            return self._record_recipe_removal_failure(
                operation_id,
                code="model_cache.removal_child_invalid",
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
                code="model_cache.removal_child_mismatch",
                detail="model-removal child identity does not match its accepted recipe owner",
                retryable=False,
            )
        if operation.state in {"queued", "running", "partial", "cancelling"}:
            return self._record_recipe_removal_failure(
                operation_id,
                code="model_cache.removal_child_pending",
                detail="waiting for the accepted model-removal child to finish",
                retryable=True,
                retry_after_seconds=5,
            )
        if operation.state != "succeeded":
            return self._record_recipe_removal_failure(
                operation_id,
                code="model_cache.removal_child_failed",
                detail="accepted model-removal child did not complete successfully",
                retryable=False,
            )
        try:
            if isinstance(operation.result, ModelCacheRemovalResult):
                result = operation.result
            else:
                result = ModelCacheRemovalResult.model_validate(operation.result)
        except (TypeError, ValueError, ValidationError):
            return self._record_recipe_removal_failure(
                operation_id,
                code="model_cache.removal_child_invalid",
                detail="successful model-removal child has an invalid result",
                retryable=False,
            )
        if result.removed_entries != child.selected_sets or result.cancelled_operations:
            return self._record_recipe_removal_failure(
                operation_id,
                code="model_cache.removal_child_mismatch",
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
            if operation is None or operation.state not in {
                "queued",
                "running",
                "partial",
            }:
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
            operation.payload = updated_owner.model_dump(mode="json")
            operation.state = "running"
            operation.status_reason = None
            operation.updated_at = now
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
                if operation is None or operation.state not in {
                    "queued",
                    "running",
                    "partial",
                }:
                    return False
                owner = self._read_removal_owner(operation)
                checkpoint = owner.checkpoint
                retry_attempts = checkpoint.retry_attempts
                retry_time: str | None = None
                delay: int | None = None
                if retryable:
                    retry_attempts += 1
                    delay = min(
                        60,
                        max(1, retry_after_seconds, 2 ** min(retry_attempts, 6)),
                    )
                    retry_time = _iso(now + timedelta(seconds=delay))
                safe = sanitize_failure_evidence({"code": code, "detail": detail})
                failure = AvailabilityOperationFailure.model_validate(
                    {
                        "code": safe.get("code", "recipe_image.removal_failed"),
                        "detail": safe.get("detail", "recipe removal did not complete"),
                        "recovery_actions": [] if retryable else ["inspect"],
                        "retryable": retryable,
                        "retry_time": retry_time,
                        "retry_after_seconds": delay,
                    }
                )
                updated_checkpoint = checkpoint.model_copy(
                    update={"retry_attempts": retry_attempts, "failure": failure}
                )
                operation.payload = owner.model_copy(
                    update={"checkpoint": updated_checkpoint}
                ).model_dump(mode="json")
                operation.state = "partial" if retryable else "failed"
                operation.status_reason = failure.detail
                operation.updated_at = now
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
                if operation is None or operation.state not in {
                    "queued",
                    "running",
                    "partial",
                }:
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
                    raise RecipeImageAvailabilityError(
                        "artifact.reference_changed",
                        "a runtime image reference remains after the removal effects",
                        retryable=True,
                        recovery_actions=("retry",),
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
                operation.result = result.model_dump(mode="json")
                operation.state = "succeeded"
                operation.status_reason = None
                operation.updated_at = now
                updated_owner = owner.model_copy(
                    update={
                        "checkpoint": checkpoint.model_copy(update={"failure": None})
                    }
                )
                operation.payload = updated_owner.model_dump(mode="json")
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
                retryable=error.retryable,
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
                raise KeyError(operation_id)
            kind = operation.kind
        if kind == UPDATE_KIND:
            try:
                return self._updates.cancel(
                    operation_id, actor=actor, request_id=request_id, reason=reason
                )
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "55P03":
                    raise
                raise RecipeImageAvailabilityError(
                    "recipe_image.cancel_busy",
                    "recipe cancellation is changing; retry with the same request key",
                    retryable=True,
                ) from error
        if kind != OPERATION_KIND:
            raise RecipeImageAvailabilityError(
                "recipe_image.not_cancellable",
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
                    raise KeyError(operation_id)
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
            raise RecipeImageAvailabilityError(
                "recipe_image.cancel_busy",
                "recipe cancellation is changing; retry with the same request key",
                retryable=True,
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
    ) -> RecipeOperationCancellationResult:
        if job.kind not in {OPERATION_KIND, UPDATE_KIND}:
            raise RecipeImageAvailabilityError(
                "recipe_image.not_cancellable",
                "operation is not a current recipe preparation",
            )
        if authorize:
            self._updates._authorize(session, actor)
        cancellation_id = _canonical_cancellation_id(request_id)
        normalized_reason = " ".join(reason.split()) if isinstance(reason, str) else ""
        if not normalized_reason or len(normalized_reason) > 512:
            raise RecipeImageAvailabilityError(
                "recipe_image.cancellation_invalid",
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
            raise RecipeImageAvailabilityError(
                "recipe_image.cancel_request_key_reused",
                "operation already has a different cancellation request",
            )
        if job.state not in {"queued", "running", "partial"}:
            raise RecipeImageAvailabilityError(
                "recipe_image.not_cancellable",
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
            raise RecipeImageAvailabilityError(
                "recipe_image.cancel_request_key_reused",
                "cancellation request key was already used",
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
            payload = dict(require_mapping(job.payload, "availability payload"))
            payload["cancellation"] = cancellation.model_dump(
                mode="json", exclude_none=True
            )
            job.payload = payload
        job.state = "cancelling"
        job.status_reason = normalized_reason
        job.updated_at = now
        return cancellation

    def _stored_cancellation(
        self, job: Job
    ) -> RecipeOperationCancellationResult | None:
        try:
            if job.kind == UPDATE_KIND:
                return self._updates._document(job).cancellation
            payload = require_mapping(job.payload, "availability payload")
            value = payload.get("cancellation")
            if value is None:
                return None
            return RecipeOperationCancellationResult.model_validate_json(
                json.dumps(value, allow_nan=False)
            )
        except (TypeError, ValueError) as error:
            raise RecipeImageAvailabilityError(
                "recipe_image.operation_invalid",
                "stored cancellation evidence is malformed",
            ) from error

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
                if job.state not in {"queued", "running", "partial"}:
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
            raise ValueError("cancellation reconciliation limit is invalid")
        progressed = self._updates.reconcile_cancellations(limit=limit)
        with self._sessions() as session:
            operation_ids = tuple(
                session.scalars(
                    select(Job.id)
                    .where(
                        Job.kind == OPERATION_KIND,
                        Job.state == "cancelling",
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
        payload: Mapping[str, object],
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
        raw_child = payload.get("model_child")
        child = raw_child if isinstance(raw_child, Mapping) else {}
        child_id = child.get("id")
        recipe_revision_id = payload.get("recipe_revision_id")
        request_keys = {
            str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"vonk:recipe-availability-model:{recipe_revision_id}:{request_id}",
                )
            )
        }
        artifact_set = child.get("artifact_set_sha256")
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
                        and item.state in {"queued", "running", "partial"}
                    ),
                    None,
                )
                if candidate is None:
                    candidate = next(
                        (
                            item
                            for item in candidates
                            if item.request_key in request_keys
                            and item.state in {"queued", "running", "partial"}
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
                    or model_child.state not in {"queued", "running", "partial"}
                ):
                    return False, False
                shared = session.scalar(
                    select(Job.id)
                    .where(
                        Job.id != operation_id,
                        Job.kind == OPERATION_KIND,
                        Job.state.in_(("queued", "running", "partial")),
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
        payload: Mapping[str, object],
        current_attempt: int,
        cancellation: RecipeOperationCancellationResult,
    ) -> bool:
        dependency = payload.get("build_dependency")
        request_key = (
            dependency.get("request_key") if isinstance(dependency, Mapping) else None
        )
        child_operation_id = (
            dependency.get("operation_id") if isinstance(dependency, Mapping) else None
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
                if child is None or child.state not in {
                    "queued",
                    "running",
                    "waiting-for-operator",
                }:
                    return False
                owner_id = (
                    child.payload.get("owner_id")
                    if isinstance(child.payload, Mapping)
                    else None
                )
                if not isinstance(owner_id, str):
                    return True
                build = session.get(RecipeBuild, owner_id)
                if build is None:
                    return True
                recipe_revision_id = payload.get("recipe_revision_id")
                build_input_sha256 = payload.get("build_input_sha256")
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
                        Job.state.in_(("queued", "running", "waiting-for-operator")),
                    )
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
                if child is None:
                    return False
                if child.payload.get("owner_id") != locked.id or (
                    not isinstance(operation_id, str)
                    and child.request_id != request_key
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
            if (
                operation is None
                or operation.kind != OPERATION_KIND
                or operation.state != "cancelling"
                or operation.current_attempt != claim.execution_attempt
                or not isinstance(operation.payload, Mapping)
                or operation.payload.get("claim_owner") != claim.claim_owner
                or self._stored_cancellation(operation) is None
                or operation.payload.get("removal_fence") is not None
            ):
                return False
            snapshot = dict(operation.payload)
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
                if (
                    operation is None
                    or operation.state != "cancelling"
                    or operation.current_attempt != claim.execution_attempt
                    or not isinstance(operation.payload, Mapping)
                    or operation.payload.get("claim_owner") != claim.claim_owner
                    or operation.payload.get("removal_fence") is not None
                    or self._stored_cancellation(operation) is None
                ):
                    return False
                payload = dict(operation.payload)
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
                payload.pop("image_reference_intent", None)
                payload.pop("claim_owner", None)
                payload.pop("claim_until", None)
                operation.payload = payload
                operation.updated_at = self._clock()
                return True
        except RuntimeImagePreparationError as error:
            if error.code == "runtime_image.publication_contended":
                return False
            raise
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "55P03":
                return False
            raise

    def _reconcile_availability_cancellation(self, operation_id: str) -> bool:
        with self._sessions() as session:
            operation = session.get(Job, operation_id)
            if (
                operation is None
                or operation.kind != OPERATION_KIND
                or operation.state != "cancelling"
            ):
                return False
            cancellation = self._stored_cancellation(operation)
            payload = dict(require_mapping(operation.payload, "availability payload"))
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
        owner = payload.get("claim_owner")
        if owner is not None:
            if not isinstance(owner, str):
                return child_changed
            if payload.get("image_reference_intent") is None:
                raw_until = payload.get("claim_until")
                if not isinstance(raw_until, str):
                    return child_changed
                try:
                    until = datetime.fromisoformat(raw_until)
                except ValueError:
                    return child_changed
                until = until if until.tzinfo is not None else until.replace(tzinfo=UTC)
                if until > now:
                    # A valid lease may still be transferring bytes. Give its
                    # exact owner the chance to retain a verified receipt under
                    # the archive lock; expiry later fences callbacks in SQL.
                    return child_changed
            recipe_revision_id = operation.authority_revision
            if not isinstance(recipe_revision_id, str):
                return child_changed
            image_identity = payload.get("image_identity")
            build_input_sha256 = payload.get("build_input_sha256")
            claim = RecipeImageAvailabilityClaim(
                operation_id=operation_id,
                recipe_revision_id=recipe_revision_id,
                image_identity=(
                    image_identity if isinstance(image_identity, str) else None
                ),
                build_input_sha256=(
                    build_input_sha256 if isinstance(build_input_sha256, str) else None
                ),
                claim_owner=owner,
                execution_attempt=current_attempt,
            )
            if not self._release_cancelled_claim(claim):
                return child_changed
        elif payload.get("image_reference_intent") is not None:
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
            if current is None or current.state != "cancelling":
                return child_changed
            current_cancellation = self._stored_cancellation(current)
            if (
                current_cancellation is None
                or current_cancellation.cancel_request_id
                != cancellation.cancel_request_id
            ):
                return child_changed
            current_payload = dict(
                require_mapping(current.payload, "availability payload")
            )
            if (
                current_payload.get("claim_owner") is not None
                or current_payload.get("image_reference_intent") is not None
                or current_payload.get("removal_fence") is not None
            ):
                return child_changed
            current.state = "cancelled"
            current.status_reason = cancellation.reason
            current.updated_at = now
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
        force_download: bool = False,
        force_rebuild: bool = False,
    ) -> RecipeImageAvailabilityView:
        """Refresh metadata and queue one exact selected Recipe operation."""

        if not isinstance(recipe_revision_id, str) or not recipe_revision_id.strip():
            raise RecipeImageAvailabilityError(
                "recipe_image.recipe_invalid", "recipe revision is required"
            )
        if force and (force_download or force_rebuild):
            raise RecipeImageAvailabilityError(
                "recipe_image.action_invalid",
                "force cannot be combined with an explicit image action",
            )
        if force_download and force_rebuild:
            raise RecipeImageAvailabilityError(
                "recipe_image.action_invalid",
                "download again and rebuild are mutually exclusive",
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
            force_download=force_download,
            force_rebuild=force_rebuild,
            model_digest=model_digest,
            build_input_sha256=build_input_sha256,
            effective_execution_key=effective_execution_key,
        )
        return self._start_request(intent, actor=actor, request_id=request_id)

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
            force_download = force_rebuild = False
        else:
            recipe_revision_id = intent.recipe_revision_id
            model_digest = intent.model_digest
            build_input_sha256 = intent.build_input_sha256
            effective_execution_key = intent.effective_execution_key
            force_download = intent.force_download
            force_rebuild = intent.force_rebuild
        force = intent.force
        if self._authority is None:
            raise RecipeImageAvailabilityError(
                "recipe_image.metadata_refresh_unavailable",
                "latest recipe metadata could not be refreshed",
            )
        try:
            raw_recipe, runtime = self._authority(recipe_revision_id, force=force)
        except RecipeImageAvailabilityError:
            raise
        except Exception as error:
            raise RecipeImageAvailabilityError(
                "recipe_image.metadata_refresh_failed",
                "latest recipe metadata could not be refreshed",
                retryable=_retryable(error),
                retry_after_seconds=_retry_after(error),
                recovery_actions=("retry",) if _retryable(error) else ("inspect",),
            ) from error
        recipe = _canonical_recipe(raw_recipe)
        computed_digest = content_sha256(recipe)
        if not isinstance(runtime, Mapping):
            raise RecipeImageAvailabilityError(
                "recipe_image.runtime_invalid",
                "selected recipe runtime projection is unavailable",
            )
        try:
            with self._sessions.begin() as session:
                if update_claim is not None:
                    if not isinstance(intent, RecipeRevisionIntent):
                        raise RecipeImageAvailabilityError(
                            "recipe_update.operation_invalid",
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
                    raise RecipeImageAvailabilityError(
                        "recipe_image.recipe_unavailable",
                        "selected recipe revision is unavailable or inactive",
                    )
                if revision.content_digest != computed_digest:
                    raise RecipeImageAvailabilityError(
                        "recipe_image.metadata_stale",
                        "refreshed recipe does not match the selected revision",
                    )
                if effective_execution_key is None:
                    effective_execution_key = revision.execution_key
                if effective_execution_key != revision.execution_key:
                    raise RecipeImageAvailabilityError(
                        "recipe_image.identity_conflict",
                        "selected recipe execution identity changed",
                    )
                if recipe.execution.mode == "image" and force_rebuild:
                    raise RecipeImageAvailabilityError(
                        "recipe_image.action_invalid",
                        "rebuild is supported only for source-build recipes",
                    )
                if recipe.execution.mode == "build" and force_download:
                    raise RecipeImageAvailabilityError(
                        "recipe_image.action_invalid",
                        "download again is supported only for published images",
                    )
                if force:
                    if recipe.execution.mode == "image":
                        force_download = True
                    else:
                        force_rebuild = True
                runtime_build_input = runtime.get("build_input_sha256")
                if recipe.execution.mode == "build":
                    provisional_intent = runtime.get("input_intent_sha256")
                    if not isinstance(runtime_build_input, str) and not isinstance(
                        provisional_intent, str
                    ):
                        raise RecipeImageAvailabilityError(
                            "recipe_image.build_input_missing",
                            "authoritative runtime projection lacks the exact build input digest",
                        )
                    if isinstance(runtime_build_input, str):
                        runtime_build_input = _digest(
                            runtime_build_input, field="build_input_sha256"
                        )
                        if build_input_sha256 is None:
                            build_input_sha256 = runtime_build_input
                        elif build_input_sha256 != runtime_build_input:
                            raise RecipeImageAvailabilityError(
                                "recipe_image.identity_conflict",
                                "submitted build input does not match authoritative runtime metadata",
                            )
                else:
                    build_input_sha256 = None
                image_identity = _image_identity(recipe)
                identity_key = image_identity or build_input_sha256
                payload: dict[str, object] = {
                    "schema_version": SCHEMA_VERSION,
                    "kind": OPERATION_KIND,
                    "request": serialize_json_value(intent),
                    "recipe_revision_id": recipe_revision_id,
                    "recipe_content_sha256": computed_digest,
                    "effective_execution_key": effective_execution_key,
                    "model_digest": model_digest,
                    "build_input_sha256": build_input_sha256,
                    "image_identity": image_identity,
                    "identity_key": identity_key,
                    "execution_mode": recipe.execution.mode,
                    "recipe": recipe.model_dump(mode="json"),
                    "runtime": dict(runtime),
                    "force_download": force_download,
                    "force_rebuild": force_rebuild,
                    "progress": _progress("prepare", total_bytes=_known_total(runtime)),
                    "retry": {"automatic_attempts": 0, "operator_retries": 0},
                }
                encoded = json.dumps(
                    payload, sort_keys=True, separators=(",", ":")
                ).encode()
                existing = session.scalar(
                    select(Job).where(Job.request_id == request_id)
                )
                if existing is not None:
                    return self._matching_request(existing, actor=actor, intent=intent)
                current_archives = tuple(
                    session.scalars(
                        select(RuntimeImageAuthorization.oci_archive_sha256).where(
                            RuntimeImageAuthorization.recipe_revision_id
                            == recipe_revision_id,
                            RuntimeImageAuthorization.state == "authorized",
                        )
                    )
                )
                try:
                    require_reference_open(
                        session,
                        (
                            ArtifactIdentity("runtime-image", archive)
                            for archive in sorted(set(current_archives))
                        ),
                        now=self._clock(),
                    )
                except ArtifactLifecycleError as error:
                    raise RecipeImageAvailabilityError(
                        error.code,
                        error.detail,
                        retryable=error.retryable,
                        recovery_actions=("retry",) if error.retryable else (),
                    ) from error
                self._lock_build_consumer(session, payload)
                now = self._clock()
                operation = Job(
                    id=str(uuid.uuid4()),
                    request_id=request_id,
                    kind=OPERATION_KIND,
                    state="queued",
                    actor=actor,
                    authority_revision=recipe_revision_id,
                    targets=[recipe_revision_id],
                    payload_digest=hashlib.sha256(encoded).hexdigest(),
                    payload=payload,
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
    def _lock_build_consumer(session: Session, payload: Mapping[str, object]) -> None:
        try:
            lock_availability_build_dependency(session, payload)
        except BuildConsumerError as error:
            raise RecipeImageAvailabilityError(
                error.code,
                str(error),
                retryable=error.retryable,
                recovery_actions=("retry",) if error.retryable else (),
            ) from error

    def _ensure_model_child(
        self,
        recipe_revision_id: str,
        *,
        actor: str,
        parent_request_key: str,
    ) -> dict[str, object] | None:
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
            preview = self._model_cache.download_preview(
                recipe_revision_id=recipe_revision_id
            )
            plan_digest = preview.get("plan_digest")
            artifact_set_sha256 = preview.get("artifact_set_sha256")
            if not isinstance(plan_digest, str) or not isinstance(
                artifact_set_sha256, str
            ):
                raise RecipeImageAvailabilityError(
                    "recipe_image.model_cache_invalid",
                    "ModelCache returned an incomplete exact artifact plan",
                    retryable=True,
                    recovery_actions=("retry",),
                )
            manifest = self._model_cache.resolve_artifact_set(
                recipe_revision_id=recipe_revision_id
            )
            manifest_document = manifest.document()
            artifacts = manifest_document.get("artifacts")
            new_bytes = preview.get("new_bytes")
            if type(new_bytes) is not int or new_bytes < 0:
                raise RecipeImageAvailabilityError(
                    "recipe_image.model_cache_invalid",
                    "ModelCache returned incomplete transfer accounting",
                    retryable=True,
                    recovery_actions=("retry",),
                )
            if manifest.digest != artifact_set_sha256:
                raise RecipeImageAvailabilityError(
                    "recipe_image.model_cache_invalid",
                    "resolved model artifact identity changed during planning",
                    retryable=True,
                    recovery_actions=("retry",),
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
                        in {"queued", "running", "partial", "succeeded", "failed"}
                        and not (candidate.state == "succeeded" and new_bytes > 0)
                    )
                ]
                state_rank = {
                    "succeeded": 0,
                    "queued": 1,
                    "running": 1,
                    "partial": 1,
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
                    actions = operation.failure
                    actions = (
                        actions.get("recovery_actions", [])
                        if isinstance(actions, Mapping)
                        else []
                    )
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
            raise RecipeImageAvailabilityError(
                "recipe_image.model_cache_unavailable",
                "exact Model artifact preparation could not be queued",
                retryable=True,
                recovery_actions=("retry",),
            ) from error
        model_content_digests = manifest_document["model_content_digests"]
        return {
            "id": operation.id,
            "request_key": str(getattr(operation, "request_key", child_request_key)),
            "state": operation.state,
            "artifact_set_sha256": artifact_set_sha256,
            "plan_digest": plan_digest,
            "model_content_digests": model_content_digests,
            "artifacts": [dict(item) for item in artifacts if isinstance(item, Mapping)]
            if isinstance(artifacts, list)
            else [],
            "progress": project_cache_progress(operation.progress, self._clock()),
        }

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
            raise RecipeImageAvailabilityError(
                "recipe_image.model_cache_invalid",
                "integrity failure did not retain an artifact-set identity",
                retryable=True,
                recovery_actions=("retry",),
            )
        repair_preview = getattr(self._model_cache, "repair_preview", None)
        start_repair = getattr(self._model_cache, "start_repair", None)
        if repair_preview is None or start_repair is None:
            raise RecipeImageAvailabilityError(
                "recipe_image.model_cache_unavailable",
                "ModelCache does not expose the canonical repair workflow",
                retryable=True,
                recovery_actions=("retry",),
            )
        preview = repair_preview(artifact_set_sha256)
        plan_digest = (
            preview.get("plan_digest") if isinstance(preview, Mapping) else None
        )
        if not isinstance(plan_digest, str):
            raise RecipeImageAvailabilityError(
                "recipe_image.model_cache_invalid",
                "ModelCache returned an incomplete repair plan",
                retryable=True,
                recovery_actions=("retry",),
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
        child: Mapping[str, object] | None,
        *,
        actor: str,
        parent_request_key: str,
    ) -> dict[str, object] | None:
        if self._model_cache is None or not isinstance(child, Mapping):
            return dict(child) if isinstance(child, Mapping) else None
        child_id = child.get("id")
        if not isinstance(child_id, str):
            return dict(child)
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
                            in {"queued", "running", "partial", "succeeded"}
                        )
                    ]
                    state_rank = {
                        "succeeded": 0,
                        "queued": 1,
                        "running": 1,
                        "partial": 1,
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
                    actions = operation.failure
                    actions = (
                        actions.get("recovery_actions", [])
                        if isinstance(actions, Mapping)
                        else []
                    )
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
            return dict(child) | {
                "id": operation.id,
                "state": operation.state,
                "progress": project_cache_progress(operation.progress, self._clock()),
                "artifact_set_sha256": operation.artifact_set_sha256,
                "plan_digest": operation.plan_digest,
                "failure": (
                    dict(operation.failure)
                    if isinstance(operation.failure, Mapping)
                    else None
                ),
            }
        except Exception as error:
            raise RecipeImageAvailabilityError(
                "recipe_image.model_cache_unavailable",
                "Model artifact operation could not be resumed",
                retryable=True,
                recovery_actions=("retry",),
            ) from error

    def _matching_request(
        self,
        existing: Job,
        *,
        actor: str,
        intent: RecipeAvailabilityIntent,
    ) -> RecipeImageAvailabilityView:
        if existing.kind != OPERATION_KIND or existing.actor != actor:
            raise RecipeImageAvailabilityError(
                "recipe_image.request_key_reused",
                "request key was already used for another operation",
            )
        try:
            payload = require_mapping(existing.payload, "availability payload")
            stored = read_availability_intent(payload.get("request"))
        except (TypeError, ValueError, ValidationError) as error:
            raise RecipeImageAvailabilityError(
                "recipe_image.operation_invalid",
                "stored preparation request is malformed",
            ) from error
        if stored != intent:
            raise RecipeImageAvailabilityError(
                "recipe_image.request_key_reused",
                "request key was already used for another operation",
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
                raise KeyError(operation_id)
            return self._view(operation)

    def get_operator_operation(
        self, operation_id: str
    ) -> RecipeImageAvailabilityView | RecipeUpdateResponse | dict[str, object]:
        """Observe either current recipe preparation or durable cache removal."""

        with self._sessions() as session:
            operation = session.get(Job, operation_id)
            if operation is None:
                raise KeyError(operation_id)
            if operation.kind == OPERATION_KIND:
                return self._view(operation)
            if operation.kind == REMOVE_OPERATION_KIND:
                intent = self._read_removal_intent(operation)
                return self._read_removal_result(operation, intent)
            if operation.kind != UPDATE_KIND:
                raise KeyError(operation_id)
        return self._updates.get(operation_id)

    def get_operator_request(
        self, request_key: str, *, actor: str
    ) -> RecipeImageAvailabilityView | RecipeUpdateResponse | dict[str, object]:
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
                raise KeyError(request_key)
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
            raise ValueError("availability list limit is invalid")
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
                raise KeyError(operation_id)
            if previous.state != "failed":
                raise RecipeImageAvailabilityError(
                    "recipe_image.not_retryable", "operation is not failed"
                )
            previous_payload = (
                previous.payload if isinstance(previous.payload, Mapping) else {}
            )
            failure = previous_payload.get("failure", {})
            failure = failure if isinstance(failure, Mapping) else {}
            recovery_actions = failure.get("recovery_actions", [])
            explicit_repair = (
                isinstance(recovery_actions, list)
                and "download_again" in recovery_actions
            )
            if failure.get("retryable") is not True and not explicit_repair:
                raise RecipeImageAvailabilityError(
                    "recipe_image.not_retryable", "operation failure is terminal"
                )
            retry = previous_payload.get("retry", {})
            retry_count = (
                int(retry.get("operator_retries", 0))
                if isinstance(retry, Mapping)
                else 0
            )
            if retry_count >= self._operator_retry_limit:
                raise RecipeImageAvailabilityError(
                    "recipe_image.retry_exhausted", "operator retry limit reached"
                )
            previous_authority = previous.authority_revision
            previous_targets = list(previous.targets)
            payload = dict(previous_payload)
        payload["request"] = serialize_json_value(intent)
        with self._sessions.begin() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is not None:
                return self._matching_request(existing, actor=actor, intent=intent)
            payload["retry"] = {
                "automatic_attempts": 0,
                "operator_retries": retry_count + 1,
            }
            payload.pop("retry_after_at", None)
            payload.pop("failure", None)
            payload.pop("build_dependency", None)
            self._lock_build_consumer(session, payload)
            now = self._clock()
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode()
            operation = Job(
                id=str(uuid.uuid4()),
                request_id=request_id,
                kind=OPERATION_KIND,
                state="queued",
                actor=actor,
                authority_revision=previous_authority,
                targets=previous_targets,
                payload_digest=hashlib.sha256(encoded).hexdigest(),
                payload=payload,
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
            raise ValueError("availability operation limit is invalid")
        with self._sessions() as session:
            # SQLAlchemy's scalar count expression is portable across the
            # SQLite fixtures and PostgreSQL deployment.
            count = int(
                session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(
                        Job.kind.in_((OPERATION_KIND, REMOVE_OPERATION_KIND)),
                        Job.state.in_(("queued", "running", "partial", "cancelling")),
                    )
                )
                or 0
            )
            return min(count, limit)

    def run_pending(self, *, limit: int = 1) -> int:
        if not 1 <= limit <= 16:
            raise ValueError("availability worker batch limit is invalid")
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
            raise ValueError("availability claim limit is invalid")
        owner_id = owner_id or str(uuid.uuid4())
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        lease_until = _iso(now + timedelta(seconds=self._claim_lease_seconds))
        claims: list[RecipeImageAvailabilityClaim] = []
        with self._sessions.begin() as session:
            # The dispatch boundary.  A queued preparation whose recipe has
            # advanced to a newer active revision is cancelled here, before any
            # claim is handed to a builder executor, so a superseded operation
            # can never occupy the build slot the current revision needs.  Only
            # not-yet-started operations are eligible; a running attempt with a
            # live lease is left alone because the active revision may reuse its
            # shared build inputs.
            self._cancel_superseded_by_active_head(session, now=now)
            active_rows = list(
                session.scalars(
                    select(Job)
                    .where(
                        Job.kind == OPERATION_KIND,
                        Job.state == "running",
                    )
                    .with_for_update()
                )
            )
            candidate_ids = list(
                session.scalars(
                    select(Job.id)
                    .where(
                        Job.kind == OPERATION_KIND,
                        Job.state.in_(("queued", "running", "partial")),
                    )
                    .order_by(Job.updated_at, Job.id)
                    .limit(limit * 8)
                )
            )
            active_builds = 0
            active_pulls = 0
            for active in active_rows:
                active_payload = (
                    active.payload if isinstance(active.payload, Mapping) else {}
                )
                if active.state != "running":
                    continue
                if isinstance(active_payload.get("image_result"), Mapping):
                    continue
                active_until = active_payload.get("claim_until")
                if isinstance(active_until, str):
                    try:
                        parsed_until = datetime.fromisoformat(active_until)
                        parsed_until = (
                            parsed_until
                            if parsed_until.tzinfo is not None
                            else parsed_until.replace(tzinfo=UTC)
                        )
                        if now >= parsed_until:
                            continue
                    except ValueError:
                        pass
                if active_payload.get("execution_mode") == "build":
                    active_builds += 1
                else:
                    active_pulls += 1
            for operation_id in candidate_ids:
                operation = session.scalar(
                    select(Job)
                    .where(
                        Job.id == operation_id,
                        Job.kind == OPERATION_KIND,
                        Job.state.in_(("queued", "running", "partial")),
                    )
                    .with_for_update(skip_locked=True)
                )
                if operation is None:
                    continue
                payload = (
                    operation.payload if isinstance(operation.payload, Mapping) else {}
                )
                if not self._retry_due(payload, now):
                    continue
                if operation.state == "running":
                    claimed_until = payload.get("claim_until")
                    if isinstance(claimed_until, str):
                        try:
                            parsed_until = datetime.fromisoformat(claimed_until)
                            parsed_until = (
                                parsed_until
                                if parsed_until.tzinfo is not None
                                else parsed_until.replace(tzinfo=UTC)
                            )
                            if now < parsed_until:
                                continue
                        except ValueError:
                            pass
                mode = payload.get("execution_mode")
                coordination_only = isinstance(payload.get("image_result"), Mapping)
                if (
                    not coordination_only
                    and mode == "build"
                    and active_builds >= self._max_parallel_builds
                ):
                    continue
                if (
                    not coordination_only
                    and mode != "build"
                    and active_pulls >= self._max_parallel
                ):
                    continue
                operation.state = "running"
                operation.current_attempt = int(operation.current_attempt) + 1
                operation.updated_at = now
                operation.payload = dict(payload) | {
                    "claim_owner": owner_id,
                    "claim_until": lease_until,
                }
                claims.append(
                    RecipeImageAvailabilityClaim(
                        operation_id=operation.id,
                        recipe_revision_id=str(payload.get("recipe_revision_id", "")),
                        image_identity=(
                            str(payload.get("image_identity"))
                            if payload.get("image_identity") is not None
                            else None
                        ),
                        build_input_sha256=(
                            str(payload.get("build_input_sha256"))
                            if payload.get("build_input_sha256") is not None
                            else None
                        ),
                        claim_owner=owner_id,
                        execution_attempt=operation.current_attempt,
                    )
                )
                if not coordination_only and mode == "build":
                    active_builds += 1
                elif not coordination_only:
                    active_pulls += 1
                if len(claims) >= limit:
                    break
        return tuple(claims)

    @staticmethod
    def _holds_live_lease(payload: Mapping[str, object], now: datetime) -> bool:
        """Report whether the operation still holds an unexpired claim lease.

        A queued operation normally has no owner.  The check is defensive: a
        lease that cannot be parsed or that has no deadline is treated as live,
        so this path can never revoke an attempt that might still be running.
        """
        owner = payload.get("claim_owner")
        if not isinstance(owner, str) or not owner:
            return False
        until = payload.get("claim_until")
        if not isinstance(until, str):
            return True
        try:
            parsed = datetime.fromisoformat(until)
        except ValueError:
            return True
        parsed = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        return now < parsed

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
        payload = operation.payload if isinstance(operation.payload, Mapping) else {}
        if isinstance(payload.get("image_result"), Mapping):
            return False
        if self._holds_live_lease(payload, now):
            return False
        detail = f"superseded by newer recipe revision {newer_revision_id}"
        failure = sanitize_failure_evidence(
            {
                "code": SUPERSEDED_PREPARATION_CODE,
                "detail": detail,
                "recovery_actions": ["download_again"],
                "retryable": False,
            }
        )
        updated = dict(payload)
        # Release the queue position and any lease keys the cancelled operation
        # held so the replacing preparation can be claimed immediately.
        updated.pop("claim_owner", None)
        updated.pop("claim_until", None)
        updated.pop("retry_after_at", None)
        updated["failure"] = failure
        updated["supersession"] = {
            "code": SUPERSEDED_PREPARATION_CODE,
            "recipe_revision_id": newer_revision_id,
            "superseded_at": _iso(now),
        }
        operation.payload = updated
        operation.result = None
        operation.state = "cancelled"
        operation.status_reason = detail[:512]
        operation.updated_at = now
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
            if operation is None or not isinstance(operation.payload, Mapping):
                return False
            value = operation.payload.get("retry_after_at")
            if not isinstance(value, str):
                return True
            try:
                eligible_at = datetime.fromisoformat(value)
            except ValueError:
                return True
            now = self._clock()
            now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
            eligible_at = (
                eligible_at
                if eligible_at.tzinfo is not None
                else eligible_at.replace(tzinfo=UTC)
            )
            return now >= eligible_at

    @staticmethod
    def _retry_due(payload: Mapping[str, object], now: datetime) -> bool:
        value = payload.get("retry_after_at")
        if not isinstance(value, str):
            return True
        try:
            eligible_at = datetime.fromisoformat(value)
        except ValueError:
            return True
        eligible_at = (
            eligible_at
            if eligible_at.tzinfo is not None
            else eligible_at.replace(tzinfo=UTC)
        )
        return now >= eligible_at

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
        if (
            operation is None
            or operation.kind != OPERATION_KIND
            or operation.state not in allowed_states
            or operation.current_attempt != claim.execution_attempt
            or not isinstance(operation.payload, Mapping)
            or operation.payload.get("claim_owner") != claim.claim_owner
            or operation.payload.get("removal_fence") is not None
        ):
            return None
        if (
            operation.state == "cancelling"
            and self._stored_cancellation(operation) is None
        ):
            return None
        raw_until = operation.payload.get("claim_until")
        if not isinstance(raw_until, str):
            return None
        try:
            until = datetime.fromisoformat(raw_until)
        except ValueError:
            return None
        until = until if until.tzinfo is not None else until.replace(tzinfo=UTC)
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
                payload = dict(operation.payload)
                operation_actor = operation.actor
                operation_request_id = operation.request_id
                operation.updated_at = self._clock()
                self._set_progress(
                    operation,
                    "prepare",
                    total_bytes=_known_total(
                        require_mapping(
                            payload.get("runtime", {}), "runtime projection"
                        )
                    ),
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
            recipe = _canonical_recipe(payload["recipe"])
            runtime = payload["runtime"]
            if not isinstance(runtime, Mapping):
                raise RecipeImageAvailabilityError(
                    "recipe_image.runtime_invalid", "runtime projection is invalid"
                )
            request_intent = read_availability_intent(payload.get("request"))
            if isinstance(request_intent, RecipeRetryIntent) and isinstance(
                payload.get("model_child"), Mapping
            ):
                model_child = self._resume_model_child(
                    mapping(payload["model_child"]),
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
            model_failure: Mapping[str, object] | None = None
            if model_child is not None:
                child_state = model_child.get("state")
                if not self._update_model_progress(claim, model_child):
                    raise _AvailabilityClaimLost()
                if child_state in {"queued", "running", "partial"}:
                    model_pending = True
                elif child_state != "succeeded":
                    model_failure = mapping(model_child.get("failure"))
            identity_key = payload.get("identity_key")
            identity = identity_key if isinstance(identity_key, str) else None
            with self._identity_lock(identity):
                stored_image = payload.get("image_result")
                receipt = None
                if isinstance(stored_image, Mapping):
                    try:
                        receipt = RuntimeImageReceipt(**dict(stored_image))
                    except (TypeError, ValueError) as error:
                        raise RecipeImageAvailabilityError(
                            "runtime_image.receipt_invalid",
                            "durable runtime image result is malformed",
                        ) from error
                if receipt is None or not self._storage.build_archive_available(
                    receipt.oci_archive_sha256, receipt.image_bytes
                ):
                    repair_payload = (
                        dict(payload) | {"force_download": True}
                        if isinstance(stored_image, Mapping)
                        and recipe.execution.mode == "image"
                        else payload
                    )
                    receipt = self._prepare_claimed_image(
                        claim, repair_payload, recipe, runtime
                    )
                    # Removal holds the same lock and commits a durable fence
                    # before deleting Controller image bytes. Late verified
                    # bytes may remain reusable, but stale work cannot accept
                    # a current authorization or operation result.
                    with self._removal_lock:
                        self._persist_receipt(claim, payload, receipt)
            with self._sessions() as session:
                latest = session.get(Job, operation_id)
                if latest is not None and isinstance(latest.payload, Mapping):
                    payload = dict(latest.payload)
            if model_pending:
                self._defer_for_model(claim)
                return
            if model_failure is not None:
                child_failure = model_failure
                failure_code = child_failure.get("code")
                if isinstance(failure_code, str):
                    retry_after_seconds = child_failure.get("retry_after_seconds")
                    retry_time = child_failure.get("retry_time")
                    recovery_actions = child_failure.get("recovery_actions", [])
                    log_excerpt = child_failure.get("log_excerpt")
                    required_bytes = child_failure.get("required_bytes")
                    free_bytes = child_failure.get("free_bytes")
                    shortfall_bytes = child_failure.get("shortfall_bytes")
                    raise RecipeImageAvailabilityError(
                        failure_code,
                        str(
                            child_failure.get(
                                "detail", "Model artifact preparation failed"
                            )
                        ),
                        retryable=child_failure.get("retryable") is True,
                        retry_after_seconds=(
                            retry_after_seconds
                            if type(retry_after_seconds) is int
                            else None
                        ),
                        retry_time=retry_time if isinstance(retry_time, str) else None,
                        recovery_actions=tuple(
                            item
                            for item in require_sequence(
                                recovery_actions, "recovery actions"
                            )
                            if isinstance(item, str)
                        ),
                        log_excerpt=log_excerpt
                        if isinstance(log_excerpt, str)
                        else None,
                        required_bytes=(
                            required_bytes if type(required_bytes) is int else None
                        ),
                        free_bytes=free_bytes if type(free_bytes) is int else None,
                        shortfall_bytes=(
                            shortfall_bytes if type(shortfall_bytes) is int else None
                        ),
                    )
                raise RecipeImageAvailabilityError(
                    "recipe_image.model_cache_failed",
                    "one or more exact Model artifacts could not be prepared",
                    retryable=True,
                    recovery_actions=("retry",),
                )
            result = {
                "schema_version": SCHEMA_VERSION,
                "recipe_content_sha256": payload["recipe_content_sha256"],
                "model_digest": payload.get("model_digest"),
                "build_input_sha256": payload.get("build_input_sha256"),
                "source": receipt.source,
                "registry_manifest_digest": receipt.registry_manifest_digest,
                "platform_manifest_digest": receipt.platform_manifest_digest,
                "image_digest": receipt.image_digest,
                "local_image_config_id": receipt.local_image_config_id,
                "oci_archive_sha256": receipt.oci_archive_sha256,
                "image_bytes": receipt.image_bytes,
                "build_id": receipt.build_id,
                "model_child": (None if model_child is None else dict(model_child)),
            }
            with self._removal_lock, self._sessions.begin() as session:
                operation = self._require_claim(session, claim)
                operation.state = "succeeded"
                operation.result = result
                operation.updated_at = self._clock()
                completed_payload = dict(operation.payload)
                completed_payload.pop("failure", None)
                completed_payload.pop("retry_after_at", None)
                operation.payload = completed_payload | {
                    "stage": "available",
                    "claim_owner": None,
                    "claim_until": None,
                }
                operation.current_attempt = int(operation.current_attempt)
                self._set_progress(
                    operation,
                    "available",
                    total_bytes=receipt.image_bytes,
                    completed_bytes=receipt.image_bytes,
                )
        except _AvailabilityClaimLost:
            return
        except Exception as error:  # noqa: BLE001 - persist failures at the background job boundary
            self._fail(claim, error)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=max(1.0, self._claim_lease_seconds / 2))
            self._release_cancelled_claim(claim)
            self._reconcile_availability_cancellation(operation_id)

    def _current_model_child(
        self,
        payload: Mapping[str, object],
        *,
        actor: str | None = None,
        parent_request_key: str | None = None,
    ) -> Mapping[str, object] | None:
        child = payload.get("model_child")
        if not isinstance(child, Mapping) or self._model_cache is None:
            if (
                child is None
                and self._model_cache is not None
                and actor is not None
                and parent_request_key is not None
                and _canonical_recipe(payload["recipe"]).models
            ):
                return self._ensure_model_child(
                    str(payload["recipe_revision_id"]),
                    actor=actor,
                    parent_request_key=parent_request_key,
                )
            return child if isinstance(child, Mapping) else None
        child_id = child.get("id")
        if not isinstance(child_id, str):
            return child
        try:
            operation = self._model_cache.get_operation(child_id)
            if (
                operation.state == "succeeded"
                and actor is not None
                and parent_request_key is not None
                and isinstance(operation.artifact_set_sha256, str)
            ):
                recipe_revision_id = payload.get("recipe_revision_id")
                if not isinstance(recipe_revision_id, str):
                    raise RecipeImageAvailabilityError(
                        "recipe_image.model_cache_invalid",
                        "availability operation lacks its exact recipe revision",
                        retryable=True,
                        recovery_actions=("retry",),
                    )
                preview = self._model_cache.download_preview(
                    recipe_revision_id=recipe_revision_id
                )
                preview_set = preview.get("artifact_set_sha256")
                preview_plan = preview.get("plan_digest")
                new_bytes = preview.get("new_bytes")
                if (
                    preview_set != operation.artifact_set_sha256
                    or not isinstance(preview_plan, str)
                    or type(new_bytes) is not int
                    or new_bytes < 0
                ):
                    raise RecipeImageAvailabilityError(
                        "recipe_image.model_cache_invalid",
                        "ModelCache returned an incomplete exact artifact plan",
                        retryable=True,
                        recovery_actions=("retry",),
                    )
                if new_bytes > 0:
                    return self._ensure_model_child(
                        recipe_revision_id,
                        actor=actor,
                        parent_request_key=(
                            f"{parent_request_key}:restore:{child_id}:{preview_plan}"
                        ),
                    )
        except ModelCacheNotFound:
            return dict(child) | {
                "state": "failed",
                "failure": {
                    "code": "recipe_image.model_child_missing",
                    "detail": "durable ModelCache child operation is unavailable",
                    "retryable": True,
                    "recovery_actions": ["retry"],
                },
            }
        failure = operation.failure
        return dict(child) | {
            "state": operation.state,
            "progress": project_cache_progress(operation.progress, self._clock()),
            "artifact_set_sha256": operation.artifact_set_sha256,
            "plan_digest": operation.plan_digest,
            "failure": (dict(failure) if isinstance(failure, Mapping) else None),
        }

    def _update_model_progress(
        self, claim: RecipeImageAvailabilityClaim, child: Mapping[str, object]
    ) -> bool:
        child_id = child.get("id")
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
                    raise RecipeImageAvailabilityError(
                        "recipe_image.model_child_missing",
                        "durable ModelCache child operation is unavailable",
                        retryable=True,
                        recovery_actions=("retry",),
                    )
            operation = session.scalar(
                select(Job)
                .where(Job.id == claim.operation_id, Job.kind == OPERATION_KIND)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
            if (
                operation is None
                or operation.current_attempt != claim.execution_attempt
                or not isinstance(operation.payload, Mapping)
                or operation.payload.get("claim_owner") != claim.claim_owner
            ):
                raise _AvailabilityClaimLost()
            cancelling = operation.state == "cancelling"
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
                raise RecipeImageAvailabilityError(
                    "recipe_image.model_child_cancelled",
                    "ModelCache child was cancelled while joining the operation",
                    retryable=True,
                    recovery_actions=("resume", "retry"),
                )
            payload = dict(operation.payload)
            payload["model_child"] = dict(child)
            operation.payload = payload
            operation.updated_at = self._clock()
            # Keep image progress separate. The view aggregates the two
            # durable members exactly once.
            return not cancelling

    @staticmethod
    def _model_child_has_cancel_intent(operation: ModelCacheOperation) -> bool:
        try:
            payload = require_mapping(operation.payload, "ModelCache payload")
            raw = payload.get("cancellation")
            if raw is None:
                return False
            ModelCacheCancellation.model_validate_json(json.dumps(raw, allow_nan=False))
            return True
        except (TypeError, ValueError, ValidationError) as error:
            raise RecipeImageAvailabilityError(
                "recipe_image.model_cache_invalid",
                "ModelCache cancellation evidence is malformed",
            ) from error

    def _defer_for_model(self, claim: RecipeImageAvailabilityClaim) -> None:
        with self._sessions.begin() as session:
            operation = self._require_claim(session, claim)
            now = self._clock()
            operation.state = "partial"
            operation.updated_at = now
            operation.payload = dict(operation.payload) | {
                "claim_owner": None,
                "claim_until": None,
                "retry_after_at": _iso(now + timedelta(seconds=1)),
            }

    def _prepare_claimed_image(
        self,
        claim: RecipeImageAvailabilityClaim,
        payload: Mapping[str, object],
        recipe: RecipeDefinition,
        runtime: Mapping[str, object],
    ) -> RuntimeImageReceipt:
        force_download = payload.get("force_download") is True
        force_rebuild = payload.get("force_rebuild") is True

        def persist_provisional_reference(
            receipt: RuntimeImageReceipt,
        ) -> None:
            self._persist_provisional_image_reference(
                claim,
                receipt=receipt,
            )

        if recipe.execution.mode == "build":
            if self._builder is None:
                raise RecipeImageAvailabilityError(
                    "recipe_image.build_unavailable",
                    "no canonical recipe build executor is configured",
                )
            build_input_sha256 = payload.get("build_input_sha256")
            dispatch_identity_missing = not isinstance(build_input_sha256, str)
            if dispatch_identity_missing:
                build_input_sha256 = ""
            if self._builder_admission is not None:
                self._builder_admission(recipe, runtime)
            self._update_progress(claim, "build", total_bytes=None)

            def report(value: Mapping[str, object]) -> None:
                phase = value.get("phase", "build")
                self._update_progress(claim, str(phase), detail=value)

            # The builder re-resolves the exact executable identity and reuses
            # a verified filesystem receipt itself, so queue-time and
            # dispatch-time cache hits take the same path.
            build_receipt = self._builder(
                recipe,
                runtime,
                claim=claim,
                build_input_sha256=build_input_sha256,
                force=force_rebuild,
                progress=report,
            )
            if not isinstance(build_receipt, Mapping):
                raise RecipeImageAvailabilityError(
                    "recipe_image.build_invalid", "builder returned no receipt"
                )
            if dispatch_identity_missing:
                resolved_input = build_receipt.get("build_input_sha256")
                if not isinstance(resolved_input, str):
                    raise RecipeImageAvailabilityError(
                        "recipe_image.build_input_missing",
                        "dispatch did not bind an exact build input identity",
                        retryable=True,
                        recovery_actions=("retry",),
                    )
                with self._sessions.begin() as session:
                    operation = self._require_claim(session, claim)
                    assigned_runtime = dict(
                        require_mapping(operation.payload["runtime"], "runtime")
                    )
                    if isinstance(build_receipt.get("builder_node_id"), str):
                        assigned_runtime["builder_node_id"] = build_receipt[
                            "builder_node_id"
                        ]
                    if isinstance(build_receipt.get("build_input_sha256"), str):
                        assigned_runtime["build_input_sha256"] = build_receipt[
                            "build_input_sha256"
                        ]
                    operation.payload = dict(operation.payload) | {
                        "build_input_sha256": resolved_input,
                        "identity_key": resolved_input,
                        "runtime": assigned_runtime,
                    }
            self._update_progress(claim, "verify")
            return prepare_runtime_image(
                recipe,
                runtime=runtime,
                storage=self._storage,
                transport=self._transport,
                build_receipt=build_receipt,
                now=self._clock(),
                force=False,
                before_publish=persist_provisional_reference,
            )
        total = _known_total(runtime)
        self._update_progress(claim, "download", total_bytes=total)
        receipt = prepare_runtime_image(
            recipe,
            runtime=runtime,
            storage=self._storage,
            transport=self._transport,
            now=self._clock(),
            force=force_download,
            progress=lambda phase, completed, total: self._update_progress(
                claim,
                phase,
                completed_bytes=completed,
                total_bytes=total,
            ),
            before_publish=persist_provisional_reference,
        )
        self._update_progress(
            claim,
            "verify",
            total_bytes=receipt.image_bytes,
            completed_bytes=receipt.image_bytes,
        )
        return receipt

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
                session, claim, allowed_states=("running", "cancelling")
            )
            if operation is None:
                return False
            operation.payload = dict(operation.payload) | {
                "claim_until": _iso(now + timedelta(seconds=self._claim_lease_seconds)),
            }
            operation.updated_at = now
            return True

    def _persist_receipt(
        self,
        claim: RecipeImageAvailabilityClaim,
        payload: Mapping[str, object],
        receipt: RuntimeImageReceipt,
    ) -> None:
        execution_key = payload.get("effective_execution_key")
        if not isinstance(execution_key, str):
            raise RecipeImageAvailabilityError(
                "recipe_image.identity_invalid",
                "effective execution identity is missing",
            )
        with self._sessions.begin() as session:
            operation = self._require_claim(session, claim)
            operation_payload = dict(operation.payload)
            reference = self._image_reference_intent_for_claim(operation_payload, claim)
            if (
                operation_payload.get("image_reference_intent") is not None
                and reference is None
            ):
                raise _AvailabilityClaimLost()
            if self._receipt_writer is None:
                persist_runtime_image_receipt(
                    session,
                    recipe_revision_id=str(payload["recipe_revision_id"]),
                    original_content_digest=str(payload["recipe_content_sha256"]),
                    effective_execution_key=execution_key,
                    receipt=receipt,
                    verified_at=self._clock(),
                )
            else:
                self._receipt_writer(
                    session,
                    str(payload["recipe_revision_id"]),
                    str(payload["recipe_content_sha256"]),
                    execution_key,
                    receipt,
                )
            operation.payload = dict(operation.payload) | {
                "image_result": receipt.to_mapping()
            }
            operation.payload.pop("image_reference_intent", None)
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
                session, claim, allowed_states=("running", "cancelling")
            )
            try:
                require_reference_open(
                    session,
                    (ArtifactIdentity("runtime-image", receipt.oci_archive_sha256),),
                    now=now,
                )
            except ArtifactLifecycleError as error:
                raise RuntimeImagePreparationError(
                    error.code, error.detail, retryable=error.retryable
                ) from error
            payload = dict(operation.payload)
            existing = payload.get("image_reference_intent")
            if existing is None:
                payload["image_reference_intent"] = serialize_json_value(reference)
                operation.payload = payload
                operation.updated_at = now
            else:
                existing_reference = read_runtime_image_reference_intent(existing)
                if existing_reference != reference:
                    same_output = (
                        existing_reference.operation_id == reference.operation_id
                        and existing_reference.recipe_revision_id
                        == reference.recipe_revision_id
                        and existing_reference.oci_archive_sha256
                        == reference.oci_archive_sha256
                        and existing_reference.image_digest == reference.image_digest
                        and existing_reference.image_bytes == reference.image_bytes
                    )
                    prior_attempt = existing_reference.attempt < reference.attempt
                    if not same_output or not prior_attempt:
                        raise RuntimeImagePreparationError(
                            "runtime_image.identity_conflict",
                            "availability retry produced a different archive identity",
                        )
                    # This callback runs under the exact archive publication
                    # lock. The prior attempt can no longer commit after this
                    # owner transfer; the exact bytes remain protected without
                    # opening a gap between provisional references.
                    payload["image_reference_intent"] = serialize_json_value(reference)
                    operation.payload = payload
                    operation.updated_at = now

    @staticmethod
    def _image_reference_intent_for_claim(
        payload: Mapping[str, object], claim: RecipeImageAvailabilityClaim
    ) -> RuntimeImageReferenceIntent | None:
        raw_reference = payload.get("image_reference_intent")
        if raw_reference is None:
            return None
        reference = read_runtime_image_reference_intent(raw_reference)
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
        detail: Mapping[str, object] | None = None,
    ) -> None:
        progress = _progress(
            phase,
            total_bytes=total_bytes,
            completed_bytes=completed_bytes,
            bytes_per_second=bytes_per_second,
            eta_seconds=eta_seconds,
        )
        operation.payload = dict(operation.payload) | {"progress": progress}
        if detail:
            safe = sanitize_failure_evidence(detail)
            operation.payload = dict(operation.payload) | {
                "step": safe.get("step") or safe.get("current_step"),
                "log_excerpt": safe.get("log_excerpt") or safe.get("log"),
            }

    def _update_progress(
        self,
        claim: RecipeImageAvailabilityClaim,
        phase: str,
        *,
        total_bytes: int | None = None,
        completed_bytes: int = 0,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        with self._sessions.begin() as session:
            operation = self._require_claim(session, claim)
            if detail is not None:
                raw_completed = detail.get(
                    "completed_bytes", detail.get("downloaded_bytes")
                )
                if type(raw_completed) is int and raw_completed >= 0:
                    completed_bytes = raw_completed
                raw_total = detail.get("total_bytes", detail.get("expected_bytes"))
                if type(raw_total) is int and raw_total >= 0:
                    total_bytes = raw_total
                raw_rate = detail.get("bytes_per_second")
                bytes_per_second = (
                    float(raw_rate)
                    if isinstance(raw_rate, (int, float))
                    and not isinstance(raw_rate, bool)
                    and raw_rate >= 0
                    else None
                )
                raw_eta = detail.get("eta_seconds")
                eta_seconds = (
                    float(raw_eta)
                    if isinstance(raw_eta, (int, float))
                    and not isinstance(raw_eta, bool)
                    and raw_eta >= 0
                    else None
                )
            else:
                bytes_per_second = None
                eta_seconds = None
            raw_progress = (
                operation.payload.get("progress")
                if isinstance(operation.payload, Mapping)
                else None
            )
            old = raw_progress if isinstance(raw_progress, Mapping) else {}
            old_completed = old.get("completed_bytes", 0)
            if type(old_completed) is int and old_completed > completed_bytes:
                completed_bytes = old_completed
            self._set_progress(
                operation,
                phase,
                total_bytes=total_bytes,
                completed_bytes=completed_bytes,
                bytes_per_second=bytes_per_second,
                eta_seconds=eta_seconds,
                detail=detail,
            )
            operation.updated_at = self._clock()

    def _fail(
        self,
        claim: RecipeImageAvailabilityClaim,
        error: BaseException,
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
                return
            retry = operation.payload.get("retry", {})
            retry = dict(retry) if isinstance(retry, Mapping) else {}
            automatic_attempts = int(retry.get("automatic_attempts", 0))
            dependency_wait = str(code) in _DEPENDENCY_WAIT_CODES
            if retryable and retry_after is None:
                retry_after = 5 if dependency_wait else min(60, 2**automatic_attempts)
            bounded = retryable and (
                dependency_wait
                or automatic_attempts + 1 < self._automatic_attempt_limit
            )
            retry["automatic_attempts"] = automatic_attempts + int(not dependency_wait)
            now = self._clock()
            now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
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
            failure: dict[str, object] = {
                "code": str(code)[:64],
                "detail": str(detail)[:512],
                "recovery_actions": list(getattr(error, "recovery_actions", ()))
                or _recovery_actions(operation.payload, str(code), retryable),
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
            failure = sanitize_failure_evidence(failure)
            operation.result = None
            payload = dict(operation.payload)
            reference = self._image_reference_intent_for_claim(payload, claim)
            if payload.get("image_reference_intent") is not None and reference is None:
                return
            payload.pop("image_reference_intent", None)
            payload |= {"retry": retry, "failure": failure}
            if str(code) in {
                "recipe_image.build_failed",
                "runtime_image.cache_missing",
            }:
                # The failed effect is settled; a later execution claim may
                # create a new child. Observation waits retain the exact child.
                payload.pop("build_dependency", None)
            if isinstance(preserved_retry_time, str):
                try:
                    parsed_retry_time = datetime.fromisoformat(preserved_retry_time)
                except ValueError:
                    parsed_retry_time = None
                if parsed_retry_time is not None:
                    payload["retry_after_at"] = _iso(parsed_retry_time)
                elif retry_after is not None:
                    payload["retry_after_at"] = _iso(
                        now + timedelta(seconds=retry_after)
                    )
                else:
                    payload.pop("retry_after_at", None)
            elif retry_after is not None:
                payload["retry_after_at"] = _iso(now + timedelta(seconds=retry_after))
            else:
                payload.pop("retry_after_at", None)
            payload["claim_owner"] = None
            payload["claim_until"] = None
            operation.payload = payload
            operation.state = "queued" if bounded else "failed"
            operation.updated_at = self._clock()
            operation.current_attempt = int(operation.current_attempt)

    def _view(self, operation: Job) -> RecipeImageAvailabilityView:
        payload = operation.payload if isinstance(operation.payload, Mapping) else {}
        result = operation.result if isinstance(operation.result, Mapping) else None
        model_child = self._current_model_child(payload)
        raw_image_progress = payload.get("progress")
        image_progress = (
            dict(raw_image_progress) if isinstance(raw_image_progress, Mapping) else {}
        )
        image_result = payload.get("image_result")
        image_ready = isinstance(image_result, Mapping)
        image_state = "succeeded" if image_ready else operation.state
        raw_failure = payload.get("failure")
        failure = raw_failure if isinstance(raw_failure, Mapping) else None
        if operation.state == "succeeded" and (result is None or failure is not None):
            raise ValueError(
                "successful image availability requires a result and no failure"
            )
        if operation.state == "failed" and failure is None:
            raise ValueError("failed image availability requires failure evidence")
        if operation.state != "succeeded" and result is not None:
            raise ValueError("image availability result requires success")
        if image_ready:
            image_bytes = image_result.get("image_bytes")
            image_progress.update(
                phase="available",
                completed_bytes=image_bytes if type(image_bytes) is int else 0,
                total_bytes=image_bytes if type(image_bytes) is int else None,
                total_bytes_known=type(image_bytes) is int,
            )
            image_progress.pop("bytes_per_second", None)
            image_progress.pop("eta_seconds", None)
        progress = dict(image_progress)
        image_members = [
            {
                "member_id": "runtime-image",
                "phase": str(image_progress.get("phase", "prepare")),
                "completed_bytes": int(image_progress.get("completed_bytes", 0) or 0),
                "total_bytes": image_progress.get("total_bytes"),
                "state": image_state,
            }
        ]
        progress["members"] = image_members
        if model_child is not None:
            child = OperationProgress.model_validate(model_child["progress"])
            model_progress = child.model_dump(
                mode="json",
                exclude_none=True,
                exclude={"members", "checkpoint", "total_bytes_known"},
            )
            image = OperationProgress.model_validate(image_progress)
            image_member = image.model_dump(
                mode="json",
                exclude_none=True,
                exclude={"members", "checkpoint", "total_bytes_known"},
            )
            progress = aggregate_progress(
                [
                    OperationMemberProgress.model_validate(
                        image_member
                        | {"member_id": "runtime-image", "state": image_state}
                    ),
                    OperationMemberProgress.model_validate(
                        model_progress
                        | {
                            "member_id": "model-cache",
                            "state": str(model_child["state"]),
                        }
                    ),
                ]
            ).model_dump(mode="json", exclude_none=True)
        actions = (
            tuple(
                str(item)
                for item in failure.get("recovery_actions", [])
                if isinstance(item, str)
            )
            if failure is not None and isinstance(failure.get("recovery_actions"), list)
            else ()
        )
        raw_model_digest = payload.get("model_digest")
        raw_build_input_sha256 = payload.get("build_input_sha256")
        return RecipeImageAvailabilityView(
            id=operation.id,
            request_id=operation.request_id,
            request=read_availability_intent(payload.get("request")),
            kind=operation.kind,
            state=operation.state,
            attempt=int(operation.current_attempt),
            recipe_revision_id=str(payload.get("recipe_revision_id", "")),
            recipe_content_sha256=str(payload.get("recipe_content_sha256", "")),
            model_digest=(
                raw_model_digest if isinstance(raw_model_digest, str) else None
            ),
            build_input_sha256=(
                raw_build_input_sha256
                if isinstance(raw_build_input_sha256, str)
                else None
            ),
            progress=progress,
            image_progress=image_progress,
            image_state=image_state,
            image_failure=None if image_ready or failure is None else dict(failure),
            result=(
                dict(result)
                if result is not None and operation.state == "succeeded"
                else None
            ),
            failure=(dict(failure) if failure is not None else None),
            supported_actions=actions,
            created_at=_iso(operation.created_at),
            updated_at=_iso(operation.updated_at),
            model_child=(None if model_child is None else dict(model_child)),
            cancellation=self._stored_cancellation(operation),
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
