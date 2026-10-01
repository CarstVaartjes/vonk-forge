"""A failed attempt leaves nothing that stops the next one.

A two-Spark profile load is failed at each phase in turn, the way a Controller
or network fault ends it. The profile's next attempt (what automatic recovery
starts) must then reach a running workload with no manual cleanup: records the
failed attempt left with no effect on a Spark are released by the one residue
reconciler, and effects it did leave are reconciled by the automatic cleanup.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from vonk_control.attempt_residues import ADOPTION_WINDOW, AttemptResidueReconciler
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.models import (
    ArtifactDistributionAssignment,
    CatalogDocumentRevision,
    InstallationNode,
    Job,
    NodeInventorySnapshot,
    RecipeInstallation,
    ResourceReservation,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_routes import RecipeRouteService
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_profile_build_process_recovery import _complete_agent_work
from .test_recipe_operations import ConcurrentPublisher, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


def _load(tmp_path: Path):
    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(
        tmp_path, nodes=2
    )
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    planner = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    profiles = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=planner
    )
    residues = AttemptResidueReconciler(
        sessions,
        abandon_never_installed=lifecycle.abandon_never_installed,
        clock=lifecycle._clock,
    )
    profile = profiles.create(
        FleetProfileInput.model_validate(
            {
                "name": "Serve across both Sparks",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    assert profiles.preview(profile.id).allowed
    application = profiles.apply(
        profile.id, request_key=str(uuid.uuid4()), actor="admin"
    )
    return SimpleNamespace(
        sessions=sessions,
        lifecycle=lifecycle,
        planner=planner,
        profiles=profiles,
        residues=residues,
        worker=RecipeOperationWorker(
            sessions,
            RecipeRouteService(
                sessions,
                publisher=ConcurrentPublisher(),
                management_policy=ManagementAddressPolicy.parse("192.168.1.0/24"),
                clock=lifecycle._clock,
            ),
            clock=lifecycle._clock,
            retirement_cleanup=lifecycle.reconcile_retired_operations,
            residue_cleanup=residues.tick,
        ),
        application=application,
        nodes=nodes,
        root=tmp_path,
    )


def _switch(load) -> Job | None:
    with load.sessions() as session:
        return session.scalar(
            select(Job)
            .where(Job.kind == "recipe.run-switch.v2")
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )


def _loop(load, until, *, complete: bool = True, rounds: int = 40) -> bool:
    for _ in range(rounds):
        if until():
            return True
        load.planner.tick()
        load.profiles.tick()
        load.worker.tick()
        if complete:
            _complete_agent_work(load.sessions, load.lifecycle, load.root)
    return until()


def _at_phase(load, phase: str) -> bool:
    switch = _switch(load)
    if switch is None or switch.state not in {"queued", "running", "waiting"}:
        return False
    progress = switch.result or {}
    return phase in {progress.get("phase"), progress.get("subphase")}


def _leave_room_for_one_attempt(load) -> None:
    """Free disk fits one attempt: anything the last one leaked blocks the next."""

    with load.sessions.begin() as session:
        claim = session.scalar(
            select(ResourceReservation)
            .join(
                RecipeInstallation,
                RecipeInstallation.id == ResourceReservation.owner_id,
            )
            .where(
                ResourceReservation.owner_kind == "installation",
                ResourceReservation.state == "active",
                RecipeInstallation.state == "planned",
            )
        )
        if claim is None:
            return  # an installed or running effect is reused, not a leak
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.disk_free_bytes = claim.amount_bytes + claim.amount_bytes // 2


def _fail_switch(load) -> None:
    switch = _switch(load)
    assert switch is not None
    with load.sessions.begin() as session:
        row = session.get(Job, switch.id)
        assert row is not None
        row.state = "failed"
        row.status_reason = "attempt ended by a fault"


@pytest.mark.parametrize(
    "phase", ["runtime-plan", "target-copy", "verify", "runtime-install", "start"]
)
def test_the_next_attempt_proceeds_after_a_failure_at_every_phase(
    tmp_path: Path, phase: str
) -> None:
    load = _load(tmp_path)
    # Agents answer only once the attempt is past its own install plan, so
    # the earlier phases fail with nothing issued to a Spark.
    late = phase in {"start"}
    assert _loop(load, lambda: _at_phase(load, phase), complete=late), phase
    _fail_switch(load)
    _leave_room_for_one_attempt(load)
    # The failed attempt's own record settles (automatic recovery is due).
    _loop(load, lambda: False, complete=False, rounds=2)
    retried = load.profiles.retry(
        load.application.id, request_key=str(uuid.uuid4()), actor="admin"
    )
    assert _loop(
        load,
        lambda: load.profiles.application(retried.id).state == "succeeded",
    ), load.profiles.application(retried.id).status_reason
    with load.sessions() as session:
        live = [
            row
            for row in session.scalars(select(RecipeInstallation))
            if row.state != "uninstalled"
        ]
        assert len(live) == 1 and live[0].state == "installed"
        claims = {
            row.owner_id
            for row in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "installation",
                    ResourceReservation.state == "active",
                )
            )
        }
        assert claims <= {live[0].id}


def test_an_install_that_fails_on_one_member_is_cleaned_and_retried(
    tmp_path: Path,
) -> None:
    """A proven partial effect goes through the automatic exact cleanup."""

    load = _load(tmp_path)

    def install_job() -> Job | None:
        with load.sessions() as session:
            return session.scalar(select(Job).where(Job.kind == "recipe.install"))

    assert _loop(load, lambda: install_job() is not None, complete=False)
    install = install_job()
    assert install is not None
    load.lifecycle.record_node_result(
        install.id, load.nodes[0], succeeded=True, evidence={"installed_bytes": 120}
    )
    load.lifecycle.record_node_result(
        install.id,
        load.nodes[1],
        succeeded=False,
        evidence={"reason": "disk write failed", "failure_kind": "integrity-failure"},
    )

    def settled() -> bool:
        switch = _switch(load)
        return load.profiles.application(load.application.id).state in {
            "failed",
            "queued",
        } and (switch is None or switch.state == "failed")

    assert _loop(load, settled)
    retried = load.profiles.retry(
        load.application.id, request_key=str(uuid.uuid4()), actor="admin"
    )
    assert _loop(
        load,
        lambda: load.profiles.application(retried.id).state == "succeeded",
        rounds=60,
    ), load.profiles.application(retried.id).status_reason


def _planned_left_by_failed_copy(load) -> str:
    assert _loop(load, lambda: _at_phase(load, "target-copy"), complete=False)
    _fail_switch(load)
    load.planner.tick()
    load.profiles.tick()  # the application fails; nothing released yet
    with load.sessions() as session:
        installation = session.scalar(select(RecipeInstallation))
        assert installation is not None and installation.state == "planned"
        planned_id = installation.id
    # Only one attempt's worth of disk remains free: a claim that held
    # admission would keep every later attempt "waiting for capacity".
    _leave_room_for_one_attempt(load)
    return planned_id


def test_the_next_attempt_adopts_the_plan_a_failed_copy_left(tmp_path: Path) -> None:
    """The NAS case: adoption is not gated behind a wait for its own claim."""

    load = _load(tmp_path)
    planned_id = _planned_left_by_failed_copy(load)
    retried = load.profiles.retry(
        load.application.id, request_key=str(uuid.uuid4()), actor="admin"
    )
    assert _loop(
        load,
        lambda: load.profiles.application(retried.id).state == "succeeded",
    ), load.profiles.application(retried.id).status_reason
    with load.sessions() as session:
        installations = tuple(session.scalars(select(RecipeInstallation)))
        assert [(row.id, row.state) for row in installations] == [
            (planned_id, "installed")
        ]


def test_a_plan_nobody_adopts_is_released_after_the_adoption_window(
    tmp_path: Path,
) -> None:
    load = _load(tmp_path)
    planned_id = _planned_left_by_failed_copy(load)
    # Within the window the plan waits for the next attempt.
    assert not load.residues.tick()
    later = AttemptResidueReconciler(
        load.sessions,
        abandon_never_installed=load.lifecycle.abandon_never_installed,
        clock=lambda: load.lifecycle._clock() + ADOPTION_WINDOW + timedelta(seconds=1),
    )
    assert later.tick()
    with load.sessions() as session:
        installation = session.get(RecipeInstallation, planned_id)
        assert installation is not None and installation.state == "uninstalled"
        assert all(
            member.state == "uninstalled"
            for member in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == planned_id
                )
            )
        )
        assert not session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == planned_id,
                ResourceReservation.state == "active",
            )
        )
        assert not session.scalar(
            select(ArtifactDistributionAssignment).where(
                ArtifactDistributionAssignment.state == "active"
            )
        )
    retried = load.profiles.retry(
        load.application.id, request_key=str(uuid.uuid4()), actor="admin"
    )
    assert _loop(
        load,
        lambda: load.profiles.application(retried.id).state == "succeeded",
    ), load.profiles.application(retried.id).status_reason


def test_a_plan_the_next_attempt_replaced_is_released_without_the_wait(
    tmp_path: Path,
) -> None:
    """The next attempt plans differently (another image or choice).

    Its plan digest differs from the cancelled attempt's, so nothing can adopt
    the older plan. It must not outlive the attempts that follow it and show
    as a second installation of the same recipe on the same Sparks.
    """

    load = _load(tmp_path)
    old_id = _planned_left_by_failed_copy(load)
    with load.sessions.begin() as session:
        old = session.get(RecipeInstallation, old_id)
        assert old is not None
        # The next attempt no longer plans what this one did.
        old.plan_digest = "f" * 64
        old.plan = {**old.plan, "plan_digest": old.plan_digest}
        old.created_at -= timedelta(minutes=1)  # the cancelled attempt came first
        # Room for both: this case is about the record, not about capacity.
        for snapshot in session.scalars(select(NodeInventorySnapshot)):
            snapshot.disk_free_bytes = snapshot.disk_total_bytes
    retried = load.profiles.retry(
        load.application.id, request_key=str(uuid.uuid4()), actor="admin"
    )
    assert _loop(
        load,
        lambda: load.profiles.application(retried.id).state == "succeeded",
    ), load.profiles.application(retried.id).status_reason
    assert _loop(load, lambda: not load.residues.tick(), complete=False, rounds=3)
    with load.sessions() as session:
        live = [
            (row.id, row.state)
            for row in session.scalars(select(RecipeInstallation))
            if row.state != "uninstalled"
        ]
        assert len(live) == 1 and live[0][0] != old_id and live[0][1] == "installed"
        assert session.get(RecipeInstallation, old_id).state == "uninstalled"


def test_a_plan_a_newer_revision_of_the_recipe_replaced_is_released(
    tmp_path: Path,
) -> None:
    """The reload follows the recipe's newest revision, not the cancelled one.

    A new revision's plan can never match the old plan's digest. The cancelled
    attempt's plan is released once a later installation of the same recipe
    exists on the same Sparks, and the newer installation is left alone.
    """

    load = _load(tmp_path)
    old_id = _planned_left_by_failed_copy(load)
    with load.sessions.begin() as session:
        old = session.get(RecipeInstallation, old_id)
        assert old is not None
        current = session.get(CatalogDocumentRevision, old.recipe_revision_id)
        assert current is not None
        document = {**current.document, "newer": True}
        digest = hashlib.sha256(
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        newer = CatalogDocumentRevision(
            id=str(uuid.uuid4()),
            document_id=current.document_id,
            kind="recipe",
            publisher=current.publisher,
            slug=current.slug,
            revision_number=current.revision_number + 1,
            schema_version=2,
            state="active",
            document=document,
            content_digest=digest,
            execution_key=current.execution_key,
            created_by="test",
            created_at=current.created_at,
        )
        session.add(newer)
        session.flush()
        later = RecipeInstallation(
            **{
                column.key: getattr(old, column.key)
                for column in RecipeInstallation.__table__.columns
            }
        )
        later.id = str(uuid.uuid4())
        later.recipe_revision_id = newer.id
        later.plan_digest = "e" * 64
        later.state = "installed"
        later.created_at = old.created_at + timedelta(minutes=1)
        session.add(later)
        session.flush()
        for member in session.scalars(
            select(InstallationNode).where(InstallationNode.installation_id == old_id)
        ).all():
            session.add(
                InstallationNode(
                    **{
                        column.key: getattr(member, column.key)
                        for column in InstallationNode.__table__.columns
                    }
                    | {"id": str(uuid.uuid4()), "installation_id": later.id}
                )
            )
        later_id = later.id

    assert load.residues.tick()
    with load.sessions() as session:
        released = session.get(RecipeInstallation, old_id)
        kept = session.get(RecipeInstallation, later_id)
        assert released is not None and released.state == "uninstalled"
        assert kept is not None and kept.state == "installed"


def test_a_record_an_active_attempt_owns_is_left_alone(tmp_path: Path) -> None:
    load = _load(tmp_path)
    assert _loop(load, lambda: _at_phase(load, "target-copy"), complete=False)
    assert not load.residues.tick()
    with load.sessions() as session:
        installation = session.scalar(select(RecipeInstallation))
        assert installation is not None and installation.state == "planned"


def test_reconcile_discards_a_plan_that_never_reached_a_node(tmp_path: Path) -> None:
    """``recipe installation reconcile`` handles a planned installation too."""

    from vonk_control.run_switch_contract import (
        RunSwitchCleanupApplyRequest,
        RunSwitchCleanupPreviewRequest,
    )

    load = _load(tmp_path)
    assert _loop(load, lambda: _at_phase(load, "target-copy"), complete=False)
    _fail_switch(load)
    load.planner.tick()
    with load.sessions() as session:
        installation = session.scalar(select(RecipeInstallation))
        assert installation is not None and installation.state == "planned"
        installation_id = installation.id
    review = load.planner.preview_cleanup(
        RunSwitchCleanupPreviewRequest(
            installation_id=installation_id, cleanup_mode="reconcile"
        ),
        actor="admin",
    )
    assert review.allowed, [reason.code for reason in review.blockers]
    assert review.cleanup_disposition == "abandon"
    operation = load.planner.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation_id,
            cleanup_mode="reconcile",
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    for _ in range(6):
        load.planner.tick()
    assert load.planner.get(operation.operation_id).state == "succeeded"
    with load.sessions() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None and installation.state == "uninstalled"


def test_an_unexpected_advance_failure_is_shown_and_backed_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a silent endless retry: the wait names the failure and next try."""

    load = _load(tmp_path)
    assert _loop(load, lambda: _at_phase(load, "target-copy"), complete=False)
    switch = _switch(load)
    assert switch is not None

    def broken(_operation_id: str) -> bool:
        raise RuntimeError("phase receipt could not be stored")

    monkeypatch.setattr(load.planner, "_advance", broken)
    load.planner.tick()
    view = load.planner.get(switch.id)
    assert view.state == "running"
    assert "phase receipt could not be stored" in (view.status_reason or "")
    assert view.result is not None
    assert view.result.observation_due_at is not None
    assert [blocker.code for blocker in view.result.blockers] == [
        "run-switch.phase-retry"
    ]
    # Backed off: the next tick does not retry it straight away.
    calls: list[str] = []
    monkeypatch.setattr(load.planner, "_advance", lambda job_id: calls.append(job_id))
    load.planner.tick()
    assert calls == []
