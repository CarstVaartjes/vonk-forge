"""Installed recipe cancellation reconciles a dropped receipt through TLS."""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import TokenCodec
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.jobs import JobService
from vonk_control.models import Base, CatalogDocument, User
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import RecipeDefinition

from .test_fleet_profile_api import _headers
from .test_profile_load_installed_cli import _https_api_peer, _process_environment
from .test_recipe_image_availability import _add_revision, _recipe, _runtime

pytest_plugins = ["tests.test_profile_load_installed_cli"]

PARENT_REQUEST_KEY = "00000000-0000-4000-8000-000000000401"
CANCEL_REQUEST_KEY = "00000000-0000-4000-8000-000000000402"
CANCEL_REASON = "operator stopped this recipe preparation"


@pytest.mark.lane
def test_installed_recipe_cancel_recovers_dropped_acceptance_and_settles(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
) -> None:
    """The CLI returns the same Controller-owned cancellation after response loss."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-image.json")
    revision_id = uuid.uuid4().hex[:24]
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            CatalogDocument(
                id="document-" + revision_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                title=recipe.metadata.title,
                created_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        _add_revision(session, revision_id, recipe)
        session.add(User(subject="operator", role="operator"))

    def resolve_recipe(
        recipe_revision_id: str, *, force: bool = False
    ) -> tuple[RecipeDefinition, Mapping[str, object]]:
        assert recipe_revision_id == revision_id
        assert force is False
        return recipe, _runtime()

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "recipe-cache"),
        authority=resolve_recipe,
        clock=lambda: now,
    )
    # Hold the accepted operation before dispatch. This tests cancellation
    # authority and receipt recovery; no Spark-side effect is implied.
    operation = service.start(
        revision_id,
        actor="operator",
        request_id=PARENT_REQUEST_KEY,
    )
    assert operation.state == "queued"

    codec = TokenCodec(b"installed-recipe-cancel-authority-key")
    headers = _headers(codec, "operator")
    app = create_app(
        jobs=JobService(sessions, clock=lambda: now),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet_profiles=FleetProfileService(sessions, clock=lambda: now),
        recipe_image_availability=service,
        now=lambda: 1,
    )
    cancel_path = f"/api/recipe/operations/{operation.id}/cancel"
    operation_path = f"/api/recipe/operations/{operation.id}"

    with (
        TestClient(app) as api,
        _https_api_peer(tmp_path, api, headers) as (
            url,
            certificate,
            peer,
        ),
    ):
        peer.drop_responses.add(("POST", cancel_path))
        environment = _process_environment(tmp_path, url, certificate, headers)
        cancelled = subprocess.run(
            [
                str(installed_vonkctl),
                "--no-input",
                "--json",
                "recipe",
                "cancel",
                operation.id,
                "--yes",
                "--request-key",
                CANCEL_REQUEST_KEY,
                "--reason",
                CANCEL_REASON,
                "--detach",
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert cancelled.returncode == 0, cancelled.stderr
        assert cancelled.stdout.endswith("\n")
        assert cancelled.stdout.count("\n") == 1
        assert not cancelled.stderr
        acceptance = json.loads(cancelled.stdout)
        assert acceptance["id"] == operation.id
        assert acceptance["state"] == "cancelling"
        cancellation = acceptance.get("cancellation")
        assert isinstance(cancellation, dict)
        assert cancellation["cancel_request_id"] == CANCEL_REQUEST_KEY
        assert cancellation["reason"] == CANCEL_REASON
        assert cancellation["cancel_actor"] == "operator"
        assert peer.dropped_responses == [("POST", cancel_path)]
        assert [(method, path) for method, path, _ in peer.calls] == [
            ("POST", cancel_path),
            ("GET", operation_path),
        ]
        assert peer.calls[0][2] == {
            "schema_version": 2,
            "request_key": CANCEL_REQUEST_KEY,
            "reason": CANCEL_REASON,
        }
        observed = service.get_operator_operation(operation.id)
        assert not isinstance(observed, dict)
        assert observed.state == "cancelling"
        assert observed.cancellation is not None
        assert observed.cancellation.cancel_request_id == CANCEL_REQUEST_KEY

        assert service.reconcile_cancellations() == 1
        assert service.get(operation.id).state == "cancelled"
        settled = subprocess.run(
            [
                str(installed_vonkctl),
                "--no-input",
                "--json",
                "recipe",
                "progress",
                operation.id,
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert settled.returncode == 0, settled.stderr
        assert settled.stdout.endswith("\n")
        assert settled.stdout.count("\n") == 1
        assert not settled.stderr
        terminal = json.loads(settled.stdout)
        assert terminal["id"] == operation.id
        assert terminal["state"] == "cancelled"
        terminal_cancellation = terminal.get("cancellation")
        assert isinstance(terminal_cancellation, dict)
        assert terminal_cancellation["cancel_request_id"] == CANCEL_REQUEST_KEY
        assert [(method, path) for method, path, _ in peer.calls] == [
            ("POST", cancel_path),
            ("GET", operation_path),
            ("GET", operation_path),
        ]
        token_value = headers["Authorization"].removeprefix("Bearer ")
        assert token_value not in cancelled.stdout + cancelled.stderr
        assert token_value not in settled.stdout + settled.stderr
