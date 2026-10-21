"""GPU clock intent, hardware observations and the owner's safety policy."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .wire_model import MAX_RUN_GENERATION, WireEnum, WireModel


class GpuClockPolicy(WireModel):
    """Exported with the wire graph so native code uses these same constants."""

    default_min_mhz: Literal[300] = 300
    default_max_mhz: Literal[2200] = 2200
    hard_max_mhz: Literal[2300] = 2300


GPU_CLOCK_POLICY = GpuClockPolicy()
GPU_CLOCK_SAFETY_EDGE_DETAIL = (
    f"GPU maximum exceeds {GPU_CLOCK_POLICY.hard_max_mhz} MHz hardware-safety edge: "
    "sustained load above the cap has hard-powered Sparks off"
)


class GpuClockRange(WireModel):
    min_mhz: int = Field(ge=0, le=GPU_CLOCK_POLICY.hard_max_mhz, strict=True)
    max_mhz: int = Field(
        ge=0,
        le=GPU_CLOCK_POLICY.hard_max_mhz,
        strict=True,
        description=GPU_CLOCK_SAFETY_EDGE_DETAIL,
    )

    @field_validator("max_mhz", mode="before")
    @classmethod
    def safety_edge(cls, value: object) -> object:
        if type(value) is int and value > GPU_CLOCK_POLICY.hard_max_mhz:
            raise ValueError(GPU_CLOCK_SAFETY_EDGE_DETAIL)
        return value

    @model_validator(mode="after")
    def ordered(self) -> GpuClockRange:
        if self.min_mhz > self.max_mhz:
            raise ValueError("GPU minimum clock must not exceed maximum clock")
        return self


def default_gpu_clock() -> GpuClockRange:
    return GpuClockRange(
        min_mhz=GPU_CLOCK_POLICY.default_min_mhz,
        max_mhz=GPU_CLOCK_POLICY.default_max_mhz,
    )


class GpuClockSupport(WireModel):
    """Driver reported graphics levels, independent of the policy ceiling."""

    min_mhz: int = Field(gt=0, le=2**32 - 1)
    max_mhz: int = Field(gt=0, le=2**32 - 1)
    levels_mhz: list[Annotated[int, Field(gt=0, le=2**32 - 1)]] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> GpuClockSupport:
        if (
            self.levels_mhz != sorted(set(self.levels_mhz))
            or self.levels_mhz[0] != self.min_mhz
            or self.levels_mhz[-1] != self.max_mhz
        ):
            raise ValueError("GPU supported clock levels and range disagree")
        return self


class GpuClockIntent(WireModel):
    """A selected profile generation fences delayed requests on the Spark."""

    generation: int = Field(ge=0, le=MAX_RUN_GENERATION)
    range: GpuClockRange


class GpuClockApplyState(WireEnum):
    APPLIED = "applied"
    WAITING = "waiting"
    EXPIRED = "expired"
    REFUSED = "refused"


class GpuClockMetric(WireEnum):
    GRAPHICS = "graphics_mhz"
    SM = "sm_mhz"
    MAX_GRAPHICS = "max_graphics_mhz"
    MAX_SM = "max_sm_mhz"
    THROTTLE = "throttle_reasons"


class GpuClockUnavailableReason(WireEnum):
    NOT_SUPPORTED = "not-supported"
    COMMAND_FAILED = "command-failed"
    INVALID_OUTPUT = "invalid-output"


class GpuClockUnavailable(WireModel):
    metric: GpuClockMetric
    reason: GpuClockUnavailableReason
    detail: str


class GpuClockObservation(WireModel):
    """Unavailable readings name their cause; a successful write is not a readback."""

    unavailable: list[GpuClockUnavailable] | None = None
    receipt_detail: str | None = None
    graphics_mhz: int | None = Field(default=None, ge=0, le=2**32 - 1)
    sm_mhz: int | None = Field(default=None, ge=0, le=2**32 - 1)
    locked_range: GpuClockRange | None = None
    max_graphics_mhz: int | None = Field(default=None, gt=0, le=2**32 - 1)
    max_sm_mhz: int | None = Field(default=None, gt=0, le=2**32 - 1)
    # Driver bitmask: preserves unknown/new NVIDIA throttle reason bits.
    throttle_reasons: int | None = Field(default=None, ge=0, le=2**64 - 1)
    desired: GpuClockIntent | None = None
    apply_state: GpuClockApplyState | None = None
    detail: str | None = None
    next_attempt_at: int | None = Field(default=None, ge=0, le=2**53 - 1)
    deadline: int | None = Field(default=None, ge=0, le=2**53 - 1)


class GpuClockCheckpoint(WireModel):
    """Root-owned desired intent and bounded retry window, crash-safe on disk."""

    desired: GpuClockIntent
    state: GpuClockApplyState
    detail: str | None = None
    next_attempt_at: int | None = Field(default=None, ge=0, le=2**53 - 1)
    deadline: int = Field(ge=0, le=2**53 - 1)
    # The last command receipt is tied to this boot, never a lock readback.
    applied_boot_id: str | None = None
    applied_range: GpuClockRange | None = None
