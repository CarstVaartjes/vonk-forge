"""Digest-bound orchestration for local recipe installation and execution."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, NoReturn, Protocol

from pydantic import TypeAdapter
from sqlalchemy import func, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session, object_session, sessionmaker
from vonk_agent_protocol import (
    AgentFailureKind,
    AgentFailureResult,
    AgentInstallResult,
    InstallAdmissionCode,
    InstallationNodeState,
    InstallationState,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    RecipeBuildCleanupEvidence,
    RecipeBuildCleanupRequest,
    RecipeBuildCode,
    RecipeBuildEvidence,
    RecipeBuildRequest,
    RecipeInstallPayload,
    RecipeJobRunRequest,
    RecipeReconcilePayload,
    RecipeStartPayload,
    RecipeStartResult,
    RecipeStopPayload,
    RecipeUninstallPayload,
    ReconcileCode,
    ReservationState,
    RouteState,
    RunState,
    SecurityRefusalError,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
    validate_result_for_operation,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)
from vonk_forge_contracts import RecipeDefinition, read_model, read_recipe

from . import agent_operation_states, artifact_job_states, job_states
from .admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    admission_attempts,
    is_admission_contention,
    job_request_key,
    lock_admission_rows,
    node_admission_key,
)
from .agent_jobs import (
    AgentJobService,
    _JsonFlagIsTrue,
    release_owned_reservations_in_session,
    superseded_cancellation_deadline,
)
from .categorized_errors import (
    InvalidValue,
    MissingRecord,
)
from .cluster_mappings import ClusterMappingPlan, ClusterMappingService
from .compiled_execution_plan import (
    MAX_COMPILED_EXECUTION_PLAN_BYTES,
    CompiledExecutionPlanError,
    validate_compiled_launch_payload,
)
from .distributed_lifecycle import (
    DistributedLifecycleError,
    DistributedRecoveryInvalid,
    canonical_distributed_readiness,
)
from .distributed_recovery import (
    enforce_recovery_deadline,
    recovery_start_plan,
    run_node_reports_absent,
    settle_absent_run_in_session,
)
from .install_admission import (
    InstallAdmissionBusy,
    InstallAdmissionService,
    InstallPlan,
    InstallPlanConflict,
)
from .install_admission import (
    require_admissible as require_install_admissible,
)
from .job_documents import (
    DistributedRecoveryMarker,
    ProfilePartialStop,
    RecipeBuildCleanupParent,
    RecipeBuildParent,
    RecipeInstallParent,
    RecipeJobActivateParent,
    RecipeJobRunParent,
    RecipeReconcileParent,
    RecipeStartParent,
    RecipeStopParent,
    RecipeUninstallParent,
    RunSwitchJobPayload,
)
from .lifecycle import CancelRequested, Effect, Outcome, Reported
from .lifecycle.agent_operation import AgentOperationAdapter, retry_scheduled
from .lifecycle.artifact_job import ArtifactJobAdapter
from .lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from .lifecycle.recipe_operation import RecipeOperationAdapter
from .logging import redact_text
from .mapping_parameters import MappingParameters
from .models import (
    STOPPABLE_NOT_RUNNING_RUN_STATES,
    STOPPABLE_RUN_STATES,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AgentPresence,
    ArtifactJob,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    FleetProfileApplication,
    InstallationNode,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from .prebuilt_images import policy_prebuilt_reference, prebuilt_reference
from .profile_stop_authority import (
    ProfileJobRunStopAuthorization,
    ProfileJobRunStopJob,
    ProfileJobRunStopTarget,
    ProfileStopAuthorityError,
    validate_profile_jobrun_stop_target,
    validate_profile_stop_owner,
)
from .recipe_action_plans import (
    StopNodeImpact,
    StopPlan,
    UninstallActiveRun,
    UninstallNodeImpact,
    UninstallPlan,
    stop_plan,
    uninstall_plan,
)
from .recipe_build_cancellation import (
    BuildConsumerError,
    RecipeBuildIntent,
    build_cancellation,
    current_build_consumers,
    read_build_intent,
    request_build_cancellation,
)
from .recipe_builds import RecipeBuildAdmissionBusy, RecipeBuildPlan, RecipeBuildService
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    StoredInstallationPlan,
    StoredRunPlan,
    build_plan_document,
    parse_stored_build_plan,
    parse_stored_build_policy,
    parse_stored_installation_plan,
    parse_stored_run_plan,
    run_endpoint_document,
    run_plan_document,
)
from .recipe_lifecycle_contract import (
    LifecycleCodeFailureResult,
    LifecycleNodeResult,
    RecipeLifecycleResult,
    RecipeOperationCancellationResult,
    RecipeOperationProgressResult,
    RecipeOperationResult,
    parse_recipe_lifecycle_result,
    validate_recipe_lifecycle_terminal,
)
from .recipe_routes import (
    RecipeRouteError,
    RecipeRouteNotReady,
    RecipeRouteService,
    route_health_recovery_pending,
    route_publication_transaction,
)
from .recipe_runtime_specs import recipe_topology
from .recipe_start_payloads import (
    RecipeStartPayloadError,
    RecipeStartPlacement,
    build_recipe_start_payload,
    validate_distributed_start_timeout_seconds,
)
from .recipe_stop_payloads import (
    RecipeStopAuthorityError,
    durable_run_stop_payloads,
    stop_payload_from_job_run,
)
from .recovery_policy import FailureKind
from .reservation_owners import run_has_live_operation
from .run_admission import (
    RunAdmissionBusy,
    RunAdmissionService,
    RunNodePlan,
    RunPlan,
)
from .run_admission import (
    require_admissible as require_run_admissible,
)
from .run_switch_contract import RunSwitchReconciliationAuthority
from .source_policy import SourcePolicyReport
from .storage_demands import StorageDemands, spark_scope
from .strict_json import read_stored_model, serialize_json_value

# Longest rendered blocker reason kept in an install refusal.  Each reason names
# the check and then the cause, so the bound has to preserve both ends.
_BLOCKER_REASON_CHARS = 200


def _bounded_blocker_reason(code: str, detail: str) -> str:
    """Keep the innermost cause visible when a blocker reason is bounded.

    Blocker details compose as "outer context: inner cause", so a prefix-only
    bound discards the actionable part: a live refusal reported
    "...is unavailable: runtime image receipt iden" and nothing else, leaving
    the failing rule invisible on every operator surface.  The head names the
    check; the tail carries the cause.
    """

    rendered = f"{code}: {detail}"
    if len(rendered) <= _BLOCKER_REASON_CHARS:
        return rendered
    keep = _BLOCKER_REASON_CHARS - 3
    head = keep // 2
    return f"{rendered[:head]}...{rendered[-(keep - head) :]}"


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


class AgentJobQueue(Protocol):
    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: Mapping[str, object],
        *,
        operation_id: str,
    ) -> AgentOperation: ...

    def notify_available(self) -> None: ...


class RecipeOperationConflict(RuntimeError):
    """A lifecycle request is stale, conflicting, or unsafe to execute."""


class RecipeRequestInvalid(InvalidRequestError, RecipeOperationConflict):
    """The request names an entity, argument or operation that cannot be acted on.

    It is refused at submit time, before anything is persisted, and the caller
    can change the request.  It is never raised for damaged stored state: that is
    rebuilt from evidence or retired as unknown (``lifecycle.evidence``).
    """


class RecipeStopAuthorityRefused(SecurityRefusalError, RecipeOperationConflict):
    """A destructive Stop or workload-intent fence lacks the exact authority.

    A Stop is built only from the run's exact durable Start authority, and a job
    joins only the workload intent it was admitted under.  Without that proof the
    Controller must not invent a destructive payload or take a newer intent.
    """


class RecipeRetryLater(UnknownOutcomeError, RecipeOperationConflict):
    """The request cannot be served yet (a service not bound, an owner busy, evidence
    not reported); nothing was persisted and the caller retries it."""


class RecipeBuildOwnershipBusy(RecipeRetryLater):
    """A build's owner rows are locked by another writer: the cancellation that
    met it repeats its transaction, and the requester hears of it only after the
    attempts are spent."""


class _RouteNotWithdrawn(UnknownOutcomeError):
    """The run's route is listed again; withdraw it again before dispatching."""


class RecipeReconciliationBlocked(UnknownOutcomeError, RecipeOperationConflict):
    """A corrupt installation lacks exact, current cleanup authority."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class InstallationReconciliationTarget:
    """One installed rank and whether its cleanup already succeeded."""

    node_id: str
    rank: int
    role: str
    installed_bytes: int
    state: str

    def document(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "rank": self.rank,
            "role": self.role,
            "installed_bytes": self.installed_bytes,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class InstallationReconciliationAuthority:
    """Immutable identity-only teardown authority; never an execution plan."""

    installation_id: str
    original_plan_digest: str
    recipe_revision_id: str
    recipe_content_sha256: str
    mapping_id: str
    mapping_generation: int
    recipe_build_id: str | None
    image_digest: str
    model_content_sha256: str | None
    targets: tuple[InstallationReconciliationTarget, ...]

    def document(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "installation_id": self.installation_id,
            "original_plan_digest": self.original_plan_digest,
            "recipe_revision_id": self.recipe_revision_id,
            "recipe_content_sha256": self.recipe_content_sha256,
            "mapping_id": self.mapping_id,
            "mapping_generation": self.mapping_generation,
            "recipe_build_id": self.recipe_build_id,
            "image_digest": self.image_digest,
            "model_content_sha256": self.model_content_sha256,
            "targets": [target.document() for target in self.targets],
        }


class RecipeArtifactJobCancellationPending(RecipeOperationConflict):
    """An issued one-shot job still needs its exact cancellation receipt."""

    def __init__(
        self, *, job_id: str, observe_due_at: datetime, observation_deadline: datetime
    ) -> None:
        super().__init__(f"artifact job cancellation is pending: {job_id}")
        self.job_id = job_id
        self.observe_due_at = observe_due_at
        self.observation_deadline = observation_deadline


class RecipeInstallPreflightExpired(RecipeOperationConflict):
    """Acceptance refused an identical plan on runtime preflight evidence.

    The receipt aged out, the host fingerprint moved, or it does not cover this
    recipe's requirements.  Nothing was persisted, so a caller that owns a
    bounded runtime preflight gate may rerun its ordinary probe and re-present
    the same plan; every other caller keeps treating this as the conflict it is.
    """


def _validated_result(kind: str, value: object) -> dict[str, object] | None:
    """Validate a lifecycle result this call composes, before it is persisted.

    Only documents the caller has just built come through here; a stored result
    is read with :func:`_recorded_result`, which never raises.
    """

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise RecipeRequestInvalid("recipe operation result is invalid")
    try:
        parsed = parse_recipe_lifecycle_result(kind, value)
    except (TypeError, ValueError) as error:
        raise RecipeRequestInvalid("recipe operation result is invalid") from error
    return json.loads(canonical_message(parsed))


def _recorded_result(
    kind: str, value: object, *, subject: str
) -> RecipeLifecycleResult | None:
    """A stored lifecycle result; damaged evidence is retired as unknown.

    The result is evidence of what was done, never a reason to refuse the next
    step: a result that does not parse is recorded as residue and read as absent.
    """

    if value is None:
        return None

    def read() -> RecipeLifecycleResult:
        return parse_recipe_lifecycle_result(kind, value)

    loaded = read_or_rebuild(kind="recipe.operation-result", subject=subject, read=read)
    return None if isinstance(loaded, Residue) else loaded


def _recorded_result_document(
    kind: str, value: object, *, subject: str
) -> dict[str, object] | None:
    """Serialize a validated result only for a merged write or API response."""
    result = _recorded_result(kind, value, subject=subject)
    return None if result is None else json.loads(canonical_message(result))


def _node_result(
    kind: str, node_id: str, evidence: object
) -> LifecycleNodeResult | None:
    try:
        result = parse_recipe_lifecycle_result(
            kind, {"node_evidence": {node_id: evidence}}
        )
    except (TypeError, ValueError):
        return None
    if (
        isinstance(result, RecipeOperationProgressResult)
        and result.node_evidence is not None
    ):
        return result.node_evidence[node_id]
    return None


# The one place the stored word of a failed rank is spelled for the rank rows.
_RANK_FAILED = "failed"


def _unproven_evidence(detail: str) -> LifecycleCodeFailureResult:
    """The typed marker recorded for a node whose evidence cannot be accepted."""

    return LifecycleCodeFailureResult(
        code="recipe.evidence_unproven", detail=detail[:512]
    )


def _evidence_is_acceptable(kind: str, node_id: str, evidence: object) -> bool:
    """Whether the lifecycle contract accepts this node evidence for the kind."""

    try:
        parse_recipe_lifecycle_result(kind, {"node_evidence": {node_id: evidence}})
    except (TypeError, ValueError):
        return False
    return True


_AcceptedRanks = tuple[frozenset[tuple[str, int, str]], bool]


def _ranks_from_mapping(
    session: Session, mapping_id: str, generation: int, revision_id: str
) -> _AcceptedRanks | None:
    """The accepted (node, rank, role) set from the saved mapping, as evidence.

    A mapping generation is immutable, so it names the same ranks the stored plan
    of an installation or run was accepted with.
    """

    mapping = session.get(ClusterMapping, mapping_id)
    if mapping is None:
        return None
    rows = tuple(
        session.scalars(
            select(ClusterMappingNode).where(
                ClusterMappingNode.mapping_id == mapping_id
            )
        )
    )
    if not rows:
        return None
    ranks = frozenset((row.node_id, row.rank, row.role) for row in rows)
    return ranks, (
        len(ranks) == len(rows)
        and mapping.node_count == len(rows)
        and mapping.generation == generation
        and mapping.recipe_revision_id == revision_id
    )


def _plan_ranks(
    plan: StoredRunPlan | StoredInstallationPlan,
) -> tuple[frozenset[tuple[str, int, str]], int]:
    return frozenset((node.node_id, node.rank, node.role) for node in plan.nodes), len(
        plan.nodes
    )


def _run_accepted_ranks(
    session: Session, run: RecipeRun, revision_id: str
) -> _AcceptedRanks | Residue:
    """The ranks a run was accepted with and whether its identity still matches.

    A stored run plan that does not parse is rebuilt from the saved mapping; when
    that is gone too the damage is retired as unknown and the caller treats the
    membership as unproven (it never refuses on the damaged document itself).
    """

    def read() -> _AcceptedRanks | Damaged:
        document = parse_stored_run_plan(run.plan)
        ranks, count = _plan_ranks(document)
        return ranks, (
            len(ranks) == count
            and document.installation_id == run.installation_id
            and document.mapping_id == run.mapping_id
            and document.mapping_generation == run.mapping_generation
            and document.recipe_revision_id == revision_id
            and document.plan_digest == run.plan_digest
        )

    return read_or_rebuild(
        kind="recipe.run-plan",
        subject=run.id,
        read=read,
        rebuild=lambda: _ranks_from_mapping(
            session, run.mapping_id, run.mapping_generation, revision_id
        ),
    )


@dataclass(frozen=True, slots=True)
class _UninstallRecipe:
    """What an uninstall needs to know of the recipe an installation came from."""

    id: str
    document_id: str
    content_digest: str
    document: RecipeDefinition | None


def _uninstall_recipe(
    session: Session, installation: RecipeInstallation, *, lock: bool
) -> _UninstallRecipe | None:
    """The recipe of an installation, from its active revision or else from the
    evidence the installation itself carries (its revision row in any state and
    the digest its accepted plan recorded); ``None`` only when no exact digest
    can be proven, which an uninstall cannot do without."""

    revision = _active_recipe_revision(
        session, installation.recipe_revision_id, for_update=lock
    )
    if revision is not None and revision.content_digest is not None:
        return _UninstallRecipe(
            revision.id,
            revision.document_id,
            revision.content_digest,
            _catalog_recipe(revision.document),
        )
    row = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
    digest = row.content_digest if row is not None else None
    if digest is None:
        loaded_plan = read_or_rebuild(
            kind="recipe.installation-plan",
            subject=installation.id,
            read=lambda: parse_stored_installation_plan(
                installation.plan, for_uninstall=True
            ),
        )
        if not isinstance(loaded_plan, Residue):
            digest = loaded_plan.recipe_content_sha256
    if not isinstance(digest, str) or not _lower_hex_digest(digest):
        return None
    retire_as_unknown(
        "recipe.installation-recipe",
        installation.id,
        BookkeepingReason.EVIDENCE_UNAVAILABLE,
        "the recipe revision is not active; the uninstall uses its recorded digest",
    )
    document = row.document if row is not None else None
    return _UninstallRecipe(
        installation.recipe_revision_id,
        row.document_id if row is not None else installation.recipe_revision_id,
        digest,
        _catalog_recipe(document),
    )


def _installation_accepted_ranks(
    session: Session,
    installation: RecipeInstallation,
    revision: _UninstallRecipe,
) -> _AcceptedRanks | Residue:
    """The ranks an installation was accepted with; rebuilt from its mapping."""

    def read() -> _AcceptedRanks | Damaged:
        document = parse_stored_installation_plan(installation.plan, for_uninstall=True)
        ranks, count = _plan_ranks(document)
        return ranks, (
            len(ranks) == count
            and document.mapping_id == installation.mapping_id
            and document.mapping_generation == installation.mapping_generation
            and document.recipe_revision_id == revision.id
            and document.recipe_content_sha256 == revision.content_digest
            and document.plan_digest == installation.plan_digest
        )

    return read_or_rebuild(
        kind="recipe.installation-plan",
        subject=installation.id,
        read=read,
        rebuild=lambda: _ranks_from_mapping(
            session,
            installation.mapping_id,
            installation.mapping_generation,
            revision.id,
        ),
    )


def _profile_jobrun_parent(job: Job) -> ProfileJobRunStopJob | Residue:
    """The typed parent of a profile JobRun Stop; damage is retired as unknown."""

    return read_or_rebuild(
        kind="recipe.profile-jobrun-stop-parent",
        subject=job.id,
        read=lambda: ProfileJobRunStopJob.model_validate_parent(job.payload),
    )


def _run_is_one_shot(session: Session, run: RecipeRun) -> bool:
    """Whether the run only hosts one-shot jobs (no service container).

    The stored plan says so; when it does not parse, the evidence is the
    activation operation that created the run, which nothing else produces.
    """

    def read() -> bool:
        return parse_stored_run_plan(run.plan).execution_mode == "one-shot-jobs"

    def rebuild() -> bool:
        return (
            session.scalar(
                select(Job.id)
                .where(
                    Job.kind == "recipe.job.activate.v1",
                    Job.payload["owner_kind"].as_string() == "run",
                    Job.payload["owner_id"].as_string() == run.id,
                )
                .limit(1)
            )
            is not None
        )

    loaded = read_or_rebuild(
        kind="recipe.run-plan", subject=run.id, read=read, rebuild=rebuild
    )
    return loaded is True


def _run_observes_per_generation(run: RecipeRun) -> bool:
    """Whether the run's observations are tracked per generation.

    A plan that does not parse is read as the current schema: the observations it
    resets are re-established by the next report of each rank, so nothing is lost.
    """

    def read() -> bool:
        return parse_stored_run_plan(run.plan).observation_schema_version == 2

    loaded = read_or_rebuild(kind="recipe.run-plan", subject=run.id, read=read)
    return True if isinstance(loaded, Residue) else loaded


_RECIPE_WIRE_PAYLOAD_MODELS = {
    "recipe.build.cleanup.v1": RecipeBuildCleanupRequest,
    "recipe.install": RecipeInstallPayload,
    "recipe.reconcile": RecipeReconcilePayload,
    "recipe.start": RecipeStartPayload,
    "recipe.stop": RecipeStopPayload,
    "recipe.uninstall": RecipeUninstallPayload,
}


@dataclass(frozen=True, slots=True)
class RecipeOperationView:
    id: str
    kind: str
    owner_id: str
    state: str
    plan_digest: str
    nodes: tuple[str, ...]
    lifecycle_result: RecipeLifecycleResult | None
    retry_due_at: datetime | None = None
    status_reason: str | None = None

    @property
    def result(self) -> dict[str, object] | None:
        """Serialize the canonical result for the public JSON response."""
        return (
            None
            if self.lifecycle_result is None
            else json.loads(canonical_message(self.lifecycle_result))
        )


@dataclass(frozen=True, slots=True)
class IssuedWorkloadReconciliation:
    job_id: str
    kind: str
    owner_id: str
    plan_digest: str
    payload_digests: tuple[str, ...]
    failure_kind: FailureKind
    observe_due_at: datetime
    observation_deadline: datetime


@dataclass(frozen=True, slots=True)
class RecipeRunRankStatus:
    node_id: str
    rank: int
    role: str
    state: str
    observed_at: datetime
    age_seconds: float
    fresh: bool


@dataclass(frozen=True, slots=True)
class RecipeRunRecoveryOwner:
    operation_id: str
    kind: Literal["recipe.start", "recipe.stop"]
    state: str  # a word of the core vocabulary


@dataclass(frozen=True, slots=True)
class RecipeRunStatus:
    id: str
    alias: str
    state: str
    route_state: str
    healthy: bool
    ranks: tuple[RecipeRunRankStatus, ...]
    run_generation: int
    observation_deadline_at: datetime | None
    route_error: str | None
    route_next_attempt_at: datetime | None
    route_recovery_pending: bool
    recovery_owners: tuple[RecipeRunRecoveryOwner, ...]


new_recipe_job = RecipeOperationAdapter.new_job
_TERMINAL_JOB_STATES = frozenset(
    job_states.words(
        LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
    )
)
_INITIAL_OBSERVATION_GRACE_SECONDS = 120
# A Stop withdraws the run's route again if a competing publication listed it
# between the withdrawal and the dispatch.
_STOP_WITHDRAWAL_ATTEMPTS = 3
_MEMORY_RESERVATION_KINDS = frozenset({"unified-memory", "host-memory", "gpu-memory"})
_MAX_ACTION_NODES = 1024
_MAX_ACTIVE_RUNS = 128
_WORKLOAD_INTENT_KINDS = frozenset(
    {
        "recipe.install",
        "recipe.start",
        "recipe.stop",
        "recipe.uninstall",
        "recipe.reconcile",
        "recipe.job.run.v1",
    }
)


def _bound_workload_intent(job: Job) -> int:
    """The exact intent a job was admitted under; a fence, so it never defaults."""

    ordinal = _parent_intent(job)
    if type(ordinal) is not int or ordinal < 1:
        raise RecipeStopAuthorityRefused("workload operation lacks its admitted intent")
    return ordinal


def _job_workload_intent(session: Session, job: Job) -> int | None:
    """The intent a job was admitted under, re-derived from its orders when its
    own payload lost it; ``None`` when nothing proves it (the caller then treats
    the job as no longer current, never as the newest intent)."""

    def read() -> int | Damaged:
        ordinal = _parent_intent(job)
        if type(ordinal) is not int or ordinal < 1:
            return Damaged("job payload carries no workload intent")
        return ordinal

    def rebuild() -> int | None:
        ordinals = {
            child.workload_intent_ordinal
            for child in session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            )
        }
        return ordinals.pop() if len(ordinals) == 1 else None

    loaded = read_or_rebuild(
        kind="recipe.workload-intent", subject=job.id, read=read, rebuild=rebuild
    )
    if isinstance(loaded, Residue) or loaded < 1:
        return None
    return loaded


def _run_start_intent(session: Session, run_id: str) -> int:
    run = session.get(RecipeRun, run_id)
    if run is None:
        raise RecipeRequestInvalid("recipe run does not exist")
    root_kind = (
        "recipe.job.activate.v1" if _run_is_one_shot(session, run) else "recipe.start"
    )
    starts = tuple(
        session.scalars(
            select(Job)
            .where(
                Job.kind == root_kind,
                Job.payload["owner_kind"].as_string() == "run",
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].as_string().is_(None),
            )
            .order_by(Job.created_at, Job.id)
            .limit(2)
        )
    )
    if len(starts) != 1:
        raise RecipeStopAuthorityRefused(
            "recipe run lacks its original workload authority"
        )
    return _bound_workload_intent(starts[0])


def _intent_is_current(session: Session, ordinal: int, targets: Sequence[str]) -> bool:
    nodes = tuple(
        session.scalars(
            select(AgentNode)
            .where(AgentNode.node_id.in_(targets))
            .order_by(AgentNode.node_id)
            .with_for_update(of=AgentNode)
        )
    )
    return tuple(node.node_id for node in nodes) == tuple(sorted(set(targets))) and all(
        node.workload_intent_ordinal == ordinal for node in nodes
    )


def _shared_workload_intent(session: Session, targets: Sequence[str]) -> int | None:
    """The one workload intent every target Spark currently shares, if there is one.

    A cleanup of what an older, superseded order may have launched is admitted as
    a child of the intent that owns the Sparks now: it takes no new intent and so
    never cancels the newer work, and the stale ordinal of the failed order (which
    would be refused) is not needed.
    """

    nodes = tuple(
        session.scalars(
            select(AgentNode)
            .where(AgentNode.node_id.in_(targets))
            .order_by(AgentNode.node_id)
            .with_for_update(of=AgentNode)
        )
    )
    ordinals = {node.workload_intent_ordinal for node in nodes}
    if len(nodes) != len(set(targets)) or len(ordinals) != 1:
        return None
    ordinal = ordinals.pop()
    return ordinal if ordinal >= 1 else None


def _workload_owner_scope(
    session: Session, kind: str, owner_id: str
) -> tuple[str, ...]:
    """The Sparks an owner's rows place work on; empty when its rows are damaged.

    An owner row that names no Spark (or one twice) is bookkeeping damage: it is
    retired as unknown and callers use the Sparks each job itself names.
    """

    if kind in {"recipe.start", "recipe.stop"}:
        if session.get(RecipeRun, owner_id) is None:
            raise RecipeRequestInvalid("recipe run does not exist")
        statement = select(RunNode.node_id).where(RunNode.run_id == owner_id)
    elif kind in {"recipe.install", "recipe.uninstall", "recipe.reconcile"}:
        if session.get(RecipeInstallation, owner_id) is None:
            raise RecipeRequestInvalid("recipe installation does not exist")
        statement = select(InstallationNode.node_id).where(
            InstallationNode.installation_id == owner_id
        )
    else:
        raise RecipeRequestInvalid("workload reconciliation kind is invalid")
    targets = tuple(sorted(session.scalars(statement)))
    if not targets or len(targets) != len(set(targets)):
        retire_as_unknown(
            "recipe.workload-owner-scope",
            owner_id,
            BookkeepingReason.ROW_INCOMPLETE,
            f"{kind} owner has no exact Spark membership",
        )
        return ()
    return targets


def _profile_effect_scope(
    owner_scope: tuple[str, ...], profile_target_node_ids: Sequence[str] | None
) -> tuple[str, ...]:
    """Validate FleetProfile's reachable effect subset against the full owner."""
    if profile_target_node_ids is None:
        return owner_scope
    targets = tuple(profile_target_node_ids)
    if (
        not targets
        or targets != tuple(sorted(set(targets)))
        # An owner whose rows name no Spark (retired as unknown) proves no subset.
        or (owner_scope and not set(targets) < set(owner_scope))
    ):
        raise RecipeRequestInvalid("profile Stop target scope is invalid")
    return targets


def _active_owned_workload_jobs(
    session: Session,
    kind: str,
    owner_id: str,
    *,
    lock: bool = False,
    include_waiting_cancellation: bool = False,
) -> tuple[Job, ...]:
    owner_kind = "run" if kind in {"recipe.start", "recipe.stop"} else "installation"
    statement = (
        select(Job)
        .where(
            Job.kind == kind,
            Job.state.in_(
                job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.NEEDS_OPERATOR,
                )
                if include_waiting_cancellation
                else ("queued", "running")
            ),
            Job.payload["owner_kind"].as_string() == owner_kind,
            Job.payload["owner_id"].as_string() == owner_id,
        )
        .order_by(Job.id)
    )
    if lock:
        statement = statement.with_for_update(of=Job)
    return tuple(
        job
        for job in session.scalars(statement)
        if job.state not in job_states.words(LifecycleState.NEEDS_OPERATOR)
        or (_cancel_requested(job))
    )


def _unissued_workload_children(
    session: Session, job: Job, *, lock: bool = False
) -> tuple[AgentOperation, ...] | None:
    ordinal = _parent_intent(job)
    if type(ordinal) is not int or ordinal < 1:
        return None
    statement = (
        select(AgentOperation)
        .where(AgentOperation.parent_job_id == job.id)
        .order_by(AgentOperation.id)
    )
    if lock:
        statement = statement.with_for_update(of=AgentOperation)
    children = tuple(session.scalars(statement))
    if not children or any(
        child.state != "queued"
        or child.current_attempt != 0
        or child.workload_intent_ordinal != ordinal
        or child.node_id not in job.targets
        for child in children
    ):
        return None
    if (
        session.scalar(
            select(AgentOperationAttempt.id)
            .where(
                AgentOperationAttempt.operation_id.in_(child.id for child in children)
            )
            .limit(1)
        )
        is not None
    ):
        return None
    return children


def _cancel_reason(value: object) -> str:
    reason = " ".join(str(value).split())
    if not reason:
        raise RecipeRequestInvalid(
            "cancellation reason is required", reason=InvalidRequestReason.INCOMPLETE
        )
    return reason[:512]


