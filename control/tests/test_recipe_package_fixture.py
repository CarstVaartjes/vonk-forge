from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import CatalogSyncError, ManagedRecipeCatalogSyncService
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
)
from vonk_control.recipe_contract import recipe_content_sha256
from vonk_control.recipe_packages import (
    PACKAGE_MEDIA_TYPE,
    RecipePackageClient,
    RecipePackageError,
    load_recipe_package,
)
from vonk_control.source_bundles import SourceBundleStore

from tests.recipe_library_source import recipe_library_root


def _publisher_fixture() -> tuple[Path, Path, dict[str, object]]:
    root = recipe_library_root()
    index_path = root / "catalog-index.json"
    descriptor = json.loads(index_path.read_text(encoding="utf-8"))
    entities = descriptor.get("catalog_entities")
    recipes = descriptor.get("recipes")
    assert isinstance(entities, list)
    assert all(
        isinstance(row, dict)
        and isinstance(row.get("document"), dict)
        and row["document"].get("kind") == "model"
        and row["document"].get("schema_version") == 2
        for row in entities
    )
    assert isinstance(recipes, list) and recipes
    fixture = root / "packages"
    assert (root / recipes[0]["package"]["path"]).is_file()
    return fixture, index_path, descriptor


def test_publisher_fixture_imports_all_published_recipes_and_reuses_persistent_packages(
    tmp_path: Path,
) -> None:
    fixture, index_path, descriptor = _publisher_fixture()
    rows = descriptor["recipes"]
    expected_recipe_count = len(rows)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=index_path.read_bytes())
        return httpx.Response(200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=(fixture / Path(request.url.path).name).read_bytes())

    cache = tmp_path / "packages"
    client = RecipePackageClient("http://127.0.0.1", cache_root=cache, transport=httpx.MockTransport(handler))
    snapshot = client.list()
    client.prepare(snapshot)
    assert len(snapshot.items) == expected_recipe_count
    assert len([path for path in calls if path.endswith(".tar.gz")]) == expected_recipe_count
    client.close()

    calls.clear()
    restarted = RecipePackageClient("http://127.0.0.1", cache_root=cache, transport=httpx.MockTransport(handler))
    restarted.prepare(restarted.list())
    assert calls == ["/v1/recipe-library/index.json"]
    restarted.close()


@pytest.mark.parametrize("tampered_field", ["publisher", "content_sha256"])
def test_publisher_package_binds_manifest_metadata_identity_and_digest(
    tmp_path: Path, tampered_field: str
) -> None:
    fixture, _index_path, index = _publisher_fixture()
    row = index["recipes"][0]
    package_name = Path(row["package"]["path"]).name
    files: dict[str, bytes] = {}
    with tarfile.open(fixture / package_name, mode="r:*") as archive:
        for member in archive.getmembers():
            stream = archive.extractfile(member)
            assert stream is not None
            files[member.name] = stream.read()
    manifest = json.loads(files["manifest.json"])
    if tampered_field == "publisher":
        recipe = json.loads(files["recipe.json"])
        recipe["identity"]["publisher"] = "broken-publisher"
        files["recipe.json"] = _canonical(recipe) + b"\n"
    else:
        manifest["recipe_content_sha256"] = "0" * 64
    manifest["files"] = [
        {
            "path": path,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for path, content in sorted(files.items())
        if path != "manifest.json"
    ]
    files["manifest.json"] = _canonical(manifest) + b"\n"
    tampered = _repack(files)
    row["package"]["sha256"] = hashlib.sha256(tampered).hexdigest()
    row["package"]["expected_bytes"] = len(tampered)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=_canonical(index) + b"\n")
        return httpx.Response(200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=tampered)

    client = RecipePackageClient(
        "http://127.0.0.1", cache_root=tmp_path / "packages", transport=httpx.MockTransport(handler)
    )
    with pytest.raises(RecipePackageError, match="invalid"):
        client.prepare(client.list())
    assert len([path for path in calls if path.endswith(".tar.gz")]) == 1
    client.close()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _repack(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(files):
            info = tarfile.TarInfo(path)
            info.size = len(files[path])
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(files[path]))
    return gzip.compress(stream.getvalue(), compresslevel=9, mtime=0)


