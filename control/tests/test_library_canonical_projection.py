from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import Actor, CursorError, TokenCodec
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.library_api import install_library_routes
from vonk_control.library_projection import LibraryProjection, LibraryProjectionError
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    ClusterMapping,
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES
from tests.recipe_library_source import recipe_library_root

ROOT = recipe_library_root()


def _document_section(document: dict[str, object], key: str) -> dict[str, object]:
    """Narrow one decoded JSON object member so a deliberate edit stays typed."""

    section = document[key]
    assert isinstance(section, dict)
    return section


def _insert_canonical_rows(
    sessions: sessionmaker,
    *,
    kind: str,
    template: dict[str, object],
    count: int,
    description: str | None = None,
    runtime_arguments: list[dict[str, object]] | None = None,
) -> None:
    rows: list[CatalogDocument | CatalogDocumentRevision] = []
    revisions: list[CatalogDocumentRevision] = []
    heads: list[CatalogDocumentHead] = []
    for index in range(count):
        document = copy.deepcopy(template)
        if description is not None:
            metadata = _document_section(document, "metadata")
            metadata["description"] = description
        identity = document["identity"]
        assert isinstance(identity, dict)
        identity["publisher"] = "test"
        identity["slug"] = f"{kind}-{index:04d}"
        if kind == "model":
            model = identity["model"]
            assert isinstance(model, dict)
            model["publisher"] = "test"
            model["slug"] = f"model-{index:04d}"
            family = identity["family"]
            assert isinstance(family, dict)
            family["publisher"] = "test"
            family["slug"] = "family"
            canonical = ModelDefinition.model_validate(document)
            title = canonical.identity.model.title
        else:
            if runtime_arguments is not None:
                runtime = document["runtime"]
                assert isinstance(runtime, dict)
                runtime["arguments"] = copy.deepcopy(runtime_arguments)
            canonical = RecipeDefinition.model_validate(document)
            title = canonical.metadata.title
        clean = canonical.model_dump(mode="json")
        document_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        now = datetime(2026, 9, 6, tzinfo=UTC)
        rows.append(
            CatalogDocument(
                id=document_id,
                kind=kind,
                publisher=canonical.identity.publisher,
                slug=canonical.identity.slug,
                title=title,
                created_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        revisions.append(
            CatalogDocumentRevision(
                id=revision_id,
                document_id=document_id,
                kind=kind,
                publisher=canonical.identity.publisher,
                slug=canonical.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=clean,
                content_digest=content_sha256(canonical),
                projected={},
                created_by="test",
                created_at=now,
            )
        )
        heads.append(
            CatalogDocumentHead(
                kind=kind,
                publisher=canonical.identity.publisher,
                slug=canonical.identity.slug,
                active_revision_id=revision_id,
            )
        )
    with sessions.begin() as session:
        session.add_all(rows)
        session.add_all(revisions)
        session.flush()
        session.add_all(heads)


def test_published_corpus_projects_all_models_and_exact_recipe_bindings(
    tmp_path: Path,
) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    expected_model_count = len(index["catalog_entities"])
    expected_recipe_count = len(index["recipes"])
    engine = create_engine(f"sqlite:///{tmp_path / 'library.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 9, 5, tzinfo=UTC)
    entities = CatalogEntityService(
        sessions, clock=clock, cursors=TokenCodec(b"l" * 32).cursor_codec()
    )

    model_documents = [entry["document"] for entry in index["catalog_entities"]]
    expected_models = {
        entry["content_sha256"]: ModelDefinition.model_validate(entry["document"])
        for entry in index["catalog_entities"]
    }
    expected_recipes = {
        row["content_sha256"]: RecipeDefinition.model_validate(row["document"])
        for row in index["recipes"]
    }
    for document in model_documents:
        revision = entities.create_draft(document, actor="test")
        entities.resolve(revision.id, actor="test")
    recipe_ids: set[str] = set()
    recipe_revision_ids: dict[str, str] = {}
    for row in index["recipes"]:
        revision = entities.create_draft(row["document"], actor="test")
        entities.resolve(revision.id, actor="test")
        recipe_ids.add(revision.document_id)
        recipe_revision_ids[row["content_sha256"]] = revision.id

    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"p" * 32).cursor_codec(),
        clock=clock,
    )
    models = []
    model_page = None
    cursor = None
    while True:
        page = projection.models(limit=100, cursor=cursor)
        if model_page is None:
            model_page = page
        models.extend(page.models)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert model_page is not None
    assert len(models) == expected_model_count
    assert {model.identity.content_sha256 for model in models} == set(expected_models)
    recipe_page = projection.recipe_library(limit=100, all_models=True)
    recipe_items = list(recipe_page.recipes)
    recipe_cursor = recipe_page.next_cursor
    while recipe_cursor is not None:
        next_page = projection.recipe_library(
            limit=100, cursor=recipe_cursor, all_models=True
        )
        recipe_items.extend(next_page.recipes)
        recipe_cursor = next_page.next_cursor
    recipe_page = recipe_page.model_copy(update={"recipes": recipe_items})
    assert len(recipe_page.recipes) == expected_recipe_count
    assert {item.identity.recipe_id for item in recipe_page.recipes} == recipe_ids
    for sort in ("name", "updated"):
        first_page = projection.recipe_library(
            limit=1,
            all_models=True,
            sort=sort,
        )
        assert first_page.next_cursor is not None
        second_page = projection.recipe_library(
            limit=1,
            all_models=True,
            sort=sort,
            cursor=first_page.next_cursor,
        )
        assert second_page.recipes
        assert (
            second_page.recipes[0].identity.content_sha256
            != first_page.recipes[0].identity.content_sha256
        )
    for item in recipe_page.recipes:
        assert (
            item.identity.recipe_revision_id
            == recipe_revision_ids[item.identity.content_sha256]
        )
        assert item.identity.recipe_revision_id not in {
            item.identity.recipe_id,
            item.identity.content_sha256,
        }

    # Creator is faceted and filterable on both nouns.
    assert set(model_page.facets.publisher) == {
        model.identity.publisher for model in models
    }
    assert set(recipe_page.facets.publisher) >= {
        item.identity.publisher for item in recipe_page.recipes
    }
    creator = min({model.identity.publisher for model in models})
    creator_models = projection.models(limit=100, publisher=[creator]).models
    assert creator_models
    assert {model.identity.publisher for model in creator_models} == {creator}

    # Abliteration is the recipe's declared alignment, faceted and filterable,
    # and it surfaces on the models that the aligned recipes serve.
    assert set(recipe_page.facets.alignment) == {
        item.alignment for item in recipe_page.recipes if item.alignment
    } | set(model_page.facets.alignment)
    assert "abliterated" in recipe_page.facets.alignment
    abliterated = projection.recipe_library(
        limit=100, all_models=True, alignment=["abliterated"]
    ).recipes
    assert abliterated
    assert {item.alignment for item in abliterated} == {"abliterated"}
    assert len(abliterated) == sum(
        1 for item in recipe_page.recipes if item.alignment == "abliterated"
    )
    assert any("abliterated" in model.alignment for model in models)

    # Sparks is the recipe topology node count, faceted and filterable.
    assert {item.node_count for item in recipe_page.recipes} == set(
        recipe_page.facets.sparks
    )
    sparks = max(recipe_page.facets.sparks)
    by_sparks = projection.recipe_library(
        limit=100, all_models=True, sparks=[sparks]
    ).recipes
    assert by_sparks
    assert {item.node_count for item in by_sparks} == {sparks}

    # Several model selectors are a union. A repeated query parameter used to
    # reach a single-value parameter, so every selector but the last was lost.
    selectors = sorted({model.selector for model in models})
    first, second = selectors[0], selectors[1]
    one = {
        item.identity.recipe_id
        for item in projection.recipe_library(
            limit=100, all_models=False, model_selectors=[first]
        ).recipes
    }
    two = {
        item.identity.recipe_id
        for item in projection.recipe_library(
            limit=100, all_models=False, model_selectors=[second]
        ).recipes
    }
    both = {
        item.identity.recipe_id
        for item in projection.recipe_library(
            limit=100, all_models=False, model_selectors=[first, second]
        ).recipes
    }
    assert both == one | two

    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=Depends(lambda: Actor("test", "viewer")),
        projection=projection,
    )
    client = TestClient(app)
    response = client.get("/api/model/library", params={"limit": 512})
    assert response.status_code == 200
    assert len(response.content) <= MAX_CONTROL_DOCUMENT_BYTES
    payload = response.json()
    model_rows = list(payload["models"])
    model_cursor = payload["next_cursor"]
    while model_cursor is not None:
        response = client.get(
            "/api/model/library", params={"limit": 512, "cursor": model_cursor}
        )
        assert response.status_code == 200
        assert len(response.content) <= MAX_CONTROL_DOCUMENT_BYTES
        payload = response.json()
        model_rows.extend(payload["models"])
        model_cursor = payload["next_cursor"]
    payload["models"] = model_rows
    assert len(payload["models"]) == expected_model_count
    assert {model["identity"]["kind"] for model in payload["models"]} == {"model"}
    assert {model["identity"]["content_sha256"] for model in payload["models"]} == set(
        expected_models
    )
    assert all(
        "recipes" not in model and "source_kind" not in model
        for model in payload["models"]
    )
    library_model_schema = app.openapi()["components"]["schemas"][
        "LibraryModelProjection"
    ]
    assert library_model_schema["properties"]["local"]["$ref"].endswith(
        "LibraryLocalState"
    )
    recipe_response = client.get("/api/recipe/library", params={"all_models": True})
    assert recipe_response.status_code == 200
    assert len(recipe_response.content) <= MAX_CONTROL_DOCUMENT_BYTES
    recipe_payload = recipe_response.json()
    recipe_rows = list(recipe_payload["recipes"])
    recipe_cursor = recipe_payload["next_cursor"]
    while recipe_cursor is not None:
        recipe_response = client.get(
            "/api/recipe/library",
            params={"all_models": True, "cursor": recipe_cursor},
        )
        assert recipe_response.status_code == 200
        assert len(recipe_response.content) <= MAX_CONTROL_DOCUMENT_BYTES
        recipe_payload = recipe_response.json()
        recipe_rows.extend(recipe_payload["recipes"])
        recipe_cursor = recipe_payload["next_cursor"]
    recipe_payload["recipes"] = recipe_rows
    assert len(recipe_payload["recipes"]) == expected_recipe_count
    assert {
        recipe["identity"]["recipe_id"] for recipe in recipe_payload["recipes"]
    } == recipe_ids
    first_recipe = recipe_payload["recipes"][0]
    first_expected = expected_recipes[first_recipe["identity"]["content_sha256"]]
    assert first_recipe["document"] == first_expected.model_dump(mode="json")
    assert first_recipe["document"]["runtime"]["engine"] == (
        first_expected.runtime.engine
    )
    assert first_recipe["document"]["release"]["version"] == (
        first_expected.release.version
    )
    assert first_recipe["document"]["topology"]["node_count"] == (
        first_expected.topology.node_count
    )
    assert first_recipe["document"]["topology"]["roles"][0]["resources"] == (
        first_expected.topology.roles[0].resources.model_dump(mode="json")
    )
    assert first_recipe["document"]["models"] == [
        selection.model_dump(mode="json") for selection in first_expected.models
    ]
    recipe_selector = first_recipe["selector"]
    detail = client.get(f"/api/recipe/{recipe_selector}")
    assert detail.status_code == 200, detail.text
    detail_payload = detail.json()
    assert detail_payload["document"]["execution"]
    assert detail_payload["document"]["models"]
    assert len(detail_payload["identity"]["content_sha256"]) == 64
    assert "source_kind" not in detail_payload["identity"]
    assert "visual_recipe" not in detail_payload
    assert "VisualRecipeDocument" not in app.openapi()["components"]["schemas"]
    expected_recipe = expected_recipes[detail_payload["identity"]["content_sha256"]]
    assert detail_payload["document"] == expected_recipe.model_dump(mode="json")
    assert detail_payload["document"]["runtime"] == expected_recipe.runtime.model_dump(
        mode="json"
    )
    assert detail_payload["document"][
        "topology"
    ] == expected_recipe.topology.model_dump(mode="json")
    assert detail_payload["document"][
        "settings"
    ] == expected_recipe.settings.model_dump(mode="json")
    assert (
        detail_payload["identity"]["recipe_revision_id"]
        == recipe_revision_ids[detail_payload["identity"]["content_sha256"]]
    )
    assert detail_payload["identity"]["recipe_revision_id"] not in {
        detail_payload["identity"]["recipe_id"],
        detail_payload["identity"]["content_sha256"],
    }
    multi_model_row = next(
        row for row in index["recipes"] if len(row["document"].get("models", [])) > 1
    )
    multi_model_recipe = RecipeDefinition.model_validate(multi_model_row["document"])
    multi_model_detail = client.get(
        "/api/recipe/"
        + multi_model_recipe.identity.publisher
        + "/"
        + multi_model_recipe.identity.slug
    )
    assert multi_model_detail.status_code == 200
    multi_model_payload = multi_model_detail.json()
    expected_model_documents = [
        expected_models[selection.model.content_sha256].model_dump(mode="json")
        for selection in multi_model_recipe.models
    ]
    assert [entry["selection"] for entry in multi_model_payload["model_documents"]] == [
        selection.model_dump(mode="json") for selection in multi_model_recipe.models
    ]
    assert [
        entry["model_document"] for entry in multi_model_payload["model_documents"]
    ] == expected_model_documents
    assert [
        entry["model_document"]["identity"]["model"]["slug"]
        for entry in multi_model_payload["model_documents"]
    ] == [
        expected_models[selection.model.content_sha256].identity.model.slug
        for selection in multi_model_recipe.models
    ]
    assert [
        entry["selection"]["files"] for entry in multi_model_payload["model_documents"]
    ] == [
        selection.model_dump(mode="json")["files"]
        for selection in multi_model_recipe.models
    ]
    assert (
        multi_model_payload["model_documents"][0]["selection"]["files"]
        != multi_model_payload["model_documents"][1]["selection"]["files"]
    )


