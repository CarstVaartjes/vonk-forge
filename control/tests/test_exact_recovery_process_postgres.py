"""Exact recovery survives sustained interruption and a lost committed response."""

from __future__ import annotations

import multiprocessing
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentResult
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.models import AgentOperation, AgentOperationAttempt, Job

from .runtime_identity_support import claim_agent
from .test_agent_jobs_postgres import (
    COMMIT,
    NODE_A,
    NODE_B,
    STOP_PAYLOAD,
    STOP_RESULT,
    parent,
)
from .test_agent_jobs_postgres import (
    service as postgres_agent_service,  # noqa: F401 - shared PostgreSQL fixture.
)
from .test_latest_workload_intent_recovery import _result

_CAPABILITIES = [
    "agent.runtime.rust.v1",
    "recipe.stop",
    "agent.lifecycle.resume.exact.v1",
]


def _commit_result_then_die(database_url: str, now: str, result: str) -> None:
    """Die after the actual result transaction, before returning its response."""
    engine = create_engine(database_url)
    jobs = AgentJobService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: datetime.fromisoformat(now),
    )
    jobs.record_result(AgentResult.model_validate_json(result))
    os._exit(23)


def _lose_committed_response(
    database_url: str, now: datetime, result: AgentResult
) -> None:
    worker = multiprocessing.get_context("spawn").Process(
        target=_commit_result_then_die,
        args=(database_url, now.isoformat(), result.model_dump_json()),
    )
    worker.start()
    try:
        # The dependency is this isolated result worker. Its committed receipt
        # permits recovery; a stuck child fails the test instead of hanging CI.
        worker.join(timeout=15)
        assert worker.exitcode == 23
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
        worker.close()


def test_postgres_exact_intent_recovers_past_budget_after_process_death(
    postgres_agent_service,  # noqa: F811 - pytest resolves the imported fixture.
    postgres_engine,
) -> None:
    """A lifetime retry cap or process-local schedule strands accepted intent.

    Exercise real PostgreSQL claims/results and actual process death, without
    repairing rows. The agent/runtime boundary supplies typed restart and stop
    receipts; this is Controller recovery evidence, not a physical stop test.
    """
    sessions, clock = postgres_agent_service
    jobs = AgentJobService(sessions, clock=clock)
    request = parent(sessions, clock)
    operation = jobs.enqueue(request.id, NODE_A, "recipe.stop", COMMIT, STOP_PAYLOAD)
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    first = None
    last = None

    for failure_number in range(1, 7):
        claim = claim_agent(
            jobs,
            NODE_A,
            "serial-a",
            30,
            protocol_version=3,
            capabilities=_CAPABILITIES,
        )
        assert claim is not None
        assert claim.operation_id == operation.id
        assert claim.attempt == failure_number
        first = claim if first is None else first
        last = claim
        interrupted = _result(
            claim,
            state="waiting-for-operator",
            evidence={
                "error_code": "agent_restart_interrupted",
                "failure_kind": "uncertain-effect",
                "uncertain": True,
                "reason": "agent restarted before its exact stop was observed",
            },
        )
        if failure_number == 6:
            _lose_committed_response(database_url, clock.now, interrupted)
        else:
            jobs.record_result(interrupted)
        with sessions() as session:
            stored = session.get(AgentOperation, operation.id)
            assert stored is not None
            assert stored.retry_due_at is not None, (
                "current exact intent must retain an automatic retry after "
                f"interruption {failure_number}"
            )
            due = stored.retry_due_at.astimezone(UTC)
            assert due > clock.now
            assert stored.status_reason is not None
            assert due.isoformat() in stored.status_reason
        if failure_number < 6:
            clock.now = due

    # A fresh worker uses the committed cooldown, and a different Spark keeps
    # progressing while the interrupted request waits for that deadline.
    jobs = AgentJobService(sessions, clock=clock)
    assert claim_agent(jobs, NODE_A, "serial-a", 30, capabilities=_CAPABILITIES) is None
    unrelated_request = parent(sessions, clock)
    unrelated = jobs.enqueue(
        unrelated_request.id, NODE_B, "recipe.stop", COMMIT, STOP_PAYLOAD
    )
    other_claim = claim_agent(jobs, NODE_B, "serial-b", 30, capabilities=_CAPABILITIES)
    assert other_claim is not None and other_claim.operation_id == unrelated.id
    jobs.succeed(other_claim, STOP_RESULT)
    with sessions() as session:
        completed = session.get(Job, unrelated_request.id)
        assert completed is not None and completed.state == "succeeded"

    clock.now = due
    recovered = claim_agent(jobs, NODE_A, "serial-a", 30, capabilities=_CAPABILITIES)
    assert recovered is not None and first is not None and last is not None
    assert recovered.operation_id == first.operation_id
    assert recovered.payload == first.payload
    assert recovered.payload_digest == first.payload_digest
    assert recovered.attempt == last.attempt + 1
    assert recovered.fence != last.fence
    with pytest.raises(StaleAgentAttempt):
        jobs.succeed(last, STOP_RESULT)

    completed_result = _result(recovered, state="succeeded", evidence={**STOP_RESULT})
    _lose_committed_response(database_url, clock.now, completed_result)
    jobs = AgentJobService(sessions, clock=clock)
    with pytest.raises(StaleAgentAttempt):
        jobs.record_result(completed_result)
    assert claim_agent(jobs, NODE_A, "serial-a", 30, capabilities=_CAPABILITIES) is None
    with sessions() as session:
        completed_request = session.get(Job, request.id)
        assert completed_request is not None
        assert completed_request.state == "succeeded"
        assert completed_request.request_id == request.request_id
        assert completed_request.payload == request.payload
        completed_operation = session.get(AgentOperation, operation.id)
        assert completed_operation is not None
        assert completed_operation.payload_digest == operation.payload_digest
        assert (
            completed_operation.workload_intent_ordinal
            == operation.workload_intent_ordinal
        )
        attempts = tuple(
            session.scalars(
                select(AgentOperationAttempt)
                .where(AgentOperationAttempt.operation_id == operation.id)
                .order_by(AgentOperationAttempt.attempt)
            )
        )
        assert len(attempts) == recovered.attempt
        assert attempts[-1].state == "succeeded"
