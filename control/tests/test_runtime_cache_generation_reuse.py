"""Keep reusable image generations and exact accepted plans separate."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import pytest
from vonk_control.compiled_execution_plan import VerifiedRuntimeImage
from vonk_control.execution_plan_service import _runtime_receipt_mapping
from vonk_control.oci_image_store import StoredImage
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImageReceipt,
    prepare_runtime_image,
)

from .test_oci_image_store import _docker_archive, _layer

pytestmark = pytest.mark.lane

BUILD_INPUT = "a" * 64
RUNTIME = {"architecture": "linux/arm64", "interface": "vonk.runtime.v1"}


@pytest.fixture
def image_generations(
    tmp_path: Path,
) -> tuple[FilesystemRuntimeImageStorage, RuntimeImageReceipt, RuntimeImageReceipt]:
    if shutil.which("skopeo") is None:
        if os.environ.get("VONK_CI_RUNTIME_CACHE_PROOF") == "1":
            pytest.fail("the runtime cache proof lane must provide skopeo")
        pytest.skip("requires the Controller image ingress tool skopeo")
    storage = FilesystemRuntimeImageStorage(tmp_path / "artifacts")
    recipe = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text()
    )

    def produce(number: int) -> RuntimeImageReceipt:
        archive = _docker_archive(
            tmp_path / f"build-{number}.tar",
            [_layer("model", f"build generation {number}".encode())],
        )
        image = storage.layout.import_archive(archive)
        assert isinstance(image, StoredImage), image
        receipt = prepare_runtime_image(
            recipe,
            runtime=RUNTIME,
            storage=storage,
            build_receipt={
                "state": "succeeded",
                "build_id": f"00000000-0000-4000-8000-{number:012d}",
                "build_input_sha256": BUILD_INPUT,
                "image_digest": image.manifest_digest,
                "oci_layout_sha256": image.manifest_digest.removeprefix("sha256:"),
                "image_bytes": image.stored_bytes,
            },
            now=datetime(2026, 10, 7, tzinfo=UTC) + timedelta(minutes=number),
        )
        assert storage.read_receipt(receipt.oci_archive_sha256) == receipt
        return receipt

    older, newer = produce(1), produce(2)
    assert older.oci_archive_sha256 != newer.oci_archive_sha256
    return storage, older, newer


def _bound_lookup(
    storage: FilesystemRuntimeImageStorage, plan: VerifiedRuntimeImage
) -> RuntimeImageReceipt | None:
    return storage.find_build(
        BUILD_INPUT,
        expected_architecture=RUNTIME["architecture"],
        expected_runtime_interface=RUNTIME["interface"],
        expected_archive_sha256=plan.oci_layout_sha256,
    )


def test_new_image_generation_preserves_the_exact_older_plan(
    image_generations: tuple[
        FilesystemRuntimeImageStorage, RuntimeImageReceipt, RuntimeImageReceipt
    ],
) -> None:
    storage, older, newer = image_generations
    old_plan = VerifiedRuntimeImage.model_validate(_runtime_receipt_mapping(older))
    new_plan = VerifiedRuntimeImage.model_validate(_runtime_receipt_mapping(newer))

    assert _bound_lookup(storage, old_plan) == older
    assert _bound_lookup(storage, new_plan) == newer
    assert storage.read_receipt(old_plan.oci_layout_sha256) == older


def test_missing_new_generation_reuses_old_bytes_without_rebinding_accepted_plan(
    image_generations: tuple[
        FilesystemRuntimeImageStorage, RuntimeImageReceipt, RuntimeImageReceipt
    ],
) -> None:
    storage, older, newer = image_generations
    old_plan = VerifiedRuntimeImage.model_validate(_runtime_receipt_mapping(older))
    new_plan = VerifiedRuntimeImage.model_validate(_runtime_receipt_mapping(newer))
    old_identity = old_plan.model_dump(mode="json")
    new_identity = new_plan.model_dump(mode="json")
    image = storage.layout.read(newer.image_digest)
    assert isinstance(image, StoredImage), image
    storage.layout.blob_path(image.layer_digests[-1]).unlink()

    restarted = FilesystemRuntimeImageStorage(storage.root.parent)
    assert (
        restarted.find_build(
            BUILD_INPUT,
            expected_architecture=RUNTIME["architecture"],
            expected_runtime_interface=RUNTIME["interface"],
        )
        == older
    )
    assert _bound_lookup(restarted, old_plan) == older
    assert _bound_lookup(restarted, new_plan) is None
    assert restarted.read_receipt(newer.oci_archive_sha256) == newer
    assert old_plan.model_dump(mode="json") == old_identity
    assert new_plan.model_dump(mode="json") == new_identity
