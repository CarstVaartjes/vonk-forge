"""Profile retry and replay through the production Run/Switch child."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
    AgentOperation,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    Job,
)
from vonk_control.recovery_policy import RecoveryPolicy
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


def test_recovery_reconciles_active_or_missing_child_without_operator_gate(
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
    active_recovery = recover()
    assert active_recovery.id != first.id
    assert active_recovery.state in {"queued", "running", "succeeded"}

    missing_child_path = tmp_path / "missing-child"
    missing_child_path.mkdir()
    sessions, _lifecycle, service, _profile, _desired, first, child_id, _nodes = (
        _failed_profile(missing_child_path)
    )
    with sessions.begin() as session:
        child = session.get(Job, child_id)
        assert child is not None
        session.delete(child)
    missing_recovery = service.retry(first.id, request_key=_uuid(813), actor="admin")
    assert missing_recovery.id != first.id
    assert missing_recovery.state in {"queued", "running", "succeeded"}


def test_retry_rejects_revoked_scope_but_uses_accepted_profile_snapshot(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, service, profile, desired, first, _child, nodes = (
        _failed_profile(tmp_path)
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        node.revoked_at = lifecycle._clock()
    with pytest.raises(FleetProfileConflict, match="Fleet scope changed"):
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
    assert service.retry_eligible(first.id)
    retry = service.retry(first.id, request_key=_uuid(804), actor="admin")
    assert retry.retry_of_application_id == first.id
    assert retry.profile_digest == first.profile_digest


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
        assert not service._superseding_intent(session, application, progress)
        assert service._retry_eligible(session, application)


def _park_exhausted_application(
    sessions, application, child_id, nodes, *, attempt: int
):
    """Re-park a real Run/Switch child with one bounded exhausted operation.

    The child and its application are left exactly as a spent retry budget
    leaves them: parked, non-terminal, and still holding the node until an
    operator decision or a newer intent replaces them.
    """

    parked_id = str(uuid.uuid4())
    with sessions.begin() as session:
        child = session.get(Job, child_id)
        assert child is not None
        child.state = "waiting-for-operator"
        child.status_reason = "interrupted before the exact effect was observed"
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "waiting-for-operator"
        row.status_reason = "interrupted before the exact effect was observed"
        session.add(
            AgentOperation(
                id=parked_id,
                parent_job_id=child_id,
                node_id=nodes[0],
                kind="recipe.start",
                payload_digest="a" * 64,
                payload={},
                authority_revision="b" * 64,
                state="waiting-for-operator",
                status_reason=(
                    f"exact recipe.start retry budget exhausted at attempt {attempt}"
                ),
                current_attempt=attempt,
                created_at=datetime(2020, 1, 1, tzinfo=UTC),
                updated_at=datetime(2020, 1, 1, tzinfo=UTC),
            )
        )
    return parked_id


def test_new_intent_supersedes_a_parked_exhausted_application(tmp_path: Path) -> None:
    """A newer authorized load replaces a parked order instead of queueing behind it."""

    sessions, _lifecycle, service, profile, _desired, first, child_id, nodes = (
        _failed_profile(tmp_path)
    )
    _park_exhausted_application(
        sessions,
        first,
        child_id,
        nodes,
        attempt=RecoveryPolicy().max_failures,
    )

    second = service.load(
        profile.number,
        request_key=_uuid(810),
        actor="admin",
        expected_plan_digest=service.preview(profile.id).plan_digest,
    )

    assert second.id != first.id
    assert second.state == "queued"
    ended = service.application(first.id)
    assert ended.state == "cancelled"
    assert "replaced by a later scoped intent" in (ended.status_reason or "")


def test_a_changed_saved_draft_does_not_cancel_accepted_parked_application(
    tmp_path: Path,
) -> None:
    """A parked order is revisited once the saved profile it bound has changed.

    The parked state owns no live step, so the advancement path never sees it.
    Without observing it, an edit to the saved profile would leave the order
    waiting forever even though its recorded intent is already obsolete.
    """

    sessions, _lifecycle, service, profile, desired, first, child_id, nodes = (
        _failed_profile(tmp_path)
    )
    _park_exhausted_application(
        sessions,
        first,
        child_id,
        nodes,
        attempt=RecoveryPolicy().max_failures,
    )
    changed = desired.assignments[0].model_copy(
        update={"assignment_name": "different-chat"}
    )
    service.update(
        profile.id,
        desired.model_copy(
            update={"assignments": [changed], "expected_revision": profile.revision}
        ),
        actor="admin",
    )

    assert service.tick() is True

    ended = service.application(first.id)
    assert ended.state in {"cancelled", "failed", "queued", "running"}
    assert ended.state != "waiting-for-operator"
    assert ended.status_reason


def test_pending_admission_is_not_cancelled_by_parked_child_observer(
    tmp_path: Path,
) -> None:
    """Admission retry owns parked rows until a workload intent is bound."""

    sessions, lifecycle, service, _profile, _desired, application, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(row.progress), strict=True
        )
        progress_data = progress.model_dump(mode="json")
        progress_data["admission_pending"] = True
        progress_data["admission_attempt"] = 1
        progress_data["admission_retry_at"] = (
            lifecycle._clock() + timedelta(hours=1)
        ).isoformat()
        progress_data["workload_intent_ordinal"] = None
        row.progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(progress_data), strict=True
        ).model_dump(mode="json")
        row.state = "waiting-for-operator"
        row.status_reason = "Profile admission is waiting for the active workload owner"

    assert service.tick() is False
    parked = service.application(application.id)
    assert parked.state == "waiting-for-operator"
    assert parked.progress.admission_pending is True


def test_exhausted_profile_retry_keeps_an_automatic_due_time(
    tmp_path: Path,
) -> None:
    """Automatic retry has a due time and does not stop at the old retry cap."""

    sessions, lifecycle, service, _profile, _desired, first, child_id, nodes = (
        _failed_profile(tmp_path)
    )
    _park_exhausted_application(
        sessions,
        first,
        child_id,
        nodes,
        attempt=RecoveryPolicy().max_failures + 10,
    )
    due = lifecycle._clock() + timedelta(minutes=1)
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(row.progress), strict=True
        ).model_dump(mode="json")
        progress["retry_due_at"] = due.isoformat()
        row.progress = FleetProfileApplicationProgress.model_validate_json(
            canonical_message(progress), strict=True
        ).model_dump(mode="json")

    assert service._automatic_profile_recovery(lifecycle._clock()) is None
    candidate = service._automatic_profile_recovery(due + timedelta(seconds=1))
    assert candidate == (first.id, "admin")
    # Scheduling recovery does not rewrite the parked row into a new state.
    assert service.application(first.id).state == "waiting-for-operator"
