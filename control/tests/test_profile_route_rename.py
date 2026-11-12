"""A profile over an identical running workload renames it; nothing restarts.

Production (2026-11-06, the hardware sweep's restore of the owner's profile):
the owner's assignment equalled the sweep's running GLM run except for the
route name.  The route name is routing metadata, not execution identity, so the
load keeps the run and gives it the profile's name in the admission
transaction.  The route publisher then moves the name in one atomic bundle.

The real Controller services run over PostgreSQL on a fake clock with fake
Sparks (``lifecycle_simulation``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select
from vonk_agent_protocol.route_activation import SupervisorPhase
from vonk_control.litellm import ServedModelInfo
from vonk_control.models import AgentOperation, Job, RecipeRun, ResourceReservation
from vonk_control.recipe_execution_contract import parse_stored_run_plan
from vonk_control.recipe_progress import _parent_identity
from vonk_control.recipe_routes import RecipeRouteNotReady

from .lifecycle_simulation.simulation import Simulation
from .lifecycle_simulation.world import PAIR, SOLO, SOLO_B, World, build_world
from .test_recipe_operations import ConcurrentPublisher

_BLOCKERS = {
    "run.port_occupied",
    "run.rendezvous_port_occupied",
    "run.insufficient_memory",
    "run.insufficient_capacity",
    "run-switch.run_admission_blocked",
}
_PROCESS_ORDERS = ("recipe.start", "recipe.stop")


@dataclass(frozen=True)
class _Run:
    """What a rename must leave alone: the run, its generation, its state."""

    id: str
    run_generation: int
    state: str
    plan_digest: str


class _FlakyPublisher(ConcurrentPublisher):
    """The route publisher, unreachable while ``down``."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False

    def publish_recipe(
        self, candidate, *, required: SupervisorPhase = SupervisorPhase.READY
    ):
        if self.down:
            # As the real activation reports an unreachable gateway.
            raise RecipeRouteNotReady("the route publisher is unreachable")
        return super().publish_recipe(candidate, required=required)


def _load(world: World, profile_id: str) -> str:
    return world.controller.profiles.apply(
        profile_id, request_key=str(uuid.uuid4()), actor="admin"
    ).id


def _profile(
    world: World, name: str, alias: str, recipe: str = SOLO, node: int = 0
) -> tuple[str, int]:
    assignment = world.assignment(recipe, world.nodes[node : node + 1])
    assignment["assignment_name"] = alias
    return world.create_profile(name, [assignment])


def _runs(world: World, alias: str | None = None) -> list[_Run]:
    with world.sessions() as session:
        statement = select(RecipeRun).where(RecipeRun.state == "running")
        if alias is not None:
            statement = statement.where(RecipeRun.alias == alias)
        return [
            _Run(run.id, run.run_generation, run.state, run.plan_digest)
            for run in session.scalars(statement.order_by(RecipeRun.created_at))
        ]


def _process_orders(world: World) -> int:
    """Every start and stop the Controller ever ordered."""

    with world.sessions() as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(AgentOperation)
                .where(AgentOperation.kind.in_(_PROCESS_ORDERS))
            )
            or 0
        )


def _process_jobs(world: World) -> int:
    with world.sessions() as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind.in_(_PROCESS_ORDERS))
            )
            or 0
        )


def _serving(sim: Simulation) -> list[frozenset[str]]:
    """What each fake Spark actually serves."""

    return [frozenset(sim.sparks.spark(index).serving) for index in range(2)]


def _endpoints(world: World, number: int) -> list[tuple[str | None, str, str | None]]:
    """(alias, endpoint state, expected run id) of a profile's assignments."""

    with world.sessions() as session:
        intent = world.controller.profiles.endpoint_intent(session, number)
    assert intent.assignments is not None
    return [
        (item.alias, str(item.state), item.expected_run_id)
        for item in intent.assignments
    ]


def _published(world: World, since: int = 0) -> list[tuple[str, ...]]:
    """The route names of each bundle published since ``since`` bundles."""

    return world.publisher.aliases[since:]


