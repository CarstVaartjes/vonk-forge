"""Pending admission for Fleet profiles."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import ValidationError
from sqlalchemy import func, or_, select, true
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.agent_words import (
    ProfileCancellationCause,
    ProfileOperationKind,
    ProfileReasonSeverity,
)

from .. import fleet_profile_states, job_states
from ..admission_locking import patient_admission
from ..fleet_profile_adapter_conversion import needs_conversion
from ..fleet_profile_contract import (
    FleetProfileOperationState,
    FleetProfilePreview,
    FleetProfileSupersedeCode,
)
from ..lifecycle.evidence import Residue
from ..lifecycle.fleet_profile import LEGACY_SUPERSEDED_PREFIXES
from ..models import FleetProfile, FleetProfileApplication
from ..operation_blockers import OperationBlocker, make_blocker
from .assessment_support import (
    _deferral_code,
    _progress_with_blockers,
    _storage_wait_of,
)
from .contracts import (
    FleetProfileAdmissionBusy,
    FleetProfileAdmissionDefect,
    FleetProfileAdmissionEffectBusy,
    FleetProfileConflict,
    FleetProfilePermissionDenied,
    FleetProfilePortPromiseConflict,
    FleetProfileStalePlanConflict,
    _FleetProfileSupersededIntentConflict,
)
from .dependencies import (
    _CANCELLED_OPERATION,
    _LOGGER,
    _MAX_PARKED_APPLICATION_OBSERVATIONS,
)
from .persistence import (
    _owns_pending_admission,
    _persisted_profile_plan,
    _persisted_profile_progress,
    _stored_progress,
)
from .projection_support import (
    _aware,
    _digest,
    _profile_document,
)

#: Every stored ISO time sorts below this; anything above it is not a time.


def _raw_pending(row: FleetProfileApplication) -> bool:
    """Whether the stored document, readable or not, says admission is pending."""
    stored = row.progress
    return isinstance(stored, dict) and stored.get("admission_pending") is True


if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def _observe_pending_admissions(
        self, now: datetime, requested_id: str | None = None
    ) -> bool:
        """Reconcile reviewed applications that could not acquire admission locks."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        candidate: tuple[str, str, str, FleetProfilePreview, int] | None = None
        retirements: list[str] = []
        # A waiting load follows a newer recipe revision at once, not when its
        # own retry backoff falls due.
        with self._sessions() as session:
            waiting = tuple(
                session.execute(
                    select(FleetProfileApplication.id, FleetProfileApplication.actor)
                    .where(
                        FleetProfileApplication.id == requested_id
                        if requested_id is not None
                        else true()
                    )
                    .where(
                        FleetProfileApplication.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED, LifecycleState.NEEDS_OPERATOR
                            )
                        ),
                        FleetProfileApplication.current_operation_id.is_(None),
                        FleetProfileApplication.current_step == 0,
                    )
                    .order_by(FleetProfileApplication.created_at)
                    .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
                )
            )
        for waiting_id, waiting_actor in waiting:
            if self._follow_newest_recipe_revisions(waiting_id, waiting_actor):
                return True
        with self._sessions() as session:
            retry_at = FleetProfileApplication.next_action_at
            rows = session.scalars(
                select(FleetProfileApplication)
                .where(
                    FleetProfileApplication.id == requested_id
                    if requested_id is not None
                    else true()
                )
                .where(
                    FleetProfileApplication.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED, LifecycleState.NEEDS_OPERATOR
                        )
                    ),
                    FleetProfileApplication.current_operation_id.is_(None),
                    FleetProfileApplication.current_step == 0,
                    # Backoff rows cannot crowd due work out of the batch. A
                    # projection that is no time at all sorts after every time
                    # and is observed now (retired as unreadable), never left
                    # to hide the load.
                    or_(
                        retry_at.is_(None),
                        retry_at <= _aware(now),
                    ),
                )
                .order_by(
                    FleetProfileApplication.created_at.desc(),
                    FleetProfileApplication.id.desc(),
                )
                .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
            )
            for row in rows:
                if isinstance(_stored_progress(row), Residue):
                    # Unreadable progress of a pending admission is retired as
                    # unknown at once, as the ordinary reconcile retires the
                    # rest (which it owns): no clock decides it.
                    if _raw_pending(row):
                        retirements.append(row.id)
                    continue
                progress = _persisted_profile_progress(row)
                plan = _persisted_profile_plan(row)
                # A load waiting in the admission queue waits on a live owner:
                # it never expires; a refusal, a cancel or a newer intent ends it.
                if progress.cancellation is not None:
                    continue
                if not _owns_pending_admission(row, progress):
                    continue
                if isinstance(plan, Residue):
                    retirements.append(row.id)
                    continue
                # Retry projections cannot hide expired accepted ownership.
                # Only the validated projection controls a live backoff.
                if row.next_action_at is not None and _aware(
                    row.next_action_at
                ) > _aware(now):
                    continue
                if candidate is None:
                    candidate = (
                        row.id,
                        row.request_key,
                        row.actor,
                        plan,
                        progress.admission_attempt,
                    )
        for application_id in retirements:
            self._finish_pending_admission(
                application_id,
                state=_CANCELLED_OPERATION,
                reason="Profile admission observation ended without readable evidence",
            )
        if candidate is None:
            return bool(retirements)
        application_id, request_key, actor, plan, refused = candidate
        if not plan.allowed:
            # The accepted intent was blocked when it was reviewed: plan it
            # again against current conditions (asking for the preparation it
            # needs) instead of replaying a plan that could never be admitted.
            replanned = self._replan_pending_application(application_id, plan, actor)
            if replanned is None:
                return True
            plan = replanned
        try:
            # The persisted plan is already bound to the execution request.
            # Admission consumes the original reviewed identity and binds it
            # once; reusing the execution digest here would hash it twice.
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, application_id)
                if row is None:
                    return False
                intended = self._intended_profile(row, session=session)
                roster_reconciliation = row.selection_generation is not None
            if isinstance(intended, Residue):
                self._finish_pending_admission(
                    application_id,
                    state=_CANCELLED_OPERATION,
                    reason="Profile admission retired: its accepted intent could "
                    "not be read",
                )
                return True
            plan = plan.model_copy(
                update={"plan_digest": intended.reviewed_plan_digest}
            )
            # A try-lock never queues: a writer that takes these locks often
            # enough refuses every try. After repeated refusals this retry
            # queues for them instead, bounded by the patient lock timeout.
            with patient_admission(refused):
                # Maintenance is not selected until admission replaces its
                # source. Use the same queue path as its initial attempt; that
                # transaction validates the source selection and fences work.
                if not roster_reconciliation:
                    self._prepare_pending_admission(application_id, plan)
                self._queue_application(
                    plan,
                    request_key=request_key,
                    actor=actor,
                    operation_kind=ProfileOperationKind.APPLY.value,
                    pending_application_id=application_id,
                    platform_maintenance=True,
                )
        except FleetProfilePortPromiseConflict as error:
            # The stored plan promises a port that is not free (any more):
            # plan again, so the next attempt promises a free one.
            if self._replan_pending_application(application_id, plan, actor):
                self._defer_pending_application(
                    application_id,
                    str(error),
                    code=_deferral_code(error),
                    retry_delay=timedelta(0),
                )
            return True
        except (FleetProfileAdmissionBusy, FleetProfileAdmissionEffectBusy) as error:
            self._defer_pending_application(
                application_id,
                str(error),
                code=_deferral_code(error),
                storage=_storage_wait_of(error),
            )
            return True
        except FleetProfileAdmissionDefect as error:
            # A defect fails at once with evidence; it is never retried.
            self._finish_pending_admission(
                application_id,
                state=LifecycleState.FAILED,
                reason=str(error),
                evidence=make_blocker(
                    error.code,
                    str(error),
                    severity=ProfileReasonSeverity.ERROR.value,
                ),
            )
            return True
        except _FleetProfileSupersededIntentConflict as error:
            # A newer accepted load owns these Sparks: the newest request steers.
            self._finish_pending_admission(
                application_id,
                state=LifecycleState.SUPERSEDED,
                reason=f"Pending profile intent was superseded: {error}",
                code=error.supersede_code,
            )
            return True
        except FleetProfileStalePlanConflict as error:
            # What the plan was reviewed against changed before admission bound
            # it: review the same intent again, as for a taken port. A fresh
            # review identical to the refused one cannot change the outcome, so
            # the refusal is deterministic and ends the load with it.
            replanned = self._replan_pending_application(application_id, plan, actor)
            if replanned is None:
                return True
            if replanned.plan_digest == plan.plan_digest:
                self._finish_pending_admission(
                    application_id,
                    state=LifecycleState.FAILED,
                    reason=error.code,
                    evidence=make_blocker(
                        error.code,
                        str(error),
                        severity=ProfileReasonSeverity.ERROR.value,
                    ),
                )
                return True
            _LOGGER.info(
                "profile application %s re-planned after a stale plan (%s): "
                "plan %s -> %s",
                application_id,
                error,
                plan.plan_digest,
                replanned.plan_digest,
            )
            self._defer_pending_application(
                application_id, str(error), code=error.code, retry_delay=timedelta(0)
            )
            return True
        except (FleetProfileConflict, KeyError) as error:
            self._defer_pending_application(
                application_id,
                str(error) or "Profile admission evidence is unavailable",
            )
            return True
        except FleetProfilePermissionDenied as error:
            self._finish_pending_admission(
                application_id,
                state=LifecycleState.FAILED,
                reason=str(error) or "Profile admission could not be resumed",
            )
            return True
        return True

    def _replan_pending_application(
        self, application_id: str, plan: FleetProfilePreview, actor: str
    ) -> FleetProfilePreview | None:
        """Plan a waiting load again, unless a newer save superseded it.

        A profile saved after this load was accepted is the newer request: the
        newest request steers, and planning again would load the content it
        replaced. Every re-plan in the admission observer goes through here.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        if self._profile_saved_since_accepted(application_id):
            self._finish_pending_admission(
                application_id,
                state=LifecycleState.SUPERSEDED,
                reason=FleetProfileSupersedeCode.SUPERSEDED_BY_INTENT,
                code=FleetProfileSupersedeCode.SUPERSEDED_BY_INTENT,
            )
            return None
        return self._replan_blocked_application(application_id, plan, actor)

    def _profile_saved_since_accepted(self, application_id: str) -> bool:
        """Whether the saved profile is no longer the one this load accepted."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions() as session:
            row = session.get(FleetProfileApplication, application_id)
            # A roster reconciliation re-applies the selected intent: a saved
            # draft is no newer load of it (admission checks drafts only).
            if row is None or row.selection_generation is not None:
                return False
            profile = session.get(FleetProfile, row.profile_id)
            if profile is None:
                return True
            try:
                return _digest(_profile_document(profile)) != row.profile_digest
            except ValidationError:
                # An unreadable saved profile proves no edit: the ordinary
                # review reports it.
                return False

    def _finish_pending_admission(
        self,
        application_id: str,
        *,
        state: FleetProfileOperationState,
        reason: str,
        code: FleetProfileSupersedeCode | None = None,
        evidence: OperationBlocker | None = None,
    ) -> None:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                return
            progress = _persisted_profile_progress(row)
            # Damaged progress names no pending admission any more; a queued
            # load that issued nothing is still this observer's to retire.
            damaged = (
                isinstance(_stored_progress(row), Residue)
                and _raw_pending(row)
                and row.current_operation_id is None
                and row.current_step == 0
                and row.state
                in job_states.words(
                    LifecycleState.QUEUED, LifecycleState.NEEDS_OPERATOR
                )
            )
            if not _owns_pending_admission(row, progress) and not damaged:
                return
            if not damaged:
                # Damaged progress is kept as the evidence of what was stored.
                row.progress = _progress_with_blockers(
                    progress,
                    progress.blockers if evidence is None else [evidence],
                    admission_pending=False,
                ).model_dump(mode="json")
            if state == ProfileCancellationCause.SUPERSEDED.value:
                assert code is not None
                self._lifecycle.supersede(row, reason, now, code=code, session=session)
            elif state == LifecycleState.CANCELLED.value:
                self._lifecycle.cancelled(row, reason, now, session=session)
            else:
                self._lifecycle.fail(row, reason, now, session=session)

    def _heal_legacy_applications(
        self, now: datetime, application_id: str | None = None
    ) -> bool:
        """Re-evaluate a legacy ``waiting-for-operator`` load: it runs again.

        An older Controller mirrored a child that waited for a person onto the load
        and left it parked: the ordinary advancement never revisited it.  No profile
        (or Run/Switch) action exists, so nothing can end that wait.  The load is
        returned to the work queue (rules 1 and 3): the next ordinary tick observes
        its exact child and records whatever that child concluded, exactly as for any
        running load; one still being admitted retries its admission.  Nothing is
        issued here and no child is touched, so running workloads are never
        disturbed.  Bounded per tick, and idempotent after a restart.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        with self._sessions.begin() as session:
            rows = tuple(
                session.scalars(
                    select(FleetProfileApplication)
                    .where(
                        FleetProfileApplication.id == application_id
                        if application_id is not None
                        else true()
                    )
                    .where(
                        FleetProfileApplication.state.in_(
                            job_states.words(LifecycleState.NEEDS_OPERATOR)
                        ),
                        func.coalesce(
                            FleetProfileApplication.progress["cancellation"][
                                "state"
                            ].as_string(),
                            "",
                        ).not_in(fleet_profile_states.CANCEL_IN_FLIGHT),
                    )
                    .order_by(
                        FleetProfileApplication.created_at,
                        FleetProfileApplication.id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
                )
            )
            for row in rows:
                if needs_conversion(row):
                    continue
                self._lifecycle.heal(row, now, session=session)
            legacy = tuple(
                session.scalars(
                    select(FleetProfileApplication)
                    .where(
                        FleetProfileApplication.id == application_id
                        if application_id is not None
                        else true()
                    )
                    .where(
                        or_(
                            *(
                                FleetProfileApplication.status_reason.startswith(prefix)
                                for prefix in LEGACY_SUPERSEDED_PREFIXES
                            )
                        ),
                        FleetProfileApplication.state.in_(
                            (
                                LifecycleState.FAILED.value,
                                LifecycleState.CANCELLED.value,
                            )
                        ),
                    )
                    .order_by(
                        FleetProfileApplication.created_at,
                        FleetProfileApplication.id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
                )
            )
            relabelled = [
                self._lifecycle.heal_superseded(row, now)
                for row in legacy
                if not needs_conversion(row)
            ]
            return bool(rows) or any(relabelled)
