"""Observed display reservation; unknown is never a memory estimate."""

from __future__ import annotations

from pydantic import Field

from .wire_model import WireEnum, WireModel


class DisplayMemorySignal(WireEnum):
    UNKNOWN = "unknown"
    MEMORY_TOTAL = "memory-total"
    RESERVED_RANGE = "reserved-range"
    EFI_VARIABLE = "efi-variable"


class DisplayMemoryObservation(WireModel):
    """Best-effort unprivileged evidence, refreshed with every inventory.

    MemTotal includes other firmware/kernel reservations. It is not itself a
    display carveout. Only a measured, platform-specific rule may classify it.
    Connected displays describe now, not what was connected at boot or Auto.
    """

    bios_version: str | None = Field(default=None, min_length=1, max_length=256)
    soc_firmware_version: int | None = Field(default=None, ge=0, le=2**32 - 1)
    display_connected: bool | None = None
    memory_total_bytes: int | None = Field(default=None, gt=0, le=16 * 1024**4)
    # Firmware-advertised RAM, not a claim about installed physical capacity.
    device_tree_memory_bytes: int | None = Field(default=None, gt=0, le=16 * 1024**4)
    carveout_bytes: int | None = Field(default=None, ge=0, le=16 * 1024**4)
    signal: DisplayMemorySignal = DisplayMemorySignal.UNKNOWN
