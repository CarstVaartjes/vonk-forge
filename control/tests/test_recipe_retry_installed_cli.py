"""A dropped retry receipt reconnects to the original frozen recipe intent."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol.reason_codes import RecipeImageCode
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.jobs import JobService
from vonk_control.models import Base, CatalogDocument, Job
from vonk_control.recipe_availability_intent import RecipeRetryIntent
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
)
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import RecipeDefinition

from .test_profile_load_installed_cli import _https_api_peer, _process_environment
from .test_recipe_image_availability import _add_revision, _recipe, _runtime

pytest_plugins = ["tests.test_profile_load_installed_cli"]

PARENT_KEY = "00000000-0000-4000-8000-000000000501"
RETRY_KEY = "00000000-0000-4000-8000-000000000502"


@pytest.mark.lane
def test_installed_retry_preserves_frozen_intent_after_catalog_change_and_lost_receipt(
    installed_vonkctl: Path, tmp_path: Path
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'retry.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    recipe = _recipe("recipe-source-build.json")
    revision_id = "retry-frozen-recipe-revision"
    document_id = "retry-frozen-recipe-document"
    with sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=document_id,
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
        original_revision = _add_revision(
            session, revision_id, recipe, document_id=document_id
        )
        original_content = original_revision.content_digest

    def resolve(
        recipe_revision_id: str, *, force: bool = False
    ) -> tuple[RecipeDefinition, Mapping[str, object]]:
        assert recipe_revision_id == revision_id
        assert force is False
        return recipe, _runtime()

    service = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "recipe-cache"),
        authority=resolve,
        clock=lambda: now,
    )
    original = service.start(revision_id, actor="operator", request_id=PARENT_KEY)
    claims = service.claim_pending(limit=1)
    assert len(claims) == 1
    service._fail(
        claims[0],
        RecipeImageAvailabilityError(
            RecipeImageCode.NOT_RETRYABLE,
            "test preparation source became unavailable",
            retryable=False,
        ),
    )
    failed = service.get(original.id)
    assert failed.state == "failed"
    assert failed.failure_evidence is not None
    with sessions.begin() as session:
        changed = recipe.model_copy(
            update={
                "metadata": recipe.metadata.model_copy(
                    update={"title": "Changed catalog choice"}
                )
            }
        )
        newer = _add_revision(
            session,
            "retry-newer-recipe-revision",
            changed,
            built=False,
            document_id=document_id,
        )
        newer.revision_number = 2
        assert newer.content_digest != original_content
    codec = TokenCodec(b"installed-recipe-retry-authority-key")
    operator = {
        "Authorization": "Bearer "
        + codec.issue(Actor("operator", "operator"), ttl_seconds=100, now=0)
    }
    app = create_app(
        jobs=JobService(sessions, clock=lambda: now),
        tokens=codec,
        recipe_image_availability=service,
        now=lambda: 1,
    )
    retry_path = f"/api/recipe/operations/{original.id}/retry"
    with (
        TestClient(app) as api,
        _https_api_peer(tmp_path, api, operator) as (url, certificate, peer),
    ):
        environment = _process_environment(tmp_path, url, certificate, operator)
        arguments = [
            str(installed_vonkctl),
            "--no-input",
            "--json",
            "recipe",
            "retry",
            original.id,
            "--yes",
            "--detach",
            "--request-key",
            RETRY_KEY,
        ]
        peer.drop_responses.add(("POST", retry_path))
        accepted = subprocess.run(
            arguments,
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert accepted.returncode == 0, accepted.stderr
        response = json.loads(accepted.stdout)
        assert response["id"] != original.id
        assert response["request_id"] == RETRY_KEY
        assert response["request"] == {"kind": "retry", "operation_id": original.id}
        assert response["recipe_revision_id"] == revision_id
        assert response["recipe_content_sha256"] == original_content
        assert peer.dropped_responses == [("POST", retry_path)]
        assert [(method, path) for method, path, _ in peer.calls] == [
            ("GET", f"/api/recipe/operations/{original.id}"),
            ("POST", retry_path),
            ("GET", f"/api/recipe/requests/{RETRY_KEY}"),
        ]
        assert peer.calls[1][2] == {"request_key": RETRY_KEY}
        repeated = subprocess.run(
            arguments,
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert repeated.returncode == 0, repeated.stderr
        assert json.loads(repeated.stdout)["id"] == response["id"]
        with sessions() as session:
            rows = list(session.scalars(select(Job)))
            assert len(rows) == 2
            parent = session.get(Job, original.id)
            retry = session.get(Job, response["id"])
            assert parent is not None and retry is not None
            assert retry.authority_revision == parent.authority_revision == revision_id
            assert retry.targets == parent.targets == [revision_id]
            assert parent.state == "failed" and parent.request_id == PARENT_KEY
        typed = service.get(response["id"])
        assert cast(RecipeRetryIntent, typed.request).operation_id == original.id
        # A narrower new credential must refuse even when this key already has
        # a readable accepted receipt. It cannot turn the refusal into adoption.
        token = Path(environment["VONK_CONTROL_TOKEN_FILE"])
        token.write_text(
            codec.issue(Actor("operator", "viewer"), ttl_seconds=100, now=0)
        )
        token.chmod(0o600)
        peer.calls.clear()
        denied = subprocess.run(
            arguments,
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert denied.returncode != 0
        assert [(method, path) for method, path, _ in peer.calls] == [
            ("GET", f"/api/recipe/operations/{original.id}"),
            ("POST", retry_path),
        ]
        with sessions() as session:
            assert len(list(session.scalars(select(Job)))) == 2
    engine.dispose()
