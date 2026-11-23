"""Whether a Spark's memory is fragmented enough that a reboot is advised.

The agent reads ``SecPageTables`` and free memory by block size around every
workload start and stop (``MemoryFragmentation``). The kernel page-table pages
a workload maps through the IOMMU are not freed when it exits; they are
unmovable, so each can pin a whole 2 MiB block of free memory that the GPU
driver wants, and only a reboot returns them. This turns the readings into one
of four states. It is advice: nothing here refuses a load or acts on the Spark.
"""

from __future__ import annotations

from enum import StrEnum

from vonk_agent_protocol.agent_words import ProfileReasonSeverity
from vonk_agent_protocol.host_tuning import MemoryFragmentation, MemoryPoint
from vonk_agent_protocol.inventory import HostSettings
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode
from vonk_agent_protocol.swap import GIB

from .common import ProjectionReason

#: The physically contiguous block size the GPU driver takes (the agent's
#: ``buddyinfo::LARGE_BLOCK_BYTES``).
LARGE_BLOCK_BYTES = 2 * 1024 * 1024

#: The large-block memory a Spark may stand to lose before it is advised to
#: reboot. One unmovable page can pin one whole large block, so the memory at
#: risk from ``n`` leftover page-table bytes is ``n / page_size`` blocks of
#: ``LARGE_BLOCK_BYTES``. 1 GiB is under 1% of a 128 GiB Spark, the order of the
#: decode-speed cost (1 to 1.5%) reported for leftover mappings; at a 4 KiB page
#: it takes 2 MiB of leftover page tables to reach it, and the same growth
#: matters less at a larger page size, where one page pins no more than a block.
AT_RISK_BYTES = GIB


class Fragmentation(StrEnum):
    #: Not enough readings of this boot to say (the first stops of a boot, a
    #: reading the agent could not take, or a stop with another workload up).
    UNKNOWN = "unknown"
    STEADY = "steady"
    #: Idle-stop ``SecPageTables`` grew since the first idle stop of this boot
    #: by enough to pin ``AT_RISK_BYTES`` of large blocks. A reboot is the only
    #: remedy, so it holds until the boot changes.
    PAGE_TABLES_GREW = "page-tables-grew"
    #: What the latest idle stop freed did not come back as 2 MiB blocks.
    LARGE_BLOCKS_SHORT = "large-blocks-short"


def at_risk_bytes(page_table_bytes: int, page_size_bytes: int) -> int:
    """Large-block memory that ``page_table_bytes`` of unmovable pages can pin."""
    return page_table_bytes // page_size_bytes * LARGE_BLOCK_BYTES


def assess(readings: MemoryFragmentation | None) -> Fragmentation:
    if readings is None:
        return Fragmentation.UNKNOWN
    grown = _page_table_growth(readings)
    if (
        grown is not None
        and at_risk_bytes(grown, readings.page_size_bytes) > AT_RISK_BYTES
    ):
        return Fragmentation.PAGE_TABLES_GREW
    short = _large_blocks_short(readings)
    if short is not None and short > AT_RISK_BYTES:
        return Fragmentation.LARGE_BLOCKS_SHORT
    if grown is None and short is None:
        return Fragmentation.UNKNOWN
    return Fragmentation.STEADY


def _page_table_growth(readings: MemoryFragmentation) -> int | None:
    first, latest = readings.idle_stop_first, readings.idle_stop_latest
    if (
        first is None
        or latest is None
        or latest.at <= first.at
        or first.sec_page_tables_bytes is None
        or latest.sec_page_tables_bytes is None
    ):
        return None
    return max(0, latest.sec_page_tables_bytes - first.sec_page_tables_bytes)


def _large_blocks_short(readings: MemoryFragmentation) -> int | None:
    before, after = readings.stop_before, readings.stop_after
    # Only the stop that left the Spark idle: another workload's memory comes
    # and goes between the two readings of a busy one.
    if before is None or after is None or after != readings.idle_stop_latest:
        return None
    freed = _gain(before, after, "free_bytes")
    large = _gain(before, after, "free_2mib_block_bytes")
    if freed is None or large is None:
        return None
    return max(0, freed - large)


def _gain(before: MemoryPoint, after: MemoryPoint, field: str) -> int | None:
    if before.free_memory is None or after.free_memory is None:
        return None
    return getattr(after.free_memory, field) - getattr(before.free_memory, field)


def memory_fragmentation_warning(host: HostSettings) -> ProjectionReason | None:
    """The reboot advice for a fragmented Spark; unknown is never a warning."""
    readings = host.memory_fragmentation
    state = assess(readings)
    if readings is None or state not in {
        Fragmentation.PAGE_TABLES_GREW,
        Fragmentation.LARGE_BLOCKS_SHORT,
    }:
        return None
    if state is Fragmentation.PAGE_TABLES_GREW:
        grown = _page_table_growth(readings) or 0
        cause = (
            f"kernel page tables grew {_mib(grown)} MiB across idle stops since boot"
        )
    else:
        cause = (
            f"{_mib(_large_blocks_short(readings) or 0)} MiB freed by the last stop "
            "is not in 2 MiB blocks"
        )
    return ProjectionReason(
        code=ProjectionCode.HOST_MEMORY_FRAGMENTED,
        detail=f"Memory fragmented; reboot recommended ({cause}).",
        severity=ProfileReasonSeverity.WARNING.value,
        recommendation=(
            "Only a reboot returns these pages; reboot when no workload needs "
            "this Spark. Nothing is changed automatically."
        ),
    )


def _mib(value: int) -> int:
    return value // (1024 * 1024)
