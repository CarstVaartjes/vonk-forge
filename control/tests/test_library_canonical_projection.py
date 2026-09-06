from __future__ import annotations

import copy
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import Actor, TokenCodec
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.fleet_profile_contract import FleetProfileAssignmentInput
from vonk_control.library_api import install_library_routes
from vonk_control.library_projection import LibraryProjection, LibraryProjectionError
from vonk_control.models import Base, CatalogDocument, CatalogDocumentRevision
from vonk_control.run_switch_contract import RunSwitchPreviewRequest
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
    expected_model_keys = {
        (
            entry["document"]["identity"]["publisher"],
            entry["document"]["identity"]["slug"],
            entry["content_sha256"],
        )
        for entry in index["catalog_entities"]
    }
    expected_linked_model_keys = {
        (
            reference["model"]["publisher"],
            reference["model"]["slug"],
            reference["model"]["content_sha256"],
        )
        for row in index["recipes"]
        for reference in row["document"].get("models", [])
    }
    expected_linked_model_count = len(expected_model_keys & expected_linked_model_keys)
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
    recipe_ids_by_slug: dict[str, str] = {}
    recipe_revision_ids: dict[str, str] = {}
    for row in index["recipes"]:
        revision = entities.create_draft(row["document"], actor="test")
        entities.resolve(revision.id, actor="test")
        recipe_ids.add(revision.document_id)
        recipe_ids_by_slug[row["document"]["identity"]["slug"]] = revision.document_id
        recipe_revision_ids[row["content_sha256"]] = revision.id

    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"p" * 32).cursor_codec(),
        clock=clock,
    )
    snapshot = projection.list(limit=100)
    assert len(snapshot.models) == expected_model_count
    assert len(snapshot.unlinked_recipes) == 0
    assert sum(bool(model.recipes) for model in snapshot.models) == expected_linked_model_count
    assert sum(not model.recipes for model in snapshot.models) == (
        expected_model_count - expected_linked_model_count
    )
    assert {
        (
            model.model_document.identity.publisher,
            model.model_document.identity.slug,
            model.model.content_sha256,
        )
        for model in snapshot.models
    } == {
        (model.identity.publisher, model.identity.slug, digest)
        for digest, model in expected_models.items()
    }
    for model in snapshot.models:
        expected = expected_models[model.model.content_sha256]
        assert model.model_document == expected
        assert model.model_document.files == expected.files
        assert model.model_document.capabilities == expected.capabilities
    assert {
        recipe.recipe_id
        for model in snapshot.models
        for recipe in model.recipes
    } == recipe_ids
    recipe_overview = projection.recipes(limit=100)
    assert len(recipe_overview.recipes) == expected_recipe_count
    assert {
        item.recipe_document.identity.slug for item in recipe_overview.recipes
    } == {recipe.identity.slug for recipe in expected_recipes.values()}
    for item in recipe_overview.recipes:
        assert item.recipe_revision_id == recipe_revision_ids[item.content_sha256]
        assert item.recipe_revision_id not in {item.recipe_id, item.content_sha256}

    app = FastAPI()
    install_library_routes(
        app,
        actor_dependency=lambda: Actor("test", "viewer"),
        projection=projection,
    )
    client = TestClient(app)
    response = client.get("/api/v1/library")
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["models"]) == expected_model_count
    assert sum(bool(model["recipes"]) for model in payload["models"]) == expected_linked_model_count
    assert sum(not model["recipes"] for model in payload["models"]) == (
        expected_model_count - expected_linked_model_count
    )
    assert any(model["recipes"] == [] for model in payload["models"])
    assert {model["model"]["kind"] for model in payload["models"]} == {"model"}
    assert all(
        "source_kind" not in recipe
        and "selected_revision" not in recipe
        and "content_sha256" in recipe
        for model in payload["models"]
        for recipe in model["recipes"]
    )
    library_model_schema = app.openapi()["components"]["schemas"]["LibraryModel"]
    assert library_model_schema["properties"]["recipes"]["minItems"] == 0
    assert library_model_schema["properties"]["recipes"]["maxItems"] == 512
    recipe_response = client.get("/api/v1/library/recipes")
    assert recipe_response.status_code == 200
    recipe_payload = recipe_response.json()
    assert len(recipe_payload["recipes"]) == expected_recipe_count
    assert {recipe["recipe_id"] for recipe in recipe_payload["recipes"]} == recipe_ids
    first_recipe = recipe_payload["recipes"][0]
    first_expected = expected_recipes[first_recipe["content_sha256"]]
    assert first_recipe["recipe_document"] == first_expected.model_dump(mode="json")
    assert first_recipe["recipe_document"]["runtime"]["engine"] == (
        first_expected.runtime.engine
    )
    assert first_recipe["recipe_document"]["release"]["version"] == (
        first_expected.release.version
    )
    assert first_recipe["recipe_document"]["topology"]["node_count"] == (
        first_expected.topology.node_count
    )
    assert first_recipe["recipe_document"]["topology"]["roles"][0]["resources"] == (
        first_expected.topology.roles[0].resources.model_dump(mode="json")
    )
    assert first_recipe["recipe_document"]["models"] == [
        selection.model_dump(mode="json") for selection in first_expected.models
    ]
    recipe_id = payload["models"][0]["recipes"][0]["recipe_id"]
    detail = client.get(f"/api/v1/library/recipes/{recipe_id}")
    assert detail.status_code == 200
    assert detail.json()["recipe"]["recipe_id"] == recipe_id
    detail_payload = detail.json()
    assert detail_payload["definition"]["execution"]
    assert detail_payload["definition"]["models"]
    assert len(detail_payload["recipe"]["content_sha256"]) == 64
    assert "source_kind" not in detail_payload["recipe"]
    assert "selected_revision" not in detail_payload
    assert "visual_recipe" not in detail_payload
    assert "VisualRecipeDocument" not in app.openapi()["components"]["schemas"]
    expected_recipe = expected_recipes[detail_payload["recipe"]["content_sha256"]]
    assert detail_payload["definition"] == expected_recipe.model_dump(mode="json")
    assert detail_payload["definition"]["runtime"] == expected_recipe.runtime.model_dump(
        mode="json"
    )
    assert detail_payload["definition"]["topology"] == expected_recipe.topology.model_dump(
        mode="json"
    )
    assert detail_payload["definition"]["settings"] == expected_recipe.settings.model_dump(
        mode="json"
    )
    assert detail_payload["recipe"]["recipe_revision_id"] == recipe_revision_ids[
        detail_payload["recipe"]["content_sha256"]
    ]
    assert detail_payload["recipe"]["recipe_revision_id"] not in {
        detail_payload["recipe"]["recipe_id"],
        detail_payload["recipe"]["content_sha256"],
    }
    profile_assignment = FleetProfileAssignmentInput.model_validate(
        {
            "recipe_revision_id": detail_payload["recipe"]["recipe_revision_id"],
            "topology_name": expected_recipe.topology.name,
            "desired_state": "installed",
            "nodes": [
                {
                    "node_id": "spk_" + "0" * 32,
                    "rank": 0,
                    "role": expected_recipe.topology.roles[0].name,
                    "endpoint_owner": True,
                }
            ],
        }
    )
    run_input = RunSwitchPreviewRequest.model_validate(
        {
            "model_version_sha256": "a" * 64,
            "recipe_revision_id": detail_payload["recipe"]["recipe_revision_id"],
            "spark_group": {
                "nodes": [
                    {
                        "node_id": "spk_" + "0" * 32,
                        "rank": 0,
                        "role": expected_recipe.topology.roles[0].name,
                        "endpoint_owner": True,
                    }
                ]
            },
            "alias": "canonical",
        }
    )
    assert profile_assignment.recipe_revision_id == detail_payload["recipe"][
        "recipe_revision_id"
    ]
    assert run_input.recipe_revision_id == detail_payload["recipe"]["recipe_revision_id"]

    multi_model_row = next(
        row for row in index["recipes"] if len(row["document"].get("models", [])) > 1
    )
    multi_model_recipe = RecipeDefinition.model_validate(multi_model_row["document"])
    multi_model_detail = client.get(
        "/api/v1/library/recipes/"
        + recipe_ids_by_slug[multi_model_recipe.identity.slug]
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
        page = projection.list(limit=512, cursor=cursor)
        model_pages.extend(page.models)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    recipe_pages = []
    cursor = None
    while True:
        page = projection.recipes(limit=512, cursor=cursor)
        recipe_pages.extend(page.recipes)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    model_digests = [item.model.content_sha256 for item in model_pages]
    recipe_digests = [item.content_sha256 for item in recipe_pages]
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
        projection.list(limit=1)
