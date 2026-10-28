"""Swap policy follows the selected profile application, including idle Sparks."""

from collections.abc import Sequence

from sqlalchemy.orm import Session
from vonk_agent_protocol.swap import SwapIntent, default_swap

from .models import FleetProfileSelection


def selected_swaps(session: Session, node_ids: Sequence[str]) -> dict[str, SwapIntent]:
    """Every Spark keeps the policy default; the selection generation fences."""
    selection = session.get(FleetProfileSelection, 1)
    generation = 0 if selection is None else selection.generation
    return {node_id: default_swap(generation) for node_id in node_ids}


def selected_swap(session: Session, node_id: str) -> SwapIntent:
    return selected_swaps(session, (node_id,))[node_id]
