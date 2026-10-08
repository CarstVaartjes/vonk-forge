"""Claim reconcile for the node-scoped agent queue."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentOperation,
    AgentResult,
    FailureCode,
    canonical_message,
)

from .. import agent_operation_states
from ..agent_operation_facts import aware as _aware
from ..agent_operation_facts import (
    operation_start_deadline as _operation_start_deadline,
)
from ..lifecycle import CancelRequested, LeaseLapsed, Outcome, Reported
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..models import AgentNode, AgentOperationAttempt, Job
from ..models import AgentOperation as StoredOperation
from .contracts import _WORKLOAD_INTENT_OPERATIONS
from .evidence import (
    _failure_result,
    _is_refusal_reason,
    _reconciled_dead_attempt_reason,
)
from .predicates import _claim_predicate
from .retirement import operator_resume_candidates_in_session
from .retry import superseded_cancellation_deadline
from .stored import column_field, column_is_document, column_value

if TYPE_CHECKING:
    from .service import AgentJobService


def _cancel_superseded_operation(
    self: AgentJobService,
    session: Session,
    operation: StoredOperation,
    parent: Job,
    now: datetime,
    *,
    superseded_by: int | None,
    disarmed: bool,
) -> None:
    """Drive a superseded, non-terminal order to its known terminal state.

    A ``waiting-for-operator`` operation has no live attempt, so it can
    never deliver the cancellation receipt that a pending supersession
    waits for.  The *order* outcome is already known - cancelled - even if
    the effect is unobserved, so the operation becomes terminal instead of
    blocking later work forever.  The effect is never re-issued.  A
    cancellation recorded without a parseable ``cancel_requested_at`` is a
    defect that disarmed cleanup entirely, so the reason names it instead
    of silently disabling recovery.
    """

    detail = (
        "cancel_requested_at missing or unparseable"
        if disarmed
        else "cancellation cleanup deadline elapsed"
    )
    AgentOperationAdapter(session).settle(
        operation,
        None,
        parent,
        CancelRequested(
            reason=(
                f"superseded by workload intent {superseded_by}; intent "
                f"{operation.workload_intent_ordinal} cancelled ({detail})"
            )
        ),
        now,
    )
    # Aggregate first: it recomputes the parent's operator reason from its
    # children, so the defect note has to be written afterwards to survive.
    self._aggregate_parent(session, operation.parent_job_id)
    if disarmed and _is_refusal_reason(parent.status_reason):
        parent.status_reason = (
            "cancel_requested carried no cancel_requested_at; the superseded "
            "order was reconciled to cancelled"
        )[:1024]


def _superseded_cancellation_state(
    self: AgentJobService,
    session: Session,
    old: StoredOperation,
    operation: StoredOperation,
    now: datetime,
) -> str:
    """Classify a prior order against the current order's supersession.

    ``block`` means an authorised cleanup window is still live, so the prior
    order must finish before later work runs.  ``cancelled`` means the prior
    order was already driven to its known terminal state here.  Anything
    else is not a supersession and is decided by the caller.
    """

    if not (
        operation.kind in _WORKLOAD_INTENT_OPERATIONS
        and old.kind in _WORKLOAD_INTENT_OPERATIONS
        and old.current_attempt > 0
        and old.workload_intent_ordinal is not None
        and operation.workload_intent_ordinal is not None
        and old.workload_intent_ordinal < operation.workload_intent_ordinal
    ):
        return "not-applicable"
    old_parent = session.get(Job, old.parent_job_id)
    if not (
        old_parent is not None
        and column_is_document(old_parent, "result")
        and column_field(old_parent, "result", "cancel_requested") is True
    ):
        return "not-applicable"
    cancellation_deadline = superseded_cancellation_deadline(
        column_value(old_parent, "result")
    )
    if cancellation_deadline is not None and _aware(now) < cancellation_deadline:
        return "block"
    self._cancel_superseded_operation(
        session,
        old,
        old_parent,
        now,
        superseded_by=operation.workload_intent_ordinal,
        disarmed=cancellation_deadline is None,
    )
    return "cancelled"


def _reconcile_dead_running_operation(
    self: AgentJobService,
    session: Session,
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    node: AgentNode,
    now: datetime,
    *,
    superseded_by: int | None,
) -> None:
    """Park a running order whose attempt can no longer report.

    The order's effect is unobserved, never proved ended, so it becomes a
    durable operator-visible wait instead of a permanent blocker.  The
    effect is never re-issued: only the existing exact-resume path may
    schedule a retry, and only for a restart-safe operation.  The recorded
    result and fence are retained so a late receipt is still accepted.
    """

    adapter = AgentOperationAdapter(session)
    if attempt is not None:
        adapter.expire_attempt(attempt)
    reason = _reconciled_dead_attempt_reason(operation, attempt, node, now)
    if superseded_by is not None and operation.workload_intent_ordinal is not None:
        reason = f"superseded by workload intent {superseded_by}; {reason}"
    # The core decides: a restart-safe order is retried (re-issued through the
    # exact-resume path, which reconciles the old effect first), an agent
    # upgrade is retried behind its dpkg fence, and an irreversible order
    # waits only if an operator has an action for it.
    self._settle_lapsed(session, operation, attempt, reason, now)
    self._aggregate_parent(session, operation.parent_job_id)


def _settle_lapsed(
    self: AgentJobService,
    session: Session,
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    reason: str,
    now: datetime,
) -> None:
    """Decide an attempt that can no longer report, through the core.

    Every order is a ``LeaseLapsed``: retried when restart-safe or an upgrade,
    observed and then waiting for an operator only when it is irreversible and
    an operator has an action for it (a one-shot job's action is ``stop``).  The
    lapsed attempt stays fenced, so its late results are rejected.
    """

    adapter = AgentOperationAdapter(
        session, resume_candidates=operator_resume_candidates_in_session
    )
    parent = session.get(Job, operation.parent_job_id)
    adapter.settle(operation, attempt, parent, LeaseLapsed(reason=reason), now)


def _claimable_operations(node_id: str, now: datetime):
    return (
        select(StoredOperation)
        .join(Job, Job.id == StoredOperation.parent_job_id)
        .join(AgentNode, AgentNode.node_id == StoredOperation.node_id)
        .where(
            StoredOperation.node_id == node_id,
            # The one owner of every claimability condition, shared with
            # ``_excluded_work_refusal`` so a refusal cannot restate (and
            # drift from) the decision it explains.
            _claim_predicate(now).expression,
        )
        .order_by(StoredOperation.created_at, StoredOperation.id)
        .execution_options(populate_existing=True)
        .limit(1)
    )


def _fail_spent_start_budgets(
    self: AgentJobService, session: Session, node_id: str, now: datetime
) -> int:
    """Close a start whose immutable budget is spent, instead of waiting.

    A start that binds a ``start_deadline`` may not outlive it, and nothing
    can make it succeed afterwards: every later attempt is refused by the
    agent before it executes anything, and the deadline never extends.  Such
    an order has no uncertain effect to wait on, so it fails with a named
    reason and flows through the normal failed-start path (cleanup Stop,
    run and job projection).  Parked or lease-lapsed orders therefore never
    sit behind an operator, and a cancellation no longer waits for a
    receipt that cannot come.  An attempt with a live lease reports its own
    outcome and is left alone.
    """

    failed = 0
    candidates = session.scalars(
        select(StoredOperation)
        .where(
            StoredOperation.node_id == node_id,
            StoredOperation.kind == AgentOperation.RECIPE_START.value,
            StoredOperation.state.in_(agent_operation_states.RUNNING_OR_PARKED),
            StoredOperation.current_attempt > 0,
        )
        .order_by(StoredOperation.id)
        .with_for_update(of=StoredOperation, skip_locked=True)
    )
    for operation in tuple(candidates):
        deadline = _operation_start_deadline(operation)
        if deadline is None or _aware(now) < _aware(deadline):
            continue
        attempt = session.scalar(
            select(AgentOperationAttempt)
            .where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
            .with_for_update(of=AgentOperationAttempt)
        )
        if attempt is None or (
            operation.state == "running"
            and attempt.state == "running"
            and _aware(attempt.lease_deadline) > _aware(now)
        ):
            continue
        reason = (
            f"distributed start deadline {_aware(deadline).isoformat()} "
            "elapsed before the start completed; it never ran to readiness "
            "and is not retried"
        )
        result = _failure_result(
            FailureCode.RECIPE_START_FAILED.value, reason, uncertain=False
        )
        message = AgentResult.model_validate_json(
            canonical_message(
                {"fence": attempt.fence, "state": "failed", "result": result}
            )
        )
        adapter = AgentOperationAdapter(session)
        adapter.record_report(attempt, "failed", result)
        # A definite, non-retryable failure: the budget is final.
        adapter.settle(
            operation,
            attempt,
            None,
            Reported(Outcome.FAILED, retryable=False, reason=reason),
            now,
        )
        if self._result_consumer is not None:
            self._result_consumer(session, operation, attempt, message)
        self._aggregate_parent(session, operation.parent_job_id)
        failed += 1
    return failed
