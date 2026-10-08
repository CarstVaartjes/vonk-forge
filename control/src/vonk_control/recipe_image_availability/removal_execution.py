"""Removal execution for exact recipe image availability."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import DBAPIError
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    InvalidRequestReason,
    LifecycleState,
    ModelCacheCode,
    RecipeImageCode,
    RuntimeImageCode,
    WaitReason,
)

from .. import job_states, model_cache_states
from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    check_removal_fence_nowait,
    dead_removal_identities,
    release_dead_removal_nowait,
    retryable_artifact_database_error,
)
from ..artifact_reference_scan import (
    runtime_image_reference_reasons,
)
from ..categorized_errors import InvalidValue
from ..logging import log_event
from ..model_cache import (
    ModelCacheError,
    ModelCacheNotFound,
)
from ..model_cache_contract import (
    ModelCacheRemovalResult,
)
from ..models import (
    Job,
)
from ..operation_contract import (
    AvailabilityOperationFailure,
    sanitize_failure_evidence,
)
from ..recipe_image_removal_contract import (
    RecipeCacheRemovalModelChild,
    RecipeCacheRemovalOwner,
)
from ..recovery_policy import RecoveryPolicy
from ..runtime_image_preparation import (
    RuntimeImagePreparationError,
    RuntimeImagePreparationUnknown,
)
from ..stored_json import read_row_column
from ..strict_json import read_stored_model, serialize_json_value
from .contracts import (
    _LOGGER,
    REMOVE_OPERATION_KIND,
    ModelCacheRemovalCoordinator,
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityUnknown,
    _iso,
    _removal_retry_is_due,
    _retryable,
)

if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def reconcile_removal_gates(
    self: RecipeImageAvailabilityService, *, limit: int = 64
) -> int:
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


def advance_removals(self: RecipeImageAvailabilityService, *, limit: int = 1) -> int:
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
    deadline = time.monotonic() + 0.25
    while advanced < limit and time.monotonic() < deadline:
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
                        if damaged is not None and damaged.state in job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.BACKOFF,
                        ):
                            self._lifecycle.fail(
                                damaged,
                                self._clock(),
                                retryable=False,
                                reason=f"{error.code}: {error.detail}",
                            )
                    self.reconcile_removal_gates()
                continue
            if advanced >= limit:
                break
        last_updated, last_id = rows[-1][1], rows[-1][0]
        boundary = (last_updated, last_id)
    return advanced


def _advance_recipe_removal(
    self: RecipeImageAvailabilityService, operation_id: str
) -> bool:
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
    self: RecipeImageAvailabilityService,
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
    self: RecipeImageAvailabilityService,
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
    self: RecipeImageAvailabilityService,
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
    self: RecipeImageAvailabilityService,
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
    self: RecipeImageAvailabilityService,
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
    self: RecipeImageAvailabilityService,
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
            if retryable and retry_attempts + 1 >= RecoveryPolicy().max_failures:
                retryable = False
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
                    "recovery_actions": [],
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
        # Storage locks are acquired only after the terminal SQL commit. Dead
        # ownership must not strand a gate until an unrelated worker tick.
        if not retryable:
            self.reconcile_removal_gates()
        return True
    except DBAPIError as error:
        translated = retryable_artifact_database_error(error)
        if translated is None:
            raise
        return False
