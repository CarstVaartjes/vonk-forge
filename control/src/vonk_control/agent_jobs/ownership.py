"""Background observation never queues behind a foreground owner."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import AgentJobService


@contextmanager
def observation_ownership(service: AgentJobService) -> Iterator[bool]:
    """A busy pass is a miss; the periodic owner retries from persisted state."""
    if not service._claim_lock.acquire(blocking=False):
        yield False
        return
    try:
        yield True
    finally:
        service._claim_lock.release()
