"""Corrupt persisted intent stays visible through real HTTP and client readers."""

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import ProfileReasonCode
from vonk_agent_protocol.agent_words import ProfileInstallationPolicy
from vonk_control.auth import Actor
from vonk_control.fleet_profile_contract import (
    FleetProfileDefinitionView,
    FleetProfileList,
    FleetProfilePreview,
    FleetProfileView,
    UnavailableFleetProfileView,
)
from vonk_control.models import Base, FleetProfile, Job
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.recipe_image_availability_api import install_recipe_operator_routes
from vonk_control.recipe_image_removal_contract import RecipeRemovalUnavailableView
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from cluster_profiles.cli_render import render_payload

from .test_fleet_profile_api import _client, _headers
from .test_operator_acceptance_postgres import DropFirstResponse

NOW = datetime(2026, 10, 6, tzinfo=UTC)
_PROFILE_MUTATIONS = {
    "autosaveProfile": "save",
    "previewProfile": "preview",
    "loadProfile": "load",
    "cancelProfileApplication": "cancel",
}


def test_profile_mutation_damage_matrix_covers_installed_routes():
    """Catches adding a profile mutation without a damaged-document behavior case."""
    client, _codec = _client()
    assert isinstance(client.app, FastAPI)
    mutations = {
        route.operation_id
        for route in client.app.routes
        if isinstance(route, APIRoute)
        and route.path.startswith("/api/profile/")
        and set(route.methods or ()) & {"PUT", "POST", "PATCH", "DELETE"}
    }
    assert mutations == set(_PROFILE_MUTATIONS)


