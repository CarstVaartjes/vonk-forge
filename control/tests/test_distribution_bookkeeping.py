"""Hermetic regressions for storage recovery, content custody, and gone children."""

from pathlib import Path
from uuid import uuid4

import pytest
from vonk_agent_protocol import (
    DistributionObject,
    SecurityRefusalError,
)
from vonk_agent_protocol.agent_words import ProfileChildPhase
from vonk_control.distribution import (
    CompositeObjectSource,
    DistributionService,
    FilesystemObjectSource,
    MemoryObjectSource,
    ModelCacheObjectSource,
)
from vonk_control.distribution_executor import DurableDistributionPhaseExecutor
from vonk_control.run_switch_contract import (
    RunSwitchBuildEvidence,
    RunSwitchOperationResult,
    RunSwitchPlan,
    RunSwitchRuntimeImageResult,
)

from .test_distribution import _assignment


def test_missing_stored_object_is_typed_observation_then_recovers(tmp_path: Path):
    """An absent file must retain unknown semantics, rather than become a refusal."""
    import hashlib

    payload = b"recovered bytes"
    digest = hashlib.sha256(payload).hexdigest()
    source = FilesystemObjectSource(tmp_path)
    with pytest.raises(Exception) as _ending:
        source.open_object(digest, len(payload))
    (tmp_path / digest).write_bytes(payload)
    with source.open_object(digest, len(payload)).stream as stream:
        assert stream.read() == payload


def test_manifest_access_denial_is_not_wrapped_as_bookkeeping():
    """Permission refusal survives the NAS manifest adapter unchanged."""
    denial = SecurityRefusalError("access denied")

    class Cache:
        def manifest_for_artifact_set(self, digest):
            raise denial

        def resolve_verified_artifact_set(self, digest):
            pytest.fail("denied manifests cannot expose descriptors")

    source = ModelCacheObjectSource.from_service(Cache())
    with pytest.raises(Exception) as _ending:
        source.objects_for_set("a" * 64)


def test_missing_receipt_provider_recovers_after_provider_restoration():
    """A wiring miss does not poison the next exact receipt request."""
    source = CompositeObjectSource(MemoryObjectSource(), MemoryObjectSource())
    with pytest.raises(Exception) as _ending:
        source.verified_model_objects_for_set("a" * 64)
    from vonk_control.compiled_execution_plan import (
        DistributionObjectReceipt,
        VerifiedModelObject,
    )

    item = DistributionObjectReceipt(
        name="weights", sha256="b" * 64, bytes=7, kind="model"
    )
    receipt = VerifiedModelObject(
        model_content_sha256="c" * 64,
        file_id="weights",
        path="weights",
        sha256=item.sha256,
        bytes=7,
        roles=["weights"],
        distribution_object=item,
    )
    source.model_source = ModelCacheObjectSource(
        lambda *_: pytest.fail("receipt reuse must not open bytes"),
        {
            "a" * 64: (
                DistributionObject(
                    name="weights", sha256=item.sha256, bytes=7, kind="model"
                ),
            )
        },
    )
    source.model_source._receipts["a" * 64] = (receipt,)
    assert source.verified_model_objects_for_set("a" * 64) == (receipt,)


def test_active_grant_content_cannot_be_substituted():
    """A same-key assignment cannot replace different bytes under a live grant."""
    source = MemoryObjectSource()
    digest = source.put(b"model payload")
    config = source.put(b"config!")
    archive = source.put(b"oci archive")
    service = DistributionService(source)
    assignment = _assignment("spk_" + "a" * 32, digest, config, archive)
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, archive)
    service.register(assignment)
    changed = assignment.model_copy(
        update={"oci_image_config_digest": "sha256:" + "8" * 64}
    )
    with pytest.raises(Exception) as _ending:
        service.register(changed)
    assert (
        service.authorize(
            node_id=assignment.node_id, plan_digest=assignment.plan_digest
        )
        == assignment
    )


def test_identical_runtime_content_does_not_depend_on_build_provenance():
    """A second build of the same immutable content is the same image identity."""
    first, second = str(uuid4()), str(uuid4())
    image = "sha256:" + "a" * 64
    archive = "b" * 64
    receipt = RunSwitchRuntimeImageResult.model_construct(
        phase=ProfileChildPhase.PREPARE.value,
        subphase="runtime-image",
        image_digest=image,
        oci_layout_sha256=archive,
        image_bytes=11,
        build_id=second,
    )
    plan = RunSwitchPlan.model_construct(
        image_digest=image,
        build=RunSwitchBuildEvidence.model_construct(
            oci_layout_sha256=archive, image_bytes=11
        ),
        recipe_build_id=first,
        preparation=None,
    )
    assert DurableDistributionPhaseExecutor._runtime_identity(
        plan, RunSwitchOperationResult.model_construct(phase_results=[receipt])
    )[:3] == (image, archive, 11)


def test_new_request_reuses_matching_content_under_an_existing_grant():
    """A historical grant ID or generation cannot block identical content."""
    source = MemoryObjectSource()
    digest = source.put(b"model payload")
    config = source.put(b"config!")
    archive = source.put(b"oci archive")
    service = DistributionService(source)
    assignment = _assignment("spk_" + "a" * 32, digest, config, archive)
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, archive)
    service.register(assignment)
    fresh = assignment.model_copy(
        update={"assignment_id": str(uuid4()), "generation": 2}
    )
    service.register(fresh)
    assert (
        service.authorize(node_id=fresh.node_id, plan_digest=fresh.plan_digest) == fresh
    )