def _settle_routes(sim: Simulation, published: int, rounds: int = 40) -> None:
    """Rounds until the publisher has taken one more bundle."""

    for _ in range(rounds):
        if len(sim.world.publisher.aliases) > published:
            return
        sim.round()
    raise AssertionError("the route publisher took no bundle")


def _identical_runs_loaded(world: World, sim: Simulation, alias: str) -> _Run:
    first, _number = _profile(world, f"Load {alias}", alias)
    assert sim.settle(_load(world, first)) == "succeeded"
    sim.round()
    (run,) = _runs(world, alias)
    return run


@pytest.mark.slow(60)
def test_a_profile_over_an_identical_run_renames_it_with_zero_restarts(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        first, _first_number = _profile(world, "Sweep", "sweep-alias")
        second, second_number = _profile(world, "Owner", "owner-alias")
        assert sim.settle(_load(world, first)) == "succeeded"
        sim.round()
        (before,) = _runs(world, "sweep-alias")
        orders, jobs, serving = (
            _process_orders(world),
            _process_jobs(world),
            _serving(sim),
        )
        bundles = len(world.publisher.aliases)
        assert world.publisher.aliases[-1] == ("sweep-alias",)

        review = world.controller.profiles.preview(second)
        assert review.allowed
        assert not _BLOCKERS & {
            reason.code
            for item in review.assessments
            for reason in item.assessment.blockers
        }
        assert [item.actions for item in review.assignments] == [["keep"]]
        (effect,) = review.effects.runs
        assert (effect.action, effect.run_id) == ("keep", before.id)
        assert (effect.alias, effect.previous_alias) == ("owner-alias", "sweep-alias")

        load = _load(world, second)
        # Liveness: a load never waits on a run its own plan keeps.
        assert sim.settle(load) == "succeeded"
        sim.round()
        sim.round()

        # The same run, untouched: no order, no process change.
        assert _runs(world, "owner-alias") == [before]
        assert _runs(world, "sweep-alias") == []
        assert _process_orders(world) == orders
        assert _process_jobs(world) == jobs
        assert _serving(sim) == serving

        # The name moved in whole bundles: old name, then new name, never both
        # names in one bundle and never neither.
        names = _published(world, bundles)
        assert names and names[-1] == ("owner-alias",)
        assert set(names) <= {("sweep-alias",), ("owner-alias",)}
        assert names == sorted(names, key=lambda item: item != ("sweep-alias",))

        # The profile's endpoint is the renamed run; the run carries its name.
        assert _endpoints(world, second_number) == [
            ("owner-alias", "not-published-yet", before.id)
        ]

        # The same name again is a plain keep: allowed, nothing to do.
        again = world.controller.profiles.preview(second)
        assert again.allowed
        assert [item.actions for item in again.assignments] == [["keep"]]
        assert [item.previous_alias for item in again.effects.runs] == [None]
        assert again.steps == []
    finally:
        world.close()


@pytest.mark.slow(60)
def test_a_controller_restart_mid_rename_publishes_the_new_name_only(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        before = _identical_runs_loaded(world, sim, "sweep-alias")
        orders, jobs = _process_orders(world), _process_jobs(world)
        second, _number = _profile(world, "Owner", "owner-alias")

        # The rename is committed by the load's admission; no route pass ran.
        load = _load(world, second)
        assert world.controller.profiles.application(load).state == "succeeded"
        assert world.publisher.aliases[-1] == ("sweep-alias",)
        world.restart()

        for _ in range(4):
            sim.round()
        assert _runs(world, "owner-alias") == [before]
        # A restarted Controller's publisher has only ever seen the new name.
        assert world.publisher.aliases
        assert set(world.publisher.aliases) == {("owner-alias",)}
        assert (_process_orders(world), _process_jobs(world)) == (orders, jobs)
    finally:
        world.close()


@pytest.mark.slow(60)
def test_a_second_rename_during_a_publisher_outage_publishes_only_the_last_name(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    world.publisher_type = _FlakyPublisher
    world.restart()
    try:
        sim = Simulation(world, seed=0)
        before = _identical_runs_loaded(world, sim, "sweep-alias")
        orders, jobs = _process_orders(world), _process_jobs(world)
        publisher = world.publisher
        assert isinstance(publisher, _FlakyPublisher)
        owner, owner_number = _profile(world, "Owner", "owner-alias")
        third, _third_number = _profile(world, "Third", "third-alias")
        bundles = len(publisher.aliases)

        publisher.down = True
        assert sim.settle(_load(world, owner)) == "succeeded"
        for _ in range(3):
            sim.round()
        # The last good bundle still serves the old name; the new name is not
        # published yet, and the run is untouched.
        assert publisher.aliases[-1] == ("sweep-alias",)
        assert len(publisher.aliases) == bundles
        assert [state for _alias, state, _run in _endpoints(world, owner_number)] != [
            "published"
        ]
        assert _runs(world, "owner-alias") == [before]

        # A newer request renames again while the publisher is still down.
        assert sim.settle(_load(world, third)) == "succeeded"
        assert _runs(world, "third-alias") == [before]
        publisher.down = False
        _settle_routes(sim, bundles)
        sim.round()

        added = _published(world, bundles)
        assert added and set(added) == {("third-alias",)}
        assert ("owner-alias",) not in publisher.aliases
        assert (_process_orders(world), _process_jobs(world)) == (orders, jobs)
    finally:
        world.close()


@pytest.mark.slow(60)
def test_the_original_start_key_replays_after_a_rename(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        before = _identical_runs_loaded(world, sim, "sweep-alias")
        with world.sessions() as session:
            run = session.get(RecipeRun, before.id)
            assert run is not None
            (start,) = (
                job
                for job in session.scalars(
                    select(Job).where(Job.kind == "recipe.start")
                )
                if _parent_identity(job, "owner_id") == run.id
            )
            installation_id, request_id = run.installation_id, start.request_id
        orders, jobs = _process_orders(world), _process_jobs(world)

        second, _number = _profile(world, "Owner", "owner-alias")
        assert sim.settle(_load(world, second)) == "succeeded"
        assert _runs(world, "owner-alias") == [before]

        lifecycle = world.controller.lifecycle
        adopted = lifecycle.adopt_start(installation_id, request_id=request_id)
        assert adopted is not None and adopted.owner_id == before.id
        with world.sessions() as session:
            replayed = lifecycle._idempotent_job_in_session(
                session,
                request_id,
                "recipe.start",
                before.plan_digest,
                owner_kind="run",
                owner_id=before.id,
                installation_id=installation_id,
            )
        assert replayed is not None and replayed.id == start.id
        assert (_process_orders(world), _process_jobs(world)) == (orders, jobs)
    finally:
        world.close()


@pytest.mark.slow(60)
def test_crashed_rank_recovery_authority_holds_after_a_rename(
    tmp_path, postgres_engine
) -> None:
    from vonk_control.distributed_recovery.authority import _recovery_authority
    from vonk_control.distributed_recovery.common import _RecoveryAuthority

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        assignment = world.assignment(PAIR, world.nodes)
        assignment["assignment_name"] = "pair-old"
        old, _number = world.create_profile("Pair old", [assignment])
        renamed = {**assignment, "assignment_name": "pair-new"}
        new, _number = world.create_profile("Pair new", [renamed])
        assert sim.settle(_load(world, old)) == "succeeded"
        sim.round()
        (before,) = _runs(world, "pair-old")
        assert sim.settle(_load(world, new)) == "succeeded"
        assert _runs(world, "pair-new") == [before]

        with world.sessions() as session:
            run = session.get(RecipeRun, before.id)
            assert run is not None
            authority = _recovery_authority(
                session,
                run,
                world.clock(),
                1,
                stop_run_generation=run.run_generation,
                start_timeout_seconds=60,
            )
        assert isinstance(authority, _RecoveryAuthority)
    finally:
        world.close()


@pytest.mark.slow(60)
def test_new_run_plans_carry_no_alias_and_keep_digests_and_routes(
    tmp_path, postgres_engine
) -> None:
    from vonk_control.recipe_execution_contract import RecipeExecutionContractError

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        before = _identical_runs_loaded(world, sim, "sweep-alias")

        def snapshot() -> tuple[object, ...]:
            with world.sessions() as session:
                run = session.get(RecipeRun, before.id)
                assert run is not None
                candidate = world.routes.candidate_in_session(
                    session,
                    include_run_id=None,
                    exclude_run_ids=frozenset(),
                    lock=False,
                    served=ServedModelInfo(),
                )
                reservations = sorted(
                    (item.id, item.resource_key, item.plan_digest)
                    for item in session.scalars(select(ResourceReservation))
                )
                return (run.plan_digest, candidate.state.digest, reservations)

        known = snapshot()
        # A new run's plan never carries the route name, and the strict plan
        # contract refuses one that does.
        with world.sessions() as session:
            run = session.get(RecipeRun, before.id)
            assert run is not None and "alias" not in run.plan
            parse_stored_run_plan(run.plan)
            with pytest.raises(RecipeExecutionContractError):
                parse_stored_run_plan({**run.plan, "alias": run.alias})
        # Digests are frozen: nothing recomputes or republishes.
        assert snapshot() == known
        bundles = len(world.publisher.aliases)
        for _ in range(3):
            sim.round()
        assert set(world.publisher.aliases[bundles:]) <= {("sweep-alias",)}
    finally:
        world.close()


@pytest.mark.slow(60)
@pytest.mark.parametrize(
    ("recipe", "node"), [(SOLO_B, 0), (SOLO, 1)], ids=["same-spark", "other-spark"]
)
def test_a_rename_owns_the_name_over_a_newer_run_the_same_plan_stops(
    tmp_path, postgres_engine, recipe: str, node: int
) -> None:
    """The plan keeps and renames R and stops X, which still holds the name and
    was created after R.  Ordering by creation would leave X owning the name
    until its stop lands; the newest accepted request owns it."""

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        before = _identical_runs_loaded(world, sim, "sweep-alias")
        both, _number = world.create_profile(
            "Both",
            [
                {
                    **world.assignment(SOLO, world.nodes[:1]),
                    "assignment_name": "sweep-alias",
                },
                {
                    **world.assignment(recipe, world.nodes[node : node + 1]),
                    "assignment_name": "coder",
                },
            ],
        )
        assert sim.settle(_load(world, both)) == "succeeded"
        sim.round()
        (newer,) = _runs(world, "coder")
        assert newer.id != before.id

        # Node 0 only: the plan stops the holder (same Spark or another one).
        rename, _number = _profile(world, "Rename", "coder")
        load = _load(world, rename)
        # Admission renamed R and queued X's stop; no round has run yet.
        assert world.controller.profiles.application(load).state != "failed"
        with world.sessions() as session:
            candidate = world.routes.candidate_in_session(
                session,
                include_run_id=None,
                exclude_run_ids=frozenset(),
                lock=False,
                served=ServedModelInfo(),
            )
            older = session.get(RecipeRun, before.id)
            holder = session.get(RecipeRun, newer.id)
            assert older is not None and holder is not None
            assert older.alias == holder.alias == "coder"
            assert holder.created_at > older.created_at
            assert older.renamed_at is not None and holder.renamed_at is None
            assert older.id in candidate.included
            assert holder.id not in candidate.included
        assert sim.settle(load) == "succeeded"
        assert [item.id for item in _runs(world, "coder")] == [before.id]
    finally:
        world.close()


def _coordinator(world: World):
    from vonk_control.agent_jobs import AgentJobService
    from vonk_control.distributed_recovery import DistributedRecoveryCoordinator

    return DistributedRecoveryCoordinator(
        world.sessions,
        routes=world.routes,
        agent_jobs=AgentJobService(world.sessions, clock=world.clock),
        clock=world.clock,
        recovery_run_stops=world.controller.lifecycle,
        start_timeout_seconds=60,
    )


def _recover(world: World, sim: Simulation, run_id: str, rounds: int = 80) -> None:
    """A rank exits; recovery proves the run's authority and queues its own Stop.

    (The simulation's Sparks do not model the following next-generation Start;
    that leg is proven on the wire in ``test_recipe_observation_wire_bridge``.)
    """

    from vonk_control.models import RunNode

    with world.sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        rank = session.scalars(
            select(RunNode).where(RunNode.run_id == run_id, RunNode.rank == 1)
        ).one()
        rank.state = "failed"
        rank.observed_run_generation = run.run_generation
        rank.observation_process_running = False
        rank.observation_endpoint_ready = False
        rank.observation_observed_at = world.clock()
        sim.sparks.spark(1).serving.discard(run_id)
    coordinator = _coordinator(world)
    for _ in range(rounds):
        # Only recovery acts here: the profile worker would meet the failed rank
        # first and stop the whole run its own way.
        world.advance(30)
        coordinator.tick()
        with world.sessions() as session:
            stops = [
                job
                for job in session.scalars(select(Job).where(Job.kind == "recipe.stop"))
                if job.payload.get("recovery") is not None
                and _parent_identity(job, "owner_id") == run_id
            ]
        if stops:
            return
    raise AssertionError("recovery did not queue the run's own Stop")


def _renamed_pair(world: World, sim: Simulation) -> _Run:
    assignment = world.assignment(PAIR, world.nodes)
    assignment["assignment_name"] = "pair-old"
    old, _number = world.create_profile("Pair old", [assignment])
    new, _number = world.create_profile(
        "Pair new", [{**assignment, "assignment_name": "pair-new"}]
    )
    assert sim.settle(_load(world, old)) == "succeeded"
    sim.round()
    (before,) = _runs(world, "pair-old")
    assert sim.settle(_load(world, new)) == "succeeded"
    sim.round()
    assert _runs(world, "pair-new") == [before]
    return before


@pytest.mark.slow(60)
def test_a_crashed_rank_is_recovered_under_the_new_name(
    tmp_path, postgres_engine
) -> None:
    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        before = _renamed_pair(world, sim)
        bundles = len(world.publisher.aliases)
        _recover(world, sim, before.id)
        # The route stays under the new name: the old one is never republished.
        assert ("pair-old",) not in world.publisher.aliases[bundles:]
        with world.sessions() as session:
            run = session.get(RecipeRun, before.id)
            assert run is not None and run.alias == "pair-new"
    finally:
        world.close()


@pytest.mark.slow(60)
def test_a_wait_remembered_by_an_older_reader_is_cleared_and_recovery_resumes(
    tmp_path, postgres_engine, monkeypatch
) -> None:
    """The old worker, still running while the new API strips the plans, cannot
    read them and remembers 'unreadable' by the bytes alone.  This release reads
    the plan again once and clears that wait."""

    from hashlib import sha256

    from vonk_agent_protocol import canonical_message
    from vonk_control.distributed_recovery import common
    from vonk_control.persisted_waits import read_wait

    world = build_world(tmp_path, postgres_engine)
    try:
        sim = Simulation(world, seed=0)
        before = _renamed_pair(world, sim)
        with world.sessions.begin() as session:
            run = session.get(RecipeRun, before.id)
            assert run is not None
            with monkeypatch.context() as old:
                old.setattr(
                    common,
                    "_plan_identity",
                    lambda item: sha256(canonical_message(item.plan)).hexdigest(),
                )
                assert common._wait_for_plan_change(run, world.clock())
            assert read_wait(run) is not None and run.route_error
            # The remembered outcome belongs to the old reader, not to this one.
            assert not common._unchanged_unreadable(run)

        _coordinator(world).tick()
        with world.sessions() as session:
            run = session.get(RecipeRun, before.id)
            assert run is not None
            assert read_wait(run) is None and run.route_error is None

        _recover(world, sim, before.id)
    finally:
        world.close()
