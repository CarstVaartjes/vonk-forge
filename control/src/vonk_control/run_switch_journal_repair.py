"""Re-derive only proven zero-budget distribution counters; never re-plan work."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    LifecycleState,
    LifecycleSubject,
    UnknownOutcomeError,
    canonical_message,
    is_state,
)

from . import agent_operation_states
from .admission_locking import AdmissionLockBusy, AdmissionRowLock, lock_admission_rows
from .job_documents import RecipeInstallParent
from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    FleetProfileApplication,
    Job,
    RunSwitchJournalRepair,
    RunSwitchJournalRepairPending,
)
from .operation_progress import stored_progress
from .recipe_operations import current_recipe_progress_attribution
from .run_switch_contract import (
    RunSwitchCancellation,
    RunSwitchOperationResult,
    RunSwitchRuntimeImageResult,
    RunSwitchRuntimeInstallResult,
    RunSwitchRuntimePlanResult,
    RunSwitchTargetTransferEvidenceResult,
    RunSwitchVerifyResult,
)
from .run_switch_journal_contract import (
    JournalRepairCode,
    JournalRepairDisposition,
    JournalRepairPurpose,
    NativeProgressWitness,
    RunSwitchJournalRepairEndEvidence,
    RunSwitchJournalRepairEvidence,
    RunSwitchJournalRepairPendingState,
)
from .stored_json import read_row_column

REPAIR_WAIT = JournalRepairCode.EVIDENCE_UNAVAILABLE


def journal_document(value: object) -> str:
    """Stable exact encoding of the SQL JSON document, including stored nulls."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _candidate(job: Job) -> RunSwitchOperationResult | None:
    raw = job.result
    if not isinstance(raw, dict):
        return None
    completed = raw.get("completed_bytes")
    total = raw.get("total_bytes")
    if (
        type(completed) is not int
        or completed <= 0
        or type(total) is not int
        or total != 0
        or raw.get("total_bytes_known") is not True
    ):
        return None
    corrected = dict(raw)
    corrected["completed_bytes"] = 0
    try:
        return RunSwitchOperationResult.model_validate_json(
            journal_document(corrected), strict=True
        )
    except (TypeError, ValueError):
        return None


def is_zero_transfer_journal_fault(job: Job) -> bool:
    return _candidate(job) is not None


@dataclass(frozen=True, slots=True)
class _Discovery:
    """Transient complete lock declaration; never a persisted authority."""

    targets: tuple[str, ...]
    job_ids: tuple[str, ...]
    application_id: str | None
    native_ids: tuple[str, ...]
    attempt_ids: tuple[str, ...]
    fingerprints: tuple[tuple[str, str, str], ...]


def _fingerprint(
    row: AgentNode
    | Job
    | FleetProfileApplication
    | AgentOperation
    | AgentOperationAttempt,
) -> tuple[str, str, str]:
    # SQL ownership snapshots include state, cancellation, fences and samples.
    # Datetimes are encoded only for equality, never interpreted as a schedule.
    values = {
        column.key: (
            value.isoformat()
            if isinstance(value := getattr(row, column.key), datetime)
            else value
        )
        for column in row.__mapper__.columns
    }
    identity = str(row.node_id if isinstance(row, AgentNode) else row.id)
    return (
        row.__tablename__,
        identity,
        hashlib.sha256(journal_document(values).encode()).hexdigest(),
    )


