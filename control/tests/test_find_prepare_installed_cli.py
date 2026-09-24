"""Installed model selection and preparation through real Controller owners."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.jobs import JobService
from vonk_control.library_projection import LibraryProjection
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    Base,
    CatalogDocumentRevision,
    ModelCacheOperation,
)
from vonk_forge_contracts import ModelDefinition

from .test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ("tests.test_profile_load_installed_cli",)

_NOW = datetime(2026, 9, 24, tzinfo=UTC)
_TOKEN_KEY = b"installed-find-prepare-controller-key"
_PAYLOAD = b"controlled fixture model bytes"
_REQUEST_KEY = "11111111-1111-4111-8111-111111111195"


def _model_document(slug: str) -> dict[str, object]:
    document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    document["identity"]["publisher"] = "fixture"
    document["identity"]["slug"] = slug
    document["identity"]["model"]["publisher"] = "fixture"
    document["identity"]["model"]["slug"] = slug
    document["source"] = {
        "repository": f"https://huggingface.co/fixture/{slug}",
        "revision": "a" * 40,
    }
    document["files"] = [
        {
            "id": "weights",
            "path": "model.safetensors",
            "roles": ["weights"],
            "sha256": hashlib.sha256(_PAYLOAD).hexdigest(),
            "size_bytes": len(_PAYLOAD),
        }
    ]
    return ModelDefinition.model_validate(document).model_dump(mode="json")


def _run_cli(
    executable: Path,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *arguments],
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


@pytest.mark.lane
def test_installed_cli_finds_later_page_missing_asset_and_accepts_exact_cache_operation(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
) -> None:
    """A later-page exact choice explains missing bytes and starts durable work."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    codec = TokenCodec(_TOKEN_KEY)
    cursor_codec = codec.cursor_codec()
    catalog = CatalogEntityService(sessions, clock=lambda: _NOW, cursors=cursor_codec)
    revisions: dict[str, CatalogDocumentRevision] = {}
    for index in range(3):
        slug = f"candidate-{index:04d}"
        draft = catalog.create_draft(_model_document(slug), actor="catalog-test")
        revisions[slug] = catalog.resolve(draft.id, actor="catalog-test")

    cache = ModelCacheService(
        sessions,
        tmp_path / "managed-model-cache",
        reserve_bytes=0,
        clock=lambda: _NOW,
    )
    target = revisions["candidate-0001"]
    manifest = cache.resolve_artifact_set(model_content_sha256=target.content_digest)
    artifact = manifest.artifacts[0]

    # Model a verified cache set whose managed object has since been lost. The
    # cache owner's own reconciliation must discover the missing bytes and
    # project the repair action before the installed operator submits work.
    with cache._session(write=True) as session:
        cache_set = cache._ensure_set(session, manifest)
        cache_set.state = "cached"
        cache_set.verified_bytes = manifest.expected_bytes
        cache_set.verified_at = _NOW
        cache_set.updated_at = _NOW
    object_path = cache._object_path(artifact.sha256)
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(_PAYLOAD)
    cache._write_object_receipt(artifact, _NOW)
    object_path.unlink()
    cache._receipt_path(artifact.sha256).unlink()
    cache.reconcile_storage()
    with sessions() as session:
        reconciled = session.get(type(cache_set), manifest.digest)
        assert reconciled is not None
        assert reconciled.state == "needs-repair"
        assert reconciled.verified_bytes == 0

    library = LibraryProjection(
        sessions,
        cursors=cursor_codec,
        clock=lambda: _NOW,
    )
    api = create_app(
        jobs=JobService(sessions, clock=lambda: _NOW, cursors=cursor_codec),
        tokens=codec,
        audits=MemoryAuditStore(),
        library_projection=library,
        model_cache=cache,
        now=lambda: 100,
    )
    actor = Actor("operator", "operator")
    token = codec.issue(actor, ttl_seconds=1_000, now=0)
    headers = {"Authorization": f"Bearer {token}"}

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
            first = _run_cli(
                installed_vonkctl,
                ("model", "library", "--limit", "1", "--sort", "name", "--json"),
                environment,
                tmp_path,
            )
            assert first.returncode == 0, first.stderr
            first_page = json.loads(first.stdout)
            first_rows = first_page["models"]
            assert len(first_rows) == 1
            assert first_rows[0]["selector"] == "fixture/candidate-0000"
            assert first_page["next_cursor"]

            second = _run_cli(
                installed_vonkctl,
                (
                    "model",
                    "library",
                    "--limit",
                    "1",
                    "--sort",
                    "name",
                    "--cursor",
                    first_page["next_cursor"],
                    "--json",
                ),
                environment,
                tmp_path,
            )
            assert second.returncode == 0, second.stderr
            second_page = json.loads(second.stdout)
            assert len(second_page["models"]) == 1
            exact_choice = second_page["models"][0]
            target_selector = f"{target.publisher}/{target.slug}"
            assert exact_choice["selector"] == target_selector
            assert exact_choice["identity"]["content_sha256"] == target.content_digest
            assert exact_choice["local"]["controller"] == "failed"

            detail = _run_cli(
                installed_vonkctl,
                ("model", "detail", target_selector),
                environment,
                tmp_path,
            )
            assert detail.returncode == 0, detail.stderr
            assert "Next: vonkctl model download " + target_selector in detail.stdout

            accepted = _run_cli(
                installed_vonkctl,
                (
                    "model",
                    "download",
                    target_selector,
                    "--detach",
                    "--request-key",
                    _REQUEST_KEY,
                    "--json",
                ),
                environment,
                tmp_path,
            )
            assert accepted.returncode == 0, accepted.stdout + accepted.stderr
            receipt = json.loads(accepted.stdout)
            operation_id = receipt["operation_id"]
            assert receipt["selector"] == target_selector
            assert receipt["request_key"] == _REQUEST_KEY

            reconnected = _run_cli(
                installed_vonkctl,
                ("model", "progress", "--request-key", _REQUEST_KEY, "--json"),
                environment,
                tmp_path,
            )
            assert reconnected.returncode == 0, reconnected.stdout + reconnected.stderr
            observed = json.loads(reconnected.stdout)
            assert observed["operation_id"] == operation_id
            assert observed["selector"] == target_selector
            assert observed["request_key"] == _REQUEST_KEY
            assert observed["state"] == "queued"

            assert not object_path.exists()
            assert not cache._receipt_path(artifact.sha256).exists()
            assert token not in (
                first.stdout
                + first.stderr
                + second.stdout
                + second.stderr
                + detail.stdout
                + detail.stderr
                + accepted.stdout
                + accepted.stderr
                + reconnected.stdout
                + reconnected.stderr
            )
            assert [call[0] for call in peer.calls].count("POST") == 1

        with sessions() as session:
            operation = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == _REQUEST_KEY
                )
            )
            assert operation is not None
            assert operation.id == operation_id
            assert operation.state == "queued"
            assert operation.actor == actor.subject
            assert operation.payload["selector"] == target_selector
            assert operation.payload["operator_action"] == "download-model"
    finally:
        cache.close()
