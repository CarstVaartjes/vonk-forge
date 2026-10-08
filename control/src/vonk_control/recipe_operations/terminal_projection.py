"""Terminal projection for digest-bound recipe operations."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InstallationState,
    LifecycleState,
    RecipeStopPayload,
    RouteState,
    RunState,
    canonical_message,
)

from ..distributed_lifecycle import (
    DistributedLifecycleError,
)
from ..distributed_recovery import (
    enforce_recovery_deadline,
    recovery_start_plan,
)
from ..job_documents import (
    ProfilePartialStop,
    RecipeStopParent,
)
from ..lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    retire_as_unknown,
)
from ..lifecycle.recipe_operation import RecipeOperationAdapter
from ..models import (
    AgentOperation,
    CatalogDocumentRevision,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..profile_stop_authority import (
    ProfileJobRunStopJob,
)
from ..recipe_lifecycle_contract import (
    LifecycleNodeResult,
    RecipeOperationCancellationResult,
    RecipeOperationProgressResult,
    RecipeOperationResult,
)
from ..recipe_progress import (
    _parent_execution_mode as _parent_execution_mode,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _recorded_parent as _recorded_parent,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _role_phases as _role_phases,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _start_deadline_failure as _start_deadline_failure,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _topology_order as _topology_order,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_stop_payloads import (
    RecipeStopAuthorityError,
    durable_run_stop_payloads,
)
from ..stored_json import read_row_column
from ..strict_json import serialize_json_value
from .intent import _intent_is_current, _job_workload_intent, _shared_workload_intent
from .observation_helpers import _active_recipe_revision
from .rank_authority import _profile_jobrun_parent
from .results import _recorded_result, _recorded_result_document, _validated_result

if TYPE_CHECKING:
    from .service import RecipeOperationService


class TerminalProjectionMixin:
    def _project_terminal_result(
        self,
        session: Session,
        job: Job,
        owner_id: str,
        children: tuple[AgentOperation, ...],
        all_children: tuple[AgentOperation, ...],
        deferred_nodes: frozenset[str],
        marker_nodes: set[str],
        node_evidence: dict[str, LifecycleNodeResult],
        now: datetime,
        recovery_error: DistributedLifecycleError | None,
        cleanup_queued: bool,
    ) -> bool:
        service = typing_cast("RecipeOperationService", self)
        profile_completion_note: str | None = None
        successful = sorted(
            {
                child.node_id
                for child in children
                if child.state == LifecycleState.SUCCEEDED.value
            }
        )
        failed = sorted(
            {
                child.node_id
                for child in children
                if child.state == LifecycleState.FAILED.value
            }
        )
        # A two-phase start has two children on its owner node: the rank
        # launch (succeeded) and the collective readiness (failed).  The
        # node failed; listing it as successful too makes the aggregate
        # result invalid, so the failed readiness could never be recorded.
        failed = sorted(set(failed) | marker_nodes)
        successful = sorted(set(successful) - set(failed))
        reconciliation_complete = False
        reconciliation_error: str | None = None
        if job.kind == WireAgentOperation.RECIPE_RECONCILE.value:
            failed = sorted(
                set(failed)
                | {
                    child.node_id
                    for child in children
                    if child.state != LifecycleState.SUCCEEDED.value
                }
            )
            reconciliation_complete = (
                not failed
                and service._reconciliation_job_complete(session, job, children)
            )
            if not failed and not reconciliation_complete:
                reconciliation_error = "topology cleanup did not uninstall every rank"
        if job.kind == WireAgentOperation.RECIPE_START.value and recovery_error is None:
            late_failure = _start_deadline_failure(job, now=now)
            if late_failure is not None:
                recovery_error = DistributedLifecycleError(late_failure)
            else:
                try:
                    enforce_recovery_deadline(read_row_column(job, "payload"), now=now)
                except DistributedLifecycleError as error:
                    recovery_error = error
        start_failed = bool(failed) or recovery_error is not None
        if _parent_execution_mode(job) == "profile-jobrun-stop":
            failed = sorted(
                set(failed)
                | {
                    child.node_id
                    for child in children
                    if child.state != LifecycleState.SUCCEEDED.value
                }
            )
        job_failed = (
            start_failed
            or reconciliation_error is not None
            or (
                job.kind == WireAgentOperation.RECIPE_RECONCILE.value
                and not reconciliation_complete
            )
            or (_parent_execution_mode(job) == "profile-jobrun-stop" and bool(failed))
        )
        stop_parent = _recorded_parent(job)
        if (
            _parent_execution_mode(job) == "profile-jobrun-stop"
            or (
                isinstance(stop_parent, RecipeStopParent)
                and stop_parent.job_run_stop_authorization is not None
            )
        ) and not job_failed:
            completion = service._complete_jobrun_stop_in_session(
                session,
                job,
                all_children,
                now=now,
                allow_pending=bool(deferred_nodes),
            )
            if isinstance(completion, Residue):
                # The exact Stop receipts cannot be proven against the
                # accepted authority: no old JobRun identity is retired, the
                # Stop ends failed and the profile's own retry answers it.
                profile_completion_note = completion.note[:500] or "unproven"
                failed = sorted(set(failed) | {child.node_id for child in children})
                successful = sorted(set(successful) - set(failed))
                job_failed = True
                recovery_error = DistributedLifecycleError(profile_completion_note)
        RecipeOperationAdapter().finish(job, now, failed=job_failed)
        projected_result = _recorded_result(
            job.kind, read_row_column(job, "result"), subject=job.id
        )
        if isinstance(
            projected_result,
            (
                RecipeOperationResult,
                RecipeOperationProgressResult,
                RecipeOperationCancellationResult,
            ),
        ):
            final_result = RecipeOperationResult(
                successful_nodes=successful,
                failed_nodes=failed,
                node_evidence=projected_result.node_evidence or {},
                launch_evidence=projected_result.launch_evidence,
            )
        else:
            final_result = RecipeOperationResult(
                successful_nodes=successful, failed_nodes=failed, node_evidence={}
            )
        job.result = _validated_result(job.kind, final_result).model_dump(mode="json")
        if recovery_error is not None:
            job.result = _validated_result(
                job.kind,
                {
                    **_recorded_result_document(
                        job.kind, read_row_column(job, "result"), subject=job.id
                    ).model_dump(mode="json"),
                    "recovery_error": (str(recovery_error) or "unproven")[:512],
                },
            ).model_dump(mode="json")
        elif reconciliation_error is not None:
            job.result = _validated_result(
                job.kind,
                {
                    **_recorded_result_document(
                        job.kind, read_row_column(job, "result"), subject=job.id
                    ).model_dump(mode="json"),
                    "recovery_error": reconciliation_error,
                },
            ).model_dump(mode="json")
        if job.kind == WireAgentOperation.RECIPE_BUILD.value:
            # Cancelled attempts returned before terminal aggregation.
            service._release(session, "recipe-build", owner_id, now)
        elif job.kind == WireAgentOperation.RECIPE_INSTALL.value:
            installation = session.get(RecipeInstallation, owner_id)
            assert installation is not None
            installation.state = (
                InstallationState.PARTIAL if failed else InstallationState.INSTALLED
            )
            installation.updated_at = now
        elif job.kind == WireAgentOperation.RECIPE_START.value:
            run = session.get(RecipeRun, owner_id)
            assert run is not None
            if start_failed:
                run.state = RunState.STOPPING
                run.route_state = RouteState.WITHDRAWN
                run.route_error = (
                    f"{recovery_error}; cleanup queued"
                    if recovery_error is not None
                    else "one or more ranks failed to start; cleanup queued"
                )
                cleanup_request_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"vonk:recipe-start-cleanup:{job.id}",
                    )
                )
                intent = _job_workload_intent(session, job)
                intent_current = intent is not None and _intent_is_current(
                    session, intent, job.targets
                )
                # Whatever the failed Start launched is stopped, even when a
                # newer intent took the Sparks over meanwhile: the cleanup is
                # admitted under the intent that owns them now.  Without a
                # single owning intent the run stays ``lost`` (stoppable), so
                # the owner of the Sparks still finds it; it is never left
                # ``failed``, which nothing stops.
                cleanup_ordinal = (
                    intent
                    if intent_current
                    else _shared_workload_intent(session, job.targets)
                )
                if not intent_current:
                    run.state = RunState.STOPPING if cleanup_ordinal else RunState.LOST
                    run.route_error = (
                        "start failed after a newer workload intent took over; "
                        + (
                            "cleanup queued"
                            if cleanup_ordinal
                            else "cleanup awaits the owning intent"
                        )
                    )
                if (
                    not session.scalar(
                        select(Job.id).where(Job.request_id == cleanup_request_id)
                    )
                    and cleanup_ordinal is not None
                ):
                    stop_nodes = tuple(
                        session.scalars(
                            select(RunNode)
                            .where(RunNode.run_id == owner_id)
                            .order_by(RunNode.rank)
                        )
                    )
                    installation = session.get(RecipeInstallation, run.installation_id)
                    revision = (
                        _active_recipe_revision(
                            session, installation.recipe_revision_id
                        )
                        if installation is not None
                        else None
                    )
                    stop_order = (
                        _topology_order(
                            read_row_column(revision, "document"), "stop_order"
                        )
                        if revision is not None
                        else None
                    )
                    try:
                        exact_stop_payloads: Mapping[str, RecipeStopPayload] | None = (
                            durable_run_stop_payloads(
                                session,
                                run,
                                stop_nodes,
                                run_generation=run.run_generation,
                                cancel_pending_start=True,
                                allow_missing_nodes=False,
                            )
                        )
                    except RecipeStopAuthorityError:
                        exact_stop_payloads = None
                    if exact_stop_payloads is None:
                        # No exact Start authority to stop from: nothing is
                        # invented.  The run is recorded failed with its
                        # reason, and the run reconciliation owns what the
                        # Sparks still report.
                        retire_as_unknown(
                            "recipe.start-cleanup",
                            run.id,
                            BookkeepingReason.ROW_INCOMPLETE,
                            "failed Start lacks exact cleanup authority",
                        )
                        run.state = RunState.FAILED
                        run.route_error = (
                            "failed recipe Start lacks exact cleanup authority"
                        )
                    else:
                        stop_node_payloads = tuple(
                            (
                                run_node.node_id,
                                json.loads(
                                    canonical_message(
                                        exact_stop_payloads[run_node.node_id]
                                    )
                                ),
                            )
                            for run_node in stop_nodes
                        )
                        service._queue_in_session(
                            session,
                            kind=WireAgentOperation.RECIPE_STOP.value,
                            owner_kind="run",
                            owner_id=owner_id,
                            plan_digest=run.plan_digest,
                            actor=job.actor,
                            request_id=cleanup_request_id,
                            node_payloads=stop_node_payloads,
                            # Exact relational Stop authority above already
                            # binds every target. An unreadable role order
                            # changes scheduling, never target membership.
                            phases=(
                                _role_phases(stop_order, stop_node_payloads)
                                if stop_order is not None
                                else None
                            )
                            or (stop_node_payloads,),
                            authority_digest=run.plan_digest.removeprefix("sha256:"),
                            now=now,
                            workload_intent_ordinal=cleanup_ordinal,
                        )
                        cleanup_queued = True
            else:
                run.state = RunState.RUNNING
                run.route_state = RouteState.PENDING
                run.route_error = None
            run.updated_at = now
        elif job.kind == WireAgentOperation.RECIPE_STOP.value and deferred_nodes:
            service._finish_offline_stop(session, job, now, failed=bool(failed))
        elif job.kind == WireAgentOperation.RECIPE_STOP.value:
            run = session.get(RecipeRun, owner_id)
            assert run is not None
            stop_parent = _recorded_parent(job)
            partial_scope = (
                stop_parent.profile_partial_stop
                if isinstance(stop_parent, RecipeStopParent)
                else None
            )
            profile_jobrun_partial_targets: set[str] | None = None
            profile_jobrun_partial_nodes: set[str] | None = None
            profile_jobrun_parent: ProfileJobRunStopJob | None = None
            parent_damage: str | None = (
                stop_parent.note if isinstance(stop_parent, Residue) else None
            )
            if _parent_execution_mode(job) == "profile-jobrun-stop":
                loaded_parent = _profile_jobrun_parent(job)
                if isinstance(loaded_parent, Residue):
                    # The accepted scope of this Stop cannot be read: the run
                    # is recorded failed with the reason, never assumed whole.
                    parent_damage = (
                        profile_completion_note
                        or "profile JobRun Stop parent contract is damaged"
                    )
                else:
                    profile_jobrun_parent = loaded_parent
                    authorization = profile_jobrun_parent.profile_stop_authorization
                    if authorization.missing_node_ids:
                        profile_jobrun_partial_targets = set(
                            authorization.reachable_node_ids
                        )
                        profile_jobrun_partial_nodes = set(authorization.run_node_ids)
                        partial_scope = ProfilePartialStop(
                            target_node_ids=authorization.reachable_node_ids,
                            missing_node_ids=authorization.missing_node_ids,
                        )
            if profile_completion_note is not None and parent_damage is None:
                parent_damage = profile_completion_note
            recovery = None
            recovery_error = None
            try:
                recovery = recovery_start_plan(read_row_column(job, "payload"), now=now)
            except DistributedLifecycleError as error:
                recovery_error = error
            recovery_intent = _job_workload_intent(session, job)
            recovery_revision: CatalogDocumentRevision | None = None
            if recovery is not None:
                recovery_installation = session.get(
                    RecipeInstallation, run.installation_id
                )
                recovery_revision = (
                    _active_recipe_revision(
                        session, recovery_installation.recipe_revision_id
                    )
                    if recovery_installation is not None
                    else None
                )
                if (
                    recovery_revision is None
                    or recovery_revision.content_digest is None
                ):
                    # Without the recipe's accepted authority the restart
                    # cannot be queued: the Stop completes as a plain Stop.
                    retire_as_unknown(
                        "recipe.recovery",
                        run.id,
                        BookkeepingReason.ROW_INCOMPLETE,
                        "distributed recovery authority is unavailable",
                    )
                    recovery = None
            if (
                recovery is not None
                and recovery_revision is not None
                and recovery_revision.content_digest is not None
                and recovery_intent is not None
                and not failed
                and _intent_is_current(session, recovery_intent, job.targets)
            ):
                phases, marker = recovery
                revision = recovery_revision
                flattened = tuple(item for phase in phases for item in phase)
                unique_payloads = tuple(
                    {
                        node_id: (node_id, payload)
                        for node_id, payload in reversed(flattened)
                    }.values()
                )
                service._queue_in_session(
                    session,
                    kind=WireAgentOperation.RECIPE_START.value,
                    owner_kind="run",
                    owner_id=owner_id,
                    plan_digest=run.plan_digest,
                    actor=job.actor,
                    request_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"vonk:distributed-recovery-start:{job.id}",
                        )
                    ),
                    node_payloads=tuple(
                        (node_id, serialize_json_value(payload))
                        for node_id, payload in unique_payloads
                    ),
                    phases=tuple(
                        tuple(
                            (node_id, serialize_json_value(payload))
                            for node_id, payload in phase
                        )
                        for phase in phases
                    ),
                    authority_digest=revision.content_digest,
                    now=now,
                    workload_intent_ordinal=recovery_intent,
                    job_context={
                        "recovery": serialize_json_value(marker),
                        "start_deadline": marker.deadline,
                    },
                )
                run.state = RunState.STARTING
                run.route_state = RouteState.WITHDRAWN
                run.route_error = "distributed recovery restarting"
                run.updated_at = now
                cleanup_queued = True
            else:
                partial_targets = (
                    partial_scope.target_node_ids if partial_scope is not None else None
                )
                partial_missing = (
                    partial_scope.missing_node_ids
                    if partial_scope is not None
                    else None
                )
                full_run_nodes = tuple(
                    session.scalars(select(RunNode).where(RunNode.run_id == owner_id))
                )
                expected_job_targets = (
                    sorted(
                        {
                            target.node_id
                            for target in profile_jobrun_parent.profile_stop_authorization.targets
                        }
                    )
                    if profile_jobrun_partial_targets is not None
                    and profile_jobrun_parent is not None
                    else sorted(partial_targets)
                    if isinstance(partial_targets, list)
                    else None
                )
                valid_partial_scope = (
                    isinstance(partial_targets, list)
                    and isinstance(partial_missing, list)
                    and partial_targets == sorted(set(partial_targets))
                    and partial_missing == sorted(set(partial_missing))
                    and partial_targets
                    == sorted(profile_jobrun_partial_targets or set(partial_targets))
                    and expected_job_targets == sorted(set(job.targets))
                    and (
                        profile_jobrun_partial_nodes is None
                        or profile_jobrun_partial_nodes
                        == {node.node_id for node in full_run_nodes}
                    )
                    and set(partial_targets).isdisjoint(partial_missing)
                    and set(partial_targets) | set(partial_missing)
                    == {node.node_id for node in full_run_nodes}
                    and bool(partial_missing)
                    and all(
                        node.state == RunState.STOPPED
                        for node in full_run_nodes
                        if node.node_id in partial_targets
                    )
                    and all(
                        node.state != RunState.STOPPED
                        for node in full_run_nodes
                        if node.node_id in partial_missing
                    )
                )
                partial_success = (
                    valid_partial_scope
                    and not failed
                    and recovery_error is None
                    and parent_damage is None
                )
                run.state = (
                    RunState.LOST
                    if partial_success
                    else RunState.FAILED
                    if failed
                    or recovery_error
                    or partial_scope is not None
                    or parent_damage is not None
                    else RunState.STOPPED
                )
                run.stopped_at = (
                    now
                    if not failed and not partial_scope and parent_damage is None
                    else None
                )
                run.route_state = RouteState.WITHDRAWN
                if parent_damage is not None:
                    run.route_error = parent_damage[:512]
                elif partial_success:
                    run.route_error = (
                        "incomplete multi-Spark model; missing ranks were not stopped"
                    )
                    service._release_node_reservations(
                        session, owner_id, job.targets, now
                    )
                elif recovery_error is not None:
                    run.route_error = str(recovery_error)[:512]
                elif partial_scope is not None and not valid_partial_scope:
                    run.route_error = (
                        "profile partial Stop scope no longer matches the full run"
                    )
                run.updated_at = now
                if (
                    not failed
                    and recovery_error is None
                    and not partial_scope
                    and parent_damage is None
                ):
                    service._release(session, "run", owner_id, now)
        elif job.kind == WireAgentOperation.RECIPE_UNINSTALL.value:
            installation = session.get(RecipeInstallation, owner_id)
            assert installation is not None
            installation.state = (
                InstallationState.FAILED if failed else InstallationState.UNINSTALLED
            )
            installation.updated_at = now
            if not failed:
                service._release(session, "installation", owner_id, now)
        elif job.kind == WireAgentOperation.RECIPE_RECONCILE.value:
            installation = session.get(RecipeInstallation, owner_id)
            assert installation is not None
            installation.state = (
                InstallationState.UNINSTALLED
                if reconciliation_complete
                else InstallationState.PARTIAL
            )
            installation.updated_at = now
            if reconciliation_complete:
                service._release(session, "installation", owner_id, now)

        return cleanup_queued
