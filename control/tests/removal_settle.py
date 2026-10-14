"""Settle model-cache removals on a fake clock, not on how fast the runner is."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from vonk_control import model_cache_states
from vonk_control.agent_operation_facts import aware
from vonk_control.artifact_lifecycle import dead_removal_identities
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import ModelCacheOperation


def advance_until_ended(
    service: ModelCacheService, operation_id: str | None = None, *, rounds: int = 16
) -> None:
    """Advance removals until the operation (or every live one) has ended.

    The scope observation has a wall-clock budget; a slow pass yields into a
    recorded wait. Skewing the service clock past that wait (restored on exit)
    makes the outcome depend on behaviour rather than runner speed.
    """
    base = service._clock
    skew = timedelta(0)
    service._clock = lambda: base() + skew
    try:
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
                    skew = max(skew, due - base())
    finally:
        service._clock = base


def release_ended_gates(service: ModelCacheService, *, rounds: int = 16) -> None:
    """Drive gate reconciliation until no ended removal still owns a gate.

    One pass has a wall-clock budget and pages through gates, so a slow runner
    may leave a gate for the next worker tick. Looping on the observable
    outcome (no dead owner left) makes the test independent of runner speed.
    """
    for _ in range(rounds):
        with service._session() as session:
            dead = dead_removal_identities(
                session, owner_kind="model-cache-operation", limit=1
            )
        if not dead:
            return
        service.reconcile_removal_gates()
    raise AssertionError("ended removal gates were not released")