class RecipeOperationService:
    """Turn accepted admission plans into one fenced, gang-aware operation group."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        install_admission: InstallAdmissionService,
        run_admission: RunAdmissionService,
        agent_jobs: AgentJobQueue,
        clock: Callable[[], datetime],
        route_withdrawer: Callable[[str], None] | None = None,
        route_publications: RecipeRouteService | None = None,
        builds: RecipeBuildService | None = None,
        mappings: ClusterMappingService | None = None,
        run_health_maximum_age_seconds: int = 300,
        distributed_start_timeout_seconds: int = 3600,
    ) -> None:
        if not 1 <= run_health_maximum_age_seconds <= 300:
            raise InvalidValue("recipe run health age is invalid")
        self._distributed_start_timeout_seconds = (
            validate_distributed_start_timeout_seconds(
                distributed_start_timeout_seconds
            )
        )
        self._sessions = sessions
        self._install_admission = install_admission
        self._run_admission = run_admission
        self._agent_jobs = agent_jobs
        self._clock = clock
        self._route_withdrawer = route_withdrawer or (lambda _run_id: None)
        self._route_publications = route_publications
        self._builds = builds
        # The mapping service holds nothing but the sessions, so a process that
        # was not handed one builds its own instead of refusing mapping requests.
        self._mappings = (
            mappings if mappings is not None else ClusterMappingService(sessions)
        )
        self._build_cleanup_cursor: str | None = None
        self._run_health_maximum_age = timedelta(seconds=run_health_maximum_age_seconds)

    # Bound by the worker; refused installs only ask for space where it is set.
    _storage_demands: StorageDemands | None = None

    def bind_storage_demands(self, demands: StorageDemands) -> None:
        """Attach the register an install refused for lack of disk asks space in."""

        self._storage_demands = demands

    def _request_install_storage(self, plan: InstallPlan) -> None:
        """Ask for the free disk each Spark that refused this install lacks."""

        if self._storage_demands is None:
            return
        for node in plan.nodes:
            if (
                node.free_bytes is None
                or node.free_after_bytes is None
                or not any(
                    reason.code == InstallAdmissionCode.INSUFFICIENT_DISK
                    for reason in node.blockers
                )
            ):
                continue
            self._storage_demands.request(
                spark_scope(node.node_id),
                node.free_bytes - node.free_after_bytes + node.disk_floor_bytes,
                source="install",
                subject=plan.recipe_revision_id,
                reason=InstallAdmissionCode.INSUFFICIENT_DISK,
            )

    def preview_mapping(
        self,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        *,
        parameters: MappingParameters,
        actor: str,
    ) -> ClusterMappingPlan:
        return self._mappings.preview(recipe_revision_id, node_ids, parameters, actor)

    def create_mapping(self, plan: ClusterMappingPlan, *, actor: str) -> str:
        try:
            return self._mappings.materialize(plan, actor=actor, now=self._clock())
        except (RuntimeError, ValueError) as error:
            raise RecipeRequestInvalid(str(error)) from error

    def preview_build(
        self, recipe_revision_id: str, builder_node_id: str
    ) -> RecipeBuildPlan:
        if self._builds is None:
            raise RecipeRetryLater("recipe build service is unavailable")
        return self._builds.plan(recipe_revision_id, builder_node_id, now=self._clock())

    def reusable_build_id(self, recipe_revision_id: str) -> str | None:
        if self._builds is None:
            return None
        return self._builds.reusable_build_id(recipe_revision_id)

    def check_build_source(self, recipe_revision_id: str) -> SourcePolicyReport:
        if self._builds is None:
            raise RecipeRetryLater("recipe build service is unavailable")
        return self._builds.check_source(recipe_revision_id)

    def _build_once(
        self,
        plan: RecipeBuildPlan,
        *,
        build_input_sha256: str,
        actor: str,
        request_id: str,
        force: bool = False,
        admission_guard: Callable[[Session], None] | None = None,
    ) -> RecipeOperationView:
        # A stale independent build request is re-planned with the current
        # builder inputs instead of being refused. A request that matches its
        # plan keeps that exact identity, and a parent Run/Switch request binds
        # the build identity in its accepted plan, so its admission guard keeps
        # it fixed. reserve_in_session rechecks current capacity either way.
        if (
            self._builds is not None
            and admission_guard is None
            and build_input_sha256 != plan.build_input_sha256
        ):
            builder_node_id = plan.builder_node_id
            with self._sessions() as session:
                previous_build = session.get(RecipeBuild, plan.build_id)
                if (
                    previous_build is not None
                    and previous_build.recipe_revision_id == plan.recipe_revision_id
                ):
                    builder_node_id = previous_build.builder_node_id
            refreshed = self._builds.prepare_plan(
                plan.recipe_revision_id, builder_node_id, now=self._clock()
            )
            if (
                refreshed.builder_node_id != plan.builder_node_id
                or refreshed.build_input_sha256 != plan.build_input_sha256
            ):
                with self._sessions.begin() as session:
                    plan = self._builds.persist_plan_in_session(
                        session, refreshed, now=self._clock()
                    )
            build_input_sha256 = plan.build_input_sha256
        intent = RecipeBuildIntent(
            kind="dependency" if admission_guard is not None else "independent"
        )
        # Force bypasses cached images, not the identity of an accepted request.
        existing = self._idempotent(request_id, "recipe.build.v1", build_input_sha256)
        if existing is not None:
            if (
                existing.state not in {"succeeded", "cancelled"}
                and not force
                and not (_cancel_requested(existing))
            ):
                with self._sessions() as session:
                    succeeded = self._successful_build_job_in_session(
                        session, existing.owner_id, build_input_sha256
                    )
                    if succeeded is not None:
                        return self._view(succeeded)
            return existing
        now = self._clock()
        with self._sessions.begin() as session:
            try:
                acquire_admission_keys(
                    session,
                    (
                        job_request_key(request_id),
                        node_admission_key(plan.builder_node_id),
                    ),
                    holder="recipe-operation",
                )
                locked = lock_admission_rows(
                    session,
                    (
                        AdmissionRowLock(
                            "build-builder-node",
                            AgentNode,
                            select(AgentNode).where(
                                AgentNode.node_id == plan.builder_node_id
                            ),
                        ),
                        AdmissionRowLock(
                            "build-recipe-revision",
                            CatalogDocumentRevision,
                            select(CatalogDocumentRevision).where(
                                CatalogDocumentRevision.id == plan.recipe_revision_id
                            ),
                        ),
                        AdmissionRowLock(
                            "build-recipe-build",
                            RecipeBuild,
                            select(RecipeBuild).where(RecipeBuild.id == plan.build_id),
                        ),
                    ),
                )
            except AdmissionLockBusy as error:
                raise RecipeBuildAdmissionBusy() from error
            build = next(iter(locked["build-recipe-build"]), None)

            # A parent-owned build must revalidate its exact execution claim
            # in the same transaction that accepts the child. The guard does
            # SQL work only and retains its parent fence through this commit.
            if admission_guard is not None:
                admission_guard(session)
            if (
                build is not None
                and build.state == "succeeded"
                and not force
                and build.build_input_sha256 == plan.build_input_sha256
                and build.builder_node_id == plan.builder_node_id
            ):
                succeeded = self._successful_build_job_in_session(
                    session, build.id, build.build_input_sha256
                )
                receipt = (
                    _recorded_result_document(
                        "recipe.build.v1", succeeded.result, subject=succeeded.id
                    )
                    if succeeded is not None
                    else None
                )
                if succeeded is None or receipt is None:
                    # The build row says succeeded but its receipt is gone: the
                    # evidence is rebuilt by building again (the same replacement
                    # a forced build performs), never by refusing the request.
                    retire_as_unknown(
                        "recipe.build-receipt",
                        build.id,
                        BookkeepingReason.ROW_INCOMPLETE,
                        "succeeded build has no successful receipt",
                    )
                    force = True
                else:
                    replay = new_recipe_job(
                        id=str(uuid.uuid4()),
                        request_id=request_id,
                        kind=succeeded.kind,
                        state="succeeded",
                        actor=actor,
                        authority_revision=succeeded.authority_revision,
                        targets=list(succeeded.targets),
                        payload_digest=succeeded.payload_digest,
                        payload=dict(succeeded.payload)
                        | {"build_intent": intent.model_dump(mode="json")},
                        result=receipt,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(replay)
                    session.flush()
                    return self._view(replay)
            # Reusing a verified receipt above needs no new execution capacity.
            # A new attempt must wait for the cancelled executor's cleanup.
            cancelling = session.scalar(
                select(Job).where(
                    Job.kind == "recipe.build.v1",
                    Job.payload["owner_id"].as_string() == plan.build_id,
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                    _JsonFlagIsTrue(Job.result, "cancel_requested").is_(True),
                )
            )
            if cancelling is not None:
                build_cancellation(cancelling)
                raise RecipeRequestInvalid(
                    "recipe build cancellation is awaiting cleanup",
                    reason=InvalidRequestReason.NOT_READY,
                )
            if (
                build is None
                or build.build_input_sha256 != plan.build_input_sha256
                or build.builder_node_id != plan.builder_node_id
            ):
                raise RecipeRequestInvalid(
                    "recipe build preview is stale",
                    reason=InvalidRequestReason.SUPERSEDED,
                )
            if force and build.state == "succeeded":
                active = session.scalar(
                    select(Job)
                    .where(
                        Job.kind == "recipe.build.v1",
                        Job.state.in_(("queued", "running")),
                        Job.payload["owner_id"].as_string() == build.id,
                        Job.payload["plan_digest"].as_string()
                        == build.build_input_sha256,
                    )
                    .order_by(Job.updated_at.desc())
                    .limit(1)
                )
                if active is not None:
                    if (
                        intent.kind == "independent"
                        and read_build_intent(active).kind == "dependency"
                    ):
                        # A distinct independent request cannot acquire intent
                        # by silently borrowing a parent's pending execution.
                        raise RecipeBuildAdmissionBusy()
                    return self._view(active)
                self._release(session, "recipe-build", build.id, now)
                job = self._start_build_in_session(
                    session,
                    build,
                    plan,
                    actor=actor,
                    request_id=request_id,
                    now=now,
                    force=True,
                    intent=intent,
                )
            elif build.state == "failed":
                previous = session.scalar(
                    select(Job)
                    .where(
                        Job.kind == "recipe.build.v1",
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.FAILED,
                                LifecycleState.NEEDS_OPERATOR,
                                LifecycleState.CANCELLED,
                            )
                        ),
                        Job.payload["owner_id"].as_string() == build.id,
                        Job.payload["plan_digest"].as_string()
                        == build.build_input_sha256,
                    )
                    .order_by(Job.updated_at.desc())
                    .limit(1)
                )
                if previous is None:
                    # A failed build with no failed receipt: the evidence is
                    # rebuilt by starting the build again.
                    retire_as_unknown(
                        "recipe.build-receipt",
                        build.id,
                        BookkeepingReason.ROW_INCOMPLETE,
                        "failed build has no failed receipt",
                    )
                    job = self._start_build_in_session(
                        session,
                        build,
                        plan,
                        actor=actor,
                        request_id=request_id,
                        now=now,
                        intent=intent,
                    )
                elif previous.state == "cancelled":
                    cancellation = build_cancellation(previous)
                    if cancellation is None or cancellation.cancelled is not True:
                        raise RecipeRequestInvalid(
                            "recipe build cancellation is awaiting cleanup",
                            reason=InvalidRequestReason.NOT_READY,
                        )
                    job = self._start_build_in_session(
                        session,
                        build,
                        plan,
                        actor=actor,
                        request_id=request_id,
                        now=now,
                        intent=intent,
                    )
                else:
                    job = self._retry_build_in_session(
                        session,
                        previous,
                        actor=actor,
                        request_id=request_id,
                        now=now,
                        intent=intent,
                    )
            elif build.state == "planned":
                job = self._start_build_in_session(
                    session,
                    build,
                    plan,
                    actor=actor,
                    request_id=request_id,
                    now=now,
                    intent=intent,
                )
            else:
                raise RecipeRequestInvalid(
                    "recipe build preview is stale",
                    reason=InvalidRequestReason.SUPERSEDED,
                )
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def build(
        self,
        plan: RecipeBuildPlan,
        *,
        build_input_sha256: str,
        actor: str,
        request_id: str,
        force: bool = False,
        admission_guard: Callable[[Session], None] | None = None,
    ) -> RecipeOperationView:
        refused: RecipeBuildAdmissionBusy | None = None
        for _attempt in admission_attempts():
            try:
                return self._build_once(
                    plan,
                    build_input_sha256=build_input_sha256,
                    actor=actor,
                    request_id=request_id,
                    force=force,
                    admission_guard=admission_guard,
                )
            except RecipeBuildAdmissionBusy as error:
                refused = error
        assert refused is not None
        raise refused

    def _start_build_in_session(
        self,
        session: Session,
        build: RecipeBuild,
        plan: RecipeBuildPlan,
        *,
        actor: str,
        request_id: str,
        now: datetime,
        intent: RecipeBuildIntent,
        force: bool = False,
    ) -> Job:
        if self._builds is None:
            raise RecipeRetryLater("recipe build service is unavailable")
        prebuilt = policy_prebuilt_reference(build.policy_report)
        if prebuilt is not None:
            return self._queue_prebuilt_build_in_session(
                session,
                build,
                plan,
                prebuilt,
                actor=actor,
                request_id=request_id,
                now=now,
                intent=intent,
                force=force,
            )
        self._builds.reserve_in_session(session, plan, now=now, request_id=request_id)
        build.state = "building"
        build.error = None
        build.updated_at = now
        return self._queue_in_session(
            session,
            kind="recipe.build.v1",
            owner_kind="recipe-build",
            owner_id=build.id,
            plan_digest=plan.build_input_sha256,
            actor=actor,
            request_id=request_id,
            node_payloads=((plan.builder_node_id, plan.agent_payload),),
            authority_digest=plan.build_input_sha256,
            now=now,
            job_context={
                "build_intent": intent.model_dump(mode="json"),
                **({"force_rebuild": True} if force else {}),
            },
        )

    def _queue_prebuilt_build_in_session(
        self,
        session: Session,
        build: RecipeBuild,
        plan: RecipeBuildPlan,
        reference: str,
        *,
        actor: str,
        request_id: str,
        now: datetime,
        intent: RecipeBuildIntent,
        force: bool,
    ) -> Job:
        """Queue a build the Controller executes by pulling a prebuilt image.

        It is an ordinary ``recipe.build.v1`` job that no Spark claims: no
        Spark operation or reservation is created. The prebuilt
        importer pulls the pinned digest and records the same evidence a Spark
        upload records, under the nominal builder named in the plan.
        """
        try:
            acquire_admission_keys(session, (job_request_key(request_id),))
        except AdmissionLockBusy as error:
            raise RecipeBuildAdmissionBusy() from error
        existing = self._idempotent_job_in_session(
            session,
            request_id,
            "recipe.build.v1",
            plan.build_input_sha256,
            owner_kind="recipe-build",
            owner_id=build.id,
        )
        if existing is not None:
            return existing
        build.state = "building"
        build.error = None
        build.updated_at = now
        payload: dict[str, object] = {
            "schema_version": 1,
            "owner_kind": "recipe-build",
            "owner_id": build.id,
            "plan_digest": plan.build_input_sha256,
            "build_intent": intent.model_dump(mode="json"),
            "prebuilt_image": reference,
            "prebuilt_node_id": build.builder_node_id,
            **({"force_rebuild": True} if force else {}),
        }
        job = new_recipe_job(
            id=str(uuid.uuid4()),
            request_id=request_id,
            kind="recipe.build.v1",
            state="running",
            actor=actor,
            authority_revision=plan.build_input_sha256,
            # The nominal builder, like any build child; no Spark operation
            # is created, so no agent ever claims this job.
            targets=[build.builder_node_id],
            payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
            payload=payload,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        return job

    @staticmethod
    def _successful_build_job_in_session(
        session: Session, build_id: str, build_input_sha256: str
    ) -> Job | None:
        job = session.scalar(
            select(Job)
            .where(
                Job.kind == "recipe.build.v1",
                Job.state == "succeeded",
                Job.payload["owner_id"].as_string() == build_id,
                Job.payload["plan_digest"].as_string() == build_input_sha256,
            )
            .order_by(Job.updated_at.desc())
            .limit(1)
        )
        return job if job is not None and isinstance(job.result, Mapping) else None

    def preview_install(
        self,
        mapping_id: str,
        recipe_build_id: str | None,
        *,
        profile_application_id: str | None = None,
    ) -> InstallPlan:
        return self._install_admission.plan_install(
            mapping_id,
            recipe_build_id,
            now=self._clock(),
            profile_application_id=profile_application_id,
        )

    def prepare_installation(
        self,
        plan: InstallPlan,
        *,
        actor: str,
        profile_application_id: str | None = None,
        workload_intent_ordinal: int | None = None,
    ) -> str:
        """Persist an admitted installation without starting Spark work.

        Run/Switch has to compile and persist the exact launch document before
        it copies model/image bytes to a target.  The regular ``install``
        method intentionally queues the agent child immediately, so this
        small lifecycle primitive stops at the durable Controller boundary.
        Reusing a matching installation makes the phase safe to replay after a
        process crash between the database commit and high-level progress
        checkpoint.
        """

        if not plan.allowed and {
            reason.code for node in plan.nodes for reason in node.blockers
        } <= {InstallAdmissionCode.INSUFFICIENT_DISK}:
            # The exact plan an earlier attempt already persisted holds its own
            # disk claim; counting that claim against itself must not stop
            # this attempt adopting it.
            with self._sessions() as session:
                adopted = self._prepared_installation_id(
                    session, plan, planned_only=True
                )
            if adopted is not None:
                return adopted
        if not plan.allowed:
            self._request_install_storage(plan)
            try:
                require_install_admissible(plan)
            except InstallAdmissionBusy:
                raise
            except InstallPlanConflict as error:
                reasons = list(
                    dict.fromkeys(
                        _bounded_blocker_reason(reason.code, reason.detail)
                        for node in plan.nodes
                        for reason in node.blockers
                    )
                )
                raise RecipeRequestInvalid(
                    "install plan is blocked: " + "; ".join(reasons[:3])
                ) from error
        now = self._clock()
        with self._sessions() as session:
            existing_id = self._prepared_installation_id(session, plan)
        if existing_id is not None:
            return existing_id
        try:
            self._install_admission.refresh_install_receipts(
                plan, now=now, profile_application_id=profile_application_id
            )
        except InstallAdmissionBusy:
            raise
        except (RuntimeError, ValueError) as error:
            raise RecipeRequestInvalid(str(error)) from error
        with self._sessions.begin() as session:
            existing_id = self._prepared_installation_id(session, plan)
            if existing_id is not None:
                return existing_id
            try:
                installation_id = self._install_admission.accept_install_in_session(
                    session,
                    plan,
                    actor=actor,
                    now=now,
                    profile_application_id=profile_application_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                )
            except InstallAdmissionBusy:
                raise
            except (RuntimeError, ValueError) as error:
                raise RecipeRequestInvalid(str(error)) from error
            installation = session.get(RecipeInstallation, installation_id)
            assert installation is not None
            # The row was written by this very transaction: its plan must carry
            # the compiled documents before anything can commit it.
            try:
                stored_plan = parse_stored_installation_plan(installation.plan)
            except RecipeExecutionContractError as error:
                raise RecipeRequestInvalid(
                    "compiled execution plan was not persisted"
                ) from error
            if not stored_plan.compiled_execution_plans:
                raise RecipeRequestInvalid("compiled execution plan was not persisted")
            return installation_id

    @staticmethod
    def _prepared_installation_id(
        session: Session, plan: InstallPlan, *, planned_only: bool = False
    ) -> str | None:
        """The newest installation already prepared for this exact plan.

        An installation whose stored plan cannot be read is no preparation: it is
        retired as unknown and skipped, so the next acceptance prepares a fresh,
        readable one (the damaged row is left to space-driven cleanup).
        """

        candidates = session.scalars(
            select(RecipeInstallation)
            .where(
                RecipeInstallation.mapping_id == plan.mapping_id,
                RecipeInstallation.mapping_generation == plan.mapping_generation,
                RecipeInstallation.recipe_build_id == plan.recipe_build_id,
                RecipeInstallation.plan_digest == plan.plan_digest,
                RecipeInstallation.state.in_(
                    (InstallationState.PLANNED,)
                    if planned_only
                    else (
                        InstallationState.PLANNED,
                        InstallationState.INSTALLING,
                        InstallationState.PARTIAL,
                        InstallationState.INSTALLED,
                    )
                ),
            )
            .order_by(RecipeInstallation.created_at.desc())
        )
        for existing in candidates:

            def read(candidate: RecipeInstallation = existing) -> bool | Damaged:
                stored = parse_stored_installation_plan(candidate.plan)
                if not stored.compiled_execution_plans:
                    return Damaged("stored installation has no compiled plan")
                return True

            if (
                read_or_rebuild(
                    kind="recipe.installation-plan", subject=existing.id, read=read
                )
                is True
            ):
                return existing.id
        return None

    def _stored_compiled_plans(
        self,
        session: Session,
        installation: RecipeInstallation,
        node_ids: Collection[str],
        *,
        now: datetime,
    ) -> dict[str, WireCompiledExecutionPlan] | Residue:
        """The compiled launch documents an installation was accepted with.

        A stored plan that does not parse (or does not cover the ranks) is
        re-planned from its mapping and build; the result is evidence only when
        it carries the installation's own plan digest.  Otherwise the damage is
        retired as unknown and the caller refuses the request, which the next
        prepare answers with a fresh installation.
        """

        wanted = set(node_ids)

        def read() -> dict[str, WireCompiledExecutionPlan] | Damaged:
            plans = parse_stored_installation_plan(
                installation.plan
            ).compiled_execution_plans
            if not plans or not wanted <= set(plans):
                return Damaged("stored installation plan lacks compiled documents")
            return dict(plans)

        def rebuild() -> dict[str, WireCompiledExecutionPlan] | None:
            try:
                fresh = self._install_admission.plan_install(
                    installation.mapping_id,
                    installation.recipe_build_id,
                    now=now,
                    _session=session,
                )
            except (RuntimeError, ValueError, KeyError):
                return None
            plans = fresh.compiled_plan_by_node
            if fresh.plan_digest != installation.plan_digest or not wanted <= set(
                plans
            ):
                return None
            return plans

        return read_or_rebuild(
            kind="recipe.installation-plan",
            subject=installation.id,
            read=read,
            rebuild=rebuild,
        )

    def start_installation(
        self,
        installation_id: str,
        *,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        """Queue the already prepared installation after target verification."""

        now = self._clock()
        with self._sessions.begin() as session:
            installation = session.get(
                RecipeInstallation, installation_id, with_for_update=True
            )
            if installation is None:
                raise RecipeRequestInvalid("recipe installation is unavailable")
            existing = self._idempotent_in_session(
                session,
                request_id,
                "recipe.install",
                installation.plan_digest,
                owner_kind="installation",
                owner_id=installation_id,
            )
            if existing is not None:
                return existing
            reconciliation = session.scalar(
                select(Job.id)
                .where(
                    Job.kind == "recipe.reconcile",
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == installation_id,
                )
                .limit(1)
            )
            if reconciliation is not None:
                raise RecipeRetryLater("recipe installation is being reconciled")
            receipt_missing = False
            if installation.state == InstallationState.INSTALLED:
                completed = session.scalar(
                    select(Job)
                    .where(
                        Job.kind == "recipe.install",
                        Job.state == "succeeded",
                        Job.payload["owner_id"].as_string() == installation_id,
                        Job.payload["plan_digest"].as_string()
                        == installation.plan_digest,
                    )
                    .order_by(Job.updated_at.desc())
                    .limit(1)
                )
                if completed is not None:
                    return self._view(completed)
                # Installed, but its receipt is gone: the effect is unknown, so
                # the install is run again (it verifies bytes already present)
                # instead of refusing the request.
                retire_as_unknown(
                    "recipe.install-receipt",
                    installation_id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "installed installation has no successful install receipt",
                )
                receipt_missing = True
            active = session.scalar(
                select(Job)
                .where(
                    Job.kind == "recipe.install",
                    Job.state.in_(("queued", "running")),
                    Job.payload["owner_id"].as_string() == installation_id,
                    Job.payload["plan_digest"].as_string() == installation.plan_digest,
                )
                .order_by(Job.updated_at.desc(), Job.id)
                .limit(1)
            )
            if active is not None:
                if (
                    workload_intent_ordinal is not None
                    and _parent_intent(active) != workload_intent_ordinal
                ):
                    raise RecipeRetryLater(
                        "prior installation intent requires exact observation before replacement"
                    )
                return self._view(active)
            if not receipt_missing and installation.state not in {
                InstallationState.PLANNED,
                InstallationState.PARTIAL,
                InstallationState.FAILED,
                InstallationState.INSTALLING,
            }:
                raise RecipeRequestInvalid("recipe installation is not launchable")
            nodes = tuple(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id == installation_id)
                    .order_by(InstallationNode.rank, InstallationNode.node_id)
                )
            )
            raw_plans = self._stored_compiled_plans(
                session, installation, [node.node_id for node in nodes], now=now
            )
            if isinstance(raw_plans, Residue) or not nodes:
                raise RecipeRetryLater(
                    "stored installation plan is unreadable; it is recorded and the "
                    "next preparation installs afresh"
                )
            revision = _active_recipe_revision(session, installation.recipe_revision_id)
            if revision is None or revision.content_digest is None:
                raise RecipeRequestInvalid("recipe revision is unavailable")
            installation.state = InstallationState.INSTALLING
            installation.updated_at = now
            for node in nodes:
                node.state = InstallationNodeState.PLANNED
                node.updated_at = now
            job = self._queue_in_session(
                session,
                kind="recipe.install",
                owner_kind="installation",
                owner_id=installation_id,
                plan_digest=installation.plan_digest,
                actor=actor,
                request_id=request_id,
                node_payloads=tuple(
                    (
                        node.node_id,
                        {
                            "installation_id": installation_id,
                            "plan_digest": installation.plan_digest,
                            "expected_bytes": node.required_bytes,
                            "compiled_execution_plan": raw_plans[
                                node.node_id
                            ].model_dump(mode="json"),
                        },
                    )
                    for node in nodes
                ),
                authority_digest=revision.content_digest,
                now=now,
                workload_intent_ordinal=workload_intent_ordinal,
            )
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def preview_run(
        self,
        installation_id: str,
        alias: str,
        *,
        released_run_ids: Collection[str] = (),
        profile_application_id: str | None = None,
        excluded_profile_application_ids: Sequence[str] = (),
    ) -> RunPlan:
        return self._run_admission.plan_run(
            installation_id,
            alias,
            now=self._clock(),
            released_run_ids=released_run_ids,
            profile_application_id=profile_application_id,
            excluded_profile_application_ids=excluded_profile_application_ids,
        )

    def _adopt_start_in_session(
        self,
        session: Session,
        installation_id: str,
        alias: str,
        *,
        request_id: str,
        plan_digest: str | None,
    ) -> RecipeOperationView | None:
        existing = session.scalar(select(Job).where(Job.request_id == request_id))
        if (
            existing is None
            or existing.kind != "recipe.start"
            or _parent_identity(existing, "owner_kind") != "run"
        ):
            return None
        recorded_digest = _parent_identity(existing, "plan_digest")
        if not isinstance(recorded_digest, str):
            return None
        owner_id = _parent_identity(existing, "owner_id")
        if not isinstance(owner_id, str):
            return None
        run = session.get(RecipeRun, owner_id)
        if (
            run is None
            or run.installation_id != installation_id
            or run.alias != alias
            # The run carries the admitted authority this child was queued
            # under, so adopting a child whose run no longer matches is unsafe
            # even when the caller cannot reproduce the original digest.
            or run.plan_digest != recorded_digest
        ):
            return None
        return self._view(existing)

    def replay_start(
        self,
        installation_id: str,
        alias: str,
        *,
        plan_digest: str,
        request_id: str,
    ) -> RecipeOperationView | None:
        """Adopt the original start child when its exact plan is still known."""

        with self._sessions() as session:
            return self._adopt_start_in_session(
                session,
                installation_id,
                alias,
                request_id=request_id,
                plan_digest=plan_digest,
            )

    def adopt_start(
        self,
        installation_id: str,
        alias: str,
        *,
        request_id: str,
    ) -> RecipeOperationView | None:
        """Adopt an already admitted start child before re-reading admission.

        Automatic recovery must bind the child that was actually queued before
        it re-derives mutable admission.  ``preview_run`` hashes node documents
        containing inventory observation time and current memory/reservation
        facts, so a refreshed inventory or the first start's own reservations
        legitimately change the digest.  Reproducing that digest is therefore
        not a requirement for recognising our own durable child: the request
        key, operation kind, owner kind and the run it owns are the identity
        that must match, and the run carries the admitted authority.
        """

        with self._sessions() as session:
            return self._adopt_start_in_session(
                session,
                installation_id,
                alias,
                request_id=request_id,
                plan_digest=None,
            )

    def adopt_owned_operation(
        self,
        request_id: str,
        *,
        kind: str,
        owner_kind: str,
        owner_id: str,
    ) -> RecipeOperationView | None:
        """Adopt the exact child already recorded for this request key.

        Scoped resume of a parent step needs to recognise a child it queued
        earlier without re-deriving a plan digest.  Admission deliberately
        keeps comparing digests for new work; this lookup is only the
        recovery-side identity check, so it requires the same request key,
        operation kind and owner as the durable record.
        """

        with self._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if (
                existing is None
                or existing.kind != kind
                or _parent_identity(existing, "owner_kind") != owner_kind
                or _parent_identity(existing, "owner_id") != owner_id
            ):
                return None
            return self._view(existing)

    def run_status(self, run_id: str) -> RecipeRunStatus:
        now = _aware(self._clock())
        with self._sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise MissingRecord(run_id)
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            installation = session.get(RecipeInstallation, run.installation_id)
            accepted = _run_accepted_ranks(
                session,
                run,
                installation.recipe_revision_id if installation is not None else "",
            )
            # A plan nobody can read proves no membership: the run is shown not
            # healthy until its ranks are re-established, never refused.
            exact_ranks = (
                not isinstance(accepted, Residue)
                and len(accepted[0]) == len(nodes)
                and accepted[0]
                == {(node.node_id, node.rank, node.role) for node in nodes}
            )
            recovery_jobs = tuple(
                job
                for job in session.scalars(
                    select(Job)
                    .where(
                        Job.kind.in_({"recipe.start", "recipe.stop"}),
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.NEEDS_OPERATOR,
                            )
                        ),
                        Job.payload["owner_kind"].as_string() == "run",
                        Job.payload["owner_id"].as_string() == run.id,
                    )
                    .order_by(Job.created_at, Job.id)
                )
                if _parent_recovery(job) is not None
            )
            recovery_owners: list[RecipeRunRecoveryOwner] = []
            for job in recovery_jobs:
                if job.kind == "recipe.start":
                    kind: Literal["recipe.start", "recipe.stop"] = "recipe.start"
                elif job.kind == "recipe.stop":
                    kind = "recipe.stop"
                else:
                    continue
                if job.state == "queued":
                    state = LifecycleState.QUEUED.value
                elif job.state == "running":
                    state = LifecycleState.RUNNING.value
                elif job.state in job_states.words(LifecycleState.NEEDS_OPERATOR):
                    state = LifecycleState.NEEDS_OPERATOR.value
                else:
                    continue
                recovery_owners.append(
                    RecipeRunRecoveryOwner(
                        operation_id=job.id,
                        kind=kind,
                        state=state,
                    )
                )
            ranks: list[RecipeRunRankStatus] = []
            for node in nodes:
                observed_at = _aware(node.updated_at)
                age = now - observed_at
                fresh = timedelta(0) <= age < self._run_health_maximum_age
                ranks.append(
                    RecipeRunRankStatus(
                        node_id=node.node_id,
                        rank=node.rank,
                        role=node.role,
                        state=node.state,
                        observed_at=observed_at,
                        age_seconds=max(0.0, age.total_seconds()),
                        fresh=fresh,
                    )
                )
            return RecipeRunStatus(
                id=run.id,
                alias=run.alias,
                state=run.state,
                route_state=run.route_state,
                healthy=bool(exact_ranks)
                and all(rank.state == "running" and rank.fresh for rank in ranks),
                ranks=tuple(ranks),
                run_generation=run.run_generation,
                observation_deadline_at=(
                    _aware(run.observation_deadline_at)
                    if run.observation_deadline_at is not None
                    else None
                ),
                route_error=run.route_error,
                route_next_attempt_at=(
                    _aware(run.route_next_attempt_at)
                    if run.route_next_attempt_at is not None
                    else None
                ),
                route_recovery_pending=route_health_recovery_pending(run.route_error),
                recovery_owners=tuple(recovery_owners),
            )

    def _install_once(
        self,
        plan: InstallPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        now = self._clock()
        install_identity = (plan.mapping_id, plan.recipe_build_id)
        existing = self._idempotent(
            request_id, "recipe.install", None, install_identity=install_identity
        )
        if existing is not None:
            return existing
        plan = self._install_admission.plan_install(
            plan.mapping_id,
            plan.recipe_build_id,
            now=now,
            compiled_execution_plans=plan.compiled_plan_by_node,
        )
        if not plan.allowed:
            self._request_install_storage(plan)
        require_install_admissible(plan)
        try:
            self._install_admission.refresh_install_receipts(plan, now=now)
        except InstallAdmissionBusy:
            raise
        except (RuntimeError, ValueError) as error:
            raise RecipeRequestInvalid(str(error)) from error
        with self._sessions.begin() as session:
            try:
                acquire_admission_keys(
                    session,
                    (
                        job_request_key(request_id),
                        *(node_admission_key(node.node_id) for node in plan.nodes),
                    ),
                    holder="recipe-operation",
                )
            except AdmissionLockBusy as error:
                raise InstallAdmissionBusy(
                    InstallAdmissionCode.CAPACITY_BUSY,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            replay = self._idempotent_in_session(
                session,
                request_id,
                "recipe.install",
                None,
                install_identity=install_identity,
            )
            if replay is not None:
                return replay
            try:
                installation_id = self._install_admission.accept_install_in_session(
                    session, plan, actor=actor, now=now
                )
            except InstallAdmissionBusy:
                raise
            except (RuntimeError, ValueError) as error:
                raise RecipeRequestInvalid(str(error)) from error
            installation = session.get(RecipeInstallation, installation_id)
            assert installation is not None
            installation.state = InstallationState.INSTALLING
            installation.updated_at = now
            compiled_plans = plan.compiled_plan_by_node
            if set(compiled_plans) != {node.node_id for node in plan.nodes}:
                raise RecipeRequestInvalid(
                    "compiled execution plan is missing for one or more mapped nodes"
                )
            job = self._queue_in_session(
                session,
                kind="recipe.install",
                owner_kind="installation",
                owner_id=installation_id,
                plan_digest=plan.plan_digest,
                actor=actor,
                request_id=request_id,
                node_payloads=tuple(
                    (
                        node.node_id,
                        {
                            "installation_id": installation_id,
                            "plan_digest": plan.plan_digest,
                            "expected_bytes": node.required_bytes,
                            "compiled_execution_plan": compiled_plans[
                                node.node_id
                            ].model_dump(mode="json"),
                        },
                    )
                    for node in plan.nodes
                ),
                authority_digest=plan.recipe_content_sha256,
                now=now,
                workload_intent_ordinal=workload_intent_ordinal,
            )
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def install(
        self,
        plan: InstallPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        refused: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return self._install_once(
                    plan,
                    plan_digest=plan_digest,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                )
            except UnknownOutcomeError as error:
                refused = error
        assert refused is not None
        raise refused

    def _start_once(
        self,
        plan: RunPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
        profile_application_id: str | None = None,
    ) -> RecipeOperationView:
        existing = self._idempotent(
            request_id,
            "recipe.start",
            None,
            installation_id=plan.installation_id,
        )
        if existing is not None:
            return existing
        now = self._clock()
        with self._sessions.begin() as session:
            plan = self._run_admission.plan_run(
                plan.installation_id,
                plan.alias,
                now=now,
                _session=session,
                profile_application_id=profile_application_id,
            )
            require_run_admissible(plan)
            try:
                acquire_admission_keys(
                    session,
                    (
                        job_request_key(request_id),
                        *(node_admission_key(node.node_id) for node in plan.nodes),
                    ),
                    holder="recipe-operation",
                )
            except AdmissionLockBusy as error:
                raise RunAdmissionBusy("run capacity writer is busy") from error
            replay = self._idempotent_in_session(
                session,
                request_id,
                "recipe.start",
                None,
                installation_id=plan.installation_id,
            )
            if replay is not None:
                return replay
            presences = {
                node_id: address
                for node_id, address in session.execute(
                    select(
                        AgentPresence.node_id, AgentPresence.management_address
                    ).where(
                        AgentPresence.node_id.in_([node.node_id for node in plan.nodes])
                    )
                )
            }
            if set(presences) != {node.node_id for node in plan.nodes}:
                # A Spark reports its address when it next contacts the Controller.
                raise RecipeRetryLater("recipe node endpoint evidence is unavailable")
            master = next((node for node in plan.nodes if node.endpoint_owner), None)
            if master is None:
                raise RecipeRequestInvalid("recipe run has no endpoint owner")
            world_size = len(plan.nodes)
            master_address = master.fabric_address if world_size > 1 else None
            master_port = master.rendezvous_port if world_size > 1 else None
            if world_size > 1 and (master_address is None or master_port is None):
                raise RecipeRequestInvalid(
                    "recipe direct-fabric rendezvous is unavailable"
                )
            active_uninstall = session.scalar(
                select(Job.id)
                .where(
                    Job.kind.in_(("recipe.uninstall", "recipe.reconcile")),
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == plan.installation_id,
                )
                .limit(1)
            )
            if active_uninstall is not None:
                raise RecipeRetryLater("recipe installation is not runnable")
            try:
                run_id = self._run_admission.accept_run_in_session(
                    session,
                    plan,
                    actor=actor,
                    now=now,
                    profile_application_id=profile_application_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                )
            except RunAdmissionBusy:
                raise
            except (RuntimeError, ValueError) as error:
                raise RecipeRequestInvalid(str(error)) from error
            run = session.get(RecipeRun, run_id)
            revision = _active_recipe_revision(session, plan.recipe_revision_id)
            installation = session.get(RecipeInstallation, plan.installation_id)
            assert run is not None and revision is not None and installation is not None
            loaded_plans = self._stored_compiled_plans(
                session, installation, [node.node_id for node in plan.nodes], now=now
            )
            if isinstance(loaded_plans, Residue):
                raise RecipeRetryLater(
                    "compiled execution plan is unavailable for the installed recipe; "
                    "it is recorded and the next preparation installs afresh"
                )
            compiled_plans = loaded_plans
            start_order = _topology_order(revision.document, "start_order")
            if start_order is None:
                raise RecipeRequestInvalid("recipe topology is invalid")
            topology = recipe_topology(revision.document)
            distributed_readiness = _canonical_distributed_readiness(revision.document)
            two_phase_start = (
                world_size > 1 and topology.distributed and distributed_readiness
            )
            start_deadline = (
                # Persist the accepted loading/JIT budget. Exact rank-loss recovery
                # derives this same duration; neither lease renewal nor retry
                # extends a deadline.
                (
                    _aware(now)
                    + timedelta(seconds=self._distributed_start_timeout_seconds)
                ).isoformat()
                if two_phase_start
                else None
            )
            run.state = RunState.STARTING
            run.updated_at = now
            recipe_digest = revision.content_digest
            assert recipe_digest is not None

            def start_payload(node: RunNodePlan) -> tuple[str, Mapping[str, object]]:
                endpoint_owner = node.endpoint_owner
                node_id = node.node_id
                try:
                    endpoint_address = (
                        presences[node_id] if endpoint_owner else node.fabric_address
                    )
                    if not isinstance(endpoint_address, str):
                        raise MissingRecord(
                            "recipe start endpoint address is unavailable"
                        )
                    payload = build_recipe_start_payload(
                        run_id=run_id,
                        installation_id=plan.installation_id,
                        recipe_revision_id=plan.recipe_revision_id,
                        mapping_id=run.mapping_id,
                        run_generation=run.run_generation,
                        plan_digest=plan.plan_digest,
                        placement=RecipeStartPlacement(
                            node_id,
                            node.rank,
                            node.role,
                            node.port,
                            node.required_memory_bytes,
                            node.memory_floor_bytes,
                            node.memory_kind,
                            node.fabric_address,
                        ),
                        compiled_endpoint_address=(
                            presences[node_id] if endpoint_owner else None
                        ),
                        world_size=world_size,
                        compiled_execution_plan=compiled_plans[node_id],
                        master_address=master_address,
                        master_port=master_port,
                        phase="rank-launch" if start_deadline is not None else None,
                        start_deadline=start_deadline,
                    )
                except (KeyError, RecipeStartPayloadError) as error:
                    raise RecipeRequestInvalid(
                        "recipe start payload is invalid"
                    ) from error
                return node_id, payload

            start_payloads = tuple(start_payload(node) for node in plan.nodes)
            role_phases = _role_phases(start_order, start_payloads)
            if role_phases is None:
                # Starting blind (without the recipe's role order) could launch
                # a rank before what it depends on.
                raise RecipeRequestInvalid("operation topology order is invalid")
            phases = role_phases
            if start_deadline is not None:
                owner_payload = next(
                    payload
                    for node_id, payload in start_payloads
                    if node_id == master.node_id
                )
                phases = (
                    tuple(item for phase in role_phases for item in phase),
                    (
                        (
                            master.node_id,
                            {
                                **owner_payload,
                                "phase": "collective-readiness",
                            },
                        ),
                    ),
                )
            job = self._queue_in_session(
                session,
                kind="recipe.start",
                owner_kind="run",
                owner_id=run_id,
                plan_digest=plan.plan_digest,
                actor=actor,
                request_id=request_id,
                node_payloads=start_payloads,
                phases=phases,
                authority_digest=recipe_digest,
                now=now,
                workload_intent_ordinal=workload_intent_ordinal,
                job_context=(
                    {"start_deadline": start_deadline}
                    if start_deadline is not None
                    else None
                ),
            )
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def start(
        self,
        plan: RunPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
        profile_application_id: str | None = None,
    ) -> RecipeOperationView:
        refused: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return self._start_once(
                    plan,
                    plan_digest=plan_digest,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    profile_application_id=profile_application_id,
                )
            except UnknownOutcomeError as error:
                refused = error
        assert refused is not None
        raise refused

    def enqueue_one_shot_job_in_session(
        self,
        session: Session,
        *,
        artifact_job_id: str,
        run_id: str,
        node_id: str,
        payload: Mapping[str, object],
        actor: str,
        request_id: str,
        authority_digest: str,
        now: datetime,
    ) -> Job:
        """Enqueue one fenced job without changing the service run lifecycle."""
        run = session.get(RecipeRun, run_id, with_for_update=True)
        if run is None or run.state != RunState.RUNNING:
            raise RecipeRequestInvalid("recipe run is not accepting jobs")
        node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == run_id,
                RunNode.node_id == node_id,
                RunNode.state == RunState.RUNNING,
            )
        )
        if node is None:
            raise RecipeRequestInvalid("recipe job target is not running")
        return self._queue_in_session(
            session,
            kind="recipe.job.run.v1",
            owner_kind="artifact-job",
            owner_id=artifact_job_id,
            plan_digest=run.plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=((node_id, payload),),
            authority_digest=authority_digest,
            now=now,
            workload_intent_ordinal=_run_start_intent(session, run_id),
        )

    def notify_agents(self) -> None:
        self._agent_jobs.notify_available()

    def _activate_job_run_once(
        self,
        plan: RunPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        existing = self._idempotent(
            request_id,
            "recipe.job.activate.v1",
            None,
            installation_id=plan.installation_id,
        )
        if existing is not None:
            return existing
        now = self._clock()
        with self._sessions.begin() as session:
            try:
                acquire_admission_keys(session, (job_request_key(request_id),))
            except AdmissionLockBusy as error:
                raise RunAdmissionBusy("run capacity writer is busy") from error
            replay = self._idempotent_in_session(
                session,
                request_id,
                "recipe.job.activate.v1",
                None,
                installation_id=plan.installation_id,
            )
            if replay is not None:
                return replay
            plan = self._run_admission.plan_run(
                plan.installation_id, plan.alias, now=now, _session=session
            )
            require_run_admissible(plan)
            installation = session.get(RecipeInstallation, plan.installation_id)
            revision = (
                _active_recipe_revision(session, installation.recipe_revision_id)
                if installation is not None
                else None
            )
            recipe = None
            if revision is not None:
                try:
                    recipe = read_recipe(revision.document)
                except (TypeError, ValueError):
                    pass
            adapters = (
                [interface.adapter for interface in recipe.interfaces]
                if recipe is not None
                else []
            )
            artifact_adapters = {
                "audio-job",
                "video-job",
                "image-job",
                "mesh-job",
                "artifact-job",
            }
            if (
                recipe is None
                or len(adapters) != 1
                or adapters[0] not in artifact_adapters
            ):
                raise RecipeRequestInvalid("recipe is not an artifact job recipe")
            if recipe.topology.node_count != 1:
                raise RecipeRequestInvalid(
                    "artifact job recipes currently require a single-node topology"
                )
            try:
                run_id = self._run_admission.accept_run_in_session(
                    session, plan, actor=actor, now=now
                )
            except (RuntimeError, ValueError) as error:
                raise RecipeRequestInvalid(str(error)) from error
            run = session.get(RecipeRun, run_id)
            assert run is not None and revision is not None
            run.state = RunState.RUNNING
            run.route_state = RouteState.WITHDRAWN
            # The run row was written by this very transaction: its plan must
            # accept the one-shot mode before anything can commit it.
            try:
                updated_plan = parse_stored_run_plan(run.plan).model_copy(
                    update={"execution_mode": "one-shot-jobs"}
                )
                run.plan = run_plan_document(updated_plan)
            except RecipeExecutionContractError as error:
                raise RecipeRequestInvalid("stored run plan is invalid") from error
            run.updated_at = now
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            for node in nodes:
                node.state = RunState.RUNNING
                node.updated_at = now
            targets = sorted(node.node_id for node in nodes)
            target_nodes = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(targets))
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                )
            )
            if tuple(node.node_id for node in target_nodes) != tuple(targets):
                raise RecipeRequestInvalid("artifact workload target disappeared")
            workload_intent_ordinal = (
                max(node.workload_intent_ordinal for node in target_nodes) + 1
            )
            for node in target_nodes:
                node.workload_intent_ordinal = workload_intent_ordinal
            AgentJobService.request_superseded_workload_cancellation_in_session(
                session, targets, workload_intent_ordinal, now
            )
            payload = {
                "schema_version": 1,
                "owner_kind": "run",
                "owner_id": run_id,
                "plan_digest": plan.plan_digest,
                "execution_mode": "one-shot-jobs",
                "workload_intent_ordinal": workload_intent_ordinal,
            }
            job = new_recipe_job(
                id=str(uuid.uuid4()),
                request_id=request_id,
                kind="recipe.job.activate.v1",
                state="succeeded",
                actor=actor,
                authority_revision=revision.content_digest or "",
                targets=targets,
                payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
                payload=payload,
                result=_validated_result("recipe.job.activate.v1", {"activated": True}),
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            return self._view(job)

    def activate_job_run(
        self,
        plan: RunPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        """Reserve an installed artifact recipe without starting a service container."""
        refused: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return self._activate_job_run_once(
                    plan,
                    plan_digest=plan_digest,
                    actor=actor,
                    request_id=request_id,
                )
            except UnknownOutcomeError as error:
                refused = error
        assert refused is not None
        raise refused

    def assess_superseded_unissued(self, kind: str, owner_id: str) -> bool:
        """Read-only: can an exact older operation be retired before a new intent?"""
        with self._sessions() as session:
            scope = _workload_owner_scope(session, kind, owner_id)
            active = _active_owned_workload_jobs(session, kind, owner_id)
            return bool(active) and all(
                tuple(sorted(job.targets)) == scope
                and _unissued_workload_children(session, job) is not None
                for job in active
            )

    def assess_superseded_issued(
        self,
        kind: str,
        owner_id: str,
        workload_intent_ordinal: int | None = None,
        *,
        profile_target_node_ids: Sequence[str] | None = None,
    ) -> IssuedWorkloadReconciliation | None:
        """Name exact issued work that needs fresh observation before resumption.

        Bookkeeping that disagrees (an owner scope that changed, an intent or plan
        the job lost, children or attempts that are missing) never refuses the
        assessment: the job's own Sparks and orders are the evidence, the
        disagreement is retired as unknown, and the job is reported for
        observation.  With several such jobs the earliest due one is named; the
        others surface on the next assessment.
        """
        if workload_intent_ordinal is not None and (
            type(workload_intent_ordinal) is not int or workload_intent_ordinal < 1
        ):
            raise RecipeRequestInvalid("workload intent ordinal is invalid")
        now = _aware(self._clock())
        with self._sessions() as session:
            scope = _workload_owner_scope(session, kind, owner_id)
            intent_scope = _profile_effect_scope(scope, profile_target_node_ids)
            if workload_intent_ordinal is not None and not _intent_is_current(
                session, workload_intent_ordinal, intent_scope
            ):
                raise RecipeRequestInvalid("workload intent was superseded")
            pending: list[IssuedWorkloadReconciliation] = []
            for job in _active_owned_workload_jobs(
                session, kind, owner_id, include_waiting_cancellation=True
            ):
                if tuple(sorted(job.targets)) != scope:
                    retire_as_unknown(
                        "recipe.workload-owner-scope",
                        job.id,
                        BookkeepingReason.EVIDENCE_MISMATCH,
                        "job targets differ from the owner's Sparks",
                    )
                ordinal = _job_workload_intent(session, job) or 0
                if (
                    workload_intent_ordinal is not None
                    and ordinal >= workload_intent_ordinal
                ):
                    continue
                if _unissued_workload_children(session, job) is not None:
                    continue
                children = tuple(
                    session.scalars(
                        select(AgentOperation)
                        .where(AgentOperation.parent_job_id == job.id)
                        .order_by(AgentOperation.id)
                    )
                )
                attempts = (
                    tuple(
                        session.scalars(
                            select(AgentOperationAttempt).where(
                                AgentOperationAttempt.operation_id.in_(
                                    tuple(child.id for child in children)
                                )
                            )
                        )
                    )
                    if children
                    else ()
                )
                if not children or not attempts:
                    retire_as_unknown(
                        "recipe.issued-workload",
                        job.id,
                        BookkeepingReason.ROW_INCOMPLETE,
                        "issued job has no orders or attempt evidence",
                    )
                latest_lease = max(
                    (_aware(attempt.lease_deadline) for attempt in attempts),
                    default=now,
                )
                # The helper grant is bounded to 300 seconds; stop/cleanup
                # helpers can run for up to 645 seconds after admission.
                # This is a polling budget, never permission to replay.
                observation_deadline = latest_lease + timedelta(seconds=960)
                observe_due_at = min(
                    observation_deadline,
                    max(
                        now + timedelta(seconds=2),
                        min(latest_lease, now + timedelta(seconds=30)),
                    ),
                )
                plan_digest = _parent_identity(job, "plan_digest")
                pending.append(
                    IssuedWorkloadReconciliation(
                        job_id=job.id,
                        kind=kind,
                        owner_id=owner_id,
                        plan_digest=(
                            plan_digest
                            if isinstance(plan_digest, str)
                            else job.payload_digest
                        ),
                        payload_digests=tuple(
                            child.payload_digest for child in children
                        ),
                        failure_kind=FailureKind.UNCERTAIN_EFFECT,
                        observe_due_at=observe_due_at,
                        observation_deadline=observation_deadline,
                    )
                )
            return min(
                pending,
                key=lambda item: (item.observe_due_at, item.job_id),
                default=None,
            )

    def reconcile_superseded_unissued(
        self,
        kind: str,
        owner_id: str,
        workload_intent_ordinal: int,
        *,
        profile_target_node_ids: Sequence[str] | None = None,
    ) -> bool:
        """Retire only exact older workload jobs with no issued agent attempt."""
        if type(workload_intent_ordinal) is not int or workload_intent_ordinal < 1:
            raise RecipeRequestInvalid("workload intent ordinal is invalid")
        now = self._clock()
        retired = False
        with self._sessions.begin() as session:
            scope = _workload_owner_scope(session, kind, owner_id)
            intent_scope = _profile_effect_scope(scope, profile_target_node_ids)
            nodes = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(intent_scope))
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                )
            )
            if tuple(node.node_id for node in nodes) != intent_scope or any(
                node.workload_intent_ordinal != workload_intent_ordinal
                or node.state != "active"
                or node.revoked_at is not None
                for node in nodes
            ):
                raise RecipeRequestInvalid("workload intent was superseded")
            for job in _active_owned_workload_jobs(session, kind, owner_id, lock=True):
                previous_ordinal = _parent_intent(job)
                if tuple(sorted(job.targets)) != scope:
                    # The job's Sparks and the owner's rows disagree: the job is
                    # not retired on this evidence, and stays for observation.
                    retire_as_unknown(
                        "recipe.workload-owner-scope",
                        job.id,
                        BookkeepingReason.EVIDENCE_MISMATCH,
                        "job targets differ from the owner's Sparks",
                    )
                    continue
                if (
                    type(previous_ordinal) is not int
                    or previous_ordinal < 1
                    or previous_ordinal >= workload_intent_ordinal
                ):
                    continue
                children = _unissued_workload_children(session, job, lock=True)
                if children is None:
                    continue
                RecipeOperationAdapter().cancelled(
                    job, now, reason="superseded before agent issuance", keep=False
                )
                adapter = AgentOperationAdapter(session)
                for child in children:
                    adapter.settle(
                        child,
                        None,
                        job,
                        CancelRequested(reason="superseded before agent issuance"),
                        now,
                    )
                retired = True
        return retired

    def preview_stop(
        self,
        run_id: str,
        *,
        profile_target_node_ids: Sequence[str] | None = None,
    ) -> StopPlan:
        with self._sessions() as session:
            return self._stop_plan_in_session(
                session,
                run_id,
                lock=False,
                profile_target_node_ids=profile_target_node_ids,
            )

    def stop(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
        profile_target_node_ids: Sequence[str] | None = None,
        profile_application_id: str | None = None,
    ) -> RecipeOperationView:
        existing = self._idempotent(
            request_id,
            "recipe.stop",
            None,
            owner_kind="run",
            owner_id=run_id,
        )
        if existing is not None and not (
            existing.state == "running"
            and (
                self._one_shot_stop_is_pending(request_id)
                or self._profile_jobrun_stop_is_pending(request_id)
            )
        ):
            return existing
        logical = self._stop_logical_job_run(
            run_id,
            plan_digest=plan_digest,
            actor=actor,
            request_id=request_id,
            workload_intent_ordinal=workload_intent_ordinal,
            profile_target_node_ids=profile_target_node_ids,
            profile_application_id=profile_application_id,
        )
        if isinstance(logical, RecipeArtifactJobCancellationPending):
            raise logical
        if logical is not None:
            if logical.state == "running":
                self._agent_jobs.notify_available()
            return logical
        now = self._clock()
        # Claim, effect, conditional completion. The accepted Stop is checked
        # read-only first so a stale or blocked request withdraws nothing; the
        # route is then withdrawn with no transaction open (the withdrawal
        # intent is durable with its claim, and a crash resumes from it); only
        # then is the Stop dispatched, in its own short transaction, and only
        # while the withdrawal is still complete.
        with self._sessions() as session:
            admitted = self._stop_plan_in_session(
                session,
                run_id,
                lock=False,
                profile_target_node_ids=profile_target_node_ids,
            )
            if not admitted.allowed:
                raise RecipeRequestInvalid(
                    "stop plan is stale or blocked",
                    reason=InvalidRequestReason.SUPERSEDED,
                )
            run = session.get(RecipeRun, run_id)
            assert run is not None
            if self._absent_stop_nodes(session, run, admitted, now, lock=False) is None:
                self._exact_stop_authority(session, run, admitted)
        for _attempt in range(_STOP_WITHDRAWAL_ATTEMPTS):
            if self._route_publications is not None:
                try:
                    self._route_publications.withdraw_run(run_id, pending="stop")
                except RecipeRouteNotReady as error:
                    raise RecipeRequestInvalid(
                        "the run's route withdrawal was superseded; retry the stop",
                        reason=InvalidRequestReason.SUPERSEDED,
                    ) from error
            else:
                self._route_withdrawer(run_id)
            try:
                job = self._dispatch_stop_after_withdrawal(
                    run_id,
                    plan_digest=plan_digest,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    profile_target_node_ids=profile_target_node_ids,
                )
            except _RouteNotWithdrawn:
                continue
            if job.state != "succeeded":
                self._agent_jobs.notify_available()
            return job
        raise RecipeRetryLater(
            "the run's route withdrawal has not settled; retry the stop"
        )

    def _dispatch_stop_after_withdrawal(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None,
        profile_target_node_ids: Sequence[str] | None,
    ) -> RecipeOperationView:
        """Queue the Stop in one short transaction, if the route is still gone."""

        now = self._clock()
        try:
            transaction = (
                self._route_publications.publication_transaction()
                if self._route_publications is not None
                else route_publication_transaction(self._sessions)
            )
            with transaction as session:
                existing = self._idempotent_in_session(
                    session,
                    request_id,
                    "recipe.stop",
                    None,
                    owner_kind="run",
                    owner_id=run_id,
                )
                if existing is not None:
                    return existing
                admitted = self._stop_plan_in_session(
                    session,
                    run_id,
                    lock=True,
                    profile_target_node_ids=profile_target_node_ids,
                )
                if not admitted.allowed:
                    raise RecipeRequestInvalid(
                        "stop plan is stale or blocked",
                        reason=InvalidRequestReason.SUPERSEDED,
                    )
                plan_digest = admitted.plan_digest
                if self._route_publications is not None and (
                    not self._route_publications.withdrawal_complete_in_session(
                        session, frozenset({run_id})
                    )
                ):
                    # A competing publication listed the run again, or the
                    # withdrawal was superseded before it completed.
                    raise _RouteNotWithdrawn(run_id)
                run = session.get(RecipeRun, run_id)
                assert run is not None
                job = self._complete_absent_stop_in_session(
                    session,
                    run=run,
                    admitted=admitted,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    now=now,
                )
                if job is not None:
                    return self._view(job, session=session)
                job = self._queue_stop_in_session(
                    session,
                    run=run,
                    admitted=admitted,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    now=now,
                    profile_target_node_ids=profile_target_node_ids,
                )
                run.route_error = None
        except IntegrityError as error:
            raced = self._idempotent(
                request_id,
                "recipe.stop",
                plan_digest,
                owner_kind="run",
                owner_id=run_id,
            )
            if raced is not None:
                return raced
            raise RecipeRequestInvalid(
                "request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            ) from error
        return self.get(job.id)

    @staticmethod
    def _absent_stop_nodes(
        session: Session,
        run: RecipeRun,
        admitted: StopPlan,
        now: datetime,
        *,
        lock: bool,
    ) -> tuple[RunNode, ...] | None:
        """The run's ranks when every one is already reported not running."""

        if admitted.missing_node_ids:
            return None
        statement = (
            select(RunNode)
            .where(RunNode.run_id == run.id)
            .order_by(RunNode.rank, RunNode.node_id)
        )
        if lock:
            statement = statement.with_for_update(of=RunNode)
        nodes = tuple(session.scalars(statement))
        if not nodes or not all(
            run_node_reports_absent(run, node, now) for node in nodes
        ):
            return None
        return nodes

    def _complete_absent_stop_in_session(
        self,
        session: Session,
        *,
        run: RecipeRun,
        admitted: StopPlan,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None,
        now: datetime,
    ) -> Job | None:
        """Finish a Stop whose ranks every Spark already reports as not running.

        There is nothing left to stop: the Sparks' own current report is the
        proof, so the run is recorded stopped, its ports and memory are released
        and the Stop succeeds without an agent round trip.
        """

        nodes = self._absent_stop_nodes(session, run, admitted, now, lock=True)
        if nodes is None:
            return None
        ordinal = self._admit_workload_intent(
            session,
            kind="recipe.stop",
            targets=sorted(node.node_id for node in nodes),
            workload_intent_ordinal=workload_intent_ordinal,
            now=now,
        )
        settle_absent_run_in_session(session, run, nodes, now)
        payload: dict[str, object] = {
            "schema_version": 1,
            "owner_kind": "run",
            "owner_id": run.id,
            "plan_digest": admitted.plan_digest,
            "workload_intent_ordinal": ordinal,
        }
        job = new_recipe_job(
            id=str(uuid.uuid4()),
            request_id=request_id,
            kind="recipe.stop",
            state="succeeded",
            actor=actor,
            authority_revision=admitted.authority_digest.removeprefix("sha256:"),
            targets=sorted(node.node_id for node in nodes),
            payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
            payload=payload,
            result=_validated_result("recipe.stop", {"stopped": True}),
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        return job

    def queue_recovery_stop_in_session(
        self,
        session: Session,
        run_id: str,
        *,
        recovery_context: DistributedRecoveryMarker,
        workload_intent_ordinal: int,
        now: datetime,
    ) -> Job | Residue:
        """Queue an accepted-run recovery through the canonical Stop owner.

        The caller already owns the route-publication transaction and has
        withdrawn this run's route.  Keeping the Stop admission and queue write
        in that transaction makes duplicate recovery ticks and newer workload
        intent serialize against the same run and node facts.

        A recovery whose scope is no longer current, or whose stored continuation
        or start authority is damaged, queues nothing: the damage is retired as
        unknown (a :class:`Residue`).  A scope that is only not current yet
        (``EVIDENCE_UNAVAILABLE``) is retried on the next pass; damaged
        authority is settled by the recovery owner as unrecoverable.  Either way
        the owner goes on to the next run.
        """

        run = session.get(RecipeRun, run_id, with_for_update=True)
        if (
            run is None
            or run.state != RunState.RUNNING
            or run.route_state != RouteState.WITHDRAWN
        ):
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "recovery Stop scope is no longer current",
            )
        if type(workload_intent_ordinal) is not int or workload_intent_ordinal < 1:
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                "recovery workload intent is invalid",
            )
        try:
            decoded = recovery_start_plan(
                recovery_context,
                now=now,
                require_unexpired=False,
            )
        except DistributedLifecycleError as error:
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"recovery continuation is invalid: {error}",
            )
        if decoded is None:
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.ROW_INCOMPLETE,
                "recovery continuation is missing",
            )
        start_phases, marker = decoded
        nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(RunNode.run_id == run.id)
                .order_by(RunNode.rank, RunNode.node_id)
            )
        )
        if (
            len(nodes) != 1
            or len(start_phases) != 1
            or len(start_phases[0]) != 1
            or start_phases[0][0][0] != nodes[0].node_id
        ):
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "recovery Start rank set is invalid",
            )
        start_payload = start_phases[0][0][1]
        try:
            accepted_start = read_stored_model(
                RecipeStartPayload, canonical_message(start_payload), from_json=True
            )
        except (TypeError, ValueError) as error:
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"recovery Start payload is invalid: {error}",
            )
        if (
            str(accepted_start.run_id) != run.id
            or accepted_start.plan_digest != run.plan_digest
            or accepted_start.run_generation != run.run_generation
            or accepted_start.run_generation <= 1
            or accepted_start.compiled_execution_plan.runtime.placement.rank != 0
            or accepted_start.compiled_execution_plan.runtime.placement.role
            != "entrypoint"
        ):
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "recovery Start differs from accepted run",
            )
        request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "vonk:singleton-recovery-stop:"
                f"{run.id}:{accepted_start.run_generation}:{marker.deadline}",
            )
        )
        existing = self._idempotent_job_in_session(
            session,
            request_id,
            "recipe.stop",
            None,
            owner_kind="run",
            owner_id=run.id,
        )
        if existing is not None:
            return existing
        admitted = self._stop_plan_in_session(session, run.id, lock=True)
        if (
            not admitted.allowed
            or admitted.authority_digest != run.plan_digest
            or {item.node_id for item in admitted.nodes} != {nodes[0].node_id}
        ):
            return retire_as_unknown(
                "recipe.recovery",
                run_id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "recovery Stop admission is blocked",
            )
        try:
            return self._queue_stop_in_session(
                session,
                run=run,
                admitted=admitted,
                actor="system:singleton-recovery",
                request_id=request_id,
                workload_intent_ordinal=workload_intent_ordinal,
                now=now,
                job_context={"recovery": serialize_json_value(recovery_context)},
                stop_run_generation=accepted_start.run_generation - 1,
            )
        except RecipeOperationConflict as error:
            if str(error) == "workload intent was superseded":
                raise DistributedRecoveryInvalid(
                    "singleton recovery was superseded by a newer workload intent",
                    reason=InvalidRequestReason.SUPERSEDED,
                ) from error
            raise

    def _exact_stop_authority(
        self,
        session: Session,
        run: RecipeRun,
        admitted: StopPlan,
        *,
        stop_run_generation: int | None = None,
    ) -> tuple[tuple[str, ...] | None, Mapping[str, RecipeStopPayload], set[str]]:
        """Bind exact run identity before route withdrawal or destructive work.

        Current consent and durable run/rank ownership authorize Stop. Historical
        Start evidence is disposable. Unreadable topology stops exact targets
        together; mismatched target ownership remains a security refusal.
        """

        revision = _active_recipe_revision(session, admitted.recipe_revision_id)
        stop_order = (
            _topology_order(revision.document, "stop_order")
            if revision is not None
            else None
        )
        if stop_order is None:
            retire_as_unknown(
                "recipe.stop-order",
                run.id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "recipe topology is unreadable; the targets are stopped together",
            )
        generation = (
            run.run_generation if stop_run_generation is None else stop_run_generation
        )
        try:
            exact_stop_payloads = durable_run_stop_payloads(
                session,
                run,
                admitted.nodes,
                run_generation=generation,
                cancel_pending_start=True,
                allow_missing_nodes=False,
            )
        except RecipeStopAuthorityError as error:
            raise RecipeStopAuthorityRefused(
                "recipe Stop exact target ownership differs"
            ) from error
        target_ids = set(admitted.target_node_ids)
        if not target_ids or not target_ids <= set(exact_stop_payloads):
            raise RecipeStopAuthorityRefused(
                "recipe Stop target lacks its exact durable Start authority"
            )
        return stop_order, exact_stop_payloads, target_ids

    def _queue_stop_in_session(
        self,
        session: Session,
        *,
        run: RecipeRun,
        admitted: StopPlan,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None,
        now: datetime,
        job_context: Mapping[str, object] | None = None,
        stop_run_generation: int | None = None,
        profile_target_node_ids: Sequence[str] | None = None,
    ) -> Job:
        stop_order, exact_stop_payloads, target_ids = self._exact_stop_authority(
            session, run, admitted, stop_run_generation=stop_run_generation
        )
        if stop_order is not None and profile_target_node_ids is not None:
            reachable_roles = {
                node.role for node in admitted.nodes if node.node_id in target_ids
            }
            stop_order = tuple(role for role in stop_order if role in reachable_roles)
        context = dict(job_context or {})
        if admitted.missing_node_ids:
            context["profile_partial_stop"] = {
                "target_node_ids": list(admitted.target_node_ids),
                "missing_node_ids": list(admitted.missing_node_ids),
            }
        stop_payloads = tuple(
            (
                node.node_id,
                json.loads(canonical_message(exact_stop_payloads[node.node_id])),
            )
            for node in admitted.nodes
            if node.node_id in target_ids
        )
        stop_phases = (
            _role_phases(stop_order, stop_payloads)
            if stop_order is not None
            else (stop_payloads,)
        )
        if stop_phases is None:
            stop_phases = (stop_payloads,)
        run.state = RunState.STOPPING
        run.route_state = RouteState.WITHDRAWN
        run.updated_at = now
        job = self._queue_in_session(
            session,
            kind="recipe.stop",
            owner_kind="run",
            owner_id=run.id,
            plan_digest=admitted.plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=stop_payloads,
            phases=stop_phases,
            authority_digest=admitted.authority_digest,
            now=now,
            workload_intent_ordinal=workload_intent_ordinal,
            job_context=context or None,
        )
        session.flush()
        return job

    def preview_uninstall(
        self, installation_id: str, *, also_removing: Collection[str] = ()
    ) -> UninstallPlan:
        """The uninstall plan; ``also_removing`` names installations removed in
        the same sweep, which do not count as users of the shared model files."""

        with self._sessions() as session:
            return self._uninstall_plan_in_session(
                session, installation_id, lock=False, also_removing=also_removing
            )

    def preview_reconciliation_authority(
        self,
        installation_id: str,
        *,
        session: Session | None = None,
        allow_active_reconciliation: bool = False,
    ) -> InstallationReconciliationAuthority:
        """Bind an explicit cleanup review to accepted installation identity.

        This path reads the admitted installation document only as opaque JSON.
        It never converts the damaged launch specification into an executable
        plan and it does not weaken the ordinary uninstall assessment.
        """

        if session is not None:
            return self._reconciliation_authority_in_session(
                session,
                installation_id,
                lock=False,
                allow_active_reconciliation=allow_active_reconciliation,
            )
        with self._sessions() as owned_session:
            return self._reconciliation_authority_in_session(
                owned_session,
                installation_id,
                lock=False,
                allow_active_reconciliation=allow_active_reconciliation,
            )

    def reconciliation_complete(
        self,
        request_id: str,
        *,
        expected_authority: RunSwitchReconciliationAuthority,
    ) -> bool:
        """Whether the reviewed reconciliation succeeded on every pending rank."""

        with self._sessions() as session:
            job = session.scalar(
                select(Job).where(
                    Job.request_id == request_id,
                    Job.kind == "recipe.reconcile",
                )
            )
            if (
                job is None
                or job.state != "succeeded"
                or canonical_message(_parent_reconciliation(job))
                != canonical_message(expected_authority)
            ):
                return False
            children = tuple(
                session.scalars(
                    select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
                )
            )
            return self._reconciliation_job_complete(session, job, children)

    @staticmethod
    def _lock_reconciliation_rows_in_session(
        session: Session, installation: RecipeInstallation
    ) -> None:
        """Acquire the complete reconciliation row set in canonical order."""

        node_rows = tuple(
            session.scalars(
                select(InstallationNode)
                .where(InstallationNode.installation_id == installation.id)
                .order_by(InstallationNode.rank, InstallationNode.node_id)
            )
        )
        node_ids = tuple(node.node_id for node in node_rows)
        mapping_nodes = tuple(
            session.scalars(
                select(ClusterMappingNode)
                .where(ClusterMappingNode.mapping_id == installation.mapping_id)
                .order_by(ClusterMappingNode.rank, ClusterMappingNode.node_id)
            )
        )
        runs = tuple(
            session.scalars(
                select(RecipeRun).where(RecipeRun.installation_id == installation.id)
            )
        )
        run_ids = tuple(run.id for run in runs)
        source_jobs = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == "recipe.install",
                    Job.state == "succeeded",
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == installation.id,
                    Job.payload["plan_digest"].as_string() == installation.plan_digest,
                )
            )
        )
        reconciliation_jobs = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == "recipe.reconcile",
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == installation.id,
                )
            )
        )
        active_jobs = tuple(
            session.scalars(
                select(Job).where(
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.OBSERVING,
                            LifecycleState.BACKOFF,
                        )
                    ),
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == installation.id,
                    Job.kind.in_(
                        ("recipe.install", "recipe.uninstall", "recipe.reconcile")
                    ),
                )
            )
        )
        active_run_jobs = (
            tuple(
                session.scalars(
                    select(Job).where(
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.OBSERVING,
                                LifecycleState.BACKOFF,
                            )
                        ),
                        Job.payload["owner_kind"].as_string() == "run",
                        Job.payload["owner_id"].as_string().in_(run_ids),
                        Job.kind.in_(("recipe.start", "recipe.stop")),
                    )
                )
            )
            if run_ids
            else ()
        )
        job_ids = tuple(
            sorted(
                {
                    job.id
                    for job in (
                        *source_jobs,
                        *reconciliation_jobs,
                        *active_jobs,
                        *active_run_jobs,
                    )
                }
            )
        )
        requests = (
            AdmissionRowLock(
                "reconcile-agent-nodes",
                AgentNode,
                select(AgentNode).where(AgentNode.node_id.in_(node_ids)),
            ),
            AdmissionRowLock(
                "reconcile-recipe-revision",
                CatalogDocumentRevision,
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.id == installation.recipe_revision_id
                ),
            ),
            AdmissionRowLock(
                "reconcile-mapping",
                ClusterMapping,
                select(ClusterMapping).where(
                    ClusterMapping.id == installation.mapping_id
                ),
            ),
            AdmissionRowLock(
                "reconcile-installation",
                RecipeInstallation,
                select(RecipeInstallation).where(
                    RecipeInstallation.id == installation.id
                ),
            ),
            AdmissionRowLock(
                "reconcile-mapping-nodes",
                ClusterMappingNode,
                select(ClusterMappingNode).where(
                    ClusterMappingNode.mapping_id == installation.mapping_id
                ),
            ),
            AdmissionRowLock(
                "reconcile-installation-nodes",
                InstallationNode,
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation.id
                ),
            ),
            AdmissionRowLock(
                "reconcile-jobs",
                Job,
                select(Job).where(Job.id.in_(job_ids)),
            ),
            AdmissionRowLock(
                "reconcile-agent-operations",
                AgentOperation,
                select(AgentOperation).where(AgentOperation.parent_job_id.in_(job_ids)),
            ),
        )
        try:
            locked = lock_admission_rows(session, requests)
        except AdmissionLockBusy as error:
            raise InstallAdmissionBusy(
                ReconcileCode.CAPACITY_BUSY, reason=WaitReason.OBSERVATION_UNAVAILABLE
            ) from error
        locked_nodes = locked["reconcile-installation-nodes"]
        locked_mapping_nodes = locked["reconcile-mapping-nodes"]
        if tuple(
            sorted((node.node_id, node.rank, node.role) for node in locked_nodes)
        ) != tuple(
            sorted((node.node_id, node.rank, node.role) for node in node_rows)
        ) or tuple(
            sorted(
                (node.node_id, node.rank, node.role) for node in locked_mapping_nodes
            )
        ) != tuple(
            sorted((node.node_id, node.rank, node.role) for node in mapping_nodes)
        ):
            raise InstallAdmissionBusy(
                ReconcileCode.MEMBERSHIP_CHANGED,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )

    def _reconciliation_authority_in_session(
        self,
        session: Session,
        installation_id: str,
        *,
        lock: bool,
        allow_active_reconciliation: bool = False,
    ) -> InstallationReconciliationAuthority:
        installation_statement = select(RecipeInstallation).where(
            RecipeInstallation.id == installation_id
        )
        installation = session.scalar(installation_statement)
        if installation is None:
            raise MissingRecord(installation_id)

        if lock:
            self._lock_reconciliation_rows_in_session(session, installation)
            # The helper refreshed every locked row after acquiring the full
            # set in canonical table/primary-key order. Keep later reads free
            # of ad-hoc locks that could invert that order.
            lock = False
            allow_active_reconciliation = False

        def blocked(code: str, detail: str) -> NoReturn:
            raise RecipeReconciliationBlocked(code, detail)

        revision_statement = select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.id == installation.recipe_revision_id,
            CatalogDocumentRevision.kind == "recipe",
        )
        if lock:
            revision_statement = revision_statement.with_for_update(
                of=CatalogDocumentRevision
            )
        revision = session.scalar(revision_statement)
        if revision is None or revision.content_digest is None:
            blocked(
                ReconcileCode.RECIPE_REVISION_UNAVAILABLE,
                "The installation's exact accepted recipe revision is unavailable.",
            )

        # Cleanup binds the accepted relational identity. Launch JSON is a
        # projection that may no longer parse after upgrade; the helper still
        # checks this installation's exact plan digest against local effects.
        if installation.state not in {
            InstallationState.INSTALLED,
            InstallationState.PARTIAL,
            InstallationState.FAILED,
        }:
            blocked(
                ReconcileCode.INSTALLATION_EFFECT_UNKNOWN,
                f"Installation state {installation.state} does not prove a complete installed effect.",
            )

        node_statement = (
            select(InstallationNode)
            .where(InstallationNode.installation_id == installation.id)
            .order_by(InstallationNode.rank, InstallationNode.node_id)
            .limit(_MAX_ACTION_NODES + 1)
        )
        if lock:
            node_statement = node_statement.with_for_update(of=InstallationNode)
        all_nodes = tuple(session.scalars(node_statement))
        if not all_nodes or len(all_nodes) > _MAX_ACTION_NODES:
            blocked(
                ReconcileCode.RANK_MEMBERSHIP_CHANGED,
                "The installation has no bounded exact node membership.",
            )
        mapping_statement = select(ClusterMapping).where(
            ClusterMapping.id == installation.mapping_id
        )
        if lock:
            mapping_statement = mapping_statement.with_for_update(of=ClusterMapping)
        mapping = session.scalar(mapping_statement)
        mapping_nodes_statement = (
            select(ClusterMappingNode)
            .where(ClusterMappingNode.mapping_id == installation.mapping_id)
            .order_by(ClusterMappingNode.rank, ClusterMappingNode.node_id)
        )
        if lock:
            mapping_nodes_statement = mapping_nodes_statement.with_for_update(
                of=ClusterMappingNode
            )
        mapping_nodes = tuple(session.scalars(mapping_nodes_statement))
        actual_membership = tuple(
            (node.node_id, node.rank, node.role) for node in all_nodes
        )
        mapping_membership = tuple(
            (node.node_id, node.rank, node.role) for node in mapping_nodes
        )
        if (
            mapping is None
            or mapping.recipe_revision_id != revision.id
            or mapping.node_count != len(mapping_nodes)
            or not mapping_nodes
            or actual_membership != mapping_membership
            or tuple(node.rank for node in all_nodes) != tuple(range(len(all_nodes)))
            or mapping.endpoint_owner_node_id
            not in {node.node_id for node in mapping_nodes}
        ):
            blocked(
                ReconcileCode.RANK_MEMBERSHIP_CHANGED,
                "The stored installation, relational membership, and saved mapping no longer identify the same exact ranks.",
            )

        active_runs_statement = select(RecipeRun).where(
            RecipeRun.installation_id == installation.id
        )
        if lock:
            active_runs_statement = active_runs_statement.with_for_update(of=RecipeRun)
        installation_runs = tuple(session.scalars(active_runs_statement))
        if any(
            run.state != RunState.STOPPED or run.route_state != RouteState.WITHDRAWN
            for run in installation_runs
        ):
            blocked(
                ReconcileCode.ACTIVE_EFFECT_UNKNOWN,
                "An installation run or route is active or has an unconfirmed effect.",
            )

        active_jobs_statement = select(Job).where(
            Job.state.in_(
                job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.OBSERVING,
                    LifecycleState.BACKOFF,
                )
            ),
            Job.payload["owner_kind"].as_string() == "installation",
            Job.payload["owner_id"].as_string() == installation.id,
            Job.kind.in_(("recipe.install", "recipe.uninstall", "recipe.reconcile")),
        )
        if lock:
            active_jobs_statement = active_jobs_statement.with_for_update(of=Job)
        active_jobs = tuple(session.scalars(active_jobs_statement))
        run_ids = {run.id for run in installation_runs}
        active_run_jobs = (
            tuple(
                session.scalars(
                    select(Job).where(
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.OBSERVING,
                                LifecycleState.BACKOFF,
                            )
                        ),
                        Job.payload["owner_kind"].as_string() == "run",
                        Job.payload["owner_id"].as_string().in_(run_ids),
                        Job.kind.in_(("recipe.start", "recipe.stop")),
                    )
                )
            )
            if run_ids
            else ()
        )
        blocking_active_jobs = (
            tuple(job for job in active_jobs if job.kind != "recipe.reconcile")
            if allow_active_reconciliation
            else active_jobs
        )
        if blocking_active_jobs or active_run_jobs:
            blocked(
                ReconcileCode.OPERATION_ACTIVE,
                "An install, start, stop, uninstall, or reconciliation operation is still active or uncertain.",
            )

        node_by_id = {node.node_id: node for node in all_nodes}
        agent_nodes_statement = (
            select(AgentNode)
            .where(AgentNode.node_id.in_(node_by_id))
            .order_by(AgentNode.node_id)
        )
        if lock:
            agent_nodes_statement = agent_nodes_statement.with_for_update(of=AgentNode)
        agent_nodes = tuple(session.scalars(agent_nodes_statement))
        agent_node_by_id = {node.node_id: node for node in agent_nodes}
        targets: list[InstallationReconciliationTarget] = []
        for node in all_nodes:
            agent_node = agent_node_by_id.get(node.node_id)
            if (
                agent_node is None
                or agent_node.state != "active"
                or agent_node.revoked_at is not None
            ):
                blocked(
                    ReconcileCode.AGENT_UNAVAILABLE,
                    f"Node {node.node_id} is not an active authorized target.",
                )
            # A succeeded, fenced reconcile attempt marked the rank uninstalled;
            # that state is the proof its cleanup already happened.
            targets.append(
                InstallationReconciliationTarget(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    installed_bytes=node.installed_bytes,
                    state="reconciled"
                    if node.state == InstallationNodeState.UNINSTALLED
                    else "pending",
                )
            )

        return InstallationReconciliationAuthority(
            installation_id=installation.id,
            original_plan_digest=installation.plan_digest,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            mapping_id=mapping.id,
            mapping_generation=installation.mapping_generation,
            recipe_build_id=installation.recipe_build_id,
            image_digest=installation.image_digest,
            model_content_sha256=installation.model_content_sha256,
            targets=tuple(targets),
        )

    def _reconcile_installation_once(
        self,
        installation_id: str,
        *,
        expected_authority: RunSwitchReconciliationAuthority,
        run_switch_plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        existing = self._idempotent(
            request_id,
            "recipe.reconcile",
            run_switch_plan_digest,
            owner_kind="installation",
            owner_id=installation_id,
        )
        if existing is not None:
            return existing
        now = self._clock()
        try:
            with self._sessions.begin() as session:
                target_nodes = tuple(
                    session.scalars(
                        select(InstallationNode.node_id)
                        .where(InstallationNode.installation_id == installation_id)
                        .order_by(InstallationNode.node_id)
                    )
                )
                try:
                    acquire_admission_keys(
                        session,
                        (
                            job_request_key(request_id),
                            *(node_admission_key(node_id) for node_id in target_nodes),
                        ),
                        holder="recipe-operation",
                    )
                except AdmissionLockBusy as error:
                    raise InstallAdmissionBusy(
                        ReconcileCode.CAPACITY_BUSY,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    ) from error
                authority = self._reconciliation_authority_in_session(
                    session, installation_id, lock=True
                )
                if canonical_message(authority.document()) != canonical_message(
                    expected_authority
                ):
                    raise RecipeRequestInvalid(
                        "reconciliation authority changed after preview",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                existing = self._idempotent_in_session(
                    session,
                    request_id,
                    "recipe.reconcile",
                    run_switch_plan_digest,
                    owner_kind="installation",
                    owner_id=installation_id,
                )
                if existing is not None:
                    return existing
                pending_targets = tuple(
                    target for target in authority.targets if target.state == "pending"
                )
                if not pending_targets:
                    raise RecipeRequestInvalid(
                        "reconciliation has no pending targets but is not complete"
                    )
                payloads = tuple(
                    (
                        target.node_id,
                        RecipeReconcilePayload(
                            installation_id=authority.installation_id,
                            plan_digest=authority.original_plan_digest,
                        ).model_dump(mode="json"),
                    )
                    for target in pending_targets
                )
                job = self._queue_in_session(
                    session,
                    kind="recipe.reconcile",
                    owner_kind="installation",
                    owner_id=installation_id,
                    plan_digest=run_switch_plan_digest,
                    actor=actor,
                    request_id=request_id,
                    node_payloads=payloads,
                    authority_digest=authority.recipe_content_sha256,
                    now=now,
                    workload_intent_ordinal=workload_intent_ordinal,
                    job_context={
                        "reconciliation_authority": authority.document(),
                    },
                )
        except AdmissionLockBusy as error:
            raise InstallAdmissionBusy(
                ReconcileCode.CAPACITY_BUSY, reason=WaitReason.OBSERVATION_UNAVAILABLE
            ) from error
        except IntegrityError as error:
            raced = self._idempotent(
                request_id,
                "recipe.reconcile",
                run_switch_plan_digest,
                owner_kind="installation",
                owner_id=installation_id,
            )
            if raced is not None:
                return raced
            raise RecipeRequestInvalid(
                "request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            ) from error
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def reconcile_installation(
        self,
        installation_id: str,
        *,
        expected_authority: RunSwitchReconciliationAuthority,
        run_switch_plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        """Queue exact managed cleanup under a current Run/Switch review."""
        refused: InstallAdmissionBusy | None = None
        for _attempt in admission_attempts():
            try:
                return self._reconcile_installation_once(
                    installation_id,
                    expected_authority=expected_authority,
                    run_switch_plan_digest=run_switch_plan_digest,
                    actor=actor,
                    request_id=request_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                )
            except InstallAdmissionBusy as error:
                refused = error
        assert refused is not None
        raise refused

    def uninstall(
        self,
        installation_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
        unattended_guard: Callable[[Session], None] | None = None,
        also_removing: Collection[str] = (),
    ) -> RecipeOperationView:
        """Queue the removal of one installation from its Sparks.

        ``also_removing`` names installations removed in the same sweep: they do
        not keep the shared model files, so the last of them to go frees them.

        ``unattended_guard`` marks a removal nobody asked for (the storage
        sweep). It takes no new workload intent, so it supersedes no order and
        costs no running workload its recovery, and it is called with the
        target Sparks locked so the caller can re-check that the installation is
        still unused; raising refuses the removal before anything is queued.
        """

        existing = self._idempotent(
            request_id,
            "recipe.uninstall",
            None,
            owner_kind="installation",
            owner_id=installation_id,
        )
        if existing is not None:
            return existing
        now = self._clock()
        try:
            with self._sessions.begin() as session:
                installation_fence = session.scalar(
                    select(RecipeInstallation)
                    .where(RecipeInstallation.id == installation_id)
                    .with_for_update(of=RecipeInstallation)
                )
                if installation_fence is None:
                    raise RecipeRequestInvalid("recipe installation does not exist")
                existing = self._idempotent_in_session(
                    session,
                    request_id,
                    "recipe.uninstall",
                    None,
                    owner_kind="installation",
                    owner_id=installation_id,
                )
                if existing is not None:
                    return existing
                plan = self._uninstall_plan_in_session(
                    session, installation_id, lock=True, also_removing=also_removing
                )
                if not plan.allowed:
                    raise RecipeRequestInvalid(
                        "uninstall plan is stale or blocked: "
                        + "; ".join(reason.code for reason in plan.blockers),
                        reason=InvalidRequestReason.SUPERSEDED,
                    )
                if plan.disposition != "uninstall":
                    # A plan that never reached a node is abandoned by the
                    # cleanup phase; it must never queue agent removal work.
                    raise RecipeRequestInvalid(
                        "installation was never installed; abandon it instead "
                        "of uninstalling it"
                    )
                job = self._queue_in_session(
                    session,
                    kind="recipe.uninstall",
                    owner_kind="installation",
                    owner_id=installation_id,
                    plan_digest=plan.plan_digest,
                    actor=actor,
                    request_id=request_id,
                    node_payloads=tuple(
                        (
                            node.node_id,
                            {
                                "installation_id": installation_id,
                                "recipe_content_sha256": (
                                    plan.installation_authority_digest
                                ),
                                "plan_digest": plan.original_plan_digest,
                                "cleanup_model_content_sha256": (
                                    plan.model_impact.model_content_sha256
                                    if node.node_id
                                    in plan.model_impact.cleanup_node_ids
                                    else None
                                ),
                            },
                        )
                        for node in plan.nodes
                        if node.state != InstallationNodeState.UNINSTALLED
                    ),
                    authority_digest=plan.installation_authority_digest,
                    now=now,
                    workload_intent_ordinal=workload_intent_ordinal,
                    unattended_guard=unattended_guard,
                )
        except IntegrityError as error:
            raced = self._idempotent(
                request_id,
                "recipe.uninstall",
                plan_digest,
                owner_kind="installation",
                owner_id=installation_id,
            )
            if raced is not None:
                return raced
            raise RecipeRequestInvalid(
                "request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            ) from error
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def abandon_never_installed(self, installation_id: str) -> dict[str, object]:
        """Resolve a persisted plan that never reached a node.

        The cleanup phase uses this instead of ``uninstall`` when the
        installation's own assessment reports the never-installed
        disposition.  Nothing is queued to an agent: there are no installed
        bytes, only the admission record and its disk reservation.  The
        assessment is re-derived under the installation row lock, so a
        concurrent install turns this into a refusal rather than a silent
        state flip.  The installation row and its immutable plan are retained,
        so the history stays auditable.
        """

        now = self._clock()
        with self._sessions.begin() as session:
            installation = session.scalar(
                select(RecipeInstallation)
                .where(RecipeInstallation.id == installation_id)
                .with_for_update(of=RecipeInstallation)
            )
            if installation is None:
                raise RecipeRequestInvalid("recipe installation does not exist")
            if installation.state == InstallationState.UNINSTALLED:
                # A restarted cleanup phase replays the disposal.  The row is
                # already resolved, so the replay succeeds instead of failing
                # the operation after the effect has landed.
                return {
                    "installation_id": installation_id,
                    "disposition": "abandoned",
                }
            plan = self._uninstall_plan_in_session(session, installation_id, lock=True)
            if not plan.allowed or plan.disposition != "abandon":
                raise RecipeRequestInvalid(
                    "installation is not a never-installed plan; it cannot be abandoned",
                    reason=InvalidRequestReason.UNSUPPORTED,
                )
            nodes = tuple(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id == installation_id)
                    .with_for_update(of=InstallationNode)
                )
            )
            for node in nodes:
                node.state = InstallationNodeState.UNINSTALLED
                node.updated_at = now
            installation.state = InstallationState.UNINSTALLED
            installation.updated_at = now
            self._release(session, "installation", installation_id, now)
        return {
            "installation_id": installation_id,
            "disposition": "abandoned",
        }

    def retry(
        self, operation_id: str, *, actor: str, request_id: str
    ) -> RecipeOperationView:
        now = self._clock()
        with self._sessions.begin() as session:
            previous = session.get(Job, operation_id, with_for_update=True)
            previous_plan_digest = (
                _parent_identity(previous, "plan_digest")
                if previous is not None
                else None
            )
            if previous is None or previous_plan_digest is None:
                raise RecipeRequestInvalid("recipe operation is not retryable")
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is not None:
                if (
                    existing.kind != previous.kind
                    or _parent_identity(existing, "plan_digest") != previous_plan_digest
                ):
                    raise RecipeRequestInvalid("request key was already used")
                return self._view(existing)
            if previous.kind == "recipe.build.v1":
                job = self._retry_build_in_session(
                    session,
                    previous,
                    actor=actor,
                    request_id=request_id,
                    now=now,
                    intent=RecipeBuildIntent(kind="independent"),
                )
            elif previous.kind == "recipe.install" and previous.state == "failed":
                job = self._retry_install_in_session(
                    session, previous, actor=actor, request_id=request_id, now=now
                )
            else:
                raise RecipeRequestInvalid("recipe operation is not retryable")
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def _retry_install_in_session(
        self,
        session: Session,
        previous: Job,
        *,
        actor: str,
        request_id: str,
        now: datetime,
    ) -> Job:
        previous_plan_digest = _parent_identity(previous, "plan_digest")
        owner_id = _parent_identity(previous, "owner_id")
        if previous_plan_digest is None or owner_id is None:
            raise RecipeRequestInvalid("recipe operation is not retryable")
        installation = session.get(RecipeInstallation, owner_id, with_for_update=True)
        if installation is None or installation.state not in {
            InstallationState.PARTIAL,
            InstallationState.FAILED,
        }:
            raise RecipeRequestInvalid("recipe installation is not retryable")
        nodes = tuple(
            session.scalars(
                select(InstallationNode)
                .where(InstallationNode.installation_id == installation.id)
                .order_by(InstallationNode.node_id)
            )
        )
        revision = _active_recipe_revision(session, installation.recipe_revision_id)
        assert revision is not None and revision.content_digest is not None
        recipe_digest = revision.content_digest
        compiled_plans = self._stored_compiled_plans(
            session, installation, [node.node_id for node in nodes], now=now
        )
        if isinstance(compiled_plans, Residue) or not nodes:
            raise RecipeRetryLater(
                "stored compiled execution plan is unreadable for install retry; it "
                "is recorded and the next preparation installs afresh"
            )
        installation.state = InstallationState.INSTALLING
        installation.updated_at = now
        for node in nodes:
            node.state = InstallationNodeState.PLANNED
        # The attempt that failed may have had its disk claim released as
        # abandoned (nothing was issued for it); the retry is that operation
        # again, so it takes the same exact claim back.
        for claim in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "installation",
                ResourceReservation.owner_id == owner_id,
                ResourceReservation.kind == "disk",
                ResourceReservation.state == ReservationState.RELEASED,
                ResourceReservation.plan_digest == previous_plan_digest,
            )
        ):
            claim.state = ReservationState.ACTIVE
            claim.released_at = None
        return self._queue_in_session(
            session,
            kind="recipe.install",
            owner_kind="installation",
            owner_id=owner_id,
            plan_digest=previous_plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(
                (
                    node.node_id,
                    {
                        "installation_id": owner_id,
                        "plan_digest": previous_plan_digest,
                        "expected_bytes": node.required_bytes,
                        "compiled_execution_plan": compiled_plans[
                            node.node_id
                        ].model_dump(mode="json"),
                    },
                )
                for node in nodes
            ),
            authority_digest=recipe_digest,
            now=now,
            workload_intent_ordinal=_bound_workload_intent(previous),
        )

    def _retry_build_in_session(
        self,
        session: Session,
        previous: Job,
        *,
        actor: str,
        request_id: str,
        now: datetime,
        intent: RecipeBuildIntent,
    ) -> Job:
        if previous.state not in job_states.words(
            LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
        ):
            raise RecipeRequestInvalid("recipe build is not retryable")
        if build_cancellation(previous) is not None:
            raise RecipeRequestInvalid("cancelled recipe build intent is not retryable")
        if self._builds is None:
            raise RecipeRetryLater("recipe build service is unavailable")
        owner_id = _parent_identity(previous, "owner_id")
        build = (
            session.get(RecipeBuild, owner_id, with_for_update=True)
            if owner_id is not None
            else None
        )
        if (
            owner_id is None
            or build is None
            or build.state not in {"building", "failed"}
        ):
            raise RecipeRequestInvalid("recipe build is not retryable")
        active = next(
            (
                job
                for job in session.scalars(
                    select(Job).where(Job.kind == "recipe.build.v1").order_by(Job.id)
                )
                if job.id != previous.id
                and job.state in {"queued", "running"}
                and isinstance(job.payload, Mapping)
                and _parent_identity(job, "owner_id") == owner_id
            ),
            None,
        )
        if active is not None:
            # A retry is already in flight: this request adopts it.
            return active

        def plan_from(document: object) -> RecipeBuildPlan | Damaged:
            payload = build_plan_document(document)
            parsed_payload = parse_stored_build_plan(payload)
            if (
                payload.get("build_id") != owner_id
                or payload.get("build_input_sha256") != build.build_input_sha256
            ):
                return Damaged("stored build plan names another build")
            return RecipeBuildPlan(
                build_id=owner_id,
                recipe_revision_id=parsed_payload.recipe_revision_id,
                recipe_content_sha256=parsed_payload.recipe_content_sha256,
                builder_node_id=build.builder_node_id,
                source_bundle_sha256=parsed_payload.source_bundle_sha256,
                build_input_sha256=build.build_input_sha256,
                agent_payload=payload,
            )

        def rebuild() -> RecipeBuildPlan | None:
            # The build order the previous attempt carried is the plan it ran.
            child = session.scalar(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == previous.id)
                .order_by(AgentOperation.id)
                .limit(1)
            )
            if child is None:
                return None
            recovered = plan_from(child.payload)
            if isinstance(recovered, Damaged):
                return None
            build.plan = recovered.agent_payload
            return recovered

        loaded = read_or_rebuild(
            kind="recipe.build-plan",
            subject=owner_id,
            read=lambda: plan_from(build.plan),
            rebuild=rebuild,
        )
        if isinstance(loaded, Residue):
            raise RecipeRetryLater(
                "stored recipe build plan is unreadable; it is recorded and the next "
                "build request re-plans it"
            )
        plan = loaded
        self._release(session, "recipe-build", owner_id, now)
        return self._start_build_in_session(
            session,
            build,
            plan,
            actor=actor,
            request_id=request_id,
            now=now,
            intent=intent,
        )

    def record_node_result(
        self,
        operation_id: str,
        node_id: str,
        *,
        succeeded: bool,
        evidence: Mapping[str, object],
    ) -> RecipeOperationView:
        refused: UnknownOutcomeError | None = None
        for _attempt in admission_attempts():
            try:
                return self._record_node_result_once(
                    operation_id, node_id, succeeded=succeeded, evidence=evidence
                )
            except UnknownOutcomeError as error:
                refused = error
        assert refused is not None
        raise refused

    def _record_node_result_once(
        self,
        operation_id: str,
        node_id: str,
        *,
        succeeded: bool,
        evidence: Mapping[str, object],
    ) -> RecipeOperationView:
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id)
            if job is None or not job.kind.startswith("recipe."):
                raise MissingRecord(operation_id)
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
            cleanup_queued = self._project_node_result(
                session,
                job,
                operation,
                succeeded=succeeded,
                evidence=evidence,
                now=now,
            )
        if cleanup_queued:
            self._agent_jobs.notify_available()
        return self.get(operation_id)

    def consume_agent_result(
        self,
        session: Session,
        operation: AgentOperation,
        _attempt: object,
        message: object,
    ) -> None:
        """Project an authenticated agent result in the queue transaction."""
        job = session.get(Job, operation.parent_job_id)
        if job is None or not job.kind.startswith("recipe."):
            return
        if job.kind == "recipe.job.run.v1":
            return
        state = getattr(message, "state", None)
        result = getattr(message, "result", None)
        if state == "cancelled" and job.kind in _WORKLOAD_INTENT_KINDS:
            if not isinstance(job.result, Mapping) or not _cancel_requested(job):
                raise RecipeRequestInvalid("recipe cancellation was not requested")
            owner_id = _parent_identity(job, "owner_id")
            if owner_id is None:
                retire_as_unknown(
                    "recipe.operation",
                    job.id,
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "the cancelled operation payload names no owner",
                )
                return
            now = self._clock()
            if job.kind in {"recipe.install", "recipe.uninstall", "recipe.reconcile"}:
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
                if node is None or installation is None:
                    raise RecipeRequestInvalid(
                        "installation cancellation scope changed",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                node.state = _RANK_FAILED
                node.updated_at = now
                installation.state = InstallationState.PARTIAL
                installation.updated_at = now
            elif job.kind in {"recipe.start", "recipe.stop"}:
                node = session.scalar(
                    select(RunNode)
                    .where(
                        RunNode.run_id == owner_id, RunNode.node_id == operation.node_id
                    )
                    .with_for_update(of=RunNode)
                )
                run = session.get(RecipeRun, owner_id, with_for_update=True)
                if node is None or run is None:
                    raise RecipeRequestInvalid(
                        "run cancellation scope changed",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                # The agent sends cancelled only after its exact host STOP has
                # returned. Keep reservations for the current intent's stop.
                node.state = RunState.STOPPED
                node.updated_at = now
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
            state == "failed"
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
        succeeded = state == "succeeded"
        raw_evidence: LifecycleNodeResult | None = None
        unproven: str | None = None
        if state not in {"succeeded", "failed"}:
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
        self._project_node_result(
            session,
            job,
            operation,
            succeeded=succeeded,
            evidence=raw_evidence,
            now=self._clock(),
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
            or any(child.state != "succeeded" for child in children)
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

        try:
            read_stored_model(
                RecipeBuildCleanupEvidence,
                canonical_message(evidence),
                from_json=True,
            )
            expected = read_stored_model(
                RecipeBuildCleanupRequest,
                canonical_message(operation.payload),
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
            or original.kind != "recipe.build.v1"
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
            cancellation is None
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
        original_job.result = cancellation.model_copy(
            update={"cancelled": True}
        ).model_dump(mode="json", exclude_none=True)
        original_job.updated_at = now
        self._release_cancelled_build(session, owner_id, now)
        return None

    def _project_node_result(
        self,
        session: Session,
        job: Job,
        operation: AgentOperation,
        *,
        succeeded: bool,
        evidence: object,
        now: datetime,
    ) -> bool:
        cleanup_queued = False
        locked_job = session.get(Job, job.id, with_for_update=True)
        if locked_job is None:
            # Nothing owns this result any more: it is recorded as residue and
            # the order that carried it stays settled.
            retire_as_unknown(
                "recipe.operation",
                job.id,
                BookkeepingReason.ROW_INCOMPLETE,
                "the operation row disappeared before its result was projected",
            )
            return False
        job = locked_job
        # Lock acquisition can outlive a retained recovery deadline. Every
        # phase/terminal transition uses a fresh time sampled inside the lock.
        now = self._clock()
        operation.updated_at = now
        node_id = operation.node_id
        # A node whose evidence cannot be accepted: the typed marker is recorded
        # in its place and the node is counted failed (its retry or cleanup is
        # the owner's), so the operation completes instead of rolling back.
        node_result = _node_result(job.kind, node_id, evidence)
        unproven: dict[str, str] = {}
        start_wire: RecipeStartPayload | Residue | None = None
        owner_id = _parent_identity(job, "owner_id")
        if owner_id is None:
            retire_as_unknown(
                "recipe.operation",
                job.id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                "the operation payload names no owner",
            )
            owner_id = ""
            unproven[node_id] = "the operation payload names no owner"
        if unproven:
            pass
        elif job.kind == "recipe.build.cleanup.v1":
            if succeeded:
                reason = self._apply_build_cleanup(
                    session, job, operation, evidence, owner_id=owner_id, now=now
                )
                if reason is not None:
                    unproven[node_id] = reason
        elif job.kind == "recipe.build.v1":
            build = session.get(RecipeBuild, owner_id, with_for_update=True)
            if build is None or build.builder_node_id != node_id:
                # The build this result belongs to is gone or is another
                # builder's: nothing is recorded on it.
                retire_as_unknown(
                    "recipe.build-result",
                    job.id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the build row is missing or names another builder",
                )
                unproven[node_id] = "recipe build authority is invalid"
            else:
                cancellation = build_cancellation(job)
                if cancellation is not None:
                    # Completion can race cancellation. Retain the authenticated
                    # attempt's outcome, but only its own cancellation governs it.
                    # A later attempt may already own this build and its capacity.
                    if cancellation.cancelled is True:
                        AgentOperationAdapter(session).record_outcome(
                            operation, None, job, Outcome.CANCELLED, now
                        )
                        RecipeOperationAdapter().cancelled(job, now)
                    else:
                        # The cancel stays in flight for the cleanup sweeper; it never
                        # waits for an operator (audit C7).
                        RecipeOperationAdapter().cancel_race(job, now)
                    return False
                force_rebuild = _parent_force_rebuild(job) is True
                recorded_build = succeeded and record_build_evidence(
                    session,
                    build,
                    evidence,
                    now=now,
                    replace_existing=force_rebuild,
                )
                if succeeded and not recorded_build:
                    unproven[node_id] = "recipe build evidence is invalid"
                if not recorded_build:
                    # A forced rebuild is an attempt to replace a still-valid
                    # receipt.  Keep that receipt authoritative if the new
                    # attempt fails; the availability operation itself reports
                    # the failed attempt and remains retryable.
                    build.state = "succeeded" if force_rebuild else "failed"
                    build.error = (
                        unproven[node_id]
                        if node_id in unproven
                        else node_result.reason[:512]
                        if isinstance(node_result, AgentFailureResult)
                        else "agent build failed"
                    )
                    build.updated_at = now
        elif job.kind == "recipe.install":
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            assert node is not None
            node.state = (
                InstallationNodeState.INSTALLED
                if succeeded
                else InstallationNodeState.FAILED
            )
            if succeeded:
                installed_bytes = (
                    node_result.installed_bytes
                    if isinstance(node_result, AgentInstallResult)
                    else None
                )
                if (
                    not isinstance(installed_bytes, int)
                    or isinstance(installed_bytes, bool)
                    or installed_bytes < 0
                ):
                    # An install whose byte count cannot be proven is not
                    # installed: the rank is failed and the install retried.
                    node.state = _RANK_FAILED
                    unproven[node_id] = "install evidence is invalid"
                else:
                    node.installed_bytes = installed_bytes
            node.updated_at = now
        elif job.kind in {"recipe.start", "recipe.stop"}:
            node = session.scalar(
                select(RunNode).where(
                    RunNode.run_id == owner_id, RunNode.node_id == node_id
                )
            )
            assert node is not None
            start_wire = (
                read_or_rebuild(
                    kind="recipe.start-payload",
                    subject=operation.id,
                    read=lambda: read_stored_model(
                        RecipeStartPayload,
                        canonical_message(operation.payload),
                        from_json=True,
                    ),
                )
                if job.kind == "recipe.start"
                else None
            )
            start_phase = (
                start_wire.phase if isinstance(start_wire, RecipeStartPayload) else None
            )
            if isinstance(start_wire, Residue):
                unproven[node_id] = start_wire.note
                succeeded = False
            if start_phase == "rank-launch":
                if not succeeded:
                    node.state = _RANK_FAILED
                elif node.state != "failed":
                    node.state = RunState.STARTING
                if succeeded:
                    launch_endpoint = _start_endpoint(operation, evidence)
                    if isinstance(launch_endpoint, Residue):
                        node.state = _RANK_FAILED
                        unproven[node_id] = launch_endpoint.note
                node.updated_at = now
            elif start_phase == "collective-readiness":
                if not succeeded:
                    node.state = _RANK_FAILED
                if succeeded:
                    endpoint = _start_endpoint(operation, evidence)
                    recorded = _recorded_result(job.kind, job.result, subject=job.id)
                    launches = (
                        recorded.launch_evidence
                        if isinstance(
                            recorded,
                            (
                                RecipeOperationResult,
                                RecipeOperationProgressResult,
                                RecipeOperationCancellationResult,
                            ),
                        )
                        else None
                    )
                    run_nodes = tuple(
                        session.scalars(
                            select(RunNode).where(RunNode.run_id == owner_id)
                        )
                    )
                    if isinstance(endpoint, Residue):
                        node.state = _RANK_FAILED
                        unproven[node_id] = endpoint.note
                    elif launches is None or any(
                        not isinstance(launches.get(started.node_id), RecipeStartResult)
                        for started in run_nodes
                    ):
                        # Readiness cannot be proven without every rank's launch
                        # evidence: no rank is marked running and the start ends
                        # through its recovery error.
                        node.state = _RANK_FAILED
                        unproven[node_id] = "collective readiness preceded rank launch"
                    else:
                        for started_node in run_nodes:
                            started_node.state = RunState.RUNNING
                            started_node.updated_at = now
                        try:
                            node.endpoint = run_endpoint_document({"url": endpoint})
                        except RecipeExecutionContractError:
                            node.state = _RANK_FAILED
                            unproven[node_id] = (
                                "recipe start endpoint evidence is invalid"
                            )
                node.updated_at = now
            else:
                node.state = (
                    RunState.RUNNING
                    if job.kind == "recipe.start" and succeeded
                    else RunState.STOPPED
                    if succeeded
                    else RunState.FAILED
                )
                if job.kind == "recipe.start" and succeeded:
                    endpoint = _start_endpoint(operation, evidence)
                    if isinstance(endpoint, Residue):
                        node.state = _RANK_FAILED
                        unproven[node_id] = endpoint.note
                    else:
                        try:
                            if endpoint is not None:
                                node.endpoint = run_endpoint_document({"url": endpoint})
                        except RecipeExecutionContractError:
                            node.state = _RANK_FAILED
                            unproven[node_id] = (
                                "recipe start endpoint evidence is invalid"
                            )
                node.updated_at = now
            if (
                job.kind == "recipe.start"
                and succeeded
                and start_phase != "rank-launch"
                and node_id not in unproven
            ):
                run = session.get(RecipeRun, owner_id)
                assert run is not None
                if _run_observes_per_generation(run):
                    run.observation_deadline_at = now + timedelta(
                        seconds=_INITIAL_OBSERVATION_GRACE_SECONDS
                    )
                    for started_node in session.scalars(
                        select(RunNode).where(RunNode.run_id == owner_id)
                    ):
                        started_node.observed_run_generation = None
                        started_node.observation_process_running = None
                        started_node.observation_observed_at = None
                        started_node.observation_endpoint_ready = None
        elif job.kind == "recipe.reconcile":
            installation = session.get(RecipeInstallation, owner_id)
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            if installation is None or node is None:
                # The rows this cleanup reconciles are gone: nothing is applied.
                retire_as_unknown(
                    "recipe.reconciliation",
                    job.id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "the installation or its node row disappeared",
                )
                unproven[node_id] = "reconciliation scope changed"
            authority = _parent_reconciliation(job)
            authority_targets = authority.targets if authority is not None else []
            target = next(
                (item for item in authority_targets if item.node_id == node_id), None
            )
            try:
                expected = read_stored_model(
                    RecipeReconcilePayload,
                    canonical_message(operation.payload),
                    from_json=True,
                )
            except (TypeError, ValueError):
                expected = None
                retire_as_unknown(
                    "recipe.reconciliation",
                    job.id,
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "reconciliation operation payload is invalid",
                )
                unproven.setdefault(
                    node_id, "reconciliation operation payload is invalid"
                )
            if node_id in unproven or installation is None or node is None:
                pass
            elif (
                expected is None
                or authority is None
                or authority.installation_id != owner_id
                or authority.original_plan_digest != expected.plan_digest
                or target is None
                or target.state != "pending"
                or target.rank != node.rank
                or target.role != node.role
                or target.installed_bytes != node.installed_bytes
                or expected.installation_id != owner_id
                or job.targets
                != sorted(
                    item.node_id
                    for item in authority_targets
                    if item.state == "pending"
                )
            ):
                retire_as_unknown(
                    "recipe.reconciliation",
                    job.id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "operation differs from its reviewed authority",
                )
                unproven[node_id] = (
                    "reconciliation operation differs from its reviewed authority"
                )
            else:
                node.state = (
                    InstallationNodeState.UNINSTALLED
                    if succeeded
                    else InstallationNodeState.FAILED
                )
                node.updated_at = now
        elif job.kind == "recipe.uninstall":
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            assert node is not None
            node.state = (
                InstallationNodeState.UNINSTALLED
                if succeeded
                else InstallationNodeState.FAILED
            )
            node.updated_at = now
        recorded_result = _recorded_result(job.kind, job.result, subject=job.id)
        start_payload = (
            start_wire
            if job.kind == "recipe.start" and isinstance(start_wire, RecipeStartPayload)
            else None
        )
        evidence_field = (
            "launch_evidence"
            if start_payload is not None
            and start_payload.phase in {None, "rank-launch"}
            else "node_evidence"
        )
        node_evidence: dict[str, LifecycleNodeResult] = {}
        if isinstance(
            recorded_result,
            (
                RecipeOperationResult,
                RecipeOperationProgressResult,
                RecipeOperationCancellationResult,
            ),
        ):
            existing_evidence = (
                recorded_result.launch_evidence
                if evidence_field == "launch_evidence"
                else recorded_result.node_evidence
            )
            node_evidence = dict(existing_evidence or {})
        observed_evidence = _node_result(job.kind, node_id, evidence)
        if node_id not in unproven and observed_evidence is None:
            unproven[node_id] = "recipe node evidence is invalid"
        if node_id in unproven:
            observed_evidence = LifecycleCodeFailureResult(
                code="recipe.evidence_unproven", detail=unproven[node_id][:512]
            )
        assert observed_evidence is not None
        if node_id in node_evidence and node_evidence[node_id] != observed_evidence:
            # The first receipt of a node stands; a differing replay is residue.
            retire_as_unknown(
                "recipe.operation-evidence",
                job.id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                f"a differing receipt was replayed for {node_id}",
            )
        else:
            node_evidence[node_id] = observed_evidence
        job.result = _validated_result(
            job.kind,
            {
                **(
                    json.loads(canonical_message(recorded_result))
                    if recorded_result is not None
                    else {}
                ),
                evidence_field: node_evidence,
            },
        )
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            )
        )
        # Nodes whose evidence was recorded as unproven (by this or an earlier
        # result of the same operation) count as failed, whatever their order said.
        recorded_for_phases = _recorded_result(job.kind, job.result, subject=job.id)
        marker_nodes = set(unproven)
        if isinstance(
            recorded_for_phases,
            (
                RecipeOperationResult,
                RecipeOperationProgressResult,
                RecipeOperationCancellationResult,
            ),
        ):
            for recorded_field in (
                recorded_for_phases.node_evidence,
                recorded_for_phases.launch_evidence,
            ):
                marker_nodes |= {
                    recorded_node
                    for recorded_node, recorded_item in (recorded_field or {}).items()
                    if isinstance(recorded_item, LifecycleCodeFailureResult)
                    and recorded_item.code == "recipe.evidence_unproven"
                }
        loaded_phases = _stored_phases(job)
        recovery_error: DistributedLifecycleError | None = None
        if isinstance(loaded_phases, Residue):
            # Nothing re-derives how a multi-phase operation was grouped: it ends
            # through its recovery error (its ranks are stopped), never advanced
            # blind and never refused.
            stored_phases: _PhaseGroups = ()
            recovery_error = DistributedLifecycleError(
                "stored operation phases are damaged; the operation was ended"
            )
            for child in children:
                if child.state not in _TERMINAL_JOB_STATES:
                    AgentOperationAdapter(session).record_outcome(
                        child, None, job, Outcome.FAILED, now
                    )
        else:
            stored_phases = loaded_phases
        if stored_phases:
            deadline_failure = _start_deadline_failure(job, now=now)
            phase_error: DistributedLifecycleError | None = None
            if deadline_failure is not None:
                phase_error = DistributedLifecycleError(deadline_failure)
            else:
                try:
                    enforce_recovery_deadline(job.payload, now=now)
                except DistributedLifecycleError as error:
                    phase_error = error
            if phase_error is not None:
                recovery_error = phase_error
                for child in children:
                    if child.state not in _TERMINAL_JOB_STATES:
                        AgentOperationAdapter(session).record_outcome(
                            child, None, job, Outcome.FAILED, now
                        )
            phase_index = _current_phase_index(children, stored_phases)
            if phase_index is not None:
                phase_operations = {
                    operation_id
                    for operation_id, _node_id, _payload in stored_phases[phase_index]
                }
                phase_children = tuple(
                    child for child in children if child.id in phase_operations
                )
                if any(
                    child.state not in _TERMINAL_JOB_STATES for child in phase_children
                ):
                    RecipeOperationAdapter().project(job, now)
                    return cleanup_queued
                if (
                    all(child.state == "succeeded" for child in phase_children)
                    and not any(
                        child.node_id in marker_nodes for child in phase_children
                    )
                    and phase_index + 1 < len(stored_phases)
                    and recovery_error is None
                ):
                    for (
                        next_operation_id,
                        next_node_id,
                        next_payload,
                    ) in stored_phases[phase_index + 1]:
                        self._agent_jobs.enqueue_in_session(
                            session,
                            job.id,
                            next_node_id,
                            job.kind,
                            job.authority_revision,
                            json.loads(canonical_message(next_payload)),
                            operation_id=next_operation_id,
                        )
                    RecipeOperationAdapter().project(job, now)
                    return True
        terminal = all(child.state in _TERMINAL_JOB_STATES for child in children)
        profile_completion_note: str | None = None
        if terminal:
            successful = sorted(
                {child.node_id for child in children if child.state == "succeeded"}
            )
            failed = sorted(
                {child.node_id for child in children if child.state == "failed"}
            )
            # A two-phase start has two children on its owner node: the rank
            # launch (succeeded) and the collective readiness (failed).  The
            # node failed; listing it as successful too makes the aggregate
            # result invalid, so the failed readiness could never be recorded.
            failed = sorted(set(failed) | marker_nodes)
            successful = sorted(set(successful) - set(failed))
            reconciliation_complete = False
            reconciliation_error: str | None = None
            if job.kind == "recipe.reconcile":
                failed = sorted(
                    set(failed)
                    | {
                        child.node_id
                        for child in children
                        if child.state != "succeeded"
                    }
                )
                reconciliation_complete = (
                    not failed
                    and self._reconciliation_job_complete(session, job, children)
                )
                if not failed and not reconciliation_complete:
                    reconciliation_error = (
                        "topology cleanup did not uninstall every rank"
                    )
            if job.kind == "recipe.start" and recovery_error is None:
                late_failure = _start_deadline_failure(job, now=now)
                if late_failure is not None:
                    recovery_error = DistributedLifecycleError(late_failure)
                else:
                    try:
                        enforce_recovery_deadline(job.payload, now=now)
                    except DistributedLifecycleError as error:
                        recovery_error = error
            start_failed = bool(failed) or recovery_error is not None
            if _parent_execution_mode(job) == "profile-jobrun-stop":
                failed = sorted(
                    set(failed)
                    | {
                        child.node_id
                        for child in children
                        if child.state != "succeeded"
                    }
                )
            job_failed = (
                start_failed
                or reconciliation_error is not None
                or (job.kind == "recipe.reconcile" and not reconciliation_complete)
                or (
                    _parent_execution_mode(job) == "profile-jobrun-stop"
                    and bool(failed)
                )
            )
            if _parent_execution_mode(job) == "profile-jobrun-stop" and not job_failed:
                completion = self._complete_profile_jobrun_stop_in_session(
                    session, job, children, now=now
                )
                if isinstance(completion, Residue):
                    # The exact Stop receipts cannot be proven against the
                    # accepted authority: no old JobRun identity is retired, the
                    # Stop ends failed and the profile's own retry answers it.
                    profile_completion_note = completion.note[:500] or "unproven"
                    failed = sorted(set(failed) | {child.node_id for child in children})
                    successful = sorted(set(successful) - set(failed))
                    job_failed = True
                    recovery_error = DistributedLifecycleError(profile_completion_note)
            RecipeOperationAdapter().finish(job, now, failed=job_failed)
            projected_result = _recorded_result(job.kind, job.result, subject=job.id)
            final_result: dict[str, object] = {
                "successful_nodes": successful,
                "failed_nodes": failed,
                "node_evidence": {},
            }
            if isinstance(
                projected_result,
                (
                    RecipeOperationResult,
                    RecipeOperationProgressResult,
                    RecipeOperationCancellationResult,
                ),
            ):
                final_result["node_evidence"] = projected_result.node_evidence or {}
                if projected_result.launch_evidence is not None:
                    final_result["launch_evidence"] = projected_result.launch_evidence
            job.result = _validated_result(job.kind, final_result)
            if recovery_error is not None:
                job.result = _validated_result(
                    job.kind,
                    {
                        **(job.result or {}),
                        "recovery_error": (str(recovery_error) or "unproven")[:512],
                    },
                )
            elif reconciliation_error is not None:
                job.result = _validated_result(
                    job.kind,
                    {
                        **(job.result or {}),
                        "recovery_error": reconciliation_error,
                    },
                )
            if job.kind == "recipe.build.v1":
                # Cancelled attempts returned before terminal aggregation.
                self._release(session, "recipe-build", owner_id, now)
            elif job.kind == "recipe.install":
                installation = session.get(RecipeInstallation, owner_id)
                assert installation is not None
                installation.state = (
                    InstallationState.PARTIAL if failed else InstallationState.INSTALLED
                )
                installation.updated_at = now
            elif job.kind == "recipe.start":
                run = session.get(RecipeRun, owner_id)
                assert run is not None
                if start_failed:
                    run.state = RunState.STOPPING
                    run.route_state = RouteState.WITHDRAWN
                    run.route_error = (
                        f"{recovery_error}; cleanup queued"
                        if recovery_error is not None
                        else "one or more ranks failed to start; cleanup queued"
                    )
                    cleanup_request_id = str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"vonk:recipe-start-cleanup:{job.id}",
                        )
                    )
                    intent = _job_workload_intent(session, job)
                    intent_current = intent is not None and _intent_is_current(
                        session, intent, job.targets
                    )
                    # Whatever the failed Start launched is stopped, even when a
                    # newer intent took the Sparks over meanwhile: the cleanup is
                    # admitted under the intent that owns them now.  Without a
                    # single owning intent the run stays ``lost`` (stoppable), so
                    # the owner of the Sparks still finds it; it is never left
                    # ``failed``, which nothing stops.
                    cleanup_ordinal = (
                        intent
                        if intent_current
                        else _shared_workload_intent(session, job.targets)
                    )
                    if not intent_current:
                        run.state = (
                            RunState.STOPPING if cleanup_ordinal else RunState.LOST
                        )
                        run.route_error = (
                            "start failed after a newer workload intent took over; "
                            + (
                                "cleanup queued"
                                if cleanup_ordinal
                                else "cleanup awaits the owning intent"
                            )
                        )
                    if (
                        not session.scalar(
                            select(Job.id).where(Job.request_id == cleanup_request_id)
                        )
                        and cleanup_ordinal is not None
                    ):
                        stop_nodes = tuple(
                            session.scalars(
                                select(RunNode)
                                .where(RunNode.run_id == owner_id)
                                .order_by(RunNode.rank)
                            )
                        )
                        installation = session.get(
                            RecipeInstallation, run.installation_id
                        )
                        revision = (
                            _active_recipe_revision(
                                session, installation.recipe_revision_id
                            )
                            if installation is not None
                            else None
                        )
                        stop_order = (
                            _topology_order(revision.document, "stop_order")
                            if revision is not None
                            else None
                        )
                        try:
                            exact_stop_payloads: (
                                Mapping[str, RecipeStopPayload] | None
                            ) = durable_run_stop_payloads(
                                session,
                                run,
                                stop_nodes,
                                run_generation=run.run_generation,
                                cancel_pending_start=True,
                                allow_missing_nodes=False,
                            )
                        except RecipeStopAuthorityError:
                            exact_stop_payloads = None
                        if exact_stop_payloads is None:
                            # No exact Start authority to stop from: nothing is
                            # invented.  The run is recorded failed with its
                            # reason, and the run reconciliation owns what the
                            # Sparks still report.
                            retire_as_unknown(
                                "recipe.start-cleanup",
                                run.id,
                                BookkeepingReason.ROW_INCOMPLETE,
                                "failed Start lacks exact cleanup authority",
                            )
                            run.state = RunState.FAILED
                            run.route_error = (
                                "failed recipe Start lacks exact cleanup authority"
                            )
                        else:
                            stop_node_payloads = tuple(
                                (
                                    run_node.node_id,
                                    json.loads(
                                        canonical_message(
                                            exact_stop_payloads[run_node.node_id]
                                        )
                                    ),
                                )
                                for run_node in stop_nodes
                            )
                            self._queue_in_session(
                                session,
                                kind="recipe.stop",
                                owner_kind="run",
                                owner_id=owner_id,
                                plan_digest=run.plan_digest,
                                actor=job.actor,
                                request_id=cleanup_request_id,
                                node_payloads=stop_node_payloads,
                                phases=(
                                    _role_phases(stop_order, stop_node_payloads)
                                    if stop_order is not None
                                    else None
                                ),
                                authority_digest=run.plan_digest.removeprefix(
                                    "sha256:"
                                ),
                                now=now,
                                workload_intent_ordinal=cleanup_ordinal,
                            )
                            cleanup_queued = True
                else:
                    run.state = RunState.RUNNING
                    run.route_state = RouteState.PENDING
                    run.route_error = None
                run.updated_at = now
            elif job.kind == "recipe.stop":
                run = session.get(RecipeRun, owner_id)
                assert run is not None
                stop_parent = _recorded_parent(job)
                partial_scope = (
                    stop_parent.profile_partial_stop
                    if isinstance(stop_parent, RecipeStopParent)
                    else None
                )
                profile_jobrun_partial_targets: set[str] | None = None
                profile_jobrun_partial_nodes: set[str] | None = None
                profile_jobrun_parent: ProfileJobRunStopJob | None = None
                parent_damage: str | None = (
                    stop_parent.note if isinstance(stop_parent, Residue) else None
                )
                if _parent_execution_mode(job) == "profile-jobrun-stop":
                    loaded_parent = _profile_jobrun_parent(job)
                    if isinstance(loaded_parent, Residue):
                        # The accepted scope of this Stop cannot be read: the run
                        # is recorded failed with the reason, never assumed whole.
                        parent_damage = (
                            profile_completion_note
                            or "profile JobRun Stop parent contract is damaged"
                        )
                    else:
                        profile_jobrun_parent = loaded_parent
                        authorization = profile_jobrun_parent.profile_stop_authorization
                        if authorization.missing_node_ids:
                            profile_jobrun_partial_targets = set(
                                authorization.reachable_node_ids
                            )
                            profile_jobrun_partial_nodes = set(
                                authorization.run_node_ids
                            )
                            partial_scope = ProfilePartialStop(
                                target_node_ids=authorization.reachable_node_ids,
                                missing_node_ids=authorization.missing_node_ids,
                            )
                if profile_completion_note is not None and parent_damage is None:
                    parent_damage = profile_completion_note
                recovery = None
                recovery_error: DistributedLifecycleError | None = None
                try:
                    recovery = recovery_start_plan(job.payload, now=now)
                except DistributedLifecycleError as error:
                    recovery_error = error
                recovery_intent = _job_workload_intent(session, job)
                recovery_revision: CatalogDocumentRevision | None = None
                if recovery is not None:
                    recovery_installation = session.get(
                        RecipeInstallation, run.installation_id
                    )
                    recovery_revision = (
                        _active_recipe_revision(
                            session, recovery_installation.recipe_revision_id
                        )
                        if recovery_installation is not None
                        else None
                    )
                    if (
                        recovery_revision is None
                        or recovery_revision.content_digest is None
                    ):
                        # Without the recipe's accepted authority the restart
                        # cannot be queued: the Stop completes as a plain Stop.
                        retire_as_unknown(
                            "recipe.recovery",
                            run.id,
                            BookkeepingReason.ROW_INCOMPLETE,
                            "distributed recovery authority is unavailable",
                        )
                        recovery = None
                if (
                    recovery is not None
                    and recovery_revision is not None
                    and recovery_revision.content_digest is not None
                    and recovery_intent is not None
                    and not failed
                    and _intent_is_current(session, recovery_intent, job.targets)
                ):
                    phases, marker = recovery
                    revision = recovery_revision
                    flattened = tuple(item for phase in phases for item in phase)
                    unique_payloads = tuple(
                        {
                            node_id: (node_id, payload)
                            for node_id, payload in reversed(flattened)
                        }.values()
                    )
                    self._queue_in_session(
                        session,
                        kind="recipe.start",
                        owner_kind="run",
                        owner_id=owner_id,
                        plan_digest=run.plan_digest,
                        actor=job.actor,
                        request_id=str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"vonk:distributed-recovery-start:{job.id}",
                            )
                        ),
                        node_payloads=tuple(
                            (node_id, serialize_json_value(payload))
                            for node_id, payload in unique_payloads
                        ),
                        phases=tuple(
                            tuple(
                                (node_id, serialize_json_value(payload))
                                for node_id, payload in phase
                            )
                            for phase in phases
                        ),
                        authority_digest=revision.content_digest,
                        now=now,
                        workload_intent_ordinal=recovery_intent,
                        job_context={
                            "recovery": serialize_json_value(marker),
                            "start_deadline": marker.deadline,
                        },
                    )
                    run.state = RunState.STARTING
                    run.route_state = RouteState.WITHDRAWN
                    run.route_error = "distributed recovery restarting"
                    run.updated_at = now
                    cleanup_queued = True
                else:
                    partial_targets = (
                        partial_scope.target_node_ids
                        if partial_scope is not None
                        else None
                    )
                    partial_missing = (
                        partial_scope.missing_node_ids
                        if partial_scope is not None
                        else None
                    )
                    full_run_nodes = tuple(
                        session.scalars(
                            select(RunNode).where(RunNode.run_id == owner_id)
                        )
                    )
                    expected_job_targets = (
                        sorted(
                            {
                                target.node_id
                                for target in profile_jobrun_parent.profile_stop_authorization.targets
                            }
                        )
                        if profile_jobrun_partial_targets is not None
                        and profile_jobrun_parent is not None
                        else sorted(partial_targets)
                        if isinstance(partial_targets, list)
                        else None
                    )
                    valid_partial_scope = (
                        isinstance(partial_targets, list)
                        and isinstance(partial_missing, list)
                        and partial_targets == sorted(set(partial_targets))
                        and partial_missing == sorted(set(partial_missing))
                        and partial_targets
                        == sorted(
                            profile_jobrun_partial_targets or set(partial_targets)
                        )
                        and expected_job_targets == sorted(set(job.targets))
                        and (
                            profile_jobrun_partial_nodes is None
                            or profile_jobrun_partial_nodes
                            == {node.node_id for node in full_run_nodes}
                        )
                        and set(partial_targets).isdisjoint(partial_missing)
                        and set(partial_targets) | set(partial_missing)
                        == {node.node_id for node in full_run_nodes}
                        and bool(partial_missing)
                        and all(
                            node.state == RunState.STOPPED
                            for node in full_run_nodes
                            if node.node_id in partial_targets
                        )
                        and all(
                            node.state != RunState.STOPPED
                            for node in full_run_nodes
                            if node.node_id in partial_missing
                        )
                    )
                    partial_success = (
                        valid_partial_scope
                        and not failed
                        and recovery_error is None
                        and parent_damage is None
                    )
                    run.state = (
                        RunState.LOST
                        if partial_success
                        else RunState.FAILED
                        if failed
                        or recovery_error
                        or partial_scope is not None
                        or parent_damage is not None
                        else RunState.STOPPED
                    )
                    run.stopped_at = (
                        now
                        if not failed and not partial_scope and parent_damage is None
                        else None
                    )
                    run.route_state = RouteState.WITHDRAWN
                    if parent_damage is not None:
                        run.route_error = parent_damage[:512]
                    elif partial_success:
                        run.route_error = "incomplete multi-Spark model; missing ranks were not stopped"
                        self._release_node_reservations(
                            session, owner_id, job.targets, now
                        )
                    elif recovery_error is not None:
                        run.route_error = str(recovery_error)[:512]
                    elif partial_scope is not None and not valid_partial_scope:
                        run.route_error = (
                            "profile partial Stop scope no longer matches the full run"
                        )
                    run.updated_at = now
                    if (
                        not failed
                        and recovery_error is None
                        and not partial_scope
                        and parent_damage is None
                    ):
                        self._release(session, "run", owner_id, now)
            elif job.kind == "recipe.uninstall":
                installation = session.get(RecipeInstallation, owner_id)
                assert installation is not None
                installation.state = (
                    InstallationState.FAILED
                    if failed
                    else InstallationState.UNINSTALLED
                )
                installation.updated_at = now
                if not failed:
                    self._release(session, "installation", owner_id, now)
            elif job.kind == "recipe.reconcile":
                installation = session.get(RecipeInstallation, owner_id)
                assert installation is not None
                installation.state = (
                    InstallationState.UNINSTALLED
                    if reconciliation_complete
                    else InstallationState.PARTIAL
                )
                installation.updated_at = now
                if reconciliation_complete:
                    self._release(session, "installation", owner_id, now)
        else:
            RecipeOperationAdapter().project(job, now)
        job.updated_at = now
        return cleanup_queued

    def get(self, operation_id: str) -> RecipeOperationView:
        with self._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or not job.kind.startswith("recipe."):
                raise MissingRecord(operation_id)
            return self._view(job, session=session)

    def cancel(
        self, operation_id: str, *, actor: str, request_id: str, reason: str
    ) -> RecipeOperationView:
        """Durably cancel a queued/running recipe operation."""
        now = self._clock()
        cancellation_reason = _cancel_reason(reason)
        with self._sessions() as session:
            candidate = session.get(Job, operation_id)
            is_build = candidate is not None and candidate.kind == "recipe.build.v1"
        if is_build:
            self._cancel_build(
                operation_id,
                actor=actor,
                request_id=request_id,
                reason=cancellation_reason,
            )
            return self.get(operation_id)
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or not job.kind.startswith("recipe."):
                raise RecipeRequestInvalid("recipe operation is not cancellable")
            bound_ordinal = _parent_intent(job)
            superseded = False
            if (
                job.kind in _WORKLOAD_INTENT_KINDS | {"recipe.job.run.v1"}
                and type(bound_ordinal) is int
            ):
                nodes = tuple(
                    session.scalars(
                        select(AgentNode)
                        .where(AgentNode.node_id.in_(job.targets))
                        .order_by(AgentNode.node_id)
                    )
                )
                superseded = (
                    len(nodes) == len(job.targets)
                    and tuple(node.node_id for node in nodes)
                    == tuple(sorted(set(job.targets)))
                    and any(
                        node.workload_intent_ordinal > bound_ordinal for node in nodes
                    )
                )
            already_invalidated = superseded and (
                job.state in {"cancelled", "failed"}
                or (
                    job.state in job_states.words(LifecycleState.NEEDS_OPERATOR)
                    and _cancel_requested(job)
                )
            )
            if already_invalidated:
                if (
                    not isinstance(actor, str)
                    or not 1 <= len(actor) <= 256
                    or not isinstance(request_id, str)
                ):
                    raise RecipeRequestInvalid(
                        "cancellation request identity is invalid"
                    )
                reused = session.scalar(
                    select(Job.id)
                    .where(
                        Job.id != job.id,
                        or_(
                            Job.request_id == request_id,
                            Job.result["cancel_request_id"].as_string() == request_id,
                        ),
                    )
                    .limit(1)
                )
                if reused is not None:
                    raise RecipeRequestInvalid(
                        "cancellation request key was already used differently",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                try:
                    if str(uuid.UUID(request_id)) != request_id:
                        raise InvalidValue("noncanonical cancellation request key")
                except ValueError as error:
                    raise RecipeRequestInvalid(
                        "cancellation request identity is invalid"
                    ) from error
                logging.getLogger(__name__).info(
                    "ignored cancellation of superseded recipe operation %s", job.id
                )
                return self._view(job)
            if job.state == "cancelled":
                previous = _recorded_result(job.kind, job.result, subject=job.id)
                if isinstance(previous, RecipeOperationCancellationResult) and (
                    previous.cancel_request_id == request_id
                    and previous.reason == cancellation_reason
                    and previous.cancel_actor == actor
                ):
                    return self._view(job)
                raise RecipeRequestInvalid(
                    "cancellation request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
            if job.state not in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.NEEDS_OPERATOR,
            ):
                # A cancel always completes (rule 4): a parent that mirrors an
                # order's wait (the Stop of a one-shot job in doubt, a legacy
                # parked order) accepts it, and the orders end it.
                raise RecipeRequestInvalid("recipe operation is not cancellable")
            previous = _recorded_result(job.kind, job.result, subject=job.id)
            if isinstance(previous, RecipeOperationCancellationResult):
                if (
                    previous.cancel_request_id == request_id
                    and previous.reason == cancellation_reason
                    and previous.cancel_actor == actor
                ):
                    return self._view(job)
                raise RecipeRequestInvalid(
                    "cancellation request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
            children = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == job.id)
                    .with_for_update(of=AgentOperation)
                )
            )
            for child in children:
                if child.state == "queued" and child.current_attempt == 0:
                    AgentOperationAdapter(session).record_outcome(
                        child, None, job, Outcome.CANCELLED, now
                    )
            if any(
                child.state
                in job_states.words(
                    LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
                )
                for child in children
            ):
                job.result = _validated_result(
                    job.kind,
                    {
                        **(
                            json.loads(canonical_message(previous))
                            if previous is not None
                            else {}
                        ),
                        "cancel_requested": True,
                        "cancel_request_id": request_id,
                        "cancel_actor": actor,
                        "cancel_requested_at": _aware(now).isoformat(),
                        "reason": cancellation_reason,
                    },
                )
                job.status_reason = cancellation_reason
                job.updated_at = now
                return self._view(job)
            RecipeOperationAdapter().cancelled(
                job, now, reason=cancellation_reason, keep=False
            )
            job.result = _validated_result(
                job.kind,
                {
                    **(
                        _recorded_result_document(job.kind, job.result, subject=job.id)
                        or {}
                    ),
                    "cancelled": True,
                    "cancel_requested": True,
                    "cancel_request_id": request_id,
                    "cancel_actor": actor,
                    "cancel_requested_at": _aware(now).isoformat(),
                    "reason": cancellation_reason,
                    "recovery": "retry creates a new operation",
                },
            )
            job.updated_at = now
        return self.get(operation_id)

    def _heal_cancelling_build(self, job_id: str) -> None:
        """A legacy build parked ``waiting-for-operator`` by a completion that raced
        its cancel is shown as cancelling again; the sweep below completes it."""
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(Job, job_id, with_for_update={"skip_locked": True})
            if job is not None:
                RecipeOperationAdapter().heal(job, now)

    def reconcile_cancelled_builds(self) -> bool:
        """Cancel unneeded dependent builds and resume exact issued cleanup."""
        with self._sessions() as session:
            eligible = (
                select(Job)
                .where(
                    Job.kind == "recipe.build.v1",
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                    or_(
                        _JsonFlagIsTrue(Job.result, "cancel_requested").is_(True),
                        Job.payload["build_intent"]["kind"].as_string() == "dependency",
                    ),
                )
                .order_by(Job.id)
                .limit(32)
            )
            if self._build_cleanup_cursor is None:
                candidates = tuple(session.scalars(eligible))
            else:
                following = tuple(
                    session.scalars(eligible.where(Job.id > self._build_cleanup_cursor))
                )
                candidates = following + tuple(
                    session.scalars(
                        eligible.where(Job.id <= self._build_cleanup_cursor).limit(
                            32 - len(following)
                        )
                    )
                )
        if candidates:
            self._build_cleanup_cursor = candidates[-1].id
        progressed = False
        for job in candidates:
            try:
                self._heal_cancelling_build(job.id)
                cancellation = build_cancellation(job)
                progressed = (
                    self._cancel_build(
                        job.id,
                        actor=cancellation.cancel_actor
                        if cancellation
                        else "controller:build-dependency",
                        request_id=cancellation.cancel_request_id
                        if cancellation
                        else str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL, f"vonk:unused-build:{job.id}"
                            )
                        ),
                        reason=cancellation.reason
                        if cancellation
                        else "No current accepted consumer needs this build",
                        only_if_unneeded=True,
                    )
                    or progressed
                )
            except (
                BuildConsumerError,
                RecipeOperationConflict,
                KeyError,
                TypeError,
                ValueError,
            ) as error:
                # Contention/malformed ownership on one execution cannot starve
                # other eligible cleanup. Every decision is rechecked in its
                # own short transaction on the next fair scheduler pass.
                logging.getLogger(__name__).info(
                    "build cleanup deferred for %s: %s", job.id, str(error)
                )
        return progressed

    def reconcile_retired_operations(self) -> bool:
        """Resume retired owners through the ordinary exact cleanup path.

        A failed job with typed completed cancellation is the retirement
        handoff. Successful cleanup permits a fresh request through the
        cancellation contract's recovery disposition. Five seconds bounds retry
        traffic per owner; the original job reports the next reconciliation time. No
        execution slot or SQL transaction spans the child admission call.
        """
        now = self._clock()
        interval = timedelta(seconds=5)
        with self._sessions() as session:
            candidates = tuple(
                session.scalars(
                    select(Job)
                    .where(
                        Job.kind.in_(
                            {
                                "recipe.start",
                                "recipe.stop",
                                "recipe.install",
                                "recipe.uninstall",
                            }
                        ),
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.FAILED, LifecycleState.CANCELLED
                            )
                        ),
                        _JsonFlagIsTrue(Job.result, "cancelled"),
                        _JsonFlagIsTrue(Job.result, "cancel_requested"),
                        Job.result["recovery"].as_string().is_(None),
                        Job.updated_at <= now - interval,
                    )
                    .order_by(Job.updated_at, Job.id)
                    # At most one child admission/publication per worker tick,
                    # matching the route worker's one-effect scheduling unit.
                    # Updating oldest-first candidates rotates blocked owners.
                    .limit(1)
                )
            )
        progressed = False
        for job in candidates:
            completed = False
            request_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:retirement-cleanup:{job.id}")
            )
            try:
                owner_id = _parent_identity(job, "owner_id")
                with self._sessions() as probe:
                    ordinal = _job_workload_intent(probe, job)
                kind = (
                    "recipe.stop"
                    if job.kind in {"recipe.start", "recipe.stop"}
                    else "recipe.uninstall"
                )
                owner_kind = "run" if kind == "recipe.stop" else "installation"
                if (
                    owner_id is None
                    or ordinal is None
                    or _parent_identity(job, "owner_kind") != owner_kind
                ):
                    # The retired operation lost its owner or its workload intent:
                    # a cleanup is never issued without them (it would take a
                    # newer intent), and the damage is recorded as unknown.
                    retire_as_unknown(
                        "recipe.retirement",
                        job.id,
                        BookkeepingReason.PERSISTED_STATE_DAMAGED,
                        "retired operation owner or workload intent is not provable",
                    )
                    reason = (
                        "exact cleanup blocked: the retired operation's owner or "
                        "workload intent is not provable"
                    )
                else:
                    reason, completed, advanced = self._retirement_cleanup(
                        job, request_id, kind, owner_kind, owner_id, ordinal
                    )
                    progressed = progressed or advanced
            except (
                RecipeOperationConflict,
                RecipeRouteError,
                ValueError,
                TypeError,
                KeyError,
                OSError,
            ) as error:
                # One unavailable or invalid owner never starves unrelated
                # cleanup. The normal admission path retains its blockers.
                reason = f"exact cleanup blocked: {redact_text(str(error))}"
            with self._sessions.begin() as session:
                stored = session.get(Job, job.id, with_for_update=True)
                if (
                    stored is not None
                    and stored.state
                    in job_states.words(LifecycleState.FAILED, LifecycleState.CANCELLED)
                    and stored.result == job.result
                ):
                    stored.status_reason = (
                        f"operator retired; {reason}"
                        + (
                            ""
                            if completed
                            else f"; next reconciliation at {(now + interval).isoformat()}"
                        )
                    )[:1024]
                    if completed:
                        stored.result = _validated_result(
                            stored.kind,
                            {
                                **(
                                    _recorded_result_document(
                                        stored.kind, stored.result, subject=stored.id
                                    )
                                    or {}
                                ),
                                "recovery": "retry creates a new operation",
                            },
                        )
                    stored.updated_at = now
        return progressed

    def _retirement_cleanup(
        self,
        job: Job,
        request_id: str,
        kind: str,
        owner_kind: str,
        owner_id: str,
        ordinal: int,
    ) -> tuple[str, bool, bool]:
        """Resume one retired owner's exact cleanup: (reason, completed, advanced)."""

        completed = False
        progressed = False
        with self._sessions() as session:
            existing = self._idempotent_in_session(
                session,
                request_id,
                kind,
                None,
                owner_kind=owner_kind,
                owner_id=owner_id,
            )
            if existing is not None:
                latest = session.scalar(
                    select(Job)
                    .where(
                        Job.kind == kind,
                        Job.payload["owner_kind"].as_string() == owner_kind,
                        Job.payload["owner_id"].as_string() == owner_id,
                        Job.payload["workload_intent_ordinal"].as_integer() == ordinal,
                    )
                    .order_by(Job.created_at.desc(), Job.id.desc())
                    .limit(1)
                )
                if latest is not None:
                    existing = self._view(latest, session=session)
            current = _intent_is_current(session, ordinal, job.targets)
            owner = session.get(
                RecipeRun if kind == "recipe.stop" else RecipeInstallation,
                owner_id,
            )
            completed = (
                owner is not None
                and owner.state
                == (
                    RunState.STOPPED
                    if kind == "recipe.stop"
                    else InstallationState.UNINSTALLED
                )
                and session.scalar(
                    select(ResourceReservation.id)
                    .where(
                        ResourceReservation.owner_kind
                        == ("run" if kind == "recipe.stop" else "installation"),
                        ResourceReservation.owner_id == owner_id,
                        ResourceReservation.state == ReservationState.ACTIVE,
                    )
                    .limit(1)
                )
                is None
            )
        if completed:
            reason = "exact cleanup confirmed; capacity released"
        elif not current:
            reason = "newer workload intent owns cleanup; uncertain capacity remains reserved"
        elif existing is not None and existing.state not in job_states.words(
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
            LifecycleState.NEEDS_OPERATOR,
        ):
            reason = f"exact cleanup {existing.id} is {existing.state}; capacity retained until its receipt"
        else:
            if existing is not None:
                request_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{existing.id}:exact-cleanup-retry")
                )
            if kind == "recipe.stop":
                plan = self.preview_stop(owner_id)
                cleanup = self.stop(
                    owner_id,
                    plan_digest=plan.plan_digest,
                    actor=job.actor,
                    request_id=request_id,
                    workload_intent_ordinal=ordinal,
                )
            else:
                plan = self.preview_uninstall(owner_id)
                cleanup = self.uninstall(
                    owner_id,
                    plan_digest=plan.plan_digest,
                    actor=job.actor,
                    request_id=request_id,
                    workload_intent_ordinal=ordinal,
                )
            progressed = True
            reason = f"exact cleanup {cleanup.id} is {cleanup.state}; capacity retained until its receipt"
        return reason, completed, progressed

    def _cancel_build(
        self,
        job_id: str,
        *,
        actor: str,
        request_id: str,
        reason: str,
        only_if_unneeded: bool = False,
    ) -> bool:
        refused: RecipeBuildOwnershipBusy | None = None
        for _attempt in admission_attempts():
            try:
                return self._cancel_current_build(
                    job_id,
                    actor=actor,
                    request_id=request_id,
                    reason=reason,
                    only_if_unneeded=only_if_unneeded,
                )
            except RecipeBuildOwnershipBusy as error:
                refused = error
        assert refused is not None
        raise refused

    def _cancel_current_build(
        self,
        job_id: str,
        *,
        actor: str,
        request_id: str,
        reason: str,
        only_if_unneeded: bool,
    ) -> bool:
        """One cancellation attempt; a lock held by another writer is retried."""

        try:
            return self._cancel_current_build_locked(
                job_id,
                actor=actor,
                request_id=request_id,
                reason=reason,
                only_if_unneeded=only_if_unneeded,
            )
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) not in {
                "55P03",
                "40P01",
                "40001",
                "57014",
            }:
                raise
            raise RecipeBuildOwnershipBusy(
                f"{RecipeBuildCode.CONSUMER_BUSY}: build ownership is changing; retry cancellation",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error

    def _cancel_prebuilt_build(
        self,
        job_id: str,
        *,
        actor: str,
        request_id: str,
        reason: str,
        only_if_unneeded: bool,
    ) -> bool:
        """Cancel a Controller pull; nothing on a Spark needs cleaning up.

        An in-flight pull finds its job no longer running and discards its
        result; its partial file is removed by the importer.
        """
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(Job, job_id, with_for_update={"nowait": True})
            if job is None or job.state == "cancelled":
                return False
            if job.state not in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.NEEDS_OPERATOR,
            ):
                # A cancel always completes: a build that already ended has
                # nothing left to cancel.
                return False
            build_id = _parent_identity(job, "owner_id")
            build = (
                session.get(RecipeBuild, build_id, with_for_update={"nowait": True})
                if build_id is not None
                else None
            )
            if build is None:
                # The build row this pull belongs to is gone: the pull has
                # nothing to import into, so its cancellation is completed
                # on the job alone and the damage is retired as unknown.
                retire_as_unknown(
                    "recipe.build-cancel",
                    job.id,
                    BookkeepingReason.ROW_INCOMPLETE,
                    "the build row of a prebuilt pull is missing",
                )
                cancellation = request_build_cancellation(
                    job,
                    actor=actor,
                    request_id=request_id,
                    reason=reason,
                    now=_aware(now),
                )
                RecipeOperationAdapter().cancelled(job, now)
                job.result = cancellation.model_copy(
                    update={"cancelled": True}
                ).model_dump(mode="json", exclude_none=True)
                return True
            if build_cancellation(job) is None:
                if only_if_unneeded and read_build_intent(job).kind == "independent":
                    return False
                try:
                    consumers = current_build_consumers(session, build)
                except BuildConsumerError as error:
                    raise RecipeRequestInvalid(f"{error.code}: {error}") from error
                if consumers:
                    if only_if_unneeded:
                        return False
                    raise RecipeRequestInvalid(
                        f"{RecipeBuildCode.SHARED_CONSUMERS}: accepted preparation still needs this build; "
                        "cancel its parent intent first"
                    )
            cancellation = request_build_cancellation(
                job, actor=actor, request_id=request_id, reason=reason, now=_aware(now)
            )
            if build.state == "building":
                build.state = (
                    "succeeded" if _parent_force_rebuild(job) is True else "failed"
                )
            build.error = cancellation.reason
            build.updated_at = now
            RecipeOperationAdapter().cancelled(job, now)
            job.result = cancellation.model_copy(update={"cancelled": True}).model_dump(
                mode="json", exclude_none=True
            )
            return True

    def _cancel_current_build_locked(
        self,
        job_id: str,
        *,
        actor: str,
        request_id: str,
        reason: str,
        only_if_unneeded: bool,
    ) -> bool:
        # Use the claim/result lock order: node, parent job, operation, build.
        with self._sessions() as session:
            hinted = session.get(Job, job_id)
            if (
                hinted is not None
                and hinted.kind == "recipe.build.v1"
                and prebuilt_reference(hinted) is not None
            ):
                return self._cancel_prebuilt_build(
                    job_id,
                    actor=actor,
                    request_id=request_id,
                    reason=reason,
                    only_if_unneeded=only_if_unneeded,
                )
            if hinted is None or hinted.kind != "recipe.build.v1":
                return False
            if len(hinted.targets) != 1:
                retire_as_unknown(
                    "recipe.build-cancel",
                    hinted.id,
                    BookkeepingReason.PERSISTED_STATE_DAMAGED,
                    "the build job does not name exactly one builder",
                )
                return False
            node_id = hinted.targets[0]
        now = self._clock()
        with self._sessions.begin() as session:
            node = session.get(AgentNode, node_id, with_for_update={"nowait": True})
            job = session.get(Job, job_id, with_for_update={"nowait": True})
            if node is None or job is None or job.targets != [node_id]:
                retire_as_unknown(
                    "recipe.build-cancel",
                    job_id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the build's builder or job changed under the cancellation",
                )
                return False
            if job.state == "cancelled":
                return False
            if job.state not in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.NEEDS_OPERATOR,
            ):
                return False
            children = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == job.id)
                    .order_by(AgentOperation.id)
                    .with_for_update(of=AgentOperation, nowait=True)
                )
            )
            if len(children) != 1 or children[0].node_id != node_id:
                retire_as_unknown(
                    "recipe.build-cancel",
                    job_id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the build job's order does not match its builder",
                )
                return False
            child = children[0]
            build_owner = _parent_identity(job, "owner_id")
            # A lock held by another writer (SQLSTATE 55P03) reaches
            # ``_cancel_current_build``, which reports it for a bounded retry.
            build = (
                session.get(RecipeBuild, build_owner, with_for_update={"nowait": True})
                if build_owner is not None
                else None
            )
            if build is None or build.builder_node_id != node_id:
                retire_as_unknown(
                    "recipe.build-cancel",
                    job_id,
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "the build row is missing or names another builder",
                )
                return False
            if build_cancellation(job) is None:
                if only_if_unneeded and read_build_intent(job).kind == "independent":
                    return False
                try:
                    consumers = current_build_consumers(session, build)
                except BuildConsumerError as error:
                    raise RecipeRequestInvalid(f"{error.code}: {error}") from error
                if consumers:
                    if only_if_unneeded:
                        return False
                    raise RecipeRequestInvalid(
                        f"{RecipeBuildCode.SHARED_CONSUMERS}: accepted preparation still needs this build; "
                        "cancel its parent intent first"
                    )
            cancellation = request_build_cancellation(
                job, actor=actor, request_id=request_id, reason=reason, now=_aware(now)
            )
            # As with a failed replacement, cancellation keeps the last verified
            # image. Explicit cache removal independently invalidates its bytes.
            if build.state == "building":
                build.state = (
                    "succeeded" if _parent_force_rebuild(job) is True else "failed"
                )
            build.error = cancellation.reason
            build.updated_at = now
            if child.current_attempt == 0:
                AgentOperationAdapter(session).record_outcome(
                    child, None, job, Outcome.CANCELLED, now
                )
                RecipeOperationAdapter().cancelled(job, now)
                job.result = cancellation.model_copy(
                    update={"cancelled": True}
                ).model_dump(mode="json", exclude_none=True)
                self._release_cancelled_build(session, build.id, now)
                return True
            cleanup_key = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:build-cleanup:{child.id}")
            )
            if session.scalar(select(Job.id).where(Job.request_id == cleanup_key)):
                return False
            payload = RecipeBuildCleanupRequest(
                build_id=build.id, operation_id=child.id
            )
            self._queue_in_session(
                session,
                kind="recipe.build.cleanup.v1",
                owner_kind="recipe-build",
                owner_id=build.id,
                plan_digest=build.build_input_sha256,
                actor=actor,
                request_id=cleanup_key,
                node_payloads=((node_id, payload.model_dump(mode="json")),),
                authority_digest=build.build_input_sha256,
                now=now,
                job_context={
                    "build_cancellation": cancellation.model_dump(
                        mode="json", exclude_none=True
                    )
                },
            )
        self._agent_jobs.notify_available()
        return True

    def _release_cancelled_build(
        self, session: Session, build_id: str, now: datetime
    ) -> None:
        remaining = session.scalar(
            select(Job.id)
            .where(
                Job.kind == "recipe.build.v1",
                Job.payload["owner_id"].as_string() == build_id,
                Job.state.in_(
                    job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.NEEDS_OPERATOR,
                    )
                ),
            )
            .limit(1)
        )
        if remaining is None:
            self._release(session, "recipe-build", build_id, now)

    def _stop_logical_job_run(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None,
        profile_target_node_ids: Sequence[str] | None = None,
        profile_application_id: str | None = None,
    ) -> RecipeOperationView | RecipeArtifactJobCancellationPending | None:
        now = self._clock()
        with self._sessions() as session:
            existing_run = session.get(RecipeRun, run_id)
            if existing_run is None or not _run_is_one_shot(session, existing_run):
                return None
            if profile_target_node_ids is not None and profile_application_id is None:
                raise RecipeRequestInvalid(
                    "partial one-shot Stop requires current profile ownership"
                )
        with self._sessions.begin() as session:
            pending_job = session.scalar(
                select(Job).where(Job.request_id == request_id).with_for_update(of=Job)
            )
            if pending_job is not None:
                if _parent_execution_mode(pending_job) == "profile-jobrun-stop":
                    if profile_application_id is None or pending_job.state != "running":
                        raise RecipeRequestInvalid(
                            "profile JobRun Stop request identity changed"
                        )
                    typed_parent = _profile_jobrun_parent(pending_job)
                    if isinstance(typed_parent, Residue):
                        # The Stop this request key already queued is in flight
                        # under its own orders; its damaged parent cannot be
                        # re-checked here, so the request adopts it (its result
                        # projection settles it).
                        return self._view(pending_job, session=session)
                    authorization = typed_parent.profile_stop_authorization
                    if (
                        typed_parent.profile_application_id != profile_application_id
                        or typed_parent.owner_id != run_id
                        or typed_parent.plan_digest != plan_digest
                        or typed_parent.workload_intent_ordinal
                        != workload_intent_ordinal
                    ):
                        raise RecipeRequestInvalid(
                            "profile JobRun Stop request changed its owner",
                            reason=InvalidRequestReason.CONFLICT,
                        )
                    try:
                        validate_profile_stop_owner(session, authorization, now=now)
                        for item in typed_parent.flattened_phase_items:
                            target = next(
                                target
                                for target in authorization.targets
                                if target.node_id == item.node_id
                                and target.stop_payload_sha256
                                == hashlib.sha256(
                                    canonical_message(item.payload)
                                ).hexdigest()
                            )
                            validate_profile_jobrun_stop_target(
                                session,
                                authorization,
                                target,
                                item.payload,
                                stop_parent=pending_job,
                                now=now,
                            )
                    except (ProfileStopAuthorityError, StopIteration) as error:
                        raise RecipeRequestInvalid(
                            "profile JobRun Stop authority is stale",
                            reason=InvalidRequestReason.SUPERSEDED,
                        ) from error
                    return self._view(pending_job, session=session)
                if (
                    pending_job.kind != "recipe.stop"
                    or pending_job.state != "running"
                    or _parent_identity(pending_job, "owner_id") != run_id
                    or _parent_execution_mode(pending_job) != "one-shot-jobs"
                ):
                    raise RecipeRequestInvalid(
                        "request key was already used differently",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                bound = _bound_workload_intent(pending_job)
                if (
                    workload_intent_ordinal is not None
                    and workload_intent_ordinal != bound
                ):
                    raise RecipeRequestInvalid("workload intent was superseded")
                workload_intent_ordinal = bound
            run = session.get(RecipeRun, run_id, with_for_update=True)
            if run is None or not _run_is_one_shot(session, run):
                raise RecipeRequestInvalid(
                    "logical recipe run changed while stopping",
                    reason=InvalidRequestReason.CONFLICT,
                )
            admitted = self._stop_plan_in_session(
                session,
                run_id,
                lock=True,
                profile_target_node_ids=profile_target_node_ids,
            )
            if not admitted.allowed:
                raise RecipeRequestInvalid("stop plan is stale or blocked")
            plan_digest = admitted.plan_digest
            installation = session.get(RecipeInstallation, run.installation_id)
            if installation is None:
                raise RecipeRequestInvalid("recipe installation does not exist")
            revision = _active_recipe_revision(session, installation.recipe_revision_id)
            # The accepted Stop plan carries the run's own authority; the recipe
            # revision's digest is used when it is readable, never required.
            authority_revision = (
                revision.content_digest
                if revision is not None and revision.content_digest
                else admitted.authority_digest.removeprefix("sha256:")
            )
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            targets = admitted.target_node_ids
            target_nodes = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(targets))
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                )
            )
            if tuple(node.node_id for node in target_nodes) != targets:
                raise RecipeRequestInvalid("artifact workload target disappeared")
            if workload_intent_ordinal is None:
                workload_intent_ordinal = (
                    max(node.workload_intent_ordinal for node in target_nodes) + 1
                )
                for node in target_nodes:
                    node.workload_intent_ordinal = workload_intent_ordinal
                AgentJobService.request_superseded_workload_cancellation_in_session(
                    session, targets, workload_intent_ordinal, now
                )
            elif (
                type(workload_intent_ordinal) is not int
                or workload_intent_ordinal < 1
                or any(
                    node.workload_intent_ordinal != workload_intent_ordinal
                    for node in target_nodes
                )
            ):
                raise RecipeRequestInvalid("workload intent was superseded")
            if profile_application_id is not None:
                profile_job = self._profile_jobrun_stop_in_session(
                    session,
                    run=run,
                    nodes=nodes,
                    installation=installation,
                    authority_revision=authority_revision,
                    target_node_ids=targets,
                    stop_plan_digest=admitted.plan_digest,
                    actor=actor,
                    request_id=request_id,
                    profile_application_id=profile_application_id,
                    workload_intent_ordinal=workload_intent_ordinal,
                    now=now,
                )
                if isinstance(profile_job, Residue):
                    # The accepted profile Stop cannot be read yet: nothing is
                    # stopped on a guess, and the profile's retry asks again.
                    raise RecipeRetryLater(
                        "the accepted profile Stop is not readable yet; it is "
                        "recorded and the stop is retried"
                    )
                if profile_job is not None:
                    return self._view(profile_job, session=session)
            payload = {
                "schema_version": 1,
                "owner_kind": "run",
                "owner_id": run_id,
                "plan_digest": plan_digest,
                "execution_mode": "one-shot-jobs",
                "workload_intent_ordinal": workload_intent_ordinal,
            }
            pending = self._one_shot_stop_prerequisite(
                session,
                run_id,
                now,
                target_node_ids=targets if admitted.missing_node_ids else None,
            )
            if pending is not None:
                if pending_job is None:
                    session.add(
                        new_recipe_job(
                            id=str(uuid.uuid4()),
                            request_id=request_id,
                            kind="recipe.stop",
                            state="running",
                            actor=actor,
                            authority_revision=authority_revision,
                            targets=list(targets),
                            payload_digest=hashlib.sha256(
                                canonical_message(payload)
                            ).hexdigest(),
                            payload=payload,
                            result=None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    session.flush()
                return pending
            for node in nodes:
                if node.node_id in targets:
                    node.state = RunState.STOPPED
                    node.updated_at = now
            run.state = RunState.LOST if admitted.missing_node_ids else RunState.STOPPED
            run.route_state = RouteState.WITHDRAWN
            run.stopped_at = None if admitted.missing_node_ids else now
            if admitted.missing_node_ids:
                run.route_error = (
                    "incomplete multi-Spark model; missing ranks were not stopped"
                )
            run.updated_at = now
            if admitted.missing_node_ids:
                self._release_node_reservations(session, run_id, targets, now)
            else:
                self._release(session, "run", run_id, now)
            job = pending_job or new_recipe_job(
                id=str(uuid.uuid4()),
                request_id=request_id,
                kind="recipe.stop",
                state="succeeded",
                actor=actor,
                authority_revision=authority_revision,
                targets=list(targets),
                payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
                payload=payload,
                result=_validated_result("recipe.stop", {"stopped": True}),
                created_at=now,
                updated_at=now,
            )
            if pending_job is not None:
                RecipeOperationAdapter().finish(pending_job, now, failed=False)
                pending_job.result = _validated_result("recipe.stop", {"stopped": True})
                pending_job.updated_at = now
            else:
                session.add(job)
            session.flush()
            return self._view(job)

    def _one_shot_stop_is_pending(self, request_id: str) -> bool:
        with self._sessions() as session:
            job = session.scalar(select(Job).where(Job.request_id == request_id))
            return bool(
                job is not None
                and job.kind == "recipe.stop"
                and job.state == "running"
                and _parent_execution_mode(job) == "one-shot-jobs"
            )

    def _profile_jobrun_stop_is_pending(self, request_id: str) -> bool:
        with self._sessions() as session:
            job = session.scalar(select(Job).where(Job.request_id == request_id))
            if (
                job is None
                or job.kind != "recipe.stop"
                or job.state != "running"
                or _parent_execution_mode(job) != "profile-jobrun-stop"
            ):
                return False
            try:
                ProfileJobRunStopJob.model_validate_parent(job.payload)
            except (TypeError, ValueError):
                return False
            return True

    def _validate_profile_jobrun_stop_child(
        self,
        session: Session,
        job: Job,
        operation: AgentOperation,
        *,
        now: datetime,
        require_current: bool,
    ) -> tuple[ProfileJobRunStopAuthorization, ProfileJobRunStopTarget] | Residue:
        """The accepted authorization and target one Stop child executes.

        A child whose identity cannot be proven against its parent's accepted
        authority is retired as unknown (a :class:`Residue`): nothing is retired
        on that evidence and the profile's own retry answers the Stop.
        """

        def unproven(reason: BookkeepingReason, note: str) -> Residue:
            return retire_as_unknown(
                "recipe.profile-jobrun-stop", operation.id, reason, note
            )

        if (
            job.kind != "recipe.stop"
            or _parent_execution_mode(job) != "profile-jobrun-stop"
            or job.payload_digest
            != hashlib.sha256(canonical_message(job.payload)).hexdigest()
        ):
            return unproven(
                BookkeepingReason.EVIDENCE_MISMATCH,
                "profile JobRun Stop parent is invalid",
            )
        parent = _profile_jobrun_parent(job)
        if isinstance(parent, Residue):
            return parent
        loaded_phases = _stored_phases(job)
        phase_matches = (
            []
            if isinstance(loaded_phases, Residue)
            else [
                payload
                for phase in loaded_phases
                for operation_id, node_id, payload in phase
                if operation_id == operation.id and node_id == operation.node_id
            ]
        )
        try:
            operation_payload = read_stored_model(
                RecipeStopPayload, canonical_message(operation.payload), from_json=True
            )
        except (TypeError, ValueError) as error:
            return unproven(
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"profile JobRun Stop child payload is invalid: {error}",
            )
        stop_digest = hashlib.sha256(canonical_message(operation_payload)).hexdigest()
        targets = [
            target
            for target in parent.profile_stop_authorization.targets
            if target.node_id == operation.node_id
            and target.stop_payload_sha256 == stop_digest
        ]
        if (
            operation.parent_job_id != job.id
            or operation.kind != "recipe.stop"
            or operation.payload_digest
            != hashlib.sha256(canonical_message(operation.payload)).hexdigest()
            or len(phase_matches) != 1
            or canonical_message(phase_matches[0])
            != canonical_message(operation_payload)
            or len(targets) != 1
        ):
            return unproven(
                BookkeepingReason.EVIDENCE_MISMATCH,
                "profile JobRun Stop child identity changed",
            )
        authorization = parent.profile_stop_authorization
        try:
            validate_profile_jobrun_stop_target(
                session,
                authorization,
                targets[0],
                operation_payload,
                operation=operation,
                stop_parent=job,
                now=now,
                require_current=require_current,
            )
        except ProfileStopAuthorityError as error:
            return unproven(
                BookkeepingReason.EVIDENCE_MISMATCH,
                f"profile JobRun Stop authority is stale: {error}",
            )
        return authorization, targets[0]

    def _complete_profile_jobrun_stop_in_session(
        self,
        session: Session,
        job: Job,
        children: Sequence[AgentOperation],
        *,
        now: datetime,
    ) -> Residue | None:
        """Retire old JobRun identities only after every exact Stop receipt.

        Every receipt is checked before anything is retired, so a receipt that
        cannot be proven (a changed payload, an owner no longer provable, a
        missing child or attempt) retires nothing: the whole completion returns a
        :class:`Residue` and the Stop ends failed, to be answered by the
        profile's own retry.  ``None`` means every identity was retired.
        """

        def unproven(reason: BookkeepingReason, note: str) -> Residue:
            return retire_as_unknown("recipe.profile-jobrun-stop", job.id, reason, note)

        if (
            job.payload_digest
            != hashlib.sha256(canonical_message(job.payload)).hexdigest()
        ):
            return unproven(
                BookkeepingReason.EVIDENCE_MISMATCH,
                "profile JobRun Stop payload digest changed",
            )
        parent = _profile_jobrun_parent(job)
        if isinstance(parent, Residue):
            return parent
        authorization = parent.profile_stop_authorization
        try:
            validate_profile_stop_owner(
                session, authorization, now=now, require_current=False
            )
        except ProfileStopAuthorityError as error:
            return unproven(
                BookkeepingReason.EVIDENCE_MISMATCH,
                f"profile JobRun Stop owner is no longer provable: {error}",
            )
        child_by_identity = {
            (
                child.node_id,
                hashlib.sha256(canonical_message(child.payload)).hexdigest(),
            ): child
            for child in children
        }
        if len(child_by_identity) != len(children) or any(
            child.state != "succeeded" for child in children
        ):
            return unproven(
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "profile JobRun Stop lacks complete successful child receipts",
            )
        proven: list[
            tuple[ProfileJobRunStopTarget, ArtifactJob, Job, AgentOperation]
        ] = []
        for target in authorization.targets:
            child = child_by_identity.get((target.node_id, target.stop_payload_sha256))
            if child is None or child.current_attempt < 1:
                return unproven(
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    "profile JobRun Stop lacks an exact issued receipt",
                )
            validated = self._validate_profile_jobrun_stop_child(
                session, job, child, now=now, require_current=False
            )
            if isinstance(validated, Residue):
                return validated
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == child.id,
                    AgentOperationAttempt.attempt == child.current_attempt,
                )
            )
            if attempt is None or attempt.state != "succeeded":
                return unproven(
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    "profile JobRun Stop result does not prove absence",
                )
            artifact = session.get(
                ArtifactJob, target.artifact_job_id, with_for_update=True
            )
            source_job = session.get(Job, target.source_job_id, with_for_update=True)
            source_operation = session.get(
                AgentOperation, target.source_operation_id, with_for_update=True
            )
            if (
                artifact is None
                or source_job is None
                or source_operation is None
                or artifact.run_id != authorization.run_id
                or artifact.operation_id != source_job.id
                or source_operation.parent_job_id != source_job.id
            ):
                return unproven(
                    BookkeepingReason.EVIDENCE_MISMATCH,
                    "stopped JobRun source no longer matches its owner",
                )
            proven.append((target, artifact, source_job, source_operation))
        reachable_nodes = tuple(
            session.scalars(
                select(RunNode)
                .where(
                    RunNode.run_id == authorization.run_id,
                    RunNode.node_id.in_(authorization.reachable_node_ids),
                )
                .with_for_update(of=RunNode)
            )
        )
        if {node.node_id for node in reachable_nodes} != set(
            authorization.reachable_node_ids
        ):
            return unproven(
                BookkeepingReason.EVIDENCE_MISMATCH,
                "profile JobRun Stop reachable run membership changed",
            )
        reason = "runtime stopped by the newer accepted profile intent"
        for _target, artifact, source_job, source_operation in proven:
            # The exact Stop receipt proves the runtime absent: a definite,
            # confirmed cancellation of the job and of its order.
            ArtifactJobAdapter(session).confirm_stopped(artifact, reason, now)
            RecipeOperationAdapter().cancelled(
                source_job, now, reason=reason, keep=False
            )
            AgentOperationAdapter(session).record_outcome(
                source_operation,
                None,
                source_job,
                Outcome.CANCELLED,
                now,
                reason=reason,
            )
        for node in reachable_nodes:
            node.state = RunState.STOPPED
            node.updated_at = now
        return None

    def _profile_jobrun_stop_in_session(
        self,
        session: Session,
        *,
        run: RecipeRun,
        nodes: Sequence[RunNode],
        installation: RecipeInstallation,
        authority_revision: str,
        target_node_ids: Sequence[str],
        stop_plan_digest: str,
        actor: str,
        request_id: str,
        profile_application_id: str,
        workload_intent_ordinal: int,
        now: datetime,
    ) -> Job | Residue | None:
        """Queue exact JobRun runtime Stops under the accepted profile Stop.

        The accepted profile Stop is the authority for the JobRun runtime Stops.
        When its progress, owner or plan cannot be read, nothing is stopped on a
        guess: the damage is retired as unknown (a :class:`Residue`) and the
        caller's retry asks again.  A JobRun whose exact identity does not match
        the run is refused as a security edge, never stopped.
        """
        from .fleet_profiles import _persisted_profile_progress

        application = session.get(FleetProfileApplication, profile_application_id)
        try:
            switch_adapter = (
                _persisted_profile_progress(application).switch_adapter
                if application is not None
                else None
            )
        except ValueError as error:
            return retire_as_unknown(
                "recipe.profile-stop",
                profile_application_id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"current profile Stop progress is invalid: {error}",
            )
        profile_operation = (
            session.get(Job, switch_adapter.active_operation_id)
            if switch_adapter is not None
            and switch_adapter.active_kind == "stop"
            and switch_adapter.active_operation_id is not None
            else None
        )
        if application is None or profile_operation is None:
            return retire_as_unknown(
                "recipe.profile-stop",
                profile_application_id,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "current profile Stop owner is unavailable",
            )
        try:
            profile_parent = read_stored_model(
                RunSwitchJobPayload,
                canonical_message(profile_operation.payload),
                strict=True,
                from_json=True,
            )
            run_switch_plan = profile_parent.plan
            stop_impacts = [
                item for item in run_switch_plan.stops if item.run_id == run.id
            ]
            expected_targets = (
                tuple(run_switch_plan.profile_stop_scope.target_node_ids)
                if run_switch_plan.profile_stop_scope is not None
                else tuple(node.node_id for node in nodes)
            )
            if len(stop_impacts) != 1 or tuple(sorted(target_node_ids)) != tuple(
                sorted(expected_targets)
            ):
                raise InvalidValue(
                    "accepted profile has no unique Stop for this run",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
        except (TypeError, ValueError) as error:
            return retire_as_unknown(
                "recipe.profile-stop",
                profile_application_id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"accepted profile Stop plan is invalid: {error}",
            )

        run_node_by_id = {node.node_id: node for node in nodes}
        reachable_node_ids = set(target_node_ids)
        target_rows: list[
            tuple[ArtifactJob, Job, AgentOperation, RecipeStopPayload]
        ] = []
        unissued_artifacts: list[ArtifactJob] = []
        for artifact in session.scalars(
            select(ArtifactJob)
            .where(ArtifactJob.run_id == run.id)
            .order_by(ArtifactJob.created_at, ArtifactJob.id)
            .with_for_update(of=ArtifactJob)
        ):
            if artifact.operation_id is None:
                if artifact_job_states.preparation_of(artifact) is None:
                    # Submitted without an order: nothing was ever issued for
                    # it, so there is no runtime to stop and it is superseded
                    # like an unissued job.
                    retire_as_unknown(
                        "recipe.artifact-job",
                        artifact.id,
                        BookkeepingReason.ROW_INCOMPLETE,
                        "submitted artifact job has no JobRun identity",
                    )
                unissued_artifacts.append(artifact)
                continue
            source_job = session.get(Job, artifact.operation_id, with_for_update=True)
            source_operations = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == artifact.operation_id)
                    .order_by(AgentOperation.node_id, AgentOperation.id)
                    .with_for_update(of=AgentOperation)
                )
            )
            if (
                source_job is None
                or source_job.kind != "recipe.job.run.v1"
                or _parent_identity(source_job, "owner_kind") != "artifact-job"
                or _parent_identity(source_job, "owner_id") != artifact.id
                or source_job.payload_digest
                != hashlib.sha256(canonical_message(source_job.payload)).hexdigest()
                or len(source_operations) != 1
                or source_operations[0].kind != "recipe.job.run.v1"
                or source_operations[0].parent_job_id != source_job.id
                or source_operations[0].payload_digest
                != hashlib.sha256(
                    canonical_message(source_operations[0].payload)
                ).hexdigest()
            ):
                raise RecipeStopAuthorityRefused(
                    "artifact JobRun physical Stop identity is ambiguous"
                )
            source_operation = source_operations[0]
            if source_operation.node_id not in run_node_by_id:
                raise RecipeStopAuthorityRefused(
                    "artifact JobRun escaped its immutable run membership"
                )
            if source_operation.node_id not in reachable_node_ids:
                continue
            source_node = run_node_by_id.get(source_operation.node_id)
            if source_node is None or source_job.targets != [source_operation.node_id]:
                raise RecipeStopAuthorityRefused(
                    "artifact JobRun escaped its immutable run membership"
                )
            try:
                request = read_stored_model(
                    RecipeJobRunRequest,
                    canonical_message(source_operation.payload),
                    from_json=True,
                )
                stop = stop_payload_from_job_run(
                    request,
                    cancel_pending_start=True,
                )
            except (TypeError, ValueError) as error:
                raise RecipeStopAuthorityRefused(
                    "artifact JobRun request cannot authorize exact Stop"
                ) from error
            if (
                request.job_id != artifact.id
                or request.run_id != run.id
                or request.installation_id != run.installation_id
                or request.recipe_revision_id != installation.recipe_revision_id
                or request.mapping_id != run.mapping_id
                or request.run_generation > run.run_generation
                or request.plan_digest != run.plan_digest
                or (
                    request.compiled_execution_plan.runtime.placement.rank,
                    request.compiled_execution_plan.runtime.placement.role,
                )
                != (source_node.rank, source_node.role)
                or stop.target_runtime_id != artifact.id
                or stop.cancel_pending_start is not True
            ):
                raise RecipeStopAuthorityRefused(
                    "artifact JobRun is not the exact current run effect"
                )
            target_rows.append((artifact, source_job, source_operation, stop))

        targets = [
            ProfileJobRunStopTarget(
                artifact_job_id=artifact.id,
                source_job_id=source_job.id,
                source_operation_id=source_operation.id,
                node_id=source_operation.node_id,
                stop_payload_sha256=hashlib.sha256(canonical_message(stop)).hexdigest(),
            )
            for artifact, source_job, source_operation, stop in target_rows
        ]
        try:
            authorization = ProfileJobRunStopAuthorization(
                schema_version=1,
                profile_application_id=application.id,
                profile_operation_id=profile_operation.id,
                profile_digest=application.profile_digest,
                profile_plan_digest=application.plan_digest,
                profile_step=application.current_step,
                run_id=run.id,
                installation_id=run.installation_id,
                recipe_revision_id=installation.recipe_revision_id,
                mapping_id=run.mapping_id,
                mapping_generation=run.mapping_generation,
                run_generation=run.run_generation,
                plan_digest=run.plan_digest,
                stop_plan_digest=stop_plan_digest,
                workload_intent_ordinal=workload_intent_ordinal,
                run_node_ids=[node.node_id for node in nodes],
                reachable_node_ids=sorted(reachable_node_ids),
                missing_node_ids=(
                    list(run_switch_plan.profile_stop_scope.missing_node_ids)
                    if run_switch_plan.profile_stop_scope is not None
                    else []
                ),
                targets=targets,
                unissued_artifact_job_ids=sorted(
                    artifact.id for artifact in unissued_artifacts
                ),
            )
            validate_profile_stop_owner(session, authorization, now=now)
            for target, row in zip(targets, target_rows, strict=True):
                validate_profile_jobrun_stop_target(
                    session, authorization, target, row[3], now=now
                )
        except (TypeError, ValueError, ProfileStopAuthorityError) as error:
            raise RecipeStopAuthorityRefused(
                "accepted profile does not authorize this exact JobRun Stop: "
                + str(error)[:240]
            ) from error

        for artifact in unissued_artifacts:
            ArtifactJobAdapter(session).settle(
                artifact,
                CancelRequested(None, "superseded before JobRun issuance"),
                now,
                reason="superseded before JobRun issuance by profile intent",
            )
        if not target_rows:
            return None

        grouped: dict[
            str, list[tuple[Mapping[str, object], ProfileJobRunStopTarget]]
        ] = {}
        for (_artifact, _source_job, source_operation, stop), target in zip(
            target_rows, targets, strict=True
        ):
            grouped.setdefault(source_operation.node_id, []).append(
                (json.loads(canonical_message(stop)), target)
            )
        for node_items in grouped.values():
            node_items.sort(
                key=lambda item: (item[1].artifact_job_id, item[1].source_operation_id)
            )
        phase_count = max(len(items) for items in grouped.values())
        phases = tuple(
            tuple(
                (node_id, grouped[node_id][index][0])
                for node_id in sorted(grouped)
                if index < len(grouped[node_id])
            )
            for index in range(phase_count)
        )
        node_payloads = tuple(
            (node_id, grouped[node_id][0][0]) for node_id in sorted(grouped)
        )
        run.state = RunState.STOPPING
        run.route_state = RouteState.WITHDRAWN
        run.route_error = "profile Stop is confirming every exact JobRun runtime"
        run.updated_at = now
        job = self._queue_in_session(
            session,
            kind="recipe.stop",
            owner_kind="run",
            owner_id=run.id,
            plan_digest=stop_plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=node_payloads,
            phases=phases,
            authority_digest=authority_revision,
            now=now,
            workload_intent_ordinal=workload_intent_ordinal,
            job_context={
                "execution_mode": "profile-jobrun-stop",
                "profile_application_id": application.id,
                "profile_operation_id": profile_operation.id,
                "profile_stop_authorization": json.loads(
                    canonical_message(authorization)
                ),
            },
        )
        try:
            ProfileJobRunStopJob.model_validate_parent(job.payload)
        except (TypeError, ValueError) as error:
            raise RecipeStopAuthorityRefused(
                "profile JobRun Stop parent contract is invalid"
            ) from error
        return job

    @staticmethod
    def _one_shot_stop_prerequisite(
        session: Session,
        run_id: str,
        now: datetime,
        *,
        target_node_ids: Sequence[str] | None = None,
    ) -> RecipeArtifactJobCancellationPending | None:
        target_scope = set(target_node_ids) if target_node_ids is not None else None
        # An ended job never blocks a Stop: a job whose stop could not be confirmed
        # ended ``cancelled`` with its residue recorded in its evidence, and a
        # legacy ``failed`` job that lost its lease did too (core rule 4).  Only a
        # job still being cancelled waits, and it completes by itself.
        active = tuple(
            session.scalars(
                select(ArtifactJob)
                .where(
                    ArtifactJob.run_id == run_id,
                    artifact_job_states.sql_preparing_or_live(ArtifactJob),
                )
                .order_by(ArtifactJob.created_at, ArtifactJob.id)
                .with_for_update(of=ArtifactJob)
            )
        )
        pending: RecipeArtifactJobCancellationPending | None = None
        for artifact in active:
            if (
                target_scope is not None
                and not RecipeOperationService._artifact_job_targets(
                    session, artifact, target_scope
                )
            ):
                continue
            if artifact.operation_id is None:
                if artifact_job_states.preparation_of(artifact) is None:
                    # Submitted without an order: nothing was ever issued for it,
                    # so it is superseded like an unissued job.
                    retire_as_unknown(
                        "recipe.artifact-job",
                        artifact.id,
                        BookkeepingReason.ROW_INCOMPLETE,
                        "submitted artifact job has no operation identity",
                    )
                ArtifactJobAdapter(session).settle(
                    artifact,
                    CancelRequested(None, "superseded by newer workload intent"),
                    now,
                    reason="superseded by newer workload intent",
                )
                continue
            parent = session.get(Job, artifact.operation_id, with_for_update=True)
            children = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == artifact.operation_id)
                    .with_for_update(of=AgentOperation)
                )
            )
            if (
                parent is None
                or parent.kind != "recipe.job.run.v1"
                or _parent_identity(parent, "owner_id") != artifact.id
                or len(children) != 1
                or children[0].node_id not in parent.targets
            ):
                raise RecipeRequestInvalid(
                    "artifact job cancellation authority changed",
                    reason=InvalidRequestReason.CONFLICT,
                )
            if (
                parent.state == "cancelled"
                and children[0].state == "cancelled"
                and children[0].current_attempt == 0
            ):
                ArtifactJobAdapter(session).settle(
                    artifact,
                    Reported(
                        Outcome.CANCELLED,
                        effect=Effect.NONE,
                        reason="superseded before agent dispatch",
                    ),
                    now,
                    reason="superseded before agent dispatch",
                )
                continue
            deadline = superseded_cancellation_deadline(parent.result)
            if deadline is None:
                raise RecipeRequestInvalid(
                    "artifact job has no cancellation authority",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            # The order carries the superseding cancel: the job mirrors it
            # (``cancelling``, or ended already) and reports the wait.
            ArtifactJobAdapter(session).project(
                artifact,
                now,
                reason="waiting for exact artifact cancellation receipt",
            )
            if pending is None:
                pending = RecipeArtifactJobCancellationPending(
                    job_id=parent.id,
                    observe_due_at=now + timedelta(seconds=5),
                    observation_deadline=deadline,
                )
        return pending

    @staticmethod
    def _artifact_job_targets(
        session: Session, artifact: ArtifactJob, target_node_ids: set[str]
    ) -> bool:
        if artifact.operation_id is None:
            return False
        parent = session.get(Job, artifact.operation_id)
        return bool(parent is not None and set(parent.targets) & target_node_ids)

    def _stop_plan_in_session(
        self,
        session: Session,
        run_id: str,
        *,
        lock: bool,
        profile_target_node_ids: Sequence[str] | None = None,
    ) -> StopPlan:
        run_statement = select(RecipeRun).where(RecipeRun.id == run_id)
        if lock:
            run_statement = run_statement.with_for_update(of=RecipeRun)
        run = session.scalar(run_statement)
        if run is None:
            raise RecipeRequestInvalid("recipe run does not exist")
        installation_statement = select(RecipeInstallation).where(
            RecipeInstallation.id == run.installation_id
        )
        if lock:
            installation_statement = installation_statement.with_for_update(
                of=RecipeInstallation
            )
        installation = session.scalar(installation_statement)
        if installation is None:
            raise RecipeRequestInvalid("recipe installation does not exist")

        # The Stop plan names the recipe revision by id and needs no more: the run
        # is stopped from its own durable Start authority, so a revision the
        # catalog can no longer serve never blocks the Stop.
        recipe_revision_id = installation.recipe_revision_id
        _active_recipe_revision(session, recipe_revision_id, for_update=lock)

        node_statement = (
            select(RunNode)
            .where(RunNode.run_id == run_id)
            .order_by(RunNode.rank, RunNode.node_id)
            .limit(_MAX_ACTION_NODES + 1)
        )
        if lock:
            node_statement = node_statement.with_for_update(of=RunNode)
        all_nodes = tuple(session.scalars(node_statement))
        nodes = all_nodes[:_MAX_ACTION_NODES]

        reservation_statement = (
            select(ResourceReservation)
            .where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind.in_(_MEMORY_RESERVATION_KINDS),
                ResourceReservation.state == ReservationState.ACTIVE,
            )
            .order_by(
                ResourceReservation.node_id,
                ResourceReservation.kind,
                ResourceReservation.resource_key,
                ResourceReservation.id,
            )
        )
        if lock:
            reservation_statement = reservation_statement.with_for_update(
                of=ResourceReservation
            )
        reservations = tuple(session.scalars(reservation_statement))
        active_by_node = {node.node_id: 0 for node in nodes}
        for reservation in reservations:
            if reservation.node_id in active_by_node:
                active_by_node[reservation.node_id] += reservation.amount_bytes

        full_node_ids = {node.node_id for node in nodes}
        target_node_ids = tuple(
            sorted(
                profile_target_node_ids
                if profile_target_node_ids is not None
                else full_node_ids
            )
        )
        missing_node_ids = tuple(sorted(full_node_ids - set(target_node_ids)))
        profile_scope_exact = profile_target_node_ids is None or (
            len(target_node_ids) == len(set(target_node_ids))
            and bool(target_node_ids)
            and set(target_node_ids) <= full_node_ids
        )
        if profile_target_node_ids is not None and profile_scope_exact:
            target_rows = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(
                        AgentNode.node_id.in_(target_node_ids),
                        AgentNode.revoked_at.is_(None),
                    )
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                    if lock
                    else select(AgentNode)
                    .where(
                        AgentNode.node_id.in_(target_node_ids),
                        AgentNode.revoked_at.is_(None),
                    )
                    .order_by(AgentNode.node_id)
                )
            )
            removed_active_rows = tuple(
                session.scalars(
                    select(AgentNode.node_id).where(
                        AgentNode.node_id.in_(missing_node_ids),
                        AgentNode.revoked_at.is_(None),
                    )
                )
            )
            profile_scope_exact = (
                tuple(row.node_id for row in target_rows) == target_node_ids
                and not removed_active_rows
                and len(full_node_ids) >= 2
                and bool(missing_node_ids)
            )

        actual_identity = {(node.node_id, node.rank, node.role) for node in nodes}
        accepted_run = _run_accepted_ranks(session, run, recipe_revision_id)
        accepted_ranks, accepted_exact = (
            (frozenset[tuple[str, int, str]](), False)
            if isinstance(accepted_run, Residue)
            else accepted_run
        )
        immutable_membership_exact = (
            len(all_nodes) <= _MAX_ACTION_NODES
            and accepted_exact
            and accepted_ranks == actual_identity
            and len(actual_identity) == len(nodes)
            and profile_scope_exact
        )
        reservation_membership_exact = (
            len(reservations) == len(nodes)
            and {reservation.node_id for reservation in reservations}
            == {node.node_id for node in nodes}
            and all(
                reservation.plan_digest == run.plan_digest
                for reservation in reservations
            )
            and all(
                active_by_node[node.node_id] == node.reserved_memory_bytes
                for node in nodes
            )
        )
        reservation_facts = tuple(
            {
                "id": reservation.id,
                "node_id": reservation.node_id,
                "kind": reservation.kind,
                "resource_key": reservation.resource_key,
                "amount_bytes": reservation.amount_bytes,
                "plan_digest": reservation.plan_digest,
                "state": reservation.state,
            }
            for reservation in reservations
        )
        return stop_plan(
            run_id=run.id,
            installation_id=run.installation_id,
            recipe_revision_id=recipe_revision_id,
            alias=run.alias,
            run_state=run.state,
            route_state=run.route_state,
            route_generation=run.route_generation,
            route_digest=run.route_digest,
            authority_digest=run.plan_digest,
            nodes=tuple(
                StopNodeImpact(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state=node.state,
                    reserved_memory_bytes=node.reserved_memory_bytes,
                    active_memory_reservation_bytes=active_by_node[node.node_id],
                )
                for node in nodes
            ),
            target_node_ids=target_node_ids,
            missing_node_ids=missing_node_ids,
            immutable_membership_exact=immutable_membership_exact,
            reservation_membership_exact=reservation_membership_exact,
            reservation_facts=reservation_facts,
        )

    def _uninstall_plan_in_session(
        self,
        session: Session,
        installation_id: str,
        *,
        lock: bool,
        also_removing: Collection[str] = (),
    ) -> UninstallPlan:
        installation_statement = select(RecipeInstallation).where(
            RecipeInstallation.id == installation_id
        )
        if lock:
            installation_statement = installation_statement.with_for_update(
                of=RecipeInstallation
            )
        installation = session.scalar(installation_statement)
        if installation is None:
            raise RecipeRequestInvalid("recipe installation does not exist")

        revision = _uninstall_recipe(session, installation, lock=lock)
        if revision is None:
            raise RecipeRetryLater(
                "recipe revision authority is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        node_statement = (
            select(InstallationNode)
            .where(InstallationNode.installation_id == installation_id)
            .order_by(InstallationNode.rank, InstallationNode.node_id)
            .limit(_MAX_ACTION_NODES + 1)
        )
        if lock:
            node_statement = node_statement.with_for_update(of=InstallationNode)
        all_nodes = tuple(session.scalars(node_statement))
        nodes = all_nodes[:_MAX_ACTION_NODES]

        active_count = int(
            session.scalar(
                select(func.count(RecipeRun.id)).where(
                    RecipeRun.installation_id == installation_id,
                    RecipeRun.state != RunState.STOPPED,
                )
            )
            or 0
        )
        active_statement = (
            select(RecipeRun)
            .where(
                RecipeRun.installation_id == installation_id,
                RecipeRun.state != RunState.STOPPED,
            )
            .order_by(RecipeRun.id)
            .limit(_MAX_ACTIVE_RUNS)
        )
        if lock:
            active_statement = active_statement.with_for_update(of=RecipeRun)
        active_runs = tuple(session.scalars(active_statement))

        operation_statement = (
            select(Job)
            .where(
                Job.kind.in_(("recipe.uninstall", "recipe.reconcile")),
                Job.state.in_(
                    job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.NEEDS_OPERATOR,
                    )
                ),
                Job.payload["owner_id"].as_string() == installation_id,
            )
            .order_by(Job.id)
            .limit(1)
        )
        if lock:
            operation_statement = operation_statement.with_for_update(of=Job)
        active_operation = session.scalar(operation_statement) is not None
        if active_operation and not lock:
            active_reconciliation = session.scalar(
                select(Job.id)
                .where(
                    Job.kind == "recipe.reconcile",
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                    Job.payload["owner_id"].as_string() == installation_id,
                )
                .limit(1)
            )
            active_uninstalls = _active_owned_workload_jobs(
                session, "recipe.uninstall", installation_id
            )
            scope = tuple(sorted(node.node_id for node in nodes))
            if (
                active_reconciliation is None
                and active_uninstalls
                and all(
                    tuple(sorted(job.targets)) == scope
                    and _unissued_workload_children(session, job) is not None
                    for job in active_uninstalls
                )
            ):
                active_operation = False

        actual_identity = {(node.node_id, node.rank, node.role) for node in nodes}
        accepted = _installation_accepted_ranks(session, installation, revision)
        accepted_ranks, accepted_exact = (
            (frozenset[tuple[str, int, str]](), False)
            if isinstance(accepted, Residue)
            else accepted
        )
        immutable_membership_exact = (
            len(all_nodes) <= _MAX_ACTION_NODES
            and accepted_exact
            and accepted_ranks == actual_identity
            and len(actual_identity) == len(nodes)
        )
        # The model that was installed is the installation row's own record; the
        # recipe document names it too.  When they disagree or the document names
        # none, the installed model wins: cleanup never removes a model it cannot
        # prove unused, and an unproven identity keeps the model.
        document_model = _primary_model_identity(revision.document)
        model_content_sha256, model_title = document_model or (
            installation.model_content_sha256 or "",
            "",
        )
        if installation.model_content_sha256 not in {None, model_content_sha256}:
            retire_as_unknown(
                "recipe.installation-model",
                installation.id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "installation model differs from its recipe revision",
            )
            model_content_sha256 = installation.model_content_sha256 or ""
            model_title = model_title or model_content_sha256[:12]
        model_title = model_title or model_content_sha256[:12] or "unknown model"
        node_ids = {node.node_id for node in nodes}
        if _lower_hex_digest(model_content_sha256):
            dependent_recipe_ids_by_node = self._model_dependents_on_nodes(
                session,
                model_content_sha256,
                node_ids,
                exclude_installation_ids={installation.id, *also_removing},
                lock=lock,
            )
        else:
            # No provable model identity: every node keeps its model.
            retire_as_unknown(
                "recipe.installation-model",
                installation.id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                "no provable model identity; the model is kept",
            )
            dependent_recipe_ids_by_node = {
                node_id: (revision.document_id,) for node_id in sorted(node_ids)
            }
        return uninstall_plan(
            installation_id=installation.id,
            recipe_id=revision.document_id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            recipe_content=revision.document,
            original_plan_digest=installation.plan_digest,
            installation_state=installation.state,
            nodes=tuple(
                UninstallNodeImpact(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state=node.state,
                    # Failed or partial ranks have no trustworthy byte count;
                    # a planned rank's count proves it never installed.
                    installed_bytes=(
                        node.installed_bytes
                        if node.state
                        in {
                            InstallationNodeState.INSTALLED,
                            InstallationNodeState.PLANNED,
                        }
                        else None
                    ),
                )
                for node in nodes
            ),
            immutable_membership_exact=immutable_membership_exact,
            active_runs=tuple(
                UninstallActiveRun(
                    run_id=run.id,
                    alias=run.alias,
                    state=run.state,
                    route_state=run.route_state,
                )
                for run in active_runs
            ),
            active_run_count=active_count,
            active_runs_truncated=active_count > _MAX_ACTIVE_RUNS,
            active_operation=active_operation,
            model_content_sha256=model_content_sha256,
            model_title=model_title,
            dependent_recipe_ids_by_node=dependent_recipe_ids_by_node,
        )

    def _model_dependents_on_nodes(
        self,
        session: Session,
        model_content_sha256: str,
        node_ids: set[str],
        *,
        exclude_installation_ids: Collection[str],
        lock: bool,
    ) -> dict[str, tuple[str, ...]]:
        statement = select(RecipeInstallation).where(
            RecipeInstallation.state != InstallationState.UNINSTALLED
        )
        if exclude_installation_ids:
            statement = statement.where(
                RecipeInstallation.id.not_in(tuple(exclude_installation_ids))
            )
        if lock:
            statement = statement.with_for_update(of=RecipeInstallation)
        candidates = tuple(session.scalars(statement))
        dependent_recipe_ids: dict[str, set[str]] = {
            node_id: set() for node_id in node_ids
        }
        if not candidates or not node_ids:
            return {node_id: () for node_id in sorted(node_ids)}
        candidate_ids = [item.id for item in candidates]
        memberships: dict[str, set[str]] = {}
        for installation_id, node_id in session.execute(
            select(InstallationNode.installation_id, InstallationNode.node_id).where(
                InstallationNode.installation_id.in_(candidate_ids),
                InstallationNode.node_id.in_(node_ids),
                InstallationNode.state != InstallationNodeState.UNINSTALLED,
            )
        ):
            memberships.setdefault(installation_id, set()).add(node_id)
        for installation in candidates:
            member_nodes = memberships.get(installation.id)
            if not member_nodes:
                continue
            revision = _active_recipe_revision(session, installation.recipe_revision_id)
            # What this installation needs is known from its recipe revision and,
            # failing that, from the model its own row records.  Whatever cannot
            # be proven unused is a dependent: a model is never removed on a guess.
            recipe_id = (
                revision.document_id
                if revision is not None
                else installation.recipe_revision_id
            )
            identity = (
                _primary_model_identity(revision.document)
                if revision is not None
                else None
            )
            identities = (
                _recipe_model_identities(session, revision.document)
                if revision is not None and identity is not None
                else None
            )
            if identity is None or identities is None:
                retire_as_unknown(
                    "recipe.model-dependents",
                    installation.id,
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    "the installation's model needs are not provable",
                )
                uses = True
            else:
                if installation.model_content_sha256 not in {None, identity[0]}:
                    retire_as_unknown(
                        "recipe.model-dependents",
                        installation.id,
                        BookkeepingReason.EVIDENCE_MISMATCH,
                        "installation model differs from its recipe revision",
                    )
                uses = (
                    any(digest == model_content_sha256 for digest, _title in identities)
                    or installation.model_content_sha256 == model_content_sha256
                )
            if uses:
                for node_id in member_nodes:
                    dependent_recipe_ids[node_id].add(recipe_id)
        return {
            node_id: tuple(sorted(recipe_ids))
            for node_id, recipe_ids in sorted(dependent_recipe_ids.items())
        }

    def _idempotent(
        self,
        request_id: str,
        kind: str,
        plan_digest: str | None,
        *,
        owner_kind: str | None = None,
        owner_id: str | None = None,
        installation_id: str | None = None,
        install_identity: tuple[str, str | None] | None = None,
    ) -> RecipeOperationView | None:
        with self._sessions() as session:
            return self._idempotent_in_session(
                session,
                request_id,
                kind,
                plan_digest,
                owner_kind=owner_kind,
                owner_id=owner_id,
                installation_id=installation_id,
                install_identity=install_identity,
            )

    def _idempotent_in_session(
        self,
        session: Session,
        request_id: str,
        kind: str,
        plan_digest: str | None,
        *,
        owner_kind: str | None = None,
        owner_id: str | None = None,
        installation_id: str | None = None,
        install_identity: tuple[str, str | None] | None = None,
    ) -> RecipeOperationView | None:
        existing = self._idempotent_job_in_session(
            session,
            request_id,
            kind,
            plan_digest,
            owner_kind=owner_kind,
            owner_id=owner_id,
            installation_id=installation_id,
            install_identity=install_identity,
        )
        return self._view(existing) if existing is not None else None

    def _idempotent_job_in_session(
        self,
        session: Session,
        request_id: str,
        kind: str,
        plan_digest: str | None,
        *,
        owner_kind: str | None = None,
        owner_id: str | None = None,
        installation_id: str | None = None,
        install_identity: tuple[str, str | None] | None = None,
    ) -> Job | None:
        existing = session.scalar(select(Job).where(Job.request_id == request_id))
        if existing is None:
            return None
        existing_digest = _parent_identity(existing, "plan_digest")
        if (
            existing.kind != kind
            or (plan_digest is not None and existing_digest != plan_digest)
            or (
                owner_kind is not None
                and _parent_identity(existing, "owner_kind") != owner_kind
            )
            or (
                owner_id is not None
                and _parent_identity(existing, "owner_id") != owner_id
            )
        ):
            raise RecipeRequestInvalid(
                "request key was already used differently",
                reason=InvalidRequestReason.CONFLICT,
            )
        if installation_id is not None:
            if (
                existing.kind not in {"recipe.start", "recipe.job.activate.v1"}
                or _parent_identity(existing, "owner_kind") != "run"
            ):
                raise RecipeRequestInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
            run = session.get(RecipeRun, _parent_identity(existing, "owner_id"))
            if run is None or run.installation_id != installation_id:
                raise RecipeRequestInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
        if install_identity is not None:
            # An install replay returns the old job only for the same mapping
            # and build; a reused key for another mapping is a conflict.
            installation = (
                session.get(RecipeInstallation, _parent_identity(existing, "owner_id"))
                if _parent_identity(existing, "owner_kind") == "installation"
                else None
            )
            if (
                installation is None
                or (installation.mapping_id, installation.recipe_build_id)
                != install_identity
            ):
                raise RecipeRequestInvalid(
                    "request key was already used differently",
                    reason=InvalidRequestReason.CONFLICT,
                )
        return existing

    @staticmethod
    def _admit_workload_intent(
        session: Session,
        *,
        kind: str,
        targets: Sequence[str],
        workload_intent_ordinal: int | None,
        now: datetime,
        supersede: bool = True,
    ) -> int:
        """Bind a request to the workload intent that owns its target Sparks.

        A standalone request takes the next ordinal and fences older orders; a
        child must carry its parent's exact, still-current ordinal. An
        unattended request (``supersede=False``) takes no new intent: it joins
        the one every target Spark already shares, so it cancels nothing and
        leaves recovery of a workload on those Sparks untouched, and any later
        load supersedes it.
        """

        try:
            target_nodes = tuple(
                lock_admission_rows(
                    session,
                    (
                        AdmissionRowLock(
                            "workload-target-nodes",
                            AgentNode,
                            select(AgentNode).where(AgentNode.node_id.in_(targets)),
                        ),
                    ),
                ).get("workload-target-nodes", ())
            )
        except AdmissionLockBusy as error:
            if kind == "recipe.install":
                raise InstallAdmissionBusy(
                    InstallAdmissionCode.CAPACITY_BUSY,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            raise RunAdmissionBusy("run capacity writer is busy") from error
        if tuple(node.node_id for node in target_nodes) != tuple(targets):
            raise RecipeRequestInvalid("workload intent target disappeared")
        if workload_intent_ordinal is None and not supersede:
            shared = {node.workload_intent_ordinal for node in target_nodes}
            if len(shared) != 1 or min(shared) < 1:
                raise RecipeRetryLater(
                    "workload intent differs across the target Sparks"
                )
            return shared.pop()
        if workload_intent_ordinal is None:
            next_ordinal = (
                max(node.workload_intent_ordinal for node in target_nodes) + 1
            )
            for node in target_nodes:
                node.workload_intent_ordinal = next_ordinal
            try:
                AgentJobService.request_superseded_workload_cancellation_in_session(
                    session, targets, next_ordinal, now
                )
            except AdmissionLockBusy as error:
                if kind == "recipe.install":
                    raise InstallAdmissionBusy(
                        InstallAdmissionCode.CAPACITY_BUSY,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    ) from error
                raise RunAdmissionBusy("run capacity writer is busy") from error
            return next_ordinal
        if (
            type(workload_intent_ordinal) is not int
            or workload_intent_ordinal < 1
            or any(
                node.workload_intent_ordinal != workload_intent_ordinal
                for node in target_nodes
            )
        ):
            raise RecipeRequestInvalid("workload intent was superseded")
        return workload_intent_ordinal

    def _queue_in_session(
        self,
        session: Session,
        *,
        kind: str,
        owner_kind: str,
        owner_id: str,
        plan_digest: str,
        actor: str,
        request_id: str,
        node_payloads: Sequence[tuple[str, Mapping[str, object]]],
        authority_digest: str,
        now: datetime,
        phases: Sequence[Sequence[tuple[str, Mapping[str, object]]]] | None = None,
        job_context: Mapping[str, object] | None = None,
        workload_intent_ordinal: int | None = None,
        unattended_guard: Callable[[Session], None] | None = None,
    ) -> Job:
        if not node_payloads:
            raise RecipeRequestInvalid("operation group has no target nodes")
        try:
            payload_model = _RECIPE_WIRE_PAYLOAD_MODELS[kind]
        except KeyError:
            payload_model = None
        if payload_model is not None:
            try:
                for _node_id, payload in node_payloads:
                    payload_model.model_validate_json(canonical_message(payload))
            except Exception as error:
                raise RecipeRequestInvalid(
                    f"{kind} payload does not satisfy its wire schema"
                ) from error
        job_id = str(uuid.uuid4())
        requested_phase_groups = (
            tuple(tuple(group) for group in phases)
            if phases is not None
            else (tuple(node_payloads),)
        )
        if not requested_phase_groups or any(
            not group for group in requested_phase_groups
        ):
            raise RecipeRequestInvalid("operation phases are invalid")
        flattened = tuple(item for group in requested_phase_groups for item in group)
        if {node_id for node_id, _payload in flattened} != {
            node_id for node_id, _payload in node_payloads
        } or len({node_id for node_id, _payload in node_payloads}) != len(
            node_payloads
        ):
            raise RecipeRequestInvalid("operation phases do not match target nodes")
        phase_groups = tuple(
            tuple((str(uuid.uuid4()), node_id, payload) for node_id, payload in group)
            for group in requested_phase_groups
        )
        targets = sorted(
            {node_id for _operation_id, node_id, _payload in sum(phase_groups, ())}
        )
        try:
            acquire_admission_keys(
                session,
                (
                    job_request_key(request_id),
                    *(node_admission_key(node_id) for node_id in targets),
                ),
                holder="recipe-operation",
            )
        except AdmissionLockBusy as error:
            if kind == "recipe.install":
                raise InstallAdmissionBusy(
                    InstallAdmissionCode.CAPACITY_BUSY,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            raise RunAdmissionBusy("run capacity writer is busy") from error
        existing = self._idempotent_job_in_session(
            session,
            request_id,
            kind,
            plan_digest,
            owner_kind=owner_kind,
            owner_id=owner_id,
        )
        if existing is not None:
            return existing
        if kind in _WORKLOAD_INTENT_KINDS:
            # Only a standalone request admits a new intent. A child carries
            # its parent's exact ordinal and may not capture newer authority.
            workload_intent_ordinal = self._admit_workload_intent(
                session,
                kind=kind,
                targets=targets,
                workload_intent_ordinal=workload_intent_ordinal,
                now=now,
                supersede=unattended_guard is None,
            )
            if unattended_guard is not None:
                # The target Spark rows are locked now, so a load admitted
                # before this point is visible to the guard and one admitted
                # after it waits for this transaction.
                unattended_guard(session)
        job_payload: dict[str, object] = {
            "schema_version": 1,
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "plan_digest": plan_digest,
        }
        if workload_intent_ordinal is not None:
            if type(workload_intent_ordinal) is not int or workload_intent_ordinal < 1:
                raise RecipeRequestInvalid("workload intent ordinal is invalid")
            job_payload["workload_intent_ordinal"] = workload_intent_ordinal
        if phases is not None:
            job_payload["phases"] = [
                [
                    {
                        "operation_id": operation_id,
                        "node_id": node_id,
                        "payload": dict(payload),
                    }
                    for operation_id, node_id, payload in group
                ]
                for group in phase_groups
            ]
        if job_context is not None:
            if set(job_context) & set(job_payload):
                raise RecipeRequestInvalid("operation context is invalid")
            job_payload.update(json.loads(canonical_message(job_context)))
        if kind in {"recipe.install", "recipe.start"}:
            try:
                for _node_id, payload in flattened:
                    # A missing or non-mapping plan is refused by the validator
                    # itself (``CompiledExecutionPlanError``).
                    validate_compiled_launch_payload(
                        payload.get("compiled_execution_plan")
                    )
            except (CompiledExecutionPlanError, TypeError, ValueError) as error:
                raise RecipeRequestInvalid(
                    f"compiled execution plan is invalid: {error}"
                ) from None
            if len(canonical_message(job_payload)) > MAX_COMPILED_EXECUTION_PLAN_BYTES:
                raise RecipeRequestInvalid("recipe operation job payload is too large")
        job = new_recipe_job(
            id=job_id,
            request_id=request_id,
            kind=kind,
            state="running",
            actor=actor,
            authority_revision=authority_digest.removeprefix("sha256:"),
            targets=targets,
            payload_digest=hashlib.sha256(canonical_message(job_payload)).hexdigest(),
            payload=job_payload,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        try:
            session.flush()
            for operation_id, node_id, payload in phase_groups[0]:
                self._agent_jobs.enqueue_in_session(
                    session,
                    job_id,
                    node_id,
                    kind,
                    authority_digest.removeprefix("sha256:"),
                    payload,
                    operation_id=operation_id,
                )
        except AdmissionLockBusy as error:
            if kind == "recipe.install":
                raise InstallAdmissionBusy(
                    InstallAdmissionCode.CAPACITY_BUSY,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            raise RunAdmissionBusy("run capacity writer is busy") from error
        except OperationalError as error:
            if not is_admission_contention(error):
                raise
            if kind == "recipe.install":
                raise InstallAdmissionBusy(
                    InstallAdmissionCode.CAPACITY_BUSY,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                ) from error
            raise RunAdmissionBusy("run capacity writer is busy") from error
        return job

    def _view(self, job: Job, *, session: Session | None = None) -> RecipeOperationView:
        try:
            validate_recipe_lifecycle_terminal(job.kind, job.state, job.result)
        except (TypeError, ValueError) as error:
            # A terminal operation whose receipt does not hold is shown without
            # it: the receipt is evidence, never a reason to hide the operation.
            retire_as_unknown(
                "recipe.operation-result",
                job.id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                f"{type(error).__name__}: {error}",
            )
        waiting_children = (
            tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(
                        AgentOperation.parent_job_id == job.id,
                        AgentOperation.state.in_(agent_operation_states.PARKED),
                    )
                    .order_by(AgentOperation.node_id, AgentOperation.id)
                )
            )
            if session is not None
            and job.state
            in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.NEEDS_OPERATOR,
            )
            else ()
        )
        retry_due_at = min(
            (
                due
                for due in (retry_scheduled(child) for child in waiting_children)
                if due is not None
            ),
            default=None,
        )
        child_reason = next(
            (child.status_reason for child in waiting_children if child.status_reason),
            None,
        )
        return RecipeOperationView(
            id=job.id,
            kind=job.kind,
            owner_id=_parent_identity(job, "owner_id") or "",
            state=job.state,
            plan_digest=_parent_identity(job, "plan_digest") or "",
            nodes=tuple(job.targets),
            lifecycle_result=_recorded_result(job.kind, job.result, subject=job.id),
            retry_due_at=retry_due_at,
            status_reason=child_reason or job.status_reason,
        )

    @staticmethod
    def _release(
        session: Session, owner_kind: str, owner_id: str, now: datetime
    ) -> None:
        release_owned_reservations_in_session(session, owner_kind, owner_id, now)

    @staticmethod
    def _release_node_reservations(
        session: Session,
        run_id: str,
        node_ids: Sequence[str],
        now: datetime,
    ) -> None:
        """Release only the ranks whose Stop receipts are proven."""

        if not node_ids:
            return
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.node_id.in_(node_ids),
                ResourceReservation.state == ReservationState.ACTIVE,
            )
        ):
            reservation.state = ReservationState.RELEASED
            reservation.released_at = now


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
    "recipe.install": TypeAdapter(RecipeInstallParent),
    "recipe.start": TypeAdapter(RecipeStartParent),
    "recipe.stop": TypeAdapter(RecipeStopParent | ProfileJobRunStopJob),
    "recipe.uninstall": TypeAdapter(RecipeUninstallParent),
    "recipe.reconcile": TypeAdapter(RecipeReconcileParent),
    "recipe.job.run.v1": TypeAdapter(RecipeJobRunParent),
    "recipe.job.activate.v1": TypeAdapter(RecipeJobActivateParent),
    "recipe.build.v1": TypeAdapter(RecipeBuildParent),
    "recipe.build.cleanup.v1": TypeAdapter(RecipeBuildCleanupParent),
}


