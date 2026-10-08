"""Measured Run/Switch progress aggregation and projection."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel
from vonk_agent_protocol import (
    OperationProgress,
    canonical_message,
)

from .lifecycle.evidence import (
    BookkeepingReason,
    retire_as_unknown,
)
from .lifecycle.run_switch import (
    set_member_state,
)
from .operation_progress import project_progress
from .run_switch_contract import (
    RunSwitchMemberProgress,
    RunSwitchMemberReceipt,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPlan,
    RunSwitchProgress,
)
from .run_switch_observation_contract import (
    RunSwitchObservedEvidence,
)


def _merge_progress_evidence(
    progress: RunSwitchOperationResult,
    plan: RunSwitchPlan,
    phase: RunSwitchPhase,
    evidence: object,
    now: datetime | None = None,
) -> None:
    """Merge typed measured observations while retaining exact phase ownership."""
    from .run_switch_operations import (
        _observe_progress,
        _plan_target_node_ids,
        _planned_transfer_parts,
    )

    if isinstance(evidence, RunSwitchObservedEvidence):
        payload = evidence
    else:
        try:
            raw = (
                evidence.model_dump(mode="json")
                if isinstance(evidence, BaseModel)
                else evidence
            )
            payload = RunSwitchObservedEvidence.model_validate_json(
                canonical_message(raw), strict=True
            )
        except (TypeError, ValueError):
            return
    if payload.progress is not None:
        _merge_progress_evidence(progress, plan, phase, payload.progress, now)
    if payload.operation is not None:
        progress.operation = payload.operation
        progress.operation_phase_index = phase.index
    reported_completed = next(
        (
            value
            for value in (
                payload.completed_bytes,
                payload.copied_bytes,
                payload.downloaded_bytes,
            )
            if value is not None
        ),
        None,
    )
    # Native install/build measurements belong to their phase's OperationProgress.
    # Only transfer evidence can advance the accepted distribution byte budget.
    if reported_completed is not None:
        if phase.kind == "transfer":
            model_download, _, _ = _planned_transfer_parts(plan)
            offset = (
                model_download
                if phase.subphase == "target-copy" and model_download is not None
                else 0
            )
            progress.completed_bytes = max(
                progress.completed_bytes, offset + reported_completed
            )
        if now is not None and payload.progress is None and payload.operation is None:
            prior = progress.operation
            completed = (
                max(prior.completed_bytes, reported_completed)
                if prior is not None and prior.phase == phase.kind
                else reported_completed
            )
            current = OperationProgress(
                phase=phase.kind,
                completed_bytes=completed,
                total_bytes=payload.total_bytes,
                total_bytes_known=payload.total_bytes is not None,
            )
            progress.operation = _observe_progress(prior, current, now)
            progress.operation_phase_index = phase.index
    if (
        phase.kind == "transfer"
        and payload.total_bytes is not None
        and progress.total_bytes is None
    ):
        model_download, _, _ = _planned_transfer_parts(plan)
        if phase.subphase == "target-copy" and model_download is not None:
            progress.total_bytes = model_download + payload.total_bytes
            progress.total_bytes_known = True
    known_nodes = set(_plan_target_node_ids(plan))
    existing = {
        item.node_id: item for item in progress.members if item.node_id in known_nodes
    }
    for item in payload.members:
        if item.node_id not in known_nodes:
            continue
        prior = existing.get(item.node_id)
        target = item.model_copy(deep=True)
        if prior is not None:
            target.completed_bytes = max(prior.completed_bytes, item.completed_bytes)
            if target.total_bytes is None:
                target.total_bytes = prior.total_bytes
            if target.phase is None:
                target.phase = prior.phase
        if target.error is not None:
            target.error = target.error[:256]
        existing[item.node_id] = target
    progress.members = list(existing.values())


def _complete_phase_progress(
    progress: RunSwitchOperationResult, plan: RunSwitchPlan, phase: RunSwitchPhase
) -> None:
    from .run_switch_operations import _planned_transfer_bytes, _planned_transfer_parts

    progress.retry_attempt = None
    if phase.kind != "transfer":
        return
    model_download, target_copy, aggregate = _planned_transfer_parts(plan)
    if phase.subphase == "model-download":
        if model_download is not None:
            progress.completed_bytes = max(progress.completed_bytes, model_download)
        return
    if phase.subphase != "target-copy":
        return
    _, member_totals = _planned_transfer_bytes(plan)
    if aggregate is not None:
        progress.completed_bytes = aggregate
        progress.total_bytes = aggregate
        progress.total_bytes_known = True
    elif target_copy is not None:
        progress.completed_bytes = max(
            progress.completed_bytes, target_copy + (model_download or 0)
        )
    entries = {item.node_id: item for item in progress.members}
    for node_id, total in member_totals.items():
        item = entries.setdefault(
            node_id, RunSwitchMemberReceipt(node_id=node_id, state="pending")
        )
        if total is not None:
            item.total_bytes = total
            item.completed_bytes = total
        item.phase = phase.kind
        set_member_state(item, "succeeded")
        item.error = None
    progress.members = list(entries.values())


def _complete_operation_progress(
    plan: RunSwitchPlan, progress: RunSwitchOperationResult
) -> RunSwitchOperationResult:
    from .run_switch_operations import _planned_transfer_bytes

    total, member_totals = _planned_transfer_bytes(plan)
    if total is not None:
        progress.completed_bytes = total
        progress.total_bytes = total
        progress.total_bytes_known = True
    entries = {item.node_id: item for item in progress.members}
    for node_id, member_total in member_totals.items():
        item = entries.setdefault(
            node_id, RunSwitchMemberReceipt(node_id=node_id, state="pending")
        )
        if member_total is not None:
            item.total_bytes = member_total
            item.completed_bytes = member_total
        item.phase = "final_verify"
        set_member_state(item, "succeeded")
        item.error = None
    progress.members = list(entries.values())
    progress.phase = "final_verify"
    progress.subphase = None
    progress.retryable = False
    progress.failed_phase = None
    progress.failure_code = None
    progress.child_operation_id = None
    return progress


def _progress_view(
    plan: RunSwitchPlan | None,
    raw: RunSwitchOperationResult,
    operation_state: str,
    status_reason: str | None,
    node_ids: Sequence[str] | None = None,
) -> RunSwitchProgress:
    """Project durable typed progress without advancing the operation."""
    from .run_switch_operations import (
        _UNKNOWN_MEMBER_NODE_ID,
        _plan_target_node_ids,
        _planned_transfer_bytes,
        _progress_operation_state,
    )

    node_ids = (
        list(_plan_target_node_ids(plan))
        if plan is not None
        else list(node_ids or [item.node_id for item in raw.members])
    )
    phase_count = max(1, len(plan.phases)) if plan is not None else 1
    phase_index = min(raw.phase_index, 31) if plan is not None else 0
    phase = raw.phase
    subphase = raw.subphase
    if plan is not None and phase_index < len(plan.phases):
        phase = phase or plan.phases[phase_index].kind
        subphase = subphase or plan.phases[phase_index].subphase
    total, member_totals = (
        _planned_transfer_bytes(plan) if plan is not None else (None, {})
    )
    total = total if total is not None else raw.total_bytes
    state = _progress_operation_state(operation_state)
    if state == "succeeded":
        phase, subphase = "final_verify", None
        phase_index = min(max(phase_index, phase_count - 1), 31)
    completed = raw.completed_bytes
    if state == "succeeded" and total is not None:
        completed = total
    elif plan is not None and total is not None:
        completed = min(completed, total)
    raw_members = {
        item.node_id: item for item in raw.members if item.node_id in node_ids
    }
    current_nodes = (
        set(plan.phases[phase_index].node_ids)
        if plan is not None and phase_index < len(plan.phases)
        else set()
    )
    members = []
    for node_id in node_ids:
        item = raw_members.get(node_id)
        member_total = (
            item.total_bytes
            if item is not None and item.total_bytes is not None
            else member_totals.get(node_id)
        )
        member_completed = item.completed_bytes if item is not None else 0
        if member_total is not None:
            member_completed = min(member_completed, member_total)
        member_state = item.state if item is not None else None
        if member_state is None:
            member_state = (
                "succeeded"
                if state == "succeeded"
                else "failed"
                if state == "failed" and node_id in current_nodes
                else "running"
                if state == "running" and node_id in current_nodes
                else "pending"
                if state == "queued" or state == "running" and raw.completed_phases
                else "unknown"
            )
        error = item.error if item is not None else None
        if state == "failed" and node_id in current_nodes and error is None:
            error = status_reason[:256] if status_reason is not None else None
        members.append(
            RunSwitchMemberProgress(
                node_id=node_id,
                phase=item.phase
                if item is not None and item.phase is not None
                else phase,
                state=member_state,
                completed_bytes=member_completed,
                total_bytes=member_total,
                error=error,
            )
        )
    if not members:
        retire_as_unknown(
            "run-switch.progress",
            "no-target-members",
            BookkeepingReason.ROW_INCOMPLETE,
            "operation has no recorded target members",
        )
        members.append(
            RunSwitchMemberProgress(
                node_id=_UNKNOWN_MEMBER_NODE_ID,
                phase=phase,
                state="unknown",
                error="no target members are recorded for this operation",
            )
        )
    measurement = raw.operation
    if measurement is not None:
        measuring_preflight = (
            raw.preflight is not None and raw.preflight.pending_job_id is not None
        )
        if (
            raw.operation_phase_index != phase_index and not measuring_preflight
        ) or state not in {"queued", "running"}:
            measurement = measurement.model_copy(
                update={
                    "phase": phase or "unknown",
                    "bytes_per_second": None,
                    "smoothed_bytes_per_second": None,
                    "eta_seconds": None,
                    "activity": None,
                }
            )
        else:
            measurement = project_progress(measurement)
    return RunSwitchProgress(
        operation=measurement,
        startup_budget_seconds=raw.startup_budget_seconds,
        start_deadline=raw.start_deadline,
        phase_index=phase_index,
        phase_count=phase_count,
        phase=phase,
        state=state,
        completed_bytes=completed,
        total_bytes=total,
        total_bytes_known=total is not None,
        subphase=subphase,
        members=members,
    )
