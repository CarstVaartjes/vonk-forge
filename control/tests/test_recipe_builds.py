from __future__ import annotations

import copy
import hashlib
import io
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

import pytest
import vonk_control.availability_production as availability_production_module
import vonk_control.recipe_builds as recipe_builds_module
import vonk_control.runtime_adapters as runtime_adapters_module
from sqlalchemy import Table, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentClaim,
    AgentResult,
    RecipeBuildRequest,
    canonical_message,
    canonical_payload,
)
from vonk_agent_protocol import (
    AgentOperation as ProtocolOperation,
)
from vonk_control.agent_jobs import AgentJobService, superseded_cancellation_deadline
from vonk_control.availability_production import build_recipe_image_availability
from vonk_control.bounded_json import require_integer
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    Base,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    Job,
    NodeArtifact,
    RecipeBuild,
    RecipeSourceBundle,
    ResourceReservation,
)
from vonk_control.recipe_builds import RecipeBuildError, RecipeBuildService
from vonk_control.recipe_execution_contract import parse_stored_build_plan
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityError,
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_image_removal_contract import RecipeCacheRemovalOwner
from vonk_control.recipe_operations import (
    RecipeOperationConflict,
    RecipeOperationService,
    _record_build_evidence,
)
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_adapters import resolve_runtime_adapter
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImageReceipt,
    persist_runtime_image_receipt,
)
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle
from vonk_forge_contracts import RecipeDefinition, content_sha256

_CACHED_ADAPTER = resolve_runtime_adapter("vllm", {"mode": "single"})


class RecordingQueue:
    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: Mapping[str, object],
        *,
        operation_id: str,
    ) -> AgentOperation:
        parent = session.get(Job, parent_job_id)
        assert parent is not None
        record = AgentOperation(
            id=operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=operation,
            payload_digest="f" * 64,
            payload=dict(payload),
            authority_revision=authority_revision,
            workload_intent_ordinal=parent.payload.get("workload_intent_ordinal"),
            state="queued",
            current_attempt=0,
            created_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
            updated_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
        )
        session.add(record)
        return record

    def notify_available(self) -> None:
        pass


def _json_object(value: object) -> dict[str, object]:
    """Narrow decoded JSON to a mutable object; a wrong shape fails the test."""

    assert isinstance(value, dict)
    return value


def _json_array(value: object) -> list[object]:
    """Narrow decoded JSON to a mutable array; a wrong shape fails the test."""

    assert isinstance(value, list)
    return value


def _json_text(value: object) -> str:
    """Narrow decoded JSON to a string; a wrong shape fails the test."""

    assert isinstance(value, str)
    return value


def test_build_disk_reserve_scales_to_the_spark_cap() -> None:
    assert recipe_builds_module._build_disk_reserve(100 * 1024**3) == 4 * 1024**3
    assert recipe_builds_module._build_disk_reserve(4 * 1024**4) == 64 * 1024**3


def setup(
    tmp_path: Path,
    *,
    network: dict[str, object] | None = None,
    engine=None,
    existing_node: bool = False,
    builder_node_id: str | None = None,
    recipe_slug: str = "qwen3-vllm",
):
    engine = engine or create_engine(f"sqlite:///{tmp_path / 'build.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    if existing_node:
        now += timedelta(seconds=1)
    node_id = builder_node_id or "spk_" + "1" * 32
    bundle = generate_source_bundle(
        {
            "Dockerfile": (
                "FROM ghcr.io/example/vllm@sha256:" + "a" * 64 + "\nUSER 10001:10001\n"
            ).encode()
        }
    )
    bundles = SourceBundleStore(tmp_path / "bundles")
    stored = bundles.put(bundle.sha256, io.BytesIO(bundle.archive))
    document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    document["execution"]["build"]["network"] = network or {
        "mode": "none",
        "hosts": [],
    }
    document["identity"]["slug"] = recipe_slug
    document["execution"]["build"]["target"] = "runtime"
    with sessions.begin() as session:
        builder = session.get(AgentNode, node_id) if existing_node else None
        if existing_node:
            assert builder is not None
            builder.binary_digest = "1" * 64
            builder.self_test_passed = True
            builder.capabilities = [
                *builder.capabilities,
                "recipe.build.v1",
                "recipe.image.import.v1",
            ]
        else:
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    architecture="linux-arm64",
                    semantic_version="1.2.3",
                    build_digest="sha256:" + "a" * 64,
                    binary_digest="1" * 64,
                    self_test_passed=True,
                    capabilities=[
                        "recipe.build.v1",
                        "recipe.image.import.v1",
                    ],
                    last_seen_at=now,
                )
            )
        session.add(
            RecipeSourceBundle(
                sha256=bundle.sha256,
                media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                archive_bytes=stored.archive_bytes,
                total_bytes=bundle.manifest.total_bytes,
                file_count=len(bundle.manifest.files),
                storage_key=f"{bundle.sha256[:2]}/{bundle.sha256}.tar",
                manifest=bundle.manifest.model_dump(mode="json"),
                verified_at=now,
            )
        )
    InventoryRepository(sessions, clock=lambda: now).record(
        InventorySnapshotInput(
            node_id,
            now,
            2 * 1024**4,
            1 * 1024**4,
            100_000,
            80_000,
            100_000,
            80_000,
            1,
            False,
            (
                "recipe.build.v1",
                "recipe.build.egress-proxy.v1",
                "recipe.image.import.v1",
            ),
            memory_pool="separate",
        )
    )
    catalog = CatalogEntityService(sessions, clock=lambda: now)
    model = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    if not existing_node:
        model_draft = catalog.create_draft(model, actor="admin")
        catalog.resolve(model_draft.id, actor="admin")
    recipe_draft = catalog.create_draft(document, actor="admin")
    with sessions.begin() as session:
        stored_revision = session.get(CatalogDocumentRevision, recipe_draft.id)
        assert stored_revision is not None
        stored_revision.projected = {
            **stored_revision.projected,
            "source_bundle_sha256": bundle.sha256,
            "build_resources": {
                "cpu_cores": 6,
                "download_bytes": 100,
                "temporary_bytes": 200,
                "memory_bytes": 300,
                "processes": 2048,
                "timeout_seconds": 600,
            },
            "build_security": {"capabilities": ["DAC_OVERRIDE"]},
            "build_options": {
                "additional_contexts": [],
                "annotations": [],
                "environment": [],
                "format": "oci",
                "identity_label": True,
                "ignorefile": None,
                "jobs": 1,
                "labels": [],
                "layer_compression": "disabled",
                "layer_labels": [],
                "layers": True,
                "no_hostname": False,
                "no_hosts": False,
                "omit_history": False,
                "os_features": [],
                "os_version": None,
                "shm_bytes": 67108864,
                "skip_unused_stages": True,
                "squash": "none",
                "timestamp": None,
                "unset_environment": [],
                "unset_labels": [],
            },
            "build_model_artifacts": [
                {
                    "path": "model.safetensors",
                    "sha256": "c" * 64,
                    "size_bytes": 1024,
                }
            ],
        }
    revision = catalog.resolve(recipe_draft.id, actor="admin")
    return sessions, bundles, now, node_id, revision


def test_build_plan_is_typed_sandboxed_and_durable(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)

    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )

    assert plan.agent_payload["kind"] == "recipe.build.v1"
    assert "command" not in plan.agent_payload
    assert plan.agent_payload["target"] == "runtime"
    assert plan.agent_payload["capabilities"] == ["DAC_OVERRIDE"]
    limits = _json_object(plan.agent_payload["limits"])
    assert limits["cpu_cores"] == 6
    assert limits["gpu"] == 0
    assert limits["processes"] == 2048
    assert plan.agent_payload["base_images"] == [
        {
            "manifest_digest": "sha256:" + "a" * 64,
            "reference": "ghcr.io/example/vllm@sha256:" + "a" * 64,
        }
    ]
    assert plan.agent_payload["base_image_storage_bytes"] == 100
    assert (
        plan.agent_payload["source_bundle_sha256"]
        == revision.projected["source_bundle_sha256"]
    )
    with sessions() as session:
        stored = session.get(RecipeBuild, plan.build_id)
        assert stored is not None and stored.state == "planned"


