"""A job whose process ran and exited nonzero ends failed with its evidence."""

from __future__ import annotations

import pytest
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    FailureCode,
    OutcomeFailed,
    OutcomeKind,
    RecipeJobFile,
    RecipeJobRunResult,
    WaitReason,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control import artifact_job_states as ajs
from vonk_control.agent_jobs import AgentJobService
from vonk_control.failure_evidence import FailureEvidenceService

from .runtime_identity_support import claim_agent
from .test_artifact_jobs import (
    MutableClock,
    running_artifact_service,
    submitted_artifact_job,
)
from .test_recipe_operations import NOW

STDERR = "stub job failed on purpose\n"


def _exit_report(claim, job, exit_code: int) -> AgentResult:
    """The report the agent sends for a measured nonzero process exit."""

    empty: tuple[RecipeJobFile, ...] = ()
    tail = {"truncated": False, "dropped_bytes": 0, "dropped_lines": 0}
    receipt = RecipeJobRunResult.model_validate(
        {
            "job_id": job.id,
            "run_id": job.run_id,
            "exit_code": exit_code,
            "output_manifest": {
                **recipe_job_manifest_document(empty),
                "manifest_sha256": recipe_job_manifest_sha256(empty),
            },
            "evidence": {"elapsed_milliseconds": 382, "peak_memory_bytes": None},
            "reason": "job adapter exited unsuccessfully",
            "diagnostics": {
                "schema_version": 1,
                "collected_at": NOW.isoformat(),
                "phase": "recipe.job.run.v1",
                "category": "runtime",
                "stdout": {"text": "", **tail},
                "stderr": {"text": STDERR, **tail},
                "versions": [],
                "sandbox": [],
                "storage": [],
                "preflight": [],
                "collector_errors": [],
            },
        }
    )
    return AgentResult(
        fence=claim.fence,
        state=AgentResultState.FAILED,
        result=OutcomeFailed(
            kind=OutcomeKind.FAILED,
            code=FailureCode.RECIPE_JOB_RUN_FAILED,
            reason="job adapter exited unsuccessfully",
            receipt=receipt,
        ),
    )


def test_nonzero_exit_ends_failed_with_its_evidence_and_no_scope_in_doubt(
    tmp_path,
) -> None:
    """A measured exit is a known end: never a lapsed lease or a scope in doubt.

    Wrong implementation caught: the result consumer ended the job without the
    effect its receipt proves, so the job read as ``agent-lease-expired`` with
    ``active_scope_may_remain`` although the agent reported the process exit.
    """

    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path)
    )
    agent_jobs = AgentJobService(sessions, clock=MutableClock(NOW))

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    recipe_operations._agent_jobs = agent_jobs
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    submitted = submitted_artifact_job(service, run_id, request_suffix=170)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None

    agent_jobs.record_result(_exit_report(claim, submitted, 3))

    ended = service.get(submitted.id)
    assert ended.state == "failed"
    assert ended.status_reason == "job adapter exited unsuccessfully"
    assert ended.supported_actions == ()
    evidence = ended.result_evidence
    assert evidence is not None
    assert evidence.elapsed_milliseconds == 382
    assert evidence.failure_kind is None
    assert evidence.active_scope_may_remain is not True
    assert submitted.operation_id is not None
    bundle = FailureEvidenceService(sessions, clock=lambda: NOW).read(
        submitted.operation_id, 1
    )
    assert bundle.diagnostics is not None
    assert STDERR.strip() in bundle.diagnostics.stderr.text
    # The ended job holds nothing: the next job on the same run is admitted
    # and claimed at once.
    fresh = submitted_artifact_job(service, run_id, request_suffix=172)
    assert fresh.state == "queued"
    assert claim_agent(agent_jobs, node_id, "serial-0") is not None


def _late_success(claim, job) -> AgentResult:
    """The report of a measured exit 0: the agent gives no reason of its own."""

    empty: tuple[RecipeJobFile, ...] = ()
    receipt = RecipeJobRunResult.model_validate(
        {
            "job_id": job.id,
            "run_id": job.run_id,
            "exit_code": 0,
            "output_manifest": {
                **recipe_job_manifest_document(empty),
                "manifest_sha256": recipe_job_manifest_sha256(empty),
            },
            "evidence": {"elapsed_milliseconds": 382, "peak_memory_bytes": None},
        }
    )
    return AgentResult(
        fence=claim.fence, state=AgentResultState.SUCCEEDED, result=receipt
    )


@pytest.mark.parametrize("exit_code", [0, 3])
def test_a_late_exit_receipt_ends_a_lost_job_without_rerunning_it(
    tmp_path, exit_code: int
) -> None:
    """A job whose lease lapsed is lost, never failed, until its exit is receipted.

    Wrong implementations caught: the spent observation budget or the job's
    own timeout failed a job that may still run; the late exit receipt was
    never read (a constant ``unknown`` observation); the receipted exit still
    read as ``agent-lease-expired`` with its scope in doubt.
    """

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
    submitted = submitted_artifact_job(service, run_id, request_suffix=180)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None

    # Its agent goes silent for hours: past the budget, the recovery deadline
    # and the job's own timeout it is observed, never failed and never re-run.
    for _ in range(30):
        clock.advance(seconds=600)
        agent_jobs.reconcile_orders()
        assert claim_agent(agent_jobs, node_id, "serial-0") is None
    lost = service.get(submitted.id)
    assert lost.state == "observing"
    assert lost.supported_actions == ("stop",)

    # The agent comes back and delivers the exit it measured.
    late = (
        _late_success(claim, submitted)
        if exit_code == 0
        else _exit_report(claim, submitted, exit_code)
    )
    assert agent_jobs.record_late_result(late)
    clock.advance(seconds=120)  # the lost order's next look
    agent_jobs.reconcile_orders()

    ended = service.get(submitted.id)
    assert ended.state in ajs.ENDED and ended.state != ajs.CANCELLED
    # The agent's own exit reason, else why its result was not accepted.
    assert ended.status_reason not in {None, WaitReason.RECEIPT_MISSING.value}
    if exit_code:
        assert ended.status_reason == "job adapter exited unsuccessfully"
    assert ended.supported_actions == ()
    evidence = ended.result_evidence
    assert evidence is not None
    assert evidence.failure_kind is None
    assert evidence.active_scope_may_remain is not True
    assert claim_agent(agent_jobs, node_id, "serial-0") is None  # never re-run
    fresh = submitted_artifact_job(service, run_id, request_suffix=182)
    assert fresh.state == "queued"
    assert claim_agent(agent_jobs, node_id, "serial-0") is not None
