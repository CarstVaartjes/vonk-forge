from __future__ import annotations

import errno
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.auth import Actor, TokenCodec
from vonk_control.catalog_entities import _build_projection
from vonk_control.distribution import (
    CompositeVerifiedObjectSource,
    ModelCacheVerifiedObjectSource,
)
from vonk_control.model_cache import (
    _CHUNK_BYTES,
    ArtifactSetManifest,
    ArtifactSpec,
    ModelCacheConflict,
    ModelCacheResolutionError,
    ModelCacheService,
    ModelCacheStorageError,
    _retry_after_seconds,
    _retryable_failure,
)
from vonk_control.model_cache_api import (
    ModelCacheOperationProvider,
    install_model_cache_routes,
    model_cache_operation_provider,
)
from vonk_control.model_cache_contract import (
    ModelCacheAccessResumeRequest,
    ModelCacheDownloadRequest,
    ModelCacheDownloadResult,
    ModelCacheEvictionPreviewRequest,
    ModelCacheEvictRequest,
)
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    FleetProfile,
    ModelCacheArtifact,
    ModelCacheOperation,
)
from vonk_control.run_switch_operations import DatabaseRunSwitchArtifactInspector
from vonk_control.worker import Worker
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256
from vonk_forge_contracts.model import ModelReference

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _canonical_model(
    *,
    publisher: str,
    slug: str,
    file_id: str,
    file_digest: str,
    dependencies: list[dict[str, str]] | None = None,
) -> ModelDefinition:
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
        "revision": "0" * 40,
    }
    document["files"] = [
        {
            "id": file_id,
            "path": f"{file_id}.safetensors",
            "sha256": file_digest,
            "size_bytes": 3,
            "roles": ["weights"],
        }
    ]
    document["dependencies"] = dependencies or []
    return ModelDefinition.model_validate(document)


def _canonical_recipe(model_digest: str) -> dict[str, object]:
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    document["models"][0]["model"]["content_sha256"] = model_digest
    return document


@pytest.fixture
def cache(tmp_path: Path):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = ModelCacheService(sessions, tmp_path / "nas-cache", reserve_bytes=0, fixture_sources=True)
    return service, sessions


