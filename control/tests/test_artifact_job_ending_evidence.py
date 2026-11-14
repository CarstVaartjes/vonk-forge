"""A failed job's detail carries the evidence its agent sent.

Hardware 2026-10-16, spark-3542, Step1X geometry job 52e7648b: the job exited
0, its output delivery was not confirmed, and the agent reported an unknown
carrying the receipt and its cause. The Controller kept only the bare receipt,
so the job failed as ``observation-unavailable`` with no exit code and no
cause, and its operation's failure read as unreadable.
"""

from __future__ import annotations

import hashlib
from datetime import UTC

from sqlalchemy import select
from vonk_agent_protocol import (
    AgentOperation as WireAgentOperation,
)
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
from vonk_control.agent_jobs import AgentJobService, controller_result_consumer
from vonk_control.cancelled_runtime import reconcile_cancelled_runtime
from vonk_control.lifecycle.artifact_job import ArtifactJobAdapter
from vonk_control.models import AgentOperation, AgentOperationAttempt, ArtifactJob, Job
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


def _receipt(job, outputs: tuple[RecipeJobFile, ...] = ()) -> dict[str, object]:
    return RecipeJobRunResult.model_validate(
        {
            "job_id": job.id,
            "run_id": job.run_id,
            "exit_code": 0,
            "output_manifest": {
                **recipe_job_manifest_document(outputs),
                "manifest_sha256": recipe_job_manifest_sha256(outputs),
            },
            "evidence": {"elapsed_milliseconds": 131000, "peak_memory_bytes": None},
        }
    ).model_dump(mode="json")


def _wired(tmp_path):
    """A submitted job on an agent queue wired to its result consumers as
    production wires them (``api/production.py``)."""

    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path)
    )
    clock = MutableClock(NOW)
    agent_jobs = AgentJobService(sessions, clock=clock)

    agent_jobs.set_result_consumer(
        controller_result_consumer(
            artifact_jobs=service.consume_agent_result,
            recipe_operations=recipe_operations.consume_agent_result,
        )
    )
    recipe_operations._agent_jobs = agent_jobs
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    job = submitted_artifact_job(service, run_id, request_suffix=652)
    return sessions, service, agent_jobs, clock, node_id, job


def _report(agent_jobs, claim, state: str, result: object) -> None:
    agent_jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {"fence": str(claim.fence), "state": state, "result": result}
            )
        )
    )


def _order(sessions, job) -> AgentOperation:
    with sessions() as session:
        order = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job.operation_id
            )
        )
        assert order is not None
        return order


def _ended(tmp_path, report):
    """Submit a job, let the agent report ``report(job)``, and observe it out."""

    sessions, service, agent_jobs, clock, node_id, job = _wired(tmp_path)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    state, result = report(job)
    clock.advance(seconds=5)
    _report(agent_jobs, claim, state, result)
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


def _pre_launch_wait(tmp_path, *, cancel: bool = False):
    """A job whose first attempt reported a wait before launch."""

    sessions, service, agent_jobs, clock, node_id, job = _wired(tmp_path)
    first = claim_agent(agent_jobs, node_id, "serial-0")
    assert first is not None
    if cancel:
        service.cancel(
            job.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000653",
            reason="newer workload intent",
        )
    clock.advance(seconds=5)
    _report(
        agent_jobs,
        first,
        "failed",
        {
            "kind": "failed",
            "code": "recipe_job_run_failed",
            "reason": "the Spark's helper is busy",
            "failure_kind": "temporary-dependency",
            "evidence": {
                "stage": "runtime-projection",
                "diagnostic": "installation reconciliation busy",
            },
        },
    )
    return sessions, service, agent_jobs, clock, node_id, job


def _reissued_after_a_pre_launch_wait(tmp_path):
    """A job whose first attempt reported a wait before launch, claimed again."""

    sessions, service, agent_jobs, clock, node_id, job = _pre_launch_wait(tmp_path)
    waiting = service.get(job.id)
    assert waiting.state == "backoff"
    # It never launched: nothing of it can remain on the Spark.
    evidence = waiting.result_evidence
    assert evidence is None or evidence.active_scope_may_remain is not True
    order = _order(sessions, job)
    assert order.next_action_at is not None
    clock.now = max(clock.now, order.next_action_at.replace(tzinfo=UTC))
    clock.advance(seconds=1)
    second = claim_agent(agent_jobs, node_id, "serial-0")
    assert second is not None
    assert _order(sessions, job).current_attempt == 2
    agent_jobs.reconcile_orders()
    assert service.get(job.id).state == "running"
    return service, agent_jobs, clock, node_id, job, second


