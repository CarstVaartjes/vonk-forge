"""Executor phases."""

from __future__ import annotations

import uuid
from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from vonk_agent_protocol import (
    InstallationState,
    InstallDegradedReason,
    RouteState,
    RunState,
    RunSwitchCode,
    WaitReason,
)

from ..cluster_mappings import (
    ClusterMappingPlan,
)
from ..install_admission import (
    InstallAdmissionBusy,
    InstallPreflightExpired,
)
from ..logging import redact_text
from ..models import (
    STOPPABLE_RUN_STATES,
    RecipeInstallation,
    RecipeRun,
)
from ..offline_stops import pending_run_stop_nodes
from ..profile_capacity import (
    ProfileHandoffInconsistent,
    prepared_profile_installation,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_installation_plan,
)
from ..recipe_operations import (
    RecipeArtifactJobCancellationPending,
    RecipeInstallPreflightExpired,
    RecipeOperationConflict,
)
from ..run_admission import (
    RunAdmissionBusy,
)
from ..run_switch_contract import (
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPlan,
    RunSwitchRuntimeInstallResult,
    RunSwitchRuntimePlanResult,
    RunSwitchStartResult,
    RunSwitchStopResult,
)
from .artifact_validation import _validate_artifact_execution
from .errors import (
    RunSwitchInstallPreflightExpired,
    RunSwitchIssuedWorkloadPending,
    RunSwitchRetryLater,
    _RunSwitchDefiniteConflict,
    _RunSwitchIncompleteProfileGroupConflict,
)
from .identity_helpers import (
    _mapping_node,
    _required_string,
    _started_operation_id,
    _string_or_none,
)
from .interfaces import PhaseExecution
from .observation_helpers import _stop_child_request_key
from .planning_helpers import _now, _required_int
from .result_helpers import _bound_workload_intent, _phase_result

if TYPE_CHECKING:
    from .executor import RecipeLifecyclePhaseExecutor


