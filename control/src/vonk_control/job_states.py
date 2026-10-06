"""The stored states of a generic job, spoken through the contract.

The ``jobs`` table is shared by many kinds (recipe operations, Run/Switch, image
availability, agent upgrades, recipe update batches), and each spelled "waiting to
retry", "being cancelled" and "waiting for a person" its own way (``partial``,
``waiting``, ``cancelling``, ``waiting-for-operator``).  A job's ``state`` is a word
of the core vocabulary; the contract adopts the old spellings, and every selection
goes through :func:`words`, which names every stored word (old and new) that means
one of the states asked for.  A row written before the rename is therefore found
exactly as before, and a row written after it is found too, and no module spells a
retired word.
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
    legacy_observation_cause,
    stored_words,
)

SUBJECT = LifecycleSubject.JOB


def words(*states: LifecycleState, kind: str | None = None) -> tuple[str, ...]:
    """Every stored word (old spellings included) that means one of ``states``.

    ``kind`` scopes the answer to one kind of job whose spelling differs (a recipe
    update batch ends ``partial``, which is not a retry there).
    """

    return stored_words(SUBJECT, states, kind)


def means(stored: str | None, *states: LifecycleState, kind: str | None = None) -> bool:
    """Whether a stored word means one of ``states``."""

    return is_state(SUBJECT, stored, *states, kind=kind)


def core(stored: str | None, kind: str | None = None) -> LifecycleState | None:
    """The core state a stored word means, or ``None`` for a word it does not know."""

    if stored is None:
        return None
    adopted = adopt_state(SUBJECT, stored, kind)
    return None if adopted is None else adopted.state


def cancel_requested_word(stored: str | None, kind: str | None = None) -> bool:
    """Whether an old word said a cancel was under way (``cancelling``)."""

    if stored is None:
        return False
    adopted = adopt_state(SUBJECT, stored, kind)
    return adopted is not None and adopted.cancel_requested


def any_of(stored: str | None, groups: Iterable[tuple[LifecycleState, ...]]) -> bool:
    return any(means(stored, *group) for group in groups)


ATTEMPT = LifecycleSubject.JOB_ATTEMPT


def attempt_words(*states: LifecycleState) -> tuple[str, ...]:
    """Every stored word of a job *attempt* that means one of ``states``.

    An attempt that lapsed its lease (``expired``) or ended with the executor's
    "unknown" (``waiting-for-operator``) was interrupted, so it is observed; a
    lapsed *job* is over (see :func:`words`).
    """

    return stored_words(ATTEMPT, states)


def attempt_means(stored: str | None, *states: LifecycleState) -> bool:
    return is_state(ATTEMPT, stored, *states)


# -- the attempt of a job --------------------------------------------------------
#
# An attempt records one try.  It ends ``succeeded``, ``failed``, ``cancelled`` or
# ``observing`` (no definite answer), and ``observation_cause`` says why: the
# executor ``reported-unknown`` or its ``lease-lapsed`` (the old ``waiting-for-
# operator`` and ``expired`` words said it in the state itself).

OBSERVING = LifecycleState.OBSERVING.value


def attempt_cause(attempt: Any) -> ObservationCause | None:
    """Why an observed attempt has no answer: the typed field, or the old word."""

    if not attempt_means(attempt.state, LifecycleState.OBSERVING):
        return None
    recorded = getattr(attempt, "observation_cause", None)
    if recorded:
        return ObservationCause(recorded)
    return legacy_observation_cause(attempt.state)


def attempt_lapsed(attempt: Any) -> bool:
    """The attempt's lease ran out without a report."""

    return attempt_cause(attempt) is ObservationCause.LEASE_LAPSED


def lapse(attempt: Any) -> None:
    """The attempt can no longer report: it is observed, because its lease lapsed."""

    attempt.state = OBSERVING
    attempt.observation_cause = ObservationCause.LEASE_LAPSED.value
