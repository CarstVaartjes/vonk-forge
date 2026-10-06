"""The stored states of an artifact job, spoken through the contract.

An artifact job has a *preparation* stage (``draft`` while its inputs upload,
``ready`` once complete) and, once submitted, a lifecycle *state* of the core
vocabulary.  Before submit the state is ``NULL``.  A cancel is the monotonic
``cancel_requested_at``; it does not change the state.

A row written before the rename kept all of this in one word: ``draft`` and
``ready`` were states, ``cancelling`` was a state, and ``waiting-for-operator`` was
the wait.  The helpers below read either shape, and the startup adoption
(:func:`adopt_legacy_artifact_jobs`) rewrites old rows once.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import Table, func, or_, update
from sqlalchemy.engine import Connection
from vonk_agent_protocol import (
    ArtifactPreparation,
    LifecycleState,
    LifecycleSubject,
    StateAlias,
    adopt_state,
    legacy_preparation,
    live_words,
    stored_words,
)

SUBJECT = LifecycleSubject.ARTIFACT_JOB

QUEUED = LifecycleState.QUEUED.value
RUNNING = LifecycleState.RUNNING.value
OBSERVING = LifecycleState.OBSERVING.value
NEEDS_OPERATOR = LifecycleState.NEEDS_OPERATOR.value
SUCCEEDED = LifecycleState.SUCCEEDED.value
FAILED = LifecycleState.FAILED.value
CANCELLED = LifecycleState.CANCELLED.value
DRAFT = ArtifactPreparation.DRAFT.value
READY = ArtifactPreparation.READY.value

#: A submitted job that has not ended (old spellings included).
LIVE: tuple[str, ...] = live_words(SUBJECT)
#: A job that has ended.
ENDED: tuple[str, ...] = (SUCCEEDED, FAILED, CANCELLED)
#: Queued or running: the job is accepting bytes and has not been cancelled by an owner.
QUEUED_OR_RUNNING: tuple[str, ...] = (QUEUED, RUNNING)
#: A job that waits for a person (either spelling).
WAITING: tuple[str, ...] = stored_words(SUBJECT, (LifecycleState.NEEDS_OPERATOR,))


def preparation_of(job: Any) -> str | None:
    """``draft`` or ``ready`` while the job is being prepared, else ``None``."""

    if job.preparation:
        return str(job.preparation)
    legacy = legacy_preparation(job.state)
    return legacy.value if legacy is not None else None


def state_of(job: Any) -> str | None:
    """The job's lifecycle state in the core vocabulary; ``None`` while preparing."""

    if job.state is None or legacy_preparation(job.state) is not None:
        return None
    adopted = adopt_state(SUBJECT, job.state)
    return adopted.state.value if adopted is not None else str(job.state)


def cancel_requested_at(job: Any) -> datetime | None:
    """When a cancel was requested: the column, or an old ``cancelling`` row's update."""

    if job.cancel_requested_at is not None:
        return job.cancel_requested_at  # type: ignore[no-any-return]
    if job.state == StateAlias.CANCELLING.value:
        return job.updated_at  # type: ignore[no-any-return]
    return None


def sql_preparing_or_live(model: Any) -> Any:
    """SQL: a job being prepared, or submitted and not yet ended (old words too)."""

    return or_(
        model.preparation.is_not(None),
        model.state.in_((*LIVE, DRAFT, READY)),
    )


def is_live(job: Any) -> bool:
    return state_of(job) in set(LIVE)


def is_ended(job: Any) -> bool:
    return state_of(job) in ENDED


def adopt_legacy_artifact_jobs(connection: Connection) -> int:
    """Move rows written before the rename onto the preparation/state/cancel shape.

    Idempotent.  A row it misses is read through the helpers above and rewritten by
    the adapter's next transition, so a gap heals instead of stranding a job.
    """

    from .models import ArtifactJob

    table = cast(Table, ArtifactJob.__table__)
    rewritten = 0
    for stage in ArtifactPreparation:
        result = connection.execute(
            update(table)
            .where(table.c.state == stage.value)
            .values(preparation=stage.value, state=None)
            .execution_options(synchronize_session=False)
        )
        rewritten += int(result.rowcount or 0)
    result = connection.execute(
        update(table)
        .where(table.c.state == StateAlias.CANCELLING.value)
        .values(
            state=OBSERVING,
            cancel_requested_at=func.coalesce(
                table.c.cancel_requested_at, table.c.updated_at
            ),
        )
        .execution_options(synchronize_session=False)
    )
    rewritten += int(result.rowcount or 0)
    adopted = adopt_state(SUBJECT, StateAlias.WAITING_FOR_OPERATOR.value)
    assert adopted is not None
    result = connection.execute(
        update(table)
        .where(table.c.state == StateAlias.WAITING_FOR_OPERATOR.value)
        .values(state=adopted.state.value)
        .execution_options(synchronize_session=False)
    )
    rewritten += int(result.rowcount or 0)
    return rewritten