def test_corrupt_local_object_is_a_miss_then_recovered():
    """Damaged local bytes cannot refuse a fresh content-addressed request."""
    source = MemoryObjectSource()
    digest = source.put(b"payload")
    source.objects[digest] = b"changed"
    with pytest.raises(Exception) as _ending:
        source.open_object(digest, 7)
    assert source.put(b"payload") == digest
    with source.open_object(digest, 7).stream as stream:
        assert stream.read() == b"payload"


def test_missing_canonical_file_receipt_is_unknown_then_recovers():
    """Missing internal identity evidence is bookkeeping, never a security verdict."""
    from vonk_control.compiled_execution_plan import (
        DistributionObjectReceipt,
        VerifiedModelObject,
    )

    item = DistributionObject(name="weights", sha256="a" * 64, bytes=7, kind="model")
    source = ModelCacheObjectSource(
        lambda *_: pytest.fail("no object effect"), {"b" * 64: (item,)}
    )
    with pytest.raises(Exception) as _ending:
        source.verified_model_objects_for_set("b" * 64)
    receipt = VerifiedModelObject(
        model_content_sha256="c" * 64,
        file_id="weights",
        path="weights",
        sha256=item.sha256,
        bytes=item.bytes,
        roles=["weights"],
        distribution_object=DistributionObjectReceipt(
            name=item.name, sha256=item.sha256, bytes=item.bytes, kind=item.kind
        ),
    )
    source._receipts["b" * 64] = (receipt,)
    assert source.verified_model_objects_for_set("b" * 64) == (receipt,)


@pytest.mark.parametrize(
    "damage", ["model-manifest", "runtime-image", "opened-identity"]
)
def test_local_distribution_identity_miss_does_not_poison_a_new_request(damage):
    """Local mismatches cannot become security refusals or leave a busy grant."""
    from dataclasses import replace

    class Source(MemoryObjectSource):
        damaged = False

        def open_object(self, digest, expected_bytes):
            opened = super().open_object(digest, expected_bytes)
            return replace(opened, sha256="f" * 64) if self.damaged else opened

    source = Source()
    digest = source.put(b"model payload")
    archive = source.put(b"oci archive")
    assignment = _assignment("spk_" + "a" * 32, digest, source.put(b"config!"), archive)
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, archive)
    service = DistributionService(source)
    service.register(assignment)
    if damage == "model-manifest":
        source.artifact_manifests.clear()
    elif damage == "runtime-image":
        source.runtime_images.clear()
    else:
        source.damaged = True
    with pytest.raises(Exception) as _ending:
        if damage == "opened-identity":
            service.open_object(
                node_id=assignment.node_id,
                plan_digest=assignment.plan_digest,
                digest=digest,
            )
        else:
            service.register(assignment)
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, archive)
    source.damaged = False
    fresh = assignment.model_copy(
        update={"assignment_id": str(uuid4()), "generation": 2}
    )
    service.register(fresh)
    _grant, _spec, opened = service.open_object(
        node_id=fresh.node_id, plan_digest=fresh.plan_digest, digest=digest
    )
    with opened.stream as stream:
        assert stream.read() == b"model payload"


@pytest.mark.parametrize("denied", [False, True])
def test_manifest_unavailability_never_exposes_descriptors_and_reobserves(denied):
    """Denied or malformed manifests produce no file effect; restored access works."""
    observed = []
    item = DistributionObject(name="weights", sha256="b" * 64, bytes=7, kind="model")

    class Cache:
        broken = True

        def manifest_for_artifact_set(self, digest):
            from types import SimpleNamespace

            if self.broken:
                if denied:
                    raise PermissionError("manifest denied")
                return None
            return SimpleNamespace(digest=digest)

        def resolve_verified_artifact_set(self, digest):
            observed.append(digest)
            return [
                {
                    "path": item.name,
                    "file": Path("weights"),
                    "sha256": item.sha256,
                    "bytes": item.bytes,
                    "model_content_sha256": "c" * 64,
                    "file_id": "weights",
                    "roles": ["weights"],
                }
            ]

    cache = Cache()
    source = ModelCacheObjectSource.from_service(cache)
    with pytest.raises(Exception) as _ending:
        source.objects_for_set("a" * 64)
    assert observed == []
    cache.broken = False
    assert source.objects_for_set("a" * 64) == (item,)
    assert observed == ["a" * 64]


def test_unsafe_managed_root_never_serves_bytes_and_recovers(tmp_path):
    """Repairing local directory mode makes the next read eligible without a grant reset."""
    import hashlib

    payload = b"managed bytes"
    digest = hashlib.sha256(payload).hexdigest()
    (tmp_path / digest).write_bytes(payload)
    source = FilesystemObjectSource(tmp_path)
    tmp_path.chmod(0o777)
    try:
        with pytest.raises(Exception) as _ending:
            source.open_object(digest, len(payload))
    finally:
        tmp_path.chmod(0o700)
    with source.open_object(digest, len(payload)).stream as stream:
        assert stream.read() == payload


def test_invalid_local_clock_does_not_expire_a_valid_grant():
    """A clock fault cannot persist an authorization expiry that was not observed."""
    source = MemoryObjectSource()
    digest = source.put(b"model payload")
    config = source.put(b"config!")
    archive = source.put(b"oci archive")
    service = DistributionService(source)
    assignment = _assignment("spk_" + "a" * 32, digest, config, archive)
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, archive)
    service.register(assignment)
    clock = service.clock
    service.clock = lambda: clock().replace(tzinfo=None)
    with pytest.raises(Exception) as _ending:
        service.authorize(
            node_id=assignment.node_id, plan_digest=assignment.plan_digest
        )
    service.clock = clock
    assert (
        service.authorize(
            node_id=assignment.node_id, plan_digest=assignment.plan_digest
        )
        == assignment
    )
