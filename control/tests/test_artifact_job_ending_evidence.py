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

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentOperation as WireAgentOperation,
)
from vonk_agent_protocol import (
    AgentResult,
    ContainerRuntimeAction,
    InstallationNodeState,
    InstallationState,
    InvalidRequestError,
    InvalidRequestReason,
    OperationFailureCode,
    RecipeJobEnding,
    RecipeJobFile,
    RecipeJobRunRequest,
    RecipeJobRunResult,
    RunAdmissionCode,
    RunState,
    canonical_message,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control import operation_api
from vonk_control.agent_jobs import AgentJobService, controller_result_consumer
from vonk_control.agent_jobs.contracts import CLAIM_LEASE_SECONDS
from vonk_control.cancelled_runtime import reconcile_cancelled_runtime
from vonk_control.host_runtime_plan_authority import derive_runtime_plan_binding
from vonk_control.jobs import states as job_states
from vonk_control.lifecycle.artifact_job import ArtifactJobAdapter
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    ArtifactJob,
    InstallationNode,
    Job,
    RecipeInstallation,
    RecipeRun,
)
from vonk_control.operations.contract import OperationFailureEvidence
from vonk_control.operations.item_contract import agent_receipt_for
from vonk_control.owner_liveness import OwnerKind, OwnerRef, owner
from vonk_control.recipe_operations import (
    RecipeArtifactJobCancellationPending,
    RecipeRetryLater,
)
from vonk_control.workload_intent import allocate_workload_intent

from .artifact_jobs_support import (
    MutableClock,
    finalized_artifact_job,
    running_artifact_service,
    submitted_artifact_job,
)
from .runtime_identity_support import claim_agent
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


def _wired(tmp_path, engine=None):
    """A submitted job on an agent queue wired to its result consumers as
    production wires them (``api/production.py``)."""

    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path, engine=engine)
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


_IMAGE_MISSING = {
    "kind": "failed",
    "code": "recipe_job_run_failed",
    "reason": "job ended without an observed exit",
    "failure_kind": "uncertain-effect",
    "evidence": {
        "stage": "runtime-adapter",
        "helper_error_code": "runtime_image_missing",
    },
}


def _image_lost(tmp_path, engine=None):
    """A held run whose job found its runtime image gone from the Spark."""

    sessions, service, agent_jobs, clock, node_id, job = _wired(tmp_path, engine)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    clock.advance(seconds=5)
    _report(agent_jobs, claim, "failed", _IMAGE_MISSING)
    return sessions, service, agent_jobs, clock, node_id, job


def test_a_job_that_finds_its_image_gone_ends_and_reinstalls_its_rank(
    tmp_path,
) -> None:
    # Wrong implementation caught: the helper's code reached the Controller
    # only as text, so the rank whose image the Spark lost stayed installed
    # and the run's next activation launched the missing image again.
    sessions, service, _agent_jobs, _clock, node_id, job = _image_lost(tmp_path)
    assert service.get(job.id).state == "failed"
    with sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == _order(sessions, job).id
            )
        )
        assert attempt is not None and attempt.result is not None
        reason_code = attempt.result.get("helper_error_code")
        assert reason_code == "runtime_image_missing"
        run = session.get(RecipeRun, job.run_id)
        assert run is not None
        rank = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == run.installation_id,
                InstallationNode.node_id == node_id,
            )
        )
        assert rank is not None
        assert rank.state == InstallationNodeState.PLANNED


def test_a_demoted_installation_lets_go_of_its_claim_but_the_held_run_keeps_its_own(
    tmp_path,
) -> None:
    sessions, _service, agent_jobs, clock, node_id, job = _wired(tmp_path)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    clock.advance(seconds=5)
    _report(
        agent_jobs,
        claim,
        "failed",
        {
            "kind": "failed",
            "code": "recipe_job_run_failed",
            "reason": "job ended without an observed exit",
            "failure_kind": "uncertain-effect",
            "evidence": {
                "stage": "runtime-adapter",
                "helper_error_code": "runtime_image_missing",
            },
        },
    )
    clock.advance(seconds=600)
    with sessions() as session:
        run = session.get(RecipeRun, job.run_id)
        assert run is not None and run.state == RunState.RUNNING
        installation = session.get(RecipeInstallation, run.installation_id)
        assert installation is not None
        assert installation.state == InstallationState.PARTIAL
        assert owner(session, OwnerRef(OwnerKind.RUN, run.id), clock()).live
        assert not owner(
            session, OwnerRef(OwnerKind.INSTALLATION, installation.id), clock()
        ).live


