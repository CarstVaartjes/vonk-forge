"""What a queued agent operation waits for while its Spark is busy.

A Spark's agent claims one operation and executes it to completion before it
polls again, so while it executes nothing on the Controller evaluates the
operations queued behind it: their wait would otherwise be silent.  This read
derives the wait from the durable state on every observation, so it is never
stored, never stale and clears by itself when the running operation ends.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from .. import agent_operation_states
from ..agent_operation_facts import attempt_is_live
from ..models import AgentOperation, AgentOperationAttempt
from ..operation_blockers import (
    BlockingOperation,
    OperationBlocker,
    QueueWaitCode,
    make_blocker,
)


def spark_busy_blockers(
    session: Session, operations: Sequence[AgentOperation], now: datetime
) -> dict[str, list[OperationBlocker]]:
    """The wait of each queued operation whose Spark holds a live attempt.

    The holder is the oldest other operation on the same Spark whose current
    attempt is still live (the boundary that keeps every other owner out).  An
    attempt whose lease lapsed holds nothing: the next poll reconciles it.
    """

    nodes = {
        operation.node_id
        for operation in operations
        if operation.state == agent_operation_states.QUEUED
    }
    if not nodes:
        return {}
    holders: dict[str, AgentOperation] = {}
    for running, attempt in session.execute(
        select(AgentOperation, AgentOperationAttempt)
        .outerjoin(
            AgentOperationAttempt,
            and_(
                AgentOperationAttempt.operation_id == AgentOperation.id,
                AgentOperationAttempt.attempt == AgentOperation.current_attempt,
            ),
        )
        .where(
            AgentOperation.node_id.in_(nodes),
            AgentOperation.state == agent_operation_states.RUNNING,
        )
        .order_by(AgentOperation.created_at, AgentOperation.id)
    ):
        if running.node_id not in holders and attempt_is_live(running, attempt, now):
            holders[running.node_id] = running
    blockers: dict[str, list[OperationBlocker]] = {}
    for operation in operations:
        holder = holders.get(operation.node_id)
        if operation.state != agent_operation_states.QUEUED or holder is None:
            continue
        blocker = make_blocker(
            QueueWaitCode.SPARK_BUSY,
            f"waits for {holder.kind} {holder.id[:8]} running on this Spark",
            severity="info",
            node_ids=[operation.node_id],
        )
        blockers[operation.id] = [
            blocker.model_copy(
                update={"operation": BlockingOperation(id=holder.id, kind=holder.kind)}
            )
        ]
    return blockers
