"""Typed Activity projections of Run/Switch evidence."""

from __future__ import annotations

from vonk_agent_protocol import (
    FailureCode,
    LifecycleEffect,
    LifecycleState,
    OperationCheckpoint,
    OperationMemberProgress,
    OperationProgress,
)

from ..operation_item_contract import OperationResultFacts
from ..run_switch_contract import RunSwitchOperation
from ..strict_json import read_stored_model


def _activity_progress(operation: RunSwitchOperation) -> OperationProgress:
    raw = operation.progress
    if raw.operation is not None:
        return raw.operation
    generic_phase = raw.phase or LifecycleEffect.UNKNOWN.value
    return OperationProgress(
        phase=generic_phase,
        completed_bytes=raw.completed_bytes,
        total_bytes=raw.total_bytes,
        total_bytes_known=raw.total_bytes_known,
        members=[
            OperationMemberProgress(
                member_id=item.node_id,
                phase=item.phase or generic_phase,
                completed_bytes=item.completed_bytes,
                total_bytes=item.total_bytes,
                state=item.state,
            )
            for item in raw.members
        ],
        checkpoint=OperationCheckpoint(
            key="run-switch-phase",
            sequence=raw.phase_index,
            digest=operation.plan_digest,
        ),
    )


def _activity_result(operation: RunSwitchOperation) -> OperationResultFacts | None:
    result = (
        read_stored_model(
            OperationResultFacts, operation.result.model_dump(mode="json")
        )
        if operation.result is not None
        else None
    )
    if operation.state == LifecycleState.FAILED:
        result = result or OperationResultFacts()
        return OperationResultFacts.model_validate(
            {
                "error_code": FailureCode.OPERATION_FAILED,
                "summary": operation.status_reason or "Run/Switch operation failed",
                "detail": operation.status_reason,
                "retryable": operation.result.retryable
                if operation.result is not None
                else False,
                "uncertain": False,
            }
        )
    return result
