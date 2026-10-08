"""Facts about the Spark order (``AgentOperation``) that more than one module needs.

The kinds that may be re-issued blindly, the dpkg safety fence of an agent
upgrade, the launch budget a start declares, and the crash-loop slowdown of an
interrupted transfer.  They are owned here so that the Controller's queue
(``agent_jobs``) and the lifecycle adapter (``lifecycle.agent_operation``) read
one definition instead of two.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import object_session
from vonk_agent_protocol import AgentOperation

from . import agent_operation_states
from .models import AgentOperation as StoredOperation
from .models import AgentOperationAttempt

LIFECYCLE_RESTART_OPERATIONS = frozenset(
    {
        AgentOperation.RECIPE_INSTALL.value,
        AgentOperation.RECIPE_START.value,
        AgentOperation.RECIPE_STOP.value,
        AgentOperation.RECIPE_UNINSTALL.value,
        AgentOperation.RECIPE_RECONCILE.value,
    }
)


RESTART_REISSUE_OPERATIONS = LIFECYCLE_RESTART_OPERATIONS | frozenset(
    {
        AgentOperation.ARTIFACT_DISTRIBUTION.value,
        AgentOperation.RUNTIME_PREFLIGHT.value,
        AgentOperation.RECIPE_BUILD.value,
        AgentOperation.RECIPE_BUILD_CLEANUP.value,
    }
)


#: Consecutive interrupted transfer attempts that copied no new bytes before
#: the retry rate is relaxed. A transfer that progresses between restarts is
#: healthy recovery and is never slowed; one that dies without progress is a
#: crash loop, which a faster retry cannot help.
STALLED_INTERRUPTION_LIMIT = 3


STALLED_RETRY_BASE_SECONDS = 30


STALLED_RETRY_MAX_SECONDS = 600
#: The fixed authority an old workload fence's cancellation-only STOP is given, and
#: so the budget a cancel of anything built on it (a profile load) is given: it never
#: outlives the order it waits for.
SUPERSEDED_CANCELLATION_SECONDS = 660


INTERRUPTION_CODES = frozenset(
    {"agent_restart_interrupted", "operation_outcome_uncertain"}
)


def completed_bytes(attempt: AgentOperationAttempt | None) -> int:
    value = None if attempt is None else (attempt.progress or {}).get("completed_bytes")
    return value if type(value) is int else 0


def stalled_interruptions(operation: StoredOperation) -> int:
    """Count trailing transfer attempts that ended interrupted without progress."""

    session = object_session(operation)
    if (
        session is None
        or operation.kind != AgentOperation.ARTIFACT_DISTRIBUTION.value
        or operation.current_attempt < 1
    ):
        return 0
    attempts = list(
        session.scalars(
            select(AgentOperationAttempt)
            .where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt <= operation.current_attempt,
            )
            .order_by(AgentOperationAttempt.attempt.desc())
            .limit(STALLED_RETRY_MAX_SECONDS // STALLED_RETRY_BASE_SECONDS + 2)
        )
    )
    stalled = 0
    for index, attempt in enumerate(attempts):
        result = attempt.result
        interrupted = agent_operation_states.attempt_is_observing(attempt) and (
            result is None or result.get("error_code") in INTERRUPTION_CODES
        )
        previous = attempts[index + 1] if index + 1 < len(attempts) else None
        if not interrupted or completed_bytes(attempt) > completed_bytes(previous):
            break
        stalled += 1
    return stalled


#: An ambiguous agent-package install can leave durable apt/dpkg recovery in
#: progress; an automatic re-dispatch never overlaps it.  A stable dispatch
#: contract, not a derivation from package-helper implementation timeouts.
# One request's uncertainty budget, starting at its first failed observation.
# This bounds executor ownership independently from standing desired state.
AGENT_ORDER_RECOVERY_BUDGET = timedelta(hours=1)

AGENT_UPGRADE_RECOVERY_FENCE = timedelta(seconds=960)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def operation_start_deadline(operation: StoredOperation) -> datetime | None:
    """Return the immutable start deadline a two-phase start bound, if any.

    Only a distributed start persists one, and it is the budget the start may not
    outlive.  It is the one clock that can bound a lapsed renewal: the lease is
    the thing being recovered, so it cannot also be the recovery budget.  An
    operation that binds none gets no allowance, because inventing a second clock
    would widen the fence without a fact to bound it.
    """

    payload = operation.payload if isinstance(operation.payload, Mapping) else {}
    value = payload.get("start_deadline")
    if not isinstance(value, str):
        return None
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        return None
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        return None
    return deadline


def lapsed_renewal_allowed(operation: StoredOperation, now: datetime) -> bool:
    """Return whether a lapsed lease may still be re-acquired by its own fence.

    A lease lapse parks a healthy start when the Controller was briefly
    unreachable, and the operation's own start budget is the clock that says how
    long that start is still legitimate.  Until it is spent, the executor that
    holds the fence may prove it is alive again; after it, nothing may.
    """

    deadline = operation_start_deadline(operation)
    return deadline is not None and aware(now) < aware(deadline)


def attempt_holds_open_launch_budget(
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    now: datetime,
) -> bool:
    """Return whether this exact attempt still owns an open launch budget.

    Dependency: the attempt is the executor of a start that declared an
    immutable readiness budget.  Owner: the operation's current attempt, still
    running.  Deadline: that budget's instant, which derives from the plan's own
    readiness window and never from the lease being recovered.  Resume
    condition: the exact fence submits its outcome, or the budget elapses.

    A start may legitimately be silent for the whole budget while its engine
    loads -- the recipe itself declares that window -- so the budget, not the
    lease the agent happened to accept, decides whether the attempt is still
    current.  The ownership facts are named here so every caller agrees on what
    "still current" means: only the operation's own attempt may use the
    allowance, so a newer attempt's takeover is never masked.
    """

    return (
        attempt is not None
        and attempt.attempt == operation.current_attempt
        and attempt.state == "running"
        and lapsed_renewal_allowed(operation, now)
    )


def attempt_is_live(
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    now: datetime,
) -> bool:
    """Return whether an attempt still holds the operation's fence.

    This is the boundary that decides whether another owner may take the
    operation over, and which attempts count as expired, so a missing attempt, an
    attempt that already stopped, or a lease deadline at or before ``now`` is not
    live.  A still-current attempt inside its own launch budget is live as well,
    because the budget is the plan's declared readiness window and the lease
    exists to bound takeover latency, not to end a launch the plan permitted.
    It is deliberately not the renewal boundary: ``_active`` additionally lets
    this exact fence renew inside the operation's own start allowance, which
    restores a healthy attempt without ever letting a second owner in.
    """

    return (
        attempt is not None
        and attempt.state == "running"
        and (
            aware(attempt.lease_deadline) > aware(now)
            or attempt_holds_open_launch_budget(operation, attempt, now)
        )
    )
