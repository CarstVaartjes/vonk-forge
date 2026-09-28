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
