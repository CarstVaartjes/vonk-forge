from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Connection, Engine, ExecutionContext
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import CatalogCode
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService, CatalogValidationError
from vonk_control.models import Base, CatalogDocumentHead, CatalogDocumentRevision
from vonk_control.source_bundles import SourceBundleStore
from vonk_forge_contracts import document_sha256

from tests.recipe_library_source import recipe_library_root
from tests.signed_recipe_release import SignedRecipeRelease, signed_recipe_releases

ROOT = recipe_library_root()
pytestmark = pytest.mark.usefixtures(signed_recipe_releases.__name__)


def _item(tmp_path: Path):
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    index["recipes"] = index["recipes"][:1]
    client = SignedRecipeRelease.from_library(index, ROOT).client(tmp_path / "packages")
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


def test_import_persists_canonical_model_recipe_and_package_identity(
    service: CatalogService, tmp_path: Path
) -> None:
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


def test_import_binds_captured_published_documents_before_sql(
    service: CatalogService, tmp_path: Path
) -> None:
    """Catch caller mutation changing already-validated bytes or dropping nulls."""
    client, item = _item(tmp_path)
    recipe = copy.deepcopy(dict(item.document))
    dependencies = [copy.deepcopy(dict(value)) for value in item.dependencies]
    # An explicitly present canonical null belongs to the published digest;
    # normalizing through a model must not erase its source presence.
    metadata = recipe["metadata"]
    assert isinstance(metadata, dict)
    metadata["alignment"] = None
    captured_recipe = copy.deepcopy(recipe)
    captured_dependencies = copy.deepcopy(dependencies)
    expected_digest = document_sha256(captured_recipe)
    with service._sessions() as session:
        engine = session.get_bind()
    assert isinstance(engine, Engine)
    mutated = False

    def mutate_caller(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        nonlocal mutated
        if not mutated and "catalog_document_revisions" in statement.lower():
            mutated = True
            metadata = recipe["metadata"]
            identity = dependencies[0]["identity"]
            assert isinstance(metadata, dict) and isinstance(identity, dict)
            metadata["description"] = "Caller replaced this after validation"
            identity["slug"] = "caller-replaced-after-validation"

    event.listen(engine, "before_cursor_execute", mutate_caller)
    try:
        imported = service.import_recipe_library(
            "test",
            library_commit=item.library_commit,
            source_path=item.source_path,
            document=recipe,
            expected_content_sha256=expected_digest,
            dependency_documents=dependencies,
        )
    except CatalogValidationError as error:
        if error.code != CatalogCode.MODEL_REFERENCE_MISSING:
            raise
        assert mutated
        assert dependencies[0]["identity"] != captured_dependencies[0]["identity"]
        pytest.fail(
            "Captured catalog identity changed after validation: caller mutation "
            "made the original pinned model reference unavailable"
        )
    finally:
        event.remove(engine, "before_cursor_execute", mutate_caller)
        client.close()
    assert (
        mutated and recipe != captured_recipe and dependencies != captured_dependencies
    )
    actual = service.get_recipe(imported.recipe_id)
    assert actual.document == captured_recipe
    assert actual.content_sha256 == expected_digest
    with service._sessions() as session:
        stored_models = session.scalars(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model"
            )
        ).all()
    assert {row.content_digest for row in stored_models} == {
        document_sha256(document) for document in captured_dependencies
    }
    assert {row.content_digest: row.document for row in stored_models} == {
        document_sha256(document): document for document in captured_dependencies
    }


