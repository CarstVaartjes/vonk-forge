"""A profile load activates a job recipe; a later load stops it.

A job recipe (one ``*-job`` interface, no service port) serves by holding its
run: the run is RUNNING in ``one-shot-jobs`` mode with its route withdrawn and
its memory reserved, and each artifact job submitted against it is a one-shot
container. The profile load is the one way such a run is created, the same as
for a serving recipe, and a later load supersedes and stops it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import func, select
from vonk_agent_protocol import (
    AgentResult,
    ProjectionCode,
    RecipeJobFile,
    ReservationState,
    WaitReason,
    canonical_message,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control import artifact_job_states as ajs
from vonk_control.agent_jobs import AgentJobService
from vonk_control.artifact_blob_store import ArtifactBlobStore
from vonk_control.artifact_jobs import ArtifactJobService
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.fleet_projection import FleetProjection
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    ArtifactJob,
    FleetProfileApplication,
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

from cluster_profiles.fleet_qualification_campaign_cli import _serving_run
from cluster_profiles.fleet_qualification_campaign_cli.contracts import Lane, Row

from .lifecycle_simulation.invariants import ENDED
from .lifecycle_simulation.simulation import ROUND_SECONDS, Simulation
from .lifecycle_simulation.sparks import Spark, Sparks
from .lifecycle_simulation.world import SOLO, SOLO_B, World, build_world
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
from .test_artifact_jobs import _configure_artifact_recipe, submitted_artifact_job
from .test_recipe_operations import installed_recipe, setup_services, start_evidence
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


class _AgentSparks(Sparks):
    """Fake Sparks whose agent claims each order before it answers it.

    The real agent answers an order only under its claim, and its report is
    that attempt's result; the base fake Sparks answer without a claim. Each
    poll also records the Spark's contact, as the real agent's poll does.
    """

    def _answer(self, spark: Spark) -> None:
        if any(
            order.kind == "runtime.preflight.v1"
            for order in self._orders(spark.node_id)
        ):
            super()._answer(spark)  # preflight as every simulated Spark has it
            return
        orders = AgentJobService(
            self.world.sessions,
            clock=self.world.clock,
            result_consumer=self.world.controller.lifecycle.consume_agent_result,
        )
        with self.world.sessions() as session:
            host = session.get(AgentNode, spark.node_id)
            fingerprint = None if host is None else host.preflight_fingerprint
        for _order in range(16):
            claim = claim_agent(
                orders,
                spark.node_id,
                "serial-0",
                runtime_identity={
                    **PACKAGED_RUNTIME_IDENTITY,
                    "architecture": "linux-arm64",
                },
                preflight_fingerprint=fingerprint,
            )
            if claim is None:
                return
            kind = claim.operation.value
            payload = json.loads(canonical_message(claim.payload))
            result: dict[str, object] = (
                start_evidence(payload)
                if kind == "recipe.start"
                else {"installed_bytes": 120}
                if kind == "recipe.install"
                else {}
            )
            orders.record_result(
                AgentResult.model_validate(
                    {"fence": claim.fence, "state": "succeeded", "result": result}
                )
            )
            run_id = payload.get("run_id")
            if kind == "recipe.start" and isinstance(run_id, str):
                spark.serving.add(run_id)
            elif kind == "recipe.stop" and isinstance(run_id, str):
                spark.serving.discard(run_id)


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


def test_a_job_that_ended_on_an_offline_spark_never_holds_a_later_load(
    tmp_path: Path, postgres_engine
) -> None:
    """A later load stops a job member whose Spark went away mid-job.

    The Spark claimed a job and stopped reporting; the job's lease lapsed, so
    the job is lost: it may still run, so it is observed, never ended by a
    deadline. A newer load then stops the member while the Spark is still
    away: its cancel ends the lost job, whose possible leftovers stay recorded
    on it. When the Spark returns the leftovers are stopped and the Spark takes
    a new member.

    Wrong implementation: the Stop of the ended job's runtime required the
    newer load's cancellation of that job, which nothing writes once the job
    has ended, so the load retried its Stop forever and the run kept its
    reservation, also after the Spark returned.
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
            (run_id,) = session.scalars(
                select(RecipeRun.id).where(RecipeRun.state == "running")
            )
            node = session.get(AgentNode, spark[0])
            assert node is not None
            architecture, fingerprint = node.architecture, node.preflight_fingerprint
        jobs = ArtifactJobService(
            world.sessions,
            recipe_operations=world.controller.lifecycle,
            blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
            clock=world.clock,
        )
        submitted = submitted_artifact_job(jobs, run_id, request_suffix=700)
        # The Spark's agent claims the job (the fake Sparks do not run jobs)
        # and then the Spark goes away.
        assert claim_agent(
            AgentJobService(world.sessions, clock=world.clock),
            spark[0],
            "serial-0",
            runtime_identity={
                **PACKAGED_RUNTIME_IDENTITY,
                "architecture": architecture,
            },
            preflight_fingerprint=fingerprint,
        )
        # Away until the faults clear: rounds alone never end a step.
        sim.sparks.by_node[spark[0]].down_steps = 1

        def order_state() -> str:
            with world.sessions() as session:
                order = session.get(Job, submitted.operation_id)
                assert order is not None
                return order.state

        # Its lease lapses: the job may still run, so its order is lost
        # (observed again), never ended unobserved.
        for _ in range(20):
            sim.round()
        assert order_state() not in ENDED

        def away() -> None:
            # The run of a Spark that is away keeps its exact Stop until the
            # Spark returns; the monitor's fleet check reads that run as still
            # live, so the rounds while away run without it.
            world.advance(ROUND_SECONDS)
            world.tick()
            sim.sparks.round()

        replacement, _number = world.create_profile("Nothing", [])
        stop_load = _load(world, replacement)

        for _ in range(10):
            away()
            if world.controller.profiles.application(stop_load).state in ENDED:
                break
        assert world.controller.profiles.application(stop_load).state == "succeeded"
        for _ in range(5):
            away()
        ended = jobs.get(submitted.id)
        assert ended.state in ajs.ENDED
        assert ended.result_evidence is not None
        assert ended.result_evidence.active_scope_may_remain is True
        with world.sessions() as session:
            run = session.get(RecipeRun, run_id)
            assert run is not None and run.state != "running"
        other, _number = world.create_profile(
            "Other member", [world.assignment(SOLO_B, spark)]
        )
        other_load = _load(world, other)
        away()

        sim.sparks.clear_faults()

        assert sim.settle(other_load) == "succeeded"
        with world.sessions() as session:
            run = session.get(RecipeRun, run_id)
            assert run is not None and run.state == "stopped"
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


