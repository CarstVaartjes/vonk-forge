from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from vonk_agent_protocol import (
    AgentOperation,
    RecipeOperationRequest,
)
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
from vonk_control.models import (
    AgentOperation as StoredAgentOperation,
    Job,
    RecipeInstallation,
    RecipeRun,
)

from test_recipe_operations import (
    ConcurrentPublisher,
    NOW,
    bind_route_publications,
    mark_current_exact_observations,
    record_exact_empty_snapshot,
    setup_services,
    start_evidence,
)


def _canonical_start_evidence(payload: dict[str, object]) -> dict[str, object]:
    return start_evidence(payload)


def _queued_recovery_restart(tmp_path: Path):
    sessions, service, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    install_plan = service.preview_install(mapping_id, build_id)
    installation = service.install(
        install_plan,
        plan_digest=install_plan.plan_digest,
        actor="admin",
        request_id="i" * 36,
    )
    for node_id in nodes:
        service.record_node_result(
            installation.id,
            node_id,
            succeeded=True,
            evidence={"installed_bytes": 120},
        )
    run_plan = service.preview_run(installation.owner_id, "deadline-gang")
    started = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="r" * 36,
    )
    completed: set[str] = set()
    while service.get(started.id).state == "running":
        with sessions() as session:
            children = tuple(
                session.scalars(
                    select(StoredAgentOperation)
                    .where(StoredAgentOperation.parent_job_id == started.id)
                    .order_by(StoredAgentOperation.node_id)
                )
            )
        pending = tuple(child for child in children if child.id not in completed)
        assert pending
        for child in pending:
            service.record_node_result(
                started.id,
                child.node_id,
                succeeded=True,
                evidence=_canonical_start_evidence(child.payload),
            )
            completed.add(child.id)
    mark_current_exact_observations(sessions, started.owner_id, NOW)

    service, routes = bind_route_publications(sessions, service, ConcurrentPublisher())
    routes.publish_run(started.owner_id)
    record_exact_empty_snapshot(sessions, nodes[1], NOW.replace(second=1))
    recovery = DistributedRecoveryCoordinator(
        sessions, routes=routes, agent_jobs=queue, clock=lambda: NOW
    )
    assert recovery.tick() is True
    with sessions() as session:
        stop_job = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == started.owner_id,
            )
        )
    assert stop_job is not None
    produced_phases = tuple(
        tuple(
            (item["node_id"], item["payload"])
            for item in group
        )
        for group in stop_job.payload["recovery"]["start_phases"]
    )
    for _node_id, payload in (
        item for group in produced_phases for item in group
    ):
        RecipeOperationRequest.parse(AgentOperation.RECIPE_START, payload)
    service.record_node_result(stop_job.id, nodes[0], succeeded=True, evidence={})
    service.record_node_result(stop_job.id, nodes[1], succeeded=True, evidence={})
    with sessions() as session:
        restart = session.scalar(
            select(Job).where(
                Job.kind == "recipe.start",
                Job.payload["owner_id"].as_string() == started.owner_id,
                Job.id != started.id,
            )
        )
    assert restart is not None
    return sessions, service, started, restart, nodes, produced_phases


def test_recovery_start_children_are_canonical_schema2_payloads(
    tmp_path: Path,
) -> None:
    (
        sessions,
        service,
        started,
        restart,
        nodes,
        produced_phases,
    ) = _queued_recovery_restart(tmp_path)

    with sessions() as session:
        rank_launches = tuple(
            session.scalars(
                select(StoredAgentOperation)
                .where(StoredAgentOperation.parent_job_id == restart.id)
                .order_by(StoredAgentOperation.node_id)
            )
        )
    assert {item.payload["phase"] for item in rank_launches} == {"rank-launch"}
    assert {item.node_id for item in rank_launches} == set(nodes)

    produced = {
        (node_id, payload["phase"]): payload
        for group in produced_phases
        for node_id, payload in group
    }
    assert {
        (operation.node_id, operation.payload["phase"]): operation.payload
        for operation in rank_launches
    } == {
        key: payload for key, payload in produced.items() if payload["phase"] == "rank-launch"
    }

    for operation in rank_launches:
        service.record_node_result(
            restart.id,
            operation.node_id,
            succeeded=True,
            evidence=_canonical_start_evidence(operation.payload),
        )

    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None
        installation = session.get(RecipeInstallation, run.installation_id)
        assert installation is not None
        persisted_plans = installation.plan["compiled_execution_plans"]
        children = tuple(
            session.scalars(
                select(StoredAgentOperation)
                .where(StoredAgentOperation.parent_job_id == restart.id)
                .order_by(StoredAgentOperation.id)
            )
        )

    assert len(children) == 3
    assert (
        sum(item.payload["phase"] == "collective-readiness" for item in children) == 1
    )
    assert {
        (operation.node_id, operation.payload["phase"]): operation.payload
        for operation in children
    } == produced
    assert {key for child in children for key in child.payload} == {
        "schema_version",
        "run_id",
        "installation_id",
        "recipe_revision_id",
        "recipe_content_sha256",
        "mapping_id",
        "mapping_generation",
        "run_generation",
        "image_digest",
        "plan_digest",
        "alias",
        "rank",
        "role",
        "port",
        "reserved_memory_bytes",
        "endpoint_address",
        "world_size",
        "compiled_execution_plan",
        "local_address",
        "master_address",
        "master_port",
        "phase",
        "start_deadline",
    }

    for child in children:
        parsed = RecipeOperationRequest.parse(
            AgentOperation.RECIPE_START,
            child.payload,
        )
        assert parsed.schema_version == 2
        assert parsed.run_id == started.owner_id
        assert parsed.mapping_id == run.mapping_id
        assert parsed.mapping_generation == run.mapping_generation
        assert parsed.run_generation == run.run_generation
        assert parsed.plan_digest == run.plan_digest
        assert parsed.recipe_revision_id == run.plan["recipe_revision_id"]
        assert parsed.rank == child.payload["rank"]
        assert parsed.role == child.payload["role"]
        assert parsed.compiled_execution_plan is not None
        persisted = persisted_plans[child.node_id]
        assert parsed.compiled_execution_plan["identity"] == persisted["identity"]
        placement = parsed.compiled_execution_plan["runtime"]["placement"]
        assert placement["rank"] == child.payload["rank"]
        assert placement["role"] == child.payload["role"]
        assert placement["world_size"] == child.payload["world_size"]
        assert placement["port"] == child.payload["port"]
        assert (
            placement["reserved_memory_bytes"] == child.payload["reserved_memory_bytes"]
        )
        assert placement["local_address"] == child.payload["local_address"]
        assert placement["master_address"] == child.payload["master_address"]
        assert placement["master_port"] == child.payload["master_port"]
        assert parsed.compiled_execution_plan["security"]["network_mode"] == "bridge"
        assert "expected_bytes" not in child.payload
        assert "kind" not in child.payload
