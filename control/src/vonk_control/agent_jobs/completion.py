"""Completion for the node-scoped agent queue."""

from __future__ import annotations

from datetime import datetime, timedelta

from vonk_agent_protocol import (
    AgentOperation,
    AgentResultState,
    FailureStage,
    OutcomeDone,
    OutcomeFailed,
    OutcomeUnknown,
    outcome_state,
)
from vonk_agent_protocol.contracts import AgentResultPayload

from ..agent_operation_facts import aware as _aware
from ..agent_operation_facts import (
    operation_start_deadline as _operation_start_deadline,
)
from ..lifecycle import Effect, Outcome, Reported
from ..lifecycle.agent_operation import cancel_requested_at
from ..models import AgentOperation as StoredOperation
from ..models import AgentOperationAttempt, Job
from .retry import _safe_retry_failure


def _report_event(
    operation: StoredOperation,
    attempt: AgentOperationAttempt,
    parent: Job | None,
    outcome: OutcomeDone | OutcomeFailed | OutcomeUnknown,
    result: AgentResultPayload,
    now: datetime,
) -> Reported:
    """What an agent's report tells the lifecycle core.

    An ``unknown`` outcome (the legacy ``waiting-for-operator``) is the agent
    saying "I could not confirm the effect", whatever failure kind (or none)
    its body carries: it is an uncertain effect, which the core retries for
    restart-safe work (after the exact-resume inspection) and observes for
    the rest.  A cancel the parent asked for, and a spent start budget, end
    a retry before it starts.
    """

    fence = attempt.fence
    state = outcome_state(outcome)
    if isinstance(outcome, OutcomeDone):
        return Reported(Outcome.DONE, fence=fence)
    if state is AgentResultState.CANCELLED:
        return Reported(Outcome.CANCELLED, fence=fence)
    raw_reason = result.get("reason")
    reason = (
        f"agent reported: {raw_reason[:200]}"
        if isinstance(raw_reason, str) and raw_reason
        else None
    )
    start_deadline = _operation_start_deadline(operation)
    if (
        operation.kind == AgentOperation.RECIPE_START.value
        and start_deadline is not None
        and _aware(now) >= _aware(start_deadline)
    ):
        # A spent start budget is final: a retry could only be refused.
        return Reported(Outcome.FAILED, fence=fence, retryable=False, reason=reason)
    if isinstance(outcome, OutcomeUnknown) and not _safe_retry_failure(
        operation.kind, AgentResultState.OBSERVING, result
    ):
        return Reported(Outcome.FAILED, fence=fence, retryable=False, reason=reason)
    retry_after = result.get("retry_after_seconds")
    due = (
        _aware(now) + timedelta(seconds=retry_after)
        if type(retry_after) is int
        else None
    )
    if isinstance(outcome, OutcomeFailed):
        requested, _ = cancel_requested_at(parent, now)
        return Reported(
            Outcome.FAILED,
            fence=fence,
            retryable=requested is None
            and _safe_retry_failure(operation.kind, state, result),
            effect=(
                Effect.NONE
                if operation.kind == AgentOperation.RECIPE_JOB_RUN.value
                and result.get("stage") == FailureStage.MODEL_MATERIALIZATION.value
                and _safe_retry_failure(operation.kind, state, result)
                else None
            ),
            retry_after=due,
            reason=reason,
        )
    return Reported(Outcome.UNKNOWN, fence=fence, retry_after=due, reason=reason)