def test_database_local_projection_reads_cache_build_and_spark_evidence(
    tmp_path: Path,
) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    recipe_document = copy.deepcopy(index["recipes"][0]["document"])
    selected_model = RecipeDefinition.model_validate(recipe_document).models[0].model
    model_document = copy.deepcopy(
        next(
            entry["document"]
            for entry in index["catalog_entities"]
            if entry["content_sha256"] == selected_model.content_sha256
            and entry["document"]["identity"]["publisher"] == selected_model.publisher
            and entry["document"]["identity"]["slug"] == selected_model.slug
        )
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'local-state.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    entities = CatalogEntityService(
        sessions,
        clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        cursors=TokenCodec(b"d" * 32).cursor_codec(),
    )
    model_revision = entities.create_draft(model_document, actor="test")
    entities.resolve(model_revision.id, actor="test")
    recipe_revision = entities.create_draft(recipe_document, actor="test")
    entities.resolve(recipe_revision.id, actor="test")

    old_document = copy.deepcopy(model_revision.document)
    _document_section(old_document, "identity")["version"] = "0.0.1"
    old_digest = content_sha256(ModelDefinition.model_validate(old_document))
    old_revision = CatalogDocumentRevision(
        id=str(uuid.uuid4()),
        document_id=model_revision.document_id,
        kind="model",
        publisher=model_revision.publisher,
        slug=model_revision.slug,
        revision_number=2,
        schema_version=2,
        state="candidate",
        document=old_document,
        content_digest=old_digest,
        projected={},
        created_by="test",
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    node_id = "spk_" + "a" * 32
    now = datetime(2026, 9, 7, tzinfo=UTC)
    recipe_digest = content_sha256(
        RecipeDefinition.model_validate(recipe_revision.document)
    )
    with sessions.begin() as session:
        session.add(old_revision)
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.add(
            ModelCacheSet(
                artifact_set_sha256="b" * 64,
                schema_version=2,
                model_content_sha256=old_digest,
                recipe_revision_sha256=recipe_digest,
                manifest={"schema_version": 2},
                expected_bytes=10,
                verified_bytes=10,
                state="cached",
                protected=False,
                protected_reasons=[],
                created_at=now,
                updated_at=now,
                last_accessed_at=now,
            )
        )
        build_id = str(uuid.uuid4())
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id=recipe_revision.id,
                builder_node_id=node_id,
                source_bundle_sha256="c" * 64,
                build_input_sha256="d" * 64,
                state="succeeded",
                policy_report={"state": "passed"},
                plan={"schema_version": 2},
                image_digest="sha256:" + "e" * 64,
                image_bytes=1,
                created_at=now,
                updated_at=now,
            )
        )
        mapping_id = str(uuid.uuid4())
        session.add(
            ClusterMapping(
                id=mapping_id,
                recipe_revision_id=recipe_revision.id,
                topology_name="single",
                generation=1,
                node_count=1,
                state="ready",
                parameters={},
                placement_digest="f" * 64,
                endpoint_owner_node_id=node_id,
                created_by="test",
                created_at=now,
                updated_at=now,
            )
        )
        installation_id = str(uuid.uuid4())
        session.add(
            RecipeInstallation(
                id=installation_id,
                recipe_revision_id=recipe_revision.id,
                model_content_sha256=old_digest,
                mapping_id=mapping_id,
                mapping_generation=1,
                recipe_build_id=build_id,
                image_digest="sha256:" + "e" * 64,
                plan_digest="1" * 64,
                plan={"schema_version": 2},
                state="installed",
                actor="test",
                created_at=now,
                updated_at=now,
            )
        )
        run_id = str(uuid.uuid4())
        session.add(
            RecipeRun(
                id=run_id,
                installation_id=installation_id,
                mapping_id=mapping_id,
                mapping_generation=1,
                run_generation=1,
                alias="local",
                plan_digest="2" * 64,
                plan={"schema_version": 2, "model_content_sha256": old_digest},
                state="running",
                route_state="published",
                actor="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            RunNode(
                run_id=run_id,
                node_id=node_id,
                rank=0,
                role="entrypoint",
                state="running",
                port=8888,
                reserved_memory_bytes=1,
                updated_at=now,
            )
        )

    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"q" * 32).cursor_codec(),
        clock=lambda: now,
        runtime_archive_available=lambda _digest, _size: False,
    )
    models = projection.models(local_only=True).models
    assert {item.identity.content_sha256 for item in models} == {old_digest}
    old = next(item for item in models if item.identity.content_sha256 == old_digest)
    assert old.local.controller == "cached"
    assert old.local.running_on == [node_id]
    recipes = projection.recipe_library().recipes
    assert len(recipes) == 1
    assert recipes[0].identity.content_sha256 == recipe_digest
    assert recipes[0].local.controller == "not_cached"
    assert recipes[0].local.running_on == [node_id]


