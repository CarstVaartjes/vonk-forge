"""A run no live work owns leaves its in-flight state by itself.

A Start that is superseded or cancelled before any Spark claims it, or a Stop
that ends without a result, skips the result projection that would move the
run. The recovery tick finds the run by its state: a never-launched run is
``stopped`` and its claims are released; any other is stopped by its exact
Stop. A wrong implementation that keeps the claims, stops a run live work still
owns, undoes a run another writer just took over, or leaves it for a later
load fails here.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete, event, insert, select, update
from vonk_agent_protocol import RunState, WaitReason
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_jobs.endings import end_unobserved_order
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator, run_reaper
from vonk_control.lifecycle.agent_operation import AgentOperationAdapter
from vonk_control.models import (
    IN_FLIGHT_RUN_STATES,
    AgentOperation,
    Job,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.owner_liveness import OWNER_SETTLE_GRACE, runs_named_by_live_work

from .test_recipe_operations import (
    NOW,
    ConcurrentPublisher,
    bind_route_publications,
    installed_recipe,
    setup_services,
    started_recipe,
)

AFTER_GRACE = NOW + OWNER_SETTLE_GRACE + timedelta(minutes=1)


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def world(request, tmp_path) -> _World:
    engine = (
        request.getfixturevalue("postgres_engine")
        if request.param == "postgres"
        else None
    )
    return _World(tmp_path, engine)


class _World:
    def __init__(self, tmp_path, engine=None, *, nodes: int = 1) -> None:
        self.sessions, service, self.queue, self.mapping, self.build, self.nodes = (
            setup_services(tmp_path, nodes=nodes, engine=engine)
        )
        self.service, self.routes = bind_route_publications(
            self.sessions, service, ConcurrentPublisher()
        )
        # A second, unused installation of the fleet: what a person or the
        # storage sweep removes, which takes a new workload intent.
        self.unused = installed_recipe(
            self.service, self.mapping, self.build, self.nodes, request_id="j" * 36
        )
        self.orders = AgentJobService(
            self.sessions,
            clock=lambda: NOW,
            result_consumer=self.service.consume_agent_result,
        )
        self.installation = installed_recipe(
            self.service, self.mapping, self.build, self.nodes, request_id="i" * 36
        )

    def start(self, request_id: str = "r" * 36):
        """A Start whose orders no Spark has claimed: the run is ``starting``."""

        plan = self.service.preview_run(self.installation.owner_id, "qwen")
        return self.service.start(
            plan, plan_digest=plan.plan_digest, actor="admin", request_id=request_id
        )

    def tick(self, now=AFTER_GRACE) -> bool:
        return DistributedRecoveryCoordinator(
            self.sessions,
            routes=self.routes,
            agent_jobs=self.queue,
            clock=lambda: now,
            run_endings=self.service,
        ).tick()

    def run(self, run_id: str) -> RecipeRun:
        with self.sessions() as session:
            run = session.get(RecipeRun, run_id)
            assert run is not None
            session.expunge(run)
            return run

    def claims(self, run_id: str) -> int:
        with self.sessions() as session:
            return len(
                session.scalars(
                    select(ResourceReservation.id).where(
                        ResourceReservation.owner_kind == "run",
                        ResourceReservation.owner_id == run_id,
                        ResourceReservation.state == "active",
                    )
                ).all()
            )

    def stops(self, run_id: str) -> list[Job]:
        with self.sessions() as session:
            return [
                job
                for job in session.scalars(select(Job).where(Job.kind == "recipe.stop"))
                if job.payload.get("owner_id") == run_id
            ]

    def fresh_start_is_admitted(self) -> None:
        assert self.start(str(uuid.uuid4())).state in {"queued", "running"}


def _supersede_by_removal(world: _World) -> None:
    """Removing the unused installation takes a newer intent: the unissued
    Start is settled cancelled, as a production uninstall does."""

    world.service.uninstall(
        world.unused.owner_id,
        actor="admin",
        request_id=str(uuid.uuid4()),
    )


def _cancel(world: _World, start) -> None:
    world.service.cancel(
        start.id, actor="admin", request_id=str(uuid.uuid4()), reason="cancelled"
    )


@pytest.mark.parametrize("ending", ["supersede", "cancel"])
def test_a_start_ended_before_any_claim_stops_its_run_and_frees_it(
    world, ending
) -> None:
    start = world.start()
    run_id = start.owner_id
    assert world.run(run_id).state == "starting"
    if ending == "supersede":
        _supersede_by_removal(world)
    else:
        _cancel(world, start)
    with world.sessions() as session:
        ended = session.get(Job, start.id)
        assert ended is not None and ended.state == "cancelled"
    # The ending skipped the projection: the run is still waiting on it.
    assert world.run(run_id).state == "starting"
    assert world.claims(run_id) > 0

    assert not world.tick(NOW + timedelta(minutes=1))  # inside the settle grace
    assert world.run(run_id).state == "starting"
    assert world.tick()
    assert world.run(run_id).state == "stopped"
    assert world.claims(run_id) == 0
    assert not world.stops(run_id)  # nothing launched: no Spark round trip
    assert world.tick() is False
    world.fresh_start_is_admitted()


def test_a_run_ended_beside_another_serving_run_is_reaped_and_the_other_kept(
    world: _World,
) -> None:
    """The Spark's report is not empty: it names the run serving beside the
    ended Start, so no absence of the Start is ever inferred from it."""

    beside = installed_recipe(
        world.service, world.mapping, world.build, world.nodes, request_id="k" * 36
    )
    serving = started_recipe(
        world.sessions, world.service, beside.owner_id, world.nodes, request_id="s" * 36
    )
    start = world.start()
    _cancel(world, start)

    assert world.tick()
    assert world.run(start.owner_id).state == "stopped"
    assert world.claims(start.owner_id) == 0
    assert world.run(serving.owner_id).state == "running"
    assert world.claims(serving.owner_id) > 0
    world.fresh_start_is_admitted()


def test_a_run_live_work_names_is_left_alone(world: _World) -> None:
    start = world.start()
    assert not world.tick()
    assert world.run(start.owner_id).state == "starting"
    assert world.claims(start.owner_id) > 0


def _end_unobserved(world: _World, job_id: str, *, claimed: bool) -> None:
    """The Controller's own ending of an order whose Spark is gone or revoked
    (``end_unobserved_order``, as the order reconcile pass issues it): the order
    and its parent end, and no result projection runs."""

    with world.sessions.begin() as session:
        parent = session.get(Job, job_id)
        assert parent is not None
        for operation in session.scalars(
            select(AgentOperation).where(AgentOperation.parent_job_id == job_id)
        ):
            if claimed:
                operation.current_attempt = 1  # a Spark claimed it before it went
            end_unobserved_order(
                world.orders,
                session,
                operation,
                AgentOperationAdapter.attempt_of(session, operation),
                parent,
                NOW,
                reason=WaitReason.SCOPE_CHANGED,
                note="agent target is gone or revoked",
            )


def _claimed_then_ended_unobserved(world: _World, start) -> None:
    """A Spark claimed the Start, then the order ended with no result."""

    _end_unobserved(world, start.id, claimed=True)


def test_a_claimed_start_ending_unobserved_gets_its_exact_stop(world: _World) -> None:
    start = world.start()
    _claimed_then_ended_unobserved(world, start)

    assert world.tick()
    run = world.run(start.owner_id)
    assert run.state == "stopping"
    (stop,) = world.stops(start.owner_id)
    assert stop.state in {"queued", "running"}
    assert world.claims(start.owner_id) > 0  # held until the Stop lands
    # A restarted worker finds the Stop its predecessor queued, not another.
    assert world.tick() is False
    assert len(world.stops(start.owner_id)) == 1
    # The Spark proves the never-launched run absent: the Stop's result settles it.
    for node_id in world.nodes:
        world.service.record_node_result(stop.id, node_id, succeeded=True, evidence={})
    assert world.run(start.owner_id).state == "stopped"
    assert world.claims(start.owner_id) == 0
    world.fresh_start_is_admitted()


def test_a_stop_that_ended_unobserved_is_issued_again(world: _World) -> None:
    start = world.start()
    _claimed_then_ended_unobserved(world, start)
    world.tick()
    (first,) = world.stops(start.owner_id)
    _end_unobserved(world, first.id, claimed=False)

    assert world.tick(AFTER_GRACE + OWNER_SETTLE_GRACE * 2)
    assert world.run(start.owner_id).state == "stopping"
    assert len(world.stops(start.owner_id)) == 2


def test_a_stop_whose_job_was_pruned_is_issued_again(world: _World) -> None:
    start = world.start()
    _claimed_then_ended_unobserved(world, start)
    world.tick()
    (first,) = world.stops(start.owner_id)
    with world.sessions.begin() as session:
        # Retention removes an ended job with its orders.
        for job_id in (first.id, start.id):
            session.execute(
                delete(AgentOperation).where(AgentOperation.parent_job_id == job_id)
            )
            session.execute(delete(Job).where(Job.id == job_id))

    assert world.tick(AFTER_GRACE + OWNER_SETTLE_GRACE * 2)
    assert len(world.stops(start.owner_id)) == 1  # the pruned one is gone, a new one


def test_a_deferred_offline_stop_order_owns_the_run_until_it_ends(
    world: _World,
) -> None:
    start = world.start()
    _claimed_then_ended_unobserved(world, start)
    world.tick()
    (stop,) = world.stops(start.owner_id)
    # The order outlives its parent operation: deferred for an offline Spark.
    with world.sessions.begin() as session:
        session.execute(update(Job).where(Job.id == stop.id).values(state="succeeded"))
    later = AFTER_GRACE + OWNER_SETTLE_GRACE * 2
    assert not world.tick(later)
    assert len(world.stops(start.owner_id)) == 1
    with world.sessions.begin() as session:
        session.execute(
            update(AgentOperation)
            .where(AgentOperation.parent_job_id == stop.id)
            .values(state="cancelled")
        )
    assert world.tick(later + OWNER_SETTLE_GRACE * 2)
    assert len(world.stops(start.owner_id)) == 2


def test_a_run_another_writer_took_over_is_not_undone(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = world.start()
    _cancel(world, start)
    named = run_reaper.runs_named_by_live_work

    def named_then_taken_over(session, run_ids):
        found = named(session, run_ids)
        # A recovery restart or a new Stop commits between the read and the move.
        with world.sessions.begin() as other:
            other.execute(
                update(RecipeRun)
                .where(RecipeRun.id == start.owner_id)
                .values(version=RecipeRun.version + 1)
            )
        return found

    monkeypatch.setattr(run_reaper, "runs_named_by_live_work", named_then_taken_over)
    assert not world.tick()
    assert world.run(start.owner_id).state == "starting"
    assert world.claims(start.owner_id) > 0
    monkeypatch.undo()
    assert world.tick()  # the next tick reads the new version and ends it
    assert world.run(start.owner_id).state == "stopped"
    world.fresh_start_is_admitted()


@pytest.mark.parametrize("state", list(RunState), ids=lambda state: state.value)
def test_every_in_flight_run_state_has_a_way_out_and_no_other_is_touched(
    world: _World, state: RunState
) -> None:
    """Derived from the enum: a state added to ``RunState`` is in flight until
    named otherwise, and then it must leave through the reaper."""

    start = world.start()
    run_id = start.owner_id
    with world.sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = state.value
        # The ending left no job behind (retention pruned it with its orders).
        for job_id in (start.id,):
            session.execute(
                delete(AgentOperation).where(AgentOperation.parent_job_id == job_id)
            )
            session.execute(delete(Job).where(Job.id == job_id))
    in_flight = state in IN_FLIGHT_RUN_STATES

    worked = world.tick()

    after = world.run(run_id).state
    if in_flight:
        assert worked
        assert after == "stopped" or (after == "stopping" and world.stops(run_id))
    else:
        assert after == state.value
        assert not world.stops(run_id)  # settled or unowned-by-design: no Stop


def test_naming_a_run_reads_live_work_only_whatever_history_there_is(
    world: _World,
) -> None:
    """One statement per kind of work, restricted to live states by index, however
    many ended jobs and runs there are: a recovery tick must not scan history."""

    run_ids = [str(uuid.uuid4()) for _ in range(50)]
    with world.sessions.begin() as session:
        session.execute(
            insert(Job),
            [
                {
                    "id": str(uuid.uuid4()),
                    "version": 1,
                    "kind": "recipe.start",
                    "state": "succeeded",
                    "payload": {"owner_kind": "run", "owner_id": run_id},
                    "actor": "admin",
                    "authority_revision": "r" * 64,
                    "targets": [],
                    "payload_digest": "d" * 64,
                    "request_id": str(uuid.uuid4()),
                    "created_at": NOW,
                    "updated_at": NOW,
                }
                for run_id in run_ids * 6
            ],
        )
    statements: list[str] = []

    def record(connection, cursor, statement, *args):
        statements.append(statement)

    with world.sessions() as session:
        event.listen(session.get_bind(), "before_cursor_execute", record)
        try:
            assert runs_named_by_live_work(session, run_ids) == frozenset()
        finally:
            event.remove(session.get_bind(), "before_cursor_execute", record)
    jobs = [text for text in statements if "FROM jobs" in text]
    assert len(statements) <= 2 and len(jobs) == 1
    assert "NOT IN" not in jobs[0]
