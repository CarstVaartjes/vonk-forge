"""A sibling replacement cannot revoke ready workload content it keeps."""

from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, ReservationState, RunState
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_jobs.retry import superseded_cancellation_deadline
from vonk_control.artifact_blob_store import ArtifactBlobStore
from vonk_control.artifact_jobs import ArtifactJobService
from vonk_control.fleet_profile_contract import (
    FleetProfileAssignmentInput,
    FleetProfileInput,
)
from vonk_control.host_helper_authority import (
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.models import AgentNode, Job, RecipeRun, ResourceReservation, RunNode

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import SOLO, SOLO_B, build_world
from .test_artifact_jobs import _configure_artifact_recipe, submitted_artifact_job
from .test_profile_job_run_grant import (
    _claimed_job,
    _grant_start,
    _grant_stop,
    _report_output,
)


@pytest.mark.parametrize("job_member", [True, False], ids=["artifact-job", "service"])
# Three complete PostgreSQL profile loads plus a claimed job and its replacement.
@pytest.mark.slow(30)
def test_sibling_load_keeps_ready_run_installation_and_running_job(
    tmp_path: Path, postgres_engine, job_member: bool
):
    """Catches whole-fleet fencing after the old profile finished successfully.

    The sibling changes twice, including across a Controller restart. The kept
    job must retain its claim, grants and result delivery; replacing that very
    lane must still cancel its next job. Services retain the same live run too.
    """
    world = (
        build_world(
            tmp_path, postgres_engine, recipe_transform=_configure_artifact_recipe
        )
        if job_member
        else build_world(tmp_path, postgres_engine)
    )
    try:
        sim = Simulation(world, seed=0)
        sim.round()
        kept = world.nodes[0]

        def choice(index: int, recipe: str) -> FleetProfileAssignmentInput:
            return FleetProfileAssignmentInput(
                recipe_selector=f"vonk-forge/{world.recipes[recipe]}",
                spark_ids=[world.nodes[index]],
                assignment_name=f"lane-{index}",
            )

        assignments = [choice(0, SOLO), choice(1, SOLO_B)]

        def load(name: str) -> str:
            profiles = world.controller.profiles
            profile = profiles.create(
                FleetProfileInput(name=name, assignments=assignments), actor="admin"
            )
            return profiles.apply(
                profile.id, request_key=str(uuid4()), actor="admin"
            ).id

        assert sim.settle(load("Two lanes")) == LifecycleState.SUCCEEDED
        with world.sessions() as session:
            run = session.scalar(
                select(RecipeRun)
                .join(RunNode)
                .where(RunNode.node_id == kept, RecipeRun.state == RunState.RUNNING)
            )
            assert run is not None
            run_id, installation_id = run.id, run.installation_id
            node = session.get(AgentNode, kept)
            assert node is not None
            ordinal = node.workload_intent_ordinal
            claims = set(
                session.scalars(
                    select(ResourceReservation.id).where(
                        ResourceReservation.node_id == kept,
                        ResourceReservation.state == ReservationState.ACTIVE,
                    )
                )
            )
            assert claims
        jobs = ArtifactJobService(
            world.sessions,
            recipe_operations=world.controller.lifecycle,
            blob_store=ArtifactBlobStore(tmp_path / "blobs"),
            clock=world.clock,
        )
        orders = AgentJobService(world.sessions, clock=world.clock)
        orders.set_result_consumer(jobs.consume_agent_result)
        authority = HostRuntimeAuthorityService(
            world.sessions,
            HostHelperGrantIssuer(
                ed25519.Ed25519PrivateKey.from_private_bytes(bytes([23]) * 32),
                clock=world.clock,
            ),
            clock=world.clock,
        )
        submitted = (
            submitted_artifact_job(jobs, run_id, request_suffix=750)
            if job_member
            else None
        )
        claimed = _claimed_job(world, orders, kept) if job_member else None
        for replacement in (SOLO, SOLO_B):
            assignments[1] = choice(1, replacement)
            # Restart the Controller services; the selected snapshot and effects
            # must be read from PostgreSQL rather than remembered in a client.
            world.restart()
            application_id = load(f"Replace sibling with {replacement}")
            with world.sessions() as session:
                node = session.get(AgentNode, kept)
                assert node is not None
                if submitted is not None:
                    order = session.get(Job, submitted.operation_id)
                    assert order is not None
                    assert superseded_cancellation_deadline(order.result) is None
                assert node.workload_intent_ordinal == ordinal
            if claimed is not None:
                claim, plan = claimed
                _grant_start(authority, kept, claim.fence, plan)
                _grant_stop(authority, kept, claim.fence, plan)
                orders.heartbeat(claim, None, 600)
            assert sim.settle(application_id) == LifecycleState.SUCCEEDED
            with world.sessions() as session:
                run = session.get(RecipeRun, run_id)
                assert run is not None and run.state == RunState.RUNNING
                assert run.installation_id == installation_id
                assert (
                    set(
                        session.scalars(
                            select(ResourceReservation.id).where(
                                ResourceReservation.node_id == kept,
                                ResourceReservation.state == ReservationState.ACTIVE,
                            )
                        )
                    )
                    == claims
                )
        if claimed is not None and submitted is not None:
            claim, plan = claimed
            _report_output(jobs, orders, kept, claim, plan)
            assert jobs.get(submitted.id).state == LifecycleState.SUCCEEDED
            later = submitted_artifact_job(jobs, run_id, request_suffix=760)
            _claimed_job(world, orders, kept)
            assignments[0] = choice(0, SOLO_B)
            load("Replace job lane")
            with world.sessions() as session:
                order = session.get(Job, later.operation_id)
                assert order is not None
                assert superseded_cancellation_deadline(order.result) is not None
    finally:
        world.close()
