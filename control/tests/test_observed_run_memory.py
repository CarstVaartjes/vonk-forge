"""Admission uses what a recipe really used once that exceeds its declaration."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import RecipeRun, ResourceReservation, RunNode
from vonk_control.run_admission import RunAdmissionService
from vonk_control.telemetry import TelemetryRepository, TelemetrySampleInput

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
            TelemetrySampleInput(
                boot_id=uuid4(),
                observed_at=observed_at,
                memory_total_bytes=1000,
                memory_available_bytes=lowest_available,
                disk_total_bytes=None,
                disk_free_bytes=None,
                gpu_utilization_percent=None,
                gpu_memory_total_bytes=None,
                gpu_memory_free_bytes=None,
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
        InventorySnapshotInput(
            node,
            now,
            1000,
            500,
            1000,
            free,
            1000,
            free,
            1,
            False,
            ("runtime.vonk.v1", "recipe.image.pull.v1"),
            memory_pool="shared",
        )
    )


def test_a_run_over_its_declaration_is_not_readmitted_where_its_real_use_does_not_fit(
    tmp_path,
) -> None:
    """Catches admission trusting a declaration that real use already exceeded.

    The 2026-10-11 freeze: a recipe whose declared peak fit with a thin margin
    really used more, and the Spark thrashed. Once a run of the same content
    has shown its real peak, that peak plus the floor must fit.
    """
    sessions, now, node, installation = setup(tmp_path, free_memory=_FREE)
    service, observed = _serve(
        sessions, now, node, installation, lowest_available=_FREE - 280
    )
    assert observed == 280

    refused = service.plan_run(installation, alias="qwen", now=now)
    assert not refused.allowed
    assert refused.nodes[0].required_memory_bytes == 280
    assert {item.code for item in refused.nodes[0].blockers} == {
        "run.insufficient_memory"
    }

    later = now + timedelta(seconds=2)
    _inventory(sessions, later, node, 280 + _FLOOR)
    fits = service.plan_run(installation, alias="qwen", now=later)
    assert fits.allowed and fits.nodes[0].free_after_bytes == _FLOOR


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