def _changed_package(package: bytes) -> tuple[bytes, dict[str, object]]:
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:*") as archive:
        for member in archive.getmembers():
            stream = archive.extractfile(member)
            assert stream is not None
            files[member.name] = stream.read()
    recipe = json.loads(files["recipe.json"])
    recipe["metadata"]["description"] += " (package sync fixture revision)"
    digest = recipe_content_sha256(recipe)
    files["recipe.json"] = _canonical(recipe) + b"\n"
    manifest = json.loads(files["manifest.json"])
    manifest["recipe_content_sha256"] = digest
    manifest["files"] = [
        {
            "path": path,
            "mode": 0o644,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for path, content in sorted(files.items())
        if path != "manifest.json"
    ]
    manifest["total_bytes"] = sum(int(entry["size"]) for entry in manifest["files"])
    files["manifest.json"] = _canonical(manifest) + b"\n"
    changed = _repack(files)
    return changed, {"sha256": hashlib.sha256(changed).hexdigest(), "expected_bytes": len(changed), "recipe_content_sha256": digest}


def _active_recipe_state(session) -> dict[str, tuple[str, str, int]]:
    heads = session.scalars(
        select(CatalogDocumentHead).where(CatalogDocumentHead.kind == "recipe")
    )
    return {
        head.slug: (
            revision.publisher,
            revision.content_digest,
            revision.revision_number,
        )
        for head in heads
        if head.active_revision_id is not None
        for revision in [session.get(CatalogDocumentRevision, head.active_revision_id)]
        if revision is not None
    }


def test_publisher_packages_sync_as_one_active_generation_and_survive_failures(tmp_path: Path) -> None:
    fixture, _index_path, original_index = _publisher_fixture()
    expected_recipe_count = len(original_index["recipes"])
    expected_model_count = len(original_index["catalog_entities"])
    index_bytes = _canonical(original_index) + b"\n"
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=index_bytes)
        name = Path(request.url.path).name
        return httpx.Response(
            200,
            headers={"content-type": PACKAGE_MEDIA_TYPE},
            content=package_overrides.get(name, (fixture / name).read_bytes()),
        )

    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        cursors=TokenCodec(b"p" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )
    package_overrides: dict[str, bytes] = {}
    cache = tmp_path / "packages"
    client = RecipePackageClient("http://127.0.0.1", cache_root=cache, transport=httpx.MockTransport(handler))
    sync = ManagedRecipeCatalogSyncService(
        sessions,
        catalog=catalog,
        reader=client,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )

    first = sync.sync(request_key="00000000-0000-0000-0000-000000000001", trigger="manual", actor="test")
    assert first.state == "current"
    assert first.imported_count == expected_recipe_count
    assert len([path for path in calls if path.endswith(".tar.gz")]) == expected_recipe_count
    with sessions() as session:
        assert session.scalar(
            select(func.count())
            .select_from(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.state == "active",
            )
        ) == expected_model_count
        assert session.scalar(
            select(func.count())
            .select_from(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        ) == expected_recipe_count
        assert session.scalar(select(func.count()).select_from(CatalogDocument)) == (
            expected_model_count + expected_recipe_count
        )
        assert session.scalar(select(func.count()).select_from(CatalogDocumentHead)) == (
            expected_model_count + expected_recipe_count
        )
        assert session.scalar(select(func.count()).select_from(CatalogRecipeModelReference)) > 0

    changed_index = copy.deepcopy(original_index)
    changed_row = changed_index["recipes"][0]
    original_package = (fixture / Path(changed_row["package"]["path"]).name).read_bytes()
    changed_bytes, changed_descriptor = _changed_package(original_package)
    changed_row["document"]["metadata"]["description"] += " (package sync fixture revision)"
    changed_row["content_sha256"] = changed_descriptor["recipe_content_sha256"]
    changed_row["package"].update(changed_descriptor)
    package_overrides[Path(changed_row["package"]["path"]).name] = changed_bytes
    changed_index["source_commit"] = "f" * 40
    index_bytes = _canonical(changed_index) + b"\n"
    calls.clear()
    second = sync.sync(request_key="00000000-0000-0000-0000-000000000002", trigger="automatic", actor="test")
    assert second.state == "current"
    assert second.updated_count == 1
    assert len([path for path in calls if path.endswith(".tar.gz")]) == 1
    with sessions() as session:
        after_second_recipes = _active_recipe_state(session)
        assert len(after_second_recipes) == expected_recipe_count
    client.close()

    # A fresh Controller process reuses every verified package object after a
    # restart; only the trusted index is requested again.
    calls.clear()
    restarted_good = RecipePackageClient(
        "http://127.0.0.1", cache_root=cache, transport=httpx.MockTransport(handler)
    )
    restarted_sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=restarted_good, clock=sync._clock
    )
    restarted_result = restarted_sync.sync(
        request_key="00000000-0000-0000-0000-000000000005",
        trigger="automatic",
        actor="test",
    )
    assert restarted_result.state == "current"
    assert calls == ["/v1/recipe-library/index.json"]
    restarted_good.close()

    # A malformed candidate fails during prepare, before any active link is changed.
    invalid_index = copy.deepcopy(changed_index)
    invalid_index["source_commit"] = "e" * 40
    invalid_name = Path(changed_row["package"]["path"]).name
    invalid_bytes = b"this is not a recipe package"
    invalid_index["recipes"][0]["package"]["sha256"] = hashlib.sha256(invalid_bytes).hexdigest()
    invalid_index["recipes"][0]["package"]["expected_bytes"] = len(invalid_bytes)
    package_overrides[invalid_name] = invalid_bytes
    index_bytes = _canonical(invalid_index) + b"\n"
    restarted = RecipePackageClient(
        "http://127.0.0.1", cache_root=cache, transport=httpx.MockTransport(handler)
    )
    invalid_snapshot = restarted.list()
    with pytest.raises(RecipePackageError, match="extract"):
        restarted.prepare(invalid_snapshot)
    with sessions() as session:
        assert _active_recipe_state(session) == after_second_recipes
    restarted.close()
    package_overrides[invalid_name] = changed_bytes

    # Force a later item failure after the package candidate has fully validated.
    failing_index = copy.deepcopy(changed_index)
    failing_index["source_commit"] = "d" * 40
    failing_row = failing_index["recipes"][1]
    failing_original = (fixture / Path(failing_row["package"]["path"]).name).read_bytes()
    failing_bytes, failing_descriptor = _changed_package(failing_original)
    failing_row["document"]["metadata"]["description"] += " (package sync fixture revision)"
    failing_row["content_sha256"] = failing_descriptor["recipe_content_sha256"]
    failing_row["package"].update(failing_descriptor)
    package_overrides[Path(failing_row["package"]["path"]).name] = failing_bytes
    index_bytes = _canonical(failing_index) + b"\n"
    original_import = catalog.import_recipe_library

    def fail_late(*args, **kwargs):
        if kwargs.get("source_path") == failing_row["source_path"]:
            raise CatalogSyncError("fixture.apply_failed", "injected package apply failure")
        return original_import(*args, **kwargs)

    catalog.import_recipe_library = fail_late  # type: ignore[method-assign]
    failing = ManagedRecipeCatalogSyncService(
        sessions,
        catalog=catalog,
        reader=RecipePackageClient("http://127.0.0.1", cache_root=cache, transport=httpx.MockTransport(handler)),
        clock=sync._clock,
    )
    failing_result = failing.sync(
        request_key="00000000-0000-0000-0000-000000000004",
        trigger="automatic",
        actor="test",
    )
    assert failing_result.state == "partial"
    assert failing_result.skipped_count >= 1
    with sessions() as session:
        assert _active_recipe_state(session) == after_second_recipes

    # Loading one identical cached package offline imports one recipe without
    # treating the partial view as a complete generation.
    current_row = changed_index["recipes"][0]
    offline = load_recipe_package(
        cache / current_row["package"]["sha256"][:2] / f"{current_row['package']['sha256']}.tar.gz",
        package_sha256=current_row["package"]["sha256"],
        publisher=current_row["document"]["identity"]["publisher"],
        slug=current_row["document"]["identity"]["slug"],
        recipe_content_sha256=current_row["content_sha256"],
        library_commit=changed_index["source_commit"],
        source_path=current_row["source_path"],
    )
    catalog.import_recipe_library(
        "offline-test",
        library_commit=offline.library_commit,
        source_path=offline.source_path,
        document=offline.document,
        expected_content_sha256=offline.content_sha256,
        dependency_documents=offline.dependencies,
        release_version=offline.release_history[0].version,
        release_released_at=offline.release_history[0].released_at,
    )
    with sessions() as session:
        assert len(_active_recipe_state(session)) == expected_recipe_count
