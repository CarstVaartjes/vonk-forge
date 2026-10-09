"""Preparation for Fleet profiles."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    ProfileReasonCode,
    RecipeImageCode,
    RunSwitchCode,
)
from vonk_agent_protocol.agent_words import ProfileReasonSeverity

from .. import job_states
from ..fleet_profile_contract import FleetProfileApplicationView, FleetProfilePreview
from ..lifecycle.evidence import Residue
from ..models import FleetProfileApplication
from ..operation_blockers import OperationBlocker, make_blocker
from ..profile_capacity import restore_released_profile_claims
from ..settings import STORAGE_ADMISSION_WAIT_SECONDS
from ..storage_demands import StorageRelief
from .assessment_support import (
    _assignments_needing_preparation,
    _disk_shortfalls,
    _progress_with_blockers,
    _relief_blocker,
)
from .contracts import (
    FleetProfileConflict,
    PreparationCanceller,
    PreparationStarter,
    StorageReliefProvider,
)
from .dependencies import _CHILD_PENDING_STATES, _LOGGER
from .persistence import (
    _application_order_key,
    _newer_profile_intent_overlaps,
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
    def _resume_live_child(
        self, application_id: str
    ) -> FleetProfileApplicationView | None:
        """Hand a failed order whose child is still live back to advancement.

        A transient error while advancing a live child marks the order failed,
        but only the running path ever advances that child again.  Refusing a
        retry because the child "is still active" therefore parked the current
        profile intent forever (live: a profile load stopped the old run, then
        never started the new one).  The child is the same authorized work, so
        continuing it is the retry; the bounded retry schedule rate-limits it.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if (
                row is None
                or row.state
                not in job_states.words(
                    LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
                )
                or not row.current_operation_id
                or self._switch_adapter is None
            ):
                return None
            progress = _persisted_profile_progress(row)
            if now >= _aware(row.created_at) + timedelta(
                seconds=STORAGE_ADMISSION_WAIT_SECONDS
            ):
                self._defer_exact_step(
                    row, progress, "Accepted effect observation budget expired", now
                )
                return self._application_view(row)
            if (
                progress.cancellation is not None
                or not self._application_is_current_selection(session, row, progress)
            ):
                return None
            try:
                child = self._switch_adapter.get(
                    row.current_operation_id, session=session
                )
            except (KeyError, RuntimeError, ValueError):
                return None
            if child.state not in _CHILD_PENDING_STATES:
                return None
            self._lifecycle.reopen(
                row,
                now,
                reason="Resumed advancing the live child operation",
                session=session,
            )
            # Failing released its unassigned claims; the resumed child needs them.
            restore_released_profile_claims(
                session,
                row,
                node_ids=self._adopted_application_scope(session, row),
            )
            session.flush()
            return self._application_view(row)

    def retry_eligible(self, application_id: str) -> bool:
        """Whether this receipt is still the current recoverable profile intent."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions() as session:
            row = session.get(FleetProfileApplication, application_id)
            return row is not None and self._retry_eligible(session, row)

    def _retry_eligible(self, session: Session, row: FleetProfileApplication) -> bool:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        if row.state not in job_states.words(
            LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
        ):
            return False
        try:
            progress = _stored_progress(row)
            if isinstance(progress, Residue):
                return False
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            # A damaged receipt cannot prove that it still carries the current
            # recoverable intent, so it simply does not advertise retry.
            return False
        if progress.intended_profile is None:
            return False
        try:
            superseded = self._superseding_intent(session, row, progress)
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            return False
        if superseded:
            return False
        try:
            plan = _persisted_profile_plan(row)
            if isinstance(plan, Residue):
                return False
            effect_nodes = {node_id for step in plan.steps for node_id in step.node_ids}
            if _newer_profile_intent_overlaps(
                session, _application_order_key(session, row), effect_nodes
            ):
                return False
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            return False
        # Only the accepted order and overlapping node fences own effects.
        # A historical retry child or an older/nonoverlapping sibling is not
        # another admission authority.
        return True

    def bind_preparation_starter(self, starter: PreparationStarter) -> None:
        """Attach the Controller's preparation authority after startup wiring."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        self._preparation_starter = starter

    def bind_storage_relief(self, relief: StorageReliefProvider) -> None:
        """Attach the authority that frees disk for a load waiting on it."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        self._storage_relief = relief

    def bind_preparation_canceller(self, canceller: PreparationCanceller) -> None:
        """Attach the authority that cancels a preparation a load asked for."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        self._preparation_canceller = canceller

    def _cancel_owned_preparations(
        self, application_id: str, plan: FleetProfilePreview, *, actor: str
    ) -> None:
        """Cancel what a cancelled waiting load asked the Controller to prepare.

        Best effort and self-healing: the application is already cancelled, so
        a preparation left behind is only useful work the next load reuses.
        Revisions another live application still waits on are kept.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        canceller = self._preparation_canceller
        if canceller is None:
            return
        revisions = {
            assignment.recipe_revision_id
            for assignment in _assignments_needing_preparation(
                plan.assignments,
                {item.assignment_id for item in plan.preparations},
                plan.reasons,
                plan.assessments,
            )
        }
        cancelled: list[str] = []
        for revision_id in sorted(revisions):
            with self._sessions() as session:
                others = tuple(
                    session.scalars(
                        select(FleetProfileApplication).where(
                            FleetProfileApplication.id != application_id,
                            FleetProfileApplication.state.in_(
                                job_states.words(
                                    LifecycleState.QUEUED,
                                    LifecycleState.RUNNING,
                                    LifecycleState.NEEDS_OPERATOR,
                                )
                            ),
                        )
                    )
                )
                shared = False
                for other in others:
                    other_plan = _persisted_profile_plan(other)
                    if isinstance(other_plan, Residue):
                        continue
                    if any(
                        item.recipe_revision_id == revision_id
                        for item in other_plan.resolved_assignments
                    ):
                        shared = True
                        break
            if shared:
                continue
            try:
                cancelled.extend(
                    canceller(
                        revision_id,
                        actor=actor,
                        reason=f"Profile application {application_id} was cancelled",
                        application_id=application_id,
                    )
                )
            except Exception:  # noqa: BLE001 -- cancellation is accepted; evidence is logged
                _LOGGER.warning(
                    "could not cancel preparation of %s for cancelled application %s",
                    revision_id,
                    application_id,
                    exc_info=True,
                )
        if cancelled:
            with self._sessions.begin() as session:
                row = session.get(FleetProfileApplication, application_id)
                if row is not None and row.state == LifecycleState.CANCELLED.value:
                    row.status_reason = (
                        (row.status_reason or "")
                        + f" Cancelled pending preparation: {', '.join(cancelled)}."
                    )[:512]

    def _request_preparations(
        self,
        preview: FleetProfilePreview,
        *,
        actor: str,
        application_id: str | None = None,
    ) -> list[OperationBlocker]:
        """Enqueue the model and image preparation a blocked load is missing.

        A load asks for what it needs: an assignment that must place assets the
        Controller has not prepared gets its preparation started here, and the
        returned reasons say how far along it is. Nothing the fleet or recipe
        cannot resolve by itself starts a preparation.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        blockers = self._request_storage(preview)
        starter = self._preparation_starter
        if starter is None:
            return blockers
        for assignment in _assignments_needing_preparation(
            preview.assignments,
            {item.assignment_id for item in preview.preparations},
            preview.reasons,
            preview.assessments,
        ):
            try:
                blockers.extend(
                    starter(
                        assignment.recipe_revision_id,
                        actor=actor,
                        application_id=application_id,
                    )
                )
            except Exception as error:  # noqa: BLE001 - a load never fails on this
                blockers.append(
                    make_blocker(
                        ProfileReasonCode.PREPARATION_NOT_STARTED,
                        f"Preparing {assignment.recipe_title} could not be "
                        f"started yet: {error}",
                        severity=ProfileReasonSeverity.WARNING.value,
                    )
                )
        return blockers

    def _request_storage(self, preview: FleetProfilePreview) -> list[OperationBlocker]:
        """Ask for the disk a load was refused for, and say how that is going.

        A load that does not fit a Spark's free disk waits (it is not refused for
        good): the Controller removes the least recently used installations
        nothing uses until it fits, and the load retries by itself. The reason
        shown names the bytes needed and the bytes that can be freed.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        if self._storage_relief is None:
            return []
        blockers: list[OperationBlocker] = []
        for item in preview.assessments:
            for node_id, needed in _disk_shortfalls(item.assessment):
                found = self._ask_storage_relief(node_id, needed, preview.profile_id)
                if found is not None:
                    blockers.append(_relief_blocker(node_id, found))
        return blockers

    def _ask_storage_relief(
        self, node_id: str, required_free_bytes: int, profile_id: str
    ) -> StorageRelief | None:
        """Register the demand on one Spark and read what eviction can do."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        relief = self._storage_relief
        if relief is None:
            return None
        try:
            return relief(
                node_id,
                required_free_bytes,
                source="profile-load",
                subject=profile_id,
                reason=RunSwitchCode.INSUFFICIENT_DISK,
            )
        except Exception:  # noqa: BLE001 -- relief is optional; admission owns the decision
            _LOGGER.warning("storage relief failed", exc_info=True)
            return None

    def _request_recovery_preparations(
        self, application_id: str
    ) -> list[OperationBlocker]:
        """Re-plan accepted intent and request assets outside the parent transaction."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions() as session:
            row = session.get(FleetProfileApplication, application_id)
            if row is None:
                return []
            intended = self._intended_profile(row, session=session)
            plan = _persisted_profile_plan(row)
            actor = row.actor
        if isinstance(intended, Residue) or isinstance(plan, Residue):
            return []
        preview = self.preview(
            plan.profile_id,
            execution_assignments=tuple(intended.assignments),
            profile_name=plan.profile_name,
            profile_digest=intended.profile_digest,
            accepted_intent=intended,
            accepted_profile_revision=plan.profile_revision,
            accepted_profile_definition=plan.profile_definition,
            profile_application_id=application_id,
        )
        return self._request_preparations(
            preview, actor=actor, application_id=application_id
        )

    def _end_exhausted_preparation(
        self,
        row: FleetProfileApplication,
        blockers: Sequence[OperationBlocker],
        session: Session,
    ) -> bool:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        exhausted = next(
            (
                item
                for item in blockers
                if item.code == RecipeImageCode.PREPARATION_EXHAUSTED
            ),
            None,
        )
        if exhausted is None:
            return False
        progress = _persisted_profile_progress(row)
        row.progress = _progress_with_blockers(
            progress,
            [exhausted, *(item for item in blockers if item is not exhausted)],
            admission_pending=False,
            admission_retry_at=None,
            retry_due_at=None,
        ).model_dump(mode="json")
        self._lifecycle.fail(
            row, exhausted.detail, _aware(self._clock()), session=session
        )
        return True
