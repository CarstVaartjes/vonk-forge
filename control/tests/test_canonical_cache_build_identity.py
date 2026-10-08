from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.catalog_entities import build_policy_projection
from vonk_control.catalog_revision_contract import write_catalog_projection
from vonk_control.compiled_execution_plan import VerifiedModelObject
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base, CatalogDocument, CatalogDocumentRevision
from vonk_control.recipe_builds import (
    derive_build_input_identity,
)
from vonk_control.runtime_adapters import resolve_runtime_adapter
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _digest(value: object) -> str:
    if isinstance(value, dict) and value.get("kind") == "model":
        return document_sha256(value)
    if isinstance(value, dict) and value.get("kind") == "recipe":
        return document_sha256(value)
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _model_document(
    *,
    path: str,
    file_digest: str,
    roles: list[str],
    publisher: str = "owner",
    slug: str = "model",
) -> dict[str, object]:
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    document["identity"]["publisher"] = publisher
    document["identity"]["slug"] = slug
    document["identity"]["model"]["publisher"] = publisher
    document["identity"]["model"]["slug"] = slug
    document["source"] = {
        "repository": f"https://huggingface.co/{publisher}/{slug}",
        "revision": "a" * 40,
    }
    document["files"] = [
        {
            "id": "weights",
            "path": path,
            "sha256": file_digest,
            "size_bytes": 12,
            "roles": roles,
        }
    ]
    return ModelDefinition.model_validate(document).model_dump(mode="json")


def _recipe_document(
    model_digest: str,
    *,
    publisher: str = "owner",
    slug: str = "recipe",
    model_slug: str = "model",
) -> dict[str, object]:
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    document["identity"]["publisher"] = publisher
    document["identity"]["slug"] = slug
    document["models"][0]["model"]["publisher"] = publisher
    document["models"][0]["model"]["slug"] = model_slug
    document["models"][0]["model"]["content_sha256"] = model_digest
    document["models"][0]["files"][0]["file_id"] = "weights"
    return document


def _add_active(
    session,
    *,
    root_id: str,
    revision_id: str,
    kind: str,
    publisher: str,
    slug: str,
    document: dict[str, object],
    revision_number: int = 1,
) -> CatalogDocumentRevision:
    if kind == "model":
        parsed = ModelDefinition.model_validate(document)
        projected: dict[str, object] = {
            "identity": parsed.identity.model_dump(mode="json"),
            "modalities": parsed.modalities,
            "artifact_count": len(parsed.files),
            "download_bytes": parsed.download_bytes,
            "installed_bytes": parsed.installed_bytes,
        }
    else:
        parsed = RecipeDefinition.model_validate(document)
        projected = {
            "title": parsed.metadata.title,
            "description": parsed.metadata.description,
            "tags": list(parsed.metadata.tags),
            "runtime_engine": parsed.runtime.engine,
            "topology": parsed.topology.model_dump(mode="json"),
        }
        projected.update(build_policy_projection(parsed))
    root = CatalogDocument(
        id=root_id,
        kind=kind,
        publisher=publisher,
        slug=slug,
        title=slug,
        created_by="test",
        created_at=NOW,
        updated_at=NOW,
    )
    revision = CatalogDocumentRevision(
        id=revision_id,
        document_id=root_id,
        kind=kind,
        publisher=publisher,
        slug=slug,
        revision_number=revision_number,
        schema_version=2,
        state="active",
        document=document,
        content_digest=document_sha256(document),
        artifact_key=("a" * 64 if kind == "model" else None),
        projected=write_catalog_projection(projected, kind=kind),
        created_by="test",
        created_at=NOW,
    )
    if revision_number == 1:
        session.add(root)
    session.add(revision)
    return revision


