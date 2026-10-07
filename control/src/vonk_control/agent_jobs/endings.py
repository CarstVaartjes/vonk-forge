"""Non-blocking endings retain uncertain effects without retaining queue ownership."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session
from vonk_agent_protocol import LifecycleState, WaitReason

from ..lifecycle import CancelRequested
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..models import AgentOperation as StoredOperation
from ..models import AgentOperationAttempt, Job
from .retirement import release_owned_reservations_in_session

if TYPE_CHECKING:
    from .service import AgentJobService


def end_unobserved_order(
    service: AgentJobService,
    session: Session,
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    parent: Job | None,
    now: datetime,
    *,
    reason: WaitReason,
    note: str,
) -> None:
    """End obsolete authority, never assert that an unobserved remote effect stopped.

    The exact historical fence and receipt remain available for reconciliation.
    The terminal queue row and lapsed attempt cannot own another launch, and a
    terminal parent releases its bookkeeping reservations. No route is changed.
    """
    adapter = AgentOperationAdapter(session)
    if attempt is not None:
        adapter.expire_attempt(attempt)
    adapter.settle(
        operation,
        attempt,
        parent,
        CancelRequested(reason=f"{reason.value}: {note}; remote effect unobserved"),
        now,
    )
    if parent is not None:
        service._aggregate_parent(session, parent.id)
        if parent.state in {
            LifecycleState.CANCELLED,
            LifecycleState.FAILED,
            LifecycleState.SUCCEEDED,
            LifecycleState.SUPERSEDED,
        }:
            release_owned_reservations_in_session(session, "job", parent.id, now)