def _next_claim(agent_jobs, clock, node_id):
    """The order the Spark gets next; one held at dispatch steps aside until
    its next check, so the poll after it reaches the order behind it."""

    for _ in range(8):
        for _ in range(2):
            claim = claim_agent(agent_jobs, node_id, "serial-0")
            if claim is not None:
                return claim
        clock.advance(seconds=CLAIM_LEASE_SECONDS)
    return None


def _intent(sessions, node_id) -> int:
    with sessions() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        return node.workload_intent_ordinal


def _launch_grant(sessions, job):
    with sessions() as session:
        order = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job.operation_id
            )
        )
        parent = session.get(Job, job.operation_id)
        assert order is not None and parent is not None
        return derive_runtime_plan_binding(
            session,
            parent=parent,
            operation=order,
            node_id=order.node_id,
            action=ContainerRuntimeAction.START,
            stop_plan_sha256=None,
        )


def _live_installs(sessions, run_id) -> list[str]:
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        return list(
            session.scalars(
                select(Job.id).where(
                    Job.kind == WireAgentOperation.RECIPE_INSTALL.value,
                    Job.owner_id == run.installation_id,
                    Job.state.in_(job_states.LIVE),
                )
            )
        )


@pytest.mark.parametrize(
    "database", ("sqlite", pytest.param("postgres", marks=pytest.mark.postgres))
)
def test_a_submission_on_a_run_that_lost_its_image_installs_it_again_then_runs(
    tmp_path, request, database
) -> None:
    # Wrong implementation caught: the held run kept taking jobs while its
    # installation was partial; nothing installed the lost image again, so
    # every submission was launched into a launch grant refused as not ready.
    engine = (
        request.getfixturevalue("postgres_engine") if database == "postgres" else None
    )
    sessions, service, agent_jobs, clock, node_id, lost = _image_lost(tmp_path, engine)
    intent = _intent(sessions, node_id)
    waiting = submitted_artifact_job(service, lost.run_id, request_suffix=670)

    # It waits, visibly, for one install of what was lost, under the Spark's
    # current workload intent (a submission takes no new one).
    assert service.get(waiting.id).status_reason == (
        RunAdmissionCode.NOT_INSTALLED.value
    )
    assert _intent(sessions, node_id) == intent
    install = _next_claim(agent_jobs, clock, node_id)
    assert install is not None
    assert install.operation == WireAgentOperation.RECIPE_INSTALL
    # An install that must try again keeps the job waiting, never launched.
    clock.advance(seconds=5)
    _report(
        agent_jobs,
        install,
        "failed",
        {
            "kind": "failed",
            "code": "recipe_install_failed",
            "reason": "distribution download stalled",
            "failure_kind": "temporary-dependency",
        },
    )
    install = _next_claim(agent_jobs, clock, node_id)
    assert install is not None
    assert install.operation == WireAgentOperation.RECIPE_INSTALL
    clock.advance(seconds=5)
    _report(agent_jobs, install, "succeeded", {"installed_bytes": 120})

    # Healed: the same held run takes the job once, and its launch is granted.
    ran = _next_claim(agent_jobs, clock, node_id)
    assert ran is not None
    assert isinstance(ran.payload, RecipeJobRunRequest)
    assert (str(ran.payload.job_id), str(ran.payload.run_id)) == (
        waiting.id,
        lost.run_id,
    )
    assert _launch_grant(sessions, service.get(waiting.id)).runtime_target_id == (
        waiting.id
    )
    # The released order keeps no hold deadline; the lost job never runs again.
    assert _order(sessions, service.get(waiting.id)).recovery_deadline is None
    assert _order(sessions, lost).current_attempt == 1
    assert _next_claim(agent_jobs, clock, node_id) is None


def test_a_newer_submission_mid_heal_waits_on_the_same_install(tmp_path) -> None:
    # Wrong implementation caught: each submission queued its own install.
    _sessions, service, _agent_jobs, _clock, _node_id, lost = _image_lost(tmp_path)
    first = submitted_artifact_job(service, lost.run_id, request_suffix=676)
    newest = submitted_artifact_job(service, lost.run_id, request_suffix=678)
    assert service.get(first.id).state == "cancelled"
    assert service.get(newest.id).state == "queued"
    assert len(_live_installs(_sessions, lost.run_id)) == 1