def _parse_recipe_parent(job: Job) -> RecipeParent:
    parent = _RECIPE_PARENT_READERS[job.kind].validate_json(
        canonical_message(job.payload)
    )
    if isinstance(parent, ProfileJobRunStopJob):
        return ProfileJobRunStopJob.model_validate_parent(job.payload)
    return parent


def _recorded_parent(job: Job) -> RecipeParent | Residue:
    return read_or_rebuild(
        kind="recipe.operation-parent",
        subject=job.id,
        read=lambda: _parse_recipe_parent(job),
    )


def _parent_identity(
    job: Job, key: Literal["owner_id", "owner_kind", "plan_digest"]
) -> str | None:
    """Identity from the canonical parent, or unknown for damaged bookkeeping."""
    parent = _recorded_parent(job)
    if isinstance(parent, Residue):
        # Phase history is bookkeeping. Rebuild only the exact affected owner
        # from relational child rows with current canonical payloads; this does
        # not restore review digests, executable phases, or security authority.
        if key != "owner_id":
            return None
        session = object_session(job)
        reader = _RECIPE_WIRE_PAYLOAD_MODELS.get(job.kind)
        if session is None or reader is None:
            return None
        owners: set[str] = set()
        try:
            for child in session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            ):
                if child.kind != job.kind or child.node_id not in job.targets:
                    return None
                wire = reader.model_validate_json(canonical_message(child.payload))
                if isinstance(wire, (RecipeStartPayload, RecipeStopPayload)):
                    owners.add(wire.run_id)
                elif isinstance(
                    wire,
                    (
                        RecipeInstallPayload,
                        RecipeUninstallPayload,
                        RecipeReconcilePayload,
                    ),
                ):
                    owners.add(wire.installation_id)
                elif isinstance(wire, RecipeBuildCleanupRequest):
                    owners.add(wire.build_id)
                else:
                    return None
        except (TypeError, ValueError):
            return None
        return owners.pop() if len(owners) == 1 else None
    if key == "owner_id":
        return parent.owner_id
    return parent.owner_kind if key == "owner_kind" else parent.plan_digest