def _artifact(
    root: Path,
    data: bytes,
    *,
    artifact_id: str = "weights",
    path: str = "weights.bin",
    model_content_sha256: str = "a" * 64,
) -> dict[str, object]:
    source = root / f"{artifact_id}.source"
    source.write_bytes(data)
    return {
        "id": artifact_id,
        "path": path,
        "kind": "file",
        "source": source.as_uri(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "download_bytes": len(data),
        "roles": ["model" if artifact_id == "weights" else "auxiliary"],
        "model_content_sha256": model_content_sha256,
    }


def _download(
    service: ModelCacheService,
    artifacts: list[dict[str, object]],
    *,
    model_content_sha256: str,
    request_key: str,
    interrupt_after_bytes: int | None = None,
):
    preview = service.download_preview(
        model_content_sha256=model_content_sha256,
        artifacts=artifacts,
    )
    operation = service.start_download(
        actor="test",
        request_key=request_key,
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model_content_sha256,
        artifacts=artifacts,
        interrupt_after_bytes=interrupt_after_bytes,
    )
    if interrupt_after_bytes is None:
        service.run_pending()
        operation = service.get_operation(operation.id)
    return operation


def _manifest_document(tmp_path: Path) -> dict[str, object]:
    data = b"manifest bytes"
    source = tmp_path / "manifest.source"
    source.write_bytes(data)
    artifact = ArtifactSpec(
        key="weights",
        artifact_id="weights",
        path="weights.bin",
        kind="file",
        repository=None,
        source=source.as_uri(),
        revision=None,
        sha256=hashlib.sha256(data).hexdigest(),
        expected_bytes=len(data),
        roles=("model",),
        model_content_sha256="a" * 64,
    )
    return ArtifactSetManifest(
        model_content_sha256="a" * 64,
        recipe_revision_sha256=None,
        model_content_digests=("a" * 64,),
        artifacts=(artifact,),
        model_definition_ref=ModelReference(
            publisher="vonk-forge", slug="manifest-model", content_sha256="a" * 64
        ),
    ).document()


def test_cache_manifest_requires_exact_canonical_field_sets(tmp_path: Path) -> None:
    document = _manifest_document(tmp_path)

    with pytest.raises(ModelCacheResolutionError, match="manifest shape is invalid"):
        ArtifactSetManifest.from_document({**document, "unexpected": True})
    missing = dict(document)
    missing.pop("model_content_digests")
    with pytest.raises(ModelCacheResolutionError, match="manifest shape is invalid"):
        ArtifactSetManifest.from_document(missing)


@pytest.mark.parametrize("schema_version", [True, 2.0])
def test_cache_manifest_requires_native_schema_version_type(
    tmp_path: Path, schema_version: object
) -> None:
    document = _manifest_document(tmp_path)
    document["schema_version"] = schema_version

    with pytest.raises(ModelCacheResolutionError) as error:
        ArtifactSetManifest.from_document(document)
    assert error.value.code == "model_cache.schema_unsupported"


@pytest.mark.parametrize(
    ("field", "value"),
    [("download_bytes", 1.0), ("download_bytes", True), ("roles", ("model",))],
)
def test_cache_manifest_rejects_coercible_artifact_types(
    tmp_path: Path, field: str, value: object
) -> None:
    document = _manifest_document(tmp_path)
    artifact = dict(document["artifacts"][0])
    artifact[field] = value
    document["artifacts"] = [artifact]

    with pytest.raises(ModelCacheResolutionError, match="manifest"):
        ArtifactSetManifest.from_document(document)


@pytest.mark.parametrize("mutation", ["extra", "missing"])
def test_cache_manifest_artifact_dto_requires_exact_fields(
    tmp_path: Path, mutation: str
) -> None:
    document = _manifest_document(tmp_path)
    artifact = dict(document["artifacts"][0])
    if mutation == "extra":
        artifact["unexpected"] = True
    else:
        artifact.pop("roles")
    document["artifacts"] = [artifact]

    with pytest.raises(ModelCacheResolutionError, match="manifest"):
        ArtifactSetManifest.from_document(document)


def test_canonical_catalog_revision_resolves_immutable_model_files(cache) -> None:
    service, sessions = cache
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    document["identity"]["publisher"] = "vonk-forge"
    document["identity"]["slug"] = "canonical-model"
    document["identity"]["model"]["publisher"] = "vonk-forge"
    document["identity"]["model"]["slug"] = "canonical-model"
    document["source"]["repository"] = "https://huggingface.co/vonk-forge/canonical-model"
    document["source"]["revision"] = "0" * 40
    document["files"] = [
        {
            "id": "weights",
            "path": "weights.bin",
            "sha256": "1" * 64,
            "size_bytes": 3,
            "roles": ["weights"],
        }
    ]
    document = ModelDefinition.model_validate(document).model_dump(mode="json")
    digest = content_sha256(ModelDefinition.model_validate(document))
    with sessions.begin() as session:
        root = CatalogDocument(
            id="00000000-0000-0000-0000-000000000031",
            kind="model",
            publisher="vonk-forge",
            slug="canonical-model",
            title="Canonical model",
            created_by="test",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(root)
        session.flush()
        session.add(
            CatalogDocumentRevision(
                id="00000000-0000-0000-0000-000000000032",
                document_id=root.id,
                kind="model",
                publisher=root.publisher,
                slug=root.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=document,
                content_digest=digest,
                projected={},
                created_by="test",
                created_at=NOW,
            )
        )

    manifest = service.resolve_artifact_set(model_content_sha256=digest)
    assert manifest.model_content_sha256 == digest
    assert manifest.model_definition_ref is not None
    assert manifest.model_definition_ref.model_dump(mode="json") == {
        "kind": "model",
        "publisher": "vonk-forge",
        "slug": "canonical-model",
        "content_sha256": digest,
    }
    assert [(item.path, item.expected_bytes, item.roles) for item in manifest.artifacts] == [
        ("weights.bin", 3, ("weights",))
    ]


@pytest.mark.parametrize("shared_object", [False, True])
def test_canonical_dependency_closure_reaches_run_switch(cache, shared_object: bool) -> None:
    service, sessions = cache
    companion = _canonical_model(
        publisher="vonk-forge",
        slug="companion",
        file_id="encoder",
        file_digest=("3" if shared_object else "2") * 64,
    )
    companion_document = companion.model_dump(mode="json")
    companion_document["files"].append({
        "id": "empty-config",
        "path": "config/empty.txt",
        "sha256": hashlib.sha256(b"").hexdigest(),
        "size_bytes": 0,
        "roles": ["config"],
    })
    companion = ModelDefinition.model_validate(companion_document)
    companion_digest = content_sha256(companion)
    primary = _canonical_model(
        publisher="vonk-forge",
        slug="primary",
        file_id="weights",
        file_digest="3" * 64,
        dependencies=[
            {
                "kind": "model",
                "publisher": "vonk-forge",
                "slug": "companion",
                "content_sha256": companion_digest,
            }
        ],
    )
    primary_digest = content_sha256(primary)
    recipe_document = _canonical_recipe(primary_digest)
    recipe_document["models"][0]["model"]["slug"] = "primary"
    recipe = RecipeDefinition.model_validate(recipe_document)
    recipe_digest = content_sha256(recipe)
    with sessions.begin() as session:
        for index, (definition, digest, slug) in enumerate(
            (
                (primary, primary_digest, "primary"),
                (companion, companion_digest, "companion"),
                (recipe, recipe_digest, recipe.identity.slug),
            ),
            start=41,
        ):
            kind = definition.kind
            root_id = f"00000000-0000-0000-0000-0000000000{index:02d}"
            session.add(
                CatalogDocument(
                    id=root_id,
                    kind=kind,
                    publisher="vonk-forge",
                    slug=slug,
                    title=slug,
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.add(
                CatalogDocumentRevision(
                    id=f"00000000-0000-0000-0000-0000000000{index + 10:02d}",
                    document_id=root_id,
                    kind=kind,
                    publisher="vonk-forge",
                    slug=slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=definition.model_dump(mode="json"),
                    content_digest=digest,
                    projected={},
                    created_by="test",
                    created_at=NOW,
                )
            )

    manifest = service.resolve_artifact_set(model_content_sha256=primary_digest)

    assert manifest.model_content_sha256 == primary_digest
    assert manifest.model_content_digests == tuple(sorted((primary_digest, companion_digest)))
    assert {item.model_content_sha256 for item in manifest.artifacts} == {
        primary_digest,
        companion_digest,
    }

    inspector = DatabaseRunSwitchArtifactInspector(service)
    with sessions() as session:
        inspection = inspector.inspect(
            session,
            model_content_sha256=primary_digest,
            recipe_revision_id="00000000-0000-0000-0000-000000000053",
            node_ids=("spk_" + "9" * 32,),
            retention="retain",
            now=NOW,
        )
    # Real ModelDefinition -> cache service -> Run/Switch, including companion
    # identity, empty support files and one-copy accounting of shared bytes.
    assert inspection.dependency_model_content_sha256 == (companion_digest,)
    assert inspection.artifact_set_sha256 == manifest.digest
    assert inspection.artifact_set_bytes == (3 if shared_object else 6)
    assert inspection.required_bytes == inspection.artifact_set_bytes
    assert inspection.missing_nas_bytes == inspection.artifact_set_bytes
    assert set(inspection.artifact_digests) == {item.sha256 for item in manifest.artifacts}
    assert hashlib.sha256(b"").hexdigest() in inspection.artifact_digests


def test_download_persists_real_primary_and_auxiliary_bytes_and_deduplicates(
    cache, tmp_path: Path
) -> None:
    service, sessions = cache
    model_a = "a" * 64
    primary = _artifact(tmp_path, b"primary model bytes", model_content_sha256=model_a)
    auxiliary = _artifact(
        tmp_path,
        b"tokenizer auxiliary bytes",
        artifact_id="tokenizer",
        path="tokenizer.json",
        model_content_sha256=model_a,
    )

    first = _download(
        service,
        [primary, auxiliary],
        model_content_sha256=model_a,
        request_key="00000000-0000-4000-8000-000000000001",
    )
    assert first.state == "succeeded"
    assert isinstance(first.result, ModelCacheDownloadResult)
    assert first.result.artifact_set_sha256 == first.artifact_set_sha256
    entry = service.get_entry(first.artifact_set_sha256 or "")
    assert entry["coverage"] == "complete"
    assert entry["expected_bytes"] == len(b"primary model bytestokenizer auxiliary bytes")
    assert entry["verified_bytes"] == entry["expected_bytes"]
    assert {item["path"] for item in entry["artifacts"]} == {
        "weights.bin",
        "tokenizer.json",
    }
    preparation = service.preparation_evidence(first.artifact_set_sha256 or "")
    assert preparation["artifact_set_sha256"] == first.artifact_set_sha256
    assert preparation["artifact_set_bytes"] == entry["expected_bytes"]
    assert preparation["controller"]["state"] == "ready"
    assert preparation["controller"]["verified_sha256"] == first.artifact_set_sha256
    assert preparation["targets"] == []

    descriptors = service.resolve_verified_artifact_set(first.artifact_set_sha256 or "")
    assert {item["sha256"] for item in descriptors} == {
        primary["sha256"],
        auxiliary["sha256"],
    }
    assert {item["file_id"] for item in descriptors} == {"weights", "tokenizer"}
    assert {item["model_content_sha256"] for item in descriptors} == {model_a}
    model_source = ModelCacheVerifiedObjectSource.from_service(service)
    receipts = model_source.verified_model_objects_for_set(
        first.artifact_set_sha256 or ""
    )
    composed_source = CompositeVerifiedObjectSource(model_source, object())
    assert composed_source.verified_model_objects_for_set(
        first.artifact_set_sha256 or ""
    ) == receipts
    assert {
        (
            item["model_content_sha256"],
            item["file_id"],
            item["path"],
            tuple(item["roles"]),
        )
        for item in receipts
    } == {
        (model_a, "weights", "weights.bin", ("model",)),
        (model_a, "tokenizer", "tokenizer.json", ("auxiliary",)),
    }
    assert all(
        item["distribution_object"]["sha256"] == item["sha256"]
        and item["distribution_object"]["bytes"] == item["bytes"]
        and item["distribution_object"]["name"] == item["path"]
        for item in receipts
    )
    assert service.read_verified_artifact(
        first.artifact_set_sha256 or "",
        str(primary["sha256"]),
        "weights.bin",
        offset=8,
        maximum_bytes=6,
    ) == b"model "

    model_b = "b" * 64
    primary_b = dict(primary, model_content_sha256=model_b)
    auxiliary_b = dict(auxiliary, model_content_sha256=model_b)
    second = _download(
        service,
        [primary_b, auxiliary_b],
        model_content_sha256=model_b,
        request_key="00000000-0000-4000-8000-000000000002",
    )
    assert second.state == "succeeded"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ModelCacheArtifact)) == 2
    assert service.storage_summary().unique_used_bytes == len(
        b"primary model bytes"
    ) + len(b"tokenizer auxiliary bytes")


@pytest.mark.parametrize(
    "invalid_result",
    [
        {"schema_version": 2, "artifact_set_sha256": "a" * 64},
        {"schema_version": 2, "removed_entries": [], "reclaimed_bytes": 0},
        "not a result document",
    ],
)
def test_cache_operation_reads_reject_malformed_or_wrong_kind_results(
    cache, tmp_path: Path, invalid_result: object,
) -> None:
    service, sessions = cache
    operation = _download(
        service,
        [_artifact(tmp_path, b"cached payload")],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000000019",
    )
    assert isinstance(operation.result, ModelCacheDownloadResult)
    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, operation.id)
        row.payload = {**row.payload, "result": invalid_result}
    with pytest.raises(ModelCacheStorageError, match="payload is invalid"):
        service.get_operation(operation.id)


def test_one_set_with_shared_digest_counts_one_physical_payload(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "9" * 64
    primary = _artifact(tmp_path, b"shared payload", model_content_sha256=model)
    alias = dict(
        primary,
        artifact_id="weights-alias",
        id="weights-alias",
        path="weights-alias.bin",
        roles=["auxiliary"],
    )
    operation = _download(
        service,
        [primary, alias],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000015",
    )
    assert operation.state == "succeeded"
    assert operation.progress["downloaded_bytes"] == len(b"shared payload")
    entry = service.get_entry(operation.artifact_set_sha256 or "")
    assert entry["coverage"] == "complete"
    assert entry["expected_bytes"] == len(b"shared payload")
    assert entry["unique_bytes"] == len(b"shared payload")
    assert service.storage_summary().unique_used_bytes == len(b"shared payload")


def test_operation_transfer_progress_counts_only_missing_objects(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "7" * 64
    cached = _artifact(tmp_path, b"cached", artifact_id="cached", model_content_sha256=model)
    first = _download(
        service,
        [cached],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000016",
    )
    assert first.progress["downloaded_bytes"] == len(b"cached")

    missing = _artifact(
        tmp_path,
        b"new object",
        artifact_id="missing",
        path="missing.bin",
        model_content_sha256=model,
    )
    preview = service.download_preview(
        model_content_sha256=model,
        artifacts=[cached, missing],
    )
    assert preview["already_cached_bytes"] == len(b"cached")
    assert preview["new_bytes"] == len(b"new object")
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000017",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model,
        artifacts=[cached, missing],
    )
    assert operation.progress["expected_bytes"] == len(b"new object")
    service.run_pending()
    operation = service.get_operation(operation.id)
    assert operation.progress["downloaded_bytes"] == len(b"new object")
    assert operation.progress["expected_bytes"] == len(b"new object")


def test_download_mutation_is_queued_until_the_controller_worker_runs(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "1" * 64
    data = b"queued payload"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)
    preview = service.download_preview(
        model_content_sha256=model,
        artifacts=[artifact],
    )

    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000014",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model,
        artifacts=[artifact],
    )
    assert operation.state == "queued"
    assert operation.attempt == 1
    assert not list(service.root.joinpath("objects").glob("*/*"))

    assert service.run_pending() == 1
    completed = service.get_operation(operation.id)
    assert completed.state == "succeeded"
    assert completed.attempt == 1


def test_activity_provider_filters_pages_and_projects_attempt_and_progress(
    cache, tmp_path: Path
) -> None:
    from vonk_control import operation_api, operation_contract
    service, _sessions = cache
    cursor_codec = TokenCodec(b"c" * 32).cursor_codec()
    provider = model_cache_operation_provider(service, cursors=cursor_codec)
    assert isinstance(provider, operation_api.OperationProvider)
    operation_ids: list[str] = []
    for index in range(2):
        model = str(index + 1) * 64
        artifact = _artifact(
            tmp_path,
            f"queued-{index}".encode(),
            artifact_id=f"weights-{index}",
            path=f"weights-{index}.bin",
            model_content_sha256=model,
        )
        preview = service.download_preview(
            model_content_sha256=model,
            artifacts=[artifact],
        )
        operation = service.start_download(
            actor="test",
            request_key=f"00000000-0000-4000-8000-00000000001{index}",
            plan_digest=str(preview["plan_digest"]),
            model_content_sha256=model,
            artifacts=[artifact],
        )
        assert operation.state == "queued"
        assert operation.attempt == 1
        operation_ids.append(operation.id)

    query = operation_api.OperationQuery(limit=1, after=None, state=None, node_id=None)
    with pytest.raises(operation_api.OperationProjectionError, match="cursor projection unavailable"):
        model_cache_operation_provider(service).list_operations(query)
    first_page = provider.list_operations(query)
    assert isinstance(first_page, operation_api.OperationListPage)
    assert first_page.total == 2
    assert len(first_page.items) == 1
    first = first_page.items[0]
    assert first["node_ids"] == []
    assert first["attempt"] == 1
    assert first["supported_actions"] == []
    progress = operation_contract.OperationProgress.model_validate(first["progress"])
    assert progress.phase == "queued"
    assert progress.completed_bytes == 0
    assert progress.total_bytes_known is True
    assert first_page.next_cursor
    decoded_cursor = cursor_codec.decode(
        first_page.next_cursor,
        resource="model-cache-operations",
        order="created-at-desc/id-desc/v1",
        context={"state": None, "node_id": None},
    )
    assert isinstance(decoded_cursor, list)
    assert len(decoded_cursor) == 2

    after = (
        datetime.fromisoformat(str(decoded_cursor[0])),
        str(decoded_cursor[1]),
    )
    second_page = provider.list_operations(
        operation_api.OperationQuery(limit=1, after=after, state=None, node_id=None)
    )
    assert second_page.total == 2
    assert len(second_page.items) == 1
    assert second_page.items[0]["id"] != first["id"]
    assert second_page.next_cursor is None

    queued_page = provider.list_operations(
        operation_api.OperationQuery(
            limit=100, after=None, state="queued", node_id=None
        )
    )
    assert queued_page.total == 2
    assert {item["id"] for item in queued_page.items} == set(operation_ids)

    node_page = provider.list_operations(
        operation_api.OperationQuery(
            limit=100,
            after=None,
            state=None,
            node_id="spk_" + "a" * 32,
        )
    )
    assert list(node_page.items) == []
    assert node_page.total == 0
    detail = provider.get_operation(str(first["id"]))
    assert detail["id"] == first["id"]
    assert detail["node_ids"] == []
    assert detail["attempt"] == 1

    assert service.run_pending(limit=2) == 2
    succeeded_page = provider.list_operations(
        operation_api.OperationQuery(
            limit=100, after=None, state="succeeded", node_id=None
        )
    )
    assert succeeded_page.total == 2
    assert all(item["state"] == "succeeded" for item in succeeded_page.items)


def test_interrupted_download_checkpoint_resumes_after_service_restart(
    cache, tmp_path: Path
) -> None:
    service, sessions = cache
    model = "c" * 64
    data = bytes(range(256)) * 12_000
    artifact = _artifact(tmp_path, data, model_content_sha256=model)

    preview = service.download_preview(model_content_sha256=model, artifacts=[artifact])
    partial = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000003",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model,
        artifacts=[artifact],
        interrupt_after_bytes=1_100_000,
    )
    assert partial.state == "partial"
    assert partial.attempt == 1
    assert partial.progress["expected_bytes"] == len(data)
    checkpoint_bytes = (
        service.root
        / "partials"
        / str(partial.artifact_set_sha256)
        / f"{artifact['sha256']}.part"
    ).stat().st_size
    assert partial.progress["downloaded_bytes"] == checkpoint_bytes
    # Replay uses the original plan identity even though the current preview
    # now sees a shorter remaining range.
    replay = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000003",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model,
        artifacts=[artifact],
    )
    assert replay.id == partial.id
    assert replay.progress["downloaded_bytes"] == partial.progress["downloaded_bytes"]
    set_digest = partial.artifact_set_sha256 or ""
    part = service.root / "partials" / set_digest / f"{artifact['sha256']}.part"
    assert 0 < part.stat().st_size < len(data)

    restarted = ModelCacheService(
        sessions,
        service.root,
        reserve_bytes=0,
        fixture_sources=True,
    )
    assert restarted.resume_operations() == 1
    restarted.run_pending()
    resumed = restarted.get_operation(partial.id)
    assert resumed.state == "succeeded"
    assert resumed.progress["downloaded_bytes"] == len(data)
    assert resumed.progress["expected_bytes"] == len(data)
    assert resumed.attempt == 2
    assert (service.root / "objects" / str(artifact["sha256"])[0:2] / str(artifact["sha256"]).strip()).read_bytes() == data
    assert restarted.get_entry(set_digest)["coverage"] == "complete"


