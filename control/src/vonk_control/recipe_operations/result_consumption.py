"""Result consumption for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    AgentFailureKind,
    AgentFailureResult,
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    RecipeBuildCleanupEvidence,
    RecipeBuildCleanupRequest,
    RecipeStartPayload,
    RecipeStopPayload,
    RouteState,
    RunState,
    UnknownOutcomeError,
    canonical_message,
    validate_result_for_operation,
)
from vonk_agent_protocol import AgentOperation as WireAgentOperation

from .. import agent_operation_states
from ..admission_locking import (
    admission_attempts,
    admission_wait_exhausted,
)
from ..job_documents import (
    RecipeBuildCleanupParent,
)
from ..lifecycle import Outcome
from ..lifecycle.agent_operation import AgentOperationAdapter, retry_scheduled
from ..lifecycle.evidence import (
    BookkeepingReason,
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
from ..recipe_build_cancellation import (
    build_cancellation,
)
from ..recipe_execution_contract import parse_stored_run_plan
from ..recipe_lifecycle_contract import (
    LifecycleNodeResult,
)
from ..recipe_progress import (
    _cancel_requested as _cancel_requested,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parent_identity as _parent_identity,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parent_reconciliation as _parent_reconciliation,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parse_recipe_parent as _parse_recipe_parent,  # noqa: PLC0414 -- shared helper export
)
from ..stored_json import read_row_column
from ..strict_json import read_stored_model, serialize_json_value
from .constants import _WORKLOAD_INTENT_KINDS
from .errors import RecipeRequestInvalid, RecipeRetryLater
from .intent import _intent_is_current, _job_workload_intent
from .interfaces import _TERMINAL_JOB_STATES, RecipeOperationView
from .orphan_cleanup import consume_orphan_cleanup
from .results import _RANK_FAILED, _node_result, _unproven_evidence

if TYPE_CHECKING:
    from .service import RecipeOperationService


class ResultConsumptionMixin:
    def record_node_result(
        self,
        operation_id: str,
        node_id: str,
        *,
        succeeded: bool,
        evidence: object,
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        refused: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return service._record_node_result_once(
                    operation_id, node_id, succeeded=succeeded, evidence=evidence
                )
            except UnknownOutcomeError as error:
                refused = error
                if admission_wait_exhausted(error):
                    break
        assert refused is not None
        raise refused

    def _record_node_result_once(
        self,
        operation_id: str,
        node_id: str,
        *,
        succeeded: bool,
        evidence: object,
    ) -> RecipeOperationView:
        service = typing_cast("RecipeOperationService", self)
        now = service._clock()
        with service._sessions.begin() as session:
            job = session.get(Job, operation_id)
            if job is None or not job.kind.startswith("recipe."):
                raise RecipeRetryLater("recipe result owner observation is unavailable")
            operations = tuple(
                session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == job.id,
                        AgentOperation.node_id == node_id,
                    )
                )
            )
            active_operations = tuple(
                item for item in operations if item.state not in _TERMINAL_JOB_STATES
            )
            operation = (
                active_operations[0]
                if len(active_operations) == 1
                else operations[0]
                if len(operations) == 1
                else None
            )
            if operation is None:
                raise RecipeRequestInvalid("node is not part of operation group")
            AgentOperationAdapter(session).record_outcome(
                operation,
                None,
                job,
                Outcome.DONE if succeeded else Outcome.FAILED,
                now,
            )
            cleanup_queued = service._project_node_result(
                session,
                job,
                operation,
                succeeded=succeeded,
                evidence=evidence,
                now=now,
            )
        if cleanup_queued:
            service._agent_jobs.notify_available()
        return service.get(operation_id)

    def consume_agent_result(
        self,
        session: Session,
        operation: AgentOperation,
        _attempt: object,
        message: object,
    ) -> None:
        """Project an authenticated agent result in the queue transaction."""
        service = typing_cast("RecipeOperationService", self)
        job = session.get(Job, operation.parent_job_id)
        if job is None or not job.kind.startswith("recipe."):
            return
        if job.kind == WireAgentOperation.RECIPE_JOB_RUN.value:
            return
        state = getattr(message, "state", None)
        result = getattr(message, "result", None)
        if (
            state == LifecycleState.CANCELLED.value
            and job.kind in _WORKLOAD_INTENT_KINDS
        ):
            # Authentication, attempt fencing and cancellation authorization
            # belong to agent_jobs. This consumer only projects its receipt.
            owner_id = _parent_identity(job, "owner_id")
            if owner_id is None:
                retire_as_unknown(
                    "recipe.operation",
                    job.id,
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "the cancelled operation payload names no owner",
                )
                return
            now = service._clock()
            if job.kind in {
                WireAgentOperation.RECIPE_INSTALL.value,
                WireAgentOperation.RECIPE_UNINSTALL.value,
                WireAgentOperation.RECIPE_RECONCILE.value,
            }:
                node = session.scalar(
                    select(InstallationNode)
                    .where(
                        InstallationNode.installation_id == owner_id,
                        InstallationNode.node_id == operation.node_id,
                    )
                    .with_for_update(of=InstallationNode)
                )
                installation = session.get(
                    RecipeInstallation, owner_id, with_for_update=True
                )
                # The queue validated the exact attempt before calling this
                # projection. Missing SQL rows must not roll its receipt back.
                if node is not None:
                    node.state = _RANK_FAILED
                    node.updated_at = now
                if installation is not None:
                    installation.state = InstallationState.PARTIAL
                    installation.updated_at = now
            elif job.kind in {
                WireAgentOperation.RECIPE_START.value,
                WireAgentOperation.RECIPE_STOP.value,
            }:
                node = session.scalar(
                    select(RunNode)
                    .where(
                        RunNode.run_id == owner_id, RunNode.node_id == operation.node_id
                    )
                    .with_for_update(of=RunNode)
                )
                run = session.get(RecipeRun, owner_id, with_for_update=True)
                try:
                    target = (
                        read_stored_model(
                            RecipeStartPayload, operation.payload, from_json=True
                        )
                        if operation.kind == WireAgentOperation.RECIPE_START
                        else read_stored_model(
                            RecipeStopPayload, operation.payload, from_json=True
                        )
                    )
                except (TypeError, ValueError):
                    # Retain the authenticated receipt; the queue's bounded
                    # observation repairs its missing exact target projection.
                    return
                if run is not None and target.run_generation != run.run_generation:
                    # A previous generation's Stop cannot release this one's
                    # resources or alter its working route.
                    return
                if node is None and run is not None:
                    try:
                        accepted = parse_stored_run_plan(read_row_column(run, "plan"))
                        rank = next(
                            (
                                rank
                                for rank in accepted.nodes
                                if rank.node_id == operation.node_id
                            ),
                            None,
                        )
                    except (TypeError, ValueError):
                        rank = None
                    if rank is not None:
                        node = RunNode(
                            run_id=owner_id,
                            node_id=rank.node_id,
                            rank=rank.rank,
                            role=rank.role,
                            state=RunState.STOPPED,
                            port=rank.port,
                            reserved_memory_bytes=rank.required_memory_bytes,
                            updated_at=now,
                        )
                        session.add(node)
                # This authenticated cancellation receipt follows exact host
                # STOP. Release only this rank, even if its projection vanished.
                service._release_node_reservations(
                    session, owner_id, (operation.node_id,), now
                )
                if node is not None:
                    node.state = RunState.STOPPED
                    node.updated_at = now
                ordinal = _job_workload_intent(session, job)
                if (
                    run is not None
                    and ordinal is not None
                    and _intent_is_current(session, ordinal, job.targets)
                ):
                    run.state = RunState.LOST
                    run.route_state = RouteState.WITHDRAWN
                    run.updated_at = now
            return
        if state == agent_operation_states.WIRE_UNKNOWN and isinstance(result, Mapping):
            parsed = validate_result_for_operation(operation.kind, result, state=state)
            if (
                isinstance(parsed, AgentFailureResult)
                and parsed.failure_kind is AgentFailureKind.UNCERTAIN_EFFECT
                and parsed.uncertain is True
            ):
                # Typed uncertain evidence proves neither failure nor
                # completion. The same child, owner state, and reservations
                # remain authoritative while its retry is assessed. This
                # also covers a fresh guarded attempt that still cannot
                # establish whether a hook or runtime effect completed.
                return
        if (
            state == LifecycleState.FAILED.value
            and operation.state in agent_operation_states.PARKED
            and retry_scheduled(operation) is not None
            and isinstance(result, Mapping)
        ):
            parsed = validate_result_for_operation(operation.kind, result, state=state)
            if isinstance(parsed, AgentFailureResult):
                # The queue owner has already admitted this exact retry using
                # its failure policy. Do not independently restate that policy
                # here or turn its scheduled cleanup into a terminal failure.
                return
        succeeded = state == LifecycleState.SUCCEEDED.value
        raw_evidence: LifecycleNodeResult | None = None
        unproven: str | None = None
        if state not in {LifecycleState.SUCCEEDED.value, LifecycleState.FAILED.value}:
            unproven = "the agent result carried no final evidence"
        else:
            raw_evidence = _node_result(job.kind, operation.node_id, result)
            if raw_evidence is None:
                unproven = "the agent evidence does not match its operation"
        if raw_evidence is None:
            # The effect is unknown: the node is recorded failed with a typed
            # marker (its retry or cleanup is the owner's), and the operation
            # still completes instead of rolling the agent's result back.
            retire_as_unknown(
                "recipe.agent-result",
                operation.id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                unproven or "no evidence",
            )
            succeeded = False
            raw_evidence = _unproven_evidence(unproven or "no evidence")
        if consume_orphan_cleanup(
            session,
            job,
            operation,
            succeeded=succeeded,
            evidence=raw_evidence,
            now=service._clock(),
        ):
            return
        service._project_node_result(
            session,
            job,
            operation,
            succeeded=succeeded,
            evidence=raw_evidence,
            now=service._clock(),
        )

    @staticmethod
    def _reconciliation_job_complete(
        session: Session, job: Job, children: Sequence[AgentOperation]
    ) -> bool:
        """Every pending rank's fenced reconcile succeeded and is uninstalled."""

        authority = _parent_reconciliation(job)
        if (
            authority is None
            or authority.installation_id != _parent_identity(job, "owner_id")
            or not authority.targets
        ):
            return False
        targets = {target.node_id: target for target in authority.targets}
        targets_value = authority.targets
        pending = {
            node_id for node_id, target in targets.items() if target.state == "pending"
        }
        if (
            len(targets) != len(targets_value)
            or not pending
            or job.targets != sorted(pending)
            or {child.node_id for child in children} != pending
            or any(child.state != LifecycleState.SUCCEEDED.value for child in children)
        ):
            return False
        installation_id = authority.installation_id
        node_rows = tuple(
            session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )
        return {node.node_id for node in node_rows} == set(targets) and all(
            node.state == InstallationNodeState.UNINSTALLED for node in node_rows
        )

    def _apply_build_cleanup(
        self,
        session: Session,
        job: Job,
        operation: AgentOperation,
        evidence: object,
        *,
        owner_id: str,
        now: datetime,
    ) -> str | None:
        """Complete a build's cancellation from its Spark cleanup receipt.

        Returns why the receipt cannot be applied (the cleanup then records the
        node as unproven and the build stays under its own sweeper), else ``None``.
        """
        service = typing_cast("RecipeOperationService", self)

        try:
            read_stored_model(
                RecipeBuildCleanupEvidence,
                canonical_message(evidence),
                from_json=True,
            )
            expected = read_stored_model(
                RecipeBuildCleanupRequest,
                canonical_message(read_row_column(operation, "payload")),
                from_json=True,
            )
            parent = _parse_recipe_parent(job)
            if not isinstance(parent, RecipeBuildCleanupParent):
                return "recipe build cleanup parent kind is invalid"
            requested = parent.build_cancellation
        except (KeyError, TypeError, ValueError) as error:
            reason = f"recipe build cleanup record is invalid: {error}"
            retire_as_unknown(
                "recipe.build-cleanup",
                job.id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                reason,
            )
            return reason[:512]
        if expected.build_id != owner_id:
            retire_as_unknown(
                "recipe.build-cleanup",
                job.id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "cleanup names another build",
            )
            return "recipe build cleanup does not match its authority"
        original = session.get(
            AgentOperation, expected.operation_id, with_for_update=True
        )
        original_job = (
            None
            if original is None
            else session.get(Job, original.parent_job_id, with_for_update=True)
        )
        build = session.get(RecipeBuild, owner_id, with_for_update=True)
        if (
            original is None
            or original.node_id != operation.node_id
            or original.kind != WireAgentOperation.RECIPE_BUILD.value
            or original_job is None
            or _parent_identity(original_job, "owner_id") != owner_id
            or build is None
        ):
            retire_as_unknown(
                "recipe.build-cleanup",
                job.id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "the cleaned build's operation or row changed",
            )
            return "recipe build cleanup authority changed"
        cancellation = build_cancellation(original_job)
        if (
            requested is None
            or cancellation is None
            or cancellation.cancel_request_id != requested.cancel_request_id
            or cancellation.cancel_actor != requested.cancel_actor
            or cancellation.cancel_requested_at != requested.cancel_requested_at
            or cancellation.reason != requested.reason
        ):
            retire_as_unknown(
                "recipe.build-cleanup",
                job.id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "the build's cancellation no longer matches the cleanup",
            )
            return "recipe build cleanup authority changed"
        AgentOperationAdapter(session).record_outcome(
            original, None, original_job, Outcome.CANCELLED, now
        )
        RecipeOperationAdapter().cancelled(original_job, now)
        original_job.result = serialize_json_value(
            cancellation.model_copy(update={LifecycleState.CANCELLED.value: True})
        )
        original_job.updated_at = now
        service._release_cancelled_build(session, owner_id, now)
        return None
