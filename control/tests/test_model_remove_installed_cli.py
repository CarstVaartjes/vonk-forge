"""Installed model removal stays bound to its accepted content digest."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.jobs import JobService
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_contract import ModelCacheRemovalResult
from vonk_control.models import Base, CatalogDocumentRevision, ModelCacheOperation

from .test_find_prepare_installed_cli import _PAYLOAD, _model_document
from .test_model_cache_cancel_recovery import _artifact
from .test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)

_NOW = datetime(2026, 9, 24, tzinfo=UTC)
_DOWNLOAD_KEY = "00000000-0000-4000-8000-000000000161"
_REMOVE_KEY = "00000000-0000-4000-8000-000000000162"
_TOKEN_KEY = b"installed-model-removal-controller-key"


@pytest.mark.lane
def test_installed_model_remove_recovers_exact_digest_after_head_change(
    installed_vonkctl: Path,
    postgres_engine: Engine,
    tmp_path: Path,
) -> None:
    """Lost acceptance and a later catalog head cannot retarget one removal."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    codec = TokenCodec(_TOKEN_KEY)
    catalog = CatalogEntityService(
        sessions, clock=lambda: _NOW, cursors=codec.cursor_codec()
    )
    document = _model_document("exact-removal-target")
    draft = catalog.create_draft(document, actor="catalog-test")
    original = catalog.resolve(draft.id, actor="catalog-test")
    original_digest = original.content_digest
    assert isinstance(original_digest, str)

    cache_root = tmp_path / "managed-model-cache"
    cache = ModelCacheService(
        sessions,
        cache_root,
        reserve_bytes=0,
        clock=lambda: _NOW,
        fixture_sources=True,
    )
    source = tmp_path / "verified-source.bin"
    artifact = _artifact(source, _PAYLOAD)
    artifact["path"] = "model.safetensors"
    artifact["model_content_sha256"] = original_digest
    preview = cache.download_preview(
        model_content_sha256=original_digest,
        artifacts=[artifact],
    )
    assert preview["blockers"] == []
    downloaded = cache.start_download(
        actor="operator",
        request_key=_DOWNLOAD_KEY,
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=original_digest,
        selector=f"{original.publisher}/{original.slug}",
        artifacts=[artifact],
    )
    assert downloaded.state == "queued"
    assert cache.run_pending(limit=1) >= 1
    downloaded = cache.get_operation(downloaded.id)
    assert downloaded.state == "succeeded"

    manifest = cache.resolve_artifact_set(
        model_content_sha256=original_digest,
        artifacts=[artifact],
    )
    assert len(manifest.artifacts) == 1
    managed_object = cache._object_path(manifest.artifacts[0].sha256)
    managed_receipt = cache._receipt_path(manifest.artifacts[0].sha256)
    assert managed_object.read_bytes() == _PAYLOAD
    assert hashlib.sha256(managed_object.read_bytes()).hexdigest() == (
        manifest.artifacts[0].sha256
    )
    assert managed_receipt.is_file()

    library = LibraryProjection(
        sessions,
        cursors=codec.cursor_codec(),
        clock=lambda: _NOW,
    )
    api = create_app(
        jobs=JobService(sessions, clock=lambda: _NOW, cursors=codec.cursor_codec()),
        tokens=codec,
        audits=MemoryAuditStore(),
        library_projection=library,
        model_cache=cache,
        now=lambda: 100,
    )
    actor = Actor("operator", "operator")
    token = codec.issue(actor, ttl_seconds=1_000, now=0)
    headers = {"Authorization": f"Bearer {token}"}
    selector = f"{original.publisher}/{original.slug}".upper()
    selector_path = f"/api/model/{quote(selector, safe='')}"
    remove_path = f"{selector_path}/remove"
    request_path = f"/api/model/requests/{_REMOVE_KEY}"

    try:
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
                    "model",
                    "remove",
                    selector,
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
            assert review["target_identity"] == original_digest
            assert review["blockers"] == []
            assert managed_object.read_bytes() == _PAYLOAD
            assert all(method == "GET" for method, _, _ in peer.calls)
            reviewed_call_count = len(peer.calls)
            peer.held_response = ("POST", remove_path)
            peer.discard_held_response = True
            process = subprocess.Popen(
                [
                    str(installed_vonkctl),
                    "--json",
                    "model",
                    "remove",
                    selector,
                    "--yes",
                    "--review-digest",
                    review["review_digest"],
                    "--request-key",
                    _REMOVE_KEY,
                    "--detach",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                cwd=tmp_path,
            )
            try:
                assert peer.response_accepted.wait(timeout=25), peer.calls
                candidate = _model_document("exact-removal-target")
                metadata = candidate["metadata"]
                assert isinstance(metadata, dict)
                metadata["description"] = "Catalog head changed after acceptance"
                next_revision = catalog.revise(
                    original.document_id,
                    candidate,
                    actor="catalog-test",
                    expected_revision=original.revision_number,
                )
                active = catalog.resolve(next_revision.id, actor="catalog-test")
                assert active.content_digest != original_digest
                peer.release_held_response.set()
                stdout, stderr = process.communicate(timeout=45)
            except BaseException:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
                raise
            finally:
                peer.release_held_response.set()
            assert process.returncode == 0, stdout + stderr
            first_receipt = json.loads(stdout)
            assert first_receipt["request_key"] == _REMOVE_KEY
            assert first_receipt["selector"] == selector.casefold()
            assert first_receipt["model_content_sha256"] == original_digest
            assert first_receipt["state"] == "queued"
            assert first_receipt["phase"] == "queued"
            assert peer.response_accepted.is_set()
            assert peer.discard_held_response
            assert [
                (method, path) for method, path, _ in peer.calls[reviewed_call_count:]
            ] == [
                ("GET", request_path),
                ("GET", f"{selector_path}/remove-review"),
                ("POST", remove_path),
                ("GET", request_path),
            ]
            submitted = peer.calls[reviewed_call_count + 2][2]
            assert isinstance(submitted, dict)
            assert submitted == {
                "schema_version": 2,
                "request_key": _REMOVE_KEY,
                "model_content_sha256": original_digest,
                "review_digest": review["review_digest"],
            }

            # A second installed process with the same key reconnects to the
            # immutable accepted intent before asking the now-mutable selector.
            prior_call_count = len(peer.calls)
            replay = subprocess.run(
                [
                    str(installed_vonkctl),
                    "--json",
                    "model",
                    "remove",
                    selector,
                    "--yes",
                    "--review-digest",
                    review["review_digest"],
                    "--request-key",
                    _REMOVE_KEY,
                    "--detach",
                ],
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
                (method, path) for method, path, _ in peer.calls[prior_call_count:]
            ] == [("GET", request_path)]

            progress = subprocess.run(
                [
                    str(installed_vonkctl),
                    "--json",
                    "model",
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
            # The accepted operation has not been advanced by its owner yet.
            assert progress.returncode == 0, progress.stdout + progress.stderr
            observed = json.loads(progress.stdout)
            assert observed["operation_id"] == first_receipt["operation_id"]
            assert observed["request_key"] == _REMOVE_KEY
            assert observed["model_content_sha256"] == original_digest
            assert observed["state"] == "queued"
            assert observed["phase"] == "queued"
            assert observed["progress"]["phase"] == "queued"
            assert observed["progress"]["completed_items"] == 0
            assert observed["progress"]["total_items"] == 2
            assert observed["progress"]["completed_bytes"] == 0
            assert managed_object.is_file() and managed_receipt.is_file()
            assert token not in stdout + stderr + replay.stdout + replay.stderr
            assert token not in progress.stdout + progress.stderr
            assert sum(method == "POST" for method, _, _ in peer.calls) == 1

        for _ in range(6):
            cache.advance_removals(limit=1)
            settled = cache.get_operation(first_receipt["operation_id"])
            if settled.state == "succeeded":
                break
        assert settled.state == "succeeded"
        assert settled.model_content_sha256 == original_digest
        assert isinstance(settled.result, ModelCacheRemovalResult)
        assert settled.result.reclaimed_bytes == len(_PAYLOAD)
        assert not managed_object.exists()
        assert not managed_receipt.exists()

        with sessions() as session:
            persisted = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == _REMOVE_KEY
                )
            )
            assert persisted is not None
            assert persisted.state == "succeeded"
            assert persisted.actor == actor.subject
            assert persisted.payload["selector"] == selector.casefold()
            assert persisted.payload["model_content_sha256"] == original_digest
            active_head = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.content_digest == active.content_digest
                )
            )
            assert active_head is not None and active_head.state == "active"
    finally:
        cache.close()
