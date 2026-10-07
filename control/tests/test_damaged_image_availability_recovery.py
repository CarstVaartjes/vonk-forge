"""Hosted OCI recovery through the production availability composition.

The external builder is a controlled receipt producer, not a physical Spark.
OCI ingress, catalog/input resolution, SQL build ownership, preparation,
collection, cancellation and availability completion use their real owners.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session
from vonk_control.availability_production import build_recipe_image_availability
from vonk_control.image_store_collection import GRACE, ImageStoreCollector
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.oci_image_store import StoredImage
from vonk_control.recipe_builds import RecipeBuildPlan, RecipeBuildService
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    prepare_runtime_image,
)

from .test_oci_image_store import _docker_archive, _layer
from .test_recipe_builds import setup as build_setup

pytestmark = pytest.mark.lane


def test_damaged_receipted_manifest_recovers_through_new_availability_request(
    tmp_path: Path,
) -> None:
    if shutil.which("skopeo") is None:
        if os.environ.get("VONK_CI_DAMAGED_IMAGE_PROOF") == "1":
            pytest.fail("the damaged image proof lane must provide skopeo")
        pytest.skip("requires the Controller image ingress tool skopeo")
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

    class ExternalBuilder:
        """Only the external build effect is controlled; dispatch is production."""

        calls: list[tuple[str, str]]

        def __init__(self) -> None:
            self.calls = []

        def build(
            self,
            selected: RecipeBuildPlan,
            *,
            build_input_sha256: str,
            request_id: str,
            admission_guard: Callable[[Session], None],
            **_kwargs: object,
        ) -> SimpleNamespace:
            with sessions.begin() as session:
                admission_guard(session)
            self.calls.append((request_id, build_input_sha256))
            restored = storage.layout.import_archive(archive)
            assert isinstance(restored, StoredImage), restored
            assert restored.manifest_digest == image.manifest_digest
            builds.record_success(
                selected.build_id,
                build_input_sha256=build_input_sha256,
                image_digest=restored.manifest_digest,
                oci_layout_sha256=restored.manifest_digest.removeprefix("sha256:"),
                image_bytes=restored.stored_bytes,
                now=now[0],
            )
            return SimpleNamespace(
                id=str(uuid.uuid4()),
                state="succeeded",
                owner_id=selected.build_id,
                result={
                    "successful_nodes": [selected.builder_node_id],
                    "failed_nodes": [],
                    "node_evidence": {
                        selected.builder_node_id: {
                            "image_digest": restored.manifest_digest,
                            "oci_layout_sha256": restored.manifest_digest.removeprefix(
                                "sha256:"
                            ),
                            "image_bytes": restored.stored_bytes,
                        }
                    },
                },
            )

    external = ExternalBuilder()

    def production():
        return build_recipe_image_availability(
            sessions,
            artifact_root=artifact_root,
            managed_catalog_sync=None,
            recipe_builds=builds,
            recipe_operations=external,
            clock=lambda: now[0],
        )

    first = production()
    try:
        completed = first.service.start(
            revision.id, actor="operator", request_id=str(uuid.uuid4())
        )
        assert first.service.run_pending() == 1
        assert first.service.get(completed.id).state == "succeeded"
        assert external.calls == []
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
        result = restarted.service.get(accepted.id)
        assert result.state == "succeeded", result.failure
        assert len(external.calls) == 1
        assert external.calls[0][1] == plan.build_input_sha256
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
