from __future__ import annotations

import json
from datetime import UTC, datetime
from importlib.resources import files

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.fleet_profile_contract import (
    FleetProfileAssignmentInput,
    FleetProfileInput,
)
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import (
    AgentNode,
    AgentNodeProfile,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

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
                AgentNodeProfile(
                    node_id=NODE_1,
                    display_name="Spark One",
                    hostname="spark-one",
                ),
                AgentNodeProfile(
                    node_id=NODE_2,
                    display_name="Spark Two",
                    hostname="spark-two",
                ),
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
                "assignments": [
                    {
                        "recipe_selector": "synthetic-tiny-image",
                        "spark_ids": [NODE_1],
                        "desired_state": "running",
                        "assignment_name": "canonical",
                    }
                ],
            }
        ),
        actor="test",
    )

    assert profile.number == 1
    assert profile.revision == 1
    assert profile.assignments[0].recipe_id == RECIPE_DOCUMENT_ID
    assert profile.assignments[0].recipe_selector == "synthetic-tiny-image"
    assert profile.assignments[0].spark_ids == [NODE_1]
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
            assignments=[],
        ),
        actor="test",
    )
    preview = service.preview(profile.id)
    assert preview.allowed
    assert preview.assignments == []
    assert preview.preparations == []
    assert preview.scope.idle_node_ids == [NODE_1, NODE_2]


def test_profile_authoring_accepts_incomplete_group_without_revision_or_scope() -> None:
    value = FleetProfileInput.model_validate(
        {
            "name": "Draft",
            "assignments": [
                {
                    "recipe_selector": "vision-dual",
                    "spark_ids": [NODE_1],
                    "model_variant": "fp8",
                }
            ],
        }
    )
    document = value.model_dump(mode="json")
    assert document["assignments"] == [
        {
            "recipe_selector": "vision-dual",
            "spark_ids": [NODE_1],
            "assignment_name": None,
            "model_variant": "fp8",
            "desired_state": "running",
        }
    ]
    assert "scope" not in document
    assert "recipe_revision_id" not in document["assignments"][0]


def test_numbered_autosave_uses_revision_and_load_freezes_whole_roster() -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = service.update_number(
        2,
        FleetProfileInput(name="Second", assignments=[]),
        actor="test",
    )
    assert created.number == 2
    assert created.fleet == [
        {"selector": NODE_1, "display_name": "Spark One", "state": "Idle"},
        {"selector": NODE_2, "display_name": "Spark Two", "state": "Idle"},
    ]

    changed = service.update_number(
        2,
        FleetProfileInput(name="Second", expected_revision=1, assignments=[]),
        actor="test",
    )
    assert changed.revision == 2
    with pytest.raises(Exception, match="revision conflict"):
        service.update_number(
            2,
            FleetProfileInput(name="Stale", expected_revision=1, assignments=[]),
            actor="other",
        )

    application = service.load(2, actor="test", request_key="00000000-0000-4000-8000-000000000099")
    assert application.state == "succeeded"
    assert application.progress.intended_profile is not None
    assert application.progress.intended_profile.scope.node_ids == [NODE_1, NODE_2]


def test_profile_read_uses_the_read_only_latest_cache_resolver() -> None:
    sessions = _sessions()
    _seed(sessions)
    newer_revision_id = "00000000-0000-4000-8000-000000000022"
    with sessions.begin() as session:
        current = session.get(CatalogDocumentRevision, RECIPE_REVISION_ID)
        assert current is not None
        session.add(
            CatalogDocumentRevision(
                id=newer_revision_id,
                document_id=RECIPE_DOCUMENT_ID,
                kind="recipe",
                publisher=current.publisher,
                slug=current.slug,
                revision_number=2,
                schema_version=2,
                state="active",
                document=current.document,
                content_digest="d" * 64,
                execution_key="c" * 64,
                created_by="test",
                created_at=datetime(2026, 9, 6, tzinfo=UTC),
            )
        )
    calls: list[dict[str, object]] = []

    def resolve(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "schema_version": 2,
            "recipe": {"recipe_revision_id": RECIPE_REVISION_ID, "cached": True},
            "model": {"cached": True, "variant": "fp16"},
            "resources": {"per_spark_memory_bytes": 10, "additional_disk_bytes": 20},
            "blockers": [],
        }

    service = FleetProfileService(sessions, clock=lambda: NOW, cache_resolver=resolve)
    profile = service.create(
        FleetProfileInput(
            name="Cached",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="synthetic-tiny-image",
                    spark_ids=[NODE_1],
                    model_variant="fp16",
                )
            ],
        ),
        actor="test",
    )
    assert calls == [{"recipe_identity": RECIPE_DOCUMENT_ID, "model_variant": "fp16"}]
    assert profile.assignments[0].recipe["state"] == "Cached"
    assert profile.assignments[0].model["state"] == "Cached"
    preview = service.preview(profile.id)
    assert preview.assignments[0].recipe_revision_id == RECIPE_REVISION_ID
    application = service.load(
        profile.number,
        actor="test",
        request_key="00000000-0000-4000-8000-000000000097",
    )
    assert application.progress.intended_profile is not None
    assert (
        application.progress.intended_profile.assignments[0].recipe_revision_id
        == RECIPE_REVISION_ID
    )
