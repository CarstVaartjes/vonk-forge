"""An admitted load whose accepted plan the present refuses is planned again."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import LifecycleState, UnknownOutcomeError, WaitReason
from vonk_control.fleet_profile_contract import FleetProfileInput, FleetProfilePreview
from vonk_control.fleet_profiles import (
    _persisted_profile_progress,
    build_production_fleet_profile_service,
)
from vonk_control.fleet_profiles.projection_support import _digest
from vonk_control.models import CatalogDocumentRevision, FleetProfileApplication
from vonk_control.operation_blockers import make_blocker
from vonk_control.recovery_policy import RECOVERY
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_recipe_operations import installed_recipe, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


def _installed_load(tmp_path: Path, inspector: CompleteArtifactInspector):
    """A recipe installed but not running, and a profile that starts it."""
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4()))
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=inspector,
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=0,
    )
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    service.bind_preparation_starter(
        lambda recipe_revision_id, *, actor, application_id=None: [
            make_blocker(
                "recipe_image.preparing",
                "Preparing the model and runtime image (prepare).",
                severity="info",
            )
        ]
    )
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Start installed",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "installed-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )
    now = [lifecycle._clock()]
    service._clock = lambda: now[0]
    return sessions, service, profile, now


def _live_applications(sessions) -> list[FleetProfileApplication]:
    with sessions() as session:
        return list(
            session.scalars(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.state.in_(("queued", "running", "backoff"))
                )
            )
        )


@pytest.mark.parametrize("looping_before_upgrade", [False, True])
def test_a_load_admitted_without_an_image_decision_recovers_by_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, looping_before_upgrade: bool
) -> None:
    """Catches an admitted load that retries a refused accepted plan forever.

    The load is admitted on a plan that records no runtime image for its start
    (as a Controller before #1639 admitted it).  Its exact step is refused; it
    is planned again from a fresh review that binds the prepared image, and
    that load issues the start.  No cancel, no new request.  A load an older
    Controller left retrying that refusal recovers the same way once this
    Controller observes it.
    """
    sessions, service, profile, now = _installed_load(
        tmp_path, CompleteArtifactInspector()
    )
    accepted = service.apply(profile.id, request_key=str(uuid4()), actor="admin")
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, accepted.id)
        assert row is not None and row.current_operation_id is None
        assert row.plan["preparation_decisions"]
        undecided = FleetProfilePreview.model_validate_json(
            json.dumps({**row.plan, "preparation_decisions": []})
        )
        row.plan = undecided.model_dump(mode="json")
        progress = _persisted_profile_progress(row)
        assert progress.intended_profile is not None
        progress.intended_profile.reviewed_plan_digest = _digest(
            undecided.reviewed_decision()
        )
        row.progress = progress.model_dump(mode="json")

    if looping_before_upgrade:
        # The older Controller retried the refused step instead of planning.
        monkeypatch.setattr(
            "vonk_control.fleet_profiles.reconcile._plan_no_longer_holds",
            lambda *_: False,
        )
        for _ in range(3):
            now[0] += timedelta(seconds=30)
            service.tick()
        waiting = service.application(accepted.id)
        assert waiting.state == "running"
        assert "profile image identity is missing" in (waiting.status_reason or "")
        monkeypatch.undo()

    for _ in range(8):
        now[0] += timedelta(seconds=30)
        service.tick()

    first = service.application(accepted.id)
    assert first.state == "superseded", first.status_reason
    (live,) = _live_applications(sessions)
    assert first.superseded_by == live.id
    successor = service.application(live.id)
    assert successor.progress.retry_of_application_id == accepted.id
    assert successor.state == "running", successor.status_reason
    assert [effect.state for effect in successor.progress.effects] == ["pending"]
    assert successor.wait is not None
    assert successor.wait.reason is WaitReason.RECEIPT_MISSING
    assert successor.blockers and all(
        item.code is WaitReason.RECEIPT_MISSING
        and item.operation is not None
        and item.operation.id != successor.id
        for item in successor.blockers
    )


@pytest.mark.parametrize("looping_before_upgrade", [False, True])
def test_repeated_stale_plan_reconciles_after_the_fault_clears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, looping_before_upgrade: bool
) -> None:
    """Repeated conflicts re-plan with bounded backoff until conditions clear."""
    inspector = CompleteArtifactInspector()
    sessions, service, profile, now = _installed_load(tmp_path, inspector)

    def refused(*_args: object, **_kwargs: object) -> object:
        raise UnknownOutcomeError(
            "accepted capacity no longer matches", reason=WaitReason.SCOPE_CHANGED
        )

    monkeypatch.setattr(
        "vonk_control.run_switch_operations.run_planning.accepted_profile_runtime_image",
        refused,
    )
    service.apply(profile.id, request_key=str(uuid4()), actor="admin")
    if looping_before_upgrade:
        with monkeypatch.context() as older:
            older.setattr(
                "vonk_control.fleet_profiles.reconcile._plan_no_longer_holds",
                lambda *_: False,
            )
            for _ in range(3):
                now[0] += timedelta(seconds=30)
                service.tick()
    for _ in range(12 * RECOVERY.max_failures):
        now[0] += timedelta(minutes=10)
        service.tick()
        if not _live_applications(sessions):
            break

    assert _live_applications(sessions)
    with sessions() as session:
        rows = list(session.scalars(select(FleetProfileApplication)))
    assert len(rows) > RECOVERY.max_failures
    assert not any(row.state == LifecycleState.FAILED for row in rows)
    monkeypatch.undo()
    # The same accepted intent is observed again; no new operator request.
    for _ in range(12):
        now[0] += timedelta(minutes=10)
        service.tick()
    assert _live_applications(sessions)
    # Planning now progressed into a real Run/Switch child, whose fake executor
    # is deliberately unfinished in this fixture.
    last = service.application(_live_applications(sessions)[-1].id)
    assert last.progress.effects and all(
        effect.operation_id is not None for effect in last.progress.effects
    )