def test_canonical_recipe_resolution_uses_selected_file_identity_and_provenance(
    tmp_path: Path,
) -> None:
    sessions = _sessions()
    file_digest = hashlib.sha256(b"model bytes!").hexdigest()
    model_document = _model_document(
        path="weights/model.safetensors", file_digest=file_digest, roles=["weights"]
    )
    model_digest = _digest(model_document)
    recipe_document = _recipe_document(model_digest)
    with sessions.begin() as session:
        _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000001",
            revision_id="00000000-0000-4000-8000-000000000002",
            kind="model",
            publisher="owner",
            slug="model",
            document=model_document,
        )
        recipe = _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000003",
            revision_id="00000000-0000-4000-8000-000000000004",
            kind="recipe",
            publisher="owner",
            slug="recipe",
            document=recipe_document,
        )

    service = ModelCacheService(sessions, tmp_path / "cache", reserve_bytes=0)
    first = service.resolve_artifact_set(recipe_revision_id=recipe.id)
    assert first.model_content_sha256 == model_digest
    assert first.recipe_revision_sha256 == _digest(recipe_document)
    assert first.artifacts[0].path == "weights/model.safetensors"
    assert first.artifacts[0].expected_bytes == 12

    editorial = json.loads(json.dumps(recipe_document))
    editorial["metadata"]["description"] = "Updated release description."
    with sessions.begin() as session:
        edited = _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000003",
            revision_id="00000000-0000-4000-8000-000000000005",
            kind="recipe",
            publisher="owner",
            slug="recipe",
            document=editorial,
            revision_number=2,
        )
    second = service.resolve_artifact_set(recipe_revision_id=edited.id)
    assert second.recipe_revision_sha256 != first.recipe_revision_sha256
    assert second.digest == first.digest

    changed_document = _model_document(
        path="weights/other.safetensors",
        file_digest=file_digest,
        roles=["weights"],
        slug="changed-model",
    )
    changed_digest = _digest(changed_document)
    changed_recipe = _recipe_document(
        changed_digest, slug="changed-recipe", model_slug="changed-model"
    )
    with sessions.begin() as session:
        _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000006",
            revision_id="00000000-0000-4000-8000-000000000007",
            kind="model",
            publisher="owner",
            slug="changed-model",
            document=changed_document,
        )
        changed_recipe_revision = _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000008",
            revision_id="00000000-0000-4000-8000-000000000009",
            kind="recipe",
            publisher="owner",
            slug="changed-recipe",
            document=changed_recipe,
        )
    changed = service.resolve_artifact_set(
        recipe_revision_id=changed_recipe_revision.id
    )
    assert changed.digest != first.digest


def test_canonical_build_identity_excludes_editorial_runtime_selectors_and_notes() -> (
    None
):
    build = {
        "base_image": {
            "repository": "runtime/base",
            "digest": "b" * 64,
            "platform": "linux/arm64",
        },
        "context": {"path": "source"},
        "dockerfile": "Dockerfile",
        "patches": [],
        "target": "runtime",
        "arguments": [{"name": "flavor", "value": "release"}],
        "network": {"mode": "none", "hosts": []},
    }
    settings = {
        "kind": "generation",
        "context_tokens": {"value": 4096, "change_effect": "restart"},
        "concurrency": {"value": 1, "change_effect": "restart"},
        "knobs": {"compiler": {"value": "clang", "change_effect": "rebuild"}},
    }
    artifacts = [
        {
            "path": "weights/model.safetensors",
            "sha256": "c" * 64,
            "size_bytes": 12,
            "roles": ["weights"],
            "mount": {"target": "/models", "read_only": True},
        }
    ]
    first = derive_build_input_identity(
        build,
        source_bundle_sha256="d" * 64,
        builder_binary_digest="e" * 64,
        effective_settings=settings,
        model_artifacts=artifacts,
    )
    editorial = {**build, "notes": "release note"}
    runtime_only = [
        {
            **artifacts[0],
            "roles": ["auxiliary"],
            "mount": {"target": "/other", "read_only": True},
        }
    ]
    assert (
        derive_build_input_identity(
            editorial,
            source_bundle_sha256="d" * 64,
            builder_binary_digest="e" * 64,
            effective_settings=settings,
            model_artifacts=runtime_only,
        )
        == first
    )
    changed_settings = {
        **settings,
        "knobs": {"compiler": {"value": "gcc", "change_effect": "rebuild"}},
    }
    assert (
        derive_build_input_identity(
            build,
            source_bundle_sha256="d" * 64,
            builder_binary_digest="e" * 64,
            effective_settings=changed_settings,
            model_artifacts=artifacts,
        )
        != first
    )
    changed_file = [{**artifacts[0], "path": "weights/other.safetensors"}]
    assert (
        derive_build_input_identity(
            build,
            source_bundle_sha256="d" * 64,
            builder_binary_digest="e" * 64,
            effective_settings=settings,
            model_artifacts=changed_file,
        )
        != first
    )


def test_build_identity_binds_the_resolved_runtime_adapter() -> None:
    build = {
        "base_image": {
            "repository": "runtime/base",
            "digest": "b" * 64,
            "platform": "linux/arm64",
        },
        "context": {"path": "source"},
        "dockerfile": "Dockerfile",
        "target": "runtime",
        "arguments": [{"name": "flavor", "value": "release"}],
        "network": {"mode": "none", "hosts": []},
    }
    adapter = resolve_runtime_adapter("vllm", {"node_count": 1})
    changed = replace(adapter, adapter_id=f"{adapter.adapter_id}-next")

    def identity(value) -> dict[str, object]:
        return derive_build_input_identity(
            build,
            source_bundle_sha256="d" * 64,
            builder_binary_digest="e" * 64,
            runtime_adapter=value.document(),
        )

    # The wrong implementation omits the adapter, so an adapter change leaves
    # the prepared-image cache key identical and reuses an unadapted image.
    assert identity(adapter) == identity(adapter)
    assert identity(adapter) != identity(changed)


