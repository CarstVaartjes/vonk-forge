"""Deleting a saved profile removes its definition and history, nothing running."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.fleet_profiles.contracts import FleetProfileInvalid
from vonk_control.models import (
    FleetProfile,
    FleetProfileApplication,
    FleetProfileRecipe,
    FleetProfileSelection,
)

from .test_fleet_profile_api import _client, _headers
from .test_fleet_profiles import (
    NOW,
    _database,
    _input,
    _seed,
    _SwitchAdapter,
    _uuid,
)


def _service() -> tuple[
    FleetProfileService, sessionmaker[Session], _SwitchAdapter, str
]:
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    adapter = _SwitchAdapter()
    service = FleetProfileService(sessions, clock=lambda: NOW, switch_adapter=adapter)
    return service, sessions, adapter, revision_id


def _count(sessions: sessionmaker[Session], model: type) -> int:
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


def test_delete_removes_the_profile_and_is_idempotent() -> None:
    service, sessions, _adapter, revision_id = _service()
    profile = service.create(_input(revision_id), actor="admin")
    assert _count(sessions, FleetProfileRecipe) == 1

    assert service.delete_number(profile.number, actor="admin").outcome == "deleted"
    assert [item.number for item in service.list().profiles] == []
    assert _count(sessions, FleetProfileRecipe) == 0
    # The same request again is the same success, not an error.
    again = service.delete_number(profile.number, actor="admin")
    assert again.outcome == "already-gone" and again.number == profile.number


def test_number_is_free_for_a_later_profile() -> None:
    service, _sessions, _adapter, revision_id = _service()
    first = service.create(_input(revision_id), actor="admin")
    service.delete_number(first.number, actor="admin")
    reused = service.create(_input(revision_id, name="Again"), actor="admin")
    assert reused.number == first.number and reused.id != first.id


def test_expected_revision_refuses_a_stale_delete() -> None:
    service, _sessions, _adapter, revision_id = _service()
    profile = service.create(_input(revision_id), actor="admin")
    with pytest.raises(FleetProfileInvalid, match="revision conflict"):
        service.delete_number(
            profile.number, actor="admin", expected_revision=profile.revision + 1
        )
    assert (
        service.delete_number(
            profile.number, actor="admin", expected_revision=profile.revision
        ).outcome
        == "deleted"
    )


def test_delete_while_loading_cancels_through_the_cancel_path() -> None:
    service, sessions, adapter, revision_id = _service()
    profile = service.create(_input(revision_id), actor="admin")
    application = service.load(profile.number, actor="admin", request_key=_uuid(901))

    first = service.delete_number(profile.number, actor="admin")
    # The issued stop is driven by the worker: nothing is removed yet.
    assert first.outcome == "cancelling"
    assert [item[0] for item in adapter.cancellation_requests] == [application.id]
    assert _count(sessions, FleetProfile) == 1

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "cancelled"
    assert service.delete_number(profile.number, actor="admin").outcome == "deleted"
    assert _count(sessions, FleetProfileApplication) == 0
    assert _count(sessions, FleetProfileSelection) == 0


def test_deleting_the_loaded_profile_touches_no_running_workload() -> None:
    service, sessions, adapter, revision_id = _service()
    profile = service.create(_input(revision_id), actor="admin")
    application = service.load(profile.number, actor="admin", request_key=_uuid(902))
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, application.id)
        assert row is not None
        row.state = "succeeded"
    stops = list(adapter.cancellations)

    assert service.delete_number(profile.number, actor="admin").outcome == "deleted"
    # Nothing was cancelled or stopped, and the loaded pointer is gone.
    assert adapter.cancellations == stops and not adapter.cancellation_requests
    assert _count(sessions, FleetProfileSelection) == 0
    assert _count(sessions, FleetProfileApplication) == 0


def test_delete_route_is_administrator_only_and_idempotent() -> None:
    client, codec = _client()
    admin = _headers(codec, "administrator")
    saved = client.put(
        "/api/profile/1", headers=admin, json={"name": "Standing", "assignments": []}
    )
    assert saved.status_code == 200
    assert (
        client.post(
            "/api/profile/1/delete", headers=_headers(codec, "operator"), json={}
        ).status_code
        == 403
    )
    for outcome in ("deleted", "already-gone"):
        response = client.post("/api/profile/1/delete", headers=admin, json={})
        assert response.status_code == 200
        assert response.json() == {"number": 1, "outcome": outcome}
    assert client.get("/api/profile", headers=admin).json()["profiles"] == []
