"""Fresh PostgreSQL acceptance for the canonical Model/Recipe catalog.

This lane consumes an immutable frozen-catalog receipt supplied by the
publication or contracts workflow.  It derives every expected identity and
count from that receipt, then imports the complete Model and Recipe corpus
into a newly created OrbStack PostgreSQL database before checking the typed
read API.  No synthetic catalog data is used here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
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
    LibraryRecipeDetail,
    LibraryRecipeList,
    LibrarySnapshot,
)
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import CatalogDocumentRevision
from vonk_control.recipe_packages import (
    PACKAGE_MEDIA_TYPE,
    RecipePackageClient,
)
from vonk_control.source_bundles import SourceBundleStore

REPOSITORY = "CarstVaartjes/vonk-forge-recipes"
SHA1 = re.compile(r"^[0-9a-f]{40}$")
EVIDENCE_ENV = "VONK_CATALOG_CORPUS_EVIDENCE"
FROZEN_ROOT_ENV = "VONK_FROZEN_CONTRACTS_ROOT"
FROZEN_COMMIT_ENV = "VONK_FROZEN_CONTRACTS_COMMIT"


@dataclass(frozen=True)
class FrozenCorpus:
    evidence: dict[str, Any]
    index: dict[str, Any]
    package_root: Path
    publication_commit: str | None
    source: str


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
    evidence_name = os.environ.get(EVIDENCE_ENV)
    frozen_root_name = os.environ.get(FROZEN_ROOT_ENV)
    if evidence_name:
        evidence_path = Path(evidence_name)
        if not evidence_path.is_file():
            pytest.skip(f"frozen catalog evidence is unavailable: {evidence_path}")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        cache_root = Path(str(evidence.get("cache_root", "")))
        snapshot_path = cache_root / "snapshot.json"
        if not snapshot_path.is_file():
            pytest.skip(f"frozen catalog snapshot is unavailable: {snapshot_path}")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        raw_index = snapshot.get("index")
        if not isinstance(raw_index, str):
            pytest.fail("frozen catalog receipt does not contain a durable index")
        index = json.loads(raw_index)
        publication = evidence.get("publication_commit") or snapshot.get(
            "publication_commit"
        )
        receipt_frozen_commit = evidence.get("frozen_contracts_commit") or snapshot.get(
            "frozen_contracts_commit"
        )
        if receipt_frozen_commit is not None:
            receipt_frozen_commit = _require_sha1(
                receipt_frozen_commit, label="frozen contracts receipt commit"
            )
            supplied_commit = os.environ.get(FROZEN_COMMIT_ENV)
            if supplied_commit is not None and _require_sha1(
                supplied_commit, label=FROZEN_COMMIT_ENV
            ) != receipt_frozen_commit:
                pytest.fail(
                    f"{FROZEN_COMMIT_ENV} does not match the frozen contracts receipt"
                )
        elif os.environ.get(FROZEN_COMMIT_ENV) is not None:
            pytest.fail(
                f"{FROZEN_COMMIT_ENV} has no frozen contracts binding in the receipt"
            )
        if publication is None and receipt_frozen_commit is None:
            pytest.fail(
                "frozen catalog receipt has no immutable publication or frozen contracts identity"
            )
        source = f"receipt:{evidence_path}"
        package_root = cache_root
        evidence_value = evidence
    elif frozen_root_name:
        package_root = Path(frozen_root_name)
        index_path = package_root / "catalog-index.json"
        if not index_path.is_file():
            pytest.skip(f"frozen catalog index is unavailable: {index_path}")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        publication = index.get("publication_commit")
        candidate = _require_sha1(
            os.environ.get(FROZEN_COMMIT_ENV), label=FROZEN_COMMIT_ENV
        )
        checkout_head = _checkout_head(package_root)
        if candidate != checkout_head:
            pytest.fail(
                f"{FROZEN_COMMIT_ENV} does not match checkout HEAD {checkout_head}"
            )
        source = f"contracts:{package_root}"
        evidence_value = {"frozen_contracts_commit": candidate}
    else:
        pytest.skip(
            f"set {EVIDENCE_ENV} or {FROZEN_ROOT_ENV} to run the immutable catalog lane"
        )

    if not isinstance(index, dict):
        pytest.fail("frozen catalog index is not an object")
    if (
        index.get("schema_version") != 2
        or index.get("kind") != "recipe-library-index"
        or index.get("repository") != REPOSITORY
    ):
        pytest.fail("frozen catalog index is not the schema-2 recipe-library index")
    if not isinstance(index.get("catalog_entities"), list) or not isinstance(
        index.get("recipes"), list
    ):
        pytest.fail("frozen catalog index does not contain Models and Recipes")
    if isinstance(evidence_value.get("models"), int):
        assert evidence_value["models"] == len(index["catalog_entities"])
    if isinstance(evidence_value.get("recipes"), int):
        assert evidence_value["recipes"] == len(index["recipes"])
    if publication is not None:
        _require_sha1(publication, label="frozen catalog publication identity")
    source_commit = index.get("source_commit")
    if not isinstance(source_commit, str) or SHA1.fullmatch(source_commit) is None:
        pytest.fail("frozen catalog source commit is invalid")
    return FrozenCorpus(
        evidence=evidence_value,
        index=index,
        package_root=package_root,
        publication_commit=publication,
        source=source,
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
    digest = str(package["sha256"])
    cached = corpus.package_root / digest[:2] / f"{digest}.tar.gz"
    if cached.is_file():
        return cached
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
            headers = {"content-type": "application/json"}
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
    return context == "orbstack" and info.returncode == 0 and any(
        line.strip().split() == ["Context:", "orbstack"]
        for line in info.stdout.splitlines()
    )


def _upgrade_fresh_database(engine: Engine) -> None:
    if not _docker_context_is_orbstack():
        pytest.skip("fresh PostgreSQL acceptance requires a healthy OrbStack Docker context")
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
    limit = _api_page_limit(api, "/api/v1/library")
    cursor: str | None = None
    models: list[Any] = []
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = api.get("/api/v1/library", params=params)
        assert response.status_code == 200, response.text
        page = LibrarySnapshot.model_validate_json(response.content)
        models.extend(page.models)
        if page.next_cursor is None:
            return models
        assert page.next_cursor != cursor, "library pagination cursor did not advance"
        cursor = page.next_cursor


def _library_recipes(api: TestClient) -> list[Any]:
    limit = _api_page_limit(api, "/api/v1/library/recipes")
    cursor: str | None = None
    recipes: list[Any] = []
    while True:
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = api.get("/api/v1/library/recipes", params=params)
        assert response.status_code == 200, response.text
        page = LibraryRecipeList.model_validate_json(response.content)
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


def test_frozen_checkout_rejects_mismatched_supplied_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package_root = tmp_path / "frozen-contracts"
    package_root.mkdir()
    (package_root / "catalog-index.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "kind": "recipe-library-index",
                "repository": REPOSITORY,
                "catalog_entities": [],
                "recipes": [],
                "source_commit": "c" * 40,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv(EVIDENCE_ENV, raising=False)
    monkeypatch.setenv(FROZEN_ROOT_ENV, str(package_root))
    monkeypatch.setenv(FROZEN_COMMIT_ENV, "b" * 40)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=f"{'a' * 40}\n"),
    )
    with pytest.raises(pytest.fail.Exception, match="does not match checkout HEAD"):
        _load_frozen_corpus()


def test_fresh_orbstack_postgres_imports_typed_canonical_model_recipe_api(
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
    package_model_keys = {
        identity
        for item in snapshot.items
        for identity in reader.fetch(item.uri).package_handle.model_identities
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
        (item.model.publisher, item.model.slug, item.model.content_sha256)
        for item in library_models
    }
    assert len(library_model_keys) == len(library_models)
    assert library_model_keys == {
        _model_key(row) for row in corpus.index["catalog_entities"]
    }
    actual_unlinked = {
        (item.model.publisher, item.model.slug, item.model.content_sha256)
        for item in library_models
        if not item.recipes
    }
    assert actual_unlinked == {
        _model_key(row) for row in corpus.index["catalog_entities"]
    } - _selected_model_keys(corpus.index)

    library_recipes = _library_recipes(api)
    by_digest = {item.content_sha256: item for item in library_recipes}
    assert len(by_digest) == len(library_recipes)
    assert set(by_digest) == {_recipe_key(row)[2] for row in corpus.index["recipes"]}
    for row in corpus.index["recipes"]:
        digest = _recipe_key(row)[2]
        detail_response = api.get(f"/api/v1/library/recipes/{by_digest[digest].recipe_id}")
        assert detail_response.status_code == 200, detail_response.text
        detail = LibraryRecipeDetail.model_validate_json(detail_response.content)
        assert detail.recipe.content_sha256 == digest
        assert detail.recipe.slug == row["document"]["identity"]["slug"]
        reference = row["document"]["models"][0]["model"]
        assert detail.model is not None
        assert detail.model.model_dump() == {
            "kind": "model",
            "publisher": reference["publisher"],
            "slug": reference["slug"],
            "content_sha256": reference["content_sha256"],
        }

    from vonk_control.catalog_api import CATALOG_OPERATION_IDS

    forbidden_paths = {
        "/api/v1/catalog/entities",
        "/api/v1/catalog/entities/{entity_id}",
        "/api/v1/catalog/entities/{entity_id}/draft",
        "/api/v1/catalog/entities/{entity_id}/resolve",
        "/api/v1/catalog/recipes",
        "/api/v1/catalog/recipes/{recipe_id}",
        "/api/v1/catalog/imports/global",
        "/api/v1/catalog/imports/recipe-library",
        "/api/v1/catalog/imports/public",
    }
    assert not forbidden_paths.intersection(
        path for _method, path in CATALOG_OPERATION_IDS
    )
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
