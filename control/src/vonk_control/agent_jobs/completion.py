"""Completion for the node-scoped agent queue."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentOperation,
    AgentResultState,
    Disposition,
    OutcomeDone,
    OutcomeFailed,
    OutcomeUnknown,
    outcome_state,
)
from vonk_agent_protocol.contracts import AgentResultPayload

from ..agent_operation_facts import aware as _aware
from ..agent_operation_facts import (
    start_deadline as _start_deadline,
)
from ..evidence_repeats import note_repeat, note_success
from ..lifecycle import Effect, Outcome, Reported
from ..lifecycle.agent_operation import cancel_requested_at
from ..models import AgentOperation as StoredOperation
from ..models import AgentOperationAttempt, Job
from .retry import job_attempt_did_not_launch, result_disposition


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
        else (
            result.get("error_code")
            if isinstance(result.get("error_code"), str)
            else None
        )
    )
    start_deadline = _start_deadline(operation, attempt, now)
    if (
        operation.kind == AgentOperation.RECIPE_START.value
        and start_deadline is not None
        and _aware(now) >= start_deadline
    ):
        # A spent start budget is final: a retry could only be refused.
        return Reported.failed(Disposition.FAIL, fence=fence, reason=reason)
    if isinstance(outcome, OutcomeUnknown):
        verdict = result_disposition(operation.kind, AgentResultState.OBSERVING, result)
        if verdict in {Disposition.FAIL, Disposition.SECURITY}:
            return Reported.failed(verdict, fence=fence, reason=reason)
    retry_after = result.get("retry_after_seconds")
    due = (
        _aware(now) + timedelta(seconds=retry_after)
        if type(retry_after) is int
        else None
    )
    if isinstance(outcome, OutcomeFailed):
        requested, _ = cancel_requested_at(parent, now)
        # A dependency or resource wait keeps the order and spends no retry; a
        # cancel the parent asked for ends it here.
        verdict = result_disposition(operation.kind, state, result)
        return Reported.failed(
            verdict if requested is None else Disposition.FAIL,
            fence=fence,
            effect=(
                Effect.NONE
                if operation.kind == AgentOperation.RECIPE_JOB_RUN.value
                and job_attempt_did_not_launch(result)
                and verdict is Disposition.WAIT
                else None
            ),
            retry_after=due,
            reason=reason,
        )
    return Reported(Outcome.UNKNOWN, fence=fence, retry_after=due, reason=reason)


def note_report(
    session: Session,
    operation: StoredOperation,
    event: Reported,
    result: AgentResultPayload,
    now: datetime,
) -> None:
    """A report is evidence about its kind of work on its Spark.

    A success clears the kind's repetition there.  A failure that ends the
    order or spends a retry (an unconfirmed effect included, by its typed
    wait reason) counts its typed code once more, so every job of a
    kind failing the same way is seen across orders; a wait is not a failure
    and counts nothing.  This only records: the order's fate is ``event``'s.
    """

    if event.outcome is Outcome.DONE:
        note_success(session, operation.kind, operation.node_id)
        return
    if event.outcome is Outcome.CANCELLED or not event.consumes_retry:
        return
    code = (
        result.get("helper_error_code")
        or result.get("error_code")
        or result.get("wait_reason")
    )
    if isinstance(code, str) and code:
        note_repeat(
            session,
            operation.kind,
            operation.node_id,
            code,
            now=now,
            detail=event.reason or f"operation {operation.id}",
        )
