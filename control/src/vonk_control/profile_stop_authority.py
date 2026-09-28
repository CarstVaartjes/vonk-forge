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
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import RecipeStopPayload

from .models import (
    AgentNode,
    AgentOperation,
    ArtifactJob,
    ClusterMappingNode,
    FleetProfile,
    FleetProfileApplication,
    FleetProfileSelection,
    Job,
    RecipeRun,
    RunNode,
)
from .recipe_stop_payloads import stop_payload_from_job_run
from .strict_json import StrictJSONModel

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


class ProfileJobRunStopAuthorization(StrictJSONModel):
    """Current accepted profile Stop and exact older one-shot effect."""

    schema_version: Literal[1]
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
    run_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    installation_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    recipe_revision_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    mapping_id: str = Field(min_length=36, max_length=36, pattern=_UUID.pattern)
    mapping_generation: int = Field(ge=1)
    run_generation: int = Field(ge=1)
    plan_digest: str = Field(min_length=64, max_length=64, pattern=_SHA256.pattern)
    workload_intent_ordinal: int = Field(ge=1)
    run_node_ids: list[str] = Field(min_length=1, max_length=32)
    reachable_node_ids: list[str] = Field(min_length=1, max_length=32)
    missing_node_ids: list[str] = Field(default_factory=list, max_length=31)
    targets: list[ProfileJobRunStopTarget] = Field(default_factory=list)
    stop_plan_digest: str = Field(min_length=64, max_length=64, pattern=_SHA256.pattern)
    unissued_artifact_job_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def identities_are_exact(self) -> ProfileJobRunStopAuthorization:
        node_ids = self.run_node_ids
        target_ids = [target.artifact_job_id for target in self.targets]
        source_jobs = [target.source_job_id for target in self.targets]
        source_operations = [target.source_operation_id for target in self.targets]
        reachable = self.reachable_node_ids
        missing = self.missing_node_ids
        if (
            node_ids != list(dict.fromkeys(node_ids))
            or reachable != sorted(set(reachable))
            or missing != sorted(set(missing))
            or set(reachable) & set(missing)
            or set(reachable) | set(missing) != set(node_ids)
            or bool(missing)
            and len(node_ids) < 2
            or len(target_ids) != len(set(target_ids))
            or len(source_jobs) != len(set(source_jobs))
            or len(source_operations) != len(set(source_operations))
            or set(target_ids) & set(self.unissued_artifact_job_ids)
            or any(target.node_id not in reachable for target in self.targets)
        ):
            raise ValueError("profile JobRun Stop authorization is ambiguous")
        return self


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
    workload_intent_ordinal: int = Field(ge=1)
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
            raise ValueError("profile JobRun Stop parent identity is inconsistent")
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
            raise ValueError("profile JobRun Stop child set is inconsistent")
        return parent


class ProfileStopAuthorityError(ValueError):
    """A current profile or exact one-shot Stop link cannot be proved."""


