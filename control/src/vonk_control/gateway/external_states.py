"""The one state machine of an external model deployment, and its one transition function.

``ExternalPublicationState`` says where a deployment stands on its way into, and out
of, the gateway.  Every writer (the operator's accept, the publication tick, the
worker's redrive) changes it through :func:`apply_event`, which follows the declared
edges below; an event that has no edge from the current state changes nothing, and a
stored word the machine never had is re-planned from the operator's intent
(:func:`settle`), so a damaged or retired word self-heals instead of blocking.

::

    PENDING --acked--> PUBLISHED --revision accepted--> PENDING
    PENDING --block--> BLOCKED --retry due | revision accepted--> PENDING
    any --withdraw--> WITHDRAWING --drained--> WITHDRAWN --enable--> PENDING
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Final

from vonk_agent_protocol.state_machines import (
    ExternalBlockReason,
    ExternalPublicationState,
    adopt_machine_state,
)
from vonk_control.models.gateway import GatewayExternalDeployment


class ExternalEvent(StrEnum):
    """What can happen to a deployment."""

    REVISION_ACCEPTED = "revision-accepted"
    ACKED = "acked"
    BLOCK = "block"
    RETRY_DUE = "retry-due"
    WITHDRAW = "withdraw"
    DRAINED = "drained"
    ENABLE = "enable"


_PENDING = ExternalPublicationState.PENDING
_PUBLISHED = ExternalPublicationState.PUBLISHED
_BLOCKED = ExternalPublicationState.BLOCKED
_WITHDRAWING = ExternalPublicationState.WITHDRAWING
_WITHDRAWN = ExternalPublicationState.WITHDRAWN

#: The declared edges: the state an event leads to, per current state.
EDGES: Final[
    Mapping[ExternalPublicationState, Mapping[ExternalEvent, ExternalPublicationState]]
] = {
    _PENDING: {
        ExternalEvent.REVISION_ACCEPTED: _PENDING,
        ExternalEvent.ACKED: _PUBLISHED,
        ExternalEvent.BLOCK: _BLOCKED,
        ExternalEvent.WITHDRAW: _WITHDRAWING,
    },
    _PUBLISHED: {
        ExternalEvent.REVISION_ACCEPTED: _PENDING,
        ExternalEvent.WITHDRAW: _WITHDRAWING,
    },
    _BLOCKED: {
        ExternalEvent.REVISION_ACCEPTED: _PENDING,
        ExternalEvent.RETRY_DUE: _PENDING,
        ExternalEvent.WITHDRAW: _WITHDRAWING,
    },
    _WITHDRAWING: {
        ExternalEvent.WITHDRAW: _WITHDRAWING,
        ExternalEvent.DRAINED: _WITHDRAWN,
    },
    _WITHDRAWN: {
        ExternalEvent.WITHDRAW: _WITHDRAWN,
        ExternalEvent.REVISION_ACCEPTED: _WITHDRAWN,
        ExternalEvent.ENABLE: _PENDING,
    },
}

#: A state the gateway still serves from: the applied entry stays until a new
#: generation without it is acknowledged.
SERVING: Final = frozenset({_PENDING, _PUBLISHED, _BLOCKED})

#: Events that clear the reason a deployment was not serving.
_CLEARS_REASON: Final = frozenset(
    {
        ExternalEvent.ACKED,
        ExternalEvent.REVISION_ACCEPTED,
        ExternalEvent.RETRY_DUE,
        ExternalEvent.ENABLE,
    }
)


def next_state(stored: object, event: ExternalEvent) -> ExternalPublicationState:
    """Where ``event`` leads from ``stored``: an event with no edge changes nothing.

    A word the machine never had is not an error: it is treated as ``pending``,
    and the planner re-derives the rest from the operator's intent.
    """

    current = adopt_machine_state(ExternalPublicationState, stored)
    if current is None:
        return _PENDING
    return EDGES[current].get(event, current)


def apply_event(
    row: GatewayExternalDeployment,
    event: ExternalEvent,
    *,
    reason: ExternalBlockReason | None = None,
    now: datetime,
) -> ExternalPublicationState:
    """Move ``row`` along the edge ``event`` names: the one writer of ``state`` and ``reason``."""

    before = (row.state, row.reason)
    state = next_state(row.state, event)
    row.state = state.value
    if event in _CLEARS_REASON:
        row.reason = None
    if reason is not None:
        row.reason = reason.value
    if (row.state, row.reason) != before:
        row.updated_at = now  # an event that changes nothing writes nothing
    return state


def settle(
    row: GatewayExternalDeployment, *, now: datetime
) -> ExternalPublicationState:
    """Make ``state`` agree with the operator's intent; the self-healing step.

    The intent is ``enabled`` and ``removal_requested``: a deployment that is not
    wanted moves toward ``withdrawn`` whatever word it carries, a wanted one that
    is withdrawn serves again, and an unknown word re-plans as ``pending``.
    """

    if adopt_machine_state(ExternalPublicationState, row.state) is None:
        row.state = _PENDING.value
        row.updated_at = now
    if not row.enabled or row.removal_requested:
        return apply_event(row, ExternalEvent.WITHDRAW, now=now)
    if row.state == _WITHDRAWN.value:
        return apply_event(row, ExternalEvent.ENABLE, now=now)
    return ExternalPublicationState(row.state)


def block_reason(row: GatewayExternalDeployment) -> ExternalBlockReason | None:
    """The typed reason on a row; a word that is not one reads as none."""

    return adopt_machine_state(ExternalBlockReason, row.reason)