def _sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    "field,value", [("assignments", [{"invalid": "choice"}]), ("labels", [])]
)
@pytest.mark.parametrize("action", [*_PROFILE_MUTATIONS.values(), "replay"])
def test_profile_mutation_routes_isolate_damaged_saved_definition(
    postgres_engine, tmp_path, field, value, action
):
    """Catches old draft parsers breaking replacement, review or accepted work."""
    from .test_fleet_profile_cancel import _application

    sessions, _switch, service, profile, application, _adapter, _nodes = _application(
        tmp_path, engine=postgres_engine
    )
    client, codec = _client(sessions, profiles=service)
    headers = {
        "Authorization": f"Bearer {codec.issue(Actor('admin', 'administrator'), ttl_seconds=100, now=0)}"
    }
    replacement = service.definition_number(profile.number).definition
    assert replacement is not None
    with sessions.begin() as session:
        setattr(session.get_one(FleetProfile, profile.id), field, value)
    base = f"/api/profile/{profile.number}"
    path = base
    method = "PUT" if action == "save" else "POST"
    body = None
    expected = 409
    if action == "save":
        body = {
            **replacement.model_dump(mode="json"),
            "expected_revision": profile.revision,
        }
        expected = 200
    elif action == "preview":
        path += "/preview"
    elif action in {"load", "replay"}:
        path += "/load"
        body = {
            "request_key": application.request_key
            if action == "replay"
            else "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        }
        if action == "replay":
            expected = 202
    else:
        path = f"/api/profile/applications/{application.id}/cancel"
        body = {
            "profile_number": profile.number,
            "request_key": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        }
        expected = 202
    assert client.request(method, path, json=body).status_code == 401
    assert (
        client.request(method, path, headers=_headers(codec), json=body).status_code
        == 403
    )
    response = client.request(method, path, headers=headers, json=body)
    assert response.status_code == expected, response.text
    if action in {"preview", "load"}:
        issue = UnavailableFleetProfileView.model_validate_json(
            client.get(base, headers=headers).content
        ).projection_issue
        assert response.headers["x-vonk-error-code"] == issue.code
        assert issue.detail in response.json()["detail"]
        assert issue.next_action in response.json()["detail"]
    elif action == "replay":
        assert response.json()["id"] == application.id
    elif action == "cancel":
        assert body is not None
        assert response.json()["cancellation"]["request_key"] == body["request_key"]
    with sessions() as session:
        row = session.get_one(FleetProfile, profile.id)
        if action == "save":
            assert row.revision == profile.revision + 1
            assert service.definition_number(profile.number).definition == replacement
        else:
            assert row.revision == profile.revision and getattr(row, field) == value


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    "field,value", [("assignments", [{"invalid": "choice"}]), ("labels", [])]
)
def test_corrupt_saved_definition_does_not_become_empty_or_hide_other_profiles(
    field, value, capsys
):
    sessions = _sessions()
    client, codec = _client(sessions)
    headers = _headers(codec, "administrator")
    for number in (1, 2):
        response = client.put(
            f"/api/profile/{number}",
            headers=headers,
            json={"name": f"Profile {number}", "expected_revision": 0},
        )
        assert response.status_code == 200, response.text
    with sessions.begin() as session:
        row = session.scalar(select(FleetProfile).where(FleetProfile.number == 1))
        assert row is not None
        row_id = row.id
        setattr(row, field, value)

    definition = client.get("/api/profile/1/definition", headers=headers)
    assert definition.status_code == 200, definition.text
    parsed = FleetProfileDefinitionView.model_validate_json(definition.content)
    assert parsed.id == row_id and parsed.revision == 1
    assert parsed.definition is None and parsed.projection_issue is not None
    detail = client.get("/api/profile/1", headers=headers)
    assert detail.status_code == 200, detail.text
    unavailable = UnavailableFleetProfileView.model_validate_json(detail.content)
    listing = client.get("/api/profile", headers=headers)
    assert listing.status_code == 200, listing.text
    profiles = FleetProfileList.model_validate_json(listing.content).profiles
    assert len(profiles) == 2
    assert profiles[1].definition is not None
    render_payload(unavailable.model_dump(mode="json"), "profile")
    text = capsys.readouterr().out
    assert "unknown" in text and "cannot be read" in text
    assert "No assignments saved" not in text
    # Observation does not repair, retire or drop the persisted malformed value.
    with sessions() as session:
        row = session.get(FleetProfile, row_id)
        assert row is not None and getattr(row, field) == value
    assert client.get("/api/profile/1/definition").status_code == 401
    # A stale or unauthorized replacement cannot use damage to bypass admission.
    replacement = {
        "name": "Repaired profile",
        "description": "Explicit complete replacement",
        "installation_policy": ProfileInstallationPolicy.KEEP_CACHED.value,
        "labels": {"use": "repair"},
        "favorite": True,
        "assignments": [],
        "expected_revision": 1,
    }
    assert client.put("/api/profile/1", json=replacement).status_code == 401
    assert (
        client.put(
            "/api/profile/1", headers=_headers(codec), json=replacement
        ).status_code
        == 403
    )
    stale = client.put(
        "/api/profile/1",
        headers=headers,
        json={**replacement, "expected_revision": 0},
    )
    assert stale.status_code == 409, stale.text
    assert stale.headers["x-vonk-error-code"] == ProfileReasonCode.REQUEST_INVALID
    assert "revision conflict" in stale.json()["detail"]
    invalid = client.put(
        "/api/profile/1", headers=headers, json={**replacement, "labels": []}
    )
    assert invalid.status_code == 422, invalid.text
    with sessions() as session:
        row = session.get_one(FleetProfile, row_id)
        assert row.revision == 1 and getattr(row, field) == value
    # A committed repair whose HTTP response is lost reconnects to that same
    # revision on replay; it must neither edit again nor reject the retry.
    with pytest.raises(httpx.ReadError):
        TestClient(DropFirstResponse(client.app, "/api/profile/1")).put(
            "/api/profile/1", headers=headers, json=replacement
        )
    repaired = client.put("/api/profile/1", headers=headers, json=replacement)
    assert repaired.status_code == 200, repaired.text
    saved = FleetProfileView.model_validate_json(repaired.content)
    assert saved.id == row_id and saved.revision == 2
    assert saved.definition.model_dump(mode="json") == {
        key: value for key, value in replacement.items() if key != "expected_revision"
    }
    restored = FleetProfileDefinitionView.model_validate_json(
        client.get("/api/profile/1/definition", headers=headers).content
    )
    assert restored.id == row_id and restored.revision == 2
    assert restored.definition == saved.definition and restored.projection_issue is None
    assert (
        FleetProfileView.model_validate_json(
            client.get("/api/profile/1", headers=headers).content
        ).definition
        == saved.definition
    )
    listed = FleetProfileList.model_validate_json(
        client.get("/api/profile", headers=headers).content
    ).profiles
    assert listed[0].definition == saved.definition
    assert listed[1] == profiles[1]
    reviewed = client.post("/api/profile/1/preview", headers=headers)
    assert reviewed.status_code == 200, reviewed.text
    assert (
        FleetProfilePreview.model_validate_json(reviewed.content).profile_definition
        == saved.definition
    )
    # Equal content keeps the existing no-op replay at the committed revision.
    unchanged = client.put(
        "/api/profile/1",
        headers=headers,
        json={**replacement, "expected_revision": 2},
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["revision"] == 2


@pytest.mark.usefixtures("damaged_json_rows")
def test_corrupt_recipe_removal_owner_is_unknown_in_operation_and_issuer_request_reads(
    tmp_path, capsys
):
    sessions = _sessions()
    operation_id = "00000000-0000-4000-8000-000000000001"
    request_id = "00000000-0000-4000-8000-000000000002"
    with sessions.begin() as session:
        session.add(
            Job(
                id=operation_id,
                request_id=request_id,
                kind="recipe.cache.remove.v2",
                state="succeeded",
                actor="operator",
                authority_revision="revision",
                targets=[],
                payload_digest="a" * 64,
                payload={"broken": "owner"},
                result={"reclaimed_bytes": 999},
                created_at=NOW,
                updated_at=NOW,
            )
        )
    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "cache"),
        authority=lambda *_args, **_kwargs: pytest.fail(
            "read probed mutable authority"
        ),
        clock=lambda: NOW,
    )
    app = FastAPI()
    install_recipe_operator_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=service,
    )
    client = TestClient(app)
    for path in (
        f"/api/recipe/operations/{operation_id}",
        f"/api/recipe/requests/{request_id}",
    ):
        response = client.get(path)
        assert response.status_code == 200, response.text
        observed = RecipeRemovalUnavailableView.model_validate_json(response.content)
        assert (
            observed.operation_id == operation_id and observed.request_key == request_id
        )
        assert observed.state == "unknown" and observed.progress is None
        assert "reclaimed_bytes" not in observed.model_dump(mode="json")
        render_payload(observed.model_dump(mode="json"), "recipe", action="progress")
        assert "cannot be read" in capsys.readouterr().out
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        service.get_operator_request(request_id, actor="someone-else")
    with sessions() as session:
        row = session.get(Job, operation_id)
        assert (
            row is not None
            and row.state == "succeeded"
            and row.payload == {"broken": "owner"}
        )
