"""Application projection for Fleet profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import ValidationError
from sqlalchemy.orm import Session, object_session
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    ProfileReasonCode,
)
from vonk_agent_protocol.agent_words import (
    ProfileCancellationCause,
    ProfileChildJobKind,
    ProfileChildPhase,
    ProfileEffectState,
)

from .. import job_states
from ..categorized_errors import MissingRecord
from ..fleet_profile_adapter_conversion import (
    conversion_observation,
    conversion_progress,
    needs_conversion,
)
from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationProjectionIssue,
    FleetProfileApplicationView,
    FleetProfileAssignment,
    FleetProfileEffectProgress,
    FleetProfileEffectState,
    FleetProfileIntendedConfiguration,
    FleetProfilePreview,
    profile_switch_child_request_key,
)
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..models import FleetProfileApplication, Job
from ..operation_blockers import bound_blockers, make_blocker
from ..operation_progress import project_progress
from ..run_switch_contract import RunSwitchStopResult
from ..run_switch_operations import RunSwitchOperationService
from .assessment_support import (
    _operation_state,
)
from .contracts import FleetProfileReviewStale
from .persistence import (
    _persisted_profile_plan,
    _persisted_profile_progress,
    _persisted_profile_result,
    _progress_from_receipt,
    _stored_progress,
)
from .projection_support import (
    _application_cancellation_view,
    _aware,
    _digest,
    _validate_remaining_effects,
)
from .run_switch_adapter import RunSwitchFleetProfileAdapter

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    @staticmethod
    def _effect_progress(
        session: Session,
        row: FleetProfileApplication,
        plan: FleetProfilePreview,
        progress: FleetProfileApplicationProgress,
    ) -> list[FleetProfileEffectProgress]:
        effects: list[FleetProfileEffectProgress] = []
        owners: list[
            tuple[
                FleetProfileApplication,
                FleetProfileApplicationProgress,
                set[str] | None,
            ]
        ] = [(row, progress, None)]
        for binding in plan.effects.adopted:
            owner = session.get(FleetProfileApplication, binding.application_id)
            if owner is not None:
                owners.append(
                    (owner, _persisted_profile_progress(owner), set(binding.node_ids))
                )
        for owner, owner_progress, scope in owners:
            document = owner_progress.switch_adapter
            if document is None or owner_progress.workload_intent_ordinal is None:
                continue
            owner_plan = _persisted_profile_plan(owner)
            if isinstance(owner_plan, Residue):
                continue
            pending = {child.queue_index: child for child in document.pending_children}
            completed = {child.queue_index: child for child in document.children}
            for index, item in enumerate(document.queue):
                nodes = RunSwitchFleetProfileAdapter._queue_item_nodes(owner, item)
                if not nodes or (scope is not None and not set(nodes) <= scope):
                    continue
                closed = completed.get(index)
                active = pending.get(index)
                effect_state: FleetProfileEffectState = (
                    closed.state.value
                    if closed
                    else ProfileEffectState.PENDING.value
                    if active
                    else ProfileEffectState.UNKNOWN.value
                    if index in document.skipped_indices
                    else ProfileEffectState.NOT_ISSUED.value
                )
                stop = (
                    next(
                        (
                            effect
                            for effect in owner_plan.effects.runs
                            if effect.run_id == item.id
                            and effect.action == ProfileChildPhase.STOP.value
                        ),
                        None,
                    )
                    if item.kind == ProfileChildPhase.STOP.value
                    else None
                )
                if (
                    stop is not None
                    and closed is not None
                    and closed.state == LifecycleState.SUCCEEDED
                    and (
                        closed.result is None
                        or not any(
                            isinstance(receipt, RunSwitchStopResult)
                            and receipt.run_id == stop.run_id
                            for receipt in closed.result.run_switch.phase_results
                        )
                    )
                ):
                    effect_state = ProfileEffectState.UNKNOWN.value
                record = closed or active
                child_progress = None
                if record is not None:
                    child_job = session.get(Job, record.operation_id)
                    expected_kind = (
                        ProfileChildJobKind.STOP.value
                        if item.kind == ProfileChildPhase.STOP.value
                        else ProfileChildJobKind.CLEANUP.value
                        if item.kind == ProfileChildPhase.CLEANUP.value
                        else ProfileChildJobKind.RUN_SWITCH.value
                    )
                    if (
                        child_job is not None
                        and child_job.kind == expected_kind
                        and child_job.request_id
                        == profile_switch_child_request_key(
                            owner.id, index, item.kind, item.id
                        )
                        and sorted(child_job.targets) == sorted(nodes)
                    ):
                        try:
                            child_progress = RunSwitchOperationService._operation_view(
                                child_job
                            ).progress
                        except (TypeError, ValueError):
                            # Damaged measurement is unavailable; queue identity,
                            # exact effects, receipts and claims remain visible.
                            child_progress = None
                effects.append(
                    FleetProfileEffectProgress(
                        effect_id=f"{owner.id}:queue:{index}:{item.kind}:{item.id}",
                        application_id=owner.id,
                        plan_digest=owner.plan_digest,
                        workload_intent_ordinal=owner_progress.workload_intent_ordinal,
                        queue_index=index,
                        kind=item.kind,
                        target_id=item.id,
                        node_ids=list(nodes),
                        request_key=profile_switch_child_request_key(
                            owner.id, index, item.kind, item.id
                        ),
                        operation_id=closed.operation_id
                        if closed
                        else active.operation_id
                        if active
                        else None,
                        state=effect_state,
                        result=closed.result if closed else None,
                        progress=child_progress,
                        stop_effect=stop,
                        original_operation_id=closed.original_operation_id
                        if closed
                        else active.original_operation_id
                        if active
                        else None,
                    )
                )
        return effects

    def _application_view(
        self, row: FleetProfileApplication
    ) -> FleetProfileApplicationView:
        try:
            view = self._stored_application_view(row)
            # Metadata uncertainty must not exempt a success receipt from its
            # invariants (the retained-journal projection can carry an issue).
            if (
                view.projection_issue is not None
                and view.state == LifecycleState.SUCCEEDED
            ):
                FleetProfileApplicationView.model_validate_json(
                    view.model_dump_json(exclude={"projection_issue"})
                )
            return view
        except ValidationError:
            progress = _progress_from_receipt(row)
            detail = "Stored application state is inconsistent; effect unknown"
            return FleetProfileApplicationView(
                id=row.id,
                request_key=row.request_key,
                profile_id=row.profile_id,
                profile_digest=row.profile_digest,
                plan_digest=row.plan_digest,
                state=LifecycleState.CANCELLED,
                attempt=progress.attempt,
                retry_of_application_id=progress.retry_of_application_id,
                current_step=row.current_step,
                total_steps=max(row.current_step, progress.total_steps),
                current_operation_id=row.current_operation_id,
                status_reason=detail,
                progress=progress,
                result=None,
                projection_issue=FleetProfileApplicationProjectionIssue(
                    code=ProfileReasonCode.APPLICATION_INTENT_INVALID,
                    detail=detail,
                ),
                created_at=_aware(row.created_at),
                updated_at=_aware(row.updated_at),
            )

    def _stored_application_view(
        self, row: FleetProfileApplication
    ) -> FleetProfileApplicationView:
        # A damaged plan exposes recorded progress without cancellation authority;
        # a damaged result is unavailable, never reconstructed from a state label.
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        if needs_conversion(row):
            observed = conversion_observation(row)
            plan = _persisted_profile_plan(row)
            total = row.current_step if isinstance(plan, Residue) else len(plan.steps)
            blockers = bound_blockers(
                [
                    make_blocker(
                        ProfileReasonCode.RETRY_CONFLICT,
                        observed.detail
                        or "Exact retained child evidence is being reconciled",
                    )
                ]
            )
            outer_progress = conversion_progress(row)
            progress = outer_progress or _progress_from_receipt(row)
            progress.total_steps = total
            progress.current_label = "Reconciling exact retained cleanup journal"
            progress.retry_due_at = observed.next_attempt_at
            progress.blockers = blockers
            state = _operation_state(row.state, default=LifecycleState.RUNNING)
            return FleetProfileApplicationView(
                id=row.id,
                request_key=row.request_key,
                profile_id=row.profile_id,
                profile_digest=row.profile_digest,
                plan_digest=row.plan_digest,
                state=state,
                attempt=progress.attempt,
                retry_of_application_id=progress.retry_of_application_id,
                superseded_by=progress.superseded_by
                if state == ProfileCancellationCause.SUPERSEDED.value
                else None,
                reason_code=progress.supersede_code
                if state == ProfileCancellationCause.SUPERSEDED.value
                else None,
                current_step=row.current_step,
                total_steps=total,
                current_operation_id=row.current_operation_id,
                status_reason=row.status_reason
                if state == LifecycleState.SUCCEEDED.value
                else observed.detail,
                progress=progress,
                result=_persisted_profile_result(row),
                projection_issue=(
                    FleetProfileApplicationProjectionIssue(
                        code=ProfileReasonCode.APPLICATION_INTENT_INVALID,
                        detail="Retained outer metadata cannot be verified; historical state and exact child evidence remain unchanged",
                    )
                    if outer_progress is None
                    else None
                ),
                blockers=blockers,
                next_attempt_at=observed.next_attempt_at,
                created_at=_aware(row.created_at),
                updated_at=_aware(row.updated_at),
            )
        plan = _persisted_profile_plan(row)
        progress = _persisted_profile_progress(row)
        if (
            row.state in {LifecycleState.QUEUED.value, LifecycleState.RUNNING.value}
            and progress.child_progress
            and progress.child_progress.operation
        ):
            progress.child_progress.operation = project_progress(
                progress.child_progress.operation, _aware(self._clock())
            )
        state, next_attempt_at = self._presented_state(row, progress)
        session = object_session(row)
        if session is not None and not isinstance(plan, Residue):
            progress = progress.model_copy(
                update={"effects": self._effect_progress(session, row, plan, progress)}
            )
        return FleetProfileApplicationView(
            id=row.id,
            request_key=row.request_key,
            profile_id=row.profile_id,
            profile_digest=row.profile_digest,
            plan_digest=row.plan_digest,
            state=state,
            attempt=progress.attempt,
            retry_of_application_id=progress.retry_of_application_id,
            superseded_by=progress.superseded_by
            if state == ProfileCancellationCause.SUPERSEDED.value
            else None,
            reason_code=progress.supersede_code
            if state == ProfileCancellationCause.SUPERSEDED.value
            else None,
            current_step=row.current_step,
            total_steps=progress.total_steps
            if isinstance(plan, Residue)
            else len(plan.steps),
            current_operation_id=row.current_operation_id,
            status_reason=row.status_reason,
            progress=progress,
            cancellation=(
                None
                if isinstance(plan, Residue)
                else _application_cancellation_view(row, plan, progress)
            ),
            result=_persisted_profile_result(row),
            blockers=(
                list(progress.blockers)
                if state
                in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.FAILED,
                    LifecycleState.NEEDS_OPERATOR,
                )
                else []
            ),
            next_attempt_at=next_attempt_at,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _intended_profile(
        application: FleetProfileApplication,
        *,
        session: Session,
    ) -> FleetProfileIntendedConfiguration | Residue:
        """The accepted intent of an application, or why none can be established.

        An application whose accepted intent cannot be read (never recorded, or
        its reviewed plan is damaged) is a :class:`Residue`: the caller retires or
        skips it. Inconsistent local projections are unknown too: execution never
        invents accepted effects, and a fresh reviewed load remains admissible.
        """

        progress = _stored_progress(application)
        if isinstance(progress, Residue):
            return progress
        if progress.intended_profile is None:
            return retire_as_unknown(
                "profile-intent",
                str(application.id),
                BookkeepingReason.ROW_INCOMPLETE,
                "the application carries no accepted intent",
            )
        if progress.intended_profile.profile_digest != application.profile_digest:
            return retire_as_unknown(
                "profile-intent",
                str(application.id),
                BookkeepingReason.EVIDENCE_MISMATCH,
                "the accepted intent identity differs from its row",
            )
        intended = progress.intended_profile
        root = (
            application
            if intended.reviewed_application_id == application.id
            else session.get(FleetProfileApplication, intended.reviewed_application_id)
        )
        if (
            root is None
            or root.profile_id != application.profile_id
            or root.profile_digest != application.profile_digest
        ):
            return retire_as_unknown(
                "profile-intent",
                str(application.id),
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "the reviewed application is unavailable",
            )
        root_progress = _stored_progress(root)
        if isinstance(root_progress, Residue):
            return root_progress
        root_plan = _persisted_profile_plan(root)
        if isinstance(root_plan, Residue):
            return root_plan
        if (
            root_progress.retry_of_application_id is not None
            or root_progress.intended_profile != intended
            or intended.reviewed_application_id != root.id
            or intended.reviewed_plan_digest != _digest(root_plan.reviewed_decision())
        ):
            return retire_as_unknown(
                "profile-intent",
                str(application.id),
                BookkeepingReason.EVIDENCE_MISMATCH,
                "the reviewed application identity is inconsistent",
            )
        plan = _persisted_profile_plan(application)
        if isinstance(plan, Residue):
            return plan
        if (
            plan.scope.node_ids != intended.scope.node_ids
            or plan.resolved_assignments
            != sorted(intended.assignments, key=lambda item: item.id)
        ):
            return retire_as_unknown(
                "profile-intent",
                str(application.id),
                BookkeepingReason.EVIDENCE_MISMATCH,
                "persisted application plan exceeds its reviewed intent",
            )
        try:
            _validate_remaining_effects(root_plan.effects, plan.effects)
        except FleetProfileReviewStale:
            return retire_as_unknown(
                "profile-intent",
                str(application.id),
                BookkeepingReason.EVIDENCE_MISMATCH,
                "persisted application effects exceed its reviewed intent",
            )
        return intended

    @staticmethod
    def _reviewed_profile_plan(
        application: FleetProfileApplication, *, session: Session
    ) -> FleetProfilePreview | Residue:
        from .service import FleetProfileService

        intended = FleetProfileService._intended_profile(application, session=session)
        if isinstance(intended, Residue):
            return intended
        reviewed = session.get(
            FleetProfileApplication, intended.reviewed_application_id
        )
        if reviewed is None:
            return retire_as_unknown(
                "profile-intent",
                str(application.id),
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "the reviewed application is not stored",
            )
        return _persisted_profile_plan(reviewed)

    def _application_assignments(
        self, application_id: str
    ) -> tuple[FleetProfileAssignment, ...]:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            intended = self._intended_profile(application, session=session)
            if isinstance(intended, Residue):
                return ()
            return tuple(sorted(intended.assignments, key=lambda item: item.id))
