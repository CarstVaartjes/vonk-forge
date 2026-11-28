"""Aggregation for the node-scoped agent queue."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from vonk_agent_protocol.agent_operations import AgentOperation
from vonk_agent_protocol.agent_words import FailureStage
from vonk_agent_protocol.failure_evidence import FailureDiagnostics, decisive_first
from vonk_agent_protocol.lifecycle_vocabulary import (
    InvalidRequestReason,
    LifecycleState,
)
from vonk_control import agent_operation_states
from vonk_control.agent_jobs.contracts import _ABANDONABLE_OPERATIONS
from vonk_control.agent_jobs.evidence import _is_refusal_reason
from vonk_control.agent_jobs.retry import (
    _abandon_operation,
    _retry_authorized_for_current_attempt,
)
from vonk_control.agent_jobs.stored import (
    column_field,
    column_is_document,
    column_value,
)
from vonk_control.agent_upgrades.status import operator_agent_upgrade_reason
from vonk_control.categorized_errors import MissingRecord
from vonk_control.distribution_executor.receipts import project_child
from vonk_control.inventory.items import runtime_value
from vonk_control.jobs import states as job_states
from vonk_control.lifecycle.agent_operation import (
    aggregate_parent_state,
    is_artifact_owned,
    set_parent_state,
)
from vonk_control.lifecycle.artifact_job import ArtifactJobAdapter
from vonk_control.lifecycle.job_ranks import job_run_verdict
from vonk_control.logging import redact_text
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation as StoredOperation
from vonk_control.models.operations import AgentOperationAttempt
from vonk_control.offline_stops import is_deferred_stop
from vonk_control.strict_json import serialize_json_value

if TYPE_CHECKING:
    from vonk_control.agent_jobs.service import AgentJobService


def _attempt_diagnostics(
    attempt: AgentOperationAttempt | None,
) -> FailureDiagnostics | None:
    diagnostics = (
        None if attempt is None else column_field(attempt, "result", "diagnostics")
    )
    return diagnostics if isinstance(diagnostics, FailureDiagnostics) else None


def _aggregate_parent(
    self: AgentJobService, session: Session, parent_job_id: str
) -> None:
    """Aggregate a parent from its orders, then project a one-shot job's state.

    Every change of an order reaches here, so the artifact job (a projection of
    its order) is updated by the same transaction that changed the order.
    """

    self._aggregate_parent_state(session, parent_job_id)
    parent = session.get(Job, parent_job_id)
    if parent is not None and parent.kind == FailureStage.ARTIFACT_DISTRIBUTION:
        projection = project_child(session, parent, self._clock())
        parent.result = serialize_json_value(projection.result)
    if parent is not None and is_artifact_owned(parent):
        orders = session.scalars(
            select(StoredOperation).where(
                StoredOperation.parent_job_id == parent_job_id
            )
        )
        adapter = ArtifactJobAdapter(session, clock=self._clock)
        for order in orders:
            adapter.project_for_order(order, self._clock())
        # The parent's end follows the artifact job just projected.
        self._aggregate_parent_state(session, parent_job_id)


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
    verdict, owned_reason = job_run_verdict(session, job, operations, verdict)
    if verdict == "queued":
        set_parent_state(job, "queued", retrying[0].status_reason, self._clock())
        return
    if verdict is None:
        return
    # A parent that already ended keeps its recorded state and reason; one
    # ended without an outcome reason, or carrying only a claim note (a wait
    # diagnostic, never an outcome), still receives its failed order's evidence.
    ended = not _is_refusal_reason(job.status_reason) and job_states.means(
        job.state,
        LifecycleState.SUCCEEDED,
        LifecycleState.FAILED,
        LifecycleState.CANCELLED,
    )
    state = verdict
    set_parent_state(
        job, state, owned_reason, self._clock(), keep_reason=owned_reason is None
    )
    if state == "succeeded":
        if not has_deferred and not ended:
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
    if ended or owned_reason is not None:
        return
    # The parent's stored state: the verdict, or the end it already recorded.
    ended_with_parent = [
        (
            operation,
            session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
            ),
        )
        for operation in operations
        if operation.state == job.state
    ]
    # A rank the platform killed decides the reason; its peers' exits after
    # losing it are consequences (one rule: ``decisive_first``).
    for operation, attempt in decisive_first(
        ended_with_parent, lambda order: _attempt_diagnostics(order[1])
    ):
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
                                None
                                if node is None
                                else runtime_value(
                                    node, lambda identity: identity.semantic_version
                                )
                            ),
                            observed_binary_digest=(
                                None
                                if node is None
                                else runtime_value(
                                    node, lambda identity: identity.binary_digest
                                )
                            ),
                            observed_build_digest=(
                                None
                                if node is None
                                else runtime_value(
                                    node, lambda identity: identity.build_digest
                                )
                            ),
                            raw_reason=reason,
                            retry_queued=_retry_authorized_for_current_attempt(
                                operation
                            ),
                        )
                job.status_reason = redact_text(reason)[:1024]
                return
    job.status_reason = None
