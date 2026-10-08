"""Pending admission for Fleet profiles."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import func, or_, select
from vonk_agent_protocol import LifecycleState, SupersedeCode
from vonk_agent_protocol.agent_words import (
    ProfileCancellationCause,
    ProfileOperationKind,
)

from .. import fleet_profile_states, job_states
from ..fleet_profile_adapter_conversion import needs_conversion
from ..fleet_profile_contract import (
    FleetProfileOperationState,
    FleetProfilePreview,
    FleetProfileSupersedeCode,
)
from ..lifecycle.evidence import Residue
from ..lifecycle.fleet_profile import LEGACY_SUPERSEDED_PREFIXES
from ..models import FleetProfileApplication
from ..settings import PROFILE_ADMISSION_OBSERVATION_WAIT_SECONDS
from .assessment_support import (
    _deferral_code,
    _progress_with_blockers,
    _storage_wait_of,
)
from .contracts import (
    FleetProfileAdmissionBusy,
    FleetProfileAdmissionEffectBusy,
    FleetProfileAdmissionStorageError,
    FleetProfileConflict,
    FleetProfilePermissionDenied,
    FleetProfileStalePlanConflict,
)
from .dependencies import _CANCELLED_OPERATION, _MAX_PARKED_APPLICATION_OBSERVATIONS
from .persistence import (
    _owns_pending_admission,
    _persisted_profile_plan,
    _persisted_profile_progress,
)
from .projection_support import (
    _aware,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def _observe_pending_admissions(self, now: datetime) -> bool:
        """Reconcile reviewed applications that could not acquire admission locks."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        candidate: tuple[str, str, str, FleetProfilePreview] | None = None
        retirements: list[str] = []
        # A waiting load follows a newer recipe revision at once, not when its
        # own retry backoff falls due.
        with self._sessions() as session:
            waiting = tuple(
                session.execute(
                    select(FleetProfileApplication.id, FleetProfileApplication.actor)
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
            rows = session.scalars(
                select(FleetProfileApplication)
                .where(
                    FleetProfileApplication.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED, LifecycleState.NEEDS_OPERATOR
                        )
                    ),
                    FleetProfileApplication.current_operation_id.is_(None),
                    FleetProfileApplication.current_step == 0,
                )
                .order_by(
                    FleetProfileApplication.created_at,
                    FleetProfileApplication.id,
                )
                .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
            )
            for row in rows:
                progress = _persisted_profile_progress(row)
                plan = _persisted_profile_plan(row)
                expired = _aware(now) >= _aware(row.created_at) + timedelta(
                    seconds=PROFILE_ADMISSION_OBSERVATION_WAIT_SECONDS
                )
                if progress.cancellation is not None:
                    continue
                if expired:
                    retirements.append(row.id)
                    continue
                if not _owns_pending_admission(row, progress):
                    continue
                if isinstance(plan, Residue):
                    retirements.append(row.id)
                    continue
                # Retry projections cannot hide expired accepted ownership.
                # Only the validated projection controls a live backoff.
                if progress.admission_retry_at is not None and _aware(
                    progress.admission_retry_at
                ) > _aware(now):
                    continue
                if candidate is None:
                    candidate = (row.id, row.request_key, row.actor, plan)
        for application_id in retirements:
            self._finish_pending_admission(
                application_id,
                state=_CANCELLED_OPERATION,
                reason="Profile admission observation ended without readable evidence",
            )
        if candidate is None:
            return bool(retirements)
        application_id, request_key, actor, plan = candidate
        if not plan.allowed:
            # The accepted intent was blocked when it was reviewed: plan it
            # again against current conditions (asking for the preparation it
            # needs) instead of replaying a plan that could never be admitted.
            replanned = self._replan_blocked_application(application_id, plan, actor)
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
            self._prepare_pending_admission(application_id, plan)
            self._queue_application(
                plan,
                request_key=request_key,
                actor=actor,
                operation_kind=ProfileOperationKind.APPLY.value,
                pending_application_id=application_id,
                platform_maintenance=True,
            )
        except (
            FleetProfileAdmissionBusy,
            FleetProfileAdmissionEffectBusy,
            FleetProfileAdmissionStorageError,
        ) as error:
            self._defer_pending_application(
                application_id,
                str(error),
                code=_deferral_code(error),
                storage=_storage_wait_of(error),
            )
            return True
        except FleetProfileStalePlanConflict as error:
            self._finish_pending_admission(
                application_id,
                state=LifecycleState.SUPERSEDED,
                reason=f"Pending profile intent was superseded: {error}",
                code=SupersedeCode.EFFECTS_CHANGED_DURING_ADMISSION,
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

    def _finish_pending_admission(
        self,
        application_id: str,
        *,
        state: FleetProfileOperationState,
        reason: str,
        code: FleetProfileSupersedeCode | None = None,
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
            expired_wait = (
                row.current_operation_id is None
                and row.current_step == 0
                and row.state
                in job_states.words(
                    LifecycleState.QUEUED, LifecycleState.NEEDS_OPERATOR
                )
                and progress.cancellation is None
                and now
                >= _aware(row.created_at)
                + timedelta(seconds=PROFILE_ADMISSION_OBSERVATION_WAIT_SECONDS)
            )
            if not _owns_pending_admission(row, progress) and not expired_wait:
                return
            row.progress = _progress_with_blockers(
                progress,
                progress.blockers,
                admission_pending=False,
                admission_retry_at=None,
            ).model_dump(mode="json")
            if state == ProfileCancellationCause.SUPERSEDED.value:
                assert code is not None
                self._lifecycle.supersede(row, reason, now, code=code, session=session)
            elif state == LifecycleState.CANCELLED.value:
                self._lifecycle.cancelled(row, reason, now, session=session)
            else:
                self._lifecycle.fail(row, reason, now, session=session)

    def _heal_legacy_applications(self, now: datetime) -> bool:
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