def test_library_pagination_covers_more_than_one_page_without_gaps(
    tmp_path: Path,
) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    engine = create_engine(f"sqlite:///{tmp_path / 'library-pagination.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    _insert_canonical_rows(
        sessions,
        kind="model",
        template=index["catalog_entities"][0]["document"],
        count=513,
    )
    _insert_canonical_rows(
        sessions,
        kind="recipe",
        template=index["recipes"][0]["document"],
        count=513,
    )
    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"p" * 32).cursor_codec(),
        clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )

    model_pages = []
    cursor = None
    while True:
        page = projection.models(limit=512, cursor=cursor)
        model_pages.extend(page.models)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    recipe_pages = []
    cursor = None
    while True:
        page = projection.recipe_library(limit=512, cursor=cursor, all_models=True)
        recipe_pages.extend(page.recipes)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    model_digests = [item.identity.content_sha256 for item in model_pages]
    recipe_digests = [item.identity.content_sha256 for item in recipe_pages]
    assert len(model_digests) == 513
    assert len(recipe_digests) == 513
    assert len(set(model_digests)) == 513
    assert len(set(recipe_digests)) == 513

    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model"
            )
        )
        assert revision is not None
        revision_id = revision.id
    with engine.begin() as connection:
        head_table = CatalogDocumentRevision.__table__
        assert isinstance(head_table, Table)
        connection.execute(
            head_table.update()
            .where(CatalogDocumentRevision.id == revision_id)
            .values(document={"kind": "model"})
        )
    with pytest.raises(LibraryProjectionError):
        projection.models(limit=1)


