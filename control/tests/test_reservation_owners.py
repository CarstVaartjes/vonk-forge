"""A reservation is held only while its owner can still use it.

Each leak is reproduced through the real reservation path: a claim is created
by a real profile load or install, its owner is ended by the writes that end
it in production (a state written directly, a failed install job), and the
claim is shown to still block before the one reconciler releases it. The fences
are tested as hard as the leaks: a live owner never loses a claim.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import ReservationState
from vonk_control.attempt_residues import AttemptResidueReconciler
from vonk_control.disk_reservations import (
    describe_disk_charges,
    outstanding_disk_charges,
)
from vonk_control.inventory_repository import MAX_INVENTORY_FUTURE_SKEW
from vonk_control.models import (
    FleetProfileApplication,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.reservation_owners import (
    ADOPTION_WINDOW,
    OWNER_SETTLE_GRACE,
    reservation_owner,
)

from .test_attempt_residues import _load, _loop
from .test_disk_reservations import _record_disk
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
    started_recipe,
)
from .test_run_switch_operations import RecordingArtifactExecutor, _request, _service


def _claims(sessions, **where) -> list[ResourceReservation]:
    with sessions() as session:
        return list(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.state.in_(("active", "promised")),
                    *(
                        getattr(ResourceReservation, key) == value
                        for key, value in where.items()
                    ),
                )
            )
        )


def _reconciler(sessions, lifecycle, *, later: timedelta = timedelta()):
    return AttemptResidueReconciler(
        sessions,
        abandon_never_installed=lifecycle.abandon_never_installed,
        clock=lambda: lifecycle._clock() + later,
    )


def _end_application(load, state: str) -> None:
    """End the order the way the scheduler's direct state writes do."""

    with load.sessions.begin() as session:
        for application in session.scalars(select(FleetProfileApplication)):
            application.state = state


@pytest.mark.parametrize("state", ["cancelled", "failed", "succeeded"])
def test_an_ended_profile_releases_every_claim_it_held(
    tmp_path: Path, state: str
) -> None:
    load = _load(tmp_path)
    held = _claims(load.sessions, owner_kind="fleet-profile")
    assert {claim.kind for claim in held} == {"disk", "port", "unified-memory"}

    _end_application(load, state)
    # The reproduction: the direct write left everything it reserved.
    assert len(_claims(load.sessions, owner_kind="fleet-profile")) == len(held)
    # While it blocks, admission says whose claim it is.
    with load.sessions() as session:
        charges = outstanding_disk_charges(
            session, held[0].node_id, inventory_observed_at=None
        )
        assert f"reserved by {state} profile application" in describe_disk_charges(
            session, charges
        )

    assert _reconciler(load.sessions, load.lifecycle).tick()
    assert _claims(load.sessions, owner_kind="fleet-profile") == []


def test_a_live_profile_keeps_its_claims(tmp_path: Path) -> None:
    load = _load(tmp_path)
    held = _claims(load.sessions, owner_kind="fleet-profile")
    for _ in range(3):
        _reconciler(load.sessions, load.lifecycle).tick()
    assert len(_claims(load.sessions, owner_kind="fleet-profile")) == len(held)


def _install_job(load) -> Job:
    with load.sessions() as session:
        job = session.scalar(select(Job).where(Job.kind == "recipe.install"))
    assert job is not None
    return job


def _installing(load) -> str:
    assert _loop(
        load,
        lambda: (
            _claims(load.sessions, owner_kind="installation")
            and any(row.state == "installing" for row in _installations(load.sessions))
        ),
        complete=False,
    )
    return _install_job(load).id


def _installations(sessions) -> list[RecipeInstallation]:
    with sessions() as session:
        return list(session.scalars(select(RecipeInstallation)))


def _end_orders(load) -> None:
    """The Run/Switch order and its profile have ended too."""

    with load.sessions.begin() as session:
        for job in session.scalars(
            select(Job).where(Job.kind == "recipe.run-switch.v2")
        ):
            job.state = "failed"
    _end_application(load, "failed")


_SETTLED = ADOPTION_WINDOW + timedelta(seconds=1)