def test_a_submission_to_a_stopping_run_queues_no_reinstall(tmp_path) -> None:
    # Wrong implementation caught: a job run's Stop keeps the run running while
    # its jobs cancel; a submission then queued an orphan install.
    sessions, service, agent_jobs, _clock, node_id, running = _wired(tmp_path)
    assert claim_agent(agent_jobs, node_id, "serial-0") is not None
    recipe_operations = service._recipe_operations
    plan = recipe_operations.preview_stop(running.run_id)
    with pytest.raises(RecipeArtifactJobCancellationPending):
        recipe_operations.stop(
            running.run_id,
            plan_digest=plan.plan_digest,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000680",
        )
    with sessions.begin() as session:  # the Spark lost the image meanwhile
        run = session.get(RecipeRun, running.run_id)
        assert run is not None
        installation = session.get(RecipeInstallation, run.installation_id)
        assert installation is not None
        installation.state = InstallationState.PARTIAL
    draft = finalized_artifact_job(service, running.run_id, request_suffix=682)
    with pytest.raises(InvalidRequestError) as refused:
        service.submit(
            draft.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000683",
        )
    assert refused.value.typed_reason is InvalidRequestReason.SUPERSEDED
    assert _live_installs(sessions, running.run_id) == []


def test_a_reinstall_that_cannot_be_queued_leaves_the_job_queued(
    tmp_path, monkeypatch
) -> None:
    # Wrong implementation caught: a refused reinstall ended the submission
    # itself instead of leaving the job to the launch grant's definite answer.
    _sessions, service, _agent_jobs, _clock, _node_id, lost = _image_lost(tmp_path)

    def refused(*_args, **_kwargs):
        raise RecipeRetryLater("installation upkeep is running")

    monkeypatch.setattr(service._recipe_operations, "start_installation", refused)
    submitted = submitted_artifact_job(service, lost.run_id, request_suffix=684)
    assert submitted.operation_id is not None
    assert submitted.state == "queued"


def test_a_load_arriving_mid_heal_supersedes_the_install_and_cancels_the_job(
    tmp_path,
) -> None:
    # Wrong implementation caught: the submission's install took a fresh
    # workload intent, so the load could not supersede it, or the held run's
    # Stop left the waiting job to run on the replaced run.
    sessions, service, agent_jobs, _clock, node_id, lost = _image_lost(tmp_path)
    waiting = submitted_artifact_job(service, lost.run_id, request_suffix=686)
    healing = _live_installs(sessions, lost.run_id)
    assert len(healing) == 1
    recipe_operations = service._recipe_operations

    # The load: a new intent on the Spark, then the held run's Stop under it.
    with sessions.begin() as session:
        ordinal = allocate_workload_intent(session, (node_id,))
        agent_jobs.request_superseded_workload_cancellation_in_session(
            session, (node_id,), ordinal, NOW
        )
    plan = recipe_operations.preview_stop(lost.run_id)
    recipe_operations.stop(
        lost.run_id,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000688",
        workload_intent_ordinal=ordinal,
    )
    assert service.get(waiting.id).state == "cancelled"
    assert _live_installs(sessions, lost.run_id) == []
    # The load's own install step is not held by the superseded one.
    with sessions() as session:
        run = session.get(RecipeRun, lost.run_id)
        assert run is not None
        installation_id = run.installation_id
    reinstall = recipe_operations.start_installation(
        installation_id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000690",
        workload_intent_ordinal=ordinal,
    )
    assert _live_installs(sessions, lost.run_id) == [reinstall.id]
    assert reinstall.id not in healing


def test_a_job_waiting_on_a_failed_reinstall_goes_on_to_a_definite_answer(
    tmp_path,
) -> None:
    # Wrong implementation caught: a job held for a reinstall that failed
    # would wait out its whole budget instead of ending at once.
    sessions, service, agent_jobs, clock, node_id, lost = _image_lost(tmp_path)
    waiting = submitted_artifact_job(service, lost.run_id, request_suffix=674)
    install = _next_claim(agent_jobs, clock, node_id)
    assert install is not None
    assert install.operation == WireAgentOperation.RECIPE_INSTALL
    clock.advance(seconds=5)
    _report(
        agent_jobs,
        install,
        "failed",
        {
            "kind": "failed",
            "code": "recipe_install_failed",
            "reason": "image pull refused",
            "failure_kind": "invalid-contract",
        },
    )
    ran = _next_claim(agent_jobs, clock, node_id)
    assert ran is not None
    assert ran.operation == WireAgentOperation.RECIPE_JOB_RUN
    with pytest.raises(InvalidRequestError) as refused:
        _launch_grant(sessions, service.get(waiting.id))
    assert refused.value.typed_reason is InvalidRequestReason.NOT_READY
