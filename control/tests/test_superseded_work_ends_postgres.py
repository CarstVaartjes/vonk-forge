"""Superseded or dead work ends; a newer intent never waits on it.

- A superseded order whose Spark went silent after claiming it is reclaimed
  when its lease lapses, although that Spark never claims again.
- A pair run that lost one rank to a crash, whose Sparks a newer load took, is
  never recorded ``failed`` while its other rank serves: the newer load stops
  that rank and then starts its own run, whether the run was settled before
  or after the load's plan was reviewed.
"""

from __future__ import annotations

import contextlib
import hashlib
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentResult, canonical_message
from vonk_control.agent_jobs import (
    CLAIM_LEASE_SECONDS,
    AgentJobService,
    StaleAgentFence,
)
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
from vonk_control.inventory_repository import (
    MAX_INVENTORY_FUTURE_SKEW,
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Job,
    NodeInventorySnapshot,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.recipe_operations.observation_helpers import (
    prepare_exact_recipe_run_observation_nodes,
)
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import RecipeLifecyclePhaseExecutor

from .recipe_stop_fixtures import recipe_stop_payload
from .runtime_identity_support import claim_agent
from .test_agent_jobs import superseded_order_payload
from .test_agent_jobs_postgres import COMMIT, NODE_B, parent, service  # noqa: F401
from .test_profile_port_claims import _ready_profile
from .test_recipe_operations import (
    ConcurrentPublisher,
    bind_route_publications,
    complete_started_recipe,
    started_recipe,
)
from .test_run_switch_operations import _request


def _newer_parent(sessions, clock, ordinal: int) -> Job:
    job = parent(sessions, clock)
    with sessions.begin() as session:
        stored = session.get(Job, job.id)
        assert stored is not None
        stored.payload = {"workload_intent_ordinal": ordinal}
        stored.payload_digest = hashlib.sha256(
            canonical_message(stored.payload)
        ).hexdigest()
    return job


def test_a_superseded_order_of_a_silent_spark_ends_and_fresh_work_runs(
    service,  # noqa: F811 - the PostgreSQL agent queue fixture
) -> None:
    sessions, clock = service
    jobs = AgentJobService(sessions, clock=clock)
    payload = recipe_stop_payload(NODE_B, plan_digest=COMMIT)
    old_parent = parent(sessions, clock)
    # A launch ends with its superseded intent; a Stop never does (it waits for
    # its Spark: ``test_own_cleanup``).
    old = jobs.enqueue(
        old_parent.id, NODE_B, "recipe.start", COMMIT, superseded_order_payload(clock)
    )
    claimed = claim_agent(jobs, NODE_B, "serial-b")
    assert claimed is not None
    # Spark B goes silent: its report never arrives and it renews nothing.
    # A newer load leaves Spark B idle: it takes the Spark and claims nothing.
    with sessions.begin() as session:
        node = session.get(AgentNode, NODE_B)
        assert node is not None
        node.workload_intent_ordinal = 2
        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, [NODE_B], 2, clock.now
        )

    clock.advance(seconds=CLAIM_LEASE_SECONDS + 1)
    jobs.reconcile_orders()

    with sessions() as session:
        order = session.get(AgentOperation, old.id)
        job = session.get(Job, old_parent.id)
        assert order is not None and job is not None
        assert order.state == "cancelled", order.status_reason
        assert job.state not in {"queued", "running"}, job.status_reason
    # A late report under the lapsed fence never reopens the order.
    late = AgentResult.model_validate(
        {"fence": claimed.fence, "state": "succeeded", "result": {}}
    )
    with contextlib.suppress(StaleAgentFence):
        jobs.record_late_result(late)
    with sessions() as session:
        order = session.get(AgentOperation, old.id)
        assert order is not None and order.state == "cancelled"
    # Fresh work of the newer intent on that Spark is claimed at once.
    fresh_parent = _newer_parent(sessions, clock, 2)
    fresh = jobs.enqueue(fresh_parent.id, NODE_B, "recipe.stop", COMMIT, payload)
    again = claim_agent(jobs, NODE_B, "serial-b")
    assert again is not None and again.fence != claimed.fence
    with sessions() as session:
        claimed_order = session.scalar(
            select(AgentOperation).where(AgentOperation.state == "running")
        )
        assert claimed_order is not None and claimed_order.id == fresh.id


def _ranks(sessions, run_id: str) -> dict[str, str]:
    with sessions() as session:
        return {
            rank.node_id: rank.state
            for rank in session.scalars(select(RunNode).where(RunNode.run_id == run_id))
        }


def _run_state(sessions, run_id: str) -> str:
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        return run.state


