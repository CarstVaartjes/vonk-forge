"""Operation Api: diagnostics."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import agent_operation_states
from ..agent_upgrade_contract import AgentUpgradePackage
from ..agent_upgrade_status import (
    GENERIC_AGENT_UPGRADE_REASONS,
    RECOVERABLE_AGENT_UPGRADE_REASONS,
    agent_upgrade_next_action,
    operator_agent_upgrade_reason,
)
from ..bounded_json import mapping
from ..lifecycle.agent_operation import retry_scheduled
from ..logging import redact_text
from ..models import AgentNode, AgentOperation, AgentOperationAttempt, Job
from ..strict_json import read_stored_model, warn_unreadable_once
from .contracts import (
    AgentUpgradeDiagnosticsResponse,
    AgentUpgradeIdentityResponse,
    AgentUpgradeTargetDiagnosticsResponse,
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _agent_upgrade_diagnostics(
    session: Session, job_id: str
) -> AgentUpgradeDiagnosticsResponse | None:
    job = session.get(Job, job_id)
    if job is None or job.kind != "agent-upgrade":
        return None
    payload = mapping(job.payload)
    if payload is None:
        warn_unreadable_once("agent-upgrade", job_id)
        return None
    if "package" not in payload or payload["package"] is None:
        # An upgrade that carries no package document simply has no
        # diagnostics to project. Unreadable optional diagnostics are omitted.
        return None
    try:
        package = read_stored_model(AgentUpgradePackage, payload["package"])
    except (TypeError, ValueError):
        warn_unreadable_once("agent-upgrade", job_id)
        return None
    operations = list(
        session.scalars(
            select(AgentOperation)
            .where(
                AgentOperation.parent_job_id == job_id,
                AgentOperation.node_id.in_(job.targets),
            )
            .order_by(AgentOperation.created_at, AgentOperation.id)
            .limit(64)
        )
    )
    operations_by_node = {operation.node_id: operation for operation in operations}
    attempts = {
        attempt.operation_id: attempt
        for attempt in session.scalars(
            select(AgentOperationAttempt)
            .join(
                AgentOperation,
                AgentOperationAttempt.operation_id == AgentOperation.id,
            )
            .where(
                AgentOperation.parent_job_id == job_id,
                AgentOperation.node_id.in_(job.targets),
                AgentOperationAttempt.attempt == AgentOperation.current_attempt,
            )
            .limit(64)
        )
    }
    nodes = {
        node.node_id: node
        for node in session.scalars(
            select(AgentNode).where(AgentNode.node_id.in_(job.targets))
        )
    }
    targets: list[AgentUpgradeTargetDiagnosticsResponse] = []
    failure_details_unavailable = False
    retry_queued_any = False
    operator_summary = None
    for node_id in job.targets:
        operation = operations_by_node.get(node_id)
        attempt = None if operation is None else attempts.get(operation.id)
        result = None if attempt is None else attempt.result
        raw_reason = None
        if isinstance(result, Mapping):
            candidate = result.get("reason")
            if not isinstance(candidate, str):
                candidate = result.get("error_code")
            if isinstance(candidate, str):
                raw_reason = redact_text(candidate)[:1024]
        node = nodes.get(node_id)
        # The controller only transitions an upgrade operation to succeeded
        # after _contact_proves_target accepts authenticated contact plus the
        # complete runtime and result evidence. Matching digests alone is not
        # the success gate and must never be projected as proof here.
        target_proven = bool(operation is not None and operation.state == "succeeded")
        unresolved_generic = bool(
            not target_proven and raw_reason in GENERIC_AGENT_UPGRADE_REASONS
        )
        failure_details_unavailable = failure_details_unavailable or unresolved_generic
        retry_queued = bool(
            operation is not None and retry_scheduled(operation) is not None
        )
        retry_queued_any = retry_queued_any or retry_queued
        retry_not_before = (
            _aware(attempt.lease_deadline).isoformat()
            if attempt is not None and retry_queued
            else None
        )
        if unresolved_generic and operator_summary is None and raw_reason is not None:
            operator_summary = operator_agent_upgrade_reason(
                node_id=node_id,
                attempt_count=(0 if operation is None else operation.current_attempt),
                package=package.model_dump(mode="json"),
                observed_semantic_version=(
                    None if node is None else node.semantic_version
                ),
                observed_binary_digest=(None if node is None else node.binary_digest),
                observed_build_digest=None if node is None else node.build_digest,
                raw_reason=raw_reason,
                retry_queued=retry_queued,
            )
        targets.append(
            AgentUpgradeTargetDiagnosticsResponse(
                node_id=node_id,
                state="not-started" if operation is None else operation.state,
                attempts=0 if operation is None else operation.current_attempt,
                target_proven=target_proven,
                observed_identity=AgentUpgradeIdentityResponse(
                    version=None if node is None else node.semantic_version,
                    binary_digest=None if node is None else node.binary_digest,
                    build_digest=None if node is None else node.build_digest,
                ),
                raw_reason=raw_reason,
                retry_not_before=retry_not_before,
                retry_queued=retry_queued,
            )
        )
    return AgentUpgradeDiagnosticsResponse(
        expected_identity=AgentUpgradeIdentityResponse(
            version=package.package_version,
            binary_digest=package.target_binary_digest,
            build_digest=package.target_build_digest,
        ),
        targets=targets,
        failure_details_unavailable=failure_details_unavailable,
        next_action=(
            agent_upgrade_next_action(retry_queued=retry_queued_any)
            if any(
                not target.target_proven
                and target.attempts
                and (
                    target.state in agent_operation_states.PARKED
                    or target.raw_reason in RECOVERABLE_AGENT_UPGRADE_REASONS
                )
                for target in targets
            )
            else None
        ),
        operator_summary=operator_summary,
    )
