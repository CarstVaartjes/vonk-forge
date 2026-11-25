"""Assessment for Fleet profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as _typing_cast
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from vonk_forge_contracts import RecipeDefinition

from vonk_agent_protocol.agent_words import (
    ProfileAction,
    ProfileChildPhase,
    ProfileInstallationPolicy,
    ProfileProjectionKind,
    ProfileReasonSeverity,
)
from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason, WaitReason
from vonk_agent_protocol.reason_codes.profiles import ProfileReasonCode
from vonk_agent_protocol.state_machines import (
    DesiredAssignmentState,
    ObservedAssignmentState,
)
from vonk_control.categorized_errors import BookkeepingUnknown, MissingRecord
from vonk_control.fleet_profile_contract.applications import (
    FleetProfileIntendedConfiguration,
)
from vonk_control.fleet_profile_contract.assessment import (
    FleetProfileAdmissionDecision,
    FleetProfileAssignmentAssessment,
    FleetProfileAssignmentPreparation,
    FleetProfileAssignmentPreview,
    FleetProfilePlanStep,
    FleetProfilePlanSummary,
    FleetProfilePreparationDecision,
    FleetProfileReason,
    FleetProfileScopePreview,
)
from vonk_control.fleet_profile_contract.definitions import (
    FleetProfileAssignment,
    FleetProfileAssignmentInput,
    FleetProfileDefinition,
)
from vonk_control.fleet_profile_contract.review import (
    FleetProfilePreview,
    FleetProfileReviewedDecision,
)
from vonk_control.fleet_profile_contract.vocabulary import FleetProfileAction
from vonk_control.fleet_profiles.assessment_support import (
    _assignments_needing_preparation,
)
from vonk_control.fleet_profiles.capacity import plan_kept_runs, sibling_runs
from vonk_control.fleet_profiles.colocation import colocated_memory_overcommit
from vonk_control.fleet_profiles.contracts import (
    FleetProfileInvalid,
    planner_contract_defect,
)
from vonk_control.fleet_profiles.control_effects import _control_effects
from vonk_control.fleet_profiles.dependencies import (
    _DISK_REFUSALS,
    _PREPARATION_RESOLVABLE_CODES,
)
from vonk_control.fleet_profiles.persistence import (
    _persisted_profile_plan,
    _reviewed_profile_plan,
    _stored_recipe,
)
from vonk_control.fleet_profiles.projection_support import (
    _aware,
    _choice_id,
    _digest,
    _profile_document,
    _review_effects_digest,
)
from vonk_control.fleet_profiles.read_observation import profile_read
from vonk_control.gateway.alias_namespace import require_alias_not_external
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.fleet import AgentNode, FleetProfile, FleetProfileApplication
from vonk_control.preparation_contract import RolloutPreparation, RuntimeImageIdentity
from vonk_control.recipe_operations.runtime_specs import recipe_topology
from vonk_control.run_switch_contract.plans import RunSwitchAssessment

if TYPE_CHECKING:
    from vonk_control.fleet_profiles.service import FleetProfileService
    from vonk_control.fleet_profiles.service import (
        FleetProfileService as _FleetProfileService,
    )


class FleetProfileService:
    def preview_draft(
        self, number: int, definition: FleetProfileDefinition
    ) -> FleetProfilePreview:
        """Review unsaved content as the profile a save would make; writes nothing.

        A number with no row gets a provisional id that names no stored profile.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions() as session:
            stored = session.scalar(
                select(FleetProfile.id).where(FleetProfile.number == number)
            )
        return self.preview(
            stored
            or str(uuid5(NAMESPACE_URL, f"vonk-forge:fleet-profile-draft:{number}")),
            draft_definition=definition,
        )

    @profile_read
    def preview(
        self,
        profile_id: str,
        *,
        execution_assignments: tuple[FleetProfileAssignment, ...] | None = None,
        profile_name: str | None = None,
        profile_digest: str | None = None,
        accepted_intent: FleetProfileIntendedConfiguration | None = None,
        accepted_profile_revision: int | None = None,
        accepted_profile_definition: FleetProfileDefinition | None = None,
        draft_definition: FleetProfileDefinition | None = None,
        allow_pending_cache_rebuild: bool = False,
        profile_application_id: str | None = None,
        excluded_application_id: str | None = None,
    ) -> FleetProfilePreview:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        now = _aware(self._clock())
        with self._sessions() as session:
            recovery_images: dict[str, RuntimeImageIdentity] = {}
            if profile_application_id is not None:
                recovery = session.get(FleetProfileApplication, profile_application_id)
                if recovery is None or recovery.profile_id != profile_id:
                    raise FleetProfileInvalid(
                        "Recovery review owner is unavailable",
                        reason=InvalidRequestReason.NOT_FOUND,
                    )
                reviewed = _reviewed_profile_plan(recovery, session=session)
                # An accepted plan that cannot be read names no exact images to
                # hold the review to: it is planned against what is observed now.
                recovery_images = (
                    {}
                    if isinstance(reviewed, Residue)
                    else {
                        item.assignment_id: item.runtime_image
                        for item in reviewed.preparation_decisions
                    }
                )
            row = session.get(FleetProfile, profile_id)
            choices: tuple[FleetProfileAssignmentInput, ...] = ()
            resolved_assignments: tuple[FleetProfileAssignment, ...]
            if accepted_intent is not None:
                # The accepted intent is the evidence of its own snapshot: whatever
                # the caller could not name (the assignments, the revision, the
                # name of a profile that was since deleted) is read from it, and
                # its own digest is the identity (never the caller's).
                resolved_assignments = (
                    execution_assignments
                    if execution_assignments is not None
                    else tuple(accepted_intent.assignments)
                )
                resolved_name = profile_name or (
                    row.name if row is not None else "Direct placement"
                )
                resolved_digest = accepted_intent.profile_digest
                installation_policy = accepted_intent.installation_policy
                # The accepted revision, never the saved row's: adopting the
                # row's would hide a save made since acceptance.
                resolved_revision = accepted_profile_revision
                resolved_definition = accepted_profile_definition
            elif draft_definition is not None:
                # An unsaved draft is previewed as its own content: the same
                # normalisation a save applies, then the one resolver. It names
                # no stored revision and writes nothing.
                readable = self._readable_definition(row) if row is not None else None
                stored = (
                    readable if isinstance(readable, FleetProfileDefinition) else None
                )
                if stored is not None and stored == draft_definition:
                    # A save of equal content replays without normalising, so
                    # the review is of the stored definition as it is.
                    reviewed = stored
                else:
                    canonical, _notes = self._validated_assignments(
                        session,
                        draft_definition.assignments,
                        retained_selectors=frozenset(
                            item.recipe_selector for item in stored.assignments
                        )
                        if stored is not None
                        else frozenset(),
                    )
                    reviewed = draft_definition.model_copy(
                        update={"assignments": canonical}
                    )
                resolved_definition = reviewed
                choices = tuple(reviewed.assignments)
                resolved_assignments = self._execution_assignments(session, choices)
                resolved_name = draft_definition.name
                resolved_digest = _digest(reviewed)
                installation_policy = draft_definition.installation_policy
                resolved_revision = None
            elif row is None:
                if execution_assignments is None:
                    raise MissingRecord(
                        profile_id, reason=InvalidRequestReason.NOT_FOUND
                    )
                resolved_assignments = execution_assignments
                resolved_name = profile_name or "Direct placement"
                resolved_digest = profile_digest or _digest(
                    {
                        "profile_id": profile_id,
                        "assignments": [
                            item.model_dump(mode="json")
                            for item in resolved_assignments
                        ],
                    }
                )
                installation_policy = ProfileInstallationPolicy.KEEP_CACHED.value
                resolved_revision = None
                resolved_definition = None
            else:
                resolved_definition = self._required_definition(row)
                choices = self._choices(row)
                resolved_assignments = self._execution_assignments(session, choices)
                resolved_name = row.name
                resolved_digest = _digest(_profile_document(row))
                installation_policy = row.installation_policy
                resolved_revision = row.revision
            assignment_previews: list[FleetProfileAssignmentPreview] = []
            assignment_preparations: list[FleetProfileAssignmentPreparation] = []
            assignment_assessments: list[FleetProfileAssignmentAssessment] = []
            reasons: list[FleetProfileReason] = []
            if accepted_intent is None:
                resolved_ids = {item.id for item in resolved_assignments}
                for choice in choices:
                    if _choice_id(choice) in resolved_ids:
                        continue
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.RECIPE_UNAVAILABLE,
                            detail=self._recipe_unavailable(session, choice),
                            severity=ProfileReasonSeverity.ERROR.value,
                        )
                    )
            roster = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                )
            )
            enrolled_node_ids = set(session.scalars(select(AgentNode.node_id)))
            switch_steps: list[FleetProfilePlanStep] = []
            preparation_unavailable_reported = False
            # Scope is the authoritative reconciliation boundary.  An idle
            # member has no assignment and must still participate in the plan.
            target_nodes = {node.node_id for node in roster}

            control = _control_effects(
                session,
                resolved_assignments,
                target_nodes,
                installation_policy,
                expected_images=recovery_images,
                excluded_application_id=excluded_application_id,
            )
            adapter_switch_needed = control.switch_needed
            changed_nodes = control.changed_nodes
            run_effects = control.effects.runs
            kept_by_plan = plan_kept_runs(control.effects)
            installation_effects = control.effects.installations
            reasons.extend(control.reasons)
            adopted_by_assignment = {
                identifier: session.get(FleetProfileApplication, effect.application_id)
                for effect in control.effects.adopted
                for identifier in effect.assignment_ids
            }
            # Ports this review already gives earlier starting assignments, so
            # two assignments sharing a Spark are promised distinct ports.
            planned_ports: frozenset[tuple[str, int]] = frozenset()
            for assignment in resolved_assignments:
                state = control.states[assignment.id]
                preparation = None
                expected_nodes = tuple(
                    sorted(node.node_id for node in assignment.nodes)
                )
                item_reasons: list[FleetProfileReason] = []
                # Exact preparation evidence is required when this assignment
                # must place the model and runtime image, or start a run: the
                # start is held to the image this review accepts. A profile that
                # already matches live state legitimately has nothing to
                # prepare, so unavailable evidence stays a warning there rather
                # than blocking an otherwise idle plan.
                requires_preparation = not state.installation_ready or (
                    assignment.desired_state == DesiredAssignmentState.RUNNING
                    and state.current_state != ObservedAssignmentState.RUNNING
                )
                active_node_ids = {node.node_id for node in roster}
                unavailable_nodes = sorted(set(expected_nodes) - active_node_ids)
                known_node_ids = set(unavailable_nodes) & enrolled_node_ids
                unknown_nodes = sorted(set(unavailable_nodes) - known_node_ids)
                removed_nodes = sorted(known_node_ids)
                revision = session.get(
                    CatalogDocumentRevision, assignment.recipe_revision_id
                )
                definition = _stored_recipe(revision) if revision else None
                required_count = (
                    recipe_topology(definition).node_count
                    if isinstance(definition, RecipeDefinition)
                    else None
                )
                if unknown_nodes:
                    item_reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.SPARK_UNAVAILABLE,
                            detail=(
                                "Assignment references Spark IDs that are not enrolled: "
                                + ", ".join(unknown_nodes)
                            ),
                            severity=ProfileReasonSeverity.ERROR.value,
                        )
                    )
                if removed_nodes:
                    item_reasons.append(
                        FleetProfileReason(
                            code=(
                                ProfileReasonCode.INCOMPLETE_MULTI_SPARK_MODEL
                                if required_count is not None and required_count > 1
                                else ProfileReasonCode.SPARK_REMOVED
                            ),
                            detail=(
                                (
                                    "Multi-Spark model cannot run because the profile "
                                    "no longer has a Spark for every rank; reachable "
                                    "ranks will be stopped. Missing Sparks: "
                                )
                                if required_count is not None and required_count > 1
                                else "Assignment includes a Spark outside the live fleet and will be ignored: "
                            )
                            + ", ".join(removed_nodes),
                            severity=ProfileReasonSeverity.WARNING.value,
                        )
                    )
                if required_count is None or len(assignment.nodes) != required_count:
                    item_reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.TOPOLOGY_INCOMPLETE,
                            detail=(
                                f"Recipe requires {required_count or 'a valid'} Sparks; "
                                f"the draft assigns {len(assignment.nodes)}."
                            ),
                            severity=ProfileReasonSeverity.ERROR.value,
                        )
                    )
                if assignment.id in adopted_by_assignment:
                    owner = adopted_by_assignment[assignment.id]
                    assert owner is not None
                    owner_plan = _persisted_profile_plan(owner)
                    assert not isinstance(owner_plan, Residue)
                    assignment_assessments.extend(
                        item
                        for item in owner_plan.assessments
                        if item.assignment_id == assignment.id
                    )
                    preparation = next(
                        (
                            item.preparation
                            for item in owner_plan.preparations
                            if item.assignment_id == assignment.id
                        ),
                        None,
                    )
                elif unavailable_nodes:
                    # A profile still covers the current fleet.  An assignment
                    # that refers to a removed Spark is deliberately not
                    # relaunched or allowed to block the other assignments.
                    # IDs that were never enrolled remain errors above.
                    # Any reachable rank cleanup was already reviewed in the
                    # profile effects above.
                    preparation = None
                elif self._assessment_provider is None:
                    if not preparation_unavailable_reported:
                        reasons.append(
                            FleetProfileReason(
                                code=ProfileReasonCode.PREPARATION_UNAVAILABLE,
                                detail=(
                                    "Exact model and OCI preparation evidence is "
                                    f"unavailable for {assignment.recipe_title}; "
                                    "the Controller has no preparation provider "
                                    "configured."
                                ),
                                severity=ProfileReasonSeverity.WARNING.value,
                            )
                        )
                        preparation_unavailable_reported = True
                else:
                    try:
                        assessment = self._assessment_provider(
                            session,
                            assignment,
                            expected_nodes,
                            allow_pending_cache_rebuild=allow_pending_cache_rebuild,
                            expected_runtime_image=recovery_images.get(assignment.id),
                            excluded_profile_application_ids=tuple(
                                item.id
                                for item in control.effects.superseded
                                if item.kind == ProfileProjectionKind.APPLICATION.value
                            ),
                            planned_ports=planned_ports,
                            kept_run_ids=kept_by_plan
                            | sibling_runs(
                                resolved_assignments,
                                assignment,
                                lambda item: control.states[item.id].run,
                            ),
                            own_run_id=state.run.id if state.run else None,
                            replaced_run_ids=frozenset(
                                effect.run_id
                                for effect in run_effects
                                if effect.action == ProfileChildPhase.STOP.value
                                and effect.profile_stop_scope is None
                            ),
                        )
                        if not isinstance(assessment, RunSwitchAssessment):
                            raise BookkeepingUnknown(
                                assessment.note
                                if isinstance(assessment, Residue)  # pyright: ignore[reportUnnecessaryIsInstance]  # an unexpected value is treated as unknown, not trusted
                                else "The planner returned an invalid assessment.",
                                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                            )
                        observed_fit_nodes = tuple(
                            sorted(
                                node.node_id for node in assessment.fit_current.nodes
                            )
                        )
                        observed_after_nodes = (
                            tuple(
                                sorted(
                                    node.node_id
                                    for node in assessment.fit_after_stop.nodes
                                )
                            )
                            if assessment.fit_after_stop is not None
                            else expected_nodes
                        )
                        if (
                            observed_fit_nodes != expected_nodes
                            or observed_after_nodes != expected_nodes
                        ):
                            raise BookkeepingUnknown(
                                "The planner assessment does not cover the exact assignment scope.",
                                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                            )
                        assignment_assessments.append(
                            FleetProfileAssignmentAssessment(
                                assignment_id=assignment.id, assessment=assessment
                            )
                        )
                        if (
                            assignment.desired_state == DesiredAssignmentState.RUNNING
                            and state.current_state != ObservedAssignmentState.RUNNING
                        ):
                            fit = assessment.fit_after_stop or assessment.fit_current
                            planned_ports |= {
                                (node.node_id, port)
                                for node in fit.nodes
                                for port in node.ports_required
                            }
                        preparation = assessment.preparation
                        if (
                            preparation is None
                            and assessment.allowed
                            and allow_pending_cache_rebuild
                        ):
                            reasons.append(
                                FleetProfileReason(
                                    code=ProfileReasonCode.RUNTIME_IMAGE_REBUILD_PENDING,
                                    detail="The accepted runtime image needs cache repair before distribution.",
                                    severity=ProfileReasonSeverity.WARNING.value,
                                )
                            )
                        elif preparation is None and requires_preparation:
                            reasons.append(
                                FleetProfileReason(
                                    code=ProfileReasonCode.PREPARATION_UNAVAILABLE,
                                    detail=next(
                                        (
                                            blocker.detail
                                            for blocker in assessment.blockers
                                        ),
                                        "The exact model and runtime image are not in the Controller cache yet; the Controller prepares them and continues the load when they are ready.",
                                    ),
                                    severity=ProfileReasonSeverity.ERROR.value,
                                )
                            )
                    except ValidationError as error:
                        reasons.append(
                            FleetProfileReason(
                                code=ProfileReasonCode.ADMISSION_DEFECT,
                                detail=str(
                                    planner_contract_defect(assignment.id, error)
                                ),
                                severity=ProfileReasonSeverity.ERROR.value,
                            )
                        )
                    except (
                        BookkeepingUnknown,
                        KeyError,
                        RuntimeError,
                        TypeError,
                        ValueError,
                    ) as error:
                        reasons.append(
                            FleetProfileReason(
                                code=ProfileReasonCode.PREPARATION_UNAVAILABLE,
                                detail=str(error)[:512]
                                or "The preparation provider returned no exact evidence.",
                                severity=(
                                    ProfileReasonSeverity.ERROR.value
                                    if requires_preparation
                                    else ProfileReasonSeverity.WARNING.value
                                ),
                            )
                        )
                    if preparation is not None and not isinstance(  # pyright: ignore[reportUnnecessaryIsInstance]  # a typed value is re-checked where it is consumed
                        preparation, RolloutPreparation
                    ):
                        reasons.append(
                            FleetProfileReason(
                                code=ProfileReasonCode.PREPARATION_UNAVAILABLE,
                                detail="The preparation provider returned an invalid contract.",
                                severity=(
                                    ProfileReasonSeverity.ERROR.value
                                    if requires_preparation
                                    else ProfileReasonSeverity.WARNING.value
                                ),
                            )
                        )
                        preparation = None
                    if preparation is not None:
                        observed_target_ids = tuple(preparation.target_node_ids)
                        observed_model_targets = tuple(
                            sorted(
                                target.node_id for target in preparation.model.targets
                            )
                        )
                        observed_image_targets = tuple(
                            sorted(
                                target.node_id
                                for target in preparation.runtime_image.targets
                            )
                        )
                        if (
                            observed_target_ids != expected_nodes
                            or observed_model_targets != expected_nodes
                            or observed_image_targets != expected_nodes
                        ):
                            reasons.append(
                                FleetProfileReason(
                                    code=ProfileReasonCode.PREPARATION_SCOPE_MISMATCH,
                                    detail=(
                                        "Preparation evidence does not cover exactly "
                                        f"the assignment target scope ({', '.join(expected_nodes)})."
                                    ),
                                    severity=ProfileReasonSeverity.ERROR.value,
                                )
                            )
                            preparation = None
                if preparation is not None:
                    assignment_preparations.append(
                        FleetProfileAssignmentPreparation(
                            assignment_id=assignment.id,
                            preparation=preparation,
                        )
                    )
                actions: list[FleetProfileAction] = []
                if unknown_nodes:
                    actions = []
                elif assignment.id in adopted_by_assignment:
                    actions.append(ProfileAction.ADOPT.value)
                elif state.current_state == assignment.desired_state:
                    actions.append(ProfileAction.KEEP.value)
                else:
                    actions.append(ProfileAction.SWITCH.value)
                assignment_previews.append(
                    FleetProfileAssignmentPreview(
                        assignment_id=assignment.id,
                        recipe_revision_id=assignment.recipe_revision_id,
                        recipe_title=assignment.recipe_title,
                        desired_state=assignment.desired_state,
                        current_state=state.current_state,
                        node_ids=[node.node_id for node in assignment.nodes],
                        option_choices=dict(assignment.option_choices),
                        actions=actions,
                        reasons=item_reasons,
                    )
                )
                reasons.extend(item_reasons)

            if adapter_switch_needed:
                if not changed_nodes or not changed_nodes <= target_nodes:
                    if not any(
                        reason.severity == ProfileReasonSeverity.ERROR.value
                        for reason in reasons
                    ):
                        # The effect scope observed now cannot be pinned to the
                        # roster (it changed while planning): the plan is not
                        # admissible yet, and it is planned again.
                        reasons.append(
                            FleetProfileReason(
                                code=ProfileReasonCode.SWITCH_SCOPE_UNRESOLVED,
                                detail=(
                                    "The Spark scope of the profile switch changed "
                                    "while it was planned; it is planned again."
                                ),
                                severity=ProfileReasonSeverity.ERROR.value,
                            )
                        )
                else:
                    switch_steps.append(
                        FleetProfilePlanStep(
                            index=len(switch_steps),
                            kind=ProfileAction.SWITCH.value,
                            node_ids=sorted(changed_nodes),
                            label=f"Switch profile {resolved_name}",
                        )
                    )
                if self._switch_adapter is None:
                    reasons.append(
                        FleetProfileReason(
                            code=ProfileReasonCode.SWITCH_AUTHORITY_UNAVAILABLE,
                            detail="Run/Switch authority is required to apply this profile.",
                            severity=ProfileReasonSeverity.ERROR.value,
                        )
                    )
            steps = switch_steps
            # Each start was fitted alone; the ones sharing a Spark must also
            # fit beside each other, or the last one can never start.
            reasons.extend(
                colocated_memory_overcommit(assignment_previews, assignment_assessments)
            )
            # The planner owns named admission blockers, including preparation
            # failures. Its canonical assessment rejects hidden preparation
            # blockers, so count each reason once and render that same owner.
            blocker_count = sum(
                reason.severity == ProfileReasonSeverity.ERROR.value
                for reason in reasons
            ) + sum(len(item.assessment.blockers) for item in assignment_assessments)
            summary = FleetProfilePlanSummary(
                already_correct=sum(
                    item.actions == [ProfileAction.KEEP.value]
                    for item in assignment_previews
                ),
                placements=0,
                builds=0,
                distributions=0,
                installs=sum(
                    not state.installation_ready
                    for identifier, state in control.states.items()
                    if identifier not in adopted_by_assignment
                ),
                starts=sum(
                    item.desired_state == DesiredAssignmentState.RUNNING
                    and ProfileAction.SWITCH.value in item.actions
                    for item in assignment_previews
                ),
                stops=sum(
                    effect.action == ProfileChildPhase.STOP.value
                    for effect in run_effects
                ),
                uninstalls=sum(
                    effect.action == "remove" for effect in installation_effects
                ),
                blockers=blocker_count,
            )
            effects = control.effects
            preparation_steps: list[FleetProfilePlanStep] = []
            if self._preparation_starter is not None:
                for assignment_preview in _assignments_needing_preparation(
                    assignment_previews,
                    {item.assignment_id for item in assignment_preparations},
                    reasons,
                    assignment_assessments,
                ):
                    title = assignment_preview.recipe_title
                    for label in (
                        f"Download the model files for {title} (skipped when cached)",
                        f"Build the runtime image for {title} (skipped when built)",
                    ):
                        preparation_steps.append(
                            FleetProfilePlanStep(
                                index=len(preparation_steps),
                                kind=ProfileChildPhase.PREPARE.value,
                                node_ids=list(assignment_preview.node_ids),
                                label=label,
                            )
                        )
            blocking_codes = {
                reason.code
                for reason in reasons
                if reason.severity == ProfileReasonSeverity.ERROR.value
            } | {
                reason.code
                for item in assignment_assessments
                for reason in item.assessment.blockers
            }
            # Disk preparation starts only after acceptance: a read-only review
            # cannot publish an eviction demand. Expose the same waitable path
            # even when the model and image are already cached.
            waits_for_preparation = bool(
                blocker_count > 0
                and (preparation_steps or blocking_codes & _DISK_REFUSALS)
                and blocking_codes <= (_PREPARATION_RESOLVABLE_CODES | _DISK_REFUSALS)
            )
            ordered_preparations = sorted(
                assignment_preparations, key=lambda item: item.assignment_id
            )
            ordered_assessments = sorted(
                assignment_assessments, key=lambda item: item.assignment_id
            )
            # A name an external model serves is shown as refused, not previewed.
            require_alias_not_external(
                session,
                {item.alias for item in resolved_assignments if item.alias},
            )
            decision = FleetProfileReviewedDecision(
                profile_id=profile_id,
                profile_name=resolved_name,
                profile_digest=resolved_digest,
                profile_revision=resolved_revision,
                profile_definition=resolved_definition,
                allowed=blocker_count == 0,
                scope=FleetProfileScopePreview(
                    node_ids=sorted(target_nodes),
                    idle_node_ids=sorted(
                        target_nodes
                        - {
                            node.node_id
                            for assignment in resolved_assignments
                            for node in assignment.nodes
                        }
                    ),
                ),
                summary=summary,
                assignments=assignment_previews,
                resolved_assignments=sorted(
                    resolved_assignments, key=lambda item: item.id
                ),
                admission_decisions=[
                    FleetProfileAdmissionDecision.from_assessment(item)
                    for item in ordered_assessments
                ],
                preparation_decisions=[
                    FleetProfilePreparationDecision.from_preparation(item)
                    for item in ordered_preparations
                ],
                effects=effects,
                steps=steps,
                preparation_steps=preparation_steps,
                waits_for_preparation=waits_for_preparation,
                reasons=reasons,
            )
            return FleetProfilePreview(
                **{
                    name: getattr(decision, name)
                    for name in type(decision).model_fields
                },
                generated_at=now,
                assessments=ordered_assessments,
                preparations=ordered_preparations,
                plan_digest=_digest(decision),
                effects_digest=_review_effects_digest(decision),
            )
