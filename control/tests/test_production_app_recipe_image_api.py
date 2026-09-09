from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from importlib.resources import files

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from vonk_control import api, availability_production, route_runtime
from vonk_control.api import production_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.models import Base, CatalogDocument, CatalogDocumentRevision
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256


def test_production_app_recipe_download_auth_and_status(
    tmp_path, monkeypatch, postgres_engine
) -> None:
    Base.metadata.create_all(postgres_engine)
    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-image.json")
            .read_text()
        )
    )
    model = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    model_definition = ModelDefinition.model_validate(model)
    model_digest = content_sha256(model_definition)
    assert recipe.models[0].model.content_sha256 == model_digest
    now = datetime.now(UTC)
    sessions = sessionmaker(bind=postgres_engine)
    with sessions.begin() as session:
        session.add_all(
            [
                CatalogDocument(
                    id="production-model-document",
                    kind="model",
                    publisher="vonk-forge",
                    slug="synthetic-tiny-fp16",
                    title="Synthetic Tiny FP16",
                    created_by="test",
                    created_at=now,
                    updated_at=now,
                ),
                CatalogDocument(
                    id="production-recipe-document",
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    title="Synthetic Tiny Image",
                    created_by="test",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.add_all(
            [
                    CatalogDocumentRevision(
                        id="production-model-revision",
                        document_id="production-model-document",
                        kind="model",
                        publisher="vonk-forge",
                        slug="synthetic-tiny-fp16",
                        revision_number=1,
                        schema_version=2,
                        state="active",
                        document=model_definition.model_dump(mode="json"),
                        content_digest=model_digest,
                        artifact_key="b" * 64,
                        projected={},
                        created_by="test",
                        created_at=now,
                    ),
                    CatalogDocumentRevision(
                        id="production-recipe-revision",
                        document_id="production-recipe-document",
                        kind="recipe",
                        publisher=recipe.identity.publisher,
                        slug=recipe.identity.slug,
                        revision_number=1,
                        schema_version=2,
                        state="active",
                        document=recipe.model_dump(mode="json"),
                        content_digest=content_sha256(recipe),
                        artifact_key="c" * 64,
                        execution_key="a" * 64,
                        projected={},
                        created_by="test",
                        created_at=now,
                    ),
            ]
        )

    database_url = postgres_engine.url.render_as_string(hide_password=False)
    signing_key = tmp_path / "token-signing-key"
    signing_key.write_bytes(b"production-api-test-signing-key-32-bytes")
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("VONK_DISTRIBUTED_START_TIMEOUT_SECONDS", "1800")
    monkeypatch.setenv("VONK_AGENT_RUNTIME", "disabled")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "127.0.0.1/32")
    monkeypatch.setenv("VONK_DATABASE_URL", database_url)
    monkeypatch.setenv("VONK_TOKEN_SIGNING_KEY_FILE", str(signing_key))
    monkeypatch.setenv("VONK_STATE_PATH", str(tmp_path / "state"))
    monkeypatch.setenv("VONK_AGENT_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("VONK_MODEL_CACHE_ROOT", str(tmp_path / "model-cache"))
    monkeypatch.setenv("VONK_WORKLOAD_TUF_METADATA_ROOT", str(tmp_path / "tuf-meta"))
    monkeypatch.setenv("VONK_WORKLOAD_TUF_TARGET_ROOT", str(tmp_path / "tuf-targets"))

    publisher = route_runtime.AtomicRouteBundlePublisher
    acknowledger = route_runtime.FileSupervisorAcknowledger

    class TemporaryPublisher(publisher):
        def __init__(self, _root, **kwargs):
            super().__init__(tmp_path / "routes", **kwargs)

    class TemporaryAcknowledger(acknowledger):
        def __init__(self, _path, **kwargs):
            super().__init__(tmp_path / "supervisor-ack.json", **kwargs)

    monkeypatch.setattr(route_runtime, "AtomicRouteBundlePublisher", TemporaryPublisher)
    monkeypatch.setattr(route_runtime, "FileSupervisorAcknowledger", TemporaryAcknowledger)

    # The auth/status route proof uses the existing synthetic image fixture;
    # model-child completion is covered by dedicated model-cache composition
    # tests with a canonical artifact source.
    original_builder = availability_production.build_recipe_image_availability
    def image_only_builder(*args, **kwargs):
        kwargs["model_cache"] = None
        return original_builder(*args, **kwargs)
    monkeypatch.setattr(availability_production, "build_recipe_image_availability", image_only_builder)

    lifecycles = []
    original_lifecycle = api.RecipeOperationService

    class CapturedLifecycle(original_lifecycle):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            lifecycles.append(self)

    monkeypatch.setattr(api, "RecipeOperationService", CapturedLifecycle)
    app = production_app()
    assert [item._distributed_start_timeout_seconds for item in lifecycles] == [1800]
    codec = TokenCodec(signing_key.read_bytes())
    operator = codec.issue(Actor("operator", "operator"), ttl_seconds=3600, now=int(time.time()))
    viewer = codec.issue(Actor("viewer", "viewer"), ttl_seconds=3600, now=int(time.time()))
    headers = {"Authorization": f"Bearer {operator}"}
    with TestClient(app) as client:
        selector = f"{recipe.identity.publisher}/{recipe.identity.slug}"
        denied = client.post(
            f"/api/recipe/{selector}/download",
            headers={"Authorization": f"Bearer {viewer}"},
            json={
                "request_key": "00000000-0000-4000-8000-000000000001",
            },
        )
        assert denied.status_code == 403

        started = client.post(
            f"/api/recipe/{selector}/download",
            headers=headers,
            json={
                "request_key": "00000000-0000-4000-8000-000000000002",
            },
        )
        assert started.status_code == 202, started.text
        assert started.json()["recipe_revision_id"] == "production-recipe-revision"
