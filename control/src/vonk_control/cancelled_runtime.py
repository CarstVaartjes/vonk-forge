"""Exact cleanup custody inherited from an accepted artifact cancellation."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as Kind
from vonk_agent_protocol import (
    LifecycleState,
    RecipeJobRunRequest,
    RecipeStopPayload,
    UnknownOutcomeError,
    WaitOwnerKind,
)
from vonk_agent_protocol.state_machines import NodeIdentityState

from .agent_jobs.stored import column_field, column_value
from .artifact_jobs.evidence import read_result_evidence
from .jobs.documents import RecipeStopParent
from .models.execution import ArtifactJob
from .models.fleet import AgentNode
from .models.jobs import Job
from .models.operations import AgentOperation
from .recipe_operations.stop_payloads import stop_payload_from_job_run
from .stored.json_values import read_row_column

if TYPE_CHECKING:
    from .recipe_operations.service import RecipeOperationService


def cancellation_stop_key(job_id: str, cancel_id: str) -> str:
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"artifact-cancel-stop:{job_id}:{cancel_id}")
    )


def cancelled_runtime_source(
    session: Session, job_id: str
) -> tuple[tuple[tuple[AgentOperation, RecipeStopPayload], ...], str] | None:
    """Revalidate cancellation and exact current owners under admission locks.

    The JobRun is named by its own identity (its job, its run), never by its
    Spark's workload intent: a newer load on the Spark does not own this job's
    container, so its exact Stop removes only that. Terminal result and
    physical settlement are independent, so this reads the original issued
    JobRun after cancellation. A job on several Sparks has one issued order
    per rank, and one exact Stop per rank.
    """
    artifact = session.get(ArtifactJob, job_id, populate_existing=True)
    if artifact is None or artifact.state != LifecycleState.CANCELLED:
        return None
    evidence = read_result_evidence(artifact.result_evidence)
    if (
        evidence is None
        or evidence.cancel_request_id is None
        or not evidence.scope_unproven
    ):
        return None
    parent = session.get(Job, artifact.operation_id) if artifact.operation_id else None
    if parent is None or column_field(parent, "result", "cancel_requested") is not True:
        return None
    sources = tuple(
        session.scalars(
            select(AgentOperation)
            .where(
                AgentOperation.parent_job_id == parent.id,
                AgentOperation.kind == Kind.RECIPE_JOB_RUN,
                AgentOperation.current_attempt > 0,
            )
            .order_by(AgentOperation.id)
            .execution_options(populate_existing=True)
        )
    )
    if not sources or column_field(parent, "result", "cancel_request_id") != (
        evidence.cancel_request_id
    ):
        return None
    stops: list[tuple[AgentOperation, RecipeStopPayload]] = []
    for source in sources:
        request = column_value(source, "payload")
        node = session.get(AgentNode, source.node_id, populate_existing=True)
        if (
            not isinstance(request, RecipeJobRunRequest)
            or node is None
            or node.state != NodeIdentityState.ACTIVE
            or node.revoked_at is not None
            or request.job_id != artifact.id
            or request.run_id != artifact.run_id
            or source.authority_revision != parent.authority_revision
        ):
            # A rank whose Spark is gone or revoked, or whose order no longer
            # names this job, run and authority, gets no Stop from here; the
            # other ranks still do (that Spark's own reconcile removes its own).
            continue
        stops.append(
            (source, stop_payload_from_job_run(request, cancel_pending_start=True))
        )
    if not stops:
        return None
    return tuple(stops), evidence.cancel_request_id


def cancellation_stop_current(
    session: Session, parent: Job, operation: AgentOperation
) -> bool:
    document = read_row_column(parent, "payload")
    if (
        not isinstance(document, RecipeStopParent)
        or document.owner_kind != WaitOwnerKind.ARTIFACT_JOB
    ):
        return True
    source = cancelled_runtime_source(session, document.owner_id)
    return source is not None and (
        parent.request_id == cancellation_stop_key(document.owner_id, source[1])
        and any(
            operation.node_id == order.node_id
            and column_value(operation, "payload") == stop
            for order, stop in source[0]
        )
    )


def reconcile_cancelled_runtime(
    service: RecipeOperationService, limit: int = 100
) -> bool:
    """Idle ticks create one idempotent Stop per accepted uncertain cancel."""
    with service._sessions() as session:
        query = (
            select(ArtifactJob.id)
            .where(
                ArtifactJob.state == LifecycleState.CANCELLED,
                ArtifactJob.operation_id.is_not(None),
            )
            .order_by(ArtifactJob.id)
            .limit(limit)
        )
        cursor = service._cancelled_runtime_cursor
        if cursor is None:
            ids = tuple(session.scalars(query))
        else:
            ids = tuple(session.scalars(query.where(ArtifactJob.id > cursor)))
            ids += tuple(
                session.scalars(
                    query.where(ArtifactJob.id <= cursor).limit(limit - len(ids))
                )
            )
    if ids:
        service._cancelled_runtime_cursor = ids[-1]
    progressed = False
    for job_id in ids:
        try:
            with service._sessions.begin() as session:
                source = cancelled_runtime_source(session, job_id)
                if source is None:
                    continue
                stops, cancel_id = source
                key = cancellation_stop_key(job_id, cancel_id)
                if (
                    session.scalar(select(Job.id).where(Job.request_id == key))
                    is not None
                ):
                    continue

                def guard(
                    locked: Session,
                    job_id: str = job_id,
                    source: tuple[
                        tuple[tuple[AgentOperation, RecipeStopPayload], ...], str
                    ] = source,
                ) -> None:
                    from .recipe_operations.errors import RecipeRequestInvalid

                    current = cancelled_runtime_source(locked, job_id)
                    if current is None or (
                        [(order.id, stop) for order, stop in current[0]],
                        current[1],
                    ) != (
                        [(order.id, stop) for order, stop in source[0]],
                        source[1],
                    ):
                        raise RecipeRequestInvalid(
                            "accepted cancellation runtime owner changed"
                        )

                service._queue_in_session(
                    session,
                    kind=Kind.RECIPE_STOP,
                    owner_kind=WaitOwnerKind.ARTIFACT_JOB,
                    owner_id=job_id,
                    plan_digest=stops[0][1].plan_digest,
                    actor="controller",
                    request_id=key,
                    node_payloads=tuple((order.node_id, stop) for order, stop in stops),
                    phases=(tuple((order.node_id, stop) for order, stop in stops),),
                    authority_digest=stops[0][0].authority_revision,
                    now=service._clock(),
                    # Own cleanup joins the Spark's current intent and cancels
                    # nothing (``unattended_guard``).
                    unattended_guard=guard,
                )
                progressed = True
        except (UnknownOutcomeError, SQLAlchemyError, TypeError, ValueError):
            logging.getLogger(__name__).exception(
                "exact cancellation Stop observation deferred for %s; next worker tick rechecks",
                job_id,
            )
    return progressed