def test_transient_download_failure_requeues_with_exact_identity_and_bound(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "e" * 64
    data = b"retryable payload"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)
    preview = service.download_preview(model_content_sha256=model, artifacts=[artifact])
    original = service._open_source
    calls = 0

    def flaky_source(spec, offset):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.ETIMEDOUT, "network copy temporarily unavailable")
        return original(spec, offset)

    service._open_source = flaky_source
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000019",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model,
        artifacts=[artifact],
    )
    service.run_pending()
    queued = service.get_operation(operation.id)
    assert queued.state == "queued"
    assert queued.attempt == 2
    assert queued.plan_digest == preview["plan_digest"]
    assert queued.artifact_set_sha256 == preview["artifact_set_sha256"]
    assert queued.progress["downloaded_bytes"] == 0

    service.run_pending()
    completed = service.get_operation(operation.id)
    assert completed.state == "succeeded"
    assert completed.attempt == 2
    assert completed.plan_digest == queued.plan_digest
    assert completed.artifact_set_sha256 == queued.artifact_set_sha256


def test_exhausted_transient_download_allows_bounded_operator_retry_after_restart(
    cache, tmp_path: Path
) -> None:
    service, sessions = cache
    model = "a" * 64
    data = b"operator retry payload"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)
    preview = service.download_preview(model_content_sha256=model, artifacts=[artifact])
    original = service._open_source
    calls = 0

    def flaky_source(spec, offset):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise OSError(errno.ETIMEDOUT, "timed out")
        return original(spec, offset)

    service._open_source = flaky_source
    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000020",
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model,
        artifacts=[artifact],
    )
    for _ in range(3):
        assert service.run_pending() == 1
    exhausted = service.get_operation(operation.id)
    assert exhausted.state == "failed"
    assert exhausted.attempt == 3
    retry = service.retry(
        exhausted.id,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000021",
    )
    assert retry.state == "queued"
    assert retry.attempt == 1
    assert retry.plan_digest == exhausted.plan_digest
    assert retry.artifact_set_sha256 == exhausted.artifact_set_sha256

    restarted = ModelCacheService(sessions, service.root, reserve_bytes=0, fixture_sources=True)
    assert restarted.resume_operations() == 1
    restarted.run_pending()
    assert restarted.get_operation(retry.id).state == "succeeded"


