"""Agent upgrades: recovery."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentResult,
    LifecycleState,
)

from .. import agent_operation_states, job_states
from ..agent_jobs import (
    schedule_agent_upgrade_retry,
)
from ..lifecycle import CancelRequested, Outcome, Reported
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..lifecycle.agent_upgrade import UNSUPPORTED_DISPATCH
from ..models import AgentNode, AgentOperation, AgentOperationAttempt, Job, JobAttempt

if TYPE_CHECKING:
    from .service import AgentUpgradeService

from .constants import _ACTIVE_ROLLOUT_STATES
from .helpers import _aware, _failure_detail
from .stored import _stored_package, _stored_result


class RecoveryMixin:
    def consume_agent_result(
        self,
        session: Session,
        operation: AgentOperation,
        attempt: AgentOperationAttempt,
        message: AgentResult,
    ) -> None:
        service = cast("AgentUpgradeService", self)
        if operation.kind != "agent.upgrade.v1":
            return
        parent = session.scalar(
            select(Job).where(Job.id == operation.parent_job_id).with_for_update(of=Job)
        )
        if parent is None or parent.kind != "agent-upgrade":
            return
        package = _stored_package(parent)
        if package is None:
            return
        node = session.scalar(
            select(AgentNode)
            .where(AgentNode.node_id == operation.node_id)
            .with_for_update(of=AgentNode)
        )
        if message.state == "succeeded" and (
            node is not None
            and service._contact_proves_target(node, operation, package, message)
        ):
            service._advance(session, parent)
            return
        if message.state not in {
            "succeeded",
            "failed",
            agent_operation_states.WIRE_UNKNOWN,
        }:
            return
        # A helper acknowledgement without exact fresh identity, and every
        # failure, is retried automatically behind the dpkg safety fence.  A
        # later authenticated contact that proves the target completes the
        # order first; the retry is dispatched only while the Spark still runs
        # the exact rollback source.  Meanwhile the rollout moves on.
        if node is None or node.state != "active" or node.revoked_at is not None:
            AgentOperationAdapter(session).settle(
                operation,
                attempt,
                parent,
                Reported(
                    Outcome.FAILED,
                    retryable=False,
                    reason="Spark is no longer an active enrolled node",
                ),
                service._clock(),
            )
        else:
            schedule_agent_upgrade_retry(operation, attempt, service._clock())
            detail = _failure_detail(attempt.result)
            if detail is not None:
                operation.status_reason = (f"{detail}; {operation.status_reason}")[:512]
        service._advance(session, parent)

    def heal_rollouts(self, limit: int = 8) -> bool:
        """Heal legacy rollouts no order change will reach (a periodic tick).

        A rollout that waits for an operator (nothing advertises an action for it),
        one the generic worker failed as an unsupported kind, and a ``running``
        worker dispatch whose lease lapsed are projected from their orders again.
        A dispatch that still holds a live lease is left alone.  Bounded and
        idempotent: a row another transaction changed meanwhile is left to the
        next pass.  Returns whether anything changed.
        """
        service = cast("AgentUpgradeService", self)

        now = service._clock()
        with service._sessions() as session:
            candidates = list(
                session.scalars(
                    select(Job.id)
                    .where(
                        Job.kind == "agent-upgrade",
                        or_(
                            Job.state.in_(
                                job_states.words(
                                    LifecycleState.NEEDS_OPERATOR,
                                    LifecycleState.RUNNING,
                                )
                            ),
                            and_(
                                Job.state == "failed",
                                Job.status_reason == UNSUPPORTED_DISPATCH,
                            ),
                        ),
                    )
                    .order_by(Job.updated_at, Job.id)
                    .limit(limit)
                )
            )
        healed = False
        for job_id in candidates:
            with service._sessions.begin() as session:
                parent = session.scalar(
                    select(Job).where(Job.id == job_id).with_for_update(of=Job)
                )
                if parent is None or parent.kind != "agent-upgrade":
                    continue
                if parent.state == "failed":
                    if parent.status_reason != UNSUPPORTED_DISPATCH:
                        continue
                    service._rollouts.reopen(parent, now, reason=None)
                elif (
                    parent.state not in _ACTIVE_ROLLOUT_STATES
                    or parent.state == "running"
                    and service._dispatch_is_live(session, parent, now)
                ):
                    continue
                service._advance(session, parent)
                healed = True
        if healed:
            service._operations.notify_available()
        return healed

    @staticmethod
    def _dispatch_is_live(session: Session, parent: Job, now: datetime) -> bool:
        attempt = (
            None
            if parent.current_attempt == 0
            else session.scalar(
                select(JobAttempt).where(
                    JobAttempt.job_id == parent.id,
                    JobAttempt.attempt == parent.current_attempt,
                )
            )
        )
        return (
            attempt is not None
            and attempt.state == "running"
            and _aware(attempt.lease_deadline) > _aware(now)
        )

    def advance_node(self, node_id: str) -> None:
        """Resume every rollout that includes ``node_id`` when that Spark polls."""
        service = cast("AgentUpgradeService", self)

        now = service._clock()
        with service._sessions.begin() as session:
            job_ids = [
                job_id
                for job_id, targets in session.execute(
                    select(Job.id, Job.targets).where(
                        Job.kind == "agent-upgrade",
                        Job.state.in_(_ACTIVE_ROLLOUT_STATES),
                    )
                )
                if isinstance(targets, list) and node_id in targets
            ]
            for job_id in job_ids:
                parent = session.scalar(
                    select(Job).where(Job.id == job_id).with_for_update(of=Job)
                )
                if parent is None or parent.state not in _ACTIVE_ROLLOUT_STATES:
                    continue
                service._settle_at_target(session, parent, node_id, now)
                service._retry_parked(session, parent, now)
                service._advance(session, parent)

    def _settle_at_target(
        self, session: Session, parent: Job, node_id: str, now: datetime
    ) -> None:
        """Resolve an attempted upgrade whose Spark reports the target build.

        Authenticated contact is the authority on what runs.  An install that
        was attempted and then reported a failure, or that expired, is
        converged by that identity alone; it never waits for a retry, a
        rollback receipt, or an operator.
        """
        service = cast("AgentUpgradeService", self)

        package = _stored_package(parent)
        if package is None:
            return
        node = session.get(AgentNode, node_id)
        if node is None or not service._at_target(node, package):
            return
        for operation in session.scalars(
            select(AgentOperation)
            .where(
                AgentOperation.parent_job_id == parent.id,
                AgentOperation.node_id == node_id,
                AgentOperation.state.in_(agent_operation_states.PARKED),
                AgentOperation.current_attempt >= 1,
            )
            .with_for_update(of=AgentOperation)
        ):
            AgentOperationAdapter(session).settle(
                operation,
                None,
                parent,
                Reported(
                    Outcome.DONE,
                    reason="Spark reports it already runs the requested agent build",
                ),
                now,
            )

    def _retry_parked(self, session: Session, parent: Job, now: datetime) -> None:
        """Turn a parked order into an automatic, fenced retry."""

        for operation in session.scalars(
            select(AgentOperation)
            .where(
                AgentOperation.parent_job_id == parent.id,
                AgentOperation.state.in_(agent_operation_states.PARKED),
                AgentOperation.next_action_at.is_(None),
            )
            .with_for_update(of=AgentOperation)
        ):
            node = session.get(AgentNode, operation.node_id)
            if node is None or node.state != "active" or node.revoked_at is not None:
                AgentOperationAdapter(session).settle(
                    operation,
                    None,
                    parent,
                    Reported(
                        Outcome.FAILED,
                        retryable=False,
                        reason="Spark is no longer an active enrolled node",
                    ),
                    now,
                )
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt)
                .where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
                .with_for_update(of=AgentOperationAttempt)
            )
            schedule_agent_upgrade_retry(operation, attempt, now)

    def _supersede_older(self, session: Session, job: Job, now: datetime) -> None:
        """The latest fleet upgrade request leads; older rollouts stop advancing.

        An older order already running on a Spark finishes under its own fence;
        queued and retry-parked orders are withdrawn so they cannot compete with
        the new request for the same Spark.
        """
        service = cast("AgentUpgradeService", self)

        for older in session.scalars(
            select(Job)
            .where(
                Job.kind == "agent-upgrade",
                Job.state.in_(_ACTIVE_ROLLOUT_STATES),
                Job.id != job.id,
            )
            .with_for_update(of=Job)
        ):
            older.result = (
                _stored_result(older)
                .model_copy(update={"superseded_by": job.id})
                .model_dump(mode="json", exclude_none=True)
            )
            for operation in session.scalars(
                select(AgentOperation)
                .where(
                    AgentOperation.parent_job_id == older.id,
                    AgentOperation.state.in_(agent_operation_states.QUEUED_OR_PARKED),
                )
                .with_for_update(of=AgentOperation)
            ):
                AgentOperationAdapter(session).settle(
                    operation,
                    None,
                    older,
                    CancelRequested(reason=f"superseded by agent upgrade {job.id}"),
                    now,
                )
            service._advance(session, older)