@pytest.mark.parametrize("settled", ["before review", "after review"])
def test_a_pair_run_that_lost_a_rank_is_stopped_by_the_newer_load(
    tmp_path, postgres_engine, settled: str
) -> None:
    sessions, _profiles, planner, _profile, _api, _headers, nodes, installation_id = (
        _ready_profile(tmp_path, postgres_engine, node_count=2)
    )
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    old = started_recipe(
        sessions, lifecycle, installation_id, nodes, request_id=str(uuid.uuid4())
    )
    clock = [lifecycle._clock() + timedelta(seconds=1)]
    lifecycle._clock = planner._clock = lambda: clock[0]
    executor = planner._phase_executor
    assert isinstance(executor, RecipeLifecyclePhaseExecutor)
    executor._clock = lambda: clock[0]
    now = clock[0]
    # Spark 2's processes die: its complete report names no run.
    with sessions.begin() as session:
        prepare_exact_recipe_run_observation_nodes(session, nodes[1], now, set())
    assert _ranks(sessions, old.owner_id)[nodes[0]] == "running"
    _routes_service, routes = bind_route_publications(
        sessions, lifecycle, ConcurrentPublisher()
    )
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=lifecycle._agent_jobs, clock=lambda: now
    )
    request = _request(sessions, nodes[0], action="switch").model_copy(
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

    def newer_intent() -> None:
        with sessions.begin() as session:
            for agent in session.scalars(select(AgentNode)):
                agent.workload_intent_ordinal += 1

    if settled == "before review":
        newer_intent()
        assert recovery.tick()
    review = planner.preview(request, actor="admin")
    assert review.allowed, review.model_dump(mode="json")
    assert [stop.run_id for stop in review.stops] == [old.owner_id]
    accepted = planner.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=review.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    if settled == "after review":
        assert recovery.tick()
    # Never failed while a rank serves: lost, stoppable, still holding claims.
    assert _run_state(sessions, old.owner_id) == "lost"
    with sessions() as session:
        assert session.scalar(
            select(ResourceReservation.id).where(
                ResourceReservation.owner_id == old.owner_id,
                ResourceReservation.state == "active",
            )
        )

    stop_id = None
    for _ in range(20):
        clock[0] += timedelta(seconds=30)
        planner.tick()
        with sessions() as session:
            stop_id = session.scalar(select(Job.id).where(Job.kind == "recipe.stop"))
        if stop_id is not None:
            break
    assert stop_id is not None, planner.get(accepted.operation_id).result
    stopped_nodes: set[str] = set()
    while lifecycle.get(stop_id).state == "running":
        with sessions() as session:
            queued = set(
                session.scalars(
                    select(AgentOperation.node_id).where(
                        AgentOperation.parent_job_id == stop_id,
                        AgentOperation.state == "queued",
                    )
                )
            )
        assert queued
        for node_id in sorted(queued):
            lifecycle.record_node_result(stop_id, node_id, succeeded=True, evidence={})
        stopped_nodes |= queued
    assert nodes[0] in stopped_nodes  # the surviving rank is stopped
    assert _run_state(sessions, old.owner_id) == "stopped"
    assert set(_ranks(sessions, old.owner_id).values()) == {"stopped"}

    # Each Spark's next inventory shows the stop; the replacement then starts.
    with sessions() as session:
        samples = [
            {
                field: getattr(snapshot, field)
                for field in NodeInventoryReport.model_fields
                if field != "schema_version"
            }
            for snapshot in session.scalars(select(NodeInventorySnapshot))
        ]
    clock[0] += MAX_INVENTORY_FUTURE_SKEW + timedelta(seconds=6)
    repository = InventoryRepository(sessions, clock=lambda: clock[0])
    for values in samples:
        repository.record(
            NodeInventoryReport.model_validate_json(
                canonical_message(
                    {
                        **values,
                        "observed_at": clock[0],
                        "schema_version": 1,
                        "nvidia_driver_version": "unknown",
                        "container_runtime_version": "unknown",
                    }
                )
            )
        )
    replacement = None
    for _ in range(10):
        due = planner.get(accepted.operation_id).next_attempt_at
        if due is not None:
            clock[0] = max(clock[0], due)
        planner.tick()
        with sessions() as session:
            replacement = session.scalar(
                select(Job).where(Job.kind == "recipe.start", Job.id != old.id)
            )
        if replacement is not None:
            break
    assert replacement is not None, planner.get(accepted.operation_id).result
    complete_started_recipe(sessions, lifecycle, replacement.id)
    replacement_run = replacement.payload["owner_id"]
    assert isinstance(replacement_run, str)
    assert set(_ranks(sessions, replacement_run).values()) == {"running"}
