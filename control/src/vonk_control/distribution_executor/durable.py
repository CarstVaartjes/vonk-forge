"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as AgentOperationKind
from vonk_agent_protocol import (
    DistributionCode,
    LifecycleState,
    OperationMemberProgress,
    OperationProgress,
    ProgressPhase,
    WaitReason,
)
from vonk_agent_protocol.agent_words import (
    FailureStage,
    ProfileChildPhase,
    ProfileEffectState,
)

from ..agent_jobs import abandon_idempotent_job_in_session
from ..failure_classification import is_security_failure
from ..job_documents import DistributionJobPayload
from ..models import (
    AgentOperation,
    AgentOperationAttempt,
    Job,
)
from ..operation_progress import aggregate_progress, project_progress
from ..run_switch_contract import (
    ArtifactVerificationEvidence,
    RunSwitchCachedTransferResult,
    RunSwitchChildProgress,
    RunSwitchCleanupResult,
    RunSwitchDistributionChildResult,
    RunSwitchDistributionEndedResult,
    RunSwitchMemberReceipt,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPlan,
    RunSwitchTargetTransferResult,
)
from ..run_switch_operations import PhaseExecution
from ..strict_json import read_stored_model
from .children import DistributionChildren
from .receipts import (
    _child_receipt,
    _ChildView,
    _evidence_projection,
    _phase_receipt,
)


