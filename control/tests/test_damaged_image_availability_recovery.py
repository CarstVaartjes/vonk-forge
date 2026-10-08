"""Hosted OCI recovery through the production availability composition.

Only the external build effect is controlled, not a physical Spark execution.
OCI ingress, catalog/input resolution, SQL build ownership, preparation,
collection, cancellation and availability completion use their real owners.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import timedelta
from functools import partial
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import AgentResult, canonical_message
from vonk_control import runtime_image_preparation
from vonk_control.agent_jobs import AgentJobService
from vonk_control.availability_production import build_recipe_image_availability
from vonk_control.image_store_collection import GRACE, ImageStoreCollector
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import AgentCertificate, Job, ResourceReservation, User
from vonk_control.oci_image_store import OciImageStore, StoredImage
from vonk_control.recipe_availability_intent import RecipeBuildDependency
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    prepare_runtime_image,
)

from .preflight_fixtures import record_passing_preflight
from .test_oci_image_store import _docker_archive, _layer
from .test_recipe_builds import setup as build_setup

pytestmark = pytest.mark.needs_skopeo


def test_damaged_receipted_manifest_recovers_through_new_availability_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skopeo = shutil.which("skopeo")
    assert skopeo is not None
    monkeypatch.setattr(
        runtime_image_preparation,
        "OciImageStore",
        partial(OciImageStore, skopeo=skopeo),
    )
    sessions, bundles, initial, node_id, revision = build_setup(
        tmp_path, recipe_slug="damaged-image-recovery"
    )
    now = [initial]
    artifact_root = tmp_path / "artifacts"
    storage = FilesystemRuntimeImageStorage(artifact_root)
    builds = RecipeBuildService(
        sessions,
        bundles=bundles,
        build_archive_available=storage.build_archive_available,
        prepared_builds=storage.find_build,
    )
    archive = _docker_archive(tmp_path / "image.tar", [_layer("model", b"exact bytes")])
    image = storage.layout.import_archive(archive)
    assert isinstance(image, StoredImage), image
    plan = builds.plan(revision.id, node_id, now=now[0])
    builds.record_success(
        plan.build_id,
        build_input_sha256=plan.build_input_sha256,
        image_digest=image.manifest_digest,
        oci_layout_sha256=image.manifest_digest.removeprefix("sha256:"),
        image_bytes=image.stored_bytes,
        now=now[0],
    )
    receipt = prepare_runtime_image(
        revision.document,
        runtime={"architecture": "linux/arm64", "interface": "vonk.runtime.v1"},
        storage=storage,
        build_receipt={
            "state": "succeeded",
            "build_id": plan.build_id,
            "build_input_sha256": plan.build_input_sha256,
            "image_digest": image.manifest_digest,
            "oci_layout_sha256": image.manifest_digest.removeprefix("sha256:"),
            "image_bytes": image.stored_bytes,
        },
        now=now[0],
    )

    jobs = AgentJobService(sessions, clock=lambda: now[0])
    operations = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=jobs,
        clock=lambda: now[0],
        builds=builds,
    )
    jobs.set_result_consumer(operations.consume_agent_result)
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
        session.add(
            AgentCertificate(
                serial="manifest-recovery-builder",
                node_id=node_id,
                not_before=initial - timedelta(seconds=1),
                not_after=initial + timedelta(days=7),
                fingerprint="manifest-recovery-builder",
            )
        )

    def production():
        return build_recipe_image_availability(
            sessions,
            artifact_root=artifact_root,
            managed_catalog_sync=None,
            recipe_builds=builds,
            recipe_operations=operations,
            clock=lambda: now[0],
        )

    first = production()
    try:
        completed = first.service.start(
            revision.id, actor="operator", request_id=str(uuid.uuid4())
        )
        assert first.service.run_pending() == 1
        assert first.service.get(completed.id).state == "succeeded"
        with sessions() as session:
            assert (
                session.scalar(select(Job).where(Job.kind == "recipe.build.v1")) is None
            )
        owner = first.service.start(
            revision.id, actor="operator", request_id=str(uuid.uuid4())
        )
        assert owner.state == "queued"
        manifest = storage.layout.blob_path(image.manifest_digest)
        manifest.write_bytes(b'{"schemaVersion":')
        old = time.time() - GRACE.total_seconds() - 60
        for blob in (storage.layout.root / "blobs" / "sha256").iterdir():
            os.utime(blob, (old, old))
        collector = ImageStoreCollector(
            sessions, artifact_root, clock=lambda: now[0], store=storage.layout
        )
        assert collector.collect() is None
        assert manifest.exists()
        assert storage.read_receipt(receipt.oci_archive_sha256) == receipt
        cancelled = first.service.cancel(
            owner.id,
            actor="operator",
            request_id=str(uuid.uuid4()),
            reason="release the queued preparation owner",
        )
        assert cancelled.state == "cancelled"
    finally:
        first.close()

    now[0] += GRACE + timedelta(seconds=1)
    assert collector.collect() is not None
    assert not manifest.exists()
    assert not (storage.root / f"{receipt.oci_archive_sha256}.receipt.json").exists()
    # Real inventory ingress restores current builder evidence after the grace.
    InventoryRepository(sessions, clock=lambda: now[0]).record(
        InventorySnapshotInput(
            node_id,
            now[0],
            2 * 1024**4,
            1024**4,
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
                "runtime.vonk.v1",
                "recipe.image.pull.v1",
            ),
            memory_pool="separate",
        )
    )
    restarted_storage = FilesystemRuntimeImageStorage(artifact_root)
    assert not restarted_storage.build_archive_available(
        receipt.oci_archive_sha256, receipt.image_bytes
    )
    assert builds.resolve(revision.id).cached is False
    restarted = production()
    try:
        accepted = restarted.service.start(
            revision.id,
            actor="operator",
            request_id=str(uuid.uuid4()),
            build_input_sha256=plan.build_input_sha256,
        )
        assert accepted.id not in {completed.id, owner.id}
        assert restarted.service.run_pending() == 1
        waiting = restarted.service.get(accepted.id)
        assert waiting.state == "queued", waiting.failure
        assert waiting.failure is not None
        with sessions() as session:
            parent = session.get(Job, accepted.id)
            assert parent is not None
            dependency = RecipeBuildDependency.model_validate_json(
                json.dumps(parent.payload["build_dependency"])
            )
            assert dependency.operation_id is not None
            child_id = str(dependency.operation_id)
            child_request = str(dependency.request_key)
            child = session.get(Job, child_id)
            assert child is not None and child.kind == "recipe.build.v1"
            assert child.request_id == child_request
            assert child.targets == [node_id]
            assert child.payload["plan_digest"] == plan.build_input_sha256
            build_id = child.payload["owner_id"]
            held = tuple(
                session.scalars(
                    select(ResourceReservation).where(
                        ResourceReservation.owner_kind == "recipe-build",
                        ResourceReservation.owner_id == build_id,
                        ResourceReservation.state == "active",
                    )
                )
            )
            assert {hold.kind for hold in held} == {"disk", "host-memory"}
            assert all(hold.plan_digest == plan.build_input_sha256 for hold in held)
        record_passing_preflight(sessions, now[0])
        claimed = jobs.claim(
            node_id,
            "manifest-recovery-builder",
            runtime_identity={
                "architecture": "linux-arm64",
                "semantic_version": "1.2.3",
                "build_digest": "sha256:" + "a" * 64,
                "binary_digest": "1" * 64,
            },
        )
        assert claimed is not None
        assert claimed.payload["build_input_sha256"] == plan.build_input_sha256
        # The sole controlled effect: build bytes arriving from this exact claim.
        restored = restarted_storage.layout.import_archive(archive)
        assert isinstance(restored, StoredImage), restored
        assert restored.manifest_digest == image.manifest_digest
        jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": claimed.fence,
                        "state": "succeeded",
                        "result": {
                            "image_digest": restored.manifest_digest,
                            "oci_layout_sha256": restored.manifest_digest.removeprefix(
                                "sha256:"
                            ),
                            "image_bytes": restored.stored_bytes,
                        },
                    }
                )
            )
        )
        assert operations.get(child_id).state == "succeeded"
        restarted.close()
        now[0] += timedelta(minutes=3)
        restarted = production()
        assert restarted.service.run_pending() == 1
        result = restarted.service.get(accepted.id)
        assert result.state == "succeeded", result.failure
        with sessions() as session:
            children = tuple(
                session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
            )
            assert [child.id for child in children] == [child_id]
            assert children[0].request_id == child_request
            assert (
                session.scalar(
                    select(ResourceReservation.id).where(
                        ResourceReservation.owner_id == build_id,
                        ResourceReservation.state == "active",
                    )
                )
                is None
            )
        assert result.result is not None
        assert result.result["build_input_sha256"] == plan.build_input_sha256
        assert result.result["oci_archive_sha256"] == receipt.oci_archive_sha256
        current = restarted_storage.find_build(
            plan.build_input_sha256,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
            expected_archive_sha256=receipt.oci_archive_sha256,
        )
        assert current is not None
        assert current.image_digest == receipt.image_digest
        assert current.recorded_at != receipt.recorded_at
        assert restarted.service.run_pending() == 0
    finally:
        restarted.close()
