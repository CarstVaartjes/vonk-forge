"""Recovery attempts for Fleet profiles."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from sqlalchemy.orm import object_session
from vonk_agent_protocol import InvalidRequestReason, LifecycleState, ProfileReasonCode
from vonk_agent_protocol.agent_words import (
    ProfileOperationKind,
    ProfileReasonSeverity,
)

from .. import job_states
from ..categorized_errors import MissingRecord
from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationView,
)
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..lifecycle.fleet_profile import FleetProfileAdapter
from ..models import FleetProfileApplication
from ..operation_blockers import OperationBlocker, bound_blockers, make_blocker
from .activity import (
    retry_disposition_of,
)
from .assessment_support import (
    _preview_blockers,
    _profile_preview_is_waitable,
    _progress_with_blockers,
    _require_recovery_preparations,
)
from .contracts import (
    FleetProfileInvalid,
)
from .dependencies import _CHILD_PENDING_STATES, _LOGGER, RETRY_WAIT
from .persistence import (
    _persisted_profile_progress,
)
from .projection_support import (
    _aware,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def _park_for_retry(
        self,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
        blockers: Sequence[OperationBlocker],
        *,
        because: BaseException | None = None,
    ) -> None:
        """Record why an application waits and when it will be checked again.

        ``because`` is the error that sends the application here: only an error
        declared retryable-by-waiting may park, anything else is a defect in the
        caller (waiting would never resolve it).

        The application is not failed: it keeps its accepted intent and the
        Controller retries it when conditions change. Its blockers replace the
        previous list, and one log line names a change of reason (not every retry).
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        assert because is None or retry_disposition_of(because) == RETRY_WAIT, (
            f"{type(because).__name__} is not retryable by waiting and must "
            "not park an application"
        )
        session = object_session(row)
        assert session is not None
        if self._end_exhausted_preparation(row, blockers, session):
            return
        now = _aware(self._clock())
        due = FleetProfileAdapter.next_retry(row.id, progress.attempt, now)
        blockers = bound_blockers(blockers)
        row.progress = _progress_with_blockers(
            progress,
            blockers,
            attempt=progress.attempt + 1,
            retry_due_at=due.isoformat(),
        ).model_dump(mode="json")
        lead = (
            f"{blockers[0].code}: {blockers[0].detail}"
            if blockers
            else "current Fleet conditions"
        )
        row.status_reason = (
            f"Waiting to retry ({lead}); next attempt at {due.isoformat()}"
        )[:512]
        row.updated_at = now
        previous = {(item.code, tuple(item.node_ids)) for item in progress.blockers}
        if previous != {(item.code, tuple(item.node_ids)) for item in blockers}:
            _LOGGER.log(
                logging.WARNING
                if any(
                    item.severity == ProfileReasonSeverity.ERROR.value
                    for item in blockers
                )
                else logging.INFO,
                "profile application %s is waiting: %s; next attempt %s",
                row.id,
                "; ".join(f"{item.code}: {item.detail}" for item in blockers[:4])
                or "current Fleet conditions",
                due.isoformat(),
            )

    def _decline_retry(
        self,
        parent: FleetProfileApplication,
        code: str,
        detail: str,
        blockers: Sequence[OperationBlocker] = (),
    ) -> FleetProfileApplicationView:
        """A recovery that cannot be started now is an unknown, never a refusal.

        The receipt keeps its accepted intent.  While it is still the current
        recoverable intent it is parked: its reason and next attempt are visible
        and the Controller looks again with bounded backoff.  Once it is not (a
        newer intent or another fleet owns the scope) it is left as it ended, with
        the reason noted; the selected profile's own reconciliation, or a new load,
        issues the replacement.  The caller gets the receipt either way.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        retire_as_unknown(
            "profile-retry", str(parent.id), BookkeepingReason.EVIDENCE_MISMATCH, detail
        )
        session = object_session(parent)
        if session is not None and self._retry_eligible(session, parent):
            self._park_for_retry(
                parent,
                _persisted_profile_progress(parent),
                list(blockers) or [make_blocker(code, detail)],
            )
        else:
            prior = (parent.status_reason or "").split(" | retry not started")[0]
            parent.status_reason = f"{prior} | retry not started: {detail}"[:512]
            parent.updated_at = _aware(self._clock())
        return self._application_view(parent)

    def _decline_retry_application(
        self,
        application_id: str,
        code: str,
        detail: str,
        blockers: Sequence[OperationBlocker] = (),
    ) -> FleetProfileApplicationView:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            return self._decline_retry(row, code, detail, blockers)

    def retry(
        self,
        application_id: str,
        *,
        request_key: str,
        actor: str,
        automatic_cache_recovery: bool = False,
    ) -> FleetProfileApplicationView:
        """Persist a new reconciliation attempt, retaining the original receipt.

        What cannot be reconciled now (a child the executor cannot show, a scope or
        assignment set that changed, a plan that is not admissible yet) leaves the
        receipt parked or noted and returns it; only a request for something that
        cannot be retried at all is refused.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        decline: tuple[str, str] | None = None
        with self._sessions() as session:
            if not automatic_cache_recovery:
                self._authorize(session, actor)
            replay = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if replay is not None:
                progress = _persisted_profile_progress(replay)
                if (
                    progress.retry_of_application_id != application_id
                    or replay.actor != actor
                ):
                    raise FleetProfileInvalid(
                        "Recovery request identity was reused for another application",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                return self._matching_application(
                    replay,
                    session=session,
                    profile_id=replay.profile_id,
                    actor=actor,
                    reviewed_digest=None,
                    retry_of_application_id=application_id,
                )
            parent = session.get(FleetProfileApplication, application_id)
            if parent is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            progress = _persisted_profile_progress(parent)
            if parent.state not in job_states.words(
                LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
            ):
                raise FleetProfileInvalid(
                    "Only failed or waiting applications can be retried",
                    reason=InvalidRequestReason.NOT_READY,
                )
            operation_kind = progress.operation_kind or ProfileOperationKind.APPLY.value
            if operation_kind != ProfileOperationKind.APPLY.value:
                raise FleetProfileInvalid(
                    "Only current profile loads can be recovered",
                    reason=InvalidRequestReason.UNSUPPORTED,
                )
            if progress.intended_profile is None:
                decline = (
                    ProfileReasonCode.RETRY_INTENT_UNAVAILABLE,
                    "the receipt carries no accepted intent to recover",
                )
            adapter = self._switch_adapter
            if decline is None and parent.current_operation_id is not None:
                if adapter is None:
                    decline = (
                        ProfileReasonCode.RETRY_EXECUTOR_UNAVAILABLE,
                        "the Run/Switch executor is not bound yet",
                    )
                else:
                    try:
                        child = adapter.get(
                            parent.current_operation_id, session=session
                        )
                    except (KeyError, RuntimeError, ValueError):
                        # The child's record is gone: the step is issued again under
                        # its deterministic identity, and its owner reconciles what
                        # it already did (it is adopted, not repeated).
                        child = None
                    if child is not None and child.state in _CHILD_PENDING_STATES:
                        # End this read before the resume takes its row lock.
                        parent_id = parent.id
                        session.close()
                        resumed = self._resume_live_child(parent_id)
                        if resumed is not None:
                            return resumed
            persisted_plan = (
                None
                if decline is not None
                else self._reviewed_profile_plan(parent, session=session)
            )
            if isinstance(persisted_plan, Residue):
                decline = (
                    ProfileReasonCode.RETRY_REVIEW_UNAVAILABLE,
                    persisted_plan.note,
                )
            intended = progress.intended_profile
            cache_loss_recovery = (
                adapter is not None
                and decline is None
                and adapter.recoverable_cache_loss(parent.id, session=session)
            )
        if (
            decline is not None
            or persisted_plan is None
            or isinstance(persisted_plan, Residue)
            or intended is None
        ):
            code, detail = decline or (
                ProfileReasonCode.RETRY_INTENT_UNAVAILABLE,
                "the receipt carries no accepted intent to recover",
            )
            return self._decline_retry_application(application_id, code, detail)
        preview = self.preview(
            persisted_plan.profile_id,
            execution_assignments=tuple(intended.assignments),
            profile_name=persisted_plan.profile_name,
            profile_digest=intended.profile_digest,
            accepted_intent=intended,
            accepted_profile_revision=persisted_plan.profile_revision,
            accepted_profile_definition=persisted_plan.profile_definition,
            allow_pending_cache_rebuild=cache_loss_recovery,
            profile_application_id=application_id,
        )
        if not preview.allowed:
            if not _profile_preview_is_waitable(preview):
                raise FleetProfileInvalid(
                    "Current Fleet state blocks application recovery",
                    reason=InvalidRequestReason.NOT_READY,
                )
            blockers = _preview_blockers(preview) + self._request_preparations(
                preview, actor=actor, application_id=application_id
            )
            return self._decline_retry_application(
                application_id,
                ProfileReasonCode.RECOVERY_WAITING,
                "current Fleet conditions do not admit the accepted plan yet",
                blockers,
            )
        if tuple(preview.scope.node_ids) != tuple(intended.scope.node_ids):
            return self._decline_retry_application(
                application_id,
                ProfileReasonCode.RECOVERY_SCOPE_CHANGED,
                "the fleet scope changed since the application was accepted",
            )
        expected_assignments = {
            assignment.id: (
                assignment.recipe_revision_id,
                assignment.desired_state,
                tuple(node.node_id for node in assignment.nodes),
            )
            for assignment in intended.assignments
        }
        observed_assignments = {
            assignment.assignment_id: (
                assignment.recipe_revision_id,
                assignment.desired_state,
                tuple(assignment.node_ids),
            )
            for assignment in preview.assignments
        }
        if observed_assignments != expected_assignments:
            return self._decline_retry_application(
                application_id,
                ProfileReasonCode.RECOVERY_ASSIGNMENTS_CHANGED,
                "the assignments changed since the application was accepted",
            )
        _require_recovery_preparations(persisted_plan, preview)
        return self._queue_application(
            preview,
            request_key=request_key,
            actor=actor,
            operation_kind=operation_kind,
            retry_of_application_id=application_id,
            automatic_cache_recovery=automatic_cache_recovery,
            platform_maintenance=automatic_cache_recovery,
        )