def test_model_cache_retry_classification_rejects_terminal_http_and_storage_errors() -> None:
    request = httpx.Request("GET", "https://example.invalid/model")
    for status in (401, 403, 404):
        response = httpx.Response(status, request=request)
        error = httpx.HTTPStatusError("request failed", request=request, response=response)
        assert _retryable_failure(error) is False
    for status in (429, 500, 503):
        response = httpx.Response(status, request=request)
        error = httpx.HTTPStatusError("request failed", request=request, response=response)
        assert _retryable_failure(error) is True
    assert _retryable_failure(OSError(errno.EACCES, "permission denied")) is False
    assert _retryable_failure(OSError(errno.ENOSPC, "no space left")) is False
    assert _retryable_failure(OSError(errno.ETIMEDOUT, "timed out")) is True


def test_provider_retry_after_and_rate_limit_reset_are_bounded_hints() -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert _retry_after_seconds({"retry-after": "7"}, now=now) == 7
    assert _retry_after_seconds({"ratelimit-reset": "30"}, now=now) == 30
    assert _retry_after_seconds(
        {"x-ratelimit-reset": str(int(now.timestamp()) + 11)}, now=now
    ) == 11
    assert _retry_after_seconds(
        {"RateLimit": '"resolvers";r=0;t=123'}, now=now
    ) == 123
    assert _retry_after_seconds(
        {"Retry-After": "7", "RateLimit": '"resolvers";r=0;t=123'}, now=now
    ) == 123
    assert _retry_after_seconds(
        {"RateLimit": '"resolvers";r=0;t=123junk'}, now=now
    ) is None
    assert _retry_after_seconds(
        {"RateLimit": '"resolvers";r=0;t=999999999999999999999'}, now=now
    ) == 365 * 24 * 60 * 60


def test_worker_prefers_nonblocking_cache_tick_when_available() -> None:
    calls: list[str] = []

    class Jobs:
        def claim(self, *_args, **_kwargs):
            raise AssertionError("generic jobs should not run before cache tick")

    class Cache:
        def tick(self):
            calls.append("tick")
            return True

    worker = Worker(Jobs(), "worker", {}, model_cache=Cache())
    assert worker.run_once() is True
    assert calls == ["tick"]


def test_same_pin_repair_verifies_before_atomic_replace_and_preserves_old_bytes(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _sessions = cache
    model = "d" * 64
    good = b"good payload"
    artifact = _artifact(tmp_path, good, artifact_id="weights", model_content_sha256=model)
    source = tmp_path / "weights.source"
    set_digest = _download(
        service,
        [artifact],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000004",
    ).artifact_set_sha256 or ""
    target = service.root / "objects" / str(artifact["sha256"])[0:2] / str(artifact["sha256"])
    assert target.read_bytes() == good

    source.write_bytes(b"bad! payload")
    bad_preview = service.repair_preview(set_digest)
    failed = service.start_repair(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000005",
        artifact_set_sha256=set_digest,
        plan_digest=str(bad_preview["plan_digest"]),
    )
    service.run_pending()
    failed = service.get_operation(failed.id)
    assert failed.state == "failed"
    assert target.read_bytes() == good

    source.write_bytes(good)
    replace = __import__("vonk_control.model_cache", fromlist=["os"]).os.replace
    calls = 0

    def fail_final_replace(source_path, target_path):
        nonlocal calls
        calls += 1
        assert target.read_bytes() == good
        if target_path == target:
            raise OSError("simulated atomic publish failure")
        return replace(source_path, target_path)

    monkeypatch.setattr("vonk_control.model_cache.os.replace", fail_final_replace)
    swap_failed = service.start_repair(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000006",
        artifact_set_sha256=set_digest,
        plan_digest=str(bad_preview["plan_digest"]),
    )
    service.run_pending()
    swap_failed = service.get_operation(swap_failed.id)
    assert swap_failed.state == "failed"
    assert target.read_bytes() == good
    assert not any(service.root.joinpath("quarantine").iterdir())

    monkeypatch.undo()
    repaired = service.start_repair(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000007",
        artifact_set_sha256=set_digest,
        plan_digest=str(bad_preview["plan_digest"]),
    )
    service.run_pending()
    repaired = service.get_operation(repaired.id)
    assert repaired.state == "succeeded"
    assert target.read_bytes() == good


def test_protection_is_derived_from_durable_references_and_blocks_eviction(
    cache, tmp_path: Path
) -> None:
    service, sessions = cache
    model = "e" * 64
    data = b"protected model"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)
    set_digest = _download(
        service,
        [artifact],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000008",
    ).artifact_set_sha256 or ""
    recipe_revision_id = "00000000-0000-4000-8000-000000000022"
    recipe_document = _canonical_recipe(model)
    recipe = RecipeDefinition.model_validate(recipe_document)
    recipe_digest = content_sha256(recipe)
    recipe_projection = {
        "title": recipe.metadata.title,
        "description": recipe.metadata.description,
        "tags": list(recipe.metadata.tags),
        "runtime_engine": recipe.runtime.engine,
        "topology": recipe.topology.model_dump(mode="json"),
    }
    recipe_projection.update(_build_projection(recipe))
    with sessions.begin() as session:
        session.add(
            CatalogDocument(
                    id="00000000-0000-4000-8000-000000000021",
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                title="Protected recipe",
                created_by="test",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id=recipe_revision_id,
                document_id="00000000-0000-4000-8000-000000000021",
                kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                revision_number=1,
                state="active",
                schema_version=2,
                document=recipe_document,
                content_digest=recipe_digest,
                    projected=recipe_projection,
                created_by="test",
                created_at=NOW,
            )
        )
    with sessions.begin() as session:
        session.add(
            FleetProfile(
                name="protected-profile",
                description="",
                installation_policy="keep-cached",
                assignments=[{"recipe_revision_id": recipe_revision_id}],
                labels={},
                favorite=False,
                created_by="test",
                created_at=NOW,
                updated_at=NOW,
            )
        )

    preview = service.eviction_preview(target_bytes=len(data))
    assert preview["selected"] == []
    assert preview["selected_bytes"] == 0
    assert preview["protected_entries"][0]["artifact_set_sha256"] == set_digest
    assert "protected entries require separate reference removal" in preview["blockers"]
    assert service.storage_summary().protected_bytes == len(data)
    with pytest.raises(ModelCacheConflict):
        service.evict(
            actor="test",
            request_key="00000000-0000-4000-8000-000000000009",
            plan_digest=str(preview["plan_digest"]),
            target_bytes=len(data),
        )

    with sessions.begin() as session:
        session.query(FleetProfile).delete()
    unprotected = service.eviction_preview(target_bytes=len(data))
    assert unprotected["blockers"] == []
    assert unprotected["selected_bytes"] == len(data)
    removed = service.evict(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000010",
        plan_digest=str(unprotected["plan_digest"]),
        target_bytes=len(data),
    )
    service.run_pending()
    removed = service.get_operation(removed.id)
    assert removed.state == "succeeded"
    assert service.storage_summary().unique_used_bytes == 0