@pytest.mark.parametrize("total_bytes,expected_status", [(0, 200), (-1, 503)])
def test_cached_download_progress_preserves_zero_and_rejects_negative_totals(
    tmp_path: Path,
    total_bytes: int,
    expected_status: int,
) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    engine = create_engine(f"sqlite:///{tmp_path / 'cache-progress.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 9, 13, tzinfo=UTC)
    cursors = TokenCodec(b"z" * 32).cursor_codec()
    entities = CatalogEntityService(sessions, clock=lambda: now, cursors=cursors)
    revision = entities.create_draft(
        index["catalog_entities"][0]["document"], actor="test"
    )
    entities.resolve(revision.id, actor="test")
    with sessions.begin() as session:
        session.add(
            ModelCacheOperation(
                request_key=str(uuid.uuid4()),
                kind="download",
                state="succeeded",
                payload={"model_content_sha256": revision.content_digest},
                progress={
                    "measurement": {
                        "phase": "completed",
                        "completed_bytes": 0,
                        "total_bytes": total_bytes,
                    }
                },
                actor="test",
                created_at=now,
                updated_at=now,
            )
        )
    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=Depends(lambda: Actor("test", "viewer")),
        projection=LibraryProjection(sessions, cursors=cursors, clock=lambda: now),
    )
    with TestClient(app) as client:
        response = client.get("/api/model/library")
    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        progress = response.json()["models"][0]["local"]["preparation"]
        assert progress["state"] == "succeeded"
        assert progress["total_bytes"] == 0


