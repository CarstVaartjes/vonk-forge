"""Recovery for Fleet profiles."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, object_session
from vonk_agent_protocol import InvalidRequestReason, LifecycleState, RecipeImageCode
from vonk_agent_protocol.agent_words import (
    ProfileAction,
    ProfileProjectionKind,
    ProfileReasonSeverity,
)

from .. import job_states
from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileChildOperation,
    FleetProfileOperationState,
    FleetProfilePlanStep,
)
from ..lifecycle.core import RECOVERY
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..lifecycle.fleet_profile import FleetProfileAdapter
from ..models import AgentNode, FleetProfile, FleetProfileApplication
from ..operation_blockers import OperationBlocker, make_blocker
from .assessment_support import (
    _progress_with_blockers,
)
from .contracts import (
    FleetProfileConflict,
    FleetProfileInvalid,
)
from .dependencies import (
    _LOGGER,
    _MAX_PARKED_APPLICATION_OBSERVATIONS,
    _OPERATION_STATE_ADAPTER,
    PROFILE_REPEATED_FAILURE_CODE,
)
from .persistence import (
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
    def _recovery_wanted(
        self,
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
    ) -> bool:
        """Whether the Controller will retry this failed application by itself.

        One owner for the question, shared by the recovery scan and by every
        view: an application that will be retried is waiting, not failed.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        return (
            self._recovery_possible(session, row, progress)
            and self._repeated_failure(session, row, progress) is None
        )

    def _recovery_possible(
        self,
        session: Session,
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
    ) -> bool:
        """Whether this failed application is the current, replayable intent."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        adapter = self._switch_adapter
        if (
            adapter is None
            or any(
                item.code == RecipeImageCode.PREPARATION_EXHAUSTED
                for item in progress.blockers
            )
            or progress.intended_profile is None
            or adapter.recovery_refused(row.id, session=session)
        ):
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

        Retries are for causes that change.  A start that fails with the same
        typed cause ``RECOVERY.max_failures`` times in a row (this attempt and the
        ones its retry lineage already recorded) is deterministic: another attempt
        repeats it and re-launches the workload each time.  Read-only (views call
        it); the recovery scan records the ending once.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        if row.state not in job_states.words(LifecycleState.FAILED):
            return None
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
        for _ in range(RECOVERY.max_failures - 1):
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
        if count < RECOVERY.max_failures:
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

    def _end_repeated_failures(
        self, ended: Sequence[tuple[str, OperationBlocker]], now: datetime
    ) -> None:
        """Record, once, that these loads stop retrying (they stay ``failed``)."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        for application_id, blocker in ended:
            with self._sessions.begin() as session:
                row = session.get(
                    FleetProfileApplication, application_id, with_for_update=True
                )
                if row is None or row.state not in job_states.words(
                    LifecycleState.FAILED
                ):
                    continue
                progress = _persisted_profile_progress(row)
                if any(
                    item.code == PROFILE_REPEATED_FAILURE_CODE
                    for item in progress.blockers
                ):
                    continue
                if self._lifecycle.end_retry(
                    row,
                    blocker.detail,
                    now,
                    progress=_progress_with_blockers(progress, [blocker]),
                    session=session,
                ):
                    _LOGGER.warning(
                        "profile application %s: %s", application_id, blocker.detail
                    )

    def _presented_state(
        self, row: FleetProfileApplication, progress: FleetProfileApplicationProgress
    ) -> tuple[FleetProfileOperationState, datetime | None]:
        """The state to show, and when the next attempt is due.

        A failed application the Controller will retry is ``queued`` with its
        next attempt time; ``failed`` is reserved for applications that stay so.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        state = _OPERATION_STATE_ADAPTER.validate_python(row.state, strict=True)
        if progress.cancellation is not None:
            return state, None
        if state == LifecycleState.FAILED:
            session = object_session(row)
            try:
                retrying = session is not None and self._recovery_wanted(
                    session, row, progress
                )
            except (FleetProfileConflict, ValidationError, TypeError, ValueError):
                retrying = False
            if retrying:
                # Automatic recovery always has a next attempt; name it even
                # when the failure recorded no explicit due time.
                return (
                    LifecycleState.QUEUED,
                    progress.retry_due_at
                    or FleetProfileAdapter.next_retry(
                        row.id, progress.attempt, _aware(row.updated_at)
                    ),
                )
            return state, None
        if state == LifecycleState.QUEUED and progress.admission_pending:
            return state, progress.admission_retry_at
        if state in {LifecycleState.QUEUED, LifecycleState.RUNNING}:
            return state, progress.retry_due_at
        return state, None

    def _automatic_profile_recovery(self, now: datetime) -> tuple[str, str] | None:
        """Find one current profile intent that can safely be reconciled again."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        ended: list[tuple[str, OperationBlocker]] = []
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
                            job_states.words(
                                LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
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
                    except FleetProfileConflict:
                        continue
                    if not self._recovery_possible(session, row, progress):
                        continue
                    repeated = self._repeated_failure(session, row, progress)
                    if repeated is not None:
                        if not any(
                            item.code == PROFILE_REPEATED_FAILURE_CODE
                            for item in progress.blockers
                        ):
                            ended.append((row.id, repeated))
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
            self._end_repeated_failures(ended, now)
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
            return True
        if row.selection_generation is not None:
            if not FleetProfileService._application_is_current_selection(
                session, row, progress
            ):
                return True
        else:
            profile = session.get(FleetProfile, row.profile_id)
            if (
                profile is None
                or _digest(_profile_document(profile)) != intended.profile_digest
            ):
                return True
        plan = _persisted_profile_plan(row)
        if isinstance(plan, Residue):
            # Without its reviewed plan the order cannot show what it still owns:
            # it is retired as superseded (its issued effects keep their own
            # cancellation receipts).
            return True
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
            return True
        nodes = list(
            session.scalars(select(AgentNode).where(AgentNode.node_id.in_(scope)))
        )
        return len(nodes) != len(scope) or any(
            node.workload_intent_ordinal != ordinal for node in nodes
        )

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
        raise FleetProfileInvalid(
            "Fleet profile step kind is unsupported",
            reason=InvalidRequestReason.UNSUPPORTED,
        )
