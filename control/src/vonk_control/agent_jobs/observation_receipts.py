"""Adopt exact observed receipts without issuing another runtime effect."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentFailureKind,
    AgentFailureResult,
    AgentOperation,
    AgentResult,
    AgentResultState,
    ArtifactDistributionResult,
    FailureStage,
    LifecycleState,
    canonical_message,
    validate_result_for_operation,
)
from vonk_agent_protocol.contracts import (
    AgentResultPayload,
    AgentUpgradePayload,
    AgentUpgradeResult,
)
from vonk_agent_protocol.package_upgrade import PackageActivationPhase
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest, RecipeJobRunResult
from vonk_agent_protocol.state_machines import NodeIdentityState

from .. import agent_operation_states as aos
from ..agent_operation_facts import aware, job_output_retention_expiry
from ..agent_outcome import agent_outcome
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..models import AgentNode, AgentOperationAttempt, Job
from ..models import AgentOperation as StoredOperation
from .contracts import _ENDED_PARENT_STATES, _WORKLOAD_INTENT_OPERATIONS
from .stored import column_field, column_value

if TYPE_CHECKING:
    from .service import AgentJobService


RUNTIME_KINDS = frozenset(
    {
        AgentOperation.RECIPE_START,
        AgentOperation.RECIPE_STOP,
        AgentOperation.RECIPE_JOB_RUN,
    }
)

OBSERVATION_KINDS = frozenset(AgentOperation)
_FAILURE_OBSERVATION_KINDS = RUNTIME_KINDS | {AgentOperation.AGENT_UPGRADE}


def receipt_state(kind: str, receipt: object) -> AgentResultState:
    if kind == AgentOperation.RECIPE_JOB_RUN:
        try:
            job = RecipeJobRunResult.model_validate_json(canonical_message(receipt))
        except (TypeError, ValueError):
            pass
        else:
            if job.ending is not None:
                return AgentResultState.OBSERVING
            if job.exit_code != 0:
                return AgentResultState.FAILED
    if kind in _FAILURE_OBSERVATION_KINDS:
        try:
            failure = AgentFailureResult.model_validate_json(canonical_message(receipt))
        except (TypeError, ValueError):
            pass
        else:
            if (
                failure.uncertain is not True
                and failure.failure_kind != AgentFailureKind.UNCERTAIN_EFFECT
            ):
                return AgentResultState.FAILED
    return AgentResultState.SUCCEEDED


def retained_observation_receipt(
    operation: StoredOperation, attempt: AgentOperationAttempt | None
) -> AgentResultPayload | None:
    """Read the exact retained outcome the old ingress retained after lease lapse.

    Its body was authenticated and validated at ingress. Its original attempt
    remained lapsed, so consume the receipt's own canonical contract rather
    than interpreting it as a failure under that lapsed state.
    """
    if (
        operation.kind not in OBSERVATION_KINDS
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
                operation.kind,
                attempt.result,
                state=receipt_state(operation.kind, attempt.result),
            ),
        )
    except (TypeError, ValueError):
        return None


def adopt_observation_receipt(
    service: AgentJobService,
    session: Session,
    operation: StoredOperation,
    attempt: AgentOperationAttempt,
    parent: Job,
    node: AgentNode,
    receipt: AgentResultPayload,
    now: datetime,
    *,
    state: AgentResultState = AgentResultState.SUCCEEDED,
) -> bool:
    """One decision for live ingress and worker replay under the queue's locks."""
    from ..cancelled_runtime import cancellation_stop_current
    from ..job_documents import DistributionJobPayload

    if (
        state != AgentResultState.SUCCEEDED
        and not isinstance(receipt, RecipeJobRunResult)
        and not (
            state == AgentResultState.FAILED
            and operation.kind in _FAILURE_OBSERVATION_KINDS
            and isinstance(receipt, AgentFailureResult)
            and receipt.uncertain is not True
            and receipt.failure_kind != AgentFailureKind.UNCERTAIN_EFFECT
        )
    ):
        return False
    if isinstance(receipt, RecipeJobRunResult):
        request = column_value(operation, "payload")
        if not isinstance(request, RecipeJobRunRequest) or (
            receipt.job_id,
            receipt.run_id,
        ) != (request.job_id, request.run_id):
            return False
    if not cancellation_stop_current(session, parent, operation):
        return False
    if isinstance(receipt, RecipeJobRunResult) and (
        receipt.output_delivery_pending
        and aware(now) >= job_output_retention_expiry(attempt.lease_deadline)
    ):
        return False

    distribution = column_value(parent, "payload")
    assignment = (
        distribution.assignments.get(node.node_id)
        if isinstance(distribution, DistributionJobPayload)
        else None
    )
    if not (
        operation.kind in OBSERVATION_KINDS
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
            or retained_observation_receipt(operation, attempt) == receipt
        )
    ):
        return False
    try:
        validated = validate_result_for_operation(operation.kind, receipt, state=state)
    except (TypeError, ValueError):
        return False
    if validated != receipt:
        return False
    if operation.kind == AgentOperation.AGENT_UPGRADE and not _upgrade_receipt_current(
        node, operation, attempt, parent, receipt, state
    ):
        return False
    # Re-observing a stored authenticated receipt is not agent contact and
    # cannot renew a lease, extend a grant or revive newer/cancelled intent.
    adapter = AgentOperationAdapter(session)
    adapter.record_report(attempt, state, receipt)
    adapter.settle(
        operation,
        attempt,
        parent,
        service._report_event(
            operation,
            attempt,
            parent,
            agent_outcome(operation.kind, state, receipt),
            receipt,
            now,
        ),
        now,
    )
    if service._result_consumer is not None:
        service._result_consumer(
            session,
            operation,
            attempt,
            AgentResult(fence=attempt.fence, state=state, result=receipt),
        )
    service._aggregate_parent(session, operation.parent_job_id)
    return True


def _upgrade_receipt_current(
    node: AgentNode,
    operation: StoredOperation,
    attempt: AgentOperationAttempt,
    parent: Job,
    receipt: AgentResultPayload,
    state: AgentResultState,
) -> bool:
    # Use the upgrade owner's exact contact decision, not a second success
    # classifier. Observation itself never records contact or renews the fence.
    from ..agent_upgrades.dispatch import DispatchMixin
    from ..agent_upgrades.stored import _stored_package
    from ..package_activation import matches_receipt

    if isinstance(receipt, AgentUpgradeResult):
        package = _stored_package(parent)
        return package is not None and DispatchMixin._contact_proves_target(
            node,
            operation,
            package,
            AgentResult(fence=attempt.fence, state=state, result=receipt),
        )
    payload = column_value(operation, "payload")
    return (
        isinstance(receipt, AgentFailureResult)
        and isinstance(payload, AgentUpgradePayload)
        and receipt.package_activation is not None
        and receipt.package_activation.phase == PackageActivationPhase.ROLLED_BACK
        and matches_receipt(receipt.package_activation, payload, node.node_id)
        and node.binary_digest == payload.rollback.source.binary_sha256
    )
