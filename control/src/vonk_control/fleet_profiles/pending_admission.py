"""Pending admission for Fleet profiles."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import ValidationError
from sqlalchemy import or_, select, true
from vonk_agent_protocol.agent_words import (
    ProfileCancellationCause,
    ProfileOperationKind,
    ProfileReasonSeverity,
)
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.reason_codes.profiles import ProfileReasonCode

from ..admission_locking import patient_admission
from ..fleet_profile_contract.review import FleetProfilePreview
from ..fleet_profile_contract.vocabulary import (
    FleetProfileOperationState,
    FleetProfileSupersedeCode,
)
from ..jobs import states as job_states
from ..lifecycle.evidence import Residue
from ..models.fleet import FleetProfile, FleetProfileApplication
from ..operations.blockers import OperationBlocker, make_blocker
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
    FleetProfileRecheckContractDefect,
    FleetProfileStalePlanConflict,
    _FleetProfileSupersededIntentConflict,
)
from .dependencies import (
    _CANCELLED_OPERATION,
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

        candidate: tuple[str, str, str, FleetProfilePreview, int, str | None] | None = (
            None
        )
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
                            job_states.words(LifecycleState.QUEUED)
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
                        job_states.words(LifecycleState.QUEUED)
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
                    # The review a stale refusal was made against, if any.
                    stale_review = (
                        progress.intended_profile.reviewed_plan_digest
                        if progress.intended_profile is not None
                        and any(
                            blocker.code == ProfileReasonCode.STALE_PLAN
                            for blocker in progress.blockers
                        )
                        else None
                    )
                    candidate = (
                        row.id,
                        row.request_key,
                        row.actor,
                        plan,
                        progress.admission_attempt,
                        stale_review,
                    )
        for application_id in retirements:
            self._finish_pending_admission(
                application_id,
                state=_CANCELLED_OPERATION,
                reason="Profile admission observation ended without readable evidence",
            )
        if candidate is None:
            return bool(retirements)
        application_id, request_key, actor, plan, refused, stale_review = candidate
        if not plan.allowed or stale_review is not None:
            # Blocked or stale reviewed evidence must be assessed afresh before
            # another admission attempt, including after a Controller restart.
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
            # it. The stale mark makes the next tick review the same intent
            # afresh, once, before admitting it. A plan reviewed afresh this
            # tick that is refused again ends the load when another review
            # cannot change it: its recheck contract failed, or the fresh
            # review is identical to the refused one.
            if stale_review is not None and (
                isinstance(error, FleetProfileRecheckContractDefect)
                or plan.plan_digest == stale_review
            ):
                code = (
                    ProfileReasonCode.ADMISSION_DEFECT
                    if isinstance(error, FleetProfileRecheckContractDefect)
                    else error.code
                )
                self._finish_pending_admission(
                    application_id,
                    state=LifecycleState.FAILED,
                    reason=code,
                    evidence=make_blocker(
                        code,
                        str(error),
                        severity=ProfileReasonSeverity.ERROR.value,
                    ),
                )
                return True
            if self._superseded_by_saved_profile(application_id):
                return True
            self._defer_pending_application(
                application_id,
                str(error),
                code=ProfileReasonCode.STALE_PLAN,
                retry_delay=timedelta(0),
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
        if self._superseded_by_saved_profile(application_id):
            return None
        return self._replan_blocked_application(application_id, plan, actor)

    def _superseded_by_saved_profile(self, application_id: str) -> bool:
        """End a waiting load a newer profile save replaced; the newest steers."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        if not self._profile_saved_since_accepted(application_id):
            return False
        self._finish_pending_admission(
            application_id,
            state=LifecycleState.SUPERSEDED,
            reason=FleetProfileSupersedeCode.SUPERSEDED_BY_INTENT,
            code=FleetProfileSupersedeCode.SUPERSEDED_BY_INTENT,
        )
        return True

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
                and row.state in job_states.words(LifecycleState.QUEUED)
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
