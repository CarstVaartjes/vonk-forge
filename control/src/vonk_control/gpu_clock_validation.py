"""Policy owns admission; optional supported levels never gate a valid range."""

from collections.abc import Sequence

from sqlalchemy.orm import Session
from vonk_agent_protocol import RunSwitchCode
from vonk_agent_protocol.agent_words import (
    ProfileReasonSeverity,
    RunSwitchReasonScopeName,
)
from vonk_agent_protocol.gpu_clock import GPU_CLOCK_POLICY, GpuClockRange

from .run_switch_contract import RunSwitchReason


def assess_gpu_clock(
    session: Session, node_ids: Sequence[str], requested: GpuClockRange
) -> tuple[list[RunSwitchReason], list[RunSwitchReason]]:
    blockers: list[RunSwitchReason] = []
    warnings: list[RunSwitchReason] = []
    if requested.max_mhz > GPU_CLOCK_POLICY.default_max_mhz:
        warnings.append(
            RunSwitchReason(
                code=RunSwitchCode.GPU_CLOCK_ABOVE_DEFAULT,
                detail=f"GPU ceiling exceeds the {GPU_CLOCK_POLICY.default_max_mhz} MHz default; sustained load above the cap has hard-powered Sparks off. Policy ceiling: {GPU_CLOCK_POLICY.hard_max_mhz} MHz.",
                node_ids=list(node_ids),
                severity=ProfileReasonSeverity.WARNING.value,
                scope=RunSwitchReasonScopeName.NODE.value,
            )
        )
    return blockers, warnings
