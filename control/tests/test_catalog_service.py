from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService, CatalogValidationError
from vonk_control.models import Base, CatalogDocumentRevision
from vonk_control.recipe_packages import PACKAGE_MEDIA_TYPE, RecipePackageClient
from vonk_control.source_bundles import SourceBundleStore

from tests.recipe_library_source import recipe_library_root

ROOT = recipe_library_root()


def _item(tmp_path: Path):
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    row = index["recipes"][0]
    package = (ROOT / row["package"]["path"]).read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps(index).encode())
        return httpx.Response(200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=package)

    client = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx.MockTransport(handler),
    )
    item = client.fetch(client.list().items[0].uri)
    return client, item


@pytest.fixture
def service(tmp_path: Path) -> CatalogService:
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    return CatalogService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "bundles"),
    )


def test_import_persists_canonical_model_recipe_and_package_identity(service: CatalogService, tmp_path: Path) -> None:
    client, item = _item(tmp_path)
    handle = item.package_handle
    assert handle is not None
    view = service.import_recipe_library(
        "test",
        library_commit=item.library_commit,
        source_path=item.source_path,
        document=item.document,
        expected_content_sha256=item.content_sha256,
        dependency_documents=item.dependencies,
        package_handle=handle,
        package_sha256=handle.package_sha256,
        source_bundle_sha256=item.source_bundle_sha256,
    )
    assert view.schema_version == 2
    assert view.recipe_id and view.id
    with service._sessions() as session:
        recipe = service.get_recipe(view.recipe_id)
        assert recipe.content_sha256 == item.content_sha256
        assert recipe.document["identity"] == item.document["identity"]
        revisions = session.scalars(select(CatalogDocumentRevision)).all()
        assert {revision.kind for revision in revisions} == {"model", "recipe"}
    client.close()


def test_import_is_idempotent_and_persists_active_canonical_revision(service: CatalogService, tmp_path: Path) -> None:
    client, item = _item(tmp_path)
    kwargs = {
        "library_commit": item.library_commit,
        "source_path": item.source_path,
        "document": item.document,
        "expected_content_sha256": item.content_sha256,
        "dependency_documents": item.dependencies,
        "package_handle": item.package_handle,
        "package_sha256": item.package_sha256,
        "source_bundle_sha256": item.source_bundle_sha256,
    }
    first = service.import_recipe_library("test", **kwargs)
    second = service.import_recipe_library("test", **kwargs)
    assert second.id == first.id
    with service._sessions() as session:
        revisions = session.scalars(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        ).all()
    assert [revision.document_id for revision in revisions] == [first.recipe_id]
    client.close()


def test_import_rejects_changed_recipe_digest(service: CatalogService, tmp_path: Path) -> None:
    client, item = _item(tmp_path)
    changed = copy.deepcopy(item.document)
    changed["metadata"]["description"] += " changed"
    with pytest.raises(CatalogValidationError, match="does not match"):
        service.import_recipe_library(
            "test",
            library_commit=item.library_commit,
            source_path=item.source_path,
            document=changed,
            expected_content_sha256=item.content_sha256,
            dependency_documents=item.dependencies,
        )
    client.close()