def _discover(session: Session, operation_id: str) -> _Discovery | None:
    job = session.get(Job, operation_id, populate_existing=True)
    progress = _candidate(job) if job is not None else None
    if job is None or progress is None or progress.child_operation_id is None:
        return None
    child = session.get(Job, progress.child_operation_id, populate_existing=True)
    if child is None:
        return None
    nodes = tuple(
        session.scalars(
            select(AgentNode)
            .where(AgentNode.node_id.in_(job.targets))
            .execution_options(populate_existing=True)
        )
    )
    application = (
        session.get(
            FleetProfileApplication,
            progress.profile_application_id,
            populate_existing=True,
        )
        if progress.profile_application_id
        else None
    )
    operations = tuple(
        session.scalars(
            select(AgentOperation)
            .where(AgentOperation.parent_job_id == child.id)
            .execution_options(populate_existing=True)
        )
    )
    attempts = tuple(
        session.scalars(
            select(AgentOperationAttempt)
            .join(
                AgentOperation, AgentOperation.id == AgentOperationAttempt.operation_id
            )
            .where(
                AgentOperation.parent_job_id == child.id,
                AgentOperationAttempt.attempt == AgentOperation.current_attempt,
            )
            .execution_options(populate_existing=True)
        )
    )
    rows: list[
        AgentNode
        | Job
        | FleetProfileApplication
        | AgentOperation
        | AgentOperationAttempt
    ] = [job, child, *nodes, *operations, *attempts]
    if application is not None:
        rows.append(application)
    return _Discovery(
        targets=tuple(sorted(job.targets)),
        job_ids=tuple(sorted({job.id, child.id})),
        application_id=progress.profile_application_id,
        native_ids=tuple(sorted(row.id for row in operations)),
        attempt_ids=tuple(sorted(row.id for row in attempts)),
        fingerprints=tuple(sorted(_fingerprint(row) for row in rows)),
    )


def _lock_discovery(session: Session, captured: _Discovery) -> None:
    requests = [
        AdmissionRowLock(
            "repair-nodes",
            AgentNode,
            select(AgentNode).where(AgentNode.node_id.in_(captured.targets)),
        ),
        AdmissionRowLock(
            "repair-jobs", Job, select(Job).where(Job.id.in_(captured.job_ids))
        ),
        AdmissionRowLock(
            "repair-native",
            AgentOperation,
            select(AgentOperation).where(AgentOperation.id.in_(captured.native_ids)),
        ),
        AdmissionRowLock(
            "repair-attempts",
            AgentOperationAttempt,
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.id.in_(captured.attempt_ids)
            ),
        ),
    ]
    if captured.application_id is not None:
        requests.append(
            AdmissionRowLock(
                "repair-profile",
                FleetProfileApplication,
                select(FleetProfileApplication).where(
                    FleetProfileApplication.id == captured.application_id
                ),
            )
        )
    # One complete declaration, common namespace+immutable-PK order. Newly
    # discovered identities are NEVER appended to this transaction's lock set.
    lock_admission_rows(session, requests)


def _prove(
    session: Session,
    job: Job,
    progress: RunSwitchOperationResult,
    now: datetime,
    purpose: JournalRepairPurpose,
) -> list[NativeProgressWitness] | None:
    # Import the sole owner predicates, not a second plan/authority interpreter.
    from .fleet_profiles import FleetProfileService, _stored_progress
    from .lifecycle.evidence import Residue
    from .run_switch_operations import (
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
                or reference.recipe_revision_id != plan.recipe_revision_id
                or reference.workload_intent_ordinal != parent.workload_intent_ordinal
                or reference.profile_application_id != progress.profile_application_id
                or reference.image_digest != receipt.image_digest
                or reference.archive_sha256 != receipt.oci_layout_sha256
                or reference.image_bytes != receipt.image_bytes
                or reference.build_id != receipt.runtime_image.build_id
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
            "queued",
            "running",
            "observing",
            "needs-operator",
            "succeeded",
            "failed",
            "cancelled",
        }
        or (purpose == JournalRepairPurpose.MEASUREMENT and child.state != "running")
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
        or child.payload_digest != _digest(child.payload)
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


