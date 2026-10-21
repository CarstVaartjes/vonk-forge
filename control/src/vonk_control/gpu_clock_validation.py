"""One driver-bound validation decision for profile Run/Switch admission."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import RunSwitchCode, canonical_message
from vonk_agent_protocol.agent_words import (
    ProfileReasonSeverity,
    RunSwitchReasonScopeName,
)
from vonk_agent_protocol.gpu_clock import GPU_CLOCK_POLICY, GpuClockRange
from vonk_agent_protocol.inventory import HostSettings

from .models import NodeInventorySnapshot
from .run_switch_contract import RunSwitchReason


def assess_gpu_clock(
    session: Session, node_ids: Sequence[str], requested: GpuClockRange
) -> tuple[list[RunSwitchReason], list[RunSwitchReason]]:
    blockers: list[RunSwitchReason] = []
    warnings: list[RunSwitchReason] = []
    for node_id in node_ids:
        snapshot = session.scalar(
            select(NodeInventorySnapshot)
            .where(NodeInventorySnapshot.node_id == node_id)
            .order_by(NodeInventorySnapshot.observed_at.desc())
            .limit(1)
        )
        try:
            host = (
                None
                if snapshot is None or snapshot.host_settings is None
                else HostSettings.model_validate_json(
                    canonical_message(snapshot.host_settings)
                )
            )
            support = None if host is None else host.gpu_clock_support
            if support is None:
                raise ValueError(
                    "GPU supported clock range is unknown; update the agent or wait for inventory"
                )
            support.validate_range(requested)
        except ValueError as error:
            blockers.append(
                RunSwitchReason(
                    code=RunSwitchCode.GPU_CLOCK_INVALID,
                    detail=str(error),
                    node_ids=[node_id],
                    severity=ProfileReasonSeverity.BLOCKER.value,
                    scope=RunSwitchReasonScopeName.NODE.value,
                )
            )
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
