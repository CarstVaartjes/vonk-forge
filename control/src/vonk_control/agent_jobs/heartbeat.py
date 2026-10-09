"""Heartbeat for the node-scoped agent queue."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import select
from vonk_agent_protocol import (
    AgentDirective,
    AgentEvidenceCode,
    AgentOperation,
    AgentProgress,
    InvalidRequestReason,
    OperationProgress,
    WaitReason,
)

from ..agent_operation_facts import aware as _aware
from ..auth import AgentSource
from ..categorized_errors import InvalidValue
from ..logging import log_event
from ..models import AgentNode, AgentOperationAttempt, Job
from ..models import AgentOperation as StoredOperation
from ..operation_contract import validate_progress_update
from ..operation_progress import (
    progress_document,
    progress_write_due,
    sample_progress,
    stored_progress,
)
from .contracts import _LOGGER, _WORKLOAD_INTENT_OPERATIONS, AgentFence
from .endings import end_unobserved_order
from .retry import _renew_distribution_grant, superseded_cancellation_deadline
from .stored import column_field, column_is_document, column_value

if TYPE_CHECKING:
    from .service import AgentJobService


def heartbeat(
    self: AgentJobService,
    fence: AgentFence,
    progress: OperationProgress | None,
    lease_seconds: int,
    *,
    source: AgentSource | None = None,
) -> AgentDirective:
    self._mark_started()
    if lease_seconds <= 0:
        raise InvalidValue(
            "lease must be positive", reason=InvalidRequestReason.OUT_OF_RANGE
        )
    with self._sessions.begin() as session:
        operation, attempt = self._active(
            session,
            fence,
            source=source,
            allow_superseded_cancellation=True,
            # A heartbeat and the exact attempt's own outcome may both
            # re-acquire a lapsed lease inside the operation's launch
            # budget; a different fence or a spent budget may not.
            allow_lapsed_renewal=True,
        )
        now = self._clock()
        parent = session.get(Job, operation.parent_job_id)
        node = session.get(AgentNode, operation.node_id)
        superseded = bool(
            node is not None
            and operation.workload_intent_ordinal is not None
            and operation.workload_intent_ordinal != node.workload_intent_ordinal
        )
        if superseded:
            cancellation_deadline = superseded_cancellation_deadline(
                None if parent is None else column_value(parent, "result")
            )
            if cancellation_deadline is None or _aware(now) >= cancellation_deadline:
                end_unobserved_order(
                    self,
                    session,
                    operation,
                    attempt,
                    parent,
                    now,
                    reason=(
                        WaitReason.JOB_STATE_UNCERTAIN
                        if cancellation_deadline is None
                        else WaitReason.LEASE_LAPSED
                    ),
                    note="superseded cancellation authority ended",
                )
                return AgentDirective(
                    fence=attempt.fence, deadline=_aware(now), cancel_requested=True
                )
            deadline = min(
                cancellation_deadline,
                max(
                    _aware(attempt.lease_deadline),
                    _aware(now) + timedelta(seconds=lease_seconds),
                ),
            )
            attempt.lease_deadline = deadline
            return AgentDirective(
                fence=attempt.fence, deadline=deadline, cancel_requested=True
            )
        deadline = max(
            _aware(attempt.lease_deadline),
            _aware(now) + timedelta(seconds=lease_seconds),
        )
        message = AgentProgress.model_validate(
            {"fence": attempt.fence, "progress": progress}
        )
        write_progress = message.progress is None
        if message.progress is not None:
            try:
                current_progress = message.progress
                retained = stored_progress(attempt)
                if (
                    operation.kind == AgentOperation.ARTIFACT_DISTRIBUTION.value
                    and retained is not None
                ):
                    # A restarted transfer walks already durable objects again.
                    # Replayed offsets are not loss of retained operation bytes.
                    replayed: dict[str, int] = {}
                    given = current_progress.model_fields_set
                    if "completed_bytes" in given:
                        replayed["completed_bytes"] = max(
                            current_progress.completed_bytes,
                            retained.completed_bytes,
                        )
                    if (
                        "completed_items" in given
                        and current_progress.completed_items is not None
                        and retained.completed_items is not None
                    ):
                        replayed["completed_items"] = max(
                            current_progress.completed_items,
                            retained.completed_items,
                        )
                    current_progress = current_progress.model_copy(update=replayed)
                validated = validate_progress_update(
                    retained, current_progress, partial=False
                )
                write_progress = progress_write_due(retained, validated, _aware(now))
                if write_progress:
                    attempt.progress = progress_document(
                        sample_progress(retained, validated, _aware(now))
                    )
            except (TypeError, ValueError):
                # Progress is optional evidence on a lease heartbeat: a
                # progress document that does not follow the retained one
                # is not stored, and the heartbeat still renews the lease.
                log_event(
                    _LOGGER,
                    "agent.evidence_dropped",
                    service="control-api",
                    code=AgentEvidenceCode.PROGRESS_DROPPED.value,
                    endpoint="heartbeat",
                    node_id=operation.node_id,
                )
                write_progress = True
        if write_progress:
            attempt.lease_deadline = deadline
            operation.updated_at = now
        else:
            deadline = _aware(attempt.lease_deadline)
        parent = session.get(Job, operation.parent_job_id)
        cancel_requested = bool(
            parent is not None
            and column_is_document(parent, "result")
            and column_field(parent, "result", "cancel_requested") is True
        )
        if (
            operation.kind
            in {
                AgentOperation.ARTIFACT_DISTRIBUTION.value,
                AgentOperation.RECIPE_INSTALL.value,
                AgentOperation.RECIPE_START.value,
                AgentOperation.RECIPE_JOB_RUN.value,
            }
            and not cancel_requested
        ):
            # Large model copies outlive their initial one-hour grant.
            # Only the authenticated, currently fenced operation may
            # renew its exact node/plan, before that grant expires. Keep
            # renewals sparse and never resurrect revoked/expired access.
            _renew_distribution_grant(session, operation, now, live_only=True)
        return AgentDirective(
            fence=message.fence,
            deadline=deadline,
            cancel_requested=cancel_requested,
        )


def known_superseded_cancellation(
    self: AgentJobService, fence: AgentProgress, *, source: AgentSource | None = None
) -> bool:
    """Identify an exact old cancellation for a benign heartbeat response."""
    with self._sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == fence.fence
            )
        )
        operation = (
            session.get(StoredOperation, attempt.operation_id)
            if attempt is not None
            else None
        )
        parent = (
            session.get(Job, operation.parent_job_id) if operation is not None else None
        )
        if (
            attempt is None
            or operation is None
            or parent is None
            or operation.kind not in _WORKLOAD_INTENT_OPERATIONS
            or operation.current_attempt != attempt.attempt
            or operation.workload_intent_ordinal
            != column_field(parent, "payload", "workload_intent_ordinal")
            or not column_is_document(parent, "result")
            or column_field(parent, "result", "cancel_requested") is not True
            or self._target_scope(column_value(parent, "targets")) is None
            or operation.node_id not in column_value(parent, "targets")
        ):
            return False
        contact_serial = (
            source.identity.certificate_serial
            if source is not None
            else attempt.agent_certificate_serial
        )
        identity = self._lock_identity(session, operation.node_id, contact_serial)
        now = self._clock()
        if identity is None or not self._identity_is_active(*identity, now):
            return False
        node, _certificate = identity
        return bool(
            node.state == "active"
            and node.revoked_at is None
            and (
                operation.state == "cancelled"
                or (
                    operation.workload_intent_ordinal is not None
                    and operation.workload_intent_ordinal < node.workload_intent_ordinal
                )
            )
        )
