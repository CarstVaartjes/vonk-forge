from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import DistributionAssignment, DistributionObject
from vonk_control.distribution import (
    CompositeVerifiedObjectSource,
    DistributionError,
    DistributionService,
    FilesystemVerifiedObjectSource,
    ModelCacheVerifiedObjectSource,
    RecipeBuildVerifiedObjectSource,
)
from vonk_control.model_cache import (
    ModelCacheConflict,
    ModelCacheResolutionError,
    ModelCacheService,
)
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    FleetProfile,
    RecipeBuild,
)

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32


@pytest.fixture
def controller(tmp_path: Path):
    database = tmp_path / "controller.sqlite"
    engine = create_engine(f"sqlite:///{database}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(node_id=NODE_A, state="active", capabilities=[]),
                AgentNode(node_id=NODE_B, state="active", capabilities=[]),
            ]
        )
    cache = ModelCacheService(
        sessions,
        tmp_path / "nas-cache",
        reserve_bytes=0,
        fixture_sources=True,
    )
    yield database, engine, sessions, cache
    engine.dispose()


def _restart_controller(database: Path, root: Path):
    engine = create_engine(f"sqlite:///{database}", connect_args={"check_same_thread": False})
    sessions = sessionmaker(engine, expire_on_commit=False)
    cache = ModelCacheService(sessions, root, reserve_bytes=0, fixture_sources=True)
    return engine, sessions, cache


def _artifact(
    root: Path,
    artifact_id: str,
    path: str,
    payload: bytes,
    *,
    model_version_sha256: str,
    token: str,
) -> dict[str, object]:
    source = root / f"{artifact_id}.source"
    source.write_bytes(payload)
    return {
        "id": artifact_id,
        "path": path,
        "kind": "file",
        "source": source.as_uri(),
        "repository": f"org/model?access_token={token}",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "download_bytes": len(payload),
        "roles": ["model" if artifact_id == "weights" else "auxiliary"],
        "model_version_sha256": model_version_sha256,
    }


def _download(
    cache: ModelCacheService,
    artifacts: list[dict[str, object]],
    *,
    model_version_sha256: str,
    recipe_revision_sha256: str,
    request_key: str,
):
    preview = cache.download_preview(
        model_version_sha256=model_version_sha256,
        recipe_revision_sha256=recipe_revision_sha256,
        artifacts=artifacts,
    )
    operation = cache.start_download(
        actor="acceptance",
        request_key=request_key,
        plan_digest=str(preview["plan_digest"]),
        model_version_sha256=model_version_sha256,
        recipe_revision_sha256=recipe_revision_sha256,
        artifacts=artifacts,
    )
    assert operation.state == "queued"
    assert cache.run_pending() == 1
    operation = cache.get_operation(operation.id)
    assert operation.state == "succeeded"
    return str(operation.artifact_set_sha256)


