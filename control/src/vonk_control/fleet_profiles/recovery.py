"""Recovery for Fleet profiles."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    FailureCode,
    LifecycleState,
    ProfileReasonCode,
    SupersedeCode,
)
from vonk_agent_protocol.agent_words import (
    ProfileAction,
    ProfileProjectionKind,
    ProfileReasonSeverity,
)

from .. import fleet_profile_states
from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileChildOperation,
    FleetProfileOperationState,
    FleetProfilePlanStep,
)
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..lifecycle.fleet_profile import FleetProfileAdapter
from ..models import AgentNode, FleetProfileApplication, FleetProfileSelection
from ..operation_blockers import OperationBlocker, make_blocker
from ..recovery_policy import RECOVERY
from .assessment_support import (
    _progress_with_blockers,
)
from .contracts import (
    FleetProfileConflict,
)
from .dependencies import (
    _LOGGER,
    _MAX_PARKED_APPLICATION_OBSERVATIONS,
    _OPERATION_STATE_ADAPTER,
    ENDING_BLOCKER_CODES,
    PROFILE_REPEATED_FAILURE_CODE,
)
from .persistence import (
    _persisted_profile_plan,
    _persisted_profile_progress,
    _unreadable_revisions,
)
from .projection_support import (
    _aware,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def _retry_ending(
        self,
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
    ) -> tuple[OperationBlocker, ...] | None:
        """Why the Controller will not retry this failed load by itself, if so.

        ``None``: it retries it, so the load backs off (a wait is not a failure)
        until its replay is admissible, a newer load supersedes it, or a cancel
        ends it.  Otherwise the typed evidence its ``failed`` end records (empty
        when its own failure already says it: a definite ending is never
        replayed).  Every ending is permanent, so a ``failed`` row this
        Controller wrote never becomes retryable again.  One owner for the
        question: the failure itself, the recovery scan and the legacy adoption.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        adapter = self._switch_adapter
        if adapter is None or progress.switch_adapter is None:
            # Without its executor nothing replays it; a load that issued no
            # Run/Switch child ended on its own admission or evidence fault, which
            # a replay repeats (a deterministic error fails fast).
            return ()
        exhausted = tuple(
            item for item in progress.blockers if item.code in ENDING_BLOCKER_CODES
        )
        if exhausted or progress.cancellation is not None:
            return exhausted
        if progress.intended_profile is None:
            return (
                make_blocker(
                    ProfileReasonCode.RETRY_INTENT_UNAVAILABLE,
                    "the receipt carries no accepted intent to recover",
                ),
            )
        if _unreadable_revisions(session, progress.intended_profile.assignments):
            # A replay can never plan it again; the selected, current load waits
            # for the selected profile to follow the newest readable revision.
            if self._application_is_current_selection(
                session, row, progress
            ) and self._current_intent(session, row):
                return None
            return (
                make_blocker(
                    ProfileReasonCode.RETRY_REVIEW_UNAVAILABLE,
                    "an accepted recipe revision no longer reads",
                ),
            )
        if adapter.recovery_refused(row.id, session=session):
            return ()
        if not self._current_intent(session, row):
            return (
                make_blocker(
                    ProfileReasonCode.RETRY_INTENT_UNAVAILABLE,
                    "A newer accepted workload intent owns the effects",
                ),
            )
        repeated = self._repeated_failure(session, row, progress)
        return None if repeated is None else (repeated,)

    def _retries_failure(self, session: Session, row: FleetProfileApplication) -> bool:
        """The lifecycle's question when a load fails: will it be retried?

        When not, the typed reason is recorded in its progress (the evidence the
        ``failed`` row keeps) and no attempt is scheduled any more.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        try:
            progress = _persisted_profile_progress(row)
        except FleetProfileConflict:
            return False
        try:
            ending = self._retry_ending(session, row, progress)
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            ending = ()
        if ending is None:
            return True
        recorded = {(item.code, item.detail) for item in progress.blockers}
        row.progress = _progress_with_blockers(
            progress,
            [
                *(item for item in ending if (item.code, item.detail) not in recorded),
                *progress.blockers,
            ],
            retry_due_at=None,
        ).model_dump(mode="json")
        return False

    def _recovery_possible(
        self,
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
    ) -> bool:
        """Whether this load's replay is admissible now (it is the current,
        replayable intent on the scope it was accepted for)."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        adapter = self._switch_adapter
        if (
            adapter is None
            or any(item.code in ENDING_BLOCKER_CODES for item in progress.blockers)
            or progress.intended_profile is None
            or adapter.recovery_refused(row.id, session=session)
        ):
            return False
        # A replay plans the accepted revisions again: one the contract no
        # longer reads can never be planned. The selected profile follows the
        # newest readable revision instead (``_followed_intent``).
        if _unreadable_revisions(session, progress.intended_profile.assignments):
            return False
        current_scope = tuple(
            session.scalars(
                select(AgentNode.node_id)
                .where(AgentNode.revoked_at.is_(None))
                .order_by(AgentNode.node_id)
            )
        )
        return tuple(
            progress.intended_profile.scope.node_ids
        ) == current_scope and self._retry_eligible(session, row)

    def _repeated_failure(
        self,
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
    ) -> OperationBlocker | None:
        """The evidence that this load keeps failing the same way, if it does.

        Retries are for causes that change.  A definite ending is never replayed
        (``recovery_refused``), so what reaches the budget is a replayable ending:
        an untyped one, a digest mismatch, or an expired observation window,
        which the adapter identifies by what it waited on.  The same
        ``failure_signature`` as many times as ``RECOVERY`` allows failures in a
        row (this attempt and the ones its retry lineage already recorded) does
        not change by itself: another attempt only repeats it.  A Controller
        cache loss has no signature and is never counted.  Read-only (views call
        it); the failure records the ending once.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        recorded = next(
            (
                blocker
                for blocker in progress.blockers
                if blocker.code == PROFILE_REPEATED_FAILURE_CODE
            ),
            None,
        )
        if recorded is not None:
            return recorded
        adapter = self._switch_adapter
        if adapter is None:
            return None
        signature = adapter.failure_signature(row.id, session=session)
        if signature is None:
            return None
        count = 1
        cursor = progress.retry_of_application_id
        while not RECOVERY.exhausted(count):
            if cursor is None:
                break
            ancestor = session.get(FleetProfileApplication, cursor)
            if (
                ancestor is None
                or adapter.failure_signature(ancestor.id, session=session) != signature
            ):
                break
            count += 1
            try:
                cursor = _persisted_profile_progress(ancestor).retry_of_application_id
            except FleetProfileConflict:
                break
        if not RECOVERY.exhausted(count):
            return None
        cause = signature.partition("\n")[0].rpartition("|")[2] or signature
        return make_blocker(
            PROFILE_REPEATED_FAILURE_CODE,
            f"Failed the same way {count} times in a row; not retrying: {cause}",
            severity=ProfileReasonSeverity.ERROR.value,
            node_ids=progress.intended_profile.scope.node_ids
            if progress.intended_profile is not None
            else (),
        )

    def _settle_retry_candidates(
        self, adopted: Sequence[str], ended: Sequence[str], now: datetime
    ) -> None:
        """Adopt legacy retried failures as backoff, and end the backoffs that
        will not be retried any more (each once, under its row lock)."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        for application_id in (*adopted, *ended):
            with self._sessions.begin() as session:
                row = session.get(
                    FleetProfileApplication, application_id, with_for_update=True
                )
                if row is None:
                    continue
                try:
                    progress = _persisted_profile_progress(row)
                    ending = self._retry_ending(session, row, progress)
                except (FleetProfileConflict, ValidationError, TypeError, ValueError):
                    continue
                if row.state == LifecycleState.FAILED.value:
                    if ending is None and self._recovery_possible(
                        session, row, progress
                    ):
                        self._lifecycle.adopt_backoff(row, now, session=session)
                elif row.state == LifecycleState.BACKOFF.value and ending is not None:
                    self._end_backoff(session, row, progress, now)
                    _LOGGER.warning(
                        "profile application %s is not retried: %s",
                        application_id,
                        "; ".join(f"{item.code}: {item.detail}" for item in ending)
                        or row.status_reason,
                    )

    def _end_backoff(
        self,
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
        now: datetime,
    ) -> None:
        """End a backoff that will not be retried, keeping its last failure as
        its reason: superseded when a newer intent took the work over, else
        failed (the failure hook records the typed reason)."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        if self._superseding_intent(session, row, progress):
            self._lifecycle.supersede(
                row,
                row.status_reason or SupersedeCode.SUPERSEDED_BY_INTENT,
                now,
                code=SupersedeCode.SUPERSEDED_BY_INTENT,
                session=session,
            )
        else:
            self._lifecycle.fail(
                row,
                row.status_reason or FailureCode.OPERATION_FAILED,
                now,
                session=session,
            )

    def _presented_state(
        self, row: FleetProfileApplication, progress: FleetProfileApplicationProgress
    ) -> tuple[FleetProfileOperationState, datetime | None]:
        """The state to show, and when the next attempt is due."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        state = _OPERATION_STATE_ADAPTER.validate_python(row.state, strict=True)
        if progress.cancellation is not None:
            return state, None
        if state == LifecycleState.QUEUED and progress.admission_pending:
            return state, progress.admission_retry_at
        if state in {
            LifecycleState.QUEUED,
            LifecycleState.RUNNING,
            LifecycleState.BACKOFF,
        }:
            return state, progress.retry_due_at
        return state, None

    def _automatic_profile_recovery(self, now: datetime) -> tuple[str, str] | None:
        """Find one backing-off profile load whose replay is due and admissible.

        The same bounded scan ends a backoff that will not be retried any more
        and adopts a legacy ``failed`` row an older Controller still retried.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        adopted: list[str] = []
        ended: list[str] = []
        try:
            with self._sessions() as session:
                adapter = self._switch_adapter
                if adapter is None:
                    return None
                # Only due rows are scanned (the typed timestamp below remains the
                # authority), and the bounded batch walks all due rows round-robin
                # so a refused or ineligible row cannot starve an eligible one.
                retry_at = func.replace(
                    FleetProfileApplication.progress["retry_due_at"].as_string(),
                    "Z",
                    "+00:00",
                )
                retry_cutoff = TypeAdapter(datetime).dump_python(
                    _aware(now), mode="json"
                )
                statement = (
                    select(FleetProfileApplication)
                    .where(
                        FleetProfileApplication.state.in_(
                            (
                                *fleet_profile_states.RETRY_PENDING,
                                # A legacy retried failure, adopted below.
                                LifecycleState.FAILED.value,
                            )
                        ),
                        or_(
                            retry_at.is_(None),
                            retry_at <= str(retry_cutoff).replace("Z", "+00:00"),
                        ),
                    )
                    .order_by(FleetProfileApplication.id)
                    .limit(_MAX_PARKED_APPLICATION_OBSERVATIONS)
                )
                cursor = self._recovery_cursor
                rows = list(
                    session.scalars(
                        statement
                        if cursor is None
                        else statement.where(FleetProfileApplication.id > cursor)
                    )
                )
                if (
                    cursor is not None
                    and len(rows) < _MAX_PARKED_APPLICATION_OBSERVATIONS
                ):
                    rows.extend(
                        session.scalars(
                            statement.where(FleetProfileApplication.id <= cursor).limit(
                                _MAX_PARKED_APPLICATION_OBSERVATIONS - len(rows)
                            )
                        )
                    )
                self._recovery_cursor = rows[-1].id if rows else None
                for row in rows:
                    try:
                        progress = _persisted_profile_progress(row)
                        ending = self._retry_ending(session, row, progress)
                    except (
                        FleetProfileConflict,
                        ValidationError,
                        TypeError,
                        ValueError,
                    ):
                        continue
                    if row.state == LifecycleState.FAILED.value:
                        # An older Controller retried a replayable failure it
                        # stored as failed; one this Controller wrote never is.
                        if ending is None and self._recovery_possible(
                            session, row, progress
                        ):
                            adopted.append(row.id)
                        continue
                    if ending is not None:
                        if row.state == LifecycleState.BACKOFF.value:
                            ended.append(row.id)
                        continue
                    if not self._recovery_possible(session, row, progress):
                        continue
                    if progress.retry_due_at is not None and _aware(
                        progress.retry_due_at
                    ) > _aware(now):
                        continue
                    if now < FleetProfileAdapter.next_retry(
                        row.id, progress.attempt, _aware(row.updated_at)
                    ):
                        continue
                    self._recovery_cursor = row.id
                    return row.id, row.actor
        finally:
            self._settle_retry_candidates(adopted, ended, now)
        return None

    @staticmethod
    def _superseding_intent(
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
    ) -> bool:
        """Fence unissued profile effects after a newer authorized intent."""
        from .service import FleetProfileService

        intended = progress.intended_profile
        if intended is None:
            return False
        selection = session.get(FleetProfileSelection, 1)
        if (
            row.selection_generation is not None
            and selection is not None
            and selection.generation > row.selection_generation
            and not FleetProfileService._application_is_current_selection(
                session, row, progress
            )
        ):
            return True
        plan = _persisted_profile_plan(row)
        if isinstance(plan, Residue):
            return False
        adopted_scope = FleetProfileService._adopted_application_scope(session, row)
        scope = (
            set(adopted_scope)
            if adopted_scope is not None
            else {node_id for step in plan.steps for node_id in step.node_ids}
        )
        if not scope:
            return False
        ordinal = progress.workload_intent_ordinal
        if ordinal is None:
            return False
        nodes = list(
            session.scalars(select(AgentNode).where(AgentNode.node_id.in_(scope)))
        )
        return any(node.workload_intent_ordinal > ordinal for node in nodes)

    def _start_step(
        self,
        application_id: str,
        step_index: int,
        step: FleetProfilePlanStep,
        *,
        actor: str,
    ) -> tuple[str | None, bool, FleetProfileChildOperation | None] | Residue:
        """Issue one plan step.  A :class:`Residue` means it cannot be issued now.

        The caller leaves the step where it is and looks again on its next pass:
        an executor that is not bound yet, or a child the executor cannot show,
        is unknown, not a failure of the load.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        kind = step.kind
        request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"vonk-forge:profile:{application_id}:{step_index}"
            )
        )
        if kind == ProfileAction.SWITCH.value:
            if self._switch_adapter is None:
                return retire_as_unknown(
                    ProfileProjectionKind.STEP.value,
                    application_id,
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    "the Run/Switch executor is not bound yet",
                )
            execution_scope = tuple(step.node_ids)
            if not execution_scope:
                # A switch that affects no Spark has no effect to issue: the step
                # is complete (a synchronous no-op).
                return None, True, None
            assignments = tuple(
                assignment
                for assignment in self._application_assignments(application_id)
                if {node.node_id for node in assignment.nodes} <= set(execution_scope)
            )
            child = self._switch_adapter.start(
                application_id=application_id,
                assignments=assignments,
                scope_node_ids=execution_scope,
                actor=actor,
                request_id=request_id,
            )
            if isinstance(child, Residue):
                return child
            if not isinstance(child, FleetProfileChildOperation):
                return retire_as_unknown(
                    ProfileProjectionKind.STEP.value,
                    application_id,
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "the Run/Switch executor returned no child operation",
                )
            return child.id, False, child
        return retire_as_unknown(
            ProfileProjectionKind.STEP.value,
            application_id,
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            "Accepted Fleet profile step is unavailable",
        )
