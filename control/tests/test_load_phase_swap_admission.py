"""A load's transient peak rides on swap; the steady demand stays the reservation.

Every test drives the real ``plan_run`` / ``accept_run_in_session`` and the
real inventory repository against PostgreSQL.
"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from vonk_agent_protocol import ResourceBlockerCode, canonical_message
from vonk_agent_protocol.swap import GIB
from vonk_control.inventory_repository import InventoryRepository, NodeInventoryReport
from vonk_control.models import RecipeRun, ResourceReservation, RunNode
from vonk_control.run_admission import RunAdmissionService
from vonk_control.telemetry import TelemetryRepository, TelemetrySample

from .test_run_admission import setup

_TOTAL = 120 * GIB
_FREE = 118 * GIB
_STEADY = 100 * GIB
_FLOOR = 4 * GIB


def _inventory(sessions, at, node, *, free=_FREE, swap=None):
    """Record one realistic Spark report through the real inventory validator."""
    report = {
        "node_id": node,
        "observed_at": at,
        "disk_total_bytes": 1000,
        "disk_free_bytes": 500,
        "host_memory_total_bytes": _TOTAL,
        "host_memory_free_bytes": free,
        "gpu_memory_total_bytes": _TOTAL,
        "gpu_memory_free_bytes": free,
        "gpu_count": 1,
        "artifact_store_read_only": False,
        "capabilities": ("runtime.vonk.v1", "recipe.image.pull.v1"),
        "memory_pool": "shared",
        "schema_version": 1,
        "nvidia_driver_version": "unknown",
        "container_runtime_version": "unknown",
    }
    if swap is not None:
        report["host_settings"] = {
            "display_manager_active": False,
            "graphical_default_target": False,
            "swap": swap,
        }
    InventoryRepository(sessions, clock=lambda: at).record(
        NodeInventoryReport.model_validate_json(canonical_message(report))
    )


def _swap(total_gib, *, used=0):
    return {
        "total_bytes": total_gib * GIB,
        "free_bytes": total_gib * GIB - used,
        "receipt_detail": "test",
    }


def _telemetry(sessions, at, node, available):
    TelemetryRepository(sessions, clock=lambda: at).record_batch(
        node,
        (
            TelemetrySample.model_validate_json(
                canonical_message(
                    {
                        "boot_id": str(uuid4()),
                        "observed_at": at,
                        "memory_total_bytes": _TOTAL,
                        "memory_available_bytes": available,
                        "disk_total_bytes": None,
                        "disk_free_bytes": None,
                        "gpu_utilization_percent": None,
                        "gpu_memory_total_bytes": None,
                        "gpu_memory_free_bytes": None,
                    }
                )
            ),
        ),
    )


def _stop(sessions, run_id, at):
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = "stopped"
        run.stopped_at = at
        for rank in session.scalars(select(RunNode).where(RunNode.run_id == run_id)):
            rank.state = "stopped"
        for claim in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == run_id,
                ResourceReservation.state == "active",
            )
        ):
            claim.state = "released"
            claim.released_at = at


def _world(tmp_path, postgres_engine):
    sessions, now, node, installation = setup(
        tmp_path, peak_bytes=_STEADY, engine=postgres_engine
    )
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=_FLOOR
    )
    return sessions, now, node, installation, service


def _codes(plan):
    return {reason.code for node in plan.nodes for reason in node.blockers}


def _accept(sessions, service, plan, now):
    with sessions.begin() as session:
        return service.accept_run_in_session(session, plan, actor="admin", now=now)


def test_f_a_swap_backed_load_never_poisons_its_content(
    tmp_path, postgres_engine
) -> None:
    """Catches the whole-run peak replacing the steady demand (2026-10-28).

    DSV4 loads past physical memory on swap; MemAvailable (which leaves swap
    out) fell to 0.7 GiB, so the run recorded nearly the whole pool as its
    peak. Re-admission then charged that as the steady demand, found it plus
    the floor above the Spark's pool and ended the load for good
    (demand_exceeds_capacity, stored by content digest).
    """
    sessions, now, node, installation, service = _world(tmp_path, postgres_engine)
    _inventory(sessions, now + timedelta(seconds=1), node, swap=_swap(64))
    at = now + timedelta(seconds=1)
    run_id = _accept(
        sessions, service, service.plan_run(installation, alias="qwen", now=at), at
    )
    _telemetry(sessions, now + timedelta(seconds=2), node, int(0.7 * GIB))
    with sessions() as session:
        rank = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert rank is not None and rank.observed_memory_bytes == _FREE - int(0.7 * GIB)
    _stop(sessions, run_id, now + timedelta(seconds=3))

    later = now + timedelta(seconds=4)
    _inventory(sessions, later, node, swap=_swap(64))
    plan = service.plan_run(installation, alias="qwen", now=later)

    assert ResourceBlockerCode.DEMAND_EXCEEDS_CAPACITY not in _codes(plan)
    assert plan.allowed, _codes(plan)
    assert plan.nodes[0].required_memory_bytes == _STEADY
    readmitted = _accept(sessions, service, plan, later)
    with sessions() as session:
        claim = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == readmitted,
                ResourceReservation.kind == "unified-memory",
            )
        )
        assert claim is not None and claim.amount_bytes == _STEADY
