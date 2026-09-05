from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib.resources import files

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import AgentNode, Base, CatalogDocument, CatalogDocumentRevision


NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
NODE_1 = "spk_" + "1" * 32
NODE_2 = "spk_" + "2" * 32
MODEL_DOCUMENT_ID = "00000000-0000-4000-8000-000000000010"
MODEL_REVISION_ID = "00000000-0000-4000-8000-000000000011"
RECIPE_DOCUMENT_ID = "00000000-0000-4000-8000-000000000020"
RECIPE_REVISION_ID = "00000000-0000-4000-8000-000000000021"


def _sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed(sessions: sessionmaker) -> None:
    model_document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    recipe_document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-image.json")
        .read_text(encoding="utf-8")
    )
    model = ModelDefinition.model_validate(model_document)
    recipe = RecipeDefinition.model_validate(recipe_document)
    model_digest = content_sha256(model)
    assert recipe.models[0].model.content_sha256 == model_digest
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=node_id,
                    state="active",
                    protocol_version=2,
                    architecture="linux-arm64",
                    capabilities=[],
                    last_seen_at=NOW,
                )
                for node_id in (NODE_1, NODE_2)
            ]
        )
        session.add_all(
            [
                CatalogDocument(
                    id=MODEL_DOCUMENT_ID,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    title=model.identity.model.title,
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CatalogDocument(
                    id=RECIPE_DOCUMENT_ID,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    title=recipe.metadata.title,
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.add_all(
            [
                CatalogDocumentRevision(
                    id=MODEL_REVISION_ID,
                    document_id=MODEL_DOCUMENT_ID,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=model.model_dump(mode="json"),
                    content_digest=model_digest,
                    artifact_key="a" * 64,
                    created_by="test",
                    created_at=NOW,
                ),
                CatalogDocumentRevision(
                    id=RECIPE_REVISION_ID,
                    document_id=RECIPE_DOCUMENT_ID,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=recipe.model_dump(mode="json"),
                    content_digest=content_sha256(recipe),
                    execution_key="b" * 64,
                    created_by="test",
                    created_at=NOW,
                ),
            ]
        )


def test_profile_uses_canonical_recipe_and_model_revisions() -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Canonical idle",
                "scope": {"node_ids": [NODE_1, NODE_2]},
                "assignments": [
                    {
                        "recipe_revision_id": RECIPE_REVISION_ID,
                        "topology_name": "solo",
                        "desired_state": "running",
                        "alias": "canonical",
                        "nodes": [
                            {
                                "node_id": NODE_1,
                                "rank": 0,
                                "role": "entrypoint",
                                "endpoint_owner": True,
                            }
                        ],
                    }
                ],
            }
        ),
        actor="test",
    )

    assert profile.assignments[0].recipe_id == RECIPE_DOCUMENT_ID
    assert profile.assignments[0].recipe_title == "Synthetic Tiny image"
    assert profile.assignments[0].model_title == "Synthetic Tiny"
    preview = service.preview(profile.id)
    assert preview.scope.node_ids == [NODE_1, NODE_2]
    assert preview.scope.idle_node_ids == [NODE_2]
    assert preview.assignments[0].recipe_revision_id == RECIPE_REVISION_ID


def test_all_idle_canonical_profile_previews_without_assignments() -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(
        FleetProfileInput(
            name="All idle",
            scope={"node_ids": [NODE_1, NODE_2]},
            assignments=[],
        ),
        actor="test",
    )
    preview = service.preview(profile.id)
    assert preview.allowed
    assert preview.assignments == []
    assert preview.preparations == []
    assert preview.scope.idle_node_ids == [NODE_1, NODE_2]
