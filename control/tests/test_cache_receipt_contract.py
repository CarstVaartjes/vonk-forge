"""The cache adapter exports validated receipt models, including nested identity."""

# ruff: noqa: F811 - pytest fixture is imported by name

from types import SimpleNamespace

import pytest
from vonk_agent_protocol import UnknownOutcomeError
from vonk_control.compiled_execution_plan import (
    DistributionObjectReceipt,
    VerifiedModelObject,
    VerifiedRuntimeImage,
)
from vonk_control.distribution import ModelCacheObjectSource
from vonk_control.execution_plan_service import _runtime_image_receipt
from vonk_control.runtime_image_preparation import RuntimeImageReceipt

from .test_nas_cache_acceptance import controller  # noqa: F401 - shared cache fixture


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
    "changes",
    [
        {"roles": []},
        {"file_id": ""},
        {"model_content_sha256": "bad"},
        {"file_id": None},
    ],
)
def test_invalid_cache_identity_never_becomes_a_verified_receipt(tmp_path, changes):
    source, manifest = _source(tmp_path, **changes)
    with pytest.raises(UnknownOutcomeError):
        source.verified_model_objects_for_set(manifest.digest)
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
        with pytest.raises(UnknownOutcomeError):
            source.verified_model_objects_for_set(digest)
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


@pytest.mark.parametrize("damage", ["missing", "malformed", "different", "unreadable"])
def test_local_receipt_miss_triggers_preparation_and_reuses_repaired_content(
    controller, tmp_path, damage, monkeypatch
):
    """Catches refusing local metadata, publishing it, or waiting for an operator."""
    from vonk_control.distribution import verified_model_receipts

    from .test_nas_cache_acceptance import _artifact, _download

    _database, _engine, sessions, cache = controller
    artifact = _artifact(
        tmp_path,
        "weights",
        "weights.bin",
        b"requested content",
        model_content_sha256="a" * 64,
        token="unused",
    )
    digest = _download(
        cache,
        [artifact],
        model_content_sha256="a" * 64,
        recipe_revision_sha256="b" * 64,
        request_key="00000000-0000-4000-8000-000000000111",
    )
    manifest = cache.manifest_for_artifact_set(digest)
    object_digest = manifest.artifacts[0].sha256
    receipt_path = cache._receipt_path(object_digest)
    if damage == "missing":
        receipt_path.unlink()
    elif damage == "malformed":
        receipt_path.write_text("{damaged")
    elif damage == "different":
        receipt_path.write_text(
            receipt_path.read_text().replace(object_digest, "f" * 64)
        )
    else:
        original = type(receipt_path).read_text

        def unreadable(path, *args, **kwargs):
            if path == receipt_path:
                raise OSError("unreadable local receipt")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(type(receipt_path), "read_text", unreadable)
    with pytest.raises(UnknownOutcomeError):
        verified_model_receipts(cache, digest, manifest)
    from sqlalchemy import select
    from vonk_agent_protocol import LifecycleState
    from vonk_control.models import ModelCacheOperation

    with sessions() as session:
        repairs = list(
            session.scalars(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.artifact_set_sha256 == digest,
                    ModelCacheOperation.state == LifecycleState.QUEUED,
                )
            )
        )
    assert len(repairs) == 1
    monkeypatch.undo()
    cache.run_pending()
    (receipt,) = verified_model_receipts(cache, digest, manifest)
    assert receipt.sha256 == object_digest
    assert cache._object_path(object_digest).read_bytes() == b"requested content"
    assert verified_model_receipts(cache, digest, manifest) == (receipt,)


def test_bad_ingress_digest_refuses_and_fresh_valid_ingress_is_admitted(
    controller, tmp_path
):
    """Local misses must not weaken verification when new bytes enter storage."""
    from vonk_agent_protocol import SecurityRefusalError

    from .test_nas_cache_acceptance import _artifact, _download

    _database, _engine, _sessions, cache = controller
    artifact = _artifact(
        tmp_path,
        "weights",
        "weights.bin",
        b"expected",
        model_content_sha256="a" * 64,
        token="unused",
    )
    digest = _download(
        cache,
        [artifact],
        model_content_sha256="a" * 64,
        recipe_revision_sha256="b" * 64,
        request_key="00000000-0000-4000-8000-000000000112",
    )
    spec = cache.manifest_for_artifact_set(digest).artifacts[0]
    ingress = tmp_path / "ingress.part"
    ingress.write_bytes(b"tampered")
    with pytest.raises(SecurityRefusalError):
        cache._publish_object(spec, ingress)
    assert not ingress.exists()
    assert cache._object_path(spec.sha256).read_bytes() == b"expected"
    ingress.write_bytes(b"expected")
    cache._publish_object(spec, ingress)
    assert (
        cache.cached_artifact_file(digest, spec.sha256, spec.path)[0].read_bytes()
        == b"expected"
    )
