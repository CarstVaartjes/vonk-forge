from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select
from vonk_control.install_admission import InstallPlanConflict
from vonk_control.inventory_repository import (
    MAX_INVENTORY_FUTURE_SKEW,
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    InstallationNode,
    RecipeInstallation,
    ResourceReservation,
)

from .test_recipe_operations import NOW, installed_recipe, setup_services
from .test_run_switch_operations import RecordingArtifactExecutor, _request, _service


def _record_disk(sessions, node_id, *, at, free, observed_at=None):
    InventoryRepository(sessions, clock=lambda: at).record(
        InventorySnapshotInput(
            node_id,
            observed_at if observed_at is not None else at,
            10_000,
            free,
            10_000,
            8_000,
            10_000,
            8_000,
            1,
            False,
            ("runtime.vonk.v1", "recipe.operations.v1"),
            memory_pool="shared",
        )
    )


def test_postgres_completed_install_is_not_reserved_twice(tmp_path, postgres_engine):
    sessions, lifecycle, _queue, mapping, build, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    original = lifecycle.preview_install(mapping, build)
    installed_recipe(lifecycle, mapping, build, nodes, request_id=str(uuid.uuid4()))
    original_node = original.nodes[0]
    headroom = original_node.required_bytes - original_node.required_download_bytes
    # The original observation predates completion: retain the whole reservation.
    assert lifecycle.preview_install(mapping, build).nodes[0].active_reserved_bytes == (
        original_node.required_bytes
    )
    later = NOW + MAX_INVENTORY_FUTURE_SKEW + timedelta(seconds=1)
    _record_disk(sessions, nodes[0], at=later, free=2_000)
    lifecycle._clock = lambda: later
    fresh = lifecycle.preview_install(mapping, build)
    assert fresh.allowed
    assert fresh.nodes[0].active_reserved_bytes == headroom
    # Exercise the other admission surface against the same real persisted receipt.
    provider = _service(sessions, later, lifecycle, RecordingArtifactExecutor())
    run = provider.preview(_request(sessions, nodes[0]), actor="admin")
    assert run.fit.nodes[0].disk_required_bytes is not None
    assert run.fit.nodes[0].disk_free_after_bytes == (
        2_000 - headroom - run.fit.nodes[0].disk_required_bytes
    )
    assert not any(
        reason.code == "run-switch.insufficient-disk" for reason in run.blockers
    )

    # New pending reservations still serialize admission. Exactly one further
    # install fits; two simultaneous accepts of the same preview cannot both win.
    free = headroom + fresh.nodes[0].required_bytes + fresh.nodes[0].disk_floor_bytes
    latest = later + timedelta(seconds=1)
    _record_disk(sessions, nodes[0], at=latest, free=free)
    admission = lifecycle._install_admission
    plan = admission.plan_install(mapping, build, now=latest)
    assert plan.allowed
    barrier = Barrier(2)

    def accept():
        barrier.wait(timeout=5)
        try:
            return admission.accept_install(plan, actor="admin", now=latest)
        except InstallPlanConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _: accept(), range(2)))
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert not admission.plan_install(mapping, build, now=latest).allowed


def test_future_dated_inventory_cannot_discount_a_later_install(
    tmp_path, postgres_engine
):
    sessions, lifecycle, _queue, mapping, build, nodes = setup_services(
        tmp_path, engine=postgres_engine
    )
    before_completion = NOW - timedelta(seconds=1)
    _record_disk(
        sessions,
        nodes[0],
        at=before_completion,
        observed_at=before_completion + MAX_INVENTORY_FUTURE_SKEW,
        free=2_000,
    )
    original = lifecycle.preview_install(mapping, build)
    installed_recipe(lifecycle, mapping, build, nodes, request_id=str(uuid.uuid4()))
    # The agent clock is within ingress's accepted +30s window, but the bytes
    # in this snapshot predate completion. Comparing the two clocks directly
    # would discount this installation against free space that never included it.
    assert lifecycle.preview_install(mapping, build).nodes[0].active_reserved_bytes == (
        original.nodes[0].required_bytes
    )
    for offset, materialized in ((0, False), (1, True)):
        at = NOW + MAX_INVENTORY_FUTURE_SKEW + timedelta(seconds=offset)
        _record_disk(sessions, nodes[0], at=at, free=2_000)
        lifecycle._clock = lambda at=at: at
        reserved = (
            lifecycle.preview_install(mapping, build).nodes[0].active_reserved_bytes
        )
        assert reserved == original.nodes[0].required_bytes - (
            original.nodes[0].required_download_bytes if materialized else 0
        )


@pytest.mark.parametrize(
    "uncertainty", ["partial", "malformed", "mismatched", "unknown-owner"]
)
def test_uncertain_install_reservations_keep_their_full_charge(tmp_path, uncertainty):
    sessions, lifecycle, _queue, mapping, build, nodes = setup_services(tmp_path)
    original = lifecycle.preview_install(mapping, build)
    installed_recipe(lifecycle, mapping, build, nodes, request_id=str(uuid.uuid4()))
    with sessions.begin() as session:
        reservation = session.scalar(
            select(ResourceReservation).where(ResourceReservation.kind == "disk")
        )
        assert reservation is not None
        installation = session.get(RecipeInstallation, reservation.owner_id)
        assert installation is not None
        node = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation.id
            )
        )
        assert node is not None
        if uncertainty == "partial":
            node.state = "failed"
        elif uncertainty == "malformed":
            installation.plan = {"bad": True}
        elif uncertainty == "mismatched":
            reservation.plan_digest = "0" * 64
        else:
            reservation.owner_id = str(uuid.uuid4())
    later = NOW + MAX_INVENTORY_FUTURE_SKEW + timedelta(seconds=1)
    _record_disk(sessions, nodes[0], at=later, free=2_000)
    lifecycle._clock = lambda: later
    assert lifecycle.preview_install(mapping, build).nodes[0].active_reserved_bytes == (
        original.nodes[0].required_bytes
    )
