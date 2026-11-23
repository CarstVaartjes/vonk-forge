"""Model cache: resolve download."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import func, select
from vonk_agent_protocol import LifecycleState
from vonk_control.model_cache.artifacts import ArtifactSetManifest, ArtifactSpec
from vonk_control.model_cache.contract import (
    ModelCacheDownloadResult,
)
from vonk_control.models.cache import (
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeBuild,
)
from vonk_control.models.catalog import (
    CatalogDocument,
    CatalogDocumentRevision,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.run_switch_operations.artifact_inspection import (
    DatabaseRunSwitchArtifactInspector,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256
from vonk_forge_contracts.model import ModelReference

from .display_fixtures import reword
from .model_cache_support import (
    NOW,
    _artifact,
    _canonical_model,
    _canonical_recipe,
    _download,
)


def test_resolve_latest_cached_returns_the_head_even_when_an_older_revision_is_cached(
    cache, monkeypatch: pytest.MonkeyPatch
):
    service, sessions = cache
    model = _canonical_model(
        publisher="vonk-forge",
        slug="resolver-model",
        file_id="weights",
        file_digest="1" * 64,
    )
    model_digest = document_sha256(model.model_dump(mode="json"))
    newer_model = _canonical_model(
        publisher="vonk-forge",
        slug="resolver-model",
        file_id="weights",
        file_digest="2" * 64,
    )
    newer_model_digest = document_sha256(newer_model.model_dump(mode="json"))
    old_document = _canonical_recipe(model_digest)
    old_document["identity"] = {"publisher": "vonk-forge", "slug": "resolver-recipe"}
    old_recipe = RecipeDefinition.model_validate(old_document)
    new_document = json.loads(json.dumps(old_document))
    new_document["models"][0]["model"]["content_sha256"] = newer_model_digest
    reword(new_document, " newer")
    new_recipe = RecipeDefinition.model_validate(new_document)
    model_root = CatalogDocument(
        id="00000000-0000-0000-0000-000000000101",
        kind="model",
        publisher="vonk-forge",
        slug="resolver-model",
        title="Resolver model",
        created_by="test",
        created_at=NOW,
        updated_at=NOW,
    )
    recipe_root = CatalogDocument(
        id="00000000-0000-0000-0000-000000000102",
        kind="recipe",
        publisher="vonk-forge",
        slug="resolver-recipe",
        title="Resolver recipe",
        created_by="test",
        created_at=NOW,
        updated_at=NOW,
    )
    old_digest = document_sha256(old_recipe.model_dump(mode="json"))
    new_digest = document_sha256(new_recipe.model_dump(mode="json"))
    old_revision = CatalogDocumentRevision(
        id="00000000-0000-0000-0000-000000000103",
        document_id=recipe_root.id,
        kind="recipe",
        publisher=recipe_root.publisher,
        slug=recipe_root.slug,
        revision_number=1,
        schema_version=2,
        state="active",
        document=old_recipe.model_dump(mode="json"),
        content_digest=old_digest,
        execution_key="a" * 64,
        projected={},
        created_by="test",
        created_at=NOW,
    )
    new_revision = CatalogDocumentRevision(
        id="00000000-0000-0000-0000-000000000104",
        document_id=recipe_root.id,
        kind="recipe",
        publisher=recipe_root.publisher,
        slug=recipe_root.slug,
        revision_number=2,
        schema_version=2,
        state="active",
        document=new_recipe.model_dump(mode="json"),
        content_digest=new_digest,
        execution_key="b" * 64,
        projected={},
        created_by="test",
        created_at=NOW,
    )
    model_revision = CatalogDocumentRevision(
        id="00000000-0000-0000-0000-000000000105",
        document_id=model_root.id,
        kind="model",
        publisher=model_root.publisher,
        slug=model_root.slug,
        revision_number=1,
        schema_version=2,
        state="failed",
        document=model.model_dump(mode="json"),
        content_digest=model_digest,
        projected={},
        created_by="test",
        created_at=NOW,
    )
    newer_model_revision = CatalogDocumentRevision(
        id="00000000-0000-0000-0000-000000000109",
        document_id=model_root.id,
        kind="model",
        publisher=model_root.publisher,
        slug=model_root.slug,
        revision_number=2,
        schema_version=2,
        state="active",
        document=newer_model.model_dump(mode="json"),
        content_digest=newer_model_digest,
        projected={},
        created_by="test",
        created_at=NOW,
    )
    build = RecipeBuild(
        id="00000000-0000-0000-0000-000000000106",
        recipe_revision_id=old_revision.id,
        builder_node_id="resolver-builder",
        source_bundle_sha256="2" * 64,
        build_input_sha256="3" * 64,
        state="succeeded",
        policy_report={},
        plan={},
        image_digest="sha256:" + "6" * 64,
        oci_layout_sha256="8" * 64,
        image_bytes=17,
        error=None,
        created_at=NOW,
        updated_at=NOW,
    )
    with sessions.begin() as session:
        session.add_all(
            [
                model_root,
                recipe_root,
                model_revision,
                newer_model_revision,
                old_revision,
                new_revision,
                AgentNode(node_id="resolver-builder", state="active"),
                build,
            ]
        )

    manifest = ArtifactSetManifest(
        model_content_sha256=model_digest,
        recipe_revision_sha256=None,
        model_content_digests=(model_digest,),
        artifacts=(
            ArtifactSpec(
                key="artifact-111111111111-weights",
                artifact_id="weights",
                path="weights.safetensors",
                kind="huggingface.file",
                repository="https://huggingface.co/vonk-forge/resolver-model",
                source=(
                    "https://huggingface.co/vonk-forge/resolver-model/resolve/"
                    + "0" * 40
                    + "/weights.safetensors"
                ),
                revision="0" * 40,
                sha256="1" * 64,
                expected_bytes=3,
                roles=("weights",),
                model_content_sha256=model_digest,
            ),
        ),
        model_definition_ref=ModelReference(
            publisher="vonk-forge",
            slug="resolver-model",
            content_sha256=model_digest,
        ),
    )
    artifact = manifest.artifacts[0]
    model_cache = ModelCacheSet(
        artifact_set_sha256=manifest.digest,
        schema_version=2,
        model_content_sha256=model_digest,
        recipe_revision_sha256=None,
        manifest=manifest.document(),
        expected_bytes=3,
        verified_bytes=3,
        state="cached",
        protected=False,
        protected_reasons=[],
        created_at=NOW,
        updated_at=NOW,
        verified_at=NOW,
        last_accessed_at=NOW,
        last_error=None,
    )
    model_object = service.root / "objects" / artifact.sha256[:2] / artifact.sha256
    model_object.parent.mkdir(parents=True)
    model_object.write_bytes(b"one")
    image_object = service.root.parent / "runtime-images" / build.oci_layout_sha256
    assert build.image_bytes is not None
    image_object.write_bytes(b"x" * build.image_bytes)
    with sessions.begin() as session:
        session.add(model_cache)

    # The catalog head is current: an older cached revision never stands in for
    # it. The head comes back with its missing cache reported.
    resolved = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe", model_variant="fp16"
    )
    assert resolved.recipe.recipe_revision_id == new_revision.id
    assert resolved.recipe.update_available is False
    assert resolved.recipe.cached is False
    assert "recipe-not-cached" in resolved.blockers

    # An exact older revision is told apart from the head.
    older = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert older.recipe.recipe_revision_id == old_revision.id
    assert older.recipe.update_available is True
    assert older.recipe.cached is True
    assert older.model.content_sha256 == model_digest

    # An explicitly selected exact revision is never silently replaced by an
    # older cached one: the newer head comes back with its missing cache
    # reported instead.
    exact = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=new_revision.id,
    )
    assert exact.recipe.recipe_revision_id == new_revision.id
    assert exact.recipe.cached is False
    assert exact.recipe.cache_state == "missing"
    assert exact.recipe.content_sha256 == new_digest
    assert "recipe-not-cached" in exact.blockers

    real_open = os.open

    def deny_model_object(path, *args, **kwargs):
        if Path(path) == model_object:
            raise PermissionError("model object is inaccessible")
        return real_open(path, *args, **kwargs)

    def deny_runtime_archive(_digest: str, _size: int) -> bool:
        raise PermissionError("runtime archive is inaccessible")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "open", deny_model_object)
        observed = service.resolve_latest_cached(
            recipe_identity="vonk-forge/resolver-recipe",
            model_variant="fp16",
            exact_revision_id=old_revision.id,
        )
        assert not (observed.model.cached and observed.recipe.cached)
    restored = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert not restored.blockers
    with monkeypatch.context() as scoped:
        scoped.setattr(
            service,
            "_runtime_archive_available",
            deny_runtime_archive,
        )
        observed = service.resolve_latest_cached(
            recipe_identity="vonk-forge/resolver-recipe",
            model_variant="fp16",
            exact_revision_id=old_revision.id,
        )
        assert not (observed.model.cached and observed.recipe.cached)

    model_object.unlink()
    missing_model_bytes = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert missing_model_bytes.recipe.cached is True
    assert missing_model_bytes.model.cached is False
    assert "model-not-cached" in missing_model_bytes.blockers
    model_object.write_bytes(b"one")

    image_object.unlink()
    missing_image_bytes = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert missing_image_bytes.recipe.recipe_revision_id == old_revision.id
    assert missing_image_bytes.recipe.cached is False
    assert "recipe-not-cached" in missing_image_bytes.blockers
    image_object.write_bytes(b"x" * build.image_bytes)

    with sessions.begin() as session:
        session.delete(model_cache)
    image_only = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert image_only.recipe.cached is True
    assert image_only.model.cached is False
    assert image_only.resources.additional_disk_bytes is None

    with sessions.begin() as session:
        session.delete(build)
    missing = service.resolve_latest_cached(
        recipe_identity="vonk-forge/resolver-recipe",
        model_variant="fp16",
        exact_revision_id=old_revision.id,
    )
    assert missing.recipe.recipe_revision_id == old_revision.id
    assert missing.recipe.expected_bytes is None
    assert missing.model.expected_bytes is None
    assert missing.resources.additional_disk_bytes is None


@pytest.mark.parametrize("shared_object", [False, True])
@pytest.mark.parametrize("partial_checkpoint", [False, True])
def test_canonical_dependency_closure_reaches_run_switch(
    cache,
    shared_object: bool,
    partial_checkpoint: bool,
) -> None:
    service, sessions = cache
    companion = _canonical_model(
        publisher="vonk-forge",
        slug="companion",
        file_id="encoder",
        file_digest=("3" if shared_object else "2") * 64,
    )
    companion_document = companion.model_dump(mode="json")
    companion_document["files"].append(
        {
            "id": "empty-config",
            "path": "config/empty.txt",
            "sha256": hashlib.sha256(b"").hexdigest(),
            "size_bytes": 0,
            "roles": ["config"],
        }
    )
    companion = ModelDefinition.model_validate(companion_document)
    companion_digest = document_sha256(companion.model_dump(mode="json"))
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
    primary_digest = document_sha256(primary.model_dump(mode="json"))
    recipe_document = _canonical_recipe(primary_digest)
    models = recipe_document["models"]
    assert isinstance(models, list) and models
    selection = models[0]
    assert isinstance(selection, dict)
    model_reference = selection["model"]
    assert isinstance(model_reference, dict)
    model_reference["slug"] = "primary"
    recipe = RecipeDefinition.model_validate(recipe_document)
    recipe_digest = document_sha256(recipe.model_dump(mode="json"))
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
    assert manifest.model_content_digests == tuple(
        sorted((primary_digest, companion_digest))
    )
    assert {item.model_content_sha256 for item in manifest.artifacts} == {
        primary_digest,
        companion_digest,
    }

    inspector = DatabaseRunSwitchArtifactInspector(service)
    if partial_checkpoint:
        for spec in manifest.artifacts:
            if spec.expected_bytes:
                partial = service._partial_path(manifest.digest, spec.sha256)
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_bytes(b"x")
    with sessions() as session:
        inspection = inspector.inspect(
            session,
            model_content_sha256=primary_digest,
            recipe_revision_id="00000000-0000-0000-0000-000000000053",
            node_ids=("spk_" + "9" * 32,),
            now=NOW,
        )
    # Real ModelDefinition -> cache service -> Run/Switch, including companion
    # identity, empty support files and one-copy accounting of shared bytes.
    assert inspection.dependency_model_content_sha256 == (companion_digest,)
    assert inspection.artifact_set_sha256 == manifest.digest
    assert inspection.artifact_set_bytes == (3 if shared_object else 6)
    assert inspection.required_bytes == inspection.artifact_set_bytes
    assert inspection.missing_nas_bytes == inspection.artifact_set_bytes


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
    assert entry["expected_bytes"] == len(
        b"primary model bytestokenizer auxiliary bytes"
    )
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
    assert (
        service.read_verified_artifact(
            first.artifact_set_sha256 or "",
            str(primary["sha256"]),
            "weights.bin",
            offset=8,
            maximum_bytes=6,
        )
        == b"model "
    )

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
        # SQL owns membership: two objects, one membership row per set.
        assert (
            session.scalar(select(func.count()).select_from(ModelCacheSetArtifact)) == 4
        )
    assert service.storage_summary().unique_used_bytes == len(
        b"primary model bytes"
    ) + len(b"tokenizer auxiliary bytes")


def test_upstream_revision_downloads_only_new_files_and_reuses_the_rest(
    cache, tmp_path: Path
) -> None:
    """A model updated upstream shares most files with the cached revision.

    Unchanged objects are reused by digest without fetching or re-hashing
    them; only the changed and added files are downloaded, and the new set
    is complete and describable for the compiler.
    """

    service, _sessions = cache
    model = "a" * 64
    shard = _artifact(tmp_path, b"unchanged shard bytes", model_content_sha256=model)
    config = _artifact(
        tmp_path,
        b"old config",
        artifact_id="config",
        path="config.json",
        model_content_sha256=model,
    )
    first = _download(
        service,
        [shard, config],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000031",
    )
    assert first.state == "succeeded"
    # The unchanged shard cannot be fetched again: its source is gone.
    Path(str(shard["source"]).removeprefix("file://")).unlink()

    updated = "b" * 64
    (tmp_path / "update").mkdir()
    new_config = _artifact(
        tmp_path / "update",
        b"new config!",
        artifact_id="config",
        path="config.json",
        model_content_sha256=updated,
    )
    added = _artifact(
        tmp_path / "update",
        b"added tokenizer",
        artifact_id="tokenizer",
        path="tokenizer.json",
        model_content_sha256=updated,
    )
    files = [dict(shard, model_content_sha256=updated), new_config, added]
    preview = service.download_preview(model_content_sha256=updated, artifacts=files)
    assert preview["artifact_set_sha256"] != first.artifact_set_sha256
    assert preview["new_bytes"] == len(b"new config!") + len(b"added tokenizer")
    second = _download(
        service,
        files,
        model_content_sha256=updated,
        request_key="00000000-0000-4000-8000-000000000032",
    )
    assert second.state == "succeeded", second.last_error
    entry = service.get_entry(second.artifact_set_sha256 or "")
    assert entry["coverage"] == "complete"
    descriptors = service.resolve_verified_artifact_set(
        second.artifact_set_sha256 or ""
    )
    assert {
        (item["model_content_sha256"], item["file_id"]) for item in descriptors
    } == {
        (updated, "weights"),
        (updated, "config"),
        (updated, "tokenizer"),
    }


@pytest.mark.parametrize(
    "invalid_result",
    [
        {"schema_version": 2, "artifact_set_sha256": "a" * 64},
        {"schema_version": 2, "removed_entries": [], "reclaimed_bytes": 0},
        "not a result document",
    ],
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_cache_operation_reads_rebuild_malformed_or_wrong_kind_results(
    cache,
    tmp_path: Path,
    invalid_result: object,
) -> None:
    """A damaged result is re-derived from the operation's own evidence."""

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
    rebuilt = service.get_operation(operation.id)
    assert rebuilt.state == "succeeded"
    assert isinstance(rebuilt.result, ModelCacheDownloadResult)
    assert rebuilt.result.artifact_set_sha256 == operation.artifact_set_sha256


