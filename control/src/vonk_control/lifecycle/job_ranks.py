"""Which order of a multi-rank job is the owner's (one rule, one place).

A job on several Sparks is one order per rank.  The order whose rank is the
rendezvous owner owns the job's inputs, outputs and result; the others are
companions that only compute.  The rule mirrors the agent's
``is_endpoint_owner`` and ``RecipeJobRunRequest.endpoint_owner``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from vonk_agent_protocol import RecipeJobRunRequest

from ..models import AgentOperation
from ..stored_json import read_row_column


@dataclass(frozen=True, slots=True)
class OwnerResolution:
    """The owner of a job's orders, with its request, parsed once."""

    owner: AgentOperation | None
    request: RecipeJobRunRequest | None


def _request_of(order: AgentOperation) -> RecipeJobRunRequest | None:
    request = read_row_column(order, "payload")
    return request if isinstance(request, RecipeJobRunRequest) else None


def resolve_owner(orders: Sequence[AgentOperation]) -> OwnerResolution:
    """The owner's order among a job's orders, or the only one there is.

    The owner is ``None`` while any order's request is unreadable or no single
    owner exists: nothing then judges the job, and a later pass decides once
    the rows read. An unreadable order is unknown, never "not the owner".
    """

    parsed = [(order, _request_of(order)) for order in orders]
    if len(parsed) == 1:
        return OwnerResolution(*parsed[0])
    known = [(order, request) for order, request in parsed if request is not None]
    if len(known) != len(parsed):
        return OwnerResolution(None, None)
    owners = [(order, request) for order, request in known if request.endpoint_owner]
    if len(owners) > 1 and all(
        request.compiled_execution_plan.runtime.placement.world_size == 1
        for _order, request in owners
    ):
        # A single-node job beside siblings no Spark was handed: the order that
        # was issued is the job's own.
        issued = [pair for pair in owners if pair[0].current_attempt > 0]
        return (
            OwnerResolution(*issued[0])
            if len(issued) == 1
            else OwnerResolution(None, None)
        )
    if len(owners) != 1:
        return OwnerResolution(None, None)
    return OwnerResolution(*owners[0])


def order_is_owner(order: AgentOperation) -> bool | None:
    """Whether the order is the owner rank's by the typed request; unreadable is ``None``."""

    request = _request_of(order)
    return None if request is None else request.endpoint_owner


def owner_order(orders: Sequence[AgentOperation]) -> AgentOperation | None:
    return resolve_owner(orders).owner


def fan_in_cancellation_key(parent_id: str) -> str:
    """The cancellation a ended rank asks of its siblings (one per job)."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{parent_id}:ranks"))
