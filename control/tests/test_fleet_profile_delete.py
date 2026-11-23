"""Deleting a saved profile removes its definition and history, nothing running."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from vonk_agent_protocol.gpu_clock import GpuClockRange, default_gpu_clock
from vonk_control.fleet_profiles.contracts import FleetProfileInvalid
from vonk_control.fleet_profiles.service import FleetProfileService
from vonk_control.gpu.clock import selected_gpu_clocks
from vonk_control.models.fleet import (
    FleetProfile,
    FleetProfileApplication,
    FleetProfileRecipe,
    FleetProfileSelection,
)

from .fleet_profiles_support import (
    NOW,
    _database,
    _follow_profile_retry,
    _input,
    _node_id,
    _seed,
    _SwitchAdapter,
    _uuid,
)
from .test_fleet_profile_api import _client, _headers


def _service(
    now: list[datetime] | None = None,
) -> tuple[FleetProfileService, sessionmaker[Session], _SwitchAdapter, str]:
    clock = now or [NOW]
    sessions = _database()
    _recipe_id, revision_id = _seed(sessions)
    adapter = _SwitchAdapter()
    service = FleetProfileService(
        sessions, clock=lambda: clock[0], switch_adapter=adapter
    )
    return service, sessions, adapter, revision_id


def _run_to_success(
    service: FleetProfileService, application_id: str, now: list[datetime]
) -> None:
    """Drive the worker until the load ends, as production does."""
    for _ in range(12):
        _follow_profile_retry(service, application_id, now)
        service.tick()
        if service.application(application_id).state == "succeeded":
            return
    raise AssertionError("the load did not finish")


def _selection(sessions: sessionmaker[Session]) -> FleetProfileSelection:
    with sessions() as session:
        row = session.get(FleetProfileSelection, 1)
        assert row is not None
        return row


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
    now = [NOW]
    service, sessions, adapter, revision_id = _service(now)
    profile = service.create(_input(revision_id), actor="admin")
    application = service.load(profile.number, actor="admin", request_key=_uuid(901))

    first = service.delete_number(profile.number, actor="admin")
    # The issued stop is driven by the worker: nothing is removed yet.
    assert first.outcome == "cancelling"
    assert [item[0] for item in adapter.cancellation_requests] == [application.id]
    assert _count(sessions, FleetProfile) == 1

    for _ in range(12):
        service.tick()
        outcome = service.delete_number(profile.number, actor="admin").outcome
        if outcome == "deleted":
            break
    assert outcome == "deleted"
    assert _count(sessions, FleetProfileApplication) == 0
    assert _selection(sessions).application_id is None


def test_deleting_the_loaded_profile_touches_no_running_workload() -> None:
    now = [NOW]
    service, sessions, adapter, revision_id = _service(now)
    profile = service.create(_input(revision_id), actor="admin")
    application = service.load(profile.number, actor="admin", request_key=_uuid(902))
    _run_to_success(service, application.id, now)
    before = _selection(sessions)
    assert before.profile_id == profile.id and before.application_id == application.id
    stops = list(adapter.cancellations)

    assert service.delete_number(profile.number, actor="admin").outcome == "deleted"
    # Nothing was cancelled or stopped; the pointer cleared, the generation advanced.
    assert adapter.cancellations == stops and not adapter.cancellation_requests
    after = _selection(sessions)
    assert after.profile_id is None and after.application_id is None
    assert after.generation > before.generation
    assert _count(sessions, FleetProfileApplication) == 0


def test_selection_generation_never_goes_backwards_across_a_delete() -> None:
    now = [NOW]
    service, sessions, _adapter, revision_id = _service(now)
    first = service.create(_input(revision_id), actor="admin")
    second = service.create(_input(revision_id, name="Other"), actor="admin")
    loaded = service.load(first.number, actor="admin", request_key=_uuid(903))
    _run_to_success(service, loaded.id, now)
    generation = _selection(sessions).generation

    assert service.delete_number(first.number, actor="admin").outcome == "deleted"
    reloaded = service.load(second.number, actor="admin", request_key=_uuid(904))
    _run_to_success(service, reloaded.id, now)
    selection = _selection(sessions)
    assert selection.application_id == reloaded.id
    assert selection.generation > generation


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


def test_delete_returns_sparks_to_the_default_clock_under_a_higher_generation() -> None:
    now = [NOW]
    service, sessions, _adapter, revision_id = _service(now)
    slow = _input(revision_id)
    slow.assignments[0].gpu_clock = GpuClockRange(min_mhz=300, max_mhz=1800)
    profile = service.create(slow, actor="admin")
    loaded = service.load(profile.number, actor="admin", request_key=_uuid(905))
    _run_to_success(service, loaded.id, now)
    with sessions() as session:
        before = selected_gpu_clocks(session, [_node_id(1)])[_node_id(1)]

    service.delete_number(profile.number, actor="admin")
    with sessions() as session:
        after = selected_gpu_clocks(session, [_node_id(1)])[_node_id(1)]
    assert before.range == GpuClockRange(min_mhz=300, max_mhz=1800)
    assert after.generation > before.generation
    assert after.range == default_gpu_clock()


def test_delete_leaves_a_selection_that_names_another_profile() -> None:
    now = [NOW]
    service, sessions, _adapter, revision_id = _service(now)
    first = service.create(_input(revision_id), actor="admin")
    second = service.create(_input(revision_id, name="Other"), actor="admin")
    loaded = service.load(first.number, actor="admin", request_key=_uuid(906))
    _run_to_success(service, loaded.id, now)
    with sessions.begin() as session:  # a newer load switched the selection
        session.get_one(FleetProfileSelection, 1).profile_id = second.id
    before = _selection(sessions)

    service.delete_number(first.number, actor="admin")
    after = _selection(sessions)
    assert after.profile_id == second.id and after.generation == before.generation
