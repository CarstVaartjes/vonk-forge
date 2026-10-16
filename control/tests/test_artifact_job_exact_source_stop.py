"""A run's Stop reaches a job's runtime by its order's own identity.

The exact JobRun Stop names the container the job's order started: its run,
job, installation and generation, on the Spark the order was sent to. Damaged
rows around that order (its parent row, a sibling no Spark was handed, the
job's link to it) never decide the Stop:

- an issued order is stopped exactly, from its own request, and a Stop that
  froze it earlier stops that very order;
- a job none of whose orders was issued holds nothing on a Spark and is retired
  as unknown, also when an earlier pass of the same Stop froze it, and a later
  pass never reads it as a new effect;
- an issued order that no exact Stop can name still holds the Stop.

Wrong implementation: the Stop refused or retried forever on the damaged rows
("source JobRun identity is inconsistent", "unknown JobRun has no exact source
authority", "accepted JobRun Stop source disappeared", "new JobRun effect
escaped the accepted Stop scope"), or released the run while its issued job
could still run.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentResult, AgentResultState, OperationProgress
from vonk_agent_protocol.recipe_operations import RecipeStopPayload, RecipeStopResult
from vonk_control.agent_jobs import AgentJobService, StaleAgentFence
from vonk_control.artifact_job_evidence import (
    ArtifactJobResultEvidence,
    dump_result_evidence,
)
from vonk_control.models import (
    AgentOperation,
    AgentOperationAttempt,
    ArtifactJob,
    Job,
    RecipeRun,
)
from vonk_control.recipe_operations import (
    RecipeArtifactJobCancellationPending,
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


def _service(tmp_path):
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

    def stop():
        return operations.stop(
            run_id,
            plan_digest=plan_digest,
            actor="operator",
            request_id=_STOP_REQUEST,
        )

    plan_digest = operations.preview_stop(run_id).plan_digest
    return sessions, operations, service, run_id, node_id, agent_jobs, clock, stop


def _order(session, artifact_job_id: str) -> tuple[Job, AgentOperation]:
    artifact = session.get(ArtifactJob, artifact_job_id)
    assert artifact is not None and artifact.operation_id is not None
    order = session.get(Job, artifact.operation_id)
    assert order is not None
    (child,) = session.scalars(
        select(AgentOperation).where(AgentOperation.parent_job_id == order.id)
    )
    return order, child


def _add_unissued_sibling(session, child: AgentOperation) -> None:
    columns = AgentOperation.__table__.columns
    sibling = AgentOperation(
        **{column.name: getattr(child, column.name) for column in columns}
    )
    sibling.id = str(uuid.uuid4())
    sibling.state = "queued"
    sibling.current_attempt = 0
    session.add(sibling)


def _run_state(sessions, run_id: str) -> str:
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        return run.state


def _activate_fresh_job_run(sessions, operations, run_id: str) -> None:
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


def _receipt_exact_stop(agent_jobs, clock, node_id, job_claim, artifact_job):
    """The Spark ends the job as its lease answer says, then receipts the exact
    Stop of the job's container."""

    try:
        directive = agent_jobs.heartbeat(
            job_claim, OperationProgress(phase="running"), 30
        )
    except StaleAgentFence:
        # The refused lease lapses and the order ends.
        clock.advance(seconds=31)
        agent_jobs.reconcile_orders()
    else:
        assert directive.cancel_requested is True
        agent_jobs.record_result(
            cancellation_result(
                job_claim,
                artifact_job,
                state="cancelled",
                reason="controller cancellation requested",
            )
        )
    # The Spark polls: a poll may first settle its lapsed order.
    exact = claim_agent(agent_jobs, node_id, "serial-0") or claim_agent(
        agent_jobs, node_id, "serial-0"
    )
    assert exact is not None and exact.fence != job_claim.fence
    assert isinstance(exact.payload, RecipeStopPayload)
    assert exact.payload.target_runtime_id == artifact_job.id
    agent_jobs.record_result(
        AgentResult(
            fence=exact.fence,
            state=AgentResultState.SUCCEEDED,
            result=RecipeStopResult(),
        )
    )


