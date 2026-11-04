"""The stored states of a generic job, spoken through the contract.

The ``jobs`` table is shared by many kinds (recipe operations, Run/Switch, image
availability, agent upgrades, recipe update batches).  A job's ``state`` is a word of
the core vocabulary, and every selection goes through :func:`words`, so no module
spells one.  A stored word outside the vocabulary is unknown (:func:`core` is
``None``), and the kind's adapter re-evaluates the job instead of trusting it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from vonk_agent_protocol import (
    LifecycleState,
    LifecycleSubject,
    ObservationCause,
    adopt_state,
    is_state,
    stored_words,
)

SUBJECT = LifecycleSubject.JOB


def words(*states: LifecycleState) -> tuple[str, ...]:
    """Every stored word that means one of ``states``."""

    return stored_words(SUBJECT, states)


def means(stored: str | None, *states: LifecycleState) -> bool:
    """Whether a stored word means one of ``states``."""

    return is_state(SUBJECT, stored, *states)


def core(stored: str | None) -> LifecycleState | None:
    """The core state a stored word means, or ``None`` for a word it does not know."""

    if stored is None:
        return None
    return adopt_state(SUBJECT, stored)


def any_of(stored: str | None, groups: Iterable[tuple[LifecycleState, ...]]) -> bool:
    return any(means(stored, *group) for group in groups)


ATTEMPT = LifecycleSubject.JOB_ATTEMPT


def attempt_words(*states: LifecycleState) -> tuple[str, ...]:
    """Every stored word of a job *attempt* that means one of ``states``."""

    return stored_words(ATTEMPT, states)


def attempt_means(stored: str | None, *states: LifecycleState) -> bool:
    return is_state(ATTEMPT, stored, *states)


# -- the attempt of a job --------------------------------------------------------
#
# An attempt records one try.  It ends ``succeeded``, ``failed``, ``cancelled`` or
# ``observing`` (no definite answer), and ``observation_cause`` says why: the
# executor ``reported-unknown`` or its ``lease-lapsed``.

OBSERVING = LifecycleState.OBSERVING.value


def attempt_cause(attempt: Any) -> ObservationCause | None:
    """Why an observed attempt has no answer (its typed field), or ``None``."""

    if not attempt_means(attempt.state, LifecycleState.OBSERVING):
        return None
    recorded = getattr(attempt, "observation_cause", None)
    return ObservationCause(recorded) if recorded else None


def attempt_lapsed(attempt: Any) -> bool:
    """The attempt's lease ran out without a report."""

    return attempt_cause(attempt) is ObservationCause.LEASE_LAPSED


def lapse(attempt: Any) -> None:
    """The attempt can no longer report: it is observed, because its lease lapsed."""

    attempt.state = OBSERVING
    attempt.observation_cause = ObservationCause.LEASE_LAPSED.value
