"""Hermetic regressions for storage recovery, content custody, and gone children."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import (
    DistributionCode,
    DistributionObject,
    InvalidRequestError,
    LifecycleState,
    SecurityRefusalError,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol.agent_words import ProfileChildPhase
from vonk_control.agent_jobs import AgentJobService
from vonk_control.distribution import (
    CompositeObjectSource,
    DistributionError,
    DistributionService,
    FilesystemObjectSource,
    MemoryObjectSource,
    ModelCacheObjectSource,
)
from vonk_control.distribution_executor import DurableDistributionPhaseExecutor
from vonk_control.models import Base, Job
from vonk_control.run_switch_contract import (
    RunSwitchBuildEvidence,
    RunSwitchDistributionEndedResult,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPlan,
    RunSwitchRuntimeImageResult,
)

from .non_blocking import assert_ended_without_blocking
from .test_distribution import _assignment


def test_missing_stored_object_is_typed_observation_then_recovers(tmp_path: Path):
    """An absent file must retain unknown semantics, rather than become a refusal."""
    import hashlib

    payload = b"recovered bytes"
    digest = hashlib.sha256(payload).hexdigest()
    source = FilesystemObjectSource(tmp_path)
    with pytest.raises(DistributionError) as caught:
        source.open_object(digest, len(payload))
    assert isinstance(caught.value, UnknownOutcomeError)
    assert caught.value.typed_reason == WaitReason.OBSERVATION_UNAVAILABLE
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
    with pytest.raises(SecurityRefusalError) as caught:
        source.objects_for_set("a" * 64)
    assert caught.value is denial


def test_missing_receipt_provider_is_configuration_validation():
    """The configured source protocol is checked before any storage effect."""
    source = CompositeObjectSource(MemoryObjectSource(), MemoryObjectSource())
    with pytest.raises(DistributionError) as caught:
        source.verified_model_objects_for_set("a" * 64)
    assert isinstance(caught.value, InvalidRequestError)


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
    with pytest.raises(SecurityRefusalError):
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
    ) == (image, archive, 11, first)


def test_gone_child_ends_and_fresh_distribution_is_admitted():
    """A vanished child reports a typed end without poisoning the next request."""
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    executor = DurableDistributionPhaseExecutor(
        sessions,
        AgentJobService(sessions, clock=lambda: datetime.now(UTC)),
        DistributionService(MemoryObjectSource()),
        clock=lambda: datetime.now(UTC),
    )
    missing_id = str(uuid4())
    fresh_key = str(uuid4())
    node = "spk_" + "a" * 32
    plan = RunSwitchPlan.model_construct(plan_digest="a" * 64)
    phase = RunSwitchPhase.model_construct(
        kind=ProfileChildPhase.TRANSFER.value, index=0, node_ids=[node]
    )

    def fresh(_world):
        child_id = executor._ensure_child(
            plan,
            phase,
            actor="operator",
            request_key=fresh_key,
            cached=(node,),
            assignments={},
            target_order=(node,),
            target_bytes=0,
            workload_intent_ordinal=1,
        )
        with sessions() as session:
            child = session.get(Job, child_id)
            assert child is not None
            return child

    def assert_reason(receipt):
        assert receipt.result.error_code == DistributionCode.UNASSIGNED

    with sessions() as session:
        ended, admitted = assert_ended_without_blocking(
            session,
            executor.get(missing_id),
            end=lambda receipt: receipt,
            fresh=fresh,
            request_key=lambda receipt: (
                receipt.request_id if isinstance(receipt, Job) else missing_id
            ),
            assert_reason=assert_reason,
        )
    assert isinstance(ended.result, RunSwitchDistributionEndedResult)
    assert ended.result.error_code == DistributionCode.UNASSIGNED
    assert admitted.state == LifecycleState.QUEUED
    engine.dispose()


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
    with pytest.raises(UnknownOutcomeError):
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
    with pytest.raises(DistributionError) as caught:
        source.verified_model_objects_for_set("b" * 64)
    assert isinstance(caught.value, UnknownOutcomeError)
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
    with pytest.raises(UnknownOutcomeError):
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
