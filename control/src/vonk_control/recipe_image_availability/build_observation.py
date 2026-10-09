"""Recover shared image-build projections through their accepted requests."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    LifecycleState,
    RecipeBuildRequest,
    SecurityRefusalError,
    UnknownOutcomeError,
)

from .. import job_states
from ..models import AgentOperation, Job, RecipeBuild
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_build_plan,
)
from .contracts import RecipeImageAvailabilityClaim

if TYPE_CHECKING:
    from ..recipe_operations import RecipeOperationService
    from .service import RecipeImageAvailabilityService


def accepted_build_request(
    session: Session, build: RecipeBuild
) -> RecipeBuildRequest | None:
    """Repair a build projection from the exact accepted executor request.

    Builder provenance and diagnostic policy reports are not image identity.
    Neither an unreadable candidate nor its history vetoes another candidate.
    """
    requests = session.scalars(
        select(AgentOperation)
        .join(Job, Job.id == AgentOperation.parent_job_id)
        .where(
            AgentOperation.kind == WireAgentOperation.RECIPE_BUILD.value,
            Job.payload["owner_id"].as_string() == build.id,
        )
        .order_by(AgentOperation.created_at.desc(), AgentOperation.id)
    )
    for operation in requests:
        try:
            request = parse_stored_build_plan(operation.payload)
        except RecipeExecutionContractError:
            continue
        if (
            request.build_input_sha256 == build.build_input_sha256
            and request.source_bundle_sha256 == build.source_bundle_sha256
        ):
            build.plan = request.model_dump(mode="json", exclude_none=True)
            return request
    try:
        return parse_stored_build_plan(build.plan)
    except RecipeExecutionContractError:
        return None


def reconcile_abandoned_builds(
    service: RecipeImageAvailabilityService,
    claim: RecipeImageAvailabilityClaim,
    recipe_operations: RecipeOperationService,
) -> None:
    """Repair accepted projections and detach unneeded damaged dependencies.

    Cancellation stays with the build owner, which preserves independent
    requests and other consumers and reconciles issued effects before
    releasing capacity. SQL never spans that owner's cancellation call.
    """
    damaged: list[str] = []
    with service._sessions.begin() as session:
        parent = service._require_claim(session, claim)
        builds = session.scalars(
            select(RecipeBuild)
            .join(
                Job,
                Job.payload["owner_id"].as_string() == RecipeBuild.id,
            )
            .where(
                RecipeBuild.recipe_revision_id == parent.authority_revision,
                Job.kind == WireAgentOperation.RECIPE_BUILD.value,
                Job.state.in_(
                    job_states.words(LifecycleState.QUEUED, LifecycleState.RUNNING)
                ),
            )
        ).unique()
        for build in builds:
            if accepted_build_request(session, build) is not None:
                continue
            damaged.extend(
                session.scalars(
                    select(Job.id).where(
                        Job.kind == WireAgentOperation.RECIPE_BUILD.value,
                        Job.payload["owner_id"].as_string() == build.id,
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED, LifecycleState.RUNNING
                            )
                        ),
                    )
                )
            )
    cancel = getattr(recipe_operations, "_cancel_build", None)
    if not callable(cancel):
        return
    for operation_id in damaged:
        try:
            cancel(
                operation_id,
                actor="recipe-image-availability",
                request_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"vonk:damaged-build-observation:{operation_id}",
                    )
                ),
                reason="Accepted build projection is unavailable",
                only_if_unneeded=True,
            )
        except SecurityRefusalError:
            raise
        except (UnknownOutcomeError, OSError, RuntimeError, ValueError):
            # Its owner retains the exact effect and retries reconciliation;
            # this consumer still searches the healthy candidate pool.
            continue
