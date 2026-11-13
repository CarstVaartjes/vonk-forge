"""A run's Stop never waits on an artifact job order that a re-plan cannot change.

An unissued order ends under the Stop's own cancellation, and an order with no
child that was ever issued to a Spark is read the same on every pass: it is
retired as unknown and the Stop goes on. Nothing is stopped on a guess: an
issued child that no exact Stop can target still holds the Stop.

Wrong implementation: the Stop refused the run ("artifact job has no
cancellation authority") or retried it forever ("unknown JobRun exact target is
not reachable"), so every Stop of that run, and every load that stops it, ended
on the same rows.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentClaim,
    AgentProgress,
    AgentResult,
    AgentResultState,
    OperationProgress,
)
from vonk_agent_protocol.recipe_operations import RecipeStopResult
from vonk_control import artifact_job_states as ajs
from vonk_control.agent_jobs import AgentJobService
from vonk_control.artifact_jobs import ArtifactJobService, ArtifactJobView
from vonk_control.lifecycle.artifact_job import ArtifactJobAdapter
from vonk_control.models import AgentNode, AgentOperation, ArtifactJob, Job, RecipeRun
from vonk_control.recipe_operations import (
    RecipeArtifactJobCancellationPending,
    RecipeOperationService,
    RecipeRetryLater,
)

from .runtime_identity_support import claim_agent
from .test_artifact_jobs import (
    NOW,
    MutableClock,
    artifact_create_request,
    cancellation_result,
    create_artifact_job,
    running_artifact_service,
    submitted_artifact_job,
)

_STOP_REQUEST = "00000000-0000-4000-8000-000000000154"


def _fenced_without_cancellation(sessions, node_id: str) -> int:
    """A newer load's intent on the Spark: its sweep never cancels a JobRun."""

    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.workload_intent_ordinal += 1
        return node.workload_intent_ordinal


def _activate_fresh_job_run(sessions, operations, run_id: str) -> None:
    """The run ended stopped and its installation admits a fresh job run."""

    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "stopped"
        installation_id = run.installation_id
    fresh = operations.preview_run(installation_id, "image-job")
    activated = operations.activate_job_run(
        fresh,
        plan_digest=fresh.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000160",
    )
    assert activated.owner_id != run_id


def _order_child(session, artifact_job_id: str):
    artifact = session.get(ArtifactJob, artifact_job_id)
    assert artifact is not None and artifact.operation_id is not None
    return select(AgentOperation).where(
        AgentOperation.parent_job_id == artifact.operation_id
    )


def test_an_unissued_order_that_records_no_cancellation_never_holds_the_stop(
    tmp_path,
) -> None:
    sessions, operations, _queue, service, run_id, node_id = running_artifact_service(
        tmp_path
    )
    agent_jobs = AgentJobService(sessions, clock=MutableClock(NOW))
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    plan = operations.preview_stop(run_id)
    ordinal = _fenced_without_cancellation(sessions, node_id)

    stopped = operations.stop(
        run_id,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id=_STOP_REQUEST,
        workload_intent_ordinal=ordinal,
    )

    assert stopped.state == "succeeded"
    # The fenced order is never handed to the Spark.
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    ArtifactJobAdapter(sessions=sessions).reconcile()
    assert service.get(submitted.id).state in ajs.ENDED
    _activate_fresh_job_run(sessions, operations, run_id)


@dataclass
class _CancelledJob:
    """A claimed job that its run's Stop cancelled; the Stop waits on it."""

    sessions: sessionmaker[Session]
    operations: RecipeOperationService
    service: ArtifactJobService
    run_id: str
    node_id: str
    agent_jobs: AgentJobService
    clock: MutableClock
    job: ArtifactJobView
    claim: AgentClaim

    def stop(self):
        return self.operations.stop(
            self.run_id,
            plan_digest=self.operations.preview_stop(self.run_id).plan_digest,
            actor="operator",
            request_id=_STOP_REQUEST,
        )

    def order_state(self) -> str:
        with self.sessions() as session:
            (child,) = session.scalars(_order_child(session, self.job.id))
            return child.state


def _cancelled_running_job(tmp_path) -> _CancelledJob:
    sessions, operations, _queue, service, run_id, node_id = running_artifact_service(
        tmp_path
    )
    clock = MutableClock(NOW)
    agent_jobs = AgentJobService(sessions, clock=clock)

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    operations._agent_jobs = agent_jobs
    job = submitted_artifact_job(service, run_id, request_suffix=118)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    cancelled = _CancelledJob(
        sessions, operations, service, run_id, node_id, agent_jobs, clock, job, claim
    )
    with pytest.raises(RecipeArtifactJobCancellationPending):
        cancelled.stop()
    # The real agent stops renewing on its first cancel directive.
    directive = agent_jobs.heartbeat(claim, OperationProgress(phase="running"), 30)
    assert directive.cancel_requested is True
    clock.advance(seconds=31)
    return cancelled