def _parent_intent(job: Job) -> int | None:
    parent = _recorded_parent(job)
    return None if isinstance(parent, Residue) else parent.workload_intent_ordinal


def _parent_execution_mode(
    job: Job,
) -> Literal["one-shot-jobs", "profile-jobrun-stop"] | None:
    parent = _recorded_parent(job)
    return None if isinstance(parent, Residue) else parent.execution_mode


def _parent_force_rebuild(job: Job) -> bool:
    parent = _recorded_parent(job)
    return isinstance(parent, RecipeBuildParent) and parent.force_rebuild


def _parent_recovery(job: Job) -> DistributedRecoveryMarker | None:
    parent = _recorded_parent(job)
    return (
        parent.recovery
        if isinstance(parent, (RecipeStartParent, RecipeStopParent))
        else None
    )


def _parent_reconciliation(job: Job) -> RunSwitchReconciliationAuthority | None:
    parent = _recorded_parent(job)
    return (
        parent.reconciliation_authority
        if isinstance(parent, RecipeReconcileParent)
        else None
    )


def _cancel_requested(job: Job) -> bool:
    return isinstance(
        _recorded_result(job.kind, job.result, subject=job.id),
        RecipeOperationCancellationResult,
    )


def _lower_hex_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _catalog_recipe(document: object) -> RecipeDefinition | None:
    if isinstance(document, RecipeDefinition):
        return document
    try:
        return RecipeDefinition.model_validate_json(
            canonical_message(document), extra="ignore"
        )
    except (TypeError, ValueError):
        return None