def test_one_set_with_shared_digest_counts_one_physical_payload(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "9" * 64
    primary = _artifact(tmp_path, b"shared payload", model_content_sha256=model)
    alias = dict(
        primary,
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


@pytest.mark.parametrize(
    "stored_state",
    [
        "verified",
        "missing",
        "symlink",
        "truncated",
        "unreadable",
        "close_unavailable",
    ],
)
def test_preview_uses_durable_verified_cache_metadata_without_reading_model_bytes(
    cache,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored_state: str,
) -> None:
    service, _sessions = cache
    model = "7" * 64
    data = b"verified cache model bytes"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)
    _download(
        service,
        [artifact],
        model_content_sha256=model,
        request_key="ea2c5c20-038a-4a0d-bdaa-40d756fb01bb",
    )
    spec = service.resolve_artifact_set(
        model_content_sha256=model,
        artifacts=[artifact],
    ).artifacts[0]
    path = service._object_path(spec.sha256)
    if stored_state == "missing":
        path.unlink()
    elif stored_state == "truncated":
        path.write_bytes(data[:-1])
    elif stored_state == "symlink":
        other = tmp_path / "outside-cache.bin"
        other.write_bytes(data)
        path.unlink()
        path.symlink_to(other)

    def reject_byte_scan(*_args, **_kwargs):
        raise AssertionError("preview reread cached model bytes")

    monkeypatch.setattr(service, "_verify_file", reject_byte_scan)
    open_file = os.open
    close_file = os.close
    if stored_state == "unreadable":

        def deny_cache_object(file, *args, **kwargs):
            if file == path:
                raise PermissionError(errno.EACCES, "cache object is unreadable")
            return open_file(file, *args, **kwargs)

        monkeypatch.setattr(os, "open", deny_cache_object)

    if stored_state == "close_unavailable":
        observed_fds: set[int] = set()

        def observe_object(file, *args, **kwargs):
            descriptor = open_file(file, *args, **kwargs)
            if file == path:
                observed_fds.add(descriptor)
            return descriptor

        def unavailable_close(descriptor):
            close_file(descriptor)
            if descriptor in observed_fds:
                observed_fds.remove(descriptor)
                raise OSError("object close observation unavailable")

        monkeypatch.setattr(os, "open", observe_object)
        monkeypatch.setattr(os, "close", unavailable_close)

    preview = service.download_preview(model_content_sha256=model, artifacts=[artifact])
    expected = len(data) if stored_state in ("verified", "receipt_missing") else 0
    assert preview["already_cached_bytes"] == expected
    assert preview["new_bytes"] == len(data) - expected
    if stored_state in ("unreadable", "close_unavailable"):
        admitted = service.start_download(
            actor="test",
            request_key=str(uuid.uuid4()),
            model_content_sha256=model,
            artifacts=[artifact],
        )
        assert admitted.state == LifecycleState.QUEUED
        monkeypatch.setattr(os, "open", open_file)
        monkeypatch.setattr(os, "close", close_file)
        service.run_pending()
        assert service.get_operation(admitted.id).state == LifecycleState.SUCCEEDED
        recovered = service.download_preview(
            model_content_sha256=model, artifacts=[artifact]
        )
        assert recovered["already_cached_bytes"] == len(data)
        assert recovered["new_bytes"] == 0


def test_admission_checks_storage_size_after_the_read_transaction_closes(
    cache, tmp_path: Path, monkeypatch
) -> None:
    """The SQL read transaction never spans the managed-storage size check.

    It fails on the wrong implementation where ``_managed_cached_objects``
    opens and stats stored objects while its ``with self._session()`` read
    transaction is still open, which puts a PostgreSQL lock edge on a
    filesystem check. The service's own session helper is instrumented, so the
    assertion observes a checked-out transaction rather than mocking it away.
    """

    service, _sessions = cache
    model = "3" * 64
    artifact = _artifact(tmp_path, b"admitted bytes", model_content_sha256=model)
    _download(
        service,
        [artifact],
        model_content_sha256=model,
        request_key="00000000-0000-4000-8000-000000000018",
    )
    sessions_open = 0
    checked_at_size_check: list[int] = []
    original_session = service._session

    @contextmanager
    def counting_session(*, write: bool = False):
        nonlocal sessions_open
        sessions_open += 1
        try:
            with original_session(write=write) as session:
                yield session
        finally:
            sessions_open -= 1

    original_fstat = os.fstat

    def recording_fstat(fd: int):
        checked_at_size_check.append(sessions_open)
        return original_fstat(fd)

    monkeypatch.setattr(service, "_session", counting_session)
    monkeypatch.setattr(os, "fstat", recording_fstat)
    preview = service.download_preview(model_content_sha256=model, artifacts=[artifact])
    assert preview["already_cached_bytes"] == len(b"admitted bytes")
    assert checked_at_size_check == [0]


def test_operation_transfer_progress_counts_only_missing_objects(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "7" * 64
    cached = _artifact(
        tmp_path, b"cached", artifact_id="cached", model_content_sha256=model
    )
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
        model_content_sha256=model,
        artifacts=[cached, missing],
    )
    assert operation.progress["expected_bytes"] == len(b"new object")
    service.run_pending()
    operation = service.get_operation(operation.id)
    assert operation.progress["downloaded_bytes"] == len(b"new object")
    assert operation.progress["expected_bytes"] == len(b"new object")


def test_preview_hashes_the_set_identity_once_whatever_the_file_count(
    cache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production 2026-11-15: a preview re-hashed the whole manifest once per
    missing file, so a many-file model cost seconds and every Library
    assessment ran out of its read budget. The work must not grow with files."""
    service, _sessions = cache
    model = "8" * 64
    shards = [f"shard {index}".encode() for index in range(40)]
    files = [
        _artifact(
            tmp_path,
            shard,
            artifact_id=f"shard-{index}",
            path=f"shard-{index}.bin",
            model_content_sha256=model,
        )
        for index, shard in enumerate(shards)
    ]
    hashed = 0
    identity = ArtifactSetManifest.identity

    def counted(self: ArtifactSetManifest):
        nonlocal hashed
        hashed += 1
        return identity(self)

    monkeypatch.setattr(ArtifactSetManifest, "identity", counted)
    preview = service.download_preview(model_content_sha256=model, artifacts=files)
    assert preview["artifact_count"] == len(files)
    assert preview["new_bytes"] == sum(len(shard) for shard in shards)
    assert hashed <= 2


def test_download_mutation_is_queued_until_the_controller_worker_runs(
    cache, tmp_path: Path
) -> None:
    service, _sessions = cache
    model = "1" * 64
    data = b"queued payload"
    artifact = _artifact(tmp_path, data, model_content_sha256=model)

    operation = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000014",
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
