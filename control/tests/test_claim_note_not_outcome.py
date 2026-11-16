"""A claim wait note is never a job's outcome reason.

Incident 2026-10-27: an idle claim poll labelled a node's own running start
"claim refused: running-lease-live" while its failure result was still
undelivered. The note outlived the wait and the parent job ended carrying it
instead of the agent's real reason.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from vonk_agent_protocol import (
    AgentFailureResult,
    AgentResult,
    FailureCode,
    LifecycleState,
    canonical_message,
)
from vonk_agent_protocol import AgentOperation as WireOperation
from vonk_control.agent_jobs import _REFUSAL_PREFIXES, AgentJobService
from vonk_control.models import AgentOperation, Job
from vonk_control.run_switch_operations.result_helpers import _child_progress_payload

from .runtime_identity_support import claim_agent
from .test_agent_jobs import COMMIT, parent
from .test_distributed_recovery_budget import _AdvancingClock
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
    start_evidence,
)

_PREFLIGHT = {
    "source_build": False,
    "minimum_free_bytes": 0,
    "fabric_connectivity": "none",
    "fabric_minimum_mbps": 0,
}
#: Claim polls the node may need to reach a fresh request behind its cleanup.
_BOUNDED_POLLS = 3
_AGENT_REASON = "the workload process exited: exit_code=1 exit_cause=python_exception"


def _running_start(tmp_path):
    """A two-rank start whose rank 0 holds a live lease on the real queue."""

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installed = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="i" * 36
    )
    clock = _AdvancingClock(NOW)
    jobs = AgentJobService(sessions, clock=clock)
    service._clock = clock
    service._agent_jobs = jobs
    jobs.set_result_consumer(service.consume_agent_result)
    plan = service.preview_run(installed.owner_id, "qwen")
    started = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="r" * 36
    )
    # Rank 1 has already reported; rank 0 is the order whose result is pending.
    sibling = claim_agent(jobs, nodes[1], "serial-1")
    assert sibling is not None and sibling.operation == WireOperation.RECIPE_START
    jobs.record_result(_result(sibling, succeeded=True))
    claim = claim_agent(jobs, nodes[0], "serial-0")
    assert claim is not None and claim.operation == WireOperation.RECIPE_START
    return sessions, service, jobs, clock, started, nodes, claim


def _result(claim, *, succeeded: bool) -> AgentResult:
    return AgentResult.model_validate_json(
        canonical_message(
            {
                "fence": claim.fence,
                "state": LifecycleState.SUCCEEDED
                if succeeded
                else LifecycleState.FAILED,
                "result": start_evidence(claim.payload)
                if succeeded
                else {
                    "code": FailureCode.RECIPE_START_FAILED,
                    "kind": "failed",
                    "reason": _AGENT_REASON,
                },
            }
        )
    )


def _outcome(sessions, job_id: str) -> str:
    with sessions() as session:
        job = session.get(Job, job_id)
        assert job is not None
        return job.status_reason or ""


@pytest.mark.parametrize("succeeded", [True, False], ids=["succeeded", "failed"])
@pytest.mark.parametrize("prefix", _REFUSAL_PREFIXES)
def test_no_refusal_note_survives_as_the_outcome(tmp_path, prefix, succeeded) -> None:
    """Catches any refusal note, of any prefix, becoming a recipe job's outcome.

    Driven by the refusal prefix tuple, so a new prefix is covered without a
    list here.
    """

    sessions, service, jobs, _clock, started, nodes, claim = _running_start(tmp_path)
    with sessions.begin() as session:
        operation = (
            session.query(AgentOperation)
            .filter_by(parent_job_id=started.id, node_id=nodes[0])
            .one()
        )
        jobs._write_refusal_note(
            session, operation=operation, job_id=started.id, reason=prefix + "x"
        )
    jobs.record_result(_result(claim, succeeded=succeeded))
    if succeeded:
        for node_id in (nodes[1], nodes[0], nodes[1], nodes[0]):
            if service.get(started.id).state == LifecycleState.SUCCEEDED:
                break
            following = claim_agent(jobs, node_id, f"serial-{nodes.index(node_id)}")
            if following is not None:
                jobs.record_result(_result(following, succeeded=True))
        assert service.get(started.id).state == LifecycleState.SUCCEEDED
        assert not _outcome(sessions, started.id).startswith(_REFUSAL_PREFIXES)
        return
    reason = _outcome(sessions, started.id)
    assert not reason.startswith(_REFUSAL_PREFIXES)
    assert _AGENT_REASON in reason


def test_a_failure_behind_a_503_ends_with_the_agents_reason(tmp_path) -> None:
    """Catches the incident: 503 on result delivery, an idle poll, then the end.

    On main the poll writes ``claim refused: running-lease-live`` on the
    running start and its parent, and the parent ends carrying that note.
    """

    sessions, service, jobs, clock, started, nodes, claim = _running_start(tmp_path)
    delivery_attempts = 0

    def deliver(message: AgentResult) -> None:
        nonlocal delivery_attempts
        delivery_attempts += 1
        if delivery_attempts == 1:
            raise ConnectionError("agent.result HTTP 503")
        jobs.record_result(message)

    message = _result(claim, succeeded=False)
    with pytest.raises(ConnectionError):
        deliver(message)
    # The agent keeps polling while its result waits for redelivery.
    clock.advance(10)
    assert claim_agent(jobs, nodes[0], "serial-0") is None
    assert _outcome(sessions, started.id) == ""
    with sessions() as session:
        order = (
            session.query(AgentOperation)
            .filter_by(parent_job_id=started.id, node_id=nodes[0])
            .one()
        )
        assert order.status_reason is None
    deliver(message)

    job = service.get(started.id)
    assert job.state == LifecycleState.FAILED
    reason = _outcome(sessions, started.id)
    assert _AGENT_REASON in reason
    assert not reason.startswith(_REFUSAL_PREFIXES)
    # What the run-switch phase reports to the operator is the real failure.
    evidence = _child_progress_payload(job)
    detail = evidence.reason or evidence.status_reason or ""
    launched = (evidence.launch_evidence or {})[nodes[0]]
    assert isinstance(launched, AgentFailureResult)
    assert launched.error_code == FailureCode.RECIPE_START_FAILED
    assert launched.reason == _AGENT_REASON
    assert _AGENT_REASON in detail
    assert not detail.startswith(_REFUSAL_PREFIXES)
    # A fresh request is still admitted on the same node.
    clock.advance(timedelta(seconds=1).total_seconds())
    fresh_parent = parent(sessions, clock)
    with sessions.begin() as session:
        fresh_row = session.get(Job, fresh_parent.id)
        assert fresh_row is not None
        fresh_row.targets = [nodes[0]]
    jobs.enqueue(
        fresh_parent.id,
        nodes[0],
        WireOperation.RUNTIME_PREFLIGHT.value,
        COMMIT,
        _PREFLIGHT,
    )
    # The failed start's own cleanup stop may be handed out first; the fresh
    # probe must still be claimed within a bounded number of polls.
    handed_out = []
    for _ in range(_BOUNDED_POLLS):
        claimed = claim_agent(jobs, nodes[0], "serial-0")
        if claimed is None:
            break
        handed_out.append(claimed.operation)
        if claimed.operation == WireOperation.RUNTIME_PREFLIGHT:
            break
    assert WireOperation.RUNTIME_PREFLIGHT in handed_out, handed_out
