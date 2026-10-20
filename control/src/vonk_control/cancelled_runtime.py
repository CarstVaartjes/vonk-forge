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
from .artifact_job_evidence import read_result_evidence
from .job_documents import RecipeStopParent
from .models import AgentNode, AgentOperation, ArtifactJob, Job
from .recipe_stop_payloads import stop_payload_from_job_run
from .stored_json import read_row_column

if TYPE_CHECKING:
    from .recipe_operations import RecipeOperationService


def cancellation_stop_key(job_id: str, cancel_id: str) -> str:
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"artifact-cancel-stop:{job_id}:{cancel_id}")
    )


def cancelled_runtime_source(
    session: Session, job_id: str
) -> tuple[AgentOperation, RecipeStopPayload, str] | None:
    """Revalidate cancellation and exact current owner under admission locks.

    A newer request owns the node: this old cancellation cannot create another
    target or adopt its authority. Terminal result and physical settlement are
    independent, so this reads the original issued JobRun after cancellation.
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
            .execution_options(populate_existing=True)
        )
    )
    if len(sources) != 1:
        return None
    source = sources[0]
    request = column_value(source, "payload")
    node = session.get(AgentNode, source.node_id, populate_existing=True)
    if (
        not isinstance(request, RecipeJobRunRequest)
        or node is None
        or node.state != NodeIdentityState.ACTIVE
        or node.revoked_at is not None
        or source.workload_intent_ordinal != node.workload_intent_ordinal
        or source.workload_intent_ordinal
        != column_field(parent, "payload", "workload_intent_ordinal")
        or request.job_id != artifact.id
        or request.run_id != artifact.run_id
        or source.authority_revision != parent.authority_revision
        or column_field(parent, "result", "cancel_request_id")
        != evidence.cancel_request_id
    ):
        return None
    return (
        source,
        stop_payload_from_job_run(request, cancel_pending_start=True),
        evidence.cancel_request_id,
    )


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
        parent.request_id == cancellation_stop_key(document.owner_id, source[2])
        and operation.node_id == source[0].node_id
        and column_value(operation, "payload") == source[1]
        and operation.workload_intent_ordinal == source[0].workload_intent_ordinal
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
                order, stop, cancel_id = source
                key = cancellation_stop_key(job_id, cancel_id)
                if (
                    session.scalar(select(Job.id).where(Job.request_id == key))
                    is not None
                ):
                    continue

                def guard(
                    locked: Session,
                    job_id: str = job_id,
                    source: tuple[AgentOperation, RecipeStopPayload, str] = source,
                ) -> None:
                    from .recipe_operations import RecipeRequestInvalid

                    current = cancelled_runtime_source(locked, job_id)
                    if (
                        current is None
                        or current[0].id != source[0].id
                        or current[1:] != source[1:]
                    ):
                        raise RecipeRequestInvalid(
                            "accepted cancellation runtime owner changed"
                        )

                service._queue_in_session(
                    session,
                    kind=Kind.RECIPE_STOP,
                    owner_kind=WaitOwnerKind.ARTIFACT_JOB,
                    owner_id=job_id,
                    plan_digest=stop.plan_digest,
                    actor="controller",
                    request_id=key,
                    node_payloads=((order.node_id, stop),),
                    phases=(((order.node_id, stop),),),
                    authority_digest=order.authority_revision,
                    now=service._clock(),
                    workload_intent_ordinal=order.workload_intent_ordinal,
                    unattended_guard=guard,
                )
                progressed = True
        except (UnknownOutcomeError, SQLAlchemyError, TypeError, ValueError):
            logging.getLogger(__name__).exception(
                "exact cancellation Stop observation deferred for %s; next worker tick rechecks",
                job_id,
            )
    return progressed
