"""Durable Run/Switch child execution for Controller artifact distribution."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from vonk_agent_protocol.agent_operations import AgentOperation as AgentOperationKind
from vonk_agent_protocol.agent_words import (
    FailureStage,
    ProfileChildPhase,
    ProfileEffectState,
)
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, WaitReason
from vonk_agent_protocol.reason_codes.fleet import DistributionCode

from ..agent_jobs.endings import end_unobserved_order
from ..agent_jobs.retirement import release_owned_reservations_in_session
from ..agent_jobs.retry import abandon_idempotent_job_in_session
from ..models.jobs import Job
from ..models.operations import AgentOperation, AgentOperationAttempt
from ..run_switch_contract.evidence import RunSwitchPhase
from ..run_switch_contract.operations import RunSwitchOperationResult
from ..run_switch_contract.phase_results import (
    RunSwitchCachedTransferResult,
    RunSwitchDistributionEndedResult,
    RunSwitchTargetTransferResult,
)
from ..run_switch_contract.plans import RunSwitchPlan
from ..run_switch_operations.constants import _is_artifact_phase
from ..run_switch_operations.interfaces import PhaseExecution
from .children import DistributionChildren
from .receipts import (
    _ChildView,
    _phase_receipt,
    project_child,
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
        if not _is_artifact_phase(phase.kind):
            raise RuntimeError(f"unexpected {phase.kind} phase")
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
                                node_id: self._target_bytes(plan) for node_id in cached
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
                        verified_build_id=build_id,
                        verified_image_digest=image_digest,
                        verified_oci_layout_sha256=archive_digest,
                        cached_nodes=list(targets),
                        cached_target_totals={
                            node_id: self._target_bytes(plan) for node_id in targets
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
                return project_child(session, child, self._clock())
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