def _try_repair_once(
    sessions: sessionmaker[Session],
    operation_id: str,
    now: datetime,
    *,
    purpose: JournalRepairPurpose = JournalRepairPurpose.MEASUREMENT,
) -> JournalRepairDisposition:
    """One short conditional repair transaction; all native reads stay in SQL."""
    # Normal healthy jobs pay one indexed read and no additional row lock.
    with sessions() as session:
        snapshot = session.get(Job, operation_id)
        if snapshot is None or _candidate(snapshot) is None:
            return JournalRepairDisposition.NOT_APPLICABLE
        captured = _discover(session, operation_id)
        if captured is None:
            return JournalRepairDisposition.DEFERRED
    try:
        with sessions.begin() as session:
            _lock_discovery(session, captured)
            if _discover(session, operation_id) != captured:
                return JournalRepairDisposition.DEFERRED
            job = session.get(Job, operation_id)
            if (
                job is None
                or job.kind != "recipe.run-switch.v2"
                or job.state not in {"queued", "running", "observing"}
            ):
                return JournalRepairDisposition.NOT_APPLICABLE
            progress = _candidate(job)
            if progress is None:
                return JournalRepairDisposition.NOT_APPLICABLE
            if purpose == JournalRepairPurpose.MEASUREMENT:
                child = session.get(Job, progress.child_operation_id)
                attempts = tuple(
                    session.execute(
                        select(AgentOperation, AgentOperationAttempt)
                        .join(
                            AgentOperationAttempt,
                            AgentOperationAttempt.operation_id == AgentOperation.id,
                        )
                        .where(
                            AgentOperation.id.in_(captured.native_ids),
                            AgentOperationAttempt.id.in_(captured.attempt_ids),
                            AgentOperationAttempt.attempt
                            == AgentOperation.current_attempt,
                        )
                    )
                )
                expired = any(
                    now
                    >= (
                        attempt.lease_deadline
                        if attempt.lease_deadline.tzinfo
                        else attempt.lease_deadline.replace(tzinfo=now.tzinfo)
                    )
                    for operation, attempt in attempts
                    if operation.state in agent_operation_states.LIVE
                    and (
                        is_state(
                            LifecycleSubject.AGENT_OPERATION_ATTEMPT,
                            attempt.state,
                            LifecycleState.RUNNING,
                        )
                        or agent_operation_states.attempt_lapsed(attempt)
                    )
                )
                deadline = progress.observation_deadline_at
                deadline_expired = deadline is not None and now >= (
                    deadline if deadline.tzinfo else deadline.replace(tzinfo=now.tzinfo)
                )
                if progress.cancellation is not None:
                    purpose = JournalRepairPurpose.CANCELLATION
                elif (
                    (
                        child is not None
                        and child.state in {"succeeded", "failed", "cancelled"}
                    )
                    or expired
                    or deadline_expired
                ):
                    purpose = JournalRepairPurpose.OWNER_OBSERVATION
            due = progress.observation_due_at
            if (
                purpose == JournalRepairPurpose.MEASUREMENT
                and due is not None
                and now
                < (due if due.tzinfo is not None else due.replace(tzinfo=now.tzinfo))
            ):
                return JournalRepairDisposition.DEFERRED
            try:
                witnesses = _prove(session, job, progress, now, purpose)
            except (KeyError, TypeError, ValueError, UnknownOutcomeError):
                witnesses = None
            if witnesses is None:
                job.status_reason = f"{REPAIR_WAIT}: waiting for exact accepted zero-transfer/native sample evidence; existing observation deadline remains unchanged"
                return JournalRepairDisposition.DEFERRED
            original = journal_document(job.result)
            assert job.result is not None
            corrected = dict(job.result)
            corrected["completed_bytes"] = 0
            original_digest = hashlib.sha256(original.encode()).hexdigest()
            evidence = RunSwitchJournalRepairEvidence(
                algorithm="zero-transfer-native-install-v1",
                purpose=purpose,
                operation_id=job.id,
                request_key=job.request_id,
                payload_digest=job.payload_digest,
                plan_digest=_plan_digest(job),
                original_digest=original_digest,
                corrected_digest=hashlib.sha256(
                    journal_document(corrected).encode()
                ).hexdigest(),
                original_document=original,
                native_samples=witnesses,
                recorded_at=now,
            )
            # The Job lock prevents concurrent checkpoint mutation; unique key
            # makes recovery idempotent without a second live state document.
            previous = session.scalar(
                select(RunSwitchJournalRepair).where(
                    RunSwitchJournalRepair.job_id == job.id,
                    RunSwitchJournalRepair.original_digest == original_digest,
                    RunSwitchJournalRepair.record_kind == "repair",
                )
            )
            if previous is None:
                session.add(
                    RunSwitchJournalRepair(
                        job_id=job.id,
                        original_digest=original_digest,
                        evidence=evidence.model_dump(mode="json"),
                        created_at=now,
                    )
                )
            else:
                retained = read_row_column(previous, "evidence")
                if (
                    not isinstance(retained, RunSwitchJournalRepairEvidence)
                    or retained.model_copy(update={"recorded_at": now}) != evidence
                ):
                    job.status_reason = f"{REPAIR_WAIT}: retained repair evidence does not match the current exact source"
                    return JournalRepairDisposition.DEFERRED
            pending = session.get(RunSwitchJournalRepairPending, operation_id)
            if pending is not None:
                retained_state = RunSwitchJournalRepairPendingState.model_validate_json(
                    canonical_message(pending.progress), strict=True
                )
                if retained_state.cancellation is not None:
                    corrected["cancellation"] = retained_state.cancellation.model_dump(
                        mode="json"
                    )
                session.delete(pending)
            job.result = corrected
            job.updated_at = now
            if (job.status_reason or "").startswith(REPAIR_WAIT):
                job.status_reason = None
            return JournalRepairDisposition.REPAIRED
    except AdmissionLockBusy:
        return JournalRepairDisposition.DEFERRED
    except DBAPIError as error:
        code = getattr(error.orig, "sqlstate", None) or getattr(
            error.orig, "pgcode", None
        )
        if code == "55P03":
            return JournalRepairDisposition.DEFERRED
        raise


