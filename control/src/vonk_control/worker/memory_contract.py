"""Typed memory report the worker publishes and the Controller API exports.

The worker has no HTTP endpoint, so it persists one small document on the
shared state volume and the API turns it into bounded metrics. The document is
content-free: process sizes, counts of named in-process collections, and the
code locations that hold the most Python heap when tracing was on.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import Field
from vonk_agent_protocol.wire_model import WireModel

#: One top-allocation list never exceeds this many entries.
MAX_TOP_ALLOCATIONS: Final[int] = 25
#: Longest code location kept per allocation entry.
MAX_LOCATION_LENGTH: Final[int] = 200


class WorkerMemoryComponent(StrEnum):
    """Every long-lived in-process collection whose size the worker reports."""

    MODEL_CACHE_BACKGROUND_OPERATIONS = "model_cache_background_operations"
    MODEL_CACHE_CANCEL_EVENTS = "model_cache_cancel_events"
    MODEL_CACHE_PROGRESS_CHECKPOINTS = "model_cache_progress_checkpoints"
    MODEL_CACHE_REVERIFIED = "model_cache_reverified"
    IMAGE_PREPARATION_FUTURES = "image_preparation_futures"
    IMAGE_PREPARATION_IDENTITY_LOCKS = "image_preparation_identity_locks"
    RUNTIME_IMAGE_FUTURES = "runtime_image_futures"
    STORAGE_DEMANDS = "storage_demands"
    STORAGE_COLLECTION_ROUNDS = "storage_collection_rounds"


class WorkerMemoryCgroupKind(StrEnum):
    """Cgroup memory classes that tell anonymous heap from file cache and tmpfs."""

    CURRENT = "current"
    ANON = "anon"
    FILE = "file"
    SHMEM = "shmem"
    KERNEL = "kernel"


class WorkerTraceState(StrEnum):
    """Where on-demand allocation tracing is."""

    OFF = "off"
    SAMPLING = "sampling"
    CAPTURED = "captured"


class WorkerMemoryComponentSize(WireModel):
    component: WorkerMemoryComponent
    entries: Annotated[int, Field(strict=True, ge=0)]


class WorkerMemoryCgroupSize(WireModel):
    kind: WorkerMemoryCgroupKind
    bytes: Annotated[int, Field(strict=True, ge=0)]


class WorkerTopAllocation(WireModel):
    """One code location, by Python-heap bytes still held when traced."""

    location: Annotated[str, Field(min_length=1, max_length=MAX_LOCATION_LENGTH)]
    size_bytes: Annotated[int, Field(strict=True, ge=0)]
    count: Annotated[int, Field(strict=True, ge=0)]
    #: Bytes gained since tracing started; the leak candidates sort first.
    growth_bytes: Annotated[int, Field(strict=True)]


class WorkerTraceReport(WireModel):
    state: WorkerTraceState
    #: Process RSS that made tracing start; absent when an operator asked for it.
    started_at_rss_bytes: Annotated[int, Field(strict=True, ge=0)] | None = None
    captured_at: datetime | None = None
    traced_bytes: Annotated[int, Field(strict=True, ge=0)] | None = None
    peak_traced_bytes: Annotated[int, Field(strict=True, ge=0)] | None = None
    top: Annotated[
        tuple[WorkerTopAllocation, ...], Field(max_length=MAX_TOP_ALLOCATIONS)
    ] = ()


class WorkerMemoryReport(WireModel):
    schema_version: Literal[1]
    recorded_at: datetime
    rss_bytes: Annotated[int, Field(strict=True, ge=0)]
    peak_rss_bytes: Annotated[int, Field(strict=True, ge=0)] | None = None
    #: Resident bytes of child processes (skopeo, tar, ...), counted apart.
    children_rss_bytes: Annotated[int, Field(strict=True, ge=0)] | None = None
    threads: Annotated[int, Field(strict=True, ge=0)]
    #: Live Python allocator blocks; grows with the heap, not with malloc slack.
    python_allocated_blocks: Annotated[int, Field(strict=True, ge=0)]
    cgroup: tuple[WorkerMemoryCgroupSize, ...] = ()
    components: tuple[WorkerMemoryComponentSize, ...] = ()
    trace: WorkerTraceReport
