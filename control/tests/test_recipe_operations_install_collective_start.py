"""Recipe operations: install collective start."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from vonk_agent_protocol.agent_operations import AgentInstallResult
from vonk_agent_protocol.agent_operations import AgentOperation as WireAgentOperation
from vonk_agent_protocol.contracts import AgentResult
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.recipe_operations import RecipeStartPayload, RecipeStopPayload
from vonk_agent_protocol.state_machines import RouteState, RunState
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.models.execution import (
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.recipe_operations.lifecycle_contract import (
    LifecycleCodeFailureResult,
    RecipeOperationProgressResult,
    RecipeOperationResult,
)
from vonk_control.recipe_operations.operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations.service import RecipeOperationService

from .node_contacts import contact_row
from .recipe_operations_support import (
    NOW,
    ConcurrentPublisher,
    _job_phases,
    _placement,
    _required,
    bind_route_publications,
    installed_recipe,
    record_exact_empty_snapshot,
    setup_services,
    start_evidence,
    started_recipe,
)
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    claim_agent,
)


def test_install_replay_is_bound_to_mapping_and_build_identity(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, _nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping_id, build_id)
    operation = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="7" * 36
    )
    assert (
        service.install(
            plan, plan_digest=plan.plan_digest, actor="admin", request_id="7" * 36
        )
        == operation
    )
    for other in (
        replace(plan, mapping_id=str(uuid.uuid4())),
        replace(plan, recipe_build_id=str(uuid.uuid4())),
    ):
        with pytest.raises(Exception) as _ending:
            service.install(
                other,
                plan_digest=plan.plan_digest,
                actor="admin",
                request_id="7" * 36,
            )
    with sessions() as session:
        assert (
            len(list(session.scalars(select(Job).where(Job.kind == "recipe.install"))))
            == 1
        )


def test_install_is_digest_bound_idempotent_and_gang_complete(tmp_path: Path) -> None:
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    operation = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="1" * 36
    )
    repeated = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="1" * 36
    )

    assert repeated == operation
    assert operation.kind == "recipe.install"
    assert operation.state == "running"
    assert queue.available == 1
    with sessions() as session:
        jobs = list(session.scalars(select(Job).where(Job.kind == "recipe.install")))
        child_operations = list(
            session.scalars(
                select(AgentOperation).where(AgentOperation.kind == "recipe.install")
            )
        )
        assert len(jobs) == 1
        assert {item.kind for item in child_operations} == {"recipe.install"}
        assert jobs[0].payload["workload_intent_ordinal"] == 1
        assert all(item.workload_intent_ordinal == 1 for item in child_operations)
        assert all(
            _required(session.get(AgentNode, node_id)).workload_intent_ordinal == 1
            for node_id in nodes
        )
        assert all(
            "shell" not in json.dumps(item.payload).lower() for item in child_operations
        )

    service.record_node_result(
        operation.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    assert service.get(operation.id).state == "running"
    service.record_node_result(
        operation.id, nodes[1], succeeded=True, evidence={"installed_bytes": 120}
    )
    completed = service.get(operation.id)
    assert completed.state == "succeeded"
    result = completed.lifecycle_result
    assert isinstance(result, RecipeOperationResult)
    assert result.successful_nodes == sorted(nodes)
    assert result.failed_nodes == []
    for node_id in nodes:
        evidence = result.node_evidence[node_id]
        assert isinstance(evidence, AgentInstallResult)
        assert evidence.installed_bytes == 120
    with sessions() as session:
        assert (
            _required(session.get(RecipeInstallation, operation.owner_id)).state
            == "installed"
        )


def test_multirank_role_phases_persist_recover_and_stop_in_reverse_order(
    tmp_path: Path,
) -> None:
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=3
    )
    install_plan = service.preview_install(mapping_id, build_id)
    install = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="0" * 36,
    )
    for node_id in nodes:
        service.record_node_result(
            install.id, node_id, succeeded=True, evidence={"installed_bytes": 120}
        )
    run_plan = service.preview_run(install.owner_id, "phased")
    start = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="1" * 36,
    )
    with sessions() as session:
        first = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        assert {_placement(item.payload)["role"] for item in first} == {
            "worker",
            "entrypoint",
        }
        assert len(first) == 3
        for item in first:
            RecipeStartPayload.model_validate(item.payload)
        stored = _required(session.get(Job, start.id))
        assert len(_job_phases(stored.payload)) == 2
    for operation in first:
        service.record_node_result(
            start.id,
            operation.node_id,
            succeeded=True,
            evidence=start_evidence(operation.payload),
        )
    recovered = RecipeOperationService(
        sessions,
        install_admission=service._install_admission,
        run_admission=service._run_admission,
        agent_jobs=queue,
        clock=lambda: NOW,
    )
    with sessions() as session:
        all_children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
        second = tuple(
            item
            for item in all_children
            if item.payload.get("phase") == "collective-readiness"
        )
        assert len(second) == 1
        RecipeStartPayload.model_validate(second[0].payload)
    recovered.record_node_result(
        start.id,
        second[0].node_id,
        succeeded=True,
        evidence=start_evidence(second[0].payload),
    )
    assert recovered.get(start.id).state == "succeeded"

    stop_plan = recovered.preview_stop(start.owner_id)
    stop = recovered.stop(
        start.owner_id,
        plan_digest=stop_plan.plan_digest,
        actor="admin",
        request_id="2" * 36,
    )
    with sessions() as session:
        first_stop = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == stop.id)
            )
        )
        assert [item.node_id for item in first_stop] == [nodes[0]]
        first_stop_payload = RecipeStopPayload.model_validate_json(
            canonical_message(first_stop[0].payload), strict=True
        )
        assert first_stop_payload.run_id == start.owner_id
        assert first_stop_payload.plan_digest == start.plan_digest
        assert first_stop_payload.cancel_pending_start is True
    recovered.record_node_result(
        stop.id, first_stop[0].node_id, succeeded=True, evidence={}
    )
    with sessions() as session:
        all_stop = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == stop.id)
            )
        )
        second_stop = tuple(item for item in all_stop if item.node_id != nodes[0])
        assert len(second_stop) == 2
    for operation in second_stop:
        recovered.record_node_result(
            stop.id, operation.node_id, succeeded=True, evidence={}
        )
    assert recovered.get(stop.id).state == "succeeded"


@pytest.mark.parametrize(
    "start_order",
    [("worker", "entrypoint"), ("entrypoint", "worker")],
)
@pytest.mark.parametrize("startup_budget", [60, 1800])
def test_distributed_start_launches_all_ranks_then_checks_collective(
    tmp_path: Path, start_order: tuple[str, ...], startup_budget: int
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        nodes=2,
        distributed_lifecycle=True,
        start_order=start_order,
        distributed_start_timeout_seconds=startup_budget,
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="p" * 36
    )
    plan = service.preview_run(installation.owner_id, "two-phase")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="q" * 36,
    )

    def children() -> tuple[AgentOperation, ...]:
        with sessions() as session:
            return tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == start.id)
                    .order_by(AgentOperation.created_at, AgentOperation.id)
                )
            )

    launches = children()
    assert len(launches) == 2
    assert all(item.payload["phase"] == "rank-launch" for item in launches)
    assert {_placement(item.payload)["role"] for item in launches} == set(start_order)
    deadline = launches[0].payload["start_deadline"]
    assert all(item.payload["start_deadline"] == deadline for item in launches)
    with sessions() as session:
        job = _required(session.get(Job, start.id))
        assert job.targets == sorted(nodes)
        phase_items = [item for phase in _job_phases(job.payload) for item in phase]
        assert len(phase_items) == 3
        assert len({item["operation_id"] for item in phase_items}) == 3
        assert [
            _placement(item["payload"])["role"] for item in _job_phases(job.payload)[0]
        ] == list(start_order)
        assert job.payload["start_deadline"] == deadline

    assert deadline == (NOW + timedelta(seconds=startup_budget)).isoformat()
    if startup_budget > 60:
        service._clock = lambda: NOW + timedelta(seconds=120)
    first_by_role = {_placement(item.payload)["role"]: item for item in launches}
    for role in start_order:
        launch = first_by_role[role]
        service.record_node_result(
            start.id,
            launch.node_id,
            succeeded=True,
            evidence=start_evidence(launch.payload),
        )

    all_children = children()
    readiness = next(
        item
        for item in all_children
        if item.payload.get("phase") == "collective-readiness"
    )
    assert len(all_children) == 3
    assert _placement(readiness.payload)["role"] == "entrypoint"
    assert readiness.payload["start_deadline"] == deadline
    assert sum(item.node_id == readiness.node_id for item in all_children) == 2
    with sessions() as session:
        run = _required(session.get(RecipeRun, start.owner_id))
        run_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == start.owner_id)
                .order_by(RunNode.rank)
            )
        )
        assert run.state == "starting"
        assert run.route_state == "withdrawn"
        assert [node.state for node in run_nodes] == ["starting", "starting"]
        assert all(node.endpoint is None for node in run_nodes)

    service.record_node_result(
        start.id,
        readiness.node_id,
        succeeded=True,
        evidence=start_evidence(readiness.payload),
    )
    assert service.get(start.id).state == "succeeded"
    with sessions() as session:
        run = _required(session.get(RecipeRun, start.owner_id))
        run_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == start.owner_id)
                .order_by(RunNode.rank)
            )
        )
        assert run.state == "running"
        assert run.route_state == "pending"
        assert [node.state for node in run_nodes] == ["running", "running"]
        assert [node.endpoint is not None for node in run_nodes] == [True, False]
        assert all(node.observed_run_generation is None for node in run_nodes)

    publisher = ConcurrentPublisher()
    _service, routes = bind_route_publications(sessions, service, publisher)
    with pytest.raises(Exception) as _ending:
        routes.publish_run(start.owner_id)
    with sessions.begin() as session:
        owner = _required(
            session.scalar(
                select(RunNode).where(
                    RunNode.run_id == start.owner_id, RunNode.role == "entrypoint"
                )
            )
        )
        owner.observed_run_generation = 1
        owner.observation_observed_at = NOW
        owner.observation_endpoint_ready = True
        owner.updated_at = NOW
    with pytest.raises(Exception) as _ending:
        routes.publish_run(start.owner_id)
    with sessions.begin() as session:
        worker = _required(
            session.scalar(
                select(RunNode).where(
                    RunNode.run_id == start.owner_id, RunNode.role == "worker"
                )
            )
        )
        worker.observed_run_generation = 1
        worker.observation_observed_at = NOW
        worker.observation_endpoint_ready = None
        worker.updated_at = NOW
    routes.publish_run(start.owner_id)
    assert publisher.aliases[-1] == ("two-phase",)
    with sessions() as session:
        assert (
            _required(session.get(RecipeRun, start.owner_id)).observation_deadline_at
            is None
        )


def test_collective_readiness_past_its_budget_fails_the_start_instead_of_waiting(
    tmp_path: Path,
) -> None:
    """A readiness phase whose start budget is spent fails; no operator wait.

    Production: both rank launches succeeded, the owner-only readiness order
    lapsed its lease, was re-issued until the hour of budget was gone, then sat
    in ``waiting-for-operator`` forever (and behind a cancelled parent).  The
    next agent poll must fail it with a named reason and fail the start.
    """

    now = [NOW]
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        nodes=2,
        distributed_lifecycle=True,
        distributed_start_timeout_seconds=3600,
    )
    service._clock = lambda: now[0]
    jobs = AgentJobService(sessions, clock=lambda: now[0])
    jobs.set_result_consumer(service.consume_agent_result)

    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="u" * 36
    )
    plan = service.preview_run(installation.owner_id, "slow-launch")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="v" * 36,
    )

    def children(phase: str) -> tuple[AgentOperation, ...]:
        with sessions() as session:
            rows = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == start.id)
                    .order_by(AgentOperation.created_at, AgentOperation.id)
                )
            )
        return tuple(row for row in rows if row.payload.get("phase") == phase)

    def success(payload: Mapping[str, object]) -> dict[str, object]:
        return start_evidence(payload)

    def claim(node_id: str):
        runtime_identity = dict(PACKAGED_RUNTIME_IDENTITY)
        return claim_agent(
            jobs,
            node_id,
            f"serial-{nodes.index(node_id)}",
            runtime_identity=runtime_identity,
        )

    def complete(claim, result: Mapping[str, object]) -> None:
        jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": claim.fence,
                        "state": "succeeded",
                        "result": result,
                    }
                )
            )
        )

    for launch in children("rank-launch"):
        launch_claim = claim(launch.node_id)
        assert launch_claim is not None
        jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": launch_claim.fence,
                        "state": "succeeded",
                        "result": start_evidence(launch_claim.payload),
                    }
                )
            )
        )

    target = children("collective-readiness")[0]
    assert claim(target.node_id) is not None

    # The budget is spent and the lease lapsed long ago; park it the old way
    # and request cancellation, as the stuck production order was.
    now[0] = NOW + timedelta(seconds=3700)
    with sessions.begin() as session:
        stored = _required(session.get(AgentOperation, target.id))
        stored.state = "backoff"
        stored.next_action_at = now[0]
        parent = _required(session.get(Job, start.id))
        parent.result = {
            **(parent.result or {}),
            "cancel_requested": True,
            "cancel_request_id": "00000000-0000-4000-8000-000000000001",
            "cancel_actor": "controller",
            "cancel_requested_at": now[0].isoformat(),
            "reason": "superseded by newer workload intent",
        }

    cleanup = claim(target.node_id)
    # The failed start queued its cleanup Stop, which the same poll may claim.
    assert cleanup is None or cleanup.operation.value == "recipe.stop"

    with sessions() as session:
        stored = _required(session.get(AgentOperation, target.id))
        assert stored.state == "failed"
        run = _required(session.get(RecipeRun, start.owner_id))
        assert run.state in {"stopping", "failed", "stopped"}
    assert service.get(start.id).state == "failed"


def test_failed_collective_readiness_fails_the_start_at_once(
    tmp_path: Path,
) -> None:
    """An engine that exited before becoming ready ends the start; no reissue.

    Wrong implementation: the definite readiness failure was re-issued for its
    whole start budget, launching the same failing engine again and again.
    """

    now = [NOW]
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        nodes=2,
        distributed_lifecycle=True,
    )
    service._clock = lambda: now[0]
    jobs = AgentJobService(sessions, clock=lambda: now[0])
    jobs.set_result_consumer(service.consume_agent_result)

    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="u" * 36
    )
    plan = service.preview_run(installation.owner_id, "slow-launch")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="v" * 36,
    )

    def children(phase: str) -> tuple[AgentOperation, ...]:
        with sessions() as session:
            rows = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == start.id)
                    .order_by(AgentOperation.created_at, AgentOperation.id)
                )
            )
        return tuple(row for row in rows if row.payload.get("phase") == phase)

    def success(payload: Mapping[str, object]) -> dict[str, object]:
        return start_evidence(payload)

    def claim(node_id: str):
        runtime_identity = dict(PACKAGED_RUNTIME_IDENTITY)
        return claim_agent(
            jobs,
            node_id,
            f"serial-{nodes.index(node_id)}",
            runtime_identity=runtime_identity,
        )

    def complete(claim, result: Mapping[str, object]) -> None:
        jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": claim.fence,
                        "state": "succeeded",
                        "result": result,
                    }
                )
            )
        )

    for launch in children("rank-launch"):
        launch_claim = claim(launch.node_id)
        assert launch_claim is not None
        jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": launch_claim.fence,
                        "state": "succeeded",
                        "result": start_evidence(launch_claim.payload),
                    }
                )
            )
        )

    target = children("collective-readiness")[0]
    readiness_claim = claim(target.node_id)
    assert readiness_claim is not None
    jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": readiness_claim.fence,
                    "state": "failed",
                    "result": {
                        "status": "failed",
                        "error_code": "recipe_start_failed",
                        "reason": "engine exited before becoming ready",
                        "failure_kind": "invalid-contract",
                    },
                }
            )
        )
    )

    with sessions() as session:
        stored = _required(session.get(AgentOperation, target.id))
        assert stored.state == LifecycleState.FAILED
        assert stored.next_action_at is None
    now[0] += timedelta(minutes=5)
    # The launched ranks are stopped; the failed readiness is never reissued.
    after = claim(target.node_id)
    assert after is None or after.operation == WireAgentOperation.RECIPE_STOP
    ended = service.get(start.id)
    assert ended.state == LifecycleState.FAILED
    assert "engine exited before becoming ready" in (ended.status_reason or "")


def test_a_silent_collective_readiness_inside_its_budget_publishes_the_route(
    tmp_path: Path,
) -> None:
    """A slow engine launch must survive its own declared readiness budget.

    The measured defect: the readiness phase declares an hour of budget, the
    agent is blocked inside the launch and silent, and the Controller expired
    the attempt after its short lease and parked the operation, so the eventual
    success never published the route.  Driving the real agent claim and result
    boundary, the still-current attempt must stay current inside the budget, its
    success must complete the start, and the run must reach its published route.
    """

    now = [NOW]
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        nodes=2,
        distributed_lifecycle=True,
        distributed_start_timeout_seconds=3600,
    )
    service._clock = lambda: now[0]
    jobs = AgentJobService(sessions, clock=lambda: now[0])
    jobs.set_result_consumer(service.consume_agent_result)

    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="u" * 36
    )
    plan = service.preview_run(installation.owner_id, "slow-launch")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="v" * 36,
    )

    def children(phase: str) -> tuple[AgentOperation, ...]:
        with sessions() as session:
            rows = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == start.id)
                    .order_by(AgentOperation.created_at, AgentOperation.id)
                )
            )
        return tuple(row for row in rows if row.payload.get("phase") == phase)

    def success(payload: Mapping[str, object]) -> dict[str, object]:
        return start_evidence(payload)

    def claim(node_id: str):
        runtime_identity = dict(PACKAGED_RUNTIME_IDENTITY)
        return claim_agent(
            jobs,
            node_id,
            f"serial-{nodes.index(node_id)}",
            runtime_identity=runtime_identity,
        )

    def complete(claim, result: Mapping[str, object]) -> None:
        jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": claim.fence,
                        "state": "succeeded",
                        "result": result,
                    }
                )
            )
        )

    for launch in children("rank-launch"):
        launch_claim = claim(launch.node_id)
        assert launch_claim is not None
        complete(launch_claim, success(launch_claim.payload))

    readiness = children("collective-readiness")
    assert len(readiness) == 1
    target = readiness[0]
    readiness_claim = claim(target.node_id)
    assert readiness_claim is not None

    # The agent is blocked inside the launch: four times its accepted lease pass
    # with no heartbeat, while the plan's own readiness budget stays open.
    now[0] = NOW + timedelta(seconds=120)
    complete(readiness_claim, success(readiness_claim.payload))

    view = service.get(start.id)
    assert view.state == "succeeded", view.status_reason
    with sessions() as session:
        run = _required(session.get(RecipeRun, start.owner_id))
        assert run.state == "running"
        assert run.route_state == "pending"
        assert [
            _required(node).state
            for node in session.scalars(
                select(RunNode)
                .where(RunNode.run_id == start.owner_id)
                .order_by(RunNode.rank)
            )
        ] == ["running", "running"]

    publisher = ConcurrentPublisher()
    _bound, routes = bind_route_publications(sessions, service, publisher)
    with sessions.begin() as session:
        owner = _required(
            session.scalar(
                select(RunNode).where(
                    RunNode.run_id == start.owner_id, RunNode.role == "entrypoint"
                )
            )
        )
        owner.observed_run_generation = 1
        owner.observation_observed_at = NOW
        owner.observation_endpoint_ready = True
        owner.updated_at = NOW
        worker = _required(
            session.scalar(
                select(RunNode).where(
                    RunNode.run_id == start.owner_id, RunNode.role == "worker"
                )
            )
        )
        worker.observed_run_generation = 1
        worker.observation_observed_at = NOW
        worker.observation_endpoint_ready = None
        worker.updated_at = NOW
    routes.publish_run(start.owner_id)
    assert publisher.aliases[-1] == ("slow-launch",)


@pytest.mark.usefixtures("damaged_json_rows")
def test_distributed_start_with_missing_run_generation_records_the_rank_unproven(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="g" * 36
    )
    plan = service.preview_run(installation.owner_id, "missing-generation")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="h" * 36,
    )
    with sessions.begin() as session:
        launch = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
        assert launch is not None
        payload = {
            key: value
            for key, value in launch.payload.items()
            if key != "run_generation"
        }
        launch.payload = payload

    # A start whose order no longer parses cannot prove the rank started: the
    # rank is recorded with a typed unproven marker, never refused. Its phase
    # cannot be inferred from a payload that failed the canonical contract.
    view = service.record_node_result(
        start.id,
        launch.node_id,
        succeeded=True,
        evidence=start_evidence(payload),
    )
    recorded = view.lifecycle_result
    assert isinstance(recorded, RecipeOperationProgressResult)
    assert recorded.node_evidence is not None
    marker = recorded.node_evidence[launch.node_id]
    assert isinstance(marker, LifecycleCodeFailureResult)


@pytest.mark.usefixtures("damaged_json_rows")
def test_tensor_parallel_start_with_missing_run_generation_records_the_rank_unproven(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="i" * 36
    )
    plan = service.preview_run(installation.owner_id, "missing-generation")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="j" * 36,
    )
    with sessions.begin() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
        assert child is not None
        payload = {
            key: value
            for key, value in child.payload.items()
            if key != "run_generation"
        }
        child.payload = payload

    view = service.record_node_result(
        start.id,
        child.node_id,
        succeeded=True,
        evidence=start_evidence(payload),
    )
    recorded = view.lifecycle_result
    assert isinstance(recorded, RecipeOperationProgressResult)
    assert recorded.node_evidence is not None
    evidence = recorded.launch_evidence or recorded.node_evidence
    marker = evidence[child.node_id]
    assert isinstance(marker, LifecycleCodeFailureResult)


def test_worker_death_while_owner_is_healthy_never_publishes_route(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="d" * 36
    )
    start = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="e" * 36,
        alias="owner-healthy-worker-dead",
    )
    record_exact_empty_snapshot(sessions, nodes[1], NOW + timedelta(seconds=1))

    publisher = ConcurrentPublisher()
    _service, routes = bind_route_publications(sessions, service, publisher)
    with pytest.raises(Exception) as _ending:
        routes.publish_run(start.owner_id)

    assert publisher.aliases == []
    with sessions() as session:
        owner = _required(
            session.scalar(
                select(RunNode).where(
                    RunNode.run_id == start.owner_id,
                    RunNode.role == "entrypoint",
                )
            )
        )
        worker = _required(
            session.scalar(
                select(RunNode).where(
                    RunNode.run_id == start.owner_id,
                    RunNode.role == "worker",
                )
            )
        )
        assert owner.state == "running"
        assert owner.observation_endpoint_ready is True
        assert worker.state == "failed"


def test_singleton_start_grants_time_for_first_exact_observation(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=1
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="g" * 36
    )
    plan = service.preview_run(installation.owner_id, "singleton-observation-grace")
    start = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id="h" * 36
    )
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
        )
    assert operation is not None
    started_at = NOW + timedelta(microseconds=500_000)
    service._clock = lambda: started_at
    service.record_node_result(
        start.id,
        operation.node_id,
        succeeded=True,
        evidence=start_evidence(operation.payload),
    )
    _bound, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    worker = RecipeOperationWorker(
        sessions, routes, clock=lambda: started_at + timedelta(milliseconds=1)
    )
    assert worker.tick() is True
    assert worker.tick() is False
    with sessions() as session:
        run = _required(session.get(RecipeRun, start.owner_id))
        node = _required(
            session.scalar(select(RunNode).where(RunNode.run_id == run.id))
        )
        assert run.observation_deadline_at is not None
        assert run.observation_deadline_at.replace(
            tzinfo=UTC
        ) == started_at + timedelta(seconds=120)
        assert run.route_state == RouteState.PENDING
        assert run.route_attempts == 1
        assert run.next_action_at is not None
        assert run.next_action_at.replace(tzinfo=UTC) > started_at
        assert node.state == LifecycleState.RUNNING
        assert node.observed_run_generation is None
    # Its Spark is present and reporting, but not for this run: the run's own
    # first telemetry is late (an away Spark would be waited for).
    with sessions.begin() as session:
        for agent in session.scalars(select(AgentNode)):
            contact_row(agent).last_seen_at = started_at + timedelta(seconds=120)
    expired = RecipeOperationWorker(
        sessions, routes, clock=lambda: started_at + timedelta(seconds=120)
    )
    assert expired._expire_initial_observation_deadline() is True
    with sessions() as session:
        node = _required(
            session.scalar(select(RunNode).where(RunNode.run_id == start.owner_id))
        )
        assert node.state == RunState.RUNNING
        run = _required(session.get(RecipeRun, start.owner_id))
        assert run.route_state == RouteState.WITHDRAWN
        assert run.observation_deadline_at is None


def test_collective_readiness_starts_distinct_observation_grace(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id="g" * 36
    )
    plan = service.preview_run(installation.owner_id, "observation-grace")
    start = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="h" * 36,
    )
    with sessions() as session:
        launches = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == start.id)
            )
        )
    assert len(launches) == 2
    for launch in launches:
        service.record_node_result(
            start.id,
            launch.node_id,
            succeeded=True,
            evidence=start_evidence(launch.payload),
        )
    with sessions() as session:
        readiness = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == start.id,
                AgentOperation.payload["phase"].as_string() == "collective-readiness",
            )
        )
    assert readiness is not None

    collective_at = NOW + timedelta(seconds=59)
    service._clock = lambda: collective_at
    service.record_node_result(
        start.id,
        readiness.node_id,
        succeeded=True,
        evidence=start_evidence(readiness.payload),
    )
    with sessions() as session:
        run = _required(session.get(RecipeRun, start.owner_id))
        assert run.observation_deadline_at is not None
        assert run.observation_deadline_at.replace(
            tzinfo=UTC
        ) == collective_at + timedelta(seconds=120)

    _bound, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    before_expiry = RecipeOperationWorker(
        sessions,
        routes,
        clock=lambda: NOW + timedelta(seconds=60),
    )
    assert before_expiry.tick() is True
    assert before_expiry.tick() is False
    with sessions() as session:
        run = _required(session.get(RecipeRun, start.owner_id))
        assert run.route_state == RouteState.PENDING
        assert run.route_attempts == 1
        assert run.next_action_at is not None
        assert run.next_action_at.replace(tzinfo=UTC) > NOW + timedelta(seconds=60)
        assert all(
            node.state == LifecycleState.RUNNING
            for node in session.scalars(
                select(RunNode).where(RunNode.run_id == start.owner_id)
            )
        )