def _seed_recipe_reference(
    sessions,
    *,
    model_version_sha256: str,
    recipe_revision_sha256: str,
    profile: bool,
) -> str:
    recipe_id = str(uuid4())
    revision_id = str(uuid4())
    publisher = "vonk-forge"
    slug = f"acceptance-{recipe_id[:8]}"
    document = {
        "schema_version": 2,
        "kind": "recipe",
        "identity": {"publisher": publisher, "slug": slug},
        "models": [
            {
                "id": "primary",
                "model": {
                    "kind": "model",
                    "publisher": publisher,
                    "slug": "acceptance-model",
                    "content_sha256": model_version_sha256,
                },
                "files": [],
            }
        ],
    }
    recipe_digest = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=recipe_id,
                kind="recipe",
                publisher=publisher,
                slug=slug,
                title="Acceptance recipe",
                created_by="acceptance",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id=revision_id,
                document_id=recipe_id,
                kind="recipe",
                publisher=publisher,
                slug=slug,
                revision_number=1,
                state="active",
                schema_version=2,
                document=document,
                content_digest=recipe_digest,
                projected={},
                created_by="acceptance",
                created_at=NOW,
            )
        )
        if profile:
            session.add(
                FleetProfile(
                    name=f"acceptance-{recipe_id[:8]}",
                    description="",
                    installation_policy="keep-cached",
                    assignments=[{"recipe_revision_id": revision_id}],
                    scope=[],
                    labels={},
                    favorite=False,
                    created_by="acceptance",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    return revision_id


def _prebuilt_oci_source(root: Path, payload: bytes):
    root.mkdir()
    archive_sha256 = hashlib.sha256(payload).hexdigest()
    image_digest = "sha256:" + hashlib.sha256(b"prebuilt-image").hexdigest()
    (root / archive_sha256).write_bytes(payload)
    return (
        FilesystemVerifiedObjectSource(
            root,
            runtime_images={archive_sha256: image_digest},
        ),
        archive_sha256,
        image_digest,
    )


def _local_build_oci_source(sessions, root: Path, revision_id: str, payload: bytes):
    root.mkdir()
    archive_sha256 = hashlib.sha256(payload).hexdigest()
    image_digest = "sha256:" + hashlib.sha256(b"locally-built-image").hexdigest()
    (root / archive_sha256).write_bytes(payload)
    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=str(uuid4()),
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256=hashlib.sha256(b"source-bundle").hexdigest(),
                build_input_sha256=hashlib.sha256(b"build-input").hexdigest(),
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=image_digest,
                oci_layout_sha256=archive_sha256,
                image_bytes=len(payload),
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return RecipeBuildVerifiedObjectSource(sessions, root), archive_sha256, image_digest


def _assignment(
    *,
    node_id: str,
    plan_digest: str,
    artifact_set_sha256: str,
    model_objects: tuple[DistributionObject, ...],
    archive_sha256: str,
    archive_bytes: int,
    image_digest: str,
) -> DistributionAssignment:
    return DistributionAssignment.parse(
        {
            "schema_version": 2,
            "assignment_id": str(uuid4()),
            "plan_digest": plan_digest,
            "generation": 1,
            "node_id": node_id,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "model_artifact_set_sha256": artifact_set_sha256,
            "objects": [
                *(item.to_mapping() for item in model_objects),
                {
                    "name": "runtime.oci.tar",
                    "sha256": archive_sha256,
                    "bytes": archive_bytes,
                    "kind": "oci-archive",
                },
            ],
            "oci_image_digest": image_digest,
            "oci_archive_sha256": archive_sha256,
        }
    )


def _read_object(
    service: DistributionService,
    *,
    node_id: str,
    plan_digest: str,
    digest: str,
) -> bytes:
    _assignment_value, _object, opened = service.open_object(
        node_id=node_id,
        plan_digest=plan_digest,
        digest=digest,
    )
    try:
        return opened.stream.read()
    finally:
        opened.stream.close()


def test_persisted_models_and_prebuilt_oci_are_reused_a_b_a_without_hf_credentials(
    controller, tmp_path: Path
) -> None:
    database, engine, sessions, cache = controller
    model_pin = "1" * 64
    recipe_pin = "2" * 64
    token = "hf_acceptance_secret"
    model_payload = b"verified model weights"
    auxiliary_payload = b"tokenizer auxiliary"
    artifacts = [
        _artifact(
            tmp_path,
            "weights",
            "weights/model.bin",
            model_payload,
            model_version_sha256=model_pin,
            token=token,
        ),
        _artifact(
            tmp_path,
            "tokenizer",
            "tokenizer.json",
            auxiliary_payload,
            model_version_sha256=model_pin,
            token=token,
        ),
    ]
    artifact_set_sha256 = _download(
        cache,
        artifacts,
        model_version_sha256=model_pin,
        recipe_revision_sha256=recipe_pin,
        request_key="00000000-0000-4000-8000-000000000101",
    )
    _seed_recipe_reference(
        sessions,
        model_version_sha256=model_pin,
        recipe_revision_sha256=recipe_pin,
        profile=True,
    )
    oci_source, archive_sha256, image_digest = _prebuilt_oci_source(
        tmp_path / "prebuilt-oci", b"pulled prebuilt OCI archive"
    )

    engine.dispose()
    restarted_engine, restarted_sessions, restarted_cache = _restart_controller(
        database, cache.root
    )
    try:
        entry = restarted_cache.get_entry(artifact_set_sha256)
        assert entry["coverage"] == "complete"
        assert restarted_cache.storage_summary().unique_used_bytes == len(
            model_payload + auxiliary_payload
        )
        manifest = restarted_cache.manifest_for_artifact_set(artifact_set_sha256)
        assert manifest.digest == artifact_set_sha256
        model_source = ModelCacheVerifiedObjectSource.from_service(restarted_cache)
        model_objects = model_source.objects_for_set(artifact_set_sha256)
        assert {item.sha256 for item in model_objects} == {
            str(artifacts[0]["sha256"]),
            str(artifacts[1]["sha256"]),
        }
        assert model_source.verify_artifact_set(artifact_set_sha256, model_objects)

        distribution = DistributionService(
            CompositeVerifiedObjectSource(model_source, oci_source),
            sessions=restarted_sessions,
        )
        plan_digest = "3" * 64
        assignments = [
            _assignment(
                node_id=node_id,
                plan_digest=plan_digest,
                artifact_set_sha256=artifact_set_sha256,
                model_objects=model_objects,
                archive_sha256=archive_sha256,
                archive_bytes=len(b"pulled prebuilt OCI archive"),
                image_digest=image_digest,
            )
            for node_id in (NODE_A, NODE_B)
        ]
        for assignment in assignments:
            distribution.register(assignment)

        serialized_assignment = json.dumps(assignments[0].to_mapping())
        assert token not in serialized_assignment
        assert all("source" not in object_.to_mapping() for object_ in assignments[0].objects)
        expected = {
            str(artifacts[0]["sha256"]): model_payload,
            str(artifacts[1]["sha256"]): auxiliary_payload,
            archive_sha256: b"pulled prebuilt OCI archive",
        }
        for node_id in (NODE_A, NODE_B, NODE_A):
            for digest, payload in expected.items():
                assert _read_object(
                    distribution,
                    node_id=node_id,
                    plan_digest=plan_digest,
                    digest=digest,
                ) == payload

        wrong_set = DistributionAssignment.parse(
            assignments[0].to_mapping() | {"model_artifact_set_sha256": "f" * 64}
        )
        with pytest.raises(DistributionError) as error:
            distribution.register(wrong_set)
        assert error.value.code == "distribution.model_set_mismatch"

        eviction = restarted_cache.eviction_preview(
            target_bytes=len(model_payload + auxiliary_payload)
        )
        assert eviction["selected"] == []
        assert eviction["selected_bytes"] == 0
        assert "protected entries require separate reference removal" in eviction["blockers"]
        with pytest.raises(ModelCacheConflict) as error:
            restarted_cache.evict(
                actor="acceptance",
                request_key="00000000-0000-4000-8000-000000000102",
                plan_digest=str(eviction["plan_digest"]),
                target_bytes=len(model_payload + auxiliary_payload),
            )
        assert error.value.code == "model_cache.eviction_blocked"
        assert restarted_cache.verified_artifact_file(
            artifact_set_sha256,
            str(artifacts[0]["sha256"]),
            "weights/model.bin",
        )[0].read_bytes() == model_payload
    finally:
        restarted_engine.dispose()


def test_succeeded_local_recipe_build_archive_uses_the_same_verified_distribution_path(
    controller, tmp_path: Path
) -> None:
    database, engine, sessions, cache = controller
    model_pin = "4" * 64
    recipe_pin = "5" * 64
    model_payload = b"model for local build"
    artifact = _artifact(
        tmp_path,
        "weights",
        "weights/model.bin",
        model_payload,
        model_version_sha256=model_pin,
        token="hf_never_forwarded",
    )
    artifact_set_sha256 = _download(
        cache,
        [artifact],
        model_version_sha256=model_pin,
        recipe_revision_sha256=recipe_pin,
        request_key="00000000-0000-4000-8000-000000000103",
    )
    revision_id = _seed_recipe_reference(
        sessions,
        model_version_sha256=model_pin,
        recipe_revision_sha256=recipe_pin,
        profile=False,
    )
    local_oci_payload = b"locally built OCI archive"
    _local_source, archive_sha256, image_digest = _local_build_oci_source(
        sessions,
        tmp_path / "local-oci",
        revision_id,
        local_oci_payload,
    )

    engine.dispose()
    restarted_engine, restarted_sessions, restarted_cache = _restart_controller(
        database, cache.root
    )
    try:
        model_source = ModelCacheVerifiedObjectSource.from_service(restarted_cache)
        oci_source = RecipeBuildVerifiedObjectSource(
            restarted_sessions, tmp_path / "local-oci"
        )
        assert oci_source.verify_runtime_image(image_digest, archive_sha256)
        distribution = DistributionService(
            CompositeVerifiedObjectSource(model_source, oci_source),
            sessions=restarted_sessions,
        )
        model_objects = model_source.objects_for_set(artifact_set_sha256)
        plan_digest = "6" * 64
        for node_id in (NODE_A, NODE_B):
            distribution.register(
                _assignment(
                    node_id=node_id,
                    plan_digest=plan_digest,
                    artifact_set_sha256=artifact_set_sha256,
                    model_objects=model_objects,
                    archive_sha256=archive_sha256,
                    archive_bytes=len(local_oci_payload),
                    image_digest=image_digest,
                )
            )
        expected = {
            str(artifact["sha256"]): model_payload,
            archive_sha256: local_oci_payload,
        }
        for node_id in (NODE_A, NODE_B, NODE_A):
            for digest, payload in expected.items():
                assert _read_object(
                    distribution,
                    node_id=node_id,
                    plan_digest=plan_digest,
                    digest=digest,
                ) == payload
    finally:
        restarted_engine.dispose()


def test_empty_support_file_can_be_cached_and_served_as_an_immutable_object(
    controller, tmp_path: Path
) -> None:
    database, engine, _sessions, cache = controller
    model_pin = "7" * 64
    empty = _artifact(
        tmp_path,
        "metadata",
        "config/empty.json",
        b"",
        model_version_sha256=model_pin,
        token="hf_never_forwarded",
    )
    set_digest = _download(
        cache,
        [empty],
        model_version_sha256=model_pin,
        recipe_revision_sha256="8" * 64,
        request_key="00000000-0000-4000-8000-000000000104",
    )
    engine.dispose()
    restarted_engine, _restarted_sessions, restarted_cache = _restart_controller(
        database, cache.root
    )
    try:
        entry = restarted_cache.get_entry(set_digest)
        assert entry["coverage"] == "complete"
        assert entry["expected_bytes"] == 0
        assert entry["verified_bytes"] == 0
        assert entry["unique_bytes"] == 0
        assert entry["artifacts"][0]["expected_bytes"] == 0
        descriptor = restarted_cache.resolve_verified_artifact_set(set_digest)[0]
        assert descriptor["bytes"] == 0
        assert descriptor["sha256"] == hashlib.sha256(b"").hexdigest()
        path, size, digest = restarted_cache.verified_artifact_file(
            set_digest,
            str(descriptor["sha256"]),
            str(descriptor["path"]),
        )
        assert (path.read_bytes(), size, digest) == (b"", 0, descriptor["sha256"])

        object_path = (
            restarted_cache.root
            / "objects"
            / str(descriptor["sha256"])[:2]
            / str(descriptor["sha256"])
        )
        object_path.unlink()
        restarted_cache.reconcile_storage()
        assert restarted_cache.get_entry(set_digest)["coverage"] == "incomplete"
    finally:
        restarted_engine.dispose()


def test_empty_weight_file_is_rejected_before_transfer(controller, tmp_path: Path) -> None:
    _database, _engine, _sessions, cache = controller
    artifact = _artifact(
        tmp_path,
        "weights",
        "weights/empty.bin",
        b"",
        model_version_sha256="a" * 64,
        token="hf_never_forwarded",
    )
    with pytest.raises(ModelCacheResolutionError) as error:
        cache.download_preview(
            model_version_sha256="a" * 64,
            artifacts=[artifact],
        )
    assert error.value.code == "model_cache.artifact_invalid"


def test_empty_support_file_requires_the_canonical_empty_digest(
    controller, tmp_path: Path
) -> None:
    _database, _engine, _sessions, cache = controller
    artifact = _artifact(
        tmp_path,
        "metadata",
        "config/empty.json",
        b"",
        model_version_sha256="b" * 64,
        token="hf_never_forwarded",
    )
    artifact["sha256"] = "0" * 64
    with pytest.raises(ModelCacheResolutionError) as error:
        cache.download_preview(
            model_version_sha256="b" * 64,
            artifacts=[artifact],
        )
    assert error.value.code == "model_cache.artifact_invalid"


def test_nonempty_artifact_pin_rejects_an_empty_source_body(controller, tmp_path: Path) -> None:
    _database, _engine, _sessions, cache = controller
    artifact = _artifact(
        tmp_path,
        "metadata",
        "config/metadata.json",
        b"",
        model_version_sha256="c" * 64,
        token="hf_never_forwarded",
    )
    expected = b"declared metadata"
    artifact["sha256"] = hashlib.sha256(expected).hexdigest()
    artifact["download_bytes"] = len(expected)
    preview = cache.download_preview(
        model_version_sha256="c" * 64,
        artifacts=[artifact],
    )
    operation = cache.start_download(
        actor="acceptance",
        request_key="00000000-0000-4000-8000-000000000105",
        plan_digest=str(preview["plan_digest"]),
        model_version_sha256="c" * 64,
        artifacts=[artifact],
    )
    for _ in range(3):
        assert cache.run_pending() == 1
    failed = cache.get_operation(operation.id)
    assert failed.state == "failed"
    assert failed.attempt == 3
    assert failed.retryable is True
    assert failed.last_error and "before the immutable artifact size" in failed.last_error
    assert failed.result is None
    expected_progress = {
        "schema_version": 2,
        "phase": "downloading",
        "completed_artifacts": 0,
        "total_artifacts": 1,
        "downloaded_bytes": 0,
        "expected_bytes": len(expected),
        "current_artifact_key": "artifact-cccccccccccc-metadata",
    }
    assert {key: failed.progress[key] for key in expected_progress} == expected_progress
    assert failed.progress["total_bytes_known"] is True
    members = failed.progress["members"]
    assert len(members) == 1
    assert members[0]["completed_bytes"] == 0
    assert members[0]["total_bytes"] == len(expected)
    entry = cache.get_entry(str(operation.artifact_set_sha256))
    assert entry["state"] == "needs-repair"
    assert entry["coverage"] == "incomplete"
    assert entry["verified_bytes"] == 0
    with pytest.raises(ModelCacheConflict, match="not completely verified"):
        cache.resolve_verified_artifact_set(str(operation.artifact_set_sha256))