@pytest.mark.parametrize("kind", ["model", "recipe"])
def test_library_cursor_refuses_a_changed_accepted_catalog(tmp_path: Path, kind: str):
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    engine = create_engine(f"sqlite:///{tmp_path / 'changing-library.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    template = index["catalog_entities" if kind == "model" else "recipes"][0][
        "document"
    ]
    _insert_canonical_rows(sessions, kind=kind, template=template, count=2)
    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        clock=lambda: datetime(2026, 9, 6, tzinfo=UTC),
    )

    def read(cursor: str | None = None):
        if kind == "model":
            return projection.models(limit=1, sort="name", cursor=cursor)
        return projection.recipe_library(
            limit=1, sort="name", cursor=cursor, all_models=True
        )

    first = read()
    assert first.next_cursor is not None
    # A later-page accepted revision is replaced during the scan. An old
    # boundary alone would silently mix the old and new library in one choice.
    with sessions.begin() as session:
        old = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == kind,
                CatalogDocumentRevision.slug == f"{kind}-0001",
            )
        )
        assert old is not None
        document = copy.deepcopy(old.document)
        metadata = _document_section(document, "metadata")
        metadata["description"] = "Changed after the first page"
        canonical_type = ModelDefinition if kind == "model" else RecipeDefinition
        canonical = canonical_type.model_validate(document)
        new = CatalogDocumentRevision(
            id=str(uuid.uuid4()),
            document_id=old.document_id,
            kind=kind,
            publisher=old.publisher,
            slug=old.slug,
            revision_number=2,
            state="active",
            document=canonical.model_dump(mode="json"),
            content_digest=content_sha256(canonical),
            schema_version=2,
            projected={},
            created_by="test",
            created_at=old.created_at,
        )
        session.add(new)
        head = session.scalar(
            select(CatalogDocumentHead).where(
                CatalogDocumentHead.kind == kind,
                CatalogDocumentHead.publisher == old.publisher,
                CatalogDocumentHead.slug == old.slug,
            )
        )
        assert head is not None
        head.active_revision_id = new.id
    with pytest.raises(CursorError):
        read(first.next_cursor)


