"""Read-only projection of the queue owner's canonical persisted wait."""

from __future__ import annotations

from collections.abc import Sequence

from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
from vonk_control.models.operations import AgentOperation
from vonk_control.operations.item_contract import OperationItem
from vonk_control.waits.contract import QueueWait
from vonk_control.waits.persisted import read_wait


def apply_queue_wait(item: OperationItem, wait: QueueWait | None) -> OperationItem:
    if wait is not None:
        item.wait = wait
        item.wait_reason = wait.reason if isinstance(wait.reason, WaitReason) else None
        item.blockers = wait.blockers
        item.next_attempt_at = wait.next_check.isoformat()
        if item.status_reason is None:
            item.status_reason = wait.blockers[0].detail
    return item


def spark_busy_blockers(operations: Sequence[AgentOperation]) -> dict[str, QueueWait]:
    return {
        operation.id: wait
        for operation in operations
        if (wait := read_wait(operation)) is not None
    }
