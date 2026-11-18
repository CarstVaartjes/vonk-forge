"""Which order of a multi-rank job is the owner's (one rule, one place).

A job on several Sparks is one order per rank.  The order whose rank is the
rendezvous owner owns the job's inputs, outputs and result; the others are
companions that only compute.  The rule mirrors the agent's
``is_endpoint_owner`` and ``RecipeJobRunRequest.endpoint_owner``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from vonk_agent_protocol import RecipeJobRunRequest

from ..models import AgentOperation
from ..stored_json import read_row_column


def order_is_owner(order: AgentOperation) -> bool | None:
    """Whether the order is the owner rank's, by the one rule on the typed request.

    ``None`` when the stored request cannot be read: an unreadable order is
    unknown, never "not the owner".
    """

    request = read_row_column(order, "payload")
    if isinstance(request, RecipeJobRunRequest):
        return request.endpoint_owner
    return None


def owner_order(orders: Sequence[AgentOperation]) -> AgentOperation | None:
    """The owner's order among a job's orders, or the only one there is.

    ``None`` while any order's request is unreadable or no single owner exists:
    nothing then judges the job, and a later pass decides once the rows read.
    """

    if len(orders) == 1:
        return orders[0]
    owners = [order_is_owner(order) for order in orders]
    if None in owners or owners.count(True) != 1:
        return None
    return orders[owners.index(True)]


def fan_in_cancellation_key(parent_id: str) -> str:
    """The cancellation a ended rank asks of its siblings (one per job)."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{parent_id}:ranks"))
