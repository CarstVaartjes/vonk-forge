from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import DistributionObject
from vonk_control.auth import TokenCodec
from vonk_control.distribution import (
    DistributionService,
    MemoryVerifiedObjectSource,
    build_distribution_service_from_components,
)
from vonk_control.distribution_executor import (
    CompositeDistributionPhaseExecutor,
    DurableDistributionPhaseExecutor,
)
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_api import model_cache_operation_provider
from vonk_control.models import (
    AgentOperation,
    AgentOperationAttempt,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
)
from vonk_control.operation_api import merge_operation_providers
from vonk_control.run_switch_operations import RunSwitchOperationService
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from .test_agent_api import NODE_A, NODE_B, agent_headers, agent_system


def _target(node: str, *, image: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        node_id=node,
        state="ready",
        verified_sha256=("e" * 64 if image else "b" * 64),
        imported_image_digest=("sha256:" + "d" * 64 if image else None),
        verified_at=datetime.now(UTC),
    )


def test_complete_two_node_distribution_is_a_verified_skip() -> None:
    nodes = ("spk_" + "a" * 32, "spk_" + "b" * 32)
    preparation = SimpleNamespace(
        model=SimpleNamespace(artifact_set_sha256="b" * 64, artifact_set_bytes=0, targets=[_target(node) for node in nodes]),
        runtime_image=SimpleNamespace(
            image_digest="sha256:" + "d" * 64,
            oci_layout_sha256="e" * 64,
            image_bytes=0,
            targets=[_target(node, image=True) for node in nodes],
        ),
    )
    plan = SimpleNamespace(
        preparation=preparation,
        storage=SimpleNamespace(artifact_digests=["c" * 64]),
        image_digest="sha256:" + "d" * 64,
        build=SimpleNamespace(oci_layout_sha256="e" * 64, image_bytes=11),
        recipe_build_id=None,
    )
    phase = SimpleNamespace(kind="transfer", node_ids=list(nodes), index=0)
    executor = DurableDistributionPhaseExecutor(None, None, None, clock=lambda: datetime.now(UTC))
    result = executor.execute(plan, phase, item_index=0, actor="test", request_key="00000000-0000-4000-8000-000000000001", progress={})
    assert result.operation_id is None
    assert result.result == {"skipped": True, "verified": False, "verified_digests": ["c" * 64], "verified_image_digest": "sha256:" + "d" * 64, "verified_oci_layout_sha256": "e" * 64, "cached_nodes": list(nodes), "cached_target_totals": {node: 0 for node in nodes}}


