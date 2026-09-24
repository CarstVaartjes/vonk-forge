"""Installed recipe removal follows its durable request through real storage."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.jobs import JobService
from vonk_control.models import (
    Base,
    CatalogDocumentHead,
    Job,
    RuntimeImageAuthorization,
)
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.recipe_image_removal_contract import (
    RecipeCacheRemovalOwner,
    RecipeCacheRemovalResult,
)
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import content_sha256

from .test_profile_load_installed_cli import _https_api_peer, _process_environment
from .test_recipe_image_availability import (
    ARCHIVE,
    ARCHIVE_SHA,
    _add_head,
    _add_revision,
    _recipe,
    _reference_receipt,
    _runtime,
)

pytest_plugins = ("tests.test_profile_load_installed_cli",)

_REMOVE_KEY = "00000000-0000-4000-8000-000000000263"
_TOKEN_KEY = b"installed-recipe-removal-controller-key"


@pytest.mark.lane
def test_installed_recipe_remove_recovers_lost_acceptance_and_reclaims_bytes(
    installed_vonkctl: Path,
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    """Reconnect by key, report real queued work, then settle the image owner."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    now = datetime.now(UTC)
    codec = TokenCodec(_TOKEN_KEY)
    recipe = _recipe("recipe-image.json")
    revision_id = "rev-cli-rm-263"
    recipe_digest = content_sha256(recipe)
    receipt = _reference_receipt().model_copy(
        update={
            "distribution_publisher": recipe.identity.publisher,
            "distribution_slug": recipe.identity.slug,
            "distribution_content_sha256": recipe_digest,
        }
    )

    with sessions.begin() as session:
        revision = _add_revision(session, revision_id, recipe)
        _add_head(session, revision)
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=revision.id,
                source="published",
                original_content_digest=recipe_digest,
                effective_execution_key=revision.execution_key,
                registry_manifest_digest=receipt.registry_manifest_digest,
                platform_manifest_digest=receipt.platform_manifest_digest,
                local_image_config_id=receipt.local_image_config_id,
                oci_archive_sha256=receipt.oci_archive_sha256,
                image_bytes=receipt.image_bytes,
                build_id=None,
                authorized_at=now,
            )
        )

    storage = FilesystemRuntimeImageStorage(tmp_path / "managed-artifacts")
    staged = storage.prepare_path()
    staged.write_bytes(ARCHIVE)
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == ARCHIVE_SHA
    published = storage.commit(staged, receipt=receipt)
    assert published.oci_archive_sha256 == ARCHIVE_SHA
    assert published.image_bytes == len(ARCHIVE)
    archive = storage.root / ARCHIVE_SHA
    receipt_file = storage.root / f"{ARCHIVE_SHA}.receipt.json"
    assert archive.read_bytes() == ARCHIVE
    assert receipt_file.is_file()

    service = RecipeImageAvailabilityService(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (recipe, _runtime()),
        clock=lambda: now,
    )
    actor = Actor("operator", "operator")
    token = codec.issue(actor, ttl_seconds=1_000, now=0)
    headers = {"Authorization": f"Bearer {token}"}
    api = create_app(
        jobs=JobService(sessions, clock=lambda: now, cursors=codec.cursor_codec()),
        tokens=codec,
        audits=MemoryAuditStore(),
        recipe_image_availability=service,
        now=lambda: 100,
    )
    selector = recipe.identity.slug
    remove_path = f"/api/recipe/{selector}/remove"
    request_path = f"/api/recipe/requests/{_REMOVE_KEY}"

    with (
        TestClient(api) as api_client,
        _https_api_peer(tmp_path, api_client, headers) as (
            url,
            certificate,
            peer,
        ),
    ):
        environment = _process_environment(tmp_path, url, certificate, headers)
        review_process = subprocess.run(
            [
                str(installed_vonkctl),
                "--json",
                "recipe",
                "remove",
                selector,
                "--keep-model",
                "--review",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=environment,
            cwd=tmp_path,
        )
        assert review_process.returncode == 0, (
            review_process.stdout + review_process.stderr
        )
        review = json.loads(review_process.stdout)
        assert review["with_model"] is False
        assert review["blockers"] == []
        assert all(method == "GET" for method, _, _ in peer.calls)
        arguments = [
            str(installed_vonkctl),
            "--json",
            "recipe",
            "remove",
            selector,
            "--keep-model",
            "--yes",
            "--review-digest",
            review["review_digest"],
            "--request-key",
            _REMOVE_KEY,
            "--detach",
        ]
        peer.held_response = ("POST", remove_path)
        peer.discard_held_response = True
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            cwd=tmp_path,
        )
        try:
            assert peer.response_accepted.wait(timeout=25), peer.calls
            accepted = service.get_operator_request(_REMOVE_KEY, actor=actor.subject)
            assert isinstance(accepted, dict)
            assert accepted["action"] == "remove"
            assert accepted["request_key"] == _REMOVE_KEY
            assert accepted["selector"] == selector
            assert accepted["with_model"] is False
            assert accepted["state"] == "queued"
            assert isinstance(accepted["operation_id"], str)
            operation_id = accepted["operation_id"]
            peer.release_held_response.set()
            stdout, stderr = process.communicate(timeout=45)
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
            raise
        finally:
            peer.release_held_response.set()

        assert stdout is not None and stderr is not None
        assert process.returncode == 0, stdout + stderr
        first_receipt = json.loads(stdout)
        assert first_receipt["request_key"] == _REMOVE_KEY
        assert first_receipt["operation_id"] == operation_id
        assert first_receipt["selector"] == selector
        assert first_receipt["with_model"] is False
        assert first_receipt["review_digest"] == review["review_digest"]
        assert first_receipt["state"] == "queued"
        assert first_receipt["reclaimed_bytes"] == 0
        assert peer.discard_held_response
        assert sum(method == "POST" for method, _, _ in peer.calls) == 1
        assert [
            (method, path)
            for method, path, _ in peer.calls
            if path == request_path or path == remove_path
        ] == [
            ("GET", request_path),
            ("POST", remove_path),
            ("GET", request_path),
        ]

        # A second installed process uses the same key and follows the accepted
        # owner; it cannot create another removal operation.
        calls_before_replay = len(peer.calls)
        replay = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=environment,
            cwd=tmp_path,
        )
        assert replay.returncode == 0, replay.stdout + replay.stderr
        replayed_receipt = json.loads(replay.stdout)
        assert replayed_receipt == first_receipt
        assert [
            (method, path) for method, path, _ in peer.calls[calls_before_replay:]
        ] == [("GET", request_path)]

        progress = subprocess.run(
            [
                str(installed_vonkctl),
                "--json",
                "recipe",
                "progress",
                "--request-key",
                _REMOVE_KEY,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=environment,
            cwd=tmp_path,
        )
        assert progress.returncode == 0, progress.stdout + progress.stderr
        pending = json.loads(progress.stdout)
        assert pending["operation_id"] == operation_id
        assert pending["request_key"] == _REMOVE_KEY
        assert pending["state"] == "queued"
        assert pending["reclaimed_bytes"] == 0
        assert pending["progress"]["phase"] == "reclaiming"
        assert pending["progress"]["completed_bytes"] == 0
        assert pending["progress"]["completed_items"] == 0
        assert pending["progress"]["total_items"] == 1
        assert archive.read_bytes() == ARCHIVE
        assert receipt_file.is_file()
        assert token not in stdout + stderr + replay.stdout + replay.stderr
        assert token not in progress.stdout + progress.stderr
        assert sum(method == "POST" for method, _, _ in peer.calls) == 1

    settled: dict[str, object] | None = None
    for _ in range(4):
        service.advance_removals(limit=1)
        operation_view = service.get_operator_request(_REMOVE_KEY, actor=actor.subject)
        assert isinstance(operation_view, dict)
        settled = operation_view
        if settled["state"] == "succeeded":
            break

    assert settled is not None
    assert settled["state"] == "succeeded"
    assert settled["operation_id"] == operation_id
    assert settled["reclaimed_bytes"] == len(ARCHIVE)
    settled_progress = settled["progress"]
    assert isinstance(settled_progress, dict)
    assert settled_progress["phase"] == "completed"
    assert settled_progress["completed_bytes"] == len(ARCHIVE)
    assert settled_progress["completed_items"] == 1
    assert settled_progress["total_items"] == 1
    assert not archive.exists()
    assert not receipt_file.exists()

    with sessions() as session:
        operation = session.scalar(select(Job).where(Job.request_id == _REMOVE_KEY))
        head = session.scalar(
            select(CatalogDocumentHead).where(
                CatalogDocumentHead.kind == "recipe",
                CatalogDocumentHead.slug == recipe.identity.slug,
            )
        )
        assert operation is not None
        assert operation.id == operation_id
        assert operation.state == "succeeded"
        assert operation.actor == actor.subject
        assert operation.authority_revision == revision_id
        assert head is not None and head.active_revision_id == revision_id
        owner = RecipeCacheRemovalOwner.model_validate(operation.payload)
        assert owner.plan.intent.request_key == _REMOVE_KEY
        assert owner.plan.intent.actor == actor.subject
        assert owner.plan.intent.recipe_revision_id == revision_id
        assert owner.plan.intent.selector == selector
        assert owner.plan.image_archives == [ARCHIVE_SHA]
        assert owner.checkpoint.image_index == 1
        assert owner.checkpoint.image_pending_bytes is None
        assert owner.checkpoint.image_reclaimed_bytes == len(ARCHIVE)
        assert isinstance(operation.result, dict)
        result = RecipeCacheRemovalResult.model_validate(operation.result)
        assert result.operation_id == operation_id
        assert result.request_key == _REMOVE_KEY
        assert result.reclaimed_bytes == len(ARCHIVE)
