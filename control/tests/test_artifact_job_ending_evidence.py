"""A failed job's detail carries the evidence its agent sent.

Hardware 2026-10-16, spark-3542, Step1X geometry job 52e7648b: the job exited
0, its output delivery was not confirmed, and the agent reported an unknown
carrying the receipt and its cause. The Controller kept only the bare receipt,
so the job failed as ``observation-unavailable`` with no exit code and no
cause, and its operation's failure read as unreadable.
"""

from __future__ import annotations

from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    OperationFailureCode,
    RecipeJobEnding,
    RecipeJobFile,
    RecipeJobRunResult,
    canonical_message,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control import operation_api
from vonk_control.agent_jobs import AgentJobService
from vonk_control.models import AgentOperation, AgentOperationAttempt
from vonk_control.operation_contract import OperationFailureEvidence
from vonk_control.operation_item_contract import agent_receipt_for

from .runtime_identity_support import claim_agent
from .test_artifact_jobs import (
    MutableClock,
    running_artifact_service,
    submitted_artifact_job,
)
from .test_recipe_operations import NOW

_REASON = "job output delivery remains unobserved"
_CAUSE = "artifact output upload was refused: HTTP 409"


def _receipt(job) -> dict[str, object]:
    empty: tuple[RecipeJobFile, ...] = ()
    return RecipeJobRunResult.model_validate(
        {
            "job_id": job.id,
            "run_id": job.run_id,
            "exit_code": 0,
            "output_manifest": {
                **recipe_job_manifest_document(empty),
                "manifest_sha256": recipe_job_manifest_sha256(empty),
            },
            "evidence": {"elapsed_milliseconds": 131000, "peak_memory_bytes": None},
        }
    ).model_dump(mode="json")


def _ended(tmp_path, report):
    """Submit a job, let the agent report ``report(job)``, and observe it out."""

    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path)
    )
    clock = MutableClock(NOW)
    agent_jobs = AgentJobService(sessions, clock=clock)

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    recipe_operations._agent_jobs = agent_jobs
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    job = submitted_artifact_job(service, run_id, request_suffix=652)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    state, result = report(job)
    clock.advance(seconds=5)
    agent_jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {"fence": str(claim.fence), "state": state, "result": result}
            )
        )
    )
    for _ in range(5):
        clock.advance(seconds=120)
        agent_jobs.reconcile_orders()
    return sessions, service.get(job.id)


def test_a_receipted_unknown_keeps_its_exit_reason_and_cause(tmp_path) -> None:
    sessions, ended = _ended(
        tmp_path,
        lambda job: (
            "observing",
            {
                "kind": "unknown",
                "wait_reason": "job-state-uncertain",
                "reason": _REASON,
                "evidence": {"stage": "job-state", "diagnostic": _CAUSE},
                "receipt": _receipt(job),
            },
        ),
    )
    # The exit proves the process ceased: the job ends, with the agent's words.
    assert ended.state == "failed"
    assert ended.status_reason is not None
    for fact in (_REASON, _CAUSE):
        assert fact in ended.status_reason, ended.status_reason
    assert ended.result_evidence is not None
    # The stage and cause are typed fields, not text to parse.
    assert ended.result_evidence.stage == "job-state"
    assert ended.result_evidence.diagnostic == _CAUSE
    assert ended.result_evidence.exit_code == 0
    # The agent reported its ending: no lease lapsed.
    assert ended.result_evidence.failure_kind is None
    assert ended.result_evidence.elapsed_milliseconds == 131000

    # Its operation's failure reads as the receipt with that account.
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == ended.operation_id
            )
        )
        assert operation is not None and operation.state == "failed"
        # The order says the agent's words too, not observation-unavailable.
        assert operation.status_reason is not None
        assert _CAUSE in operation.status_reason, operation.status_reason
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id
            )
        )
        assert attempt is not None
        receipt, unreadable = agent_receipt_for(
            operation.kind, operation.state, attempt.result
        )
        detail = operation_api.operation_detail_response(
            operation_api._operation_item(operation, attempt)
        )
    assert not unreadable
    # The order's typed failure is the job's process receipt, not an
    # unreadable stored result.
    assert isinstance(detail.failure, OperationFailureEvidence)
    reason_code = OperationFailureCode(detail.failure.error_code)
    assert reason_code is OperationFailureCode.ARTIFACT_PROCESS_FAILED
    assert isinstance(receipt, RecipeJobRunResult)
    assert receipt.reason == _REASON
    assert receipt.ending == RecipeJobEnding(stage="job-state", diagnostic=_CAUSE)


def test_a_definite_failure_after_launch_ends_the_job_with_its_stage_and_cause(
    tmp_path,
) -> None:
    # Wrong implementation caught: the result consumer read any failure other
    # than a contract or authority refusal as unreadable and reopened the
    # failed order as an observation, so the job was observed without end.
    cause = "exit_code=0 cause=job output could not be read entries=1 [mesh.glb]"
    _, ended = _ended(
        tmp_path,
        lambda _job: (
            "failed",
            {
                "kind": "failed",
                "code": "recipe_job_run_failed",
                "reason": "job outputs stayed unreadable past the job's deadline",
                "failure_kind": "temporary-dependency",
                "evidence": {"stage": "output-storage", "diagnostic": cause},
            },
        ),
    )
    assert ended.state == "failed"
    assert ended.status_reason is not None
    for fact in ("stayed unreadable", cause):
        assert fact in ended.status_reason, ended.status_reason
    assert ended.result_evidence is not None
    assert ended.result_evidence.stage == "output-storage"
    assert ended.result_evidence.diagnostic == cause
    # The agent reported: no lease lapsed.
    evidence = ended.result_evidence
    assert evidence is None or evidence.failure_kind != "agent-lease-expired"
