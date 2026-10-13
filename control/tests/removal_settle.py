"""Settle model-cache removals on a fake clock, not on how fast the runner is."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from vonk_control import model_cache_states
from vonk_control.agent_operation_facts import aware
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import ModelCacheOperation


def advance_until_ended(
    service: ModelCacheService, operation_id: str | None = None, *, rounds: int = 16
) -> None:
    """Advance removals until the operation (or every live one) has ended.

    The scope observation has a wall-clock budget; a slow pass yields into a
    recorded wait. Moving a fake clock past that wait makes the outcome depend
    on behaviour rather than runner speed.
    """
    now = [service._clock()]
    service._clock = lambda: now[0]
    for _ in range(rounds):
        service.advance_removals(limit=1)
        live = select(ModelCacheOperation).where(
            ModelCacheOperation.state.in_(model_cache_states.LIVE)
        )
        if operation_id is not None:
            live = live.where(ModelCacheOperation.id == operation_id)
        with service._sessions() as session:
            rows = session.scalars(live).all()
        if not rows:
            return
        for row in rows:
            if row.next_action_at is not None:
                due = aware(row.next_action_at) + timedelta(seconds=1)
                now[0] = max(now[0], due)
