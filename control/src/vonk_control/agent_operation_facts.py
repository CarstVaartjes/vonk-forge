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
from vonk_agent_protocol import AgentClaim, AgentOperation

from . import agent_operation_states
from .models import AgentOperation as StoredOperation
from .models import AgentOperationAttempt, Job
from .operation_progress import stored_progress
from .stall_window import last_progress_at, latest_progress, stall_deadline

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


def advanced_bytes(
    operation: StoredOperation, attempt: AgentOperationAttempt | None
) -> bool:
    """Whether ``attempt`` copied more of an artifact than any earlier attempt."""

    session = object_session(operation)
    if (
        session is None
        or attempt is None
        or operation.kind != AgentOperation.ARTIFACT_DISTRIBUTION.value
    ):
        return False
    earlier = session.scalars(
        select(AgentOperationAttempt).where(
            AgentOperationAttempt.operation_id == operation.id,
            AgentOperationAttempt.attempt < attempt.attempt,
        )
    )
    return completed_bytes(attempt) > max(map(completed_bytes, earlier), default=0)


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


#: How long a start may go without reported progress once it has passed its
#: accepted deadline: the agent's own stall window (the claim's observation
#: budget), so both sides end a stalled load at the same point.
START_STALL_WINDOW = timedelta(
    seconds=AgentClaim.model_fields["observation_budget_seconds"].default
)


def progressed_start_deadline(
    accepted: datetime, progressed: datetime | None, now: datetime
) -> datetime:
    """The accepted start deadline, extended by the start's reported progress.

    A start whose load keeps progressing is healthy however long it takes: the
    deadline is a full stall window past its last progress, and never earlier
    than the deadline it was accepted with.
    """

    accepted = aware(accepted)
    return stall_deadline(
        accepted - START_STALL_WINDOW,
        latest_progress(None, progressed, now=now),
        START_STALL_WINDOW,
    )


def start_deadline(
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    now: datetime,
) -> datetime | None:
    """The start deadline of ``operation`` as its current attempt's progress has it.

    Only the current attempt's progress counts: an earlier attempt that
    progressed and then failed cannot hold a later one open.
    """

    accepted = operation_start_deadline(operation)
    if accepted is None:
        return None
    current = (
        attempt
        if attempt is not None and attempt.attempt == operation.current_attempt
        else None
    )
    progressed = None if current is None else last_progress_at(stored_progress(current))
    return progressed_start_deadline(accepted, progressed, now)


def job_start_deadline(job: Job, accepted: datetime, now: datetime) -> datetime:
    """A start job's accepted deadline, extended by its orders' reported progress."""

    session = object_session(job)
    if session is None:
        return aware(accepted)
    attempts = session.scalars(
        select(AgentOperationAttempt)
        .join(
            StoredOperation,
            (AgentOperationAttempt.operation_id == StoredOperation.id)
            & (AgentOperationAttempt.attempt == StoredOperation.current_attempt),
        )
        .where(
            StoredOperation.parent_job_id == job.id,
            StoredOperation.kind == AgentOperation.RECIPE_START.value,
        )
    )
    progressed = [
        moment
        for attempt in attempts
        if (moment := last_progress_at(stored_progress(attempt))) is not None
    ]
    return progressed_start_deadline(accepted, max(progressed, default=None), now)


def lapsed_renewal_allowed(
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    now: datetime,
) -> bool:
    """Return whether a lapsed lease may still be re-acquired by its own fence.

    A lease lapse parks a healthy start when the Controller was briefly
    unreachable, and the operation's own start budget is the clock that says how
    long that start is still legitimate.  Until it is spent (or, once its load
    progresses, until it stalls), the executor that holds the fence may prove it
    is alive again; after it, nothing may.
    """

    deadline = start_deadline(operation, attempt, now)
    return deadline is not None and aware(now) < deadline


def attempt_holds_open_launch_budget(
    operation: StoredOperation,
    attempt: AgentOperationAttempt | None,
    now: datetime,
) -> bool:
    """Return whether this exact attempt still owns an open launch budget.

    Dependency: the attempt is the executor of a start that declared an
    immutable readiness budget.  Owner: the operation's current attempt, still
    running.  Deadline: that budget's instant, which derives from the plan's own
    readiness window and never from the lease being recovered, extended a stall
    window past the attempt's last reported progress.  Resume condition: the
    exact fence submits its outcome, or the budget elapses without progress.

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
        and lapsed_renewal_allowed(operation, attempt, now)
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