def _primary_model_identity(document: object) -> tuple[str, str] | None:
    """The canonical primary model identity, or unknown for unreadable content."""
    recipe = _catalog_recipe(document)
    if recipe is None or not recipe.models:
        return None
    model = recipe.models[0].model
    return model.content_sha256, f"{model.publisher}/{model.slug}"


def _recipe_model_identities(
    session: Session,
    document: object,
) -> tuple[tuple[str, str], ...] | None:
    """Every model a recipe needs (its own and the dependencies of those).

    ``None`` when the closure cannot be read from the catalog: the caller then
    keeps whatever it cannot prove unused.  A model reached twice is one model.
    """

    recipe = _catalog_recipe(document)
    if recipe is None or not recipe.models:
        return None
    result: list[tuple[str, str]] = []
    pending = [selection.model for selection in recipe.models]
    seen: set[tuple[str, str, str]] = set()
    while pending:
        reference = pending.pop(0)
        key = (reference.publisher, reference.slug, reference.content_sha256)
        if key in seen:
            continue
        seen.add(key)
        revision = session.scalar(
            select(CatalogDocumentRevision)
            .where(
                CatalogDocumentRevision.kind == "model",
                CatalogDocumentRevision.publisher == reference.publisher,
                CatalogDocumentRevision.slug == reference.slug,
                CatalogDocumentRevision.content_digest == reference.content_sha256,
                CatalogDocumentRevision.state == "active",
            )
            .limit(1)
        )
        if revision is None or not isinstance(revision.document, Mapping):
            return None
        try:
            model = read_model(revision.document)
        except (TypeError, ValueError):
            return None
        result.append(
            (reference.content_sha256, f"{reference.publisher}/{reference.slug}")
        )
        pending.extend(model.dependencies)
    unique = {digest: (digest, title) for digest, title in result}
    return tuple(unique.values())


