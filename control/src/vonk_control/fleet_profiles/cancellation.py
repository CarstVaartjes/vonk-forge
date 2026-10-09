"""Cancellation for Fleet profiles."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import func, select
from vonk_agent_protocol import (
    DesiredAssignmentState,
    InvalidRequestReason,
    LifecycleState,
    ObservedAssignmentState,
    SupersedeCode,
    canonical_message,
)
from vonk_agent_protocol.agent_words import (
    ProfileCancellationCause,
    ProfileOperationKind,
)

from .. import fleet_profile_states, job_states
from ..categorized_errors import MissingRecord
from ..fleet_profile_adapter_conversion import needs_conversion
from ..fleet_profile_contract import (
    FleetProfileApplicationCancellationIntent,
    FleetProfileApplicationProgress,
    FleetProfileApplicationView,
)
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..lifecycle.types import Effect as _LifecycleEffect
from ..models import (
    AgentNode,
    FleetProfile,
    FleetProfileApplication,
)
from ..strict_json import read_stored_model
from .assessment_support import (
    _deferral_code,
    _profile_preview_is_waitable,
    _storage_wait_of,
)
from .contracts import (
    FleetProfileAdmissionBusy,
    FleetProfileAdmissionEffectBusy,
    FleetProfileAdmissionStorageError,
    FleetProfileConflict,
    FleetProfileInvalid,
    _FleetProfileSupersededIntentConflict,
)
from .persistence import (
    _application_effect_scope,
    _persisted_profile_plan,
    _persisted_profile_progress,
    _stored_progress,
)
from .projection_support import (
    _aware,
    _roster_digest,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def cancel(
        self,
        application_id: str,
        *,
        profile_number: int,
        request_key: str,
        actor: str,
    ) -> FleetProfileApplicationView:
        """Persist one exact cancellation and reconcile only issued effects."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        try:
            parsed_key = uuid.UUID(request_key)
        except (TypeError, ValueError, AttributeError) as error:
            raise FleetProfileInvalid(
                "Profile cancellation request key is invalid",
                reason=InvalidRequestReason.MALFORMED,
            ) from error
        if str(parsed_key) != request_key:
            raise FleetProfileInvalid(
                "Profile cancellation request key is invalid",
                reason=InvalidRequestReason.MALFORMED,
            )

        with self._sessions() as snapshot_session:
            self._authorize(snapshot_session, actor)
            snapshot = snapshot_session.get(FleetProfileApplication, application_id)
            if snapshot is None:
                raise FleetProfileInvalid(
                    application_id, reason=InvalidRequestReason.MALFORMED
                )
            snapshot_number = snapshot_session.scalar(
                select(FleetProfile.number).where(
                    FleetProfile.id == snapshot.profile_id
                )
            )
            if snapshot_number != profile_number:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            snapshot_scope = _application_effect_scope(snapshot)

        now = _aware(self._clock())
        cancellation: FleetProfileApplicationCancellationIntent | None = None
        waiting_only = False
        with self._admission_session(actor, node_ids=snapshot_scope) as session:
            row = session.get(
                FleetProfileApplication,
                application_id,
                with_for_update={"nowait": True},
            )
            if row is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            if (
                session.scalar(
                    select(FleetProfile.number).where(FleetProfile.id == row.profile_id)
                )
                != profile_number
            ):
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )

            progress = _persisted_profile_progress(row)
            plan = _persisted_profile_plan(row)
            scope = _application_effect_scope(row)
            if scope != snapshot_scope:
                # Observation raced another owner. Preserve its exact scope;
                # the next observer re-reads before taking admission locks.
                return self._application_view(row)
            previous = progress.cancellation
            if previous is not None:
                if previous.request_key == request_key and previous.actor != actor:
                    raise FleetProfileInvalid(
                        "Profile cancellation request key belongs to another actor",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                # A new observer of the same stop follows its original bounded
                # receipt. Request history cannot force another cancellation.
                cancellation = previous
            else:
                if self._adopted_application_scope(session, row) is not None:
                    # Continuing effects belong to the newer selected request.
                    # An older cancellation cannot mutate that newer authority.
                    return self._application_view(row)
                # A failed application the Controller will retry by itself is
                # shown as queued, so it must be cancellable like any queued
                # one: cancelling stops that retry, and a child it still owns is
                # reconciled by the same fence and cancellation as for a live
                # application.
                retrying_failure = (
                    row.state == LifecycleState.FAILED.value
                    and self._recovery_wanted(session, row, progress)
                )
                if (
                    row.state
                    not in job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.NEEDS_OPERATOR,
                    )
                    and not retrying_failure
                ):
                    return self._application_view(row)
                ordinal = progress.workload_intent_ordinal
                # A workload fence precedes every workload effect, so an issued
                # child without one is evidence this cancel cannot fence: it is not
                # refused for it (a cancel always completes).  It is driven like any
                # other: the child is asked to stop and observed, and the cancel ends
                # within its budget with that child's effect recorded.
                has_child = (
                    progress.switch_adapter is not None
                    or row.current_operation_id is not None
                )
                cancel_ordinal: int | None = None
                adopted_ordinals = (
                    {
                        node_id: effect.workload_intent_ordinal
                        for effect in plan.effects.adopted
                        for node_id in effect.node_ids
                    }
                    if not isinstance(plan, Residue)
                    else {}
                )
                if ordinal is not None or adopted_ordinals:
                    cancel_ordinal = max([ordinal or 0, *adopted_ordinals.values()]) + 1
                    nodes = (
                        tuple(
                            session.scalars(
                                select(AgentNode)
                                .where(AgentNode.node_id.in_(scope))
                                .order_by(AgentNode.node_id)
                                .with_for_update(nowait=True)
                            )
                        )
                        if scope
                        else ()
                    )
                    # A node whose fence is not this application's is owned by a
                    # newer (or an unrecorded) intent: it is left alone, and the
                    # cancel completes for the nodes this application still owns.
                    cancellable_nodes = tuple(
                        node.node_id
                        for node in nodes
                        if node.workload_intent_ordinal
                        == adopted_ordinals.get(node.node_id, ordinal)
                    )
                    for node in nodes:
                        if node.workload_intent_ordinal == adopted_ordinals.get(
                            node.node_id, ordinal
                        ):
                            node.workload_intent_ordinal = cancel_ordinal
                    if cancellable_nodes:
                        adapter = self._switch_adapter
                        if adapter is None:
                            retire_as_unknown(
                                "profile-cancellation",
                                application_id,
                                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                                "the cancellation executor is unavailable",
                            )
                        else:
                            adapter.request_superseded_workload_cancellation_in_session(
                                session, cancellable_nodes, cancel_ordinal, now
                            )
                cancellation = FleetProfileApplicationCancellationIntent(
                    request_key=request_key,
                    actor=actor,
                    requested_at=now,
                    cause=ProfileCancellationCause.OPERATOR.value,
                    workload_intent_ordinal=cancel_ordinal,
                )
                progress_data = progress.model_dump(mode="json")
                progress_data["cancellation"] = cancellation.model_dump(mode="json")
                row.progress = read_stored_model(
                    FleetProfileApplicationProgress,
                    canonical_message(progress_data),
                    strict=True,
                    from_json=True,
                ).model_dump(mode="json")
                # Nothing fenced or issued: the application only waits (for
                # preparation, build capacity or its turn), so the core cancels it
                # at once and leaves the running workload alone.  Otherwise the
                # cancel is driven by the worker (its stops are the core's, spaced
                # and bounded), never run inside this admission transaction.
                waiting_only = (
                    ordinal is None and not adopted_ordinals and not has_child
                )
                if retrying_failure:
                    # An ended load the Controller would retry is shown as queued
                    # and is cancellable like one: reopen it so the cancel (and any
                    # child it still owns) is driven to its end like a live load's.
                    self._lifecycle.reopen(
                        row,
                        now,
                        reason="Cancellation requested; reconciling issued profile effects.",
                        session=session,
                    )
                self._lifecycle.request_cancel(
                    row,
                    now,
                    reason=(
                        "Profile application cancelled before any workload "
                        "effect was issued; the running workload was not touched."
                        if waiting_only
                        else "Cancellation requested; reconciling issued profile effects."
                    ),
                    session=session,
                    run_commands=False,
                )

        if waiting_only:
            if not isinstance(plan, Residue):
                self._cancel_owned_preparations(application_id, plan, actor=actor)
            return self.application(application_id)
        adapter = self._switch_adapter
        if adapter is not None and cancellation is not None:
            request_cancellation = getattr(adapter, "request_cancellation", None)
            if callable(request_cancellation):
                request_cancellation(
                    application_id,
                    request_key=cancellation.request_key,
                    actor=cancellation.actor,
                )
        return self.application(application_id)

    def _observe_pending_cancellation(self, now: datetime) -> bool:
        """Advance one due cancellation before selecting ordinary active work."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        adapter = self._switch_adapter
        if adapter is None:
            return False
        progress = FleetProfileApplication.progress
        cancellation = progress["cancellation"]
        cancellation_state = cancellation["state"].as_string()
        observation_due = cancellation["observation_due_at"].as_string()
        eligible = (
            FleetProfileApplication.state.in_(
                job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.NEEDS_OPERATOR,
                )
            )
            & cancellation_state.in_(fleet_profile_states.CANCEL_IN_FLIGHT)
            & (func.coalesce(observation_due, "") <= now.isoformat())
        )
        with self._sessions() as session:
            candidate = session.scalar(
                select(FleetProfileApplication)
                .where(eligible)
                .order_by(
                    FleetProfileApplication.updated_at,
                    FleetProfileApplication.created_at,
                    FleetProfileApplication.id,
                )
                .limit(1)
            )
            if candidate is None or needs_conversion(candidate):
                return False
            application_id = candidate.id
            try:
                intent = _persisted_profile_progress(candidate).cancellation
            except FleetProfileConflict:
                intent = None
        request_cancellation = getattr(adapter, "request_cancellation", None)
        if intent is not None and callable(request_cancellation):
            request_cancellation(
                application_id,
                request_key=intent.request_key,
                actor=intent.actor,
            )
        with self._sessions.begin() as session:
            row = session.scalar(
                select(FleetProfileApplication)
                .where(eligible, FleetProfileApplication.id == application_id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None or needs_conversion(row):
                return False
            plan = _persisted_profile_plan(row)
            current = _stored_progress(row)
            if isinstance(current, Residue):
                # A cancel always completes: evidence that cannot be read is an
                # unknown effect, recorded as such (the document is retained
                # untouched), not a reason to park the load for a person.  (An
                # unreadable *plan* is not needed to drive the cancel.)
                self._lifecycle.cancelled(
                    row,
                    "Profile application cancelled; its persisted effect evidence "
                    f"could not be read, so its effect is unknown: {current.note[:300]}",
                    now,
                    effect=_LifecycleEffect.UNKNOWN,
                    session=session,
                )
                return True
            intent = current.cancellation
            if intent is None or intent.state != LifecycleState.OBSERVING:
                return False
            if (
                intent.observation_due_at is not None
                and _aware(intent.observation_due_at) > now
            ):
                return False
            return self._advance_cancellation_in_session(
                session, row, plan, current, now
            )

    def _reconcile_selected_roster(self, now: datetime) -> bool:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        drift_signature: tuple[tuple[str, str], ...] = ()
        with self._sessions() as session:
            selected = self._selected_profile_snapshot(session)
            if selected is None or isinstance(selected, Residue):
                # A damaged selected receipt is recorded; the ordinary
                # application worker fails it without blocking unrelated work.
                return False
            roster = tuple(
                session.scalars(
                    select(AgentNode.node_id)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                )
            )
            roster_changed = roster != selected.roster_node_ids
            application = session.get(FleetProfileApplication, selected.application_id)
            if (
                application is not None
                and application.state == LifecycleState.SUCCEEDED.value
            ):
                drift_signature = tuple(
                    sorted(
                        (assignment.id, state.current_state)
                        for assignment in selected.intended.assignments
                        if assignment.desired_state == DesiredAssignmentState.RUNNING
                        for state in (self._assignment_state(session, assignment),)
                        if state.current_state != ObservedAssignmentState.RUNNING
                    )
                )
            if not roster_changed and not drift_signature:
                return False

        # Preserve the accepted topology exactly. Preview compares it with the
        # current roster and owns the incomplete multi-Spark cleanup decision.
        assignments = tuple(selected.intended.assignments)
        preview = self.preview(
            selected.profile_id,
            execution_assignments=assignments,
            profile_name=selected.plan.profile_name,
            profile_digest=selected.intended.profile_digest,
            accepted_intent=selected.intended,
            accepted_profile_revision=selected.profile_revision,
            accepted_profile_definition=selected.plan.profile_definition,
        )
        request_key = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "vonk-forge:fleet-profile-reconcile:"
                f"{selected.application_id}:{selected.generation}:"
                f"{_roster_digest(roster)}:"
                f"{hashlib.sha256(repr(drift_signature).encode()).hexdigest()}",
            )
        )
        if not preview.allowed:
            # Keep the prior generation selected. The next normal worker tick
            # rechecks the same roster change after cache, capacity or roster
            # blockers clear; no failed application can poison reconciliation.
            # Missing assets are asked for now, so the recheck finds them.
            if _profile_preview_is_waitable(preview):
                self._request_preparations(preview, actor=selected.actor)
            return False
        pending: FleetProfileApplicationView | None = None
        try:
            pending = self._create_pending_application(
                preview,
                request_key=request_key,
                actor=selected.actor,
                operation_kind=ProfileOperationKind.APPLY.value,
                select_profile=True,
                selection_precondition=selected,
            )
            self._queue_application(
                preview,
                request_key=request_key,
                actor=selected.actor,
                operation_kind=ProfileOperationKind.APPLY.value,
                pending_application_id=pending.id,
                platform_maintenance=True,
            )
        except (FleetProfileAdmissionBusy, FleetProfileAdmissionEffectBusy) as error:
            if pending is None:
                raise
            self._defer_pending_application(
                pending.id,
                str(error),
                code=_deferral_code(error),
                storage=_storage_wait_of(error),
            )
        except FleetProfileAdmissionStorageError as error:
            if pending is None:
                raise
            self._defer_pending_application(
                pending.id, str(error), retry_delay=timedelta(seconds=60)
            )
        except _FleetProfileSupersededIntentConflict as error:
            if pending is None:
                raise
            self._finish_pending_admission(
                pending.id,
                state=LifecycleState.SUPERSEDED,
                reason=str(error),
                code=SupersedeCode.SUPERSEDED_BY_INTENT,
            )
        except FleetProfileConflict as error:
            if pending is None:
                raise
            self._finish_pending_admission(
                pending.id,
                state=LifecycleState.FAILED,
                reason=str(error) or "Profile reconcile failed",
            )
        return True
