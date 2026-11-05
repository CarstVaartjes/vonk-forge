"""The one writer of ``agent_nodes.workload_intent_ordinal``.

A Spark's workload intent is the ordinal of the newest request that owns its
workload. It is numbered above every ordinal the Spark carries and written by
a conditional UPDATE: ``... WHERE node_id = :n AND workload_intent_ordinal =
:seen``. A writer that lost the race to a newer intent changes nothing and
reads again (``allocate_workload_intent``) or is told it no longer owns the
Spark (``take_workload_intent``). Nothing locks an ``agent_nodes`` row to read
or number an intent, so agent traffic and admission never wait on each other
for it; the claim fence still refuses an order whose ordinal is no longer the
Spark's.

Only this module assigns the column or computes the next ordinal; an
added-line guard refuses both anywhere else.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from .models import AgentNode

# Every lost race is another request's committed intent, so a loop that keeps
# losing means the Sparks are churning, not that this request can succeed by
# waiting. The caller's request fails typed and is asked again.
ALLOCATION_ATTEMPTS = 16


class WorkloadIntentUnavailable(RuntimeError):
    """A target Spark vanished, or kept changing owner through every attempt."""


def _ordinals(session: Session, node_ids: Sequence[str]) -> dict[str, int]:
    return dict(
        session.execute(
            select(AgentNode.node_id, AgentNode.workload_intent_ordinal).where(
                AgentNode.node_id.in_(node_ids)
            )
        ).tuples()
    )


def take_workload_intent(
    session: Session, owned: Mapping[str, int], *, at_least: int = 0
) -> tuple[int, tuple[str, ...]]:
    """Number a new intent above ``owned`` and write it to the Sparks still at them.

    ``owned`` maps each Spark to the ordinal its caller read and owns. Returns
    the new ordinal and the Sparks that took it; a Spark now at another ordinal
    belongs to a newer intent and is left alone.
    """

    ordinal = max([at_least, *owned.values()]) + 1
    taken: list[str] = []
    for node_id in sorted(owned):
        won = session.execute(
            update(AgentNode)
            .where(
                AgentNode.node_id == node_id,
                AgentNode.workload_intent_ordinal == owned[node_id],
            )
            .values(workload_intent_ordinal=ordinal)
            .execution_options(synchronize_session="fetch")
        )
        if isinstance(won, CursorResult) and won.rowcount == 1:
            taken.append(node_id)
    return ordinal, tuple(taken)


def allocate_workload_intent(session: Session, node_ids: Sequence[str]) -> int:
    """The next workload intent for ``node_ids``, written to every one of them."""

    wanted = sorted(set(node_ids))
    if not wanted:
        return 1
    for _attempt in range(ALLOCATION_ATTEMPTS):
        seen = _ordinals(session, wanted)
        if len(seen) != len(wanted):
            raise WorkloadIntentUnavailable("a workload intent target disappeared")
        ordinal, taken = take_workload_intent(session, seen)
        if len(taken) == len(wanted):
            return ordinal
    raise WorkloadIntentUnavailable("workload intent kept changing owner")


def raise_workload_intent(
    session: Session, node_ids: Sequence[str], ordinal: int
) -> None:
    """Carry an ordinal the caller already owns onto ``node_ids``, never lowering one."""

    if not node_ids:
        return
    session.execute(
        update(AgentNode)
        .where(
            AgentNode.node_id.in_(node_ids),
            AgentNode.workload_intent_ordinal < ordinal,
        )
        .values(workload_intent_ordinal=ordinal)
        .execution_options(synchronize_session="fetch")
    )
