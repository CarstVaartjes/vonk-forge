"""Replan for Fleet profiles."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import ValidationError
from vonk_agent_protocol import LifecycleState, ProfileReasonCode
from vonk_forge_contracts import RecipeDefinition

from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfilePreview,
)
from ..lifecycle.evidence import BookkeepingReason, Residue
from ..lifecycle.fleet_profile import FleetProfileAdapter
from ..lifecycle.types import Effect as _LifecycleEffect
from ..lifecycle.types import State as _LifecycleState
from ..models import CatalogDocumentRevision, FleetProfile, FleetProfileApplication
from ..operation_blockers import OperationBlocker, bound_blockers, make_blocker
from ..stored_json import read_row_column
from .assessment_support import (
    _preview_blockers,
    _profile_preview_is_waitable,
    _progress_with_blockers,
    _storage_wait_of_preview,
)
from .contracts import (
    FleetProfileConflict,
)
from .dependencies import _CANCELLED_OPERATION
from .persistence import (
    _owns_pending_admission,
    _persisted_profile_plan,
    _persisted_profile_progress,
)
from .projection_support import (
    _aware,
    _digest,
    _profile_document,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    @staticmethod
    def _defer_exact_step(
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
        reason: str,
        now: datetime,
        *,
        code: ProfileReasonCode = ProfileReasonCode.RETRY_CONFLICT,
    ) -> None:
        """Continue the same accepted queue/child identity without replacement admission."""
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

    def _step_unissued(self, application_id: str, residue: Residue) -> bool:
        """A step could not be issued: wait for the evidence, or retire the load.

        Evidence that is only *not there yet* (an executor not bound) leaves the
        load where it is and is looked at again on the next pass.  Evidence that is
        damaged or no longer matches retires the load as superseded with its effect
        unknown (what it already issued keeps its own lifecycle), so the selected
        profile's reconciliation can issue a fresh one.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        if residue.reason is BookkeepingReason.EVIDENCE_UNAVAILABLE:
            with self._sessions.begin() as session:
                row = session.get(
                    FleetProfileApplication, application_id, with_for_update=True
                )
                if row is not None and row.state in {
                    _LifecycleState.QUEUED,
                    _LifecycleState.RUNNING,
                }:
                    progress = _persisted_profile_progress(row)
                    if (
                        progress.cancellation is None
                        and self._application_is_current_selection(
                            session, row, progress
                        )
                    ):
                        self._defer_exact_step(
                            row, progress, residue.note, _aware(self._clock())
                        )
            return False
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is not None and row.state in {
                _LifecycleState.QUEUED,
                _LifecycleState.RUNNING,
            }:
                self._lifecycle.cancelled(
                    row,
                    "Profile order retired: its stored evidence could not be "
                    f"read ({residue.note}); its effect is unknown",
                    _aware(self._clock()),
                    effect=_LifecycleEffect.UNKNOWN,
                    session=session,
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
                self._finish_pending_admission(
                    application_id,
                    state=_CANCELLED_OPERATION,
                    reason="Profile admission retired: its accepted intent could "
                    "not be read; load the profile again",
                )
                return None
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
            self._finish_pending_admission(
                application_id,
                state=LifecycleState.FAILED,
                reason=str(error) or "Profile admission could not be resumed",
            )
            return None
        if not fresh.allowed:
            if not _profile_preview_is_waitable(fresh):
                self._finish_pending_admission(
                    application_id,
                    state=LifecycleState.FAILED,
                    reason="Fleet profile intent contains a security or contract blocker",
                )
                return None
            blockers = _preview_blockers(fresh) + self._request_preparations(
                fresh, actor=actor, application_id=application_id
            )
            self._defer_pending_application(
                application_id,
                "Waiting for current Fleet conditions: "
                + "; ".join(item.code for item in blockers[:8])
                + ".",
                blockers=blockers,
                storage=_storage_wait_of_preview(fresh),
                wait_for_space=True,
            )
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
                            update={"reviewed_plan_digest": fresh.plan_digest}
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
                versions = []
                for item in moved:
                    revision = session.get(
                        CatalogDocumentRevision, item.recipe_revision_id
                    )
                    document = (
                        read_row_column(revision, "document") if revision else None
                    )
                    version = (
                        document.release.version
                        if isinstance(document, RecipeDefinition)
                        else None
                    )
                    versions.append(
                        f"{item.recipe_title} {version}"
                        if isinstance(version, str)
                        else item.recipe_title
                    )
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
