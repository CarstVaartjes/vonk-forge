"""PostgreSQL authority for saved Fleet profiles and live-versus-desired plans."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime

from pydantic import ConfigDict, TypeAdapter, ValidationError
from sqlalchemy import String, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationResult,
    FleetProfileApplicationView,
    FleetProfileAssignment,
    FleetProfileAssignmentContext,
    FleetProfileAssignmentInput,
    FleetProfileAssignmentPreparation,
    FleetProfileAssignmentPreview,
    FleetProfileAssignmentView,
    FleetProfileChildOperation,
    FleetProfileChildProgress,
    FleetProfileInput,
    FleetProfileIntendedConfiguration,
    FleetProfileList,
    FleetProfileNode,
    FleetProfilePlanStep,
    FleetProfilePlanSummary,
    FleetProfilePreview,
    FleetProfileReason,
    FleetProfileScope,
    FleetProfileScopePreview,
    FleetProfileStepResult,
    FleetProfileSwitchAdapter,
    FleetProfileSwitchAdapterResult,
    FleetProfileSwitchAdapterState,
    FleetProfileSwitchChildResult,
    FleetProfileView,
)
from .logging import redact_text
from .models import (
    AgentNode,
    AgentNodeProfile,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    FleetProfile,
    FleetProfileApplication,
    InstallationNode,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .operation_contract import OperationFailureEvidence
from .operation_progress import project_progress
from .preparation_contract import RolloutPreparation
from .recipe_operations import RecipeOperationConflict, RecipeOperationService
from .recipe_runtime_specs import (
    recipe_topology,
    resolve_recipe_entities,
)
from .run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchOperation,
    RunSwitchOperationResult,
    RunSwitchStopApplyRequest,
    RunSwitchStopPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from .run_switch_operations import (
    RunSwitchOperationConflict,
    RunSwitchOperationService,
)

_STORED_ASSIGNMENTS = TypeAdapter(
    list[FleetProfileAssignmentInput], config=ConfigDict(strict=True)
)
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
def _persisted_profile_plan(row: FleetProfileApplication) -> FleetProfilePreview:
    """Load the complete stored preview through its canonical contract."""

    try:
        plan = FleetProfilePreview.model_validate_json(
            json.dumps(row.plan), strict=True
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise FleetProfileConflict("Persisted Fleet profile plan is invalid") from error
    if (
        plan.profile_id != row.profile_id
        or plan.profile_digest != row.profile_digest
        or plan.plan_digest != row.plan_digest
    ):
        raise FleetProfileConflict("Persisted Fleet profile plan identity is invalid")
    return plan


def _persisted_profile_result(
    row: FleetProfileApplication,
) -> FleetProfileApplicationResult | None:
    """Load a stored result without treating malformed JSON as no result."""

    if row.result is None:
        if row.state == "succeeded":
            raise FleetProfileConflict("Persisted Fleet profile result is invalid")
        return None
    try:
        return FleetProfileApplicationResult.model_validate_json(
            json.dumps(row.result), strict=True
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise FleetProfileConflict("Persisted Fleet profile result is invalid") from error


def _persisted_profile_progress(
    row: FleetProfileApplication,
) -> FleetProfileApplicationProgress:
    """Load progress through its canonical contract before worker mutation."""

    try:
        return FleetProfileApplicationProgress.model_validate_json(
            canonical_message(row.progress), strict=True
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise FleetProfileConflict(
            "Persisted Fleet profile progress is invalid"
        ) from error


_PROFILE_PHASE_BY_RUN_PHASE = {
    "transfer": "target-copy",
    "verify": "final-verify",
    "prepare": "runtime-install",
    "final_verify": "final-verify",
}


class FleetProfileConflict(RuntimeError):
    """A Fleet profile is invalid, stale, or cannot be safely applied."""


class RunSwitchFleetProfileAdapter:
    """Compose one durable profile child from Run/Switch operations.

    The adapter plans conflicts against the complete profile assignment set
    before queuing any Run/Switch child.  Run/Switch remains the authority for
    each exact model, recipe revision, mapping, cache transfer, and run
    admission; this class only sequences those children under one profile
    application identity.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        run_switch: RunSwitchOperationService,
    ) -> None:
        self._sessions = sessions
        self._run_switch = run_switch

    def start(
        self,
        *,
        application_id: str,
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
        actor: str,
        request_id: str,
    ) -> FleetProfileChildOperation:
        ordered_assignments = tuple(sorted(assignments, key=lambda item: item.id))
        if tuple(scope_node_ids) != tuple(sorted(set(scope_node_ids))):
            raise FleetProfileConflict(
                "profile switch scope must be the complete sorted node boundary"
            )
        scope = set(scope_node_ids)
        if any(
            node.node_id not in scope
            for assignment in ordered_assignments
            for node in assignment.nodes
        ):
            raise FleetProfileConflict(
                "profile switch assignment contains a node outside its bound scope"
            )
        with self._sessions.begin() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            existing = self._state(application)
            if existing is not None:
                expected_scope = list(scope_node_ids)
                expected_assignment_ids = [item.id for item in ordered_assignments]
                if (
                    existing.get("scope_node_ids") != expected_scope
                    or existing.get("assignment_ids") != expected_assignment_ids
                ):
                    raise FleetProfileConflict(
                        "profile switch child was replayed with a different "
                        "bound scope or assignments"
                    )
                return self._view_from_state(application, existing)
            queue = self._plan_queue(session, ordered_assignments, scope_node_ids)
            state: dict[str, object] = {
                "schema_version": 2,
                "child_id": application_id,
                "scope_node_ids": list(scope_node_ids),
                "assignment_ids": [item.id for item in ordered_assignments],
                "assignments": [
                    item.model_dump(mode="json") for item in ordered_assignments
                ],
                "queue": queue,
                "position": 0,
                "active_operation_id": None,
                "active_kind": None,
                "children": [],
                "actor": actor,
                "request_id": request_id,
            }
        self._save_state(application_id, state)
        return self._advance(application_id, ordered_assignments)

    def get(self, operation_id: str) -> FleetProfileChildOperation:
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, operation_id)
            if application is None:
                raise KeyError(operation_id)
            state = self._state(application)
            if state is None:
                raise KeyError(operation_id)
            assignments = self._assignments_from_state(state)
        active = state.get("active_operation_id")
        if isinstance(active, str):
            self._run_switch.tick()
        return self._advance(operation_id, assignments)

    @staticmethod
    def _assignment_request(
        session: Session,
        assignment: FleetProfileAssignment,
        *,
        request_key: str | None = None,
    ) -> RunSwitchApplyRequest:
        """Map one profile assignment onto the Run/Switch authority.

        This is the single definition of how a profile assignment becomes a
        Run/Switch request.  Both apply and preparation projection use it, so a
        preview can never describe a different model, revision, group or alias
        than the child operation it later binds.
        """

        revision = session.get(
            CatalogDocumentRevision, assignment.recipe_revision_id
        )
        if revision is None:
            raise KeyError(assignment.recipe_revision_id)
        resolved = resolve_recipe_entities(session, revision.document)
        models = resolved.get("models")
        model_digest = (
            models[0].content_digest
            if isinstance(models, Sequence) and models
            else None
        )
        if not isinstance(model_digest, str):
            raise FleetProfileConflict(
                "Profile assignment has no exact model identity"
            )
        group = SparkGroup(
            nodes=[
                SparkGroupNode(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    endpoint_owner=node.endpoint_owner,
                )
                for node in sorted(assignment.nodes, key=lambda item: item.rank)
            ]
        )
        return RunSwitchApplyRequest(
            model_content_sha256=model_digest,
            recipe_revision_id=assignment.recipe_revision_id,
            spark_group=group,
            alias=assignment.alias or assignment.recipe_title.lower().replace(" ", "-"),
            action="switch",
            retention="retain-cached",
            plan_digest=None,
            request_key=request_key,
        )

    def preparation(
        self,
        session: Session,
        assignment: FleetProfileAssignment,
        expected_nodes: tuple[str, ...],
    ) -> RolloutPreparation | None:
        """Project the exact preparation a profile apply would bind.

        Run/Switch remains the authority for the exact model artifact set, the
        runtime image identity and per-target readiness.  Asking it to plan is
        what keeps a profile preview honest: the preview shows the same
        preparation evidence the child operation will require, instead of a
        second, weaker estimate maintained at the profile boundary.
        """

        observed = tuple(sorted(node.node_id for node in assignment.nodes))
        if observed != expected_nodes:
            raise ValueError(
                "Profile assignment nodes changed during preparation projection."
            )
        request = self._assignment_request(session, assignment)
        plan = self._run_switch.preview(
            request, actor="controller:profile-preparation"
        )
        if plan.preparation is None:
            codes = sorted({reason.code for reason in plan.blockers})[:8]
            detail = (
                "The Run/Switch authority cannot attest exact model and OCI "
                f"preparation evidence for {assignment.recipe_title}"
            )
            if codes:
                detail += " (" + ", ".join(codes) + ")"
            raise ValueError(detail + ".")
        return plan.preparation

    def _advance(
        self,
        application_id: str,
        assignments: tuple[FleetProfileAssignment, ...],
    ) -> FleetProfileChildOperation:
        with self._sessions.begin() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            state = self._state(application)
            if state is None:
                raise KeyError(application_id)
            active = state.get("active_operation_id")
            position = state.get("position")
            if isinstance(active, str):
                try:
                    child = self._run_switch.get(active)
                except KeyError:
                    return self._failed_in_session(
                        session, application, state, "Run/Switch child is unavailable"
                    )
                if child.state in {"queued", "running"}:
                    view = self._view_from_child(application_id, state, child)
                    state["state"] = view.state
                    if view.progress is not None:
                        state["child_progress"] = view.progress.model_dump(mode="json")
                    self._write_state(session, application, state)
                    session.flush()
                    return view
                if child.state in {"failed", "cancelled"}:
                    reason = child.status_reason or (
                        f"Run/Switch child ended in {child.state}"
                    )
                    return self._failed_in_session(session, application, state, reason)
                if child.state != "succeeded":
                    return self._failed_in_session(
                        session,
                        application,
                        state,
                        f"Run/Switch child returned {child.state}",
                    )
                children = list(state.get("children", []))
                children.append(
                    {
                        "operation_id": child.operation_id,
                        "kind": state.get("active_kind"),
                        "state": child.state,
                    }
                )
                state["children"] = children
                state["active_operation_id"] = None
                state["active_kind"] = None
                state["position"] = int(position or 0) + 1
                self._write_state(session, application, state)
                session.flush()
                position = state["position"]
            queue = state.get("queue")
            if not isinstance(queue, list) or int(position or 0) >= len(queue):
                state["state"] = "succeeded"
                state["result"] = {
                    "children": list(state.get("children", [])),
                    "assignment_ids": list(state.get("assignment_ids", [])),
                }
                self._write_state(session, application, state)
                session.flush()
                return self._view_from_state(application, state)
            item = queue[int(position or 0)]
            if not isinstance(item, Mapping):
                return self._failed_in_session(
                    session,
                    application,
                    state,
                    "Persisted profile switch queue is invalid",
                )
            operation = self._start_child(
                application_id,
                item,
                assignments,
                tuple(str(node_id) for node_id in state["scope_node_ids"]),
                str(state["actor"]),
                str(state["request_id"]),
                int(position or 0),
            )
            state["active_operation_id"] = operation.operation_id
            state["active_kind"] = item.get("kind")
            self._write_state(session, application, state)
            session.flush()
            return self._view_from_child(application_id, state, operation)

    def _start_child(
        self,
        application_id: str,
        item: Mapping[str, object],
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
        actor: str,
        request_id: str,
        position: int,
    ) -> RunSwitchOperation:
        child_request_key = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "vonk-forge:profile-run-switch:"
                f"{application_id}:{position}:{item.get('kind')}:{item.get('id')}",
            )
        )
        kind = item.get("kind")
        if kind == "stop":
            run_id = item.get("id")
            if not isinstance(run_id, str):
                raise FleetProfileConflict(
                    "Profile switch stop item has no run identity"
                )
            preview = self._run_switch.preview_stop(
                RunSwitchStopPreviewRequest(run_id=run_id), actor=actor
            )
            return self._run_switch.apply_stop(
                RunSwitchStopApplyRequest(
                    run_id=run_id,
                    plan_digest=preview.plan_digest,
                    request_key=child_request_key,
                ),
                actor=actor,
            )
        assignment_id = item.get("id")
        assignment = next(
            (value for value in assignments if value.id == assignment_id), None
        )
        if assignment is None:
            raise FleetProfileConflict("Profile switch assignment is unavailable")
        with self._sessions() as session:
            request = self._assignment_request(
                session, assignment, request_key=child_request_key
            )
        plan = self._run_switch.preview(request, actor=actor)
        if not plan.allowed:
            raise RunSwitchOperationConflict(
                "profile child plan blocked: "
                + "; ".join(reason.code for reason in plan.blockers[:8])
            )
        return self._run_switch.apply(
            request.model_copy(update={"plan_digest": plan.plan_digest}), actor=actor
        )

    def _plan_queue(
        self,
        session: Session,
        assignments: tuple[FleetProfileAssignment, ...],
        scope_node_ids: tuple[str, ...],
    ) -> list[dict[str, object]]:
        scope = set(scope_node_ids)
        desired = {
            (
                assignment.recipe_revision_id,
                frozenset(node.node_id for node in assignment.nodes),
            )
            for assignment in assignments
        }
        preserved: set[tuple[str, frozenset[str]]] = set()
        stops: list[dict[str, object]] = []
        for run in session.scalars(
            select(RecipeRun)
            .where(RecipeRun.state.in_(_ACTIVE_RUN_STATES))
            .order_by(RecipeRun.created_at, RecipeRun.id)
        ):
            members = set(self._run_member_ids(session, run))
            if not members.intersection(scope):
                continue
            if not members <= scope:
                raise RunSwitchOperationConflict(
                    "profile switch contains a distributed run outside its "
                    "complete scope"
                )
            installation = session.get(RecipeInstallation, run.installation_id)
            identity = (
                installation.recipe_revision_id if installation is not None else "",
                frozenset(members),
            )
            if (
                self._run_is_healthy(session, run, members)
                and identity in desired
                and identity not in preserved
            ):
                preserved.add(identity)
                continue
            stops.append({"kind": "stop", "id": run.id})
        queue = stops
        for assignment in assignments:
            identity = (
                assignment.recipe_revision_id,
                frozenset(node.node_id for node in assignment.nodes),
            )
            if assignment.desired_state == "running" and identity not in preserved:
                queue.append({"kind": "run", "id": assignment.id})
        return queue

    @staticmethod
    def _run_member_ids(session: Session, run: RecipeRun) -> tuple[str, ...]:
        members = {
            node.node_id
            for node in session.scalars(select(RunNode).where(RunNode.run_id == run.id))
        }
        members.update(
            node.node_id
            for node in session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == run.installation_id
                )
            )
        )
        return tuple(sorted(members))

    @staticmethod
    def _run_is_healthy(
        session: Session, run: RecipeRun, members: set[str]
    ) -> bool:
        nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == run.id)
                .order_by(RunNode.rank)
            )
        )
        return (
            run.state == "running"
            and run.route_state == "published"
            and len(nodes) == len(members)
            and {node.node_id for node in nodes} == members
            and all(node.state == "running" for node in nodes)
        )

    @staticmethod
    def _state(application: FleetProfileApplication) -> dict[str, object] | None:
        try:
            progress = FleetProfileApplicationProgress.model_validate(
                application.progress
            )
        except ValidationError as error:
            raise FleetProfileConflict(
                "persisted profile application progress is invalid"
            ) from error
        raw = progress.switch_adapter
        if raw is None:
            return None
        return raw.model_dump(mode="python")

    def _save_state(self, application_id: str, state: Mapping[str, object]) -> None:
        with self._sessions.begin() as session:
            application = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if application is None:
                raise KeyError(application_id)
            self._write_state(session, application, state)

    @staticmethod
    def _write_state(
        session: Session,
        application: FleetProfileApplication,
        state: Mapping[str, object],
    ) -> None:
        typed = FleetProfileSwitchAdapterState.model_validate(state)
        progress = FleetProfileApplicationProgress.model_validate(application.progress)
        application.progress = {
            **progress.model_dump(mode="json"),
            "switch_adapter": typed.model_dump(mode="json"),
        }

    def _failed_in_session(
        self,
        session: Session,
        application: FleetProfileApplication,
        state: dict[str, object],
        reason: str,
    ) -> FleetProfileChildOperation:
        state["state"] = "failed"
        state["status_reason"] = reason[:512]
        self._write_state(session, application, state)
        session.flush()
        return self._view_from_state(application, state)

    @staticmethod
    def _assignments_from_state(
        state: Mapping[str, object],
    ) -> tuple[FleetProfileAssignment, ...]:
        raw = state.get("assignments")
        if not isinstance(raw, list):
            raise FleetProfileConflict(
                "profile switch child has no assignment snapshot"
            )
        try:
            values = tuple(FleetProfileAssignment.model_validate(item) for item in raw)
        except ValueError as error:
            raise FleetProfileConflict(
                "profile switch child assignment snapshot is invalid"
            ) from error
        return tuple(sorted(values, key=lambda item: item.id))

    def _view_from_child(
        self,
        application_id: str,
        state: Mapping[str, object],
        child: RunSwitchOperation,
    ) -> FleetProfileChildOperation:
        run_phase = child.current_phase or child.progress.phase or "prepare"
        phase = _PROFILE_PHASE_BY_RUN_PHASE.get(run_phase, run_phase)
        progress = FleetProfileChildProgress(
            operation=child.progress.operation,
            phase=phase,
            node_ids=list(state.get("scope_node_ids", [])),
            bytes=child.progress.completed_bytes,
            total_bytes=child.progress.total_bytes,
        )
        child_state = child.state if child.state in {
            "queued",
            "running",
            "waiting-for-operator",
            "failed",
            "succeeded",
            "cancelled",
        } else "running"
        result = (
            FleetProfileSwitchChildResult(
                run_switch_operation_id=child.operation_id,
                run_switch=RunSwitchOperationResult.model_validate(child.result),
            )
            if child.result is not None
            else None
        )
        return FleetProfileChildOperation(
            id=application_id,
            state=child_state,
            progress=progress,
            status_reason=child.status_reason,
            result=result,
        )

    @staticmethod
    def _view_from_state(
        application: FleetProfileApplication,
        state: Mapping[str, object],
    ) -> FleetProfileChildOperation:
        child_state = state.get("state")
        if child_state not in {
            "queued",
            "running",
            "waiting-for-operator",
            "failed",
            "succeeded",
            "cancelled",
        }:
            child_state = "running"
        raw_progress = state.get("child_progress")
        progress = (
            FleetProfileChildProgress.model_validate(raw_progress)
            if isinstance(raw_progress, Mapping)
            else FleetProfileChildProgress(
                phase="final-verify" if child_state == "succeeded" else "prepare",
                node_ids=list(state.get("scope_node_ids", [])),
            )
        )
        return FleetProfileChildOperation(
            id=application.id,
            state=child_state,
            progress=progress,
            status_reason=state.get("status_reason") if isinstance(state.get("status_reason"), str) else None,
            result=(
                FleetProfileSwitchAdapterResult.model_validate(state["result"])
                if isinstance(state.get("result"), Mapping)
                else None
            ),
        )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


