from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import Actor, TokenCodec
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.library_api import install_library_routes
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import Base

ROOT = Path(
    os.environ.get(
        "VONK_LIBRARY_CORPUS_ROOT",
        "/private/tmp/vonk-forge-recipes-qwen38-vllm-main57",
    )
)


def test_published_corpus_projects_all_models_and_exact_recipe_bindings(tmp_path: Path) -> None:
    index = json.loads((ROOT / "catalog-index.json").read_text(encoding="utf-8"))
    engine = create_engine(f"sqlite:///{tmp_path / 'library.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 9, 5, tzinfo=UTC)
    entities = CatalogEntityService(
        sessions, clock=clock, cursors=TokenCodec(b"l" * 32).cursor_codec()
    )

    model_documents = [entry["document"] for entry in index["catalog_entities"]]
    for document in model_documents:
        revision = entities.create_draft(document, actor="test")
        entities.resolve(revision.id, actor="test")
    recipe_ids: set[str] = set()
    for row in index["recipes"]:
        revision = entities.create_draft(row["document"], actor="test")
        entities.resolve(revision.id, actor="test")
        recipe_ids.add(revision.document_id)

    projection = LibraryProjection(
        sessions,
        cursors=TokenCodec(b"p" * 32).cursor_codec(),
        clock=clock,
    )
    snapshot = projection.list(limit=100)
    assert len(snapshot.models) == 92
    assert len(snapshot.unlinked_recipes) == 0
    assert sum(bool(model.recipes) for model in snapshot.models) == 79
    assert sum(not model.recipes for model in snapshot.models) == 13
    assert {
        recipe.recipe_id
        for model in snapshot.models
        for recipe in model.recipes
    } == recipe_ids
    recipe_overview = projection.recipes(limit=100)
    assert len(recipe_overview.recipes) == 85

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
    assert len(payload["models"]) == 92
    assert sum(bool(model["recipes"]) for model in payload["models"]) == 79
    assert sum(not model["recipes"] for model in payload["models"]) == 13
    assert any(model["recipes"] == [] for model in payload["models"])
    library_model_schema = app.openapi()["components"]["schemas"]["LibraryModel"]
    assert "minItems" not in library_model_schema["properties"]["recipes"]
    recipe_response = client.get("/api/v1/library/recipes")
    assert recipe_response.status_code == 200
    assert len(recipe_response.json()["recipes"]) == 85
    recipe_id = payload["models"][0]["recipes"][0]["recipe_id"]
    detail = client.get(f"/api/v1/library/recipes/{recipe_id}")
    assert detail.status_code == 200
    assert detail.json()["recipe"]["recipe_id"] == recipe_id
