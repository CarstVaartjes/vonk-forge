"""Accepted execution requests own their exact runtime preparation."""

from __future__ import annotations

import hashlib

import pytest

from vonk_agent_protocol.contracts import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.distribution import DistributionObject
from vonk_agent_protocol.host_helper import (
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
)
from vonk_agent_protocol.reason_codes.fleet import DistributionCode
from vonk_control.agent_jobs.stored import column_value
from vonk_control.distribution.service import DistributionService
from vonk_control.distribution.sources import MemoryObjectSource
from vonk_control.host_helper_authority import HostHelperAuthorityError
from vonk_control.models.operations import AgentOperation

from .test_host_helper_authority import NOW, runtime_service


@pytest.mark.parametrize(
    "operation_kind",
    [
        ProtocolAgentOperation.RECIPE_START.value,
        ProtocolAgentOperation.RECIPE_JOB_RUN.value,
    ],
)
def test_accepted_execution_can_prepare_its_exact_runtime_image(
    operation_kind: str,
) -> None:
    service = runtime_service(operation_kind=operation_kind)
    grant = service.issue_grant(
        node_id="spk_" + "1" * 32,
        fence="40000000-0000-4000-8000-000000000004",
        action=ContainerRuntimeAction.IMAGE_PULL,
        request_sha256="e" * 64,
        certificate_serial="certificate-1",
    )
    assert isinstance(grant.claims.operation, ExecuteContainerRuntimeRequestOperation)
    assert grant.claims.operation.request_sha256 == "e" * 64
    # A superseded or cancelled caller has no authority to prepare new bytes.
    cancelled = runtime_service(operation_kind=operation_kind, cancel_requested=True)
    with pytest.raises(HostHelperAuthorityError):
        cancelled.issue_grant(
            node_id="spk_" + "1" * 32,
            fence="40000000-0000-4000-8000-000000000004",
            action=ContainerRuntimeAction.IMAGE_PULL,
            request_sha256="e" * 64,
            certificate_serial="certificate-1",
        )


@pytest.mark.parametrize(
    "operation_kind",
    [
        ProtocolAgentOperation.RECIPE_START.value,
        ProtocolAgentOperation.RECIPE_JOB_RUN.value,
    ],
)
def test_execution_request_creates_delivery_without_an_earlier_distribution(
    operation_kind: str,
) -> None:
    from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
    from vonk_agent_protocol.recipe_operations import RecipeStartPayload

    authority = runtime_service(operation_kind=operation_kind)
    with authority._sessions() as session:
        operation = session.get(AgentOperation, "30000000-0000-4000-8000-000000000003")
        assert operation is not None
        payload = column_value(operation, "payload")
        assert isinstance(payload, RecipeStartPayload | RecipeJobRunRequest)
    source = MemoryObjectSource({})
    content = b"exact request model"
    digest = source.put(content)
    spec = payload.compiled_execution_plan.model_copy(
        update={
            "artifacts": [
                item.model_copy(update={"sha256": digest, "size_bytes": len(content)})
                for item in payload.compiled_execution_plan.artifacts
            ],
        }
    )
    payload = type(payload).model_validate_json(
        canonical_message(payload.model_copy(update={"compiled_execution_plan": spec}))
    )
    with authority._sessions.begin() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None
        stored.payload = payload.model_dump(mode="json")
        stored.payload_digest = hashlib.sha256(canonical_message(payload)).hexdigest()
    objects = (
        DistributionObject(
            name=spec.artifacts[0].path,
            sha256=digest,
            bytes=len(content),
            kind="model",
        ),
    )
    source.register_artifact_set(spec.identity.model_artifact_set_sha256, objects)
    source.register_runtime_image(
        spec.runtime_image.image_digest, spec.runtime_image.oci_layout_sha256
    )
    delivery = DistributionService(
        source, sessions=authority._sessions, clock=lambda: NOW
    )
    delivery.prepare_request_delivery(
        node_id=operation.node_id, plan_digest=payload.plan_digest
    )
    grant = delivery.authorize(
        node_id=operation.node_id, plan_digest=payload.plan_digest
    )
    assert grant.objects == objects
    assert grant.oci_image_digest == spec.runtime_image.image_digest
    _, _, opened = delivery.open_object(
        node_id=operation.node_id, plan_digest=payload.plan_digest, digest=digest
    )
    with opened.stream:
        assert opened.stream.read() == content
    # Another observation renews the same content grant and immediately reuses
    # the existing bytes rather than needing a separate distribution request.
    delivery.prepare_request_delivery(
        node_id=operation.node_id, plan_digest=payload.plan_digest
    )
    assert (
        delivery.authorize(
            node_id=operation.node_id, plan_digest=payload.plan_digest
        ).objects
        == objects
    )
    assert (
        delivery.authorize(
            node_id=operation.node_id, plan_digest=payload.plan_digest
        ).assignment_id
        == grant.assignment_id
    )


