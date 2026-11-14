"""The stored states of an artifact job, spoken through the contract.

An artifact job has a *preparation* stage (``draft`` while its inputs upload,
``ready`` once complete) and, once submitted, a lifecycle *state* of the core
vocabulary.  Before submit the state is ``NULL``.  A cancel is the monotonic
``cancel_requested_at``; it does not change the state.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
from vonk_agent_protocol import (
    ArtifactPreparation,
    LifecycleState,
    LifecycleSubject,
    adopt_state,
    live_words,
)

SUBJECT = LifecycleSubject.ARTIFACT_JOB

QUEUED = LifecycleState.QUEUED.value
RUNNING = LifecycleState.RUNNING.value
OBSERVING = LifecycleState.OBSERVING.value
NEEDS_OPERATOR = LifecycleState.NEEDS_OPERATOR.value
SUCCEEDED = LifecycleState.SUCCEEDED.value
FAILED = LifecycleState.FAILED.value
CANCELLED = LifecycleState.CANCELLED.value
BACKOFF = LifecycleState.BACKOFF.value
DRAFT = ArtifactPreparation.DRAFT.value
READY = ArtifactPreparation.READY.value

#: A submitted job that has not ended.
LIVE: tuple[str, ...] = live_words(SUBJECT)
#: A job that has ended.
ENDED: tuple[str, ...] = (SUCCEEDED, FAILED, CANCELLED)
#: Queued or running: the job is accepting bytes and has not been cancelled by an owner.
QUEUED_OR_RUNNING: tuple[str, ...] = (QUEUED, RUNNING)
#: Waiting for its order's next claim: never issued, or put back for reissue.
AWAITING_CLAIM: tuple[str, ...] = (QUEUED, BACKOFF)


def preparation_of(job: Any) -> str | None:
    """``draft`` or ``ready`` while the job is being prepared, else ``None``."""

    return str(job.preparation) if job.preparation else None


def state_of(job: Any) -> str | None:
    """The job's lifecycle state in the core vocabulary; ``None`` while preparing."""

    if job.state is None:
        return None
    adopted = adopt_state(SUBJECT, job.state)
    return adopted.value if adopted is not None else str(job.state)


def sql_preparing_or_live(model: Any) -> Any:
    """SQL: a job being prepared, or submitted and not yet ended."""

    return or_(model.preparation.is_not(None), model.state.in_(LIVE))


def is_live(job: Any) -> bool:
    return state_of(job) in set(LIVE)


def is_ended(job: Any) -> bool:
    return state_of(job) in ENDED
