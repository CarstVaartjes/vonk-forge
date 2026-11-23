"""Background observation never queues behind a foreground owner."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vonk_control.agent_jobs.service import AgentJobService


@contextmanager
def observation_ownership(service: AgentJobService) -> Generator[bool]:
    """A busy pass is a miss; the periodic owner retries from persisted state."""
    if not service._claim_lock.acquire(blocking=False):
        yield False
        return
    try:
        yield True
    finally:
        service._claim_lock.release()
