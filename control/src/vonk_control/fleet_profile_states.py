"""The stored states of a profile application, spoken through the contract.

An application's ``state`` is a word of the core vocabulary (``queued``, ``running``,
``needs-operator``, ``succeeded``, ``failed``, ``cancelled``, ``superseded``), and
so is the state of its cancellation intent: a cancel being driven is ``observing``
(it was ``cancelling``).  The contract adopts the old words; every selection by
state goes through :func:`words` so an old row or progress document is found as
well as a new one.
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


def words(*states: LifecycleState) -> tuple[str, ...]:
    """Every stored word (old spellings included) that means one of ``states``."""

    return stored_words(SUBJECT, states)


#: A cancel that is being driven: ``observing``, or the old ``cancelling``.
CANCEL_IN_FLIGHT: tuple[str, ...] = words(LifecycleState.OBSERVING)
#: An application that waits for a person (either spelling).
NEEDS_OPERATOR: tuple[str, ...] = words(LifecycleState.NEEDS_OPERATOR)


def cancel_in_flight(stored: object) -> bool:
    return isinstance(stored, str) and is_state(
        SUBJECT, stored, LifecycleState.OBSERVING
    )