def test_prepared_plan_failure_rolls_back_without_orphan_build(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    prepared = service.prepare_plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.binary_digest = "2" * 64

    with (
        pytest.raises(RecipeBuildError, match="runtime identity changed"),
        sessions.begin() as session,
    ):
        service.persist_plan_in_session(session, prepared, now=now)

    with sessions() as session:
        assert session.get(RecipeBuild, prepared.build_id) is None


def test_build_identity_changes_when_builder_runtime_changes(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)

    first = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.binary_digest = "2" * 64
    second = service.plan(revision.id, node_id, now=now)

    assert second.build_id != first.build_id
    assert second.build_input_sha256 != first.build_input_sha256


def test_build_resolution_reuses_exact_receipt_without_builder_admission(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.state = "revoked"
        node.binary_digest = None

    resolution = service.resolve(revision.id)

    assert resolution.cached
    assert resolution.build_id == plan.build_id
    assert resolution.build_input_sha256 == plan.build_input_sha256
    assert resolution.builder_binary_digest == "1" * 64
    assert resolution.image_digest == "sha256:" + "b" * 64


def test_an_adapter_change_invalidates_the_prepared_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    cached = service.resolve(revision.id)
    assert cached.cached
    assert cached.build_input_sha256 == plan.build_input_sha256

    # A reviewed adapter change is a different executable input.  Omitting the
    # adapter from the identity would reuse the image the previous adaptation
    # produced and leave the built recipe unadapted.
    adapter = cached.input_intent["runtime_adapter"]
    assert isinstance(adapter, dict)
    current = runtime_adapters_module._ENGINE_ADAPTERS[
        adapter["adapter_id"].split(".")[-2]
    ]
    monkeypatch.setitem(
        runtime_adapters_module._ENGINE_ADAPTERS,
        current.adapter_id.split(".")[-2],
        runtime_adapters_module._AdapterSpec(
            f"{current.adapter_id}.next", current.launcher
        ),
    )
    changed = service.resolve(revision.id)
    assert not changed.cached
    assert changed.build_id is None
    assert changed.input_intent_sha256 != cached.input_intent_sha256


def _write_controller_build_receipt(
    storage: FilesystemRuntimeImageStorage,
    *,
    archive: bytes,
    image_digest: str,
    build_id: str,
    build_input_sha256: str,
    distribution_content_sha256: str,
) -> RuntimeImageReceipt:
    """Publish the exact filesystem receipt a completed Controller build leaves."""

    staged = storage.prepare_path()
    staged.write_bytes(archive)
    return storage.commit(
        staged,
        receipt=RuntimeImageReceipt(
            schema_version=2,
            source="controller-build",
            distribution_publisher="vonk",
            distribution_slug="cached",
            distribution_content_sha256=distribution_content_sha256,
            registry_manifest_digest=None,
            platform_manifest_digest=image_digest,
            image_digest=image_digest,
            oci_archive_sha256=hashlib.sha256(archive).hexdigest(),
            image_bytes=len(archive),
            local_image_config_id="sha256:" + "c" * 64,
            local_image_reference=None,
            architecture="linux-arm64",
            runtime_interface="vonk.runtime.v1",
            archive_path=str(staged),
            recorded_at="2026-09-15T00:00:00Z",
            build_id=build_id,
            build_input_sha256=build_input_sha256,
            runtime_interface_label="v1",
            runtime_adapter=_CACHED_ADAPTER.adapter_id,
            runtime_adapter_sha256=_CACHED_ADAPTER.digest,
        ),
    )


def test_nonforced_availability_dispatch_reuses_build_resolved_after_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    artifact_root = tmp_path / "artifacts"
    storage = FilesystemRuntimeImageStorage(artifact_root)
    archive = b"cached source build archive"
    archive_digest = hashlib.sha256(archive).hexdigest()
    builds = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    plan = builds.plan(revision.id, node_id, now=now)
    image_digest = "sha256:" + "b" * 64
    builds.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest=image_digest,
        oci_layout_sha256=archive_digest,
        image_bytes=len(archive),
        now=now,
    )
    published = _write_controller_build_receipt(
        storage,
        archive=archive,
        image_digest=image_digest,
        build_id=plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        distribution_content_sha256=revision.content_digest,
    )
    cached = builds.resolve(revision.id)
    assert cached.cached
    assert cached.oci_layout_sha256 == published.oci_archive_sha256

    class DelayedCachedResolution:
        def __init__(self) -> None:
            self.resolve_calls = 0

        def resolve(self, recipe_revision_id: str):
            self.resolve_calls += 1
            resolved = builds.resolve(recipe_revision_id)
            if self.resolve_calls == 1:
                return replace(
                    resolved,
                    build_input_sha256=None,
                    build_id=None,
                    builder_node_id=None,
                    builder_binary_digest=None,
                    image_digest=None,
                    oci_layout_sha256=None,
                    image_bytes=None,
                )
            return resolved

        def __getattr__(self, name: str):
            return getattr(builds, name)

    class Operations:
        def build(self, *_args, **_kwargs):
            raise AssertionError("verified cached bytes must bypass build dispatch")

    class Transport:
        def inspect_archive(
            self,
            archive_path: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            expected_archive_sha256: str,
            expected_archive_bytes: int,
        ) -> PulledImageEvidence:
            assert archive_path.read_bytes() == archive
            return PulledImageEvidence(
                manifest_digest=image_digest,
                requested_manifest_digest=None,
                config_id="sha256:" + "c" * 64,
                local_reference="localhost/vonk/cached@" + image_digest,
                architecture=expected_architecture,
                runtime_interface="v1",
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    delayed = DelayedCachedResolution()
    monkeypatch.setattr(
        availability_production_module, "SkopeoOCIImageTransport", Transport
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=artifact_root,
        managed_catalog_sync=None,
        recipe_builds=delayed,
        recipe_operations=Operations(),
        clock=lambda: now,
    )
    operation = production.service.start(
        revision.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000733",
    )
    assert operation.build_input_sha256 is None

    assert production.service.run_pending() == 1
    completed = production.service.get(operation.id)
    assert completed.state == "succeeded", completed.failure
    assert completed.result is not None
    assert completed.result["build_id"] == plan.build_id
    assert completed.result["build_input_sha256"] == plan.build_input_sha256
    assert delayed.resolve_calls == 2
    with sessions() as session:
        build_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )
    assert build_jobs == ()
    production.close()


def test_present_archive_without_receipt_is_reprepared_not_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    artifact_root = tmp_path / "artifacts"
    storage = FilesystemRuntimeImageStorage(artifact_root)
    archive = b"cached source build archive"
    archive_digest = hashlib.sha256(archive).hexdigest()
    # The upload producer publishes the archive; preparation publishes the
    # receipt. A Controller death in between must not force a rebuild.
    (storage.root / archive_digest).write_bytes(archive)
    image_digest = "sha256:" + "b" * 64
    builds = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    plan = builds.plan(revision.id, node_id, now=now)
    builds.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest=image_digest,
        oci_layout_sha256=archive_digest,
        image_bytes=len(archive),
        now=now,
    )

    resolution = builds.resolve(revision.id)

    assert resolution.cached is True
    assert resolution.stale_receipt is False
    assert resolution.receipt_pending is True
    assert resolution.build_input_sha256 == plan.build_input_sha256

    class Operations:
        def build(self, *_args, **_kwargs):
            raise AssertionError(
                "present bytes with a missing receipt must be re-prepared, not rebuilt"
            )

    class Transport:
        def inspect_archive(
            self,
            archive_path: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            expected_archive_sha256: str,
            expected_archive_bytes: int,
        ) -> PulledImageEvidence:
            assert archive_path.read_bytes() == archive
            return PulledImageEvidence(
                manifest_digest=image_digest,
                requested_manifest_digest=None,
                config_id="sha256:" + "c" * 64,
                local_reference="localhost/vonk/cached@" + image_digest,
                architecture=expected_architecture,
                runtime_interface="v1",
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    monkeypatch.setattr(
        availability_production_module, "SkopeoOCIImageTransport", Transport
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=artifact_root,
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=Operations(),
        clock=lambda: now,
    )
    operation = production.service.start(
        revision.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000734",
    )
    assert operation.build_input_sha256 == plan.build_input_sha256

    assert production.service.run_pending() == 1
    completed = production.service.get(operation.id)
    assert completed.state == "succeeded", completed.failure
    republished = storage.read_receipt(archive_digest)
    assert republished.build_input_sha256 == plan.build_input_sha256
    assert republished.build_id == plan.build_id
    with sessions() as session:
        build_jobs = tuple(
            session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )
    assert build_jobs == ()
    production.close()


def test_legacy_unreadable_build_receipt_is_replaced_from_verified_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-adapter receipt must not pin an already-built recipe forever.

    The archive is content-addressed and verified before any receipt is
    derived, so a receipt file the current contract cannot parse is stale
    metadata about those same bytes.  The live download refused before any
    durable operation with ``runtime_image.receipt_unavailable: ... lacks its
    adapter`` because the scan over every receipt collected this one file
    before the request could be queued; preparation must instead re-derive and
    replace it.
    """

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    artifact_root = tmp_path / "artifacts"
    storage = FilesystemRuntimeImageStorage(artifact_root)
    archive = b"cached source build archive"
    archive_digest = hashlib.sha256(archive).hexdigest()
    (storage.root / archive_digest).write_bytes(archive)
    image_digest = "sha256:" + "b" * 64
    builds = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    plan = builds.plan(revision.id, node_id, now=now)
    builds.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest=image_digest,
        oci_layout_sha256=archive_digest,
        image_bytes=len(archive),
        now=now,
    )
    # Exactly what a Controller wrote before runtime_adapter existed: valid
    # JSON and the verified archive identity, but no adapter identity.
    (storage.root / f"{archive_digest}.receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source": "controller-build",
                "distribution_publisher": "vonk",
                "distribution_slug": "cached",
                "distribution_content_sha256": revision.content_digest,
                "registry_manifest_digest": None,
                "platform_manifest_digest": image_digest,
                "image_digest": image_digest,
                "oci_archive_sha256": archive_digest,
                "image_bytes": len(archive),
                "local_image_config_id": "sha256:" + "c" * 64,
                "local_image_reference": None,
                "architecture": "linux-arm64",
                "runtime_interface": "vonk.runtime.v1",
                "runtime_interface_label": "v1",
                "archive_path": str(storage.root / archive_digest),
                "recorded_at": "2026-09-15T00:00:00Z",
                "build_id": plan.build_id,
                "build_input_sha256": plan.build_input_sha256,
            }
        ),
        encoding="utf-8",
    )

    # The scan that resolves a cached build must skip the file it cannot
    # parse; the same bytes are still re-prepared below.
    assert (
        storage.find_build(
            plan.build_input_sha256,
            expected_architecture="linux-arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is None
    )

    class Operations:
        def build(self, *_args, **_kwargs):
            raise AssertionError("verified bytes must be re-prepared, not rebuilt")

    class Transport:
        def inspect_archive(
            self,
            archive_path: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            expected_archive_sha256: str,
            expected_archive_bytes: int,
        ) -> PulledImageEvidence:
            assert archive_path.read_bytes() == archive
            return PulledImageEvidence(
                manifest_digest=image_digest,
                requested_manifest_digest=None,
                config_id="sha256:" + "c" * 64,
                local_reference="localhost/vonk/cached@" + image_digest,
                architecture=expected_architecture,
                runtime_interface="v1",
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    monkeypatch.setattr(
        availability_production_module, "SkopeoOCIImageTransport", Transport
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=artifact_root,
        managed_catalog_sync=None,
        recipe_builds=builds,
        recipe_operations=Operations(),
        clock=lambda: now,
    )
    operation = production.service.start(
        revision.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000735",
    )
    assert operation.build_input_sha256 == plan.build_input_sha256

    assert production.service.run_pending() == 1
    completed = production.service.get(operation.id)
    assert completed.state == "succeeded", completed.failure
    repaired = storage.read_receipt(archive_digest)
    assert repaired.runtime_adapter == _CACHED_ADAPTER.adapter_id
    assert repaired.runtime_adapter_sha256 == _CACHED_ADAPTER.digest
    assert repaired.build_input_sha256 == plan.build_input_sha256
    production.close()


def test_build_resolution_reports_stale_receipt_when_archive_is_gone(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    storage = FilesystemRuntimeImageStorage(tmp_path / "artifacts")
    archive = b"cached source build archive"
    archive_digest = hashlib.sha256(archive).hexdigest()
    service = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    plan = service.plan(revision.id, node_id, now=now)
    image_digest = "sha256:" + "b" * 64
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest=image_digest,
        oci_layout_sha256=archive_digest,
        image_bytes=len(archive),
        now=now,
    )
    _write_controller_build_receipt(
        storage,
        archive=archive,
        image_digest=image_digest,
        build_id=plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        distribution_content_sha256=revision.content_digest,
    )
    (storage.root / archive_digest).unlink()

    resolution = service.resolve(revision.id)

    assert resolution.cached is False
    assert resolution.stale_receipt is True
    rebuilt = service.plan(revision.id, node_id, now=now, resolution=resolution)
    assert rebuilt.build_input_sha256 == plan.build_input_sha256


def test_resolution_reuses_the_present_receipt_for_the_shared_identity(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    storage = FilesystemRuntimeImageStorage(tmp_path / "artifacts")
    archive = b"cached source build archive"
    archive_digest = hashlib.sha256(archive).hexdigest()
    service = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    plan = service.plan(revision.id, node_id, now=now)
    image_digest = "sha256:" + "b" * 64
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest=image_digest,
        oci_layout_sha256=archive_digest,
        image_bytes=len(archive),
        now=now,
    )
    _write_controller_build_receipt(
        storage,
        archive=archive,
        image_digest=image_digest,
        build_id=plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        distribution_content_sha256=revision.content_digest,
    )
    # A newer succeeded build on a second builder with the same executable
    # identity has no archive of its own. The present filesystem receipt for
    # that identity is still the authority, so no rebuild is dispatched.
    second_node = "spk_22222222222222222222222222222222"
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=second_node,
                state="active",
                architecture="linux-arm64",
                semantic_version="1.2.3",
                build_digest="sha256:" + "a" * 64,
                binary_digest="1" * 64,
                self_test_passed=True,
                capabilities=["recipe.build.v1", "recipe.image.import.v1"],
                last_seen_at=now,
            )
        )
    InventoryRepository(sessions, clock=lambda: now).record(
        InventorySnapshotInput(
            second_node,
            now,
            2 * 1024**4,
            1 * 1024**4,
            100_000,
            80_000,
            100_000,
            80_000,
            1,
            False,
            (
                "recipe.build.v1",
                "recipe.build.egress-proxy.v1",
                "recipe.image.import.v1",
            ),
            memory_pool="separate",
        )
    )
    second_plan = service.plan(revision.id, second_node, now=now)
    assert second_plan.build_input_sha256 == plan.build_input_sha256
    service.record_success(
        second_plan.build_id,
        build_input_sha256=second_plan.build_input_sha256,
        image_digest="sha256:" + "e" * 64,
        oci_layout_sha256="d" * 64,
        image_bytes=len(archive),
        now=now + timedelta(seconds=5),
    )

    resolution = service.resolve(revision.id)

    assert resolution.cached is True
    assert resolution.stale_receipt is False
    assert resolution.image_digest == image_digest
    assert resolution.oci_layout_sha256 == archive_digest


def test_missing_build_archive_is_not_reused_and_replans_the_same_build_input(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=lambda _digest, _size: False,
    )
    plan = service.plan(revision.id, node_id, now=now)
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )

    resolution = service.resolve(revision.id)
    rebuilt = service.plan(revision.id, node_id, now=now, resolution=resolution)

    assert resolution.cached is False
    assert resolution.stale_receipt is True
    assert rebuilt.build_id == plan.build_id
    assert rebuilt.build_input_sha256 == plan.build_input_sha256
    with sessions() as session:
        stored = session.get(RecipeBuild, plan.build_id)
        assert stored is not None
        assert stored.state == "planned"
        assert stored.image_digest is None
        assert stored.oci_layout_sha256 is None
        assert stored.image_bytes is None


def test_build_resolution_reuses_notes_only_revision_when_inputs_match(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    with sessions.begin() as session:
        current = session.get(CatalogDocumentRevision, revision.id)
        assert current is not None
        document = copy.deepcopy(current.document)
        _json_object(document["metadata"])["title"] = "Editorially renamed recipe"
        canonical = RecipeDefinition.model_validate(document)
        document = canonical.model_dump(mode="json")
        content_digest = content_sha256(canonical)
        newer_revision = CatalogDocumentRevision(
            id="notes-revision-" + "1" * 19,
            document_id=current.document_id,
            kind=current.kind,
            publisher=current.publisher,
            slug=current.slug,
            revision_number=current.revision_number + 1,
            schema_version=2,
            state="active",
            document=document,
            content_digest=content_digest,
            artifact_key="e" * 64,
            execution_key="f" * 64,
            projected=copy.deepcopy(current.projected),
            created_by="test",
            created_at=now,
        )
        session.add(newer_revision)

    resolution = service.resolve(newer_revision.id)

    assert resolution.cached
    assert resolution.build_id == plan.build_id
    assert resolution.build_input_sha256 == plan.build_input_sha256


def test_build_resolution_without_cache_returns_durable_intent_identity(
    tmp_path: Path,
) -> None:
    sessions, bundles, _now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.state = "revoked"
        node.binary_digest = None

    resolution = service.resolve(revision.id)

    assert not resolution.cached
    assert resolution.build_id is None
    assert resolution.build_input_sha256 is None
    assert len(resolution.input_intent_sha256) == 64
    assert "builder_binary_digest" not in resolution.input_intent


@pytest.mark.parametrize(
    "field,value",
    [
        ("build_input_sha256", "0" * 64),
        ("image_digest", None),
        ("oci_layout_sha256", None),
        ("image_bytes", None),
        ("builder_binary_digest", "2" * 64),
    ],
)
def test_build_resolution_rejects_incomplete_or_mismatched_cache_receipts(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    with sessions.begin() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        if field == "builder_binary_digest":
            build.policy_report = dict(build.policy_report) | {field: value}
        else:
            setattr(build, field, value)

    resolution = service.resolve(revision.id)

    assert not resolution.cached
    assert resolution.build_input_sha256 is None
    assert resolution.build_id is None


def test_build_plan_rejects_a_stale_resolution_but_keeps_live_admission(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    resolution = service.resolve(revision.id)
    with sessions.begin() as session:
        current = session.get(CatalogDocumentRevision, revision.id)
        assert current is not None
        document = copy.deepcopy(current.document)
        _json_object(_json_object(document["execution"])["build"])["arguments"] = [
            {"name": "changed", "value": "yes"}
        ]
        canonical = RecipeDefinition.model_validate(document)
        document = canonical.model_dump(mode="json")
        content_digest = content_sha256(canonical)
        newer_revision = CatalogDocumentRevision(
            id="new-revision-" + "1" * 25,
            document_id=current.document_id,
            kind=current.kind,
            publisher=current.publisher,
            slug=current.slug,
            revision_number=current.revision_number + 1,
            schema_version=2,
            state="active",
            document=document,
            content_digest=content_digest,
            artifact_key="e" * 64,
            execution_key="f" * 64,
            projected=copy.deepcopy(current.projected),
            created_by="test",
            created_at=now,
        )
        session.add(newer_revision)

    with pytest.raises(RecipeBuildError, match="immutable build resolution"):
        service.plan(newer_revision.id, node_id, now=now, resolution=resolution)


def test_build_plan_from_intent_rechecks_selected_builder_capacity(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    resolution = service.resolve(revision.id)
    newer = now + timedelta(seconds=1)
    InventoryRepository(sessions, clock=lambda: newer).record(
        InventorySnapshotInput(
            node_id,
            newer,
            2 * 1024**4,
            1,
            100_000,
            80_000,
            100_000,
            80_000,
            1,
            False,
            ("recipe.build.v1", "recipe.build.egress-proxy.v1"),
            memory_pool="separate",
        )
    )

    with pytest.raises(RecipeBuildError, match="temporary disk capacity"):
        service.plan(revision.id, node_id, now=newer, resolution=resolution)


def test_build_identity_changes_when_archive_format_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)

    first = service.plan(revision.id, node_id, now=now)
    monkeypatch.setattr(
        recipe_builds_module, "BUILD_ARTIFACT_FORMAT", "future-archive-v2"
    )
    second = service.plan(revision.id, node_id, now=now)

    assert second.build_id != first.build_id
    assert second.build_input_sha256 != first.build_input_sha256


def test_build_reservation_rejects_changed_builder_runtime(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.binary_digest = "2" * 64

    with (
        sessions.begin() as session,
        pytest.raises(RecipeBuildError, match="runtime identity changed"),
    ):
        service.reserve_in_session(session, plan, now=now)


def test_stored_build_envelope_names_the_field_that_invalidated_it(
    tmp_path: Path,
) -> None:
    """The stored-envelope reader must name the field and rule it rejected.

    Every contract failure was collapsed into "stored source build envelope is
    invalid", so a live ``build.plan_invalid`` blocker could not say which
    stored field an older Controller had written outside the current model.
    The field path and error type are what make the blocker actionable.
    """

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        stored = session.get(RecipeBuild, plan.build_id)
        assert stored is not None
        document = copy.deepcopy(stored.plan)
        # Valid JSON that the current model still rejects: the limit must be
        # strictly positive.  Corrupt the stored JSON directly so the real
        # stored-envelope read boundary is what rejects it.
        _json_object(document["limits"])["temporary_bytes"] = 0
        table = RecipeBuild.__table__
        assert isinstance(table, Table)
        session.execute(
            table.update().where(RecipeBuild.id == plan.build_id).values(plan=document)
        )

    with (
        sessions.begin() as session,
        pytest.raises(RecipeBuildError) as raised,
    ):
        service.reserve_in_session(session, plan, now=now)

    assert raised.value.code == "build.plan_invalid"
    message = str(raised.value)
    assert "limits.temporary_bytes" in message, message
    assert "greater_than" in message, message


def test_removal_does_not_corrupt_the_stored_build_envelope(tmp_path: Path) -> None:
    """Removal preserves another request's build and its canonical envelope.

    This catches implicit build cancellation or removal-only fields written
    into the strict build plan while cache eviction is accepted.
    """

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    planned = builds.plan(revision.id, node_id, now=now)
    recipe = RecipeDefinition.model_validate(revision.document)
    availability = RecipeImageAvailabilityService(
        sessions,
        storage=FilesystemRuntimeImageStorage(tmp_path / "cache"),
        authority=lambda *_args, **_kwargs: (
            recipe,
            {
                "architecture": "linux/arm64",
                "interface": "vonk.runtime.v1",
                "image_bytes": 1,
            },
        ),
        clock=lambda: now,
    )

    result = availability.remove_selector(
        recipe.identity.slug,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000024",
    )
    assert result["cancelled_builds"] == []

    with sessions() as session:
        stored = session.get(RecipeBuild, planned.build_id)
        assert stored is not None
        assert stored.state == "planned"
        # The exact stored document still satisfies the canonical contract.
        assert parse_stored_build_plan(stored.plan).build_id == planned.build_id

    # The fence belongs to the exact removal owner.
    with sessions() as session:
        removal = session.scalar(
            select(Job).where(Job.request_id == "00000000-0000-4000-8000-000000000024")
        )
        assert removal is not None
        owner = RecipeCacheRemovalOwner.model_validate_json(
            canonical_message(removal.payload)
        )
        assert owner.plan.intent.request_key == removal.request_id

    # Replanning reconnects to the same valid build request.
    replanned = builds.plan(revision.id, node_id, now=now)
    assert replanned.build_id == planned.build_id
    assert parse_stored_build_plan(replanned.agent_payload).build_id == planned.build_id
    with sessions() as session:
        rebuilt = session.get(RecipeBuild, planned.build_id)
        assert rebuilt is not None
        assert rebuilt.state == "planned"
        assert rebuilt.error is None


def test_planner_repairs_a_build_envelope_an_older_removal_fence_damaged(
    tmp_path: Path,
) -> None:
    """The planner rebuilds a row whose stored envelope no longer parses.

    An older Controller's removal merged ``removal_fence`` and ``cancelled``
    into the stored ``RecipeBuildRequest``.  The row is damaged history, not a
    live request, so the planner must replace its documents with a freshly
    derived envelope instead of rejecting the operator's build with
    ``build.plan_invalid``.  Repair derives new bytes; it never blesses the
    damaged document.
    """

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    planned = service.plan(revision.id, node_id, now=now)
    damaged = copy.deepcopy(planned.agent_payload) | {
        "removal_fence": "00000000-0000-4000-8000-0000000000ff",
        "cancelled": True,
    }
    table = RecipeBuild.__table__
    assert isinstance(table, Table)
    with sessions.begin() as session:
        session.execute(
            table.update()
            .where(RecipeBuild.id == planned.build_id)
            .values(
                plan=damaged,
                state="failed",
                error="recipe Controller cache removal cancelled the build",
            )
        )

    repaired = service.plan(revision.id, node_id, now=now)

    assert parse_stored_build_plan(repaired.agent_payload).build_id == planned.build_id
    with sessions() as session:
        stored = session.get(RecipeBuild, planned.build_id)
        assert stored is not None
        assert stored.state == "planned"
        assert stored.error is None
        repaired_document = _json_object(stored.plan)
        # Repair replaces the damaged bytes; the forbidden engine keys are gone.
        assert "removal_fence" not in repaired_document
        assert "cancelled" not in repaired_document
        assert parse_stored_build_plan(repaired_document).build_id == planned.build_id


def test_planner_fails_closed_for_an_in_flight_build_with_a_damaged_envelope(
    tmp_path: Path,
) -> None:
    """Repair is for stale history; an active attempt is not overwritten.

    The tolerant read only applies to a row whose stored envelope is stale
    metadata.  A row that a worker is currently building is still owned by that
    attempt, so a damaged envelope there stays a named ``build.plan_invalid``
    rejection rather than being rewritten under the running work.
    """

    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    planned = service.plan(revision.id, node_id, now=now)
    damaged = copy.deepcopy(planned.agent_payload) | {"removal_fence": "x"}
    with sessions.begin() as session:
        stored = session.get(RecipeBuild, planned.build_id)
        assert stored is not None
        stored.state = "building"
        stored.plan = damaged

    with pytest.raises(RecipeBuildError) as raised:
        service.plan(revision.id, node_id, now=now)

    assert raised.value.code == "build.plan_invalid"
    assert "removal_fence" in str(raised.value)


def test_build_rejects_builder_without_runtime_identity(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.binary_digest = None

    with pytest.raises(RecipeBuildError, match="inactive or incompatible"):
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=now
        )


def test_build_plan_passes_the_installed_agent_claim_boundary(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )
    payload_digest = hashlib.sha256(
        canonical_payload(ProtocolOperation.RECIPE_BUILD, plan.agent_payload)
    ).hexdigest()

    claim = AgentClaim(
        schema_version=1,
        job_id="00000000-0000-4000-8000-000000000001",
        operation_id="00000000-0000-4000-8000-000000000002",
        attempt=1,
        fence="00000000-0000-4000-8000-000000000003",
        node_id=node_id,
        operation=ProtocolOperation.RECIPE_BUILD,
        authority_revision="a" * 64,
        payload_digest=payload_digest,
        payload=RecipeBuildRequest.model_validate(plan.agent_payload),
        deadline=now,
    )

    assert claim.payload["platform"] == "linux/arm64"


def test_starting_build_atomically_reserves_temporary_disk_and_memory(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )

    operation = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="build-reservation-test",
    )

    with sessions() as session:
        reservations = tuple(
            session.scalars(
                select(ResourceReservation)
                .where(
                    ResourceReservation.owner_kind == "recipe-build",
                    ResourceReservation.owner_id == plan.build_id,
                    ResourceReservation.state == "active",
                )
                .order_by(ResourceReservation.kind)
            )
        )
    limits = _json_object(plan.agent_payload["limits"])
    assert [(item.kind, item.amount_bytes) for item in reservations] == [
        (
            "disk",
            max(
                require_integer(limits["temporary_bytes"], "temporary bytes"),
                require_integer(
                    plan.agent_payload["source_bundle_bytes"], "source bundle bytes"
                )
                + require_integer(limits["output_bytes"], "output bytes")
                + require_integer(
                    plan.agent_payload["base_image_storage_bytes"],
                    "base image storage bytes",
                ),
            ),
        ),
        ("host-memory", require_integer(limits["memory_bytes"], "memory bytes")),
    ]

    operations.record_node_result(
        operation.id,
        node_id,
        succeeded=False,
        evidence={"reason": "expected test failure"},
    )
    with sessions() as session:
        assert (
            session.scalar(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "recipe-build",
                    ResourceReservation.owner_id == plan.build_id,
                    ResourceReservation.state == "active",
                )
            )
            is None
        )


@pytest.mark.parametrize(
    ("source_state", "outcome"),
    [
        ("running", "success"),
        ("waiting-for-operator", "success"),
        ("running", "failed"),
        ("waiting-for-operator", "mismatch"),
    ],
)
def test_cancelled_build_keeps_capacity_until_cleanup_is_confirmed(
    tmp_path: Path,
    source_state: str,
    outcome: str,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    original = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="cancel-build-test",
    )
    with sessions.begin() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == original.id)
        )
        assert child is not None
        child.state = source_state
        stored_job = session.get(Job, original.id)
        assert stored_job is not None
        stored_job.state = source_state
        child.current_attempt = 1
        child_id = child.id
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.capabilities = [*node.capabilities, "recipe.build.cleanup.v1"]
    if source_state == "waiting-for-operator":
        with sessions.begin() as session:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.capabilities = [
                capability
                for capability in node.capabilities
                if capability != "recipe.build.cleanup.v1"
            ]
        operations.cancel(
            original.id,
            actor="admin",
            request_id="d75c1b26-f9f5-48b0-94a3-8190bf7c181f",
            reason="remove recipe cache",
        )
        with sessions.begin() as session:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.capabilities = [*node.capabilities, "recipe.build.cleanup.v1"]
        assert operations.reconcile_cancelled_builds()
    else:
        operations.cancel(
            original.id,
            actor="admin",
            request_id="d75c1b26-f9f5-48b0-94a3-8190bf7c181f",
            reason="remove recipe cache",
        )
    with sessions() as session:
        original_job = session.get(Job, original.id)
        assert original_job is not None and original_job.state == source_state
        assert original_job.result is not None
        assert original_job.result["cancel_requested"] is True
        # A cancellation without a parseable instant can never authorise the
        # bounded cleanup STOP and would wedge the node forever.
        assert superseded_cancellation_deadline(original_job.result) is not None
        assert (
            session.scalar(
                select(ResourceReservation).where(
                    ResourceReservation.owner_id == plan.build_id,
                    ResourceReservation.state == "active",
                )
            )
            is not None
        )
        cleanup = session.scalar(
            select(Job).where(Job.kind == "recipe.build.cleanup.v1")
        )
        assert cleanup is not None
        cleanup_id = cleanup.id
        cleanup_child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == cleanup.id)
        )
        assert (
            cleanup_child is not None
            and cleanup_child.payload["operation_id"] == child_id
        )
    assert not operations.reconcile_cancelled_builds()
    # A late execution result must not publish the removed image, lose the
    # cancellation metadata, or release the reservation before cleanup.
    operations.record_node_result(
        original.id,
        node_id,
        succeeded=source_state == "running",
        evidence={
            "build_input_sha256": plan.build_input_sha256,
            "image_bytes": 500,
            "image_digest": "sha256:" + "b" * 64,
            "oci_layout_sha256": "c" * 64,
            "policy": {"dockerfile": "Dockerfile", "findings": [], "passed": True},
        }
        if source_state == "running"
        else {"reason": "execution stopped after cancellation"},
    )
    with sessions.begin() as session:
        AgentJobService(sessions, clock=lambda: now)._aggregate_parent(
            session, original.id
        )
        removed = session.get(RecipeBuild, plan.build_id)
        assert removed is not None and removed.state == "failed"
    evidence = {
        "schema_version": 1,
        "build_id": plan.build_id,
        "operation_id": child_id,
        "stopped": True,
    }
    if outcome == "mismatch":
        evidence["operation_id"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        with pytest.raises(RecipeOperationConflict, match="cleanup evidence"):
            operations.record_node_result(
                cleanup_id, node_id, succeeded=True, evidence=evidence
            )
    elif outcome == "failed":
        operations.record_node_result(
            cleanup_id,
            node_id,
            succeeded=False,
            evidence={"reason": "systemd stop failed"},
        )
    else:
        operations.record_node_result(
            cleanup_id, node_id, succeeded=True, evidence=evidence
        )
    with sessions() as session:
        remaining = session.scalar(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == plan.build_id,
                ResourceReservation.state == "active",
            )
        )
        original_job = session.get(Job, original.id)
        assert original_job is not None
        assert superseded_cancellation_deadline(original_job.result) is not None
        if outcome == "success":
            assert original_job.state == "cancelled"
            assert remaining is None
        else:
            assert remaining is not None
            assert original_job.state == "waiting-for-operator"


@pytest.mark.parametrize(
    ("operation_state", "build_state"),
    (
        ("failed", "failed"),
        ("waiting-for-operator", "building"),
        ("expired", "building"),
    ),
)
def test_terminal_build_can_be_retried_once_with_fresh_fencing_and_capacity(
    tmp_path: Path, operation_state: str, build_state: str
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    first = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="initial-build",
    )
    with sessions.begin() as session:
        job = session.get(Job, first.id)
        assert job is not None
        job.state = operation_state
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
        )
        assert child is not None
        child.state = operation_state
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        build.state = build_state

    retried = operations.retry(first.id, actor="admin", request_id="retry-build")
    repeated = operations.retry(first.id, actor="admin", request_id="retry-build")

    assert repeated == retried
    assert retried.id != first.id
    assert retried.owner_id == plan.build_id
    assert retried.state == "running"
    with sessions() as session:
        stored_build = session.get(RecipeBuild, plan.build_id)
        assert stored_build is not None
        assert stored_build.state == "building"
        children = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id.in_((first.id, retried.id))
                )
            )
        )
        assert len(children) == 2
        assert children[0].id != children[1].id
        reservations = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "recipe-build",
                    ResourceReservation.owner_id == plan.build_id,
                )
            )
        )
        assert sum(item.state == "active" for item in reservations) == 2
        assert sum(item.state == "released" for item in reservations) == 2


@pytest.mark.parametrize("force", [False, True])
def test_fresh_build_request_retries_matching_failed_build_idempotently(
    tmp_path: Path,
    force: bool,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    first = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="failed-acceptance-build",
    )
    with sessions.begin() as session:
        job = session.get(Job, first.id)
        assert job is not None
        job.state = "failed"
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
        )
        assert child is not None
        child.state = "failed"
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        build.state = "failed"

    retried = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="fresh-acceptance-build",
        force=force,
    )
    replay = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="fresh-acceptance-build",
        force=force,
    )

    assert retried == replay
    assert retried.id != first.id
    assert retried.owner_id == plan.build_id
    assert retried.state == "running"
    with sessions() as session:
        stored_build = session.get(RecipeBuild, plan.build_id)
        assert stored_build is not None
        assert stored_build.state == "building"
        stored_job = session.get(Job, retried.id)
        assert stored_job is not None
        assert stored_job.request_id == "fresh-acceptance-build"


def test_successful_build_retry_converges_original_and_new_request_keys(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    first = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="initial-build",
    )
    with sessions.begin() as session:
        job = session.get(Job, first.id)
        assert job is not None
        job.state = "waiting-for-operator"
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == first.id)
        )
        assert child is not None
        child.state = "waiting-for-operator"

    retried = operations.retry(first.id, actor="admin", request_id="retry-build")
    succeeded = operations.record_node_result(
        retried.id,
        node_id,
        succeeded=True,
        evidence={
            "build_input_sha256": plan.build_input_sha256,
            "image_bytes": 500,
            "image_digest": "sha256:" + "b" * 64,
            "oci_layout_sha256": "c" * 64,
            "policy": {
                "dockerfile": "Dockerfile",
                "findings": [],
                "passed": True,
            },
        },
    )
    assert succeeded.state == "succeeded"

    original_replay = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="initial-build",
    )
    new_replay = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="fresh-acceptance-build",
    )
    repeated_replay = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="fresh-acceptance-build",
    )

    assert original_replay == succeeded
    assert new_replay == repeated_replay
    assert new_replay.id != succeeded.id
    assert new_replay.state == "succeeded"
    assert new_replay.result == succeeded.result
    with sessions() as session:
        stored_job = session.get(Job, new_replay.id)
        assert stored_job is not None
        assert stored_job.request_id == "fresh-acceptance-build"
        assert (
            session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == new_replay.id
                )
            )
            is None
        )


@pytest.mark.parametrize("completed_before_restart", [False, True])
def test_forced_image_build_resumes_after_worker_restart(
    tmp_path: Path,
    completed_before_restart: bool,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    runtime = {"recipe_revision_id": revision.id, "builder_node_id": node_id}

    def add_parent(parent_id: str):
        with sessions.begin() as session:
            session.add(
                Job(
                    id=parent_id,
                    request_id=parent_id,
                    kind="recipe.image.availability.v2",
                    state="running",
                    actor="operator",
                    authority_revision=revision.id,
                    targets=[revision.id],
                    payload_digest="a" * 64,
                    payload={
                        "runtime": runtime,
                        "recipe_revision_id": revision.id,
                        "build_input_sha256": plan.build_input_sha256,
                    },
                    current_attempt=1,
                    created_at=now,
                    updated_at=now,
                )
            )

    parent_id = "00000000-0000-4000-8000-000000000731"
    add_parent(parent_id)

    def production():
        return build_recipe_image_availability(
            sessions,
            artifact_root=tmp_path / "artifacts",
            managed_catalog_sync=None,
            recipe_builds=builds,
            recipe_operations=operations,
            clock=lambda: now,
        )

    def complete_build(_progress=None):
        with sessions() as session:
            job = session.scalar(
                select(Job).where(
                    Job.kind == "recipe.build.v1",
                    Job.state == "running",
                )
            )
            assert job is not None
            job_id = job.id
        operations.record_node_result(
            job_id,
            node_id,
            succeeded=True,
            evidence={
                "build_input_sha256": plan.build_input_sha256,
                "image_bytes": 500,
                "image_digest": "sha256:" + "b" * 64,
                "oci_layout_sha256": "c" * 64,
                "policy": {"dockerfile": "Dockerfile", "findings": [], "passed": True},
            },
        )

    # An explicit rebuild must not reuse this older successful receipt when
    # its own running child is replayed after the worker stops.
    operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="operator",
        request_id="original-cached-build",
    )
    complete_build()

    first = production()
    assert first.service._builder is not None
    claim = first.service.claim_pending(limit=1)[0]
    recipe = RecipeDefinition.model_validate_json(json.dumps(revision.document))
    with pytest.raises(RecipeImageAvailabilityError) as waiting:
        first.service._builder(
            recipe,
            runtime,
            claim=claim,
            build_input_sha256=plan.build_input_sha256,
            force=True,
            progress=lambda _: None,
        )
    assert waiting.value.code == "recipe_image.build_wait"
    first.close()
    if completed_before_restart:
        complete_build()

    restarted = production()
    assert restarted.service._builder is not None
    with sessions.begin() as session:
        parent = session.get(Job, parent_id)
        assert parent is not None
        parent.payload = dict(parent.payload) | {
            "claim_until": (now - timedelta(seconds=1)).isoformat()
        }
    claim = restarted.service.claim_pending(limit=1)[0]
    if not completed_before_restart:
        with pytest.raises(RecipeImageAvailabilityError) as waiting:
            restarted.service._builder(
                recipe,
                runtime,
                claim=claim,
                build_input_sha256=plan.build_input_sha256,
                force=True,
                progress=lambda _: None,
            )
        assert waiting.value.code == "recipe_image.build_wait"
        complete_build()
    result = restarted.service._builder(
        recipe,
        runtime,
        claim=claim,
        build_input_sha256=plan.build_input_sha256,
        force=True,
        progress=lambda _: None,
    )
    assert result["state"] == "succeeded"
    with sessions() as session:
        jobs = tuple(session.scalars(select(Job).where(Job.kind == "recipe.build.v1")))
        assert len(jobs) == 2
        assert all(job.state == "succeeded" for job in jobs)

    # A separate explicit download still requests a fresh build.
    new_parent_id = "00000000-0000-4000-8000-000000000732"
    add_parent(new_parent_id)
    new_claim = restarted.service.claim_pending(limit=1)[0]
    with pytest.raises(RecipeImageAvailabilityError) as waiting:
        restarted.service._builder(
            recipe,
            runtime,
            claim=new_claim,
            build_input_sha256=plan.build_input_sha256,
            force=True,
            progress=lambda _: None,
        )
    assert waiting.value.code == "recipe_image.build_wait"
    complete_build()
    restarted.service._builder(
        recipe,
        runtime,
        claim=new_claim,
        build_input_sha256=plan.build_input_sha256,
        force=True,
        progress=lambda _: None,
    )
    with sessions() as session:
        jobs = tuple(session.scalars(select(Job).where(Job.kind == "recipe.build.v1")))
        assert len(jobs) == 3
        assert all(job.state == "succeeded" for job in jobs)
    restarted.close()


def test_build_plan_rejects_disk_below_concurrent_oci_export_peak(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    projected = revision.projected
    build_resources = _json_object(projected["build_resources"])
    source_bytes = len(
        bundles.get(_json_text(projected["source_bundle_sha256"])).archive
    )
    temporary_bytes = require_integer(
        build_resources["temporary_bytes"], "temporary bytes"
    )
    roles = _json_array(_json_object(revision.document["topology"])["roles"])
    output_bytes = max(
        require_integer(
            _json_object(_json_object(_json_object(role)["resources"])["disk"])[
                "image_bytes"
            ],
            "role image bytes",
        )
        for role in roles
    )
    base_image_bytes = require_integer(
        build_resources["download_bytes"], "download bytes"
    )
    peak_bytes = max(temporary_bytes, base_image_bytes + source_bytes + output_bytes)
    disk_total_bytes = 2 * 1024**4
    required_bytes = peak_bytes + recipe_builds_module._build_disk_reserve(
        disk_total_bytes
    )
    # The envelope itself fits, but accepting it would violate the filesystem
    # reserve retained for the Spark host.
    newer = now + timedelta(seconds=1)
    InventoryRepository(sessions, clock=lambda: newer).record(
        InventorySnapshotInput(
            node_id,
            newer,
            disk_total_bytes,
            required_bytes - 1,
            100_000,
            80_000,
            100_000,
            80_000,
            1,
            False,
            ("recipe.build.v1",),
            memory_pool="separate",
        )
    )

    with pytest.raises(RecipeBuildError, match="temporary disk capacity"):
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=newer
        )


def test_build_plan_accepts_public_network_only_with_egress_boundary_capability(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(
        tmp_path, network={"mode": "public", "hosts": ["pypi.org"]}
    )

    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )
    assert plan.agent_payload["network"] == {
        "mode": "public",
        "hosts": ["pypi.org"],
    }

    # Job claims report executable operations; probed host capabilities are
    # recorded separately by inventory. Exercise the contact write that happens
    # every poll before planning another public-network build.
    with sessions.begin() as session:
        session.add(
            AgentCertificate(
                serial="builder-serial",
                node_id=node_id,
                not_before=now - timedelta(seconds=1),
                not_after=now + timedelta(hours=1),
                fingerprint="builder-fingerprint",
            )
        )
    assert (
        AgentJobService(sessions, clock=lambda: now).claim(
            node_id,
            "builder-serial",
            30,
            capabilities=[
                "agent.runtime.rust.v1",
                "recipe.build.v1",
                "recipe.image.import.v1",
            ],
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": "1.2.3",
                "build_digest": "sha256:" + "a" * 64,
                "binary_digest": "1" * 64,
                "self_test_passed": True,
                "observation_receipt_public_key": "d" * 64,
            },
        )
        is None
    )
    after_claim = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )
    assert after_claim.build_input_sha256 == plan.build_input_sha256


def test_public_build_rejects_stale_inventory_without_egress_capability(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(
        tmp_path, network={"mode": "public", "hosts": ["pypi.org"]}
    )
    newer = now + timedelta(seconds=1)
    InventoryRepository(sessions, clock=lambda: newer).record(
        InventorySnapshotInput(
            node_id,
            newer,
            2 * 1024**4,
            1 * 1024**4,
            100_000,
            80_000,
            100_000,
            80_000,
            1,
            False,
            ("recipe.build.v1", "recipe.image.import.v1"),
            memory_pool="separate",
        )
    )

    with pytest.raises(RecipeBuildError, match="fresh builder inventory"):
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=newer
        )


def test_source_check_returns_the_structured_pre_dispatch_policy_report(
    tmp_path: Path,
) -> None:
    sessions, bundles, _now, _node_id, revision = setup(tmp_path)
    # The check is independent of builder capacity and exposes every finding to the UI.
    report = RecipeBuildService(sessions, bundles=bundles).check_source(revision.id)

    assert report.passed is True
    assert report.findings == ()
    assert report.source_bundle_sha256


def test_success_does_not_claim_isolated_build_image_is_installed(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    plan = service.plan(revision.id, node_id, now=now)

    completed = service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )

    assert completed.image_digest == "sha256:" + "b" * 64
    with sessions() as session:
        artifact = session.scalar(
            select(NodeArtifact).where(NodeArtifact.node_id == node_id)
        )
        assert artifact is None


def test_build_result_refreshes_upload_evidence_after_a_retried_attempt(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )
    old_image = "sha256:" + "a" * 64
    new_image = "sha256:" + "b" * 64
    old_layout = "c" * 64
    new_layout = "d" * 64
    with sessions.begin() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        build.image_digest = old_image
        build.oci_layout_sha256 = old_layout
        build.image_bytes = 400

    stale_session = sessions()
    try:
        stale_build = stale_session.get(RecipeBuild, plan.build_id)
        assert stale_build is not None and stale_build.image_digest == old_image
        with sessions.begin() as upload_session:
            uploaded = upload_session.get(RecipeBuild, plan.build_id)
            assert uploaded is not None
            uploaded.image_digest = new_image
            uploaded.oci_layout_sha256 = new_layout
            uploaded.image_bytes = 500

        _record_build_evidence(
            stale_session,
            stale_build,
            {
                "build_input_sha256": plan.build_input_sha256,
                "image_bytes": 500,
                "image_digest": new_image,
                "oci_layout_sha256": new_layout,
                "policy": {
                    "dockerfile": "Dockerfile",
                    "findings": [],
                    "passed": True,
                },
            },
            now=now,
        )
        assert stale_build.image_digest == new_image
    finally:
        stale_session.rollback()
        stale_session.close()


def test_build_result_accepts_protocol_frozen_empty_findings(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    operation_view = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="admin",
        request_id="frozen-policy-result",
    )
    image_digest = "sha256:" + "b" * 64
    layout_digest = "c" * 64
    with sessions.begin() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        build.image_digest = image_digest
        build.oci_layout_sha256 = layout_digest
        build.image_bytes = 500
        agent_operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == operation_view.id
            )
        )
        assert agent_operation is not None
        operation_id = agent_operation.id

    message = AgentResult.parse(
        {
            "schema_version": 1,
            "job_id": operation_view.id,
            "operation_id": operation_id,
            "attempt": 1,
            "fence": "33333333-3333-4333-8333-333333333333",
            "node_id": node_id,
            "deadline": "2026-08-11T20:30:00+00:00",
            "state": "succeeded",
            "result": {
                "build_input_sha256": plan.build_input_sha256,
                "image_bytes": 500,
                "image_digest": image_digest,
                "oci_layout_sha256": layout_digest,
                "policy": {
                    "dockerfile": "Dockerfile",
                    "findings": [],
                    "passed": True,
                },
            },
        }
    )
    with sessions.begin() as session:
        agent_operation = session.get(AgentOperation, operation_id)
        assert agent_operation is not None
        agent_operation.state = "succeeded"
        operations.consume_agent_result(session, agent_operation, object(), message)

    with sessions() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None and build.state == "succeeded"
        job = session.get(Job, operation_view.id)
        assert job is not None and job.state == "succeeded"
        result = job.result
        assert result is not None
        evidence = _json_object(result["node_evidence"])
        assert _json_object(_json_object(evidence[node_id])["policy"])["findings"] == []
        assert (
            session.scalar(select(NodeArtifact).where(NodeArtifact.node_id == node_id))
            is None
        )


def _authorize_distribution_fixture(sessions, plan, revision, now):
    """Wire/import tests start after the image verifier has produced its receipt."""
    with sessions.begin() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        assert build.image_digest is not None
        assert build.oci_layout_sha256 is not None
        assert build.image_bytes is not None
        receipt = RuntimeImageReceipt(
            schema_version=2,
            source="controller-build",
            distribution_publisher=revision.publisher,
            distribution_slug=revision.slug,
            distribution_content_sha256=revision.content_digest,
            registry_manifest_digest=None,
            image_digest=build.image_digest,
            platform_manifest_digest=build.image_digest,
            oci_archive_sha256=build.oci_layout_sha256,
            image_bytes=build.image_bytes,
            local_image_config_id="sha256:" + "c" * 64,
            local_image_reference=None,
            architecture="linux-arm64",
            runtime_interface="vonk.runtime.v1",
            runtime_interface_label="v1",
            archive_path="/verified/fixture/archive",
            recorded_at=now.isoformat(),
            build_id=build.id,
            build_input_sha256=build.build_input_sha256,
            runtime_adapter=_CACHED_ADAPTER.adapter_id,
            runtime_adapter_sha256=_CACHED_ADAPTER.digest,
        )
        persist_runtime_image_receipt(
            session,
            recipe_revision_id=revision.id,
            original_content_digest=revision.content_digest,
            effective_execution_key="a" * 64,
            receipt=receipt,
            verified_at=now,
        )
    return receipt


def test_distribution_reimports_one_build_digest_for_every_mapped_node(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, builder, revision = setup(tmp_path)
    service = RecipeBuildService(
        sessions, bundles=bundles, prepared_builds=lambda *_args, **_kwargs: receipt
    )
    plan = service.plan(revision.id, builder, now=now)
    service.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    receipt = _authorize_distribution_fixture(sessions, plan, revision, now)
    target = "spk_" + "2" * 32
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=target,
                state="active",
                architecture="linux-arm64",
                capabilities=["recipe.image.import.v1"],
            )
        )
        session.add(
            NodeArtifact(
                node_id=builder,
                kind="image",
                digest="b" * 64,
                source="docker-archive:" + "c" * 64,
                size_bytes=500,
                state="verified",
                ref_count=0,
                verified_at=now,
                updated_at=now,
            )
        )
        mapping = ClusterMapping(
            recipe_revision_id=revision.id,
            topology_name="synthetic-test",
            generation=1,
            node_count=2,
            state="ready",
            parameters={},
            placement_digest="d" * 64,
            endpoint_owner_node_id=builder,
            created_by="admin",
            created_at=now,
            updated_at=now,
        )
        session.add(mapping)
        session.flush()
        session.add_all(
            (
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=builder,
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=now,
                ),
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=target,
                    rank=1,
                    role="worker",
                    endpoint_owner=False,
                    created_at=now,
                ),
            )
        )
        mapping_id = mapping.id

    distribution = service.plan_distribution(plan.build_id, mapping_id, generation=1)

    assert [item[0] for item in distribution.targets] == [builder, target]
    assert {item[1]["image_digest"] for item in distribution.targets} == {
        "sha256:" + "b" * 64
    }
    assert distribution.targets[0][1]["kind"] == "recipe.image.import.v1"


def test_image_distribution_requires_the_previewed_plan_digest(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, builder, revision = setup(tmp_path)
    builds = RecipeBuildService(
        sessions, bundles=bundles, prepared_builds=lambda *_args, **_kwargs: receipt
    )
    build_plan = builds.plan(revision.id, builder, now=now)
    builds.record_success(
        build_plan.build_id,
        build_input_sha256=build_plan.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    receipt = _authorize_distribution_fixture(sessions, build_plan, revision, now)
    target = "spk_" + "2" * 32
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=target,
                state="active",
                architecture="linux-arm64",
                capabilities=["recipe.image.import.v1"],
            )
        )
        mapping = ClusterMapping(
            recipe_revision_id=revision.id,
            topology_name="synthetic-test",
            generation=1,
            node_count=2,
            state="ready",
            parameters={},
            placement_digest="d" * 64,
            endpoint_owner_node_id=builder,
            created_by="admin",
            created_at=now,
            updated_at=now,
        )
        session.add(mapping)
        session.flush()
        session.add_all(
            (
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=builder,
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    created_at=now,
                ),
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=target,
                    rank=1,
                    role="worker",
                    endpoint_owner=False,
                    created_at=now,
                ),
            )
        )
        mapping_id = mapping.id

    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    preview = operations.preview_image_distribution(
        build_plan.build_id,
        mapping_id,
        mapping_generation=1,
    )

    assert preview.image_digest == "sha256:" + "b" * 64
    assert preview.node_ids == (builder, target)
    assert len(preview.plan_digest) == 64
    with pytest.raises(
        RecipeOperationConflict,
        match="submitted image distribution plan does not match preview",
    ):
        operations.distribute_image(
            build_plan.build_id,
            mapping_id,
            mapping_generation=1,
            plan_digest="0" * 64,
            actor="admin",
            request_id="stale-distribution",
        )

    operation = operations.distribute_image(
        build_plan.build_id,
        mapping_id,
        mapping_generation=1,
        plan_digest=preview.plan_digest,
        actor="admin",
        request_id="accepted-distribution",
    )
    assert operation.kind == "recipe.image.import.v1"
    assert operation.plan_digest == preview.plan_digest
    assert operation.nodes == (builder, target)


@pytest.mark.parametrize(
    "settings",
    [
        {
            "kind": "generation",
            "context_tokens": 4096,
            "change_effects": {"context_tokens": "rebuild"},
        },
        None,
    ],
)
def test_build_readers_reject_retired_or_null_persisted_settings(
    tmp_path, settings
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    with sessions.begin() as session:
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None
        document = copy.deepcopy(row.document)
        document["settings"] = settings
        # Corrupt stored JSON directly so the read boundary is exercised;
        # ordinary catalog writes independently enforce immutability.
        table = CatalogDocumentRevision.__table__
        assert isinstance(table, Table)
        session.execute(
            table.update()
            .where(CatalogDocumentRevision.id == revision.id)
            .values(document=document)
        )
    service = RecipeBuildService(sessions, bundles=bundles)
    for action in (
        lambda: service.resolve(revision.id),
        lambda: service.plan(revision.id, node_id, now=now),
    ):
        with pytest.raises(RecipeBuildError, match="stored recipe") as error:
            action()
        assert error.value.code == "build.contract_invalid"
    with sessions() as session:
        assert session.scalar(select(RecipeBuild)) is None


def test_build_readers_require_persisted_settings(tmp_path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    with sessions.begin() as session:
        row = session.get(CatalogDocumentRevision, revision.id)
        assert row is not None
        document = copy.deepcopy(row.document)
        document.pop("settings")
        # Corrupt stored JSON directly so the read boundary is exercised;
        # ordinary catalog writes independently enforce immutability.
        table = CatalogDocumentRevision.__table__
        assert isinstance(table, Table)
        session.execute(
            table.update()
            .where(CatalogDocumentRevision.id == revision.id)
            .values(document=document)
        )
    service = RecipeBuildService(sessions, bundles=bundles)
    with pytest.raises(RecipeBuildError, match="stored recipe"):
        service.resolve(revision.id)
    with pytest.raises(RecipeBuildError, match="stored recipe"):
        service.plan(revision.id, node_id, now=now)


def test_persisted_canonical_settings_preserve_build_identity_and_rebuild_changes(
    tmp_path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    catalog = CatalogEntityService(sessions, clock=lambda: now)
    document = copy.deepcopy(revision.document)
    _json_object(document["settings"])["knobs"] = {
        "compiler": {"value": "clang", "change_effect": "rebuild"},
        "enabled": {"value": False, "change_effect": "rebuild"},
        "count": {"value": 0, "change_effect": "rebuild"},
        "label": {"value": "", "change_effect": "rebuild"},
    }
    canonical = RecipeDefinition.model_validate(document)

    def publish(document):
        draft = catalog.revise(revision.document_id, document, actor="admin")
        with sessions.begin() as session:
            stored = session.get(CatalogDocumentRevision, draft.id)
            assert stored is not None
            stored.projected = copy.deepcopy(revision.projected)
        return catalog.resolve(draft.id, actor="admin")

    selected = publish(canonical.model_dump(mode="json"))
    service = RecipeBuildService(sessions, bundles=bundles)
    first = service.plan(selected.id, node_id, now=now)
    assert recipe_builds_module._build_effective_settings(canonical.settings) == {
        "values": {
            "knobs.compiler": "clang",
            "knobs.enabled": False,
            "knobs.count": 0,
            "knobs.label": "",
        },
        "change_effects": {
            name: "rebuild"
            for name in (
                "knobs.compiler",
                "knobs.enabled",
                "knobs.count",
                "knobs.label",
            )
        },
    }
    assert (
        service.plan(selected.id, node_id, now=now).build_input_sha256
        == first.build_input_sha256
    )
    _json_object(_json_object(_json_object(document["settings"])["knobs"])["compiler"])[
        "value"
    ] = "gcc"
    changed = publish(document)
    assert (
        service.plan(changed.id, node_id, now=now).build_input_sha256
        != first.build_input_sha256
    )