def test_contracts_and_routes_are_schema_two_and_do_not_accept_sources_or_force_flags(
    cache,
) -> None:
    service, _sessions = cache
    assert "artifacts" not in ModelCacheDownloadRequest.model_fields
    assert "protected" not in ModelCacheEvictionPreviewRequest.model_fields
    assert set(ModelCacheDownloadRequest.model_fields) >= {"request_key", "plan_digest"}
    assert set(ModelCacheEvictRequest.model_fields) >= {"request_key", "plan_digest"}
    assert set(ModelCacheAccessResumeRequest.model_fields) >= {
        "request_key",
        "artifact_set_sha256",
        "plan_digest",
    }
    with pytest.raises(ValueError):
        ModelCacheDownloadRequest(
            request_key="00000000-0000-4000-8000-000000000011",
            plan_digest="f" * 64,
            artifacts=[],
        )

    app = FastAPI()
    install_model_cache_routes(
        app,
        actor_dependency=Depends(lambda: Actor("admin", "administrator")),
        service=service,
        audits=[],
    )
    client = TestClient(app)
    inventory = client.get("/api/v1/model-cache")
    assert inventory.status_code == 200
    assert inventory.json()["schema_version"] == 2
    assert inventory.json()["storage"]["unique_used_bytes"] == 0
    operations = client.get("/api/v1/model-cache/operations")
    assert operations.status_code == 200
    assert operations.json() == {
        "schema_version": 2,
        "operations": [],
        "total": 0,
        "next_cursor": None,
    }
    updates = client.get("/api/v1/model-cache/updates")
    assert updates.status_code == 200
    assert updates.json() == {
        "schema_version": 2,
        "source_policy": "nas-first",
        "updates": [],
        "total": 0,
        "next_cursor": None,
    }
    bad = client.post(
        "/api/v1/model-cache/download",
        json={
            "schema_version": 2,
            "request_key": "00000000-0000-4000-8000-000000000012",
            "plan_digest": "f" * 64,
            "model_content_sha256": "a" * 64,
            "artifacts": [{"source": "file:///etc/passwd"}],
            "protected": True,
        },
    )
    assert bad.status_code == 422
    assert {route.path for route in app.routes} >= {
        "/api/v1/model-cache",
        "/api/v1/model-cache/download-preview",
        "/api/v1/model-cache/download",
        "/api/v1/model-cache/repair-preview",
        "/api/v1/model-cache/repair",
        "/api/v1/model-cache/eviction-preview",
        "/api/v1/model-cache/evict",
        "/api/v1/model-cache/updates",
        "/api/v1/model-cache/operations",
        "/api/v1/model-cache/operations/{operation_id}",
        "/api/v1/model-cache/operations/{operation_id}/retry",
        "/api/v1/model-cache/operations/{operation_id}/check-access-and-resume",
    }


def test_verified_serving_seam_refuses_incomplete_or_tampered_sets(cache, tmp_path: Path) -> None:
    service, _sessions = cache
    model = "f" * 64
    data = b"bounded bytes"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)
    interrupted = _download(
        service,
        [artifact],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000013",
        interrupt_after_bytes=1,
    )
    with pytest.raises(ModelCacheConflict, match="not completely verified"):
        service.resolve_verified_artifact_set(interrupted.artifact_set_sha256 or "")

    assert service.resume_operations() == 1
    service.run_pending()
    set_digest = interrupted.artifact_set_sha256 or ""
    target = service.root / "objects" / str(artifact["sha256"])[0:2] / str(artifact["sha256"])
    assert service.read_verified_artifact(set_digest, str(artifact["sha256"]), "weights.bin") == data
    target.write_bytes(b"tampered!!!")
    with pytest.raises(ModelCacheConflict, match="not completely verified"):
        service.read_verified_artifact(set_digest, str(artifact["sha256"]), "weights.bin")


def test_controller_worker_drains_queued_cache_operations_without_inline_api_transfer() -> None:
    calls: list[int] = []

    class Jobs:
        def claim(self, *_args, **_kwargs):
            raise AssertionError("cache work should be selected before generic jobs")

    class CacheWorker:
        def run_pending(self, *, limit: int) -> int:
            calls.append(limit)
            return 1

    worker = Worker(Jobs(), "worker", {}, model_cache=CacheWorker())
    assert worker.run_once() is True
    assert calls == [1]


def test_empty_http_support_artifact_does_not_issue_an_invalid_zero_range(
    cache, tmp_path: Path
) -> None:
    _service, sessions = cache
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request, content=b"")

    http_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    service = ModelCacheService(
        sessions,
        tmp_path / "http-nas-cache",
        reserve_bytes=0,
        http_client=http_client,
        fixture_sources=True,
    )
    artifact = {
        "id": "metadata",
        "path": "config/empty.json",
        "kind": "http.file",
        "source": "http://fixture.invalid/empty",
        "repository": "http://fixture.invalid/empty",
        "revision": "fixture-revision",
        "sha256": hashlib.sha256(b"").hexdigest(),
        "download_bytes": 0,
        "roles": ["auxiliary"],
        "model_content_sha256": "e" * 64,
    }
    try:
        operation = _download(
            service,
            [artifact],
            model_content_sha256="e" * 64,
            request_key="00000000-0000-4000-8000-000000000018",
        )
        assert operation.state == "succeeded"
        assert len(requests) == 1
        assert requests[0].headers.get("range") is None
        assert service.get_entry(operation.artifact_set_sha256 or "")["coverage"] == "complete"
    finally:
        http_client.close()


def test_activity_progress_with_unknown_total_has_no_rate_or_eta_fields() -> None:
    from vonk_control.model_cache_progress import cache_progress
    value = cache_progress({"phase": "downloading", "completed_artifacts": 0,
        "total_artifacts": 1, "downloaded_bytes": 12, "expected_bytes": None},
        previous=None, now=NOW)
    progress = ModelCacheOperationProvider._progress(value)
    assert progress["phase"] == "download"
    assert progress["completed_bytes"] == 12
    assert progress["total_bytes_known"] is False
    assert "eta_seconds" not in progress
    assert "bytes_per_second" not in progress
    with pytest.raises(ValidationError):
        ModelCacheOperationProvider._progress({"phase": "downloading", "downloaded_bytes": 12})


def test_failed_eviction_exposes_durable_failure_after_restart(cache, tmp_path, monkeypatch):
    service, sessions = cache
    downloaded = _download(
        service, [_artifact(tmp_path, b"eviction bytes")],
        model_content_sha256="a" * 64,
        request_key="00000000-0000-4000-8000-000000000071",
    )
    preview = service.eviction_preview(target_bytes=14)
    operation = service.evict(
        actor="test", request_key="00000000-0000-4000-8000-000000000072",
        plan_digest=preview["plan_digest"], target_bytes=14,
    )
    original = Path.unlink

    def fail_object_removal(path, *args, **kwargs):
        if path.parent == service.root / "objects" or "objects" in path.parts:
            raise OSError("object removal failed")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_object_removal)
    service.run_pending()
    restarted = ModelCacheService(sessions, service.root, reserve_bytes=0, fixture_sources=True)
    app = FastAPI()
    install_model_cache_routes(
        app, actor_dependency=Depends(lambda: Actor("admin", "administrator")),
        service=restarted, audits=[],
    )
    client = TestClient(app)
    response = client.get(f"/api/v1/model-cache/operations/{operation.id}")
    assert response.status_code == 200
    document = response.json()
    assert document["state"] == "failed"
    assert document["result"] is None
    assert document["failure"]["code"] == "model_cache.eviction_failed"
    assert document["failure"]["detail"] == "object removal failed"
    from vonk_control.model_cache_contract import ModelCacheOperationResponse
    with pytest.raises(ValidationError, match="requires failure evidence"):
        ModelCacheOperationResponse.model_validate(document | {"failure": None})
    succeeded = client.get(f"/api/v1/model-cache/operations/{downloaded.id}").json()
    assert succeeded["state"] == "succeeded"
    with pytest.raises(ValidationError, match="requires a result"):
        ModelCacheOperationResponse.model_validate(succeeded | {"result": None})
    with sessions.begin() as session:
        row = session.get(ModelCacheOperation, downloaded.id)
        row.payload = {key: value for key, value in row.payload.items() if key != "result"}
    with pytest.raises(ValidationError, match="requires a result"):
        restarted.get_operation(downloaded.id)


