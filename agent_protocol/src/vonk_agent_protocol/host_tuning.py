"""Spark host tuning as the agent measures it: the facts behind the host checks.

Informative only: the Controller turns these into advice with the commands an
operator runs with ``sudo``. Nothing here gates admission or a load, and the
platform never changes these settings itself. Every part is optional evidence:
a reading the agent could not take is ``None`` (unknown), never a default.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, model_validator

from vonk_agent_protocol.telemetry import COUNTER_MAX
from vonk_agent_protocol.wire_model import (
    MAX_TELEMETRY_CAPACITY_BYTES,
    WireEnum,
    WireModel,
)

# Memory in bytes, at most telemetry's capacity bound (16 TiB).
MemoryBytes = Annotated[int, Field(strict=True, ge=0, le=MAX_TELEMETRY_CAPACITY_BYTES)]
# A link rate in Mb/s, at most 1 Tb/s.
Mbps = Annotated[int, Field(ge=1, le=1_000_000)]


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

    @model_validator(mode="after")
    def _distinct(self) -> HostTuning:
        units = [item.unit for item in self.units or ()]
        if len(set(units)) != len(units):
            raise ValueError("a unit is reported twice")
        if self.snaps is not None and len(set(self.snaps)) != len(self.snaps):
            raise ValueError("a snap is reported twice")
        return self


class MemoryFragmentation(WireModel):
    """``SecPageTables`` after workload stops that left the Spark idle.

    Informative only: the Controller derives a reboot recommendation, and
    nothing acts on it. Page-table pages that a process leaves behind (PyTorch
    ``expandable_segments`` maps many IOMMU pages) are not freed when it exits,
    can each pin a large free block, and only a reboot returns them. A stop is
    idle when no GPU compute process runs after it (observed, not tracked), so
    the reading holds no workload's own tables. The readings are keyed by
    ``boot_id``; the agent starts them afresh when the boot changes.
    """

    # /proc/sys/kernel/random/boot_id.
    boot_id: Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
    # The running kernel's page size, which turns pages into blocks.
    page_size_bytes: Annotated[int, Field(strict=True, ge=1, le=2**30)]
    # ``SecPageTables`` after the first idle stop of this boot and after the
    # latest.
    idle_stop_first_bytes: MemoryBytes
    idle_stop_latest_bytes: MemoryBytes


class FabricDevice(WireModel):
    """One RDMA device's traffic over the window since the agent's last report.

    A DGX Spark reaches one cabled port through two RDMA devices; ``port`` is
    their shared PCI bus:slot.function (the PCI domain differs), which pairs
    the siblings. ``bytes`` is received plus transmitted bytes in the window.
    ``rate_mbps`` is the port's negotiated rate and ``errors`` the link and
    transport errors counted in the window; each is ``None`` when unreadable
    (for ``errors``, also when the device's error total went backwards, a
    counter reset). ``rate_mbps`` needs no window, so it is reported even when
    there is none (the first report after an agent restart, a byte counter
    reset): then ``bytes``, ``window_seconds`` and ``errors`` are all ``None``.
    They are facts, and nothing here is a
    verdict: the Controller compares siblings and the declared bandwidth.
    """

    port: Annotated[str, Field(pattern=r"^[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")]
    active: bool
    bytes: MemoryBytes | None = None
    window_seconds: Annotated[int, Field(strict=True, ge=1, le=86_400)] | None = None
    rate_mbps: Mbps | None = None
    errors: Annotated[int, Field(strict=True, ge=0, le=COUNTER_MAX)] | None = None