def test_partial_child_replays_and_aggregates_cached_target(agent_system) -> None:
    _client, services, _tokens, clock = agent_system
    model = DistributionObject("weights/model.bin", "a" * 64, 10, "model")
    config = DistributionObject("config/tokenizer.json", "b" * 64, 5, "model")
    archive = DistributionObject("image.oci.tar", "c" * 64, 11, "oci-archive")
    source = MemoryVerifiedObjectSource({"a" * 64: b"x" * 10, "b" * 64: b"y" * 5, "c" * 64: b"z" * 11})
    source.register_artifact_set("d" * 64, (model, config))
    source.register_runtime_image("sha256:" + "e" * 64, archive.sha256)
    distribution = DistributionService(source, clock=clock, sessions=services.sessions)
    executor = DurableDistributionPhaseExecutor(services.sessions, services.operations, distribution, clock=clock)
    executor._model_objects = lambda _plan: (model, config)
    executor._archive = lambda _plan, **_kwargs: archive
    targets = [
        SimpleNamespace(node_id=NODE_A, state="preparing", verified_sha256=None, imported_image_digest=None, verified_at=None),
            SimpleNamespace(node_id=NODE_B, state="ready", verified_sha256="d" * 64, imported_image_digest=None, verified_at=datetime.now(UTC)),
    ]
    image_targets = [
        SimpleNamespace(node_id=NODE_A, state="preparing", verified_sha256=None, imported_image_digest=None, verified_at=None),
            SimpleNamespace(node_id=NODE_B, state="ready", verified_sha256="c" * 64, imported_image_digest="sha256:" + "e" * 64, verified_at=datetime.now(UTC)),
    ]
    preparation = SimpleNamespace(
        model=SimpleNamespace(artifact_set_sha256="d" * 64, artifact_set_bytes=15, targets=targets),
            runtime_image=SimpleNamespace(image_digest="sha256:" + "e" * 64, oci_layout_sha256="c" * 64, image_bytes=11, build_id=None, targets=image_targets),
    )
    plan = SimpleNamespace(
        preparation=preparation,
        storage=SimpleNamespace(artifact_digests=["a" * 64, "b" * 64]),
        image_digest=None,
        build=SimpleNamespace(oci_layout_sha256=None, image_bytes=None),
        recipe_build_id=None,
        recipe_revision_id=None,
        generated_at=clock.now,
        plan_digest="f" * 64,
        mapping=None,
    )
    phase = SimpleNamespace(kind="transfer", node_ids=[NODE_A, NODE_B], index=0)
    build_progress = {"phase_results": [{"build_id": str(uuid4()), "image_digest": "sha256:" + "e" * 64, "oci_layout_sha256": "c" * 64, "image_bytes": 11}]}
    first = executor.execute(plan, phase, item_index=0, actor="test", request_key="00000000-0000-4000-8000-000000000001", progress=build_progress)
    assert first.operation_id is not None
    with services.sessions.begin() as session:
        child = session.get(Job, first.operation_id)
        assert child is not None
        operation = next(iter(child.payload["assignments"].values()))
        assert operation["assignment_id"]
        stored = session.query(AgentOperation).filter_by(parent_job_id=child.id).one()
        assert stored.payload["distribution_assignment"]["assignment_id"] == operation["assignment_id"]
        stored.state = "succeeded"
        stored.current_attempt = 1
        session.add(AgentOperationAttempt(
            operation_id=stored.id,
            attempt=1,
            fence=str(uuid4()),
            lease_deadline=clock.now,
            agent_certificate_serial="serial-a",
            state="succeeded",
            progress={"bytes": 26, "total_bytes": 26},
            result={
                "verified": True,
                "verified_digests": ["a" * 64, "b" * 64],
                "verified_image_digest": "sha256:" + "e" * 64,
                "imported_image_digest": "sha256:" + "e" * 64,
                "verified_oci_layout_sha256": "c" * 64,
            },
        ))
    view = executor.get(first.operation_id)
    assert view.state == "succeeded"
    assert [member["node_id"] for member in view.result["members"]] == [NODE_A, NODE_B]
    assert view.result["progress"]["completed_bytes"] == 52
    assert view.result["progress"]["total_bytes"] == 52
    replay = executor.execute(plan, phase, item_index=0, actor="test", request_key="00000000-0000-4000-8000-000000000001", progress=build_progress)
    assert replay.operation_id == first.operation_id
    verify = executor.execute(plan, SimpleNamespace(kind="verify", node_ids=[NODE_A, NODE_B], index=1), item_index=0, actor="test", request_key="00000000-0000-4000-8000-000000000001", progress={"cached_nodes": [NODE_B], "evidence": view.result["evidence"]})
    assert verify.result["verified"] is True


