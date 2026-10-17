"""Derive signed lifecycle-plan bindings from durable Controller authority."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as AgentOperationKind
from vonk_agent_protocol import (
    ContainerRuntimeAction,
    InstallationState,
    InvalidRequestError,
    InvalidRequestReason,
    RunState,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import RecipeStartPayload, RecipeStopPayload

from .categorized_errors import InvalidType
from .models import (
    AgentOperation,
    ArtifactJob,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .own_cleanup import own_runtime_target
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_plan,
)
from .stored_json import read_row_column
from .strict_json import read_stored_model, serialize_json_value


class RuntimePlanAuthorityError(ValueError):
    """Durable operation state cannot authorize the requested lifecycle plan."""


class RuntimePlanAuthorityRefused(SecurityRefusalError, RuntimePlanAuthorityError):
    """The requested plan is not the one this durable owner and digest bind."""


class RuntimePlanAuthorityStale(InvalidRequestError, RuntimePlanAuthorityError):
    """Durable state moved on: the plan is stale and the caller must refresh it."""


class RuntimePlanEvidenceUnavailable(UnknownOutcomeError, RuntimePlanAuthorityError):
    """The durable evidence that would authorize the plan is missing or unreadable."""


class RuntimePlanUnsatisfiable(InvalidRequestError, RuntimePlanAuthorityError):
    """The durable record can never bind this attempt's plan; no wait changes it."""


@dataclass(frozen=True, slots=True)
class RuntimePlanBinding:
    start_plan_sha256: str | None = None
    stop_plan_sha256: str | None = None
    run_generation: int | None = None
    runtime_run_id: str | None = None
    runtime_target_id: str | None = None
    runtime_installation_id: str | None = None


@dataclass(frozen=True, slots=True)
class _RunAuthority:
    run: RecipeRun
    installation: RecipeInstallation
    revision: CatalogDocumentRevision
    mapping: ClusterMapping
    nodes: tuple[RunNode, ...]


def derive_runtime_plan_binding(
    session: Session,
    *,
    parent: Job,
    operation: AgentOperation,
    node_id: str,
    action: ContainerRuntimeAction,
    stop_plan_sha256: str | None,
    run_generation: int | None = None,
) -> RuntimePlanBinding:
    """Select and hash the exact plan owned by this durable child operation.

    Every Stop removes its order's own container (``own_cleanup``): it is bound
    to the order's identity and to the Stop document the agent sent
    (``stop_plan_sha256``); the helper executes that document only when its
    identity is the one bound here.

    An inspection whose agent names a generation (``run_generation``) is
    bound to its Start's own generation, read from the stored order; an
    inspection that names none (an older agent) stays unbound.
    """

    if action is ContainerRuntimeAction.RUN_INSPECT and run_generation is not None:
        # Granted only for a recipe.start (``_ACTION_KINDS``).
        return RuntimePlanBinding(
            run_generation=_parse_start(operation.payload).run_generation
        )
    if action not in {ContainerRuntimeAction.START, ContainerRuntimeAction.STOP}:
        return RuntimePlanBinding()
    if action is ContainerRuntimeAction.STOP:
        return _own_stop_binding(parent, operation, stop_plan_sha256)
    _validate_child_record(parent, operation, node_id)
    if operation.kind not in {
        AgentOperationKind.RECIPE_START.value,
        AgentOperationKind.RECIPE_JOB_RUN.value,
    }:
        raise RuntimePlanAuthorityRefused(
            "lifecycle plan owner does not match action",
            reason=SecurityRefusalReason.HELPER_REQUEST_PLAN_BINDING_INVALID,
        )
    if operation.kind == AgentOperationKind.RECIPE_START.value:
        start = _parse_start(operation.payload)
        _service_run_authority(
            session,
            parent=parent,
            operation=operation,
            node_id=node_id,
            start=start,
        )
        return RuntimePlanBinding(
            start_plan_sha256=_payload_sha256(start),
            run_generation=start.run_generation,
            runtime_run_id=start.run_id,
            runtime_target_id=start.run_id,
            runtime_installation_id=start.installation_id,
        )
    job_plan = _parse_job_run(operation.payload)
    _job_run_authority(
        session,
        parent=parent,
        operation=operation,
        node_id=node_id,
        request=job_plan,
    )
    return RuntimePlanBinding(
        start_plan_sha256=_payload_sha256(job_plan),
        run_generation=job_plan.run_generation,
        runtime_run_id=job_plan.run_id,
        runtime_target_id=job_plan.job_id,
        runtime_installation_id=job_plan.installation_id,
    )