@pytest.mark.parametrize("transient", [False, True])
def test_a_request_delivers_its_image_after_the_nas_evicted_the_model(
    tmp_path, transient: bool
) -> None:
    """A Spark that holds the model's files still needs its grant to pull the
    image. The accepted plan names the files, so the grant needs no NAS copy;
    the files themselves stay unreadable while the NAS lacks them."""
    from vonk_control.distribution.model_cache import ModelCacheObjectSource
    from vonk_control.distribution.sources import CompositeObjectSource
    from vonk_control.distribution.types import DistributionError
    from vonk_control.model_cache.service import ModelCacheService

    authority = runtime_service(operation_kind=ProtocolAgentOperation.RECIPE_START)
    with authority._sessions() as session:
        operation = session.get(AgentOperation, "30000000-0000-4000-8000-000000000003")
        assert operation is not None
        payload = column_value(operation, "payload")
    spec = payload.compiled_execution_plan
    # The NAS no longer holds the set: eviction deleted its row and files.
    nas = ModelCacheService(
        authority._sessions, tmp_path / "nas", free_fraction=0, fixture_sources=True
    )
    images = MemoryObjectSource()
    images.register_runtime_image(
        spec.runtime_image.image_digest, spec.runtime_image.oci_layout_sha256
    )
    delivery = DistributionService(
        CompositeObjectSource(ModelCacheObjectSource.from_service(nas), images),
        sessions=authority._sessions,
        clock=lambda: NOW,
    )

    delivery.prepare_request_delivery(
        node_id=operation.node_id, plan_digest=payload.plan_digest
    )

    grant = delivery.authorize(
        node_id=operation.node_id, plan_digest=payload.plan_digest
    )
    assert grant.oci_image_digest == spec.runtime_image.image_digest
    assert {(item.sha256, item.bytes) for item in grant.objects} == {
        (item.sha256, item.size_bytes) for item in spec.artifacts
    }
    if transient:
        # A read that fails for any other cause is a wait, not a deleted set.
        def unreadable(*_args: object, **_kwargs: object) -> object:
            raise OSError("cache database unavailable")

        nas.manifest_for_artifact_set = unreadable  # type: ignore[method-assign]
    with pytest.raises(DistributionError) as unread:
        delivery.open_object(
            node_id=operation.node_id,
            plan_digest=payload.plan_digest,
            digest=spec.artifacts[0].sha256,
        )
    assert unread.value.code == (
        DistributionCode.OBJECT_UNAVAILABLE
        if transient
        else DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE
    )
    with pytest.raises(DistributionError) as foreign:
        delivery.open_object(
            node_id=operation.node_id, plan_digest=payload.plan_digest, digest="f" * 64
        )
    assert foreign.value.code == DistributionCode.UNASSIGNED


def test_a_warm_process_reads_a_deleted_set_as_definite() -> None:
    """The process memo holds the manifest after eviction deleted the set; the
    file read must still say the set is gone, not that it is waiting."""
    from types import SimpleNamespace

    from vonk_control.distribution.model_cache import ModelCacheObjectSource
    from vonk_control.distribution.types import DistributionError
    from vonk_control.model_cache.errors import ModelCacheNotFound

    digest, file_sha = "a" * 64, "b" * 64
    held = {"present": True}

    def cached_artifact_file(*_args: object) -> object:
        if not held["present"]:
            raise ModelCacheNotFound("not_found", "set deleted")
        raise AssertionError("not reached")

    service = SimpleNamespace(
        manifest_for_artifact_set=lambda _digest: SimpleNamespace(digest=digest),
        resolve_verified_artifact_set=lambda _digest: [
            {"path": "w.bin", "sha256": file_sha, "bytes": 4, "file": "w.bin"}
        ],
        cached_artifact_file=cached_artifact_file,
    )
    source = ModelCacheObjectSource.from_service(service)
    assert source.objects_for_set(digest)
    held["present"] = False
    with pytest.raises(DistributionError) as gone:
        source.open_object(file_sha, 4)
    assert gone.value.code == DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE


def test_the_object_source_memo_forgets_old_sets_and_keeps_recent_ones() -> None:
    """Catches a memo that keeps every set the process ever served: an old set
    is read again from the stored manifest, a recent one is not."""
    from types import SimpleNamespace

    from vonk_control.distribution.model_cache import ModelCacheObjectSource

    reads: list[str] = []

    def manifest(digest: str) -> SimpleNamespace:
        reads.append(digest)
        return SimpleNamespace(digest=digest)

    service = SimpleNamespace(
        manifest_for_artifact_set=manifest,
        resolve_verified_artifact_set=lambda d: [
            {"path": "w", "sha256": d[::-1], "bytes": 1, "file": "w"}
        ],
    )
    source = ModelCacheObjectSource.from_service(service)
    sets = [f"{number:064x}" for number in range(65)]
    for digest in sets:
        source.objects_for_set(digest)
    assert len(reads) == 65
    source.objects_for_set(sets[0])
    assert len(reads) == 66
    source.objects_for_set(sets[64])
    assert len(reads) == 66
