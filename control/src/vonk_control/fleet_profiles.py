"""PostgreSQL authority for saved Fleet profiles and live-versus-desired plans."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileAssignment,
    FleetProfileAssignmentInput,
    FleetProfileAssignmentPreview,
    FleetProfileInput,
    FleetProfileList,
    FleetProfileNode,
    FleetProfilePlanStep,
    FleetProfilePlanSummary,
    FleetProfilePreview,
    FleetProfileReason,
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
        "schema_version": 1,
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "installation_policy": row.installation_policy,
        "labels": dict(row.labels),
        "favorite": row.favorite,
        "assignments": list(row.assignments),
    }


class FleetProfileService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        recipe_operations: RecipeOperationService | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._recipe_operations = recipe_operations

    def list(self) -> FleetProfileList:
        now = _aware(self._clock())
        with self._sessions() as session:
            rows = tuple(
                session.scalars(
                    select(FleetProfile)
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

    def create(self, value: FleetProfileInput, *, actor: str) -> FleetProfileView:
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            assignments = self._validated_assignments(session, value.assignments)
            row = FleetProfile(
                name=value.name,
                description=value.description,
                installation_policy=value.installation_policy,
                labels=dict(value.labels),
                favorite=value.favorite,
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
            assignments = self._validated_assignments(session, value.assignments)
            row.name = value.name
            row.description = value.description
            row.installation_policy = value.installation_policy
            row.labels = dict(value.labels)
            row.favorite = value.favorite
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
            reasons: list[FleetProfileReason] = []
            stop_steps: list[dict[str, object]] = []
            uninstall_steps: list[dict[str, object]] = []
            assignment_steps: list[dict[str, object]] = []
            desired_installation_ids: set[str] = set()
            desired_run_ids: set[str] = set()
            target_nodes = {
                node.node_id for item in view.assignments for node in item.nodes
            }

            for assignment in view.assignments:
                state = self._assignment_state(session, assignment)
                if state.installation is not None:
                    desired_installation_ids.add(state.installation.id)
                if state.run is not None and assignment.desired_state == "running":
                    desired_run_ids.add(state.run.id)
                actions: list[str] = []
                item_reasons: list[FleetProfileReason] = []
                if assignment.desired_state == "installed" and state.run is not None:
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
                if state.current_state == assignment.desired_state or (
                    assignment.desired_state == "installed"
                    and state.current_state == "running"
                ):
                    if not actions:
                        actions.append("keep")
                else:
                    if state.mapping is None:
                        actions.append("create-placement")
                        assignment_steps.append(
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
                            item_reasons.append(
                                FleetProfileReason(
                                    code="profile.build_missing",
                                    detail=f"{assignment.recipe_title} needs a successful build before this profile can install it.",
                                    severity="error",
                                )
                            )
                        else:
                            actions.extend(("distribute-image", "install"))
                            assignment_steps.extend(
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
                        assignment_steps.append(
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
                    if (
                        not node_ids.intersection(target_nodes)
                        or installation.id in desired_installation_ids
                    ):
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
                        if run.id in desired_run_ids:
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
                    uninstall_steps.append(
                        {
                            "kind": "uninstall",
                            "owner_id": installation.id,
                            "recipe_revision_id": installation.recipe_revision_id,
                            "node_ids": sorted(node_ids),
                            "label": "Remove an installation not listed in this profile",
                        }
                    )

            raw_steps = stop_steps + uninstall_steps + assignment_steps
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
                distributions=sum(step.kind == "distribute-image" for step in steps),
                installs=sum(step.kind == "install" for step in steps),
                starts=sum(step.kind == "start" for step in steps),
                stops=sum(step.kind == "stop" for step in steps),
                uninstalls=sum(step.kind == "uninstall" for step in steps),
                blockers=blocker_count,
            )
            identity = {
                "schema_version": 1,
                "profile_id": view.id,
                "profile_digest": view.profile_digest,
                "steps": [step.model_dump(mode="json") for step in steps],
                "assignment_state": [
                    item.model_dump(mode="json") for item in assignment_previews
                ],
                "reasons": [reason.model_dump(mode="json") for reason in reasons],
            }
            return FleetProfilePreview(
                profile_id=view.id,
                profile_name=view.name,
                profile_digest=view.profile_digest,
                generated_at=now,
                allowed=blocker_count == 0,
                summary=summary,
                assignments=assignment_previews,
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
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if existing is not None:
                if (
                    existing.plan_digest != plan_digest
                    or existing.profile_id != profile_id
                ):
                    raise FleetProfileConflict(
                        "Fleet profile request key was reused for another plan"
                    )
                return self._application_view(existing)
            row = FleetProfileApplication(
                request_key=request_key,
                profile_id=profile_id,
                profile_digest=preview.profile_digest,
                plan_digest=preview.plan_digest,
                state="succeeded" if not preview.steps else "queued",
                plan=preview.model_dump(mode="json"),
                current_step=0,
                current_operation_id=None,
                progress={"completed_steps": 0, "total_steps": len(preview.steps)},
                result={"changed": False} if not preview.steps else None,
                actor=actor,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._application_view(row)

    def application(self, application_id: str) -> FleetProfileApplicationView:
        with self._sessions() as session:
            row = session.get(FleetProfileApplication, application_id)
            if row is None:
                raise KeyError(application_id)
            return self._application_view(row)

    def tick(self) -> bool:
        """Advance at most one profile application step; safe to call repeatedly."""

        if self._recipe_operations is None:
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
                    child = self._recipe_operations.get(row.current_operation_id)
                except KeyError:
                    row.state = "failed"
                    row.status_reason = "Child recipe operation is unavailable"
                    row.updated_at = now
                    return True
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
                    row.status_reason = (
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
                results[str(row.current_step)] = {
                    "operation_id": child.id,
                    "owner_id": child.owner_id,
                    "kind": child.kind,
                }
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
            operation_id, synchronous = self._start_step(
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
            current.updated_at = _aware(self._clock())
        return True

    def _start_step(
        self,
        application_id: str,
        step_index: int,
        step: Mapping[str, object],
        *,
        actor: str,
    ) -> tuple[str | None, bool]:
        assert self._recipe_operations is not None
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
        if kind == "stop" and isinstance(owner_id, str):
            plan = self._recipe_operations.preview_stop(owner_id)
            operation = self._recipe_operations.stop(
                owner_id,
                plan_digest=plan.plan_digest,
                actor=actor,
                request_id=request_id,
            )
            return operation.id, False
        if kind == "uninstall" and isinstance(owner_id, str):
            plan = self._recipe_operations.preview_uninstall(owner_id)
            operation = self._recipe_operations.uninstall(
                owner_id,
                plan_digest=plan.plan_digest,
                actor=actor,
                request_id=request_id,
            )
            return operation.id, False
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
            return None, True
        mapping_id, generation = self._mapping_identity(assignment, context)
        build_id = self._successful_build_id(assignment.recipe_revision_id)
        if kind == "distribute-image":
            preview = self._recipe_operations.preview_image_distribution(
                build_id, mapping_id, mapping_generation=generation
            )
            if not preview.node_ids:
                return None, True
            operation = self._recipe_operations.distribute_image(
                build_id,
                mapping_id,
                mapping_generation=generation,
                plan_digest=preview.plan_digest,
                actor=actor,
                request_id=request_id,
            )
            return operation.id, False
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
            return operation.id, False
        if kind == "start":
            installation_id = self._installation_identity(assignment, context)
            plan = self._recipe_operations.preview_run(
                installation_id,
                assignment.alias or assignment.recipe_title.lower().replace(" ", "-"),
            )
            operation = self._recipe_operations.start(
                plan, plan_digest=plan.plan_digest, actor=actor, request_id=request_id
            )
            return operation.id, False
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
        document = _profile_document(row)
        return FleetProfileView(
            id=row.id,
            name=row.name,
            description=row.description,
            installation_policy=row.installation_policy,
            labels=dict(row.labels),
            favorite=row.favorite,
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
