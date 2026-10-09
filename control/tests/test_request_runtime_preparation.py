"""Accepted execution requests own their exact runtime preparation."""

from __future__ import annotations

import hashlib

import pytest
from vonk_agent_protocol import AgentOperation as ProtocolAgentOperation
from vonk_agent_protocol import (
    ContainerRuntimeAction,
    DistributionObject,
    ExecuteContainerRuntimeRequestOperation,
    canonical_message,
)
from vonk_control.agent_jobs.stored import column_value
from vonk_control.distribution import DistributionService, MemoryObjectSource
from vonk_control.host_helper_authority import HostHelperAuthorityError
from vonk_control.models import AgentOperation

from .test_host_helper_authority import NOW, runtime_service


class RequestObjectSource(MemoryObjectSource):
    def objects_for_set(self, digest: str) -> tuple[DistributionObject, ...]:
        return self.artifact_manifests[digest]


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
    source = RequestObjectSource({})
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
            name="weights/model.bin", sha256=digest, bytes=len(content), kind="model"
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
