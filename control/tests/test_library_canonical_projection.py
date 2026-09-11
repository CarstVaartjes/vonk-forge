from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import Actor, TokenCodec
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.library_api import install_library_routes
from vonk_control.library_projection import LibraryProjection, LibraryProjectionError
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ModelCacheSet,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from tests.recipe_library_source import recipe_library_root

ROOT = recipe_library_root()


def _insert_canonical_rows(
    sessions: sessionmaker,
    *,
    kind: str,
    template: dict[str, object],
    count: int,
) -> None:
    rows: list[CatalogDocument | CatalogDocumentRevision] = []
    revisions: list[CatalogDocumentRevision] = []
    for index in range(count):
        document = copy.deepcopy(template)
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
    with sessions.begin() as session:
        session.add_all(rows)
        session.add_all(revisions)


def test_published_corpus_projects_all_models_and_exact_recipe_bindings(tmp_path: Path) -> None:
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
    model_page = projection.models(limit=100)
    assert len(model_page.models) == expected_model_count
    assert {
        model.identity.content_sha256 for model in model_page.models
    } == set(expected_models)
    recipe_page = projection.recipe_library(limit=100, all_models=True)
    assert len(recipe_page.recipes) == expected_recipe_count
    assert {
        item.identity.recipe_id for item in recipe_page.recipes
    } == recipe_ids
    for item in recipe_page.recipes:
        assert item.identity.recipe_revision_id == recipe_revision_ids[
            item.identity.content_sha256
        ]
        assert item.identity.recipe_revision_id not in {
            item.identity.recipe_id,
            item.identity.content_sha256,
        }

    # Creator is faceted and filterable on both nouns.
    assert set(model_page.facets.publisher) == {
        model.identity.publisher for model in model_page.models
    }
    assert set(recipe_page.facets.publisher) >= {
        item.identity.publisher for item in recipe_page.recipes
    }
    creator = min({model.identity.publisher for model in model_page.models})
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
    assert any("abliterated" in model.alignment for model in model_page.models)

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
    selectors = sorted({model.selector for model in model_page.models})
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
    response = client.get("/api/model/library")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["models"]) == expected_model_count
    assert {model["identity"]["kind"] for model in payload["models"]} == {"model"}
    assert {model["identity"]["content_sha256"] for model in payload["models"]} == set(expected_models)
    assert all("recipes" not in model and "source_kind" not in model for model in payload["models"])
    library_model_schema = app.openapi()["components"]["schemas"]["LibraryModelProjection"]
    assert library_model_schema["properties"]["local"]["$ref"].endswith("LibraryLocalState")
    recipe_response = client.get("/api/recipe/library", params={"all_models": True})
    assert recipe_response.status_code == 200
    recipe_payload = recipe_response.json()
    assert len(recipe_payload["recipes"]) == expected_recipe_count
    assert {recipe["identity"]["recipe_id"] for recipe in recipe_payload["recipes"]} == recipe_ids
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
    assert detail_payload["document"]["topology"] == expected_recipe.topology.model_dump(
        mode="json"
    )
    assert detail_payload["document"]["settings"] == expected_recipe.settings.model_dump(
        mode="json"
    )
    assert detail_payload["identity"]["recipe_revision_id"] == recipe_revision_ids[
        detail_payload["identity"]["content_sha256"]
    ]
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
    assert [entry["selection"]["files"] for entry in multi_model_payload["model_documents"]] == [
        selection.model_dump(mode="json")["files"]
        for selection in multi_model_recipe.models
    ]
    assert multi_model_payload["model_documents"][0]["selection"]["files"] != multi_model_payload[
        "model_documents"
    ][1]["selection"]["files"]


def test_database_local_projection_reads_cache_build_and_spark_evidence(
    tmp_path: Path,
) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    model_document = copy.deepcopy(index["catalog_entities"][0]["document"])
    recipe_document = copy.deepcopy(index["recipes"][0]["document"])
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
    old_document["identity"]["version"] = "0.0.1"
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
    recipe_digest = content_sha256(RecipeDefinition.model_validate(recipe_revision.document))
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
    )
    models = projection.models(local_only=True).models
    assert {item.identity.content_sha256 for item in models} == {old_digest}
    old = next(item for item in models if item.identity.content_sha256 == old_digest)
    assert old.local.controller == "cached"
    assert old.local.running_on == [node_id]
    recipes = projection.recipe_library().recipes
    assert len(recipes) == 1
    assert recipes[0].identity.content_sha256 == recipe_digest
    assert recipes[0].local.controller == "cached"


def test_library_pagination_covers_more_than_one_page_without_gaps(tmp_path: Path) -> None:
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
        connection.execute(
            CatalogDocumentRevision.__table__.update()
            .where(CatalogDocumentRevision.id == revision_id)
            .values(document={"kind": "model"})
        )
    with pytest.raises(LibraryProjectionError):
        projection.models(limit=1)