def test_a_failed_install_releases_its_claim_but_a_live_one_keeps_it(
    tmp_path: Path,
) -> None:
    load = _load(tmp_path)
    job_id = _installing(load)
    held = _claims(load.sessions, owner_kind="installation")
    assert held

    # Fence: the install job is running, so the installation is live.
    assert not _reconciler(load.sessions, load.lifecycle, later=_SETTLED).tick()
    assert len(_claims(load.sessions, owner_kind="installation")) == len(held)

    # The agent reports the install failed: the installation becomes partial
    # and, before the fix, kept its claim for good.
    with load.sessions.begin() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.state = "failed"
        for installation in session.scalars(select(RecipeInstallation)):
            installation.state = "partial"
    _end_orders(load)
    assert len(_claims(load.sessions, owner_kind="installation")) == len(held)

    assert _reconciler(load.sessions, load.lifecycle, later=_SETTLED).tick()
    assert _claims(load.sessions, owner_kind="installation") == []
    # Only the bookkeeping goes; the installation stays for its exact cleanup.
    assert [row.state for row in _installations(load.sessions)] == ["partial"]


@pytest.mark.parametrize("ended_as", ["cancelled", "retired"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_a_cancelled_or_retired_install_keeps_its_claim_until_cleanup(
    tmp_path: Path, ended_as: str
) -> None:
    load = _load(tmp_path)
    job_id = _installing(load)
    held = _claims(load.sessions, owner_kind="installation")
    with load.sessions.begin() as session:
        job = session.get(Job, job_id)
        assert job is not None
        if ended_as == "cancelled":
            job.state = "cancelled"
        else:
            job.state = "failed"
            job.result = {"cancel_requested": True, "cancelled": True}
        for installation in session.scalars(select(RecipeInstallation)):
            installation.state = "partial"
    _end_orders(load)

    # The write may still be running on a Spark: the exact cleanup owns it.
    _reconciler(load.sessions, load.lifecycle, later=_SETTLED).tick()
    assert len(_claims(load.sessions, owner_kind="installation")) == len(held)
    with load.sessions() as session:
        owner = reservation_owner(session, "installation", held[0].owner_id)
    assert "awaiting exact cleanup" in owner.describe()


def test_a_planned_install_is_kept_while_its_order_is_live_and_freed_after(
    tmp_path: Path,
) -> None:
    load = _load(tmp_path)
    assert _loop(
        load,
        lambda: any(row.state == "planned" for row in _installations(load.sessions)),
        complete=False,
    )
    held = _claims(load.sessions, owner_kind="installation")
    assert held
    # The order that prepared it is still running: it is live however long the
    # copy phases take.
    assert not _reconciler(load.sessions, load.lifecycle, later=_SETTLED).tick()
    assert len(_claims(load.sessions, owner_kind="installation")) == len(held)

    _end_orders(load)
    assert _reconciler(load.sessions, load.lifecycle, later=_SETTLED).tick()
    assert _claims(load.sessions, owner_kind="installation") == []


def test_retrying_a_failed_install_takes_back_the_claim_it_lost(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    first = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid.uuid4())
    )
    service.record_node_result(
        first.id, nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    service.record_node_result(
        first.id, nodes[1], succeeded=False, evidence={"code": "pull.failed"}
    )
    before = _claims(sessions, owner_kind="installation")
    assert before
    settled = OWNER_SETTLE_GRACE + timedelta(seconds=1)
    assert _reconciler(sessions, service, later=settled).tick()
    assert _claims(sessions, owner_kind="installation") == []

    service.retry(first.id, actor="admin", request_id=str(uuid.uuid4()))
    assert {claim.id for claim in _claims(sessions, owner_kind="installation")} == {
        claim.id for claim in before
    }
    # The retry is a live operation again.
    assert not _reconciler(sessions, service, later=settled).tick()


def test_a_claim_whose_owner_is_gone_is_released_and_an_unknown_kind_is_kept(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    ghost = str(uuid.uuid4())
    with sessions.begin() as session:
        for index, kind in enumerate(
            ("run", "recipe-build", "fleet-profile", "mystery")
        ):
            session.add(
                ResourceReservation(
                    node_id=nodes[0],
                    kind="disk",
                    resource_key=f"ghost-{index}",
                    amount_bytes=10,
                    owner_kind=kind,
                    owner_id=ghost,
                    state="active",
                    plan_digest="0" * 64,
                    created_at=NOW - timedelta(days=1),
                )
            )
    assert _reconciler(sessions, service).tick()
    assert [claim.owner_kind for claim in _claims(sessions)] == ["mystery"]


def test_a_stopped_run_releases_and_a_live_run_keeps_its_claims(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    run = started_recipe(
        sessions, service, installation.owner_id, nodes, request_id=str(uuid.uuid4())
    )
    held = _claims(sessions, owner_kind="run")
    assert held
    assert not _reconciler(sessions, service).tick()
    assert len(_claims(sessions, owner_kind="run")) == len(held)

    with sessions.begin() as session:
        row = session.get(RecipeRun, run.owner_id)
        assert row is not None
        row.state = "stopped"
    assert _reconciler(sessions, service).tick()
    assert _claims(sessions, owner_kind="run") == []


def test_an_install_with_nothing_issued_is_judged_only_after_the_settling_grace(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    plan = service.preview_install(mapping_id, build_id)
    first = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid.uuid4())
    )
    for node_id in nodes:
        service.record_node_result(
            first.id, node_id, succeeded=False, evidence={"code": "pull.failed"}
        )
    held = _claims(sessions, owner_kind="installation")
    assert held
    assert not _reconciler(sessions, service).tick()
    assert len(_claims(sessions, owner_kind="installation")) == len(held)
    after = OWNER_SETTLE_GRACE + timedelta(seconds=1)
    assert _reconciler(sessions, service, later=after).tick()
    assert _claims(sessions, owner_kind="installation") == []


@pytest.mark.parametrize("serving", [False, True])
def test_an_installed_claim_is_released_once_the_spark_reports_its_files(
    tmp_path: Path, serving: bool
) -> None:
    """Installed files are already subtracted from the reported free disk, so a
    claim kept for them counts the same bytes twice (an idle 264 GB install
    refused a load on a Spark with space). It goes once a Spark observation
    after completion includes them, but not while a workload still serves from
    it, and never before that observation."""

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    if serving:
        started_recipe(
            sessions,
            service,
            installation.owner_id,
            nodes,
            request_id=str(uuid.uuid4()),
        )
    held = [
        claim.id
        for claim in _claims(sessions, owner_kind="installation")
        if claim.kind == "disk"
    ]
    assert held
    after = MAX_INVENTORY_FUTURE_SKEW + timedelta(seconds=1)

    # The Spark has not reported since the install completed.
    _reconciler(sessions, service, later=after).tick()
    assert len(_claims(sessions, owner_kind="installation", kind="disk")) == len(held)

    _record_disk(sessions, nodes[0], at=NOW + after, free=2_000)
    _reconciler(sessions, service, later=after).tick()
    kept = _claims(sessions, owner_kind="installation", kind="disk")
    assert (len(kept) == len(held)) if serving else kept == []
    if serving:
        return
    # The installation itself is untouched.
    with sessions() as session:
        assert [row.state for row in session.scalars(select(RecipeInstallation))] == [
            "installed"
        ]


def test_disk_owner_release_restores_run_and_install_admission(tmp_path: Path) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    later = NOW + timedelta(seconds=1)
    _record_disk(sessions, nodes[0], at=later, free=2_000)
    service._clock = lambda: later
    with sessions.begin() as session:
        claim = ResourceReservation(
            node_id=nodes[0],
            kind="disk",
            resource_key="occupied",
            amount_bytes=1_900,
            owner_kind="fleet-profile",
            owner_id=str(uuid.uuid4()),
            state=ReservationState.ACTIVE,
            plan_digest="0" * 64,
            created_at=NOW,
        )
        session.add(claim)
        session.flush()
        claim_id = claim.id
    provider = _service(sessions, later, service, RecordingArtifactExecutor())
    assert not provider.preview(_request(sessions, nodes[0]), actor="admin").allowed
    assert not service.preview_install(mapping_id, build_id).allowed
    with sessions.begin() as session:
        claim = session.get(ResourceReservation, claim_id)
        assert claim is not None
        claim.state = ReservationState.RELEASED
    assert provider.preview(_request(sessions, nodes[0]), actor="admin").allowed
    assert service.preview_install(mapping_id, build_id).allowed