def _choice_id(value: FleetProfileAssignmentInput) -> str:
    identity = ":".join(
        (
            value.recipe_selector,
            value.assignment_name or "",
            value.model_variant or "",
            value.desired_state,
            *value.spark_ids,
        )
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-forge:fleet-profile:{identity}"))


# Execution IDs remain deterministic across previews, while logical authoring
# changes (choice, variant, group or desired state) produce a new assignment.
_assignment_id = _choice_id


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
        "number": row.number,
        "revision": row.revision,
        "name": row.name,
        "description": row.description,
        "installation_policy": row.installation_policy,
        "labels": dict(row.labels),
        "favorite": row.favorite,
        "assignments": list(row.assignments),
    }


def build_production_fleet_profile_service(
    sessions: sessionmaker[Session],
    *,
    clock: Callable[[], datetime],
    run_switch_operations: RunSwitchOperationService,
    recipe_operations: RecipeOperationService | None = None,
    cache_resolver: Callable[..., Mapping[str, object]] | None = None,
) -> FleetProfileService:
    """Compose the Controller's Fleet profile service and its authority.

    The Run/Switch adapter is both the apply boundary and the preparation
    provider.  Composing them in one place means a profile preview and the
    child operation it later queues always resolve the exact same model,
    recipe revision, target group and runtime image identity.  It also makes
    the preparation binding impossible to drop by accident: a deployment
    cannot construct this service without a provider that can attest the
    assets a plan requires.
    """

    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch_operations)
    return FleetProfileService(
        sessions,
        clock=clock,
        recipe_operations=recipe_operations,
        switch_adapter=adapter,
        cache_resolver=cache_resolver,
        preparation_provider=adapter.preparation,
    )


