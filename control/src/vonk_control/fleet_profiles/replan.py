"""Replan for Fleet profiles."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import ValidationError
from sqlalchemy.orm import object_session
from vonk_agent_protocol import LifecycleState, ProfileReasonCode
from vonk_agent_protocol.agent_words import ProfileCancellationCause
from vonk_forge_contracts import RecipeDefinition

from ..fleet_profile_contract import (
    FleetProfileApplicationCancellationIntent,
    FleetProfileApplicationProgress,
    FleetProfilePreview,
)
from ..lifecycle.evidence import Residue
from ..lifecycle.fleet_profile import FleetProfileAdapter
from ..lifecycle.types import State as _LifecycleState
from ..models import CatalogDocumentRevision, FleetProfile, FleetProfileApplication
from ..operation_blockers import OperationBlocker, bound_blockers, make_blocker
from ..settings import STORAGE_ADMISSION_WAIT_SECONDS
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
    def _defer_exact_step(
        self,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
        reason: str,
        now: datetime,
        *,
        code: ProfileReasonCode = ProfileReasonCode.RETRY_CONFLICT,
    ) -> None:
        """Continue the same accepted queue/child identity without replacement admission."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        # The accepted request owns the budget. Re-observation and restart
        # cannot extend it; expiry enters the ordinary exact-effect stop path.
        deadline = _aware(row.created_at) + timedelta(
            seconds=STORAGE_ADMISSION_WAIT_SECONDS
        )
        if now >= deadline:
            session = object_session(row)
            assert session is not None
            progress.retry_due_at = None
            progress.cancellation = FleetProfileApplicationCancellationIntent(
                request_key=str(uuid.uuid5(uuid.UUID(row.id), "observation-expiry")),
                actor=row.actor,
                requested_at=now,
                cause=ProfileCancellationCause.OPERATOR.value,
                workload_intent_ordinal=progress.workload_intent_ordinal,
                pending_operation_ids=(
                    [row.current_operation_id] if row.current_operation_id else []
                ),
            )
            row.progress = progress.model_dump(mode="json")
            if row.state == LifecycleState.FAILED:
                self._lifecycle.reopen(
                    row,
                    now,
                    reason="Reconciling expired accepted effects",
                    session=session,
                )
            self._lifecycle.request_cancel(
                row,
                now,
                reason="Accepted effect observation budget expired",
                session=session,
                run_commands=False,
            )
            return
        progress.attempt += 1
        progress.retry_due_at = min(
            deadline, FleetProfileAdapter.next_retry(row.id, progress.attempt, now)
        )
        progress.blockers = bound_blockers([make_blocker(code, reason)])
        row.progress = progress.model_dump(mode="json")
        row.status_reason = f"Waiting for exact accepted effect ({reason}); next attempt at {progress.retry_due_at.isoformat()}"[
            :512
        ]
        row.updated_at = now

    def _step_unissued(self, application_id: str, residue: Residue) -> bool:
        """A step could not be issued: wait for the evidence, or retire the load.

        Missing, damaged and stale evidence follows the same exact step under
        the accepted request's deadline. Expiry enters bounded cancellation;
        only a newer accepted intent can supersede this request.
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
                    self._defer_exact_step(
                        row, progress, residue.note, _aware(self._clock())
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
        if not fresh.allowed:
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