def _plan_digest(job: Job) -> str:
    from .run_switch_operations import _run_switch_payload

    parent = _run_switch_payload(job)
    if parent is None:
        raise ValueError("accepted parent disappeared")
    return parent.plan_digest


# A damaged observation gets the same bounded window across restarts. The
# interval is capped; neither a changed sample nor a new caller resets it.
REPAIR_BUDGET = timedelta(minutes=2)


def _pending(
    session: Session, job: Job, now: datetime
) -> tuple[RunSwitchJournalRepairPending, RunSwitchJournalRepairPendingState]:
    row = session.get(RunSwitchJournalRepairPending, job.id)
    if row is None:
        state = RunSwitchJournalRepairPendingState(
            deadline_at=now + REPAIR_BUDGET, next_attempt_at=now
        )
        row = RunSwitchJournalRepairPending(
            job_id=job.id, progress=state.model_dump(mode="json")
        )
        session.add(row)
    else:
        state = RunSwitchJournalRepairPendingState.model_validate_json(
            canonical_message(row.progress), strict=True
        )
    return row, state


def record_repair_cancellation(
    sessions: sessionmaker[Session],
    operation_id: str,
    cancellation: RunSwitchCancellation,
) -> None:
    """Commit intent before attempting any journal evidence or child observation."""
    with sessions.begin() as session:
        job = session.get(Job, operation_id, with_for_update=True)
        if job is None or job.state not in {"queued", "running", "observing"}:
            return
        # The repair may have committed after cancel's initial snapshot. The
        # same Job lock hands intent to the now-readable canonical checkpoint.
        if _candidate(job) is None:
            progress = RunSwitchOperationResult.model_validate_json(
                canonical_message(job.result), strict=True
            )
            if progress.cancellation is None:
                progress.cancellation = cancellation
                job.result = progress.model_dump(mode="json", exclude_none=True)
                job.updated_at = cancellation.requested_at
            return
        row, state = _pending(session, job, cancellation.requested_at)
        if state.cancellation is None:
            state.cancellation = cancellation
        state.next_attempt_at = cancellation.requested_at
        row.progress = state.model_dump(mode="json")


