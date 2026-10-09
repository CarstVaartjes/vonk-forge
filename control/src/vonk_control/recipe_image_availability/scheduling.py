"""Scheduling for exact recipe image availability."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session, object_session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    InvalidRequestReason,
    LifecycleState,
    RecipeImageCode,
    WaitReason,
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
    AvailabilityUnknownEnd,
)
from ..models import (
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
    RecipeImageAvailabilityUnknown,
)
from .failure_projection import _OBSERVATION_BUDGET

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
    self: RecipeImageAvailabilityService,
    *,
    limit: int = 4,
    owner_id: str | None = None,
    requested_operation_id: str | None = None,
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
        # Publication never changes intent. Accepted requests fence obsolete
        # claims; this scan only observes their durable preparation clocks.
        candidate_ids = list(
            session.scalars(
                select(Job.id)
                .where(
                    Job.kind == OPERATION_KIND,
                    Job.id == requested_operation_id
                    if requested_operation_id is not None
                    else true(),
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.BACKOFF,
                            LifecycleState.OBSERVING,
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
                            LifecycleState.OBSERVING,
                        )
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            if operation is None:
                continue
            payload = self._payload(operation)
            if isinstance(payload, Residue):
                # No readable accepted execution can be dispatched. End and
                # fence this owner; a new request has independent authority.
                self._lifecycle.fail(
                    operation, now, reason=payload.reason.value, retryable=False
                )
                operation.payload = serialize_json_value(
                    AvailabilityUnknownEnd(residue=payload.reason)
                )
                continue
            if (
                operation.state == LifecycleState.OBSERVING.value
                and self._stored_cancellation(operation) is not None
            ):
                # Cancellation has its own immutable observation bound.
                continue
            created = operation.created_at
            created = created if created.tzinfo else created.replace(tzinfo=UTC)
            if now >= created + _OBSERVATION_BUDGET:
                _expire_preparation(self, session, operation, payload, now)
                continue
            if (
                operation.state == LifecycleState.RUNNING.value
                and payload.claim_until is not None
                and now < payload.claim_until
            ):
                continue
            if not self._retry_due(payload, now):
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


def _expire_preparation(
    self: RecipeImageAvailabilityService,
    session: Session,
    operation: Job,
    payload: AvailabilityJobPayload,
    now: datetime,
) -> None:
    """Fence publication, then let bounded cancellation reconcile exact children."""
    operation.payload = serialize_json_value(
        payload.model_copy(
            update={
                "failure": AvailabilityOperationFailure(
                    code=RecipeImageCode.PREPARATION_EXHAUSTED,
                    detail="Image preparation observation budget ended",
                    retryable=False,
                    recovery_actions=[],
                ),
            }
        )
    )
    self._request_cancellation(
        session,
        operation,
        actor=operation.actor,
        request_id=str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:image-expiry:{operation.id}")
        ),
        reason=WaitReason.OBSERVATION_UNAVAILABLE.value,
        authorize=False,
    )


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


def _cancel_superseded_operation(
    self: RecipeImageAvailabilityService,
    operation: Job,
    newer_revision_id: str,
    *,
    now: datetime,
) -> bool:
    """Fence every obsolete preparation; cancellation observes its exact children."""
    if operation.state not in job_states.words(
        LifecycleState.QUEUED,
        LifecycleState.RUNNING,
        LifecycleState.BACKOFF,
        LifecycleState.OBSERVING,
    ):
        return False
    payload = self._payload(operation)
    if isinstance(payload, Residue):
        self._lifecycle.fail(
            operation, now, reason=payload.reason.value, retryable=False
        )
        operation.payload = serialize_json_value(
            AvailabilityUnknownEnd(residue=payload.reason)
        )
        return True
    detail = f"superseded by accepted preparation for {newer_revision_id}"
    failure = AvailabilityOperationFailure(
        code=SUPERSEDED_PREPARATION_CODE,
        detail=detail,
        recovery_actions=[AvailabilityRecoveryAction.DOWNLOAD_AGAIN],
        retryable=False,
    )
    updated = payload.model_copy(
        update={
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
    session = object_session(operation)
    if session is None:
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.OPERATION_INVALID,
            "Accepted preparation ownership is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if self._stored_cancellation(operation) is not None:
        return True
    self._request_cancellation(
        session,
        operation,
        actor=operation.actor,
        request_id=str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"vonk:image-supersede:{operation.id}:{newer_revision_id}:{now.isoformat()}",
            )
        ),
        reason=detail[:512],
        authorize=False,
    )
    return True


def _cancel_older_preparations(
    self: RecipeImageAvailabilityService,
    session: Session,
    *,
    newer_revision: CatalogDocumentRevision,
    now: datetime,
    limit: int = 64,
    current_operation_id: str | None = None,
) -> tuple[str, ...]:
    """Fence prior requests; fresh intent never joins an older retry budget."""
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
                Job.state.in_(
                    job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.BACKOFF,
                        LifecycleState.OBSERVING,
                    )
                ),
                CatalogDocumentRevision.document_id == newer_revision.document_id,
                Job.id != current_operation_id,
            )
            .order_by(Job.created_at, Job.id)
            .with_for_update(of=Job, skip_locked=True)
        )
    )
    cancelled: list[str] = []
    for operation in candidates:
        if self._cancel_superseded_operation(operation, newer_revision.id, now=now):
            cancelled.append(operation.id)
    return tuple(cancelled)
