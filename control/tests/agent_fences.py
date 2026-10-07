"""Resolve the attempt a fence names, as the Controller does for agent messages."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentClaim, AgentProgress, AgentResult
from vonk_control.models import AgentOperation, AgentOperationAttempt

Fenced = AgentClaim | AgentProgress | AgentResult | str


def _fence(value: Fenced) -> str:
    return value if isinstance(value, str) else value.fence


def fenced_attempt(
    sessions: sessionmaker[Session], value: Fenced
) -> AgentOperationAttempt:
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == _fence(value)
            )
        )
        assert attempt is not None
        session.expunge(attempt)
        return attempt


def fenced_operation(sessions: sessionmaker[Session], value: Fenced) -> AgentOperation:
    attempt = fenced_attempt(sessions, value)
    with sessions() as session:
        operation = session.get(AgentOperation, attempt.operation_id)
        assert operation is not None
        session.expunge(operation)
        return operation


def park_for_operator(
    sessions: sessionmaker[Session], jobs: object, value: Fenced, reason: str
) -> None:
    """Leave an order the way an operator wait does: parked, with no schedule.

    The Controller never parks a restart-safe order any more (an uncertain effect
    is retried), so a test that needs an operator wait writes the stored row such a
    wait leaves behind, exactly as an order written before the lifecycle core
    would be: ``waiting-for-operator``, no ``next_action_at``, the attempt's own
    waiting result, and the parent aggregated from it.
    """

    from vonk_control.agent_jobs import (
        AgentJobService,
        _document,  # pyright: ignore[reportPrivateUsage]
        _failure_result,  # pyright: ignore[reportPrivateUsage]
    )
    from vonk_control.recovery_policy import FailureKind

    assert isinstance(jobs, AgentJobService)
    with sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == _fence(value)
            )
        )
        assert attempt is not None
        operation = session.get(AgentOperation, attempt.operation_id)
        assert operation is not None
        attempt.state = "waiting-for-operator"
        attempt.result = _document(
            _failure_result(
                "operation_requires_operator",
                reason,
                uncertain=True,
                failure_kind=FailureKind.INVALID_AUTHORITY,
            )
        )
        operation.state = "waiting-for-operator"
        operation.next_action_at = None
        operation.status_reason = reason
        jobs._aggregate_parent(  # pyright: ignore[reportPrivateUsage]
            session, operation.parent_job_id
        )