_DIGEST = re.compile(r"[0-9a-f]{64}")


def _own_stop_binding(
    parent: Job, operation: AgentOperation, stop_plan_sha256: str | None
) -> RuntimePlanBinding:
    """Bind the agent's Stop of its own order's container to that identity.

    Only the identity decides: run, container, installation and generation
    from the stored order (a Start or JobRun the order started, or the target
    a Stop order names). The rest of the document is the agent's (its timeout
    is bounded by the helper, ``cancel_pending_start`` fences only this
    identity's own Start, the plan digest only narrows which container
    matches), so it is bound by its digest, never re-derived and compared:
    what the run, its revision, mapping or reservations became since, and the
    digests of the Controller's own rows, never decide a Stop.
    """
    # The grant admits only the kinds a Stop serves (``_ACTION_KINDS``).
    order = (
        _parse_stop(operation.payload)
        if operation.kind == AgentOperationKind.RECIPE_STOP.value
        else _parse_start(operation.payload)
        if operation.kind == AgentOperationKind.RECIPE_START.value
        else _parse_job_run(operation.payload)
    )
    target = own_runtime_target(parent, order)
    # Neither is ever an end of the cleanup: the agent waits and asks again.
    if target is None:
        raise RuntimePlanAuthorityStale(
            "the order is not its parent's own",
            reason=InvalidRequestReason.MALFORMED,
        )
    if stop_plan_sha256 is None or not _DIGEST.fullmatch(stop_plan_sha256):
        raise RuntimePlanAuthorityStale(
            "the Stop document digest is missing",
            reason=InvalidRequestReason.MALFORMED,
        )
    return RuntimePlanBinding(
        stop_plan_sha256=stop_plan_sha256,
        run_generation=target.run_generation,
        runtime_run_id=target.run_id,
        runtime_target_id=target.target_id,
        runtime_installation_id=target.installation_id,
    )


def _service_run_authority(
    session: Session,
    *,
    parent: Job,
    operation: AgentOperation,
    node_id: str,
    start: RecipeStartPayload,
) -> _RunAuthority:
    if parent.kind != "recipe.start" or parent.payload.get("owner_kind") != "run":
        raise RuntimePlanAuthorityRefused(
            "service Start owner is invalid",
            reason=SecurityRefusalReason.HELPER_REQUEST_PLAN_BINDING_INVALID,
        )
    run = session.get(RecipeRun, start.run_id)
    authority = _load_run_authority(session, run)
    run = authority.run
    _check_operation_target(operation, node_id, start, authority)
    if (
        parent.payload.get("owner_id") != run.id
        or parent.payload.get("plan_digest") != run.plan_digest
        or start.run_generation != run.run_generation
        or start.run_id != run.id
        or start.installation_id != run.installation_id
        or start.mapping_id != run.mapping_id
        or start.plan_digest != run.plan_digest
        or parent.authority_revision
        != authority.revision.content_digest.removeprefix("sha256:")
    ):
        raise RuntimePlanAuthorityStale(
            "service Start identity is stale",
            reason=InvalidRequestReason.SUPERSEDED,
        )
    if (
        run.state not in {RunState.STARTING, RunState.RUNNING}
        or authority.installation.state != InstallationState.INSTALLED
        or authority.mapping.state != "ready"
        or authority.revision.state != "active"
    ):
        raise RuntimePlanAuthorityStale(
            "service Start is no longer authorized",
            reason=InvalidRequestReason.NOT_READY,
        )
    return authority


