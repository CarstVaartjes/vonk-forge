"""The stored states of a profile application, spoken through the contract.

An application's ``state`` is a word of the core vocabulary (``queued``, ``running``,
``backoff``, ``needs-operator``, ``succeeded``, ``failed``, ``cancelled``,
``superseded``), and so is the state of its cancellation intent: a cancel being
driven is ``observing`` (it was ``cancelling``).  The contract adopts the old
words; every selection by state goes through :func:`words` so an old row or
progress document is found as well as a new one.
"""

from __future__ import annotations

from vonk_agent_protocol import (
    LifecycleState,
    LifecycleSubject,
    is_state,
    stored_words,
)

SUBJECT = LifecycleSubject.FLEET_PROFILE_APPLICATION

OBSERVING = LifecycleState.OBSERVING.value
CANCELLED = LifecycleState.CANCELLED.value
BACKOFF = LifecycleState.BACKOFF.value


def words(*states: LifecycleState) -> tuple[str, ...]:
    """Every stored word (old spellings included) that means one of ``states``."""

    return stored_words(SUBJECT, states)


#: A cancel that is being driven: ``observing``, or the old ``cancelling``.
CANCEL_IN_FLIGHT: tuple[str, ...] = words(LifecycleState.OBSERVING)
#: An application that waits for a person (either spelling).
NEEDS_OPERATOR: tuple[str, ...] = words(LifecycleState.NEEDS_OPERATOR)
#: A live load whose automatic retry is pending: one backing off after a failure
#: (its last failure is the evidence), or a legacy wait for a person.
RETRY_PENDING: tuple[str, ...] = words(
    LifecycleState.BACKOFF, LifecycleState.NEEDS_OPERATOR
)
#: A load a retry starts from: one backing off, or a failed one a person retries
#: (a retry is a new load; the failed one keeps its end).
RETRY_FROM: tuple[str, ...] = (*RETRY_PENDING, *words(LifecycleState.FAILED))
#: A load that is not over: its work runs, waits, or backs off for a retry.
LIVE: tuple[str, ...] = words(
    LifecycleState.QUEUED,
    LifecycleState.RUNNING,
    LifecycleState.BACKOFF,
    LifecycleState.NEEDS_OPERATOR,
)


def cancel_in_flight(stored: object) -> bool:
    return isinstance(stored, str) and is_state(
        SUBJECT, stored, LifecycleState.OBSERVING
    )
