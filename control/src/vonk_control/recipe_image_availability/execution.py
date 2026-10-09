"""Execution for exact recipe image availability."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    RecipeImageCode,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_forge_contracts import RecipeDefinition

from .. import job_states, model_cache_states
from ..failure_classification import is_security_failure
from ..job_documents import (
    AvailabilityJobPayload,
    AvailabilityJobResult,
    AvailabilityModelChild,
)
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..lifecycle.image_availability import PREPARATION_BUDGET
from ..model_cache import (
    ModelCacheNotFound,
)
from ..models import (
    CatalogDocumentRevision,
    Job,
    ModelCacheOperation,
)
from ..operation_blockers import (
    make_blocker,
)
from ..operation_contract import (
    AvailabilityOperationFailure,
    AvailabilityRecoveryAction,
)
from ..recipe_availability_intent import (
    RecipeRetryIntent,
)
from ..recipe_image_availability_clocks_contract import StoredAvailabilityClocks
from ..recipe_image_availability_reader_contract import (
    AvailabilityDownloadPreview,
    StoredAvailabilityIdentity,
    StoredModelChildCancellation,
)
from ..stored_json import Residue, read_row_column
from ..strict_json import serialize_json_value
from ..worker_memory_contract import WorkerMemoryComponent
from .contracts import (
    OPERATION_KIND,
    SCHEMA_VERSION,
    BuildUnsettled,
    RecipeImageAvailabilityClaim,
    RecipeImageAvailabilityUnknown,
    _AvailabilityClaimLost,
    _known_total,
    _read,
    _same_preparation_content,
)

if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def run_claim(
    self: RecipeImageAvailabilityService, claim: RecipeImageAvailabilityClaim
) -> None:
    """Execute one claim; callers may run claims in their own bounded pool."""

    self._run(claim)


def memory_footprint(
    self: RecipeImageAvailabilityService,
) -> dict[WorkerMemoryComponent, int]:
    with self._identity_locks_guard:
        return {
            WorkerMemoryComponent.IMAGE_PREPARATION_IDENTITY_LOCKS: len(
                self._identity_locks
            )
        }


def _identity_lock(
    self: RecipeImageAvailabilityService, identity_key: str | None
) -> threading.Lock:
    if not identity_key:
        return threading.Lock()
    with self._identity_locks_guard:
        lock = self._identity_locks.get(identity_key)
        if lock is None:
            lock = threading.Lock()
            self._identity_locks[identity_key] = lock
        return lock


def _eligible(self: RecipeImageAvailabilityService, operation_id: str) -> bool:
    with self._sessions() as session:
        operation = session.get(Job, operation_id)
        if operation is None:
            return False
        clocks = StoredAvailabilityClocks.read(operation.payload)
        now = self._clock()
        now = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        return clocks.retry_after_at is None or now >= clocks.retry_after_at


def _retry_due(payload: AvailabilityJobPayload, now: datetime) -> bool:
    return payload.retry_after_at is None or now >= payload.retry_after_at


def _claim_operation(
    self: RecipeImageAvailabilityService,
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
    if operation.state == LifecycleState.RUNNING.value:
        revision = session.get(CatalogDocumentRevision, operation.authority_revision)
        if revision is not None:
            newest = session.scalar(
                select(Job)
                .join(
                    CatalogDocumentRevision,
                    CatalogDocumentRevision.id == Job.authority_revision,
                )
                .where(
                    Job.kind == OPERATION_KIND,
                    CatalogDocumentRevision.document_id == revision.document_id,
                )
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(1)
            )
            if newest is not None and newest.id != operation.id:
                latest = self._payload(newest)
                if isinstance(latest, AvailabilityJobPayload) and (
                    latest.force_rebuild
                    or not _same_preparation_content(latest, payload)
                ):
                    self._cancel_superseded_operation(
                        operation, newest.authority_revision, now=self._lifecycle.now()
                    )
                    return None
    if (
        operation.state in job_states.words(LifecycleState.OBSERVING)
        and self._stored_cancellation(operation) is None
    ):
        return None
    if operation.state == LifecycleState.RUNNING.value:
        created = operation.created_at
        created = created if created.tzinfo else created.replace(tzinfo=UTC)
        if self._lifecycle.now() >= created + PREPARATION_BUDGET:
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
    self: RecipeImageAvailabilityService,
    session: Session,
    claim: RecipeImageAvailabilityClaim,
    *,
    allowed_states: tuple[str, ...] = ("running",),
) -> Job:
    operation = self._claim_operation(session, claim, allowed_states=allowed_states)
    if operation is None:
        raise _AvailabilityClaimLost
    return operation


def _run(
    self: RecipeImageAvailabilityService, claim: RecipeImageAvailabilityClaim
) -> None:
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
        lock = self._identity_lock(identity)
        if not lock.acquire(blocking=False):
            self._fail(
                claim,
                BuildUnsettled(
                    RecipeImageCode.BUILDER_BUSY,
                    "Exact image content is being prepared by another owner",
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    retry_after_seconds=1,
                ),
            )
            return
        try:
            receipt = payload.image_result
            if receipt is None or not self._storage.build_archive_available(
                receipt.oci_archive_sha256, receipt.image_bytes
            ):
                prepared = self._prepare_claimed_image(claim, payload, recipe, runtime)
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
        finally:
            lock.release()
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
            completed_payload = completed_payload.model_copy(update={"failure": None})
            completed_payload = completed_payload.model_copy(
                update={"retry_after_at": None}
            )
            completed_payload = completed_payload.model_copy(update={"blockers": None})
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
    self: RecipeImageAvailabilityService,
    payload: AvailabilityJobPayload | StoredAvailabilityIdentity | object,
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
    self: RecipeImageAvailabilityService,
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
        if actor is not None and parent_request_key is not None:
            if operation.state == LifecycleState.CANCELLED.value:
                return self._ensure_model_child(
                    payload.recipe_revision_id,
                    actor=actor,
                    parent_request_key=f"{parent_request_key}:replacement:{child_id}",
                )
            if operation.state == LifecycleState.FAILED.value:
                failure = _read(AvailabilityOperationFailure, operation.failure)
                if not is_security_failure(
                    failure.code if failure is not None else None
                ):
                    return self._resume_model_child(
                        child, actor=actor, parent_request_key=parent_request_key
                    )
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
        if actor is not None and parent_request_key is not None:
            return self._ensure_model_child(
                payload.recipe_revision_id,
                actor=actor,
                parent_request_key=parent_request_key,
            )
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
    self: RecipeImageAvailabilityService,
    claim: RecipeImageAvailabilityClaim,
    child: AvailabilityModelChild,
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
                    recovery_actions=(),
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
                recovery_actions=(),
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


def _defer_for_model(
    self: RecipeImageAvailabilityService, claim: RecipeImageAvailabilityClaim
) -> None:
    with self._sessions.begin() as session:
        operation = self._require_claim(session, claim)
        now = self._clock()
        payload = self._payload(operation)
        if isinstance(payload, Residue):
            return
        payload = payload.model_copy(update={"claim_owner": None, "claim_until": None})
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