def test_partial_child_failure_is_projected_after_aggregation(agent_system) -> None:
    _client, services, _tokens, clock = agent_system
    source = MemoryVerifiedObjectSource()
    distribution = DistributionService(source, clock=clock, sessions=services.sessions)
    executor = DurableDistributionPhaseExecutor(services.sessions, services.operations, distribution, clock=clock)
    # The failure path is intentionally checked at the durable projection
    # boundary; no fabricated verification receipt can make it succeed.
    with services.sessions.begin() as session:
        child = Job(
            id=str(uuid4()), request_id=str(uuid4()), kind="artifact-distribution", state="queued",
            actor="test", authority_revision="f" * 64, targets=[NODE_A], payload_digest="0" * 64,
            payload={"cached_nodes": [], "target_totals": {NODE_A: 26}}, result=None,
            created_at=clock.now, updated_at=clock.now,
        )
        session.add(child)
        session.flush()
        services.operations.enqueue_in_session(session, child.id, NODE_A, "artifact.distribution.v1", "f" * 64, {"schema_version": 1, "plan_digest": "f" * 64}, operation_id=str(uuid4()))
        operation = session.query(AgentOperation).filter_by(parent_job_id=child.id).one()
        operation.state = "failed"
        operation.current_attempt = 1
        session.add(AgentOperationAttempt(
            operation_id=operation.id, attempt=1, fence=str(uuid4()), lease_deadline=clock.now,
            agent_certificate_serial="serial-a", state="failed", progress={"bytes": 2, "total_bytes": 26},
            result={"reason": "digest mismatch"},
        ))
        child_id = child.id
    view = executor.get(child_id)
    assert view.state == "failed"
    assert view.result["members"][0]["error"] == "digest mismatch"


def test_model_download_is_a_durable_cache_child_with_exact_pins() -> None:
    calls: list[dict[str, object]] = []
    cache_view = SimpleNamespace(
        id=str(uuid4()),
        state="queued",
        artifact_set_sha256="d" * 64,
        progress={"downloaded_bytes": 3, "expected_bytes": 15},
        last_error=None,
        result=None,
    )

    class Cache:
        def download_preview(self, **kwargs):
            calls.append({"preview": kwargs})
            return {
                "artifact_set_sha256": "d" * 64,
                "plan_digest": "e" * 64,
                "expected_bytes": 15,
                "artifact_count": 2,
                "new_bytes": 12,
                "blockers": [],
                "_manifest": SimpleNamespace(
                    digest="d" * 64,
                    recipe_revision_sha256="b" * 64,
                ),
            }

        def start_download(self, **kwargs):
            calls.append({"start": kwargs})
            return cache_view

        def get_operation(self, operation_id):
            if operation_id != cache_view.id:
                from vonk_control.model_cache import ModelCacheNotFound
                raise ModelCacheNotFound("model_cache.operation_missing", "missing")
            return cache_view

    plan = SimpleNamespace(
        preparation=SimpleNamespace(model=SimpleNamespace(
            artifact_set_sha256="d" * 64,
            model_version_sha256="a" * 64,
            recipe_revision_sha256="b" * 64,
            artifact_count=2,
            artifact_set_bytes=15,
        )),
        recipe_revision_id=str(uuid4()),
    )
    phase = SimpleNamespace(kind="transfer", subphase="model-download", index=2)
    executor = CompositeDistributionPhaseExecutor(
        None,
        None,
        None,
        model_cache=Cache(),
        clock=lambda: datetime.now(UTC),
    )
    result = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000001",
        progress={},
    )
    assert result.operation_id == cache_view.id
    assert calls[0]["preview"]["artifact_set_sha256"] == "d" * 64
    assert calls[1]["start"]["plan_digest"] == "e" * 64
    replay = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000001",
        progress={},
    )
    assert replay.operation_id == cache_view.id
    assert calls[1]["start"]["request_key"] == calls[3]["start"]["request_key"]
    projected = executor.get(cache_view.id)
    assert projected.state == "queued"
    assert projected.progress["completed_bytes"] == 3