def try_repair_zero_transfer_journal(
    sessions: sessionmaker[Session],
    operation_id: str,
    now: datetime,
    *,
    purpose: JournalRepairPurpose = JournalRepairPurpose.MEASUREMENT,
) -> JournalRepairDisposition:
    from .agent_operation_facts import aware

    with sessions() as session:
        snapshot = session.get(Job, operation_id)
        if (
            snapshot is None
            or snapshot.state not in {"queued", "running", "observing"}
            or _candidate(snapshot) is None
        ):
            return JournalRepairDisposition.NOT_APPLICABLE
    with sessions.begin() as session:
        job = session.scalar(
            select(Job).where(Job.id == operation_id).with_for_update(skip_locked=True)
        )
        if job is None:
            return JournalRepairDisposition.DEFERRED
        if (
            job.state not in {"queued", "running", "observing"}
            or _candidate(job) is None
        ):
            return JournalRepairDisposition.NOT_APPLICABLE
        row, state = _pending(session, job, now)
        if now < aware(state.next_attempt_at) and now < aware(state.deadline_at):
            return JournalRepairDisposition.DEFERRED
        cancellation = state.cancellation
        if cancellation is not None:
            purpose = JournalRepairPurpose.CANCELLATION
        expired = now >= aware(state.deadline_at)
    if expired:
        try:
            return _end_unproven_journal(sessions, operation_id, now)
        except AdmissionLockBusy:
            return JournalRepairDisposition.DEFERRED
    result = _try_repair_once(sessions, operation_id, now, purpose=purpose)
    with sessions.begin() as session:
        job = session.scalar(
            select(Job).where(Job.id == operation_id).with_for_update(skip_locked=True)
        )
        if job is None:
            return result
        row = session.get(RunSwitchJournalRepairPending, operation_id)
        if row is None:
            return result
        state = RunSwitchJournalRepairPendingState.model_validate_json(
            canonical_message(row.progress), strict=True
        )
        if result == JournalRepairDisposition.REPAIRED:
            if state.cancellation is not None:
                progress = RunSwitchOperationResult.model_validate_json(
                    canonical_message(job.result), strict=True
                )
                progress.cancellation = state.cancellation
                job.result = progress.model_dump(mode="json", exclude_none=True)
            session.delete(row)
        elif result == JournalRepairDisposition.DEFERRED:
            state.attempts += 1
            state.next_attempt_at = min(
                state.deadline_at,
                now + timedelta(seconds=min(2 ** min(state.attempts, 5), 30)),
            )
            row.progress = state.model_dump(mode="json")
            job.status_reason = f"{REPAIR_WAIT}: exact child evidence unavailable; next attempt {state.next_attempt_at.isoformat()}; deadline {state.deadline_at.isoformat()}"
    return result


