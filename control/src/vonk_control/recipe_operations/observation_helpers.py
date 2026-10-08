"""Observation helpers for digest-bound recipe operations."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    LifecycleState,
    RecipeBuildCleanupRequest,
    RecipeBuildEvidence,
    RecipeBuildRequest,
    RecipeInstallPayload,
    RecipeJobRunRequest,
    RecipeReconcilePayload,
    RecipeStartPayload,
    RecipeStartResult,
    RecipeStopPayload,
    RecipeUninstallPayload,
    canonical_message,
)

from ..categorized_errors import (
    InvalidValue,
)
from ..job_documents import (
    RecipeBuildCleanupParent,
    RecipeBuildParent,
    RecipeInstallParent,
    RecipeJobActivateParent,
    RecipeJobRunParent,
    RecipeReconcileParent,
    RecipeStartParent,
    RecipeStopParent,
    RecipeUninstallParent,
)
from ..lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    retire_as_unknown,
)
from ..models import (
    STOPPABLE_NOT_RUNNING_RUN_STATES,
    STOPPABLE_RUN_STATES,
    AgentOperation,
    CatalogDocumentRevision,
    RecipeBuild,
    RecipeRun,
    RunNode,
)
from ..profile_stop_authority import (
    ProfileJobRunStopJob,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_build_plan,
    parse_stored_build_policy,
)
from ..reservation_owners import run_has_live_operation
from ..stored_json import read_row_column
from ..strict_json import read_stored_model
from .results import _RANK_FAILED


def _active_recipe_revision(
    session: Session,
    revision_id: str | None,
    *,
    for_update: bool = False,
) -> CatalogDocumentRevision | None:
    """Load only an active canonical Recipe revision by stable id."""

    if not isinstance(revision_id, str) or not revision_id:
        return None
    statement = select(CatalogDocumentRevision).where(
        CatalogDocumentRevision.id == revision_id,
        CatalogDocumentRevision.kind == "recipe",
        CatalogDocumentRevision.state == "active",
    )
    if for_update:
        statement = statement.with_for_update(of=CatalogDocumentRevision)
    return session.scalar(statement)


RecipeParent = (
    RecipeInstallParent
    | RecipeStartParent
    | RecipeStopParent
    | RecipeUninstallParent
    | RecipeReconcileParent
    | RecipeJobRunParent
    | RecipeJobActivateParent
    | RecipeBuildParent
    | RecipeBuildCleanupParent
    | ProfileJobRunStopJob
)


_RECIPE_PARENT_READERS: dict[str, TypeAdapter[RecipeParent]] = {
    WireAgentOperation.RECIPE_INSTALL.value: TypeAdapter(RecipeInstallParent),
    WireAgentOperation.RECIPE_START.value: TypeAdapter(RecipeStartParent),
    WireAgentOperation.RECIPE_STOP.value: TypeAdapter(
        RecipeStopParent | ProfileJobRunStopJob
    ),
    WireAgentOperation.RECIPE_UNINSTALL.value: TypeAdapter(RecipeUninstallParent),
    WireAgentOperation.RECIPE_RECONCILE.value: TypeAdapter(RecipeReconcileParent),
    WireAgentOperation.RECIPE_JOB_RUN.value: TypeAdapter(RecipeJobRunParent),
    "recipe.job.activate.v1": TypeAdapter(RecipeJobActivateParent),
    WireAgentOperation.RECIPE_BUILD.value: TypeAdapter(RecipeBuildParent),
    WireAgentOperation.RECIPE_BUILD_CLEANUP.value: TypeAdapter(
        RecipeBuildCleanupParent
    ),
}


RecipeWirePayload = (
    RecipeInstallPayload
    | RecipeStartPayload
    | RecipeStopPayload
    | RecipeUninstallPayload
    | RecipeReconcilePayload
    | RecipeJobRunRequest
    | RecipeBuildRequest
    | RecipeBuildCleanupRequest
)


_PhaseGroups = tuple[tuple[tuple[str, str, RecipeWirePayload], ...], ...]


def record_build_evidence(
    session: Session,
    build: RecipeBuild,
    evidence: object,
    *,
    now: datetime,
    replace_existing: bool = False,
) -> bool:
    """Record a build's image evidence; ``False`` when the evidence does not hold.

    Evidence that is not exactly an image digest, layout digest and size, or that
    differs from what the build already recorded, is not recorded: the caller
    ends the attempt as failed (a retry builds again).  A stored plan or policy
    that does not parse never blocks the evidence: it is retired as unknown.
    """

    # A retried build may already be present in this transaction's identity map
    # with the previous attempt's upload fields. Refresh under the row lock so
    # terminal evidence is compared with the upload transaction that just
    # completed, not with stale in-memory values.
    session.refresh(build, with_for_update=True)
    try:
        result = RecipeBuildEvidence.model_validate_json(canonical_message(evidence))
    except (TypeError, ValueError):
        return False
    image_digest = result.image_digest
    layout_digest = result.oci_layout_sha256
    image_bytes = result.image_bytes
    try:
        parse_stored_build_plan(read_row_column(build, "plan"))
        parse_stored_build_policy(read_row_column(build, "policy_report"))
    except RecipeExecutionContractError as error:
        retire_as_unknown(
            "recipe.build-envelope",
            build.id,
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            f"stored recipe build envelope is invalid{error.detail}",
        )
    if (
        (not replace_existing and build.image_digest not in {None, image_digest})
        or (
            not replace_existing
            and build.oci_layout_sha256 not in {None, layout_digest}
        )
        or (not replace_existing and build.image_bytes not in {None, image_bytes})
    ):
        return False
    build.state = LifecycleState.SUCCEEDED.value
    build.image_digest = image_digest
    build.oci_layout_sha256 = layout_digest
    build.image_bytes = image_bytes
    build.error = None
    build.updated_at = now
    return True


def _start_endpoint(
    operation: AgentOperation, evidence: object
) -> str | Residue | None:
    """The serving rank reports its ready endpoint; every other result is empty.

    A rank-launch phase only launches the process, so it never reports one.
    A start result that does not match its order is retired as unknown (a
    :class:`Residue`): the rank is then recorded as not proven started.
    """

    try:
        result = read_stored_model(
            RecipeStartResult, canonical_message(evidence), from_json=True
        )
        start = read_stored_model(
            RecipeStartPayload,
            canonical_message(read_row_column(operation, "payload")),
            from_json=True,
        )
    except (TypeError, ValueError) as error:
        return retire_as_unknown(
            "recipe.start-result",
            operation.id,
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            f"start result is invalid: {error}",
        )
    serving = (
        start.compiled_execution_plan.runtime.placement.endpoint_address is not None
        and start.phase != "rank-launch"
    )
    if serving != (result.endpoint is not None):
        return retire_as_unknown(
            "recipe.start-result",
            operation.id,
            BookkeepingReason.EVIDENCE_MISMATCH,
            "start endpoint does not match the serving rank",
        )
    return result.endpoint


def _aware(value: datetime) -> datetime:
    return (
        value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    ).astimezone(UTC)


def prepare_exact_recipe_run_observation_nodes(
    session: Session,
    node_id: str,
    observed_at: datetime,
    included_run_ids: set[str],
) -> tuple[RunNode, ...]:
    """Lock this node's stoppable ranks; an empty report fails running ones.

    A complete empty report is also the Spark's word that no local run exists.
    For a stoppable run that is not running and owned by no live operation (a
    cancelled start leaves it lost) that is a fresh absence observation, the
    evidence that releases its claim.
    """

    assigned = tuple(
        session.execute(
            select(RunNode, RecipeRun)
            .join(RecipeRun, RecipeRun.id == RunNode.run_id)
            .where(
                RunNode.node_id == node_id,
                RecipeRun.state.in_(STOPPABLE_RUN_STATES),
            )
            .order_by(RunNode.run_id)
            .with_for_update(of=RunNode)
        )
    )
    if included_run_ids - {node.run_id for node, _ in assigned}:
        raise InvalidValue("recipe run observation is not assigned")
    if not included_run_ids:
        for node, run in assigned:
            if _aware(node.updated_at) >= observed_at:
                continue
            if run.state not in STOPPABLE_NOT_RUNNING_RUN_STATES:
                node.state = _RANK_FAILED
                node.observed_run_generation = None
                node.observation_process_running = None
                node.observation_failure_diagnostics = None
                node.observation_observed_at = None
                node.observation_endpoint_ready = None
                node.updated_at = observed_at
            elif _aware(run.updated_at) < observed_at and not run_has_live_operation(
                session, run.id
            ):
                node.observed_run_generation = run.run_generation
                node.observation_process_running = False
                node.observation_observed_at = observed_at
    return tuple(node for node, _ in assigned)
