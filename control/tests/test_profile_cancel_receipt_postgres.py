"""Cancellation acceptance always has a durable receipt or a typed refusal."""

from datetime import timedelta
from uuid import uuid4

import pytest
from vonk_agent_protocol import LifecycleState, ProfileReasonCode
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileAssignmentInput,
    FleetProfileInput,
)
from vonk_control.fleet_profiles import _persisted_profile_progress
from vonk_control.models import FleetProfileApplication, FleetProfileSelection, Job

from .lifecycle_simulation.world import SOLO, build_world
from .test_fleet_profile_api import _client, _headers
from .test_fleet_profile_cancel import _application


def test_cancel_failed_child_during_retry_records_receipt_and_finishes(
    tmp_path, postgres_engine
) -> None:
    """Catches dropping a cancel while its failed child awaits another observation."""
    sessions, run_switch, profiles, profile, application, _adapter, _nodes = (
        _application(tmp_path, engine=postgres_engine)
    )
    assert profiles.tick()
    active = profiles.application(application.id)
    adapter = active.progress.switch_adapter
    assert adapter is not None and adapter.pending_children
    child_id = adapter.pending_children[0].operation_id
    with sessions.begin() as session:
        child = session.get_one(Job, child_id)
        child.state = LifecycleState.FAILED.value
        child.status_reason = "Member failed during transfer"
        row = session.get_one(FleetProfileApplication, application.id)
        progress = _persisted_profile_progress(row)
        row.next_action_at = profiles._clock() + timedelta(seconds=60)
        row.progress = progress.model_dump(mode="json")
        assert row.state == LifecycleState.RUNNING.value
    api, tokens = _client(sessions, profiles=profiles)
    key = str(uuid4())
    path = f"/api/profile/applications/{application.id}"
    response = api.post(
        f"{path}/cancel",
        json={"profile_number": profile.number, "request_key": key},
        headers=_headers(tokens, "administrator"),
    )
    assert response.status_code == 202, response.text
    accepted = FleetProfileApplicationView.model_validate_json(response.content)
    assert accepted.progress.cancellation is not None
    assert accepted.progress.cancellation.request_key == key
    receipt = api.get(
        f"{path}/cancellations/{key}", headers=_headers(tokens, "administrator")
    )
    assert receipt.status_code == 200, receipt.text
    assert profiles.tick()
    ended = profiles.application(application.id)
    assert ended.state == LifecycleState.CANCELLED
    assert ended.cancellation is not None
    assert ended.cancellation.state == LifecycleState.CANCELLED
    assert not ended.cancellation.pending_effects
    assert run_switch.get(child_id).state == LifecycleState.FAILED.value


def test_cancel_adopted_executor_refuses_without_acknowledging_acceptance(
    tmp_path, postgres_engine
) -> None:
    """Catches HTTP 202 with no receipt when a newer load owns continuing effects."""
    world = build_world(tmp_path, postgres_engine)
    try:
        profiles = world.controller.profiles
        choices = [
            FleetProfileAssignmentInput(
                recipe_selector=f"vonk-forge/{world.recipes[SOLO]}",
                spark_ids=[node],
                assignment_name=f"lane-{index}",
            )
            for index, node in enumerate(world.nodes)
        ]
        profile = profiles.create(
            FleetProfileInput(name="Original", assignments=choices[:1]), actor="admin"
        )
        original = profiles.apply(profile.id, request_key=str(uuid4()), actor="admin")
        assert profiles.tick()
        world.advance(1)
        successor_profile = profiles.create(
            FleetProfileInput(name="Continues original", assignments=choices),
            actor="admin",
        )
        successor = profiles.apply(
            successor_profile.id, request_key=str(uuid4()), actor="admin"
        )
        with world.sessions.begin() as session:
            row = session.get_one(FleetProfileApplication, original.id)
            assert profiles._adopted_application_scope(session, row)
            progress = _persisted_profile_progress(row)
            adapter = progress.switch_adapter
            assert adapter is not None and adapter.pending_children
            child = session.get_one(Job, adapter.pending_children[0].operation_id)
            child.state = LifecycleState.FAILED.value
            child.status_reason = "Member failed during transfer"
            row.next_action_at = world.clock() + timedelta(seconds=60)
            row.progress = progress.model_dump(mode="json")
        api, tokens = _client(world.sessions, profiles=profiles)
        key = str(uuid4())
        path = f"/api/profile/applications/{original.id}"
        response = api.post(
            f"{path}/cancel",
            json={"profile_number": profile.number, "request_key": key},
            headers=_headers(tokens, "administrator"),
        )
        assert response.status_code == 409, response.text
        assert (
            response.headers["x-vonk-error-code"] == ProfileReasonCode.REQUEST_INVALID
        )
        assert "newer selected" in response.json()["detail"]
        assert profiles.application(original.id).progress.cancellation is None
        with world.sessions() as session:
            assert (
                session.get_one(FleetProfileSelection, 1).application_id == successor.id
            )
        assert (
            api.get(
                f"{path}/cancellations/{key}", headers=_headers(tokens, "administrator")
            ).status_code
            == 404
        )
    finally:
        world.close()


@pytest.mark.parametrize(
    "state",
    [LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.SUPERSEDED],
)
def test_cancel_terminal_application_refuses_instead_of_losing_request(
    tmp_path, postgres_engine, state: LifecycleState
) -> None:
    """Catches accepting a new cancel key for a load that already ended."""
    sessions, _run_switch, profiles, profile, application, _adapter, _nodes = (
        _application(tmp_path, engine=postgres_engine)
    )
    with sessions.begin() as session:
        session.get_one(FleetProfileApplication, application.id).state = state.value
    api, tokens = _client(sessions, profiles=profiles)
    response = api.post(
        f"/api/profile/applications/{application.id}/cancel",
        json={"profile_number": profile.number, "request_key": str(uuid4())},
        headers=_headers(tokens, "administrator"),
    )
    assert response.status_code == 409, response.text
    assert response.headers["x-vonk-error-code"] == ProfileReasonCode.REQUEST_INVALID
    assert profiles.application(application.id).progress.cancellation is None