def test_a_pre_launch_wait_reissues_the_job_and_its_success_ends_it_clean(
    tmp_path,
) -> None:
    # Production 2026-11-14, job f8e83bc3: the helper was busy at the job's
    # Start, the agent reported a wait before launch and the order was put back
    # for reissue, but the artifact consumer read the failure report as an
    # unreadable receipt and reopened the order as an observation. The job sat
    # observing, unclaimable, for the whole recovery budget and ended failed.
    service, agent_jobs, clock, node_id, job, second = (
        _reissued_after_a_pre_launch_wait(tmp_path)
    )
    image = b"png"
    digest = hashlib.sha256(image).hexdigest()
    service.put_output(
        job.id,
        node_id=node_id,
        name="output.png",
        media_type="image/png",
        expected_sha256=digest,
        content=image,
    )
    output = RecipeJobFile(
        name="output.png", media_type="image/png", size_bytes=len(image), sha256=digest
    )
    clock.advance(seconds=5)
    _report(agent_jobs, second, "succeeded", _receipt(job, (output,)))
    done = service.get(job.id)
    assert done.state == "succeeded"
    evidence = done.result_evidence
    assert evidence is not None and evidence.exit_code == 0
    # The first attempt's wait is no account of how the job ended.
    assert (
        evidence.recoverable,
        evidence.active_scope_may_remain,
        evidence.late_results_accepted,
        evidence.failure_kind,
        evidence.stage,
        evidence.diagnostic,
    ) == (None, None, None, None, None, None)


def test_a_reissued_job_left_in_doubt_keeps_its_scope_in_doubt(tmp_path) -> None:
    # Wrong implementation caught: the first attempt's "nothing launched"
    # outlived it, so a second attempt that ran and was lost read as gone.
    service, agent_jobs, clock, _node_id, job, second = (
        _reissued_after_a_pre_launch_wait(tmp_path)
    )
    clock.advance(seconds=5)
    _report(
        agent_jobs,
        second,
        "observing",
        {"kind": "unknown", "wait_reason": "job-state-uncertain", "reason": "lost"},
    )
    lost = service.get(job.id)
    assert lost.state == "observing"
    assert lost.result_evidence is not None
    assert lost.result_evidence.active_scope_may_remain is True


def test_an_unknown_without_receipt_is_observed_once_as_the_order_decided(
    tmp_path,
) -> None:
    # Wrong implementation caught: the consumer re-read the unknown report as
    # an unreadable receipt and settled the order a second time, spending two
    # of its bounded looks on one report and overriding its schedule.
    sessions, service, agent_jobs, clock, node_id, job = _wired(tmp_path)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    clock.advance(seconds=5)
    _report(
        agent_jobs,
        claim,
        "observing",
        {"kind": "unknown", "wait_reason": "job-state-uncertain", "reason": "lost"},
    )
    order = _order(sessions, job)
    assert (order.state, order.observe_count) == ("observing", 1)
    assert service.get(job.id).state == "observing"


def test_an_observation_never_reopens_an_order_put_back_for_reissue(
    tmp_path,
) -> None:
    # Wrong implementation caught: any later unreadable signal for the job (an
    # input the Spark could not fetch, a malformed report) reopened the
    # scheduled reissue as an observation and parked the job unclaimable.
    sessions, service, agent_jobs, clock, node_id, job = _pre_launch_wait(tmp_path)
    with sessions.begin() as session:
        artifact_job = session.get(ArtifactJob, job.id)
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job.operation_id
            )
        )
        parent = session.get(Job, job.operation_id)
        assert artifact_job and operation and parent
        service._observe_result(
            ArtifactJobAdapter(session, clock=clock),
            operation,
            parent,
            artifact_job,
            clock(),
        )
    order = _order(sessions, job)
    assert (order.state, order.observe_count) == ("backoff", 0)
    assert order.next_action_at is not None
    clock.now = max(clock.now, order.next_action_at.replace(tzinfo=UTC))
    clock.advance(seconds=1)
    assert claim_agent(agent_jobs, node_id, "serial-0") is not None
    assert _order(sessions, job).current_attempt == 2


def test_a_pre_launch_wait_after_a_cancel_ends_the_job_cancelled(tmp_path) -> None:
    # The report proves nothing launched, so the cancel ends the order and the
    # job cancelled at once, with nothing left to stop: no Stop is sent for it
    # and the next job on the run is admitted without waiting for one.
    sessions, service, agent_jobs, _clock, node_id, job = _pre_launch_wait(
        tmp_path, cancel=True
    )
    assert _order(sessions, job).state == "cancelled"
    cancelled = service.get(job.id)
    assert cancelled.state == "cancelled"
    evidence = cancelled.result_evidence
    assert evidence is not None
    assert evidence.active_scope_may_remain is not True
    assert evidence.failure_kind is None
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    reconcile_cancelled_runtime(service._recipe_operations)
    with sessions() as session:
        stops = session.scalars(
            select(Job.id).where(Job.kind == WireAgentOperation.RECIPE_STOP.value)
        ).all()
    assert stops == []
    fresh = submitted_artifact_job(service, job.run_id, request_suffix=660)
    assert fresh.state == "queued"
    assert claim_agent(agent_jobs, node_id, "serial-0") is not None