def _job_run_authority(
    session: Session,
    *,
    parent: Job,
    operation: AgentOperation,
    node_id: str,
    request: RecipeJobRunRequest,
) -> _RunAuthority:
    if (
        parent.kind != "recipe.job.run.v1"
        or parent.payload.get("owner_kind") != "artifact-job"
        or parent.payload.get("owner_id") != request.job_id
    ):
        raise RuntimePlanAuthorityRefused(
            "artifact JobRun owner is invalid",
            reason=SecurityRefusalReason.HELPER_REQUEST_PLAN_BINDING_INVALID,
        )
    artifact_job = session.get(ArtifactJob, request.job_id)
    run = session.get(RecipeRun, request.run_id)
    authority = _load_run_authority(session, run)
    run = authority.run
    _check_operation_target(operation, node_id, request, authority)
    if (
        artifact_job is None
        or artifact_job.run_id != run.id
        or artifact_job.operation_id != parent.id
        or parent.payload.get("plan_digest") != run.plan_digest
        or request.installation_id != run.installation_id
        or request.mapping_id != run.mapping_id
        or request.plan_digest != run.plan_digest
        or parent.authority_revision
        != authority.revision.content_digest.removeprefix("sha256:")
    ):
        raise RuntimePlanAuthorityStale(
            "artifact JobRun identity is stale",
            reason=InvalidRequestReason.SUPERSEDED,
        )
    if (
        request.run_generation != run.run_generation
        or run.state != RunState.RUNNING
        or authority.installation.state != InstallationState.INSTALLED
        or authority.mapping.state != "ready"
        or authority.revision.state != "active"
    ):
        raise RuntimePlanAuthorityStale(
            "artifact JobRun start is no longer authorized",
            reason=InvalidRequestReason.NOT_READY,
        )
    return authority