@pytest.mark.parametrize("cached_by", ["earlier-revision-set", "objects-only"])
def test_new_model_revision_with_the_same_files_reuses_the_cached_set(
    tmp_path: Path, monkeypatch, cached_by: str
) -> None:
    """Same bytes, new model revision: compile binds the cached set as-is.

    The set is keyed by its bytes, so model revision 2 resolves to the set
    revision 1 cached, whose row keeps revision 1's model identities. The
    compiler looks model files up by the requesting revision's identity; it
    must get the shared verified objects described with that identity, with
    no download and no re-hash.
    """

    from vonk_control.distribution import ModelCacheObjectSource
    from vonk_control.execution_plan_service import (
        ControllerExecutionPlanService,
        ExecutionPlanCompilationError,
    )

    sessions = _sessions()
    data = b"model bytes!"
    file_digest = hashlib.sha256(data).hexdigest()
    first_model = _model_document(
        path="weights/model.safetensors", file_digest=file_digest, roles=["weights"]
    )
    second_model = json.loads(json.dumps(first_model))
    second_model["metadata"]["description"] = "Revised model card."
    first_digest, second_digest = _digest(first_model), _digest(second_model)
    assert first_digest != second_digest
    with sessions.begin() as session:
        _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000001",
            revision_id="00000000-0000-4000-8000-000000000002",
            kind="model",
            publisher="owner",
            slug="model",
            document=first_model,
        )
        _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000001",
            revision_id="00000000-0000-4000-8000-000000000003",
            kind="model",
            publisher="owner",
            slug="model",
            document=second_model,
            revision_number=2,
        )
        first_recipe = _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000004",
            revision_id="00000000-0000-4000-8000-000000000005",
            kind="recipe",
            publisher="owner",
            slug="recipe",
            document=_recipe_document(first_digest),
        )
        second_recipe = _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000004",
            revision_id="00000000-0000-4000-8000-000000000006",
            kind="recipe",
            publisher="owner",
            slug="recipe",
            document=_recipe_document(second_digest),
            revision_number=2,
        )

    service = ModelCacheService(sessions, tmp_path / "cache", reserve_bytes=0)
    first = service.resolve_artifact_set(recipe_revision_id=first_recipe.id)
    second = service.resolve_artifact_set(recipe_revision_id=second_recipe.id)
    assert second.digest == first.digest
    # Revision 1 downloaded and verified these bytes at ingress.
    spec = first.artifacts[0]
    service._object_path(spec.sha256).parent.mkdir(parents=True, exist_ok=True)
    service._object_path(spec.sha256).write_bytes(data)
    service._write_object_receipt(spec, NOW)
    if cached_by == "earlier-revision-set":
        with sessions.begin() as session:
            row = service._ensure_set(session, first)
            row.state, row.verified_bytes = "cached", len(data)
    preview = service.download_preview(recipe_revision_id=second_recipe.id)
    assert preview["artifact_set_sha256"] == second.digest
    assert preview["new_bytes"] == 0

    if cached_by == "earlier-revision-set":
        # The stored row speaks for revision 1 only.
        stored = ModelCacheObjectSource.from_service(service)
        assert {
            item.model_content_sha256
            for item in stored.verified_model_objects_for_set(first.digest)
        } == {first_digest}
    # Revision 2 gets the same verified objects under its own identity.
    receipts = ModelCacheObjectSource.from_service(
        service
    ).verified_model_objects_for_set(second.digest, second)
    assert [
        (item.model_content_sha256, item.file_id, item.sha256) for item in receipts
    ] == [(second_digest, "weights", file_digest)]
    # The set is recorded as cached for every consumer, never downloaded.
    assert service.get_entry(second.digest)["coverage"] == "complete"

    # Profile loads and the library see revision 2's model as cached.
    status = service.resolve_latest_cached(recipe_identity=second_recipe.id)
    assert "model-not-cached" not in status.blockers

    # Compilation asks for exactly that: capture what it binds.
    bound: list[tuple[VerifiedModelObject, ...]] = []
    original = ModelCacheObjectSource.verified_model_objects_for_set

    def capture(self, digest, manifest=None):
        result = original(self, digest, manifest)
        bound.append(result)
        return result

    monkeypatch.setattr(
        ModelCacheObjectSource,
        "verified_model_objects_for_set",
        capture,
    )
    with sessions() as session:
        revision = session.get(CatalogDocumentRevision, second_recipe.id)
        assert revision is not None
        with pytest.raises(ExecutionPlanCompilationError, match="build receipt"):
            ControllerExecutionPlanService(service).compile_installation(
                session,
                revision=revision,
                build=None,
                mapping_nodes=(),
                parameters=None,
            )
    assert [item.model_content_sha256 for item in bound[0]] == [second_digest]
