"""Offline Stop orders keep exact authority without holding a fleet operation."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .models import AgentOperation, Job


def deferred_stop_nodes(job: Job) -> frozenset[str]:
    # Imported here because the canonical job contracts reach the queue adapters.
    from .job_documents import RecipeStopParent
    from .profile_stop_authority import ProfileJobRunStopJob
    from .strict_json import read_stored_model

    if job.kind != "recipe.stop":
        return frozenset()
    try:
        parent = (
            ProfileJobRunStopJob.model_validate_parent(job.payload)
            if job.payload.get("execution_mode") == "profile-jobrun-stop"
            else read_stored_model(RecipeStopParent, job.payload, from_json=True)
        )
    except (TypeError, ValueError):
        return frozenset()
    intent = parent.offline_stop_intent
    return frozenset(intent.node_ids) if intent is not None else frozenset()


def is_deferred_stop(job: Job | None, operation: AgentOperation) -> bool:
    """Only an exact retained Stop may execute after its foreground parent ends.

    It is recognised by identity: the order is one of its parent's retained
    phase items on its offline node, and names the run its parent owns. The
    digests of the Controller's own rows never decide it. Authentication and
    attempt fences remain enforced by the normal claim and result paths.
    """
    from .job_documents import RecipeStopParent
    from .profile_stop_authority import ProfileJobRunStopJob
    from .strict_json import read_stored_model

    if (
        job is None
        or operation.parent_job_id != job.id
        or operation.kind != "recipe.stop"
        or operation.node_id not in deferred_stop_nodes(job)
    ):
        return False
    parent = (
        ProfileJobRunStopJob.model_validate_parent(job.payload)
        if job.payload.get("execution_mode") == "profile-jobrun-stop"
        else read_stored_model(RecipeStopParent, job.payload, from_json=True)
    )
    return operation.payload.get("run_id") == parent.owner_id and any(
        item.operation_id == operation.id and item.node_id == operation.node_id
        for phase in parent.phases or ()
        for item in phase
    )


def pending_run_stop_nodes(session: Session, run_id: str) -> frozenset[str]:
    from sqlalchemy import select

    return frozenset(
        node_id
        for job in session.scalars(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == run_id,
            )
        )
        for node_id in deferred_stop_nodes(job)
    )