def _job_in_flight_on_an_offline_spark(tmp_path: Path, world: World, sim: Simulation):
    """A loaded job member whose Spark went offline with a claimed job.

    The Spark's agent claimed the job (so it may have had effects), then the
    Spark stopped reporting and answering, past the job's lease and its
    accepted timeout, and came back. Returns the member, its run, the job
    service, the job and how many loads there were.
    """

    spark = world.nodes[0]
    member, _number = world.create_profile(
        "Job member", [world.assignment(SOLO, (spark,))]
    )
    assert sim.settle(_load(world, member)) == "succeeded"
    with world.sessions() as session:
        (run_id,) = session.scalars(
            select(RecipeRun.id).where(RecipeRun.state == "running")
        )
        loads = session.scalars(
            select(func.count()).select_from(FleetProfileApplication)
        ).one()
        host = session.get(AgentNode, spark)
        assert host is not None
        fingerprint = host.preflight_fingerprint
    jobs = ArtifactJobService(
        world.sessions,
        recipe_operations=world.controller.lifecycle,
        blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
        clock=world.clock,
    )
    in_flight = submitted_artifact_job(jobs, run_id, request_suffix=700)
    # The Spark's agent claims it, as the host its preflight proved.
    claim = claim_agent(
        AgentJobService(world.sessions, clock=world.clock),
        spark,
        "serial-0",
        runtime_identity={**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"},
        preflight_fingerprint=fingerprint,
    )
    assert claim is not None and jobs.get(in_flight.id).state == ajs.RUNNING

    offline = sim.sparks.by_node[spark]
    offline.down_steps = 1
    for _ in range(8):
        sim.round(600)
        with world.sessions() as session:
            # Offline, the member keeps its run and its memory: nothing else
            # can use that Spark.
            held = session.get(RecipeRun, run_id)
            assert held is not None and held.state == "running"
            assert (
                session.scalar(
                    select(ResourceReservation.id).where(
                        ResourceReservation.owner_kind == "run",
                        ResourceReservation.owner_id == run_id,
                        ResourceReservation.state == ReservationState.ACTIVE,
                    )
                )
                is not None
            )
    offline.down_steps = 0
    for _ in range(3):
        sim.round()
    return member, run_id, jobs, in_flight.id, loads


def _assert_ended_and_stopped_exactly(
    world: World, jobs: ArtifactJobService, job_id: str
) -> None:
    """The lost job ended by the newer intent; an exact Stop proved it gone.

    Wrong implementations: a deadline ended the lost job failed while it may
    still run, or the exact Stop's receipt left its runtime recorded in doubt.
    """

    ended = jobs.get(job_id)
    assert ended.state in ajs.ENDED and ended.supported_actions == ()
    assert ended.result_evidence is not None
    assert ended.result_evidence.active_scope_may_remain is False
    assert ended.result_evidence.residue_resolved_by == "exact-stop"
    with world.sessions() as session:
        order = session.get(Job, ended.operation_id)
        assert order is not None and order.state in ENDED
        assert all(
            state in ENDED
            for state in session.scalars(
                select(AgentOperation.state).where(
                    AgentOperation.parent_job_id == order.id
                )
            )
        )


def test_a_spark_restart_mid_job_keeps_the_job_lost_and_the_member(
    tmp_path: Path, postgres_engine
) -> None:
    """The run survives the Spark's restart; the job stays lost, never re-run.

    Nothing receipted the claimed job's end, so it may still run: past its
    accepted window it is observed with its Stop advertised, never failed by
    that window and never run again. The member reads running on the same run
    when the Spark is back, and self-healing leaves it alone.
    """

    world = build_world(
        tmp_path, postgres_engine, recipe_transform=_configure_artifact_recipe
    )
    try:
        sim = Simulation(world, seed=0)
        sim.round()
        member, run_id, jobs, job_id, loads = _job_in_flight_on_an_offline_spark(
            tmp_path, world, sim
        )

        lost = jobs.get(job_id)
        assert lost.state == ajs.OBSERVING and lost.run_id == run_id
        assert lost.supported_actions == ("stop",)
        assert lost.result_evidence is not None
        assert lost.result_evidence.failure_kind == "agent-lease-expired"
        assert lost.result_evidence.active_scope_may_remain is True
        assert world.controller.profiles.get(member).assignments[0].observed_state == (
            "Running"
        )
        assert world.controller.profiles._reconcile_selected_roster(world.clock()) is (
            False
        )
        with world.sessions() as session:
            run = session.get(RecipeRun, run_id)
            assert run is not None and run.state == "running"
            assert (
                session.scalars(
                    select(func.count()).select_from(FleetProfileApplication)
                ).one()
                == loads
            )
            assert (
                session.scalars(
                    select(func.count())
                    .select_from(AgentOperation)
                    .where(AgentOperation.kind == "recipe.job.run.v1")
                ).one()
                == 1
            )
    finally:
        world.close()


def test_a_job_member_whose_run_is_stopped_after_a_lost_job_is_reactivated(
    tmp_path: Path, postgres_engine, monkeypatch
) -> None:
    """The newest submission stops the run a lost job may still occupy.

    The job's effect is in doubt, so the next submission drives the run's exact
    Stop. The selected profile then activates its member again as a new run,
    with no operator action, and the lost job is never run again.
    """

    monkeypatch.setattr(
        "vonk_control.artifact_jobs.service.bounded_attempts",
        lambda: iter(range(2)),
    )
    world = build_world(
        tmp_path, postgres_engine, recipe_transform=_configure_artifact_recipe
    )
    try:
        sim = Simulation(world, seed=0, sparks_type=_AgentSparks)
        sim.round()
        member, lost, jobs, job_id, loads = _job_in_flight_on_an_offline_spark(
            tmp_path, world, sim
        )
        newer = submitted_artifact_job(jobs, lost, request_suffix=710)
        assert newer.operation_id is None

        current = None
        for _ in range(30):
            sim.round()
            with world.sessions() as session:
                current = session.scalar(
                    select(RecipeRun.id).where(
                        RecipeRun.state == "running", RecipeRun.id != lost
                    )
                )
            if current is not None:
                break

        assert current is not None
        assert world.controller.profiles.get(member).assignments[0].observed_state == (
            "Running"
        )
        with world.sessions() as session:
            stopped = session.get(RecipeRun, lost)
            assert stopped is not None and stopped.state == "stopped"
            stops = {
                stop.payload["execution_mode"]: stop.state
                for stop in session.scalars(
                    select(Job).where(
                        Job.kind == "recipe.stop",
                        Job.payload["owner_id"].as_string() == lost,
                    )
                )
            }
            # The submission asked for the run's Stop; the profile's newer
            # intent stopped the lost job's exact runtime.
            assert "one-shot-jobs" in stops
            assert stops["profile-jobrun-stop"] == "succeeded"
            assert (
                session.scalars(
                    select(func.count()).select_from(FleetProfileApplication)
                ).one()
                == loads + 1
            )
            activations = session.scalars(
                select(Job.payload["owner_id"].as_string()).where(
                    Job.kind == _ACTIVATION
                )
            ).all()
            assert sorted(activations) == sorted([lost, current])
            assert (
                session.scalars(
                    select(func.count())
                    .select_from(AgentOperation)
                    .where(AgentOperation.kind == "recipe.job.run.v1")
                ).one()
                == 1
            )
        _assert_ended_and_stopped_exactly(world, jobs, job_id)
    finally:
        world.close()


def test_a_switch_from_a_job_member_with_a_lost_job_stops_it_and_serves_the_new_member(
    tmp_path: Path, postgres_engine
) -> None:
    """A load that replaces a job member on its Spark stops it, then serves.

    The member's Spark restarted mid-job, so the job's effect is in doubt. The
    operator then loads another member on the same Spark: the new load's own
    run-switch stops the old run's exact job runtime before it starts the new
    member, and the new member serves.

    Wrong implementation: the exact JobRun Stop looked for its owner only
    among the profile's own Stop children, never in the run-switch that stops
    the run first, so the load retried the unreadable owner forever and the
    old run kept the Spark.
    """

    world = build_world(
        tmp_path, postgres_engine, recipe_transform=_configure_artifact_recipe
    )
    try:
        sim = Simulation(world, seed=0, sparks_type=_AgentSparks)
        sim.round()
        _member, lost, jobs, job_id, _loads = _job_in_flight_on_an_offline_spark(
            tmp_path, world, sim
        )
        spark = world.nodes[0]
        other, _number = world.create_profile(
            "Other member", [world.assignment(SOLO_B, (spark,))]
        )

        load = _load(world, other)

        assert sim.settle(load) == "succeeded", world.controller.profiles.application(
            load
        )
        with world.sessions() as session:
            stopped = session.get(RecipeRun, lost)
            assert stopped is not None and stopped.state == "stopped"
            assert (
                session.scalar(
                    select(ResourceReservation.id).where(
                        ResourceReservation.owner_kind == "run",
                        ResourceReservation.owner_id == lost,
                        ResourceReservation.state == ReservationState.ACTIVE,
                    )
                )
                is None
            )
            # The job's runtime was stopped exactly, under the new load.
            jobrun_stop = session.scalar(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == lost,
                    Job.payload["execution_mode"].as_string() == "profile-jobrun-stop",
                )
            )
            assert jobrun_stop is not None
            assert jobrun_stop.state == "succeeded"
            assert jobrun_stop.payload["profile_application_id"] == load
            (serving,) = session.scalars(
                select(RecipeRun.id).where(RecipeRun.state == "running")
            )
        assert serving != lost
        assert world.controller.profiles.get(other).assignments[0].observed_state == (
            "Running"
        )
        _assert_ended_and_stopped_exactly(world, jobs, job_id)
    finally:
        world.close()