@pytest.mark.parametrize(
    "damage", ["order-kind", "order-owner", "order-targets", "unissued-sibling"]
)
def test_an_issued_job_in_a_damaged_order_is_stopped_exactly(
    tmp_path, damage: str
) -> None:
    sessions, operations, service, run_id, node_id, agent_jobs, clock, stop = _service(
        tmp_path
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    job_claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert job_claim is not None
    with sessions.begin() as session:
        order, child = _order(session, submitted.id)
        if damage == "order-kind":
            order.kind = "recipe.start"
        elif damage == "order-owner":
            order.payload = {**order.payload, "owner_id": "another-artifact-job"}
        elif damage == "order-targets":
            order.targets = ["another-spark"]
        else:
            _add_unissued_sibling(session, child)

    stopping = stop()

    # The job may still run: the run is not released, and its exact Stop goes
    # to the Spark the order was sent to.
    assert stopping.state not in {"succeeded", "failed", "cancelled"}
    assert _run_state(sessions, run_id) == "stopping"
    _receipt_exact_stop(agent_jobs, clock, node_id, job_claim, submitted)
    assert stop().state == "succeeded"
    _activate_fresh_job_run(sessions, operations, run_id)


def test_a_frozen_job_whose_order_vanished_never_holds_the_stop(tmp_path) -> None:
    sessions, operations, service, run_id, node_id, agent_jobs, _clock, stop = _service(
        tmp_path
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    job_claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert job_claim is not None
    # The accepted Stop froze the job's exact Stop and waits on its cancel.
    with pytest.raises(RecipeArtifactJobCancellationPending):
        stop()
    with sessions.begin() as session:
        _order_row, child = _order(session, submitted.id)
        for attempt in session.scalars(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == child.id
            )
        ):
            session.delete(attempt)
        session.flush()
        session.delete(child)

    # No order of the job is left on any Spark: the Spark's lease is refused.
    with pytest.raises(StaleAgentFence):
        agent_jobs.heartbeat(job_claim, OperationProgress(phase="running"), 30)
    assert stop().state == "succeeded"
    _activate_fresh_job_run(sessions, operations, run_id)


def test_a_frozen_job_is_stopped_by_its_frozen_order(tmp_path) -> None:
    sessions, operations, service, run_id, node_id, agent_jobs, clock, stop = _service(
        tmp_path
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    job_claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert job_claim is not None
    with pytest.raises(RecipeArtifactJobCancellationPending):
        stop()
    with sessions.begin() as session:
        _order_row, child = _order(session, submitted.id)
        _add_unissued_sibling(session, child)

    stopping = stop()

    assert stopping.state not in {"succeeded", "failed", "cancelled"}
    assert _run_state(sessions, run_id) == "stopping"
    _receipt_exact_stop(agent_jobs, clock, node_id, job_claim, submitted)
    assert stop().state == "succeeded"
    _activate_fresh_job_run(sessions, operations, run_id)


def test_a_job_retired_on_an_earlier_pass_never_escapes_the_stop(tmp_path) -> None:
    sessions, operations, service, run_id, node_id, agent_jobs, _clock, stop = _service(
        tmp_path
    )
    running = submitted_artifact_job(service, run_id, request_suffix=118)
    job_claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert job_claim is not None
    childless = submitted_artifact_job(service, run_id, request_suffix=120)
    with sessions.begin() as session:
        _order_row, child = _order(session, childless.id)
        session.delete(child)

    # The childless job is retired; the running job's cancel is awaited.
    with pytest.raises(RecipeArtifactJobCancellationPending):
        stop()
    # The next pass reads the childless job the same: it is no new effect.
    with pytest.raises(RecipeArtifactJobCancellationPending):
        stop()
    agent_jobs.record_result(
        cancellation_result(
            job_claim,
            running,
            state="cancelled",
            reason="controller cancellation requested",
        )
    )
    assert stop().state == "succeeded"
    _activate_fresh_job_run(sessions, operations, run_id)


def _unlink_order(sessions, artifact_job_id: str) -> None:
    """The job loses its link to its order; its evidence says it may remain."""

    with sessions.begin() as session:
        artifact = session.get(ArtifactJob, artifact_job_id)
        assert artifact is not None
        artifact.operation_id = None
        artifact.result_evidence = dump_result_evidence(
            ArtifactJobResultEvidence(active_scope_may_remain=True)
        )


def test_a_job_that_names_no_issued_order_never_holds_the_stop(tmp_path) -> None:
    sessions, operations, service, run_id, node_id, agent_jobs, _clock, stop = _service(
        tmp_path
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    _unlink_order(sessions, submitted.id)

    assert stop().state == "succeeded"
    # Its order was never handed to a Spark and never is.
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    _activate_fresh_job_run(sessions, operations, run_id)


def test_a_job_that_names_no_order_but_was_issued_holds_the_stop(tmp_path) -> None:
    sessions, _operations, service, run_id, node_id, agent_jobs, _clock, stop = (
        _service(tmp_path)
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    assert claim_agent(agent_jobs, node_id, "serial-0") is not None
    _unlink_order(sessions, submitted.id)

    # Its issued order may still run and no exact Stop can name it from the
    # job: nothing is stopped on a guess, and the run keeps serving.
    with pytest.raises(RecipeRetryLater):
        stop()
    assert _run_state(sessions, run_id) == "running"
    # The held Stop leaves the run serving: fresh work is still admitted.
    fresh = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000170"),
    )
    assert fresh.id != submitted.id
