"""A scheduled-retry note is never a job's outcome reason.

Sweep 2026-10-31 (job 99da47ec, order 44995325): attempt 1 of a recipe start
waited on a temporary dependency, so the order and its job carried
"... temporarily unavailable; exact recipe-start interrupted; retry scheduled
at ...". Attempt 2 then failed definitively (the workload was OOM-killed), but
the job ended carrying the stale retry note, so the run-switch reported
"retry scheduled" and the sweep recorded a deferral instead of the OOM.
"""

from __future__ import annotations

from vonk_agent_protocol import (
    AgentFailureKind,
    AgentResult,
    FailureCode,
    LifecycleState,
    canonical_message,
)
from vonk_agent_protocol import AgentOperation as WireOperation
from vonk_control.models import AgentOperation
from vonk_control.run_switch_operations.result_helpers import _child_progress_payload

from .runtime_identity_support import claim_agent
from .test_claim_note_not_outcome import _outcome, _result, _running_start

_OOM_REASON = (
    "the workload process exited: exit_code=137 oom_killed=true exit_cause=oom_killed"
)
_RETRY_AFTER_SECONDS = 5


def _failed(claim, result: dict) -> AgentResult:
    return AgentResult.model_validate_json(
        canonical_message(
            {"fence": claim.fence, "state": LifecycleState.FAILED, "result": result}
        )
    )


def _retry_after_a_temporary_failure(tmp_path):
    """Attempt 1 waits on a temporary dependency; attempt 2 is claimed.

    Returns the services, the start job, the note its parent showed while the
    retry was scheduled, and the claim of the retry.
    """

    sessions, service, jobs, clock, started, nodes, claim = _running_start(tmp_path)
    jobs.record_result(
        _failed(
            claim,
            {
                "code": FailureCode.RUNTIME_OBSERVATION_UNAVAILABLE,
                "failure_kind": AgentFailureKind.TEMPORARY_DEPENDENCY,
                "kind": "failed",
                "reason": "workload preparation is temporarily unavailable",
                "retry_after_seconds": _RETRY_AFTER_SECONDS,
            },
        )
    )
    with sessions() as session:
        order = (
            session.query(AgentOperation)
            .filter_by(parent_job_id=started.id, node_id=nodes[0])
            .one()
        )
        assert (order.current_attempt, order.state) == (1, LifecycleState.BACKOFF)
        assert order.next_action_at is not None
    # Precondition: while the retry is scheduled the job shows the wait.
    note = _outcome(sessions, started.id)
    assert note
    assert service.get(started.id).state == LifecycleState.QUEUED
    clock.advance(_RETRY_AFTER_SECONDS * 4)
    retry = claim_agent(jobs, nodes[0], "serial-0")
    assert retry is not None and retry.operation == WireOperation.RECIPE_START
    return sessions, service, jobs, started, nodes, note, retry


def test_a_definite_failure_after_a_scheduled_retry_ends_with_its_own_reason(
    tmp_path,
) -> None:
    """Catches the parent ending with the retry note instead of the failure.

    On main the recipe parent is ended by the terminal projection keeping its
    reason, so the aggregate treats the retry note as an outcome.
    """

    sessions, service, jobs, started, _nodes, note, retry = (
        _retry_after_a_temporary_failure(tmp_path)
    )
    jobs.record_result(
        _failed(
            retry,
            {
                "code": FailureCode.RECIPE_START_FAILED,
                "kind": "failed",
                "reason": _OOM_REASON,
            },
        )
    )

    job = service.get(started.id)
    assert job.state == LifecycleState.FAILED
    reason = _outcome(sessions, started.id)
    assert _OOM_REASON in reason
    assert note not in reason
    # What the run-switch phase reports (advance.py) is the fresh failure, so
    # the sweep reads its exit_cause instead of a scheduled retry.
    evidence = _child_progress_payload(job)
    detail = evidence.reason or evidence.status_reason or ""
    assert _OOM_REASON in detail
    assert note not in detail


def test_a_success_after_a_scheduled_retry_carries_no_retry_note(tmp_path) -> None:
    """Catches a started job that succeeded still saying a retry is scheduled."""

    sessions, service, jobs, started, nodes, note, retry = (
        _retry_after_a_temporary_failure(tmp_path)
    )
    jobs.record_result(_result(retry, succeeded=True))
    for node_id in (nodes[1], nodes[0], nodes[1], nodes[0]):
        if service.get(started.id).state == LifecycleState.SUCCEEDED:
            break
        following = claim_agent(jobs, node_id, f"serial-{nodes.index(node_id)}")
        if following is not None:
            jobs.record_result(_result(following, succeeded=True))
    assert service.get(started.id).state == LifecycleState.SUCCEEDED
    assert note not in _outcome(sessions, started.id)
