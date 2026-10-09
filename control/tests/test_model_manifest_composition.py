from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.artifact_reference_scan import model_set_objects
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.compiled_execution_plan import (
    VerifiedRuntimeImage,
    compile_verified_execution_plan,
)
from vonk_control.distribution import ModelCacheObjectSource
from vonk_control.execution_plan_service import _bind_runtime_artifacts
from vonk_control.model_cache import ArtifactSetManifest, ModelCacheService
from vonk_control.model_cache_contract import ModelCacheDownloadPayload
from vonk_control.models import Base, Job, ModelCacheOperation
from vonk_control.recipe_runtime_specs import compile_runtime_spec
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

from .test_recipe_image_availability import _runtime, _service


@pytest.mark.parametrize("same_source", [False, True])
def test_shared_catalog_files_resume_the_same_parent_after_producer_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_source: bool
) -> None:
    """Catch duplicate keys, discarded provenance, and charging shared bytes twice."""
    now = datetime(2026, 10, 7, tzinfo=UTC)
    payloads = {"shared": b"shared", "target": b"target", "assistant": b"assistant"}
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        name = request.url.path.rsplit("/", 1)[-1].removesuffix(".bin")
        return httpx2.Response(200, request=request, content=payloads[name])

    models: list[ModelDefinition] = []
    for role in ("target", "assistant"):
        raw = json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "model-definition.json")
            .read_text()
        )
        raw["identity"]["slug"] = role
        raw["source"] = {
            "repository": "https://huggingface.co/models/"
            + ("shared-source" if same_source else role),
            "revision": "0" * 40,
        }
        raw["files"] = [
            {
                "id": name,
                "path": f"{name}.bin",
                "roles": ["tokenizer" if name == "shared" else "weights"],
                "sha256": hashlib.sha256(payloads[name]).hexdigest(),
                "size_bytes": len(payloads[name]),
            }
            for name in ("shared", role)
        ]
        models.append(ModelDefinition.model_validate(raw))

    raw_recipe = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text()
    )
    raw_recipe["models"] = [
        {
            "id": role,
            "model": {
                "kind": "model",
                "publisher": model.identity.publisher,
                "slug": model.identity.slug,
                "content_sha256": document_sha256(model.model_dump(mode="json")),
            },
            "files": [
                {
                    "id": f"{role}-{name}",
                    "file_id": name,
                    "roles": ["entrypoint"],
                    "mount": {"target": f"/models/{role}"},
                }
                for name in ("shared", role)
            ],
        }
        for role, model in zip(("target", "assistant"), models, strict=True)
    ]
    recipe = RecipeDefinition.model_validate(raw_recipe)
    recipe_digest = document_sha256(recipe.model_dump(mode="json"))
    engine = create_engine(
        f"sqlite:///{tmp_path / 'composition.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions, clock=lambda: now, cursors=TokenCodec(b"c" * 32).cursor_codec()
    )
    revision = catalog.import_recipe_library(
        "test",
        library_commit="0" * 40,
        source_path="recipes/composition.json",
        document=recipe.model_dump(mode="json"),
        expected_content_sha256=recipe_digest,
        dependency_documents=[model.model_dump(mode="json") for model in models],
        source_bundle_sha256="c" * 64,
    )
    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    cache_root = tmp_path / "cache"

    def new_cache() -> ModelCacheService:
        return ModelCacheService(
            sessions, cache_root, reserve_bytes=0, http_client=client, clock=lambda: now
        )

    cache = new_cache()
    single = cache.resolve_artifact_set(
        model_content_sha256=raw_recipe["models"][0]["model"]["content_sha256"]
    )
    assert all(
        spec.key == f"artifact-{spec.sha256[:12]}-{spec.artifact_id}"
        for spec in single.artifacts
    )
    # A manifest returned by resolution need not already have a set row.
    single_preview = cache.download_preview(
        model_content_sha256=single.model_content_sha256
    )
    seeded = cache.start_download(
        actor="operator",
        request_key=str(uuid.uuid4()),
        plan_digest=str(single_preview["plan_digest"]),
        model_content_sha256=single.model_content_sha256,
    )
    cache.run_pending()
    assert cache.get_operation(seeded.id).state == "succeeded"
    shared_digest = hashlib.sha256(payloads["shared"]).hexdigest()
    shared_path = cache_root / "objects" / shared_digest[:2] / shared_digest
    shared_inode = shared_path.stat().st_ino
    requests.clear()
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")

    def availability(model_cache: ModelCacheService):
        return _service(
            sessions,
            storage=storage,
            authority=lambda *_args, **_kwargs: (recipe, _runtime()),
            model_cache=model_cache,
            clock=lambda: now,
        )

    service = availability(cache)
    request_key = str(uuid.uuid4())
    parent = service.start(revision.id, actor="operator", request_id=request_key)
    # Reproduce the former producer's collision, without replacing any reader,
    # storage, worker, or downstream receipt consumer.
    with monkeypatch.context() as old_producer:
        old_producer.setattr(
            "vonk_control.model_cache.resolution._composed_artifacts", tuple
        )
        service.run_pending(limit=1)
    waiting = service.get(parent.id)
    assert waiting.state == "queued"
    assert waiting.model_child is None
    assert "cache artifact keys must be unique" in str(waiting.failure)
    with sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(ModelCacheOperation)) == 1
        )
    cache.close()

    now += timedelta(seconds=90)
    cache = new_cache()
    service = availability(cache)
    service.run_pending(limit=1)
    recovered = service.get(parent.id)
    assert recovered.model_child is not None, recovered.failure
    child_id = recovered.model_child["id"]
    with sessions() as session:
        child = session.get(ModelCacheOperation, child_id)
        accepted_parent = session.get(Job, parent.id)
        assert child is not None and accepted_parent is not None
        assert accepted_parent.request_id == request_key
        assert isinstance(child.payload, dict)
        payload = ModelCacheDownloadPayload.model_validate_json(
            canonical_message(child.payload)
        )
        manifest = ArtifactSetManifest.from_contract(payload.manifest)
        assert manifest.digest == child.artifact_set_sha256
        child_plan = child.plan_digest
        assert len(manifest.artifacts) == 4
        assert len({spec.key for spec in manifest.artifacts}) == 4
        assert manifest.expected_bytes == sum(map(len, payloads.values()))
        assert len(payload.transfer.artifacts) == 3
        assert payload.transfer.total_bytes == len(payloads["assistant"])
        assert set(model_set_objects(session, (manifest.digest,))[manifest.digest]) == {
            hashlib.sha256(value).hexdigest() for value in payloads.values()
        }
    cache.close()

    # A new service reads the real persisted JSON and resumes the accepted child.
    cache = new_cache()
    cache.run_pending()
    child = cache.get_operation(child_id)
    assert child.state == "succeeded", child.failure
    assert child.plan_digest == child_plan
    assert requests == [
        "/models/"
        + ("shared-source" if same_source else "assistant")
        + "/resolve/"
        + "0" * 40
        + "/assistant.bin"
    ]
    assert shared_path.stat().st_ino == shared_inode
    assert shared_path.read_bytes() == payloads["shared"]
    entry = cache.get_entry(manifest.digest)
    assert entry["expected_bytes"] == entry["verified_bytes"] == manifest.expected_bytes
    source = ModelCacheObjectSource.from_service(cache)
    receipts = source.verified_model_objects_for_set(manifest.digest, manifest)
    assert {(item.model_content_sha256, item.file_id) for item in receipts} == {
        (spec.model_content_sha256, spec.artifact_id) for spec in manifest.artifacts
    }
    model_documents = {
        document_sha256(model.model_dump(mode="json")): model for model in models
    }
    runtime = compile_runtime_spec(
        recipe,
        recipe_digest=recipe_digest,
        models=model_documents,
        package_handle={
            "image_reference": "localhost/vonk/build@sha256:" + "e" * 64,
            "image_digest": "e" * 64,
        },
        role="entrypoint",
        rank=0,
    )
    runtime = _bind_runtime_artifacts(runtime, model_documents)
    plan = compile_verified_execution_plan(
        runtime,
        model_artifact_set_sha256=manifest.digest,
        model_objects=receipts,
        runtime_image=VerifiedRuntimeImage(
            image_digest="sha256:" + "e" * 64,
            oci_layout_sha256="f" * 64,
            image_bytes=3,
            build_id="test-build",
            local_image_config_id="sha256:" + "a" * 64,
            runtime_interface_label="v1",
        ),
    )
    assert len(plan.artifacts) == 4
    now += timedelta(seconds=90)
    service = availability(cache)
    service.run_pending(limit=1)
    completed = service.get(parent.id)
    assert completed.state == "succeeded", completed.failure
    assert completed.model_child is not None and completed.model_child["id"] == child_id
    assert (
        cache.resolve_artifact_set(
            model_content_sha256=single.model_content_sha256
        ).digest
        == single.digest
    )
    # Editorial successor provenance and selection ordering do not change the
    # immutable set identity or charge another transfer of the shared bytes.
    successor_models = []
    for model in models:
        document = model.model_dump(mode="json")
        document["metadata"]["description"] += " Editorial successor."
        successor_models.append(ModelDefinition.model_validate(document))
    successor_recipe = recipe.model_dump(mode="json")
    for selection, model in zip(
        successor_recipe["models"], successor_models, strict=True
    ):
        selection["model"]["content_sha256"] = document_sha256(
            model.model_dump(mode="json")
        )
    successor_recipe["models"].reverse()
    successor = RecipeDefinition.model_validate(successor_recipe)
    successor_revision = catalog.import_recipe_library(
        "test",
        library_commit="1" * 40,
        source_path="recipes/composition.json",
        document=successor.model_dump(mode="json"),
        expected_content_sha256=document_sha256(successor.model_dump(mode="json")),
        dependency_documents=[
            model.model_dump(mode="json") for model in successor_models
        ],
        source_bundle_sha256="c" * 64,
    )
    successor_manifest = cache.resolve_artifact_set(
        recipe_revision_id=successor_revision.id
    )
    assert successor_manifest.digest == manifest.digest
    successor_preview = cache.download_preview(recipe_revision_id=successor_revision.id)
    assert successor_preview["new_bytes"] == 0
    assert successor_preview["already_cached_bytes"] == manifest.expected_bytes
    cache.close()
    client.close()
    engine.dispose()
