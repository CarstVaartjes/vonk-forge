"""Canonical Controller projections for exact recipe runtime Stop authority."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestError,
    InvalidRequestReason,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import RecipeStartPayload, RecipeStopPayload

from .categorized_errors import BookkeepingUnknown
from .categorized_faults import security_reason
from .models import (
    CatalogDocumentRevision,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .recipe_action_plans import StopNodeImpact
from .strict_json import read_stored_model


class RecipeStopAuthorityError(ValueError):
    """A durable recipe start cannot authorize an exact runtime Stop."""


class StopPayloadRefused(SecurityRefusalError, RecipeStopAuthorityError):
    """Durable Start evidence disagrees with the run, mapping or plan: the Stop is refused (destructive-effect fence)."""

    def __init__(
        self,
        *args: Any,
        reason: SecurityRefusalReason | None = None,
        **fields: Any,
    ) -> None:
        RecipeStopAuthorityError.__init__(self, *args, **fields)
        self.typed_reason = (
            reason if reason is not None else security_reason(args[0] if args else None)
        )


class StopPayloadInvalid(InvalidRequestError, RecipeStopAuthorityError):
    """The Stop request names an invalid generation, an empty target set or an ambiguous durable plan."""

    def __init__(
        self,
        *args: Any,
        reason: InvalidRequestReason | None = InvalidRequestReason.MALFORMED,
        field: str | None = None,
        **fields: Any,
    ) -> None:
        RecipeStopAuthorityError.__init__(self, *args, **fields)
        self.typed_reason = reason
        self.typed_field = field


class StopPayloadUnknown(UnknownOutcomeError, RecipeStopAuthorityError):
    """Durable Start evidence that cannot be read or is incomplete: unknown until the owner re-derives it."""

    def __init__(
        self,
        *args: Any,
        reason: WaitReason | None = None,
        **fields: Any,
    ) -> None:
        RecipeStopAuthorityError.__init__(self, *args, **fields)
        self.typed_reason = reason


def stop_payload_from_start(
    value: RecipeStartPayload | Mapping[str, object],
    *,
    cancel_pending_start: bool,
) -> RecipeStopPayload:
    """Project one exact Stop target from its complete typed Start effect."""

    try:
        start = read_stored_model(
            RecipeStartPayload, canonical_message(value), from_json=True
        )
        if type(start.run_generation) is not int or start.run_generation < 1:
            raise BookkeepingUnknown("start generation is missing")
        payload = RecipeStopPayload(
            run_id=start.run_id,
            target_runtime_id=start.run_id,
            installation_id=start.installation_id,
            recipe_revision_id=start.recipe_revision_id,
            mapping_id=start.mapping_id,
            plan_digest=start.plan_digest,
            recipe_content_sha256=start.compiled_execution_plan.identity.recipe_revision_sha256,
            rank=start.compiled_execution_plan.runtime.placement.rank,
            role=start.compiled_execution_plan.runtime.placement.role,
            stop_timeout_seconds=start.compiled_execution_plan.lifecycle.stop_timeout_seconds,
            cancel_pending_start=cancel_pending_start,
            run_generation=start.run_generation,
        )
        return read_stored_model(
            RecipeStopPayload, canonical_message(payload), from_json=True
        )
    except (TypeError, ValueError) as error:
        raise StopPayloadUnknown(
            "recipe Start payload is invalid", reason=WaitReason.OBSERVATION_UNAVAILABLE
        ) from error


def stop_payload_from_job_run(
    value: RecipeJobRunRequest | Mapping[str, object],
    *,
    cancel_pending_start: bool,
) -> RecipeStopPayload:
    """Project a transient artifact-container Stop from its typed job request."""

    try:
        job = read_stored_model(
            RecipeJobRunRequest, canonical_message(value), from_json=True
        )
        if type(job.run_generation) is not int or job.run_generation < 1:
            raise BookkeepingUnknown("job run generation is missing")
        payload = RecipeStopPayload(
            run_id=job.run_id,
            target_runtime_id=job.job_id,
            installation_id=job.installation_id,
            recipe_revision_id=job.recipe_revision_id,
            mapping_id=job.mapping_id,
            plan_digest=job.plan_digest,
            recipe_content_sha256=job.compiled_execution_plan.identity.recipe_revision_sha256,
            rank=job.compiled_execution_plan.runtime.placement.rank,
            role=job.compiled_execution_plan.runtime.placement.role,
            stop_timeout_seconds=job.compiled_execution_plan.lifecycle.stop_timeout_seconds,
            cancel_pending_start=cancel_pending_start,
            run_generation=job.run_generation,
        )
        return read_stored_model(
            RecipeStopPayload, canonical_message(payload), from_json=True
        )
    except (TypeError, ValueError) as error:
        raise StopPayloadUnknown(
            "recipe JobRun payload is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error


def durable_run_stop_payloads(
    session: Session,
    run: RecipeRun,
    nodes: Sequence[RunNode | StopNodeImpact],
    *,
    run_generation: int,
    cancel_pending_start: bool,
    allow_missing_nodes: bool,
) -> dict[str, RecipeStopPayload]:
    """Authorize exact Stop from current durable run and rank ownership.

    Historical Job/AgentOperation rows are evidence of how a run started,
    not the authority to stop that exact run. The caller binds current consent;
    the helper checks run, installation, generation and plan labels before any
    destructive effect.
    """

    if type(run_generation) is not int or not 1 <= run_generation <= run.run_generation:
        raise StopPayloadInvalid(
            "recipe Stop generation is invalid",
            reason=InvalidRequestReason.OUT_OF_RANGE,
        )
    requested = {node.node_id: node for node in nodes}
    if not requested or len(requested) != len(nodes):
        raise StopPayloadInvalid(
            "recipe Stop target set is empty or duplicated",
            reason=InvalidRequestReason.INCOMPLETE,
        )
    installation = session.get(RecipeInstallation, run.installation_id)
    revision = (
        session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if (
        installation is None
        or revision is None
        or run.mapping_id != installation.mapping_id
        or run.mapping_generation != installation.mapping_generation
        or revision.kind != "recipe"
        or revision.content_digest is None
    ):
        raise StopPayloadRefused("recipe Stop identity is stale")
    durable_nodes = tuple(
        session.scalars(select(RunNode).where(RunNode.run_id == run.id))
    )
    if {(node.node_id, node.rank, node.role) for node in durable_nodes} != {
        (node.node_id, node.rank, node.role) for node in nodes
    }:
        raise StopPayloadRefused("recipe Stop run membership differs")
    # The timeout protects the Stop itself. An unreadable launch plan must not
    # remove this operation: use the canonical recipe timeout when available,
    # otherwise the bounded default. Neither choice changes the target.
    timeout = 60
    document = revision.document
    runtime = document.get("runtime") if isinstance(document, Mapping) else None
    lifecycle = runtime.get("lifecycle") if isinstance(runtime, Mapping) else None
    value = (
        lifecycle.get("stop_timeout_seconds")
        if isinstance(lifecycle, Mapping)
        else None
    )
    if type(value) is int and 1 <= value <= 600:
        timeout = value
    return {
        node.node_id: RecipeStopPayload(
            run_id=run.id,
            target_runtime_id=run.id,
            installation_id=run.installation_id,
            recipe_revision_id=installation.recipe_revision_id,
            mapping_id=run.mapping_id,
            plan_digest=run.plan_digest,
            run_generation=run_generation,
            recipe_content_sha256=revision.content_digest,
            rank=node.rank,
            role=node.role,
            stop_timeout_seconds=timeout,
            cancel_pending_start=cancel_pending_start,
        )
        for node in nodes
    }
