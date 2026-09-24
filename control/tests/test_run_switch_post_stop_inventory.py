"""A reconciled stop does not turn pre-stop physical readings into free memory."""

from dataclasses import fields
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_control.inventory_repository import (
    MAX_INVENTORY_FUTURE_SKEW,
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import Job, NodeInventorySnapshot, RecipeRun
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import RecipeLifecyclePhaseExecutor

from .test_profile_port_claims import _ready_profile
from .test_recipe_operations import started_recipe
from .test_run_switch_operations import RecordingArtifactExecutor, _request, _service


@pytest.mark.parametrize("node_count", [1, 2])
def test_switch_waits_for_new_physical_inventory_after_stop(
    tmp_path, postgres_engine, node_count
):
    sessions, _profiles, planner, _profile, _api, _headers, nodes, installation_id = (
        _ready_profile(tmp_path, postgres_engine, node_count=node_count)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    old = started_recipe(
        sessions,
        lifecycle,
        installation_id,
        nodes,
        request_id=str(uuid4()),
        alias="old",
    )
    now = [lifecycle._clock() + timedelta(seconds=1)]
    lifecycle._clock = planner._clock = lambda: now[0]
    executor = planner._phase_executor
    assert isinstance(executor, RecipeLifecyclePhaseExecutor)
    executor._clock = lambda: now[0]
    request = _request(sessions, nodes[0], action="switch")
    request = request.model_copy(
        update={
            "spark_group": SparkGroup(
                nodes=[
                    SparkGroupNode(
                        node_id=node_id,
                        rank=rank,
                        role="entrypoint" if rank == 0 else "worker",
                        endpoint_owner=rank == 0,
                    )
                    for rank, node_id in enumerate(nodes)
                ]
            )
        }
    )
    review = planner.preview(request, actor="admin")
    assert review.allowed, review.model_dump(mode="json")
    assert [stop.run_id for stop in review.stops] == [old.owner_id]
    accepted = planner.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=review.plan_digest,
            request_key=str(uuid4()),
        ),
        actor="admin",
    )
    stop_id = None
    for _ in range(20):
        planner.tick()
        with sessions() as session:
            stop_id = session.scalar(select(Job.id).where(Job.kind == "recipe.stop"))
        if stop_id is not None:
            break
    assert stop_id is not None
    for node_id in nodes:
        lifecycle.record_node_result(
            stop_id, node_id, succeeded=True, evidence={"stopped": True}
        )
    with sessions() as session:
        run = session.get(RecipeRun, old.owner_id)
        assert run is not None and run.state == "stopped" and run.stopped_at is not None
    for _ in range(20):
        planner.tick()
        current = planner.get(accepted.operation_id)
        if current.result is not None and current.result.retry_reason:
            break
    with sessions() as session:
        initial_starts = tuple(
            session.scalars(select(Job.id).where(Job.kind == "recipe.start"))
        )
        assert initial_starts == (old.id,), (
            "pre-stop inventory authorized a replacement start"
        )
    assert current.state == "running"
    assert current.result is not None
    assert current.result.retry_reason == "run-switch.post-stop-inventory-pending"

    # Restart the owning service while the wait is durable. A sample at the
    # maximum admitted clock lead can still predate the stop, even when its
    # receipt arrives later; it must not release the wait.
    with sessions() as session:
        samples = [
            {
                field.name: getattr(snapshot, field.name)
                for field in fields(InventorySnapshotInput)
            }
            for snapshot in session.scalars(select(NodeInventorySnapshot))
        ]
    repository = InventoryRepository(sessions, clock=lambda: now[0])
    now[0] += MAX_INVENTORY_FUTURE_SKEW
    for values in samples:
        values["observed_at"] = now[0]
        repository.record(InventorySnapshotInput(**values))
    restarted = _service(sessions, now[0], lifecycle, RecordingArtifactExecutor())
    assert restarted.tick()
    waiting = restarted.get(accepted.operation_id)
    assert waiting.state == "running" and waiting.result is not None
    assert waiting.result.retry_reason == "run-switch.post-stop-inventory-pending"
    now[0] += timedelta(seconds=6)
    samples[0]["observed_at"] = now[0]
    repository.record(InventorySnapshotInput(**samples[0]))
    if node_count > 1:
        partial = _service(sessions, now[0], lifecycle, RecordingArtifactExecutor())
        partial.tick()
        waiting = partial.get(accepted.operation_id)
        assert waiting.result is not None
        assert waiting.result.retry_reason == "run-switch.post-stop-inventory-pending"
        with sessions() as session:
            assert tuple(
                session.scalars(select(Job.id).where(Job.kind == "recipe.start"))
            ) == (old.id,)
        now[0] += timedelta(seconds=6)
        for values in samples[1:]:
            values["observed_at"] = now[0]
            repository.record(InventorySnapshotInput(**values))
    resumed = _service(sessions, now[0], lifecycle, RecordingArtifactExecutor())
    for _ in range(10):
        resumed.tick()
        with sessions() as session:
            starts = tuple(
                session.scalars(select(Job).where(Job.kind == "recipe.start"))
            )
        if len(starts) == 2:
            break
    assert len(starts) == 2
    replacement = next(job for job in starts if job.id != old.id)
    assert replacement.state == "running"
    current = resumed.get(accepted.operation_id)
    assert current.result is not None and current.result.retry_reason is None
    # A later observer adopts the committed child, even if physical telemetry
    # subsequently goes stale: it must not acquire another start identity.
    again = _service(
        sessions, now[0] + timedelta(minutes=10), lifecycle, RecordingArtifactExecutor()
    )
    again.tick()
    with sessions() as session:
        assert {
            job.id
            for job in session.scalars(select(Job).where(Job.kind == "recipe.start"))
        } == {old.id, replacement.id}
