"""PostgreSQL authority for saved Fleet profiles and live-versus-desired plans."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import String, cast, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileAssignment,
    FleetProfileAssignmentPreparation,
    FleetProfileAssignmentInput,
    FleetProfileAssignmentPreview,
    FleetProfileChildOperation,
    FleetProfileSwitchAdapter,
    FleetProfileInput,
    FleetProfileList,
    FleetProfileNode,
    FleetProfilePlanStep,
    FleetProfilePlanSummary,
    FleetProfilePreview,
    FleetProfileReason,
    FleetProfileScope,
    FleetProfileScopePreview,
    FleetProfileStatusView,
    FleetProfileView,
)
from .models import (
    AgentNode,
    CatalogEntity,
    CatalogEntityRevision,
    ClusterMapping,
    ClusterMappingNode,
    FleetProfile,
    FleetProfileApplication,
    InstallationNode,
    LocalRecipe,
    LocalRecipeRevision,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .preparation_contract import RolloutPreparation
from .recipe_contract import RecipeContractError, recipe_topology
from .recipe_operations import RecipeOperationConflict, RecipeOperationService

_ACTIVE_RUN_STATES = frozenset({"planned", "starting", "running", "stopping"})
_ACTIVE_INSTALL_STATES = frozenset(
    {"planned", "installing", "installed", "partial", "failed"}
)
_CHILD_PENDING_STATES = frozenset(
    {"queued", "pending", "leased", "running", "starting", "stopping", "installing"}
)
_CHILD_FAILED_STATES = frozenset(
    {"failed", "expired", "cancelled", "waiting-for-operator"}
)
_INTERNAL_PLACEMENT_PREFIX = "~placement-"
_INTERNAL_PLACEMENT_LABEL = "vonk.internal.placement"


class FleetProfileConflict(RuntimeError):
    """A Fleet profile is invalid, stale, or cannot be safely applied."""


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


def _assignment_id(value: FleetProfileAssignmentInput) -> str:
    identity = ":".join(
        (
            value.recipe_revision_id,
            value.topology_name,
            value.desired_state,
            value.alias or "",
            *(
                f"{node.rank}:{node.node_id}:{node.role}:{int(node.endpoint_owner)}"
                for node in value.nodes
            ),
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-forge:fleet-profile:{identity}"))


def _expanded_roles(topology: Mapping[str, object]) -> tuple[tuple[str, bool], ...]:
    raw_roles = topology.get("roles")
    if not isinstance(raw_roles, Sequence) or isinstance(raw_roles, (str, bytes)):
        raise FleetProfileConflict("recipe topology roles are unavailable")
    expanded: list[tuple[str, bool]] = []
    for raw_role in raw_roles:
        if not isinstance(raw_role, Mapping):
            raise FleetProfileConflict("recipe topology role is invalid")
        name = raw_role.get("name")
        count = raw_role.get("count")
        endpoint_owner = raw_role.get("endpoint_owner", False)
        if (
            not isinstance(name, str)
            or not name
            or type(count) is not int
            or not 1 <= count <= 32
            or not isinstance(endpoint_owner, bool)
        ):
            raise FleetProfileConflict("recipe topology role is invalid")
        expanded.extend((name, endpoint_owner) for _ in range(count))
    return tuple(expanded)


def _profile_document(row: FleetProfile) -> dict[str, object]:
    return {
        "schema_version": 2,
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "installation_policy": row.installation_policy,
        "labels": dict(row.labels),
        "favorite": row.favorite,
        "scope": list(row.scope),
        "assignments": list(row.assignments),
    }


class FleetProfileService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        recipe_operations: RecipeOperationService | None = None,
        switch_adapter: FleetProfileSwitchAdapter | None = None,
        preparation_provider: Callable[[
            Session, FleetProfileAssignment, tuple[str, ...]
        ], RolloutPreparation | None] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._recipe_operations = recipe_operations
        self._switch_adapter = switch_adapter
        self._preparation_provider = preparation_provider

    def list(self) -> FleetProfileList:
        now = _aware(self._clock())
        with self._sessions() as session:
            rows = tuple(
                session.scalars(
                    select(FleetProfile)
                    .where(FleetProfile.name.not_like(f"{_INTERNAL_PLACEMENT_PREFIX}%"))
                    .order_by(
                        FleetProfile.favorite.desc(), FleetProfile.name, FleetProfile.id
                    )
                    .limit(128)
                )
            )
            return FleetProfileList(
                generated_at=now, profiles=[self._view(session, row) for row in rows]
            )

    def get(self, profile_id: str) -> FleetProfileView:
        with self._sessions() as session:
            row = session.get(FleetProfile, profile_id)
            if row is None:
                raise KeyError(profile_id)
            return self._view(session, row)

    def duplicate(
        self,
        profile_id: str,
        *,
        name: str,
        description: str | None = None,
        actor: str,
    ) -> FleetProfileView:
        source = self.get(profile_id)
        value = FleetProfileInput(
            name=name,
            description=source.description if description is None else description,
            installation_policy=source.installation_policy,
            labels=dict(source.labels),
            favorite=False,
            scope=source.scope,
            assignments=[
                FleetProfileAssignmentInput.model_validate(
                    item.model_dump(
                        mode="json",
                        exclude={
                            "id",
                            "recipe_id",
                            "recipe_title",
                            "model_title",
                        },
                    )
                )
                for item in source.assignments
            ],
        )
        return self.create(value, actor=actor)

    def capture_current(
        self,
        *,
        name: str,
        description: str = "Captured current Fleet setup",
        installation_policy: str = "keep-cached",
        labels: Mapping[str, str] | None = None,
        favorite: bool = False,
        actor: str,
    ) -> FleetProfileView:
        """Capture the controller's current setup as an immutable profile."""

        with self._sessions() as session:
            scope = sorted(
                node.node_id
                for node in session.scalars(
                    select(AgentNode).where(AgentNode.revoked_at.is_(None))
                )
            )
            active_installations = tuple(
                session.scalars(
                    select(RecipeInstallation)
                    .where(RecipeInstallation.state.in_(_ACTIVE_INSTALL_STATES))
                    .order_by(RecipeInstallation.created_at, RecipeInstallation.id)
                )
            )
            runs = {
                run.installation_id: run
                for run in session.scalars(
                    select(RecipeRun).where(RecipeRun.state.in_(_ACTIVE_RUN_STATES))
                )
            }
            assignments: list[FleetProfileAssignmentInput] = []
            for installation in active_installations:
                mapping = session.get(ClusterMapping, installation.mapping_id)
                if mapping is None:
                    continue
                members = tuple(
                    session.scalars(
                        select(ClusterMappingNode)
                        .where(ClusterMappingNode.mapping_id == mapping.id)
                        .order_by(ClusterMappingNode.rank)
                    )
                )
                if not members:
                    continue
                run = runs.get(installation.id)
                assignments.append(
                    FleetProfileAssignmentInput(
                        recipe_revision_id=installation.recipe_revision_id,
                        topology_name=mapping.topology_name,
                        desired_state="running" if run is not None else "installed",
                        alias=run.alias if run is not None else None,
                        nodes=[
                            FleetProfileNode(
                                node_id=member.node_id,
                                rank=member.rank,
                                role=member.role,
                                endpoint_owner=member.endpoint_owner,
                            )
                            for member in members
                        ],
                    )
                )
        value = FleetProfileInput(
            name=name,
            description=description,
            installation_policy=installation_policy,
            labels=dict(labels or {}),
            favorite=favorite,
            scope=FleetProfileScope(node_ids=scope),
            assignments=assignments,
        )
        return self.create(value, actor=actor)

    def status(self, profile_id: str) -> FleetProfileStatusView:
        preview = self.preview(profile_id)
        with self._sessions() as session:
            profile = session.get(FleetProfile, profile_id)
            if profile is None:
                raise KeyError(profile_id)
            active = session.scalar(
                select(FleetProfileApplication.id)
                .where(
                    FleetProfileApplication.profile_id == profile_id,
                    FleetProfileApplication.state.in_(
                        ("queued", "running", "waiting-for-operator")
                    ),
                )
                .limit(1)
            )
            latest = session.scalar(
                select(FleetProfileApplication)
                .where(FleetProfileApplication.profile_id == profile_id)
                .order_by(
                    FleetProfileApplication.created_at.desc(),
                    FleetProfileApplication.id.desc(),
                )
                .limit(1)
            )
        blocked = not preview.allowed
        switching = active is not None
        matched = preview.allowed and not preview.steps and not switching
        partially_applied = bool(
            latest is not None
            and latest.state in {"failed", "waiting-for-operator"}
            and latest.current_step > 0
        )
        needs_preparation = bool(
            preview.steps
            and any(
                step.kind
                in {"create-placement", "build", "distribute-image", "install"}
                for step in preview.steps
            )
        )
        ready = bool(preview.steps) and not any(
            step.kind
            in {"create-placement", "build", "distribute-image", "install"}
            for step in preview.steps
        )
        state = (
            "blocked"
            if blocked
            else "switching"
            if switching
            else "partially-applied"
            if partially_applied
            else "matched"
            if matched
            else "needs-preparation"
            if needs_preparation
            else "ready"
            if ready
            else "drifted"
        )
        return FleetProfileStatusView(
            profile_id=preview.profile_id,
            profile_digest=preview.profile_digest,
            state=state,
            matched=matched,
            drifted=not matched and not blocked,
            scope=preview.scope,
            reasons=preview.reasons,
            generated_at=preview.generated_at,
        )

    def prepare_preview(self, profile_id: str) -> FleetProfilePreview:
        """Return the reviewed, preparation-only plan for a profile."""

        preview = self.preview(profile_id)
        preparation_kinds = {
            "create-placement",
            "build",
            "distribute-image",
            "install",
        }
        steps = [
            step for step in preview.steps if step.kind in preparation_kinds
        ]
        steps = [
            step.model_copy(update={"index": index})
            for index, step in enumerate(steps)
        ]
        summary = FleetProfilePlanSummary(
            already_correct=preview.summary.already_correct,
            placements=sum(step.kind == "create-placement" for step in steps),
            builds=sum(step.kind == "build" for step in steps),
            distributions=sum(step.kind == "distribute-image" for step in steps),
            installs=sum(step.kind == "install" for step in steps),
            starts=0,
            stops=0,
            uninstalls=0,
            blockers=preview.summary.blockers,
        )
        plan_identity = {
            "schema_version": 2,
            "mode": "prepare",
            "profile_id": preview.profile_id,
            "profile_digest": preview.profile_digest,
            "scope": preview.scope.model_dump(mode="json"),
            "assignments": [
                item.model_dump(mode="json") for item in preview.assignments
            ],
            "preparations": [
                {
                    "assignment_id": item.assignment_id,
                    "preparation": self._preparation_identity(item.preparation),
                }
                for item in preview.preparations
            ],
            "reasons": [reason.model_dump(mode="json") for reason in preview.reasons],
            "steps": [step.model_dump(mode="json") for step in steps],
        }
        return preview.model_copy(
            update={
                "steps": steps,
                "summary": summary,
                "plan_digest": _digest(plan_identity),
            }
        )

    def prepare(
        self,
        profile_id: str,
        *,
        plan_digest: str,
        request_key: str,
        actor: str,
    ) -> FleetProfileApplicationView:
        """Queue preparation work while leaving the current setup intact."""

        preview = self.prepare_preview(profile_id)
        if not preview.allowed:
            raise FleetProfileConflict("Fleet profile preview is blocked")
        if preview.plan_digest != plan_digest:
            raise FleetProfileConflict("Fleet profile preparation preview is stale")
        return self._queue_application(
            preview,
            request_key=request_key,
            actor=actor,
            operation_kind="fleet-profile.prepare",
        )

    def switch(
        self, profile_id: str, *, plan_digest: str, request_key: str, actor: str
    ) -> FleetProfileApplicationView:
        return self.apply(
            profile_id,
            plan_digest=plan_digest,
            request_key=request_key,
            actor=actor,
        )

    def create(self, value: FleetProfileInput, *, actor: str) -> FleetProfileView:
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            scope = self._validated_scope(session, value)
            assignments = self._validated_assignments(session, value.assignments)
            row = FleetProfile(
                name=value.name,
                description=value.description,
                installation_policy=value.installation_policy,
                labels=dict(value.labels),
                favorite=value.favorite,
                scope=scope,
                assignments=assignments,
                created_by=actor,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as error:
                raise FleetProfileConflict(
                    "a Fleet profile with this name already exists"
                ) from error
            result = self._view(session, row)
        return result

    def ensure_internal_placement(
        self,
        profile_id: str,
        assignment: FleetProfileAssignmentInput,
        *,
        actor: str,
    ) -> FleetProfileView:
        """Materialize one hidden, deterministic profile for a direct placement.

        The profile coordinator remains the sole multi-step lifecycle engine.  A
        direct Library placement only supplies a one-assignment desired state and
        is deliberately omitted from the saved-profile collection.
        """

        now = _aware(self._clock())
        name = f"{_INTERNAL_PLACEMENT_PREFIX}{profile_id[:12]}"
        with self._sessions.begin() as session:
            scope = self._validated_scope(
                session,
                FleetProfileInput(
                    name="Direct placement",
                    scope=FleetProfileScope(
                        node_ids=sorted(node.node_id for node in assignment.nodes)
                    ),
                    assignments=[assignment],
                ),
            )
            assignments = self._validated_assignments(session, (assignment,))
            existing = session.get(FleetProfile, profile_id, with_for_update=True)
            if existing is None:
                existing = FleetProfile(
                    id=profile_id,
                    name=name,
                    description="Direct Library placement",
                    installation_policy="keep-cached",
                    labels={_INTERNAL_PLACEMENT_LABEL: "true"},
                    favorite=False,
                    scope=scope,
                    assignments=assignments,
                    created_by=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(existing)
                try:
                    session.flush()
                except IntegrityError as error:
                    raise FleetProfileConflict(
                        "direct placement identity conflicts with saved Fleet state"
                    ) from error
            elif (
                existing.name != name
                or existing.labels.get(_INTERNAL_PLACEMENT_LABEL) != "true"
                or existing.installation_policy != "keep-cached"
                or existing.scope != scope
                or existing.assignments != assignments
            ):
                raise FleetProfileConflict(
                    "direct placement identity conflicts with saved Fleet state"
                )
            return self._view(session, existing)

    def apply_internal_placement(
        self,
        profile_id: str,
        *,
        profile_plan_digest: str,
        placement_plan_digest: str,
        request_key: str,
        actor: str,
        metadata: Mapping[str, object],
    ) -> FleetProfileApplicationView:
        """Apply through the profile coordinator and bind direct-placement metadata."""

        result = self.apply(
            profile_id,
            plan_digest=profile_plan_digest,
            request_key=request_key,
            actor=actor,
        )
        with self._sessions.begin() as session:
            row = session.get(FleetProfileApplication, result.id, with_for_update=True)
            if row is None:
                raise FleetProfileConflict(
                    "direct placement application is unavailable"
                )
            progress = dict(row.progress)
            existing = progress.get("library_placement")
            placement = {
                **dict(metadata),
                "plan_digest": placement_plan_digest,
            }
            if existing is not None and existing != placement:
                raise FleetProfileConflict(
                    "direct placement request key was reused for another plan"
                )
            progress["library_placement"] = placement
            row.progress = progress
            row.updated_at = _aware(self._clock())
            session.flush()
            return self._application_view(row)

    def replay_internal_placement(
        self, request_key: str
    ) -> FleetProfileApplicationView | None:
        """Return an exact direct-placement replay before live state is re-previewed."""

        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if row is None:
                return None
            if not isinstance(row.progress.get("library_placement"), Mapping):
                raise FleetProfileConflict(
                    "direct placement request key was used by another operation"
                )
            return self._application_view(row)

    def update(
        self, profile_id: str, value: FleetProfileInput, *, actor: str
    ) -> FleetProfileView:
        del (
            actor
        )  # Updates retain the original creator and are audited at the API boundary.
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.get(FleetProfile, profile_id, with_for_update=True)
            if row is None:
                raise KeyError(profile_id)
            scope = self._validated_scope(session, value)
            assignments = self._validated_assignments(session, value.assignments)
            row.name = value.name
            row.description = value.description
            row.installation_policy = value.installation_policy
            row.labels = dict(value.labels)
            row.favorite = value.favorite
            row.scope = scope
            row.assignments = assignments
            row.updated_at = now
            try:
                session.flush()
            except IntegrityError as error:
                raise FleetProfileConflict(
                    "a Fleet profile with this name already exists"
                ) from error
            result = self._view(session, row)
        return result

    def delete(self, profile_id: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(FleetProfile, profile_id, with_for_update=True)
            if row is None:
                raise KeyError(profile_id)
            active = session.scalar(
                select(FleetProfileApplication.id)
                .where(
                    FleetProfileApplication.profile_id == profile_id,
                    FleetProfileApplication.state.in_(("queued", "running")),
                )
                .limit(1)
            )
            if active is not None:
                raise FleetProfileConflict("Fleet profile has an active application")
            session.execute(
                delete(FleetProfileApplication).where(
                    FleetProfileApplication.profile_id == profile_id
                )
            )
            session.delete(row)

    def preview(self, profile_id: str) -> FleetProfilePreview:
        now = _aware(self._clock())
        with self._sessions() as session:
            row = session.get(FleetProfile, profile_id)
            if row is None:
                raise KeyError(profile_id)
            view = self._view(session, row)
            assignment_previews: list[FleetProfileAssignmentPreview] = []
            assignment_preparations: list[FleetProfileAssignmentPreparation] = []
            reasons: list[FleetProfileReason] = []
            scope_rows = {
                node.node_id: node
                for node in session.scalars(
                    select(AgentNode).where(
                        AgentNode.node_id.in_(view.scope.node_ids)
                    )
                )
            }
            missing_scope_nodes = [
                node_id
                for node_id in view.scope.node_ids
                if node_id not in scope_rows
                or scope_rows[node_id].revoked_at is not None
            ]
            if missing_scope_nodes:
                reasons.append(
                    FleetProfileReason(
                        code="profile.scope_changed",
                        detail=(
                            "Profile scope contains a Spark that is no longer an "
                            "active enrolled Fleet member: "
                            + ", ".join(missing_scope_nodes)
                        ),
                        severity="error",
                    )
                )
            stop_steps: list[dict[str, object]] = []
            uninstall_steps: list[dict[str, object]] = []
            preparation_steps: list[dict[str, object]] = []
            start_steps: list[dict[str, object]] = []
            switch_steps: list[dict[str, object]] = []
            desired_installation_ids: set[str] = set()
            desired_run_ids: set[str] = set()
            managed_run_ids: set[str] = set()
            adapter_switch_needed = False
            preparation_unavailable_reported = False
            # Scope is the authoritative reconciliation boundary.  An idle
            # member has no assignment and must still participate in the plan.
            target_nodes = set(view.scope.node_ids)

            for assignment in view.assignments:
                state = self._assignment_state(session, assignment)
                preparation = None
                expected_nodes = tuple(sorted(node.node_id for node in assignment.nodes))
                if self._preparation_provider is None:
                    if not preparation_unavailable_reported:
                        reasons.append(
                            FleetProfileReason(
                                code="profile.preparation_unavailable",
                                detail=(
                                    "Exact model and OCI preparation evidence is "
                                    "unavailable from the configured Controller provider."
                                ),
                                severity="warning",
                            )
                        )
                        preparation_unavailable_reported = True
                else:
                    try:
                        preparation = self._preparation_provider(
                            session, assignment, expected_nodes
                        )
                    except (KeyError, RuntimeError, ValueError) as error:
                        reasons.append(
                            FleetProfileReason(
                                code="profile.preparation_unavailable",
                                detail=str(error)[:512]
                                or "The preparation provider returned no exact evidence.",
                                severity="warning",
                            )
                        )
                    if preparation is not None and not isinstance(
                        preparation, RolloutPreparation
                    ):
                        reasons.append(
                            FleetProfileReason(
                                code="profile.preparation_unavailable",
                                detail="The preparation provider returned an invalid contract.",
                                severity="warning",
                            )
                        )
                        preparation = None
                    if preparation is not None:
                        observed_target_ids = tuple(preparation.target_node_ids)
                        observed_model_targets = tuple(
                            sorted(target.node_id for target in preparation.model.targets)
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
                                    code="profile.preparation_scope_mismatch",
                                    detail=(
                                        "Preparation evidence does not cover exactly "
                                        f"the assignment target scope ({', '.join(expected_nodes)})."
                                    ),
                                    severity="error",
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
                if state.installation is not None:
                    desired_installation_ids.add(state.installation.id)
                if (
                    state.run is not None
                    and assignment.desired_state == "running"
                    and state.current_state == "running"
                ):
                    desired_run_ids.add(state.run.id)
                if self._switch_adapter is not None and state.run is not None:
                    managed_run_ids.add(state.run.id)
                actions: list[str] = []
                item_reasons: list[FleetProfileReason] = []
                if (
                    self._switch_adapter is None
                    and assignment.desired_state == "installed"
                    and state.run is not None
                ):
                    actions.append("stop")
                    stop_steps.append(
                        {
                            "kind": "stop",
                            "assignment_id": assignment.id,
                            "owner_id": state.run.id,
                            "recipe_revision_id": assignment.recipe_revision_id,
                            "node_ids": [node.node_id for node in assignment.nodes],
                            "label": f"Stop {assignment.recipe_title}",
                        }
                    )
                if self._switch_adapter is not None:
                    already_desired = state.current_state == assignment.desired_state
                    if already_desired:
                        if not actions:
                            actions.append("keep")
                    else:
                        actions.append("switch")
                        adapter_switch_needed = True
                else:
                    if state.current_state == assignment.desired_state or (
                        assignment.desired_state == "installed"
                        and state.current_state == "running"
                    ):
                        if not actions:
                            actions.append("keep")
                    else:
                        if state.mapping is None:
                            actions.append("create-placement")
                            preparation_steps.append(
                                {
                                    "kind": "create-placement",
                                    "assignment_id": assignment.id,
                                    "recipe_revision_id": assignment.recipe_revision_id,
                                    "node_ids": [node.node_id for node in assignment.nodes],
                                    "label": f"Place {assignment.recipe_title}",
                                }
                            )
                        if state.installation is None or state.current_state in {
                            "not-placed",
                            "placed",
                            "degraded",
                        }:
                            if state.build is None:
                                actions.append("build")
                                preparation_steps.append(
                                    {
                                        "kind": "build",
                                        "assignment_id": assignment.id,
                                        "recipe_revision_id": assignment.recipe_revision_id,
                                        "node_ids": [
                                            node.node_id for node in assignment.nodes
                                        ],
                                        "label": f"Build {assignment.recipe_title}",
                                    }
                                )
                            actions.extend(("distribute-image", "install"))
                            preparation_steps.extend(
                                (
                                    {
                                        "kind": "distribute-image",
                                        "assignment_id": assignment.id,
                                        "recipe_revision_id": assignment.recipe_revision_id,
                                        "node_ids": [
                                            node.node_id for node in assignment.nodes
                                        ],
                                        "label": f"Prepare {assignment.recipe_title} image",
                                    },
                                    {
                                        "kind": "install",
                                        "assignment_id": assignment.id,
                                        "recipe_revision_id": assignment.recipe_revision_id,
                                        "node_ids": [
                                            node.node_id for node in assignment.nodes
                                        ],
                                        "label": f"Install {assignment.recipe_title}",
                                    },
                                )
                            )
                        if (
                            assignment.desired_state == "running"
                            and state.current_state != "running"
                        ):
                            actions.append("start")
                            start_steps.append(
                                {
                                    "kind": "start",
                                    "assignment_id": assignment.id,
                                    "recipe_revision_id": assignment.recipe_revision_id,
                                    "node_ids": [node.node_id for node in assignment.nodes],
                                    "label": f"Run {assignment.recipe_title} as {assignment.alias}",
                                }
                            )
                assignment_previews.append(
                    FleetProfileAssignmentPreview(
                        assignment_id=assignment.id,
                        recipe_revision_id=assignment.recipe_revision_id,
                        recipe_title=assignment.recipe_title,
                        desired_state=assignment.desired_state,
                        current_state=state.current_state,
                        node_ids=[node.node_id for node in assignment.nodes],
                        actions=actions,
                        reasons=item_reasons,
                    )
                )
                reasons.extend(item_reasons)

            # Every active run intersecting scope is reconciled to the desired
            # running set, independent of installation retention policy.  A
            # distributed run that crosses the boundary is a hard blocker: the
            # controller must never stop only the in-scope ranks.
            active_runs = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(RecipeRun.state.in_(_ACTIVE_RUN_STATES))
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                )
            )
            run_nodes = self._run_nodes(session, [run.id for run in active_runs])
            scheduled_stops = {
                str(step["owner_id"])
                for step in stop_steps
                if isinstance(step.get("owner_id"), str)
            }
            for run in active_runs:
                members = set(run_nodes.get(run.id, ()))
                # Installation membership is the authoritative complete
                # placement even when a partial observation omitted a rank.
                members.update(self._installation_node_ids(session, run.installation_id))
                intersection = members & target_nodes
                if not intersection:
                    continue
                if not members <= target_nodes:
                    reasons.append(
                        FleetProfileReason(
                            code="profile.distributed_cross_scope",
                            detail=(
                                f"Running workload {run.alias} uses Sparks outside "
                                "the profile scope; review the complete distributed group."
                            ),
                            severity="error",
                        )
                    )
                    continue
                if (
                    run.id in desired_run_ids
                    or run.id in scheduled_stops
                    or run.id in managed_run_ids
                ):
                    continue
                stop_steps.append(
                    {
                        "kind": "stop",
                        "owner_id": run.id,
                        "recipe_revision_id": self._run_recipe_revision_id(
                            session, run
                        ),
                        "node_ids": sorted(members),
                        "label": f"Stop unlisted run {run.alias}",
                    }
                )
                scheduled_stops.add(run.id)

            if row.installation_policy == "exact" and target_nodes:
                installations = tuple(
                    session.scalars(
                        select(RecipeInstallation)
                        .where(RecipeInstallation.state.in_(_ACTIVE_INSTALL_STATES))
                        .order_by(RecipeInstallation.created_at, RecipeInstallation.id)
                    )
                )
                installation_nodes = self._installation_nodes(
                    session, [item.id for item in installations]
                )
                for installation in installations:
                    nodes = installation_nodes.get(installation.id, ())
                    node_ids = {node.node_id for node in nodes}
                    if not node_ids.intersection(target_nodes) or installation.id in desired_installation_ids:
                        continue
                    if not node_ids <= target_nodes:
                        reasons.append(
                            FleetProfileReason(
                                code="profile.shared_installation_scope",
                                detail="Exact installation policy would affect a multi-Spark installation outside the profile scope.",
                                severity="error",
                            )
                        )
                        continue
                    runs = tuple(
                        session.scalars(
                            select(RecipeRun)
                            .where(
                                RecipeRun.installation_id == installation.id,
                                RecipeRun.state.in_(_ACTIVE_RUN_STATES),
                            )
                            .order_by(RecipeRun.created_at, RecipeRun.id)
                        )
                    )
                    for run in runs:
                        if run.id in desired_run_ids or run.id in scheduled_stops:
                            continue
                        stop_steps.append(
                            {
                                "kind": "stop",
                                "owner_id": run.id,
                                "recipe_revision_id": installation.recipe_revision_id,
                                "node_ids": sorted(node_ids),
                                "label": f"Stop unlisted run {run.alias}",
                            }
                        )
                        scheduled_stops.add(run.id)
                    uninstall_steps.append(
                        {
                            "kind": "uninstall",
                            "owner_id": installation.id,
                            "recipe_revision_id": installation.recipe_revision_id,
                            "node_ids": sorted(node_ids),
                            "label": "Remove an installation not listed in this profile",
                        }
                    )

            if stop_steps:
                reasons.append(
                    FleetProfileReason(
                        code="profile.interruption_expected",
                        detail=(
                            "The reviewed plan includes required runtime stops; "
                            "affected workloads may be unavailable until final starts complete."
                        ),
                        severity="warning",
                    )
                )
            if self._switch_adapter is not None and adapter_switch_needed:
                switch_steps.append(
                    {
                        "kind": "switch",
                        "node_ids": sorted(target_nodes),
                        "label": f"Switch profile {view.name}",
                    }
                )
            # Prepare images and installations while the current profile can
            # still serve traffic. Required stops then release runtime
            # resources before final starts and verification.
            raw_steps = (
                preparation_steps
                + stop_steps
                + uninstall_steps
                + switch_steps
                + start_steps
            )
            steps = [
                FleetProfilePlanStep(index=index, **step)
                for index, step in enumerate(raw_steps)
            ]
            blocker_count = sum(reason.severity == "error" for reason in reasons)
            summary = FleetProfilePlanSummary(
                already_correct=sum(
                    item.actions == ["keep"] for item in assignment_previews
                ),
                placements=sum(step.kind == "create-placement" for step in steps),
                builds=sum(step.kind == "build" for step in steps),
                distributions=sum(step.kind == "distribute-image" for step in steps),
                installs=sum(step.kind == "install" for step in steps),
                starts=sum(step.kind in {"start", "switch"} for step in steps),
                stops=sum(step.kind == "stop" for step in steps),
                uninstalls=sum(step.kind == "uninstall" for step in steps),
                blockers=blocker_count,
            )
            identity = {
                "schema_version": 2,
                "profile_id": view.id,
                "profile_digest": view.profile_digest,
                "scope": sorted(target_nodes),
                "steps": [step.model_dump(mode="json") for step in steps],
                "assignment_state": [
                    item.model_dump(mode="json") for item in assignment_previews
                ],
                "preparations": [
                    {
                        "assignment_id": item.assignment_id,
                        "preparation": self._preparation_identity(
                            item.preparation
                        ),
                    }
                    for item in sorted(
                        assignment_preparations,
                        key=lambda item: item.assignment_id,
                    )
                ],
                "reasons": [reason.model_dump(mode="json") for reason in reasons],
            }
            return FleetProfilePreview(
                profile_id=view.id,
                profile_name=view.name,
                profile_digest=view.profile_digest,
                generated_at=now,
                allowed=blocker_count == 0,
                scope=FleetProfileScopePreview(
                    node_ids=sorted(target_nodes),
                    idle_node_ids=sorted(
                        target_nodes
                        - {
                            node.node_id
                            for assignment in view.assignments
                            for node in assignment.nodes
                        }
                    ),
                ),
                summary=summary,
                assignments=assignment_previews,
                preparations=sorted(
                    assignment_preparations,
                    key=lambda item: item.assignment_id,
                ),
                steps=steps,
                reasons=reasons,
                plan_digest=_digest(identity),
            )

    def apply(
        self, profile_id: str, *, plan_digest: str, request_key: str, actor: str
    ) -> FleetProfileApplicationView:
        preview = self.preview(profile_id)
        if not preview.allowed:
            raise FleetProfileConflict("Fleet profile preview is blocked")
        if preview.plan_digest != plan_digest:
            raise FleetProfileConflict("Fleet profile preview is stale")
        return self._queue_application(
            preview,
            request_key=request_key,
            actor=actor,
            operation_kind="fleet-profile.apply",
        )

    def _queue_application(
        self,
        preview: FleetProfilePreview,
        *,
        request_key: str,
        actor: str,
        operation_kind: str,
    ) -> FleetProfileApplicationView:
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if existing is not None:
                if (
                    existing.plan_digest != preview.plan_digest
                    or existing.profile_id != preview.profile_id
                ):
                    raise FleetProfileConflict(
                        "Fleet profile request key was reused for another plan"
                    )
                return self._application_view(existing)
            row = FleetProfileApplication(
                request_key=request_key,
                profile_id=preview.profile_id,
                profile_digest=preview.profile_digest,
                plan_digest=preview.plan_digest,
                state="succeeded" if not preview.steps else "queued",
                plan=preview.model_dump(mode="json"),
                current_step=0,
                current_operation_id=None,
                progress={
                    "operation_kind": operation_kind,
                    "completed_steps": 0,
                    "total_steps": len(preview.steps),
                },
                result={"changed": False} if not preview.steps else None,
                actor=actor,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._application_view(row)

    def operation_provider(self) -> object:
        """Project profile applications into the global Activity provider contract."""

        # Keep this import local so the profile domain remains usable by the
        # profile routes when the optional Activity projection is unavailable.
        from .operation_api import OperationListPage, OperationProvider

        def list_operations(query: object) -> object:
            after = getattr(query, "after", None)
            state = getattr(query, "state", None)
            node_id = getattr(query, "node_id", None)
            limit = int(getattr(query, "limit", 100))
            with self._sessions() as session:
                base_statement = select(FleetProfileApplication).order_by(
                    FleetProfileApplication.created_at.desc(),
                    FleetProfileApplication.id.desc(),
                )
                if isinstance(state, str):
                    base_statement = base_statement.where(
                        FleetProfileApplication.state == state
                    )
                # The plan is canonical JSON. Quoted containment avoids matching
                # a node-id substring while keeping this projection portable across
                # PostgreSQL JSON and SQLite JSON test databases.
                if isinstance(node_id, str):
                    base_statement = base_statement.where(
                        cast(
                            FleetProfileApplication.plan["scope"]["node_ids"],
                            String,
                        ).like(f'%"{node_id}"%')
                    )
                total = int(
                    session.scalar(
                        select(func.count()).select_from(base_statement.subquery())
                    )
                    or 0
                )
                statement = base_statement
                if after is not None:
                    after_at, after_id = after
                    statement = statement.where(
                        (FleetProfileApplication.created_at < after_at)
                        | (
                            (FleetProfileApplication.created_at == after_at)
                            & (FleetProfileApplication.id < after_id)
                        )
                    )
                rows = tuple(session.scalars(statement.limit(limit)))
                return OperationListPage(
                    items=[self._operation_item(row) for row in rows],
                    next_cursor=None,
                    total=total,
                )

        def get_operation(operation_id: str) -> Mapping[str, object]:
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, operation_id)
                if row is None:
                    raise KeyError(operation_id)
                return self._operation_item(row)

        return OperationProvider(
            family="fleet-profile",
            list_operations=list_operations,
            get_operation=get_operation,
        )

    @staticmethod
    def _operation_scope(row: FleetProfileApplication) -> tuple[str, ...]:
        plan = row.plan if isinstance(row.plan, Mapping) else {}
        scope = plan.get("scope")
        node_ids = scope.get("node_ids") if isinstance(scope, Mapping) else None
        if not isinstance(node_ids, list):
            return ()
        return tuple(node_id for node_id in node_ids if isinstance(node_id, str))

    @classmethod
    def _operation_phase(cls, row: FleetProfileApplication) -> str:
        if row.state == "succeeded":
            return "final_verify"
        plan = row.plan if isinstance(row.plan, Mapping) else {}
        steps = plan.get("steps")
        if isinstance(steps, list) and 0 <= row.current_step < len(steps):
            step = steps[row.current_step]
            kind = step.get("kind") if isinstance(step, Mapping) else None
            if kind in {"create-placement", "build", "install"}:
                return "prepare"
            if kind == "distribute-image":
                return "transfer"
            if kind == "stop":
                return "stop"
            if kind == "uninstall":
                return "cleanup"
            if kind == "start":
                return "start"
            if kind == "switch":
                progress = row.progress if isinstance(row.progress, Mapping) else {}
                child_progress = progress.get("child_progress")
                if isinstance(child_progress, Mapping):
                    phase = child_progress.get("phase")
                    if isinstance(phase, str):
                        return phase
                return "prepare"
        return "final_verify"

    @classmethod
    def _operation_item(cls, row: FleetProfileApplication) -> dict[str, object]:
        """Build a schema-2 Activity row for inspect-only profile recovery.

        Profile applications currently have one durable resumable attempt; the
        Activity projection therefore exposes attempt 1 and no retry action.
        """

        progress = {"phase": cls._operation_phase(row)}
        operation_kind = (
            row.progress.get("operation_kind")
            if isinstance(row.progress, Mapping)
            else None
        )
        return {
            "id": row.id,
            "parent_id": None,
            "node_ids": list(cls._operation_scope(row)),
            "kind": (
                operation_kind
                if isinstance(operation_kind, str)
                else "fleet-profile.apply"
            ),
            "state": row.state,
            "attempt": 1,
            "progress": progress,
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
            "supported_actions": [],
            "result": row.result,
        }

    def application(self, application_id: str) -> FleetProfileApplicationView:
        with self._sessions() as session:
            row = session.get(FleetProfileApplication, application_id)
            if row is None:
                raise KeyError(application_id)
            return self._application_view(row)

    def tick(self) -> bool:
        """Advance at most one profile application step; safe to call repeatedly."""

        if self._recipe_operations is None and self._switch_adapter is None:
            return False
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            row = session.scalar(
                select(FleetProfileApplication)
                .where(FleetProfileApplication.state.in_(("queued", "running")))
                .order_by(
                    FleetProfileApplication.created_at, FleetProfileApplication.id
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return False
            steps = row.plan.get("steps") if isinstance(row.plan, Mapping) else None
            if not isinstance(steps, list):
                row.state = "failed"
                row.status_reason = "Persisted Fleet profile plan is invalid"
                row.updated_at = now
                return True
            if row.current_operation_id:
                try:
                    child_source = (
                        row.progress.get("child_source")
                        if isinstance(row.progress, Mapping)
                        else None
                    )
                    if child_source == "switch-adapter":
                        if self._switch_adapter is None:
                            raise KeyError(row.current_operation_id)
                        child = self._switch_adapter.get(row.current_operation_id)
                    else:
                        if self._recipe_operations is None:
                            raise KeyError(row.current_operation_id)
                        child = self._recipe_operations.get(row.current_operation_id)
                except (KeyError, RuntimeError, ValueError) as error:
                    row.state = "failed"
                    row.status_reason = str(error)[:512] or "Child operation is unavailable"
                    row.updated_at = now
                    return True
                if isinstance(child, FleetProfileChildOperation):
                    progress = dict(row.progress)
                    if child.progress is not None:
                        progress["child_progress"] = child.progress.model_dump(
                            mode="json"
                        )
                    row.progress = progress
                if child.state in _CHILD_PENDING_STATES:
                    row.state = "running"
                    row.updated_at = now
                    return True
                if child.state in _CHILD_FAILED_STATES:
                    row.state = (
                        "waiting-for-operator"
                        if child.state == "waiting-for-operator"
                        else "failed"
                    )
                    child_reason = (
                        child.status_reason
                        if isinstance(child, FleetProfileChildOperation)
                        else None
                    )
                    row.status_reason = child_reason or (
                        f"Profile step {row.current_step + 1} ended in {child.state}"
                    )
                    row.updated_at = now
                    return True
                if child.state != "succeeded":
                    row.state = "failed"
                    row.status_reason = f"Profile step {row.current_step + 1} returned unsupported state {child.state}"
                    row.updated_at = now
                    return True
                progress = dict(row.progress)
                results = (
                    dict(progress.get("step_results", {}))
                    if isinstance(progress.get("step_results"), Mapping)
                    else {}
                )
                child_result: dict[str, object] = {"operation_id": child.id}
                if not isinstance(child, FleetProfileChildOperation):
                    child_result.update({"owner_id": child.owner_id, "kind": child.kind})
                if isinstance(child, FleetProfileChildOperation):
                    child_result["result"] = child.result
                results[str(row.current_step)] = child_result
                progress["step_results"] = results
                row.progress = progress
                row.current_operation_id = None
                row.current_step += 1
            if row.current_step >= len(steps):
                row.state = "succeeded"
                row.progress = {
                    **dict(row.progress),
                    "completed_steps": len(steps),
                    "total_steps": len(steps),
                }
                row.result = {"changed": bool(steps), "completed_steps": len(steps)}
                row.updated_at = now
                return True
            raw_step = steps[row.current_step]
            if not isinstance(raw_step, Mapping):
                row.state = "failed"
                row.status_reason = "Persisted Fleet profile step is invalid"
                row.updated_at = now
                return True
            row.state = "running"
            row.progress = {
                **dict(row.progress),
                "completed_steps": row.current_step,
                "total_steps": len(steps),
                "current_label": raw_step.get("label", "Applying profile"),
            }
            row.updated_at = now
            step = dict(raw_step)
            application_id = row.id
            step_index = row.current_step
            actor = row.actor

        try:
            operation_id, synchronous, child = self._start_step(
                application_id,
                step_index,
                step,
                actor=actor,
            )
        except (
            KeyError,
            RecipeOperationConflict,
            FleetProfileConflict,
            RuntimeError,
            ValueError,
        ) as error:
            with self._sessions.begin() as session:
                failed = session.get(
                    FleetProfileApplication, application_id, with_for_update=True
                )
                if failed is not None and failed.state in {"queued", "running"}:
                    failed.state = "failed"
                    failed.status_reason = str(error)[:512]
                    failed.updated_at = _aware(self._clock())
            return True
        with self._sessions.begin() as session:
            current = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if current is None or current.state not in {"queued", "running"}:
                return True
            if synchronous:
                current.current_step += 1
                current.progress = {
                    **dict(current.progress),
                    "completed_steps": current.current_step,
                }
            else:
                current.current_operation_id = operation_id
                current.progress = {
                    **dict(current.progress),
                    "child_source": (
                        "switch-adapter" if step.get("kind") == "switch" else "recipe"
                    ),
                    **(
                        {
                            "child_progress": child.progress.model_dump(mode="json")
                        }
                        if isinstance(child, FleetProfileChildOperation)
                        and child.progress is not None
                        else {}
                    ),
                }
            current.updated_at = _aware(self._clock())
        return True

    def _start_step(
        self,
        application_id: str,
        step_index: int,
        step: Mapping[str, object],
        *,
        actor: str,
    ) -> tuple[str | None, bool, FleetProfileChildOperation | None]:
        kind = step.get("kind")
        request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"vonk-forge:profile:{application_id}:{step_index}"
            )
        )
        owner_id = step.get("owner_id")
        assignment_id = step.get("assignment_id")
        assignment = (
            self._application_assignment(application_id, assignment_id)
            if isinstance(assignment_id, str)
            else None
        )
        if kind == "switch":
            if self._switch_adapter is None:
                raise FleetProfileConflict("Fleet profile switch adapter is unavailable")
            assignments = self._application_assignments(application_id)
            child = self._switch_adapter.start(
                application_id=application_id,
                assignments=assignments,
                scope_node_ids=self._application_scope(application_id),
                actor=actor,
                request_id=request_id,
            )
            if not isinstance(child, FleetProfileChildOperation):
                raise FleetProfileConflict(
                    "Fleet profile switch adapter returned an invalid child operation"
                )
            return child.id, False, child
        if self._recipe_operations is None:
            raise FleetProfileConflict("Fleet profile recipe operations are unavailable")
        if kind == "stop" and isinstance(owner_id, str):
            plan = self._recipe_operations.preview_stop(owner_id)
            operation = self._recipe_operations.stop(
                owner_id,
                plan_digest=plan.plan_digest,
                actor=actor,
                request_id=request_id,
            )
            return operation.id, False, None
        if kind == "uninstall" and isinstance(owner_id, str):
            plan = self._recipe_operations.preview_uninstall(owner_id)
            operation = self._recipe_operations.uninstall(
                owner_id,
                plan_digest=plan.plan_digest,
                actor=actor,
                request_id=request_id,
            )
            return operation.id, False, None
        if assignment is None:
            raise FleetProfileConflict("Fleet profile assignment is unavailable")
        node_ids = tuple(node.node_id for node in assignment.nodes)
        context = self._assignment_context(application_id, assignment.id)
        if kind == "create-placement":
            plan = self._recipe_operations.preview_mapping(
                assignment.recipe_revision_id, node_ids, parameters={}, actor=actor
            )
            mapping_id = self._recipe_operations.create_mapping(plan, actor=actor)
            self._store_assignment_context(
                application_id,
                assignment.id,
                {
                    **context,
                    "mapping_id": mapping_id,
                    "mapping_generation": plan.generation,
                },
            )
            return None, True, None
        if kind == "build":
            builder_node_id = min(node_ids)
            plan = self._recipe_operations.preview_build(
                assignment.recipe_revision_id, builder_node_id
            )
            operation = self._recipe_operations.build(
                plan,
                build_input_sha256=plan.build_input_sha256,
                actor=actor,
                request_id=request_id,
            )
            return operation.id, False, None
        mapping_id, generation = self._mapping_identity(assignment, context)
        build_id = self._successful_build_id(assignment.recipe_revision_id)
        if kind == "distribute-image":
            preview = self._recipe_operations.preview_image_distribution(
                build_id, mapping_id, mapping_generation=generation
            )
            if not preview.node_ids:
                return None, True, None
            operation = self._recipe_operations.distribute_image(
                build_id,
                mapping_id,
                mapping_generation=generation,
                plan_digest=preview.plan_digest,
                actor=actor,
                request_id=request_id,
            )
            return operation.id, False, None
        if kind == "install":
            plan = self._recipe_operations.preview_install(mapping_id, build_id)
            operation = self._recipe_operations.install(
                plan, plan_digest=plan.plan_digest, actor=actor, request_id=request_id
            )
            self._store_assignment_context(
                application_id,
                assignment.id,
                {
                    **context,
                    "mapping_id": mapping_id,
                    "mapping_generation": generation,
                    "installation_id": operation.owner_id,
                },
            )
            return operation.id, False, None
        if kind == "start":
            installation_id = self._installation_identity(assignment, context)
            plan = self._recipe_operations.preview_run(
                installation_id,
                assignment.alias or assignment.recipe_title.lower().replace(" ", "-"),
            )
            operation = self._recipe_operations.start(
                plan, plan_digest=plan.plan_digest, actor=actor, request_id=request_id
            )
            return operation.id, False, None
        raise FleetProfileConflict("Fleet profile step kind is unsupported")

    def _validated_assignments(
        self, session: Session, values: Sequence[FleetProfileAssignmentInput]
    ) -> list[dict[str, object]]:
        assignments: list[dict[str, object]] = []
        all_node_ids = {node.node_id for value in values for node in value.nodes}
        nodes = (
            {
                node.node_id: node
                for node in session.scalars(
                    select(AgentNode).where(AgentNode.node_id.in_(all_node_ids))
                )
            }
            if all_node_ids
            else {}
        )
        for value in values:
            revision = session.get(LocalRecipeRevision, value.recipe_revision_id)
            if revision is None:
                raise FleetProfileConflict("profile recipe revision does not exist")
            if revision.lifecycle != "resolved" or revision.content_sha256 is None:
                raise FleetProfileConflict("profile recipe revision must be resolved")
            try:
                topology = recipe_topology(revision.document)
            except RecipeContractError as error:
                raise FleetProfileConflict(str(error)) from error
            if topology.get("name") != value.topology_name:
                raise FleetProfileConflict(
                    "profile topology does not match the recipe revision"
                )
            node_count = topology.get("node_count")
            if type(node_count) is not int or node_count != len(value.nodes):
                raise FleetProfileConflict(
                    "profile Spark count does not match the recipe topology"
                )
            expected_roles = _expanded_roles(topology)
            actual_roles = tuple(
                (node.role, node.endpoint_owner)
                for node in sorted(value.nodes, key=lambda item: item.rank)
            )
            if expected_roles != actual_roles:
                raise FleetProfileConflict(
                    "profile ranks and roles do not match the recipe topology"
                )
            ranked_node_ids = [
                node.node_id for node in sorted(value.nodes, key=lambda item: item.rank)
            ]
            if ranked_node_ids != sorted(ranked_node_ids):
                raise FleetProfileConflict(
                    "profile rank order must match deterministic Spark identity order"
                )
            for node in value.nodes:
                enrolled = nodes.get(node.node_id)
                if enrolled is None or enrolled.revoked_at is not None:
                    raise FleetProfileConflict(
                        "profile Spark is not an active enrolled Fleet member"
                    )
            assignments.append(
                {
                    "id": _assignment_id(value),
                    "recipe_revision_id": value.recipe_revision_id,
                    "topology_name": value.topology_name,
                    "desired_state": value.desired_state,
                    "alias": value.alias,
                    "nodes": [node.model_dump(mode="json") for node in value.nodes],
                }
            )
        return assignments

    def _validated_scope(
        self, session: Session, value: FleetProfileInput
    ) -> list[str]:
        """Resolve and validate the explicit reconciliation boundary."""

        assigned = {
            node.node_id for assignment in value.assignments for node in assignment.nodes
        }
        scope = list(value.scope.node_ids)
        if not scope:
            raise FleetProfileConflict(
                "Fleet profile scope must contain at least one enrolled Spark"
            )
        if not assigned <= set(scope):
            raise FleetProfileConflict(
                "profile assignment nodes must be inside profile scope"
            )
        rows = {
            node.node_id: node
            for node in session.scalars(
                select(AgentNode).where(AgentNode.node_id.in_(scope))
            )
        }
        if len(rows) != len(scope) or any(
            rows[node].revoked_at is not None for node in scope
        ):
            raise FleetProfileConflict(
                "profile scope contains an inactive Spark; every member must be "
                "an active enrolled Fleet member"
            )
        return sorted(scope)

    def _view(self, session: Session, row: FleetProfile) -> FleetProfileView:
        assignments: list[FleetProfileAssignment] = []
        for raw in row.assignments:
            if not isinstance(raw, Mapping):
                raise FleetProfileConflict("stored Fleet profile assignment is invalid")
            revision_id = raw.get("recipe_revision_id")
            if not isinstance(revision_id, str):
                raise FleetProfileConflict(
                    "stored Fleet profile recipe revision is invalid"
                )
            revision = session.get(LocalRecipeRevision, revision_id)
            if revision is None:
                raise FleetProfileConflict(
                    "stored Fleet profile recipe revision is unavailable"
                )
            recipe = session.get(LocalRecipe, revision.recipe_id)
            if recipe is None:
                raise FleetProfileConflict("stored Fleet profile recipe is unavailable")
            model_title = self._model_title(session, revision.document)
            assignments.append(
                FleetProfileAssignment(
                    id=str(raw["id"]),
                    recipe_id=recipe.id,
                    recipe_title=recipe.title,
                    model_title=model_title,
                    recipe_revision_id=revision.id,
                    topology_name=str(raw["topology_name"]),
                    desired_state=str(raw["desired_state"]),
                    alias=raw.get("alias"),
                    nodes=[
                        FleetProfileNode.model_validate(item)
                        for item in raw.get("nodes", [])
                    ],
                )
            )
        scope = list(row.scope)
        try:
            profile_scope = FleetProfileScope(node_ids=scope)
        except ValueError as error:
            raise FleetProfileConflict("stored Fleet profile scope is invalid") from error
        document = _profile_document(row)
        document["scope"] = list(profile_scope.node_ids)
        return FleetProfileView(
            id=row.id,
            name=row.name,
            description=row.description,
            installation_policy=row.installation_policy,
            labels=dict(row.labels),
            favorite=row.favorite,
            scope=profile_scope,
            assignments=assignments,
            profile_digest=_digest(document),
            created_by=row.created_by,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _model_title(session: Session, document: Mapping[str, object]) -> str | None:
        model = document.get("model")
        if not isinstance(model, Mapping):
            return None
        publisher, slug, content_sha256 = (
            model.get("publisher"),
            model.get("slug"),
            model.get("content_sha256"),
        )
        if not all(isinstance(item, str) for item in (publisher, slug, content_sha256)):
            return None
        title = session.scalar(
            select(CatalogEntity.title)
            .join(
                CatalogEntityRevision,
                CatalogEntityRevision.entity_id == CatalogEntity.id,
            )
            .where(
                CatalogEntity.kind == "model-version",
                CatalogEntity.publisher == publisher,
                CatalogEntity.slug == slug,
                CatalogEntityRevision.content_sha256 == content_sha256,
            )
            .limit(1)
        )
        return title or f"{publisher}/{slug}"

    class _AssignmentState:
        def __init__(
            self,
            *,
            current_state: str,
            mapping: ClusterMapping | None,
            installation: RecipeInstallation | None,
            run: RecipeRun | None,
            build: RecipeBuild | None,
        ) -> None:
            self.current_state = current_state
            self.mapping = mapping
            self.installation = installation
            self.run = run
            self.build = build

    @staticmethod
    def _preparation_identity(preparation: RolloutPreparation) -> dict[str, object]:
        identity = preparation.model_dump(mode="json")
        for asset_name in ("model", "runtime_image"):
            asset = identity.get(asset_name)
            if not isinstance(asset, dict):
                continue
            controller = asset.get("controller")
            if isinstance(controller, dict):
                controller.pop("verified_at", None)
                controller.pop("state", None)
                controller.pop("verified_bytes", None)
                controller.pop("missing_bytes", None)
            targets = asset.get("targets")
            if isinstance(targets, list):
                for target in targets:
                    if isinstance(target, dict):
                        target.pop("verified_at", None)
                        target.pop("state", None)
                        target.pop("present_bytes", None)
                        target.pop("missing_bytes", None)
                        target.pop("verified_sha256", None)
                        target.pop("imported_image_digest", None)
                        target.pop("reason", None)
        identity.pop("controller_ready", None)
        identity.pop("targets_ready", None)
        identity.pop("ready", None)
        identity.pop("reasons", None)
        return identity


    def _assignment_state(
        self, session: Session, assignment: FleetProfileAssignment
    ) -> _AssignmentState:
        build = session.scalar(
            select(RecipeBuild)
            .where(
                RecipeBuild.recipe_revision_id == assignment.recipe_revision_id,
                RecipeBuild.state == "succeeded",
            )
            .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
            .limit(1)
        )
        mappings = tuple(
            session.scalars(
                select(ClusterMapping)
                .where(
                    ClusterMapping.recipe_revision_id == assignment.recipe_revision_id,
                    ClusterMapping.topology_name == assignment.topology_name,
                    ClusterMapping.state == "ready",
                )
                .order_by(ClusterMapping.updated_at.desc(), ClusterMapping.id.desc())
            )
        )
        expected = {
            (node.node_id, node.rank, node.role, node.endpoint_owner)
            for node in assignment.nodes
        }
        mapping = None
        for candidate in mappings:
            members = tuple(
                session.scalars(
                    select(ClusterMappingNode).where(
                        ClusterMappingNode.mapping_id == candidate.id
                    )
                )
            )
            actual = {
                (node.node_id, node.rank, node.role, node.endpoint_owner)
                for node in members
            }
            if actual == expected:
                mapping = candidate
                break
        if mapping is None:
            return self._AssignmentState(
                current_state="not-placed",
                mapping=None,
                installation=None,
                run=None,
                build=build,
            )
        installation = session.scalar(
            select(RecipeInstallation)
            .where(
                RecipeInstallation.mapping_id == mapping.id,
                RecipeInstallation.recipe_revision_id == assignment.recipe_revision_id,
                RecipeInstallation.state.in_(_ACTIVE_INSTALL_STATES),
            )
            .order_by(
                RecipeInstallation.updated_at.desc(), RecipeInstallation.id.desc()
            )
            .limit(1)
        )
        if installation is None:
            return self._AssignmentState(
                current_state="placed",
                mapping=mapping,
                installation=None,
                run=None,
                build=build,
            )
        install_members = tuple(
            session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation.id
                )
            )
        )
        exact_installed = (
            installation.state == "installed"
            and len(install_members) == len(expected)
            and {(node.node_id, node.rank, node.role) for node in install_members}
            == {(node.node_id, node.rank, node.role) for node in assignment.nodes}
            and all(node.state == "installed" for node in install_members)
        )
        if not exact_installed:
            state = (
                "installing"
                if installation.state in {"planned", "installing"}
                else "degraded"
            )
            return self._AssignmentState(
                current_state=state,
                mapping=mapping,
                installation=installation,
                run=None,
                build=build,
            )
        run = session.scalar(
            select(RecipeRun)
            .where(
                RecipeRun.installation_id == installation.id,
                RecipeRun.state.in_(_ACTIVE_RUN_STATES),
            )
            .order_by(RecipeRun.updated_at.desc(), RecipeRun.id.desc())
            .limit(1)
        )
        if run is None:
            return self._AssignmentState(
                current_state="installed",
                mapping=mapping,
                installation=installation,
                run=None,
                build=build,
            )
        run_members = tuple(
            session.scalars(select(RunNode).where(RunNode.run_id == run.id))
        )
        healthy = (
            run.state == "running"
            and run.route_state == "published"
            and len(run_members) == len(expected)
            and {(node.node_id, node.rank, node.role) for node in run_members}
            == {(node.node_id, node.rank, node.role) for node in assignment.nodes}
            and all(node.state == "running" for node in run_members)
        )
        return self._AssignmentState(
            current_state="running" if healthy else "degraded",
            mapping=mapping,
            installation=installation,
            run=run,
            build=build,
        )

    @staticmethod
    def _installation_nodes(
        session: Session, installation_ids: Sequence[str]
    ) -> dict[str, tuple[InstallationNode, ...]]:
        grouped: dict[str, list[InstallationNode]] = {}
        if installation_ids:
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id.in_(installation_ids)
                )
            ):
                grouped.setdefault(node.installation_id, []).append(node)
        return {key: tuple(value) for key, value in grouped.items()}

    @staticmethod
    def _run_nodes(
        session: Session, run_ids: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        if run_ids:
            for node in session.scalars(
                select(RunNode).where(RunNode.run_id.in_(run_ids))
            ):
                grouped.setdefault(node.run_id, []).append(node.node_id)
        return {key: tuple(value) for key, value in grouped.items()}

    @staticmethod
    def _installation_node_ids(
        session: Session, installation_id: str
    ) -> tuple[str, ...]:
        return tuple(
            node.node_id
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )

    @staticmethod
    def _run_recipe_revision_id(session: Session, run: RecipeRun) -> str | None:
        return session.scalar(
            select(RecipeInstallation.recipe_revision_id).where(
                RecipeInstallation.id == run.installation_id
            )
        )

    def _application_view(
        self, row: FleetProfileApplication
    ) -> FleetProfileApplicationView:
        steps = row.plan.get("steps") if isinstance(row.plan, Mapping) else []
        return FleetProfileApplicationView(
            id=row.id,
            profile_id=row.profile_id,
            profile_digest=row.profile_digest,
            plan_digest=row.plan_digest,
            state=row.state,
            current_step=row.current_step,
            total_steps=len(steps) if isinstance(steps, list) else 0,
            current_operation_id=row.current_operation_id,
            status_reason=row.status_reason,
            progress=dict(row.progress),
            result=dict(row.result) if isinstance(row.result, Mapping) else None,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    def _application_assignment(
        self, application_id: str, assignment_id: object
    ) -> FleetProfileAssignment | None:
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            profile = session.get(FleetProfile, application.profile_id)
            if profile is None:
                raise KeyError(application.profile_id)
            return next(
                (
                    item
                    for item in self._view(session, profile).assignments
                    if item.id == assignment_id
                ),
                None,
            )

    def _application_scope(self, application_id: str) -> tuple[str, ...]:
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            return self._operation_scope(application)

    def _application_assignments(
        self, application_id: str
    ) -> tuple[FleetProfileAssignment, ...]:
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            profile = session.get(FleetProfile, application.profile_id)
            if profile is None:
                raise KeyError(application.profile_id)
            assignments = self._view(session, profile).assignments
            return tuple(sorted(assignments, key=lambda item: item.id))

    def _assignment_context(
        self, application_id: str, assignment_id: str
    ) -> dict[str, object]:
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            contexts = (
                application.progress.get("assignments")
                if isinstance(application.progress, Mapping)
                else None
            )
            value = (
                contexts.get(assignment_id) if isinstance(contexts, Mapping) else None
            )
            return dict(value) if isinstance(value, Mapping) else {}

    def _store_assignment_context(
        self, application_id: str, assignment_id: str, value: Mapping[str, object]
    ) -> None:
        with self._sessions.begin() as session:
            application = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if application is None:
                raise KeyError(application_id)
            progress = dict(application.progress)
            assignments = (
                dict(progress.get("assignments", {}))
                if isinstance(progress.get("assignments"), Mapping)
                else {}
            )
            assignments[assignment_id] = dict(value)
            progress["assignments"] = assignments
            application.progress = progress
            application.updated_at = _aware(self._clock())

    def _mapping_identity(
        self, assignment: FleetProfileAssignment, context: Mapping[str, object]
    ) -> tuple[str, int]:
        mapping_id, generation = (
            context.get("mapping_id"),
            context.get("mapping_generation"),
        )
        if isinstance(mapping_id, str) and type(generation) is int:
            return mapping_id, generation
        with self._sessions() as session:
            state = self._assignment_state(session, assignment)
            if state.mapping is None:
                raise FleetProfileConflict("Fleet profile placement is unavailable")
            return state.mapping.id, state.mapping.generation

    def _installation_identity(
        self, assignment: FleetProfileAssignment, context: Mapping[str, object]
    ) -> str:
        installation_id = context.get("installation_id")
        if isinstance(installation_id, str):
            return installation_id
        with self._sessions() as session:
            state = self._assignment_state(session, assignment)
            if state.installation is None:
                raise FleetProfileConflict("Fleet profile installation is unavailable")
            return state.installation.id

    def _successful_build_id(self, recipe_revision_id: str) -> str:
        with self._sessions() as session:
            build_id = session.scalar(
                select(RecipeBuild.id)
                .where(
                    RecipeBuild.recipe_revision_id == recipe_revision_id,
                    RecipeBuild.state == "succeeded",
                )
                .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
                .limit(1)
            )
            if build_id is None:
                raise FleetProfileConflict("Fleet profile recipe build is unavailable")
            return build_id


__all__ = ["FleetProfileConflict", "FleetProfileService"]
