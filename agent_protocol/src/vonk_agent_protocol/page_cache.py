"""Page-cache drops the privileged helper performs while a workload loads.

On a unified-memory Spark the driver and vLLM's free-memory check count
reclaimable page cache as used, so a load that read its weights through the
cache can fail an allocation with memory still reclaimable. The agent asks;
the helper syncs (bounded) and drops the clean page cache. A drop that fails is
reported, never a refusal of the start.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, Field

from .wire_model import WireEnum, WireModel

MemoryBytes = Annotated[int, Field(strict=True, ge=0, le=16 * 1024**4)]


class PageCacheDropKind(WireEnum):
    PAGE_CACHE_DROP = "page-cache-drop"


class PageCacheDropRequest(WireModel):
    """Peer-authorized helper request; carries no path or container authority."""

    kind: PageCacheDropKind
    request_id: UUID


class PageCacheSync(WireEnum):
    """Whether the bounded ``sync`` before the drop finished in time.

    Absent from a report whose sync never started.
    """

    COMPLETED = "completed"
    # Still writing back at its bound (or an earlier sync still is): the drop
    # proceeds and reclaims only what is already clean.
    TIMED_OUT = "timed-out"


class PageCacheDropProblem(WireEnum):
    """Why a requested drop reclaimed nothing; a note, never a refusal."""

    NOT_PERMITTED = "not-permitted"
    DROP_FAILED = "drop-failed"
    # An earlier drop has not returned (a stuck mount holds the kernel's walk):
    # this ask answers at once instead of queueing behind it.
    DROP_IN_PROGRESS = "drop-in-progress"
    HELPER_UNAVAILABLE = "helper-unavailable"


class PageCacheDropReport(WireModel):
    """One drop: ``Cached`` from /proc/meminfo before and after it."""

    request_id: UUID
    dropped_at: AwareDatetime
    cached_before_bytes: MemoryBytes | None = None
    cached_after_bytes: MemoryBytes | None = None
    mem_available_after_bytes: MemoryBytes | None = None
    sync: PageCacheSync | None = None
    problem: PageCacheDropProblem | None = None


class PageCacheDropEvidence(WireModel):
    """A start's drops, bounded whatever the load's length."""

    drops: Annotated[int, Field(strict=True, ge=0, le=2**32 - 1)]
    problems: Annotated[int, Field(strict=True, ge=0, le=2**32 - 1)]
    reclaimed_bytes: Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
    first: PageCacheDropReport | None = None
    last: PageCacheDropReport | None = None
    last_problem: PageCacheDropReport | None = None
