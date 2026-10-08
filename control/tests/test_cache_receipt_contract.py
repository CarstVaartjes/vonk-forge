"""The cache adapter exports validated receipt models, including nested identity."""

from types import SimpleNamespace

import pytest
from vonk_agent_protocol import DistributionCode
from vonk_control.compiled_execution_plan import (
    DistributionObjectReceipt,
    VerifiedModelObject,
    VerifiedRuntimeImage,
)
from vonk_control.distribution import DistributionUnknown, ModelCacheObjectSource
from vonk_control.execution_plan_service import _runtime_image_receipt
from vonk_control.runtime_image_preparation import RuntimeImageReceipt


def _source(tmp_path, **changes):
    descriptor = {
        "path": "model.bin",
        "sha256": "a" * 64,
        "bytes": 3,
        "file": tmp_path / "model.bin",
        "file_id": "model",
        "model_content_sha256": "b" * 64,
        "roles": ["weights"],
    } | changes
    manifest = SimpleNamespace(digest="c" * 64)
    service = SimpleNamespace(
        manifest_for_artifact_set=lambda digest: manifest,
        resolve_verified_artifact_set=lambda digest: [descriptor],
    )
    return ModelCacheObjectSource.from_service(service), manifest


def test_cache_exports_typed_receipts_with_exact_nested_identity(tmp_path):
    source, manifest = _source(tmp_path)
    (receipt,) = source.verified_model_objects_for_set(manifest.digest)
    assert isinstance(receipt, VerifiedModelObject)
    assert isinstance(receipt.distribution_object, DistributionObjectReceipt)
    assert receipt.sha256 == receipt.distribution_object.sha256
    assert receipt.bytes == receipt.distribution_object.bytes == 3
    assert receipt.path == receipt.distribution_object.name == "model.bin"
    assert source.verified_model_objects_for_set(manifest.digest) == (receipt,)


@pytest.mark.parametrize(
    "changes", [{"roles": []}, {"file_id": ""}, {"model_content_sha256": "bad"}]
)
def test_invalid_cache_identity_never_becomes_a_verified_receipt(tmp_path, changes):
    source, manifest = _source(tmp_path, **changes)
    with pytest.raises(DistributionUnknown) as caught:
        source.verified_model_objects_for_set(manifest.digest)
    assert caught.value.code == DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE
    assert source._receipts == {}
    assert source._paths == {}
    assert source._manifests == {}
    # Repair by the source must admit a fresh verification on the same adapter;
    # failed local bookkeeping must not publish or poison cached authorization.
    descriptor = source._service.resolve_verified_artifact_set(manifest.digest)[0]
    descriptor.update(roles=["weights"], file_id="model", model_content_sha256="b" * 64)
    (receipt,) = source.verified_model_objects_for_set(manifest.digest)
    assert receipt.file_id == "model"


@pytest.mark.parametrize("missing_provider", [True, False])
def test_unavailable_local_manifest_allows_fresh_verification(
    tmp_path, monkeypatch, missing_provider
):
    source, manifest = _source(tmp_path)
    digest = manifest.digest
    with monkeypatch.context() as damaged:
        if missing_provider:
            damaged.setattr(source._service, "manifest_for_artifact_set", None)
        else:
            damaged.setattr(manifest, "digest", "bad")
        with pytest.raises(DistributionUnknown) as caught:
            source.verified_model_objects_for_set(digest)
        assert caught.value.code == DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE
        assert source._receipts == source._paths == source._manifests == {}
    (receipt,) = source.verified_model_objects_for_set(digest)
    assert receipt.file_id == "model"


def test_runtime_receipt_projection_keeps_exact_launch_identity():
    stored = RuntimeImageReceipt(
        schema_version=2,
        distribution_publisher="example",
        distribution_slug="model",
        distribution_content_sha256="a" * 64,
        image_digest="sha256:" + "b" * 64,
        oci_archive_sha256="c" * 64,
        image_bytes=3,
        local_image_config_id="sha256:" + "d" * 64,
        architecture="linux-arm64",
        runtime_interface="vonk.runtime.v1",
        archive_path="/cache/image.tar",
        recorded_at="2026-10-07T00:00:00Z",
        build_id="build",
        runtime_interface_label="v1",
        runtime_adapter="adapter",
        runtime_adapter_sha256="e" * 64,
    )
    launch = _runtime_image_receipt(stored)
    assert isinstance(launch, VerifiedRuntimeImage)
    assert launch.oci_layout_sha256 == stored.oci_archive_sha256
    assert launch.image_digest == stored.image_digest
    assert launch.local_image_config_id == stored.local_image_config_id
    assert launch.build_id == stored.build_id
    assert launch.image_bytes == stored.image_bytes
    assert VerifiedRuntimeImage.model_validate_json(launch.model_dump_json()) == launch