class ExecutorPhasesMixin:
    def execute(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> PhaseExecution:
        service = typing_cast("RecipeLifecyclePhaseExecutor", self)
        if phase.kind in {"transfer", "verify", "cleanup"}:
            if service._artifact_executor is None:
                raise RunSwitchRetryLater(
                    f"run-switch.{phase.kind}-executor-unavailable"
                )
            execution = service._artifact_executor.execute(
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
            if execution.operation_id is None and execution.waiting:
                raise RunSwitchRetryLater(
                    f"run-switch.{phase.kind}-waiting-without-child"
                )
            if (
                execution.operation_id is None
                and execution.result is None
                and not execution.waiting
            ):
                raise RunSwitchRetryLater(
                    f"run-switch.{phase.kind}-returned-no-evidence"
                )
            if execution.operation_id is None:
                _validate_artifact_execution(plan, phase, execution.result)
            return execution
        if phase.kind == "prepare" and phase.subphase == "runtime-image":
            if service._artifact_executor is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.RUNTIME_IMAGE_EXECUTOR_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            execution = service._artifact_executor.execute(
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
            if (
                execution.operation_id is None
                and execution.waiting
                and phase.subphase != "runtime-image"
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.RUNTIME_IMAGE_WAITING_WITHOUT_CHILD,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            if execution.operation_id is None and not execution.waiting:
                _validate_artifact_execution(plan, phase, execution.result)
            return execution
        if phase.kind == "stop":
            if item_index >= len(plan.stops):
                return PhaseExecution()
            target = plan.stops[item_index]
            ordinal = _bound_workload_intent(progress)
            stop_digest = target.plan_digest
            profile_target_node_ids = (
                plan.profile_stop_scope.target_node_ids
                if plan.profile_stop_scope is not None
                else None
            )
            service._lifecycle.reconcile_superseded_unissued(
                "recipe.stop",
                target.run_id,
                ordinal,
                profile_target_node_ids=profile_target_node_ids,
            )
            if profile_target_node_ids is not None:
                service._observe_older_issued(
                    "recipe.stop",
                    target.run_id,
                    ordinal,
                    profile_target_node_ids=profile_target_node_ids,
                )
            if target.state in {RunState.STARTING, RunState.STOPPING}:
                # A newer explicit Stop can cancel an older same-run command
                # under the current node ordinal.  Re-preview this exact run
                # because its prior Start/Stop may have changed state after
                # the high-level plan was reviewed.  The low-level Stop
                # authority validates current membership and reservations.
                with service._sessions() as session:
                    run = session.get(RecipeRun, target.run_id)
                    if run is None:
                        raise RunSwitchRetryLater(RunSwitchCode.STOP_TARGET_DISAPPEARED)
                    if (
                        run.state == RunState.STOPPED
                        and run.route_state == RouteState.WITHDRAWN
                    ):
                        return PhaseExecution(
                            result=_phase_result({"run_id": target.run_id}, phase=phase)
                        )
                fresh = service._lifecycle.preview_stop(
                    target.run_id,
                    profile_target_node_ids=profile_target_node_ids,
                )
                if not fresh.allowed:
                    raise RunSwitchRetryLater(
                        RunSwitchCode.STOP_STILL_UNRESOLVED_AFTER_CANCELLATION
                    )
                stop_digest = fresh.plan_digest
            profile_application_id = _string_or_none(progress.profile_application_id)
            child_key = _stop_child_request_key(
                request_key, target.run_id, profile_application_id
            )
            try:
                value = service._lifecycle.stop(
                    target.run_id,
                    plan_digest=stop_digest,
                    actor=actor,
                    request_id=child_key,
                    workload_intent_ordinal=ordinal,
                    profile_target_node_ids=profile_target_node_ids,
                    profile_application_id=profile_application_id,
                )
            except RecipeArtifactJobCancellationPending as pending:
                raise RunSwitchIssuedWorkloadPending(
                    kind="artifact-job-cancellation",
                    owner_id=target.run_id,
                    job_id=pending.job_id,
                    observe_due_at=pending.observe_due_at,
                    observation_deadline=pending.observation_deadline,
                ) from pending
            return PhaseExecution(
                value.id, _phase_result({"run_id": target.run_id}, phase=phase)
            )
        if phase.kind == "prepare" and phase.subphase == "container-build":
            return service._execute_container_build(
                plan,
                phase_index=phase.index,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
        if phase.kind == "prepare" and phase.subphase == "runtime-plan":
            # Bind and persist the exact schema-2 launch plan at the
            # Controller boundary.  This phase deliberately does not enqueue
            # Spark work: target-copy must verify every model/image receipt
            # before the agent install child can start.
            mapping_id = plan.mapping.mapping_id if plan.mapping is not None else None
            phase_results = progress.phase_results
            if phase_results:
                for result in reversed(phase_results):
                    if isinstance(result, RunSwitchRuntimePlanResult):
                        mapping_id = result.mapping_id or mapping_id
            if (
                mapping_id is None
                and plan.mapping is not None
                and plan.mapping.action == "create"
            ):
                mapping_plan = ClusterMappingPlan(
                    recipe_revision_id=_required_string(plan.recipe_revision_id),
                    recipe_content_sha256=_required_string(plan.recipe_content_sha256),
                    topology_name=plan.mapping.topology_name,
                    generation=_required_int(plan.mapping.mapping_generation) or 1,
                    parameters=dict(plan.mapping.parameters),
                    nodes=tuple(_mapping_node(node) for node in plan.mapping.nodes),
                    placement_digest=plan.mapping.placement_digest,
                )
                mapping_id = service._mappings.materialize(
                    mapping_plan, actor=actor, now=_now(service._clock)
                )
            if mapping_id is None:
                return PhaseExecution(
                    result=_phase_result({"prepared": True}, phase=phase)
                )
            profile_application_id = _string_or_none(progress.profile_application_id)
            if profile_application_id is not None:
                with service._sessions() as session:
                    try:
                        handed_off = prepared_profile_installation(
                            session,
                            profile_application_id,
                            _required_string(plan.recipe_revision_id),
                            tuple(node.node_id for node in plan.spark_group.nodes),
                            workload_intent_ordinal=_bound_workload_intent(progress),
                        )
                    except ProfileHandoffInconsistent as error:
                        raise RunSwitchRetryLater(
                            f"{RunSwitchCode.INSTALLATION_HANDOFF_INCONSISTENT}: {error}",
                            reason=WaitReason.SCOPE_CHANGED,
                        ) from error
                    if handed_off is not None:
                        installation_id, install_plan_digest = handed_off
                        installation, _ = service._bound_installation(
                            session,
                            plan,
                            installation_id,
                            mapping_id,
                            install_plan_digest,
                        )
                        if installation.state not in {
                            InstallationState.PLANNED,
                            InstallationState.INSTALLING,
                            InstallationState.PARTIAL,
                            InstallationState.INSTALLED,
                        }:
                            raise RunSwitchRetryLater(
                                RunSwitchCode.INSTALLATION_HANDOFF_UNAVAILABLE,
                                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                            )
                        stored = parse_stored_installation_plan(installation.plan)
                        if stored.plan_digest != install_plan_digest:
                            raise RunSwitchRetryLater(
                                RunSwitchCode.INSTALLATION_IDENTITY_CHANGED,
                                reason=WaitReason.SCOPE_CHANGED,
                            )
                        return service._prepared_installation_result(
                            installation_id,
                            mapping_id,
                            install_plan_digest,
                            stored.compiled_execution_plans,
                        )
            try:
                install_plan = service._lifecycle.preview_install(
                    mapping_id,
                    plan.recipe_build_id,
                    profile_application_id=_string_or_none(
                        progress.profile_application_id
                    ),
                )
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise RunSwitchRetryLater(
                    f"{RunSwitchCode.INSTALL_PLAN_UNAVAILABLE}: {error}",
                    reason=WaitReason.STALE_PLAN,
                ) from error
            prepare_installation = getattr(
                service._lifecycle, "prepare_installation", None
            )
            if not callable(prepare_installation):
                raise RunSwitchRetryLater(
                    RunSwitchCode.INSTALL_PREPARATION_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            try:
                installation_id = prepare_installation(
                    install_plan,
                    actor=actor,
                    profile_application_id=_string_or_none(
                        progress.profile_application_id
                    ),
                    workload_intent_ordinal=_bound_workload_intent(progress),
                )
            except (RecipeInstallPreflightExpired, InstallPreflightExpired) as error:
                # Compiling the launch document above can outlast the runtime
                # preflight window this phase was admitted on.  Nothing else
                # about the install changed, so ask the caller to rerun the
                # ordinary probe rather than failing an identical plan.
                raise RunSwitchInstallPreflightExpired(
                    f"{RunSwitchCode.INSTALL_PREFLIGHT_EXPIRED}: {error}"
                ) from error
            except InstallAdmissionBusy:
                raise
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise RunSwitchRetryLater(
                    f"{RunSwitchCode.INSTALL_PREPARATION_FAILED}: {error}"
                ) from error
            prepared_id = _required_string(installation_id)
            with service._sessions() as session:
                installation = session.get(RecipeInstallation, prepared_id)
                if installation is None:
                    raise RunSwitchRetryLater(
                        RunSwitchCode.INSTALL_PREPARATION_UNAVAILABLE,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                try:
                    stored_plan = parse_stored_installation_plan(installation.plan)
                except RecipeExecutionContractError as error:
                    raise RunSwitchRetryLater(
                        RunSwitchCode.INSTALLATION_IDENTITY_UNAVAILABLE,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    ) from error
            return service._prepared_installation_result(
                prepared_id,
                mapping_id,
                stored_plan.plan_digest,
                stored_plan.compiled_execution_plans,
            )
        if phase.kind == "prepare" and phase.subphase == "runtime-install":
            ordinal = _bound_workload_intent(progress)
            installation_id = plan.installation_id
            phase_results = progress.phase_results
            if installation_id is None and isinstance(phase_results, list):
                for result in reversed(phase_results):
                    if isinstance(
                        result,
                        RunSwitchRuntimePlanResult | RunSwitchRuntimeInstallResult,
                    ):
                        installation_id = result.installation_id
                        break
            if installation_id is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.INSTALLATION_PREPARATION_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            service._lifecycle.reconcile_superseded_unissued(
                "recipe.install", installation_id, ordinal
            )
            service._observe_older_issued("recipe.install", installation_id, ordinal)
            start_installation = getattr(service._lifecycle, "start_installation", None)
            if not callable(start_installation):
                raise RunSwitchRetryLater(
                    RunSwitchCode.INSTALL_EXECUTOR_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            try:
                value = start_installation(
                    installation_id,
                    actor=actor,
                    request_id=str(
                        uuid.uuid5(uuid.UUID(request_key), "runtime-install")
                    ),
                    workload_intent_ordinal=ordinal,
                )
            except InstallAdmissionBusy:
                raise
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise RunSwitchRetryLater(
                    f"{RunSwitchCode.INSTALL_START_FAILED}: {error}"
                ) from error
            return PhaseExecution(
                _started_operation_id(value),
                _phase_result({"installation_id": installation_id}, phase=phase),
            )
        if phase.kind == "prepare":
            raise _RunSwitchDefiniteConflict(RunSwitchCode.PREPARE_SUBPHASE_UNSUPPORTED)
        if phase.kind == "start":
            ordinal = _bound_workload_intent(progress)
            installation_id = plan.installation_id
            phase_results = progress.phase_results
            if installation_id is None and isinstance(phase_results, list):
                for result in reversed(phase_results):
                    if isinstance(
                        result,
                        RunSwitchRuntimePlanResult | RunSwitchRuntimeInstallResult,
                    ):
                        installation_id = result.installation_id
                        break
            if installation_id is None or plan.alias is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.START_INSTALLATION_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            start_request_id = str(uuid.uuid5(uuid.UUID(request_key), "start"))
            # Adopt the start this phase already queued before re-previewing.
            # Run admission hashes inventory observation time and current
            # reservations, so a refreshed inventory re-derives a different
            # plan digest for the identical child and the idempotency check
            # would have rejected our own durable request key.
            adopted = service._lifecycle.adopt_start(
                installation_id, plan.alias, request_id=start_request_id
            )
            if adopted is not None:
                return PhaseExecution(
                    adopted.id, _phase_result({"run_id": adopted.owner_id}, phase=phase)
                )
            service._require_post_stop_inventory(plan)
            service._lifecycle.reconcile_superseded_unissued(
                "recipe.uninstall", installation_id, ordinal
            )
            service._observe_older_issued("recipe.uninstall", installation_id, ordinal)
            profile_application_id = _string_or_none(progress.profile_application_id)
            low_level = service._lifecycle.preview_run(
                installation_id,
                plan.alias,
                profile_application_id=profile_application_id,
            )
            value = service._lifecycle.start(
                low_level,
                plan_digest=low_level.plan_digest,
                actor=actor,
                request_id=start_request_id,
                workload_intent_ordinal=ordinal,
                profile_application_id=profile_application_id,
            )
            return PhaseExecution(
                value.id, _phase_result({"run_id": value.owner_id}, phase=phase)
            )
        if phase.kind == "uninstall":
            ordinal = _bound_workload_intent(progress)
            installation_id = plan.installation_id
            if installation_id is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.UNINSTALL_TARGET_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            if (
                plan.cleanup_mode == "reconcile"
                and plan.cleanup_disposition != "abandon"
            ):
                authority = plan.reconciliation_authority
                if authority is None:
                    raise RunSwitchRetryLater(
                        RunSwitchCode.RECONCILIATION_AUTHORITY_UNAVAILABLE,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                reconcile_request_id = str(
                    uuid.uuid5(uuid.UUID(request_key), "reconcile")
                )
                adopted = service._lifecycle.adopt_owned_operation(
                    reconcile_request_id,
                    kind="recipe.reconcile",
                    owner_kind="installation",
                    owner_id=installation_id,
                )
                if adopted is not None:
                    return PhaseExecution(
                        adopted.id,
                        _phase_result(
                            {"installation_id": installation_id}, phase=phase
                        ),
                    )
                service._lifecycle.reconcile_superseded_unissued(
                    "recipe.reconcile", installation_id, ordinal
                )
                service._observe_older_issued(
                    "recipe.reconcile", installation_id, ordinal
                )
                try:
                    value = service._lifecycle.reconcile_installation(
                        installation_id,
                        expected_authority=authority,
                        run_switch_plan_digest=plan.plan_digest,
                        actor=actor,
                        request_id=reconcile_request_id,
                        workload_intent_ordinal=ordinal,
                    )
                except RunAdmissionBusy:
                    # A competing capacity writer is recoverable.  Preserve
                    # the exact phase checkpoint so the outer service parks
                    # this operation and retries after the writer releases
                    # its rows instead of turning it into a terminal
                    # reconciliation conflict.
                    raise
                except InstallAdmissionBusy:
                    raise
                except (
                    KeyError,
                    RecipeOperationConflict,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise RunSwitchRetryLater(
                        f"{RunSwitchCode.RECONCILIATION_START_FAILED}: {error}"
                    ) from error
                return PhaseExecution(
                    value.id,
                    _phase_result({"installation_id": installation_id}, phase=phase),
                )
            if plan.cleanup_disposition == "abandon":
                # The installation's own assessment proved the plan never
                # reached a node, so no agent order is queued.  The lifecycle
                # re-checks that disposition under the row lock and records the
                # disposal on the cleanup operation's receipt.
                try:
                    abandoned = service._lifecycle.abandon_never_installed(
                        installation_id
                    )
                except (
                    KeyError,
                    RecipeOperationConflict,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise RunSwitchRetryLater(
                        f"{RunSwitchCode.UNINSTALL_ABANDON_FAILED}: {error}"
                    ) from error
                return PhaseExecution(
                    result=_phase_result(
                        {
                            **abandoned,
                            "reason": InstallDegradedReason.INSTALLATION_NOT_INSTALLED,
                        },
                        phase=phase,
                    )
                )
            uninstall_request_id = str(uuid.uuid5(uuid.UUID(request_key), "uninstall"))
            # Reconnect to the removal this operation already queued before
            # asking for a fresh assessment, so a restart never creates a
            # second removal for the same installation.
            adopted = service._lifecycle.adopt_owned_operation(
                uninstall_request_id,
                kind="recipe.uninstall",
                owner_kind="installation",
                owner_id=installation_id,
            )
            if adopted is not None:
                return PhaseExecution(
                    adopted.id,
                    _phase_result({"installation_id": installation_id}, phase=phase),
                )
            service._lifecycle.reconcile_superseded_unissued(
                "recipe.uninstall", installation_id, ordinal
            )
            service._observe_older_issued("recipe.uninstall", installation_id, ordinal)
            try:
                uninstall_plan = service._lifecycle.preview_uninstall(installation_id)
                value = service._lifecycle.uninstall(
                    installation_id,
                    plan_digest=uninstall_plan.plan_digest,
                    actor=actor,
                    request_id=uninstall_request_id,
                    workload_intent_ordinal=ordinal,
                )
            except RunAdmissionBusy:
                # Capacity contention is a retryable admission outcome.  Do
                # not wrap it as uninstall-start-failed; the parent service
                # will release its transaction and schedule the same exact
                # uninstall attempt again.
                raise
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise RunSwitchRetryLater(
                    f"{RunSwitchCode.UNINSTALL_START_FAILED}: {error}"
                ) from error
            return PhaseExecution(
                value.id,
                _phase_result({"installation_id": installation_id}, phase=phase),
            )
        if phase.kind == "final_verify":
            if plan.action == "cleanup":
                return service._verify_cleanup(plan, request_key=request_key)
            if plan.action == "install":
                return service._verify_installation(plan, progress)
            run_id = plan.run_id
            phase_results = progress.phase_results
            if run_id is None and isinstance(phase_results, list):
                for result in reversed(phase_results):
                    if isinstance(result, RunSwitchStartResult | RunSwitchStopResult):
                        run_id = result.run_id
                        break
            if run_id is None or service._lifecycle is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.FINAL_VERIFICATION_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            status = service._lifecycle.run_status(run_id)
            if plan.action == "stop":
                scope = plan.profile_stop_scope
                if scope is not None:
                    reachable_stopped = (
                        status.state == RunState.LOST
                        and status.route_state == RouteState.WITHDRAWN
                        and all(
                            rank.state == RunState.STOPPED
                            for rank in status.ranks
                            if rank.node_id in scope.target_node_ids
                        )
                    )
                    if reachable_stopped:
                        missing_ranks = [
                            rank
                            for rank in status.ranks
                            if rank.node_id in scope.missing_node_ids
                        ]
                        raise _RunSwitchIncompleteProfileGroupConflict(
                            f"{RunSwitchCode.PROFILE_INCOMPLETE_MULTI_SPARK_MODEL}: "
                            f"{plan.alias or run_id} was removed from service after "
                            "stopping reachable ranks; missing Spark ranks may still "
                            "be running: "
                            + ", ".join(
                                f"rank {rank.rank} ({rank.node_id})"
                                for rank in missing_ranks
                            )
                        )
                with service._sessions() as session:
                    pending_offline = pending_run_stop_nodes(session, run_id)
                verified = (
                    (
                        status.state == RunState.STOPPED
                        or (status.state == RunState.STOPPING and bool(pending_offline))
                    )
                    and status.route_state == RouteState.WITHDRAWN
                    and all(
                        rank.state == RunState.STOPPED
                        or rank.node_id in pending_offline
                        for rank in status.ranks
                    )
                )
                waiting = (
                    status.state in STOPPABLE_RUN_STATES
                    or status.route_state
                    in {
                        RouteState.PENDING,
                    }
                )
                status_reason = (
                    f"{RunSwitchCode.STOP_VERIFICATION_PENDING}: run {run_id} is "
                    f"{status.state}, route is {status.route_state}"
                )
            else:
                verified = status.healthy and status.route_state == RouteState.PUBLISHED
                waiting = False
                status_reason = None
                route_error = (
                    redact_text(status.route_error)
                    if status.route_error is not None
                    else None
                )
                if not verified:
                    if status.state in {
                        RunState.FAILED,
                        RunState.LOST,
                        RunState.STOPPED,
                    }:
                        detail = route_error or "run owner reached a terminal state"
                        raise RunSwitchRetryLater(
                            f"{RunSwitchCode.RUN_OWNER_TERMINAL}: {status.state}; {detail}"
                        )
                    if status.route_state == RouteState.FAILED:
                        detail = route_error or "route owner reported terminal failure"
                        raise RunSwitchRetryLater(
                            f"{RunSwitchCode.ROUTE_OWNER_FAILED}: {detail}"
                        )
                    waiting = True
                    route_cause = f"route is {status.route_state}"
                    if status.route_state == RouteState.PENDING:
                        if status.route_next_attempt_at is not None:
                            route_cause = (
                                "route publication is pending; route owner next "
                                f"attempt {status.route_next_attempt_at.isoformat()}"
                            )
                        elif status.observation_deadline_at is not None:
                            route_cause = (
                                "route publication is pending; route owner has no "
                                "retry scheduled; initial observation deadline "
                                f"{status.observation_deadline_at.isoformat()}"
                            )
                        else:
                            route_cause = (
                                "route publication is pending; route owner next "
                                "attempt is not scheduled"
                            )
                    elif status.route_state == RouteState.WITHDRAWN:
                        route_cause = (
                            f"route is withdrawn; cause {route_error or 'unknown'}"
                        )
                    if status.recovery_owners:
                        owners = ", ".join(
                            f"{owner.kind} {owner.operation_id} ({owner.state})"
                            for owner in status.recovery_owners
                        )
                        status_reason = (
                            f"{RunSwitchCode.DISTRIBUTED_RECOVERY_ACTIVE}: {owners}; run "
                            f"{run_id} generation {status.run_generation}; {route_cause}"
                        )
                    elif status.route_recovery_pending:
                        status_reason = (
                            f"{RunSwitchCode.ROUTE_HEALTH_RECOVERY_ACTIVE}: route owner is "
                            f"reconciling run {run_id} generation {status.run_generation}; "
                            f"{route_cause}"
                        )
                    elif status.state in {RunState.STARTING, RunState.STOPPING}:
                        status_reason = (
                            f"{RunSwitchCode.RUN_OWNER_ACTIVE}: run {run_id} generation "
                            f"{status.run_generation} is {status.state}; {route_cause}"
                        )
                    elif status.route_state == RouteState.PENDING:
                        status_reason = (
                            f"{RunSwitchCode.ROUTE_PUBLICATION_PENDING}: run {run_id} "
                            f"generation {status.run_generation}; {route_cause}"
                        )
                    elif status.route_state == RouteState.WITHDRAWN:
                        cause = route_error or "no terminal route-owner cause recorded"
                        status_reason = (
                            f"{RunSwitchCode.ROUTE_WITHDRAWN_OWNER_UNKNOWN}: run {run_id} "
                            f"generation {status.run_generation} remains {status.state}; "
                            f"route cause {cause}; waiting for exact reconciliation"
                        )
                    else:
                        status_reason = (
                            f"{RunSwitchCode.FINAL_OWNER_STATE_UNKNOWN}: run {run_id} "
                            f"generation {status.run_generation} is {status.state}; "
                            f"route is {status.route_state}; waiting for exact reconciliation"
                        )
            evidence = {
                "run_id": run_id,
                "state": status.state,
                "route_state": status.route_state,
                "healthy": status.healthy,
                "ranks": [
                    {
                        "node_id": rank.node_id,
                        "rank": rank.rank,
                        "role": rank.role,
                        "state": rank.state,
                        "fresh": rank.fresh,
                    }
                    for rank in status.ranks
                ],
            }
            if verified:
                return PhaseExecution(
                    result=_phase_result(
                        {"final_verified": True, **evidence}, phase=phase
                    )
                )
            if waiting:
                return PhaseExecution(
                    result=_phase_result(
                        {"final_verified": False, **evidence}, phase=phase
                    ),
                    waiting=True,
                    status_reason=status_reason,
                )
            raise RunSwitchRetryLater(RunSwitchCode.FINAL_VERIFICATION_FAILED)
        return PhaseExecution()
