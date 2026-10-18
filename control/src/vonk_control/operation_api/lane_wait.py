"""Queued agent work explains its next claim, even while its Spark cannot poll.

This is an observation of durable queue/attempt evidence, never a second
admission decision. Reading it cannot renew a lease or advance the queue.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import WaitReason
from vonk_agent_protocol.agent_words import ProfileReasonSeverity

from .. import agent_operation_states
from ..agent_jobs.contracts import CLAIM_LEASE_SECONDS
from ..agent_jobs.retry import agent_upgrade_in_flight
from ..agent_jobs.stored import column_field
from ..agent_operation_facts import attempt_is_live, aware
from ..models import AgentOperation, AgentOperationAttempt
from ..operation_blockers import (
    BlockingOperation,
    OperationBlocker,
    ProgressAlarmCode,
    make_blocker,
)
from ..operation_item_contract import OperationItem
from ..operation_progress import stored_progress
from ..settings import PROFILE_STALL_WINDOW_SECONDS
from ..stall_window import last_progress_at


@dataclass(frozen=True)
class QueueWait:
    reason: WaitReason
    blockers: list[OperationBlocker]
    next_check_at: datetime


def apply_queue_wait(item: OperationItem, wait: QueueWait | None) -> OperationItem:
    if wait is not None:
        item.wait_reason = wait.reason
        item.blockers = wait.blockers
        item.next_attempt_at = wait.next_check_at.isoformat()
        if item.status_reason is None:
            item.status_reason = wait.blockers[0].detail
    return item


def spark_busy_blockers(
    session: Session, operations: Sequence[AgentOperation], now: datetime
) -> dict[str, QueueWait]:
    """Explain every queued order, retaining unconfirmed effects after lease loss.

    A live attempt owns its lane; an unconfirmed one needs reconciliation on
    the next authenticated poll. With neither, the order awaits the poll itself.
    The holder's scheduled observation/retry owns the next check; otherwise it
    is due at the lease boundary, or one claim interval after enqueue. Overdue
    checks stay overdue across reads and restarts. A fixed stall window since
    the later of queue creation and blocker progress escalates without stopping
    work; heartbeat/lease renewal and reads never reset that window.
    """

    nodes = {
        operation.node_id
        for operation in operations
        if operation.state == agent_operation_states.QUEUED
    }
    if not nodes:
        return {}
    holders: dict[str, tuple[AgentOperation, AgentOperationAttempt | None]] = {}
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
            AgentOperation.state.in_(agent_operation_states.RUNNING_OR_PARKED),
            AgentOperation.current_attempt > 0,
        )
        .order_by(AgentOperation.created_at, AgentOperation.id)
    ):
        unconfirmed = running.state == agent_operation_states.OBSERVING or (
            attempt is not None and agent_operation_states.attempt_is_observing(attempt)
        )
        if (
            running.state != agent_operation_states.RUNNING
            and not unconfirmed
            and not agent_upgrade_in_flight(session, running, now)
        ):
            continue
        # Prefer the live executor over older uncertain history on this Spark.
        current = holders.get(running.node_id)
        if current is None or (
            not attempt_is_live(*current, now)
            and attempt_is_live(running, attempt, now)
        ):
            holders[running.node_id] = (running, attempt)
    waits: dict[str, QueueWait] = {}
    for operation in operations:
        if operation.state != agent_operation_states.QUEUED:
            continue
        holder = holders.get(operation.node_id)
        progressed_at = aware(operation.created_at)
        reason = WaitReason.OBSERVATION_UNAVAILABLE
        detail = "Awaiting this Spark's next authenticated claim poll"
        blocking_operation = None
        run_id = None
        next_check_at = progressed_at + timedelta(seconds=CLAIM_LEASE_SECONDS)
        if holder is not None:
            running, attempt = holder
            live = attempt_is_live(running, attempt, now)
            reason = WaitReason.SPARK_BUSY if live else WaitReason.JOB_STATE_UNCERTAIN
            detail = (
                f"waits for {running.kind} {running.id[:8]} running on this Spark"
                if live
                else f"Awaiting reconciliation of {running.kind} {running.id} "
                "on this Spark; its effect is unconfirmed"
            )
            if running.next_action_at is not None:
                next_check_at = aware(running.next_action_at)
            elif live and attempt is not None:
                next_check_at = aware(attempt.lease_deadline)
            blocking_operation = BlockingOperation(id=running.id, kind=running.kind)
            workload = column_field(running, "payload", "run_id")
            run_id = workload if isinstance(workload, str) else None
            movement = last_progress_at(stored_progress(attempt))
            if movement is not None:
                progressed_at = max(progressed_at, aware(movement))
        deadline = progressed_at + timedelta(seconds=PROFILE_STALL_WINDOW_SECONDS)
        blocker = make_blocker(
            reason,
            detail,
            severity=ProfileReasonSeverity.INFO.value,
            node_ids=[operation.node_id],
        ).model_copy(update={"operation": blocking_operation, "run_id": run_id})
        blockers = [blocker]
        if aware(now) >= deadline:
            blockers.append(
                make_blocker(
                    ProgressAlarmCode.STALLED,
                    f"No queue progress since {progressed_at.isoformat()}; "
                    "inspect the Spark's contact and the blocking operation. "
                    "The order will proceed automatically when it can claim.",
                    severity=ProfileReasonSeverity.WARNING.value,
                    node_ids=[operation.node_id],
                )
            )
        waits[operation.id] = QueueWait(
            reason=reason,
            blockers=blockers,
            next_check_at=next_check_at,
        )
    return waits
