"""The stored states of a Spark order and its attempts, spoken through the contract.

An order's ``state`` is a word of the core vocabulary: ``queued``, ``running``,
``backoff`` (a scheduled retry), ``observing`` (the effect is being checked),
``needs-operator`` (a person must act), ``succeeded``, ``failed``, ``cancelled``.
A row written before the rename carries ``waiting-for-operator`` for all three
kinds of wait; the contract adopts it, and every selection below includes it, so
an old row is found as well as a new one and no module spells a retired word.

An attempt records one try.  It ends ``succeeded``, ``failed``, ``cancelled`` or
``observing`` (no definite answer), and the typed ``observation_cause`` says why:
the executor ``reported-unknown`` or its ``lease-lapsed``.  The old spellings
``waiting-for-operator`` and ``expired`` carried that cause in the word itself; the
helpers read either.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_
from vonk_agent_protocol import (
    AgentResultState,
    LifecycleState,
    LifecycleSubject,
    ObservationCause,
    StateAlias,
    is_state,
    legacy_observation_cause,
    live_words,
    stored_words,
)

ORDER = LifecycleSubject.AGENT_OPERATION
ATTEMPT = LifecycleSubject.AGENT_OPERATION_ATTEMPT

_WAIT = (
    LifecycleState.BACKOFF,
    LifecycleState.OBSERVING,
    LifecycleState.NEEDS_OPERATOR,
)

#: What the agent's result calls "I could not confirm the effect" (the wire word).
WIRE_UNKNOWN = AgentResultState.OBSERVING.value

#: The retired spellings an old attempt row may carry (see ``legacy_observation_cause``).
LEGACY_UNKNOWN = StateAlias.WAITING_FOR_OPERATOR.value
LEGACY_LAPSED = StateAlias.EXPIRED.value

#: Immutable outcome carried by historical aggregate/member reads only.
#: Current normal lifecycle writers do not emit it; it is neither a core state
#: nor an adopted alias, and its terminal display meaning proves no effect.
#: The underlying retained SQL columns still accept strings.
RETAINED_COMPENSATED = "compensated"


# -- orders ------------------------------------------------------------------

QUEUED = LifecycleState.QUEUED.value
RUNNING = LifecycleState.RUNNING.value
BACKOFF = LifecycleState.BACKOFF.value
OBSERVING = LifecycleState.OBSERVING.value
NEEDS_OPERATOR = LifecycleState.NEEDS_OPERATOR.value
#: An order that waits: a scheduled retry, an observation or a person.
PARKED: tuple[str, ...] = stored_words(ORDER, _WAIT)
QUEUED_OR_PARKED: tuple[str, ...] = stored_words(ORDER, (LifecycleState.QUEUED, *_WAIT))
RUNNING_OR_PARKED: tuple[str, ...] = stored_words(
    ORDER, (LifecycleState.RUNNING, *_WAIT)
)
#: An order that has not ended.
LIVE: tuple[str, ...] = live_words(ORDER)


def order_is_parked(stored: str | None) -> bool:
    return is_state(ORDER, stored, *_WAIT)


# -- attempts ----------------------------------------------------------------

#: An attempt that ended with no definite answer.
ATTEMPT_OBSERVING: tuple[str, ...] = stored_words(ATTEMPT, (LifecycleState.OBSERVING,))
FAILED = LifecycleState.FAILED.value


def attempt_is_observing(attempt: Any) -> bool:
    return is_state(ATTEMPT, attempt.state, LifecycleState.OBSERVING)


def attempt_cause(attempt: Any) -> ObservationCause | None:
    """Why an attempt is observed: the typed field, or the old word's own meaning."""

    if not attempt_is_observing(attempt):
        return None
    recorded = getattr(attempt, "observation_cause", None)
    if recorded:
        return ObservationCause(recorded)
    return legacy_observation_cause(attempt.state)


def attempt_lapsed(attempt: Any) -> bool:
    """The attempt's lease ran out without a report."""

    return attempt_cause(attempt) is ObservationCause.LEASE_LAPSED


def attempt_reported_unknown(attempt: Any) -> bool:
    """The executor reported that it could not confirm the effect."""

    return attempt_cause(attempt) is ObservationCause.REPORTED_UNKNOWN


def attempt_failed_or_unknown(attempt: Any) -> bool:
    """A definite failure, or a report that the effect is unknown."""

    return attempt.state == FAILED or attempt_reported_unknown(attempt)


def attempt_wire_state(attempt: Any) -> str:
    """The agent-wire word of an attempt's state (for the protocol validators)."""

    return WIRE_UNKNOWN if attempt_reported_unknown(attempt) else str(attempt.state)


def lapse(attempt: Any) -> None:
    """The attempt can no longer report: observed, because its lease lapsed."""

    attempt.state = OBSERVING
    attempt.observation_cause = ObservationCause.LEASE_LAPSED.value


def record_wire_state(attempt: Any, wire_state: str) -> None:
    """Keep the executor's reported state on its attempt, in the core vocabulary."""

    wire_state = AgentResultState(wire_state).value
    if wire_state == WIRE_UNKNOWN:
        attempt.state = OBSERVING
        attempt.observation_cause = ObservationCause.REPORTED_UNKNOWN.value
    else:
        attempt.state = wire_state
        attempt.observation_cause = None


# -- the same questions asked of the database ----------------------------------


def sql_attempt_lapsed(model: Any) -> Any:
    """SQL: the attempt's lease lapsed without a report (old or new spelling)."""

    return or_(
        model.state == LEGACY_LAPSED,
        and_(
            model.state == OBSERVING,
            model.observation_cause == ObservationCause.LEASE_LAPSED.value,
        ),
    )


def sql_attempt_failed_or_unknown(model: Any) -> Any:
    """SQL: the attempt failed, or its executor reported the effect unknown."""

    return or_(
        model.state == FAILED,
        model.state == LEGACY_UNKNOWN,
        and_(
            model.state == OBSERVING,
            model.observation_cause == ObservationCause.REPORTED_UNKNOWN.value,
        ),
    )


def attempt_outcome_key(attempt: Any) -> str:
    """What an attempt's outcome is called when narrated: failed, or why it is observed."""

    cause = attempt_cause(attempt)
    return cause.value if cause is not None else str(attempt.state)