def test_model_download_uses_real_cache_manifest_and_reports_complete_coverage(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = ModelCacheService(
        sessions,
        tmp_path / "nas-cache",
        reserve_bytes=0,
        fixture_sources=True,
    )
    model_version = "a" * 64
    recipe_digest = "b" * 64
    payload = (b"model-payload-" * 100_000) + b"!"
    source = tmp_path / "weights.source"
    source.write_bytes(payload)
    artifact = {
        "id": "weights",
        "path": "weights.bin",
        "kind": "file",
        "source": source.as_uri(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "download_bytes": len(payload),
        "roles": ["model"],
        "model_version_sha256": model_version,
    }
    seed_preview = service.download_preview(
        model_version_sha256=model_version,
        recipe_revision_sha256=recipe_digest,
        artifacts=[artifact],
    )
    seeded = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000011",
        plan_digest=str(seed_preview["plan_digest"]),
        model_version_sha256=model_version,
        recipe_revision_sha256=recipe_digest,
        artifacts=[artifact],
        interrupt_after_bytes=1024,
    )
    assert seeded.state == "partial"
    artifact_set = seeded.artifact_set_sha256
    assert artifact_set
    manifest = service.manifest_for_artifact_set(artifact_set)
    plan = SimpleNamespace(
        preparation=SimpleNamespace(
            model=SimpleNamespace(
                artifact_set_sha256=artifact_set,
                model_version_sha256=model_version,
                recipe_revision_sha256=recipe_digest,
                artifact_count=1,
                artifact_set_bytes=len(payload),
            )
        ),
        recipe_revision_id=None,
    )
    phase = SimpleNamespace(kind="transfer", subphase="model-download", index=2)
    executor = CompositeDistributionPhaseExecutor(
        None,
        None,
        None,
        model_cache=service,
        clock=lambda: datetime.now(UTC),
    )
    first = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000012",
        progress={},
    )
    assert first.operation_id
    service.run_pending(limit=2)
    completed = executor.get(first.operation_id)
    assert completed.state == "succeeded"
    assert completed.result["artifact_set_sha256"] == manifest.digest == artifact_set
    assert completed.result["coverage"] == "complete"
    assert completed.result["evidence"]["coverage"] == "complete"
    # The set was completed by the earlier resumable operation before this
    # child ran, so this operation received no bytes despite its planned
    # remaining range.
    assert completed.result["progress"]["completed_bytes"] == 0
    assert completed.result["progress"]["total_bytes"] < len(payload)


