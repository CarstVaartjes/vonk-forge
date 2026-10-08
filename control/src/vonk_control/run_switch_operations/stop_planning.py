"""Stop planning."""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import select
from vonk_agent_protocol import (
    InvalidRequestReason,
    RunSwitchCode,
    UnknownOutcomeError,
)

from ..admission_locking import (
    admission_attempts,
)
from ..categorized_errors import (
    MissingRecord,
)
from ..lifecycle.evidence import (
    Residue,
    read_or_rebuild,
)
from ..models import (
    STOPPABLE_RUN_STATES,
    ClusterMapping,
    ClusterMappingNode,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
)
from ..recipe_execution_contract import (
    parse_stored_run_plan,
)
from ..run_switch_contract import (
    InvocationMetadata,
    RunSwitchCleanupPreviewRequest,
    RunSwitchPlan,
    RunSwitchProfileStopScope,
    RunSwitchReason,
    RunSwitchStopPreviewRequest,
    SparkGroup,
    SparkGroupNode,
    StopImpact,
)
from ..strict_json import read_stored_model
from .constants import _active_recipe_revision
from .errors import RunSwitchRequestInvalid
from .planning_helpers import _as_reason, _now

if TYPE_CHECKING:
    from .service import RunSwitchOperationService


class StopPlanningMixin:
    def preview_stop(
        self,
        request: RunSwitchStopPreviewRequest | str,
        *,
        actor: str,
        profile_stop_scope: RunSwitchProfileStopScope | None = None,
    ) -> RunSwitchPlan:
        service = typing_cast("RunSwitchOperationService", self)
        run_id = request if isinstance(request, str) else request.run_id
        invocation = (
            InvocationMetadata() if isinstance(request, str) else request.invocation
        )
        now = _now(service._clock)
        with service._sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise MissingRecord(run_id)
            installation = session.get(RecipeInstallation, run.installation_id)
            # Stopping a live run never waits on its stored plan: a plan that
            # cannot be read is rebuilt from the installation (the evidence of
            # which recipe and model the run serves), else it is retired.
            run_plan = read_or_rebuild(
                kind="run-switch.run-plan",
                subject=run.id,
                read=lambda: parse_stored_run_plan(run.plan),
            )
            recipe_revision_id = (
                installation.recipe_revision_id
                if isinstance(run_plan, Residue) and installation is not None
                else run_plan.recipe_revision_id
                if not isinstance(run_plan, Residue)
                else None
            )
            revision = _active_recipe_revision(session, recipe_revision_id)
            mapping = session.get(ClusterMapping, run.mapping_id)
            mapping_nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == run.mapping_id)
                    .order_by(ClusterMappingNode.rank)
                )
            )
            original_group = SparkGroup(
                nodes=[
                    SparkGroupNode(
                        node_id=node.node_id,
                        rank=node.rank,
                        role=node.role,
                        endpoint_owner=node.endpoint_owner,
                    )
                    for node in mapping_nodes
                ]
            )
            target_node_ids = tuple(node.node_id for node in original_group.nodes)
            if profile_stop_scope is not None:
                if original_group != profile_stop_scope.original_group:
                    raise RunSwitchRequestInvalid(
                        RunSwitchCode.PROFILE_STOP_SCOPE_CHANGED,
                        reason=InvalidRequestReason.CONFLICT,
                    )
                target_node_ids = tuple(profile_stop_scope.target_node_ids)
            model_digest = (
                installation.model_content_sha256 if installation is not None else None
            )
            recipe_digest = revision.content_digest if revision is not None else None
            (
                _model_document,
                _model_documents,
                _document_blockers,
            ) = service._resolve_documents(
                session,
                revision,
                model_digest,
                requested_recipe_digest=recipe_digest,
            )
            (
                freshness,
                fit_current,
                _fit_blockers,
                fit_warnings,
                _memory_shortfalls,
            ) = service._fit(
                session,
                revision,
                original_group,
                now=now,
                excluded_run_ids=(run.id,),
            )
            inspection = service._inspect_artifacts(
                session,
                model_digest,
                revision.id if revision is not None else None,
                original_group,
                retention="retain-cached",
                now=now,
            )
            build = (
                session.get(RecipeBuild, installation.recipe_build_id)
                if installation is not None and installation.recipe_build_id is not None
                else None
            )
            build_candidate = build or (
                service._latest_build(session, revision.id)
                if revision is not None
                else None
            )
            build_evidence, runtime_storage, _build_blockers, _build_warnings = (
                service._build_evidence(
                    session,
                    revision,
                    build,
                    build_candidate,
                    original_group,
                    require_available=False,
                )
            )
            stop_digest = service._stop_digest(
                run.id,
                target_node_ids=(
                    profile_stop_scope.target_node_ids
                    if profile_stop_scope is not None
                    else None
                ),
            )
            if profile_stop_scope is not None and stop_digest is not None:
                lifecycle = service._lifecycle
                # An unbound lifecycle owner cannot cross-check the stop set:
                # that is unknown, and the stop (idempotent, scoped by the
                # profile's own reviewed group) goes ahead on the digest.
                lifecycle_stop = (
                    lifecycle.preview_stop(
                        run.id,
                        profile_target_node_ids=profile_stop_scope.target_node_ids,
                    )
                    if lifecycle is not None
                    else None
                )
                if lifecycle_stop is not None and (
                    lifecycle_stop.target_node_ids
                    != tuple(profile_stop_scope.target_node_ids)
                    or lifecycle_stop.missing_node_ids
                    != tuple(profile_stop_scope.missing_node_ids)
                    or {
                        (node.node_id, node.rank, node.role)
                        for node in lifecycle_stop.nodes
                    }
                    != {
                        (node.node_id, node.rank, node.role)
                        for node in profile_stop_scope.original_group.nodes
                    }
                    or not lifecycle_stop.allowed
                ):
                    stop_digest = None
            stops = (
                [
                    StopImpact(
                        run_id=run.id,
                        run_plan_digest=run.plan_digest,
                        alias=run.alias,
                        state=run.state,
                        node_ids=list(target_node_ids),
                        reserved_bytes=service._run_reserved_bytes(
                            session,
                            run.id,
                            node_ids=target_node_ids,
                        ),
                        plan_digest=stop_digest,
                    )
                ]
                if stop_digest is not None and run.state in STOPPABLE_RUN_STATES
                else []
            )
            # Stopping a live run must remain possible when catalog/cache
            # evidence has aged or is unavailable. Capacity and artifact
            # findings remain diagnostics, but they do not block the stop.
            blockers: list[RunSwitchReason] = []
            warnings = [*fit_warnings, *inspection.warnings]
            if profile_stop_scope is not None:
                missing_ranks = [
                    node
                    for node in profile_stop_scope.original_group.nodes
                    if node.node_id in profile_stop_scope.missing_node_ids
                ]
                warnings.append(
                    _as_reason(
                        RunSwitchCode.PROFILE_INCOMPLETE_MULTI_SPARK_MODEL,
                        "This profile Stop will withdraw the model route and stop "
                        "only reachable ranks. Missing Spark ranks may still be running: "
                        + ", ".join(
                            f"rank {node.rank} ({node.node_id})"
                            for node in missing_ranks
                        ),
                        scope="group",
                        node_ids=profile_stop_scope.missing_node_ids,
                    )
                )
            if run.state not in STOPPABLE_RUN_STATES:
                blockers.append(
                    _as_reason(
                        RunSwitchCode.RUN_NOT_ACTIVE,
                        "The selected run is no longer active and cannot be stopped.",
                        scope="operation",
                        node_ids=[node.node_id for node in mapping_nodes],
                    )
                )
            elif stop_digest is None:
                blockers.append(
                    _as_reason(
                        RunSwitchCode.STOP_PLAN_UNAVAILABLE,
                        "The existing run cannot be represented by a safe stop plan.",
                        scope="operation",
                        node_ids=[node.node_id for node in mapping_nodes],
                    )
                )
            phases = service._phases(
                action="stop",
                group=original_group,
                phase_node_ids=target_node_ids,
                installation_id=installation.id if installation is not None else None,
                installation_state=installation.state
                if installation is not None
                else None,
                stops=stops,
                inspection=inspection,
                partial_stop=profile_stop_scope is not None,
                runtime_storage=runtime_storage,
                retention="retain-cached",
                blockers=blockers,
                stop_before_transfer=False,
                stop_before_prepare=False,
            )
            storage = service._storage(inspection, retention="retain-cached")
            preparation = (
                None
                if profile_stop_scope is not None
                else service._preparation(
                    revision=revision,
                    group=original_group,
                    inspection=inspection,
                    build=build,
                    build_candidate=build_candidate,
                    runtime_storage=runtime_storage,
                    now=now,
                    reasons=[*blockers, *warnings],
                )
            )
            plan_data = read_stored_model(
                RunSwitchPlan,
                {
                    "schema_version": 2,
                    "generated_at": now,
                    "action": "stop",
                    "model_content_sha256": model_digest,
                    "recipe_revision_id": revision.id if revision is not None else None,
                    "recipe_content_sha256": recipe_digest,
                    "alias": run.alias,
                    "run_id": run.id,
                    "spark_group": original_group,
                    "profile_stop_scope": profile_stop_scope,
                    "mapping": service._mapping_selection(mapping, mapping_nodes),
                    "installation_id": installation.id
                    if installation is not None
                    else None,
                    "installation_state": installation.state
                    if installation is not None
                    else None,
                    "recipe_build_id": (
                        installation.recipe_build_id
                        if installation is not None
                        else None
                    ),
                    "image_digest": (
                        installation.image_digest if installation is not None else None
                    ),
                    "start_plan_digest": None,
                    "freshness": freshness,
                    "fit_current": fit_current,
                    "fit_after_stop": None,
                    "fit": fit_current,
                    "storage": storage,
                    "runtime_storage": runtime_storage,
                    "build": build_evidence,
                    "preparation": preparation,
                    "conflicts": [],
                    "stops": stops,
                    "reclaimed_bytes": 0,
                    "phases": phases,
                    "allowed": not blockers,
                    "blockers": blockers,
                    "warnings": warnings,
                    "invocation": invocation,
                    "plan_digest": "0" * 64,
                    "stop_before_prepare": False,
                },
            )
            return service._finalize_plan(plan_data)

    def preview_profile_stop(
        self,
        run_id: str,
        profile_stop_scope: RunSwitchProfileStopScope,
        *,
        actor: str,
    ) -> RunSwitchPlan:
        """Preview a partial Stop authorized only by a FleetProfile review."""
        service = typing_cast("RunSwitchOperationService", self)

        return service.preview_stop(
            run_id,
            actor=actor,
            profile_stop_scope=profile_stop_scope,
        )

    def preview_cleanup(
        self,
        request: RunSwitchCleanupPreviewRequest | str,
        *,
        actor: str,
    ) -> RunSwitchPlan:
        """Re-observe uncertain cleanup authority from a fresh read transaction."""
        service = typing_cast("RunSwitchOperationService", self)
        refused: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return service._preview_cleanup_once(request, actor=actor)
            except UnknownOutcomeError as error:
                refused = error
        assert refused is not None
        raise refused
