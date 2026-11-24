"""The cache adapter exports validated receipt models, including nested identity."""

# ruff: noqa: F811 - pytest fixture is imported by name

import pytest
from sqlalchemy import delete, select

from vonk_agent_protocol.agent_words import Disposition
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.outcome import SecurityRefusalError, UnknownOutcomeError
from vonk_control.distribution.model_cache import ModelCacheObjectSource
from vonk_control.distribution.sources import verified_model_receipts
from vonk_control.execution_plan.compiled import (
    VerifiedRuntimeImage,
)
from vonk_control.execution_plan.service import _runtime_image_receipt
from vonk_control.failures.classification import disposition
from vonk_control.models.cache import (
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
)
from vonk_control.runtime_image_preparation.contracts import RuntimeImageReceipt

from .test_nas_cache_acceptance import (
    _artifact,
    _download,
    controller,  # noqa: F401 - shared cache fixture
)


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
    assert launch.image_bytes == stored.image_bytes
    assert VerifiedRuntimeImage.model_validate_json(launch.model_dump_json()) == launch


def test_a_damaged_object_triggers_preparation_and_reuses_repaired_content(
    controller, tmp_path
):
    """Catches refusing a wrong-size object, publishing it, or waiting for an operator.

    The copy to a Spark is what reads the NAS bytes, so it owns this repair.
    """

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
    cache._object_path(object_digest).write_bytes(b"short")
    with pytest.raises(UnknownOutcomeError):
        ModelCacheObjectSource.from_service(cache).objects_for_set(digest)

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
    cache.run_pending()
    source = ModelCacheObjectSource.from_service(cache)
    (served,) = source.objects_for_set(digest)
    assert served.sha256 == object_digest
    assert cache._object_path(object_digest).read_bytes() == b"requested content"
    assert source.objects_for_set(digest) == (served,)


@pytest.mark.parametrize("nas", ["evicted", "bytes-held"])
def test_a_set_the_nas_evicted_still_compiles_without_a_download(
    controller, tmp_path, nas
):
    """Catches compilation that needs the NAS copy of a model every target
    Spark already holds: after eviction that waited or downloaded it again.
    A set over bytes the NAS still holds is recorded for a later copy."""

    _database, _engine, sessions, cache = controller
    artifact = _artifact(
        tmp_path,
        "weights",
        "weights.bin",
        b"evicted content",
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
    manifest = cache.manifest_for_artifact_set(digest)
    # Eviction removes the bytes and the set's rows.
    for spec in manifest.artifacts if nas == "evicted" else ():
        cache._object_path(spec.sha256).unlink()
    with sessions.begin() as session:
        session.execute(
            delete(ModelCacheSetArtifact).where(
                ModelCacheSetArtifact.artifact_set_sha256 == digest
            )
        )
        session.execute(
            delete(ModelCacheSet).where(ModelCacheSet.artifact_set_sha256 == digest)
        )
        session.execute(delete(ModelCacheOperation))

    (receipt,) = verified_model_receipts(cache, digest, manifest)

    assert receipt.sha256 == manifest.artifacts[0].sha256
    assert receipt.file_id == "weights"
    with sessions() as session:
        assert session.scalars(select(ModelCacheOperation)).all() == []
        row = session.get(ModelCacheSet, digest)
        assert (row.state if row is not None else None) == (
            None if nas == "evicted" else "cached"
        )


def test_bad_ingress_digest_refuses_and_fresh_valid_ingress_is_admitted(
    controller, tmp_path
):
    """Local misses must not weaken verification when new bytes enter storage."""

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
    with pytest.raises(SecurityRefusalError) as refused:
        cache._publish_object(spec, ingress)
    assert disposition(refused.value) is Disposition.SECURITY
    assert not ingress.exists()
    assert cache._object_path(spec.sha256).read_bytes() == b"expected"
    ingress.write_bytes(b"expected")
    cache._publish_object(spec, ingress)
    assert (
        cache.cached_artifact_file(digest, spec.sha256, spec.path)[0].read_bytes()
        == b"expected"
    )