def test_production_composite_uncached_cache_then_two_target_distribution(
    agent_system,
    tmp_path: Path,
) -> None:
    """Exercise the production source pair and durable child handoff.

    The model source is a fixture-backed cache because catalog/network inputs
    are unavailable in this unit lane.  OCI bytes still cross the production
    RecipeBuildVerifiedObjectSource boundary, and the agent HTTP boundary is
    exercised with the enrolled certificate identities.
    """
    client, services, _tokens, clock = agent_system
    cache = ModelCacheService(
        services.sessions,
        tmp_path / "model-cache",
        reserve_bytes=0,
        fixture_sources=True,
    )
    model = ModelDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "model-definition.json")
            .read_text(encoding="utf-8")
        )
    )
    model_version = content_sha256(model)
    recipe_document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-image.json")
        .read_text(encoding="utf-8")
    )
    recipe_document["identity"]["slug"] = "production-composite"
    recipe_document["models"][0]["model"]["content_sha256"] = model_version
    recipe = RecipeDefinition.model_validate(recipe_document)
    recipe_document = recipe.model_dump(mode="json")
    recipe_digest = content_sha256(recipe)
    model_payload = b"model weights"
    auxiliary_payload = b"tokenizer auxiliary"
    model_source = tmp_path / "weights.source"
    auxiliary_source = tmp_path / "tokenizer.source"
    model_source.write_bytes(model_payload)
    auxiliary_source.write_bytes(auxiliary_payload)
    artifacts = [
        {
            "id": "weights",
            "path": "weights.bin",
            "kind": "file",
            "source": model_source.as_uri(),
            "sha256": hashlib.sha256(model_payload).hexdigest(),
            "download_bytes": len(model_payload),
            "roles": ["model"],
            "model_version_sha256": model_version,
        },
        {
            "id": "tokenizer",
            "path": "tokenizer.json",
            "kind": "file",
            "source": auxiliary_source.as_uri(),
            "sha256": hashlib.sha256(auxiliary_payload).hexdigest(),
            "download_bytes": len(auxiliary_payload),
            "roles": ["auxiliary"],
            "model_version_sha256": model_version,
        },
    ]
    preview = cache.download_preview(
        model_version_sha256=model_version,
        recipe_revision_sha256=recipe_digest,
        artifacts=artifacts,
    )
    parent_request = "00000000-0000-4000-8000-000000000021"
    cache_request = str(
        uuid.uuid5(
            uuid.UUID(parent_request),
            f"model-download:0:{preview['artifact_set_sha256']}",
        )
    )
    # Seed the trusted fixture manifest and durable cache child.  The
    # production composite below replays this exact child by its deterministic
    # request key and performs the normal worker transition.
    seeded = cache.start_download(
        actor="operator",
        request_key=cache_request,
        plan_digest=str(preview["plan_digest"]),
        model_version_sha256=model_version,
        recipe_revision_sha256=recipe_digest,
        artifacts=artifacts,
    )
    assert seeded.state == "queued"
    artifact_set = str(seeded.artifact_set_sha256)
    archive_payload = b"prebuilt arm64 oci archive"
    archive_digest = hashlib.sha256(archive_payload).hexdigest()
    image_digest = "sha256:" + "d" * 64
    recipe_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    model_id = str(uuid.uuid4())
    model_revision_id = str(uuid.uuid4())
    build_id = str(uuid.uuid4())
    now = clock.now
    with services.sessions.begin() as session:
        session.add_all(
            [
                CatalogDocument(
                    id=recipe_id,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    title=recipe.metadata.title,
                    created_by="test",
                    created_at=now,
                    updated_at=now,
                ),
                CatalogDocument(
                    id=model_id,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    title=model.identity.model.title,
                    created_by="test",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                CatalogDocumentRevision(
                    id=revision_id,
                    document_id=recipe_id,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=recipe_document,
                    content_digest=recipe_digest,
                    projected={},
                    created_by="test",
                    created_at=now,
                ),
                CatalogDocumentRevision(
                    id=model_revision_id,
                    document_id=model_id,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=model.model_dump(mode="json"),
                    content_digest=model_version,
                    projected={},
                    created_by="test",
                    created_at=now,
                ),
            ]
        )
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256="c" * 64,
                build_input_sha256="e" * 64,
                state="succeeded",
                policy_report={"fixture": "prebuilt-oci"},
                plan={"architecture": "linux-arm64"},
                image_digest=image_digest,
                oci_layout_sha256=archive_digest,
                image_bytes=len(archive_payload),
                error=None,
                created_at=now,
                updated_at=now,
            )
        )
    (services.artifact_root / archive_digest).write_bytes(archive_payload)
    distribution = build_distribution_service_from_components(
        cache,
        services.sessions,
        services.artifact_root,
        clock=clock,
    )
    object.__setattr__(services, "distribution", distribution)
    nodes = (NODE_A, NODE_B)
    preparation = SimpleNamespace(
        model=SimpleNamespace(
            artifact_set_sha256=artifact_set,
            model_version_sha256=model_version,
            recipe_revision_sha256=recipe_digest,
            artifact_count=2,
            artifact_set_bytes=len(model_payload) + len(auxiliary_payload),
            targets=[
                SimpleNamespace(
                    node_id=node,
                    state="pending",
                    verified_sha256=None,
                    verified_at=None,
                )
                for node in nodes
            ],
        ),
        runtime_image=SimpleNamespace(
            image_digest=image_digest,
            oci_layout_sha256=archive_digest,
            image_bytes=len(archive_payload),
            targets=[
                SimpleNamespace(
                    node_id=node,
                    state="pending",
                    verified_sha256=None,
                    imported_image_digest=None,
                    verified_at=None,
                )
                for node in nodes
            ],
        ),
    )
    plan = SimpleNamespace(
        preparation=preparation,
        storage=SimpleNamespace(
            artifact_digests=[item["sha256"] for item in artifacts],
            missing_nas_bytes=0,
        ),
        image_digest=image_digest,
        build=SimpleNamespace(
            oci_layout_sha256=archive_digest,
            image_bytes=len(archive_payload),
            build_input_sha256="e" * 64,
            build_id=build_id,
        ),
        recipe_build_id=build_id,
        recipe_revision_id=None,
        generated_at=now,
        plan_digest="f" * 64,
        mapping=SimpleNamespace(mapping_generation=1),
        spark_group=SimpleNamespace(nodes=[]),
    )
    executor = CompositeDistributionPhaseExecutor(
        services.sessions,
        services.operations,
        distribution,
        model_cache=cache,
        clock=clock,
    )
    model_phase = SimpleNamespace(kind="transfer", subphase="model-download", index=0)
    model_child = executor.execute(
        plan,
        model_phase,
        item_index=0,
        actor="operator",
        request_key=parent_request,
        progress={},
    )
    assert model_child.operation_id == seeded.id
    assert cache.run_pending() == 1
    model_result = executor.get(model_child.operation_id)
    assert model_result.state == "succeeded"
    assert model_result.result["coverage"] == "complete"
    assert model_result.result["artifact_set_sha256"] == artifact_set
    assert model_result.result["progress"]["completed_bytes"] == (
        len(model_payload) + len(auxiliary_payload)
    )

    copy_phase = SimpleNamespace(kind="transfer", subphase="target-copy", index=1, node_ids=list(nodes))
    copy_child = executor.execute(
        plan,
        copy_phase,
        item_index=0,
        actor="operator",
        request_key=parent_request,
        progress={},
    )
    assert copy_child.operation_id
    with services.sessions.begin() as session:
        child = session.get(Job, copy_child.operation_id)
        assert child is not None
        assignments = child.payload["assignments"]
        assert set(assignments) == set(nodes)
        for node in nodes:
            assignment = assignments[node]
            assert assignment["model_artifact_set_sha256"] == artifact_set
            assert {item["sha256"] for item in assignment["objects"]} == {
                *(item["sha256"] for item in artifacts),
                archive_digest,
            }
            assert assignment["oci_image_digest"] == image_digest
            operation = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == child.id,
                    AgentOperation.node_id == node,
                )
            )
            assert operation is not None
            operation.state = "succeeded"
            operation.current_attempt = 1
            session.add(
                AgentOperationAttempt(
                    operation_id=operation.id,
                    attempt=1,
                    fence=str(uuid.uuid4()),
                    lease_deadline=now,
                    agent_certificate_serial="serial-a" if node == NODE_A else "serial-b",
                    state="succeeded",
                    progress={"bytes": 45, "total_bytes": 45},
                    result={
                        "verified": True,
                        "verified_digests": [item["sha256"] for item in artifacts],
                        "verified_image_digest": image_digest,
                        "imported_image_digest": image_digest,
                        "verified_oci_layout_sha256": archive_digest,
                    },
                )
            )
    manifest_response = client.get(
        "/agent/v1/distribution/manifests/" + plan.plan_digest,
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert manifest_response.status_code == 200
    assert {item["sha256"] for item in manifest_response.json()["objects"]} == {
        *(item["sha256"] for item in artifacts),
        archive_digest,
    }
    for node, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
        response = client.get(
            "/agent/v1/distribution/manifests/" + plan.plan_digest,
            headers=agent_headers(node, serial),
        )
        assert response.status_code == 200
        for item in response.json()["objects"]:
            payload_response = client.get(
                "/agent/v1/distribution/objects/"
                + item["sha256"]
                + "?plan_digest="
                + plan.plan_digest,
                headers=agent_headers(node, serial),
            )
            assert payload_response.status_code == 200
            assert len(payload_response.content) == item["bytes"]
    view = executor.get(copy_child.operation_id)
    assert view.state == "succeeded"
    assert {member["node_id"] for member in view.result["members"]} == set(nodes)
    assert len(view.result["evidence"]) == 2
    replay = executor.execute(
        plan,
        copy_phase,
        item_index=0,
        actor="operator",
        request_key=parent_request,
        progress={},
    )
    assert replay.operation_id == copy_child.operation_id

    cached_model = executor.execute(
        plan,
        model_phase,
        item_index=0,
        actor="operator",
        request_key=parent_request,
        progress={},
    )
    assert cached_model.operation_id is None
    assert cached_model.result == {
        "skipped": True,
        "coverage": "complete",
        "artifact_set_sha256": artifact_set,
        "downloaded_bytes": 0,
        "total_bytes": 0,
    }

    copy_view = executor.get(copy_child.operation_id)
    member_progress = copy_view.result["progress"]["members"]
    run_id = str(uuid.uuid4())
    with services.sessions.begin() as session:
        session.add(
            Job(
                id=run_id,
                request_id=str(uuid.uuid4()),
                kind="recipe.run-switch.v2",
                state="running",
                actor="operator",
                authority_revision=plan.plan_digest,
                targets=list(nodes),
                payload_digest=plan.plan_digest,
                payload={"action": "run"},
                result={
                    "phase": "transfer",
                    "phase_index": 0,
                    "completed_bytes": 90,
                    "total_bytes": 90,
                    "total_bytes_known": True,
                    "members": member_progress,
                },
                created_at=now,
                updated_at=now,
            )
        )
    run_provider = RunSwitchOperationService(
        services.sessions,
        clock=clock,
    ).activity_provider()
    family_item = run_provider.get_operation(run_id)
    assert family_item["progress"]["completed_bytes"] == 90
    assert {
        (item["member_id"], item["completed_bytes"], item["total_bytes"])
        for item in family_item["progress"]["members"]
    } == {(NODE_A, 45, 45), (NODE_B, 45, 45)}
    restarted_provider = RunSwitchOperationService(
        services.sessions,
        clock=clock,
    ).activity_provider()
    assert restarted_provider.get_operation(run_id)["progress"] == family_item["progress"]

    cursors = TokenCodec(b"p" * 32).cursor_codec()
    merged = merge_operation_providers(
        (run_provider, model_cache_operation_provider(cache, cursors)),
        cursor=None,
        limit=100,
        state=None,
        node_id=None,
        cursors=cursors,
    )
    merged_run = next(item for item in merged.items if item["id"] == run_id)
    assert merged_run["progress"]["completed_bytes"] == 90
    assert {
        (item["member_id"], item["completed_bytes"], item["total_bytes"])
        for item in merged_run["progress"]["members"]
    } == {(NODE_A, 45, 45), (NODE_B, 45, 45)}

    unknown_id = str(uuid.uuid4())
    with services.sessions.begin() as session:
        session.add(
            Job(
                id=unknown_id,
                request_id=str(uuid.uuid4()),
                kind="recipe.run-switch.v2",
                state="running",
                actor="operator",
                authority_revision=plan.plan_digest,
                targets=[NODE_A],
                payload_digest=plan.plan_digest,
                payload={"action": "run"},
                result={
                    "phase": "transfer",
                    "phase_index": 0,
                    "completed_bytes": 0,
                    "total_bytes": None,
                    "total_bytes_known": False,
                    "members": [{
                        "node_id": NODE_A,
                        "phase": "transfer",
                        "state": "running",
                        "completed_bytes": 0,
                        "total_bytes": None,
                    }],
                },
                created_at=now,
                updated_at=now,
            )
        )
    unknown_item = run_provider.get_operation(unknown_id)
    assert unknown_item["progress"].get("total_bytes") is None
    assert unknown_item["progress"]["total_bytes_known"] is False
    assert unknown_item["progress"]["members"][0].get("total_bytes") is None
