"""Projection for digest-bound recipe operations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentFailureResult,
    AgentInstallResult,
    InstallationNodeState,
    LifecycleState,
    RecipeOperationCode,
    RecipeReconcilePayload,
    RecipeStartPayload,
    RecipeStartResult,
    RunState,
    canonical_message,
)
from vonk_agent_protocol import AgentOperation as WireAgentOperation

from ..distributed_lifecycle import (
    DistributedLifecycleError,
)
from ..distributed_recovery import (
    enforce_recovery_deadline,
)
from ..lifecycle import Outcome
from ..lifecycle.agent_operation import AgentOperationAdapter
from ..lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from ..lifecycle.recipe_operation import RecipeOperationAdapter
from ..models import (
    AgentOperation,
    InstallationNode,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..offline_stops import deferred_stop_nodes
from ..recipe_build_cancellation import (
    build_cancellation,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    run_endpoint_document,
)
from ..recipe_lifecycle_contract import (
    LifecycleCodeFailureResult,
    LifecycleNodeResult,
    RecipeOperationCancellationResult,
    RecipeOperationProgressResult,
    RecipeOperationResult,
)
from ..recipe_progress import (
    _current_phase_index as _current_phase_index,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parent_force_rebuild as _parent_force_rebuild,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parent_identity as _parent_identity,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parent_reconciliation as _parent_reconciliation,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _start_deadline_failure as _start_deadline_failure,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _stored_phases as _stored_phases,  # noqa: PLC0414 -- shared helper export
)
from ..stored_json import read_row_column
from ..strict_json import read_stored_model, serialize_json_value
from .constants import _INITIAL_OBSERVATION_GRACE_SECONDS
from .interfaces import _TERMINAL_JOB_STATES
from .observation_helpers import _PhaseGroups, _start_endpoint, record_build_evidence
from .rank_authority import _run_observes_per_generation
from .results import _RANK_FAILED, _node_result, _recorded_result, _validated_result

if TYPE_CHECKING:
    from .service import RecipeOperationService


class ProjectionMixin:
    def _project_node_result(
        self,
        session: Session,
        job: Job,
        operation: AgentOperation,
        *,
        succeeded: bool,
        evidence: object,
        now: datetime,
    ) -> bool:
        service = typing_cast("RecipeOperationService", self)
        cleanup_queued = False
        locked_job = session.get(Job, job.id, with_for_update=True)
        if locked_job is None:
            # Nothing owns this result any more: it is recorded as residue and
            # the order that carried it stays settled.
            retire_as_unknown(
                "recipe.operation",
                job.id,
                BookkeepingReason.ROW_INCOMPLETE,
                "the operation row disappeared before its result was projected",
            )
            return False
        job = locked_job
        # Lock acquisition can outlive a retained recovery deadline. Every
        # phase/terminal transition uses a fresh time sampled inside the lock.
        now = service._clock()
        operation.updated_at = now
        node_id = operation.node_id
        # A node whose evidence cannot be accepted: the typed marker is recorded
        # in its place and the node is counted failed (its retry or cleanup is
        # the owner's), so the operation completes instead of rolling back.
        node_result = _node_result(job.kind, node_id, evidence)
        unproven: dict[str, str] = {}
        start_wire: RecipeStartPayload | Residue | None = None
        owner_id = _parent_identity(job, "owner_id")
        if owner_id is None:
            retire_as_unknown(
                "recipe.operation",
                job.id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                "the operation payload names no owner",
            )
            owner_id = ""
            unproven[node_id] = "the operation payload names no owner"
        if unproven:
            pass
        elif job.kind == WireAgentOperation.RECIPE_BUILD_CLEANUP.value:
            if succeeded:
                reason = service._apply_build_cleanup(
                    session, job, operation, evidence, owner_id=owner_id, now=now
                )
                if reason is not None:
                    unproven[node_id] = reason
        elif job.kind == WireAgentOperation.RECIPE_BUILD.value:
            build = session.get(RecipeBuild, owner_id, with_for_update=True)
            if build is None or build.builder_node_id != node_id:
                # The build this result belongs to is gone or is another
                # builder's: nothing is recorded on it.
                retire_as_unknown(
                    "recipe.build-result",
                    job.id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the build row is missing or names another builder",
                )
                unproven[node_id] = "recipe build authority is invalid"
            else:
                cancellation = build_cancellation(job)
                if cancellation is not None:
                    # Completion can race cancellation. Retain the authenticated
                    # attempt's outcome, but only its own cancellation governs it.
                    # A later attempt may already own this build and its capacity.
                    if cancellation.cancelled is True:
                        AgentOperationAdapter(session).record_outcome(
                            operation, None, job, Outcome.CANCELLED, now
                        )
                        RecipeOperationAdapter().cancelled(job, now)
                    else:
                        # The cancel stays in flight for the cleanup sweeper; it never
                        # waits for an operator (audit C7).
                        RecipeOperationAdapter().cancel_race(job, now)
                    return False
                force_rebuild = _parent_force_rebuild(job) is True
                recorded_build = succeeded and record_build_evidence(
                    session,
                    build,
                    evidence,
                    now=now,
                )
                if succeeded and not recorded_build:
                    unproven[node_id] = "recipe build evidence is invalid"
                if not recorded_build:
                    # A forced rebuild is an attempt to replace a still-valid
                    # receipt.  Keep that receipt authoritative if the new
                    # attempt fails; the availability operation itself reports
                    # the failed attempt and remains retryable.
                    build.state = (
                        LifecycleState.SUCCEEDED.value
                        if force_rebuild
                        else LifecycleState.FAILED.value
                    )
                    build.error = (
                        unproven[node_id]
                        if node_id in unproven
                        else node_result.reason[:512]
                        if isinstance(node_result, AgentFailureResult)
                        and node_result.reason is not None
                        else "agent build failed"
                    )
                    build.updated_at = now
        elif job.kind == WireAgentOperation.RECIPE_INSTALL.value:
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            assert node is not None
            node.state = (
                InstallationNodeState.INSTALLED
                if succeeded
                else InstallationNodeState.FAILED
            )
            if succeeded:
                installed_bytes = (
                    node_result.installed_bytes
                    if isinstance(node_result, AgentInstallResult)
                    else None
                )
                if (
                    not isinstance(installed_bytes, int)
                    or isinstance(installed_bytes, bool)
                    or installed_bytes < 0
                ):
                    # An install whose byte count cannot be proven is not
                    # installed: the rank is failed and the install retried.
                    node.state = _RANK_FAILED
                    unproven[node_id] = "install evidence is invalid"
                else:
                    node.installed_bytes = installed_bytes
            node.updated_at = now
        elif job.kind in {
            WireAgentOperation.RECIPE_START.value,
            WireAgentOperation.RECIPE_STOP.value,
        }:
            node = session.scalar(
                select(RunNode).where(
                    RunNode.run_id == owner_id, RunNode.node_id == node_id
                )
            )
            assert node is not None
            start_wire = (
                read_or_rebuild(
                    kind="recipe.start-payload",
                    subject=operation.id,
                    read=lambda: read_stored_model(
                        RecipeStartPayload,
                        canonical_message(read_row_column(operation, "payload")),
                        from_json=True,
                    ),
                )
                if job.kind == WireAgentOperation.RECIPE_START.value
                else None
            )
            start_phase = (
                start_wire.phase if isinstance(start_wire, RecipeStartPayload) else None
            )
            if isinstance(start_wire, Residue):
                unproven[node_id] = start_wire.note
                succeeded = False
            if start_phase == "rank-launch":
                if not succeeded:
                    node.state = _RANK_FAILED
                elif node.state != LifecycleState.FAILED.value:
                    node.state = RunState.STARTING
                if succeeded:
                    launch_endpoint = _start_endpoint(operation, evidence)
                    if isinstance(launch_endpoint, Residue):
                        node.state = _RANK_FAILED
                        unproven[node_id] = launch_endpoint.note
                node.updated_at = now
            elif start_phase == "collective-readiness":
                if not succeeded:
                    node.state = _RANK_FAILED
                if succeeded:
                    endpoint = _start_endpoint(operation, evidence)
                    recorded = _recorded_result(
                        job.kind, read_row_column(job, "result"), subject=job.id
                    )
                    launches = (
                        recorded.launch_evidence
                        if isinstance(
                            recorded,
                            (
                                RecipeOperationResult,
                                RecipeOperationProgressResult,
                                RecipeOperationCancellationResult,
                            ),
                        )
                        else None
                    )
                    run_nodes = tuple(
                        session.scalars(
                            select(RunNode).where(RunNode.run_id == owner_id)
                        )
                    )
                    if isinstance(endpoint, Residue):
                        node.state = _RANK_FAILED
                        unproven[node_id] = endpoint.note
                    elif launches is None or any(
                        not isinstance(launches.get(started.node_id), RecipeStartResult)
                        for started in run_nodes
                    ):
                        # Readiness cannot be proven without every rank's launch
                        # evidence: no rank is marked running and the start ends
                        # through its recovery error.
                        node.state = _RANK_FAILED
                        unproven[node_id] = "collective readiness preceded rank launch"
                    else:
                        for started_node in run_nodes:
                            started_node.state = RunState.RUNNING
                            started_node.updated_at = now
                        try:
                            node.endpoint = run_endpoint_document({"url": endpoint})
                        except RecipeExecutionContractError:
                            node.state = _RANK_FAILED
                            unproven[node_id] = (
                                "recipe start endpoint evidence is invalid"
                            )
                node.updated_at = now
            else:
                node.state = (
                    RunState.RUNNING
                    if job.kind == WireAgentOperation.RECIPE_START.value and succeeded
                    else RunState.STOPPED
                    if succeeded
                    else RunState.FAILED
                )
                if job.kind == WireAgentOperation.RECIPE_START.value and succeeded:
                    endpoint = _start_endpoint(operation, evidence)
                    if isinstance(endpoint, Residue):
                        node.state = _RANK_FAILED
                        unproven[node_id] = endpoint.note
                    else:
                        try:
                            if endpoint is not None:
                                node.endpoint = run_endpoint_document({"url": endpoint})
                        except RecipeExecutionContractError:
                            node.state = _RANK_FAILED
                            unproven[node_id] = (
                                "recipe start endpoint evidence is invalid"
                            )
                node.updated_at = now
            if (
                job.kind == WireAgentOperation.RECIPE_START.value
                and succeeded
                and start_phase != "rank-launch"
                and node_id not in unproven
            ):
                run = session.get(RecipeRun, owner_id)
                assert run is not None
                if _run_observes_per_generation(run):
                    run.observation_deadline_at = now + timedelta(
                        seconds=_INITIAL_OBSERVATION_GRACE_SECONDS
                    )
                    for started_node in session.scalars(
                        select(RunNode).where(RunNode.run_id == owner_id)
                    ):
                        started_node.observed_run_generation = None
                        started_node.observation_process_running = None
                        started_node.observation_failure_diagnostics = None
                        started_node.observation_observed_at = None
                        started_node.observation_endpoint_ready = None
        elif job.kind == WireAgentOperation.RECIPE_RECONCILE.value:
            installation = session.get(RecipeInstallation, owner_id)
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            if installation is None or node is None:
                # The rows this cleanup reconciles are gone: nothing is applied.
                retire_as_unknown(
                    "recipe.reconciliation",
                    job.id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "the installation or its node row disappeared",
                )
                unproven[node_id] = "reconciliation scope changed"
            authority = _parent_reconciliation(job)
            authority_targets = authority.targets if authority is not None else []
            target = next(
                (item for item in authority_targets if item.node_id == node_id), None
            )
            try:
                expected = read_stored_model(
                    RecipeReconcilePayload,
                    canonical_message(read_row_column(operation, "payload")),
                    from_json=True,
                )
            except (TypeError, ValueError):
                expected = None
                retire_as_unknown(
                    "recipe.reconciliation",
                    job.id,
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "reconciliation operation payload is invalid",
                )
                unproven.setdefault(
                    node_id, "reconciliation operation payload is invalid"
                )
            if node_id in unproven or installation is None or node is None:
                pass
            elif (
                expected is None
                or authority is None
                or authority.installation_id != owner_id
                or authority.original_plan_digest != expected.plan_digest
                or target is None
                or target.state != "pending"
                or target.rank != node.rank
                or target.role != node.role
                or target.installed_bytes != node.installed_bytes
                or expected.installation_id != owner_id
                or job.targets
                != sorted(
                    item.node_id
                    for item in authority_targets
                    if item.state == "pending"
                )
            ):
                retire_as_unknown(
                    "recipe.reconciliation",
                    job.id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "operation differs from its reviewed authority",
                )
                unproven[node_id] = (
                    "reconciliation operation differs from its reviewed authority"
                )
            else:
                node.state = (
                    InstallationNodeState.UNINSTALLED
                    if succeeded
                    else InstallationNodeState.FAILED
                )
                node.updated_at = now
        elif job.kind == WireAgentOperation.RECIPE_UNINSTALL.value:
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            assert node is not None
            node.state = (
                InstallationNodeState.UNINSTALLED
                if succeeded
                else InstallationNodeState.FAILED
            )
            node.updated_at = now
        recorded_result = _recorded_result(
            job.kind, read_row_column(job, "result"), subject=job.id
        )
        start_payload = (
            start_wire
            if job.kind == WireAgentOperation.RECIPE_START.value
            and isinstance(start_wire, RecipeStartPayload)
            else None
        )
        evidence_field = (
            "launch_evidence"
            if start_payload is not None
            and start_payload.phase in {None, "rank-launch"}
            else "node_evidence"
        )
        node_evidence: dict[str, LifecycleNodeResult] = {}
        if isinstance(
            recorded_result,
            (
                RecipeOperationResult,
                RecipeOperationProgressResult,
                RecipeOperationCancellationResult,
            ),
        ):
            existing_evidence = (
                recorded_result.launch_evidence
                if evidence_field == "launch_evidence"
                else recorded_result.node_evidence
            )
            node_evidence = dict(existing_evidence or {})
        observed_evidence = _node_result(job.kind, node_id, evidence)
        if node_id not in unproven and observed_evidence is None:
            unproven[node_id] = "recipe node evidence is invalid"
        if node_id in unproven:
            observed_evidence = LifecycleCodeFailureResult(
                code=RecipeOperationCode.EVIDENCE_UNPROVEN,
                detail=unproven[node_id][:512],
            )
        assert observed_evidence is not None
        if node_id in node_evidence and node_evidence[node_id] != observed_evidence:
            # The first receipt of a node stands; a differing replay is residue.
            retire_as_unknown(
                "recipe.operation-evidence",
                job.id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                f"a differing receipt was replayed for {node_id}",
            )
        else:
            node_evidence[node_id] = observed_evidence
        job.result = serialize_json_value(
            _validated_result(
                job.kind,
                {
                    **(
                        json.loads(canonical_message(recorded_result))
                        if recorded_result is not None
                        else {}
                    ),
                    evidence_field: node_evidence,
                },
            )
        )
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            )
        )
        all_children = children
        deferred_nodes = deferred_stop_nodes(job)
        children = tuple(
            child for child in children if child.node_id not in deferred_nodes
        )
        # Nodes whose evidence was recorded as unproven (by this or an earlier
        # result of the same operation) count as failed, whatever their order said.
        recorded_for_phases = _recorded_result(
            job.kind, read_row_column(job, "result"), subject=job.id
        )
        marker_nodes = set(unproven)
        if isinstance(
            recorded_for_phases,
            (
                RecipeOperationResult,
                RecipeOperationProgressResult,
                RecipeOperationCancellationResult,
            ),
        ):
            for recorded_field in (
                recorded_for_phases.node_evidence,
                recorded_for_phases.launch_evidence,
            ):
                marker_nodes |= {
                    recorded_node
                    for recorded_node, recorded_item in (recorded_field or {}).items()
                    if isinstance(recorded_item, LifecycleCodeFailureResult)
                    and recorded_item.code == RecipeOperationCode.EVIDENCE_UNPROVEN
                }
        loaded_phases = _stored_phases(job)
        recovery_error: DistributedLifecycleError | None = None
        if isinstance(loaded_phases, Residue):
            # Nothing re-derives how a multi-phase operation was grouped: it ends
            # through its recovery error (its ranks are stopped), never advanced
            # blind and never refused.
            stored_phases: _PhaseGroups = ()
            recovery_error = DistributedLifecycleError(
                "stored operation phases are damaged; the operation was ended"
            )
            for child in children:
                if child.state not in _TERMINAL_JOB_STATES:
                    AgentOperationAdapter(session).record_outcome(
                        child, None, job, Outcome.FAILED, now
                    )
        else:
            stored_phases = tuple(
                group
                for phase in loaded_phases
                if (
                    group := tuple(
                        item for item in phase if item[1] not in deferred_nodes
                    )
                )
            )
        if stored_phases:
            deadline_failure = _start_deadline_failure(job, now=now)
            phase_error: DistributedLifecycleError | None = None
            if deadline_failure is not None:
                phase_error = DistributedLifecycleError(deadline_failure)
            else:
                try:
                    enforce_recovery_deadline(read_row_column(job, "payload"), now=now)
                except DistributedLifecycleError as error:
                    phase_error = error
            if phase_error is not None:
                recovery_error = phase_error
                for child in children:
                    if child.state not in _TERMINAL_JOB_STATES:
                        AgentOperationAdapter(session).record_outcome(
                            child, None, job, Outcome.FAILED, now
                        )
            phase_index = _current_phase_index(children, stored_phases)
            if phase_index is not None:
                phase_operations = {
                    operation_id
                    for operation_id, _node_id, _payload in stored_phases[phase_index]
                }
                phase_children = tuple(
                    child for child in children if child.id in phase_operations
                )
                if any(
                    child.state not in _TERMINAL_JOB_STATES for child in phase_children
                ):
                    RecipeOperationAdapter().project(job, now)
                    return cleanup_queued
                if (
                    all(
                        child.state == LifecycleState.SUCCEEDED.value
                        for child in phase_children
                    )
                    and not any(
                        child.node_id in marker_nodes for child in phase_children
                    )
                    and phase_index + 1 < len(stored_phases)
                    and recovery_error is None
                ):
                    for (
                        next_operation_id,
                        next_node_id,
                        next_payload,
                    ) in stored_phases[phase_index + 1]:
                        service._agent_jobs.enqueue_in_session(
                            session,
                            job.id,
                            next_node_id,
                            job.kind,
                            job.authority_revision,
                            json.loads(canonical_message(next_payload)),
                            operation_id=next_operation_id,
                        )
                    RecipeOperationAdapter().project(job, now)
                    return True
        terminal = all(child.state in _TERMINAL_JOB_STATES for child in children)
        if terminal:
            cleanup_queued = service._project_terminal_result(
                session=session,
                job=job,
                owner_id=owner_id,
                children=children,
                all_children=all_children,
                deferred_nodes=deferred_nodes,
                marker_nodes=marker_nodes,
                node_evidence=node_evidence,
                now=now,
                recovery_error=recovery_error,
                cleanup_queued=cleanup_queued,
            )
        else:
            RecipeOperationAdapter().project(job, now)
        job.updated_at = now
        return cleanup_queued