def test_repair_resumes_quarantined_bytes_after_restart(cache, tmp_path, monkeypatch):
    service, sessions = cache
    data = b"x" * (2 * 1024 * 1024 + 3)
    artifact = _artifact(tmp_path, data)
    small = _artifact(tmp_path, b"config", artifact_id="tokenizer", path="config.json")
    downloaded = _download(service, [small, artifact], model_content_sha256="a" * 64,
                           request_key="00000000-0000-4000-8000-000000001001")
    digest = downloaded.artifact_set_sha256
    preview = service.repair_preview(digest)
    repair = service.start_repair(actor="test", request_key="00000000-0000-4000-8000-000000001002",
                                  artifact_set_sha256=digest, plan_digest=preview["plan_digest"])
    service._run_download(repair.id, force=True, interrupt_after_bytes=1024 * 1024)
    assert service.get_operation(repair.id).state == "partial"
    assert service.read_verified_artifact(digest, artifact["sha256"], "weights.bin") == data
    service.close()
    restarted = ModelCacheService(sessions, service.root, reserve_bytes=0, fixture_sources=True)
    offsets = []
    original = restarted._open_source

    def open_source(spec, offset):
        offsets.append(offset)
        return original(spec, offset)

    monkeypatch.setattr(restarted, "_open_source", open_source)
    restarted.run_pending()
    assert restarted.get_operation(repair.id).state == "succeeded"
    assert offsets == [1024 * 1024]
    assert restarted.read_verified_artifact(digest, artifact["sha256"], "weights.bin") == data
    restarted.close()


def test_atomic_repair_keeps_path_and_open_reader_available(cache, tmp_path, monkeypatch):
    service, _ = cache
    data = b"immutable model"
    artifact = _artifact(tmp_path, data)
    downloaded = _download(service, [artifact], model_content_sha256="a" * 64,
                           request_key="00000000-0000-4000-8000-000000001003")
    digest = downloaded.artifact_set_sha256
    target, _, _ = service.verified_artifact_file(digest, artifact["sha256"], "weights.bin")
    original = __import__("os").replace
    replacements = []
    with target.open("rb") as reader:
        def replace(source, destination):
            assert target.read_bytes() == data
            assert destination == target
            original(source, destination)
            assert target.read_bytes() == data
            assert reader.read() == data
            replacements.append(destination)
        monkeypatch.setattr("vonk_control.model_cache.os.replace", replace)
        repair = service.start_repair(actor="test", request_key="00000000-0000-4000-8000-000000001004",
                                      artifact_set_sha256=digest,
                                      plan_digest=service.repair_preview(digest)["plan_digest"])
        service.run_pending()
    assert service.get_operation(repair.id).state == "succeeded"
    assert replacements == [target]


