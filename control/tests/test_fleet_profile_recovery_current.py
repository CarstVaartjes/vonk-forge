"""Profile retry and replay through the production Run/Switch child."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileInput,
)
from vonk_control.fleet_profiles import (
    FleetProfileConflict,
    build_production_fleet_profile_service,
)
from vonk_control.models import (
    AgentNode,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    Job,
)
from vonk_control.run_switch_operations import RunSwitchOperationService

from .test_fleet_profiles import _uuid
from .test_recipe_operations import setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


def _failed_profile(tmp_path: Path):
    """Fail a persisted Run/Switch child, leaving its parent recoverable."""

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
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    service = build_production_fleet_profile_service(
        sessions,
        clock=lifecycle._clock,
        run_switch_operations=run_switch,
    )
    desired = FleetProfileInput.model_validate(
        {
            "name": "Recover failed child",
            "assignments": [
                {
                    "recipe_selector": f"vonk-forge/{revision.slug}",
                    "spark_ids": list(nodes),
                    "desired_state": "running",
                    "assignment_name": "recover-chat",
                }
            ],
        }
    )
    profile = service.create(desired, actor="admin")
    preview = service.preview(profile.id)
    assert preview.allowed
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(800),
        actor="admin",
    )
    assert service.tick()
    with sessions.begin() as session:
        child = session.scalar(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        assert child is not None
        child_id = child.id
        child.state = "failed"
        child.status_reason = "temporary runtime dependency unavailable"
    assert service.tick()
    assert service.application(application.id).state == "failed"
    return sessions, lifecycle, service, profile, desired, application, child_id, nodes


def test_explicit_retry_preserves_failed_child_recovery_and_replay(
    tmp_path: Path,
) -> None:
    sessions, _lifecycle, service, _profile, _desired, first, first_child, _nodes = (
        _failed_profile(tmp_path)
    )
    failed = service.application(first.id)
    assert service.retry_eligible(first.id)

    second = service.retry(first.id, request_key=_uuid(801), actor="admin")
    assert second.retry_of_application_id == first.id
    assert second.attempt == 2
    assert service.application_by_request_key(_uuid(801), actor="admin") == second

    assert not service.retry_eligible(first.id)
    with pytest.raises(FleetProfileConflict, match="superseded"):
        service.retry(first.id, request_key=_uuid(802), actor="admin")

    assert service.tick()
    with sessions() as session:
        children = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.run-switch.v2"))
        )
    assert len(children) == 2
    assert first_child in {child.id for child in children}
    assert service.application(first.id) == failed

    second_child_id = next(child.id for child in children if child.id != first_child)
    with sessions.begin() as session:
        second_child = session.get(Job, second_child_id)
        assert second_child is not None
        second_child.state = "failed"
        second_child.status_reason = "dependency still unavailable"
    assert service.tick()
    third = service.retry(second.id, request_key=_uuid(807), actor="admin")
    assert third.retry_of_application_id == second.id
    assert third.attempt == 3


def test_recovery_waits_for_original_active_child_and_reports_missing_child(
    tmp_path: Path,
) -> None:
    sessions, _lifecycle, service, _profile, _desired, first, child_id, _nodes = (
        _failed_profile(tmp_path)
    )

    def recover():
        return service.retry(first.id, request_key=_uuid(803), actor="admin")

    with sessions.begin() as session:
        child = session.get(Job, child_id)
        assert child is not None
        child.state = "running"
        child.status_reason = None
    with pytest.raises(FleetProfileConflict, match="still active"):
        recover()

    with sessions.begin() as session:
        child = session.get(Job, child_id)
        assert child is not None
        session.delete(child)
    with pytest.raises(FleetProfileConflict, match="must be reconciled"):
        recover()


def test_retry_rejects_revoked_scope_and_changed_profile_intent(tmp_path: Path) -> None:
    sessions, lifecycle, service, profile, desired, first, _child, nodes = (
        _failed_profile(tmp_path)
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.revoked_at = lifecycle._clock()
    with pytest.raises(FleetProfileConflict, match="blocks"):
        service.retry(first.id, request_key=_uuid(804), actor="admin")

    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.revoked_at = None
    changed_assignment = desired.assignments[0].model_copy(
        update={"assignment_name": "different-chat"}
    )
    service.update(
        profile.id,
        desired.model_copy(
            update={
                "assignments": [changed_assignment],
                "expected_revision": profile.revision,
            }
        ),
        actor="admin",
    )
    assert not service.retry_eligible(first.id)
    with pytest.raises(FleetProfileConflict, match="obsolete"):
        service.retry(first.id, request_key=_uuid(804), actor="admin")


def test_repeated_idle_loads_preserve_distinct_noop_receipts(tmp_path: Path) -> None:
    _sessions, _lifecycle, service, _profile, _desired, _first, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    idle = service.create(
        FleetProfileInput(name="Keep cached", installation_policy="keep-cached"),
        actor="admin",
    )
    first = service.load(
        idle.number,
        request_key=_uuid(805),
        actor="admin",
        expected_plan_digest=service.preview(idle.id).plan_digest,
    )
    second = service.load(
        idle.number,
        request_key=_uuid(806),
        actor="admin",
        expected_plan_digest=service.preview(idle.id).plan_digest,
    )
    assert first.state == second.state == "succeeded"
    assert first.id != second.id
    assert first.result is not None and first.result.changed is False
    assert second.result is not None and second.result.changed is False
    assert (
        service.load(
            idle.number,
            request_key=_uuid(806),
            actor="admin",
            expected_plan_digest=service.preview(idle.id).plan_digest,
        )
        == second
    )


def test_intent_checks_do_not_resolve_storage_inside_coordination(tmp_path: Path):
    sessions, _lifecycle, service, profile, _desired, first, _child, _nodes = (
        _failed_profile(tmp_path)
    )

    def unavailable_cache(**_kwargs):
        raise AssertionError("intent coordination must not consult artifact storage")

    service._cache_resolver = unavailable_cache
    with sessions.begin() as session:
        application = session.get(FleetProfileApplication, first.id)
        assert application is not None
        progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(application.progress), strict=True
        )
        assert not service._superseding_intent(session, application, progress)
        assert service._retry_eligible(session, application)
        saved = session.get(FleetProfile, profile.id)
        assert saved is not None
        saved.name = "New saved intent"
        saved.revision += 1
        assert service._superseding_intent(session, application, progress)
        assert not service._retry_eligible(session, application)
