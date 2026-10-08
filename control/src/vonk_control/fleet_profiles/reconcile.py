"""Reconcile for Fleet profiles."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import case, func, or_, select
from vonk_agent_protocol import (
    LifecycleState,
    ProfileReasonCode,
    SupersedeCode,
    UnknownOutcomeError,
    canonical_message,
)
from vonk_agent_protocol.agent_words import (
    ProfileAction,
    ProfileChildPhase,
    ProfileChildSource,
)

from .. import fleet_profile_states, job_states
from ..failure_classification import error_code, is_security_failure
from ..fleet_profile_adapter_conversion import (
    convert_due_retained_applications,
    needs_conversion,
)
from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
    FleetProfileChildOperation,
    FleetProfileStepResult,
)
from ..lifecycle.evidence import Residue
from ..lifecycle.fleet_profile import FleetProfileAdapter
from ..lifecycle.types import Effect as _LifecycleEffect
from ..lifecycle.types import State as _LifecycleState
from ..models import FleetProfileApplication
from ..operation_blockers import PHASE_RETRY_CODE, bound_blockers, make_blocker
from ..recipe_operations import RecipeOperationConflict
from ..reservation_owners import release_dead_owner_reservations
from ..run_switch_contract import RunSwitchStopResult
from ..strict_json import read_stored_model
from .activity import (
    retry_disposition_of,
)
from .contracts import (
    FleetProfileChildPlanBlocked,
    FleetProfileConflict,
    FleetProfilePermissionDenied,
    FleetProfileReviewStale,
    _FleetProfileRecoveryBindingConflict,
)
from .dependencies import (
    _CHILD_FAILED_STATES,
    _CHILD_PENDING_STATES,
    RETRY_SUPERSEDE,
    RETRY_WAIT,
)
from .persistence import (
    _persisted_profile_plan,
    _persisted_profile_progress,
    _stored_progress,
)
from .projection_support import (
    _aware,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def tick(self) -> bool:
        """Observe one due cancellation, then advance one ordinary work item."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        now = _aware(self._clock())
        with self._sessions.begin() as session:
            released = bool(release_dead_owner_reservations(session, now))
        converted = (
            bool(convert_due_retained_applications(self._sessions, now)) or released
        )
        if self._switch_adapter is None:
            return converted
        if self._reconcile_selected_roster(now):
            return True
        pending_admission_observed = self._observe_pending_admissions(now) or converted
        parked_observed = self._heal_legacy_applications(now)
        recovery_deferred = False
        cancellation_observed = self._observe_pending_cancellation(now)
        recovery = self._automatic_profile_recovery(now)
        if recovery is not None:
            application_id, actor = recovery
            request_key = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"vonk-forge:profile-cache-recovery:{application_id}",
                )
            )
            try:
                self.retry(
                    application_id,
                    request_key=request_key,
                    actor=actor,
                    automatic_cache_recovery=True,
                )
            except _FleetProfileRecoveryBindingConflict as error:
                preparation_blockers = self._request_recovery_preparations(
                    application_id
                )
                with self._sessions.begin() as session:
                    row = session.get(
                        FleetProfileApplication, application_id, with_for_update=True
                    )
                    if row is not None and self._retry_eligible(session, row):
                        self._park_for_retry(
                            row,
                            _persisted_profile_progress(row),
                            [
                                make_blocker(
                                    ProfileReasonCode.RECOVERY_CACHE_PENDING, str(error)
                                ),
                                *preparation_blockers,
                            ],
                            because=error,
                        )
                        recovery_deferred = True
                # No replacement intent or unknown-output build was admitted.
                # The existing backoff revisits this receipt after cache repair.
            except (FleetProfileConflict, FleetProfilePermissionDenied) as error:
                preparation_blockers = (
                    self._request_recovery_preparations(application_id)
                    if retry_disposition_of(error) == RETRY_WAIT
                    and not is_security_failure(error_code(error))
                    else []
                )
                with self._sessions.begin() as session:
                    row = session.get(
                        FleetProfileApplication, application_id, with_for_update=True
                    )
                    disposition = retry_disposition_of(error)
                    if (
                        row is not None
                        and disposition == RETRY_SUPERSEDE
                        and self._lifecycle.retry_pending(row)
                    ):
                        # What waiting cannot change (a stale review, a lost
                        # selection, a superseded intent) never becomes valid:
                        # end the application so the client re-reviews and
                        # re-submits instead of parking it forever.
                        self._lifecycle.supersede(
                            row,
                            str(error),
                            _aware(self._clock()),
                            code=error.supersede_code
                            if isinstance(error, FleetProfileConflict)
                            else SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION,
                            session=session,
                        )
                        recovery_deferred = True
                    elif (
                        row is not None
                        and disposition == RETRY_WAIT
                        and not is_security_failure(error_code(error))
                        and self._retry_eligible(session, row)
                    ):
                        self._park_for_retry(
                            row,
                            _persisted_profile_progress(row),
                            [
                                make_blocker(
                                    error_code(error)
                                    or ProfileReasonCode.RETRY_CONFLICT,
                                    str(error) or "The profile could not be retried",
                                ),
                                *preparation_blockers,
                            ],
                            because=error,
                        )
                        recovery_deferred = True
            else:
                return True
        with self._sessions.begin() as session:
            cancellation_state = func.coalesce(
                FleetProfileApplication.progress["cancellation"]["state"].as_string(),
                "",
            )
            admission_pending = func.coalesce(
                FleetProfileApplication.progress["admission_pending"].as_boolean(),
                False,
            )
            row = session.scalar(
                select(FleetProfileApplication)
                .where(
                    FleetProfileApplication.state.in_(
                        (LifecycleState.QUEUED.value, LifecycleState.RUNNING.value)
                    ),
                    cancellation_state.not_in(fleet_profile_states.CANCEL_IN_FLIGHT),
                    admission_pending.is_(False),
                    or_(
                        FleetProfileApplication.progress["retry_due_at"]
                        .as_string()
                        .is_(None),
                        func.replace(
                            FleetProfileApplication.progress[
                                "retry_due_at"
                            ].as_string(),
                            "Z",
                            "+00:00",
                        )
                        <= now.isoformat(),
                    ),
                )
                .order_by(
                    case(
                        (FleetProfileApplication.id > self._ordinary_cursor, 0), else_=1
                    )
                    if self._ordinary_cursor is not None
                    else FleetProfileApplication.updated_at,
                    FleetProfileApplication.id,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return (
                    pending_admission_observed
                    or cancellation_observed
                    or parked_observed
                    or recovery_deferred
                )
            self._ordinary_cursor = row.id
            if needs_conversion(row):
                return (
                    pending_admission_observed
                    or cancellation_observed
                    or parked_observed
                    or recovery_deferred
                )
            plan = _persisted_profile_plan(row)
            stored = _stored_progress(row)
            if isinstance(plan, Residue) or isinstance(stored, Residue):
                # Damaged evidence is retired as unknown, never parked and never
                # failed for a person: the load ends cancelled with its effect
                # unknown (what it issued keeps its own lifecycle and receipts),
                # and the selected profile's reconciliation issues a fresh one.
                damaged = plan if isinstance(plan, Residue) else stored
                assert isinstance(damaged, Residue)
                self._lifecycle.cancelled(
                    row,
                    "Profile application retired; its persisted evidence could "
                    f"not be read ({damaged.kind}), so its effect is unknown",
                    now,
                    effect=_LifecycleEffect.UNKNOWN,
                    session=session,
                )
                return True
            progress = stored
            if progress.intended_profile is not None:
                intended = self._intended_profile(row, session=session)
                if isinstance(intended, Residue):
                    self._lifecycle.supersede(
                        row,
                        intended.note,
                        now,
                        code=SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION,
                        effect=_LifecycleEffect.UNKNOWN,
                        session=session,
                    )
                    return True
            if (
                progress.retry_due_at is not None
                and _aware(progress.retry_due_at) > now
            ):
                return (
                    pending_admission_observed
                    or cancellation_observed
                    or parked_observed
                    or recovery_deferred
                )
            if (
                progress.cancellation is not None
                and progress.cancellation.state == LifecycleState.OBSERVING
            ):
                # Defensive parity with the SQL exclusion above. Never let a
                # malformed query or dialect quirk monopolize ordinary work.
                return cancellation_observed
            if not self._application_is_current_selection(session, row, progress):
                self._lifecycle.supersede(
                    row,
                    "Profile order was replaced by a newer fleet profile load; "
                    "issued effects retain their cancellation receipts",
                    now,
                    code=SupersedeCode.SUPERSEDED_BY_INTENT,
                    effect=_LifecycleEffect.UNKNOWN,
                    session=session,
                )
                return True
            if self._superseding_intent(session, row, progress):
                self._lifecycle.supersede(
                    row,
                    "Profile order was replaced by a changed profile or later "
                    "scoped intent; issued effects retain their own cancellation receipts",
                    now,
                    code=SupersedeCode.SUPERSEDED_BY_INTENT,
                    effect=_LifecycleEffect.UNKNOWN,
                    session=session,
                )
                return True
            steps = plan.steps
            if row.current_operation_id:
                try:
                    child = self._switch_adapter.advance(
                        row.current_operation_id, session=session
                    )
                except FleetProfilePermissionDenied as error:
                    self._defer_exact_step(
                        row,
                        _persisted_profile_progress(row),
                        str(error),
                        now,
                        code=ProfileReasonCode.SWITCH_AUTHORITY_UNAVAILABLE,
                    )
                    return True
                except UnknownOutcomeError as error:
                    if retry_disposition_of(error) == RETRY_WAIT:
                        self._defer_exact_step(
                            row, _persisted_profile_progress(row), str(error), now
                        )
                    else:
                        self._lifecycle.cancelled(
                            row,
                            str(error),
                            now,
                            effect=_LifecycleEffect.UNKNOWN,
                            session=session,
                        )
                    return True
                except (KeyError, RuntimeError, ValueError) as error:
                    # A damaged persisted document is retained as the evidence of
                    # what was issued (a missing child record is reconciled before
                    # this point, by issuing the step again).
                    self._lifecycle.fail(
                        row,
                        str(error) or "Child operation is unavailable",
                        now,
                        session=session,
                    )
                    return True
                # The adapter may have checkpointed its child in this same
                # row/transaction. Read that receipt before mirroring progress.
                progress = _persisted_profile_progress(row)
                progress_data = progress.model_dump(mode="json")
                if child.progress is not None:
                    progress_data["child_progress"] = child.progress.model_dump(
                        mode="json"
                    )
                if child.state in _CHILD_PENDING_STATES:
                    # A live child that keeps retrying a phase is a stall the
                    # application names, with its cause, until the child moves.
                    held = [
                        item
                        for item in progress.blockers
                        if item.code == PHASE_RETRY_CODE
                    ]
                    if child.stalls != held:
                        progress_data["blockers"] = [
                            item.model_dump(mode="json")
                            for item in bound_blockers(
                                [
                                    *(
                                        item
                                        for item in progress.blockers
                                        if item.code != PHASE_RETRY_CODE
                                    ),
                                    *child.stalls,
                                ]
                            )
                        ]
                    if child.stalls or held:
                        reason = (
                            (child.status_reason or "")[:512] if child.stalls else None
                        )
                        if row.status_reason != reason:
                            row.status_reason = reason
                if progress_data != progress.model_dump(mode="json"):
                    progress = read_stored_model(
                        FleetProfileApplicationProgress,
                        canonical_message(progress_data),
                        strict=True,
                        from_json=True,
                    )
                    row.progress = progress.model_dump(mode="json")
                if child.state in _CHILD_PENDING_STATES:
                    progress.retry_due_at = FleetProfileAdapter.next_retry(
                        row.id, 1, now
                    )
                    row.progress = progress.model_dump(mode="json")
                    row.updated_at = now
                    if (
                        row.state == LifecycleState.RUNNING.value
                        and not session.is_modified(row)
                    ):
                        return (
                            pending_admission_observed
                            or cancellation_observed
                            or parked_observed
                            or recovery_deferred
                        )
                    self._lifecycle.project(
                        row, now, state=_LifecycleState.RUNNING, session=session
                    )
                    return True
                if child.state in _CHILD_FAILED_STATES:
                    # The load follows its child: a child that ended is not a wait
                    # for a person (none exists), it is a definite end.
                    self._lifecycle.fail(
                        row,
                        child.status_reason
                        or f"Profile step {row.current_step + 1} ended in {child.state}",
                        now,
                        session=session,
                    )
                    return True
                if child.state != LifecycleState.SUCCEEDED.value:
                    self._lifecycle.fail(
                        row,
                        f"Profile step {row.current_step + 1} returned unsupported "
                        f"state {child.state}",
                        now,
                        session=session,
                    )
                    return True
                # This worker dispatches reviewed switch steps only. Preserve
                # the exact native receipt under its immutable plan index.
                progress.step_results[str(row.current_step)] = FleetProfileStepResult(
                    operation_id=child.id,
                    kind=ProfileAction.SWITCH.value,
                    result=child.result,
                )
                progress.retry_due_at = None
                row.progress = progress.model_dump(mode="json")
                row.current_operation_id = None

                row.current_step += 1
            if row.current_step >= len(steps):
                waiting_for_adopted = False
                for effect in plan.effects.adopted:
                    owner = session.get(FleetProfileApplication, effect.application_id)
                    if owner is None or owner.plan_digest != effect.plan_digest:
                        self._lifecycle.fail(
                            row,
                            "Continuing assignment evidence is unavailable; exact effects will be reconciled",
                            now,
                            session=session,
                        )
                        return True
                    owner_plan = _persisted_profile_plan(owner)
                    if isinstance(owner_plan, Residue):
                        self._lifecycle.fail(
                            row,
                            "Continuing assignment evidence is unreadable; exact effects will be reconciled",
                            now,
                            session=session,
                        )
                        return True
                    if owner.state in job_states.words(
                        LifecycleState.FAILED,
                        LifecycleState.CANCELLED,
                        LifecycleState.SUPERSEDED,
                    ):
                        self._lifecycle.fail(
                            row,
                            owner.status_reason
                            or "A continuing assignment ended before the selected profile converged",
                            now,
                            session=session,
                        )
                        return True
                    owner_progress = _persisted_profile_progress(owner)
                    document = owner_progress.switch_adapter
                    for stop in effect.stops:
                        completed = (
                            None
                            if document is None
                            else next(
                                (
                                    child
                                    for child in document.children
                                    if child.queue_index == stop.queue_index
                                    and (
                                        child.operation_id == stop.operation_id
                                        or child.original_operation_id
                                        == stop.operation_id
                                    )
                                    and child.kind == ProfileChildPhase.STOP.value
                                ),
                                None,
                            )
                        )
                        if (
                            completed is None
                            or completed.state != LifecycleState.SUCCEEDED
                            or completed.result is None
                            or not any(
                                isinstance(receipt, RunSwitchStopResult)
                                and receipt.run_id == stop.effect.run_id
                                for receipt in completed.result.run_switch.phase_results
                            )
                        ):
                            waiting_for_adopted = True
                    expected_images = {
                        item.assignment_id: item.runtime_image
                        for item in owner_plan.preparation_decisions
                    }
                    assignments = [
                        item
                        for item in plan.resolved_assignments
                        if item.id in effect.assignment_ids
                    ]
                    waiting_for_adopted |= (
                        owner.state != LifecycleState.SUCCEEDED
                        or any(
                            self._assignment_state(
                                session,
                                item,
                                expected_image=expected_images.get(item.id),
                            ).current_state
                            != item.desired_state
                            for item in assignments
                        )
                    )
                if waiting_for_adopted:
                    progress.retry_due_at = FleetProfileAdapter.next_retry(
                        row.id, 1, now
                    )
                    row.progress = progress.model_dump(mode="json")
                    row.status_reason = "Waiting for exact continuing assignments to finish under the selected profile"
                    row.updated_at = now
                    return True
                continuing_scope = self._adopted_application_scope(session, row)
                self._lifecycle.succeed(row, now, reason=None, session=session)
                progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(
                        {
                            **progress.model_dump(mode="json"),
                            "completed_steps": len(steps),
                            "total_steps": len(steps),
                            "current_label": (
                                "Continuing assignments completed; changed sibling assignments were superseded"
                                if continuing_scope is not None
                                else progress.current_label
                            ),
                        }
                    ),
                    strict=True,
                    from_json=True,
                )
                row.progress = progress.model_dump(mode="json")
                row.result = FleetProfileApplicationResult(
                    changed=bool(steps), completed_steps=len(steps)
                ).model_dump(mode="json")
                row.updated_at = now
                return True
            raw_step = steps[row.current_step]
            self._lifecycle.project(
                row, now, state=_LifecycleState.RUNNING, session=session
            )
            progress = read_stored_model(
                FleetProfileApplicationProgress,
                canonical_message(
                    {
                        **progress.model_dump(mode="json"),
                        "completed_steps": row.current_step,
                        "total_steps": len(steps),
                        "current_label": raw_step.label,
                    }
                ),
                strict=True,
                from_json=True,
            )
            row.progress = progress.model_dump(mode="json")
            row.updated_at = now
            step = raw_step
            application_id = row.id
            step_index = row.current_step
            actor = row.actor

        try:
            started = self._start_step(
                application_id,
                step_index,
                step,
                actor=actor,
            )
        except (
            KeyError,
            RecipeOperationConflict,
            FleetProfileChildPlanBlocked,
            FleetProfileConflict,
            FleetProfilePermissionDenied,
            RuntimeError,
            ValueError,
        ) as error:
            with self._sessions.begin() as session:
                failed = session.get(
                    FleetProfileApplication, application_id, with_for_update=True
                )
                if failed is not None and failed.state in {
                    LifecycleState.QUEUED.value,
                    LifecycleState.RUNNING.value,
                }:
                    try:
                        cancellation = _persisted_profile_progress(failed).cancellation
                    except FleetProfileConflict:
                        cancellation = None
                    if cancellation is not None:
                        # Cancellation can win while the stable child request is
                        # outside the parent transaction. Keep the durable cancel
                        # intent and let the next worker pass reconcile whether
                        # the child was issued; a start error cannot resurrect or
                        # fail that request on its behalf.
                        failed.status_reason = (
                            "Cancellation is reconciling the profile child: "
                            + (str(error)[:360] or "child start was interrupted")
                        )[:512]
                    elif isinstance(error, FleetProfilePermissionDenied) or (
                        isinstance(error, UnknownOutcomeError)
                        and retry_disposition_of(error) == RETRY_WAIT
                    ):
                        failed_progress = _persisted_profile_progress(failed)
                        if self._application_is_current_selection(
                            session, failed, failed_progress
                        ):
                            self._defer_exact_step(
                                failed,
                                failed_progress,
                                str(error),
                                _aware(self._clock()),
                                code=(
                                    ProfileReasonCode.SWITCH_AUTHORITY_UNAVAILABLE
                                    if isinstance(error, FleetProfilePermissionDenied)
                                    else ProfileReasonCode.RETRY_CONFLICT
                                ),
                            )
                        else:
                            self._lifecycle.supersede(
                                failed,
                                "A newer selected intent replaced the retry",
                                _aware(self._clock()),
                                code=SupersedeCode.SUPERSEDED_BY_INTENT,
                                session=session,
                            )
                    elif isinstance(
                        error, FleetProfileReviewStale | UnknownOutcomeError
                    ):
                        # The reviewed plan no longer matches what the child
                        # would do, or an admission owner is busy or evidence is
                        # unavailable: end the load (superseded) so it never
                        # blocks other work, and the client loads again. The
                        # oldest row is otherwise picked again at once, so a
                        # retry here would starve the rest of the queue.
                        self._lifecycle.supersede(
                            failed,
                            str(error),
                            _aware(self._clock()),
                            code=SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION,
                            session=session,
                        )
                    else:
                        self._lifecycle.fail(
                            failed,
                            str(error) or "Profile operation could not be started",
                            _aware(self._clock()),
                            session=session,
                        )
                    failed.updated_at = _aware(self._clock())
            return True
        if isinstance(started, Residue):
            return self._step_unissued(application_id, started)
        operation_id, synchronous, child = started
        with self._sessions.begin() as session:
            current = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if current is None or current.state not in {
                LifecycleState.QUEUED.value,
                LifecycleState.RUNNING.value,
            }:
                return True
            try:
                current_progress = _persisted_profile_progress(current)
                current_progress.retry_due_at = None
                progress_data = current_progress.model_dump(mode="json")
                if synchronous:
                    current.current_step += 1
                    progress_data["completed_steps"] = current.current_step
                else:
                    current.current_operation_id = operation_id
                    progress_data["child_source"] = (
                        ProfileChildSource.SWITCH_ADAPTER.value
                    )
                    if (
                        isinstance(child, FleetProfileChildOperation)
                        and child.progress is not None
                    ):
                        progress_data["child_progress"] = child.progress.model_dump(
                            mode="json"
                        )
                current.progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(progress_data),
                    strict=True,
                    from_json=True,
                ).model_dump(mode="json")
            except FleetProfileConflict as error:
                self._lifecycle.fail(
                    current, str(error), _aware(self._clock()), session=session
                )
            current.updated_at = _aware(self._clock())
        return True
