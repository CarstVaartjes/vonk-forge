from vonk_agent_protocol import canonical_message

"""Admission bounds a recipe's load by what its runs really used."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from vonk_control.inventory_repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.models import (
    CatalogDocumentRevision,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.observed_run_memory import observed_role_peaks
from vonk_control.run_admission import RunAdmissionService
from vonk_control.telemetry import TelemetryRepository, TelemetrySample

from .test_run_admission import setup

# The fixture Spark: 300 bytes free at admission, a declared peak of 225 and a
# 50-byte platform floor, so the declaration alone fits with 25 to spare.
_FREE = 300
_FLOOR = 50


def _serve(sessions, now, node, installation, *, lowest_available, other_claim=None):
    """Admit one run, report telemetry while it serves, then stop it."""
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=_FLOOR
    )
    run_id = service.accept_run(
        service.plan_run(installation, alias="qwen", now=now), actor="admin", now=now
    )
    if other_claim is not None:
        owner_kind, state = other_claim
        with sessions.begin() as session:
            session.add(
                ResourceReservation(
                    node_id=node,
                    kind="unified-memory",
                    resource_key="other",
                    amount_bytes=1,
                    owner_kind=owner_kind,
                    owner_id=str(uuid4()),
                    state=state,
                    plan_digest="f" * 64,
                    created_at=now,
                )
            )
    observed_at = now + timedelta(seconds=1)
    TelemetryRepository(sessions, clock=lambda: observed_at).record_batch(
        node,
        (
            TelemetrySample.model_validate_json(
                canonical_message(
                    {
                        "boot_id": str(uuid4()),
                        "observed_at": observed_at,
                        "memory_total_bytes": 1000,
                        "memory_available_bytes": lowest_available,
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
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = "stopped"
        run.stopped_at = now
        rank = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert rank is not None
        rank.state = "stopped"
        observed = rank.observed_memory_bytes
        for claim in session.scalars(
            select(ResourceReservation).where(ResourceReservation.state == "active")
        ):
            claim.state = "released"
            claim.released_at = now
    return service, observed


def _inventory(sessions, now, node, free):
    InventoryRepository(sessions, clock=lambda: now).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node,
                    "observed_at": now,
                    "disk_total_bytes": 1000,
                    "disk_free_bytes": 500,
                    "host_memory_total_bytes": 1000,
                    "host_memory_free_bytes": free,
                    "gpu_memory_total_bytes": 1000,
                    "gpu_memory_free_bytes": free,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": ("runtime.vonk.v1", "recipe.image.pull.v1"),
                    "memory_pool": "shared",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )


def test_a_runs_whole_run_peak_never_raises_its_steady_reservation(
    tmp_path,
) -> None:
    """Catches a swap-backed load poisoning its own content (2026-10-28).

    The observed peak spans the whole run, its load included, and
    MemAvailable leaves out swap: raising the steady demand with it made a
    content whose load swapped never fit again (demand_exceeds_capacity,
    stored by content digest). It bounds the load only.
    """
    sessions, now, node, installation = setup(tmp_path, free_memory=_FREE)
    service, observed = _serve(
        sessions, now, node, installation, lowest_available=_FREE - 280
    )
    assert observed == 280

    plan = service.plan_run(installation, alias="qwen", now=now)
    assert plan.nodes[0].required_memory_bytes == 225
    assert "resource.demand_exceeds_capacity" not in {
        item.code for item in plan.nodes[0].blockers
    }


def test_real_use_below_the_declaration_never_loosens_admission(tmp_path) -> None:
    """Catches a served run's lighter use shrinking the creator's contract."""
    sessions, now, node, installation = setup(tmp_path, free_memory=_FREE)
    service, observed = _serve(
        sessions, now, node, installation, lowest_available=_FREE - 100
    )
    assert observed == 100

    plan = service.plan_run(installation, alias="qwen", now=now)
    assert plan.allowed and plan.nodes[0].required_memory_bytes == 225


@pytest.mark.parametrize(
    ("other_claim", "attributed"),
    [(("recipe-build", "active"), False), (("fleet-profile", "promised"), True)],
)
def test_only_a_sole_active_claim_owner_is_charged_with_the_use(
    tmp_path, other_claim, attributed
) -> None:
    """Catches charging a builder's memory to the run, or ignoring a run whose
    neighbour holds only a promise (no memory until its own run starts)."""
    sessions, now, node, installation = setup(tmp_path, free_memory=_FREE)
    _, observed = _serve(
        sessions,
        now,
        node,
        installation,
        lowest_available=_FREE - 280,
        other_claim=other_claim,
    )
    assert observed == (280 if attributed else None)


def test_a_runs_real_use_outlives_its_collected_history(tmp_path) -> None:
    """Catches the observed peak leaving with the run's terminal history.

    Terminal history is collected a day after a run ends (its rank rows go
    with it), so an over-declaration was forgotten and the next run's load
    was bounded by the declaration again. The installation keeps the peak.
    """
    sessions, now, node, installation = setup(tmp_path, free_memory=_FREE)
    service, observed = _serve(
        sessions, now, node, installation, lowest_available=_FREE - 280
    )
    assert observed == 280
    with sessions.begin() as session:
        # As terminal_history_collection removes an ended run.
        session.execute(delete(RunNode))
        session.execute(delete(RecipeRun))

    with sessions() as session:
        digest = session.scalars(
            select(CatalogDocumentRevision.content_digest).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        ).one()
        assert observed_role_peaks(session, digest) == {
            service.plan_run(installation, alias="qwen", now=now).nodes[0].role: 280
        }
