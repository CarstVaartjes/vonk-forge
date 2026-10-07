"""Scheduling for exact recipe image availability."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    InvalidRequestReason,
    LifecycleState,
    RecipeImageCode,
)

from .. import job_states, model_cache_states
from ..artifact_lifecycle import (
    ArtifactIdentity,
    has_pending_removal,
)
from ..categorized_errors import InvalidValue
from ..job_documents import (
    AvailabilityJobPayload,
    AvailabilitySupersession,
)
from ..models import (
    CatalogDocumentHead,
    CatalogDocumentRevision,
    Job,
)
from ..operation_blockers import (
    make_blocker,
)
from ..operation_contract import (
    AvailabilityOperationFailure,
    AvailabilityRecoveryAction,
)
from ..stored_json import Residue
from ..strict_json import serialize_json_value
from .contracts import (
    _CLAIM_SCAN_WINDOW,
    _MODEL_WAIT_POLL_SECONDS,
    OPERATION_KIND,
    REMOVE_OPERATION_KIND,
    SUPERSEDED_PREPARATION_CODE,
    RecipeImageAvailabilityClaim,
)

if TYPE_CHECKING:
    from .service import RecipeImageAvailabilityService


def resume_operations(self: RecipeImageAvailabilityService, *, limit: int = 16) -> int:
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


def run_pending(self: RecipeImageAvailabilityService, *, limit: int = 1) -> int:
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
    self: RecipeImageAvailabilityService, *, limit: int = 4, owner_id: str | None = None
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
    self: RecipeImageAvailabilityService,
    operation: Job,
    payload: AvailabilityJobPayload,
    now: datetime,
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


def _holds_live_lease(payload: AvailabilityJobPayload, now: datetime) -> bool:
    return bool(payload.claim_owner) and (
        payload.claim_until is None or now < payload.claim_until
    )


def _cancel_superseded_operation(
    self: RecipeImageAvailabilityService,
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
    self: RecipeImageAvailabilityService,
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
    self: RecipeImageAvailabilityService,
    session: Session,
    *,
    now: datetime,
    limit: int = 64,
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
        if self._cancel_superseded_operation(operation, str(head_revision_id), now=now)
    )
