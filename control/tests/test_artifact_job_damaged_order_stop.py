"""A run's Stop never waits on an artifact job whose order bookkeeping is damaged.

Before a one-shot run stops, its live artifact jobs' cancellations are awaited.
A job whose order rows no longer agree with it (the order's kind, the job it
names, or the Sparks its node is on) cannot be re-read into a different answer
by a re-plan: it is retired as unknown and the Stop goes on. The Stop's newer
intent fences the order, the job settles once its run has ended, and the
installation admits fresh work.

Wrong implementation: the Stop refused the run as a conflict ("artifact job
cancellation authority changed"), so every Stop of that run, and every load
that stops it, ended on the same damaged rows.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from vonk_control import artifact_job_states as ajs
from vonk_control.lifecycle.artifact_job import ArtifactJobAdapter
from vonk_control.models import AgentOperation, ArtifactJob, Job, RecipeRun

from .test_artifact_jobs import running_artifact_service, submitted_artifact_job


@pytest.mark.parametrize("damage", ["order-kind", "order-owner", "order-targets"])
def test_a_damaged_artifact_job_order_never_holds_the_run_stop(
    tmp_path, damage: str
) -> None:
    sessions, operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    with sessions.begin() as session:
        artifact = session.get(ArtifactJob, submitted.id)
        assert artifact is not None and artifact.operation_id is not None
        order = session.get(Job, artifact.operation_id)
        assert order is not None
        if damage == "order-kind":
            order.kind = "recipe.start"
        elif damage == "order-owner":
            order.payload = {**order.payload, "owner_id": "another-artifact-job"}
        else:
            order.targets = ["another-spark"]
        order_id = order.id

    plan = operations.preview_stop(run_id)
    stopped = operations.stop(
        run_id,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000154",
    )

    assert stopped.state == "succeeded"
    ArtifactJobAdapter(sessions=sessions).reconcile()
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "stopped"
        installation_id = run.installation_id
        (child,) = session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == order_id)
        )
        # The order ended without ever being issued to the Spark.
        assert child.state in {"cancelled", "failed"}
        assert child.current_attempt == 0
    assert service.get(submitted.id).state in ajs.ENDED
    # Nothing is left holding the installation: fresh work is admitted.
    fresh = operations.preview_run(installation_id, "image-job")
    activated = operations.activate_job_run(
        fresh,
        plan_digest=fresh.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000160",
    )
    assert activated.owner_id != run_id
