"""Typed ownership for profile-authorized one-shot JobRun cleanup Stops."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    RunState,
    SecurityRefusalError,
    canonical_message,
)
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import RecipeStopPayload

from . import job_states
from .categorized_errors import BookkeepingUnknown
from .integer_domains import MAX_DATABASE_BIGINT, MAX_DATABASE_INTEGER
from .models import (
    AgentNode,
    AgentOperation,
    ArtifactJob,
    ClusterMappingNode,
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .recipe_stop_payloads import stop_payload_from_job_run
from .strict_json import StrictJSONModel, read_stored_model

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_NODE_ID = re.compile(r"^spk_[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProfileJobRunStopTarget(StrictJSONModel):
    """One exact transient runtime and its durable JobRun source."""

    artifact_job_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    source_job_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    source_operation_id: str = Field(
        min_length=36, max_length=36, pattern=_UUID.pattern
    )
    node_id: str = Field(min_length=36, max_length=36, pattern=_NODE_ID.pattern)
    stop_payload_sha256: str = Field(
        min_length=64, max_length=64, pattern=_SHA256.pattern
    )


class RunStopScope(StrictJSONModel):
    """An exact run and complete membership under its accepted Stop intent."""

    schema_version: Literal[1]
    run_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    installation_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    recipe_revision_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    mapping_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    mapping_generation: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    run_generation: int = Field(le=MAX_DATABASE_BIGINT, ge=1)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=_SHA256.pattern)
    workload_intent_ordinal: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    run_node_ids: list[str] = Field(min_length=1, max_length=32)
    reachable_node_ids: list[str] = Field(min_length=1, max_length=32)
    missing_node_ids: list[str] = Field(default_factory=list, max_length=31)
    stop_plan_digest: str = Field(min_length=64, max_length=64, pattern=_SHA256.pattern)

    @model_validator(mode="after")
    def membership_is_exact(self) -> RunStopScope:
        nodes, reachable, missing = (
            self.run_node_ids,
            self.reachable_node_ids,
            self.missing_node_ids,
        )
        if (
            nodes != list(dict.fromkeys(nodes))
            or reachable != sorted(set(reachable))
            or missing != sorted(set(missing))
            or set(reachable) & set(missing)
            or set(reachable) | set(missing) != set(nodes)
            or (bool(missing) and len(nodes) < 2)
        ):
            raise BookkeepingUnknown("Stop run membership is ambiguous")
        return self


class JobRunStopScope(RunStopScope):
    """The exact issued transient targets within an accepted run Stop."""

    targets: list[ProfileJobRunStopTarget] = Field(default_factory=list)
    unissued_artifact_job_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def targets_are_exact(self) -> JobRunStopScope:
        target_ids = [target.artifact_job_id for target in self.targets]
        source_jobs = [target.source_job_id for target in self.targets]
        source_operations = [target.source_operation_id for target in self.targets]
        if (
            len(target_ids) != len(set(target_ids))
            or len(source_jobs) != len(set(source_jobs))
            or len(source_operations) != len(set(source_operations))
            or set(target_ids) & set(self.unissued_artifact_job_ids)
            or any(
                target.node_id not in self.reachable_node_ids for target in self.targets
            )
        ):
            raise BookkeepingUnknown("profile JobRun Stop authorization is ambiguous")
        return self


class ProfileStopOwnerBinding(RunStopScope):
    """The canonical accepted profile operation that owns this exact Stop."""

    profile_application_id: str = Field(
        min_length=36, max_length=36, pattern=_UUID.pattern
    )
    profile_operation_id: str = Field(
        min_length=36, max_length=36, pattern=_UUID.pattern
    )
    profile_digest: str = Field(min_length=64, max_length=64, pattern=_SHA256.pattern)
    profile_plan_digest: str = Field(
        min_length=64, max_length=64, pattern=_SHA256.pattern
    )
    profile_step: int = Field(ge=0)


class ProfileJobRunStopAuthorization(ProfileStopOwnerBinding, JobRunStopScope):
    """Current accepted profile Stop owns this immutable JobRun scope."""


class ProfileJobRunStopPhaseItem(StrictJSONModel):
    operation_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    node_id: str = Field(min_length=36, max_length=36, pattern=_NODE_ID.pattern)
    payload: RecipeStopPayload


class ProfileJobRunStopJob(StrictJSONModel):
    """The one-shot Stop parent persisted for a current profile owner."""

    schema_version: Literal[1]
    owner_kind: Literal["run"]
    owner_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=_SHA256.pattern)
    workload_intent_ordinal: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    execution_mode: Literal["profile-jobrun-stop"]
    profile_application_id: str = Field(
        min_length=36, max_length=36, pattern=_UUID.pattern
    )
    profile_operation_id: str = Field(
        min_length=36, max_length=36, pattern=_UUID.pattern
    )
    profile_stop_authorization: ProfileJobRunStopAuthorization
    phases: list[list[ProfileJobRunStopPhaseItem]] = Field(min_length=1)

    @property
    def flattened_phase_items(self) -> tuple[ProfileJobRunStopPhaseItem, ...]:
        return tuple(item for phase in self.phases for item in phase)

    @classmethod
    def model_validate_parent(cls, raw: Mapping[str, object]) -> ProfileJobRunStopJob:
        parent = cls.model_validate_json(canonical_message(raw), strict=True)
        authorization = parent.profile_stop_authorization
        if (
            parent.owner_id != authorization.run_id
            or parent.profile_application_id != authorization.profile_application_id
            or parent.profile_operation_id != authorization.profile_operation_id
            or parent.workload_intent_ordinal != authorization.workload_intent_ordinal
            or parent.plan_digest != authorization.stop_plan_digest
            or any(not phase for phase in parent.phases)
            or any(
                len({item.node_id for item in phase}) != len(phase)
                for phase in parent.phases
            )
        ):
            raise BookkeepingUnknown(
                "profile JobRun Stop parent identity is inconsistent"
            )
        target_keys = {
            (item.node_id, item.stop_payload_sha256) for item in authorization.targets
        }
        child_keys = [
            (
                item.node_id,
                hashlib.sha256(canonical_message(item.payload)).hexdigest(),
            )
            for item in parent.flattened_phase_items
        ]
        if (
            len(target_keys) != len(authorization.targets)
            or len(child_keys) != len(set(child_keys))
            or set(child_keys) != target_keys
            or any(
                item.payload.cancel_pending_start is not True
                for item in parent.flattened_phase_items
            )
        ):
            raise BookkeepingUnknown("profile JobRun Stop child set is inconsistent")
        return parent


class ProfileStopAuthorityError(SecurityRefusalError, ValueError):
    """A current profile or exact one-shot Stop link cannot be proved."""


def validate_profile_stop_owner(
    session: Session,
    authorization: ProfileStopOwnerBinding,
    *,
    now: datetime,
    require_current: bool = True,
) -> tuple[RecipeRun, tuple[RunNode, ...]]:
    """Prove the exact accepted profile step still owns this Run Stop."""

    # Imports are local because FleetProfileService composes this module's
    # lifecycle owner through RunSwitch.
    from .fleet_profile_contract import profile_switch_child_request_key
    from .fleet_profiles import (
        FleetProfileService,
        _persisted_profile_plan,
        _persisted_profile_progress,
    )
    from .job_documents import RecipeStopParent, RunSwitchJobPayload
    from .lifecycle.evidence import Residue
    from .run_switch_contract import RunSwitchOperationResult, RunSwitchPlan
    from .run_switch_operations import (
        _phase_request_key,
        _plan_target_node_ids,
        _stop_child_request_key,
    )

    application = session.get(
        FleetProfileApplication, authorization.profile_application_id
    )
    profile_operation = session.get(Job, authorization.profile_operation_id)
    run = session.get(RecipeRun, authorization.run_id)
    if application is None or profile_operation is None or run is None:
        raise ProfileStopAuthorityError("current profile Stop owner is unavailable")
    if application.profile_id is None:
        raise ProfileStopAuthorityError("profile Stop has no saved profile owner")
    switch_stop = profile_operation.kind == "recipe.run-switch.v2" and not isinstance(
        authorization, ProfileJobRunStopAuthorization
    )
    if profile_operation.kind != "recipe.stop.v2" and not switch_stop:
        raise ProfileStopAuthorityError("profile Stop parent operation kind changed")
    if (
        hashlib.sha256(canonical_message(profile_operation.payload)).hexdigest()
        != profile_operation.payload_digest
    ):
        raise ProfileStopAuthorityError("profile Stop parent payload digest changed")
    if (
        profile_operation.payload.get("workload_intent_ordinal")
        != authorization.workload_intent_ordinal
    ):
        raise ProfileStopAuthorityError("profile Stop workload intent changed")
    if application.plan_digest != authorization.profile_plan_digest:
        raise ProfileStopAuthorityError("profile Stop reviewed plan digest changed")
    if (
        run.installation_id != authorization.installation_id
        or run.mapping_id != authorization.mapping_id
        or run.mapping_generation != authorization.mapping_generation
        or run.run_generation != authorization.run_generation
        or run.plan_digest != authorization.plan_digest
    ):
        raise ProfileStopAuthorityError("profile Stop run generation changed")

    if require_current and (
        application.state != "running"
        or profile_operation.state
        not in job_states.words(
            LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
        )
        or (
            run.state not in {RunState.RUNNING, RunState.STARTING, RunState.STOPPING}
            and not (switch_stop and run.state == RunState.LOST)
        )
    ):
        raise ProfileStopAuthorityError("current profile Stop identity is stale")

    profile_progress = profile_operation.payload.get("progress")
    if (
        not isinstance(profile_progress, Mapping)
        or profile_progress.get("profile_application_id") != application.id
        or profile_progress.get("workload_intent_ordinal")
        != authorization.workload_intent_ordinal
    ):
        raise ProfileStopAuthorityError("current profile Stop progress is inconsistent")

    current: RunSwitchOperationResult | None = None
    try:
        progress = _persisted_profile_progress(application)
        current_plan = _persisted_profile_plan(application)
        intended = FleetProfileService._intended_profile(application, session=session)
        reviewed = FleetProfileService._reviewed_profile_plan(
            application, session=session
        )
        run_switch_plan = read_stored_model(
            RunSwitchPlan,
            canonical_message(profile_operation.payload.get("plan")),
            strict=True,
            from_json=True,
        )
        if switch_stop:
            root = RunSwitchJobPayload.model_validate_json(
                canonical_message(profile_operation.payload), strict=True
            )
            current = RunSwitchOperationResult.model_validate_json(
                canonical_message(profile_operation.result), strict=True
            )
            phase_index, item_index = current.phase_index, current.item_index
            if (
                root.operation_kind != profile_operation.kind
                or root.action != run_switch_plan.action
                or run_switch_plan.action not in {"run", "install", "switch"}
                or current.profile_application_id != application.id
                or current.workload_intent_ordinal
                != authorization.workload_intent_ordinal
                or current.cancellation is not None
                or phase_index is None
                or item_index is None
                or phase_index >= len(run_switch_plan.phases)
                or item_index >= len(run_switch_plan.stops)
            ):
                raise ProfileStopAuthorityError(
                    "profile replacement Stop phase is inconsistent"
                )
            phase = run_switch_plan.phases[phase_index]
            if (
                phase.kind != "stop"
                or current.phase != phase.kind
                or current.subphase != phase.subphase
                or run_switch_plan.stops[item_index].run_id != run.id
            ):
                raise ProfileStopAuthorityError(
                    "profile replacement does not own this current Stop phase"
                )
    except (TypeError, ValueError) as error:
        raise ProfileStopAuthorityError(
            "current profile Stop plan is invalid"
        ) from error
    # A Stop is a destructive effect: evidence that cannot be read proves no
    # authority, so it is refused here (the load itself retires as unknown).
    if (
        isinstance(current_plan, Residue)
        or isinstance(intended, Residue)
        or isinstance(reviewed, Residue)
    ):
        raise ProfileStopAuthorityError("current profile Stop plan is invalid")

    switch_adapter = progress.switch_adapter
    if switch_adapter is None or switch_adapter.child_id != application.id:
        raise ProfileStopAuthorityError("profile Stop is not the active reviewed child")
    pending = [
        child
        for child in switch_adapter.pending_children
        if child.operation_id == profile_operation.id
    ]
    if len(pending) != 1:
        raise ProfileStopAuthorityError("profile Stop is not the active reviewed child")
    child = pending[0]
    item = switch_adapter.queue[child.queue_index]
    expected_kind = (
        ("install" if run_switch_plan.action == "install" else "run")
        if switch_stop
        else "stop"
    )
    if (
        child.kind != expected_kind
        or profile_operation.request_id
        != profile_switch_child_request_key(
            application.id, child.queue_index, item.kind, item.id
        )
    ):
        raise ProfileStopAuthorityError("profile Stop queue request binding changed")
    if switch_stop and current is not None and current.child_operation_id is not None:
        linked = session.get(Job, current.child_operation_id)
        phase_key = (
            _phase_request_key(
                profile_operation.request_id,
                current.phase_index,
                current.item_index,
                current.phase_retry_generation,
            )
            if current.phase_retry_generation
            else profile_operation.request_id
        )
        expected_key = _stop_child_request_key(phase_key, run.id, application.id)
        if (
            linked is None
            or linked.kind != "recipe.stop"
            or linked.request_id != expected_key
            or linked.payload_digest
            != hashlib.sha256(canonical_message(linked.payload)).hexdigest()
        ):
            raise ProfileStopAuthorityError(
                "profile Stop linked child identity changed"
            )
        try:
            linked_document = RecipeStopParent.model_validate_json(
                canonical_message(linked.payload), strict=True
            )
        except (TypeError, ValueError) as error:
            raise ProfileStopAuthorityError(
                "profile Stop linked child is unreadable"
            ) from error
        linked_review = linked_document.service_stop_review
        if (
            linked_document.owner_kind != "run"
            or linked_document.owner_id != run.id
            or linked_document.workload_intent_ordinal
            != authorization.workload_intent_ordinal
            or linked_review is None
            or linked_review.profile_stop_owner != authorization
            or linked.targets != authorization.reachable_node_ids
        ):
            raise ProfileStopAuthorityError("profile Stop linked child scope changed")

    profile = session.get(FleetProfile, application.profile_id)
    selection = session.get(FleetProfileSelection, 1)
    if (
        profile is None
        or application.selection_generation is None
        or (
            require_current
            and (
                selection is None
                or not FleetProfileService._application_is_current_selection(
                    session, application, progress
                )
            )
        )
        or intended.profile_digest != application.profile_digest
        or application.profile_digest != authorization.profile_digest
        or progress.workload_intent_ordinal != authorization.workload_intent_ordinal
        or (require_current and progress.cancellation is not None)
        or (
            require_current
            and FleetProfileService._superseding_intent(session, application, progress)
        )
    ):
        raise ProfileStopAuthorityError("a newer profile intent owns this Stop")

    run_nodes = tuple(
        session.scalars(
            select(RunNode)
            .where(RunNode.run_id == run.id)
            .order_by(RunNode.rank, RunNode.node_id)
        )
    )
    node_ids = tuple(node.node_id for node in run_nodes)
    if (
        not run_nodes
        or len(node_ids) != len(set(node_ids))
        or len({node.rank for node in run_nodes}) != len(run_nodes)
        or list(node_ids) != authorization.run_node_ids
    ):
        raise ProfileStopAuthorityError("profile Stop run membership changed")
    fleet_nodes = tuple(
        session.scalars(
            select(AgentNode)
            .where(AgentNode.node_id.in_(node_ids))
            .order_by(AgentNode.node_id)
        )
    )
    fleet_by_id = {node.node_id: node for node in fleet_nodes}
    if tuple(sorted(fleet_by_id)) != tuple(sorted(node_ids)):
        raise ProfileStopAuthorityError("profile Stop fleet intent changed")

    profile_step = next(
        (
            step
            for step in current_plan.steps
            if step.index == authorization.profile_step
        ),
        None,
    )
    effect_matches = [
        effect for effect in current_plan.effects.runs if effect.run_id == run.id
    ]
    stops = [item for item in run_switch_plan.stops if item.run_id == run.id]
    mapping_nodes = tuple(
        session.scalars(
            select(ClusterMappingNode)
            .where(ClusterMappingNode.mapping_id == run.mapping_id)
            .order_by(ClusterMappingNode.rank, ClusterMappingNode.node_id)
        )
    )
    run_members = tuple((node.rank, node.node_id, node.role) for node in run_nodes)
    mapping_members = tuple(
        (node.rank, node.node_id, node.role) for node in mapping_nodes
    )
    switch_members = tuple(
        (node.rank, node.node_id, node.role)
        for node in run_switch_plan.spark_group.nodes
    )
    profile_scope = (
        effect_matches[0].profile_stop_scope
        if switch_stop and len(effect_matches) == 1
        else run_switch_plan.profile_stop_scope
    )
    expected_target_ids = (
        tuple(profile_scope.target_node_ids) if profile_scope is not None else node_ids
    )
    expected_reachable_ids = tuple(sorted(expected_target_ids))
    expected_missing_ids = (
        tuple(profile_scope.missing_node_ids) if profile_scope is not None else ()
    )
    adopted_scope = FleetProfileService._adopted_application_scope(session, application)
    if (
        require_current
        and adopted_scope is not None
        and not set(node_ids) <= set(adopted_scope)
    ):
        raise ProfileStopAuthorityError(
            "profile Stop exceeds its adopted assignment scope"
        )
    stop = stops[0] if len(stops) == 1 else None
    if require_current and (
        any(
            fleet_by_id[node_id].revoked_at is not None
            or fleet_by_id[node_id].workload_intent_ordinal
            != authorization.workload_intent_ordinal
            for node_id in expected_reachable_ids
        )
        or any(
            fleet_by_id[node_id].revoked_at is None for node_id in expected_missing_ids
        )
    ):
        raise ProfileStopAuthorityError("profile Stop fleet intent changed")
    if (
        current_plan.plan_digest != application.plan_digest
        or application.plan_digest != authorization.profile_plan_digest
        or reviewed.profile_digest != application.profile_digest
        or profile_step is None
        or profile_step.kind != "switch"
        or not set(expected_reachable_ids) <= set(profile_step.node_ids)
        or len(effect_matches) != 1
        or effect_matches[0].action != "stop"
        or effect_matches[0].installation_id != run.installation_id
        or effect_matches[0].alias != run.alias
        or tuple(effect_matches[0].node_ids) != tuple(sorted(node_ids))
        or (
            (profile_scope is None and effect_matches[0].profile_stop_scope is not None)
            or (
                profile_scope is not None
                and effect_matches[0].profile_stop_scope != profile_scope
            )
        )
        or (not switch_stop and run_switch_plan.action != "stop")
        or (not switch_stop and run_switch_plan.run_id != run.id)
        or run_switch_plan.plan_digest != profile_operation.payload.get("plan_digest")
        or (not switch_stop and switch_members != run_members)
        or mapping_members != run_members
        or profile_operation.targets
        != (
            list(_plan_target_node_ids(run_switch_plan))
            if switch_stop
            else list(expected_reachable_ids)
        )
        or authorization.reachable_node_ids != list(expected_reachable_ids)
        or authorization.missing_node_ids != list(expected_missing_ids)
        or stop is None
        or stop.alias != run.alias
        or stop.run_plan_digest != run.plan_digest
        or stop.plan_digest != authorization.stop_plan_digest
        or tuple(stop.node_ids) != expected_target_ids
        or not set(expected_reachable_ids) <= set(intended.scope.node_ids)
        or set(expected_missing_ids) & set(intended.scope.node_ids)
    ):
        raise ProfileStopAuthorityError("accepted profile does not own this exact Stop")

    accepted_revision_id = run_switch_plan.recipe_revision_id
    if switch_stop:
        installation = session.get(RecipeInstallation, run.installation_id)
        if installation is None:
            raise ProfileStopAuthorityError("profile Stop installation disappeared")
        accepted_revision_id = installation.recipe_revision_id
    if (
        authorization.installation_id != run.installation_id
        or authorization.mapping_id != run.mapping_id
        or authorization.mapping_generation != run.mapping_generation
        or authorization.run_generation != run.run_generation
        or authorization.plan_digest != run.plan_digest
        or authorization.recipe_revision_id != accepted_revision_id
        or (require_current and authorization.profile_step != application.current_step)
        or (require_current and now.tzinfo is None)
    ):
        raise ProfileStopAuthorityError("profile Stop lifecycle binding is stale")
    return run, run_nodes


def validate_jobrun_stop_source(
    session: Session,
    target: ProfileJobRunStopTarget,
    stop: RecipeStopPayload,
    run: RecipeRun,
    *,
    workload_intent_ordinal: int,
) -> tuple[ArtifactJob, Job, AgentOperation]:
    """Prove the exact immutable issued JobRun target a cleanup Stop names."""

    artifact = session.get(ArtifactJob, target.artifact_job_id)
    source_job = session.get(Job, target.source_job_id)
    source_operation = session.get(AgentOperation, target.source_operation_id)
    source_siblings = tuple(
        session.scalars(
            select(AgentOperation)
            .where(AgentOperation.parent_job_id == target.source_job_id)
            .order_by(AgentOperation.id)
        )
    )
    if (
        artifact is None
        or artifact.run_id != run.id
        or artifact.operation_id != target.source_job_id
        or source_job is None
        or source_operation is None
        or source_job.kind != "recipe.job.run.v1"
        or source_job.payload.get("owner_kind") != "artifact-job"
        or source_job.payload.get("owner_id") != artifact.id
        or source_job.authority_revision != stop.recipe_content_sha256
        or source_job.payload_digest
        != hashlib.sha256(canonical_message(source_job.payload)).hexdigest()
        or source_operation.parent_job_id != source_job.id
        or source_operation.node_id != target.node_id
        or source_operation.kind != "recipe.job.run.v1"
        or source_operation.workload_intent_ordinal is None
        or source_operation.workload_intent_ordinal
        != source_job.payload.get("workload_intent_ordinal")
        or source_operation.workload_intent_ordinal >= workload_intent_ordinal
        or source_operation.payload_digest
        != hashlib.sha256(canonical_message(source_operation.payload)).hexdigest()
        or source_job.targets != [target.node_id]
        or len(source_siblings) != 1
        or source_siblings[0].id != source_operation.id
    ):
        raise ProfileStopAuthorityError("source JobRun identity is inconsistent")
    try:
        request = read_stored_model(
            RecipeJobRunRequest,
            canonical_message(source_operation.payload),
            from_json=True,
        )
        expected = stop_payload_from_job_run(request, cancel_pending_start=True)
    except (TypeError, ValueError) as error:
        raise ProfileStopAuthorityError("source JobRun request is invalid") from error
    installation = session.get(RecipeInstallation, run.installation_id)
    member = session.scalar(
        select(RunNode).where(
            RunNode.run_id == run.id, RunNode.node_id == target.node_id
        )
    )
    if (
        installation is None
        or member is None
        or request.recipe_revision_id != installation.recipe_revision_id
        or request.run_generation > run.run_generation
        or request.compiled_execution_plan.runtime.placement.rank != member.rank
        or request.compiled_execution_plan.runtime.placement.role != member.role
        or request.compiled_execution_plan.runtime.placement.world_size
        != len(
            tuple(session.scalars(select(RunNode.id).where(RunNode.run_id == run.id)))
        )
        or request.job_id != artifact.id
        or request.run_id != run.id
        or request.installation_id != run.installation_id
        or request.mapping_id != run.mapping_id
        or request.plan_digest != run.plan_digest
        or stop.cancel_pending_start is not True
        or canonical_message(stop) != canonical_message(expected)
        or hashlib.sha256(canonical_message(stop)).hexdigest()
        != target.stop_payload_sha256
    ):
        raise ProfileStopAuthorityError("JobRun Stop differs from its source")
    return artifact, source_job, source_operation


def validate_run_jobrun_stop_target(
    session: Session,
    scope: JobRunStopScope,
    target: ProfileJobRunStopTarget,
    stop: RecipeStopPayload,
    *,
    stop_parent: Job,
    operation: AgentOperation | None = None,
    require_current: bool = True,
) -> tuple[ArtifactJob, Job, AgentOperation]:
    """Bind an ordinary accepted run Stop to its frozen exact JobRun targets."""
    from .job_documents import RecipeStopParent

    accepted = RecipeStopParent.model_validate_json(
        canonical_message(stop_parent.payload), strict=True
    )
    run = session.get(RecipeRun, scope.run_id)
    installation = session.get(RecipeInstallation, scope.installation_id)
    nodes = tuple(
        session.scalars(
            select(RunNode).where(RunNode.run_id == scope.run_id).order_by(RunNode.rank)
        )
    )
    if (
        run is None
        or installation is None
        or installation.recipe_revision_id != scope.recipe_revision_id
        or stop_parent.kind != "recipe.stop"
        or stop_parent.payload_digest
        != hashlib.sha256(canonical_message(stop_parent.payload)).hexdigest()
        or accepted.owner_kind != "run"
        or accepted.owner_id != scope.run_id
        or accepted.execution_mode != "one-shot-jobs"
        or accepted.plan_digest != scope.stop_plan_digest
        or accepted.workload_intent_ordinal != scope.workload_intent_ordinal
        or accepted.job_run_stop_authorization != scope
        or stop_parent.targets != scope.reachable_node_ids
        or run.installation_id != scope.installation_id
        or run.mapping_id != scope.mapping_id
        or run.mapping_generation != scope.mapping_generation
        or run.run_generation != scope.run_generation
        or run.plan_digest != scope.plan_digest
        or [node.node_id for node in nodes] != scope.run_node_ids
        or target not in scope.targets
        or (require_current and run.state not in {RunState.RUNNING, RunState.STOPPING})
    ):
        raise ProfileStopAuthorityError("accepted run JobRun Stop owner changed")
    if require_current:
        agents = tuple(
            session.scalars(
                select(AgentNode).where(AgentNode.node_id.in_(scope.reachable_node_ids))
            )
        )
        if len(agents) != len(scope.reachable_node_ids) or any(
            node.workload_intent_ordinal != scope.workload_intent_ordinal
            for node in agents
        ):
            raise ProfileStopAuthorityError("accepted run JobRun Stop was superseded")
    matches = [
        item
        for phase in accepted.phases or ()
        for item in phase
        if item.node_id == target.node_id
        and hashlib.sha256(canonical_message(item.payload)).hexdigest()
        == target.stop_payload_sha256
    ]
    phase_keys = [
        (item.node_id, hashlib.sha256(canonical_message(item.payload)).hexdigest())
        for phase in accepted.phases or ()
        for item in phase
    ]
    if len(phase_keys) != len(scope.targets) or set(phase_keys) != {
        (item.node_id, item.stop_payload_sha256) for item in scope.targets
    }:
        raise ProfileStopAuthorityError("accepted run JobRun Stop manifest changed")
    if len(matches) != 1 or (
        operation is not None
        and (
            operation.parent_job_id != stop_parent.id
            or operation.id != matches[0].operation_id
            or operation.kind != "recipe.stop"
            or operation.node_id != target.node_id
            or operation.workload_intent_ordinal != scope.workload_intent_ordinal
            or operation.authority_revision != stop_parent.authority_revision
            or operation.payload_digest
            != hashlib.sha256(canonical_message(operation.payload)).hexdigest()
            or canonical_message(operation.payload) != canonical_message(stop)
        )
    ):
        raise ProfileStopAuthorityError("issued run JobRun Stop child changed")
    if canonical_message(matches[0].payload) != canonical_message(stop):
        raise ProfileStopAuthorityError("run JobRun Stop phase target changed")
    return validate_jobrun_stop_source(
        session,
        target,
        stop,
        run,
        workload_intent_ordinal=scope.workload_intent_ordinal,
    )


def validate_profile_jobrun_stop_target(
    session: Session,
    authorization: ProfileJobRunStopAuthorization,
    target: ProfileJobRunStopTarget,
    stop: RecipeStopPayload,
    *,
    operation: AgentOperation | None = None,
    stop_parent: Job | None = None,
    now: datetime,
    require_current: bool = True,
) -> None:
    """Bind a schema-2 Stop to the exact older typed JobRun request."""

    run, _run_nodes = validate_profile_stop_owner(
        session, authorization, now=now, require_current=require_current
    )
    if target not in authorization.targets:
        raise ProfileStopAuthorityError("profile Stop target is not authorized")
    _artifact, source_job, _source_operation = validate_jobrun_stop_source(
        session,
        target,
        stop,
        run,
        workload_intent_ordinal=authorization.workload_intent_ordinal,
    )
    source_cancel_request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{target.source_job_id}:{authorization.workload_intent_ordinal}",
        )
    )
    if (
        not isinstance(source_job.result, Mapping)
        or source_job.result.get("cancel_requested") is not True
        or source_job.result.get("cancel_request_id") != source_cancel_request_id
        or source_job.result.get("cancel_actor") != "controller"
        or source_job.result.get("reason") != "superseded by newer workload intent"
    ):
        raise ProfileStopAuthorityError("source JobRun cancellation owner changed")
    if stop_parent is not None:
        typed_parent = ProfileJobRunStopJob.model_validate_parent(stop_parent.payload)
        if (
            stop_parent.kind != "recipe.stop"
            or stop_parent.payload_digest
            != hashlib.sha256(canonical_message(stop_parent.payload)).hexdigest()
            or stop_parent.payload.get("owner_kind") != "run"
            or stop_parent.payload.get("owner_id") != run.id
            or typed_parent.plan_digest != authorization.stop_plan_digest
            or typed_parent.profile_stop_authorization != authorization
            or typed_parent.profile_application_id
            != authorization.profile_application_id
            or typed_parent.profile_operation_id != authorization.profile_operation_id
            or stop_parent.payload.get("workload_intent_ordinal")
            != authorization.workload_intent_ordinal
            or stop_parent.targets != [target.node_id]
            or (
                operation is not None
                and (
                    operation.kind != "recipe.stop"
                    or operation.parent_job_id != stop_parent.id
                    or operation.node_id != target.node_id
                    or operation.workload_intent_ordinal
                    != authorization.workload_intent_ordinal
                    or operation.payload_digest
                    != hashlib.sha256(canonical_message(operation.payload)).hexdigest()
                    or canonical_message(operation.payload) != canonical_message(stop)
                )
            )
        ):
            raise ProfileStopAuthorityError("issued profile Stop child changed")
    elif operation is not None:
        raise ProfileStopAuthorityError("issued profile Stop parent is unavailable")
