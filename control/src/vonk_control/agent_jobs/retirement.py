"""Retirement for the node-scoped agent queue."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationState,
    InvalidRequestReason,
    LifecycleState,
    ReservationState,
    RunState,
    canonical_message,
)

from .. import agent_operation_states, job_states
from ..agent_operation_facts import (
    attempt_holds_open_launch_budget as _attempt_holds_open_launch_budget,
)
from ..agent_operation_facts import aware as _aware
from ..categorized_errors import InvalidValue, MissingRecord
from ..lifecycle import CancelRequested, OperatorAction, Outcome, Reported
from ..lifecycle.agent_operation import (
    AgentOperationAdapter,
    set_parent_state,
)
from ..lifecycle.evidence import Residue
from ..models import (
    AgentNode,
    AgentOperationAttempt,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from ..models import AgentOperation as StoredOperation
from ..recipe_lifecycle_contract import (
    RecipeOperationCancellationResult,
)
from .contracts import _WORKLOAD_INTENT_OPERATIONS, OperatorRetirementRefused
from .retry import _retry_authorized_for_current_attempt
from .scope import _target_scope
from .stored import column_field, column_is_document, column_value


def release_owned_reservations_in_session(
    session: Session, owner_kind: str, owner_id: str, now: datetime
) -> None:
    """Release every active resource reservation an operation owner holds."""

    for reservation in session.scalars(
        select(ResourceReservation).where(
            ResourceReservation.owner_kind == owner_kind,
            ResourceReservation.owner_id == owner_id,
            ResourceReservation.state == ReservationState.ACTIVE,
        )
    ):
        reservation.state = ReservationState.RELEASED
        reservation.released_at = now


def operator_resume_candidates_in_session(
    session: Session, job_id: str, now: datetime
) -> tuple[StoredOperation, ...]:
    """Return the parked children whose current Job owner still has intent.

    The Job projection and the resume mutation share this owner and intent
    decision. The mutation checks the retry budget as a typed refusal and
    the former as an absent action.
    """

    job = session.get(Job, job_id)
    if job is None or job.state not in job_states.words(
        LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
    ):
        return ()
    if column_value(job, "result") is not None:
        if not column_is_document(job, "result"):
            return ()
        cancel_requested = column_field(job, "result", "cancel_requested")
        if cancel_requested is not None and cancel_requested is not False:
            return ()
    scope = _target_scope(column_value(job, "targets"))
    if scope is None:
        return ()
    parent_intent = column_field(job, "payload", "workload_intent_ordinal")
    if parent_intent is not None and (
        type(parent_intent) is not int or parent_intent < 1
    ):
        return ()
    candidates = tuple(
        session.scalars(
            select(StoredOperation)
            .where(
                StoredOperation.parent_job_id == job_id,
                StoredOperation.state.in_(agent_operation_states.PARKED),
            )
            .order_by(StoredOperation.id)
        )
    )
    if not candidates:
        return ()
    nodes = {
        node.node_id: node
        for node in session.scalars(
            select(AgentNode).where(AgentNode.node_id.in_(scope))
        )
    }
    # A topology-wide request cannot resume only its still-current subset.
    # The mutation locks this complete target scope before this shared decision.
    if set(nodes) != set(scope) or any(
        node.state != "active"
        or node.revoked_at is not None
        or (parent_intent is not None and node.workload_intent_ordinal != parent_intent)
        for node in nodes.values()
    ):
        return ()
    eligible: list[StoredOperation] = []
    for operation in candidates:
        if operation.node_id not in scope:
            continue
        node = nodes.get(operation.node_id)
        if node is None:
            continue
        if operation.current_attempt < 1:
            continue
        if parent_intent is None:
            if operation.workload_intent_ordinal is not None:
                continue
        elif operation.workload_intent_ordinal != parent_intent:
            continue
        if (
            operation.kind in _WORKLOAD_INTENT_OPERATIONS
            and operation.workload_intent_ordinal is None
        ):
            continue
        eligible.append(operation)
    return tuple(eligible)


def operator_resume_eligible_operations_in_session(
    session: Session, job_id: str, now: datetime
) -> tuple[StoredOperation, ...]:
    """Return the current-owner resume action for parked, unscheduled work."""

    job = session.get(Job, job_id)
    if job is None or job.state not in job_states.words(LifecycleState.NEEDS_OPERATOR):
        return ()
    return tuple(
        operation
        for operation in operator_resume_candidates_in_session(session, job_id, now)
        if not _retry_authorized_for_current_attempt(operation)
    )


def authorize_operator_resume_in_session(
    session: Session, job_id: str, now: datetime
) -> None:
    """Authorise one bounded claim for each parked operation of ``job_id``.

    The claim predicate requires a ``waiting-for-operator`` operation to carry
    its own schedule (``next_action_at``), so releasing the parent job alone
    leaves the operation unclaimable and an operator resume that wrote only the
    parent state silently did nothing.  The lifecycle core takes the
    operator's ``resume`` and schedules the claim, due immediately.  Earlier
    failures never exhaust it: retry rate is bounded by the claim path, not the
    lifetime of intent.  It performs no external work, so it is safe inside the
    caller's SQL transaction.
    """

    operations = operator_resume_candidates_in_session(session, job_id, now)
    if not operations:
        raise InvalidValue(
            "job has no current authorized resume action",
            reason=InvalidRequestReason.NOT_READY,
        )
    operations = tuple(
        session.scalars(
            select(StoredOperation)
            .where(StoredOperation.id.in_([operation.id for operation in operations]))
            .order_by(StoredOperation.id)
            .with_for_update(of=StoredOperation)
        )
    )
    parent = session.get(Job, job_id)
    if parent is None or (
        parent.state not in job_states.words(LifecycleState.NEEDS_OPERATOR)
        and any(
            not _retry_authorized_for_current_attempt(operation)
            for operation in operations
        )
    ):
        raise InvalidValue(
            "job is not waiting for operator", reason=InvalidRequestReason.NOT_READY
        )
    adapter = AgentOperationAdapter(
        session, resume_candidates=operator_resume_candidates_in_session
    )
    for operation in operations:
        # An order already scheduled at this attempt is left alone: resume is
        # idempotent and must not move a scheduled retry earlier (the core
        # ignores a resume of anything but a real operator wait).  The due time
        # is set: the parent aggregate treats a retry without one as unparked
        # and would return the job to ``waiting-for-operator`` before the agent
        # polls.
        adapter.settle(
            operation,
            None,
            parent,
            OperatorAction(
                "resume",
                reason=(
                    f"operator resumed; attempt {operation.current_attempt + 1} "
                    "authorised"
                ),
            ),
            now,
        )


def retire_exhausted_operations_in_session(
    session: Session, job_id: str, now: datetime
) -> tuple[str, ...]:
    """Fence exhausted orders while retaining their effects for exact cleanup.

    This is the bounded, audited terminal counterpart to
    :func:`authorize_operator_resume_in_session`.  It is admitted only when no
    parked operation of the job holds a live attempt lease, an
    already-authorised retry, or an open launch budget. An expired lease does not prove the effect
    stopped. The worker resumes the ordinary stop/uninstall path from durable
    cancellation facts; only its successful receipt releases reservations.
    """

    job = session.get(Job, job_id)
    if job is None:
        raise MissingRecord(job_id, reason=InvalidRequestReason.NOT_FOUND)
    scope = _target_scope(column_value(job, "targets"))
    from .service import AgentJobService

    if not scope or not AgentJobService._lock_target_scopes(
        session, {"retire": (job_id, scope)}, scope[0]
    ):
        raise OperatorRetirementRefused(job_id, "its target scope changed")
    if job.state not in job_states.words(LifecycleState.NEEDS_OPERATOR):
        raise OperatorRetirementRefused(job_id, "job is not waiting for operator")
    if (
        session.scalar(
            select(StoredOperation.id)
            .where(
                StoredOperation.parent_job_id == job_id,
                or_(
                    StoredOperation.state == "running",
                    and_(
                        StoredOperation.state == "queued",
                        StoredOperation.current_attempt > 0,
                    ),
                ),
            )
            .limit(1)
        )
        is not None
    ):
        raise OperatorRetirementRefused(
            job_id, "another issued operation is still active"
        )
    operations = tuple(
        session.scalars(
            select(StoredOperation)
            .where(
                StoredOperation.parent_job_id == job_id,
                StoredOperation.state.in_(agent_operation_states.PARKED),
            )
            .order_by(StoredOperation.id)
            .with_for_update(of=StoredOperation)
        )
    )
    if not operations:
        raise OperatorRetirementRefused(job_id, "job has no parked operation")
    attempts: list[AgentOperationAttempt] = []
    for operation in operations:
        if _retry_authorized_for_current_attempt(operation):
            raise OperatorRetirementRefused(
                operation.id, "another attempt is already authorised"
            )
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        if (
            attempt is not None
            and attempt.state == "running"
            and _aware(attempt.lease_deadline) > _aware(now)
        ):
            raise OperatorRetirementRefused(
                operation.id, "a live attempt still holds its lease"
            )
        if _attempt_holds_open_launch_budget(operation, attempt, now):
            raise OperatorRetirementRefused(
                operation.id, "its issued start still holds an open launch budget"
            )
        if attempt is not None:
            attempts.append(attempt)
    reasons = {
        operation.id: (
            f"operator retired the parked {operation.kind} operation; capacity "
            "is retained until exact cleanup confirms the effect stopped"
        )
        for operation in operations
    }
    adapter = AgentOperationAdapter(session)
    for operation in operations:
        # Retirement is the operator's definite decision to end the order; its
        # effect stays unknown for the exact cleanup to resolve.
        adapter.settle(
            operation,
            None,
            job,
            Reported(Outcome.FAILED, retryable=False, reason=reasons[operation.id]),
            now,
        )
    for attempt in attempts:
        adapter.expire_attempt(attempt)
    job_reason = reasons[operations[0].id]
    if job.kind in {
        "recipe.start",
        "recipe.stop",
        "recipe.install",
        "recipe.uninstall",
        "recipe.reconcile",
    }:
        previous_document = column_value(job, "result")
        if isinstance(previous_document, Residue):
            # End the queue owner, retaining the damaged document as evidence.
            job_reason = f"{previous_document.reason.value}: {job_reason}"[:512]
        else:
            previous = (
                previous_document.model_dump(mode="json", exclude_none=True)
                if isinstance(previous_document, BaseModel)
                else {}
            )
            # Reuse the lifecycle cancellation contract. Failed + cancelled is the
            # durable retirement handoff; ordinary cancellation uses cancelled state.
            job.result = RecipeOperationCancellationResult.model_validate_json(
                canonical_message(
                    {
                        "cancel_requested": True,
                        "cancelled": True,
                        "cancel_requested_at": _aware(now).isoformat(),
                        "cancel_request_id": str(
                            uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:retire:{job.id}")
                        ),
                        "cancel_actor": job.actor,
                        "reason": job_reason[:512],
                        **{
                            key: previous[key]
                            for key in ("node_evidence", "launch_evidence")
                            if key in previous
                        },
                    }
                ),
                strict=True,
            ).model_dump(mode="json", exclude_none=True)
    _release_retired_owner_in_session(session, job, job_reason, now)
    set_parent_state(job, "failed", job_reason, now)
    return tuple(operation.id for operation in operations)


def _release_retired_owner_in_session(
    session: Session, job: Job, reason: str, now: datetime
) -> None:
    """Retain uncertain effects for the normal exact cleanup lifecycle.

    The owner binding is read from the job's own payload, so this stays the
    same authority the operation was admitted under. Retirement proves only
    that the order ended; even failed owners can still have physical effects.
    Only an already stopped/uninstalled owner has evidence to release capacity.
    """

    owner_kind = column_field(job, "payload", "owner_kind")
    owner_id = column_field(job, "payload", "owner_id")
    if isinstance(owner_kind, str) and isinstance(owner_id, str):
        if owner_kind == "run":
            run = session.get(RecipeRun, owner_id, with_for_update=True)
            # Retirement is bookkeeping, not evidence a serving run stopped.
            if run is not None and run.state == RunState.STOPPED:
                release_owned_reservations_in_session(
                    session, owner_kind, owner_id, now
                )
        elif owner_kind == "installation":
            installation = session.get(
                RecipeInstallation, owner_id, with_for_update=True
            )
            if installation is not None and installation.state in {
                InstallationState.PLANNED,
                InstallationState.INSTALLING,
            }:
                # Ending an unfinished installer releases its busy marker;
                # verified installations and serving runs remain unchanged.
                installation.state = InstallationState.PARTIAL
                installation.updated_at = now
            if (
                installation is not None
                and installation.state == InstallationState.UNINSTALLED
            ):
                release_owned_reservations_in_session(
                    session, owner_kind, owner_id, now
                )
    adapter = AgentOperationAdapter(session)
    for child in session.scalars(
        select(StoredOperation).where(
            StoredOperation.parent_job_id == job.id,
            StoredOperation.state == "queued",
            StoredOperation.current_attempt == 0,
        )
    ):
        adapter.settle(
            child,
            None,
            job,
            CancelRequested(reason="parent operation was retired by the operator"),
            now,
        )
