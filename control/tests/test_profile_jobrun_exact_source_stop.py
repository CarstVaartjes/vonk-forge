"""A profile's Stop reaches a job's runtime by its order's own identity.

The profile load that replaces a run stops each issued job of it exactly, as
the run's own Stop does: the order the job's Spark claimed names the container.
Damaged rows around that order (a sibling no Spark was handed, the job's link
to it, its parent's ``targets``) never decide the Stop.

Wrong implementation: the profile Stop retried "artifact JobRun physical Stop
identity is ambiguous" on every pass until its deadline, or settled the job as
never issued while its order could still run.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.recipe_operations import RecipeStopPayload
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.models import AgentOperation, ArtifactJob, Job
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_artifact_job_lifecycle import _issued_job
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


def _damage(sessions, artifact_job_id: str, damage: str) -> str:
    """Damage the rows around the job's issued order; return its Spark."""

    with sessions.begin() as session:
        artifact = session.get(ArtifactJob, artifact_job_id)
        assert artifact is not None and artifact.operation_id is not None
        order = session.get(Job, artifact.operation_id)
        assert order is not None
        (child,) = session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == order.id)
        )
        if damage in {"unissued-sibling", "linked-to-unissued"}:
            columns = AgentOperation.__table__.columns
            sibling = AgentOperation(
                **{column.name: getattr(child, column.name) for column in columns}
            )
            sibling.id = str(uuid.uuid4())
            sibling.state = "queued"
            sibling.current_attempt = 0
            if damage == "linked-to-unissued":
                # The job's link moves to another order no Spark was handed.
                columns = Job.__table__.columns
                other = Job(
                    **{column.name: getattr(order, column.name) for column in columns}
                )
                other.id = str(uuid.uuid4())
                other.request_id = str(uuid.uuid4())
                other.state = "queued"
                other.result = None
                session.add(other)
                session.flush()
                sibling.parent_job_id = other.id
                artifact.operation_id = other.id
            session.add(sibling)
        elif damage == "order-targets":
            order.targets = ["another-spark"]
        else:
            artifact.operation_id = None
        return child.node_id


@pytest.mark.parametrize(
    "damage", ["order-targets", "unissued-sibling", "unlinked", "linked-to-unissued"]
)
def test_a_profile_stops_an_issued_job_in_a_damaged_order_exactly(
    tmp_path, damage: str
) -> None:
    sessions, lifecycle, _artifacts, _agent_jobs, clock, artifact, _claim, run_id = (
        _issued_job(tmp_path, 890)
    )
    node_id = _damage(sessions, artifact.id, damage)
    lifecycle._clock = clock
    switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    profiles = build_production_fleet_profile_service(
        sessions, clock=clock, run_switch_operations=switch
    )
    profile = profiles.create(
        FleetProfileInput(name="Stop issued artifact", assignments=[]), actor="admin"
    )
    accepted = profiles.load(
        profile.number,
        actor="admin",
        request_key="00000000-0000-4000-8000-000000000893",
    )

    stop = None
    for _ in range(8):
        profiles.tick()
        switch.tick()
        with sessions() as session:
            stop = session.scalar(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["execution_mode"].as_string() == "profile-jobrun-stop",
                )
            )
        if stop is not None:
            break

    # The profile's Stop names the job's container on the Spark its order was
    # sent to.
    assert stop is not None, profiles.application(accepted.id)
    with sessions() as session:
        (child,) = session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == stop.id)
        )
        payload = RecipeStopPayload.model_validate_json(
            canonical_message(child.payload)
        )
    assert child.node_id == node_id
    assert payload.target_runtime_id == artifact.id
    assert payload.run_id == run_id
