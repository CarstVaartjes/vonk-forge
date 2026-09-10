"""Fresh PostgreSQL acceptance for the canonical Model/Recipe catalog.

This lane reads the current canonical recipe checkout used by the complete
Controller suite. It derives expected identities from that catalog, imports
all Models and Recipes into disposable PostgreSQL, and checks the typed API
and durable offline cache. It runs on Linux CI and OrbStack without historical
temporary receipts or opt-in skips. The transport uses real catalog archives;
this is repository integration evidence, not a publication or hardware claim.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, AuthError, TokenCodec
from vonk_control.catalog_api import install_catalog_routes
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.library_api import install_library_routes
from vonk_control.library_contract import (
    ModelLibraryResponse,
    RecipeDetailResponse,
    RecipeLibraryResponse,
)
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import CatalogDocumentRevision
from vonk_control.recipe_packages import (
    PACKAGE_MEDIA_TYPE,
    RecipePackageClient,
)
from vonk_control.source_bundles import SourceBundleStore

from .recipe_library_source import recipe_library_root

REPOSITORY = "CarstVaartjes/vonk-forge-recipes"
SHA1 = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class FrozenCorpus:
    index: dict[str, Any]
    package_root: Path
    publication_commit: str | None


def _require_sha1(value: object, *, label: str) -> str:
    if not isinstance(value, str) or SHA1.fullmatch(value) is None:
        pytest.fail(f"{label} must be a full lowercase SHA-1")
    return value


def _checkout_head(package_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(package_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        pytest.fail(f"frozen contracts checkout has no immutable Git HEAD: {error}")
    return _require_sha1(result.stdout.strip(), label="frozen contracts checkout HEAD")


def _load_frozen_corpus() -> FrozenCorpus:
    # The normal test/CI checkout is the authority. Missing input fails through
    # recipe_library_root instead of silently skipping an old temporary receipt.
    package_root = recipe_library_root()
    _checkout_head(package_root)
    index = json.loads((package_root / "catalog-index.json").read_text(encoding="utf-8"))
    if not isinstance(index, dict):
        pytest.fail("canonical catalog index is not an object")
    if (
        index.get("schema_version") != 2
        or index.get("kind") != "recipe-library-index"
        or index.get("repository") != REPOSITORY
    ):
        pytest.fail("canonical catalog index is not the schema-2 recipe-library index")
    if not isinstance(index.get("catalog_entities"), list) or not isinstance(
        index.get("recipes"), list
    ):
        pytest.fail("canonical catalog index does not contain Models and Recipes")
    publication = index.get("publication_commit")
    if publication is not None:
        _require_sha1(publication, label="catalog publication identity")
    _require_sha1(index.get("source_commit"), label="catalog source commit")
    return FrozenCorpus(
        index=index,
        package_root=package_root,
        publication_commit=publication,
    )


def _model_key(row: dict[str, Any]) -> tuple[str, str, str]:
    identity = row["document"]["identity"]
    return (
        str(identity["publisher"]),
        str(identity["slug"]),
        str(row["content_sha256"]),
    )


def _recipe_key(row: dict[str, Any]) -> tuple[str, str, str]:
    identity = row["document"]["identity"]
    return (
        str(identity["publisher"]),
        str(identity["slug"]),
        str(row["content_sha256"]),
    )


def _selected_model_keys(index: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {
        (
            str(selection["model"]["publisher"]),
            str(selection["model"]["slug"]),
            str(selection["model"]["content_sha256"]),
        )
        for row in index["recipes"]
        for selection in row["document"]["models"]
    }


def _package_path(corpus: FrozenCorpus, row: dict[str, Any]) -> Path:
    package = row["package"]
    location = Path(str(package["path"]))
    direct = corpus.package_root / location
    if direct.is_file():
        return direct
    pytest.fail(f"frozen package archive is unavailable: {location}")


def _transport(corpus: FrozenCorpus, calls: list[str]) -> httpx.MockTransport:
    index_bytes = json.dumps(
        corpus.index, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    by_name = {
        Path(str(row["package"]["path"])).name: row
        for row in corpus.index["recipes"]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("index.json"):
            # Raw GitHub serves generated catalog JSON as text/plain on some
            # publication paths.  The production reader accepts both MIME
            # types, so keep this acceptance transport representative.
            headers = {"content-type": "text/plain"}
            if corpus.publication_commit:
                headers["x-vonk-publication-commit"] = corpus.publication_commit
            return httpx.Response(200, headers=headers, content=index_bytes)
        row = by_name.get(Path(request.url.path).name)
        if row is None:
            return httpx.Response(404)
        archive = _package_path(corpus, row).read_bytes()
        return httpx.Response(
            200,
            headers={
                "content-type": PACKAGE_MEDIA_TYPE,
                **(
                    {"x-vonk-publication-commit": corpus.publication_commit}
                    if corpus.publication_commit
                    else {}
                ),
            },
            content=archive,
        )

    return httpx.MockTransport(handler)


def _upgrade_fresh_database(engine: Engine) -> None:
    # postgres_engine already supplies a fresh disposable Docker database on
    # Linux CI or OrbStack; its fixture owns engine availability and cleanup.
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", engine.url.render_as_string(hide_password=False)
    )
    command.upgrade(config, "head")


def _app(
    codec: TokenCodec,
    *,
    catalog: CatalogService,
    sync: ManagedRecipeCatalogSyncService,
    sessions: sessionmaker,
) -> TestClient:
    app = FastAPI()

    def actor(request: Request) -> Actor:
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return codec.verify(value[7:], now=int(datetime.now(UTC).timestamp()))
        except AuthError:
            raise HTTPException(status_code=401, detail="authentication failed") from None

    install_catalog_routes(
        app,
        actor_dependency=Depends(actor),
        audits=MemoryAuditStore(),
        service=catalog,
        managed_sync=sync,
    )
    install_library_routes(
        app,
        actor_dependency=Depends(actor),
        projection=LibraryProjection(
            sessions,
            cursors=codec.cursor_codec(),
        ),
    )
    token = codec.issue(
        Actor("fresh-launch-acceptance", "administrator"),
        now=int(datetime.now(UTC).timestamp()),
        ttl_seconds=300,
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _api_page_limit(api: TestClient, path: str) -> int:
    parameters = api.app.openapi()["paths"][path]["get"]["parameters"]
    for parameter in parameters:
        if parameter.get("name") != "limit":
            continue
        maximum = parameter.get("schema", {}).get("maximum")
        if type(maximum) is int and maximum >= 1:
            return maximum
    pytest.fail(f"{path} does not publish a bounded maximum page size")


def _library_models(api: TestClient) -> list[Any]:
    limit = _api_page_limit(api, "/api/model/library")
    cursor: str | None = None
    models: list[Any] = []
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = api.get("/api/model/library", params=params)
        assert response.status_code == 200, response.text
        page = ModelLibraryResponse.model_validate_json(response.content)
        models.extend(page.models)
        if page.next_cursor is None:
            return models
        assert page.next_cursor != cursor, "library pagination cursor did not advance"
        cursor = page.next_cursor


def _library_recipes(api: TestClient) -> list[Any]:
    limit = _api_page_limit(api, "/api/recipe/library")
    cursor: str | None = None
    recipes: list[Any] = []
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = api.get("/api/recipe/library", params={**params, "all_models": True})
        assert response.status_code == 200, response.text
        page = RecipeLibraryResponse.model_validate_json(response.content)
        recipes.extend(page.recipes)
        if page.next_cursor is None:
            return recipes
        assert page.next_cursor != cursor, "recipe pagination cursor did not advance"
        cursor = page.next_cursor


def test_frozen_corpus_closure_is_dynamic_and_keeps_unlinked_models() -> None:
    corpus = _load_frozen_corpus()
    models = corpus.index["catalog_entities"]
    recipes = corpus.index["recipes"]
    model_keys = {_model_key(row) for row in models}
    recipe_keys = {_recipe_key(row) for row in recipes}
    assert len(model_keys) == len(models)
    assert len(recipe_keys) == len(recipes)
    selected = _selected_model_keys(corpus.index)
    assert selected <= model_keys
    assert selected
    for row in recipes:
        package = row["package"]
        digest = str(package["sha256"])
        archive = _package_path(corpus, row)
        assert archive.stat().st_size == package["expected_bytes"]
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
    assert model_keys - selected


def test_fresh_postgres_imports_typed_canonical_model_recipe_api(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    corpus = _load_frozen_corpus()
    _upgrade_fresh_database(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    clock = lambda: datetime.now(UTC)
    catalog = CatalogService(
        sessions,
        clock=clock,
        cursors=TokenCodec(b"f" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )
    calls: list[str] = []
    reader = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=_transport(corpus, calls),
        publication_commit=corpus.publication_commit,
    )
    snapshot = reader.list()
    reader.prepare(snapshot)
    model_keys = {_model_key(row) for row in corpus.index["catalog_entities"]}
    fetched_items = [reader.fetch(item.uri) for item in snapshot.items]
    for item in fetched_items:
        assert item.package_handle is not None
        assert item.package_handle.source_commit == snapshot.commit
        if corpus.publication_commit is not None:
            assert item.package_handle.publication_commit == corpus.publication_commit
    package_model_keys = {
        identity
        for item in fetched_items
        for identity in item.package_handle.model_identities
    }
    assert package_model_keys <= model_keys
    assert _selected_model_keys(corpus.index) <= package_model_keys
    sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=reader, clock=clock
    )
    result = sync.sync(
        request_key="00000000-0000-4000-8000-000000000093",
        trigger="manual",
        actor="system:fresh-launch-acceptance",
        expected_commit=snapshot.commit,
    )
    assert result.state == "current", (
        f"catalog sync was not current: problems={list(result.problems)!r}; "
        f"processed={result.processed_count} imported={result.imported_count} "
        f"updated={result.updated_count} skipped={result.skipped_count}"
    )
    assert result.imported_count == len(corpus.index["recipes"])
    assert len([path for path in calls if path.endswith(".tar.gz")]) == len(
        corpus.index["recipes"]
    )

    forbidden_tables = {
        "local_recipes",
        "local_recipe_revisions",
        "managed_recipe_library_links",
        "recipe_imports",
        "recipe_import_items",
        "recipe_global_links",
        "recipe_test_reports",
        "catalog_entities",
        "catalog_entity_revisions",
        "catalog_entity_heads",
        "catalog_entity_model_references",
        "model_targets",
        "recipe_releases",
        "runtime_distributions",
        "patch_bundles",
    }
    assert not forbidden_tables.intersection(inspect(postgres_engine).get_table_names())

    with sessions() as session:
        assert session.scalar(
            select(func.count()).select_from(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.state == "active",
            )
        ) == len(corpus.index["catalog_entities"])
        assert session.scalar(
            select(func.count()).select_from(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        ) == len(corpus.index["recipes"])

    api = _app(
        TokenCodec(b"a" * 32),
        catalog=catalog,
        sync=sync,
        sessions=sessions,
    )
    library_models = _library_models(api)
    library_model_keys = {
        (item.identity.publisher, item.identity.slug, item.identity.content_sha256)
        for item in library_models
    }
    assert len(library_model_keys) == len(library_models)
    assert library_model_keys == {
        _model_key(row) for row in corpus.index["catalog_entities"]
    }
    expected_model_documents = {
        _model_key(row): row["document"] for row in corpus.index["catalog_entities"]
    }
    assert {
        (item.identity.publisher, item.identity.slug, item.identity.content_sha256): item.document.model_dump(
            mode="json"
        )
        for item in library_models
    } == expected_model_documents

    library_recipes = _library_recipes(api)
    by_digest = {item.identity.content_sha256: item for item in library_recipes}
    assert len(by_digest) == len(library_recipes)
    assert set(by_digest) == {_recipe_key(row)[2] for row in corpus.index["recipes"]}
    expected_recipe_documents = {
        _recipe_key(row)[2]: row["document"] for row in corpus.index["recipes"]
    }
    assert {
        item.identity.content_sha256: item.document.model_dump(mode="json")
        for item in library_recipes
    } == expected_recipe_documents
    multi_model_detail_seen = False
    for row in corpus.index["recipes"]:
        digest = _recipe_key(row)[2]
        detail_response = api.get(f"/api/recipe/{by_digest[digest].selector}")
        assert detail_response.status_code == 200, detail_response.text
        detail = RecipeDetailResponse.model_validate_json(detail_response.content)
        assert detail.identity.content_sha256 == digest
        assert detail.identity.slug == row["document"]["identity"]["slug"]
        assert [entry.selection.model_dump() for entry in detail.model_documents] == row[
            "document"
        ]["models"]
        assert [
            entry.model_document.model_dump(mode="json")
            for entry in detail.model_documents
        ] == [
            next(
                model["document"]
                for model in corpus.index["catalog_entities"]
                if model["content_sha256"] == selection["model"]["content_sha256"]
            )
            for selection in row["document"]["models"]
        ]
        if len(row["document"]["models"]) > 1:
            multi_model_detail_seen = True
            assert len(detail.model_documents) == len(row["document"]["models"])
    assert multi_model_detail_seen, "frozen corpus has no multi-Model Recipe detail"

    from vonk_control.catalog_api import CATALOG_OPERATION_IDS

    forbidden_paths = {
        "/api/catalog/entities",
        "/api/catalog/entities/{entity_id}",
        "/api/catalog/entities/{entity_id}/draft",
        "/api/catalog/entities/{entity_id}/resolve",
        "/api/catalog/recipes",
        "/api/catalog/recipes/{recipe_id}",
        "/api/catalog/imports/global",
        "/api/catalog/imports/recipe-library",
        "/api/catalog/imports/public",
    }
    assert not forbidden_paths.intersection(
        path for _method, path in CATALOG_OPERATION_IDS
    )
    forbidden_fragments = (
        "/entities",
        "model-target",
        "recipe-release",
        "runtime-distribution",
        "patch-bundle",
    )
    assert all(
        not any(fragment in path for fragment in forbidden_fragments)
        for _method, path in CATALOG_OPERATION_IDS
    )
    canonical_library_paths = {
        "/api/model/library",
        "/api/recipe/library",
        "/api/recipe/{selector}",
    }
    assert canonical_library_paths <= set(api.app.openapi()["paths"])
    operation_ids = {
        operation.get("operationId")
        for methods in api.app.openapi()["paths"].values()
        if isinstance(methods, dict)
        for operation in methods.values()
        if isinstance(operation, dict)
    }
    assert all(
        isinstance(operation_id, str)
        and "LocalRecipe" not in operation_id
        and "CatalogEntity" not in operation_id
        for operation_id in operation_ids
    )
    api.close()
    reader.close()

    # A new reader can continue from the durable snapshot and package objects
    # with the publication endpoint unavailable.  The failed index request is
    # visible in calls; no package request is hidden behind synthetic success.
    offline_calls: list[str] = []

    def offline(request: httpx.Request) -> httpx.Response:
        offline_calls.append(request.url.path)
        raise httpx.ConnectError("publication offline", request=request)

    restarted = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx.MockTransport(offline),
        publication_commit=corpus.publication_commit,
    )
    offline_snapshot = restarted.list()
    restarted.prepare(offline_snapshot)
    assert offline_snapshot.commit == snapshot.commit
    assert len(offline_snapshot.items) == len(corpus.index["recipes"])
    assert offline_calls == ["/v1/recipe-library/index.json"]
    assert not (tmp_path / "packages" / "snapshot.candidate.json").exists()
    restarted.close()
