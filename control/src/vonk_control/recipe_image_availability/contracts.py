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

import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    OperationCheckpoint,
    OperationProgress,
    RecipeBuildCode,
    RecipeImageCode,
    RecipePackageCode,
    RuntimeImageCode,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_forge_contracts import RecipeDefinition, read_recipe

from .. import job_states
from ..admission_locking import is_admission_contention
from ..cache_removal_review import (
    CacheRemovalAsset,
    CacheRemovalFinding,
)
from ..categorized_errors import InvalidValue
from ..categorized_faults import security_reason
from ..failure_classification import is_security_failure
from ..job_documents import (
    AvailabilityRuntime,
)
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..model_cache import (
    ModelCacheError,
    ModelCacheRemovalScope,
    model_cache_failure_is_terminal,
)
from ..model_cache_contract import (
    ModelCacheOperationProgress,
)
from ..operation_blockers import (
    OperationBlocker,
)
from ..operation_contract import (
    AvailabilityOperationFailure,
)
from ..recipe_image_removal_contract import (
    RECIPE_CACHE_REMOVE_KIND,
)
from ..runtime_image_preparation import (
    RuntimeImagePreparationUnknown,
    RuntimeImageStorage,
)
from ..stored_json import JsonColumn, read_column

_LOGGER = logging.getLogger(__package__)
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

    attempts = 0
    while error is not None and attempts < 8:
        if isinstance(error, DBAPIError) and is_admission_contention(error):
            return True
        # Only an explicit cause: __context__ is whatever was being handled
        # when this was raised, which says nothing about this failure.
        error = error.__cause__
        attempts += 1
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

    if (
        code
        in {RecipeImageCode.MODEL_CHILD_MISSING, RecipeImageCode.MODEL_CHILD_CANCELLED}
        and not retryable
    ):
        return []
    if code in _CAPACITY_FAILURE_CODES:
        return ["free_space"]
    if code in _INTEGRITY_FAILURE_CODES or code in _RECOVERABLE_MISS_CODES:
        return ["retry"] if retryable else []
    if retryable:
        return ["retry"]
    return ["inspect"]
