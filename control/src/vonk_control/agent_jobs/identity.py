"""Identity for the node-scoped agent queue."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentClaim,
    AgentProgress,
    AgentResult,
    InvalidRequestReason,
    SecurityRefusalReason,
    canonical_message,
)
from vonk_agent_protocol.claims import AGENT_PROTOCOL_VERSION, AgentRuntimeIdentity

from ..agent_operation_facts import aware as _aware
from ..agent_operation_facts import lapsed_renewal_allowed as _lapsed_renewal_allowed
from ..auth import AgentSource
from ..categorized_errors import InvalidValue
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..logging import redact_text
from ..models import (
    AgentCertificate,
    AgentNode,
    AgentNodeProfile,
    AgentOperationAttempt,
    Job,
)
from ..models import AgentOperation as StoredOperation
from .contracts import (
    _WORKLOAD_INTENT_OPERATIONS,
    AgentContactIdentityMismatch,
    AgentFence,
    StaleAgentFence,
)
from .stored import column_field, column_is_document, column_value

if TYPE_CHECKING:
    from .service import AgentJobService


def _active(
    self: AgentJobService,
    session: Session,
    fence: AgentFence,
    *,
    source: AgentSource | None = None,
    allow_superseded_cancellation: bool = False,
    allow_lapsed_renewal: bool = False,
) -> tuple[StoredOperation, AgentOperationAttempt]:
    token = self._fence_token(fence)
    identity_hint = session.execute(
        select(
            StoredOperation.id,
            StoredOperation.node_id,
            AgentOperationAttempt.agent_certificate_serial,
            StoredOperation.parent_job_id,
        )
        .join(
            AgentOperationAttempt,
            AgentOperationAttempt.operation_id == StoredOperation.id,
        )
        .where(AgentOperationAttempt.fence == token)
    ).one_or_none()
    if identity_hint is None:
        raise StaleAgentFence(
            "agent operation lease, certificate, or fence is stale",
            reason=SecurityRefusalReason.STALE_FENCE,
        )
    operation_id, node_id, certificate_serial, parent_job_id = identity_hint
    scopes = self._lock_operation_scopes(session, (operation_id,), node_id)
    if scopes is None or scopes[operation_id][0] != parent_job_id:
        raise StaleAgentFence(
            "agent operation lease, certificate, or fence is stale",
            reason=SecurityRefusalReason.STALE_FENCE,
        )
    identity = self._lock_identity(session, node_id, certificate_serial)
    now = self._clock()
    if identity is None or not self._identity_is_active(*identity, now):
        raise StaleAgentFence(
            "agent operation lease, certificate, or fence is stale",
            reason=SecurityRefusalReason.STALE_FENCE,
        )
    node, certificate = identity
    self._consume_contact(session, source, node, certificate)
    parent = session.scalar(
        select(Job).where(Job.id == parent_job_id).with_for_update(of=Job)
    )
    if (
        parent is None
        or parent.state not in {"queued", "running"}
        or node.node_id not in column_value(parent, "targets")
    ):
        raise StaleAgentFence(
            "agent operation lease, certificate, or fence is stale",
            reason=SecurityRefusalReason.STALE_FENCE,
        )
    operation = session.scalar(
        select(StoredOperation)
        .where(StoredOperation.id == operation_id)
        .with_for_update(of=StoredOperation)
        .execution_options(populate_existing=True)
    )
    if operation is None:
        raise StaleAgentFence(
            "agent operation lease, certificate, or fence is stale",
            reason=SecurityRefusalReason.STALE_FENCE,
        )
    if (
        self._target_scope(column_value(parent, "targets")) != scopes[operation_id][1]
        or operation.parent_job_id != parent_job_id
        or operation.node_id != node.node_id
        or operation.authority_revision != parent.authority_revision
        or (
            operation.kind in _WORKLOAD_INTENT_OPERATIONS
            and operation.workload_intent_ordinal is None
        )
        or (
            operation.kind in _WORKLOAD_INTENT_OPERATIONS
            and operation.workload_intent_ordinal
            != column_field(parent, "payload", "workload_intent_ordinal")
        )
        or (
            operation.workload_intent_ordinal is not None
            and operation.workload_intent_ordinal != node.workload_intent_ordinal
            and not (
                allow_superseded_cancellation
                and operation.kind in _WORKLOAD_INTENT_OPERATIONS
                and operation.workload_intent_ordinal < node.workload_intent_ordinal
                and column_is_document(parent, "result")
                and column_field(parent, "result", "cancel_requested") is True
            )
        )
        or node.state != "active"
        or node.revoked_at is not None
    ):
        raise StaleAgentFence(
            "agent operation lease, certificate, or fence is stale",
            reason=SecurityRefusalReason.STALE_FENCE,
        )
    attempt = session.scalar(
        select(AgentOperationAttempt)
        .where(
            AgentOperationAttempt.fence == token,
            AgentOperationAttempt.operation_id == operation.id,
        )
        .with_for_update(of=AgentOperationAttempt)
        .execution_options(populate_existing=True)
    )
    if (
        attempt is None
        or operation.state != "running"
        or attempt.operation_id != operation.id
        or operation.current_attempt != attempt.attempt
        or attempt.state != "running"
        or (
            _aware(attempt.lease_deadline) <= _aware(now)
            and not (
                allow_lapsed_renewal
                # The exact fence that holds the attempt may re-acquire it
                # while the operation's own immutable start budget is still
                # open.  The lease still decides when another owner may take
                # over, so this restores a healthy executor without opening
                # the fence to anyone else.
                and _lapsed_renewal_allowed(operation, now)
            )
        )
    ):
        raise StaleAgentFence(
            "agent operation lease, certificate, or fence is stale",
            reason=SecurityRefusalReason.STALE_FENCE,
        )
    self._record_contact(session, node, certificate, now, None, None, None)
    return operation, attempt


def _record_contact(
    session: Session,
    node: AgentNode,
    certificate: AgentCertificate,
    now: datetime,
    runtime_identity: AgentRuntimeIdentity | None,
    preflight_fingerprint: str | None,
    hostname: str | None,
) -> None:
    current = None if node.last_seen_at is None else _aware(node.last_seen_at)
    observed = _aware(now)
    contact_time = observed if current is None or observed > current else current
    node.last_seen_at = contact_time
    if hostname is not None:
        profile = session.scalar(
            select(AgentNodeProfile)
            .where(AgentNodeProfile.node_id == node.node_id)
            .with_for_update(of=AgentNodeProfile)
        )
        if profile is not None and profile.hostname != hostname:
            profile.hostname = hostname
    if runtime_identity is not None:
        # A claim is the only caller with a runtime identity; it passed the
        # single protocol gate, so the node now speaks this version.
        node.protocol_version = AGENT_PROTOCOL_VERSION
        node.preflight_fingerprint = preflight_fingerprint
        node.architecture = runtime_identity.architecture
        node.semantic_version = runtime_identity.semantic_version
        node.build_digest = runtime_identity.build_digest
        node.binary_digest = runtime_identity.binary_digest
        node.contact_certificate_serial = certificate.serial
        node.contact_observation_digest = hashlib.sha256(
            canonical_message(
                {
                    "certificate_fingerprint": certificate.fingerprint,
                    "certificate_serial": certificate.serial,
                    "node_id": node.node_id,
                    "observed_at": _aware(contact_time).isoformat(),
                    "hostname": hostname,
                    "runtime_identity": runtime_identity,
                }
            )
        ).hexdigest()


def _runtime_identity(
    value: object,
) -> AgentRuntimeIdentity:
    if value is None:
        raise InvalidValue(
            "agent runtime identity is required",
            reason=InvalidRequestReason.INCOMPLETE,
        )
    try:
        return AgentRuntimeIdentity.model_validate(value)
    except (TypeError, ValidationError) as error:
        raise InvalidValue(
            "agent runtime identity is invalid",
            reason=InvalidRequestReason.MALFORMED,
        ) from error


def _consume_contact(
    self: AgentJobService,
    session: Session,
    source: AgentSource | None,
    node: AgentNode,
    certificate: AgentCertificate,
) -> None:
    if source is None:
        return
    identity = source.identity
    if (
        identity.node_id != node.node_id
        or identity.certificate_serial != certificate.serial
        or identity.certificate_fingerprint != certificate.fingerprint
        or identity.verified is not True
    ):
        raise AgentContactIdentityMismatch(
            "agent contact source does not match its locked identity",
            reason=SecurityRefusalReason.AGENT_IDENTITY_MISMATCH,
        )
    if self._contact_consumer is None:
        # The contact was authenticated above; only its persistence is
        # unavailable, and that is bookkeeping, not a reason to refuse the
        # Spark.  It is recorded and the next contact is stored normally.
        retire_as_unknown(
            "agent-job.contact",
            node.node_id,
            BookkeepingReason.EVIDENCE_UNAVAILABLE,
            "no contact consumer is configured",
        )
        return
    self._contact_consumer(session, source)


def _fence_token(fence: AgentFence) -> str:
    if isinstance(fence, str):
        return fence
    if isinstance(fence, (AgentClaim, AgentProgress, AgentResult)):
        return fence.fence
    raise StaleAgentFence(
        "agent operation lease, certificate, or fence is stale",
        reason=SecurityRefusalReason.STALE_FENCE,
    )


def _reason(reason: str | None) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise InvalidValue(
            "failure reason is required", reason=InvalidRequestReason.INCOMPLETE
        )
    return redact_text(reason)[:1024]


def _lock_identity(
    session: Session,
    node_id: str,
    certificate_serial: str,
) -> tuple[AgentNode, AgentCertificate] | None:
    node = session.scalar(
        select(AgentNode)
        .where(AgentNode.node_id == node_id)
        .with_for_update(of=AgentNode)
    )
    if node is None:
        return None
    certificate = session.scalar(
        select(AgentCertificate)
        .where(
            AgentCertificate.serial == certificate_serial,
            AgentCertificate.node_id == node_id,
        )
        .with_for_update(of=AgentCertificate)
    )
    return None if certificate is None else (node, certificate)


def _identity_is_active(
    node: AgentNode,
    certificate: AgentCertificate,
    now: datetime,
) -> bool:
    return (
        node.state == "active"
        and node.revoked_at is None
        and certificate.state == "active"
        and certificate.revoked_at is None
        and _aware(certificate.not_before) <= _aware(now)
        and _aware(certificate.not_after) > _aware(now)
    )
