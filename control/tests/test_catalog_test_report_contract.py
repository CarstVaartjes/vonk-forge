"""Boundary coverage for attaching a canonical test report to a recipe revision.

The platform no longer owns a hand-written test-report schema: the canonical
``vonk_forge_contracts.TestReport`` is the single authority.  These tests pin the
seam where a report enters a catalog revision and where a stored projection is
parsed back, because that is where the model replaced the retired validator.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import TokenCodec
from vonk_control.catalog_revision_contract import (
    CatalogRevisionContractError,
    read_catalog_projection,
    write_catalog_projection,
)
from vonk_control.catalog_service import (
    CatalogService,
    CatalogValidationError,
    RecipeRevisionView,
)
from vonk_control.models import Base, CatalogDocumentRevision
from vonk_control.recipe_packages import PACKAGE_MEDIA_TYPE, RecipePackageClient

from tests.recipe_library_source import recipe_library_root

ROOT = recipe_library_root()


def _valid_report(recipe_sha256: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "recipe_sha256": recipe_sha256,
        "source_bundle_sha256": "1" * 64,
        "build_input_sha256": "2" * 64,
        "image_digest": "sha256:" + "3" * 64,
        "topology_name": "single-spark",
        "node_count": 1,
        "runtime": {
            "agent_version": "2.2.0",
            "container_runtime": "podman",
            "architecture": "linux/arm64",
        },
        "checks": [
            {"name": "container.started", "passed": True},
            {"name": "endpoint.healthy", "passed": True},
            {"name": "inference.completed", "passed": True},
        ],
        "started_at": "2026-09-05T00:00:00Z",
        "finished_at": "2026-09-05T00:10:00Z",
    }


def _model_violations(valid: dict[str, object]) -> dict[str, dict[str, object]]:
    """Return reports that only the canonical model rejects.

    Every value keeps the shape the retired JSON Schema accepted, so a
    regression that stops consulting ``TestReport`` accepts all of them again.
    """

    topology = deepcopy(valid)
    topology["topology_name"] = "Single Spark"

    reversed_window = deepcopy(valid)
    reversed_window["finished_at"] = "2026-09-04T23:59:59Z"

    duplicate_checks = deepcopy(valid)
    duplicate_checks["checks"] = [
        {"name": "container.started", "passed": True},
        {"name": "container.started", "passed": True},
    ]

    missing_version = deepcopy(valid)
    del missing_version["schema_version"]

    return {
        "topology_name_not_a_slug": topology,
        "finished_at_before_started_at": reversed_window,
        "duplicate_check_names": duplicate_checks,
        "missing_schema_version": missing_version,
    }


def _import_one_recipe(
    tmp_path: Path,
) -> tuple[CatalogService, RecipeRevisionView, RecipePackageClient]:
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    service = CatalogService(
        sessionmaker(engine, expire_on_commit=False),
        clock=lambda: datetime(2026, 9, 5, tzinfo=UTC),
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
    )
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    row = index["recipes"][0]
    package = (ROOT / row["package"]["path"]).read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("index.json"):
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(index).encode(),
            )
        return httpx.Response(
            200, headers={"content-type": PACKAGE_MEDIA_TYPE}, content=package
        )

    client = RecipePackageClient(
        "http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx.MockTransport(handler),
    )
    item = client.fetch(client.list().items[0].uri)
    view = service.import_recipe_library(
        "test",
        library_commit=item.library_commit,
        source_path=item.source_path,
        document=item.document,
        expected_content_sha256=item.content_sha256,
        dependency_documents=item.dependencies,
        package_handle=item.package_handle,
        package_sha256=item.package_sha256,
        source_bundle_sha256=item.source_bundle_sha256,
    )
    return service, view, client


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[tuple[CatalogService, RecipeRevisionView]]:
    service, view, client = _import_one_recipe(tmp_path)
    try:
        yield service, view
    finally:
        client.close()


def test_attach_test_report_accepts_canonical_report_and_exports_json(
    catalog: tuple[CatalogService, RecipeRevisionView],
) -> None:
    service, view = catalog
    assert view.content_sha256 is not None
    report = _valid_report(view.content_sha256)

    stored = service.attach_test_report(view.recipe_id, report, "test")

    assert stored == report
    exported = service.publication_export(view.recipe_id, "acme")
    assert exported["test_report"] == report
    recipe = exported["recipe"]
    assert isinstance(recipe, dict)
    identity = recipe["identity"]
    assert isinstance(identity, dict)
    assert identity["publisher"] == "acme"


@pytest.mark.parametrize(
    "case",
    [
        "topology_name_not_a_slug",
        "finished_at_before_started_at",
        "duplicate_check_names",
        "missing_schema_version",
    ],
)
def test_attach_test_report_rejects_report_the_model_forbids(
    catalog: tuple[CatalogService, RecipeRevisionView], case: str
) -> None:
    service, view = catalog
    assert view.content_sha256 is not None
    bad = _model_violations(_valid_report(view.content_sha256))[case]

    with pytest.raises(CatalogValidationError) as raised:
        service.attach_test_report(view.recipe_id, bad, "test")

    assert raised.value.code == "catalog.test_report_invalid"


def test_read_catalog_projection_rejects_stored_report_the_model_forbids(
    catalog: tuple[CatalogService, RecipeRevisionView],
) -> None:
    service, view = catalog
    assert view.content_sha256 is not None
    bad = _model_violations(_valid_report(view.content_sha256))[
        "finished_at_before_started_at"
    ]
    with service._sessions.begin() as session:
        revision = session.get(CatalogDocumentRevision, view.id)
        assert revision is not None
        projected = read_catalog_projection(revision).model_dump(
            mode="json", exclude_none=False
        )
        projected["test_report"] = bad
        # Simulate a row persisted by an earlier platform build: an active
        # revision is immutable through the ORM, so a legacy projection is
        # written through the Core UPDATE the import path also uses.
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == view.id)
            .values(projected=projected)
        )

    with service._sessions() as session:
        stored = session.get(CatalogDocumentRevision, view.id)
        assert stored is not None
        with pytest.raises(CatalogRevisionContractError):
            read_catalog_projection(stored)


def test_write_catalog_projection_rejects_report_the_model_forbids(
    catalog: tuple[CatalogService, RecipeRevisionView],
) -> None:
    service, view = catalog
    assert view.content_sha256 is not None
    bad = _model_violations(_valid_report(view.content_sha256))["duplicate_check_names"]
    with service._sessions() as session:
        revision = session.get(CatalogDocumentRevision, view.id)
        assert revision is not None
        projected = read_catalog_projection(revision).model_dump(
            mode="json", exclude_none=False
        )
    projected["test_report"] = bad

    with pytest.raises(CatalogRevisionContractError):
        write_catalog_projection(projected, kind="recipe")
