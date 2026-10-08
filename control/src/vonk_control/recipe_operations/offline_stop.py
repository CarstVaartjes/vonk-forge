"""Offline stop for digest-bound recipe operations."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InvalidRequestError,
    LifecycleState,
    ProjectionCode,
    RunState,
    SecurityRefusalError,
    UnknownOutcomeError,
    canonical_message,
)

from ..admission_locking import (
    AdmissionLockBusy,
    acquire_admission_keys,
    is_admission_contention,
    node_admission_key,
)
from ..job_documents import (
    RecipeStopParent,
)
from ..lifecycle.recipe_operation import RecipeOperationAdapter
from ..logging import redact_text
from ..models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeRun,
    RunNode,
)
from ..offline_stops import deferred_stop_nodes
from ..profile_stop_authority import (
    ProfileJobRunStopJob,
)
from ..recipe_lifecycle_contract import (
    RecipeOperationProgressResult,
    RecipeOperationResult,
)
from ..recipe_progress import (
    _parent_identity as _parent_identity,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _recorded_parent as _recorded_parent,  # noqa: PLC0414 -- shared helper export
)
from ..stored_json import read_row_column
from ..strict_json import serialize_json_value
from .intent import _bound_workload_intent
from .interfaces import _TERMINAL_JOB_STATES
from .observation_helpers import _aware
from .results import _recorded_result

if TYPE_CHECKING:
    from .service import RecipeOperationService


class OfflineStopMixin:
    def reconcile_pending_service_stops(self) -> bool:
        """Give one due Stop the external publication turn for this worker pass."""
        service = typing_cast("RecipeOperationService", self)
        now = service._clock()
        offline_progressed = service.reconcile_offline_stops()
        with service._sessions() as session:
            candidates = tuple(
                session.scalars(
                    select(Job)
                    .where(
                        Job.kind == WireAgentOperation.RECIPE_STOP.value,
                        Job.state == LifecycleState.RUNNING.value,
                        Job.payload["service_stop_review"]["stage"]
                        .as_string()
                        .in_(["accepted", "withdrawal-claimed"]),
                        Job.updated_at <= now - timedelta(seconds=5),
                    )
                    .order_by(Job.updated_at, Job.id)
                    .limit(1)
                )
            )
        progressed = False
        for candidate in candidates:
            # Advance this due clock before external work, in a short claim,
            # so malformed/unavailable owners cannot monopolize the first page.
            with service._sessions.begin() as session:
                claimed = session.get(Job, candidate.id, with_for_update=True)
                if (
                    claimed is None
                    or claimed.state != LifecycleState.RUNNING.value
                    or claimed.updated_at != candidate.updated_at
                ):
                    continue
                claimed.updated_at = now
            try:
                document = service._service_stop_document(candidate)
                review = document.service_stop_review
                assert review is not None
                service.stop(
                    document.owner_id,
                    plan_digest=document.plan_digest,
                    actor=candidate.actor,
                    request_id=candidate.request_id,
                    workload_intent_ordinal=_bound_workload_intent(candidate),
                    profile_target_node_ids=review.profile_target_node_ids,
                    profile_application_id=review.profile_application_id,
                )
                progressed = True
            except (
                UnknownOutcomeError,
                InvalidRequestError,
                SecurityRefusalError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                with service._sessions.begin() as session:
                    retained = session.get(Job, candidate.id, with_for_update=True)
                    if (
                        retained is not None
                        and retained.state == LifecycleState.RUNNING.value
                    ):
                        retained.status_reason = (
                            f"accepted exact Stop deferred: {redact_text(str(error))}; next reconciliation at {(now + timedelta(seconds=5)).isoformat()}"
                        )[:1024]
                        retained.updated_at = now
        return progressed or offline_progressed

    def reconcile_offline_stops(self) -> bool:
        """Detach unreachable ranks from foreground completion, retaining exact orders.

        No capacity is released for an unreachable rank. Its already authorized,
        idempotent Stop is claimable on contact, using the normal signed-plan and
        attempt-fencing path. No request, slot or fleet queue waits for contact.
        """
        service = typing_cast("RecipeOperationService", self)
        from ..job_documents import OfflineStopIntent

        now = service._clock()
        with service._sessions() as session:
            candidates = tuple(
                session.execute(
                    select(Job.id, Job.targets)
                    .where(
                        Job.kind == WireAgentOperation.RECIPE_STOP.value,
                        Job.state.in_(
                            [
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.OBSERVING,
                                LifecycleState.BACKOFF,
                            ]
                        ),
                    )
                    .order_by(Job.updated_at, Job.id)
                )
            )
        changed = False
        for identity, target_snapshot in candidates:
            try:
                with service._sessions.begin() as session:
                    acquire_admission_keys(
                        session,
                        tuple(
                            node_admission_key(node_id) for node_id in target_snapshot
                        ),
                        holder="offline-stop-reconciliation",
                    )
                    job = session.get(Job, identity, with_for_update={"nowait": True})
                    if job is None or job.targets != target_snapshot:
                        continue
                    parent = _recorded_parent(job)
                    if (
                        not isinstance(parent, (RecipeStopParent, ProfileJobRunStopJob))
                        or not parent.phases
                    ):
                        continue
                    nodes = {
                        node.node_id: node
                        for node in session.scalars(
                            select(AgentNode).where(AgentNode.node_id.in_(job.targets))
                        )
                    }
                    offline = deferred_stop_nodes(job) | frozenset(
                        node_id
                        for node_id in job.targets
                        if node_id in nodes
                        and nodes[node_id].revoked_at is None
                        and (
                            (last_seen := nodes[node_id].last_seen_at) is None
                            or _aware(now) - _aware(last_seen) > timedelta(seconds=150)
                        )
                    )
                    if not offline:
                        continue
                    parent.offline_stop_intent = OfflineStopIntent(
                        node_ids=sorted(offline)
                    )
                    service._write_stop_parent(job, parent, now=now)
                    children = {
                        child.id: child
                        for child in session.scalars(
                            select(AgentOperation).where(
                                AgentOperation.parent_job_id == job.id
                            )
                        )
                    }
                    # Retain future offline phases too: completion must not lose
                    # an exact Stop that was behind a different rank's phase.
                    online_pending = False
                    online_failed = False
                    for phase in parent.phases:
                        online = [item for item in phase if item.node_id not in offline]
                        if not online_pending and not online_failed:
                            for item in online:
                                if item.operation_id not in children:
                                    service._agent_jobs.enqueue_in_session(
                                        session,
                                        job.id,
                                        item.node_id,
                                        job.kind,
                                        job.authority_revision,
                                        json.loads(canonical_message(item.payload)),
                                        operation_id=item.operation_id,
                                    )
                            online_pending = any(
                                item.operation_id not in children
                                or children[item.operation_id].state
                                not in _TERMINAL_JOB_STATES
                                for item in online
                            )
                            online_failed |= any(
                                item.operation_id in children
                                and children[item.operation_id].state
                                != LifecycleState.SUCCEEDED
                                and children[item.operation_id].state
                                in _TERMINAL_JOB_STATES
                                for item in online
                            )
                        for item in phase:
                            if (
                                item.node_id in offline
                                and item.operation_id not in children
                            ):
                                service._agent_jobs.enqueue_in_session(
                                    session,
                                    job.id,
                                    item.node_id,
                                    job.kind,
                                    job.authority_revision,
                                    json.loads(canonical_message(item.payload)),
                                    operation_id=item.operation_id,
                                )
                    if not online_pending:
                        service._finish_offline_stop(
                            session, job, now, failed=online_failed
                        )
                    changed = True
            except AdmissionLockBusy:
                continue
            except OperationalError as error:
                if not is_admission_contention(error):
                    raise
                continue
        return changed

    def _finish_offline_stop(
        self, session: Session, job: Job, now: datetime, *, failed: bool
    ) -> None:
        """Complete reachable work; only exact stopped ranks release capacity."""
        service = typing_cast("RecipeOperationService", self)
        run_id = _parent_identity(job, "owner_id")
        run = session.get(RecipeRun, run_id) if run_id is not None else None
        if run is None:
            return
        nodes = tuple(session.scalars(select(RunNode).where(RunNode.run_id == run.id)))
        orders = tuple(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == job.id,
                )
            )
        )
        pending_nodes = {
            order.node_id for order in orders if order.state != LifecycleState.SUCCEEDED
        }
        for node in nodes:
            if node.node_id in pending_nodes and node.state == RunState.STOPPED:
                node.state = RunState.STOPPING
        stopped = sorted(
            node.node_id
            for node in nodes
            if node.state == RunState.STOPPED and node.node_id not in pending_nodes
        )
        service._release_node_reservations(session, run.id, stopped, now)
        complete = bool(nodes) and len(stopped) == len(nodes)
        run.state = RunState.STOPPED if complete else RunState.STOPPING
        run.stopped_at = now if complete else None
        run.updated_at = now
        run.route_error = (
            None
            if complete
            else f"{ProjectionCode.NODE_OFFLINE}: exact Stop pending reconnect"
        )
        RecipeOperationAdapter().finish(
            job, now, failed=failed, reason=run.route_error, keep=False
        )
        recorded = _recorded_result(
            job.kind, read_row_column(job, "result"), subject=job.id
        )
        failed_nodes = sorted(
            {
                child.node_id
                for child in session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == job.id,
                        AgentOperation.state == LifecycleState.FAILED,
                    )
                )
                if child.node_id not in deferred_stop_nodes(job)
            }
        )
        job.result = serialize_json_value(
            RecipeOperationResult(
                successful_nodes=sorted(set(stopped) - set(failed_nodes)),
                failed_nodes=failed_nodes,
                recovery_error=run.route_error if failed and not failed_nodes else None,
                node_evidence=(recorded.node_evidence or {})
                if isinstance(
                    recorded, (RecipeOperationResult, RecipeOperationProgressResult)
                )
                else {},
            )
        )
