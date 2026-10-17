"""A progressing owner keeps its claim; a silent one loses it on liveness.

Production: model copies over Wi-Fi ran for an hour and more, and every
Controller redeploy (about a minute without renewals) outlived the 30 s lease,
so a live, progressing copy ended with "lease ended" and started a new attempt.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentOperation as ProtocolAgentOperation
from vonk_control.agent_jobs.contracts import CLAIM_LEASE_SECONDS, PROGRESSING_LEASE
from vonk_control.models import AgentOperation, AgentOperationAttempt

from .runtime_identity_support import claim_agent
from .test_agent_jobs import COMMIT, NODE_A, parent
from .test_agent_jobs import service as _service_fixture

agent_service = _service_fixture

#: The Controller redeploy seen in production left about a minute without
#: renewals; this one is longer than that and three base leases.
RESTART = timedelta(seconds=95)
MIB = 1024 * 1024


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _attempt(sessions, fence) -> AgentOperationAttempt:
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(AgentOperationAttempt.fence == fence)
        )
        assert attempt is not None
        session.expunge(attempt)
        return attempt


def _copying(completed_bytes: int) -> dict[str, object]:
    return {"phase": "copying", "completed_bytes": completed_bytes}


def _claimed_transfer(agent_service):
    jobs, sessions, clock = agent_service
    operation = jobs.enqueue(
        parent(sessions, clock).id,
        NODE_A,
        ProtocolAgentOperation.ARTIFACT_DISTRIBUTION.value,
        COMMIT,
        {"plan_digest": COMMIT},
    )
    claim = claim_agent(jobs, NODE_A, "serial-a")
    assert claim is not None
    jobs.heartbeat(claim, _copying(0), CLAIM_LEASE_SECONDS)
    clock.advance(seconds=10)
    jobs.heartbeat(claim, _copying(40 * MIB), CLAIM_LEASE_SECONDS)
    return operation, claim


def test_a_progressing_transfer_keeps_its_claim_through_controller_restarts(
    agent_service,
) -> None:
    # Wrong implementation: every renewal earned only the 30 s lease, so a
    # restart longer than that expired the live attempt and the sweep that
    # runs on the restarted Controller issued a new one.
    jobs, sessions, clock = agent_service
    operation, claim = _claimed_transfer(agent_service)

    copied = 40 * MIB
    # Ninety minutes of copying, past an hour, with the Controller gone for a
    # restart between every two renewals.
    for _ in range(timedelta(minutes=90) // RESTART):
        clock.advance(seconds=int(RESTART.total_seconds()))
        jobs.reconcile_orders()
        copied += 380 * MIB
        directive = jobs.heartbeat(claim, _copying(copied), CLAIM_LEASE_SECONDS)
        assert directive.cancel_requested is False
        assert directive.deadline >= clock.now + PROGRESSING_LEASE

    attempt = _attempt(sessions, claim.fence)
    assert attempt.state == "running"
    assert attempt.attempt == 1
    assert attempt.progress is not None
    assert attempt.progress["completed_bytes"] == copied
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        assert stored.state == "running"
        assert stored.current_attempt == 1


def test_a_renewal_without_progress_earns_only_the_base_lease(agent_service) -> None:
    # Wrong implementation: any heartbeat earned the progressing lease, so a
    # stuck owner that still renews would hold its claim on renewals alone.
    jobs, sessions, clock = agent_service
    _operation, claim = _claimed_transfer(agent_service)
    earned = _aware(_attempt(sessions, claim.fence).lease_deadline)
    assert earned >= clock.now + PROGRESSING_LEASE

    clock.advance(seconds=10)
    same = jobs.heartbeat(claim, _copying(40 * MIB), CLAIM_LEASE_SECONDS)
    # Never shortened: the agent treats an earlier deadline as a stale answer.
    assert same.deadline == earned

    # Renewals keep arriving, the bytes do not move: once the earned lease is
    # spent, each renewal adds only the base lease again.
    renewals = (PROGRESSING_LEASE + timedelta(minutes=1)) // timedelta(seconds=20)
    for _ in range(renewals):
        clock.advance(seconds=20)
        stalled = jobs.heartbeat(claim, _copying(40 * MIB), CLAIM_LEASE_SECONDS)
    assert stalled.deadline == clock.now + timedelta(seconds=CLAIM_LEASE_SECONDS)


def test_an_owner_that_stops_renewing_loses_its_claim_on_liveness(
    agent_service,
) -> None:
    # A claim never outlives its owner: once the progressing lease passes
    # without a renewal, the sweep ends the attempt and its fence is refused.
    jobs, sessions, clock = agent_service
    _operation, claim = _claimed_transfer(agent_service)

    clock.advance(seconds=int(PROGRESSING_LEASE.total_seconds()) - 5)
    jobs.reconcile_orders()
    assert _attempt(sessions, claim.fence).state == "running"

    clock.advance(seconds=10)
    jobs.reconcile_orders()

    assert _attempt(sessions, claim.fence).state != "running"
    with pytest.raises(Exception):  # noqa: B017 -- the attempt state above is the evidence
        jobs.heartbeat(claim, _copying(80 * MIB), CLAIM_LEASE_SECONDS)