def _topology_order(
    document: object, key: Literal["start_order", "stop_order"]
) -> tuple[str, ...] | None:
    """The recipe's role order, or ``None`` when its topology cannot be read."""

    try:
        topology = recipe_topology(document)
    except Exception:  # noqa: BLE001 - any unreadable topology is "no order"
        return None
    return tuple(topology.start_order if key == "start_order" else topology.stop_order)


def _canonical_distributed_readiness(document: object) -> bool:
    recipe = _catalog_recipe(document)
    if recipe is None:
        raise RecipeRequestInvalid("distributed readiness interface is invalid")
    try:
        readiness = canonical_distributed_readiness(
            topology=recipe.topology,
            interfaces=tuple(
                serialize_json_value(interface) for interface in recipe.interfaces
            ),
        )
    except DistributedLifecycleError as error:
        raise RecipeRequestInvalid(str(error)) from error
    return readiness is not None


def _start_deadline_failure(job: Job, *, now: datetime) -> str | None:
    """The accepted start budget, with damaged bookkeeping ended by recovery."""
    if job.kind != "recipe.start":
        return None
    parent = _recorded_parent(job)
    if not isinstance(parent, RecipeStartParent):
        return "distributed start deadline is invalid"
    if parent.start_deadline is not None and _aware(now) >= _aware(
        parent.start_deadline
    ):
        return "distributed start deadline elapsed"
    return None


