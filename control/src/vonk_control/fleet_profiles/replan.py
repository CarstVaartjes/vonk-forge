"""Replan for Fleet profiles."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import ValidationError
from sqlalchemy.orm import Session, object_session
from vonk_agent_protocol import LifecycleState, ProfileReasonCode, SupersedeCode
from vonk_forge_contracts import RecipeDefinition

from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileAssignment,
    FleetProfileIntendedConfiguration,
    FleetProfilePreview,
)
from ..lifecycle.evidence import Residue
from ..lifecycle.fleet_profile import FleetProfileAdapter
from ..lifecycle.types import State as _LifecycleState
from ..models import CatalogDocumentRevision, FleetProfile, FleetProfileApplication
from ..operation_blockers import OperationBlocker, bound_blockers, make_blocker
from ..settings import PROFILE_STALL_WINDOW_SECONDS
from ..stall_window import Verdict, progress_verdict
from .assessment_support import (
    _preview_blockers,
    _profile_preview_is_waitable,
    _progress_with_blockers,
)
from .contracts import (
    FleetProfileConflict,
    FleetProfileReviewStale,
)
from .dependencies import _LOGGER
from .persistence import (
    _owns_pending_admission,
    _persisted_profile_plan,
    _persisted_profile_progress,
    _stored_recipe,
    _unreadable_revisions,
)
from .projection_support import (
    _aware,
    _digest,
    _profile_document,
    _validate_remaining_effects,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


def _revision_label(session: Session, item: FleetProfileAssignment) -> str:
    """The recipe title and release version of an assignment's revision."""

    revision = session.get(CatalogDocumentRevision, item.recipe_revision_id)
    document = _stored_recipe(revision) if revision else None
    version = (
        document.release.version if isinstance(document, RecipeDefinition) else None
    )
    return (
        f"{item.recipe_title} {version}"
        if isinstance(version, str)
        else item.recipe_title
    )


