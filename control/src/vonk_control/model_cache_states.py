"""The stored states of a model-cache operation, spoken through the contract.

A row's ``state`` is a word of the core vocabulary (``queued``, ``running``,
``backoff``, ``succeeded``, ``failed``, ``cancelled``).  Every selection by state
goes through the words below, so no module spells one.
"""

from __future__ import annotations

from vonk_agent_protocol import (
    LifecycleState,
    LifecycleSubject,
    adopt_state,
    is_live,
    is_state,
    live_words,
    stored_words,
)

SUBJECT = LifecycleSubject.MODEL_CACHE_OPERATION

#: Interrupted or deferred work that retries by itself.
BACKOFF = LifecycleState.BACKOFF.value
#: Every stored word of an operation that has not ended.
LIVE: tuple[str, ...] = live_words(SUBJECT)
#: An operation a new consumer may attach to or wait on: not ended, and not being
#: cancelled.  A stored row never says ``observing``; the *view* of one whose
#: cancellation is being driven does, and such an operation is not joined.
ACTIVE: tuple[str, ...] = stored_words(
    SUBJECT,
    (LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF),
)
#: Operations waiting to be (re)started: never started, or interrupted.
WAITING: tuple[str, ...] = stored_words(
    SUBJECT, (LifecycleState.QUEUED, LifecycleState.BACKOFF)
)
#: Operations that are waiting or have failed (a failure can be reopened).
WAITING_OR_FAILED: tuple[str, ...] = stored_words(
    SUBJECT, (LifecycleState.QUEUED, LifecycleState.BACKOFF, LifecycleState.FAILED)
)


def words(*states: LifecycleState) -> tuple[str, ...]:
    """Every stored word that means one of ``states``."""

    return stored_words(SUBJECT, states)


def operation_is_live(stored: str | None) -> bool:
    """Whether a stored word is an operation that has not ended."""

    return is_live(SUBJECT, stored)


def operation_is_backoff(stored: str | None) -> bool:
    """Whether a stored word is interrupted work waiting to retry."""

    return is_state(SUBJECT, stored, LifecycleState.BACKOFF)


def adopted(stored: str) -> str:
    """The word a stored state is shown as: its core word, or the stored word."""

    adopt = adopt_state(SUBJECT, stored)
    return adopt.value if adopt is not None else stored
