"""Supersession for the node-scoped agent queue."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import InvalidRequestReason, LifecycleState, RunState

from .. import agent_operation_states, job_states
from ..admission_locking import (
    AdmissionRowLock,
    acquire_admission_keys,
    lock_admission_rows,
    node_admission_key,
)
from ..agent_operation_facts import aware as _aware
from ..categorized_errors import InvalidValue
from ..lifecycle import CancelRequested
from ..lifecycle.agent_operation import AgentOperationAdapter, set_parent_state
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..models import AgentOperation as StoredOperation
from ..models import AgentOperationAttempt, Job, RecipeRun
from ..recovery_policy import FailureKind
from .contracts import (
    _ABANDONABLE_OPERATIONS,
    _WORKLOAD_INTENT_OPERATIONS,
    SupersededAgentEffect,
)
from .evidence import _exact_service_stop_receipt_covers_start
from .retry import _abandon_operation, superseded_cancellation_deadline
from .scope import _target_scope
from .stored import column_field, column_is_document, column_message, column_value


def request_superseded_workload_cancellation_in_session(
    session: Session, targets: Sequence[str], ordinal: int, now: datetime
) -> None:
    """Cancel older overlapping orders without declaring issued effects finished."""
    scope = tuple(sorted(set(targets)))
    if (
        not scope
        or len(scope) != len(targets)
        or type(ordinal) is not int
        or ordinal < 1
    ):
        raise InvalidValue(
            "workload cancellation scope is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    acquire_admission_keys(
        session,
        tuple(node_admission_key(node_id) for node_id in scope),
        holder="agent-job",
    )
    abandon_superseded_idempotent_operations_in_session(session, scope, ordinal, now)
    parent_ids = tuple(
        session.scalars(
            select(StoredOperation.parent_job_id)
            .join(Job, Job.id == StoredOperation.parent_job_id)
            .where(
                StoredOperation.node_id.in_(scope),
                StoredOperation.kind.in_(_WORKLOAD_INTENT_OPERATIONS),
                or_(
                    and_(
                        StoredOperation.workload_intent_ordinal.is_not(None),
                        StoredOperation.workload_intent_ordinal < ordinal,
                    ),
                    and_(
                        StoredOperation.workload_intent_ordinal.is_(None),
                        Job.payload["workload_intent_ordinal"].as_integer().is_(None),
                    ),
                ),
                Job.state.in_(
                    job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.NEEDS_OPERATOR,
                    )
                ),
            )
            .distinct()
            .order_by(StoredOperation.parent_job_id)
        )
    )
    locked = lock_admission_rows(
        session,
        (
            AdmissionRowLock(
                "superseded-workload-parents",
                Job,
                select(Job).where(Job.id.in_(parent_ids)),
            ),
            AdmissionRowLock(
                "superseded-workload-children",
                StoredOperation,
                select(StoredOperation).where(
                    StoredOperation.parent_job_id.in_(parent_ids)
                ),
            ),
        )
        if parent_ids
        else (),
    )
    parents = {
        parent.id: parent for parent in locked.get("superseded-workload-parents", ())
    }
    adapter = AgentOperationAdapter(session)
    children_by_parent: dict[str, list[StoredOperation]] = {}
    for child in locked.get("superseded-workload-children", ()):
        children_by_parent.setdefault(child.parent_job_id, []).append(child)
    for parent_id in parent_ids:
        parent = parents.get(parent_id)
        if parent is None or parent.state not in job_states.words(
            LifecycleState.QUEUED,
            LifecycleState.RUNNING,
            LifecycleState.NEEDS_OPERATOR,
        ):
            continue
        children = tuple(children_by_parent.get(parent_id, ()))
        bound = column_field(parent, "payload", "workload_intent_ordinal")
        if bound is None:
            owner_kind = column_field(parent, "payload", "owner_kind")
            owner_id = column_field(parent, "payload", "owner_id")
            run = (
                session.get(RecipeRun, owner_id)
                if owner_kind == "run" and isinstance(owner_id, str)
                else None
            )
            # Legacy workload parents predate intent ordinals.  Retire one
            # only when its run is durably stopped and every parked child
            # is an unissued claim refusal; an uncertain issued effect must
            # remain visible and block until its normal receipt arrives.
            if run is None or run.state != RunState.STOPPED or not children:
                continue
            if any(
                child.state not in {"succeeded", "failed", "cancelled"}
                and not (
                    child.state in agent_operation_states.PARKED
                    and child.current_attempt > 0
                    and (child.status_reason or "").startswith("claim refused:")
                )
                for child in children
            ):
                continue
            for child in children:
                adapter.withdraw_retry(child)
                if child.state in agent_operation_states.PARKED:
                    adapter.settle(
                        child,
                        None,
                        parent,
                        CancelRequested(reason="superseded by newer workload intent"),
                        now,
                    )
            set_parent_state(
                parent, "cancelled", "superseded by newer workload intent", now
            )
            continue
        if (
            type(bound) is not int
            or bound >= ordinal
            or not children
            or any(
                child.kind not in _WORKLOAD_INTENT_OPERATIONS
                or child.workload_intent_ordinal != bound
                or child.node_id not in column_value(parent, "targets")
                for child in children
            )
        ):
            # The order's identity does not prove it is older than this
            # intent: it is left alone and recorded.  Its issued effects
            # are still found by ``assess_superseded_agent_effects`` (which
            # reads the operations, not the parent) and its agent is told
            # to stop by the heartbeat, so nothing waits on this parent.
            retire_as_unknown(
                "agent-job.superseded-order",
                parent.id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "the order's workload identity does not match its children",
            )
            continue
        for child in children:
            # Superseded work is never retried: withdraw any schedule.  An
            # order that never ran ends now; one that did stays until its
            # cancellation receipt arrives or the cancellation window closes.
            adapter.withdraw_retry(child)
            if child.state == "queued" and child.current_attempt == 0:
                adapter.settle(
                    child,
                    None,
                    parent,
                    CancelRequested(reason="superseded by newer workload intent"),
                    now,
                )
        if any(
            child.state in agent_operation_states.RUNNING_OR_PARKED
            for child in children
        ):
            previous = (
                json.loads(column_message(parent, "result"))
                if column_is_document(parent, "result")
                else {}
            )
            if previous.get("cancel_requested") is not True:
                parent.result = {
                    **previous,
                    "cancel_requested": True,
                    "cancel_request_id": str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"{parent.id}:{ordinal}")
                    ),
                    "cancel_actor": "controller",
                    "cancel_requested_at": _aware(now).isoformat(),
                    "reason": "superseded by newer workload intent",
                }
            elif superseded_cancellation_deadline(previous) is None:
                # A repeated cancellation path must repair a result that
                # carries the flag but no usable timestamp, not merely one
                # with the key missing.  Without a parseable instant the
                # cleanup STOP can never be authorised and the operation
                # would wait forever.
                parent.result = {
                    **previous,
                    "cancel_requested_at": _aware(now).isoformat(),
                }
            state = "running"
        elif all(
            child.state
            in {"succeeded", "failed", "cancelled", *agent_operation_states.PARKED}
            for child in children
        ):
            # A superseded parent may have one rank finish just before the
            # replacement fences the other rank.  Once no child is still
            # running or parked, the cancellation is terminal even when
            # the children have mixed terminal outcomes.  Leaving the
            # parent ``running`` here makes every newer intent wait for a
            # receipt that can no longer arrive.
            state = (
                "failed"
                if any(child.state == "failed" for child in children)
                else "cancelled"
            )
        else:
            state = parent.state
        set_parent_state(parent, state, "superseded by newer workload intent", now)


def abandon_superseded_idempotent_operations_in_session(
    session: Session, targets: Sequence[str], ordinal: int, now: datetime
) -> int:
    """Close parked idempotent transfers an older workload intent left behind.

    A cancelled or superseded order has no use for a transfer that is parked
    for an operator or waiting on a retry: nothing runs, and what it copied
    stays on the Spark for the next request. Closing it lets the order that
    superseded it settle instead of waiting for a receipt that cannot come.
    """

    scope = tuple(sorted(set(targets)))
    if not scope:
        return 0
    acquire_admission_keys(
        session,
        tuple(node_admission_key(node_id) for node_id in scope),
        holder="agent-job",
    )
    parked = tuple(
        session.scalars(
            select(StoredOperation)
            .where(
                StoredOperation.node_id.in_(scope),
                StoredOperation.kind.in_(
                    _ABANDONABLE_OPERATIONS & _WORKLOAD_INTENT_OPERATIONS
                ),
                StoredOperation.workload_intent_ordinal.is_not(None),
                StoredOperation.workload_intent_ordinal < ordinal,
                StoredOperation.current_attempt > 0,
                StoredOperation.state.in_(agent_operation_states.PARKED),
            )
            .order_by(StoredOperation.id)
            .with_for_update(of=StoredOperation)
        )
    )
    for operation in parked:
        _abandon_operation(
            operation,
            operation.parent_job_id,
            now,
            reason="superseded by a newer workload intent; the parked transfer "
            "was abandoned and its copied bytes remain on the Spark",
        )
    return len(parked)


def assess_superseded_agent_effects_in_session(
    session: Session, targets: Sequence[str], current_ordinal: int, now: datetime
) -> tuple[SupersededAgentEffect, ...]:
    """Read only: identify older issued effects still awaiting a stop receipt."""
    scope = tuple(sorted(set(targets)))
    if (
        not scope
        or len(scope) != len(targets)
        or type(current_ordinal) is not int
        or current_ordinal < 1
    ):
        raise InvalidValue(
            "workload observation scope is invalid",
            reason=InvalidRequestReason.MALFORMED,
        )
    candidates = tuple(
        session.scalars(
            select(StoredOperation)
            .where(
                StoredOperation.node_id.in_(scope),
                StoredOperation.kind.in_(_WORKLOAD_INTENT_OPERATIONS),
                StoredOperation.workload_intent_ordinal.is_not(None),
                StoredOperation.workload_intent_ordinal < current_ordinal,
                StoredOperation.current_attempt > 0,
                StoredOperation.state.in_(agent_operation_states.RUNNING_OR_PARKED),
            )
            .order_by(StoredOperation.parent_job_id, StoredOperation.id)
        )
    )
    pending = []
    for operation in candidates:
        if _exact_service_stop_receipt_covers_start(
            session, operation, current_ordinal
        ):
            continue
        if (
            operation.kind in _ABANDONABLE_OPERATIONS
            and operation.state in agent_operation_states.PARKED
        ):
            # A parked idempotent transfer runs nothing a cancellation
            # receipt could stop, and an ended job never stamped the
            # cancellation identity this assessment otherwise requires.
            continue
        parent = session.get(Job, operation.parent_job_id)
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        deadline = superseded_cancellation_deadline(
            None if parent is None else column_value(parent, "result")
        )
        if (
            parent is None
            or attempt is None
            or deadline is None
            or operation.workload_intent_ordinal
            != column_field(parent, "payload", "workload_intent_ordinal")
            or operation.node_id not in column_value(parent, "targets")
            or _target_scope(column_value(parent, "targets")) is None
        ):
            # The effect is real but its order's identity is damaged: it is
            # still an issued effect awaiting its stop receipt, observed
            # until a bounded deadline from what the operation itself
            # carries, never refused (rule 5).
            retire_as_unknown(
                "agent-job.superseded-effect",
                operation.id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "the effect's order identity is damaged; observing it",
            )
        lease_deadline = (
            _aware(attempt.lease_deadline) if attempt is not None else _aware(now)
        )
        observation_deadline = max(
            deadline if deadline is not None else _aware(now), lease_deadline
        ) + timedelta(seconds=960)
        observe_due_at = min(
            observation_deadline,
            max(
                _aware(now) + timedelta(seconds=2),
                min(
                    lease_deadline,
                    _aware(now) + timedelta(seconds=30),
                ),
            ),
        )
        pending.append(
            SupersededAgentEffect(
                parent_job_id=operation.parent_job_id,
                operation_id=operation.id,
                node_id=operation.node_id,
                kind=operation.kind,
                failure_kind=FailureKind.UNCERTAIN_EFFECT,
                observe_due_at=observe_due_at,
                observation_deadline=observation_deadline,
            )
        )
    return tuple(pending)
