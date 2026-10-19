"""Reconcile for the node-scoped agent queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import or_, select
from vonk_agent_protocol import AgentOperation, WaitReason

from .. import agent_operation_states as aos
from ..admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    label_transaction,
    lock_admission_rows,
)
from ..agent_operation_facts import attempt_holds_order as _attempt_holds_order
from ..agent_operation_facts import aware as _aware
from ..lifecycle import Reconciler
from ..lifecycle.agent_operation import AgentOperationAdapter, lapsed_running_orders
from ..lifecycle.artifact_job import ArtifactJobAdapter
from ..lifecycle.types import State as _LifecycleState
from ..models import AgentNode, AgentOperationAttempt, Job
from ..models import AgentOperation as StoredOperation
from ..own_cleanup import CLEANUP_KINDS
from .distribution_receipts import (
    adopt_distribution_receipt,
    retained_distribution_receipt,
)
from .endings import end_unobserved_order
from .ownership import observation_ownership
from .persistence import _OrderStore
from .retirement import operator_resume_candidates_in_session

if TYPE_CHECKING:
    from .service import AgentJobService


def reconcile_orders(self: AgentJobService, limit: int = 100) -> bool:
    """One pass of the lifecycle core over the Spark orders.

    It decides what no claim and no report will.  An attempt that lapsed on a
    Spark that never polls again is retried (or ends with its cancelled owner)
    instead of staying ``running`` forever, and a wait whose cause is gone, or
    that no operator can act on, is retried or ended instead of waiting for a
    person.  Idempotent and restart-safe: a pass interrupted anywhere is simply
    repeated, and a row another transaction changed meanwhile is left to the
    next pass.  Returns whether anything changed.
    """

    gone = _sweep_gone_targets(self, limit)
    progressed = self._sweep_lapsed_attempts(limit)
    report = self._order_reconciler().reconcile(limit)
    # A one-shot job is a projection of its order: heal any that did not follow.
    healed = ArtifactJobAdapter(sessions=self._sessions, clock=self._clock).reconcile(
        limit
    )
    rollouts = (
        self._reconcile_rollouts(limit)
        if self._reconcile_rollouts is not None
        else False
    )
    return gone or progressed or report.changed > 0 or healed > 0 or rollouts


def _order_reconciler(self: AgentJobService) -> Reconciler:
    reconciler = getattr(self, "_reconciler", None)
    if reconciler is None:
        adapter = AgentOperationAdapter(
            sessions=self._sessions,
            clock=self._clock,
            resume_candidates=operator_resume_candidates_in_session,
        )
        reconciler = Reconciler(
            _OrderStore(self, adapter),
            {operation.value: adapter for operation in AgentOperation},
            clock=self._clock,
            enabled=True,
        )
        self._reconciler = reconciler
    return reconciler


def _sweep_lapsed_attempts(self: AgentJobService, limit: int) -> bool:
    """Re-observe retained transfer receipts and decide lapsed running orders.

    This is the periodic form of what the claim path did only when a Spark
    polled.  The order is decided exactly as there (``_reconcile_dead_running_
    operation``), a superseded one included: its intent is gone, so it ends
    cancelled once its lease lapses (the fence rejects a late result), even
    when its Spark never claims anything again.  Only an attempt that still
    holds a lease or an open launch budget is left alone.
    """

    now = self._clock()
    with self._sessions() as session:
        candidate_query = (
            select(
                StoredOperation.id,
                StoredOperation.node_id,
                StoredOperation.parent_job_id,
            )
            .where(
                or_(
                    lapsed_running_orders(now),
                    # Re-read an authenticated terminal transfer receipt
                    # even during backoff: its effect already happened.
                    (StoredOperation.kind == AgentOperation.ARTIFACT_DISTRIBUTION)
                    & StoredOperation.state.in_(aos.PARKED)
                    & select(AgentOperationAttempt.id)
                    .where(
                        AgentOperationAttempt.operation_id == StoredOperation.id,
                        AgentOperationAttempt.attempt
                        == StoredOperation.current_attempt,
                        aos.sql_attempt_lapsed(AgentOperationAttempt),
                        AgentOperationAttempt.result["downloaded_bytes"]
                        .as_string()
                        .is_not(None),
                    )
                    .exists(),
                    # Only a running order is decided here; a lost one
                    # observes past its deadline and is the reconciler's.
                    (StoredOperation.recovery_deadline <= now)
                    & (StoredOperation.state == aos.RUNNING),
                ),
                StoredOperation.current_attempt > 0,
            )
            .order_by(StoredOperation.id)
            .limit(limit)
        )
        cursor = self._order_sweep_cursor
        if cursor is None:
            candidates = tuple(session.execute(candidate_query))
        else:
            following = tuple(
                session.execute(candidate_query.where(StoredOperation.id > cursor))
            )
            candidates = following + tuple(
                session.execute(
                    candidate_query.where(StoredOperation.id <= cursor).limit(
                        limit - len(following)
                    )
                )
            )
    if candidates:
        # A busy or unusable receipt cannot monopolize this bounded pass.
        # The cursor is observation fairness only, never dispatch authority.
        self._order_sweep_cursor = str(candidates[-1][0])
    progressed = False
    for operation_id, node_id, parent_job_id in candidates:
        # Decide without a lock whether there is anything to do: an order
        # whose attempt is still live (an open launch budget, say) is looked
        # at every pass, and taking the node's rows for each look kept the
        # node busy for the admissions that need it.
        if not self._sweep_would_act(operation_id, node_id):
            continue
        try:
            with (
                observation_ownership(self) as owned,
                self._sessions.begin() as session,
            ):
                if not owned:
                    continue
                label_transaction(session, "order-sweep")
                scopes = self._lock_operation_scopes(
                    session, (operation_id,), node_id, nowait=True
                )
                if scopes is None or scopes[operation_id][0] != parent_job_id:
                    continue
                locked = lock_admission_rows(
                    session,
                    (
                        AdmissionRowLock(
                            "sweep-order",
                            StoredOperation,
                            select(StoredOperation).where(
                                StoredOperation.id == operation_id
                            ),
                        ),
                        AdmissionRowLock(
                            "sweep-attempt",
                            AgentOperationAttempt,
                            select(AgentOperationAttempt).where(
                                AgentOperationAttempt.operation_id == operation_id,
                                AgentOperationAttempt.attempt
                                == select(StoredOperation.current_attempt)
                                .where(StoredOperation.id == operation_id)
                                .scalar_subquery(),
                            ),
                        ),
                    ),
                )
                operation = next(iter(locked["sweep-order"]), None)
                node = session.get(AgentNode, node_id)
                if operation is None or node is None or operation.state not in aos.LIVE:
                    continue
                attempt = next(
                    (
                        row
                        for row in locked["sweep-attempt"]
                        if row.attempt == operation.current_attempt
                    ),
                    None,
                )
                now = self._clock()
                receipt = retained_distribution_receipt(operation, attempt)
                parent = session.get(Job, parent_job_id)
                if (
                    receipt is not None
                    and attempt is not None
                    and parent is not None
                    and adopt_distribution_receipt(
                        self, session, operation, attempt, parent, node, receipt, now
                    )
                ):
                    progressed = True
                    continue
                if operation.state != aos.RUNNING:
                    continue
                superseded_by = _superseding_intent(operation, node)
                if _attempt_holds_order(
                    operation, attempt, now, superseded=superseded_by is not None
                ):
                    continue
                self._reconcile_dead_running_operation(
                    session,
                    operation,
                    attempt,
                    node,
                    now,
                    superseded_by=superseded_by,
                )
                progressed = True
        except AdmissionLockBusy:
            continue  # an admission owns the node; the next pass retries
    if progressed:
        self.notify_available()
    return progressed


def _sweep_would_act(self: AgentJobService, operation_id: str, node_id: str) -> bool:
    """Unlocked pre-check for the sweep; the locked pass decides again."""

    with self._sessions() as session:
        operation = session.get(StoredOperation, operation_id)
        node = session.get(AgentNode, node_id)
        if operation is None or node is None or operation.state not in aos.LIVE:
            return False
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        if retained_distribution_receipt(operation, attempt) is not None:
            return True
        if operation.state in aos.PARKED:
            return operation.recovery_deadline is not None and _aware(
                operation.recovery_deadline
            ) <= _aware(self._clock())
        return not _attempt_holds_order(
            operation,
            attempt,
            self._clock(),
            superseded=_superseding_intent(operation, node) is not None,
        )


def _superseding_intent(operation: StoredOperation, node: AgentNode) -> int | None:
    """The node's newer workload intent when it superseded ``operation``'s;
    a cleanup order is never superseded."""

    if (
        operation.kind not in CLEANUP_KINDS
        and operation.workload_intent_ordinal is not None
        and operation.workload_intent_ordinal < node.workload_intent_ordinal
    ):
        return node.workload_intent_ordinal
    return None


def _sweep_gone_targets(self: AgentJobService, limit: int) -> bool:
    """Retire obsolete target authority even when its last lease is still live."""
    terminal = (
        _LifecycleState.SUCCEEDED,
        _LifecycleState.FAILED,
        _LifecycleState.CANCELLED,
        _LifecycleState.SUPERSEDED,
    )
    with self._sessions() as session:
        candidates = tuple(
            session.execute(
                select(
                    StoredOperation.id,
                    StoredOperation.node_id,
                    StoredOperation.parent_job_id,
                )
                .outerjoin(AgentNode, AgentNode.node_id == StoredOperation.node_id)
                .where(
                    StoredOperation.state.not_in(terminal),
                    or_(
                        AgentNode.node_id.is_(None),
                        AgentNode.state != "active",
                        AgentNode.revoked_at.is_not(None),
                    ),
                )
                .order_by(StoredOperation.created_at, StoredOperation.id)
                .limit(limit)
            )
        )
    changed = False
    for operation_id, node_id, parent_id in candidates:
        try:
            with (
                observation_ownership(self) as owned,
                self._sessions.begin() as session,
            ):
                if not owned:
                    continue
                label_transaction(session, "gone-target")
                rows = lock_admission_rows(
                    session,
                    (
                        AdmissionRowLock(
                            "gone-target",
                            AgentNode,
                            select(AgentNode).where(AgentNode.node_id == node_id),
                        ),
                        AdmissionRowLock(
                            "gone-parent", Job, select(Job).where(Job.id == parent_id)
                        ),
                        AdmissionRowLock(
                            "gone-order",
                            StoredOperation,
                            select(StoredOperation).where(
                                StoredOperation.id == operation_id
                            ),
                        ),
                    ),
                )
                nodes = rows["gone-target"]
                if nodes and nodes[0].state == "active" and nodes[0].revoked_at is None:
                    continue
                orders = rows["gone-order"]
                if not orders or orders[0].state in terminal:
                    continue
                operation = orders[0]
                attempt = AgentOperationAdapter.attempt_of(session, operation)
                parents = rows["gone-parent"]
                end_unobserved_order(
                    self,
                    session,
                    operation,
                    attempt,
                    parents[0] if parents else None,
                    self._clock(),
                    reason=WaitReason.SCOPE_CHANGED,
                    note="agent target is gone or revoked",
                )
                changed = True
        except AdmissionLockBusy:
            continue  # the next worker pass retries without holding a slot
    return changed
