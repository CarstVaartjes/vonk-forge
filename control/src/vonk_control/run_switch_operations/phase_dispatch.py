"""Phase dispatch."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

import httpx2
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    OperationProgress,
    RouteState,
    RunState,
    RunSwitchCode,
    RuntimePreflightCode,
    UnknownOutcomeError,
    run_switch_code,
)
from vonk_agent_protocol.agent_words import ProfileChildPhase, ProfileReportedPhase

from ..admission_locking import (
    AdmissionLockBusy,
    busy_detail,
    patient_admission,
)
from ..bounded_json import require_integer
from ..content_identity import same_image
from ..failure_classification import error_code, is_security_failure
from ..install_admission import (
    InstallAdmissionBusy,
)
from ..lifecycle.types import (
    State as _LifecycleState,
)
from ..logging import log_event
from ..models import (
    Job,
)
from ..preparation_contract import (
    RuntimeImageIdentity,
)
from ..recipe_builds import RecipeBuildAdmissionBusy
from ..run_admission import (
    RETRYABLE_PLAN_BLOCKERS,
    RUN_ADMISSION_WAIT_CODES,
    RunAdmissionBusy,
)
from ..run_switch_contract import (
    RunSwitchFinalVerifyResult,
)
from ..run_switch_progress import (
    _complete_phase_progress as _complete_phase_progress,  # noqa: PLC0414 -- shared helper export
)
from ..run_switch_progress import (
    _merge_progress_evidence as _merge_progress_evidence,  # noqa: PLC0414 -- shared helper export
)
from ..runtime_image_preparation import (
    RuntimeImagePreparationError,
)
from ..stored_json import read_row_column
from .artifact_validation import _validate_artifact_execution
from .constants import (
    _FINAL_VERIFICATION_MAX_SECONDS,
    _LOGGER,
    _OBSERVING,
    _RUNTIME_IMAGE_OWNER_CHANGED,
)
from .contracts import FinalVerificationExpiry
from .endings_helpers import _reject_invalid_operation
from .errors import (
    RunSwitchInstallPreflightExpired,
    RunSwitchIssuedWorkloadPending,
    RunSwitchOperationConflict,
    RunSwitchPostStopEvidencePending,
    _RunSwitchBuildParentChanged,
)
from .image_receipts import _build_receipt_in_session
from .interfaces import PhaseExecution, _PhasePreflightGate
from .observation_helpers import _failure_code_of, _phase_request_key
from .ownership import _checkpoint_matches, _complete_cancellation
from .planning_helpers import _aware, _refused_retries, _start_parent, _stored_job_plan
from .provider import _ADAPTER
from .result_helpers import (
    _observe_progress,
    _persisted_result,
    _phase_result,
    _read_progress,
)

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class PhaseDispatchMixin:
    def _execute_checkpoint(
        self,
        operation_id: str,
        now: datetime,
        checkpoint_job: Callable[[Session], Job | None],
        fail: Callable[..., None],
        expected_image: RuntimeImageIdentity | None,
    ) -> bool:
        service = typing_cast("RunSwitchOperationService", self)
        expiry_event: FinalVerificationExpiry | None = None
        with service._sessions.begin() as session:
            job = checkpoint_job(session)
            if job is None or job.state not in {
                LifecycleState.QUEUED.value,
                LifecycleState.RUNNING.value,
            }:
                return False
            plan = _stored_job_plan(job)
            progress = _read_progress(read_row_column(job, "result"))
            if progress.cancellation:
                _complete_cancellation(job, progress, now)
                return True
            if plan is None:
                _reject_invalid_operation(
                    job, "run-switch persisted plan is invalid", now
                )
                return True
            _ADAPTER.project(job, None, now, state=_LifecycleState.RUNNING)
            phase_index = require_integer(progress.phase_index, "phase index")
            item_index = require_integer(progress.item_index, "item index")
            if phase_index >= len(plan.phases):
                return True
            phase = plan.phases[phase_index]
            actor = job.actor
            retry_generation = require_integer(
                progress.phase_retry_generation or 0,
                "phase retry generation",
            )
            request_key = (
                _phase_request_key(
                    job.request_id, phase_index, item_index, retry_generation
                )
                if retry_generation
                else job.request_id
            )
        gate = getattr(service._phase_executor, "preflight", None)
        if isinstance(gate, _PhasePreflightGate):
            try:
                checkpoint, blocked = gate(
                    plan, phase, actor=actor, request_key=request_key, progress=progress
                )
            except (UnknownOutcomeError, RuntimeError, ValueError, KeyError) as error:
                code = error_code(error) or RuntimePreflightCode.RECEIPT_INVALID
                fail(
                    f"{code}: {type(error).__name__}: {error}",
                    failure_code=code,
                    replan=True,
                    definite=getattr(error, "definite", False),
                )
                return True
            if checkpoint is not None:
                with service._sessions.begin() as session:
                    job = checkpoint_job(session)
                    if job is None:
                        return False
                    current = _read_progress(read_row_column(job, "result"))
                    if not _checkpoint_matches(
                        job, current, phase_index, item_index, None
                    ):
                        return False
                    current.preflight = checkpoint
                    current.observation_due_at = (
                        checkpoint.next_check_at
                        if checkpoint.next_check_at is not None
                        else None
                    )
                    if blocked:
                        # Runtime evidence that says "not now" (a full disk, a
                        # missing prerequisite) is observed again at the core's
                        # backoff, with a re-plan before any Start could have
                        # launched: the request stays accepted and recovers when
                        # the cause clears, instead of ending the load.
                        current.force_replan = (
                            "start" not in current.completed_phases
                            and current.phase != "start"
                        )
                        service._schedule_checkpoint_retry(job, current, blocked, now)
                        return True
                    if checkpoint.pending_job_id:
                        current.operation = _observe_progress(
                            current.operation,
                            OperationProgress.model_validate(
                                {
                                    "phase": "runtime-preflight",
                                    "completed_bytes": 0,
                                    "total_bytes_known": False,
                                }
                            ),
                            now,
                        )
                    job.result = _persisted_result(current)
                    job.status_reason = (
                        f"{checkpoint.last_failure_code}: {checkpoint.last_failure_detail}"
                        if checkpoint.last_failure_code
                        else None
                    )
                    job.updated_at = now
                if checkpoint.pending_job_id or checkpoint.next_check_at is not None:
                    return True
        if phase.state in {"skipped", "retained"}:
            execution = PhaseExecution()
        elif phase.state == "blocked":
            fail(f"run-switch phase blocked: {phase.kind}", replan=True)
            return True
        elif service._phase_executor is None:
            fail(f"run-switch phase executor unavailable: {phase.kind}")
            return True
        else:
            try:
                with patient_admission(_refused_retries(progress)):
                    execution = service._phase_executor.execute(
                        plan,
                        phase,
                        item_index=item_index,
                        actor=actor,
                        request_key=request_key,
                        progress=progress,
                    )
            except _RunSwitchBuildParentChanged:
                return False
            except RunSwitchInstallPreflightExpired as expired:
                return service._hold_for_preflight_refresh(
                    operation_id, phase_index, item_index, cause=str(expired)
                )
            except (
                AdmissionLockBusy,
                InstallAdmissionBusy,
                RunAdmissionBusy,
                RecipeBuildAdmissionBusy,
            ) as busy:
                return service._hold_capacity_writer(
                    operation_id,
                    phase_index,
                    item_index,
                    reason=busy.code,
                    detail=(
                        busy.detail
                        if isinstance(busy, RunAdmissionBusy) and busy.detail
                        else busy_detail(busy)
                    ),
                )
            except RunSwitchPostStopEvidencePending as pending:
                return service._hold_capacity_writer(
                    operation_id,
                    phase_index,
                    item_index,
                    reason=pending.code,
                    detail=str(pending),
                    # Evidence collected at the threshold itself is not "after" it:
                    # the retry waits one second past it, never lands on it.
                    not_before=(
                        None
                        if pending.collected_after is None
                        else _aware(pending.collected_after) + timedelta(seconds=1)
                    ),
                )
            except RunSwitchIssuedWorkloadPending as pending:
                return service._hold_issued_observation(
                    operation_id,
                    phase_index=phase_index,
                    item_index=item_index,
                    pending=pending,
                )
            except RunSwitchOperationConflict as error:
                code = error_code(error)
                # Bookkeeping becomes unknown, then reconcile: a conflict that is
                # not a security refusal is observed again by re-entering the
                # same idempotent phase.  That includes final verification: a
                # check that cannot be observed (wrong image, missing member) is
                # not proof that the workload failed, so it is looked at again,
                # visibly (the retry names the cause and attempt), until it
                # holds, a newer intent supersedes it, or the load is cancelled.
                # A re-plan is only for planning drift before a Start could have
                # launched, never for a final verification of a live workload.
                fail(
                    str(error),
                    failure_code=code,
                    replan=phase.kind
                    not in {
                        ProfileChildPhase.PREPARE.value,
                        ProfileChildPhase.TRANSFER.value,
                        ProfileChildPhase.VERIFY.value,
                        ProfileReportedPhase.FINAL_VERIFY.value,
                    },
                    definite=error.definite,
                )
                return True
            except UnknownOutcomeError as error:
                # An unknown outcome is observed again, never ended: it is not
                # a definite failure even where the preparation owner left its
                # ``retryable`` flag unset, and the core backs the retry off.
                code = getattr(error, "code", None)
                if isinstance(code, str) and (
                    code == _RUNTIME_IMAGE_OWNER_CHANGED or code.startswith("artifact.")
                ):
                    return service._hold_capacity_writer(
                        operation_id,
                        phase_index,
                        item_index,
                        reason=code,
                        detail=str(getattr(error, "detail", None) or error),
                    )
                fail(
                    f"{type(error).__name__}: {error}",
                    failure_code=_failure_code_of(error),
                )
                return True
            except RuntimeImagePreparationError as error:
                # A publication that no longer finds its owner is re-checked,
                # visibly: a cancelled or superseded owner settles on the next
                # tick, a current one prepares again (reusing the archive).
                if error.code == _RUNTIME_IMAGE_OWNER_CHANGED or (
                    error.retryable and error.code.startswith("artifact.")
                ):
                    return service._hold_capacity_writer(
                        operation_id,
                        phase_index,
                        item_index,
                        reason=error.code,
                        detail=error.detail,
                    )
                # Bytes that failed their digest are refused and fetched again
                # by the ordinary preparation retry, with exponential backoff.
                # The preparation owner types its own verdict: an invalid archive
                # or identity is not retried, a transient failure and a digest
                # that is fetched again are.
                fail(
                    f"{type(error).__name__}: {error}",
                    failure_code=error.code,
                    definite=is_security_failure(error.code),
                )
                return True
            except (
                OSError,
                httpx2.HTTPError,
                RuntimeError,
                TypeError,
                ValueError,
                KeyError,
            ) as error:
                detail = f"{type(error).__name__}: {error}"
                fail(
                    detail,
                    failure_code=_failure_code_of(error),
                    replan=isinstance(error, (RuntimeError, ValueError))
                    and phase.kind
                    not in {
                        ProfileChildPhase.PREPARE.value,
                        ProfileChildPhase.TRANSFER.value,
                        ProfileChildPhase.VERIFY.value,
                    },
                )
                return True
        if (
            execution.waiting
            and execution.operation_id is None
            and phase.kind != "final_verify"
            and not (phase.kind == "prepare" and phase.subphase == "runtime-image")
        ):
            fail(run_switch_code(f"{phase.kind}-waiting-without-child"), replan=True)
            return True
        if (
            execution.operation_id is None
            and execution.result is not None
            and (
                phase.kind in {"transfer", "verify", "cleanup"}
                or (phase.kind == "prepare" and phase.subphase == "runtime-image")
            )
        ):
            try:
                _validate_artifact_execution(
                    plan, phase, execution.result, expected_image=expected_image
                )
            except RunSwitchOperationConflict as error:
                # Completed work whose receipt does not validate is not proof
                # that the work failed: its effect is unknown.  Entering the
                # same idempotent phase again observes it (a transfer or verify
                # re-reads the bytes; a cleanup re-reads the reclaim evidence),
                # and a re-plan drops stale evidence.  Never terminal.
                fail(
                    str(error),
                    failure_code=error_code(error),
                    replan=True,
                    definite=error.definite,
                )
                return True
        with service._sessions.begin() as session:
            job = checkpoint_job(session)
            if job is None:
                return False
            progress = _read_progress(read_row_column(job, "result"))
            if not _checkpoint_matches(job, progress, phase_index, item_index, None):
                return False
            previous_status_reason = job.status_reason
            if progress.observation_due_at is not None:
                progress.observation_due_at = None
                progress.observation_deadline_at = None
                job.status_reason = None
            retry_reason = progress.retry_reason
            if retry_reason in (
                AdmissionLockBusy.code,
                InstallAdmissionBusy.code,
                RunAdmissionBusy.code,
                *RETRYABLE_PLAN_BLOCKERS,
                *RUN_ADMISSION_WAIT_CODES,
                RecipeBuildAdmissionBusy.code,
                RunSwitchPostStopEvidencePending.code,
                _RUNTIME_IMAGE_OWNER_CHANGED,
            ) or (
                isinstance(retry_reason, str) and retry_reason.startswith("artifact.")
            ):
                progress.retry_reason = None
                progress.retry_attempt = None
            deadline_expired = False
            _merge_progress_evidence(
                progress,
                plan,
                phase,
                execution.result,
                now,
            )
            if execution.waiting:
                progress.phase = phase.kind
                progress.subphase = phase.subphase
                if phase.kind == "final_verify" and execution.operation_id is None:
                    if progress.final_verify_started_at is None:
                        progress.final_verify_started_at = now.timestamp()
                    started = progress.final_verify_started_at
                    if (
                        isinstance(started, bool)
                        or not isinstance(started, (int, float))
                        or now.timestamp() < started
                    ):
                        service._mark_failed(
                            job,
                            RunSwitchCode.FINAL_VERIFICATION_CLOCK_INVALID,
                            now=now,
                            progress=progress,
                        )
                        return True
                    raw_start_deadline = progress.start_deadline
                    start_deadline = (
                        _aware(raw_start_deadline)
                        if raw_start_deadline is not None
                        else None
                    )
                    # The start's budget begins when it is first dispatched, so
                    # the Controller moves its deadline past the queue wait; the
                    # start job's own payload is the one source of the accepted
                    # deadline.
                    verify_run_id = (
                        execution.result.run_id
                        if isinstance(execution.result, RunSwitchFinalVerifyResult)
                        else None
                    )
                    if isinstance(verify_run_id, str):
                        issued = session.scalar(
                            select(Job)
                            .where(
                                Job.kind == "recipe.start",
                                Job.payload["owner_id"].as_string() == verify_run_id,
                            )
                            .order_by(Job.created_at.desc())
                            .limit(1)
                        )
                        issued_parent = _start_parent(issued)
                        issued_deadline = (
                            issued_parent.start_deadline
                            if issued_parent is not None
                            else None
                        )
                        if issued_deadline is not None:
                            anchored = _aware(issued_deadline)
                            if start_deadline is None or anchored > start_deadline:
                                start_deadline = anchored
                                progress.start_deadline = anchored
                    deadline_expired = (
                        plan.action in {"run", "switch"}
                        and start_deadline is not None
                        and now >= start_deadline
                    )
                    if (
                        now.timestamp() - started >= _FINAL_VERIFICATION_MAX_SECONDS
                        and (
                            deadline_expired
                            or start_deadline is None
                            or plan.action not in {"run", "switch"}
                        )
                    ):
                        reason = (
                            f"{RunSwitchCode.FINAL_VERIFICATION_TIMEOUT}: exact run and "
                            "route evidence did not arrive within the observation bound"
                        )
                        service._mark_failed(
                            job,
                            reason,
                            now=now,
                            failure_code=RunSwitchCode.FINAL_VERIFICATION_TIMEOUT,
                            progress=progress,
                        )
                        return True
                    due = now + timedelta(
                        seconds=(
                            60
                            if deadline_expired or now.timestamp() - started >= 300
                            else 5
                        )
                    )
                    progress.observation_due_at = due
                    if deadline_expired and start_deadline is not None:
                        owner_reason = execution.status_reason or (
                            "run and route owners have not produced exact final evidence"
                        )
                        job.status_reason = (
                            f"{RunSwitchCode.FINAL_VERIFICATION_EXPIRED}: accepted start "
                            f"deadline {start_deadline.isoformat()} passed; "
                            f"{owner_reason[:220]}; exact reconciliation retains the "
                            f"run and reservations; next observation at {due.isoformat()}"
                        )[:512]
                        _ADAPTER.project(
                            job,
                            progress,
                            now,
                            state=_LifecycleState.OBSERVING,
                            due=due,
                            visible=_OBSERVING,
                        )
                        if not (
                            isinstance(previous_status_reason, str)
                            and previous_status_reason.startswith(
                                f"{RunSwitchCode.FINAL_VERIFICATION_EXPIRED}:"
                            )
                        ):
                            phase_evidence = _phase_result(
                                execution.result or {}, phase=phase
                            )
                            expiry_event = FinalVerificationExpiry(
                                operation_id=job.id,
                                run_id=phase_evidence.run_id
                                if isinstance(
                                    phase_evidence, RunSwitchFinalVerifyResult
                                )
                                else None,
                                run_state=next(
                                    (
                                        state
                                        for state in RunState
                                        if state.value == phase_evidence.state
                                    ),
                                    None,
                                )
                                if isinstance(
                                    phase_evidence, RunSwitchFinalVerifyResult
                                )
                                else None,
                                route_state=next(
                                    (
                                        state
                                        for state in RouteState
                                        if state.value == phase_evidence.route_state
                                    ),
                                    None,
                                )
                                if isinstance(
                                    phase_evidence, RunSwitchFinalVerifyResult
                                )
                                else None,
                                accepted_start_deadline=start_deadline.isoformat(),
                                status_reason=job.status_reason,
                            )
                    else:
                        job.status_reason = execution.status_reason or (
                            "Waiting for exact run and route verification; "
                            f"next observation at {due.isoformat()}"
                        )
                    # Keep one current observation while awaiting route publication.
                    # Repeated polling must not grow durable phase receipts.
                    progress.final_observation = _phase_result(
                        execution.result or {}, phase=phase
                    )
                elif execution.result is not None:
                    results = list(progress.phase_results)
                    results.append(_phase_result(execution.result, phase=phase))
                    progress.phase_results = results
                elif phase.kind == "prepare" and phase.subphase == "runtime-image":
                    due = now + timedelta(seconds=5)
                    progress.observation_due_at = due
                    job.status_reason = execution.status_reason or (
                        "Runtime image preparation is running in the background; "
                        f"next check at {due.isoformat()}"
                    )
                    _ADAPTER.project(
                        job,
                        progress,
                        now,
                        state=_LifecycleState.OBSERVING,
                        due=due,
                        visible=_OBSERVING,
                    )
            elif execution.operation_id is not None:
                progress.child_operation_id = execution.operation_id
                progress.phase = phase.kind
                progress.subphase = phase.subphase
                if phase.kind == "start":
                    child = session.get(Job, execution.operation_id)
                    child_parent = _start_parent(child)
                    raw_deadline = (
                        child_parent.start_deadline
                        if child_parent is not None
                        else None
                    )
                    if child is not None and raw_deadline is not None:
                        deadline = _aware(raw_deadline)
                        progress.start_deadline = deadline
                        budget = int(
                            (deadline - _aware(child.created_at)).total_seconds()
                        )
                        if budget > 0:
                            progress.startup_budget_seconds = budget
                if execution.result is not None:
                    results = list(progress.phase_results)
                    results.append(_phase_result(execution.result, phase=phase))
                    progress.phase_results = results
            else:
                if execution.result is not None:
                    results = list(progress.phase_results)
                    results.append(_phase_result(execution.result, phase=phase))
                    progress.phase_results = results
                if phase.subphase == "container-build":
                    try:
                        receipt = _build_receipt_in_session(
                            session, plan, expected_image=expected_image
                        )
                    except RunSwitchOperationConflict as error:
                        # The build's receipt is read from the Controller's own
                        # records, which may not be complete yet: observe it
                        # again without issuing the build a second time.
                        service._settle_invalid_receipt(job, error, now, reissue=False)
                        return True
                    results = list(progress.phase_results)
                    if not any(same_image(item, receipt) for item in results):
                        results.append(_phase_result(receipt, phase=phase))
                        progress.phase_results = results
                completed = list(progress.completed_phases)
                completed.append(phase.kind)
                progress.completed_phases = completed
                _complete_phase_progress(progress, plan, phase)
                progress.phase_index = (
                    require_integer(progress.phase_index, "phase index") + 1
                )
                progress.item_index = 0
                next_index = int(progress.phase_index)
                progress.phase = (
                    plan.phases[next_index].kind
                    if next_index < len(plan.phases)
                    else "final_verify"
                )
                progress.subphase = (
                    plan.phases[next_index].subphase
                    if next_index < len(plan.phases)
                    else None
                )
            if not deadline_expired and not (
                execution.waiting
                and phase.kind == "prepare"
                and phase.subphase == "runtime-image"
            ):
                _ADAPTER.project(job, progress, now, state=_LifecycleState.RUNNING)
            job.result = _persisted_result(progress)
            job.updated_at = now
            if progress.cancellation and not progress.child_operation_id:
                _complete_cancellation(job, progress, now)
        if expiry_event is not None:
            log_event(
                _LOGGER,
                "run_switch.final_verification_expired",
                service="control-worker",
                **expiry_event.model_dump(mode="json"),
            )
        return True