class DurableDistributionPhaseExecutor(DistributionChildren):
    """Durable boundary for durable artifact distribution."""

    def execute(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> PhaseExecution:
        if phase.kind not in {
            ProfileChildPhase.TRANSFER.value,
            ProfileChildPhase.VERIFY.value,
        }:
            return PhaseExecution(
                result=_phase_receipt(
                    RunSwitchCleanupResult(
                        phase=ProfileChildPhase.CLEANUP.value,
                        subphase=phase.subphase,
                        scope="spark-local",
                        reclaimed_bytes=0,
                        nas_evicted=False,
                    ),
                    phase=phase,
                )
            )
        if item_index != 0:
            raise RuntimeError(f"unexpected {phase.kind} item index {item_index}")
        if phase.kind == ProfileChildPhase.VERIFY.value:
            targets = tuple(phase.node_ids)
            cached = self._cached_targets(plan, targets)
            if len(cached) == len(targets):
                return PhaseExecution(
                    result=_phase_receipt(
                        self._verification_result(
                            plan,
                            progress,
                            skipped=True,
                            cached_nodes=cached,
                            cached_target_totals={
                                node_id: self._target_bytes(plan, node_id)
                                for node_id in cached
                            },
                        ),
                        phase=phase,
                    )
                )
            return PhaseExecution(
                result=_phase_receipt(
                    self._verify_evidence(plan, progress, targets, cached), phase=phase
                )
            )
        targets = tuple(phase.node_ids)
        intent_ordinal = progress.workload_intent_ordinal
        if type(intent_ordinal) is int and intent_ordinal > 0:
            adopted = self._adopt_child(
                plan,
                phase,
                actor=actor,
                request_key=request_key,
                workload_intent_ordinal=intent_ordinal,
            )
            if adopted is not None:
                return adopted
        cached = self._cached_targets(plan, targets)
        missing = tuple(node_id for node_id in targets if node_id not in cached)
        if not missing:
            image_digest, archive_digest, _bytes, build_id = self._runtime_identity(
                plan, progress
            )
            return PhaseExecution(
                result=_phase_receipt(
                    RunSwitchCachedTransferResult(
                        phase=ProfileChildPhase.TRANSFER.value,
                        subphase=ProfileChildPhase.TARGET_COPY.value,
                        skipped=True,
                        verified=False,
                        verified_digests=list(plan.storage.artifact_digests),
                        verified_build_id=build_id,
                        verified_image_digest=image_digest,
                        verified_oci_layout_sha256=archive_digest,
                        cached_nodes=list(targets),
                        cached_target_totals={
                            node_id: self._target_bytes(plan, node_id)
                            for node_id in targets
                        },
                    ),
                    phase=phase,
                )
            )
        if type(intent_ordinal) is not int or intent_ordinal < 1:
            raise RuntimeError("distribution requires its authorized workload intent")
        model_objects, model_set_digest, model_set_bytes = self._model_objects(
            plan, progress
        )
        image_digest, layout_digest, image_bytes, build_id = self._runtime_identity(
            plan, progress
        )
        image = self._archive(
            build_id=build_id,
            image_digest=image_digest,
            layout_digest=layout_digest,
            image_bytes=image_bytes,
        )
        assignments = {
            node_id: self._assignment(
                plan,
                node_id,
                model_objects,
                image,
                model_set_digest=model_set_digest,
            )
            for node_id in missing
        }
        child_id = self._ensure_child(
            plan,
            phase,
            actor=actor,
            request_key=request_key,
            cached=cached,
            assignments=assignments,
            target_order=targets,
            # The image is pulled, not downloaded as an object; Docker fetches
            # only the layers the node lacks.
            target_bytes=model_set_bytes,
            workload_intent_ordinal=intent_ordinal,
            recovered_child_id=progress.recovery_child_operation_id,
        )
        return PhaseExecution(
            operation_id=child_id,
            result=_phase_receipt(
                RunSwitchTargetTransferResult(
                    phase=ProfileChildPhase.TRANSFER.value,
                    subphase=ProfileChildPhase.TARGET_COPY.value,
                    cached_nodes=list(cached),
                    assignments=assignments,
                ),
                phase=phase,
            ),
        )

    def abandon(
        self, session: Session, operation_id: str, now: datetime, *, reason: str
    ) -> bool:
        """Close a parked distribution child its owner cancelled.

        Idempotent and content-addressed: finished objects and partial files
        stay on the Spark for the next transfer.
        """

        child = session.get(Job, operation_id)
        if child is None or child.kind != FailureStage.ARTIFACT_DISTRIBUTION:
            return False
        return abandon_idempotent_job_in_session(session, child.id, now, reason=reason)

    def expire(
        self,
        session: Session,
        operation_id: str | None,
        now: datetime,
        *,
        plan_digest: str,
    ) -> None:
        """Fence outstanding transfer attempts without claiming remote cleanup.

        Successful effects and their exact receipts survive; queued or uncertain
        orders cease owning claims. A late receipt cannot revive an expired attempt.
        """
        if operation_id is None:
            return
        from ..agent_jobs.endings import end_unobserved_order
        from ..agent_jobs.retirement import release_owned_reservations_in_session

        child = session.get(Job, operation_id)
        if child is not None and child.kind != FailureStage.ARTIFACT_DISTRIBUTION:
            child = None
        operations = session.scalars(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == operation_id,
                AgentOperation.kind == AgentOperationKind.ARTIFACT_DISTRIBUTION,
                AgentOperation.authority_revision == plan_digest,
            )
        )
        for operation in operations:
            if operation.state in {
                LifecycleState.SUCCEEDED,
                LifecycleState.FAILED,
                LifecycleState.CANCELLED,
            }:
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
            )
            end_unobserved_order(
                self._operations,
                session,
                operation,
                attempt,
                child,
                now,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                note="distribution observation window exhausted",
            )
        release_owned_reservations_in_session(session, "job", operation_id, now)

    def get(self, operation_id: str) -> _ChildView | None:
        with self._sessions() as session:
            child = session.get(Job, operation_id)
            if child is None or child.kind != FailureStage.ARTIFACT_DISTRIBUTION:
                # This reader does not own recipe install/run/stop children.
                # An ownership miss must reach their lifecycle reader; an
                # unknown distribution observation would shadow its receipt.
                return None
            try:
                return self.project_child(session, child, self._clock())
            except (TypeError, ValueError, RuntimeError):
                return _ChildView(
                    state=ProfileEffectState.UNKNOWN.value,
                    result=RunSwitchDistributionEndedResult(
                        phase=ProfileChildPhase.TRANSFER,
                        subphase=ProfileChildPhase.TARGET_COPY,
                        reason=WaitReason.RECEIPT_MISSING,
                        error_code=DistributionCode.UNASSIGNED,
                    ),
                )

    @staticmethod
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
            state = DurableDistributionPhaseExecutor._member_state(operation.state)
            if (
                state in {ProfileEffectState.PENDING, ProfileEffectState.UNKNOWN}
                and attempt is not None
            ):
                state = DurableDistributionPhaseExecutor._member_state(attempt.state)
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
                    assigned = sum(item.bytes for item in assignment.objects)
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
                total = (
                    measured.total_bytes if measured.total_bytes is not None else total
                )
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
                    activity=measured.activity
                    if state == LifecycleState.RUNNING
                    else None,
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
                by_node[node_id]
                for node_id in payload.target_order
                if node_id in by_node
            ]
            if not members:
                members = list(by_node.values())
        total = (
            sum(
                member.total_bytes
                for member in members
                if member.total_bytes is not None
            )
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
        return _ChildView(state=child.state, result=_child_receipt(receipt))
