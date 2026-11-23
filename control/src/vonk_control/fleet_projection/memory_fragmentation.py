"""Whether a Spark's leftover kernel page tables advise a reboot.

After every workload stop that leaves the Spark idle the agent reads
``SecPageTables`` (``MemoryFragmentation``). The page-table pages a workload
maps through the IOMMU are not freed when it exits; they are unmovable, so each
can pin a whole 2 MiB block of free memory that the GPU driver wants, and only
a reboot returns them. Compaction does heal movable scatter, so free-block
counts are no reason to reboot and are not used. This is advice: nothing here
refuses a load or acts on the Spark.
"""

from __future__ import annotations

from dataclasses import dataclass

from vonk_agent_protocol.agent_words import ProfileReasonSeverity
from vonk_agent_protocol.host_tuning import MemoryFragmentation
from vonk_agent_protocol.inventory import HostSettings
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode

from .common import ProjectionReason

#: The margin: 512 leftover page-table pages. Each can pin one 2 MiB block of
#: free memory, so 512 pages put 1 GiB at risk: under 1% of a 128 GiB Spark,
#: the order of the decode-speed cost (1 to 1.5%) reported for leftover
#: mappings. At a 4 KiB page that is 2 MiB of leftover page tables.
MARGIN_PAGES = 512


@dataclass(frozen=True)
class Fragmentation:
    #: ``SecPageTables`` at the latest idle stop minus the first of this boot
    #: (never negative).
    grown_bytes: int
    reboot_advised: bool


def assess(readings: MemoryFragmentation) -> Fragmentation:
    """First versus latest idle stop of this boot. Not latched: the advice
    clears if the latest reading drops back within the margin."""
    grown = max(0, readings.idle_stop_latest_bytes - readings.idle_stop_first_bytes)
    return Fragmentation(grown, grows_past_margin(grown, readings.page_size_bytes))


def grows_past_margin(grown_bytes: int, page_size_bytes: int) -> bool:
    return grown_bytes // page_size_bytes > MARGIN_PAGES


def memory_fragmentation_warning(host: HostSettings) -> ProjectionReason | None:
    """The reboot advice for a Spark with leftover page tables; none when unread."""
    if host.memory_fragmentation is None:
        return None
    assessed = assess(host.memory_fragmentation)
    if not assessed.reboot_advised:
        return None
    return ProjectionReason(
        code=ProjectionCode.HOST_MEMORY_FRAGMENTED,
        detail=(
            "Memory fragmented; reboot recommended (kernel page tables grew "
            f"{assessed.grown_bytes // (1024 * 1024)} MiB across idle stops since boot)."
        ),
        severity=ProfileReasonSeverity.WARNING.value,
        recommendation=(
            "Only a reboot returns these pages; reboot when no workload needs "
            "this Spark. Nothing is changed automatically."
        ),
    )
