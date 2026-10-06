"""Corrupt persisted intent stays visible through real HTTP and client readers."""

from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.auth import Actor
from vonk_control.fleet_profile_contract import (
    FleetProfileDefinitionView,
    FleetProfileList,
    UnavailableFleetProfileView,
)
from vonk_control.models import Base, FleetProfile, Job
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.recipe_image_availability_api import install_recipe_operator_routes
from vonk_control.recipe_image_removal_contract import RecipeRemovalUnavailableView
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from cluster_profiles.cli_render import render_payload

from .test_fleet_profile_api import _client, _headers

NOW = datetime(2026, 10, 6, tzinfo=UTC)


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
    # Once the owning record is repaired, the same GET converges without an
    # Idle/reapply or another profile revision.
    with sessions.begin() as session:
        row = session.get(FleetProfile, row_id)
        assert row is not None
        setattr(row, field, [] if field == "assignments" else {})
    restored = FleetProfileDefinitionView.model_validate_json(
        client.get("/api/profile/1/definition", headers=headers).content
    )
    assert restored.id == row_id and restored.revision == 1
    assert restored.definition is not None and restored.projection_issue is None


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
    with pytest.raises(KeyError):
        service.get_operator_request(request_id, actor="someone-else")
    with sessions() as session:
        row = session.get(Job, operation_id)
        assert (
            row is not None
            and row.state == "succeeded"
            and row.payload == {"broken": "owner"}
        )
