"""Starting a container does not establish its model memory allocation."""

from pathlib import Path

from sqlalchemy import select
from vonk_control.memory_reservations import memory_reservations
from vonk_control.models import AgentOperation, RunNode

from .test_recipe_operations import (
    installed_recipe,
    setup_services,
    start_evidence,
)


def test_rank_launch_keeps_future_memory_reserved_until_readiness(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping, build, nodes = setup_services(
        tmp_path, nodes=2, distributed_lifecycle=True
    )
    installation = installed_recipe(
        service, mapping, build, nodes, request_id="memory-materialization-install"
    )
    plan = service.preview_run(installation.owner_id, "memory-materialization")
    operation = service.start(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="memory-materialization-start",
    )
    with sessions() as session:
        launch = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == operation.id,
                AgentOperation.node_id == nodes[0],
            )
        )
        assert launch is not None
        assert launch.payload["phase"] == "rank-launch"
        evidence = start_evidence(launch.payload)
    service.record_node_result(
        operation.id,
        nodes[0],
        succeeded=True,
        evidence=evidence,
    )
    with sessions() as session:
        rank = session.scalar(
            select(RunNode).where(
                RunNode.run_id == operation.owner_id,
                RunNode.node_id == nodes[0],
            )
        )
        assert rank is not None and rank.state == "starting"
        totals = memory_reservations(session, nodes[0], memory_pool="shared")
        assert totals.unmaterialized_bytes_by_kind == totals.committed_bytes_by_kind
        assert totals.unmaterialized_bytes_by_kind["unified-memory"] > 0
