"""Installed recipe-update acceptance across CLI, HTTPS, PostgreSQL, and restart."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import Actor
from vonk_control.models import Base, Job, User
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.recipe_image_availability_api import (
    install_recipe_operator_routes,
)
from vonk_control.recipe_update_contract import RecipeUpdateResponse
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    persist_runtime_image_receipt,
    prepare_runtime_image,
)
from vonk_control.strict_json import ControllerAPIRoute
from vonk_forge_contracts import RecipeDefinition

from .test_profile_load_installed_cli import (
    _https_api_peer,
    _process_environment,
)
from .test_recipe_image_availability import (
    ARCHIVE,
    _add_head,
    _add_revision,
    _recipe,
    _runtime,
)

pytest_plugins = ("tests.test_profile_load_installed_cli",)


def _cached_recipe(slug: str):
    base = _recipe("recipe-image.json")
    execution = base.execution
    assert execution.mode == "image"
    image_digest = hashlib.sha256(slug.encode()).hexdigest()
    return base.model_copy(
        update={
            "identity": base.identity.model_copy(update={"slug": slug}),
            "execution": execution.model_copy(
                update={
                    "image": execution.image.model_copy(update={"digest": image_digest})
                }
            ),
        }
    )


def _archive_variant(variant: int) -> bytes:
    return bytes(byte ^ variant for byte in ARCHIVE)


class _ImageTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def pull_and_export(
        self, reference: str, destination: Path, **_: object
    ) -> PulledImageEvidence:
        destination.write_bytes(self.payload)
        image_digest = reference.rsplit("@", 1)[1]
        return PulledImageEvidence(
            manifest_digest=image_digest,
            requested_manifest_digest=image_digest,
            config_id="sha256:" + hashlib.sha256(reference.encode()).hexdigest(),
            local_reference=reference,
            architecture="linux/arm64",
            runtime_interface="v1",
            archive_sha256=hashlib.sha256(self.payload).hexdigest(),
            archive_bytes=len(self.payload),
        )

    def inspect_archive(
        self,
        archive: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str,
        expected_archive_bytes: int,
    ) -> PulledImageEvidence:
        raise AssertionError(archive)


def _update_app(service: RecipeImageAvailabilityService) -> FastAPI:
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute
    install_recipe_operator_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        service=service,
    )
    return app


def _assert_parent(value: object) -> RecipeUpdateResponse:
    assert isinstance(value, RecipeUpdateResponse)
    return value


def test_installed_update_survives_cli_and_worker_death_with_frozen_cache_scope(
    installed_vonkctl: Path,
    postgres_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    now = [datetime.now(UTC)]
    storage = FilesystemRuntimeImageStorage(tmp_path / "managed-images")
    recipes: dict[str, RecipeDefinition] = {}
    receipts = {}

    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
    for index in range(3):
        recipe = _cached_recipe(f"installed-batch-{index}")
        revision_id = str(uuid.uuid4())
        recipes[revision_id] = recipe
        receipt = prepare_runtime_image(
            recipe,
            runtime=_runtime(),
            storage=storage,
            transport=_ImageTransport(_archive_variant(index)),
        )
        receipts[revision_id] = receipt
        with sessions.begin() as session:
            revision = _add_revision(session, revision_id, recipe)
            revision.document_id = str(uuid.uuid4())
            _add_head(session, revision)
            assert revision.execution_key is not None
            persist_runtime_image_receipt(
                session,
                recipe_revision_id=revision_id,
                original_content_digest=revision.content_digest,
                effective_execution_key=revision.execution_key,
                receipt=receipt,
                verified_at=now[0],
            )

    def authority(
        recipe_revision_id: str, *, force: bool = False
    ) -> tuple[RecipeDefinition, dict[str, object]]:
        return recipes[recipe_revision_id], _runtime()

    def fresh_service() -> RecipeImageAvailabilityService:
        return RecipeImageAvailabilityService(
            sessions,
            storage=storage,
            authority=authority,
            transport=None,
            clock=lambda: now[0],
            claim_lease_seconds=10,
        )

    service = fresh_service()
    initial_scope = service._updates._cached_revisions()
    assert set(initial_scope) == set(recipes)
    request_key = str(uuid.uuid4())
    accepted = Event()
    release_response = Event()
    update = service.update

    def hold_committed_response(
        *, actor: str, request_id: str, selectors: list[str] | None, all: bool
    ) -> RecipeUpdateResponse:
        result = update(
            actor=actor, request_id=request_id, selectors=selectors, all=all
        )
        accepted.set()
        assert release_response.wait(15), "test did not release the accepted response"
        return result

    monkeypatch.setattr(service, "update", hold_committed_response)
    headers = {"Authorization": "Bearer installed-update-test-token"}
    peer_root = tmp_path / "submit-peer"
    peer_root.mkdir()
    with (
        TestClient(_update_app(service)) as api,
        _https_api_peer(peer_root, api, headers) as (
            url,
            certificate,
            peer,
        ),
    ):
        peer.drop_responses.add(("POST", "/api/recipe/update"))
        environment = _process_environment(peer_root, url, certificate, headers)
        process = subprocess.Popen(
            [
                str(installed_vonkctl),
                "--json",
                "recipe",
                "update",
                "--all",
                "--request-key",
                request_key,
                "--detach",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=peer_root,
        )
        try:
            assert accepted.wait(15), "installed CLI did not reach the real API"
            with sessions() as session:
                stored = session.scalar(
                    select(Job).where(Job.request_id == request_key)
                )
                assert stored is not None
                assert stored.kind == "recipe.cache.update.v2"
            process.kill()
            process.communicate(timeout=5)
        finally:
            release_response.set()
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

        deadline = time.monotonic() + 5
        while (
            "POST",
            "/api/recipe/update",
        ) not in peer.dropped_responses and time.monotonic() < deadline:
            time.sleep(0.01)
        assert peer.dropped_responses == [("POST", "/api/recipe/update")]
        assert process.returncode is not None and process.returncode < 0

    parent = _assert_parent(service.get_operator_request(request_key, actor="operator"))
    parent_id = parent.id
    frozen_revision_ids = [child.recipe_revision_id for child in parent.children]
    frozen_request_keys = [child.request_key for child in parent.children]
    assert frozen_revision_ids == initial_scope
    assert all(child.operation_id is None for child in parent.children)

    # Current availability changes after acceptance: one original archive goes
    # missing and a different recipe becomes cached. Neither changes the parent.
    Path(receipts[initial_scope[0]].archive_path).unlink()
    replacement = _cached_recipe("installed-batch-added-after-acceptance")
    replacement_id = str(uuid.uuid4())
    recipes[replacement_id] = replacement
    replacement_receipt = prepare_runtime_image(
        replacement,
        runtime=_runtime(),
        storage=storage,
        transport=_ImageTransport(_archive_variant(7)),
    )
    with sessions.begin() as session:
        revision = _add_revision(session, replacement_id, replacement)
        revision.document_id = str(uuid.uuid4())
        _add_head(session, revision)
        assert revision.execution_key is not None
        persist_runtime_image_receipt(
            session,
            recipe_revision_id=replacement_id,
            original_content_digest=revision.content_digest,
            effective_execution_key=revision.execution_key,
            receipt=replacement_receipt,
            verified_at=now[0],
        )
    changed_scope = fresh_service()._updates._cached_revisions()
    assert set(changed_scope) == (set(initial_scope) - {initial_scope[0]}) | {
        replacement_id
    }

    # This worker dies after the actual child row commits, before it can save
    # that child ID into the parent's frozen document.
    worker = """