def test_model_detail_resolves_every_model_cache_selector_form(tmp_path: Path) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    template = index["catalog_entities"][0]["document"]
    engine = create_engine(f"sqlite:///{tmp_path / 'model-selectors.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    _insert_canonical_rows(sessions, kind="model", template=template, count=1)
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "model"
            )
        )
        assert revision is not None
        selectors = (
            "test/model-0000",
            "model-0000",
            revision.id,
            revision.document_id,
            revision.content_digest,
        )
        expected_digest = revision.content_digest

    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=Depends(lambda: Actor("test", "viewer")),
        projection=LibraryProjection(
            sessions,
            cursors=TokenCodec(b"m" * 32).cursor_codec(),
        ),
    )
    with TestClient(app) as client:
        for selector in selectors:
            response = client.get(f"/api/model/{selector}")
            assert response.status_code == 200, response.text
            assert response.json()["identity"]["content_sha256"] == expected_digest


def test_recipe_library_pages_by_wire_bytes_without_changing_cursor_limit(
    tmp_path: Path,
) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    template = index["recipes"][0]["document"]
    engine = create_engine(f"sqlite:///{tmp_path / 'large-recipe-library.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    count = 300
    _insert_canonical_rows(
        sessions,
        kind="recipe",
        template=template,
        count=count,
        # Multibyte canonical text ensures the limit is a wire-byte budget, not
        # a character count or a fixed row count.
        description="é" * 4000,
    )
    cursors = TokenCodec(b"b" * 32).cursor_codec()
    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=Depends(lambda: Actor("test", "viewer")),
        projection=LibraryProjection(sessions, cursors=cursors),
    )

    collected: list[str] = []
    cursor: str | None = None
    with TestClient(app) as client:
        while True:
            params: dict[str, str | int | bool] = {
                "all_models": True,
                "limit": 512,
                "sort": "name",
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get("/api/recipe/library", params=params)
            assert response.status_code == 200, response.text
            assert len(response.content) <= MAX_CONTROL_DOCUMENT_BYTES
            payload = response.json()
            assert payload["filters"]["sort"] == "name"
            page = payload["recipes"]
            collected.extend(item["selector"] for item in page)
            next_cursor = payload["next_cursor"]
            if cursor is None:
                assert 0 < len(page) < count
                assert next_cursor is not None
                # The selected page is the largest contiguous prefix that
                # fits: adding the next real row with an equal-size name
                # cursor would cross the exact serialized response budget.
                next_page = client.get(
                    "/api/recipe/library",
                    params={
                        "all_models": True,
                        "limit": 512,
                        "sort": "name",
                        "cursor": next_cursor,
                    },
                )
                assert next_page.status_code == 200, next_page.text
                next_payload = next_page.json()
                assert next_payload["recipes"]
                assert next_payload["next_cursor"] is not None
                assert len(next_payload["next_cursor"]) == len(next_cursor)
                assert response.content == json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                over_budget_payload = dict(payload)
                over_budget_payload["recipes"] = [
                    *page,
                    next_payload["recipes"][0],
                ]
                over_budget_payload["next_cursor"] = next_payload["next_cursor"]
                assert (
                    len(
                        json.dumps(
                            over_budget_payload,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                    > MAX_CONTROL_DOCUMENT_BYTES
                )
                changed_limit = client.get(
                    "/api/recipe/library",
                    params={
                        "all_models": True,
                        "limit": 511,
                        "sort": "name",
                        "cursor": next_cursor,
                    },
                )
                assert changed_limit.status_code == 422
            if next_cursor is None:
                break
            cursor = next_cursor

    assert collected == [f"test/recipe-{index:04d}" for index in range(count)]


def test_model_library_pages_by_wire_bytes_without_losing_entries(
    tmp_path: Path,
) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    template = index["catalog_entities"][0]["document"]
    engine = create_engine(f"sqlite:///{tmp_path / 'large-model-library.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    count = 160
    _insert_canonical_rows(
        sessions,
        kind="model",
        template=template,
        count=count,
        description="é" * 4000,
    )
    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=Depends(lambda: Actor("test", "viewer")),
        projection=LibraryProjection(
            sessions, cursors=TokenCodec(b"n" * 32).cursor_codec()
        ),
    )

    collected: list[str] = []
    cursor: str | None = None
    with TestClient(app) as client:
        while True:
            params: dict[str, str | int | bool] = {
                "limit": 512,
                "sort": "name",
            }
            if cursor is not None:
                params["cursor"] = cursor
            response = client.get("/api/model/library", params=params)
            assert response.status_code == 200, response.text
            assert len(response.content) <= MAX_CONTROL_DOCUMENT_BYTES
            payload = response.json()
            collected.extend(item["selector"] for item in payload["models"])
            cursor = payload["next_cursor"]
            if cursor is None:
                break

    assert collected == [f"test/model-{index:04d}" for index in range(count)]


def test_library_item_at_one_byte_over_wire_budget_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    template = index["recipes"][0]["document"]
    engine = create_engine(f"sqlite:///{tmp_path / 'exact-budget.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    _insert_canonical_rows(sessions, kind="recipe", template=template, count=1)
    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=Depends(lambda: Actor("test", "viewer")),
        projection=LibraryProjection(
            sessions,
            cursors=TokenCodec(b"p" * 32).cursor_codec(),
        ),
    )

    with TestClient(app) as client:
        fitting = client.get("/api/recipe/library", params={"all_models": True})
        assert fitting.status_code == 200, fitting.text
        exact_wire_bytes = len(fitting.content)
        assert exact_wire_bytes <= MAX_CONTROL_DOCUMENT_BYTES

        # This cap is one byte below the observed, fully serialized response.
        # An envelope-bracket undercount of two bytes would incorrectly accept
        # and return a response larger than this configured budget.
        monkeypatch.setattr(
            "vonk_control.library_projection.MAX_CONTROL_DOCUMENT_BYTES",
            exact_wire_bytes - 1,
        )
        over_budget = client.get("/api/recipe/library", params={"all_models": True})

    assert over_budget.status_code == 422
    detail = over_budget.json()["detail"]
    assert isinstance(detail, str)
    observed = int(detail.split("requires ", 1)[1].split(" bytes", 1)[0])
    assert observed == exact_wire_bytes


def test_recipe_library_refuses_one_item_larger_than_wire_budget_actionably(
    tmp_path: Path,
) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    template = index["recipes"][0]["document"]
    engine = create_engine(f"sqlite:///{tmp_path / 'oversized-recipe-library.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    arguments = [
        {"name": f"large-{index}", "setting": None, "value": "x" * 65_400}
        for index in range(16)
    ]
    _insert_canonical_rows(
        sessions,
        kind="recipe",
        template=template,
        count=1,
        runtime_arguments=arguments,
    )
    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=Depends(lambda: Actor("test", "viewer")),
        projection=LibraryProjection(
            sessions, cursors=TokenCodec(b"o" * 32).cursor_codec()
        ),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/recipe/library", params={"all_models": True, "limit": 10}
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "narrow filters" in detail
    assert str(MAX_CONTROL_DOCUMENT_BYTES) in detail
    observed = int(detail.split("requires ", 1)[1].split(" bytes", 1)[0])
    assert observed > MAX_CONTROL_DOCUMENT_BYTES