def test_a_cancelled_job_whose_spark_goes_silent_is_stopped_exactly(
    tmp_path,
) -> None:
    """The Spark takes the cancel and never reports: the lapsed order ends, so
    it never holds the run's Stop, and the exact JobRun Stop is handed to it.

    Wrong implementation: the run's Stop (and every load that stops the run)
    waited on a cancelled JobRun's receipt that could no longer arrive.
    """

    cancelled = _cancelled_running_job(tmp_path)
    cancelled.agent_jobs.reconcile_orders()
    assert cancelled.order_state() not in {"queued", "running"}

    stopping = cancelled.stop()
    assert stopping.state not in {"succeeded", "failed", "cancelled"}
    exact = claim_agent(cancelled.agent_jobs, cancelled.node_id, "serial-0")
    assert exact is not None and exact.fence != cancelled.claim.fence
    cancelled.agent_jobs.record_result(
        AgentResult(
            fence=exact.fence,
            state=AgentResultState.SUCCEEDED,
            result=RecipeStopResult(),
        )
    )
    assert cancelled.stop().state == "succeeded"
    _activate_fresh_job_run(cancelled.sessions, cancelled.operations, cancelled.run_id)


def test_a_stale_heartbeat_on_an_ended_cancelled_job_is_known_cancelled(
    tmp_path,
) -> None:
    """Catches a plain refusal (which the agent retries) instead of the typed
    ``superseded_operation_cancelled`` answer for a cancelled JobRun."""

    cancelled = _cancelled_running_job(tmp_path)
    cancelled.agent_jobs.reconcile_orders()

    stale = AgentProgress(fence=cancelled.claim.fence, progress=None)
    assert cancelled.agent_jobs.known_superseded_cancellation(stale) is True


def test_a_late_cancelled_receipt_ends_a_cancelled_job_without_cleanup(
    tmp_path,
) -> None:
    """The Spark's exit-130 receipt arrives after its lease: the job ends
    cancelled with its effect proved stopped, so no cleanup Stop is queued.

    Wrong implementation: the late receipt was read as a lapsed lease, leaving
    the job's runtime in doubt and a cleanup Stop queued for it.
    """

    cancelled = _cancelled_running_job(tmp_path)
    # Past its lease the receipt is late: the agent API hands it on as one.
    assert cancelled.agent_jobs.record_late_result(
        cancellation_result(
            cancelled.claim,
            cancelled.job,
            state="cancelled",
            reason="controller cancellation requested",
        )
    )

    assert cancelled.order_state() == "cancelled"
    assert cancelled.service.get(cancelled.job.id).state == ajs.CANCELLED
    with cancelled.sessions() as session:
        stops = session.scalars(
            select(Job.id).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_kind"].as_string() == "artifact-job",
            )
        ).all()
    assert stops == []
    assert cancelled.stop().state == "succeeded"
    _activate_fresh_job_run(cancelled.sessions, cancelled.operations, cancelled.run_id)


def test_an_order_without_its_child_never_holds_the_stop(tmp_path) -> None:
    sessions, operations, _queue, service, run_id, node_id = running_artifact_service(
        tmp_path
    )
    agent_jobs = AgentJobService(sessions, clock=MutableClock(NOW))
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    with sessions.begin() as session:
        (child,) = session.scalars(_order_child(session, submitted.id))
        session.delete(child)

    plan = operations.preview_stop(run_id)
    stopped = operations.stop(
        run_id,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id=_STOP_REQUEST,
    )

    assert stopped.state == "succeeded"
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    ArtifactJobAdapter(sessions=sessions).reconcile()
    assert service.get(submitted.id).state in ajs.ENDED
    _activate_fresh_job_run(sessions, operations, run_id)


def test_an_issued_child_off_the_run_holds_the_stop(tmp_path) -> None:
    sessions, operations, _queue, service, run_id, node_id = running_artifact_service(
        tmp_path
    )
    agent_jobs = AgentJobService(sessions, clock=MutableClock(NOW))
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    assert claim_agent(agent_jobs, node_id, "serial-0") is not None
    with sessions.begin() as session:
        (child,) = session.scalars(_order_child(session, submitted.id))
        child.node_id = "spk_" + "f" * 32

    plan = operations.preview_stop(run_id)
    with pytest.raises(RecipeRetryLater):
        operations.stop(
            run_id,
            plan_digest=plan.plan_digest,
            actor="operator",
            request_id=_STOP_REQUEST,
        )

    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "running"
    # The held Stop leaves the run serving: fresh work is still admitted.
    fresh = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000170"),
    )
    assert fresh.id != submitted.id
