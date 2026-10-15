"""A job run its agent reported as unconfirmed ends with the agent's evidence."""

from __future__ import annotations

from vonk_agent_protocol import AgentResult, canonical_message
from vonk_control import artifact_job_states as ajs
from vonk_control.agent_jobs import AgentJobService

from .runtime_identity_support import claim_agent
from .test_artifact_jobs import (
    NOW,
    MutableClock,
    running_artifact_service,
    submitted_artifact_job,
)

_REASON = "job custody or delivery remains unobserved"
_CAUSE = "order authority ended during the input delivery"


def test_agent_reported_unconfirmed_job_ends_failed_with_its_reason(
    tmp_path,
) -> None:
    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path)
    )
    clock = MutableClock(NOW)
    agent_jobs = AgentJobService(sessions, clock=clock)
    recipe_operations._agent_jobs = agent_jobs
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    submitted = submitted_artifact_job(service, run_id, request_suffix=321)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None

    # The agent's lease is live; it reports at once that it could not confirm
    # the job's effect (hardware 2026-10-15, step1x geometry on spark-2297).
    clock.advance(seconds=5)
    agent_jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": str(claim.fence),
                    "state": "observing",
                    "result": {
                        "kind": "unknown",
                        "wait_reason": "job-state-uncertain",
                        "reason": _REASON,
                        "evidence": {"stage": "job-state", "diagnostic": _CAUSE},
                    },
                }
            )
        )
    )
    observed = service.get(submitted.id)
    assert observed.state == ajs.OBSERVING
    assert observed.status_reason is not None
    assert _REASON in observed.status_reason and _CAUSE in observed.status_reason

    for _ in range(40):
        clock.advance(seconds=15)
        agent_jobs.reconcile_orders()
        if service.get(submitted.id).state == ajs.FAILED:
            break
    ended = service.get(submitted.id)

    # In doubt, never re-run, and its residue still a Stop target; but the
    # lease never lapsed and the agent's own reason and cause are kept.
    assert ended.state == ajs.FAILED
    assert ended.status_reason is not None
    assert _REASON in ended.status_reason and _CAUSE in ended.status_reason
    assert ended.result_evidence is not None
    assert ended.result_evidence.failure_kind is None
    assert ended.result_evidence.active_scope_may_remain is True
    assert ended.result_evidence.late_results_accepted is False