def test_reconciliation_reuses_verified_bytes_but_detects_same_size_mutation(cache, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from vonk_control.cached_file_verification import CachedFileVerifier

    service, _ = cache
    artifact = _artifact(tmp_path, b"good")
    downloaded = _download(service, [artifact], model_content_sha256="a" * 64,
                           request_key="00000000-0000-4000-8000-000000001005")
    monkeypatch.setattr("vonk_control.model_cache.verified_files", CachedFileVerifier())
    calls = []
    original = hashlib.sha256
    def sha256():
        calls.append(1)
        return original()
    monkeypatch.setattr("vonk_control.cached_file_verification.hashlib", SimpleNamespace(sha256=sha256))
    service.reconcile_storage()
    service.reconcile_storage()
    service.get_entry(downloaded.artifact_set_sha256)
    assert len(calls) == 1
    service._object_path(artifact["sha256"]).write_bytes(b"evil")
    service.reconcile_storage()
    assert service.get_entry(downloaded.artifact_set_sha256)["state"] == "needs-repair"


def test_repair_capacity_admission_preserves_verified_object(cache, tmp_path, monkeypatch):
    from collections import namedtuple
    service, _ = cache
    artifact = _artifact(tmp_path, b"model")
    downloaded = _download(service, [artifact], model_content_sha256="a" * 64,
                           request_key="00000000-0000-4000-8000-000000001006")
    digest = downloaded.artifact_set_sha256
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("vonk_control.model_cache.shutil.disk_usage", lambda _: usage(100, 100, 0))
    with pytest.raises(ModelCacheConflict, match="insufficient-reserved-storage"):
        service.start_repair(actor="test", request_key="00000000-0000-4000-8000-000000001007",
                             artifact_set_sha256=digest,
                             plan_digest=service.repair_preview(digest)["plan_digest"])
    assert service.read_verified_artifact(digest, artifact["sha256"], "weights.bin") == b"model"


def test_repair_checkpoint_requires_exact_nested_contract():
    from vonk_control.model_cache_contract import ModelCacheRepairCheckpoint
    valid = {"transfer_id": "a" * 32, "completed_objects": ["b" * 64]}
    assert ModelCacheRepairCheckpoint.model_validate(valid).model_dump(mode="json") == valid
    for invalid in ({"transfer_id": "a" * 32}, dict(valid, transfer_id="../object"),
                    dict(valid, completed_objects=[7]), dict(valid, legacy=True)):
        with pytest.raises(ValidationError):
            ModelCacheRepairCheckpoint.model_validate(invalid)


def test_cache_receipts_survive_restart_with_rolling_rate_and_bounded_writes(cache, tmp_path):
    from vonk_control.model_cache_progress import project_cache_progress
    service, sessions = cache
    clock = [datetime.now(UTC)]
    service._clock = lambda: clock[0]
    raw = _artifact(tmp_path, b"x" * 100)
    preview = service.download_preview(artifacts=[raw])
    operation = service.start_download(actor="test", request_key="00000000-0000-4000-8000-000000000991",
        plan_digest=preview["plan_digest"], artifacts=[raw])
    with sessions() as session:
        manifest = ArtifactSetManifest.from_document(session.get(ModelCacheOperation, operation.id).payload["manifest"])
    spec = manifest.artifacts[0]
    def checkpoint(owner, count, state="partial", force=False):
        owner._checkpoint_artifact(spec, operation_id=operation.id, set_digest=manifest.digest,
            actual_bytes=count, state=state, force_progress=force)
    checkpoint(service, 10)
    clock[0] += timedelta(seconds=0.1)
    checkpoint(service, 20)
    assert service.get_operation(operation.id).progress["downloaded_bytes"] == 10
    clock[0] += timedelta(seconds=0.9)
    checkpoint(service, 30)
    measured = service.get_operation(operation.id).progress["measurement"]
    assert measured["bytes_per_second"] == 20
    assert measured["members"][0]["bytes_per_second"] == 20
    restarted = ModelCacheService(sessions, service.root, reserve_bytes=0, fixture_sources=True, clock=lambda: clock[0])
    clock[0] += timedelta(seconds=1)
    checkpoint(restarted, 40)
    current = restarted.get_operation(operation.id).progress
    assert current["measurement"]["bytes_per_second"] == 10
    assert 10 < current["measurement"]["smoothed_bytes_per_second"] < 20
    assert project_cache_progress(current, clock[0])["members"][0]["observed_at"] == clock[0].isoformat()
    clock[0] += timedelta(seconds=0.1)
    checkpoint(restarted, 100, state="verifying", force=True)
    verifying = restarted.get_operation(operation.id).progress["measurement"]
    assert verifying["completed_bytes"] == 100
    assert verifying["phase"] == "verify"
    assert "eta_seconds" not in verifying
    assert "bytes_per_second" not in verifying
    restarted.close()


def test_cache_measurements_handle_unknown_total_and_observation_gap():
    from vonk_control.model_cache_progress import cache_progress, project_cache_progress
    def snapshot(count, total=100):
        return {"phase": "downloading", "completed_artifacts": 0, "total_artifacts": 1,
            "downloaded_bytes": count, "expected_bytes": total}
    first = cache_progress(snapshot(0), previous=None, now=NOW)
    second = cache_progress(snapshot(10), previous=first, now=NOW + timedelta(seconds=1))
    assert second["measurement"]["eta_seconds"] == 9
    restarted = cache_progress(snapshot(20), previous=second, now=NOW + timedelta(seconds=60))
    assert "bytes_per_second" not in restarted["measurement"]
    unknown = cache_progress(snapshot(30, None), previous=restarted, now=NOW + timedelta(seconds=61))
    assert unknown["measurement"]["bytes_per_second"] == 10
    assert "eta_seconds" not in unknown["measurement"]
    stale = project_cache_progress(unknown, NOW + timedelta(seconds=200))
    assert stale["activity"] == "possibly_stalled"
    assert "bytes_per_second" not in stale


def test_large_model_keeps_exact_aggregate_without_truncated_member_list(cache, tmp_path):
    from dataclasses import replace
    service, _ = cache
    raw = _artifact(tmp_path, b"x")
    manifest = service.resolve_artifact_set(artifacts=[raw])
    specs = tuple(replace(manifest.artifacts[0], key=f"file-{i}", sha256=f"{i:064x}") for i in range(1025))
    large = replace(manifest, artifacts=specs)
    transfer = {"artifacts": {spec.sha256: {"baseline_bytes": 0, "received_bytes": 0} for spec in specs}}
    progress = service._progress(large, phase="downloading", transfer=transfer)
    assert progress["measurement"]["total_items"] == 1025
    assert progress["measurement"]["total_bytes"] == 1025
    assert progress["measurement"]["members"] == []


class _FragmentedByteStream(httpx.SyncByteStream):
    def __init__(
        self,
        payload: bytes,
        *,
        fragment: int,
        fail_after: int | None = None,
    ) -> None:
        self._payload = payload
        self._fragment = fragment
        self._fail_after = fail_after

    def __iter__(self):
        sent = 0
        while sent < len(self._payload):
            if self._fail_after is not None and sent >= self._fail_after:
                raise httpx.ReadTimeout("read timeout")
            end = min(len(self._payload), sent + self._fragment)
            yield self._payload[sent:end]
            sent = end


def _http_artifact(payload: bytes, *, model: str = "b" * 64) -> dict[str, object]:
    return {
        "id": "weights",
        "path": "weights.bin",
        "kind": "http.file",
        "source": "https://example.test/weights.bin",
        "revision": "a" * 40,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "download_bytes": len(payload),
        "roles": ["model"],
        "model_content_sha256": model,
    }


def _http_cache_service(tmp_path: Path, sessions, handler, *, clock=None):
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    service = ModelCacheService(
        sessions,
        tmp_path / "http-nas-cache",
        reserve_bytes=0,
        http_client=client,
        fixture_sources=True,
        clock=clock,
    )
    return service, client


def _track_checkpoint_sessions(service: ModelCacheService):
    sampled = []
    writes = {"count": 0}
    real_checkpoint = service._checkpoint_artifact

    def wrapped(spec, **kwargs):
        sampled.append(kwargs.get("force_progress", True))
        real_session = service._session

        @contextmanager
        def tracking_session(*, write: bool = False):
            if write:
                writes["count"] += 1
            with real_session(write=write) as session:
                yield session

        service._session = tracking_session
        try:
            return real_checkpoint(spec, **kwargs)
        finally:
            service._session = real_session

    service._checkpoint_artifact = wrapped
    return sampled, writes


def test_fragmented_http_download_bounds_checkpoints(cache, tmp_path: Path, monkeypatch) -> None:
    _existing, sessions = cache
    payload = bytes(range(256)) * ((4 * _CHUNK_BYTES) // 256)
    fragment = 16 * 1024
    assert len(payload) // fragment > 20
    import vonk_control.model_cache as model_cache_mod

    fsyncs = {"count": 0}
    real_fsync = model_cache_mod.os.fsync

    def counted_fsync(fd):
        fsyncs["count"] += 1
        return real_fsync(fd)

    monkeypatch.setattr(model_cache_mod.os, "fsync", counted_fsync)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            stream=_FragmentedByteStream(payload, fragment=fragment),
        )

    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    sampled, writes = _track_checkpoint_sessions(service)
    try:
        operation = _download(
            service,
            [_http_artifact(payload)],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000992",
        )
        assert operation.state == "succeeded"
        assert operation.progress["downloaded_bytes"] == len(payload)
        sampled_progress = sampled.count(False)
        expected_chunks = (len(payload) + _CHUNK_BYTES - 1) // _CHUNK_BYTES
        assert sampled_progress <= expected_chunks
        assert sampled_progress < len(payload) // fragment
        assert writes["count"] <= 3
        assert fsyncs["count"] < len(payload) // fragment
        assert fsyncs["count"] <= expected_chunks + 8
    finally:
        service.close()
        client.close()


@pytest.mark.parametrize("mode", ["interrupt", "timeout"])
def test_fragmented_http_final_progress_is_durable_and_resumes(
    cache, tmp_path: Path, mode: str, monkeypatch
) -> None:
    _existing, sessions = cache
    payload = bytes(range(256)) * ((3 * _CHUNK_BYTES) // 256)
    durable_after = 2 * _CHUNK_BYTES + _CHUNK_BYTES // 2
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        range_header = request.headers.get("range")
        if range_header:
            start = int(range_header.removeprefix("bytes=").split("-", 1)[0])
            body = payload[start:]
            end = start + len(body) - 1 if body else start
            return httpx.Response(
                206,
                request=request,
                content=body,
                headers={"content-range": f"bytes {start}-{end}/{len(payload)}"},
            )
        if mode == "timeout":
            return httpx.Response(
                200,
                request=request,
                stream=_FragmentedByteStream(
                    payload, fragment=16 * 1024, fail_after=durable_after
                ),
            )
        return httpx.Response(
            200,
            request=request,
            stream=_FragmentedByteStream(payload, fragment=16 * 1024),
        )

    service, client = _http_cache_service(
        tmp_path, sessions, handler, clock=lambda: NOW
    )
    # Check ordering, not merely the eventual size visible in the page cache.
    import os

    synced_sizes: dict[int, int] = {}
    real_fsync = os.fsync
    real_checkpoint = service._checkpoint_artifact

    def synced(fd):
        result = real_fsync(fd)
        info = os.fstat(fd)
        synced_sizes[info.st_ino] = info.st_size
        return result

    def checked_checkpoint(spec, **kwargs):
        part = service._partial_path(kwargs["set_digest"], spec.sha256)
        assert kwargs["actual_bytes"] <= synced_sizes.get(part.stat().st_ino, 0)
        return real_checkpoint(spec, **kwargs)

    monkeypatch.setattr(os, "fsync", synced)
    monkeypatch.setattr(service, "_checkpoint_artifact", checked_checkpoint)
    artifact = _http_artifact(payload)
    try:
        preview = service.download_preview(
            model_content_sha256="b" * 64, artifacts=[artifact]
        )
        if mode == "interrupt":
            interrupted = service.start_download(
                actor="test",
                request_key="00000000-0000-4000-8000-000000000993",
                plan_digest=str(preview["plan_digest"]),
                model_content_sha256="b" * 64,
                artifacts=[artifact],
                interrupt_after_bytes=durable_after,
            )
            observed = interrupted
            assert observed.state == "partial"
        else:
            queued = service.start_download(
                actor="test",
                request_key="00000000-0000-4000-8000-000000000994",
                plan_digest=str(preview["plan_digest"]),
                model_content_sha256="b" * 64,
                artifacts=[artifact],
            )
            service.run_pending()
            observed = service.get_operation(queued.id)
            assert observed.state == "queued"
        part = (
            service.root
            / "partials"
            / str(observed.artifact_set_sha256)
            / f"{artifact['sha256']}.part"
        )
        assert part.stat().st_size == durable_after
        assert observed.progress["downloaded_bytes"] == durable_after
        assert observed.progress["downloaded_bytes"] == part.stat().st_size
        service.run_pending()
        resumed = service.get_operation(observed.id)
        assert resumed.state == "succeeded"
        assert resumed.progress["downloaded_bytes"] == len(payload)
        assert any(request.headers.get("range") == f"bytes={durable_after}-" for request in requests)
        assert (
            service.root
            / "objects"
            / str(artifact["sha256"])[0:2]
            / str(artifact["sha256"])
        ).read_bytes() == payload
    finally:
        service.close()
        client.close()


def test_fragmented_http_shutdown_preserves_sub_chunk_tail(cache, tmp_path: Path) -> None:
    _existing, sessions = cache
    payload = b"s" * (2 * _CHUNK_BYTES)
    fragments = []

    class ShutdownStream(httpx.SyncByteStream):
        def __iter__(self):
            fragments.append(1)
            yield payload[:4096]
            service._closed.set()
            fragments.append(2)
            yield payload[4096:8192]
            raise AssertionError("read another fragment after shutdown")

    def handler(request):
        return httpx.Response(200, request=request, stream=ShutdownStream())

    service, client = _http_cache_service(tmp_path, sessions, handler)
    sampled, _writes = _track_checkpoint_sessions(service)
    artifact = _http_artifact(payload)
    try:
        preview = service.download_preview(model_content_sha256="b" * 64, artifacts=[artifact])
        operation = service.start_download(
            actor="test", request_key="00000000-0000-4000-8000-000000000995",
            plan_digest=preview["plan_digest"], model_content_sha256="b" * 64,
            artifacts=[artifact],
        )
        service.run_pending()
        observed = service.get_operation(operation.id)
        assert observed.state == "partial"
        assert fragments == [1, 2]
        assert sampled == [True]
        assert observed.progress["downloaded_bytes"] == 8192
        part = service._partial_path(observed.artifact_set_sha256, artifact["sha256"])
        assert part.read_bytes() == payload[:8192]
    finally:
        service.close()
        client.close()


@pytest.mark.parametrize("ending", ["complete", "truncated", "oversized", "fsync_error"])
def test_fragmented_http_tail_checkpoint_never_exceeds_synced_bytes(
    cache, tmp_path, monkeypatch, ending
):
    import os
    import stat

    _existing, sessions = cache
    payload = b"t" * 8192
    body = payload[:4096] if ending == "truncated" else payload
    if ending == "oversized":
        body += b"extra"
    synced_sizes = {}
    checkpoints = []
    real_fsync = os.fsync

    def sync(fd):
        info = os.fstat(fd)
        if ending == "fsync_error" and stat.S_ISREG(info.st_mode) and info.st_size:
            raise OSError(errno.EIO, "test durability failure")
        result = real_fsync(fd)
        synced_sizes[info.st_ino] = info.st_size
        return result

    monkeypatch.setattr(os, "fsync", sync)

    def handler(request):
        return httpx.Response(
            200, request=request, stream=_FragmentedByteStream(body, fragment=4096)
        )

    service, client = _http_cache_service(tmp_path, sessions, handler, clock=lambda: NOW)
    real_checkpoint = service._checkpoint_artifact

    def checkpoint(spec, **kwargs):
        part = service._partial_path(kwargs["set_digest"], spec.sha256)
        count = kwargs["actual_bytes"]
        assert count <= synced_sizes.get(part.stat().st_ino, 0)
        checkpoints.append(count)
        return real_checkpoint(spec, **kwargs)

    monkeypatch.setattr(service, "_checkpoint_artifact", checkpoint)
    try:
        operation = _download(
            service, [_http_artifact(payload)], model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000996",
        )
        expected = 0 if ending == "fsync_error" else min(len(body), len(payload))
        assert checkpoints[-1] == expected
        assert operation.progress["downloaded_bytes"] == expected
        assert (operation.state == "succeeded") == (ending == "complete")
        if ending != "complete":
            assert not service._object_path(hashlib.sha256(payload).hexdigest()).exists()
    finally:
        service.close()
        client.close()


def test_slow_fragmented_http_checkpoints_before_one_mib(cache, tmp_path, monkeypatch):
    import vonk_control.model_cache as model_cache_mod

    _existing, sessions = cache
    payload = b"s" * 8192
    elapsed = [0.0]
    monkeypatch.setattr(model_cache_mod.time, "monotonic", lambda: elapsed[0])
    checkpoints = []

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            yield payload[:4096]
            assert checkpoints == []
            elapsed[0] = 1.0
            yield payload[4096:]
            assert checkpoints == [8192]

    def handler(request):
        return httpx.Response(200, request=request, stream=SlowStream())

    service, client = _http_cache_service(tmp_path, sessions, handler, clock=lambda: NOW)
    real_checkpoint = service._checkpoint_artifact

    def checkpoint(spec, **kwargs):
        if not kwargs.get("force_progress", True):
            checkpoints.append(kwargs["actual_bytes"])
        return real_checkpoint(spec, **kwargs)

    monkeypatch.setattr(service, "_checkpoint_artifact", checkpoint)
    try:
        operation = _download(
            service, [_http_artifact(payload)], model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000000997",
        )
        assert operation.state == "succeeded"
    finally:
        service.close()
        client.close()


@pytest.mark.parametrize("complete_tail", [False, True])
def test_download_resyncs_retained_bytes_after_disk_failure(
    cache, tmp_path, monkeypatch, complete_tail
):
    import os
    import stat

    _existing, sessions = cache
    payload = b"r" * 8192
    fail_sync = [True]
    requests = []
    synced_sizes = {}
    real_fsync = os.fsync

    def sync(fd):
        info = os.fstat(fd)
        if fail_sync[0] and stat.S_ISREG(info.st_mode) and info.st_size:
            raise OSError(errno.EIO, "test durability failure")
        result = real_fsync(fd)
        synced_sizes[info.st_ino] = info.st_size
        return result

    monkeypatch.setattr(os, "fsync", sync)

    def handler(request):
        requests.append(request)
        if "range" in request.headers:
            start = int(request.headers["range"].removeprefix("bytes=").split("-")[0])
            return httpx.Response(
                206, request=request, content=payload[start:],
                headers={"content-range": f"bytes {start}-{len(payload)-1}/{len(payload)}"},
            )
        return httpx.Response(
            200, request=request,
            stream=_FragmentedByteStream(
                payload, fragment=4096, fail_after=None if complete_tail else 4096
            ),
        )

    service, client = _http_cache_service(tmp_path, sessions, handler, clock=lambda: NOW)
    artifact = _http_artifact(payload)
    publish = service._publish_object

    def checked_publish(spec, part):
        assert synced_sizes.get(part.stat().st_ino, 0) == len(payload)
        return publish(spec, part)

    monkeypatch.setattr(service, "_publish_object", checked_publish)
    try:
        def download(number):
            return _download(
                service, [artifact], model_content_sha256="b" * 64,
                request_key=f"00000000-0000-4000-8000-{number:012d}",
            )

        failed = download(998)
        assert failed.state == "failed"
        assert "test durability failure" in failed.last_error
        assert failed.progress["downloaded_bytes"] == 0
        # A fresh attempt must not publish or request a range while retained
        # bytes still cannot be synced, even if their hash already matches.
        failed_again = download(999)
        assert failed_again.state == "failed"
        assert len(requests) == 1
        fail_sync[0] = False
        recovered = download(1000)
        assert recovered.state == "succeeded"
        if complete_tail:
            assert len(requests) == 1
        else:
            assert requests[-1].headers["range"] == "bytes=4096-"
        assert service._object_path(artifact["sha256"]).read_bytes() == payload
    finally:
        service.close()
        client.close()
