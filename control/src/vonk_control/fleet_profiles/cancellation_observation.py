"""Cancellation observation for Fleet profiles."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy.orm import Session
from vonk_agent_protocol import LifecycleState, canonical_message

from .. import job_states
from ..agent_jobs import AgentJobService
from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfilePreview,
)
from ..lifecycle.evidence import Residue
from ..lifecycle.fleet_profile import cancellation_state, doc_state
from ..lifecycle.types import Effect as _LifecycleEffect
from ..lifecycle.types import State as _LifecycleState
from ..models import FleetProfileApplication
from ..strict_json import read_stored_model
from .contracts import (
    FleetProfileChildPlanBlocked,
    FleetProfileConflict,
)
from .dependencies import _CANCELLATION_OBSERVATION_SECONDS, _CHILD_PENDING_STATES
from .persistence import (
    _application_effect_scope,
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
    @staticmethod
    def _defer_cancellation_observation(
        row: FleetProfileApplication,
        progress: FleetProfileApplicationProgress,
        now: datetime,
        *,
        due_at: datetime | None = None,
    ) -> None:
        """Persist the next bounded observation time for a pending owner."""

        if progress.cancellation is None:
            return
        due = _aware(due_at) if due_at is not None else None
        if due is None or due <= now:
            due = now + timedelta(seconds=_CANCELLATION_OBSERVATION_SECONDS)
        progress.cancellation.observation_due_at = due
        row.progress = progress.model_dump(mode="json")

    def _advance_cancellation_in_session(
        self,
        session: Session,
        row: FleetProfileApplication,
        plan: FleetProfilePreview | Residue,
        progress: FleetProfileApplicationProgress,
        now: datetime,
    ) -> bool:
        """Drive one pass of a cancel: the core stops and observes, and it completes.

        The children are stopped by :meth:`_stop_children` (the same reconcile the
        worker always did, one pass per due observation); the core decides the rest:
        when nothing is left the load is ``cancelled``, and when the budget is spent
        it is ``cancelled`` anyway with the children's effect recorded unknown and
        their operation ids kept.  A cancel never parks the load for a person.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        del plan, progress
        settled = self._lifecycle.tick_cancel(
            row,
            now,
            terminal_reason=(
                "Profile application cancelled after issued effects were reconciled"
            ),
            session=session,
        )
        return bool(settled.changed or settled.commands)

    def _stop_children(self, session: Session, row: FleetProfileApplication) -> bool:
        """One idempotent pass of the cancel's stop: ``True`` when nothing is left.

        Reconciles the exact current child (a Run/Switch operation under the
        profile's switch-adapter document), then the older agent effects fenced by
        the cancel's workload intent.  Whatever cannot be confirmed yet is a wait the
        core observes again (and bounds), recorded for the operator view; nothing
        here is a refusal.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        adapter = self._switch_adapter
        now = _aware(self._clock())
        progress = _stored_progress(row)
        if isinstance(progress, Residue):
            # Unreadable evidence is an unknown, not a verdict: it is observed
            # again, and the cancel's budget ends it.
            return False
        intent = progress.cancellation
        if intent is None:
            return True
        if adapter is None:
            return False
        plan = _persisted_profile_plan(row)
        if not isinstance(plan, Residue) and plan.effects.adopted:
            borrowed_scope = tuple(
                sorted(
                    {
                        node_id
                        for effect in plan.effects.adopted
                        for node_id in effect.node_ids
                    }
                )
            )
            if intent.workload_intent_ordinal is None:
                return False
            effects = AgentJobService.assess_superseded_agent_effects_in_session(
                session, borrowed_scope, intent.workload_intent_ordinal, now
            )
            if effects:
                operation_ids = sorted(effect.operation_id for effect in effects)
                progress_data = progress.model_dump(mode="json")
                cancellation_data = dict(progress_data["cancellation"])
                cancellation_data.update(
                    {
                        "pending_operation_ids": operation_ids,
                        "observation_due_at": min(
                            effect.observe_due_at for effect in effects
                        ).isoformat(),
                        "observation_deadline_at": min(
                            effect.observation_deadline for effect in effects
                        ).isoformat(),
                    }
                )
                progress_data["cancellation"] = cancellation_data
                row.progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(progress_data),
                    strict=True,
                    from_json=True,
                ).model_dump(mode="json")
                row.status_reason = (
                    "Cancellation is waiting for adopted assignment effects: "
                    + ", ".join(operation_ids)
                )[:512]
                row.updated_at = now
                return False
        if progress.switch_adapter is not None:
            try:
                child = adapter.advance(row.id, session=session)
            except (
                FleetProfileChildPlanBlocked,
                KeyError,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                self._defer_cancellation_observation(row, progress, now)
                row.status_reason = (
                    "Cancellation is waiting for its profile switch child to be "
                    "reconciled by the Run/Switch owner"
                )
                row.updated_at = now
                return False
            progress = _persisted_profile_progress(row)
            progress_data = progress.model_dump(mode="json")
            if child.progress is not None:
                progress_data["child_progress"] = child.progress.model_dump(mode="json")
            row.progress = read_stored_model(
                FleetProfileApplicationProgress,
                canonical_message(progress_data),
                strict=True,
                from_json=True,
            ).model_dump(mode="json")
            progress = _persisted_profile_progress(row)
            if child.state == LifecycleState.CANCELLED.value or (
                progress.cancellation is not None
                and progress.cancellation.state == LifecycleState.CANCELLED.value
            ):
                return True
            self._defer_cancellation_observation(
                row,
                progress,
                now,
                due_at=(
                    progress.switch_adapter.observation_due_at
                    if progress.switch_adapter is not None
                    else None
                ),
            )
            if child.state in _CHILD_PENDING_STATES or child.state in job_states.words(
                LifecycleState.NEEDS_OPERATOR
            ):
                row.status_reason = (
                    child.status_reason
                    or "Cancellation is waiting for the active profile child"
                )[:512]
            else:
                row.status_reason = (
                    f"Cancellation is waiting for the Run/Switch owner to reconcile "
                    f"child state {child.state}"
                )[:512]
            row.updated_at = now
            return False

        if row.current_operation_id is not None:
            self._defer_cancellation_observation(row, progress, now)
            row.status_reason = (
                "Cancellation is waiting for the recorded profile child identity "
                "to become available"
            )
            row.updated_at = now
            return False

        # (An unreadable plan falls back to the scope the row declared.)
        scope = _application_effect_scope(row)
        if scope and intent.workload_intent_ordinal is not None:
            try:
                effects = AgentJobService.assess_superseded_agent_effects_in_session(
                    session, scope, intent.workload_intent_ordinal, now
                )
            except (TypeError, ValueError) as error:
                self._defer_cancellation_observation(row, progress, now)
                row.status_reason = (
                    "Cancellation is waiting on issued-effect evidence it cannot "
                    f"read yet: {str(error)[:360]}"
                )[:512]
                row.updated_at = now
                return False
            if effects:
                deadline = min(effect.observation_deadline for effect in effects)
                due = min(effect.observe_due_at for effect in effects)
                operation_ids = sorted(effect.operation_id for effect in effects)
                progress_data = progress.model_dump(mode="json")
                cancellation_data = dict(progress_data["cancellation"])
                cancellation_data.update(
                    {
                        "pending_operation_ids": operation_ids,
                        "observation_due_at": due.isoformat(),
                        "observation_deadline_at": deadline.isoformat(),
                    }
                )
                progress_data["cancellation"] = cancellation_data
                row.progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(progress_data),
                    strict=True,
                    from_json=True,
                ).model_dump(mode="json")
                owner = (
                    "waiting for issued agent cancellation receipts"
                    if now < deadline
                    else "issued agent cancellation receipts are overdue; its "
                    "stop budget will end the cancel"
                )
                row.status_reason = f"Cancellation {owner}: " + ", ".join(operation_ids)
                row.updated_at = now
                return False

        # No workload intent left to settle and no child: nothing is left.
        return True

    def _observe_children(
        self, session: Session, row: FleetProfileApplication
    ) -> _LifecycleEffect:
        """Read-only: whether anything of the cancelled load's children is left."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        try:
            progress = _persisted_profile_progress(row)
        except FleetProfileConflict:
            return _LifecycleEffect.UNKNOWN
        plan = _persisted_profile_plan(row)
        if (
            not isinstance(plan, Residue)
            and plan.effects.adopted
            and progress.cancellation is not None
        ):
            ordinal = progress.cancellation.workload_intent_ordinal
            if ordinal is None:
                return _LifecycleEffect.UNKNOWN
            scope = tuple(
                sorted(
                    {
                        node_id
                        for effect in plan.effects.adopted
                        for node_id in effect.node_ids
                    }
                )
            )
            if AgentJobService.assess_superseded_agent_effects_in_session(
                session, scope, ordinal, _aware(self._clock())
            ):
                return _LifecycleEffect.UNKNOWN
        children = self._lifecycle.bound(session).children_of(session, row)
        state = self._lifecycle.aggregate_state(children)
        if state is not None and state in {
            _LifecycleState.SUCCEEDED,
            _LifecycleState.FAILED,
            _LifecycleState.CANCELLED,
        }:
            return _LifecycleEffect.STOPPED
        if progress.cancellation is None:
            return _LifecycleEffect.NONE
        return _LifecycleEffect.UNKNOWN

    def _finish_cancel_records(
        self, session: Session, row: FleetProfileApplication, residue: bool
    ) -> None:
        """Close the records of a cancel that ended: the cancellation, the child and
        the result.  The state itself was written by the lifecycle adapter."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        del session
        now = _aware(self._clock())
        progress = _persisted_profile_progress(row)
        cancellation = progress.cancellation
        if cancellation is not None:
            cancellation.observation_due_at = None
            cancellation.observation_deadline_at = None
            if not residue:
                cancellation.pending_operation_ids = []
        cancellation_state(progress, LifecycleState.CANCELLED)
        document = progress.switch_adapter
        if document is not None and document.state not in {
            LifecycleState.SUCCEEDED.value,
            LifecycleState.FAILED.value,
            LifecycleState.CANCELLED.value,
        }:
            doc_state(document, LifecycleState.CANCELLED)
            # Retain pending child identities even at cancellation expiry: a
            # terminal parent does not establish the child's physical effect.
        row.progress = progress.model_dump(mode="json")
        row.current_operation_id = None
        row.result = {
            "changed": bool(progress.completed_steps or progress.step_results),
            "completed_steps": min(progress.completed_steps, row.current_step),
        }
        row.updated_at = now
