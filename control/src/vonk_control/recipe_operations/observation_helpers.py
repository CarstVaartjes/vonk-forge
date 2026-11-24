"""Observation helpers for digest-bound recipe operations."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session

from vonk_agent_protocol.agent_operations import AgentOperation as WireAgentOperation
from vonk_agent_protocol.agent_operations import RecipeStartResult
from vonk_agent_protocol.build_import import (
    RecipeBuildCleanupRequest,
    RecipeBuildEvidence,
    RecipeBuildRequest,
)
from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import (
    RecipeInstallPayload,
    RecipeReconcilePayload,
    RecipeStartPayload,
    RecipeStopPayload,
    RecipeUninstallPayload,
)
from vonk_agent_protocol.state_machines import InstallationState, RunState
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.fleet_profiles.stop_contract import ProfileJobRunStopJob
from vonk_control.jobs import states as job_states
from vonk_control.jobs.documents import (
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
from vonk_control.lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.execution import (
    STOPPABLE_NOT_RUNNING_RUN_STATES,
    STOPPABLE_RUN_STATES,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.owner_liveness import OwnerKind, run_has_live_operation
from vonk_control.recipe_operations.errors import RecipeRetryLater
from vonk_control.recipe_operations.execution_contract import (
    RecipeExecutionContractError,
    parse_stored_build_plan,
    parse_stored_build_policy,
    parse_stored_run_plan,
)
from vonk_control.recipe_operations.lifecycle_contract import JOB_ACTIVATION
from vonk_control.recipe_operations.results import _RANK_FAILED
from vonk_control.stored.rows import read_row_column
from vonk_control.stored.states import CatalogRevisionState, RecipeBuildState
from vonk_control.strict_json import read_stored_model


def wait_for_installation_upkeep(session: Session, installation_id: str) -> None:
    """Wait while the installation's own reconcile or uninstall has not ended.

    Starting a run and (re)installing both wait here. The wait names the
    operation it waits for and lasts exactly as long as that operation is
    queued, running or needs an operator: it has no timer of its own.
    """
    upkeep = session.scalar(
        select(Job)
        .where(
            Job.kind.in_(
                (
                    WireAgentOperation.RECIPE_UNINSTALL.value,
                    WireAgentOperation.RECIPE_RECONCILE.value,
                )
            ),
            Job.state.in_(job_states.LIVE),
            Job.owner_kind == OwnerKind.INSTALLATION,
            Job.owner_id == installation_id,
        )
        .order_by(Job.id)
        .limit(1)
    )
    if upkeep is not None:
        raise RecipeRetryLater(
            f"recipe installation waits for its {upkeep.kind} operation "
            f"{upkeep.id} to end"
        )


def run_takes_jobs(session: Session, run: RecipeRun | None) -> bool:
    """Whether a held run admits a job now: running, and no Stop of it live.

    An accepted Stop of a job run waits on its jobs' cancellation with the run
    still ``running``; a job (or a reinstall for one) admitted then would
    escape it, and a stopping run never takes jobs again.
    """

    if run is None or run.state != RunState.RUNNING:
        return False
    return (
        session.scalar(
            select(Job.id)
            .where(
                Job.kind == WireAgentOperation.RECIPE_STOP.value,
                Job.owner_kind == OwnerKind.RUN,
                Job.owner_id == run.id,
                Job.state.in_(job_states.LIVE),
            )
            .limit(1)
        )
        is None
    )


def reinstall_in_flight(session: Session, installation_id: str) -> str | None:
    """The live install a held run's jobs wait for, while it is not installed.

    A lost image makes the installation partial under its held job run
    (``installed_bytes.demote_ranks_on_lost_image``); the next submission
    installs it again (``start_installation``) and its job waits for exactly
    that install: dispatch holds the order and the job shows the wait. With no
    live install there is nothing to wait for, so the order goes on and the
    launch grant answers it.
    """

    installation = session.get(RecipeInstallation, installation_id)
    if installation is None or installation.state == InstallationState.INSTALLED:
        return None
    return session.scalar(
        select(Job.id)
        .where(
            Job.kind == WireAgentOperation.RECIPE_INSTALL.value,
            Job.state.in_(job_states.LIVE),
            Job.owner_kind == OwnerKind.INSTALLATION,
            Job.owner_id == installation_id,
        )
        .limit(1)
    )


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
        CatalogDocumentRevision.state == CatalogRevisionState.ACTIVE,
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
    JOB_ACTIVATION: TypeAdapter(RecipeJobActivateParent),
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
) -> bool:
    """Record a build's image evidence; ``False`` when the evidence does not hold.

    Malformed peer evidence returns an unknown observation to the bounded
    operation owner. Current verified content repairs damaged stored evidence.
    A stored plan or policy never independently vetoes this completion.
    """

    # A retried build may already be present in this transaction's identity map
    # with the previous attempt's upload fields. Refresh under the row lock so
    # terminal evidence is compared with the upload transaction that just
    # completed, not with stale in-memory values.
    session.refresh(build, with_for_update=True)
    try:
        result = RecipeBuildEvidence.model_validate_json(canonical_message(evidence))
    except TypeError, ValueError:
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
    # The authenticated current attempt and verified ingress own these bytes.
    # Stored success/size observations are derived bookkeeping, not a veto.
    build.state = RecipeBuildState.SUCCEEDED.value
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
    evidence that releases its claim. A running job run is the exception: it
    has no process between its one-shot jobs, so no report is evidence of its
    loss; each in-flight job is judged by its own order.
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
        # A stop crossed this report between the unlocked and locked reads: a
        # fact about a moment, so the caller re-observes (never a 422).
        raise RecipeRetryLater(
            "recipe run observation is not assigned yet",
            reason=WaitReason.SCOPE_CHANGED,
        )
    if not included_run_ids:
        for node, run in assigned:
            if _aware(node.updated_at) >= observed_at:
                continue
            if run.state not in STOPPABLE_NOT_RUNNING_RUN_STATES:
                if _run_is_one_shot(session, run):
                    continue
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


def _run_is_one_shot(session: Session, run: RecipeRun) -> bool:
    """Whether the run only hosts one-shot jobs (no service container).

    The stored plan says so; when it does not parse, the evidence is the
    activation operation that created the run, which nothing else produces.
    """

    def read() -> bool:
        return (
            parse_stored_run_plan(read_row_column(run, "plan")).execution_mode
            == "one-shot-jobs"
        )

    def rebuild() -> bool:
        return (
            session.scalar(
                select(Job.id)
                .where(
                    Job.kind == JOB_ACTIVATION,
                    Job.owner_kind == "run",
                    Job.owner_id == run.id,
                )
                .limit(1)
            )
            is not None
        )

    loaded = read_or_rebuild(
        kind="recipe.run-plan", subject=run.id, read=read, rebuild=rebuild
    )
    return loaded is True
