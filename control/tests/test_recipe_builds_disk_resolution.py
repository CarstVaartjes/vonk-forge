"""Recipe builds: disk resolution."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import vonk_control.availability_production.production as availability_production_module
import vonk_control.runtime.adapters as runtime_adapters_module
from sqlalchemy import select
from vonk_agent_protocol import (
    LifecycleState,
    canonical_message,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.state_machines import NodeIdentityState
from vonk_control.availability_production.production import (
    build_recipe_image_availability,
)
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.fleet import AgentNode, AgentNodeContact
from vonk_control.models.jobs import Job
from vonk_control.recipe_builds.common import (
    RecipeBuildError,
    _build_disk_reserve,
)
from vonk_control.recipe_builds.service import RecipeBuildService
from vonk_control.runtime_image_preparation.contracts import (
    PulledImageEvidence,
)
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from vonk_forge_contracts import document_sha256, read_recipe

from .display_fixtures import reword
from .recipe_builds_support import (
    _CACHED_ADAPTER,
    _json_object,
    _write_controller_build_receipt,
    setup,
)
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    inventory_items_fixture,
    set_inventory_identity,
)
from .runtime_image_fixtures import place_test_image, remove_test_image


def test_build_disk_reserve_scales_to_the_spark_cap() -> None:
    assert _build_disk_reserve(100 * 1024**3) == 4 * 1024**3
    assert _build_disk_reserve(4 * 1024**4) == 64 * 1024**3


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
