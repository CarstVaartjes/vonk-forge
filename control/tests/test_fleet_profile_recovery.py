"""Recovery drives the persisted coordinator and observes actual lifecycle effects."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.fleet_profiles import FleetProfileConflict, FleetProfileService
from vonk_control.models import (
    AgentNode,
    Base,
    FleetProfileApplication,
    RecipeInstallation,
)

from .test_fleet_profiles import (
    NOW,
    _input,
    _node_id,
    _ProfileLifecycleSimulator,
    _seed,
    _uuid,
)


class FailingStart(_ProfileLifecycleSimulator):
    fail = True

    def start(self, plan, **kwargs):
        if self.fail:
            raise RuntimeError("Runtime temporarily unavailable")
        return super().start(plan, **kwargs)


def setup_recovery(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'recovery.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    _recipe, revision = _seed(sessions)
    operations = FailingStart(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, recipe_operations=operations
    )
    profile = service.create(_input(revision), actor="admin")
    preview = service.preview(profile.id)
    application = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(800),
        actor="admin",
    )
    finish(service, application.id)
    assert service.application(application.id).state == "failed"
    return sessions, operations, service, profile, application


def finish(service, application_id):
    for _ in range(20):
        if service.application(application_id).state in {"failed", "succeeded"}:
            return
        assert service.tick()
    pytest.fail("profile coordinator did not finish")


def test_retry_reconciles_partial_completion_and_survives_restart(tmp_path):
    sessions, operations, service, _profile, original = setup_recovery(tmp_path)
    failed = service.application(original.id)
    assert failed.progress.completed_steps == 4
    assert failed.progress.step_results
    with sessions() as session:
        installation_id = session.scalar(select(RecipeInstallation.id))
    assert service.retry_eligible(original.id)
    retry = service.retry(original.id, request_key=_uuid(801), actor="admin")
    assert retry.id != original.id
    assert retry.retry_of_application_id == original.id
    assert retry.attempt == 2
    assert retry.total_steps == 1
    assert retry.progress.intended_profile == failed.progress.intended_profile
    with sessions() as session:
        assert [
            step["kind"]
            for step in session.get(FleetProfileApplication, retry.id).plan["steps"]
        ] == ["start"]
    assert not service.retry_eligible(original.id)
    with pytest.raises(FleetProfileConflict, match="superseded"):
        service.retry(original.id, request_key=_uuid(802), actor="admin")
    persisted_url = sessions.kw["bind"].url
    sessions.kw["bind"].dispose()
    sessions = sessionmaker(create_engine(persisted_url), expire_on_commit=False)
    operations.sessions = sessions
    restarted = FleetProfileService(
        sessions, clock=lambda: NOW + timedelta(seconds=1), recipe_operations=operations
    )
    assert restarted.retry(original.id, request_key=_uuid(801), actor="admin") == retry
    operations.fail = False
    operations.events.clear()
    finish(restarted, retry.id)
    final = restarted.application(retry.id)
    assert final.state == "succeeded"
    assert operations.events == ["start"]
    assert final.result.completed_steps == 1
    assert restarted.application(original.id) == failed
    with sessions() as session:
        assert list(session.scalars(select(RecipeInstallation.id))) == [installation_id]
    assert restarted.retry(original.id, request_key=_uuid(801), actor="admin") == final
    with pytest.raises(FleetProfileConflict, match="request key"):
        restarted.retry(retry.id, request_key=_uuid(801), actor="admin")


def test_failed_retry_can_itself_be_retried_without_replaying_completed_work(tmp_path):
    _sessions, operations, service, _profile, original = setup_recovery(tmp_path)
    retry = service.retry(original.id, request_key=_uuid(801), actor="admin")
    finish(service, retry.id)
    assert service.retry_eligible(retry.id)
    third = service.retry(retry.id, request_key=_uuid(802), actor="admin")
    assert third.attempt == 3
    operations.fail = False
    finish(service, third.id)
    assert service.application(third.id).state == "succeeded"


def test_recovery_rejects_obsolete_intent_and_revoked_scope(tmp_path):
    sessions, _operations, service, profile, original = setup_recovery(tmp_path)
    with sessions.begin() as session:
        session.get(AgentNode, _node_id(1)).revoked_at = NOW
    with pytest.raises(FleetProfileConflict, match="blocks"):
        service.retry(original.id, request_key=_uuid(801), actor="admin")
    with sessions.begin() as session:
        session.get(AgentNode, _node_id(1)).revoked_at = None
    changed = _input(_uuid(2), name="New intent")
    service.update(profile.id, changed, actor="admin")
    assert not service.retry_eligible(original.id)
    with pytest.raises(FleetProfileConflict, match="obsolete"):
        service.retry(original.id, request_key=_uuid(801), actor="admin")


def test_admitted_application_executes_immutable_assignment_after_profile_edit(
    tmp_path,
):
    _sessions, operations, service, profile, original = setup_recovery(tmp_path)
    retry = service.retry(original.id, request_key=_uuid(801), actor="admin")
    changed = _input(_uuid(2)).model_copy(
        update={
            "assignments": [
                _input(_uuid(2)).assignments[0].model_copy(
                    update={"assignment_name": "different-chat"}
                )
            ]
        }
    )
    service.update(profile.id, changed, actor="admin")
    operations.fail = False
    finish(service, retry.id)
    assert service.application(retry.id).state == "succeeded"
    assert service._application_assignments(retry.id)[0].alias == "studio-chat"


def test_retry_already_reconciled_fleet_returns_real_noop_receipt(tmp_path):
    sessions, operations, service, profile, original = setup_recovery(tmp_path)
    with sessions() as session:
        installation_id = session.scalar(select(RecipeInstallation.id))
    operations.fail = False
    plan = operations.preview_run(installation_id, "studio-chat")
    operations.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=_uuid(810)
    )
    operations.events.clear()
    retry = service.retry(original.id, request_key=_uuid(801), actor="admin")
    assert retry.state == "succeeded"
    assert retry.total_steps == 0
    assert retry.result.changed is False
    assert operations.events == []


def test_terminal_application_contract_rejects_contradictory_receipts(tmp_path):
    import json

    from pydantic import ValidationError
    from vonk_control.fleet_profile_contract import FleetProfileApplicationView

    _sessions, _operations, service, _profile, original = setup_recovery(tmp_path)
    failed = service.application(original.id).model_dump(mode="json")
    for changes in (
        {"status_reason": None},
        {"attempt": 2},
        {"state": "succeeded", "status_reason": None, "result": None},
        {"state": "succeeded", "result": {"changed": True, "completed_steps": 1}},
    ):
        with pytest.raises(ValidationError):
            FleetProfileApplicationView.model_validate_json(
                json.dumps({**failed, **changes})
            )


def test_retry_does_not_abandon_a_still_active_child_after_poll_failure(tmp_path):
    sessions, operations, service, _profile, original = setup_recovery(tmp_path)
    child_id = _uuid(820)
    operations.operations[child_id] = SimpleNamespace(id=child_id, state="running")
    with sessions.begin() as session:
        session.get(
            FleetProfileApplication, original.id
        ).current_operation_id = child_id
    with pytest.raises(FleetProfileConflict, match="still active"):
        service.retry(original.id, request_key=_uuid(801), actor="admin")
    del operations.operations[child_id]
    with pytest.raises(FleetProfileConflict, match="must be reconciled"):
        service.retry(original.id, request_key=_uuid(801), actor="admin")
