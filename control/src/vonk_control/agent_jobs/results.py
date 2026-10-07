"""Results for the node-scoped agent queue."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from vonk_agent_protocol import (
    AgentOperation,
    AgentResult,
    FailureCode,
    InvalidRequestReason,
    SecurityRefusalReason,
    UnknownOutcomeError,
    canonical_message,
    validate_result_for_operation,
)
from vonk_agent_protocol.contracts import AgentResultPayload
from vonk_agent_protocol.recipe_jobs import RecipeJobRunResult

from .. import agent_operation_states
from ..admission_locking import admission_attempts
from ..agent_operation_facts import aware as _aware
from ..agent_outcome import stored_report
from ..auth import AgentSource
from ..lifecycle import CancelRequested, LeaseLapsed, Outcome, Reported
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..models import AgentOperation as StoredOperation
from ..models import AgentOperationAttempt, Job
from ..operation_contract import sanitize_failure_evidence
from .contracts import (
    _ABANDONABLE_OPERATIONS,
    _ENDED_PARENT_STATES,
    _WORKLOAD_INTENT_OPERATIONS,
    AgentFence,
    StaleAgentFence,
    StaleAgentRequest,
)
from .evidence import _failure_result, _lease_expiry_reason
from .predicates import _document
from .retirement import operator_resume_candidates_in_session
from .retry import superseded_cancellation_deadline
from .stored import column_field, column_is_document, column_message, column_value

if TYPE_CHECKING:
    from .service import AgentJobService


def succeed(
    self: AgentJobService, fence: AgentFence, result: AgentResultPayload
) -> None:
    refused: UnknownOutcomeError | None = None
    for _attempt in admission_attempts():
        try:
            self._finish(fence, "succeeded", result=result, reason=None)
            return
        except UnknownOutcomeError as error:
            refused = error
    assert refused is not None
    raise refused


def fail(self: AgentJobService, fence: AgentFence, reason: str) -> None:
    result = _failure_result(
        FailureCode.OPERATION_FAILED.value, reason, uncertain=False
    )
    refused: UnknownOutcomeError | None = None
    for _attempt in admission_attempts():
        try:
            self._finish(fence, "failed", result=result, reason=None)
            return
        except UnknownOutcomeError as error:
            refused = error
    assert refused is not None
    raise refused


def record_result(
    self: AgentJobService, message: AgentResult, *, source: AgentSource | None = None
) -> None:
    """Persist one exact agent result and consume it in the same transaction."""
    self._finish(
        message,
        message.state,
        result=message.result,
        reason=None,
        source=source,
    )


def record_late_result(
    self: AgentJobService, message: AgentResult, *, source: AgentSource | None = None
) -> bool:
    """Retain stale effects; close only a proved cancellation of the old order.

    Only the authenticated node may submit the exact historical fence.
    Certificate rotation may change its current TLS credential while the
    durable receipt still names the original certificate. Old success and
    failure stay diagnostic. A typed cancellation acknowledgement may
    retire only the exact superseded order after the agent confirms its
    host action has ceased; it never applies that order's desired effect.
    """
    self._mark_started()
    with self._sessions.begin() as session:
        hint = session.execute(
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
            .where(AgentOperationAttempt.fence == message.fence)
        ).one_or_none()
        if hint is None:
            raise StaleAgentFence(
                "agent operation lease, certificate, or fence is stale",
                reason=SecurityRefusalReason.STALE_FENCE,
            )
        operation_id, node_id, serial, parent_job_id = hint
        scopes = self._lock_operation_scopes(session, (operation_id,), node_id)
        if scopes is None or scopes[operation_id][0] != parent_job_id:
            raise StaleAgentFence(
                "agent operation authority is stale",
                reason=SecurityRefusalReason.STALE_FENCE,
            )
        # Certificate rotation replaces the TLS identity while retaining
        # the node's durable ledger. Authenticate current contact under
        # its fresh certificate; the expired attempt remains bound to the
        # original serial and fence for diagnostics only.
        contact_serial = (
            source.identity.certificate_serial if source is not None else serial
        )
        identity = self._lock_identity(session, node_id, contact_serial)
        now = self._clock()
        if identity is None or not self._identity_is_active(*identity, now):
            raise StaleAgentFence(
                "agent certificate is no longer active",
                reason=SecurityRefusalReason.STALE_FENCE,
            )
        node, certificate = identity
        self._consume_contact(session, source, node, certificate)
        parent = session.get(Job, parent_job_id, with_for_update=True)
        operation = session.get(StoredOperation, operation_id, with_for_update=True)
        attempt = session.scalar(
            select(AgentOperationAttempt)
            .where(AgentOperationAttempt.fence == message.fence)
            .with_for_update(of=AgentOperationAttempt)
        )
        if (
            parent is None
            or operation is None
            or attempt is None
            or node.state != "active"
            or node.revoked_at is not None
            or self._target_scope(column_value(parent, "targets"))
            != scopes[operation_id][1]
            or operation.authority_revision != parent.authority_revision
            or attempt.operation_id != operation.id
            or attempt.agent_certificate_serial != serial
        ):
            raise StaleAgentFence(
                "agent operation authority or expired attempt is stale",
                reason=SecurityRefusalReason.STALE_FENCE,
            )
        message, _outcome = stored_report(operation.kind, message)
        validate_result_for_operation(
            operation.kind, message.result, state=message.state
        )
        evidence = _document(message.result)
        if message.state in {"failed", agent_operation_states.WIRE_UNKNOWN}:
            evidence = sanitize_failure_evidence(evidence)
        if agent_operation_states.attempt_wire_state(
            attempt
        ) == message.state and column_message(attempt, "result") == canonical_message(
            evidence
        ):
            return False
        superseded_intent = (
            operation.workload_intent_ordinal is not None
            and operation.workload_intent_ordinal != node.workload_intent_ordinal
        )
        if (
            message.state == "cancelled"
            and superseded_intent
            and operation.kind in _WORKLOAD_INTENT_OPERATIONS
            and operation.workload_intent_ordinal
            == column_field(parent, "payload", "workload_intent_ordinal")
            and column_is_document(parent, "result")
            and superseded_cancellation_deadline(column_value(parent, "result"))
            is not None
            and operation.current_attempt == attempt.attempt
            and operation.state in agent_operation_states.RUNNING_OR_PARKED
            and (
                attempt.state == "running"
                or agent_operation_states.attempt_lapsed(attempt)
            )
            and column_value(attempt, "result") is None
            and (
                (
                    operation.kind == AgentOperation.RECIPE_JOB_RUN.value
                    and isinstance(message.result, RecipeJobRunResult)
                    and message.result.exit_code == 130
                    and not message.result.outputs
                )
                or (
                    operation.kind != AgentOperation.RECIPE_JOB_RUN.value
                    and evidence.get("error_code")
                    == FailureCode.OPERATION_CANCELLED.value
                    and evidence.get("uncertain") is not True
                )
            )
        ):
            adapter = AgentOperationAdapter(session)
            adapter.record_report(attempt, "cancelled", message.result)
            # The agent confirmed its host action has ceased: a definite end.
            adapter.settle(
                operation,
                attempt,
                parent,
                Reported(Outcome.CANCELLED, reason="cancellation confirmed"),
                now,
            )
            if self._result_consumer is not None:
                self._result_consumer(session, operation, attempt, message)
            self._aggregate_parent(session, operation.parent_job_id)
            return True
        if attempt.state == "running" and (
            _aware(attempt.lease_deadline) <= _aware(now) or superseded_intent
        ):
            AgentOperationAdapter.expire_attempt(attempt)
            if (
                operation.current_attempt == attempt.attempt
                and operation.state == "running"
            ):
                lapse_reason = _lease_expiry_reason(operation, attempt, node, now)
                adapter = AgentOperationAdapter(
                    session, resume_candidates=operator_resume_candidates_in_session
                )
                if parent.state in _ENDED_PARENT_STATES:
                    # The job already ended, so nothing can claim or resume
                    # this order: it ends with it instead of waiting.
                    adapter.settle(
                        operation,
                        attempt,
                        parent,
                        CancelRequested(
                            reason=(
                                f"job {parent.id} ended without this operation; "
                                "its retry was abandoned"
                                if operation.kind in _ABANDONABLE_OPERATIONS
                                else f"{lapse_reason}; its job already ended"
                            )
                        ),
                        now,
                    )
                else:
                    # An interrupted exact-resume order reconciles its own
                    # effect on the next attempt: the core schedules it here,
                    # as the claim and sweep paths do, instead of waiting
                    # for an operator.
                    adapter.settle(
                        operation,
                        attempt,
                        parent,
                        LeaseLapsed(reason=lapse_reason),
                        now,
                    )
                if parent.state in {"queued", "running"}:
                    self._aggregate_parent(session, operation.parent_job_id)
        if not agent_operation_states.attempt_lapsed(attempt):
            raise StaleAgentRequest(
                "agent operation attempt is not expired",
                reason=InvalidRequestReason.NOT_READY,
            )
        if column_value(attempt, "result") is not None:
            if column_message(attempt, "result") != canonical_message(evidence):
                raise StaleAgentRequest(
                    "expired attempt evidence changed",
                    reason=InvalidRequestReason.CONFLICT,
                )
            return True
        attempt.result = evidence
        return True
