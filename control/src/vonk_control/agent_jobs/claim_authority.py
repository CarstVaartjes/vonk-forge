"""Claim authority for the node-scoped agent queue."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentOperation,
    AgentResult,
    FailureCode,
    LifecycleState,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import AgentFailureResult

from .. import agent_operation_states, job_states
from ..lifecycle import Outcome, Reported
from ..lifecycle.agent_operation import AgentOperationAdapter, set_parent_state
from ..lifecycle.evidence import BookkeepingReason, retire_as_unknown
from ..models import AgentNode, AgentOperationAttempt, Job, RecipeBuild
from ..models import AgentOperation as StoredOperation
from ..recipe_builds import BUILD_ARTIFACT_FORMAT
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_build_policy,
)
from .contracts import _TERMINAL_PARENT_STATES, _WORKLOAD_INTENT_OPERATIONS
from .endings import end_unobserved_order
from .evidence import _failure_result
from .retry import _retry_authorized_for_current_attempt, schedule_agent_upgrade_retry
from .stored import column_field, column_is_document, column_value

if TYPE_CHECKING:
    from .service import AgentJobService


def _reconcile_agent_upgrade(
    self: AgentJobService,
    session: Session,
    node_id: str,
    certificate_serial: str,
    now: datetime,
    runtime_identity: AgentRuntimeIdentity,
    *,
    operation_id: str | None,
    parent_job_id: str | None,
) -> None:
    if operation_id is None:
        return
    operation = session.scalar(
        select(StoredOperation)
        .where(
            StoredOperation.id == operation_id,
            StoredOperation.parent_job_id == parent_job_id,
            StoredOperation.node_id == node_id,
            StoredOperation.kind == AgentOperation.AGENT_UPGRADE.value,
            StoredOperation.state.in_(agent_operation_states.LIVE),
        )
        .order_by(StoredOperation.created_at, StoredOperation.id)
        .with_for_update(of=StoredOperation)
        .execution_options(populate_existing=True)
        .limit(1)
    )
    from vonk_agent_protocol.contracts import AgentUpgradePayload

    from ..package_activation import matches_receipt

    receipt = runtime_identity.package_activation
    if operation is None or operation.current_attempt == 0 or receipt is None:
        return
    payload = column_value(operation, "payload")
    if not isinstance(payload, AgentUpgradePayload):
        end_unobserved_order(
            self,
            session,
            operation,
            AgentOperationAdapter.attempt_of(session, operation),
            session.get(Job, operation.parent_job_id),
            now,
            reason=WaitReason.JOB_STATE_UNCERTAIN,
            note="stored upgrade payload is damaged",
        )
        return
    if not matches_receipt(receipt, payload, node_id):
        return
    if receipt.phase in {"rolled_back", "rollback_failed"}:
        if (
            receipt.phase == "rolled_back"
            and runtime_identity.binary_digest != payload.rollback.source.binary_sha256
        ):
            return
        current = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        # The same terminal receipt is reported until the next install.
        # It must not revoke an operator-authorized retry of that outcome.
        if (
            _retry_authorized_for_current_attempt(operation)
            and current is not None
            and column_is_document(current, "result")
            and column_field(current, "result", "package_activation") == receipt
        ):
            return
        if current is not None:
            AgentOperationAdapter.record_report(
                current,
                "failed",
                AgentFailureResult(
                    reason="agent package " + receipt.phase,
                    package_activation=receipt,
                ),
            )
        # The helper restored (or tried to restore) the rollback source.
        # Retry this Spark behind the dpkg safety fence while the rollout
        # continues with the next one.
        schedule_agent_upgrade_retry(operation, current, now)
        operation.status_reason = (
            f"Spark package {receipt.phase}; {operation.status_reason}"
        )[:512]
        self._aggregate_parent(session, operation.parent_job_id)
        return
    if receipt.phase != "acknowledged" or (
        runtime_identity.build_digest
        != column_field(operation, "payload", "target_build_digest")
        or runtime_identity.binary_digest
        != column_field(operation, "payload", "target_binary_digest")
        or runtime_identity.architecture
        != column_field(operation, "payload", "architecture")
    ):
        return
    evidence = {
        "architecture": runtime_identity.architecture,
        "binary_digest": runtime_identity.binary_digest,
        "build_digest": runtime_identity.build_digest,
        "package_sha256": column_field(operation, "payload", "package_sha256"),
        "package_version": column_field(operation, "payload", "package_version"),
        "status": "upgraded",
        "activation_receipt": receipt.model_dump(mode="json"),
    }
    attempt = session.scalar(
        select(AgentOperationAttempt)
        .where(
            AgentOperationAttempt.operation_id == operation.id,
            AgentOperationAttempt.attempt == operation.current_attempt,
        )
        .with_for_update(of=AgentOperationAttempt)
    )
    if attempt is None or attempt.state not in {
        "running",
        "failed",
        *agent_operation_states.ATTEMPT_OBSERVING,
    }:
        return
    message = AgentResult.model_validate(
        {"fence": attempt.fence, "state": "succeeded", "result": evidence}
    )
    # Preserve explicit helper failures as truthful attempt audit. Exact
    # contact reconciles the operation projection, not the historical fact
    # that the signed helper attempt returned failure.
    if attempt.state != "failed":
        AgentOperationAdapter.record_report(attempt, "succeeded", message.result)
    AgentOperationAdapter(session).settle(
        operation, attempt, None, Reported(Outcome.DONE), now
    )
    if self._result_consumer is not None:
        self._result_consumer(session, operation, attempt, message)
    self._aggregate_parent(session, operation.parent_job_id)


def _recipe_build_runtime_matches(
    session: Session,
    operation: StoredOperation,
    runtime_identity: AgentRuntimeIdentity,
) -> bool:
    build_id = column_field(operation, "payload", "build_id")
    build = session.get(RecipeBuild, build_id) if isinstance(build_id, str) else None
    try:
        report = parse_stored_build_policy(build.policy_report) if build else None
    except RecipeExecutionContractError:
        return False
    return bool(
        build is not None
        and build.builder_node_id == operation.node_id
        and report is not None
        and report.builder_binary_digest == runtime_identity.binary_digest
        and report.artifact_format == BUILD_ARTIFACT_FORMAT
    )


def _reject_recipe_build_claim(
    self: AgentJobService,
    session: Session,
    operation: StoredOperation,
    certificate_serial: str,
    now: datetime,
) -> None:
    reason = _failure_result(
        FailureCode.RECIPE_BUILD_FAILED.value,
        "builder runtime identity changed before claim",
        uncertain=False,
    )
    fence = str(uuid.uuid4())
    attempt = AgentOperationAdapter(session).reject_attempt(
        operation, certificate_serial, fence, now, result=reason
    )
    session.flush()
    if self._result_consumer is not None:
        self._result_consumer(
            session,
            operation,
            attempt,
            AgentResult.model_validate_json(
                canonical_message({"fence": fence, "state": "failed", "result": reason})
            ),
        )
    self._aggregate_parent(session, operation.parent_job_id)


def _claim_has_authority(
    self: AgentJobService,
    session: Session,
    operation: StoredOperation,
    now: datetime,
    *,
    node: AgentNode,
    locked_targets: tuple[str, ...],
) -> bool:
    job = session.scalar(
        select(Job).where(Job.id == operation.parent_job_id).with_for_update(of=Job)
    )
    if job is None:
        # An order whose parent job is gone has no authority to be claimed;
        # it is recorded and the Spark is simply offered no work for it.
        retire_as_unknown(
            "agent-job.claim",
            operation.id,
            BookkeepingReason.ROW_INCOMPLETE,
            "the agent operation lacks its parent job",
        )
        return False
    if self._target_scope(column_value(job, "targets")) != locked_targets:
        return False
    current_operation = session.scalar(
        select(StoredOperation)
        .where(StoredOperation.id == operation.id)
        .with_for_update(of=StoredOperation)
    )
    if current_operation is None:
        return False
    if (
        current_operation.node_id != node.node_id
        or current_operation.node_id not in column_value(job, "targets")
        or current_operation.authority_revision != job.authority_revision
    ):
        self._record_claim_refusal(
            session,
            operation=current_operation,
            job_id=job.id,
            check="operation-authority-stale",
            kind=current_operation.kind,
        )
        return False
    if (
        (
            current_operation.kind in _WORKLOAD_INTENT_OPERATIONS
            and current_operation.workload_intent_ordinal is None
        )
        or (
            current_operation.kind in _WORKLOAD_INTENT_OPERATIONS
            and current_operation.workload_intent_ordinal
            != column_field(job, "payload", "workload_intent_ordinal")
        )
        or (
            current_operation.workload_intent_ordinal is not None
            and current_operation.workload_intent_ordinal
            != node.workload_intent_ordinal
        )
    ):
        self._record_claim_refusal(
            session,
            operation=current_operation,
            job_id=job.id,
            check="workload-intent-mismatch",
            kind=current_operation.kind,
            operation_intent=current_operation.workload_intent_ordinal,
            parent_intent=column_field(job, "payload", "workload_intent_ordinal"),
            node_intent=node.workload_intent_ordinal,
        )
        return False
    if (
        column_is_document(job, "result")
        and column_field(job, "result", "cancel_requested") is True
    ):
        self._record_claim_refusal(
            session,
            operation=current_operation,
            job_id=job.id,
            check="parent-cancel-requested",
            kind=current_operation.kind,
        )
        return False
    if node.state != "active" or node.revoked_at is not None:
        self._record_claim_refusal(
            session,
            operation=current_operation,
            job_id=job.id,
            check="node-not-eligible",
            kind=current_operation.kind,
            node_state=node.state,
        )
        return False
    if (
        job.state in job_states.words(LifecycleState.NEEDS_OPERATOR)
        and current_operation.state in agent_operation_states.PARKED
        and _retry_authorized_for_current_attempt(current_operation)
    ):
        set_parent_state(job, "queued", None, now)
        return True
    if job.state in _TERMINAL_PARENT_STATES:
        self._record_claim_refusal(
            session,
            operation=current_operation,
            job_id=job.id,
            check="parent-not-claimable",
            kind=current_operation.kind,
            parent_state=job.state,
        )
        return False
    return True