def validate_profile_stop_owner(
    session: Session,
    authorization: ProfileJobRunStopAuthorization,
    *,
    now: datetime,
    require_current: bool = True,
) -> tuple[RecipeRun, tuple[RunNode, ...]]:
    """Prove the exact accepted profile step still owns this Run Stop."""

    # Imports are local because FleetProfileService composes this module's
    # lifecycle owner through RunSwitch.
    from .fleet_profiles import (
        FleetProfileService,
        _persisted_profile_plan,
        _persisted_profile_progress,
    )
    from .run_switch_contract import RunSwitchPlan

    application = session.get(
        FleetProfileApplication, authorization.profile_application_id
    )
    profile_operation = session.get(Job, authorization.profile_operation_id)
    run = session.get(RecipeRun, authorization.run_id)
    if application is None or profile_operation is None or run is None:
        raise ProfileStopAuthorityError("current profile Stop owner is unavailable")
    if application.profile_id is None:
        raise ProfileStopAuthorityError("profile Stop has no saved profile owner")
    if profile_operation.kind != "recipe.stop.v2":
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
        or profile_operation.state not in {"queued", "running", "waiting-for-operator"}
        or run.state not in {"running", "starting", "stopping"}
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

    try:
        progress = _persisted_profile_progress(application)
        current_plan = _persisted_profile_plan(application)
        intended = FleetProfileService._intended_profile(application, session=session)
        reviewed = FleetProfileService._reviewed_profile_plan(
            application, session=session
        )
        run_switch_plan = RunSwitchPlan.model_validate_json(
            canonical_message(profile_operation.payload.get("plan")), strict=True
        )
    except (TypeError, ValueError) as error:
        raise ProfileStopAuthorityError(
            "current profile Stop plan is invalid"
        ) from error

    switch_adapter = progress.switch_adapter
    if (
        switch_adapter is None
        or switch_adapter.child_id != application.id
        or switch_adapter.active_kind != "stop"
        or switch_adapter.active_operation_id != profile_operation.id
    ):
        raise ProfileStopAuthorityError("profile Stop is not the active reviewed child")

    profile = session.get(FleetProfile, application.profile_id)
    selection = session.get(FleetProfileSelection, 1)
    if (
        profile is None
        or application.selection_generation is None
        or (
            require_current
            and (
                selection is None
                or selection.generation != application.selection_generation
                or selection.application_id != application.id
                or selection.profile_id != application.profile_id
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
    profile_scope = run_switch_plan.profile_stop_scope
    expected_target_ids = (
        tuple(profile_scope.target_node_ids) if profile_scope is not None else node_ids
    )
    expected_reachable_ids = tuple(sorted(expected_target_ids))
    expected_missing_ids = (
        tuple(profile_scope.missing_node_ids) if profile_scope is not None else ()
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
        or run_switch_plan.action != "stop"
        or run_switch_plan.run_id != run.id
        or run_switch_plan.plan_digest != profile_operation.payload.get("plan_digest")
        or switch_members != run_members
        or mapping_members != run_members
        or profile_operation.targets != list(expected_reachable_ids)
        or authorization.reachable_node_ids != list(expected_reachable_ids)
        or authorization.missing_node_ids != list(expected_missing_ids)
        or any(
            target.node_id not in expected_reachable_ids
            for target in authorization.targets
        )
        or stop is None
        or stop.alias != run.alias
        or stop.run_plan_digest != run.plan_digest
        or stop.plan_digest != authorization.stop_plan_digest
        or tuple(stop.node_ids) != expected_target_ids
        or not set(expected_reachable_ids) <= set(intended.scope.node_ids)
        or set(expected_missing_ids) & set(intended.scope.node_ids)
    ):
        raise ProfileStopAuthorityError("accepted profile does not own this exact Stop")

    if (
        authorization.installation_id != run.installation_id
        or authorization.mapping_id != run.mapping_id
        or authorization.mapping_generation != run.mapping_generation
        or authorization.run_generation != run.run_generation
        or authorization.plan_digest != run.plan_digest
        or authorization.recipe_revision_id != run_switch_plan.recipe_revision_id
        or (require_current and authorization.profile_step != application.current_step)
        or (require_current and now.tzinfo is None)
    ):
        raise ProfileStopAuthorityError("profile Stop lifecycle binding is stale")
    return run, run_nodes


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
    source_cancel_request_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{target.source_job_id}:{authorization.workload_intent_ordinal}",
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
        or source_job.authority_revision
        != stop.compiled_execution_plan.identity.recipe_revision_sha256
        or source_job.payload_digest
        != hashlib.sha256(canonical_message(source_job.payload)).hexdigest()
        or source_operation.parent_job_id != source_job.id
        or source_operation.node_id != target.node_id
        or source_operation.kind != "recipe.job.run.v1"
        or source_operation.workload_intent_ordinal is None
        or source_operation.workload_intent_ordinal
        != source_job.payload.get("workload_intent_ordinal")
        or source_operation.workload_intent_ordinal
        >= authorization.workload_intent_ordinal
        or source_operation.payload_digest
        != hashlib.sha256(canonical_message(source_operation.payload)).hexdigest()
        or source_job.targets != [target.node_id]
        or len(source_siblings) != 1
        or source_siblings[0].id != source_operation.id
        or not isinstance(source_job.result, Mapping)
        or source_job.result.get("cancel_requested") is not True
        or source_job.result.get("cancel_request_id") != source_cancel_request_id
        or source_job.result.get("cancel_actor") != "controller"
        or source_job.result.get("reason") != "superseded by newer workload intent"
    ):
        raise ProfileStopAuthorityError("source JobRun identity is inconsistent")
    try:
        request = RecipeJobRunRequest.model_validate_json(
            canonical_message(source_operation.payload)
        )
        expected = stop_payload_from_job_run(request, cancel_pending_start=True)
    except (TypeError, ValueError) as error:
        raise ProfileStopAuthorityError("source JobRun request is invalid") from error
    if (
        request.job_id != artifact.id
        or request.run_id != run.id
        or request.installation_id != run.installation_id
        or request.mapping_id != run.mapping_id
        or request.plan_digest != run.plan_digest
        or stop.cancel_pending_start is not True
        or canonical_message(stop) != canonical_message(expected)
        or hashlib.sha256(canonical_message(stop)).hexdigest()
        != target.stop_payload_sha256
    ):
        raise ProfileStopAuthorityError("profile JobRun Stop differs from its source")
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