def test_a_submission_while_the_spark_is_offline_waits_and_runs_when_it_returns(
    tmp_path: Path, postgres_engine
) -> None:
    """An offline Spark runs nothing, so a submission waits for it.

    The waiting submission names the Spark, and the newest one supersedes an
    older waiting one. When the Spark is back and its agent asks for work, the
    member serves on the same run and the newest submission runs there once.
    """

    world = build_world(
        tmp_path, postgres_engine, recipe_transform=_configure_artifact_recipe
    )
    try:
        sim = Simulation(world, seed=0)
        sim.round()
        spark = world.nodes[0]
        member, _number = world.create_profile(
            "Job member", [world.assignment(SOLO, (spark,))]
        )
        assert sim.settle(_load(world, member)) == "succeeded"
        with world.sessions() as session:
            (run_id,) = session.scalars(
                select(RecipeRun.id).where(RecipeRun.state == "running")
            )
            host = session.get(AgentNode, spark)
            assert host is not None
            fingerprint = host.preflight_fingerprint
        jobs = ArtifactJobService(
            world.sessions,
            recipe_operations=world.controller.lifecycle,
            blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
            clock=world.clock,
        )
        offline = sim.sparks.by_node[spark]
        offline.down_steps = 1
        for _ in range(8):
            sim.round(600)

        older = submitted_artifact_job(jobs, run_id, request_suffix=720)
        newer = submitted_artifact_job(jobs, run_id, request_suffix=730)

        waiting = f"{ProjectionCode.NODE_OFFLINE.value}: {spark}"
        assert (newer.state, newer.status_reason) == (ajs.QUEUED, waiting)
        superseded = jobs.get(older.id)
        assert superseded.state == ajs.CANCELLED
        assert WaitReason(superseded.status_reason) is WaitReason.SCOPE_CHANGED

        offline.down_steps = 0
        for _ in range(3):
            sim.round()
        claim = claim_agent(
            AgentJobService(world.sessions, clock=world.clock),
            spark,
            "serial-0",
            runtime_identity={
                **PACKAGED_RUNTIME_IDENTITY,
                "architecture": "linux-arm64",
            },
            preflight_fingerprint=fingerprint,
        )

        assert claim is not None
        running = jobs.get(newer.id)
        assert (running.state, running.run_id) == (ajs.RUNNING, run_id)
        assert running.status_reason is None
        assert _finish_artifact_job(jobs, world.sessions, newer.id) == ajs.SUCCEEDED
        assert world.controller.profiles.get(member).assignments[0].observed_state == (
            "Running"
        )
        with world.sessions() as session:
            attempts = dict(
                session.execute(
                    select(ArtifactJob.id, AgentOperation.current_attempt).join(
                        AgentOperation,
                        AgentOperation.parent_job_id == ArtifactJob.operation_id,
                    )
                ).all()
            )
            assert (
                session.scalar(select(Job.id).where(Job.kind == "recipe.stop")) is None
            )
        # The superseded order was never issued; the newest was issued once.
        assert attempts == {older.id: 0, newer.id: 1}
    finally:
        world.close()
