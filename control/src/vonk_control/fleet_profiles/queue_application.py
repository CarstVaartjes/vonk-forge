"""Queue application for Fleet profiles."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from vonk_agent_protocol import (
    DesiredAssignmentState,
    InvalidRequestReason,
    LifecycleState,
    ObservedAssignmentState,
    ProfileReasonCode,
    SupersedeCode,
)
from vonk_agent_protocol.agent_words import (
    ProfileCancellationCause,
    ProfileChildPhase,
    ProfileOperationKind,
    ProfileReasonSeverity,
)

from .. import job_states
from ..categorized_errors import MissingRecord
from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
    FleetProfileApplicationView,
    FleetProfileIntendedConfiguration,
    FleetProfileOperationKind,
    FleetProfilePreview,
    FleetProfileScope,
)
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..lifecycle.fleet_profile import FleetProfileAdapter
from ..lifecycle.types import Effect as _LifecycleEffect
from ..models import (
    AgentNode,
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
    RecipeRun,
    RunNode,
)
from ..preparation_contract import RuntimeImageIdentity
from ..profile_capacity import (
    release_replaced_profile_claims,
    reserve_profile_disk,
    reserve_profile_memory,
    reserve_profile_ports,
)
from ..recipe_build_cancellation import (
    BuildConsumerError,
    lock_profile_build_dependencies,
)
from .assessment_support import (
    _progress_with_blockers,
    _require_recovery_preparations,
)
from .contracts import (
    FleetProfileAdmissionEffectBusy,
    FleetProfileConflict,
    FleetProfileInvalid,
    FleetProfileSelectionLost,
    FleetProfileStalePlanConflict,
    _FleetProfileSupersededIntentConflict,
)
from .dependencies import _INSTALLATION_POLICY_ADAPTER
from .persistence import (
    _application_order_key,
    _newer_profile_intent_overlaps,
    _next_profile_acceptance_time,
    _persisted_profile_plan,
    _persisted_profile_progress,
    _remaining_reviewed_effects,
)
from .projection_support import (
    _aware,
    _digest,
    _replace_selected_profile_application,
    _replace_selected_profile_roster,
    _set_selected_profile,
    _validate_remaining_effects,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def _queue_application(
        self,
        preview: FleetProfilePreview,
        *,
        request_key: str,
        actor: str,
        operation_kind: FleetProfileOperationKind,
        retry_of_application_id: str | None = None,
        automatic_cache_recovery: bool = False,
        pending_application_id: str | None = None,
        platform_maintenance: bool = False,
    ) -> FleetProfileApplicationView:
        """Admit new submissions as their actor; maintain accepted intent as the platform."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        now = _aware(self._clock())
        reviewed_plan_digest = preview.plan_digest
        application_id = pending_application_id or str(uuid.uuid4())
        # (An executor that is not bound yet is not a reason to refuse: the load is
        # accepted and queued, and its worker issues the steps once it is bound.)
        # The preview identifies the work; the request identifies its execution.
        # The same reconciliation can be needed again after a failure or drift.
        preview = preview.model_copy(
            update={
                "plan_digest": _digest(
                    {
                        "schema_version": 2,
                        "reconciliation_digest": preview.plan_digest,
                        "retry_of_application_id": retry_of_application_id,
                        "request_key": request_key,
                    }
                )
            }
        )
        with self._admission_session(
            actor,
            node_ids=preview.scope.node_ids,
            # A durable pending receipt also exists during a NEW submission.
            # Only the maintenance callers may bypass current actor authority.
            platform_maintenance=platform_maintenance,
        ) as session:
            profile = session.get(
                FleetProfile, preview.profile_id, with_for_update={"nowait": True}
            )
            if profile is None:
                raise MissingRecord(
                    preview.profile_id, reason=InvalidRequestReason.NOT_FOUND
                )
            existing = session.scalar(
                select(FleetProfileApplication)
                .where(FleetProfileApplication.request_key == request_key)
                .with_for_update(nowait=True)
            )
            existing_progress = (
                _persisted_profile_progress(existing) if existing is not None else None
            )
            pending_roster_reconciliation = bool(
                existing is not None
                and existing_progress is not None
                and existing_progress.admission_pending
                and existing.selection_generation is not None
            )
            selected_application = bool(
                existing is not None and existing.selection_generation is not None
            )
            retry_parent: FleetProfileApplication | None = None
            retry_parent_progress: FleetProfileApplicationProgress | None = None
            selected_generation: int | None = None
            selected_application_id: str | None = None
            selected_roster_digest: str | None = None
            if retry_of_application_id is not None:
                retry_parent = session.get(
                    FleetProfileApplication,
                    retry_of_application_id,
                    with_for_update={"nowait": True},
                )
                if retry_parent is None:
                    raise MissingRecord(
                        retry_of_application_id, reason=InvalidRequestReason.NOT_FOUND
                    )
                retry_parent_progress = _persisted_profile_progress(retry_parent)
                selected_application = selected_application or (
                    retry_parent_progress.intended_profile is not None
                    and self._application_is_current_selection(
                        session, retry_parent, retry_parent_progress
                    )
                )
            pending_ordinal: int | None = None
            created_at = (
                _next_profile_acceptance_time(session, now) if existing is None else now
            )
            if existing is not None:
                pending = (
                    pending_application_id == existing.id
                    and existing_progress is not None
                    and existing_progress.admission_pending
                )
                if pending and existing_progress is not None:
                    pending_ordinal = existing_progress.workload_intent_ordinal
                if pending and existing.state in {
                    LifecycleState.CANCELLED.value,
                    ProfileCancellationCause.SUPERSEDED.value,
                }:
                    raise FleetProfileInvalid(
                        "Profile application was superseded by a later intent",
                        reason=InvalidRequestReason.SUPERSEDED,
                    )
                if not pending:
                    return self._matching_application(
                        existing,
                        session=session,
                        profile_id=preview.profile_id,
                        reviewed_digest=reviewed_plan_digest
                        if retry_of_application_id is None
                        else None,
                        actor=actor,
                        retry_of_application_id=retry_of_application_id,
                    )
            if selected_application:
                selection = session.get(
                    FleetProfileSelection, 1, with_for_update={"nowait": True}
                )
                selection_source = (
                    session.get(FleetProfileApplication, selection.application_id)
                    if pending_roster_reconciliation and selection is not None
                    else existing
                    if existing is not None
                    and existing.selection_generation is not None
                    else retry_parent
                )
                selection_source_progress = (
                    _persisted_profile_progress(selection_source)
                    if selection_source is not None and selection_source is not existing
                    else existing_progress
                    if selection_source is existing
                    else retry_parent_progress
                )
                if (
                    selection is None
                    or selection_source is None
                    or selection_source_progress is None
                    or (
                        pending_roster_reconciliation
                        and existing is not None
                        and existing.selection_generation != selection.generation
                    )
                    or not self._application_is_current_selection(
                        session, selection_source, selection_source_progress
                    )
                ):
                    raise _FleetProfileSupersededIntentConflict(
                        "Selected Fleet profile was replaced by a newer load"
                    )
                selected_generation = selection.generation
                selected_application_id = selection.application_id
                selected_roster_digest = selection.roster_digest
            # Use the reviewed snapshot. Resolving through the cache here both
            # substituted newer choices and performed storage work under SQL locks.
            frozen_assignments = tuple(preview.resolved_assignments)
            if not selected_application:
                self._validate_draft_review_identity(
                    session,
                    profile,
                    profile_digest=preview.profile_digest,
                    assignments=frozen_assignments,
                )
            self._reserve_preview_assets(session, preview, now=now)
            stop_ids = tuple(
                sorted(
                    {
                        effect.run_id
                        for effect in preview.effects.runs
                        if effect.action == ProfileChildPhase.STOP.value
                    }
                )
            )
            if stop_ids:
                # A reviewed replacement owns the exact runs and run-node rows
                # it is about to stop. Lock them before the roster snapshot so
                # another owner cannot change a reviewed effect mid-admission.
                tuple(
                    session.scalars(
                        select(RecipeRun)
                        .where(RecipeRun.id.in_(stop_ids))
                        .order_by(RecipeRun.id)
                        .with_for_update(nowait=True)
                    )
                )
                tuple(
                    session.scalars(
                        select(RunNode)
                        .where(RunNode.run_id.in_(stop_ids))
                        .order_by(RunNode.run_id, RunNode.rank)
                        .with_for_update(nowait=True)
                    )
                )
            scope_nodes = list(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                    .with_for_update(nowait=True)
                )
            )
            frozen_nodes = tuple(node.node_id for node in scope_nodes)
            if frozen_nodes != tuple(preview.scope.node_ids):
                raise FleetProfileStalePlanConflict(
                    "Profile fleet scope changed during admission; review again"
                )
            if selected_application:
                intended = (
                    existing_progress.intended_profile
                    if existing_progress is not None
                    else retry_parent_progress.intended_profile
                    if retry_parent_progress is not None
                    else None
                )
                if (
                    intended is None
                    or intended.profile_digest != preview.profile_digest
                    or tuple(intended.scope.node_ids) != frozen_nodes
                    or tuple(intended.assignments) != tuple(frozen_assignments)
                    or (
                        retry_of_application_id is None
                        and intended.reviewed_plan_digest != reviewed_plan_digest
                    )
                    or (
                        retry_of_application_id is None
                        and intended.reviewed_application_id != application_id
                    )
                ):
                    raise _FleetProfileSupersededIntentConflict(
                        "Selected profile differs from its accepted snapshot",
                    )
                installation_policy = intended.installation_policy
            else:
                installation_policy = _INSTALLATION_POLICY_ADAPTER.validate_python(
                    profile.installation_policy, strict=True
                )
                intended = FleetProfileIntendedConfiguration(
                    profile_digest=preview.profile_digest,
                    reviewed_plan_digest=reviewed_plan_digest,
                    reviewed_application_id=application_id,
                    installation_policy=installation_policy,
                    scope=FleetProfileScope(node_ids=list(frozen_nodes)),
                    assignments=list(frozen_assignments),
                )
            execution_nodes = {
                node_id for step in preview.steps for node_id in step.node_ids
            }
            if not execution_nodes <= set(frozen_nodes):
                raise FleetProfileStalePlanConflict(
                    "Profile switch scope changed during admission"
                )
            whole_fleet_intent = selected_application or retry_of_application_id is None
            prior_profile_work = (
                session.scalar(
                    select(FleetProfileApplication.id)
                    .where(
                        FleetProfileApplication.id != application_id,
                        FleetProfileApplication.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.NEEDS_OPERATOR,
                            )
                        ),
                    )
                    .limit(1)
                )
                if whole_fleet_intent and self._switch_adapter is not None
                else None
            )
            fenced_nodes = (
                set(frozen_nodes)
                if whole_fleet_intent
                and self._switch_adapter is not None
                and (execution_nodes or prior_profile_work is not None)
                else execution_nodes
            )
            adopted_nodes = {
                node_id
                for effect in preview.effects.adopted
                for node_id in effect.node_ids
            }
            adopted_application_ids = {
                effect.application_id for effect in preview.effects.adopted
            }
            fenced_nodes -= adopted_nodes
            attempt = 1
            recovery_ordinal: int | None = None
            accepted_images: dict[str, RuntimeImageIdentity] = {}
            reviewed_application: FleetProfileApplication | None = None
            if retry_of_application_id is not None:
                parent = retry_parent
                if parent is None:
                    raise MissingRecord(
                        retry_of_application_id, reason=InvalidRequestReason.NOT_FOUND
                    )
                prior = retry_parent_progress or _persisted_profile_progress(parent)
                if parent.state not in job_states.words(
                    LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
                ):
                    return self._application_view(parent)
                if prior.intended_profile is None:
                    return self._decline_retry(
                        parent,
                        ProfileReasonCode.RETRY_INTENT_UNAVAILABLE,
                        "the receipt carries no accepted intent to recover",
                    )
                if self._superseding_intent(session, parent, prior):
                    return self._decline_retry(
                        parent,
                        ProfileReasonCode.RETRY_INTENT_UNAVAILABLE,
                        "A newer accepted workload intent owns the effects",
                    )
                parent_intent = self._intended_profile(parent, session=session)
                if isinstance(parent_intent, Residue):
                    return self._decline_retry(
                        parent,
                        ProfileReasonCode.RETRY_INTENT_UNAVAILABLE,
                        parent_intent.note,
                    )
                intended = parent_intent
                reviewed_application = session.get(
                    FleetProfileApplication, intended.reviewed_application_id
                )
                if reviewed_application is None:
                    raise _FleetProfileSupersededIntentConflict(
                        "Persisted application review source is unavailable",
                    )
                reviewed_plan = _persisted_profile_plan(reviewed_application)
                if isinstance(reviewed_plan, Residue):
                    return self._decline_retry(
                        parent,
                        ProfileReasonCode.RETRY_REVIEW_UNAVAILABLE,
                        "the reviewed plan of the receipt cannot be read",
                    )
                _validate_remaining_effects(reviewed_plan.effects, preview.effects)
                attempt = prior.attempt + 1
                _require_recovery_preparations(reviewed_plan, preview)
                accepted_images = {
                    item.assignment_id: item.runtime_image
                    for item in reviewed_plan.preparation_decisions
                }
                if automatic_cache_recovery:
                    # A receipt that recorded no workload intent takes a fresh one
                    # from the node fence below, like any other admission.
                    recovery_ordinal = prior.workload_intent_ordinal
            control = self._control_effects(
                session,
                frozen_assignments,
                set(frozen_nodes),
                installation_policy,
                expected_images=accepted_images,
                excluded_application_id=application_id,
            )
            if (
                control.effects != _remaining_reviewed_effects(session, preview.effects)
                or control.changed_nodes != execution_nodes
                or any(
                    reason.severity == ProfileReasonSeverity.ERROR.value
                    for reason in control.reasons
                )
                or any(
                    control.states[item.assignment_id].current_state
                    != item.current_state
                    for item in preview.assignments
                )
            ):
                raise FleetProfileStalePlanConflict(
                    "Profile workload effects changed during admission; review again"
                )
            affected_nodes = [
                node for node in scope_nodes if node.node_id in fenced_nodes
            ]
            intent_order = (
                _application_order_key(session, existing)
                if existing is not None
                else _application_order_key(session, reviewed_application)
                if reviewed_application is not None
                else (_aware(created_at), application_id)
            )
            if not selected_application:
                current_selection = session.get(
                    FleetProfileSelection, 1, with_for_update={"nowait": True}
                )
                current_selected_application = (
                    session.get(
                        FleetProfileApplication, current_selection.application_id
                    )
                    if current_selection is not None
                    else None
                )
                current_selected_order = None
                if current_selected_application is not None:
                    try:
                        current_selected_order = _application_order_key(
                            session, current_selected_application
                        )
                    except FleetProfileConflict:
                        # The selection FK and receipt columns remain
                        # sufficient to order this accepted root against a
                        # fresh request. Corrupt retry/progress history must
                        # not veto unrelated newly reviewed work.
                        current_selected_order = (
                            _aware(current_selected_application.created_at),
                            current_selected_application.id,
                        )
                if (
                    current_selection is not None
                    and current_selected_application is not None
                    and current_selected_application.selection_generation
                    == current_selection.generation
                    and current_selected_order is not None
                    and current_selected_order > intent_order
                ):
                    raise _FleetProfileSupersededIntentConflict(
                        "Profile load was superseded by a newer accepted profile"
                    )
            if _newer_profile_intent_overlaps(session, intent_order, fenced_nodes):
                raise _FleetProfileSupersededIntentConflict(
                    "Profile intent was superseded by a later accepted overlapping request"
                )
            if self._switch_adapter is not None:
                self._switch_adapter.validate_resources_in_session(
                    session, frozen_assignments, preview
                )
            dependency_wait: BuildConsumerError | None = None
            try:
                lock_profile_build_dependencies(session, preview)
            except BuildConsumerError as error:
                if error.retryable:
                    dependency_wait = error
                # The reviewed build record is gone or unreadable: there is no
                # running build to protect, and the exact image identity is held
                # by the plan itself and verified again when its child starts.
                retire_as_unknown(
                    "profile-build-dependency",
                    application_id,
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    f"{error.code}: {error}",
                )
            if dependency_wait is not None:
                raise FleetProfileAdmissionEffectBusy(
                    f"{dependency_wait.code}: {dependency_wait}"
                ) from dependency_wait
            workload_intent_ordinal = (
                recovery_ordinal
                if recovery_ordinal is not None
                else pending_ordinal
                if pending_ordinal is not None
                else max(node.workload_intent_ordinal for node in affected_nodes) + 1
                if affected_nodes
                else None
            )
            if pending_ordinal is not None and any(
                node.workload_intent_ordinal != pending_ordinal
                for node in affected_nodes
            ):
                raise _FleetProfileSupersededIntentConflict(
                    "Profile workload intent was superseded before admission resumed"
                )
            if workload_intent_ordinal is not None:
                for node in affected_nodes:
                    node.workload_intent_ordinal = workload_intent_ordinal
                # (With no executor bound yet the older workloads' cancellation is
                # requested by the load's own worker when its executor is bound:
                # it requests it again for the same node fence and intent before
                # it completes.)
                if pending_ordinal is None and self._switch_adapter is not None:
                    self._switch_adapter.request_superseded_workload_cancellation_in_session(
                        session,
                        tuple(sorted(fenced_nodes)),
                        workload_intent_ordinal,
                        now,
                    )
                for prior_application in session.scalars(
                    select(FleetProfileApplication)
                    .where(
                        FleetProfileApplication.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.NEEDS_OPERATOR,
                            )
                        )
                    )
                    .order_by(FleetProfileApplication.id)
                    .with_for_update(nowait=True)
                ):
                    if prior_application.id in adopted_application_ids:
                        # Unconsumed profile promises on changed siblings lose
                        # their fence in this same transaction. Materialized
                        # installation/run claims already changed owner under
                        # the consumer's atomic handoff and remain until exact
                        # effect reconciliation. Continuing promises stay owned
                        # by the original application and are never duplicated.
                        release_replaced_profile_claims(
                            session,
                            prior_application,
                            node_ids=tuple(sorted(fenced_nodes)),
                            now=now,
                        )
                        # Flush replaced promises before a successor inserts the
                        # same unique promised port; this is not capacity freed.
                        session.flush()
                        continue
                    prior_plan = _persisted_profile_plan(prior_application)
                    if isinstance(prior_plan, Residue):
                        # Its plan cannot be read, so what it owns is unknown: it is
                        # retired by the newer authority (its agent effects were
                        # independently fenced above, and keep their own receipts).
                        self._lifecycle.cancelled(
                            prior_application,
                            "Profile order retired by a later scoped intent; its "
                            "stored plan could not be read, so its effect is unknown",
                            now,
                            effect=_LifecycleEffect.UNKNOWN,
                            session=session,
                        )
                        continue
                    try:
                        prior_scope = {
                            node_id
                            for step in prior_plan.steps
                            for node_id in step.node_ids
                        }
                        if not prior_scope & fenced_nodes:
                            continue
                        prior_progress = _persisted_profile_progress(prior_application)
                        prior_order = _application_order_key(session, prior_application)
                    except FleetProfileConflict as error:
                        # Quarantine the invalid order, retaining its evidence.
                        # Its agent effects were independently fenced above;
                        # malformed history cannot roll back the new authority.
                        self._lifecycle.fail(
                            prior_application, str(error), now, session=session
                        )
                        continue
                    prior_ordinal = prior_progress.workload_intent_ordinal
                    if prior_progress.admission_pending:
                        # A parked receipt has not passed admission and owns no
                        # selected profile or workload order. Keep a newer one
                        # parked; an older one is superseded by this acceptance.
                        if prior_order > intent_order:
                            continue
                        self._lifecycle.supersede(
                            prior_application,
                            "Profile order was replaced before admission by a later "
                            "scoped intent",
                            now,
                            code=SupersedeCode.SUPERSEDED_BY_INTENT,
                            by=application_id,
                            session=session,
                        )
                        continue
                    if (
                        prior_order > intent_order
                        and prior_progress.intended_profile is not None
                    ):
                        # New pending receipts do not acquire the node fence.
                        # A later accepted receipt can therefore appear after
                        # the earlier overlap scan; preserve it instead of
                        # cancelling it as though it were an older pending row.
                        raise _FleetProfileSupersededIntentConflict(
                            "Profile intent was superseded by a later accepted overlapping request"
                        )
                    if (
                        prior_ordinal is None
                        or prior_ordinal >= workload_intent_ordinal
                    ):
                        continue
                    self._lifecycle.supersede(
                        prior_application,
                        "Profile order was replaced by a later scoped intent; "
                        "issued effects retain their own cancellation receipts",
                        now,
                        code=SupersedeCode.SUPERSEDED_BY_INTENT,
                        by=application_id,
                        effect=_LifecycleEffect.UNKNOWN,
                        session=session,
                    )
            progress = FleetProfileApplicationProgress(
                operation_kind=operation_kind,
                attempt=attempt,
                retry_of_application_id=retry_of_application_id,
                intended_profile=intended,
                workload_intent_ordinal=workload_intent_ordinal,
                completed_steps=0,
                total_steps=len(preview.steps),
            ).model_dump(mode="json")
            row = existing
            if row is None:
                row = FleetProfileAdapter.new_application(
                    id=application_id,
                    request_key=request_key,
                    profile_id=preview.profile_id,
                    profile_digest=preview.profile_digest,
                    plan_digest=preview.plan_digest,
                    state=LifecycleState.SUCCEEDED.value
                    if not preview.steps and not preview.effects.adopted
                    else LifecycleState.QUEUED.value,
                    plan=preview.model_dump(mode="json"),
                    current_step=0,
                    current_operation_id=None,
                    selection_generation=(
                        selected_generation if selected_application else None
                    ),
                    progress=progress,
                    result=(
                        FleetProfileApplicationResult(
                            changed=False, completed_steps=0
                        ).model_dump(mode="json")
                        if not preview.steps and not preview.effects.adopted
                        else None
                    ),
                    actor=actor,
                    created_at=created_at,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.profile_id = preview.profile_id
                row.profile_digest = preview.profile_digest
                row.plan_digest = preview.plan_digest
                self._lifecycle.reset(
                    row,
                    now,
                    steps=bool(preview.steps or preview.effects.adopted),
                    session=session,
                )
                row.plan = preview.model_dump(mode="json")
                row.current_step = 0
                row.current_operation_id = None
                row.progress = progress
                row.result = (
                    FleetProfileApplicationResult(
                        changed=False, completed_steps=0
                    ).model_dump(mode="json")
                    if not preview.steps and not preview.effects.adopted
                    else None
                )
                row.status_reason = None
                row.updated_at = now
            session.flush()
            if retry_of_application_id is not None and existing is None:
                if retry_parent is not None:
                    # An ended parent absorbs the event; the reason is still the
                    # record of why no attempt is scheduled for it.
                    # The earlier failure stays in the reason: it is what the
                    # operator needs, and the successor carries the continuation.
                    earlier = (retry_parent.status_reason or "").strip()
                    superseded_by = (
                        f"Reconciled by profile retry {row.id}"
                        + (f"; earlier failure: {earlier}" if earlier else "")
                    )[:512]
                    self._lifecycle.supersede(
                        retry_parent,
                        superseded_by,
                        now,
                        code=SupersedeCode.SUPERSEDED_BY_RETRY,
                        by=row.id,
                        session=session,
                    )
                    retry_parent.status_reason = superseded_by
                    # Superseded by its retry: no attempt is scheduled for it.
                    retry_parent.progress = _progress_with_blockers(
                        _persisted_profile_progress(retry_parent),
                        [],
                        retry_due_at=None,
                    ).model_dump(mode="json")
                    retry_parent.updated_at = now
                if (
                    selected_generation is None
                    or selected_application_id is None
                    or selected_roster_digest is None
                ):
                    raise FleetProfileSelectionLost(
                        "Selected profile retry lost its current selection"
                    )
                row.selection_generation = selected_generation
                _replace_selected_profile_application(
                    session,
                    expected_generation=selected_generation,
                    expected_application_id=selected_application_id,
                    expected_roster_digest=selected_roster_digest,
                    application_id=row.id,
                    now=now,
                )
                session.flush()
            reserve_profile_disk(
                session,
                row,
                preview,
                {
                    item.id
                    for item in frozen_assignments
                    if not control.states[item.id].installation_ready
                    and item.id
                    not in {
                        identifier
                        for effect in preview.effects.adopted
                        for identifier in effect.assignment_ids
                    }
                },
                now=now,
            )
            runtime_assignments = {
                item.id
                for item in frozen_assignments
                if item.desired_state == DesiredAssignmentState.RUNNING
                and item.id
                not in {
                    identifier
                    for effect in preview.effects.adopted
                    for identifier in effect.assignment_ids
                }
                and control.states[item.id].current_state
                != ObservedAssignmentState.RUNNING
            }
            reserve_profile_ports(
                session,
                row,
                preview,
                runtime_assignments,
                now=now,
            )
            reserve_profile_memory(
                session,
                row,
                preview,
                runtime_assignments,
                now=now,
            )
            current_scope = tuple(
                session.scalars(
                    select(AgentNode.node_id)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                )
            )
            if current_scope != frozen_nodes:
                raise FleetProfileStalePlanConflict(
                    "Profile fleet scope changed during admission; review again"
                )
            if pending_roster_reconciliation:
                if (
                    selected_generation is None
                    or selected_application_id is None
                    or selected_roster_digest is None
                ):
                    raise FleetProfileStalePlanConflict(
                        "Selected profile changed before roster admission"
                    )
                row.selection_generation = _replace_selected_profile_roster(
                    session,
                    expected_generation=selected_generation,
                    expected_application_id=selected_application_id,
                    expected_roster_digest=selected_roster_digest,
                    profile_id=preview.profile_id,
                    profile_revision=(
                        preview.profile_revision
                        if preview.profile_revision is not None
                        else profile.revision
                    ),
                    application_id=row.id,
                    node_ids=frozen_nodes,
                    now=now,
                )
            elif (
                existing is not None
                and existing_progress is not None
                and existing_progress.admission_pending
                and existing.selection_generation is None
                and operation_kind == ProfileOperationKind.APPLY.value
            ):
                if preview.profile_revision is None:
                    raise _FleetProfileSupersededIntentConflict(
                        "Selected profile revision is unavailable",
                    )
                row.selection_generation = _set_selected_profile(
                    session,
                    profile_id=preview.profile_id,
                    profile_revision=preview.profile_revision,
                    application_id=row.id,
                    node_ids=frozen_nodes,
                    now=now,
                )
            session.flush()
            return self._application_view(row)
