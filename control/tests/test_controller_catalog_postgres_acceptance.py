"""Disposable PostgreSQL acceptance for the published Model/Recipe corpus.

The large package bytes are produced once by the production-reader lane.  This
test deliberately consumes that evidence and cache instead of downloading a
second copy.  The test remains skipped when the cross-repository evidence is
not present; it is an acceptance lane, not a synthetic fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, AuthError, TokenCodec

pytest.importorskip("vonk_control.recipe_packages")

from vonk_control.catalog_api import install_catalog_routes
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.models import (
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    LocalRecipe,
    ManagedRecipeLibraryLink,
    RecipeImport,
)
from vonk_control.recipe_packages import PACKAGE_MEDIA_TYPE, RecipePackageClient
from vonk_control.source_bundles import SourceBundleStore

PUBLICATION_COMMIT = "2001c6502bfdc66141dd7224bfde5d77734e9959"
REPOSITORY = "CarstVaartjes/vonk-forge-recipes"
EVIDENCE_PATH = Path(
    os.environ.get(
        "VONK_CATALOG_CORPUS_EVIDENCE",
        "/private/tmp/vonk-production-reader-corpus-evidence.json",
    )
)


def _corpus() -> tuple[dict[str, Any], Path]:
    if not EVIDENCE_PATH.is_file():
        pytest.skip(f"published corpus evidence is unavailable: {EVIDENCE_PATH}")
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    cache_root = Path(str(evidence.get("cache_root", "")))
    snapshot_path = cache_root / "snapshot.json"
    if (
        evidence.get("publication_commit") != PUBLICATION_COMMIT
        or not snapshot_path.is_file()
    ):
        pytest.skip("published corpus evidence is not the requested immutable publication")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    index = json.loads(snapshot["index"])
    if index.get("repository") != REPOSITORY or index.get("schema_version") != 2:
        pytest.fail("published corpus index identity is invalid")
    return index, cache_root


def _docker_context_is_orbstack() -> bool:
    try:
        context = subprocess.run(
            ["docker", "context", "show"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        info = subprocess.run(
            ["docker", "info"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        context == "orbstack"
        and info.returncode == 0
        and "Context: orbstack" in info.stdout
    )


def _upgrade_fresh_database(engine: Engine) -> None:
    if not _docker_context_is_orbstack():
        pytest.skip(
            "fresh PostgreSQL acceptance requires a healthy OrbStack Docker context"
        )
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", engine.url.render_as_string(hide_password=False)
    )
    command.upgrade(config, "head")


def _transport(index: dict[str, Any], cache_root: Path, calls: list[str]):
    index_bytes = json.dumps(
        index, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    package_digests = {
        Path(row["package"]["path"]).name: row["package"]["sha256"]
        for row in index["recipes"]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("index.json"):
            # Raw GitHub serves generated JSON as text/plain in some paths.
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=index_bytes,
            )
        package_name = Path(request.url.path).name
        package_digest = package_digests.get(package_name)
        if package_digest is None:
            return httpx.Response(404)
        package_path = cache_root / package_digest[:2] / f"{package_digest}.tar.gz"
        if not package_path.is_file():
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={
                "content-type": PACKAGE_MEDIA_TYPE,
                "x-vonk-publication-commit": PUBLICATION_COMMIT,
            },
            content=package_path.read_bytes(),
        )

    return httpx.MockTransport(handler)


def _app(
    codec: TokenCodec,
    *,
    catalog: CatalogService,
    reader: RecipePackageClient,
    sync: ManagedRecipeCatalogSyncService,
) -> TestClient:
    app = FastAPI()

    def actor(request: Request) -> Actor:
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return codec.verify(value[7:])
        except AuthError:
            raise HTTPException(status_code=401, detail="authentication failed") from None

    install_catalog_routes(
        app,
        actor_dependency=Depends(actor),
        audits=MemoryAuditStore(),
        service=catalog,
        recipe_library=reader,
        managed_sync=sync,
    )
    token = codec.issue(Actor("viewer", "acceptance"), ttl_seconds=300)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def test_published_reader_accepts_raw_github_mime_and_binds_immutable_publication(
    tmp_path: Path,
) -> None:
    index, cache_root = _corpus()
    calls: list[str] = []
    reader = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=_transport(index, cache_root, calls),
        publication_commit=PUBLICATION_COMMIT,
    )
    snapshot = reader.list()
    item = reader.fetch(snapshot.items[0].uri)
    assert any(path.endswith("index.json") for path in calls)
    assert any(path.endswith(".tar.gz") for path in calls)
    assert item.package_handle is not None
    assert item.package_handle.publication_commit == PUBLICATION_COMMIT
    assert item.package_handle.source_commit == index["source_commit"]
    reader.close()


def test_fresh_orbstack_postgres_imports_exact_published_corpus_and_survives_offline_restart(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    index, cache_root = _corpus()
    models = [row["document"] for row in index["catalog_entities"]]
    recipes = index["recipes"]
    assert len(models) == 92
    assert all(document["kind"] == "model" for document in models)
    assert len(recipes) == 84

    _upgrade_fresh_database(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    clock = lambda: datetime.now(UTC)
    catalog = CatalogService(
        sessions,
        clock=clock,
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )
    calls: list[str] = []
    reader = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=cache_root,
        transport=_transport(index, cache_root, calls),
        publication_commit=PUBLICATION_COMMIT,
    )
    snapshot = reader.list()
    assert snapshot.repository == REPOSITORY
    assert snapshot.commit == index["source_commit"]
    assert len(snapshot.items) == 84
    reader.prepare(snapshot)

    sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=reader, clock=clock
    )
    result = sync.sync(
        request_key="00000000-0000-4000-8000-000000000091",
        trigger="manual",
        actor="system:acceptance",
        expected_commit=snapshot.commit,
    )
    assert result.state == "current"
    assert result.imported_count == 84

    model_keys = {
        (
            row["document"]["identity"]["publisher"],
            row["document"]["identity"]["slug"],
            row["content_sha256"],
        )
        for row in index["catalog_entities"]
    }
    package_model_keys: set[tuple[str, str, str]] = set()
    for row in recipes:
        item = reader.fetch(
            "vonk://catalog/"
            f"{row['document']['identity']['publisher']}/"
            f"{row['document']['identity']['slug']}@sha256:{row['content_sha256']}"
        )
        package_model_keys.update(item.package_handle.model_identities)
    assert len(package_model_keys) == 81
    assert len(model_keys - package_model_keys) == 11

    with sessions() as session:
        assert session.scalar(
            select(func.count()).select_from(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.state == "active",
            )
        ) == 92
        assert session.scalar(
            select(func.count()).select_from(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        ) == 84
        assert session.scalar(select(func.count()).select_from(LocalRecipe)) == 84
        assert session.scalar(select(func.count()).select_from(ManagedRecipeLibraryLink)) == 84
        receipts = session.scalars(select(RecipeImport)).all()
        assert len(receipts) == 84
        assert {
            receipt.redacted_source["commit"] for receipt in receipts
        } == {index["source_commit"]}
        assert {
            receipt.redacted_source["package_handle"]["publication_commit"]
            for receipt in receipts
        } == {PUBLICATION_COMMIT}

        revisions = session.scalars(select(CatalogDocumentRevision)).all()
        revision_ids = {
            (revision.kind, revision.publisher, revision.slug, revision.content_digest): revision.id
            for revision in revisions
            if revision.state == "active"
        }
        expected_bindings = {
            (
                revision_ids[
                    (
                        "recipe",
                        row["document"]["identity"]["publisher"],
                        row["document"]["identity"]["slug"],
                        row["content_sha256"],
                    )
                ],
                selection["id"],
                selection["model"]["publisher"],
                selection["model"]["slug"],
                selection["model"]["content_sha256"],
            )
            for row in recipes
            for selection in row["document"]["models"]
        }
        bindings = session.scalars(select(CatalogRecipeModelReference)).all()
        actual_bindings = {
            (
                binding.recipe_revision_id,
                binding.selection_id,
                binding.model_publisher,
                binding.model_slug,
                binding.model_content_digest,
            )
            for binding in bindings
        }
        assert actual_bindings == expected_bindings

    api = _app(
        TokenCodec(b"a" * 32), catalog=catalog, reader=reader, sync=sync
    )
    response = api.get("/api/v1/catalog/public-recipes")
    assert response.status_code == 200
    payload = response.json()
    assert payload["repository"] == REPOSITORY
    assert payload["commit"] == snapshot.commit
    assert len(payload["recipes"]) == 84
    assert {
        (item["publisher"], item["slug"], item["content_sha256"])
        for item in payload["recipes"]
    } == {
        (
            row["document"]["identity"]["publisher"],
            row["document"]["identity"]["slug"],
            row["content_sha256"],
        )
        for row in recipes
    }
    api.close()
    reader.close()

    # A new reader can continue from the durable snapshot and package objects
    # with the publication endpoint unavailable.  The failed index request is
    # visible in calls; no package request is hidden behind a synthetic success.
    offline_calls: list[str] = []

    def offline(request: httpx.Request) -> httpx.Response:
        offline_calls.append(request.url.path)
        raise httpx.ConnectError("publication offline", request=request)

    restarted = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=cache_root,
        transport=httpx.MockTransport(offline),
        publication_commit=PUBLICATION_COMMIT,
    )
    offline_snapshot = restarted.list()
    restarted.prepare(offline_snapshot)
    assert offline_snapshot.commit == snapshot.commit
    assert len(offline_snapshot.items) == 84
    assert offline_calls == ["/v1/recipe-library/index.json"]
    restarted.close()


def test_canonical_catalog_surface_does_not_expose_legacy_entity_routes() -> None:
    from vonk_control.catalog_api import CATALOG_OPERATION_IDS

    assert all("/entities" not in path for _method, path in CATALOG_OPERATION_IDS)
