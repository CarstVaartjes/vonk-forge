"""Aggregation for the node-scoped agent queue."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation, InvalidRequestReason, LifecycleState

from .. import agent_operation_states, job_states
from ..agent_upgrade_status import operator_agent_upgrade_reason
from ..categorized_errors import MissingRecord
from ..lifecycle.agent_operation import (
    aggregate_parent_state,
    is_artifact_owned,
    set_parent_state,
)
from ..lifecycle.artifact_job import ArtifactJobAdapter
from ..logging import redact_text
from ..models import AgentNode, AgentOperationAttempt, Job
from ..models import AgentOperation as StoredOperation
from ..offline_stops import is_deferred_stop
from .contracts import _ABANDONABLE_OPERATIONS
from .retry import _abandon_operation, _retry_authorized_for_current_attempt
from .stored import column_field, column_is_document, column_value

if TYPE_CHECKING:
    from .service import AgentJobService


def _aggregate_parent(
    self: AgentJobService, session: Session, parent_job_id: str
) -> None:
    """Aggregate a parent from its orders, then project a one-shot job's state.

    Every change of an order reaches here, so the artifact job (a projection of
    its order) is updated by the same transaction that changed the order.
    """

    self._aggregate_parent_state(session, parent_job_id)
    parent = session.get(Job, parent_job_id)
    if parent is not None and is_artifact_owned(parent):
        orders = session.scalars(
            select(StoredOperation).where(
                StoredOperation.parent_job_id == parent_job_id
            )
        )
        adapter = ArtifactJobAdapter(session, clock=self._clock)
        for order in orders:
            adapter.project_for_order(order, self._clock())


def _aggregate_parent_state(
    self: AgentJobService, session: Session, parent_job_id: str
) -> None:
    job = session.scalar(
        select(Job).where(Job.id == parent_job_id).with_for_update(of=Job)
    )
    if job is None:
        raise MissingRecord(parent_job_id, reason=InvalidRequestReason.NOT_FOUND)
    if (
        job.kind == "recipe.build.v1"
        and column_is_document(job, "result")
        and column_field(job, "result", "cancel_requested") is True
        and column_field(job, "result", "cancelled") is not True
    ):
        # A stopped build needs a separate cleanup receipt before its
        # reservation can be released, even if completion raced removal.
        set_parent_state(
            job,
            LifecycleState.OBSERVING.value,
            None,
            self._clock(),
            keep_reason=True,
        )
        return
    if job.kind == "agent-upgrade" and self._advance_rollout is not None:
        self._advance_rollout(session, job)
        return
    operations = list(
        session.scalars(
            select(StoredOperation)
            .where(StoredOperation.parent_job_id == parent_job_id)
            .order_by(StoredOperation.created_at, StoredOperation.id)
        )
    )
    has_deferred = any(is_deferred_stop(job, operation) for operation in operations)
    operations = [
        operation for operation in operations if not is_deferred_stop(job, operation)
    ]
    if not operations and job.kind == AgentOperation.RECIPE_STOP:
        return
    retrying = [
        operation
        for operation in operations
        if _retry_authorized_for_current_attempt(operation)
    ]
    cancel_requested = column_is_document(job, "result") and (
        column_field(job, "result", "cancel_requested") is True
    )
    verdict = aggregate_parent_state(
        [(operation.state, operation in retrying) for operation in operations],
        cancel_requested=cancel_requested,
    )
    if verdict == "queued":
        set_parent_state(job, "queued", retrying[0].status_reason, self._clock())
        return
    if (
        job.kind == "agent-upgrade"
        and job.state in job_states.words(LifecycleState.NEEDS_OPERATOR)
        and set(column_value(job, "targets"))
        - {operation.node_id for operation in operations}
    ):
        # Sequential agent upgrades intentionally materialize one target at
        # a time. If the next target drifted ineligible, preserve the
        # service's specific operator-facing reason instead of declaring the
        # job successful merely because every materialized operation passed.
        return
    if verdict is None:
        return
    state = verdict
    set_parent_state(job, state, None, self._clock(), keep_reason=True)
    if state == "succeeded":
        if not has_deferred:
            job.status_reason = None
        return
    if state == "failed":
        # A failed job grants no further claims, so a sibling still parked
        # for retry would wait forever behind a retry that cannot run.
        for operation in operations:
            if (
                operation.state in agent_operation_states.PARKED
                and operation.kind in _ABANDONABLE_OPERATIONS
            ):
                _abandon_operation(operation, job.id, job.updated_at)
    for operation in operations:
        if operation.state != state:
            continue
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        if attempt is not None and column_value(attempt, "result") is not None:
            reason = column_field(attempt, "result", "reason")
            if not isinstance(reason, str):
                reason = column_field(attempt, "result", "error_code")
            if isinstance(reason, str):
                if job.kind == "agent-upgrade":
                    package = column_field(job, "payload", "package")
                    node = session.get(AgentNode, operation.node_id)
                    if isinstance(package, Mapping):
                        reason = operator_agent_upgrade_reason(
                            node_id=operation.node_id,
                            attempt_count=operation.current_attempt,
                            package=package,
                            observed_semantic_version=(
                                None if node is None else node.semantic_version
                            ),
                            observed_binary_digest=(
                                None if node is None else node.binary_digest
                            ),
                            observed_build_digest=(
                                None if node is None else node.build_digest
                            ),
                            raw_reason=reason,
                            retry_queued=_retry_authorized_for_current_attempt(
                                operation
                            ),
                        )
                job.status_reason = redact_text(reason)[:1024]
                return
    job.status_reason = None
