"""Agent acceptance resolves exact owners, never scans history or stores receipts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Response
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentClaim,
    AgentDirective,
    AgentProgress,
    AgentResult,
    RecipeRunObservationsWire,
    canonical_message,
)
from vonk_agent_protocol.claims import ClaimRequest
from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_agent_protocol.enrollment import ActivateRequest, IssuedCertificateResponse
from vonk_agent_protocol.state_machines import CertificateRecordState
from vonk_agent_protocol.telemetry import TelemetryRequest

from .agent_operation_facts import aware
from .auth import agent_identity_from_scope
from .inventory_items import inventory_items, runtime_value
from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    NodeInventorySnapshot,
    NodeTelemetrySample,
    RecipeBuild,
    RunNode,
)
from .operator_receipt_contract import AcceptanceReceipt, ReceiptOwner, ReceiptPolicy

if TYPE_CHECKING:
    from .operator_acceptance import AcceptanceArguments, OperatorReceiptResolver


AGENT_POLICIES = frozenset(
    {
        ReceiptPolicy.AGENT_CLAIM,
        ReceiptPolicy.AGENT_HEARTBEAT,
        ReceiptPolicy.AGENT_RESULT,
        ReceiptPolicy.AGENT_INVENTORY,
        ReceiptPolicy.AGENT_TELEMETRY,
        ReceiptPolicy.AGENT_OBSERVATIONS,
        ReceiptPolicy.AGENT_IMAGE,
        ReceiptPolicy.AGENT_OUTPUT,
        ReceiptPolicy.AGENT_GRANT,
        ReceiptPolicy.ENROLLMENT_ISSUE,
        ReceiptPolicy.ENROLLMENT_ACTIVATE,
    }
)


@dataclass(frozen=True)
class InventoryAcceptance:
    id: str
    evidence_digest: str


@dataclass(frozen=True)
class ObservationAcceptance:
    # Only rows actually judged and written by the producer, not fleet history.
    node_ids: tuple[str, ...]


def resolve_agent_receipt(
    resolver: OperatorReceiptResolver,
    policy: ReceiptPolicy,
    result: Response,
    arguments: AcceptanceArguments,
) -> AcceptanceReceipt:
    from .operator_acceptance import _available, _identity, _refused, _require

    request = _available(arguments.request)
    body = arguments.body
    identity = agent_identity_from_scope(dict(request.scope))
    if policy == ReceiptPolicy.AGENT_OUTPUT:
        _require(identity is not None)
        assert identity is not None
        job_id = _identity(request.path_params.get("job_id"))
        digest = _identity(request.path_params.get("sha256"))
        saved = _available(resolver.artifacts).get(job_id)
        _require(
            any(
                item.name == request.headers.get("x-vonk-artifact-name")
                and item.sha256 == digest
                and str(item.size_bytes) == request.headers.get("content-length")
                and item.media_type == request.headers.get("content-type")
                for item in saved.output_files
            )
        )
        return AcceptanceReceipt(
            owner=ReceiptOwner.ARTIFACT_JOB,
            id=job_id,
            href=f"/api/artifact-jobs/{job_id}",
            content_digest=digest,
        )

    required = _available(resolver.agent)
    # Enrollment already committed the public certificate and its exact CSR
    # binding. It is not browser token issuance and creates no second owner.
    if policy == ReceiptPolicy.ENROLLMENT_ISSUE:
        _require(isinstance(result, Response))
        issued = IssuedCertificateResponse.model_validate_json(bytes(result.body))
        with required.sessions() as session:
            certificate = session.get(AgentCertificate, issued.serial)
            _require(certificate is not None)
            assert certificate is not None
            _require(
                certificate.node_id == issued.node_id
                and certificate.generation == issued.generation
                and certificate.fingerprint == issued.fingerprint
                and certificate.certificate_pem == issued.certificate_pem
                and certificate.chain_pem == issued.chain_pem
                and certificate.revoked_at is None
            )
        return AcceptanceReceipt(
            owner=ReceiptOwner.ENROLLMENT,
            id=issued.node_id,
            href=f"/api/fleet/{issued.node_id}",
            revision=issued.generation,
            content_digest=issued.fingerprint,
        )

    _require(identity is not None)
    assert identity is not None
    href = f"/api/fleet/{identity.node_id}"
    with required.sessions() as session:
        if policy == ReceiptPolicy.ENROLLMENT_ACTIVATE:
            _require(isinstance(body, ActivateRequest))
            assert isinstance(body, ActivateRequest)
            certificate = session.get(AgentCertificate, identity.certificate_serial)
            _require(certificate is not None)
            assert certificate is not None
            _require(
                certificate.node_id == body.node_id == identity.node_id
                and certificate.generation == body.generation
                and certificate.state == CertificateRecordState.ACTIVE
                and certificate.revoked_at is None
            )
            return AcceptanceReceipt(
                owner=ReceiptOwner.ENROLLMENT,
                id=identity.node_id,
                href=href,
                revision=body.generation,
            )

        if policy == ReceiptPolicy.AGENT_INVENTORY:
            evidence = getattr(request.state, "inventory_acceptance", None)
            _require(isinstance(evidence, InventoryAcceptance))
            assert isinstance(evidence, InventoryAcceptance)
            # Project scalar identity only: damaged/large old JSON is irrelevant.
            saved = session.execute(
                select(
                    NodeInventorySnapshot.node_id,
                    NodeInventorySnapshot.evidence_digest,
                ).where(NodeInventorySnapshot.id == evidence.id)
            ).one_or_none()
            _require(
                saved is not None
                and saved.node_id == identity.node_id
                and saved.evidence_digest == evidence.evidence_digest
            )
            return AcceptanceReceipt(
                owner=ReceiptOwner.INVENTORY,
                id=evidence.id,
                href=href,
                content_digest=evidence.evidence_digest,
            )

        if policy == ReceiptPolicy.AGENT_TELEMETRY:
            _require(isinstance(body, TelemetryRequest))
            assert isinstance(body, TelemetryRequest)
            for sample in body.samples:
                saved = session.scalar(
                    select(NodeTelemetrySample).where(
                        NodeTelemetrySample.node_id == identity.node_id,
                        NodeTelemetrySample.boot_id == sample.boot_id,
                        NodeTelemetrySample.observed_at == sample.observed_at,
                    )
                )
                _require(saved is not None)
                assert saved is not None
                # These are the producer's contract fields, not another schema.
                _require(
                    all(
                        aware(saved.observed_at) == sample.observed_at
                        if name == "observed_at"
                        else getattr(saved, name) == getattr(sample, name)
                        for name in type(sample).model_fields
                    )
                )
            return AcceptanceReceipt(
                owner=ReceiptOwner.TELEMETRY,
                id=saved.id,
                href=href,
                content_digest=hashlib.sha256(canonical_message(body)).hexdigest(),
            )

        if policy == ReceiptPolicy.AGENT_OBSERVATIONS:
            evidence = getattr(request.state, "observation_acceptance", None)
            _require(
                isinstance(body, RecipeRunObservationsWire)
                and isinstance(evidence, ObservationAcceptance)
            )
            assert isinstance(body, RecipeRunObservationsWire)
            assert isinstance(evidence, ObservationAcceptance)
            for node_id in evidence.node_ids:
                saved = session.execute(
                    select(
                        RunNode.node_id,
                        RunNode.observation_observed_at,
                    ).where(RunNode.id == node_id)
                ).one_or_none()
                _require(
                    saved is not None
                    and saved.node_id == identity.node_id
                    and saved.observation_observed_at is not None
                    and aware(saved.observation_observed_at) >= body.observed_at
                )
            return AcceptanceReceipt(
                owner=ReceiptOwner.RUN_OBSERVATION,
                id=identity.node_id,
                href=href,
                content_digest=hashlib.sha256(canonical_message(body)).hexdigest(),
            )

        if policy == ReceiptPolicy.AGENT_IMAGE:
            build_id = _identity(request.path_params.get("build_id"))
            saved = session.get(RecipeBuild, build_id)
            _require(
                saved is not None
                and saved.builder_node_id == identity.node_id
                and saved.oci_layout_sha256
                == request.headers.get("x-vonk-oci-layout-sha256")
                and saved.image_digest == request.headers.get("x-vonk-image-digest")
                and str(saved.image_bytes) == request.headers.get("x-vonk-image-bytes")
            )
            assert saved is not None
            return AcceptanceReceipt(
                owner=ReceiptOwner.RECIPE_IMAGE,
                id=build_id,
                href=href,
                content_digest=saved.oci_layout_sha256,
            )

        if policy == ReceiptPolicy.AGENT_CLAIM:
            _require(isinstance(body, ClaimRequest) and isinstance(result, Response))
            assert isinstance(body, ClaimRequest)
            node = session.get(AgentNode, identity.node_id)
            _require(node is not None)
            assert node is not None
            _require(
                node.contact_certificate_serial == identity.certificate_serial
                and node.last_seen_at is not None
                and runtime_value(node, lambda identity: identity.binary_digest)
                == body.runtime_identity.binary_digest
                and runtime_value(node, lambda identity: identity.build_digest)
                == body.runtime_identity.build_digest
                and runtime_value(node, lambda identity: identity.architecture)
                == body.runtime_identity.architecture
                and inventory_items(node).preflight_fingerprint
                == body.preflight_fingerprint
            )
            if result.status_code == 204:
                return AcceptanceReceipt(
                    owner=ReceiptOwner.NODE,
                    id=identity.node_id,
                    href=href,
                    content_digest=node.contact_observation_digest,
                )
            claim = AgentClaim.model_validate_json(bytes(result.body))
            fence = claim.fence
        elif isinstance(body, AgentProgress | AgentResult):
            fence = body.fence
        elif policy == ReceiptPolicy.AGENT_GRANT:
            # Grants read an already accepted authority and mint a signed,
            # short-lived capability. No new execution intent is accepted.
            from .agent_api.common import HostHelperGrantResponse

            _require(isinstance(result, Response))
            grant = HostHelperGrantResponse.model_validate_json(
                bytes(result.body)
            ).grant
            _require(grant.claims.node_id == identity.node_id)
            # Package activation is bound by its existing upgrade payload nonce.
            fence = getattr(body, "fence", None)
            if fence is None:
                from .agent_api.common import PackageActivationGrantRequest

                _require(isinstance(body, PackageActivationGrantRequest))
                assert isinstance(body, PackageActivationGrantRequest)
                owned = session.execute(
                    select(
                        AgentOperation.id,
                        AgentOperation.payload_digest,
                    )
                    .where(
                        AgentOperation.node_id == identity.node_id,
                        AgentOperation.kind == OperationKind.AGENT_UPGRADE,
                        AgentOperation.payload["rollback"]["attempt_nonce"].as_string()
                        == body.receipt.attempt_nonce,
                    )
                    .limit(2)
                ).all()
                _require(len(owned) == 1)
                return AcceptanceReceipt(
                    owner=ReceiptOwner.AGENT_OPERATION,
                    id=owned[0].id,
                    href=href,
                    content_digest=owned[0].payload_digest,
                )
        else:
            raise _refused("Agent response has no exact receipt context")

        saved = session.execute(
            select(AgentOperationAttempt, AgentOperation)
            .join(
                AgentOperation,
                AgentOperation.id == AgentOperationAttempt.operation_id,
            )
            .where(AgentOperationAttempt.fence == fence)
        ).one_or_none()
        _require(saved is not None)
        assert saved is not None
        attempt, operation = saved
        _require(operation.node_id == identity.node_id)
        if policy == ReceiptPolicy.AGENT_CLAIM:
            from .agent_jobs.stored import column_value

            _require(
                attempt.agent_certificate_serial == identity.certificate_serial
                and operation.current_attempt == attempt.attempt
                and claim.payload == column_value(operation, "payload")
                and claim.deadline == aware(attempt.lease_deadline)
            )
        elif policy == ReceiptPolicy.AGENT_HEARTBEAT:
            directive = AgentDirective.model_validate_json(bytes(result.body))
            _require(
                directive.fence == attempt.fence
                and aware(attempt.lease_deadline) >= directive.deadline
            )
        elif policy == ReceiptPolicy.AGENT_RESULT:
            from vonk_agent_protocol import AgentResultState

            from .agent_jobs.evidence import sanitized_failure_report
            from .agent_jobs.stored import column_message
            from .agent_outcome import stored_report

            assert isinstance(body, AgentResult)
            report, _outcome = stored_report(operation.kind, body)
            if report.state in {AgentResultState.FAILED, AgentResultState.OBSERVING}:
                report = sanitized_failure_report(operation.kind, report)
            _require(
                column_message(attempt, "result") == canonical_message(report.result)
            )
        return AcceptanceReceipt(
            owner=ReceiptOwner.AGENT_ATTEMPT,
            id=attempt.fence,
            href=href,
            revision=attempt.attempt,
            request_id=arguments.request_id
            if policy == ReceiptPolicy.AGENT_CLAIM
            else None,
            content_digest=operation.payload_digest,
        )
