"""Adopt exact content/preparation receipts without issuing another effect."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentOperation,
    AgentResult,
    AgentResultState,
    ArtifactDistributionResult,
    FailureStage,
    LifecycleState,
    validate_result_for_operation,
)
from vonk_agent_protocol.contracts import AgentResultPayload
from vonk_agent_protocol.state_machines import NodeIdentityState

from .. import agent_operation_states as aos
from ..lifecycle import Outcome, Reported
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..models import AgentNode, AgentOperationAttempt, Job
from ..models import AgentOperation as StoredOperation
from .contracts import _ENDED_PARENT_STATES, _WORKLOAD_INTENT_OPERATIONS
from .stored import column_field, column_value

if TYPE_CHECKING:
    from .service import AgentJobService


CONTENT_KINDS = frozenset(
    {
        AgentOperation.RUNTIME_PREFLIGHT,
        AgentOperation.ARTIFACT_DISTRIBUTION,
        AgentOperation.RECIPE_BUILD,
        AgentOperation.RECIPE_BUILD_CLEANUP,
        AgentOperation.RECIPE_INSTALL,
        AgentOperation.RECIPE_RECONCILE,
        AgentOperation.RECIPE_UNINSTALL,
    }
)


def retained_preparation_receipt(
    operation: StoredOperation, attempt: AgentOperationAttempt | None
) -> AgentResultPayload | None:
    """Read the exact late success the old ingress retained after lease lapse.

    Its body was authenticated and validated at ingress. Its original attempt
    remained lapsed, so consume the receipt's own canonical contract rather
    than interpreting it as a failure under that lapsed state.
    """
    if (
        operation.kind not in CONTENT_KINDS
        or attempt is None
        or attempt.attempt != operation.current_attempt
        or not aos.attempt_lapsed(attempt)
        or attempt.result is None
    ):
        return None
    try:
        return cast(
            AgentResultPayload,
            validate_result_for_operation(
                operation.kind, attempt.result, state=AgentResultState.SUCCEEDED
            ),
        )
    except (TypeError, ValueError):
        return None


def adopt_preparation_receipt(
    service: AgentJobService,
    session: Session,
    operation: StoredOperation,
    attempt: AgentOperationAttempt,
    parent: Job,
    node: AgentNode,
    receipt: AgentResultPayload,
    now: datetime,
) -> bool:
    """One decision for live ingress and worker replay under the queue's locks."""
    from ..job_documents import DistributionJobPayload

    distribution = column_value(parent, "payload")
    assignment = (
        distribution.assignments.get(node.node_id)
        if isinstance(distribution, DistributionJobPayload)
        else None
    )
    if not (
        operation.kind in CONTENT_KINDS
        and node.state == NodeIdentityState.ACTIVE
        and node.revoked_at is None
        and operation.node_id == node.node_id
        and node.node_id
        in (service._target_scope(column_value(parent, "targets")) or ())
        and operation.authority_revision == parent.authority_revision
        and isinstance(column_value(operation, "payload"), BaseModel)
        and (
            operation.kind != AgentOperation.ARTIFACT_DISTRIBUTION
            or (
                parent.kind == FailureStage.ARTIFACT_DISTRIBUTION
                and assignment is not None
                and assignment.node_id == node.node_id
                and assignment.plan_digest == operation.authority_revision
                and isinstance(receipt, ArtifactDistributionResult)
                and receipt.downloaded_bytes
                == sum(
                    {item.sha256: item.bytes for item in assignment.objects}.values()
                )
            )
        )
        and operation.current_attempt == attempt.attempt
        and attempt.operation_id == operation.id
        and operation.state in aos.RUNNING_OR_PARKED
        and parent.state not in _ENDED_PARENT_STATES
        and column_field(parent, "result", "cancel_requested") is not True
        and (
            operation.kind not in _WORKLOAD_INTENT_OPERATIONS
            or (
                operation.workload_intent_ordinal == node.workload_intent_ordinal
                and operation.workload_intent_ordinal
                == column_field(parent, "payload", "workload_intent_ordinal")
            )
        )
        and (
            attempt.state == LifecycleState.RUNNING or aos.attempt_is_observing(attempt)
        )
        and (
            attempt.result is None
            or aos.attempt_reported_unknown(attempt)
            or retained_preparation_receipt(operation, attempt) == receipt
        )
    ):
        return False
    try:
        validated = validate_result_for_operation(
            operation.kind, receipt, state=AgentResultState.SUCCEEDED
        )
    except (TypeError, ValueError):
        return False
    if validated != receipt:
        return False
    # Re-observing a stored authenticated receipt is not agent contact and
    # cannot renew a lease, extend a grant or revive newer/cancelled intent.
    adapter = AgentOperationAdapter(session)
    adapter.record_report(attempt, AgentResultState.SUCCEEDED, receipt)
    adapter.settle(
        operation,
        attempt,
        parent,
        Reported(Outcome.DONE, fence=attempt.fence),
        now,
    )
    if service._result_consumer is not None:
        service._result_consumer(
            session,
            operation,
            attempt,
            AgentResult(
                fence=attempt.fence, state=AgentResultState.SUCCEEDED, result=receipt
            ),
        )
    service._aggregate_parent(session, operation.parent_job_id)
    return True
