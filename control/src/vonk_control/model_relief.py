"""A Spark's unused model objects go before its installations.

An agent reports the bytes of its model objects no installation links or
names (``unused_model_bytes``). When a Spark needs free space, the storage
collector counts those first and asks the agent, through the inventory
verdict, to remove them (least recently used first) until the Spark has the
free space it wants; it uninstalls installations only for what they cannot
cover.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import AgentNode

#: How long an ask stays in the verdict without the collector repeating it.
#: The collector repeats it while the Spark stays short.
WANT_TTL = timedelta(minutes=10)

#: How long after an ask the Spark's report may not show its effect yet: the
#: agent hears it with its next inventory (two minutes), removes between
#: claims, and the inventory after that measures it.
WANT_SETTLE = timedelta(minutes=5)


def free_bytes_wanted(node: AgentNode, now: datetime) -> int | None:
    """The free space the collector currently asks of this Spark, if any."""

    at = node.storage_free_wanted_at
    if node.storage_free_wanted_bytes is None or at is None:
        return None
    at = at if at.tzinfo is not None else at.replace(tzinfo=now.tzinfo)
    return node.storage_free_wanted_bytes if now - at <= WANT_TTL else None


def want_free_bytes(session: Session, node_id: str, wanted: int, now: datetime) -> None:
    """Ask the Spark to reach ``wanted`` free bytes from its unused model
    objects. A standing ask that is the same and fresh is left alone, so its
    time still says when the agent first heard it."""

    node = session.get(AgentNode, node_id)
    if node is None:
        return
    current = free_bytes_wanted(node, now)
    if current == wanted and node.storage_free_wanted_at is not None:
        at = node.storage_free_wanted_at
        at = at if at.tzinfo is not None else at.replace(tzinfo=now.tzinfo)
        if now - at <= WANT_TTL / 2:
            return
    node.storage_free_wanted_bytes = wanted
    node.storage_free_wanted_at = now
