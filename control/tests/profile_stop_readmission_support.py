"""Prove confirmed profile cleanup leaves the same physical gang admissible."""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.recipe_operations import RecipeStartPayload
from vonk_control.agent_jobs import AgentJobService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import AgentNode, RecipeRun, ResourceReservation, RunNode
from vonk_control.recipe_operations import RecipeOperationService

from .agent_fences import fenced_operation
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
from .test_fleet_profile_cancel import _agent_result
from .test_recipe_operations import start_evidence


def start_on_released_gang(
    sessions: sessionmaker[Session],
    lifecycle: RecipeOperationService,
    old_run_id: str,
    nodes: tuple[str, ...],
    *,
    clock: Callable[[], datetime],
    request_id: str,
) -> str:
    """Catch terminal-only cleanup, leaked reservations, and stale redispatch."""
    assert lifecycle._clock() == clock()
    with sessions() as session:
        stopped = session.get(RecipeRun, old_run_id)
        assert stopped is not None and stopped.state == "stopped"
        assert set(
            session.scalars(select(RunNode.node_id).where(RunNode.run_id == old_run_id))
        ) == set(nodes)
        installation_id = stopped.installation_id
        assert not list(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == old_run_id,
                    ResourceReservation.state == "active",
                )
            )
        )
    # This is the fixture's physical producer reporting the same known devices
    # again after transport lease expiry, not a SQL claim/state rewrite. Preserve
    # its exact topology and capabilities; do not refresh the healthy sibling.
    inventory = InventoryRepository(sessions, clock=clock)
    for node_id in nodes:
        report = inventory.latest(node_id, now=clock(), maximum_age=300)
        inventory.record(
            InventorySnapshotInput(
                node_id,
                clock(),
                report.disk_total_bytes,
                report.disk_free_bytes,
                report.host_memory_total_bytes,
                report.host_memory_free_bytes,
                report.gpu_memory_total_bytes,
                report.gpu_memory_free_bytes,
                report.gpu_count,
                report.artifact_store_read_only,
                report.capabilities,
                memory_pool=report.memory_pool,
                fabric_address=report.fabric_address,
                fabric_bandwidth_mbps=report.fabric_bandwidth_mbps,
                nvidia_driver_version=report.nvidia_driver_version,
                container_runtime_version=report.container_runtime_version,
                network_interfaces=report.network_interfaces,
                nas_route_interface=report.nas_route_interface,
            )
        )
    preview = lifecycle.preview_run(installation_id, f"after-stop-{request_id}")
    assert preview.allowed, preview.nodes
    assert {target.node_id for target in preview.nodes} == set(nodes)
    fresh = lifecycle.start(
        preview, plan_digest=preview.plan_digest, actor="admin", request_id=request_id
    )
    assert fresh.owner_id != old_run_id
    jobs = AgentJobService(
        sessions, clock=clock, result_consumer=lifecycle.consume_agent_result
    )
    for _ in range(4):
        if lifecycle.get(fresh.id).state == "succeeded":
            break
        progressed = False
        for node_id in nodes:
            with sessions() as session:
                node = session.get(AgentNode, node_id)
                assert node is not None
                runtime_identity = {
                    **PACKAGED_RUNTIME_IDENTITY,
                    "architecture": node.architecture,
                }
                fingerprint = node.preflight_fingerprint
            claim = claim_agent(
                jobs,
                node_id,
                f"serial-{nodes.index(node_id)}",
                runtime_identity=runtime_identity,
                preflight_fingerprint=fingerprint,
            )
            if claim is None:
                continue
            assert claim.operation.value == "recipe.start"
            assert isinstance(claim.payload, RecipeStartPayload)
            native = fenced_operation(sessions, claim)
            assert native.parent_job_id == fresh.id
            assert claim.payload.run_id == fresh.owner_id
            jobs.record_result(
                _agent_result(
                    claim,
                    state="succeeded",
                    result=start_evidence(claim.payload.model_dump(mode="json")),
                )
            )
            progressed = True
        assert progressed
    assert lifecycle.get(fresh.id).state == "succeeded"
    with sessions() as session:
        active = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == fresh.owner_id,
                    ResourceReservation.state == "active",
                )
            )
        )
        assert {claim.node_id for claim in active} == set(nodes)
    return fresh.owner_id