class FleetProfileService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        recipe_operations: RecipeOperationService | None = None,
        switch_adapter: FleetProfileSwitchAdapter | None = None,
        cache_resolver: Callable[..., Mapping[str, object]] | None = None,
        preparation_provider: Callable[[
            Session, FleetProfileAssignment, tuple[str, ...]
        ], RolloutPreparation | None] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._recipe_operations = recipe_operations
        self._switch_adapter = switch_adapter
        self._cache_resolver = cache_resolver
        self._preparation_provider = preparation_provider

    @staticmethod
    def _next_profile_number(session: Session) -> int:
        """Allocate the next stable user profile number without renumbering."""

        maximum = session.scalar(select(func.max(FleetProfile.number)))
        return max(1, int(maximum or 0) + 1)

    @staticmethod
    def _recipe_document(
        session: Session, selector: str
    ) -> tuple[CatalogDocument, CatalogDocumentRevision]:
        """Resolve one exact recipe selector and its newest active revision."""

        normalized_selector = selector.strip().casefold()
        if normalized_selector.count("/") != 1:
            raise FleetProfileConflict(
                "recipe selector must use canonical publisher/slug form"
            )
        publisher, slug = normalized_selector.split("/", 1)
        candidates = tuple(
            session.scalars(
                select(CatalogDocument)
                .where(
                    CatalogDocument.kind == "recipe",
                    CatalogDocument.publisher == publisher,
                    CatalogDocument.slug == slug,
                )
                .order_by(CatalogDocument.publisher, CatalogDocument.slug, CatalogDocument.id)
            )
        )
        if len(candidates) != 1:
            raise FleetProfileConflict(
                "recipe selector is not an exact unique active recipe"
            )
        document = candidates[0]
        revision = session.scalar(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.document_id == document.id,
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
            .order_by(
                CatalogDocumentRevision.revision_number.desc(),
                CatalogDocumentRevision.created_at.desc(),
                CatalogDocumentRevision.id.desc(),
            )
            .limit(1)
        )
        if revision is None:
            raise FleetProfileConflict("recipe has no active catalog revision")
        return document, revision

    @staticmethod
    def _recipe_selector(document: CatalogDocument) -> str:
        return f"{document.publisher}/{document.slug}"

    def _resolve_choice(
        self, session: Session, choice: FleetProfileAssignmentInput
    ) -> tuple[CatalogDocument, CatalogDocumentRevision, Mapping[str, object] | None]:
        """Resolve one choice once for both presentation and execution."""

        document, revision = self._recipe_document(session, choice.recipe_selector)
        cache: Mapping[str, object] | None = None
        if self._cache_resolver is not None:
            try:
                candidate = self._cache_resolver(
                    recipe_identity=document.id,
                    model_variant=choice.model_variant,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                raise FleetProfileConflict("latest cached profile resolution failed") from error
            if not isinstance(candidate, Mapping):
                raise FleetProfileConflict("latest cached profile resolution returned an invalid contract")
            cache = candidate
            recipe_part = candidate.get("recipe")
            chosen_id = (
                recipe_part.get("recipe_revision_id")
                if isinstance(recipe_part, Mapping)
                else None
            )
            if isinstance(chosen_id, str) and chosen_id != revision.id:
                chosen = session.get(CatalogDocumentRevision, chosen_id)
                if (
                    chosen is None
                    or chosen.kind != "recipe"
                    or chosen.state != "active"
                    or chosen.document_id != document.id
                ):
                    raise FleetProfileConflict("cache resolver returned an incompatible recipe revision")
                revision = chosen
        return document, revision, cache

    @staticmethod
    def _assignment_selector(choice: FleetProfileAssignmentInput) -> str:
        if choice.assignment_name is not None:
            return choice.assignment_name
        value = choice.recipe_selector.lower().replace("/", "-").replace(" ", "-")
        value = "".join(character if character.isalnum() or character in "-_." else "-" for character in value)
        value = value.strip("-_.") or "assignment"
        return value[:63].rstrip("-_.") or "assignment"

    @staticmethod
    def _choices(row: FleetProfile) -> tuple[FleetProfileAssignmentInput, ...]:
        try:
            return tuple(_STORED_ASSIGNMENTS.validate_json(canonical_message(row.assignments)))
        except (TypeError, ValueError, ValidationError) as error:
            raise FleetProfileConflict("persisted Fleet profile choices are invalid") from error

    def _execution_assignments(
        self, session: Session, row: FleetProfile
    ) -> tuple[FleetProfileAssignment, ...]:
        """Resolve logical choices into strict, load-bound assignments.

        A choice may be an incomplete distributed draft.  The generated rank
        mapping remains deterministic so preview can report the topology
        blocker; it is never submitted unless the recipe's exact topology
        accepts the complete group.
        """

        result: list[FleetProfileAssignment] = []
        for choice in self._choices(row):
            document, revision, _cache = self._resolve_choice(session, choice)
            topology = recipe_topology(revision.document)
            roles = _expanded_roles(topology)
            nodes = [
                FleetProfileNode(
                    node_id=node_id,
                    rank=rank,
                    role=roles[rank][0] if rank < len(roles) else "unresolved",
                    endpoint_owner=(roles[rank][1] if rank < len(roles) else rank == 0),
                )
                for rank, node_id in enumerate(choice.spark_ids)
            ]
            alias = choice.assignment_name
            if choice.desired_state == "running" and alias is None:
                alias = self._assignment_selector(choice)
            result.append(
                FleetProfileAssignment(
                    id=_choice_id(choice),
                    recipe_revision_id=revision.id,
                    topology_name=str(topology.get("name", "unresolved")),
                    desired_state=choice.desired_state,
                    alias=alias,
                    nodes=nodes,
                    recipe_id=document.id,
                    recipe_title=document.title,
                    model_title=self._model_title(session, revision.document),
                )
            )
        return tuple(result)

    def list(self) -> FleetProfileList:
        now = _aware(self._clock())
        with self._sessions() as session:
            rows = tuple(
                session.scalars(
                    select(FleetProfile)
                    .order_by(
                        FleetProfile.number.asc()
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

    def get_number(self, number: int) -> FleetProfileView:
        if type(number) is not int or number < 1:
            raise KeyError(number)
        with self._sessions() as session:
            row = session.scalar(select(FleetProfile).where(FleetProfile.number == number))
            if row is None:
                raise KeyError(number)
            return self._view(session, row)

    def read_number(self, number: int) -> FleetProfileView:
        """Read a stable unused number without creating persistent state."""

        try:
            return self.get_number(number)
        except KeyError:
            if type(number) is not int or number < 1:
                raise
            now = _aware(self._clock())
            with self._sessions() as session:
                roster = tuple(
                    session.scalars(
                        select(AgentNode)
                        .where(AgentNode.revoked_at.is_(None))
                        .order_by(AgentNode.node_id)
                    )
                )
                display_names = {
                    item.node_id: item.display_name
                    for item in session.scalars(
                        select(AgentNodeProfile).where(
                            AgentNodeProfile.node_id.in_(
                                [node.node_id for node in roster]
                            )
                        )
                    )
                }
            fleet = [
                {
                    "selector": node.node_id,
                    "display_name": display_names.get(node.node_id, node.node_id),
                    "state": "Idle",
                }
                for node in roster
            ]
            document = {"schema_version": 2, "number": number, "revision": 1, "assignments": []}
            return FleetProfileView(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-forge:profile:{number}")),
                number=number,
                revision=1,
                name="Default" if number == 1 else f"Profile {number}",
                description="",
                installation_policy="keep-cached",
                labels={},
                favorite=False,
                assignments=[],
                fleet=fleet,
                status="not-created",
                warnings=["Profile has not been created; the first edit will save it"],
                next_actions=[f"vonkctl --profile {number} profile add RECIPE --spark SPARK"],
                profile_digest=_digest(document),
                created_by="uncreated",
                created_at=now,
                updated_at=now,
            )

    def create(
        self, value: FleetProfileInput, *, actor: str, number: int | None = None
    ) -> FleetProfileView:
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            assignments = self._validated_assignments(session, value.assignments)
            row = FleetProfile(
                number=number if number is not None else self._next_profile_number(session),
                revision=1,
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
            if value.expected_revision is not None and row.revision != value.expected_revision:
                raise FleetProfileConflict(
                    f"profile revision conflict: expected {value.expected_revision}, current {row.revision}"
                )
            assignments = self._validated_assignments(session, value.assignments)
            row.name = value.name
            row.description = value.description
            row.installation_policy = value.installation_policy
            row.labels = dict(value.labels)
            row.favorite = value.favorite
            row.revision += 1
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

    def update_number(
        self, number: int, value: FleetProfileInput, *, actor: str
    ) -> FleetProfileView:
        """Autosave a numbered profile with optimistic concurrency."""

        with self._sessions() as session:
            row = session.scalar(select(FleetProfile).where(FleetProfile.number == number))
        if row is None:
            if type(number) is not int or number < 1:
                raise KeyError(number)
            return self.create(value, actor=actor, number=number)
        return self.update(row.id, value, actor=actor)

    def load(
        self,
        number: int,
        *,
        actor: str,
        request_key: str,
    ) -> FleetProfileApplicationView:
        """Preview and bind one whole-fleet execution in one operator action."""

        profile = self.get_number(number)
        preview = self.preview(profile.id)
        if not preview.allowed:
            raise FleetProfileConflict("Fleet profile preview is blocked")
        return self._queue_application(
            preview,
            request_key=request_key,
            actor=actor,
            operation_kind="fleet-profile.apply",
        )

    def progress_number(self, number: int) -> FleetProfileApplicationView:
        profile = self.get_number(number)
        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfileApplication)
                .where(FleetProfileApplication.profile_id == profile.id)
                .order_by(
                    FleetProfileApplication.created_at.desc(),
                    FleetProfileApplication.id.desc(),
                )
                .limit(1)
            )
            if row is None:
                raise KeyError(number)
            return self._application_view(row)

    def preview(
        self,
        profile_id: str,
        *,
        execution_assignments: tuple[FleetProfileAssignment, ...] | None = None,
        target_node_ids: tuple[str, ...] | None = None,
        profile_name: str | None = None,
        profile_digest: str | None = None,
    ) -> FleetProfilePreview:
        now = _aware(self._clock())
        with self._sessions() as session:
            row = session.get(FleetProfile, profile_id)
            if row is None and execution_assignments is None:
                raise KeyError(profile_id)
            if row is not None:
                view = self._view(session, row)
                resolved_assignments = self._execution_assignments(session, row)
                resolved_name = view.name
                resolved_digest = view.profile_digest
            else:
                resolved_assignments = execution_assignments
                resolved_name = profile_name or "Direct placement"
                resolved_digest = profile_digest or _digest(
                    {"profile_id": profile_id, "assignments": [item.model_dump(mode="json") for item in resolved_assignments]}
                )
            assignment_previews: list[FleetProfileAssignmentPreview] = []
            assignment_preparations: list[FleetProfileAssignmentPreparation] = []
            reasons: list[FleetProfileReason] = []
            roster = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
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
            target_nodes = (
                set(target_node_ids)
                if target_node_ids is not None
                else {node.node_id for node in roster}
            )

            for assignment in resolved_assignments:
                state = self._assignment_state(session, assignment)
                preparation = None
                expected_nodes = tuple(sorted(node.node_id for node in assignment.nodes))
                item_reasons: list[FleetProfileReason] = []
                # Exact preparation evidence is only required when this
                # assignment must actually place the model and runtime image.
                # A profile that already matches live state legitimately has
                # nothing to prepare, so unavailable evidence stays a warning
                # there rather than blocking an otherwise idle plan.
                requires_preparation = state.installation is None or (
                    state.current_state in {"not-placed", "placed", "degraded"}
                )
                active_node_ids = {node.node_id for node in roster}
                unknown_nodes = sorted(set(expected_nodes) - active_node_ids)
                revision = session.get(
                    CatalogDocumentRevision, assignment.recipe_revision_id
                )
                required_count = None
                if revision is not None:
                    topology = recipe_topology(revision.document)
                    value = topology.get("node_count")
                    required_count = value if type(value) is int else None
                if unknown_nodes:
                    item_reasons.append(
                        FleetProfileReason(
                            code="profile.spark_unavailable",
                            detail=(
                                "Assignment references inactive or unknown Sparks: "
                                + ", ".join(unknown_nodes)
                            ),
                            severity="error",
                        )
                    )
                if required_count is None or len(assignment.nodes) != required_count:
                    item_reasons.append(
                        FleetProfileReason(
                            code="profile.topology_incomplete",
                            detail=(
                                f"Recipe requires {required_count or 'a valid'} Sparks; "
                                f"the draft assigns {len(assignment.nodes)}."
                            ),
                            severity="error",
                        )
                    )
                if self._preparation_provider is None:
                    if not preparation_unavailable_reported:
                        reasons.append(
                            FleetProfileReason(
                                code="profile.preparation_unavailable",
                                detail=(
                                    "Exact model and OCI preparation evidence is "
                                    f"unavailable for {assignment.recipe_title}; "
                                    "the Controller has no preparation provider "
                                    "configured."
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
                                severity=(
                                    "error" if requires_preparation else "warning"
                                ),
                            )
                        )
                    if preparation is not None and not isinstance(
                        preparation, RolloutPreparation
                    ):
                        reasons.append(
                            FleetProfileReason(
                                code="profile.preparation_unavailable",
                                detail="The preparation provider returned an invalid contract.",
                                severity=(
                                    "error" if requires_preparation else "warning"
                                ),
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
            if self._switch_adapter is not None and not resolved_assignments:
                # An empty assignment set is an explicit all-idle outcome.  If
                # the scope currently contains a run, route reconciliation
                # through the composite child so Run/Switch can stop the
                # complete distributed group exactly once.
                for run in active_runs:
                    members = set(run_nodes.get(run.id, ()))
                    members.update(
                        self._installation_node_ids(session, run.installation_id)
                    )
                    if members & target_nodes:
                        adapter_switch_needed = True
                        break
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
                if self._switch_adapter is not None and adapter_switch_needed:
                    # The composite child reconciles every runtime in its complete
                    # profile scope. Keep cross-scope validation above, but do not
                    # enqueue a second outer stop for the same conflict.
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

            installation_policy = row.installation_policy if row is not None else "keep-cached"
            if installation_policy == "exact" and target_nodes:
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
                        if (
                            self._switch_adapter is not None
                            and adapter_switch_needed
                        ):
                            continue
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
                        "label": f"Switch profile {resolved_name}",
                    }
                )
            # Prepare images and installations while the current profile can
            # still serve traffic. Required stops then release runtime
            # resources before final starts and verification.
            if self._switch_adapter is not None and adapter_switch_needed:
                raw_steps = (
                    preparation_steps
                    + stop_steps
                    + switch_steps
                    + uninstall_steps
                    + start_steps
                )
            else:
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
                "profile_id": profile_id,
                "profile_digest": resolved_digest,
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
                profile_id=profile_id,
                profile_name=resolved_name,
                profile_digest=resolved_digest,
                generated_at=now,
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
        retry_of_application_id: str | None = None,
    ) -> FleetProfileApplicationView:
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            profile = session.get(FleetProfile, preview.profile_id, with_for_update=True)
            if profile is None:
                raise KeyError(preview.profile_id)
            existing = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if existing is not None:
                existing_progress = FleetProfileApplicationProgress.model_validate(existing.progress)
                if (
                    existing.profile_id != preview.profile_id
                    or existing_progress.retry_of_application_id != retry_of_application_id
                    or (retry_of_application_id is None and existing.plan_digest != preview.plan_digest)
                ):
                    raise FleetProfileConflict(
                        "Fleet profile request key was reused for another plan"
                    )
                return self._application_view(existing)
            intended_view = self._view(session, profile)
            frozen_assignments = self._execution_assignments(session, profile)
            frozen_nodes = tuple(
                node.node_id
                for node in session.scalars(
                    select(AgentNode)
                    .where(AgentNode.revoked_at.is_(None))
                    .order_by(AgentNode.node_id)
                )
            )
            intended = FleetProfileIntendedConfiguration(
                profile_digest=intended_view.profile_digest,
                installation_policy=profile.installation_policy,
                scope=FleetProfileScope(node_ids=list(frozen_nodes)),
                assignments=list(frozen_assignments),
            )
            if intended.profile_digest != preview.profile_digest:
                raise FleetProfileConflict("Fleet profile changed during application admission")
            attempt = 1
            if retry_of_application_id is not None:
                parent = session.get(FleetProfileApplication, retry_of_application_id, with_for_update=True)
                if parent is None:
                    raise KeyError(retry_of_application_id)
                prior = FleetProfileApplicationProgress.model_validate(parent.progress)
                if parent.state not in {"failed", "waiting-for-operator"}:
                    raise FleetProfileConflict("Only failed or waiting applications can be retried")
                if parent.profile_digest != intended.profile_digest:
                    raise FleetProfileConflict("Application intent is obsolete because the saved profile changed")
                profile_filter = (
                    FleetProfileApplication.profile_id.is_(None)
                    if parent.profile_id is None
                    else FleetProfileApplication.profile_id == parent.profile_id
                )
                applications = session.scalars(select(FleetProfileApplication).where(
                    profile_filter,
                    FleetProfileApplication.id != parent.id,
                ))
                for other in applications:
                    other_progress = FleetProfileApplicationProgress.model_validate(other.progress)
                    if (other_progress.retry_of_application_id == parent.id
                            or other.state in {"queued", "running"}
                            or _aware(other.created_at) > _aware(parent.created_at)):
                        raise FleetProfileConflict("Application has been superseded by another application")
                if prior.intended_profile is None:
                    raise FleetProfileConflict("Persisted application intent is unavailable")
                intended = prior.intended_profile
                attempt = prior.attempt + 1
            row = FleetProfileApplication(
                request_key=request_key,
                profile_id=preview.profile_id,
                profile_digest=preview.profile_digest,
                plan_digest=preview.plan_digest,
                state="succeeded" if not preview.steps else "queued",
                plan=preview.model_dump(mode="json"),
                current_step=0,
                current_operation_id=None,
                progress=FleetProfileApplicationProgress(
                    operation_kind=operation_kind,
                    attempt=attempt,
                    retry_of_application_id=retry_of_application_id,
                    intended_profile=intended,
                    completed_steps=0,
                    total_steps=len(preview.steps),
                ).model_dump(mode="json"),
                result=(
                    {"changed": False, "completed_steps": 0}
                    if not preview.steps
                    else None
                ),
                actor=actor,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return self._application_view(row)

    def retry_eligible(self, application_id: str) -> bool:
        """Whether this receipt is still the current recoverable profile intent."""
        with self._sessions() as session:
            row = session.get(FleetProfileApplication, application_id)
            return row is not None and self._retry_eligible(session, row)

    def _retry_eligible(self, session: Session, row: FleetProfileApplication) -> bool:
        if row.state not in {"failed", "waiting-for-operator"}:
            return False
        progress = FleetProfileApplicationProgress.model_validate(row.progress)
        profile = session.get(FleetProfile, row.profile_id)
        if (
            progress.intended_profile is None
            or profile is None
            or self._view(session, profile).profile_digest != row.profile_digest
        ):
            return False
        others = session.scalars(select(FleetProfileApplication).where(
            FleetProfileApplication.profile_id == row.profile_id,
            FleetProfileApplication.id != row.id,
        ))
        return not any(
            FleetProfileApplicationProgress.model_validate(other.progress).retry_of_application_id == row.id
            or other.state in {"queued", "running"}
            or _aware(other.created_at) > _aware(row.created_at)
            for other in others
        )

    def retry(
        self, application_id: str, *, request_key: str, actor: str
    ) -> FleetProfileApplicationView:
        """Persist a new reconciliation attempt, retaining the original receipt."""
        with self._sessions() as session:
            replay = session.scalar(select(FleetProfileApplication).where(
                FleetProfileApplication.request_key == request_key
            ))
            if replay is not None:
                progress = FleetProfileApplicationProgress.model_validate(replay.progress)
                if progress.retry_of_application_id != application_id:
                    raise FleetProfileConflict("Retry request key was reused for another application")
                return self._application_view(replay)
            parent = session.get(FleetProfileApplication, application_id)
            if parent is None:
                raise KeyError(application_id)
            progress = FleetProfileApplicationProgress.model_validate(parent.progress)
            if parent.state not in {"failed", "waiting-for-operator"}:
                raise FleetProfileConflict("Only failed or waiting applications can be retried")
            if progress.intended_profile is None:
                raise FleetProfileConflict("Persisted application intent is unavailable")
            if parent.current_operation_id is not None:
                provider = self._switch_adapter if progress.child_source == "switch-adapter" else self._recipe_operations
                if provider is None:
                    raise FleetProfileConflict("Current child operation authority is unavailable")
                try:
                    child = provider.get(parent.current_operation_id)
                except (KeyError, RuntimeError, ValueError) as error:
                    raise FleetProfileConflict("Current child operation state must be reconciled before retry") from error
                if child.state not in {"succeeded", "failed", "cancelled", "expired", "waiting-for-operator"}:
                    raise FleetProfileConflict("Current child operation is still active")
            operation_kind = progress.operation_kind or "fleet-profile.apply"
            if operation_kind != "fleet-profile.apply":
                raise FleetProfileConflict("Only current profile loads can be recovered")
            profile_digest = parent.profile_digest
            persisted_plan = _persisted_profile_plan(parent)
        preview = self.preview(persisted_plan.profile_id)
        if preview.profile_digest != profile_digest:
            raise FleetProfileConflict("Application intent is obsolete because the saved profile changed")
        if not preview.allowed:
            raise FleetProfileConflict("Current Fleet state blocks application recovery")
        # Attempt identity is distinct even when the remaining work is unchanged.
        preview = preview.model_copy(update={"plan_digest": _digest({
            "schema_version": 2, "reconciliation_digest": preview.plan_digest,
            "retry_of_application_id": application_id, "request_key": request_key,
        })})
        return self._queue_application(preview, request_key=request_key, actor=actor,
                                       operation_kind=operation_kind,
                                       retry_of_application_id=application_id)

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
                    items=[self._operation_item(row, retry_available=self._retry_eligible(session, row)) for row in rows],
                    next_cursor=None,
                    total=total,
                )

        def get_operation(operation_id: str) -> Mapping[str, object]:
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, operation_id)
                if row is None:
                    raise KeyError(operation_id)
                return self._operation_item(row, retry_available=self._retry_eligible(session, row))

        return OperationProvider(
            family="fleet-profile",
            list_operations=list_operations,
            get_operation=get_operation,
        )

    @staticmethod
    def _operation_scope(row: FleetProfileApplication) -> tuple[str, ...]:
        return tuple(_persisted_profile_plan(row).scope.node_ids)

    @classmethod
    def _operation_phase(cls, row: FleetProfileApplication) -> str:
        if row.state == "succeeded":
            return "final_verify"
        plan = _persisted_profile_plan(row)
        if 0 <= row.current_step < len(plan.steps):
            kind = plan.steps[row.current_step].kind
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
                progress = FleetProfileApplicationProgress.model_validate(row.progress)
                if progress.child_progress is not None:
                    return progress.child_progress.phase
                return "prepare"
        return "final_verify"

    @classmethod
    def _operation_item(
        cls, row: FleetProfileApplication, *, retry_available: bool = False
    ) -> dict[str, object]:
        """Project profile progress and its operator-visible failure into Activity."""

        typed_progress = FleetProfileApplicationProgress.model_validate(row.progress)
        progress = {"phase": cls._operation_phase(row)}
        operation_kind = typed_progress.operation_kind
        failure = None
        if row.state in {"failed", "waiting-for-operator"}:
            if not row.status_reason or not row.status_reason.strip():
                raise FleetProfileConflict("Profile application failure reason is missing")
            failure = OperationFailureEvidence(
                error_code="fleet_profile_application_failed",
                summary=(
                    "Profile application needs attention"
                    if row.state == "waiting-for-operator"
                    else "Profile application failed"
                ),
                detail=redact_text(row.status_reason),
                retryable=retry_available,
                uncertain=row.state == "waiting-for-operator",
            ).model_dump(mode="json")
        return {
            "id": row.id,
            "parent_id": typed_progress.retry_of_application_id,
            "node_ids": list(cls._operation_scope(row)),
            "kind": operation_kind or "fleet-profile.apply",
            "state": row.state,
            "attempt": typed_progress.attempt,
            "progress": progress,
            "created_at": _aware(row.created_at).isoformat(),
            "updated_at": _aware(row.updated_at).isoformat(),
            "supported_actions": ["retry"] if retry_available else [],
            "failure": failure,
            "result": (
                result.model_dump(mode="json")
                if (result := _persisted_profile_result(row)) is not None
                else None
            ),
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
            try:
                plan = _persisted_profile_plan(row)
                progress = _persisted_profile_progress(row)
            except FleetProfileConflict as error:
                row.state = "failed"
                row.status_reason = str(error)[:512]
                row.updated_at = now
                return True
            steps = [step.model_dump(mode="json") for step in plan.steps]
            if row.current_operation_id:
                try:
                    child_source = progress.child_source
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
                    progress_data = progress.model_dump(mode="json")
                    if child.progress is not None:
                        progress_data["child_progress"] = child.progress.model_dump(
                            mode="json"
                        )
                    progress = FleetProfileApplicationProgress.model_validate_json(
                        canonical_message(progress_data), strict=True
                    )
                    row.progress = progress.model_dump(mode="json")
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
                progress_data = progress.model_dump(mode="json")
                results = dict(progress_data.get("step_results", {}))
                child_result = FleetProfileStepResult(
                    operation_id=child.id,
                    result=(
                        child.result
                        if isinstance(child, FleetProfileChildOperation)
                        else None
                    ),
                )
                if not isinstance(child, FleetProfileChildOperation):
                    child_result = child_result.model_copy(
                        update={"owner_id": child.owner_id, "kind": child.kind}
                    )
                results[str(row.current_step)] = child_result.model_dump(mode="json")
                progress_data["step_results"] = results
                progress = FleetProfileApplicationProgress.model_validate_json(
                    canonical_message(progress_data), strict=True
                )
                row.progress = progress.model_dump(mode="json")
                row.current_operation_id = None
                row.current_step += 1
            if row.current_step >= len(steps):
                row.state = "succeeded"
                row.status_reason = None
                progress = FleetProfileApplicationProgress.model_validate_json(
                    canonical_message(
                        {
                            **progress.model_dump(mode="json"),
                            "completed_steps": len(steps),
                            "total_steps": len(steps),
                        }
                    ),
                    strict=True,
                )
                row.progress = progress.model_dump(mode="json")
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
            progress = FleetProfileApplicationProgress.model_validate_json(
                canonical_message(
                    {
                        **progress.model_dump(mode="json"),
                        "completed_steps": row.current_step,
                        "total_steps": len(steps),
                        "current_label": raw_step.get("label", "Applying profile"),
                    }
                ),
                strict=True,
            )
            row.progress = progress.model_dump(mode="json")
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
                    failed.status_reason = str(error)[:512] or "Profile operation could not be started"
                    failed.updated_at = _aware(self._clock())
            return True
        with self._sessions.begin() as session:
            current = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if current is None or current.state not in {"queued", "running"}:
                return True
            try:
                current_progress = _persisted_profile_progress(current)
                progress_data = current_progress.model_dump(mode="json")
                if synchronous:
                    current.current_step += 1
                    progress_data["completed_steps"] = current.current_step
                else:
                    current.current_operation_id = operation_id
                    progress_data["child_source"] = (
                        "switch-adapter" if step.get("kind") == "switch" else "recipe"
                    )
                    if (
                        isinstance(child, FleetProfileChildOperation)
                        and child.progress is not None
                    ):
                        progress_data["child_progress"] = child.progress.model_dump(
                            mode="json"
                        )
                current.progress = FleetProfileApplicationProgress.model_validate_json(
                    canonical_message(progress_data), strict=True
                ).model_dump(mode="json")
            except FleetProfileConflict as error:
                current.state = "failed"
                current.status_reason = str(error)[:512]
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
        for value in values:
            document, _revision = self._recipe_document(session, value.recipe_selector)
            # Save the canonical catalog selector.  This is a logical recipe
            # choice; its active revision is deliberately resolved later.
            normalized = value.model_copy(
                update={"recipe_selector": self._recipe_selector(document)}
            )
            assignments.append(json.loads(canonical_message(normalized)))
        return assignments

    def _view(self, session: Session, row: FleetProfile) -> FleetProfileView:
        choices = self._choices(row)
        assignments: list[FleetProfileAssignmentView] = []
        assigned_nodes = {node_id for choice in choices for node_id in choice.spark_ids}
        cache_summary: dict[str, object] = {"cached": 0, "missing": 0, "unknown": 0}
        warnings: list[str] = []
        for choice in choices:
            recipe, revision, cache = self._resolve_choice(session, choice)
            topology = recipe_topology(revision.document)
            required_sparks = topology.get("node_count")
            required = required_sparks if type(required_sparks) is int else None
            if self._cache_resolver is None:
                warnings.append("Cache resolution is unavailable until the cache service is configured")
            recipe_state = "Cached" if cache and bool(cache.get("recipe", {}).get("cached")) else "Recipe not cached"
            model_state = "Cached" if cache and bool(cache.get("model", {}).get("cached")) else "Model not cached"
            if cache is None:
                cache_summary["unknown"] = int(cache_summary["unknown"]) + 1
            elif recipe_state == "Cached" and model_state == "Cached":
                cache_summary["cached"] = int(cache_summary["cached"]) + 1
            else:
                cache_summary["missing"] = int(cache_summary["missing"]) + 1
            resources = dict(cache.get("resources", {})) if cache else {}
            model_document = resolve_recipe_entities(session, revision.document).get("models", ())
            model = model_document[0] if model_document else None
            model_selector = None
            model_name = None
            if model is not None:
                model_selector = f"{model.publisher}/{model.slug}"
                model_name = self._model_title(session, revision.document)
            assignment_selector = self._assignment_selector(choice)
            assignments.append(
                FleetProfileAssignmentView(
                    selector=assignment_selector,
                    display_name=recipe.title,
                    recipe_selector=f"{recipe.publisher}/{recipe.slug}",
                    recipe_id=recipe.id,
                    spark_ids=list(choice.spark_ids),
                    required_sparks=required,
                    assigned_sparks=len(choice.spark_ids),
                    model={
                        "selector": model_selector,
                        "name": model_name,
                        "variant": choice.model_variant,
                        "state": model_state,
                        "content_sha256": cache.get("model", {}).get("content_sha256") if cache else None,
                    },
                    recipe={
                        "selector": f"{recipe.publisher}/{recipe.slug}",
                        "name": recipe.title,
                        "state": recipe_state,
                        "revision_id": revision.id,
                    },
                    resources=resources,
                    observed_state="Not loaded",
                )
            )
        roster = tuple(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.revoked_at.is_(None))
                .order_by(AgentNode.node_id)
            )
        )
        display_names = {
            item.node_id: item.display_name
            for item in session.scalars(
                select(AgentNodeProfile).where(
                    AgentNodeProfile.node_id.in_([node.node_id for node in roster])
                )
            )
        }
        fleet = [
            {
                "selector": node.node_id,
                "display_name": display_names.get(node.node_id, node.node_id),
                "state": "Assigned" if node.node_id in assigned_nodes else "Idle",
            }
            for node in roster
        ]
        document = _profile_document(row)
        return FleetProfileView(
            id=row.id,
            number=row.number,
            revision=row.revision,
            name=row.name,
            description=row.description,
            installation_policy=row.installation_policy,
            labels=dict(row.labels),
            favorite=row.favorite,
            assignments=assignments,
            fleet=fleet,
            status="draft",
            cache_summary=cache_summary,
            warnings=sorted(set(warnings)),
            next_actions=[f"vonkctl --profile {row.number} profile load"],
            profile_digest=_digest(document),
            created_by=row.created_by,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _model_title(session: Session, document: Mapping[str, object]) -> str | None:
        try:
            models = resolve_recipe_entities(session, document).get("models")
        except (KeyError, RuntimeError, TypeError, ValueError):
            return None
        if not isinstance(models, Sequence) or not models:
            return None
        model = models[0]
        root = session.get(CatalogDocument, model.document_id)
        return root.title if root is not None else f"{model.publisher}/{model.slug}"

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
        plan = _persisted_profile_plan(row)
        progress = FleetProfileApplicationProgress.model_validate(row.progress)
        if row.state in {"queued", "running"} and progress.child_progress and progress.child_progress.operation:
            progress.child_progress.operation = project_progress(progress.child_progress.operation, _aware(self._clock()))
        return FleetProfileApplicationView(
            id=row.id,
            profile_id=row.profile_id,
            profile_digest=row.profile_digest,
            plan_digest=row.plan_digest,
            state=row.state,
            attempt=FleetProfileApplicationProgress.model_validate(row.progress).attempt,
            retry_of_application_id=FleetProfileApplicationProgress.model_validate(row.progress).retry_of_application_id,
            current_step=row.current_step,
            total_steps=len(plan.steps),
            current_operation_id=row.current_operation_id,
            status_reason=row.status_reason,
            progress=progress,
            result=_persisted_profile_result(row),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    @staticmethod
    def _intended_profile(application: FleetProfileApplication) -> FleetProfileIntendedConfiguration:
        progress = FleetProfileApplicationProgress.model_validate(application.progress)
        if progress.intended_profile is None:
            raise FleetProfileConflict("Persisted application intent is unavailable")
        if progress.intended_profile.profile_digest != application.profile_digest:
            raise FleetProfileConflict("Persisted application intent digest is inconsistent")
        return progress.intended_profile

    def _application_assignment(
        self, application_id: str, assignment_id: object
    ) -> FleetProfileAssignment | None:
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            return next(
                (
                    item
                    for item in self._intended_profile(application).assignments
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
            assignments = self._intended_profile(application).assignments
            return tuple(sorted(assignments, key=lambda item: item.id))

    def _assignment_context(
        self, application_id: str, assignment_id: str
    ) -> dict[str, object]:
        with self._sessions() as session:
            application = session.get(FleetProfileApplication, application_id)
            if application is None:
                raise KeyError(application_id)
            progress = FleetProfileApplicationProgress.model_validate(
                application.progress
            )
            value = progress.assignments.get(assignment_id)
            return value.model_dump(mode="python") if value is not None else {}

    def _store_assignment_context(
        self, application_id: str, assignment_id: str, value: Mapping[str, object]
    ) -> None:
        with self._sessions.begin() as session:
            application = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if application is None:
                raise KeyError(application_id)
            progress = FleetProfileApplicationProgress.model_validate(
                application.progress
            ).model_dump(mode="python")
            assignments = (
                dict(progress.get("assignments", {}))
                if isinstance(progress.get("assignments"), Mapping)
                else {}
            )
            assignments[assignment_id] = FleetProfileAssignmentContext.model_validate(
                value
            ).model_dump(mode="json")
            progress["assignments"] = assignments
            application.progress = FleetProfileApplicationProgress.model_validate(
                progress
            ).model_dump(mode="json")
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


__all__ = [
    "FleetProfileConflict",
    "FleetProfileService",
    "RunSwitchFleetProfileAdapter",
]