class FleetProfileService:
    def _defer_exact_step(
        self,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
        reason: str,
        now: datetime,
        *,
        code: str = ProfileReasonCode.RETRY_CONFLICT,
        ending: Sequence[OperationBlocker] = (),
    ) -> None:
        """Continue the same accepted queue/child identity without replacement admission.

        A wait is not a failure: the load keeps waiting with its reason on the
        bounded retry clock, however long it takes. Only a cancel request or a
        newer load ends it, or a blocker no wait can clear (``ending``): the
        load ends failed with it.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        session = object_session(row)
        if (
            ending
            and session is not None
            and self._end_definite_wait(row, ending, session)
        ):
            return
        progress.attempt += 1
        progress.retry_due_at = FleetProfileAdapter.next_retry(
            row.id, progress.attempt, now
        )
        progress.blockers = bound_blockers([make_blocker(code, reason)])
        row.progress = progress.model_dump(mode="json")
        row.status_reason = f"Waiting for exact accepted effect ({reason}); next attempt at {progress.retry_due_at.isoformat()}"[
            :512
        ]
        row.updated_at = now

    def _replan_admitted(
        self, row: FleetProfileApplication, error: BaseException, now: datetime
    ) -> None:
        """An accepted plan the present refuses is planned again, never retried.

        The load fails its step with the refusal: it backs off, and its
        automatic recovery reviews the same intent against the present and
        admits that fresh plan as its successor.  A recovery that cannot be
        planned, or that keeps meeting the same refusal, ends the lineage
        failed with it (the recovery's own ending and repeat budget).
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        progress = _persisted_profile_progress(row)
        progress.blockers = bound_blockers(
            [make_blocker(ProfileReasonCode.STALE_PLAN, str(error))]
        )
        row.progress = progress.model_dump(mode="json")
        self._lifecycle.fail(
            row, ProfileReasonCode.STALE_PLAN, now, session=object_session(row)
        )

    def _defer_on_residue(
        self,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
        residue: Residue,
        now: datetime,
    ) -> None:
        """Wait for unreadable own evidence for one stall window, then end.

        Unreadable evidence is not a live owner of the wait: if it reads again
        within the window the load continues; otherwise it has stalled and ends
        failed with that evidence (never as a cancel nobody asked for).
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        since = progress.residue_since or now
        if (
            progress_verdict(
                started_at=since,
                progressed_at=None,
                window=timedelta(seconds=PROFILE_STALL_WINDOW_SECONDS),
                now=now,
            )
            is Verdict.STALLED
        ):
            session = object_session(row)
            assert session is not None
            progress.retry_due_at = None
            progress.blockers = bound_blockers(
                [make_blocker(ProfileReasonCode.RETRY_CONFLICT, residue.note)]
            )
            row.progress = progress.model_dump(mode="json")
            self._lifecycle.fail(
                row,
                f"{ProfileReasonCode.RETRY_CONFLICT}: accepted evidence stayed "
                f"unreadable for {PROFILE_STALL_WINDOW_SECONDS}s: {residue.note}",
                now,
                session=session,
            )
            return
        progress.residue_since = since
        self._defer_exact_step(row, progress, residue.note, now)

    def _step_unissued(self, application_id: str, residue: Residue) -> bool:
        """A step could not be issued: the load waits for the evidence.

        Missing, damaged and stale evidence follows the same exact step; only a
        cancel request or a newer accepted intent ends this request.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is not None and row.state in {
                _LifecycleState.QUEUED,
                _LifecycleState.RUNNING,
            }:
                progress = _persisted_profile_progress(row)
                if progress.cancellation is None:
                    self._defer_on_residue(
                        row, progress, residue, _aware(self._clock())
                    )
        return True

    def _replan_blocked_application(
        self, application_id: str, blocked: FleetProfilePreview, actor: str
    ) -> FleetProfilePreview | None:
        """Bind a waiting load to its plan once nothing blocks it any more.

        Returns the admissible plan, now bound to the accepted intent, or
        ``None`` while conditions still block it (the reasons are recorded and
        the preparation it needs is requested again).
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        try:
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, application_id)
                if row is None:
                    return None
                intended = self._intended_profile(row, session=session)
            if isinstance(intended, Residue):
                self._step_unissued(application_id, intended)
                return None
            if any(
                reason.code == ProfileReasonCode.RECIPE_UNAVAILABLE
                for reason in blocked.reasons
            ):
                # No effects have been issued. Resolve the missing choices from
                # the same accepted draft, rather than admitting its partial set.
                fresh = self.preview(
                    blocked.profile_id, excluded_application_id=application_id
                )
                if (
                    fresh.profile_digest != intended.profile_digest
                    or fresh.scope.node_ids != intended.scope.node_ids
                ):
                    self._defer_pending_application(
                        application_id,
                        "Accepted profile content or fleet scope differs from observation",
                        blockers=_preview_blockers(blocked),
                    )
                    return None
            else:
                fresh = self.preview(
                    blocked.profile_id,
                    execution_assignments=tuple(intended.assignments),
                    profile_name=blocked.profile_name,
                    profile_digest=intended.profile_digest,
                    accepted_intent=intended,
                    accepted_profile_revision=blocked.profile_revision,
                    accepted_profile_definition=blocked.profile_definition,
                    excluded_application_id=application_id,
                )
        except (FleetProfileConflict, KeyError) as error:
            self._defer_pending_application(
                application_id,
                str(error) or "Profile admission evidence is unavailable",
                blockers=_preview_blockers(blocked),
                wait_for_space=True,
            )
            return None
        try:
            if fresh.scope.node_ids != blocked.scope.node_ids:
                raise FleetProfileReviewStale(
                    "Profile fleet scope changed before admission resumed; review again"
                )
            _validate_remaining_effects(blocked.effects, fresh.effects)
        except FleetProfileReviewStale as error:
            self._finish_pending_admission(
                application_id,
                state=LifecycleState.SUPERSEDED,
                reason=str(error),
                code=error.supersede_code,
            )
            return None
        if not fresh.allowed:
            self._wait_for_conditions(application_id, fresh, actor)
            return None
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                return None
            progress = _persisted_profile_progress(row)
            if not _owns_pending_admission(row, progress):
                return None
            pending_digest = _digest(
                {
                    "schema_version": 2,
                    "reconciliation_digest": fresh.plan_digest,
                    "retry_of_application_id": None,
                    "request_key": row.request_key,
                }
            )
            assert progress.intended_profile is not None
            row.plan = fresh.model_copy(
                update={"plan_digest": pending_digest}
            ).model_dump(mode="json")
            row.plan_digest = pending_digest
            row.progress = _progress_with_blockers(
                progress.model_copy(
                    update={
                        "intended_profile": progress.intended_profile.model_copy(
                            update={
                                "reviewed_plan_digest": fresh.plan_digest,
                                "assignments": list(fresh.resolved_assignments),
                            }
                        ),
                        "total_steps": len(fresh.steps),
                    }
                ),
                [],
            ).model_dump(mode="json")
            row.updated_at = _aware(self._clock())
        return fresh

    def _follow_newest_recipe_revisions(self, application_id: str, actor: str) -> bool:
        """Re-plan a load that has not started anything on the newest recipes.

        A load waiting to be admitted (for example for a runtime image) has
        touched no Spark yet, so nothing running is swapped by following the
        recipe: profile loads always use the newest revision.  The saved
        profile must be the one that was accepted; a changed profile is a new
        intent and keeps superseding this one.  Returns ``True`` when the
        application was re-planned against the newest revisions and the current
        Fleet.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        try:
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, application_id)
                if row is None:
                    return False
                progress = _persisted_profile_progress(row)
                if (
                    not _owns_pending_admission(row, progress)
                    or row.current_operation_id is not None
                    or row.current_step != 0
                ):
                    return False
                intended = self._intended_profile(row, session=session)
                if isinstance(intended, Residue):
                    return False
                profile = session.get(FleetProfile, row.profile_id)
                if (
                    profile is None
                    or _digest(_profile_document(profile)) != intended.profile_digest
                ):
                    return False
                newest = self._execution_assignments(session, profile)
                accepted = {item.id: item for item in intended.assignments}
                if set(accepted) != {item.id for item in newest}:
                    return False
                moved = [
                    item
                    for item in newest
                    if item.recipe_revision_id != accepted[item.id].recipe_revision_id
                ]
                if not moved:
                    return False
                versions = [_revision_label(session, item) for item in moved]
                followed = intended.model_copy(
                    update={"assignments": sorted(newest, key=lambda item: item.id)}
                )
                plan = _persisted_profile_plan(row)
                if isinstance(plan, Residue):
                    return False
                fresh = self.preview(
                    row.profile_id,
                    execution_assignments=tuple(followed.assignments),
                    profile_name=plan.profile_name,
                    profile_digest=followed.profile_digest,
                    accepted_intent=followed,
                    accepted_profile_revision=plan.profile_revision,
                    accepted_profile_definition=plan.profile_definition,
                    excluded_application_id=application_id,
                )
                # Following a recipe must preserve the same consent boundary
                # as every other pending re-plan. Let ordinary admission name
                # a changed scope or unreviewed destructive effect.
                if fresh.scope.node_ids != plan.scope.node_ids:
                    return False
                _validate_remaining_effects(plan.effects, fresh.effects)
        except (FleetProfileConflict, ValidationError, KeyError):
            # The ordinary admission path reports whatever is unresumable.
            return False
        blockers: list[OperationBlocker] = []
        if not fresh.allowed and _profile_preview_is_waitable(fresh):
            blockers = _preview_blockers(fresh) + self._request_preparations(
                fresh, actor=actor, application_id=application_id
            )
        reason = (
            f"Recipe updated to {', '.join(versions)}; re-planned."
            if versions
            else "Recipe updated; re-planned."
        )
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                return False
            progress = _persisted_profile_progress(row)
            if (
                not _owns_pending_admission(row, progress)
                or row.current_operation_id is not None
                or row.current_step != 0
                or progress.intended_profile != intended
            ):
                return False
            pending_digest = _digest(
                {
                    "schema_version": 2,
                    "reconciliation_digest": fresh.plan_digest,
                    "retry_of_application_id": None,
                    "request_key": row.request_key,
                }
            )
            row.plan = fresh.model_copy(
                update={"plan_digest": pending_digest}
            ).model_dump(mode="json")
            row.plan_digest = pending_digest
            row.progress = _progress_with_blockers(
                progress.model_copy(
                    update={
                        "intended_profile": followed.model_copy(
                            update={"reviewed_plan_digest": fresh.plan_digest}
                        ),
                        "total_steps": len(fresh.steps),
                    }
                ),
                blockers,
                admission_pending=True,
                admission_attempt=0,
                admission_retry_at=now.isoformat(),
            ).model_dump(mode="json")
            self._lifecycle.project(
                row, now, state=_LifecycleState.QUEUED, reason=reason, session=session
            )
        return True

    def _followed_intent(
        self,
        session: Session,
        profile_id: str,
        intended: FleetProfileIntendedConfiguration,
    ) -> tuple[FleetProfileIntendedConfiguration, str] | None:
        """The accepted intent moved onto the newest readable revisions, and why.

        Profiles follow the newest revision.  An accepted assignment whose
        recipe revision no longer reads can never be planned again, so it moves
        to the newest readable revision of the same recipe choice (identified by
        that revision's content digest); every other assignment stays exactly
        as accepted.  ``None`` when every accepted revision reads, when the
        saved profile changed (a new intent, loaded explicitly), or when a
        choice has no readable revision (the load stays ended, naming it).
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        unreadable = _unreadable_revisions(session, intended.assignments)
        if not unreadable:
            return None
        profile = session.get(FleetProfile, profile_id)
        if (
            profile is None
            or _digest(_profile_document(profile)) != intended.profile_digest
        ):
            return None
        try:
            newest = {
                item.id: item for item in self._execution_assignments(session, profile)
            }
        except (FleetProfileConflict, ValidationError):
            return None
        assignments: list[FleetProfileAssignment] = []
        moves: list[str] = []
        for item in intended.assignments:
            stale = unreadable.get(item.id)
            if stale is None:
                assignments.append(item)
                continue
            successor = newest.get(item.id)
            revision = (
                session.get(CatalogDocumentRevision, successor.recipe_revision_id)
                if successor is not None
                else None
            )
            if successor is None or revision is None or revision.id == stale.id:
                return None
            assignments.append(successor)
            moves.append(
                f"recipe revision {stale.id} no longer reads; following "
                f"{_revision_label(session, successor)} revision {revision.id} "
                f"(digest {revision.content_digest})"
            )
        return intended.model_copy(update={"assignments": assignments}), "; ".join(
            moves
        )

    def _hand_over_to_follower(
        self, application_id: str, follower_id: str, moves: str
    ) -> None:
        """The backing-off load names the fresh load that follows the newest
        revision.

        It is superseded by that load (nothing is issued or stopped), keeping
        its own failure in the reason, so whoever watches it follows the new
        load instead of reporting a failure of the recipe.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None or not self._lifecycle.retry_pending(row):
                return
            earlier = (row.status_reason or "").strip()
            reason = (
                f"{moves}: load {follower_id} follows it"
                + (f"; earlier failure: {earlier}" if earlier else "")
            )[:512]
            self._lifecycle.supersede(
                row,
                reason,
                _aware(self._clock()),
                code=SupersedeCode.SUPERSEDED_BY_INTENT,
                by=follower_id,
                session=session,
            )
        _LOGGER.info("profile application %s: %s", application_id, reason)
