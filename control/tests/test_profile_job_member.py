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

from sqlalchemy import func, select
from vonk_agent_protocol import (
    RecipeJobFile,
    ReservationState,
    RunDegradedReason,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control import artifact_job_states as ajs
from vonk_control.agent_jobs import AgentJobService
from vonk_control.artifact_blob_store import ArtifactBlobStore
from vonk_control.artifact_jobs import ArtifactJobService
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
from vonk_control.fleet_projection import FleetProjection
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    FleetProfileApplication,
    Job,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.recipe_execution_contract import parse_stored_run_plan
from vonk_control.recipe_operations import prepare_exact_recipe_run_observation_nodes
from vonk_control.recipe_routes import RecipeRouteService
from vonk_control.run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchRuntimeInstallResult,
)
from vonk_control.run_switch_operations import (
    RecipeLifecyclePhaseExecutor,
    RunSwitchOperationService,
)

from cluster_profiles.fleet_qualification_campaign_cli import _serving_run
from cluster_profiles.fleet_qualification_campaign_cli.contracts import Lane, Row

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import SOLO, World, build_world
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
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


def test_an_idle_job_member_reads_as_running_and_self_healing_leaves_it_alone(
    tmp_path: Path, postgres_engine
) -> None:
    """Every view reads a held job run by the rule its load verified it by.

    Nothing runs between submissions, so the run's ranks report nothing and
    go stale while the Spark itself stays online. The member still reads as
    running, has no endpoint to report, is not degraded, is ready for
    qualification, and self-healing keeps it rather than re-loading it.
    """

    world = build_world(
        tmp_path, postgres_engine, recipe_transform=_configure_artifact_recipe
    )
    try:
        sim = Simulation(world, seed=0)
        sim.round()
        spark = world.nodes[:1]
        member, number = world.create_profile(
            "Job member", [world.assignment(SOLO, spark)]
        )
        assert sim.settle(_load(world, member)) == "succeeded"
        with world.sessions() as session:
            (run,) = session.scalars(
                select(RecipeRun).where(RecipeRun.state == "running")
            )
            run_id, alias = run.id, run.alias
            loads = session.scalar(
                select(func.count()).select_from(FleetProfileApplication)
            )

        # Idle well past the ranks' freshness window, with the Spark reporting.
        for _ in range(15):
            sim.round()

        assert world.controller.profiles.get(member).assignments[0].observed_state == (
            "Running"
        )
        with world.sessions() as session:
            intent = world.controller.profiles.endpoint_intent(session, number)
        assert intent.projection_issue is None
        assert intent.assignments == ()
        snapshot = FleetProjection(world.sessions, clock=world.clock).read()
        (node,) = (node for node in snapshot.nodes if node.id == spark[0])
        (presence,) = (item for item in node.loaded if item.run_id == run_id)
        assert presence.rank_fresh is False
        assert presence.healthy is True
        assert presence.degraded_reason is None
        lane = Lane(
            row=Row(
                key="job-member",
                sequence=1,
                content_sha256="0" * 64,
                node_count=1,
                interface="image-job",
                recovery=(),
                licenses=(),
            ),
            node_ids=spark,
            alias=alias,
            kind="artifact-job",
        )
        fleet = {item.id: item.model_dump(mode="json") for item in snapshot.nodes}
        assert _serving_run(fleet, lane) == run_id
        # No drift: the selected load is kept, and so is its held run.
        assert world.controller.profiles._reconcile_selected_roster(world.clock()) is (
            False
        )
        with world.sessions() as session:
            assert (
                session.scalar(
                    select(func.count()).select_from(FleetProfileApplication)
                )
                == loads
            )
            held = session.get(RecipeRun, run_id)
            assert held is not None and held.state == "running"
            assert (
                session.scalar(select(Job.id).where(Job.kind == "recipe.stop")) is None
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


def test_a_lost_job_member_is_reactivated_and_its_in_flight_job_ends_failed(
    tmp_path: Path, postgres_engine
) -> None:
    """A Spark comes back without the job run: the profile activates it again.

    The Spark held a claimed job when it went away; on its return it reports a
    complete empty observation (it has no local run), so the run's rank fails.
    Recovery settles a job run instead of restarting it (the job may have had
    external effects). The in-flight job ends failed with its evidence and is
    never run again, and the selected profile activates the member again as a
    new run, without waiting out the old job's observation window.
    """

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
        assert sim.settle(_load(world, member)) == "succeeded"
        with world.sessions() as session:
            (lost,) = session.scalars(
                select(RecipeRun.id).where(RecipeRun.state == "running")
            )
            loads = session.scalars(
                select(func.count()).select_from(FleetProfileApplication)
            ).one()
            host = session.get(AgentNode, spark[0])
            assert host is not None
            fingerprint = host.preflight_fingerprint
        lifecycle = world.controller.lifecycle
        agent_jobs = AgentJobService(world.sessions, clock=world.clock)
        jobs = ArtifactJobService(
            world.sessions,
            recipe_operations=lifecycle,
            blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
            clock=world.clock,
        )
        in_flight = submitted_artifact_job(jobs, lost, request_suffix=700)
        # The Spark's agent claims it (as the host its preflight proved): the
        # job may now have effects.
        claim = claim_agent(
            agent_jobs,
            spark[0],
            "serial-0",
            runtime_identity={
                **PACKAGED_RUNTIME_IDENTITY,
                "architecture": "linux-arm64",
            },
            preflight_fingerprint=fingerprint,
        )
        assert claim is not None and jobs.get(in_flight.id).state == ajs.RUNNING
        routes = world.controller.worker._routes
        assert isinstance(routes, RecipeRouteService)
        recoveries = DistributedRecoveryCoordinator(
            world.sessions,
            routes=routes,
            agent_jobs=agent_jobs,
            clock=world.clock,
            recovery_run_stops=lifecycle,
        )

        # The Spark restarts and reports, completely, that it runs nothing.
        world.advance(30)
        with world.sessions.begin() as session:
            prepare_exact_recipe_run_observation_nodes(
                session, spark[0], world.clock(), set()
            )
        current = None
        for _ in range(30):
            recoveries.tick()
            sim.round()
            with world.sessions() as session:
                current = session.scalar(
                    select(RecipeRun.id).where(
                        RecipeRun.state == "running", RecipeRun.id != lost
                    )
                )
            if current is not None:
                break

        # The member runs again, as a new activation, with no operator action.
        assert current is not None
        assert world.controller.profiles.get(member).assignments[0].observed_state == (
            "Running"
        )
        with world.sessions() as session:
            settled = session.get(RecipeRun, lost)
            assert settled is not None and settled.state == "failed"
            assert (
                session.scalar(
                    select(func.count()).select_from(FleetProfileApplication)
                )
                == loads + 1
            )
            activations = session.scalars(
                select(Job.payload["owner_id"].as_string()).where(
                    Job.kind == _ACTIVATION
                )
            ).all()
            assert sorted(activations) == sorted([lost, current])
            assert session.scalar(select(Job.id).where(Job.kind == "recipe.start")) is (
                None
            )
            # The job ran once and is never run again, on either run.
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AgentOperation)
                    .where(AgentOperation.kind == "recipe.job.run.v1")
                )
                == 1
            )
        # The in-flight job ended failed with its evidence, on the lost run.
        ended = jobs.get(in_flight.id)
        assert ended.state == ajs.FAILED
        reason_code = RunDegradedReason(ended.status_reason)
        assert reason_code is RunDegradedReason.RUN_NOT_RUNNING
        assert ended.run_id == lost
        assert ended.result_evidence is not None
        assert ended.result_evidence.active_scope_may_remain is True
    finally:
        world.close()
