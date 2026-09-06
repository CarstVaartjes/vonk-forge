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
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.models import Base, CatalogDocumentRevision
from vonk_control.recipe_packages import (
    PACKAGE_MEDIA_TYPE,
    RecipePackageClient,
    RecipePackageError,
)

from tests.recipe_library_source import recipe_library_root

ROOT = recipe_library_root()


def _fixture() -> tuple[dict[str, object], dict[str, object], bytes]:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    row = index["recipes"][0]
    package = (ROOT / row["package"]["path"]).read_bytes()
    return index, row, package


def _archive_files(package: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:*") as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers()}


def _repack(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(files):
            info = tarfile.TarInfo(path)
            info.size = len(files[path])
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(files[path]))
    return gzip.compress(output.getvalue(), compresslevel=9, mtime=0)


def test_candidate_package_decodes_and_restart_only_reads_index(tmp_path: Path) -> None:
    index, row, package = _fixture()
    index["recipes"] = [row]
    package_path = row["package"]["path"]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(index).encode())
        assert request.url.path.endswith(Path(package_path).name)
        return httpx.Response(200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=package)

    client = RecipePackageClient("http://127.0.0.1", cache_root=tmp_path / "packages", transport=httpx.MockTransport(handler))
    snapshot = client.list()
    assert len(snapshot.catalog_entities) == 92
    item = client.fetch(snapshot.items[0].uri)
    assert item.document["kind"] == "recipe"
    assert item.dependencies and item.dependencies[0]["kind"] == "model"
    client.close()

    calls.clear()
    restarted = RecipePackageClient("http://127.0.0.1", cache_root=tmp_path / "packages", transport=httpx.MockTransport(handler))
    restarted.prepare(restarted.list())
    assert calls == ["/v1/recipe-library/index.json"]
    restarted.close()


def test_canonical_synthetic_nested_source_path_lists_and_fetches(
    tmp_path: Path,
) -> None:
    index_path = ROOT / "tests/fixtures/canonical-synthetic-canary/index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    row = index["recipes"][0]
    package = (ROOT / row["package"]["path"]).read_bytes()
    assert row["source_path"] == (
        "tests/fixtures/canonical-synthetic-canary/recipe.json"
    )
    index_bytes = json.dumps(index).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(
                200, headers={"content-type": "application/json"}, content=index_bytes
            )
        return httpx.Response(
            200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=package
        )

    client = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx.MockTransport(handler),
    )
    snapshot = client.list()
    item = client.fetch(snapshot.items[0].uri)
    assert item.source_path == row["source_path"]
    assert item.package_handle is not None
    assert item.package_handle.package_sha256 == row["package"]["sha256"]
    client.close()


@pytest.mark.parametrize(
    "source_path",
    (
        "../recipe.json",
        "/recipes/recipe.json",
        "recipes\\recipe.json",
        "recipes/\x00recipe.json",
        "recipes//recipe.json",
    ),
)
def test_candidate_package_rejects_unsafe_source_path(
    tmp_path: Path, source_path: str
) -> None:
    index, row, _package = _fixture()
    index = copy.deepcopy(index)
    index["recipes"] = [copy.deepcopy(row)]
    index["recipes"][0]["source_path"] = source_path

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("index.json")
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(index).encode(),
        )

    client = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RecipePackageError, match="invalid"):
        client.list()
    client.close()


def test_candidate_package_rejects_model_snapshot_digest_mismatch(tmp_path: Path) -> None:
    index, row, package = _fixture()
    files = _archive_files(package)
    model_path = next(path for path in files if path.startswith("models/") and path.endswith(".json"))
    model = json.loads(files[model_path])
    model["metadata"]["description"] += " tampered"
    files[model_path] = json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
    manifest = json.loads(files["manifest.json"])
    manifest["files"] = [
        {"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for path, content in sorted(files.items())
        if path != "manifest.json"
    ]
    package = _repack({**files, "manifest.json": json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()})
    index = copy.deepcopy(index)
    index["recipes"] = [row]
    row["package"]["sha256"] = hashlib.sha256(package).hexdigest()
    row["package"]["expected_bytes"] = len(package)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(index).encode())
        return httpx.Response(200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=package)

    client = RecipePackageClient("http://127.0.0.1", cache_root=tmp_path / "packages", transport=httpx.MockTransport(handler))
    with pytest.raises(RecipePackageError, match="invalid"):
        client.fetch(client.list().items[0].uri)
    client.close()


def test_candidate_package_imports_into_canonical_controller_documents(tmp_path: Path) -> None:
    index, row, package = _fixture()
    index["recipes"] = [row]
    index_bytes = json.dumps(index).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=index_bytes)
        return httpx.Response(200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=package)

    client = RecipePackageClient("http://127.0.0.1", cache_root=tmp_path / "packages", transport=httpx.MockTransport(handler))
    item = client.fetch(client.list().items[0].uri)
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
    )
    handle = item.package_handle
    assert handle is not None
    package_sha256 = handle.package_sha256
    assert handle.closure_path.is_dir()
    assert (handle.closure_path / "recipe.json").is_file()
    view = catalog.import_recipe_library(
        "package-test",
        library_commit=item.library_commit,
        source_path=item.source_path,
        document=item.document,
        expected_content_sha256=item.content_sha256,
        dependency_documents=item.dependencies,
        package_handle=handle,
        package_sha256=package_sha256,
        source_bundle_sha256=item.source_bundle_sha256,
    )
    assert view.schema_version == 2
    with sessions() as session:
        revisions = session.scalars(select(CatalogDocumentRevision)).all()
        assert {revision.kind for revision in revisions} == {"model", "recipe"}
        assert all(revision.state == "active" for revision in revisions)
        assert any(revision.content_digest == item.content_sha256 for revision in revisions)
        receipt = next(
            revision
            for revision in revisions
            if revision.kind == "recipe"
            and revision.content_digest == item.content_sha256
        )
        assert receipt.projected["package_sha256"] == package_sha256
        assert receipt.projected["source_bundle_sha256"] == item.source_bundle_sha256
        assert receipt.projected["package_handle"]["closure_path"].endswith(
            f"{package_sha256}/closure"
        )
    restarted = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
    )
    queried = restarted.get_recipe(view.recipe_id)
    assert queried.content_sha256 == item.content_sha256
    assert queried.document["identity"] == item.document["identity"]
    client.close()


def test_published_index_imports_all_models_including_unreferenced_versions(
    tmp_path: Path,
) -> None:
    index_path = ROOT / "catalog-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    model_documents = [entry["document"] for entry in index["catalog_entities"]]
    recipe_models = {
        reference["model"]["slug"]
        for row in index["recipes"]
        for reference in row["document"].get("models", [])
        if isinstance(reference, dict)
        and isinstance(reference.get("model"), dict)
        and isinstance(reference["model"].get("slug"), str)
    }
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog-models.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        cursors=TokenCodec(b"m" * 32).cursor_codec(),
    )
    assert catalog.import_catalog_models("index-test", model_documents) == 92
    with sessions() as session:
        revisions = session.scalars(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model"
            )
        ).all()
        assert len(revisions) == 92
        assert all(revision.state == "active" for revision in revisions)
        assert any(revision.slug not in recipe_models for revision in revisions)
