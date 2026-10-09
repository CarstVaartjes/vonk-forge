"""Run switch journal repair: proof."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    canonical_message,
)

from ..content_identity import same_image
from ..job_documents import RecipeInstallParent
from ..models import (
    AgentCertificate,
    AgentOperation,
    AgentOperationAttempt,
    FleetProfileApplication,
    Job,
)
from ..operation_progress import stored_progress
from ..recipe_operations import current_recipe_progress_attribution
from ..run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchRuntimeImageResult,
    RunSwitchRuntimeInstallResult,
    RunSwitchRuntimePlanResult,
    RunSwitchTargetTransferEvidenceResult,
    RunSwitchVerifyResult,
)
from ..run_switch_journal_contract import (
    JournalRepairPurpose,
    NativeProgressWitness,
)
from ..stored_json import read_row_column


def _prove(
    session: Session,
    job: Job,
    progress: RunSwitchOperationResult,
    now: datetime,
    purpose: JournalRepairPurpose,
) -> list[NativeProgressWitness] | None:
    # Import the sole owner predicates, not a second plan/authority interpreter.
    from ..fleet_profiles import FleetProfileService, _stored_progress
    from ..lifecycle.evidence import Residue
    from ..run_switch_operations import (
        RecipeLifecyclePhaseExecutor,
        RunSwitchOperationService,
        _digest,
        _phase_request_key,
        _plan_target_node_ids,
        _planned_transfer_parts,
        _run_switch_payload,
        _validate_artifact_execution,
    )

    parent = _run_switch_payload(job)
    plan = parent.plan if parent is not None else None
    if (
        parent is None
        or plan is None
        or not plan.allowed
        or parent.operation_kind != job.kind
        or parent.action != plan.action
        or parent.plan_digest != plan.plan_digest
        or job.payload_digest != _digest(job.payload)
        or job.authority_revision != (plan.recipe_content_sha256 or plan.plan_digest)
        or tuple(sorted(job.targets)) != tuple(sorted(_plan_target_node_ids(plan)))
        or len(set(job.targets)) != len(job.targets)
        or _planned_transfer_parts(plan) != (0, 0, 0)
        or progress.workload_intent_ordinal != parent.workload_intent_ordinal
        or (
            progress.cancellation is not None
            and purpose == JournalRepairPurpose.MEASUREMENT
        )
        or progress.force_replan
        or progress.phase_index >= len(plan.phases)
        or progress.item_index != 0
        or (
            purpose == JournalRepairPurpose.MEASUREMENT
            and RunSwitchOperationService._scope_intent_status(session, job)
            != "current"
        )
    ):
        return None
    raw_result = job.result
    if raw_result is None:
        return None
    phase = plan.phases[progress.phase_index]
    if (
        phase.kind != "prepare"
        or phase.subphase != "runtime-install"
        or progress.phase != phase.kind
        or progress.subphase != phase.subphase
        or progress.operation_phase_index != phase.index
        or progress.child_operation_id is None
        or progress.operation is None
        or progress.operation.completed_bytes != raw_result["completed_bytes"]
    ):
        return None
    prior_phases = plan.phases[: progress.phase_index]
    # Narrow historical fault boundary: only the initial managed image/plan
    # preparation receipts, not arbitrary transfer/stop/cleanup history.
    if (
        progress.completed_phases != [item.kind for item in prior_phases]
        or len(progress.phase_results) not in {len(prior_phases), len(prior_phases) + 1}
        or progress.final_observation is not None
    ):
        return None
    if len(progress.phase_results) == len(prior_phases) + 1 and not isinstance(
        progress.phase_results[-1], RunSwitchRuntimeInstallResult
    ):
        return None
    for prior_phase, receipt in zip(
        prior_phases, progress.phase_results[: len(prior_phases)], strict=True
    ):
        if (
            prior_phase.subphase != receipt.subphase
            or prior_phase.kind != receipt.phase
        ):
            return None
        if isinstance(receipt, RunSwitchRuntimeImageResult):
            _validate_artifact_execution(plan, prior_phase, receipt)
            reference = progress.runtime_image_reference_intent
            if (
                reference is None
                or reference.operation_id != job.id
                or reference.request_key != job.request_id
                or reference.actor != job.actor
                or reference.plan_digest != plan.plan_digest
                or reference.phase_index != prior_phase.index
                or reference.item_index != 0
                or reference.workload_intent_ordinal != parent.workload_intent_ordinal
                or reference.profile_application_id != progress.profile_application_id
                or not same_image(reference, receipt.runtime_image)
                or reference.build_input_sha256
                != receipt.runtime_image.build_input_sha256
                or (
                    receipt.effective_execution_key is not None
                    and receipt.effective_execution_key not in reference.execution_keys
                )
            ):
                return None
        elif isinstance(receipt, RunSwitchTargetTransferEvidenceResult):
            if (receipt.copied_bytes or 0) != 0 or (receipt.downloaded_bytes or 0) != 0:
                return None
        elif isinstance(receipt, RunSwitchVerifyResult):
            _validate_artifact_execution(plan, prior_phase, receipt)
        elif not isinstance(receipt, RunSwitchRuntimePlanResult):
            return None
    if progress.profile_application_id is not None:
        application = session.scalar(
            select(FleetProfileApplication).where(
                FleetProfileApplication.id == progress.profile_application_id
            )
        )
        if application is None:
            return None
        application_progress = _stored_progress(application)
        if isinstance(application_progress, Residue):
            return None
        adopted = FleetProfileService._adopted_application_scope(session, application)
        journal = application_progress.switch_adapter
        if (
            (
                purpose == JournalRepairPurpose.MEASUREMENT
                and not FleetProfileService._application_is_current_selection(
                    session, application, application_progress
                )
            )
            or (adopted is not None and not set(job.targets) <= set(adopted))
            or (
                application_progress.cancellation is not None
                and purpose == JournalRepairPurpose.MEASUREMENT
            )
            or journal is None
            or not any(
                child.operation_id == job.id for child in journal.pending_children
            )
        ):
            return None
    child = session.scalar(select(Job).where(Job.id == progress.child_operation_id))
    if (
        child is None
        or child.kind != "recipe.install"
        or child.state
        not in {
            LifecycleState.QUEUED.value,
            LifecycleState.RUNNING.value,
            LifecycleState.OBSERVING.value,
            LifecycleState.NEEDS_OPERATOR.value,
            LifecycleState.SUCCEEDED.value,
            LifecycleState.FAILED.value,
            LifecycleState.CANCELLED.value,
        }
        or (
            purpose == JournalRepairPurpose.MEASUREMENT
            and child.state != LifecycleState.RUNNING.value
        )
    ):
        return None
    child_parent = read_row_column(child, "payload")
    key = (
        _phase_request_key(
            job.request_id,
            progress.phase_index,
            progress.item_index,
            progress.phase_retry_generation,
        )
        if progress.phase_retry_generation
        else job.request_id
    )
    if (
        not isinstance(child_parent, RecipeInstallParent)
        or child.request_id != str(uuid.uuid5(uuid.UUID(key), "runtime-install"))
        or child.actor != job.actor
        or child.payload_digest != _digest(child_parent)
        or child_parent.workload_intent_ordinal != parent.workload_intent_ordinal
        or child_parent.owner_kind != "installation"
        or tuple(sorted(child.targets)) != tuple(sorted(job.targets))
    ):
        return None
    runtime_plan = next(
        (
            receipt
            for receipt in reversed(progress.phase_results)
            if isinstance(receipt, RunSwitchRuntimePlanResult)
        ),
        None,
    )
    if (
        runtime_plan is None
        or runtime_plan.installation_id != child_parent.owner_id
        or runtime_plan.mapping_id is None
    ):
        return None
    RecipeLifecyclePhaseExecutor._bound_installation(
        session,
        plan,
        child_parent.owner_id,
        runtime_plan.mapping_id,
        child_parent.plan_digest,
    )
    # Lock only this exact native scope, NOWAIT, before sampling its owner.
    rows = tuple(
        session.execute(
            select(AgentOperation, AgentOperationAttempt)
            .outerjoin(
                AgentOperationAttempt,
                (AgentOperationAttempt.operation_id == AgentOperation.id)
                & (AgentOperationAttempt.attempt == AgentOperation.current_attempt),
            )
            .where(AgentOperation.parent_job_id == child.id)
            .order_by(AgentOperation.node_id, AgentOperation.id)
        )
    )
    for operation, attempt in rows:
        if (
            operation.authority_revision != child.authority_revision
            or operation.workload_intent_ordinal != parent.workload_intent_ordinal
            or operation.payload_digest != _digest(operation.payload)
            or (operation.current_attempt > 0 and attempt is None)
        ):
            return None
    # Measurement repair requires every current sample field/freshness.
    # Cancellation and expired/terminal observation instead preserve the raw
    # sample as historical evidence while the sole child owner reconciles it;
    # they never trust it as a fresh measurement or issue another child.
    attribution = current_recipe_progress_attribution(session, child, now=now)
    if purpose == JournalRepairPurpose.MEASUREMENT and (
        attribution is None or attribution[0] != progress.operation
    ):
        return None
    witnesses = []
    for operation, attempt in rows:
        if attempt is None:
            continue
        sample = stored_progress(attempt)
        # Without a retained parent attempt witness, a later native attempt
        # could replay identical sample values. Only the original attempt proves
        # attribution; this is an algorithm boundary, never an issuance cap.
        if purpose == JournalRepairPurpose.MEASUREMENT and (
            operation.current_attempt != 1 or attempt.attempt != 1
        ):
            return None
        certificate = session.get(AgentCertificate, attempt.agent_certificate_serial)
        if certificate is None or certificate.node_id != operation.node_id:
            return None
        witnesses.append(
            NativeProgressWitness(
                operation_id=operation.id,
                attempt_id=attempt.id,
                node_id=operation.node_id,
                certificate_serial=attempt.agent_certificate_serial,
                fence=attempt.fence,
                payload_digest=operation.payload_digest,
                sample_digest=hashlib.sha256(canonical_message(sample)).hexdigest()
                if sample is not None
                else None,
                sample=sample,
            )
        )
    return witnesses if witnesses else None
