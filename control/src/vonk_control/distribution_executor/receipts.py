"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from vonk_agent_protocol.agent_words import (
    ProfileChildPhase,
    ProfileEffectState,
    ProgressPhase,
)
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, WaitReason
from vonk_agent_protocol.reason_codes.fleet import DistributionCode
from vonk_agent_protocol.wire_model import OperationMemberProgress, OperationProgress
from vonk_control.agent_operation_facts import aware
from vonk_control.failure_classification import is_security_failure
from vonk_control.jobs.documents import DistributionJobPayload
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt
from vonk_control.operations.progress import aggregate_progress, project_progress
from vonk_control.run_switch_contract.evidence import RunSwitchPhase
from vonk_control.run_switch_contract.observation import RunSwitchObservedEvidence
from vonk_control.run_switch_contract.phase_results import (
    RunSwitchDistributionChildResult,
    RunSwitchDistributionEndedResult,
    RunSwitchPhaseResult,
)
from vonk_control.run_switch_contract.progress import (
    ArtifactVerificationEvidence,
    RunSwitchChildProgress,
    RunSwitchMemberReceipt,
)
from vonk_control.run_switch_contract.vocabulary import RunSwitchMemberState
from vonk_control.run_switch_operations.constants import (
    _MEMBER_STATE_ADAPTER,
    _is_artifact_phase,
)
from vonk_control.strict_json import read_stored_model


@dataclass(frozen=True, slots=True)
class RuntimeImagePull:
    """The runtime image a node pulls from the layered store."""

    image_digest: str
    config_digest: str
    address: str


@dataclass(frozen=True, slots=True)
class _ChildView:
    """Small child projection consumed by RunSwitchOperationService."""

    state: str
    result: RunSwitchDistributionChildResult | RunSwitchPhaseResult
    retry_due_at: datetime | None = None

    @property
    def progress(self) -> RunSwitchChildProgress | None:
        return getattr(self.result, "progress", None)


_PHASE_RECEIPT_ADAPTER = TypeAdapter(RunSwitchPhaseResult)


def _phase_receipt(
    value: RunSwitchPhaseResult,
    *,
    phase: RunSwitchPhase | None = None,
) -> RunSwitchPhaseResult:
    """Validate the canonical phase receipt and its accepted checkpoint."""
    try:
        receipt = _PHASE_RECEIPT_ADAPTER.validate_python(value, strict=True)
        if phase is not None:
            subphase = phase.subphase
            if subphase is None and _is_artifact_phase(phase.kind):
                subphase = ProfileChildPhase.TARGET_COPY
            if receipt.phase != phase.kind or receipt.subphase != subphase:
                raise ValueError("phase receipt belongs to a different checkpoint")
        return receipt
    except (TypeError, ValueError) as error:
        raise RuntimeError("run-switch phase receipt is invalid") from error


def _child_receipt(
    value: RunSwitchDistributionChildResult,
) -> RunSwitchDistributionChildResult:
    """The worker persists the same typed projection a reader consumes."""
    return read_stored_model(RunSwitchDistributionChildResult, value, strict=True)


def _evidence_projection(node_id: str, value: object) -> ArtifactVerificationEvidence:
    """Consume the shared partial observation without inventing missing evidence."""
    try:
        observed = read_stored_model(RunSwitchObservedEvidence, value, from_json=True)
        return ArtifactVerificationEvidence(
            node_id=node_id,
            downloaded_bytes=observed.downloaded_bytes,
            copied_bytes=observed.copied_bytes,
            error=observed.error,
            reason=observed.reason,
            uncertain=observed.uncertain,
            failure_kind=observed.failure_kind,
            error_code=observed.error_code,
            diagnostic=observed.diagnostic,
        )
    except (TypeError, ValueError):
        return ArtifactVerificationEvidence(node_id=node_id, uncertain=True)


def _member_state(value: str) -> RunSwitchMemberState:
    return _MEMBER_STATE_ADAPTER.validate_python(
        {
            LifecycleState.QUEUED.value: ProfileEffectState.PENDING.value,
            LifecycleState.RUNNING.value: LifecycleState.RUNNING.value,
            LifecycleState.SUCCEEDED.value: LifecycleState.SUCCEEDED.value,
            LifecycleState.FAILED.value: LifecycleState.FAILED.value,
        }.get(value, ProfileEffectState.UNKNOWN.value)
    )


