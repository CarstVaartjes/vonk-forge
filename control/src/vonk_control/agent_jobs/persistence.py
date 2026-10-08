"""Persistence for the node-scoped agent queue."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from ..admission_locking import AdmissionLockBusy, label_transaction
from ..lifecycle import Lifecycle
from ..lifecycle.agent_operation import AgentOperationAdapter, parked_orders, same_order
from ..models import AgentOperation as StoredOperation
from ..models import AgentOperationAttempt, Job
from .contracts import _LOGGER
from .retirement import operator_resume_candidates_in_session

if TYPE_CHECKING:
    from .service import AgentJobService


class _OrderStore:
    """The reconciler's view of the waiting Spark orders.

    ``due`` reads committed state; ``save`` re-reads the order under the same locks
    the claim path takes (target nodes, then parent jobs, then the order and its
    attempt) and applies a decision only if the order is still what the decision
    was made from.  The write is the adapter's ``apply``: the one projection of a
    core decision onto the stored order.
    """

    def __init__(
        self, service: AgentJobService, adapter: AgentOperationAdapter
    ) -> None:
        self._service = service
        self._adapter = adapter

    def due(self, now: datetime, limit: int) -> Sequence[Lifecycle]:
        with self._service._sessions() as session:
            orders = tuple(
                session.scalars(
                    select(StoredOperation)
                    .where(parked_orders(now))
                    .order_by(StoredOperation.created_at, StoredOperation.id)
                    .limit(limit)
                )
            )
            return tuple(
                self._adapter.lifecycle(
                    order,
                    AgentOperationAdapter.attempt_of(session, order),
                    session.get(Job, order.parent_job_id),
                    now,
                )
                for order in orders
            )

    def save(self, before: Lifecycle, after: Lifecycle) -> bool:
        try:
            return self._save(before, after)
        except AdmissionLockBusy:
            return False  # an admission owns the node; the next pass reads afresh

    def _save(self, before: Lifecycle, after: Lifecycle) -> bool:
        service = self._service
        with service._claim_lock, service._sessions.begin() as session:
            label_transaction(session, "order-reconcile")
            hint = session.execute(
                select(StoredOperation.node_id, StoredOperation.parent_job_id).where(
                    StoredOperation.id == before.id
                )
            ).one_or_none()
            if hint is None:
                return False
            node_id, parent_job_id = hint
            scopes = service._lock_operation_scopes(
                session, (before.id,), node_id, nowait=True
            )
            if scopes is None or scopes[before.id][0] != parent_job_id:
                return False
            order = session.scalar(
                select(StoredOperation)
                .where(StoredOperation.id == before.id)
                .with_for_update(of=StoredOperation)
                .execution_options(populate_existing=True)
            )
            if order is None:
                return False
            attempt = session.scalar(
                select(AgentOperationAttempt)
                .where(
                    AgentOperationAttempt.operation_id == order.id,
                    AgentOperationAttempt.attempt == order.current_attempt,
                )
                .with_for_update(of=AgentOperationAttempt)
            )
            parent = session.get(Job, parent_job_id)
            now = service._clock()
            bound = AgentOperationAdapter(
                session,
                clock=service._clock,
                resume_candidates=operator_resume_candidates_in_session,
            )
            current = bound.lifecycle(order, attempt, parent, now)
            if not same_order(current, before):
                return False  # a report, claim or cancel got there first
            if bound.apply(order, attempt, before, after, now):
                service._aggregate_parent(session, parent_job_id)
        return True

    def record_residue(self, row: Lifecycle, reason: str) -> None:
        _LOGGER.info("order %s %s: %s", row.id, row.kind, reason)
