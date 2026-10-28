"""Spark swap size: the owner's policy, the desired intent and what the helper did.

Swap is a load-time cushion, never memory capacity: admission keeps counting
only the physical pool. The helper keeps one swap file of its own beside the
operating system's (``/swap.img``), sized so all active swap reaches the
desired total; it never touches swap it does not own.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from .wire_model import MAX_RUN_GENERATION, WireEnum, WireModel

GIB = 1024**3


class SwapPolicy(WireModel):
    """Exported with the wire graph so native code uses these same constants."""

    # Today's Ubuntu /swap.img; a smaller total is refused, never clamped.
    minimum_gib: Literal[16] = 16
    # Owner 2026-10-28: creators run 36-48 GB per node for the biggest loads.
    default_gib: Literal[64] = 64
    # The whole unified pool of one Spark.
    maximum_gib: Literal[128] = 128
    # Sizes are whole GiB: a smaller shortfall is only swap-area header pages
    # (/swap.img's 16 GiB reports 16 GiB minus one page), never a reason for a
    # swap file.
    tolerance_mib: Literal[512] = 512
    # An old managed swap file is released only while its pages in use plus
    # this much stay available in memory; otherwise it stays active (more swap,
    # never less) until they fit.
    release_headroom_gib: Literal[16] = 16


SWAP_POLICY = SwapPolicy()
SWAP_SIZE_DETAIL = (
    f"Swap size must be a whole number of GiB from {SWAP_POLICY.minimum_gib} "
    f"to {SWAP_POLICY.maximum_gib}"
)


class SwapIntent(WireModel):
    """A selected profile generation fences delayed requests on the Spark."""

    generation: int = Field(ge=0, le=MAX_RUN_GENERATION)
    size_gib: int = Field(
        ge=SWAP_POLICY.minimum_gib,
        le=SWAP_POLICY.maximum_gib,
        strict=True,
        description=SWAP_SIZE_DETAIL,
    )

    @field_validator("size_gib", mode="before")
    @classmethod
    def within_policy(cls, value: object) -> object:
        if type(value) is int and not (
            SWAP_POLICY.minimum_gib <= value <= SWAP_POLICY.maximum_gib
        ):
            raise ValueError(SWAP_SIZE_DETAIL)
        return value


def default_swap(generation: int = 0) -> SwapIntent:
    return SwapIntent(generation=generation, size_gib=SWAP_POLICY.default_gib)


class SwapApplyState(WireEnum):
    APPLIED = "applied"
    WAITING = "waiting"
    EXPIRED = "expired"


class SwapWaitReason(WireEnum):
    """Why the desired swap is not (all) active yet; each one retries."""

    # Creating the file would leave the filesystem short of space.
    INSUFFICIENT_DISK = "insufficient-disk"
    # fallocate, mkswap or swapon failed; the detail carries its account.
    COMMAND_FAILED = "command-failed"
    # Creating, publishing or removing a managed swap file failed.
    FILE_FAILED = "file-failed"
    # A kernel or filesystem reading (active swap, boot id, free space) failed.
    HOST_UNREADABLE = "host-unreadable"
    # Releasing an old swap file would pull more pages into memory than the
    # host can spare; it stays active (more swap than desired) until it can.
    RELEASE_DEFERRED = "release-deferred"


class SwapCheckpoint(WireModel):
    """Root-owned desired intent and bounded retry window, crash-safe on disk.

    The helper publishes the same record read-only for the agent.
    """

    desired: SwapIntent
    state: SwapApplyState
    reason: SwapWaitReason | None = None
    detail: str | None = Field(default=None, max_length=2048)
    next_attempt_at: int | None = Field(default=None, ge=0, le=2**53 - 1)
    deadline: int = Field(ge=0, le=2**53 - 1)
    # The boot the managed swap was last activated on; a reboot re-applies.
    applied_boot_id: str | None = Field(default=None, max_length=64)


class SwapObservation(WireModel):
    """Active swap as the kernel reports it, beside the helper's last receipt."""

    total_bytes: int = Field(ge=0, le=16 * 1024**4)
    free_bytes: int = Field(ge=0, le=16 * 1024**4)
    # The helper's receipt; None with receipt_detail naming why it is missing.
    receipt: SwapCheckpoint | None = None
    receipt_detail: str | None = Field(default=None, max_length=512)


def swap_satisfied(total_bytes: int, desired: SwapIntent) -> bool:
    """Active swap reaches the desired total, within the policy tolerance."""
    return total_bytes + SWAP_POLICY.tolerance_mib * 1024**2 >= desired.size_gib * GIB