import json, os, sys
from datetime import datetime
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.models import CatalogDocumentRevision
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import RecipeDefinition
sessions = sessionmaker(create_engine(sys.argv[1]))
def authority(revision_id, **kwargs):
    with sessions() as session:
        row = session.get(CatalogDocumentRevision, revision_id)
        return RecipeDefinition.model_validate_json(json.dumps(row.document)), json.loads(sys.argv[3])
service = RecipeImageAvailabilityService(sessions, storage=FilesystemRuntimeImageStorage(Path(sys.argv[2])), authority=authority, clock=lambda: datetime.fromisoformat(sys.argv[4]), claim_lease_seconds=10)
actual = service._start_request
def die_after_child_commit(*args, **kwargs):
    result = actual(*args, **kwargs)
    os._exit(23)
service._start_request = die_after_child_commit
claim = service.claim_update(owner="installed-test-crashed-worker")
if claim is None:
    raise SystemExit(42)
service.run_update_claim(claim)
raise SystemExit(43)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            worker,
            postgres_engine.url.render_as_string(hide_password=False),
            str(storage.root),
            json.dumps(_runtime()),
            now[0].isoformat(),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 23, result.stderr
    interrupted = _assert_parent(fresh_service().get_operator_operation(parent_id))
    assert [child.recipe_revision_id for child in interrupted.children] == (
        frozen_revision_ids
    )
    assert interrupted.children[0].operation_id is None
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 2
        child_job = session.scalar(
            select(Job).where(Job.request_id == frozen_request_keys[0])
        )
        assert child_job is not None
        first_child_id = child_job.id

    # A newly constructed service represents the worker restart. It adopts the
    # committed deterministic child key, then issues the remaining frozen keys.
    now[0] += timedelta(seconds=11)
    restarted = fresh_service()
    for index in range(len(frozen_revision_ids)):
        if index:
            now[0] += timedelta(seconds=31)
            restarted = fresh_service()
        claim = restarted.claim_update(owner=f"installed-test-restarted-{index}")
        assert claim is not None
        restarted.run_update_claim(claim)
    recovered = _assert_parent(restarted.get_operator_operation(parent_id))
    assert [child.recipe_revision_id for child in recovered.children] == (
        frozen_revision_ids
    )
    assert recovered.children[0].operation_id == first_child_id
    assert all(child.operation_id is not None for child in recovered.children)
    with sessions() as session:
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 1 + len(frozen_revision_ids)
        assert len({job.request_id for job in jobs}) == len(jobs)
        assert all(
            session.scalar(select(Job).where(Job.request_id == key)) is not None
            for key in frozen_request_keys
        )

    # A fresh installed CLI can observe and replay only the original parent,
    # even though the current cache now contains a different complete scope.
    resume_root = tmp_path / "resume-peer"
    resume_root.mkdir()
    with (
        TestClient(_update_app(fresh_service())) as api,
        _https_api_peer(resume_root, api, headers) as (
            url,
            certificate,
            peer,
        ),
    ):
        environment = _process_environment(resume_root, url, certificate, headers)
        replay = subprocess.run(
            [
                str(installed_vonkctl),
                "--json",
                "recipe",
                "update",
                "--all",
                "--request-key",
                request_key,
                "--detach",
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            env=environment,
            cwd=resume_root,
        )
        assert replay.returncode == 0, replay.stderr
        replayed = json.loads(replay.stdout)
        assert replayed["id"] == parent_id
        assert [
            child["recipe_revision_id"] for child in replayed["children"]
        ] == frozen_revision_ids
        progress = subprocess.run(
            [
                str(installed_vonkctl),
                "--json",
                "recipe",
                "progress",
                "--request-key",
                request_key,
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
            env=environment,
            cwd=resume_root,
        )
        assert progress.returncode == 0, progress.stderr
        observed = json.loads(progress.stdout)
        assert observed["id"] == parent_id
        assert [
            child["recipe_revision_id"] for child in observed["children"]
        ] == frozen_revision_ids
        assert [(method, path) for method, path, _ in peer.calls] == [
            ("POST", "/api/recipe/update"),
            ("GET", f"/api/recipe/requests/{request_key}"),
        ]
        assert all(
            document is None or "installed-update-test-token" not in str(document)
            for _, _, document in peer.calls
        )
    with sessions() as session:
        assert len(list(session.scalars(select(Job)))) == 1 + len(frozen_revision_ids)
