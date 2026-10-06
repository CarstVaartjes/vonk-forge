"""What refused work needs free, as the worker's storage reclaimer sees it.

Work that is refused for lack of disk (a profile load, an install, a build, a
model download) says how much free space it needs on one Spark or on the NAS.
The reclaimer in the same worker frees it by removing what nothing uses, and
the refused work finds the space on its next ordinary retry. A demand is only a
request: it lapses on its own unless the refused work repeats it, so nothing
durable can be left behind by work that went away, and the reclaimer measures
free space itself rather than trusting a number that has aged.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from vonk_agent_protocol import StorageDemandCode

from .worker_memory_contract import WorkerMemoryComponent

_LOGGER = logging.getLogger(__name__)

NAS_MODELS = "nas:model"
NAS_IMAGES = "nas:image"


def spark_scope(node_id: str) -> str:
    return f"spark:{node_id}"


@dataclass(frozen=True, slots=True)
class StorageDemand:
    scope: str
    #: Free bytes the refused work needs on that scope to be admitted.
    required_free_bytes: int
    source: str
    subject: str
    reason: str
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class StorageRelief:
    """What a waiting load can say about the space it needs."""

    code: str
    needed_bytes: int
    freeable_bytes: int
    detail: str


STORAGE_EVICTING = StorageDemandCode.EVICTING
STORAGE_INSUFFICIENT = StorageDemandCode.INSUFFICIENT_AFTER_EVICTION


class StorageDemands:
    """Thread-safe register of unexpired demands, one per (scope, source, subject)."""

    def __init__(
        self, clock: Callable[[], datetime], *, ttl: timedelta | None = None
    ) -> None:
        from .settings import STORAGE_DEMAND_TTL_SECONDS

        self._clock = clock
        self._ttl = ttl or timedelta(seconds=STORAGE_DEMAND_TTL_SECONDS)
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str, str], StorageDemand] = {}

    def request(
        self,
        scope: str,
        required_free_bytes: int,
        *,
        source: str,
        subject: str = "",
        reason: str,
    ) -> None:
        """Ask for free space. Refused work never fails because it asked."""

        if required_free_bytes <= 0:
            return
        try:
            demand = StorageDemand(
                scope,
                int(required_free_bytes),
                source,
                subject,
                reason,
                self._clock(),
            )
            with self._lock:
                self._items[(scope, source, subject)] = demand
        except Exception:  # asking for space is best effort
            _LOGGER.warning("could not record a storage demand", exc_info=True)

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        with self._lock:
            return {WorkerMemoryComponent.STORAGE_DEMANDS: len(self._items)}

    def active(self) -> list[StorageDemand]:
        now = self._clock()
        with self._lock:
            for key, demand in list(self._items.items()):
                if now - demand.requested_at > self._ttl:
                    del self._items[key]
            return sorted(
                self._items.values(),
                key=lambda item: (item.scope, item.source, item.subject),
            )

    def settle(self, scope: str, free_bytes: int) -> None:
        """Drop every demand on ``scope`` that its free space now covers."""

        with self._lock:
            for key, demand in list(self._items.items()):
                if demand.scope == scope and demand.required_free_bytes <= free_bytes:
                    del self._items[key]


__all__ = [
    "NAS_IMAGES",
    "NAS_MODELS",
    "STORAGE_EVICTING",
    "STORAGE_INSUFFICIENT",
    "StorageDemand",
    "StorageDemands",
    "StorageRelief",
    "spark_scope",
]
