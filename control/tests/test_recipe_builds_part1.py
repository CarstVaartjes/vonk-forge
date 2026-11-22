from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import vonk_control.availability_production.production as availability_production_module
import vonk_control.recipe_builds as recipe_builds_module
import vonk_control.runtime.adapters as runtime_adapters_module
from sqlalchemy import Table, select
from vonk_agent_protocol import (
    AgentClaim,
    LifecycleState,
    ProgressPhase,
    RecipeBuildRequest,
    canonical_message,
)
from vonk_agent_protocol import (
    AgentOperation as ProtocolOperation,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.agent_jobs import AgentJobService, superseded_cancellation_deadline
from vonk_control.availability_production.production import (
    build_recipe_image_availability,
)
from vonk_control.bounded_json import require_integer
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.models import (
    AgentNode,
    AgentNodeContact,
    AgentOperation,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    ResourceReservation,
)
from vonk_control.recipe_builds import RecipeBuildError, RecipeBuildService
from vonk_control.recipe_execution_contract import parse_stored_build_plan
from vonk_control.recipe_image_availability import (
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_image_removal_contract import RecipeCacheRemovalOwner
from vonk_control.recipe_operations import (
    RecipeOperationService,
)
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime.adapters import resolve_runtime_adapter
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImageReceipt,
)
from vonk_forge_contracts import RecipeDefinition, document_sha256, read_recipe

from .display_fixtures import reword
from .recipe_builds_support import (
    RecordingQueue,
    _json_object,
    _restore_builder_inventory,
    setup,
)
from .recipe_removal_review_support import remove_after_review
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    inventory_items_fixture,
    set_inventory_identity,
)
from .runtime_image_fixtures import place_test_image, remove_test_image

_CACHED_ADAPTER = resolve_runtime_adapter("vllm", {"node_count": 1})


def test_build_disk_reserve_scales_to_the_spark_cap() -> None:
    assert recipe_builds_module._build_disk_reserve(100 * 1024**3) == 4 * 1024**3
    assert recipe_builds_module._build_disk_reserve(4 * 1024**4) == 64 * 1024**3


def test_build_plan_is_typed_sandboxed_and_durable(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)

    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )

    assert "command" not in plan.agent_payload
    assert plan.agent_payload["capabilities"] == ["DAC_OVERRIDE"]
    limits = _json_object(plan.agent_payload["limits"])
    assert limits["cpu_cores"] == 6
    assert limits["processes"] == 2048
    assert plan.agent_payload["base_images"] == [
        {
            "manifest_digest": "sha256:" + "a" * 64,
            "reference": "ghcr.io/example/vllm@sha256:" + "a" * 64,
        }
    ]
    assert plan.agent_payload["base_image_storage_bytes"] == 100
    assert plan.agent_payload["source_bundle_sha256"] == revision.source_bundle_sha256
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
        set_inventory_identity(node, binary_digest="2" * 64)

    with (
        pytest.raises(RecipeBuildError),
        sessions.begin() as session,
    ):
        service.persist_plan_in_session(session, prepared, now=now)

    with sessions() as session:
        assert session.get(RecipeBuild, prepared.build_id) is None
    # A fresh request observes the new accepted builder runtime.
    fresh = service.plan(revision.id, node_id, now=now)
    assert fresh.build_id != prepared.build_id


