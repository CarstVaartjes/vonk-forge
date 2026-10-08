"""Activity for Fleet profiles."""

from __future__ import annotations

from typing import Any
from typing import cast as _typing_cast

from sqlalchemy import case
from vonk_agent_protocol import LifecycleState

from .. import fleet_profile_states
from ..fleet_profile_contract import FleetProfileApplicationCancellationIntent
from ..models import FleetProfileApplication
from .dependencies import _PROFILE_ACTIVITY_ACTIVE_STATES


def _profile_activity_pending(state, cancellation_state):
    """One pending predicate for in-memory display and SQL selection."""

    active = (
        state in _PROFILE_ACTIVITY_ACTIVE_STATES
        if isinstance(state, str)
        else state.in_(_PROFILE_ACTIVITY_ACTIVE_STATES)
    )
    if isinstance(cancellation_state, str) or cancellation_state is None:
        return active & fleet_profile_states.cancel_in_flight(cancellation_state)
    return active & cancellation_state.in_(fleet_profile_states.CANCEL_IN_FLIGHT)


def _profile_activity_state(
    state: str, cancellation: FleetProfileApplicationCancellationIntent | None
) -> str:
    """Project an active application from its durable cancellation owner."""

    if _profile_activity_pending(
        state, cancellation.state if cancellation is not None else None
    ):
        return fleet_profile_states.OBSERVING
    return state


def _profile_activity_state_expression():
    return case(
        (
            _typing_cast(
                Any,
                _profile_activity_pending(
                    FleetProfileApplication.state,
                    FleetProfileApplication.progress["cancellation"][
                        "state"
                    ].as_string(),
                ),
            ),
            fleet_profile_states.OBSERVING,
        ),
        # A failed application with a scheduled retry is presented as queued.
        (
            (FleetProfileApplication.state == LifecycleState.FAILED.value)
            & FleetProfileApplication.progress["retry_due_at"].as_string().is_not(None),
            LifecycleState.QUEUED.value,
        ),
        else_=FleetProfileApplication.state,
    )


def retry_disposition_of(error: BaseException) -> str | None:
    """What an automatic retry does with ``error``; None for a non-conflict."""

    return getattr(type(error), "retry_disposition", None)