@pytest.mark.parametrize("damage", ["model", "recipe", "dependency"])
def test_import_rejects_malformed_batch_before_any_revision_is_written(
    service: CatalogService, tmp_path: Path, damage: str
) -> None:
    """Catch writing valid earlier dependencies before later validation fails."""
    client, item = _item(tmp_path)
    recipe = copy.deepcopy(dict(item.document))
    dependencies = [copy.deepcopy(dict(value)) for value in item.dependencies]
    malformed = copy.deepcopy(dependencies[0])
    malformed["identity"] = ["not-a-canonical-identity"]
    try:
        with pytest.raises(CatalogValidationError):
            if damage == "model":
                service.import_catalog_models("test", [*dependencies, malformed])
            else:
                digest = document_sha256(recipe)
                if damage == "recipe":
                    recipe["identity"] = ["not-a-canonical-identity"]
                else:
                    dependencies.append(malformed)
                service.import_recipe_library(
                    "test",
                    library_commit=item.library_commit,
                    source_path=item.source_path,
                    document=recipe,
                    expected_content_sha256=digest,
                    dependency_documents=dependencies,
                )
        with service._sessions() as session:
            assert session.scalars(select(CatalogDocumentRevision)).all() == []
    finally:
        client.close()


def test_import_is_idempotent_and_persists_active_canonical_revision(
    service: CatalogService, tmp_path: Path
) -> None:
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


def test_reimport_selects_retained_recipe_head_without_rebinding_models(
    service: CatalogService, tmp_path: Path
) -> None:
    """A fresh resolution selects once; a later exact import restores its head."""
    client, item = _item(tmp_path)
    original = copy.deepcopy(dict(item.document))
    changed = copy.deepcopy(original)
    metadata = changed["metadata"]
    assert isinstance(metadata, dict)
    metadata["description"] = "A later accepted recipe revision"

    def import_document(document: dict[str, object]):
        return service.import_recipe_library(
            "test",
            library_commit=item.library_commit,
            source_path=item.source_path,
            document=document,
            expected_content_sha256=document_sha256(document),
            dependency_documents=item.dependencies,
        )

    try:
        first = import_document(original)
        with service._sessions() as session:
            head = session.scalar(
                select(CatalogDocumentHead).where(CatalogDocumentHead.kind == "recipe")
            )
            assert head is not None
            assert (
                head.active_revision_id,
                head.candidate_revision_id,
                head.generation,
            ) == (first.id, None, 1)
            model_heads = {
                row.id: (row.active_revision_id, row.generation)
                for row in session.scalars(
                    select(CatalogDocumentHead).where(
                        CatalogDocumentHead.kind == "model"
                    )
                )
            }
        later = import_document(changed)
        assert later.id != first.id
        assert service.get_recipe(first.recipe_id).id == later.id
        restored = import_document(original)
        assert restored.id == first.id
        assert service.get_recipe(first.recipe_id).document == original
        assert service.get_recipe(first.recipe_id).content_sha256 == item.content_sha256
        with service._sessions() as session:
            head = session.scalar(
                select(CatalogDocumentHead).where(CatalogDocumentHead.kind == "recipe")
            )
            assert head is not None
            assert (
                head.active_revision_id,
                head.candidate_revision_id,
                head.generation,
            ) == (first.id, None, 3)
            assert model_heads == {
                row.id: (row.active_revision_id, row.generation)
                for row in session.scalars(
                    select(CatalogDocumentHead).where(
                        CatalogDocumentHead.kind == "model"
                    )
                )
            }
    finally:
        client.close()


def test_import_rejects_changed_recipe_digest(
    service: CatalogService, tmp_path: Path
) -> None:
    client, item = _item(tmp_path)
    changed = copy.deepcopy(item.document)
    metadata = changed["metadata"]
    assert isinstance(metadata, dict)
    metadata["description"] += " changed"
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


def test_resolve_recipe_revision_resolves_active_model_references(
    service: CatalogService, tmp_path: Path
) -> None:
    """A documented recipe resolves only while every model reference is active.

    The service method previously called a ``_resolve_recipe`` attribute that
    only exists as a module-level function, so the call raised ``AttributeError``
    instead of resolving the pinned models.
    """
    client, item = _item(tmp_path)
    try:
        service.import_recipe_library(
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
    finally:
        client.close()
    assert (
        service.resolve_recipe_revision(item.document, actor="test")
        == item.content_sha256
    )
