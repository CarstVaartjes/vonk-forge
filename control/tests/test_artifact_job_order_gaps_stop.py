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

import contextlib

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentResult, AgentResultState, OperationProgress
from vonk_agent_protocol.recipe_operations import RecipeStopResult
from vonk_control import artifact_job_states as ajs
from vonk_control.agent_jobs import AgentJobService, StaleAgentFence
from vonk_control.lifecycle.artifact_job import ArtifactJobAdapter
from vonk_control.models import AgentNode, AgentOperation, ArtifactJob, RecipeRun
from vonk_control.recipe_operations import (
    RecipeArtifactJobCancellationPending,
    RecipeRetryLater,
)

from .runtime_identity_support import claim_agent
from .test_artifact_jobs import (
    NOW,
    MutableClock,
    artifact_create_request,
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


def test_a_running_job_whose_spark_never_acknowledges_the_cancel_is_stopped_exactly(
    tmp_path,
) -> None:
    """The run Stop cancels its running job; a Spark that keeps renewing without
    stopping it never holds the Stop: the order ends after its stop budget and
    the exact JobRun Stop is handed to the Spark."""

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
    submitted_artifact_job(service, run_id, request_suffix=118)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    plan = operations.preview_stop(run_id)

    def stop():
        return operations.stop(
            run_id,
            plan_digest=plan.plan_digest,
            actor="operator",
            request_id=_STOP_REQUEST,
        )

    stopping = None
    for _tick in range(64):  # bounded: the stop budget ends the order first
        try:
            stopping = stop()
            break
        except RecipeArtifactJobCancellationPending:
            pass
        with contextlib.suppress(StaleAgentFence):
            directive = agent_jobs.heartbeat(
                claim, OperationProgress(phase="running"), 30
            )
            assert directive.cancel_requested is True
        clock.advance(seconds=60)
        agent_jobs.reconcile_orders()
    assert stopping is not None
    assert stopping.state not in {"succeeded", "failed", "cancelled"}
    exact = claim_agent(agent_jobs, node_id, "serial-0") or claim_agent(
        agent_jobs, node_id, "serial-0"
    )
    assert exact is not None and exact.fence != claim.fence
    agent_jobs.record_result(
        AgentResult(
            fence=exact.fence,
            state=AgentResultState.SUCCEEDED,
            result=RecipeStopResult(),
        )
    )
    assert stop().state == "succeeded"
    _activate_fresh_job_run(sessions, operations, run_id)


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
