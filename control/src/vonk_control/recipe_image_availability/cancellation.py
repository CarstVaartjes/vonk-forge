"""Cancellation for exact recipe image availability."""

from __future__ import annotations

import uuid
from datetime import UTC
from typing import TYPE_CHECKING, cast

from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    RecipeImageCode,
    WaitReason,
)

from .. import job_states, model_cache_states
from ..categorized_errors import InvalidValue, MissingRecord
from ..job_documents import (
    AvailabilityJobPayload,
    RecipeBuildParent,
)
from ..models import (
    Job,
    ModelCacheOperation,
    RecipeBuild,
)
from ..recipe_build_cancellation import (
    BuildConsumerError,
    current_build_consumers,
    lock_build_dependency,
    request_build_cancellation,
)
from ..recipe_image_availability_reader_contract import (
    StoredAvailabilityIdentity,
)
from ..recipe_image_availability_view_contract import (
    RecipeImageAvailabilityView,
)
from ..recipe_lifecycle_contract import RecipeOperationCancellationResult
from ..recipe_update_contract import UPDATE_KIND, RecipeUpdateResponse
from ..stored_json import Residue, read_row_column
from ..strict_json import serialize_json_value
from .contracts import (
    OPERATION_KIND,
    ModelCacheCancellationOwner,
    RecipeImageAvailabilityInvalid,
    RecipeImageAvailabilityUnknown,
    _canonical_cancellation_id,
    _read,
)

if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def update(
    self: RecipeImageAvailabilityService,
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
    self: RecipeImageAvailabilityService,
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
    self: RecipeImageAvailabilityService,
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
    self: RecipeImageAvailabilityService, job: Job
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
    self: RecipeImageAvailabilityService,
    operation_id: str,
    cancellation: RecipeOperationCancellationResult,
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


def reconcile_cancellations(
    self: RecipeImageAvailabilityService, *, limit: int = 8
) -> int:
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
    self: RecipeImageAvailabilityService,
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
    self: RecipeImageAvailabilityService,
    operation_id: str,
    payload: AvailabilityJobPayload,
    current_attempt: int,
    cancellation: RecipeOperationCancellationResult,
) -> bool:
    dependency = payload.build_dependency
    request_key = str(dependency.request_key) if dependency else None
    child_operation_id = (
        str(dependency.operation_id) if dependency and dependency.operation_id else None
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