def _end_unproven_journal(
    sessions: sessionmaker[Session], operation_id: str, now: datetime
) -> JournalRepairDisposition:
    """End bookkeeping, fence only its exact install orders, preserve all bytes."""
    from .agent_jobs import release_owned_reservations_in_session
    from .lifecycle.agent_operation import AgentOperationAdapter
    from .lifecycle.recipe_operation import RecipeOperationAdapter
    from .lifecycle.run_switch import RunSwitchAdapter
    from .lifecycle.types import Outcome
    from .models import RecipeInstallation, ResourceReservation
    from .run_switch_operations import _run_switch_payload

    # Discover the complete write set without holding any row. Acquire it in
    # the shared order and NOWAIT; never take resource locks under a Job lock.
    with sessions() as session:
        snapshot = session.get(Job, operation_id)
        candidate = _candidate(snapshot) if snapshot is not None else None
        if snapshot is None or candidate is None:
            return JournalRepairDisposition.NOT_APPLICABLE
        captured = _discover(session, operation_id)
        snapshot_fingerprint = _fingerprint(snapshot)
        child_id = candidate.child_operation_id
        installation_id = next(
            (
                receipt.installation_id
                for receipt in reversed(candidate.phase_results)
                if isinstance(receipt, RunSwitchRuntimePlanResult)
            ),
            None,
        )
        owner_ids = [operation_id, installation_id, candidate.profile_application_id]
        requests = [
            AdmissionRowLock(
                "expiry-nodes",
                AgentNode,
                select(AgentNode).where(AgentNode.node_id.in_(snapshot.targets)),
            ),
            AdmissionRowLock(
                "expiry-installation",
                RecipeInstallation,
                select(RecipeInstallation).where(
                    RecipeInstallation.id == installation_id
                ),
            ),
            AdmissionRowLock(
                "expiry-reservations",
                ResourceReservation,
                select(ResourceReservation).where(
                    ResourceReservation.owner_id.in_(owner_ids)
                ),
            ),
            AdmissionRowLock(
                "expiry-jobs",
                Job,
                select(Job).where(Job.id.in_([operation_id, child_id])),
            ),
            AdmissionRowLock(
                "expiry-orders",
                AgentOperation,
                select(AgentOperation).where(AgentOperation.parent_job_id == child_id),
            ),
            AdmissionRowLock(
                "expiry-attempts",
                AgentOperationAttempt,
                select(AgentOperationAttempt)
                .join(
                    AgentOperation,
                    AgentOperation.id == AgentOperationAttempt.operation_id,
                )
                .where(AgentOperation.parent_job_id == child_id),
            ),
        ]
    with sessions.begin() as session:
        lock_admission_rows(session, requests)
        job = session.get(Job, operation_id, populate_existing=True)
        if (
            job is None
            or _fingerprint(job) != snapshot_fingerprint
            or _discover(session, operation_id) != captured
        ):
            return JournalRepairDisposition.DEFERRED
        progress = _candidate(job)
        if progress is None or job.state not in {"queued", "running", "observing"}:
            return JournalRepairDisposition.NOT_APPLICABLE
        row, state = _pending(session, job, now)
        parent = _run_switch_payload(job)
        child = session.get(Job, progress.child_operation_id)
        # Scope cleanup to the accepted idempotent installation, never a run or
        # a destructive phase. No observation authorizes withdrawing a route.
        child_parent = read_row_column(child, "payload") if child is not None else None
        if (
            child is not None
            and isinstance(child_parent, RecipeInstallParent)
            and parent is not None
            and child.kind == "recipe.install"
            and child.actor == job.actor
            and child_parent.owner_id == installation_id
            and child_parent.workload_intent_ordinal == parent.workload_intent_ordinal
            and set(child.targets) == set(job.targets)
        ):
            for operation in session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == child.id)
            ):
                if operation.state in agent_operation_states.LIVE:
                    attempt = session.scalar(
                        select(AgentOperationAttempt).where(
                            AgentOperationAttempt.operation_id == operation.id,
                            AgentOperationAttempt.attempt == operation.current_attempt,
                        )
                    )
                    AgentOperationAdapter(session).record_outcome(
                        operation,
                        attempt,
                        child,
                        Outcome.CANCELLED,
                        now,
                        reason=JournalRepairCode.EXHAUSTED,
                    )
                    AgentOperationAdapter.withdraw_retry(operation)
                for retained_attempt in session.scalars(
                    select(AgentOperationAttempt).where(
                        AgentOperationAttempt.operation_id == operation.id
                    )
                ):
                    AgentOperationAdapter.end_unobserved_attempt(retained_attempt, now)
            if child.state not in {"succeeded", "failed", "cancelled"}:
                RecipeOperationAdapter().cancelled(
                    child, now, reason=JournalRepairCode.EXHAUSTED
                )
            release_owned_reservations_in_session(
                session, child_parent.owner_kind, child_parent.owner_id, now
            )
        # A missing child row is not evidence that its installation's disk
        # promise is still needed. Actual stored bytes remain in capacity facts;
        # run-owned memory/port claims and routes are independent and untouched.
        if child is None and installation_id is not None:
            release_owned_reservations_in_session(
                session, "installation", installation_id, now
            )
        if progress.profile_application_id is not None:
            # Only promises still held by this preparation application. Running
            # assignment claims have already transferred to their run owners.
            for reservation in session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "fleet-profile",
                    ResourceReservation.owner_id == progress.profile_application_id,
                    ResourceReservation.node_id.in_(job.targets),
                    ResourceReservation.state.in_(["active", "promised"]),
                )
            ):
                reservation.state = "released"
                reservation.released_at = now
        # Parent reservations, if any, cannot survive its end.
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_id == job.id,
                ResourceReservation.state.in_(["active", "promised"]),
            )
        ):
            reservation.state = "released"
            reservation.released_at = now
        reason = f"{JournalRepairCode.EXHAUSTED}: bounded journal observation ended; original evidence retained, installation bytes remain reusable"
        if state.cancellation is not None:
            RunSwitchAdapter().cancelled(job, progress, now, reason=reason)
        else:
            RunSwitchAdapter().reject(job, reason, now)
        # The immutable history retains cancellation after the transient clock
        # is removed. No missing witness is manufactured into repair evidence.
        original = journal_document(job.result)
        original_digest = hashlib.sha256(original.encode()).hexdigest()
        evidence = RunSwitchJournalRepairEndEvidence(
            code=JournalRepairCode.EXHAUSTED.value,
            operation_id=job.id,
            request_key=job.request_id,
            original_digest=original_digest,
            original_document=original,
            cancellation=state.cancellation,
            recorded_at=now,
        )
        session.add(
            RunSwitchJournalRepair(
                job_id=job.id,
                original_digest=original_digest,
                record_kind="end",
                evidence=evidence.model_dump(mode="json", exclude_none=True),
                created_at=now,
            )
        )
        # Do not replace or bless the damaged measurement at expiry.
        session.delete(row)
    return JournalRepairDisposition.ENDED
