"""Canonical, persisted successful probe evidence for lifecycle test hosts."""

import uuid
from datetime import timedelta

from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.runtime_preflight import RuntimePreflightResult
from vonk_control.inventory_items import record_preflight
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    Job,
)
from vonk_control.runtime_preflight import (
    mandatory_capabilities,
    recipe_requirements,
    request_digest,
)

from .runtime_identity_support import set_inventory_fingerprint


def record_passing_preflight(
    sessions, now, *, floor=10, source_build=False, node_ids=None
):
    with sessions.begin() as session:
        recipe = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        request = recipe_requirements(
            recipe.document, source_build=source_build, minimum_free_bytes=floor
        )
        digest = request_digest(request)
        for node in session.scalars(select(AgentNode)):
            if node_ids is not None and node.node_id not in node_ids:
                continue
            set_inventory_fingerprint(node, "a" * 64)
            key = str(uuid.uuid4())
            job = Job(
                id=key,
                request_id=key,
                kind="runtime.preflight.v1",
                state="succeeded",
                actor="test",
                authority_revision=digest,
                targets=[node.node_id],
                payload_digest=digest,
                payload=request.model_dump(mode="json"),
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            operation = AgentOperation(
                parent_job_id=job.id,
                node_id=node.node_id,
                kind="runtime.preflight.v1",
                payload_digest=digest,
                payload=request.model_dump(mode="json"),
                authority_revision=digest,
                state="succeeded",
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
            session.add(operation)
            session.flush()
            result = RuntimePreflightResult.model_validate_json(
                canonical_message(
                    {
                        "fingerprint": "a" * 64,
                        "observed_at": int(now.timestamp()),
                        "findings": [
                            {
                                "capability": value,
                                "status": "passed",
                                "code": "available",
                            }
                            for value in mandatory_capabilities(request)
                        ],
                    }
                )
            )
            session.add(
                AgentOperationAttempt(
                    operation_id=operation.id,
                    attempt=1,
                    fence=str(uuid.uuid4()),
                    lease_deadline=now + timedelta(seconds=60),
                    agent_certificate_serial=session.scalar(
                        select(AgentCertificate.serial).where(
                            AgentCertificate.node_id == node.node_id,
                            AgentCertificate.state == "active",
                        )
                    ),
                    state="succeeded",
                    result=result.model_dump(mode="json"),
                )
            )
            record_preflight(node, digest, result, now)
