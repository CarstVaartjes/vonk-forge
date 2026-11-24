"""The durable checkpoint of a recipe-bound lifecycle admission probe round."""

from __future__ import annotations

from typing import Annotated

from pydantic import AwareDatetime, ConfigDict, Field

from vonk_agent_protocol.identifiers import NodeId
from vonk_agent_protocol.lifecycle_vocabulary import ProgressAlarmCode
from vonk_agent_protocol.reason_codes.runtime import RuntimePreflightCode
from vonk_agent_protocol.reason_codes.vocabulary import OperationReason
from vonk_control.identifiers import UuidId
from vonk_control.inventory.items_contract import PreflightInventoryItem
from vonk_control.operations.blockers import (
    MAX_BLOCKERS,
    OperationBlocker,
    make_blocker,
)
from vonk_control.strict_json import StrictJSONModel
from vonk_control.waits.contract import typed_wait_reason


class LifecyclePreflightCheckpoint(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    phase_index: int = Field(ge=0, le=31)
    pending_jobs: dict[NodeId, UuidId] = Field(default_factory=dict)
    retry_at: dict[NodeId, AwareDatetime] = Field(default_factory=dict)
    next_check_at: AwareDatetime | None = None
    last_failure_code: OperationReason | None = None
    last_failure_detail: str | None = None
    attempts: dict[NodeId, Annotated[int, Field(ge=1)]] = Field(default_factory=dict)
    receipts: dict[NodeId, PreflightInventoryItem] = Field(default_factory=dict)
    #: What the pending probes wait for now: each queued child's own persisted
    #: queue wait (its typed reason, blocking operation and Spark), replaced on
    #: every check. The reason that started the round is not the current wait.
    child_waits: list[OperationBlocker] = Field(
        default_factory=list, max_length=MAX_BLOCKERS
    )

    @property
    def current_reason(self) -> OperationReason | ProgressAlarmCode | None:
        """The reason this round waits now: a queued probe's own wait first."""
        return self.child_waits[0].code if self.child_waits else self.last_failure_code

    def current_blockers(self, node_ids: list[str]) -> list[OperationBlocker]:
        """Every dependency of this round: each queued probe's own wait, then the
        checkpoint's reason for the Sparks backing off that no probe wait names."""
        reason = typed_wait_reason(
            self.last_failure_code, RuntimePreflightCode.REQUIRED
        )
        detail = self.last_failure_detail or reason.value
        if not self.child_waits:
            return [make_blocker(reason, detail, node_ids=node_ids)]
        named = {node for blocker in self.child_waits for node in blocker.node_ids}
        backing_off = sorted(set(self.retry_at) - named)
        own = (
            [make_blocker(reason, detail, node_ids=backing_off)] if backing_off else []
        )
        return [*self.child_waits, *own][:MAX_BLOCKERS]
