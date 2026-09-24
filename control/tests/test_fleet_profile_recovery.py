"""Recovery drives the persisted coordinator and observes actual lifecycle effects."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from vonk_control.fleet_profiles import FleetProfileConflict

from .test_fleet_profile_recovery_current import _failed_profile
from .test_fleet_profiles import _uuid


def test_profile_edit_supersedes_unissued_assignment_after_failed_load(
    tmp_path,
):
    from vonk_control.models import Job

    sessions, _lifecycle, service, profile, desired, original, _child, _nodes = (
        _failed_profile(tmp_path)
    )
    retry = service.retry(original.id, request_key=_uuid(801), actor="admin")
    changed = desired.model_copy(
        update={
            "expected_revision": profile.revision,
            "assignments": [
                desired.assignments[0].model_copy(
                    update={"assignment_name": "different-chat"}
                )
            ],
        }
    )
    service.update(profile.id, changed, actor="admin")
    assert service.tick()
    assert service.application(retry.id).state == "cancelled"
    assert "replaced" in (service.application(retry.id).status_reason or "")
    with sessions() as session:
        assert (
            len(
                tuple(
                    session.scalars(
                        select(Job).where(Job.kind == "recipe.run-switch.v2")
                    )
                )
            )
            == 1
        )


def test_retry_does_not_override_newer_direct_workload_intent(tmp_path):
    from vonk_control.models import ClusterMapping, RecipeBuild

    from .test_recipe_operations import installed_recipe, started_recipe

    sessions, lifecycle, service, _profile, _desired, original, _child, nodes = (
        _failed_profile(tmp_path)
    )
    with sessions() as session:
        mapping_id = session.scalar(select(ClusterMapping.id))
        build_id = session.scalar(select(RecipeBuild.id))
    assert mapping_id is not None and build_id is not None
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=_uuid(809)
    )
    started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id=_uuid(810),
        alias="recover-chat",
    )
    with pytest.raises(FleetProfileConflict, match="superseded"):
        service.retry(original.id, request_key=_uuid(801), actor="admin")


def test_terminal_application_contract_rejects_contradictory_receipts(tmp_path):

    from pydantic import ValidationError
    from vonk_control.fleet_profile_contract import FleetProfileApplicationView

    _sessions, _lifecycle, service, _profile, _desired, original, _child, _nodes = (
        _failed_profile(tmp_path)
    )
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
