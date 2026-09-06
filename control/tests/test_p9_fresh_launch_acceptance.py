"""P9 connected launch checks for the published Model/Recipe catalog.

This module is deliberately separate from SQLite/unit coverage.  The corpus
checks consume the production-reader evidence and package cache; the connected
test then uses the disposable PostgreSQL fixture and the Controller catalog API
when the pinned contract package and OrbStack are available.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
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
REPOSITORY = "CarstVaartjes/vonk-forge-recipes"


def _published_index() -> tuple[dict[str, Any], dict[str, Any], Path, str]:
    if not EVIDENCE_PATH.is_file():
        pytest.skip(f"published corpus evidence is unavailable: {EVIDENCE_PATH}")
    evidence = json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))
    cache_root = Path(str(evidence.get("cache_root", "")))
    snapshot_path = cache_root / "snapshot.json"
    publication = evidence.get("publication_commit")
    if not isinstance(publication, str) or re.fullmatch(r"[0-9a-f]{40}", publication) is None:
        pytest.fail("published corpus receipt does not carry a valid publication commit")
    if not snapshot_path.is_file():
        pytest.skip("published corpus evidence has no durable index snapshot")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot_publication = snapshot.get("publication_commit")
    if snapshot_publication is not None and snapshot_publication != publication:
        pytest.fail("publication receipt and durable index snapshot disagree")
    index = json.loads(snapshot["index"])
    if index.get("schema_version") != 2 or index.get("kind") != "recipe-library-index":
        pytest.fail("published corpus index is not the schema-2 recipe-library index")
    if index.get("repository") != REPOSITORY:
        pytest.fail("published corpus index repository identity is invalid")
    return evidence, index, cache_root, publication


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


def _linked_package_cache(
    index: dict[str, Any], source_root: Path, destination_root: Path
) -> Path:
    """Expose verified package bytes through a private cache without copying them."""

    destination_root.mkdir(parents=True, exist_ok=True)
    prefixes = {
        str(row["package"]["sha256"])[:2] for row in index["recipes"]
    }
    for prefix in prefixes:
        source = source_root / prefix
        if not source.is_dir():
            raise AssertionError(f"verified package cache prefix is missing: {source}")
        (destination_root / prefix).symlink_to(source, target_is_directory=True)
    return destination_root


def test_p9_corpus_closure_is_dynamic_and_preserves_unlinked_models() -> None:
    """Derive all expected counts from the immutable index, including growth."""

    _evidence, index, _cache_root, _publication = _published_index()
    models = index["catalog_entities"]
    recipes = index["recipes"]
    model_keys = {_model_key(row) for row in models}
    assert len(model_keys) == len(models), "the canonical model index has duplicate identities"
    recipe_keys: set[tuple[str, str, str]] = set()
    selected_model_keys: set[tuple[str, str, str]] = set()
    for row in recipes:
        recipe_key = (
            str(row["document"]["identity"]["publisher"]),
            str(row["document"]["identity"]["slug"]),
            str(row["content_sha256"]),
        )
        assert recipe_key not in recipe_keys, "the canonical recipe index has duplicate identities"
        recipe_keys.add(recipe_key)
        selected_model_keys.update(_recipe_model_keys(row))
        assert _recipe_model_keys(row) <= model_keys, (
            f"recipe {recipe_key} references a model outside the complete model index"
        )
    package_model_keys = _package_model_keys(index, _cache_root)
    unlinked = model_keys - package_model_keys

    assert package_model_keys <= model_keys
    assert selected_model_keys <= package_model_keys
    assert package_model_keys | unlinked == model_keys

    if IMPORT_EVIDENCE_PATH.is_file():
        imported = json.loads(IMPORT_EVIDENCE_PATH.read_text(encoding="utf-8"))
        assert imported.get("imported") == len(recipes)
        assert imported.get("counts", {}).get("model") == len(package_model_keys)
        assert imported.get("counts", {}).get("recipe") == len(recipes)


def test_p9_package_snapshot_is_complete_and_offline_restartable() -> None:
    """Verify every package byte and the durable snapshot used after restart."""

    _evidence, index, cache_root, _publication = _published_index()
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
        module_name = "_p9_controller_catalog_postgres_acceptance"
        module = sys.modules.get(module_name)
        if module is not None:
            return module
        path = Path(__file__).with_name("test_controller_catalog_postgres_acceptance.py")
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load acceptance module from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
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
    _evidence, index, cache_root, publication = _published_index()
    acceptance._upgrade_fresh_database(postgres_engine)

    from sqlalchemy import func, select
    from sqlalchemy.orm import sessionmaker
    from vonk_control.auth import TokenCodec
    from vonk_control.catalog_service import CatalogService
    from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
    from vonk_control.library_api import install_library_routes
    from vonk_control.library_projection import LibraryProjection
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
    private_cache = _linked_package_cache(index, cache_root, tmp_path / "packages")
    reader = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=private_cache,
        transport=acceptance._transport(index, cache_root, []),
        publication_commit=publication,
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
        recipe_revisions = list(
            session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "active",
                )
            )
        )
    recipe_id_by_digest = {
        revision.content_digest: revision.document_id for revision in recipe_revisions
    }
    expected_pairs = {
        (
            str(selection["model"]["publisher"]),
            str(selection["model"]["slug"]),
            str(selection["model"]["content_sha256"]),
            recipe_id_by_digest[str(row["content_sha256"])],
        )
        for row in index["recipes"]
        for selection in row["document"]["models"]
    }

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

    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"l" * 32).cursor_codec(),
        clock=clock,
    )
    install_library_routes(
        api.app,
        actor_dependency=lambda: acceptance.Actor("acceptance", "administrator"),
        projection=projection,
    )
    library = api.get("/api/v1/library?limit=100")
    assert library.status_code == 200, library.text
    library_payload = library.json()
    assert len(library_payload["models"]) == len(index["catalog_entities"])
    actual_pairs = {
        (
            item["model"]["publisher"],
            item["model"]["slug"],
            item["model"]["content_sha256"],
            recipe["recipe_id"],
        )
        for item in library_payload["models"]
        for recipe in item["recipes"]
    }
    assert actual_pairs == expected_pairs
    assert library_payload["unlinked_recipes"] == []

    for recipe_id in sorted(recipe_id_by_digest.values()):
        detail = api.get(f"/api/v1/library/recipes/{recipe_id}")
        assert detail.status_code == 200, detail.text
        detail_payload = detail.json()
        document = next(
            row["document"]
            for row in index["recipes"]
            if recipe_id_by_digest[str(row["content_sha256"])] == recipe_id
        )
        assert detail_payload["recipe"]["recipe_id"] == recipe_id
        expected_model = document["models"][0]["model"]
        assert detail_payload["model"] == {
            "kind": "model-version",
            "publisher": expected_model["publisher"],
            "slug": expected_model["slug"],
            "content_sha256": expected_model["content_sha256"],
        }
    api.close()
    reader.close()
