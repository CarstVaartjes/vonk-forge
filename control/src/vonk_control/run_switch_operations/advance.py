"""Advance."""

from __future__ import annotations

from datetime import datetime
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    RunSwitchCode,
)

from .. import job_states
from ..admission_locking import (
    admission_attempts,
)
from ..bounded_json import require_integer
from ..content_identity import same_image
from ..lifecycle.run_switch import (
    KEEP as _KEEP_REASON,
)
from ..lifecycle.run_switch import (
    LIVE_STATES as _LIVE_STATES,
)
from ..lifecycle.types import (
    Effect as _LifecycleEffect,
)
from ..lifecycle.types import (
    State as _LifecycleState,
)
from ..models import (
    Job,
)
from ..profile_capacity import (
    accepted_profile_runtime_image,
)
from ..recipe_build_cancellation import (
    needs_container_build,
)
from ..recipe_builds import RecipeBuildAdmissionBusy
from ..recipe_operations import (
    RecipeOperationView,
)
from ..recovery_policy import (
    RecoveryDecision,
    classify,
)
from ..run_switch_contract import (
    RunSwitchDistributionChildResult,
    RunSwitchOperationResult,
)
from ..run_switch_progress import (
    _complete_operation_progress as _complete_operation_progress,  # noqa: PLC0414 -- shared helper export
)
from ..run_switch_progress import (
    _complete_phase_progress as _complete_phase_progress,  # noqa: PLC0414 -- shared helper export
)
from ..run_switch_progress import (
    _merge_progress_evidence as _merge_progress_evidence,  # noqa: PLC0414 -- shared helper export
)
from ..stored_json import read_row_column
from .artifact_validation import _validate_artifact_execution
from .constants import _OBSERVING, _OPERATION_KINDS, _TERMINAL_STATES
from .endings_helpers import _reject_invalid_operation
from .errors import (
    RunSwitchOperationConflict,
    RunSwitchRetryLater,
    _RunSwitchBuildParentChanged,
)
from .identity_helpers import _string_or_none
from .image_receipts import _build_receipt_in_session, _plan_target_node_ids
from .observation_helpers import (
    _established_start_effect,
    _established_stop_effect,
    _EstablishedEffect,
    _start_still_progressing,
)
from .ownership import (
    _checkpoint_matches,
    _complete_cancellation,
    _lock_current_build_parent,
)
from .planning_helpers import _aware, _now, _stored_job_plan
from .provider import _ADAPTER
from .result_helpers import (
    _bound_workload_intent,
    _child_failure_code,
    _child_failure_kind,
    _child_progress_payload,
    _child_result,
    _persisted_result,
    _phase_result,
    _progress_damaged,
    _read_progress,
    _without_observation_time,
)

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class AdvanceMixin:
    def _advance(self, operation_id: str) -> bool:
        service = typing_cast("RunSwitchOperationService", self)
        now = _now(service._clock)
        from ..run_switch_journal_contract import JournalRepairDisposition
        from ..run_switch_journal_repair import try_repair_zero_transfer_journal

        repair = try_repair_zero_transfer_journal(service._sessions, operation_id, now)
        if repair != JournalRepairDisposition.NOT_APPLICABLE:
            # Repair dispatches nothing. The next ordinary turn observes the
            # same child; ambiguous evidence retains its raw journal and scope.
            return repair in {
                JournalRepairDisposition.REPAIRED,
                JournalRepairDisposition.ENDED,
            }
        # A deferred build detachment re-enters the same fenced cancel path,
        # before child observation can end the parent through the lifecycle core.
        # A busy boundary leaves only the durable intent and releases all locks;
        # the worker's bounded polling retries it without a Stop of shared work.
        with service._sessions() as session:
            snapshot = session.get(Job, operation_id, with_for_update=True)
            plan = _stored_job_plan(snapshot) if snapshot is not None else None
            progress = (
                _read_progress(read_row_column(snapshot, "result"))
                if snapshot is not None
                else None
            )
            cancellation = progress.cancellation if progress is not None else None
            if (
                snapshot is not None
                and snapshot.state in _LIVE_STATES
                and plan is not None
                and progress is not None
                and not _progress_damaged(read_row_column(snapshot, "result"))
                and (
                    service._settle_checkpoint_observation(
                        session, snapshot, progress, now
                    )
                    or service._settle_stop_observation(
                        session, snapshot, plan, progress, now
                    )
                )
            ):
                session.commit()
                return True
            detach_build = (
                snapshot is not None
                and snapshot.state in _LIVE_STATES
                and plan is not None
                and progress is not None
                and needs_container_build(plan, progress.phase_index)
            )
        if detach_build and cancellation is not None:
            for _attempt in admission_attempts():
                try:
                    service.cancel(
                        operation_id,
                        actor=cancellation.actor,
                        request_key=cancellation.request_key,
                        reason=cancellation.reason,
                    )
                except RunSwitchRetryLater:
                    # The failed transaction has closed before bounded backoff.
                    continue
                return True
            return False
        if service._refresh_blocked_plan(operation_id, now):
            return True
        with service._sessions() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or job.kind not in _OPERATION_KINDS:
                return True
            if job.state not in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.OBSERVING,
                LifecycleState.NEEDS_OPERATOR,
            ):
                return False
            plan = _stored_job_plan(job)
            if plan is None:
                _reject_invalid_operation(
                    job, "run-switch persisted plan is invalid", now
                )
                session.commit()
                return True
            if _progress_damaged(read_row_column(job, "result")):
                # The stored result is the evidence of what was issued; it is
                # retained untouched rather than replaced with a fabricated
                # empty progress document. The operation is retired as unknown.
                _reject_invalid_operation(
                    job, "run-switch persisted progress is invalid", now
                )
                session.commit()
                return True
            progress = _read_progress(read_row_column(job, "result"))
            intent_status = service._scope_intent_status(session, job)
            if intent_status == "invalid":
                service._mark_failed(
                    job,
                    "run-switch workload authority or Spark scope is invalid",
                    now=now,
                    progress=progress,
                )
                session.commit()
                return True
            if intent_status == "missing-target":
                service._mark_failed(
                    job,
                    "run-switch target node no longer exists; accepted intent is superseded",
                    now=now,
                    progress=progress,
                    failure_code=RunSwitchCode.SUPERSEDED,
                )
                session.commit()
                return True
            if intent_status == "waiting":
                pending_due = progress.observation_due_at
                if (
                    job.state in job_states.words(LifecycleState.OBSERVING)
                    and pending_due is not None
                    and now < _aware(pending_due)
                ):
                    return False
                _ADAPTER.retry(
                    job,
                    progress,
                    RunSwitchCode.TARGET_NOT_ACTIVE,
                    now,
                    visible=_OBSERVING,
                    record_reason=False,
                    describe=lambda due: (
                        "Waiting for a target Spark to return to active state; "
                        f"next check at {due.isoformat()}"
                    ),
                )
                job.result = _persisted_result(progress)
                job.updated_at = now
                session.commit()
                return True
            if intent_status == "superseded":
                _ADAPTER.cancelled(
                    job,
                    progress,
                    now,
                    reason=(
                        f"{RunSwitchCode.SUPERSEDED}: the logical order was cancelled by "
                        "a later authorized Spark intent; issued effects still "
                        "require their own cancellation receipts"
                    ),
                    effect=_LifecycleEffect.UNKNOWN,
                )
                job.result = _persisted_result(progress)
                job.updated_at = now
                session.commit()
                return True
            if progress.observation_due_at is None and (
                job.state in job_states.words(LifecycleState.NEEDS_OPERATOR)
                or (
                    job.state in job_states.words(LifecycleState.OBSERVING)
                    and not progress.child_operation_id
                    and progress.phase != "final_verify"
                )
            ):
                # A wait nothing will ever end: a legacy ``waiting-for-operator``
                # (no Run/Switch action exists) or a ``waiting`` with no clock.
                # Rules 1 and 3: it is retried, never left for a person.
                _ADAPTER.heal(job, progress, now)
                job.result = _persisted_result(progress)
                session.commit()
                return True
            observation_due = progress.observation_due_at
            if (
                observation_due is not None
                and now < _aware(observation_due)
                # A cancel in flight looks at its child on every pass, so it ends
                # as soon as the child does; only the stop attempts are spaced
                # (by the core), never the observation.
                and not progress.cancellation
            ):
                return False
            raw_phase_index = progress.phase_index
            raw_item_index = progress.item_index
            child_id = progress.child_operation_id
            if job.state in job_states.words(
                LifecycleState.OBSERVING, LifecycleState.NEEDS_OPERATOR
            ):
                if not child_id:
                    # Final verification observes an existing run and route;
                    # reopening this checkpoint cannot issue a new workload.
                    # A due automatic wait (an inactive target Spark that has
                    # returned, a background preparation, a backoff) resumes
                    # the exact checkpoint: no child is outstanding, and the
                    # phase key is unchanged, so re-entry is idempotent.
                    # Newer intent was checked above, and the persisted start
                    # deadline remains immutable.
                    if progress.phase != "final_verify" and not (
                        job.state in job_states.words(LifecycleState.OBSERVING)
                        and observation_due is not None
                    ):
                        return False
                    _ADAPTER.project(
                        job,
                        None,
                        now,
                        state=_LifecycleState.RUNNING,
                        reason=_KEEP_REASON
                        if progress.phase == "final_verify"
                        else None,
                    )
                    session.commit()
                else:
                    # Only observe the already-issued child. No new effect is
                    # authorized by reopening this parent's observation checkpoint.
                    _ADAPTER.project(job, None, now, state=_LifecycleState.RUNNING)
                    session.commit()
            if progress.cancellation and child_id is None:
                _complete_cancellation(job, progress, now)
                session.commit()
                return True
            expected_image = None
            profile_application_id = _string_or_none(progress.profile_application_id)
            if (
                profile_application_id is not None
                and plan.recipe_revision_id is not None
                and plan.action != "stop"
            ):
                try:
                    expected_image = accepted_profile_runtime_image(
                        session,
                        profile_application_id,
                        plan.recipe_revision_id,
                        _plan_target_node_ids(plan),
                    )
                except ValueError as error:
                    service._mark_failed(job, str(error), now=now, progress=progress)
                    session.commit()
                    return True
            if (
                type(raw_phase_index) is not int
                or type(raw_item_index) is not int
                or raw_phase_index < 0
                or raw_item_index < 0
            ):
                _ADAPTER.reject(job, "run-switch persisted progress is invalid", now)
                session.commit()
                return True
            # Declare the validated integers so the checkpoint closure below
            # carries their exact type rather than the raw JSON union.
            phase_index: int = raw_phase_index
            item_index: int = raw_item_index
            if phase_index >= len(plan.phases):
                progress = _complete_operation_progress(plan, progress)
                _ADAPTER.succeed(job, progress, now)
                job.result = _persisted_result(progress)
                job.updated_at = now
                session.commit()
                return True
            checkpoint_actor = job.actor
            checkpoint_request_key = job.request_id

        checkpoint_plan = plan
        checkpoint_cancellation = progress.cancellation
        checkpoint_phase = plan.phases[phase_index]
        is_build_checkpoint = (
            checkpoint_phase.kind,
            checkpoint_phase.subphase,
            checkpoint_phase.state,
        ) == ("prepare", "container-build", "planned")
        checkpoint_ordinal = (
            _bound_workload_intent(progress) if is_build_checkpoint else 0
        )

        def checkpoint_job(session: Session) -> Job | None:
            if not is_build_checkpoint:
                return session.get(Job, operation_id, with_for_update=True)
            try:
                return _lock_current_build_parent(
                    session,
                    plan=checkpoint_plan,
                    phase_index=phase_index,
                    item_index=item_index,
                    actor=checkpoint_actor,
                    request_key=checkpoint_request_key,
                    ordinal=checkpoint_ordinal,
                    child_id=child_id,
                    cancellation=checkpoint_cancellation,
                )
            except (_RunSwitchBuildParentChanged, RecipeBuildAdmissionBusy):
                return None

        def fail(
            reason: str,
            *,
            failure_code: str | None = None,
            replan: bool = False,
            clear_child: bool = False,
            definite: bool = False,
        ) -> None:
            service._fail(
                operation_id,
                reason,
                definite=definite,
                replan=replan,
                checkpoint=(phase_index, item_index, child_id),
                failure_code=failure_code,
                checkpoint_guard=checkpoint_job,
                clear_child=clear_child,
            )

        if child_id is not None:
            if not isinstance(child_id, str):
                # Bookkeeping: the child is looked up again by the phase's
                # deterministic identity, which reconciles what it issued.
                fail("run-switch child operation identity is invalid", clear_child=True)
                return True
            try:
                child = service._get_child_operation(child_id)
            except KeyError:
                fail("run-switch child operation disappeared", clear_child=True)
                return True
            if child is None:
                fail("run-switch child operation disappeared", clear_child=True)
                return True
            if child.state in {
                LifecycleState.QUEUED.value,
                LifecycleState.RUNNING.value,
            }:
                child_progress = _child_progress_payload(child)
                with service._sessions.begin() as session:
                    job = checkpoint_job(session)
                    if job is None:
                        return False
                    original = _read_progress(read_row_column(job, "result"))
                    progress = original.model_copy(deep=True)
                    if not _checkpoint_matches(
                        job, progress, phase_index, item_index, child_id
                    ):
                        return False
                    persisted_plan = _stored_job_plan(job)
                    if persisted_plan is None:
                        _reject_invalid_operation(
                            job, "run-switch persisted plan is invalid", now
                        )
                        return True
                    persisted_phase_index = require_integer(
                        progress.phase_index, "phase index"
                    )
                    persisted_phase = (
                        persisted_plan.phases[persisted_phase_index]
                        if persisted_phase_index < len(persisted_plan.phases)
                        else persisted_plan.phases[-1]
                    )
                    _merge_progress_evidence(
                        progress,
                        persisted_plan,
                        persisted_phase,
                        child_progress,
                        now,
                    )
                    progress.phase = persisted_phase.kind
                    progress.subphase = persisted_phase.subphase
                    if progress.cancellation:
                        # A cancel is driven, not waited for: the child is stopped
                        # when it can be and observed at the core's bounded rate,
                        # and the cancel ends within the stop budget either way.
                        if service._cancel_with_session(
                            session, job, progress, now, tick=True
                        ):
                            job.result = _persisted_result(progress)
                            job.updated_at = now
                            return True
                        return False
                    retry_due_at = (
                        child.retry_due_at
                        if isinstance(child, RecipeOperationView)
                        else None
                    )
                    if child.state == LifecycleState.QUEUED.value and isinstance(
                        retry_due_at, datetime
                    ):
                        progress.observation_due_at = _aware(retry_due_at)
                    else:
                        progress.observation_due_at = None
                    child_reason = child_progress.status_reason
                    status_reason = (
                        child_reason[:512] if isinstance(child_reason, str) else None
                    )
                    if (
                        job.state == LifecycleState.RUNNING.value
                        and job.status_reason == status_reason
                        and _without_observation_time(progress)
                        == _without_observation_time(original)
                    ):
                        return False
                    _ADAPTER.project(
                        job,
                        progress,
                        now,
                        state=_LifecycleState.RUNNING,
                        reason=status_reason,
                    )
                    job.result = _persisted_result(progress)
                    job.updated_at = now
                return True
            if (
                child.state in _TERMINAL_STATES
                and child.state != LifecycleState.SUCCEEDED.value
            ):
                with service._sessions.begin() as session:
                    job = checkpoint_job(session)
                    current = (
                        _read_progress(read_row_column(job, "result"))
                        if job is not None
                        else RunSwitchOperationResult()
                    )
                    if (
                        job is not None
                        and _checkpoint_matches(
                            job, current, phase_index, item_index, child_id
                        )
                        and current.cancellation
                    ):
                        _complete_cancellation(job, current, now)
                        return True
            if (
                child.state in job_states.words(LifecycleState.NEEDS_OPERATOR)
                and checkpoint_cancellation
            ):
                # A cancelled order needs no operator for an idempotent
                # transfer: nothing is running, so close its parked operations
                # (copied bytes stay on the Spark) and finish the cancellation
                # instead of waiting for an owner that has nothing to resume.
                with service._sessions.begin() as session:
                    job = checkpoint_job(session)
                    if job is None:
                        return False
                    current = _read_progress(read_row_column(job, "result"))
                    if (
                        _checkpoint_matches(
                            job, current, phase_index, item_index, child_id
                        )
                        and current.cancellation
                        and service._abandon_idempotent_child(session, child_id, now)
                    ):
                        _complete_cancellation(job, current, now)
                        return True
            if (
                child.state not in _TERMINAL_STATES
                or child.state != LifecycleState.SUCCEEDED.value
            ):
                established = _established_start_effect(
                    service._lifecycle, plan.phases[phase_index], child
                ) or _established_stop_effect(
                    service._lifecycle, plan.phases[phase_index], child
                )
                if established is not None:
                    # The effect is established under current authority, so the
                    # ordinary success path records the checkpoint the missing
                    # acknowledgement would have produced.
                    assert isinstance(child, RecipeOperationView)
                    child = _EstablishedEffect(child, established)
                elif _start_still_progressing(
                    service._lifecycle, plan.phases[phase_index], child
                ):
                    return service._hold_start_observation(
                        operation_id,
                        child_id=child_id,
                        phase_index=phase_index,
                        item_index=item_index,
                    )
                elif child.state in job_states.words(LifecycleState.NEEDS_OPERATOR):
                    # The lifecycle child owns the uncertain effect. Keep its
                    # exact identity and capacity reservation instead of
                    # turning an agent restart into a terminal parent failure.
                    with service._sessions.begin() as session:
                        job = checkpoint_job(session)
                        if job is None:
                            return False
                        progress = _read_progress(read_row_column(job, "result"))
                        if not _checkpoint_matches(
                            job, progress, phase_index, item_index, child_id
                        ):
                            return False
                        child_reason = (
                            _child_progress_payload(child).status_reason
                            or "Lifecycle effect is uncertain; exact child remains pending"
                        )[:400]
                        _ADAPTER.retry(
                            job,
                            progress,
                            child_reason,
                            now,
                            visible=_OBSERVING,
                            record_reason=False,
                            describe=lambda due: (
                                f"{child_reason}; next exact observation at "
                                f"{due.isoformat()}"
                            ),
                        )
                        job.result = _persisted_result(progress)
                        job.updated_at = now
                    return True
                else:
                    reason = f"run-switch phase operation failed: {child.state if child else 'unknown'}"
                    evidence = _child_progress_payload(child)
                    detail = evidence.reason or evidence.status_reason
                    if isinstance(detail, str) and detail:
                        reason += ": " + detail[:384]
                    kind = _child_failure_kind(child)
                    if classify(kind) is RecoveryDecision.RETRY:
                        with service._sessions.begin() as session:
                            job = checkpoint_job(session)
                            if job is None:
                                return False
                            current = _read_progress(read_row_column(job, "result"))
                            if not _checkpoint_matches(
                                job, current, phase_index, item_index, child_id
                            ):
                                return False
                            current.child_operation_id = None
                            current.phase_retry_generation = (
                                require_integer(
                                    current.phase_retry_generation or 0,
                                    "phase retry generation",
                                )
                                + 1
                            )
                            service._schedule_checkpoint_retry(
                                job, current, reason, now
                            )
                        return True
                    # An uncertain effect stays attached to its exact child;
                    # only a terminal temporary dependency is safe to resubmit.
                    service._fail(
                        operation_id,
                        reason,
                        checkpoint=(phase_index, item_index, child_id),
                        failure_code=_child_failure_code(evidence),
                        child_evidence=evidence,
                        checkpoint_guard=checkpoint_job,
                    )
                    return True
            with service._sessions.begin() as session:
                job = checkpoint_job(session)
                if job is None:
                    return False
                progress = _read_progress(read_row_column(job, "result"))
                if not _checkpoint_matches(
                    job, progress, phase_index, item_index, child_id
                ):
                    return False
                progress.observation_due_at = None
                progress.observation_deadline_at = None
                job.status_reason = None
                phase_index = require_integer(progress.phase_index, "phase index")
                item_index = require_integer(progress.item_index, "item index") + 1
                persisted_plan = _stored_job_plan(job)
                if persisted_plan is None:
                    _reject_invalid_operation(
                        job, "run-switch persisted plan is invalid", now
                    )
                    return True
                phase = persisted_plan.phases[phase_index]
                _merge_progress_evidence(
                    progress,
                    persisted_plan,
                    phase,
                    _child_progress_payload(child),
                    now,
                )
                # Preserve terminal child receipts for the following verify
                # phase. Byte/member projection alone cannot prove every
                # model object and the imported OCI identity reached the
                # target; the receipts are the durable handoff across a
                # restart.
                child_result = _child_result(child)
                child_receipts = (
                    child_result.evidence
                    if isinstance(child_result, RunSwitchDistributionChildResult)
                    else None
                )
                if isinstance(child_receipts, list):
                    results = list(progress.phase_results)
                    try:
                        results.extend(
                            _phase_result(receipt, phase=phase)
                            for receipt in child_receipts
                        )
                    except RunSwitchOperationConflict as error:
                        # A receipt that does not validate is an unknown, not a
                        # failure: the idempotent child is issued again under a
                        # new identity and its fresh receipt is validated.
                        service._settle_invalid_receipt(job, error, now, reissue=True)
                        return True
                    progress.phase_results = results
                if phase.subphase == "container-build":
                    try:
                        receipt = _build_receipt_in_session(
                            session,
                            persisted_plan,
                            expected_image=expected_image,
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
                if phase.kind in {"transfer", "verify", "cleanup"} or (
                    phase.kind == "prepare" and phase.subphase == "runtime-image"
                ):
                    try:
                        _validate_artifact_execution(
                            persisted_plan,
                            phase,
                            child_result,
                            expected_image=expected_image,
                        )
                    except RunSwitchOperationConflict as error:
                        # A receipt that does not validate is an unknown, not a
                        # failure: the idempotent child is issued again under a
                        # new identity and its fresh receipt is validated.
                        service._settle_invalid_receipt(job, error, now, reissue=True)
                        return True
                if (
                    child_receipts is None
                    and child_result is not None
                    and (
                        phase.kind in {"transfer", "verify", "cleanup"}
                        or phase.subphase == "runtime-image"
                    )
                ):
                    try:
                        receipt = _phase_result(child_result, phase=phase)
                    except RunSwitchOperationConflict as error:
                        # A receipt that does not validate is an unknown, not a
                        # failure: the idempotent child is issued again under a
                        # new identity and its fresh receipt is validated.
                        service._settle_invalid_receipt(job, error, now, reissue=True)
                        return True
                    progress.phase_results = [
                        *progress.phase_results,
                        receipt,
                    ]
                item_total = len(persisted_plan.stops) if phase.kind == "stop" else 1
                progress.child_operation_id = None
                if item_index >= item_total:
                    completed = list(progress.completed_phases)
                    completed.append(phase.kind)
                    progress.completed_phases = completed
                    _complete_phase_progress(progress, persisted_plan, phase)
                    progress.phase_index = phase_index + 1
                    progress.item_index = 0
                    progress.phase = (
                        persisted_plan.phases[phase_index + 1].kind
                        if phase_index + 1 < len(persisted_plan.phases)
                        else "final_verify"
                    )
                    progress.subphase = (
                        persisted_plan.phases[phase_index + 1].subphase
                        if phase_index + 1 < len(persisted_plan.phases)
                        else None
                    )
                else:
                    progress.item_index = item_index
                _ADAPTER.project(job, progress, now, state=_LifecycleState.RUNNING)
                job.result = _persisted_result(progress)
                job.updated_at = now
                if progress.cancellation:
                    _complete_cancellation(job, progress, now)
            return True
        return service._execute_checkpoint(
            operation_id, now, checkpoint_job, fail, expected_image
        )
