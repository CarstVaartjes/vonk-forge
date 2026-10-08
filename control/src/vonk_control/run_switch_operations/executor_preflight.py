"""Executor preflight."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    LifecycleState,
    RunState,
    RunSwitchCode,
    WaitReason,
    canonical_message,
)

from ..inventory_repository import MAX_INVENTORY_FUTURE_SKEW, InventoryRepository
from ..lifecycle_preflight import LifecyclePreflight, LifecyclePreflightCheckpoint
from ..models import (
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    RecipeRun,
    RunNode,
)
from ..offline_stops import pending_run_stop_nodes
from ..recipe_builds import RecipeBuildAdmissionBusy, RecipeBuildPlan
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    build_plan_document,
    parse_stored_build_plan,
)
from ..recipe_operations import (
    RecipeOperationConflict,
)
from ..run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchPlan,
)
from ..strict_json import (
    read_stored_model,
)
from .build_helpers import _container_build_result
from .errors import (
    RunSwitchIssuedWorkloadPending,
    RunSwitchPostStopEvidencePending,
    RunSwitchRetryLater,
    _RunSwitchBuildParentChanged,
)
from .identity_helpers import _started_operation_id, _string_or_none
from .interfaces import PhaseExecution
from .ownership import _lock_current_build_parent
from .planning_helpers import _aware, _now
from .result_helpers import _bound_workload_intent

if TYPE_CHECKING:
    from .executor import RecipeLifecyclePhaseExecutor


class ExecutorPreflightMixin:
    def preflight(self, plan, phase, *, actor, request_key, progress):
        service = typing_cast("RecipeLifecyclePhaseExecutor", self)
        if (
            phase.kind in {"stop", "cleanup", "final_verify", "verify"}
            or phase.state != "planned"
        ):
            return None, None
        with service._sessions() as session:
            revision = session.get(CatalogDocumentRevision, plan.recipe_revision_id)
            if (
                revision is None
                or revision.content_digest != plan.recipe_content_sha256
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.PREFLIGHT_RECIPE_CHANGED,
                    reason=WaitReason.SCOPE_CHANGED,
                )
            document = revision.document
        nodes = {node.node_id: False for node in plan.spark_group.nodes}
        if (
            phase.subphase == "container-build"
            and plan.build.builder_node_id
            or not progress.completed_phases
            and plan.build.state in {"planned", "building"}
            and plan.build.builder_node_id
        ):
            nodes[plan.build.builder_node_id] = True
        previous = progress.preflight
        if service._preflight is None:
            service._preflight = LifecyclePreflight(
                service._sessions,
                service._lifecycle._agent_jobs,
                service._clock,
                service._lifecycle._install_admission._disk_floor,
            )
        return service._preflight.ensure(
            document=document,
            nodes=nodes,
            phase_index=phase.index,
            request_key=request_key,
            actor=actor,
            previous=read_stored_model(
                LifecyclePreflightCheckpoint,
                canonical_message(previous),
                from_json=True,
            )
            if previous
            else None,
        )

    def _require_post_stop_inventory(self, plan: RunSwitchPlan) -> None:
        service = typing_cast("RecipeLifecyclePhaseExecutor", self)
        if not plan.stops:
            return
        now = _now(service._clock)
        expected_pools = {
            node.node_id: node.memory_pool for node in plan.fit_current.nodes
        }
        inventory = InventoryRepository(service._sessions, clock=lambda: now)
        with service._sessions() as session:
            for stop in plan.stops:
                run = session.get(RecipeRun, stop.run_id)
                if run is None or run.plan_digest != stop.run_plan_digest:
                    raise RunSwitchRetryLater(
                        RunSwitchCode.STOPPED_RUN_IDENTITY_CHANGED,
                        reason=WaitReason.SCOPE_CHANGED,
                    )
                members = set(
                    session.scalars(
                        select(RunNode.node_id).where(RunNode.run_id == run.id)
                    )
                )
                if members != set(stop.node_ids):
                    raise RunSwitchRetryLater(
                        RunSwitchCode.STOPPED_RUN_MEMBERSHIP_CHANGED,
                        reason=WaitReason.SCOPE_CHANGED,
                    )
                pending_offline = pending_run_stop_nodes(session, run.id)
                ranks = tuple(
                    session.scalars(select(RunNode).where(RunNode.run_id == run.id))
                )
                reachable_complete = (
                    run.state == RunState.STOPPING
                    and bool(pending_offline)
                    and not (members & expected_pools.keys() & pending_offline)
                    and all(
                        rank.state == RunState.STOPPED
                        or rank.node_id in pending_offline
                        for rank in ranks
                    )
                )
                if not reachable_complete and (
                    run.state != RunState.STOPPED or run.stopped_at is None
                ):
                    raise RunSwitchPostStopEvidencePending(
                        f"Stop receipt for {stop.run_id} is not complete"
                    )
                if not (members & expected_pools.keys()):
                    continue
                assert run.stopped_at is not None or reachable_complete
                stopped_at = (
                    max(
                        _aware(rank.updated_at)
                        for rank in ranks
                        if rank.state == RunState.STOPPED
                    )
                    if reachable_complete
                    else _aware(run.stopped_at or now)
                )
                for node_id in sorted(members & expected_pools.keys()):
                    try:
                        snapshot = inventory.latest(
                            node_id,
                            now=now,
                            maximum_age=service._inventory_max_age,
                            _session=session,
                        )
                    except KeyError:
                        raise RunSwitchPostStopEvidencePending(
                            f"Spark {node_id} has no post-stop inventory"
                        ) from None
                    if snapshot.memory_pool != expected_pools.get(node_id):
                        raise RunSwitchRetryLater(
                            RunSwitchCode.POST_STOP_MEMORY_POOL_CHANGED,
                            reason=WaitReason.SCOPE_CHANGED,
                        )
                    # A sample received after the receipt can still have been
                    # collected before the stop. Preserve the producer clock's
                    # full admitted lead instead of treating receipt time as
                    # collection time or a released promise as physical memory.
                    if (
                        snapshot.stale
                        or _aware(snapshot.received_at) < stopped_at
                        or snapshot.observed_at
                        <= stopped_at + MAX_INVENTORY_FUTURE_SKEW
                    ):
                        raise RunSwitchPostStopEvidencePending(
                            f"Spark {node_id} needs inventory collected after stop {stop.run_id} "
                            f"({(stopped_at + MAX_INVENTORY_FUTURE_SKEW).isoformat()}; "
                            f"latest {snapshot.observed_at.isoformat()})",
                            collected_after=stopped_at + MAX_INVENTORY_FUTURE_SKEW,
                        )

    def _execute_container_build(
        self,
        plan: RunSwitchPlan,
        *,
        phase_index: int,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> PhaseExecution:
        """Start or replay the existing durable ``recipe.build.v1`` child."""
        service = typing_cast("RecipeLifecyclePhaseExecutor", self)

        build_id = plan.recipe_build_id or plan.build.build_id
        revision_id = plan.recipe_revision_id
        if build_id is None or revision_id is None:
            raise RunSwitchRetryLater(
                RunSwitchCode.CONTAINER_BUILD_IDENTITY_UNAVAILABLE,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        expected_build_id = _string_or_none(plan.build.build_id)
        expected_build_input = _string_or_none(plan.build.build_input_sha256)
        if expected_build_id != build_id or expected_build_input is None:
            raise RunSwitchRetryLater(
                RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID, reason=WaitReason.STALE_PLAN
            )
        ordinal = _bound_workload_intent(progress)

        def admission_guard(session: Session) -> None:
            _lock_current_build_parent(
                session,
                plan=plan,
                phase_index=phase_index,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                ordinal=ordinal,
            )

        with service._sessions.begin() as session:
            admission_guard(session)
            build = session.get(RecipeBuild, build_id)
            if build is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_RECEIPT_UNAVAILABLE,
                    reason=WaitReason.RECEIPT_MISSING,
                )
            # Reconnecting bypasses capacity admission, never identity. Both
            # a completed receipt and an active child must match the reviewed
            # executable inputs before either may be adopted.
            if expected_build_input != build.build_input_sha256:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                    reason=WaitReason.STALE_PLAN,
                )
            if build.state == LifecycleState.SUCCEEDED.value:
                return PhaseExecution(result=_container_build_result(build))
            if build.state not in {"planned", "building", LifecycleState.FAILED.value}:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_STATE_INVALID,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            active = session.scalar(
                select(Job)
                .where(
                    Job.kind == "recipe.build.v1",
                    Job.state.in_(
                        (LifecycleState.QUEUED.value, LifecycleState.RUNNING.value)
                    ),
                    Job.payload["owner_id"].as_string() == build.id,
                    Job.payload["plan_digest"].as_string() == build.build_input_sha256,
                )
                .order_by(Job.updated_at.desc(), Job.id)
                .limit(1)
            )
            if active is not None:
                return PhaseExecution(
                    active.id,
                    _container_build_result(build),
                )
            builder_node_id = build.builder_node_id
            build_input_sha256 = build.build_input_sha256
            source_bundle_sha256 = build.source_bundle_sha256
            try:
                stored_plan = build_plan_document(build.plan)
            except RecipeExecutionContractError as error:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                    reason=WaitReason.STALE_PLAN,
                ) from error
        service._require_post_stop_inventory(plan)
        start_build = getattr(service._lifecycle, "build", None)
        if not callable(start_build):
            raise RunSwitchRetryLater(
                RunSwitchCode.CONTAINER_BUILD_EXECUTOR_UNAVAILABLE,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        # Preview already selected and persisted the exact executable build
        # plan in ``RecipeBuild.plan``.  Re-running preview here would admit
        # mutable builder evidence a second time and could derive a different
        # build id/input digest between preview and apply.  Consume the
        # durable producer record instead; the lifecycle build primitive still
        # validates the current builder runtime and resource admission before
        # it queues the child.
        with service._sessions() as session:
            revision = session.get(CatalogDocumentRevision, revision_id)
            try:
                parsed_plan = parse_stored_build_plan(stored_plan)
            except RecipeExecutionContractError as error:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                    reason=WaitReason.STALE_PLAN,
                ) from error
            if (
                parsed_plan.build_id != build_id
                or parsed_plan.recipe_revision_id != revision_id
                or parsed_plan.source_bundle_sha256 != source_bundle_sha256
                or parsed_plan.build_input_sha256 != build_input_sha256
                or revision is None
                or revision.kind != "recipe"
                or revision.state != "active"
                or parsed_plan.recipe_content_sha256 != revision.content_digest
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                    reason=WaitReason.STALE_PLAN,
                )
            try:
                build_plan = RecipeBuildPlan(
                    build_id=build_id,
                    recipe_revision_id=revision_id,
                    recipe_content_sha256=revision.content_digest,
                    builder_node_id=builder_node_id,
                    source_bundle_sha256=source_bundle_sha256,
                    build_input_sha256=build_input_sha256,
                    agent_payload=dict(stored_plan),
                )
            except (TypeError, ValueError) as error:
                raise RunSwitchRetryLater(
                    f"{RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID}: {error}",
                    reason=WaitReason.STALE_PLAN,
                ) from error
        child_key = str(uuid.uuid5(uuid.UUID(request_key), "container-build"))
        try:
            value = start_build(
                build_plan,
                build_input_sha256=build_input_sha256,
                actor=actor,
                request_id=child_key,
                admission_guard=admission_guard,
            )
        except (RecipeBuildAdmissionBusy, _RunSwitchBuildParentChanged):
            raise
        except (
            KeyError,
            RecipeOperationConflict,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise RunSwitchRetryLater(
                f"{RunSwitchCode.CONTAINER_BUILD_START_UNAVAILABLE}: {error}",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        with service._sessions.begin() as session:
            admission_guard(session)
            persisted = session.get(RecipeBuild, build_id)
            if persisted is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_RECEIPT_UNAVAILABLE,
                    reason=WaitReason.RECEIPT_MISSING,
                )
            if (
                persisted.recipe_revision_id != revision_id
                or persisted.build_input_sha256 != build_input_sha256
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                    reason=WaitReason.STALE_PLAN,
                )
            result = _container_build_result(persisted)
            if persisted.state == LifecycleState.SUCCEEDED.value:
                return PhaseExecution(result=result)
        return PhaseExecution(
            _started_operation_id(value),
            result,
        )

    def _observe_older_issued(
        self,
        kind: str,
        owner_id: str,
        ordinal: int,
        *,
        profile_target_node_ids: Sequence[str] | None = None,
    ) -> None:
        service = typing_cast("RecipeLifecyclePhaseExecutor", self)
        pending = service._lifecycle.assess_superseded_issued(
            kind,
            owner_id,
            ordinal,
            profile_target_node_ids=profile_target_node_ids,
        )
        if pending is not None:
            raise RunSwitchIssuedWorkloadPending(
                kind=kind,
                owner_id=owner_id,
                job_id=pending.job_id,
                observe_due_at=pending.observe_due_at,
                observation_deadline=pending.observation_deadline,
            )
