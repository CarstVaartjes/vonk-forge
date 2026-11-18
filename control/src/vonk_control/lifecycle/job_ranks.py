"""Which order of a multi-rank job is the owner's (one rule, one place).

A job on several Sparks is one order per rank.  The order whose rank is the
rendezvous owner owns the job's inputs, outputs and result; the others are
companions that only compute.  The rule mirrors the agent's
``is_endpoint_owner`` and ``RecipeJobRunRequest.endpoint_owner``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

from ..models import AgentOperation


def payload_is_owner(payload: object) -> bool:
    """Whether a stored job-run payload is the owner rank's (read, not parsed)."""

    plan = (
        payload.get("compiled_execution_plan") if isinstance(payload, Mapping) else None
    )
    runtime = plan.get("runtime") if isinstance(plan, Mapping) else None
    placement = runtime.get("placement") if isinstance(runtime, Mapping) else None
    if not isinstance(placement, Mapping):
        return False
    local = placement.get("local_address")
    return placement.get("world_size") == 1 or (
        local is not None and local == placement.get("master_address")
    )


def owner_order(orders: Sequence[AgentOperation]) -> AgentOperation | None:
    """The owner's order among a job's orders, or the only one there is."""

    owners = [order for order in orders if payload_is_owner(order.payload)]
    if len(owners) == 1:
        return owners[0]
    return orders[0] if len(orders) == 1 else None


def fan_in_cancellation_key(parent_id: str) -> str:
    """The cancellation a ended rank asks of its siblings (one per job)."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{parent_id}:ranks"))