def _role_phases(
    order: Sequence[str],
    node_payloads: Sequence[tuple[str, Mapping[str, object]]],
) -> tuple[tuple[tuple[str, Mapping[str, object]], ...], ...] | None:
    """Group node payloads into role phases; ``None`` when the roles do not
    match the recipe's order (the caller decides whether to order or refuse)."""

    by_role: dict[str, list[tuple[str, Mapping[str, object]]]] = {}
    reader = TypeAdapter(RecipeStartPayload | RecipeStopPayload)
    for node_id, payload in node_payloads:
        try:
            wire = reader.validate_json(canonical_message(payload))
        except (TypeError, ValueError):
            return None
        role = (
            wire.role
            if isinstance(wire, RecipeStopPayload)
            else wire.compiled_execution_plan.runtime.placement.role
        )
        by_role.setdefault(role, []).append((node_id, dict(payload)))
    if set(by_role) != set(order) or len(set(order)) != len(order):
        return None
    return tuple(
        tuple(sorted(by_role[role], key=lambda item: item[0])) for role in order
    )


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


def _stored_phases(job: Job) -> _PhaseGroups | Residue:
    """Read the canonical parent and its typed phase payloads; damage is unknown."""

    def read() -> _PhaseGroups | Damaged:
        parent = _parse_recipe_parent(job)
        if isinstance(parent, RecipeJobActivateParent):
            return ()
        if parent.phases is None:
            return ()
        if not parent.phases or any(not phase for phase in parent.phases):
            return Damaged("stored operation phases are invalid")
        seen_operations: set[str] = set()
        groups: list[tuple[tuple[str, str, RecipeWirePayload], ...]] = []
        for phase in parent.phases:
            group: list[tuple[str, str, RecipeWirePayload]] = []
            for item in phase:
                if item.operation_id in seen_operations:
                    return Damaged("stored operation phases are invalid")
                seen_operations.add(item.operation_id)
                group.append((item.operation_id, item.node_id, item.payload))
            groups.append(tuple(group))
        return tuple(groups)

    return read_or_rebuild(kind="recipe.operation-phases", subject=job.id, read=read)


def _current_phase_index(
    children: Sequence[AgentOperation],
    phases: _PhaseGroups,
) -> int | None:
    child_operations = {child.id for child in children}
    for index in range(len(phases) - 1, -1, -1):
        phase_operations = {
            operation_id for operation_id, _node_id, _payload in phases[index]
        }
        if phase_operations <= child_operations:
            return index
    return None


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
        parse_stored_build_plan(build.plan)
        parse_stored_build_policy(build.policy_report)
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
    build.state = "succeeded"
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
            RecipeStartPayload, canonical_message(operation.payload), from_json=True
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


__all__ = [
    "RecipeOperationConflict",
    "RecipeOperationService",
    "RecipeOperationView",
    "RecipeRunRankStatus",
    "RecipeRunStatus",
    "prepare_exact_recipe_run_observation_nodes",
]