def project_child(session: Session, child: Job, now: datetime) -> _ChildView:
    """Pure projection: missing measurements cannot reverse an issued effect."""
    try:
        payload = read_stored_model(
            DistributionJobPayload, child.payload, from_json=True
        )
    except (TypeError, ValueError):
        payload = None
    members: list[RunSwitchMemberReceipt] = []
    measured_members: list[OperationMemberProgress] = []
    evidence: list[ArtifactVerificationEvidence] = []
    next_checks: list[datetime] = []
    if payload is not None:
        for node_id in payload.cached_nodes:
            total = payload.target_totals.get(node_id)
            members.append(
                RunSwitchMemberReceipt(
                    node_id=node_id,
                    phase=ProfileChildPhase.TRANSFER.value,
                    state=LifecycleState.SUCCEEDED.value,
                    completed_bytes=total or 0,
                    total_bytes=total,
                    cached=True,
                )
            )
            measured_members.append(
                OperationMemberProgress(
                    member_id=node_id,
                    phase=ProgressPhase.TRANSFER.value,
                    state=LifecycleState.SUCCEEDED.value,
                    completed_bytes=total or 0,
                    total_bytes=total,
                )
            )
    operations = session.scalars(
        select(AgentOperation)
        .where(AgentOperation.parent_job_id == child.id)
        .order_by(AgentOperation.node_id)
    )
    for operation in operations:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == operation.current_attempt,
            )
        )
        if operation.state not in {
            LifecycleState.SUCCEEDED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }:
            # The worker will re-observe the exact order, including the
            # agent's replayed receipt; a parked member is never silent.
            due = operation.next_action_at
            if due is None and attempt is not None:
                due = attempt.lease_deadline
            next_checks.append(
                min(
                    aware(due) if due is not None else now,
                    now + timedelta(seconds=5),
                )
            )
        state = _member_state(operation.state)
        if (
            state in {ProfileEffectState.PENDING, ProfileEffectState.UNKNOWN}
            and attempt is not None
        ):
            state = _member_state(attempt.state)
        result = (attempt.result if attempt is not None else None) or {}
        observed = _evidence_projection(operation.node_id, result)
        if not result:
            observed.uncertain = True
        evidence.append(observed)
        try:
            measured = project_progress(
                read_stored_model(
                    OperationProgress,
                    (attempt.progress if attempt is not None else None) or {},
                ),
                now,
            )
        except (TypeError, ValueError):
            measured = OperationProgress(phase=ProgressPhase.TRANSFER.value)
        total = (
            payload.target_totals.get(operation.node_id)
            if payload is not None
            else None
        )
        if payload is not None:
            assignment = payload.assignments.get(operation.node_id)
            if assignment is not None:
                assigned = sum(
                    {item.sha256: item.bytes for item in assignment.objects}.values()
                )
                if assignment.node_id != operation.node_id or (
                    total is not None and assigned != total
                ):
                    total = None
                elif total is None:
                    total = assigned
        if state == LifecycleState.SUCCEEDED:
            completed = observed.downloaded_bytes or 0
            if observed.downloaded_bytes != total:
                total = None
        else:
            completed = measured.completed_bytes
            total = measured.total_bytes if measured.total_bytes is not None else total
            if total is not None and completed > total:
                total = None
        failed = state == LifecycleState.FAILED
        members.append(
            RunSwitchMemberReceipt(
                node_id=operation.node_id,
                phase=ProfileChildPhase.TRANSFER.value,
                state=state,
                completed_bytes=completed,
                total_bytes=total,
                error=observed.error or observed.reason,
                failure_kind=observed.failure_kind if failed else None,
                error_code=observed.error_code if failed else None,
                diagnostic=observed.diagnostic if failed else None,
            )
        )
        measured_members.append(
            OperationMemberProgress(
                member_id=operation.node_id,
                phase=ProgressPhase.TRANSFER.value,
                state=state,
                completed_bytes=completed,
                total_bytes=total,
                bytes_per_second=measured.bytes_per_second
                if state == LifecycleState.RUNNING
                else None,
                smoothed_bytes_per_second=measured.smoothed_bytes_per_second
                if state == LifecycleState.RUNNING
                else None,
                eta_seconds=measured.eta_seconds
                if state == LifecycleState.RUNNING
                else None,
                activity=measured.activity if state == LifecycleState.RUNNING else None,
            )
        )
    if not members:
        return _ChildView(
            state=ProfileEffectState.UNKNOWN.value,
            result=RunSwitchDistributionEndedResult(
                phase=ProfileChildPhase.TRANSFER,
                subphase=ProfileChildPhase.TARGET_COPY,
                reason=WaitReason.RECEIPT_MISSING,
                error_code=DistributionCode.UNASSIGNED,
            ),
        )
    if payload is not None:
        by_node = {member.node_id: member for member in members}
        members = [
            by_node[node_id] for node_id in payload.target_order if node_id in by_node
        ]
        if not members:
            members = list(by_node.values())
    total = (
        sum(member.total_bytes for member in members if member.total_bytes is not None)
        if all(member.total_bytes is not None for member in members)
        else None
    )
    failed = [member for member in members if member.state == LifecycleState.FAILED]
    kinds = {member.failure_kind for member in failed}
    denied = next(
        (
            member.error_code
            for member in failed
            if is_security_failure(member.error_code)
        ),
        None,
    )
    receipt = RunSwitchDistributionChildResult(
        phase=ProfileChildPhase.TRANSFER.value,
        subphase=ProfileChildPhase.TARGET_COPY.value,
        progress=RunSwitchChildProgress(
            phase=ProgressPhase.TRANSFER.value,
            completed_bytes=sum(member.completed_bytes for member in members),
            total_bytes=total,
            total_bytes_known=total is not None,
            members=members,
            operation=aggregate_progress(measured_members),
        ),
        members=members,
        evidence=evidence,
        reason=child.status_reason
        or next((member.error for member in members if member.error), None),
        failure_kind=next(iter(kinds)) if len(kinds) == 1 else None,
        error_code=denied,
        uncertain=bool(failed) or any(item.uncertain for item in evidence),
    )
    return _ChildView(
        state=child.state,
        result=_child_receipt(receipt),
        retry_due_at=max(now, min(next_checks)) if next_checks else None,
    )
