"""A profile load activates a job recipe; a later load stops it.

A job recipe (one ``*-job`` interface, no service port) serves by holding its
run: the run is RUNNING in ``one-shot-jobs`` mode with its route withdrawn and
its memory reserved, and each artifact job submitted against it is a one-shot
container. The profile load is the one way such a run is created, the same as
for a serving recipe, and a later load supersedes and stops it.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from vonk_agent_protocol import (
    RecipeJobFile,
    ReservationState,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control.artifact_blob_store import ArtifactBlobStore
from vonk_control.artifact_jobs import ArtifactJobService
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.recipe_execution_contract import parse_stored_run_plan
from vonk_control.run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchRuntimeInstallResult,
)
from vonk_control.run_switch_operations import (
    RecipeLifecyclePhaseExecutor,
    RunSwitchOperationService,
)

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import SOLO, World, build_world
from .test_artifact_jobs import _configure_artifact_recipe, submitted_artifact_job
from .test_recipe_operations import installed_recipe, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _request,
)

_ACTIVATION = "recipe.job.activate.v1"


def _load(world: World, profile_id: str) -> str:
    return world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    ).id


def _finish_artifact_job(
    service: ArtifactJobService, sessions, job_id: str
) -> str | None:
    """The Spark ran the job and reported one output; the job's end state."""

    job = service.get(job_id)
    output = b"png-output"
    digest = hashlib.sha256(output).hexdigest()
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job.operation_id
            )
        )
        assert operation is not None and operation.kind == "recipe.job.run.v1"
        node_id = operation.node_id
    service.put_output(
        job.id,
        node_id=node_id,
        name="output.png",
        media_type="image/png",
        expected_sha256=digest,
        content=output,
    )
    outputs = (
        RecipeJobFile(
            name="output.png",
            media_type="image/png",
            size_bytes=len(output),
            sha256=digest,
        ),
    )
    result = {
        "job_id": job.id,
        "run_id": job.run_id,
        "exit_code": 0,
        "output_manifest": {
            **recipe_job_manifest_document(outputs),
            "manifest_sha256": recipe_job_manifest_sha256(outputs),
        },
        "evidence": {"elapsed_milliseconds": 10, "peak_memory_bytes": None},
    }
    with sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        service.consume_agent_result(
            session,
            stored,
            object(),
            SimpleNamespace(state="succeeded", result=result),
        )
    return service.get(job.id).state


def test_a_profile_load_activates_a_job_recipe_and_a_later_load_stops_it(
    tmp_path: Path, postgres_engine
) -> None:
    world = build_world(
        tmp_path, postgres_engine, recipe_transform=_configure_artifact_recipe
    )
    try:
        sim = Simulation(world, seed=0)
        sim.round()
        spark = world.nodes[:1]
        member, _number = world.create_profile(
            "Job member", [world.assignment(SOLO, spark)]
        )

        load = _load(world, member)

        assert sim.settle(load) == "succeeded", world.controller.profiles.application(
            load
        )
        with world.sessions() as session:
            (run,) = session.scalars(
                select(RecipeRun).where(RecipeRun.state == "running")
            )
            assert parse_stored_run_plan(run.plan).execution_mode == "one-shot-jobs"
            assert run.route_state == "withdrawn"
            (activation,) = session.scalars(select(Job).where(Job.kind == _ACTIVATION))
            (switch,) = session.scalars(
                select(Job).where(Job.kind == "recipe.run-switch.v2")
            )
            node = session.get(AgentNode, spark[0])
            assert node is not None
            assert isinstance(switch.result, dict)
            assert switch.result["profile_application_id"] == load
            # The run is the profile's, under the load's own workload intent.
            assert activation.payload["owner_id"] == run.id
            assert activation.payload["profile_application_id"] == load
            ordinal = activation.payload["workload_intent_ordinal"]
            assert type(ordinal) is int
            assert ordinal == switch.payload["workload_intent_ordinal"]
            assert node.workload_intent_ordinal == ordinal
            # No service container was ever asked for.
            assert (
                session.scalar(select(Job.id).where(Job.kind == "recipe.start")) is None
            )
            run_id = run.id

        jobs = ArtifactJobService(
            world.sessions,
            recipe_operations=world.controller.lifecycle,
            blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
            clock=world.clock,
        )
        submitted = submitted_artifact_job(jobs, run_id, request_suffix=700)
        assert _finish_artifact_job(jobs, world.sessions, submitted.id) == "succeeded"

        replacement, _number = world.create_profile("Nothing", [])
        stop_load = _load(world, replacement)

        assert sim.settle(stop_load) == "succeeded"
        with world.sessions() as session:
            stop = session.scalar(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == run_id,
                )
            )
            run = session.get(RecipeRun, run_id)
            assert stop is not None and run is not None
            # The finished job left nothing on the Spark: the run is stopped
            # under the newer load's intent and its reservation released.
            assert stop.payload["execution_mode"] == "one-shot-jobs"
            stop_ordinal = stop.payload["workload_intent_ordinal"]
            assert type(stop_ordinal) is int and stop_ordinal > ordinal
            assert run.state == "stopped" and run.route_state == "withdrawn"
            assert (
                session.scalar(
                    select(ResourceReservation.id).where(
                        ResourceReservation.owner_kind == "run",
                        ResourceReservation.owner_id == run_id,
                        ResourceReservation.state == ReservationState.ACTIVE,
                    )
                )
                is None
            )
    finally:
        world.close()


def test_a_resumed_start_phase_adopts_the_activation_it_already_made(
    tmp_path: Path,
) -> None:
    """A restart re-enters the start phase; it binds the run it already activated."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, recipe_transform=_configure_artifact_recipe
    )
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    planner = RunSwitchOperationService(
        sessions,
        memory_floor_bytes=0,
        lifecycle=lifecycle,
        clock=lambda: lifecycle._clock(),
        artifacts=CompleteArtifactInspector(missing_spark_bytes=1024),
        artifact_phase_executor=RecordingArtifactExecutor(),
    )
    plan = planner.preview(_request(sessions, nodes[0]), actor="admin")
    start = next(phase for phase in plan.phases if phase.kind == "start")
    executor = RecipeLifecyclePhaseExecutor(
        lifecycle, sessions, ClusterMappingService(sessions), lifecycle._clock
    )
    with sessions.begin() as session:
        for node_id in nodes:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.workload_intent_ordinal = 4
    progress = RunSwitchOperationResult(
        workload_intent_ordinal=4,
        phase_results=[
            RunSwitchRuntimeInstallResult(
                phase="prepare",
                subphase="runtime-install",
                installation_id=installation.owner_id,
            )
        ],
    )
    request_key = str(uuid.uuid4())

    first, second = (
        executor.execute(
            plan,
            start,
            item_index=0,
            actor="admin",
            request_key=request_key,
            progress=progress,
        )
        for _ in range(2)
    )

    assert first.operation_id is not None
    assert second.operation_id == first.operation_id
    with sessions() as session:
        (activation,) = session.scalars(select(Job).where(Job.kind == _ACTIVATION))
        assert activation.payload["workload_intent_ordinal"] == 4
        assert session.scalar(select(Job.id).where(Job.kind == "recipe.start")) is None
        nodes_now = {
            node.workload_intent_ordinal
            for node in session.scalars(
                select(AgentNode).where(AgentNode.node_id.in_(nodes))
            )
        }
    # The run-switch's own intent is kept, not bumped past itself.
    assert nodes_now == {4}
