"""P9 connected launch checks for the published Model/Recipe catalog.

This module is deliberately separate from SQLite/unit coverage.  The corpus
checks consume the production-reader evidence and package cache; the connected
test then uses the disposable PostgreSQL fixture and the Controller catalog API
when the pinned contract package and OrbStack are available.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

EVIDENCE_PATH = Path(
    os.environ.get(
        "VONK_CATALOG_CORPUS_EVIDENCE",
        "/private/tmp/vonk-production-reader-corpus-evidence.json",
    )
)
IMPORT_EVIDENCE_PATH = Path(
    os.environ.get(
        "VONK_CANONICAL_IMPORT_CORPUS_EVIDENCE",
        "/private/tmp/vonk-canonical-import-corpus-evidence.json",
    )
)
PUBLICATION_COMMIT = "2001c6502bfdc66141dd7224bfde5d77734e9959"
REPOSITORY = "CarstVaartjes/vonk-forge-recipes"


def _published_index() -> tuple[dict[str, Any], dict[str, Any], Path]:
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
    if index.get("schema_version") != 2 or index.get("kind") != "recipe-library-index":
        pytest.fail("published corpus index is not the schema-2 recipe-library index")
    if index.get("repository") != REPOSITORY:
        pytest.fail("published corpus index repository identity is invalid")
    return evidence, index, cache_root


def _model_key(row: dict[str, Any]) -> tuple[str, str, str]:
    document = row["document"]
    identity = document["identity"]
    return (
        str(identity["publisher"]),
        str(identity["slug"]),
        str(row["content_sha256"]),
    )


def _recipe_model_keys(recipe: dict[str, Any]) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for selection in recipe["document"]["models"]:
        model = selection["model"]
        keys.add((str(model["publisher"]), str(model["slug"]), str(model["content_sha256"])))
    return keys


def _package_model_keys(
    index: dict[str, Any], cache_root: Path
) -> set[tuple[str, str, str]]:
    """Resolve package model snapshots to the complete index identities."""

    model_digests = {
        (
            str(row["document"]["identity"]["publisher"]),
            str(row["document"]["identity"]["slug"]),
        ): str(row["content_sha256"])
        for row in index["catalog_entities"]
    }
    package_models: set[tuple[str, str, str]] = set()
    for row in index["recipes"]:
        package = row["package"]
        digest = str(package["sha256"])
        package_path = cache_root / digest[:2] / f"{digest}.tar.gz"
        with tarfile.open(package_path, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.name.startswith("models/") or not member.name.endswith(".json"):
                    continue
                stream = archive.extractfile(member)
                assert stream is not None
                document = json.load(stream)
                identity = document["identity"]
                identity_key = (str(identity["publisher"]), str(identity["slug"]))
                assert identity_key in model_digests, (
                    f"package {row['document']['identity']['slug']} carries an unknown model"
                )
                package_models.add((*identity_key, model_digests[identity_key]))
    return package_models


def test_p9_corpus_closure_is_dynamic_and_preserves_unlinked_models() -> None:
    """Derive all expected counts from the immutable index, including growth."""

    _evidence, index, _cache_root = _published_index()
    models = index["catalog_entities"]
    recipes = index["recipes"]
    model_keys = {_model_key(row) for row in models}
    assert len(model_keys) == len(models), "the canonical model index has duplicate identities"
    recipe_keys: set[tuple[str, str, str]] = set()
    for row in recipes:
        recipe_key = (
            str(row["document"]["identity"]["publisher"]),
            str(row["document"]["identity"]["slug"]),
            str(row["content_sha256"]),
        )
        assert recipe_key not in recipe_keys, "the canonical recipe index has duplicate identities"
        recipe_keys.add(recipe_key)
        assert _recipe_model_keys(row) <= model_keys, (
            f"recipe {recipe_key} references a model outside the complete model index"
        )
    package_model_keys = _package_model_keys(index, _cache_root)
    unlinked = model_keys - package_model_keys

    # These are lower bounds for the launch corpus.  All downstream assertions
    # use the live index sizes, so adding a model or recipe does not require a
    # test edit.
    assert len(models) >= 92
    assert len(recipes) >= 84
    assert len(unlinked) == 11

    if IMPORT_EVIDENCE_PATH.is_file():
        imported = json.loads(IMPORT_EVIDENCE_PATH.read_text(encoding="utf-8"))
        assert imported.get("imported") == len(recipes)
        assert imported.get("counts", {}).get("model") == len(package_model_keys)
        assert imported.get("counts", {}).get("recipe") == len(recipes)


def test_p9_package_snapshot_is_complete_and_offline_restartable() -> None:
    """Verify every package byte and the durable snapshot used after restart."""

    _evidence, index, cache_root = _published_index()
    snapshot = cache_root / "snapshot.json"
    assert snapshot.is_file()
    assert not (cache_root / "snapshot.candidate.json").exists(), (
        "an unpromoted package candidate must not be used as the restart snapshot"
    )
    package_digests: set[str] = set()
    for row in index["recipes"]:
        package = row["package"]
        digest = str(package["sha256"])
        assert digest not in package_digests
        package_digests.add(digest)
        package_path = cache_root / digest[:2] / f"{digest}.tar.gz"
        assert package_path.is_file(), package_path
        assert package_path.stat().st_size == package["expected_bytes"]
        hasher = hashlib.sha256()
        with package_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        assert hasher.hexdigest() == digest
    assert len(package_digests) == len(index["recipes"])


def test_p9_catalog_surface_has_no_legacy_entity_authoring_routes() -> None:
    """Keep old catalog entities out of the active HTTP authority surface."""

    catalog_api = importlib.import_module("vonk_control.catalog_api")
    operation_paths = [path for _method, path in catalog_api.CATALOG_OPERATION_IDS]
    forbidden_fragments = (
        "/entities",
        "model-target",
        "recipe-release",
        "runtime-distribution",
        "patch-bundle",
    )
    assert all(
        not any(fragment in path for fragment in forbidden_fragments)
        for path in operation_paths
    )


def _acceptance_module():
    try:
        return importlib.import_module(
            "control.tests.test_controller_catalog_postgres_acceptance"
        )
    except ImportError as error:
        pytest.fail(
            "P9 Controller acceptance is blocked by the production profile/API "
            f"import surface: {error}"
        )


def test_p9_postgres_controller_api_projects_model_recipe_pairs(
    postgres_engine: Any, tmp_path: Path
) -> None:
    """Import/query the full corpus, then verify API list/detail identity."""

    acceptance = _acceptance_module()
    index, cache_root = acceptance._corpus()
    acceptance._upgrade_fresh_database(postgres_engine)

    from sqlalchemy import func, select
    from sqlalchemy.orm import sessionmaker
    from vonk_control.auth import TokenCodec
    from vonk_control.catalog_service import CatalogService
    from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
    from vonk_control.models import CatalogDocumentRevision
    from vonk_control.recipe_packages import RecipePackageClient
    from vonk_control.source_bundles import SourceBundleStore

    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    clock = lambda: datetime.now(UTC)
    catalog = CatalogService(
        sessions,
        clock=clock,
        cursors=TokenCodec(b"p" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )
    reader = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=cache_root,
        transport=acceptance._transport(index, cache_root, []),
        publication_commit=acceptance.PUBLICATION_COMMIT,
    )
    snapshot = reader.list()
    reader.prepare(snapshot)
    sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=reader, clock=clock
    )
    result = sync.sync(
        request_key="00000000-0000-4000-8000-000000000092",
        trigger="manual",
        actor="system:p9-acceptance",
        expected_commit=snapshot.commit,
    )
    assert result.state == "current"
    assert result.imported_count == len(index["recipes"])
    with sessions() as session:
        assert session.scalar(
            select(func.count()).select_from(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.state == "active",
            )
        ) == len(index["catalog_entities"])

    api = acceptance._app(
        acceptance.TokenCodec(b"p" * 32),
        catalog=catalog,
        reader=reader,
        sync=sync,
    )
    public = api.get("/api/v1/catalog/public-recipes")
    assert public.status_code == 200, public.text
    public_by_slug = {row["slug"]: row for row in public.json()["recipes"]}
    assert len(public_by_slug) == len(index["recipes"])
    for row in index["recipes"]:
        document = row["document"]
        slug = document["identity"]["slug"]
        selected = document["models"][0]["model"]
        projected = public_by_slug[slug]
        assert projected["model_version_publisher"] == selected["publisher"]
        assert projected["model_version_slug"] == selected["slug"]

    local = api.get("/api/v1/catalog/recipes?limit=100")
    assert local.status_code == 200, local.text
    assert len(local.json()["recipes"]) == len(index["recipes"])
    for summary in local.json()["recipes"]:
        detail = api.get(f"/api/v1/catalog/recipes/{summary['recipe_id']}")
        assert detail.status_code == 200, detail.text
        document = detail.json()["document"]
        expected = next(
            row for row in index["recipes"] if row["document"]["identity"]["slug"] == summary["slug"]
        )
        assert detail.json()["content_sha256"] == expected["content_sha256"]
        assert document["models"][0]["model"] == expected["document"]["models"][0]["model"]
    api.close()
    reader.close()