def _load_run_authority(session: Session, run: RecipeRun | None) -> _RunAuthority:
    """The run's durable rows, agreeing with its launch plan.

    Identity is the run, installation, mapping and their generations, the plan
    digest and the Spark placement; which recipe revision row recorded the
    installation is provenance, and the recipe's content is bound by its digest
    (``authority_revision``).
    """
    if run is None:
        raise RuntimePlanEvidenceUnavailable(
            "recipe run is unavailable",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    installation = session.get(RecipeInstallation, run.installation_id)
    revision = (
        session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    mapping = session.get(ClusterMapping, run.mapping_id)
    nodes = tuple(
        session.scalars(
            select(RunNode)
            .where(RunNode.run_id == run.id)
            .order_by(RunNode.rank, RunNode.node_id)
        )
    )
    mapping_nodes = (
        tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == mapping.id)
                .order_by(ClusterMappingNode.rank, ClusterMappingNode.node_id)
            )
        )
        if mapping is not None
        else ()
    )
    try:
        stored = parse_stored_run_plan(run.plan)
    except RecipeExecutionContractError as error:
        raise RuntimePlanEvidenceUnavailable(
            "recipe run plan is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error
    placement = {(node.node_id, node.rank, node.role) for node in nodes}
    if (
        installation is None
        or revision is None
        or mapping is None
        or not nodes
        or run.mapping_id != installation.mapping_id
        or run.mapping_generation != installation.mapping_generation
        or mapping.generation != run.mapping_generation
        or revision.kind != "recipe"
        or revision.schema_version != 2
        or revision.content_digest is None
        or stored.run_generation != run.run_generation
        or stored.installation_id != run.installation_id
        or stored.mapping_id != run.mapping_id
        or stored.mapping_generation != run.mapping_generation
        or stored.plan_digest != run.plan_digest
        or placement != {(node.node_id, node.rank, node.role) for node in stored.nodes}
        or placement != {(node.node_id, node.rank, node.role) for node in mapping_nodes}
    ):
        raise RuntimePlanAuthorityStale(
            "recipe run authority is stale",
            reason=InvalidRequestReason.SUPERSEDED,
        )
    return _RunAuthority(run, installation, revision, mapping, nodes)


def _check_operation_target(
    operation: AgentOperation,
    node_id: str,
    payload: RecipeStartPayload | RecipeJobRunRequest,
    authority: _RunAuthority,
) -> None:
    node = next((item for item in authority.nodes if item.node_id == node_id), None)
    if (
        node is None
        or operation.node_id != node_id
        or payload.run_id != authority.run.id
        or payload.compiled_execution_plan.runtime.placement.rank != node.rank
        or payload.compiled_execution_plan.runtime.placement.role != node.role
        or payload.compiled_execution_plan.runtime.placement.world_size
        != len(authority.nodes)
    ):
        raise RuntimePlanAuthorityRefused(
            "lifecycle plan node identity differs",
            reason=SecurityRefusalReason.HELPER_REQUEST_PLAN_BINDING_INVALID,
        )


def _validate_child_record(
    parent: Job, operation: AgentOperation, node_id: str
) -> None:
    if (
        operation.node_id != node_id
        or operation.parent_job_id != parent.id
        or hashlib.sha256(
            canonical_message(read_row_column(parent, "payload"))
        ).hexdigest()
        != parent.payload_digest
        or hashlib.sha256(canonical_message(operation.payload)).hexdigest()
        != operation.payload_digest
        or operation.authority_revision != parent.authority_revision
    ):
        raise RuntimePlanAuthorityRefused(
            "lifecycle operation record is inconsistent",
            reason=SecurityRefusalReason.HELPER_REQUEST_PLAN_BINDING_INVALID,
        )
    parent_document = serialize_json_value(read_row_column(parent, "payload"))
    phases = (
        parent_document.get("phases") if isinstance(parent_document, Mapping) else None
    )
    if not isinstance(phases, list):
        # The parent's digest-bound document never gains a manifest (an order
        # queued before its producer recorded one).
        raise RuntimePlanUnsatisfiable(
            "lifecycle operation manifest is missing",
            reason=InvalidRequestReason.INCOMPLETE,
        )
    matches = [
        item
        for phase in phases
        if isinstance(phase, list)
        for item in phase
        if isinstance(item, Mapping) and item.get("operation_id") == operation.id
    ]
    if (
        len(matches) != 1
        or matches[0].get("node_id") != node_id
        or not isinstance(matches[0].get("payload"), Mapping)
        or canonical_message(matches[0]["payload"])
        != canonical_message(operation.payload)
    ):
        raise RuntimePlanAuthorityRefused(
            "lifecycle operation manifest differs",
            reason=SecurityRefusalReason.HELPER_REQUEST_PLAN_BINDING_INVALID,
        )


def _parse_start(value: object) -> RecipeStartPayload:
    try:
        if not isinstance(value, Mapping):
            raise InvalidType(
                "Start payload is not an object", reason=InvalidRequestReason.MALFORMED
            )
        return read_stored_model(
            RecipeStartPayload, canonical_message(value), from_json=True
        )
    except (TypeError, ValueError) as error:
        raise RuntimePlanEvidenceUnavailable(
            "durable Start plan is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error


def _parse_job_run(value: object) -> RecipeJobRunRequest:
    try:
        if not isinstance(value, Mapping):
            raise InvalidType(
                "JobRun payload is not an object", reason=InvalidRequestReason.MALFORMED
            )
        return read_stored_model(
            RecipeJobRunRequest, canonical_message(value), from_json=True
        )
    except (TypeError, ValueError) as error:
        raise RuntimePlanEvidenceUnavailable(
            "durable JobRun plan is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error


def _parse_stop(value: object) -> RecipeStopPayload:
    try:
        if not isinstance(value, Mapping):
            raise InvalidType(
                "Stop payload is not an object", reason=InvalidRequestReason.MALFORMED
            )
        return read_stored_model(
            RecipeStopPayload, canonical_message(value), from_json=True
        )
    except (TypeError, ValueError) as error:
        raise RuntimePlanEvidenceUnavailable(
            "durable Stop plan is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error


def _payload_sha256(payload: object) -> str:
    return hashlib.sha256(canonical_message(payload)).hexdigest()
