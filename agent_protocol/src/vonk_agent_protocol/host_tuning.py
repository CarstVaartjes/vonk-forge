"""Spark host tuning as the agent measures it: the facts behind the host checks.

Informative only: the Controller turns these into advice with the commands an
operator runs with ``sudo``. Nothing here gates admission or a load, and the
platform never changes these settings itself. Every part is optional evidence:
a reading the agent could not take is ``None`` (unknown), never a default.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AwareDatetime, Field, model_validator

from .wire_model import MAX_TELEMETRY_CAPACITY_BYTES, WireEnum, WireModel

# Memory in bytes, at most telemetry's capacity bound (16 TiB).
MemoryBytes = Annotated[int, Field(strict=True, ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)]


class HostUnit(WireEnum):
    """The systemd units the host checks look at; the agent asks for each."""

    BLUETOOTH = "bluetooth.service"
    CUPS = "cups.service"
    CUPS_SOCKET = "cups.socket"
    CUPS_PATH = "cups.path"
    CUPS_BROWSED = "cups-browsed.service"
    SNAPD = "snapd.service"
    SNAPD_SOCKET = "snapd.socket"
    SNAPD_SEEDED = "snapd.seeded.service"
    SNAPD_SNAP_REPAIR = "snapd.snap-repair.timer"
    DGX_DASHBOARD = "dgx-dashboard.service"
    DGX_DASHBOARD_ADMIN = "dgx-dashboard-admin.service"
    FWUPD_REFRESH = "fwupd-refresh.timer"


class InstalledUnit(WireModel):
    """One listed unit installed on the Spark (systemd LoadState ``loaded``)."""

    unit: HostUnit
    active: bool
    # systemd's MemoryCurrent for the unit's cgroup; None when systemd does not
    # account it (a timer, a socket, accounting off).
    memory_bytes: MemoryBytes | None = None


#: A snap's name (snapd: lowercase letters, digits and hyphens, at most 40).
SnapName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")]
MAX_REPORTED_SNAPS = 128


class FreeMemory(WireModel):
    """Free memory from /proc/buddyinfo: all of it, and the part in blocks of
    at least 2 MiB, the physically contiguous size the GPU driver wants."""

    free_bytes: MemoryBytes
    free_2mib_block_bytes: MemoryBytes

    @model_validator(mode="after")
    def _within_free(self) -> FreeMemory:
        if self.free_2mib_block_bytes > self.free_bytes:
            raise ValueError("free memory in 2 MiB blocks exceeds free memory")
        return self


class HostTuning(WireModel):
    """The measured host settings the host checks explain."""

    # The listed units installed on this Spark, in HostUnit order; a unit that
    # is not installed (or masked) is absent. None when systemd gave no answer.
    units: list[InstalledUnit] | None = Field(default=None, max_length=len(HostUnit))
    # Installed snaps by name, sorted; None when /snap is absent or unreadable.
    snaps: list[SnapName] | None = Field(default=None, max_length=MAX_REPORTED_SNAPS)
    # /proc/sys/vm/watermark_boost_factor.
    watermark_boost_factor: int | None = Field(default=None, ge=0, le=2**31 - 1)
    # /proc/sys/vm/compaction_proactiveness (kernel range 0..100).
    compaction_proactiveness: int | None = Field(default=None, ge=0, le=100)
    # The kernel command line has the word ``splash``.
    kernel_splash: bool | None = None
    free_memory: FreeMemory | None = None
    # /proc/meminfo SecPageTables: the IOMMU and secondary-MMU page tables.
    sec_page_tables_bytes: MemoryBytes | None = None

    @model_validator(mode="after")
    def _distinct(self) -> HostTuning:
        units = [item.unit for item in self.units or ()]
        if len(set(units)) != len(units):
            raise ValueError("a unit is reported twice")
        if self.snaps is not None and len(set(self.snaps)) != len(self.snaps):
            raise ValueError("a snap is reported twice")
        return self


class MemoryPoint(WireModel):
    """The memory fragmentation readings at one lifecycle point: the kernel's
    secondary page tables (``SecPageTables``) and free memory by block size."""

    at: AwareDatetime
    sec_page_tables_bytes: MemoryBytes | None = None
    free_memory: FreeMemory | None = None


class MemoryFragmentation(WireModel):
    """What the agent read around workload loads and stops since this boot.

    Informative only: the Controller derives a reboot recommendation, and
    nothing acts on it. Page-table pages that a process leaves behind (PyTorch
    ``expandable_segments`` maps many IOMMU pages) are not freed when it exits,
    fragment memory, and only a reboot returns them, so the readings are keyed
    by ``boot_id`` and the agent starts them afresh when the boot changes.
    Every reading is optional evidence: one that could not be taken is absent.
    """

    # /proc/sys/kernel/random/boot_id.
    boot_id: Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
    # The running kernel's page size, which turns pages into blocks.
    page_size_bytes: Annotated[int, Field(strict=True, ge=1, le=2**30)]
    # Around the latest workload start and the latest workload stop.
    load_before: MemoryPoint | None = None
    load_after: MemoryPoint | None = None
    stop_before: MemoryPoint | None = None
    stop_after: MemoryPoint | None = None
    # The after-stop readings taken while no other workload was resident on the
    # Spark (a co-resident workload holds its own page tables), the first of
    # this boot and the latest.
    idle_stop_first: MemoryPoint | None = None
    idle_stop_latest: MemoryPoint | None = None