def test_build_identity_changes_when_builder_runtime_changes(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)

    first = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        set_inventory_identity(node, binary_digest="2" * 64)
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
        node.state = NodeIdentityState.RETIRED
        set_inventory_identity(node, binary_digest=None)

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

    digest = hashlib.sha256(archive).hexdigest()
    place_test_image(storage, digest, len(archive))
    staged = storage.existing_archive(digest, len(archive))
    return storage.commit(
        staged,
        receipt=RuntimeImageReceipt(
            schema_version=2,
            distribution_publisher="vonk",
            distribution_slug="cached",
            distribution_content_sha256=distribution_content_sha256,
            image_digest=image_digest,
            oci_archive_sha256=hashlib.sha256(archive).hexdigest(),
            image_bytes=len(archive),
            local_image_config_id="sha256:" + "c" * 64,
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
            assert archive_path.name == expected_archive_sha256
            return PulledImageEvidence(
                manifest_digest=image_digest,
                config_id="sha256:" + "c" * 64,
                local_reference="localhost/vonk/cached@" + image_digest,
                architecture=expected_architecture,
                runtime_interface="v1",
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    delayed = DelayedCachedResolution()
    monkeypatch.setattr(
        availability_production_module, "OciLayoutImageTransport", Transport
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=artifact_root,
        storage=FilesystemRuntimeImageStorage(artifact_root),
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


@pytest.mark.parametrize("damage", ["receipt", "size", LifecycleState.FAILED])
def test_present_archive_with_damaged_receipt_is_reprepared_not_rebuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    artifact_root = tmp_path / "artifacts"
    storage = FilesystemRuntimeImageStorage(artifact_root)
    archive = b"cached source build archive"
    archive_digest = hashlib.sha256(archive).hexdigest()
    # The upload producer publishes the archive; preparation publishes the
    # receipt. A Controller death in between must not force a rebuild.
    place_test_image(storage, archive_digest, len(archive))
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

    if damage in {"size", LifecycleState.FAILED}:
        _write_controller_build_receipt(
            storage,
            archive=archive,
            image_digest=image_digest,
            build_id=plan.build_id,
            build_input_sha256=plan.build_input_sha256,
            distribution_content_sha256=revision.content_digest,
        )
        with sessions.begin() as session:
            damaged = session.get(RecipeBuild, plan.build_id)
            assert damaged is not None
            if damage == "size":
                damaged.image_bytes = len(archive) + 1
            else:
                damaged.state = LifecycleState.FAILED
                damaged.error = "replacement attempt failed"

    resolution = builds.resolve(revision.id)

    assert resolution.cached is True
    assert resolution.stale_receipt is False
    assert resolution.receipt_pending is (damage == "receipt")
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
            assert archive_path.name == expected_archive_sha256
            return PulledImageEvidence(
                manifest_digest=image_digest,
                config_id="sha256:" + "c" * 64,
                local_reference="localhost/vonk/cached@" + image_digest,
                architecture=expected_architecture,
                runtime_interface="v1",
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    monkeypatch.setattr(
        availability_production_module, "OciLayoutImageTransport", Transport
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=artifact_root,
        storage=FilesystemRuntimeImageStorage(artifact_root),
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
    with sessions() as session:
        repaired = session.get(RecipeBuild, plan.build_id)
        assert repaired is not None and repaired.image_bytes == len(archive)
        assert repaired.state == LifecycleState.SUCCEEDED
        assert repaired.error is None
        layout, size = repaired.oci_layout_sha256, repaired.image_bytes
        assert layout is not None and size is not None
        assert storage.build_archive_available(layout, size)
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
    place_test_image(storage, archive_digest, len(archive))
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
                "distribution_publisher": "vonk",
                "distribution_slug": "cached",
                "distribution_content_sha256": revision.content_digest,
                "image_digest": image_digest,
                "oci_archive_sha256": archive_digest,
                "image_bytes": len(archive),
                "local_image_config_id": "sha256:" + "c" * 64,
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
            assert archive_path.name == expected_archive_sha256
            return PulledImageEvidence(
                manifest_digest=image_digest,
                config_id="sha256:" + "c" * 64,
                local_reference="localhost/vonk/cached@" + image_digest,
                architecture=expected_architecture,
                runtime_interface="v1",
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    monkeypatch.setattr(
        availability_production_module, "OciLayoutImageTransport", Transport
    )
    production = build_recipe_image_availability(
        sessions,
        artifact_root=artifact_root,
        storage=FilesystemRuntimeImageStorage(artifact_root),
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
    remove_test_image(storage, archive_digest)

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
                inventory_snapshot=inventory_items_fixture(
                    second_node,
                    observed_at=now,
                    identity=AgentRuntimeIdentity.model_validate(
                        {
                            **PACKAGED_RUNTIME_IDENTITY,
                            "architecture": "linux-arm64",
                            "semantic_version": "1.2.3",
                            "build_digest": "sha256:" + "a" * 64,
                            "binary_digest": "1" * 64,
                        }
                    ),
                ),
                contact=AgentNodeContact(last_seen_at=now),
            )
        )
    InventoryRepository(sessions, clock=lambda: now).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": second_node,
                    "observed_at": now,
                    "disk_total_bytes": 2 * 1024**4,
                    "disk_free_bytes": 1 * 1024**4,
                    "host_memory_total_bytes": 100_000,
                    "host_memory_free_bytes": 80_000,
                    "gpu_memory_total_bytes": 100_000,
                    "gpu_memory_free_bytes": 80_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": (
                        "recipe.build.v1",
                        "recipe.build.egress-proxy.v1",
                        "recipe.image.import.v1",
                    ),
                    "memory_pool": "separate",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
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
        reword(document, "Editorially renamed recipe")
        read_recipe(document)
        content_digest = document_sha256(document)
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
            source_bundle_sha256=current.source_bundle_sha256,
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
        node.state = NodeIdentityState.RETIRED
        set_inventory_identity(node, binary_digest=None)

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
        _json_object(_json_object(document["execution"])["build"])["network"] = {
            "hosts": ["pypi.org"]
        }
        read_recipe(document)
        content_digest = document_sha256(document)
        newer_revision = CatalogDocumentRevision(
            id=str(uuid.uuid4()),
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
            source_bundle_sha256=current.source_bundle_sha256,
            projected=copy.deepcopy(current.projected),
            created_by="test",
            created_at=now,
        )
        session.add(newer_revision)

    with pytest.raises(RecipeBuildError):
        service.plan(newer_revision.id, node_id, now=now, resolution=resolution)
    fresh = service.plan(newer_revision.id, node_id, now=now)
    assert fresh.recipe_content_sha256 == newer_revision.content_digest


def test_build_plan_from_intent_rechecks_selected_builder_capacity(
    tmp_path: Path,
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    resolution = service.resolve(revision.id)
    newer = now + timedelta(seconds=1)
    InventoryRepository(sessions, clock=lambda: newer).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node_id,
                    "observed_at": newer,
                    "disk_total_bytes": 2 * 1024**4,
                    "disk_free_bytes": 1,
                    "host_memory_total_bytes": 100_000,
                    "host_memory_free_bytes": 80_000,
                    "gpu_memory_total_bytes": 100_000,
                    "gpu_memory_free_bytes": 80_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": ("recipe.build.v1", "recipe.build.egress-proxy.v1"),
                    "memory_pool": "separate",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )

    with pytest.raises(RecipeBuildError):
        service.plan(revision.id, node_id, now=newer, resolution=resolution)

    fresh_time = newer + timedelta(seconds=1)
    _restore_builder_inventory(sessions, node_id, fresh_time)
    assert (
        RecipeBuildService(sessions, bundles=bundles)
        .plan(revision.id, node_id, now=fresh_time)
        .recipe_content_sha256
        == revision.content_digest
    )


def test_build_identity_changes_when_archive_format_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)

    first = service.plan(revision.id, node_id, now=now)
    monkeypatch.setattr(
        "vonk_control.recipe_builds.planning.BUILD_ARTIFACT_FORMAT", "future-archive-v2"
    )
    monkeypatch.setattr(
        "vonk_control.recipe_builds.source.BUILD_ARTIFACT_FORMAT", "future-archive-v2"
    )
    monkeypatch.setattr(
        "vonk_control.recipe_builds.persistence.BUILD_ARTIFACT_FORMAT",
        "future-archive-v2",
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
        set_inventory_identity(node, binary_digest="2" * 64)

    with (
        sessions.begin() as session,
        pytest.raises(RecipeBuildError),
    ):
        service.reserve_in_session(session, plan, now=now)
    fresh = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        service.reserve_in_session(session, fresh, now=now)


def test_damaged_reservation_projection_leaves_capacity_free_and_repairs(tmp_path):
    """A damaged stored envelope cannot reserve capacity or poison fresh work."""

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
        pytest.raises(RecipeBuildError),
    ):
        service.reserve_in_session(session, plan, now=now)

    # Damaged storage never admits an unverified envelope or leaves capacity.
    with sessions() as session:
        assert tuple(session.scalars(select(ResourceReservation))) == ()
    with sessions.begin() as session:
        session.execute(
            table.update()
            .where(RecipeBuild.id == plan.build_id)
            .values(plan=plan.agent_payload)
        )
    with sessions.begin() as session:
        service.reserve_in_session(session, plan, now=now)


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

    result = remove_after_review(
        availability,
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


@pytest.mark.usefixtures("damaged_json_rows")
def test_planner_repairs_active_projection_without_replacing_execution(tmp_path):
    """Damaged derived metadata cannot cancel or permanently veto exact work."""
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    service = RecipeBuildService(sessions, bundles=bundles)
    planned = service.plan(revision.id, node_id, now=now)
    with sessions.begin() as session:
        stored = session.get(RecipeBuild, planned.build_id)
        assert stored is not None
        stored.state = ProgressPhase.BUILDING.value
        stored.plan = copy.deepcopy(planned.agent_payload) | {"removal_fence": "x"}
    repaired = service.plan(revision.id, node_id, now=now)
    assert repaired.build_id == planned.build_id
    assert repaired.agent_payload == planned.agent_payload
    with sessions() as session:
        stored = session.get(RecipeBuild, planned.build_id)
        assert stored is not None and stored.state == ProgressPhase.BUILDING.value
        assert stored.plan == planned.agent_payload
    assert service.plan(revision.id, node_id, now=now).build_id == planned.build_id


def test_build_rejects_builder_without_runtime_identity(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        set_inventory_identity(node, binary_digest=None)

    with pytest.raises(RecipeBuildError):
        RecipeBuildService(sessions, bundles=bundles).plan(
            revision.id, node_id, now=now
        )

    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        set_inventory_identity(node, architecture="linux-arm64", binary_digest="1" * 64)
    assert (
        RecipeBuildService(sessions, bundles=bundles)
        .plan(revision.id, node_id, now=now)
        .recipe_content_sha256
        == revision.content_digest
    )


def test_build_plan_passes_the_installed_agent_claim_boundary(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )
    claim = AgentClaim(
        fence="00000000-0000-4000-8000-000000000003",
        operation=ProtocolOperation.RECIPE_BUILD,
        payload=RecipeBuildRequest.model_validate(plan.agent_payload),
        deadline=now,
    )

    assert claim.payload["dockerfile"] == "Dockerfile"


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
        ("needs-operator", "success"),
        ("running", "failed"),
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
            "image_bytes": 500,
            "image_digest": "sha256:" + "b" * 64,
            "oci_layout_sha256": "c" * 64,
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
    if outcome == "failed":
        operations.record_node_result(
            cleanup_id,
            node_id,
            succeeded=False,
            evidence={"reason": "systemd stop failed"},
        )
    else:
        operations.record_node_result(cleanup_id, node_id, succeeded=True, evidence={})
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
            assert original_job.state == "observing"
