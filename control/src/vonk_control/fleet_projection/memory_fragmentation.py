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
from vonk_agent_protocol.host_tuning import LARGE_BLOCK_BYTES, MemoryFragmentation
from vonk_agent_protocol.inventory import HostSettings
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode
from vonk_agent_protocol.swap import GIB

from .common import ProjectionReason

#: The large-block memory a Spark may stand to lose before it is advised to
#: reboot. One unmovable page can pin one whole large block, so ``n`` leftover
#: page-table bytes put ``n / page_size`` blocks of ``LARGE_BLOCK_BYTES`` at
#: risk. 1 GiB is under 1% of a 128 GiB Spark, the order of the decode-speed
#: cost (1 to 1.5%) reported for leftover mappings; at a 4 KiB page it takes
#: 2 MiB of leftover page tables to pass it.
AT_RISK_BYTES = GIB


@dataclass(frozen=True)
class Fragmentation:
    #: ``SecPageTables`` gained from the first idle stop of this boot to the
    #: latest (never negative).
    grown_bytes: int
    #: Large-block memory those bytes can pin.
    at_risk_bytes: int
    #: Growth is monotonic, so this holds until the boot changes.
    reboot_advised: bool


def assess(readings: MemoryFragmentation) -> Fragmentation:
    grown = max(
        0,
        readings.idle_stop_latest.sec_page_tables_bytes
        - readings.idle_stop_first.sec_page_tables_bytes,
    )
    at_risk = grown // readings.page_size_bytes * LARGE_BLOCK_BYTES
    return Fragmentation(grown, at_risk, pins_more_than_margin(at_risk))


def pins_more_than_margin(at_risk_bytes: int) -> bool:
    return at_risk_bytes > AT_RISK_BYTES


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
