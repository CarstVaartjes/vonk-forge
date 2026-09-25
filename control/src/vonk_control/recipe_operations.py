"""Digest-bound orchestration for local recipe installation and execution."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentFailureKind,
    AgentFailureResult,
    RecipeBuildCleanupEvidence,
    RecipeBuildCleanupRequest,
    RecipeInstallPayload,
    RecipeReconcilePayload,
    RecipeReconcileResult,
    RecipeStartPayload,
    RecipeStopPayload,
    RecipeUninstallPayload,
    canonical_message,
    format_model_identity,
    parse_recipe_operation_result,
    validate_result_for_operation,
)
from vonk_agent_protocol import (
    AgentOperation as ProtocolAgentOperation,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from .admission_locking import (
    AdmissionLockBusy,
    AdmissionRowLock,
    acquire_admission_keys,
    is_admission_contention,
    job_request_key,
    lock_admission_rows,
    node_admission_key,
)
from .agent_jobs import (
    EXACT_LIFECYCLE_RESUME_CAPABILITY,
    RECIPE_RECONCILE_FEATURE_CAPABILITY,
    AgentJobService,
    _JsonFlagIsTrue,
    release_owned_reservations_in_session,
    superseded_cancellation_deadline,
)
from .cluster_mappings import ClusterMappingPlan, ClusterMappingService
from .compiled_execution_plan import (
    MAX_COMPILED_EXECUTION_PLAN_BYTES,
    CompiledExecutionPlanError,
    validate_compiled_launch_payload,
)
from .distributed_lifecycle import (
    DistributedLifecycleError,
    canonical_distributed_readiness,
)
from .distributed_recovery import enforce_recovery_deadline, recovery_start_plan
from .install_admission import (
    InstallAdmissionBusy,
    InstallAdmissionService,
    InstallPlan,
    InstallPreflightExpired,
    installation_plan_digest_from_stored_document,
    refreshable_preflight_is_the_only_blocker,
)
from .logging import redact_text
from .models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AgentPresence,
    ArtifactJob,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    Job,
    NodeArtifact,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
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
    build_plan_document,
    installation_plan_document,
    parse_stored_build_plan,
    parse_stored_build_policy,
    parse_stored_installation_plan,
    parse_stored_run_plan,
    run_endpoint_document,
    run_plan_document,
)
from .recipe_lifecycle_contract import (
    RecipeOperationCancellationResult,
    RecipeOperationResult,
    parse_recipe_lifecycle_result,
    validate_recipe_lifecycle_terminal,
)
from .recipe_routes import (
    RecipeRouteError,
    RecipeRouteService,
    route_publication_transaction,
)
from .recipe_runtime_specs import recipe_topology
from .recipe_start_payloads import (
    RecipeStartPayloadError,
    RecipeStartPlacement,
    build_recipe_start_payload,
    validate_distributed_start_timeout_seconds,
)
from .recovery_policy import FailureKind
from .run_admission import RunAdmissionBusy, RunAdmissionService, RunNodePlan, RunPlan
from .source_policy import SourcePolicyReport

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


class RecipeReconciliationBlocked(RecipeOperationConflict):
    """A corrupt installation lacks exact, current cleanup authority."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class InstallationReconciliationTarget:
    """Exact source install request and local spec identity for one node."""

    node_id: str
    rank: int
    role: str
    installed_bytes: int
    install_operation_id: str
    install_operation_payload_sha256: str
    compiled_spec_canonical_sha256: str
    state: str
    cleanup_receipt_sha256: str | None

    def document(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "rank": self.rank,
            "role": self.role,
            "installed_bytes": self.installed_bytes,
            "install_operation_id": self.install_operation_id,
            "install_operation_payload_sha256": self.install_operation_payload_sha256,
            "compiled_spec_canonical_sha256": self.compiled_spec_canonical_sha256,
            "state": self.state,
            "cleanup_receipt_sha256": self.cleanup_receipt_sha256,
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
    stored_plan_canonical_sha256: str
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
            "stored_plan_canonical_sha256": self.stored_plan_canonical_sha256,
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
    """Validate a lifecycle result before persistence and on readback."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise RecipeOperationConflict("recipe operation result is invalid")
    try:
        parse_recipe_lifecycle_result(kind, value)
    except (TypeError, ValueError) as error:
        raise RecipeOperationConflict("recipe operation result is invalid") from error
    return dict(value)


def _stored_run_plan(value: object) -> dict[str, object]:
    try:
        return run_plan_document(value)
    except RecipeExecutionContractError as error:
        raise RecipeOperationConflict("stored run plan is invalid") from error


def _stored_installation_plan(value: object) -> dict[str, object]:
    try:
        return installation_plan_document(value, for_uninstall=True)
    except RecipeExecutionContractError as error:
        raise RecipeOperationConflict("stored installation plan is invalid") from error


_DISTRIBUTED_START_CAPABILITY = "recipe.start.two-phase.v1"
_EXACT_RUN_INSPECTION_CAPABILITY = "recipe.run.inspect.exact.v1"

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
    result: dict[str, object] | None
    retry_due_at: datetime | None = None
    status_reason: str | None = None


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
class ImageDistributionPreview:
    recipe_build_id: str
    mapping_id: str
    mapping_generation: int
    image_digest: str
    node_ids: tuple[str, ...]
    plan_digest: str


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
class RecipeRunStatus:
    id: str
    alias: str
    state: str
    route_state: str
    healthy: bool
    ranks: tuple[RecipeRunRankStatus, ...]


_TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "expired", "cancelled"})
_INITIAL_OBSERVATION_GRACE_SECONDS = 120
_RETRYABLE_IMAGE_DISTRIBUTION_STATES = frozenset({"failed", "waiting-for-operator"})
_RETRYABLE_IMAGE_OPERATION_STATES = _TERMINAL_JOB_STATES | frozenset(
    {"waiting-for-operator"}
)
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
        "recipe.image.import.v1",
        "recipe.job.run.v1",
    }
)


def _bound_workload_intent(job: Job) -> int:
    ordinal = job.payload.get("workload_intent_ordinal")
    if type(ordinal) is not int or ordinal < 1:
        raise RecipeOperationConflict("workload operation lacks its admitted intent")
    return ordinal


def _run_start_intent(session: Session, run_id: str) -> int:
    run = session.get(RecipeRun, run_id)
    if run is None:
        raise RecipeOperationConflict("recipe run does not exist")
    root_kind = (
        "recipe.job.activate.v1"
        if _stored_run_plan(run.plan).get("execution_mode") == "one-shot-jobs"
        else "recipe.start"
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
        raise RecipeOperationConflict(
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


def _workload_owner_scope(
    session: Session, kind: str, owner_id: str
) -> tuple[str, ...]:
    if kind in {"recipe.start", "recipe.stop"}:
        if session.get(RecipeRun, owner_id) is None:
            raise RecipeOperationConflict("recipe run does not exist")
        statement = select(RunNode.node_id).where(RunNode.run_id == owner_id)
    elif kind in {"recipe.install", "recipe.uninstall", "recipe.reconcile"}:
        if session.get(RecipeInstallation, owner_id) is None:
            raise RecipeOperationConflict("recipe installation does not exist")
        statement = select(InstallationNode.node_id).where(
            InstallationNode.installation_id == owner_id
        )
    else:
        raise RecipeOperationConflict("workload reconciliation kind is invalid")
    targets = tuple(sorted(session.scalars(statement)))
    if not targets or len(targets) != len(set(targets)):
        raise RecipeOperationConflict("workload owner scope is invalid")
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
                ("queued", "running", "waiting-for-operator")
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
        if job.state != "waiting-for-operator"
        or (
            isinstance(job.result, Mapping)
            and job.result.get("cancel_requested") is True
        )
    )


def _unissued_workload_children(
    session: Session, job: Job, *, lock: bool = False
) -> tuple[AgentOperation, ...] | None:
    ordinal = job.payload.get("workload_intent_ordinal")
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
        raise RecipeOperationConflict("cancellation reason is required")
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
            raise ValueError("recipe run health age is invalid")
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
        self._mappings = mappings
        self._build_cleanup_cursor: str | None = None
        self._run_health_maximum_age = timedelta(seconds=run_health_maximum_age_seconds)

    def preview_mapping(
        self,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        *,
        parameters: Mapping[str, object],
        actor: str,
    ) -> ClusterMappingPlan:
        if self._mappings is None:
            raise RecipeOperationConflict("cluster mapping service is unavailable")
        return self._mappings.preview(recipe_revision_id, node_ids, parameters, actor)

    def create_mapping(self, plan: ClusterMappingPlan, *, actor: str) -> str:
        if self._mappings is None:
            raise RecipeOperationConflict("cluster mapping service is unavailable")
        try:
            return self._mappings.materialize(plan, actor=actor, now=self._clock())
        except (RuntimeError, ValueError) as error:
            raise RecipeOperationConflict(str(error)) from error

    def preview_build(
        self, recipe_revision_id: str, builder_node_id: str
    ) -> RecipeBuildPlan:
        if self._builds is None:
            raise RecipeOperationConflict("recipe build service is unavailable")
        return self._builds.plan(recipe_revision_id, builder_node_id, now=self._clock())

    def check_build_source(self, recipe_revision_id: str) -> SourcePolicyReport:
        if self._builds is None:
            raise RecipeOperationConflict("recipe build service is unavailable")
        return self._builds.check_source(recipe_revision_id)

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
        intent = RecipeBuildIntent(
            kind="dependency" if admission_guard is not None else "independent"
        )
        # Force bypasses cached images, not the identity of an accepted request.
        existing = self._idempotent(request_id, "recipe.build.v1", build_input_sha256)
        if existing is not None:
            if (
                existing.state not in {"succeeded", "cancelled"}
                and not force
                and not (
                    isinstance(existing.result, Mapping)
                    and existing.result.get("cancel_requested") is True
                )
            ):
                with self._sessions() as session:
                    succeeded = self._successful_build_job_in_session(
                        session, existing.owner_id, build_input_sha256
                    )
                    if succeeded is not None:
                        return self._view(succeeded)
            return existing
        if build_input_sha256 != plan.build_input_sha256:
            raise RecipeOperationConflict(
                "submitted build input does not match preview"
            )
        now = self._clock()
        with self._sessions.begin() as session:
            try:
                acquire_admission_keys(
                    session,
                    (
                        job_request_key(request_id),
                        node_admission_key(plan.builder_node_id),
                    ),
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
                            select(RecipeBuild).where(
                                RecipeBuild.id == plan.build_id
                            ),
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
                if succeeded is None:
                    raise RecipeOperationConflict("recipe build receipt is unavailable")
                replay = Job(
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
                    result=_validated_result("recipe.build.v1", succeeded.result),
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
                    Job.state.in_(("queued", "running", "waiting-for-operator")),
                    _JsonFlagIsTrue(Job.result, "cancel_requested").is_(True),
                )
            )
            if cancelling is not None:
                build_cancellation(cancelling)
                raise RecipeOperationConflict(
                    "recipe build cancellation is awaiting cleanup"
                )
            if (
                build is None
                or build.build_input_sha256 != plan.build_input_sha256
                or build.builder_node_id != plan.builder_node_id
            ):
                raise RecipeOperationConflict("recipe build preview is stale")
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
                            ("failed", "waiting-for-operator", "expired", "cancelled")
                        ),
                        Job.payload["owner_id"].as_string() == build.id,
                        Job.payload["plan_digest"].as_string()
                        == build.build_input_sha256,
                    )
                    .order_by(Job.updated_at.desc())
                    .limit(1)
                )
                if previous is None:
                    raise RecipeOperationConflict(
                        "failed recipe build receipt is unavailable"
                    )
                if previous.state == "cancelled":
                    cancellation = build_cancellation(previous)
                    if cancellation is None or cancellation.cancelled is not True:
                        raise RecipeOperationConflict(
                            "recipe build cancellation is awaiting cleanup"
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
                raise RecipeOperationConflict("recipe build preview is stale")
        self._agent_jobs.notify_available()
        return self.get(job.id)

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
            raise RecipeOperationConflict("recipe build service is unavailable")
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

        if not plan.allowed:
            if refreshable_preflight_is_the_only_blocker(plan):
                # The plan is unchanged and every objection is preflight
                # *evidence*: the receipt aged out, the host fingerprint
                # moved, or it does not cover this recipe's requirements.
                # Rejecting the plan here dead-ended Run/Switch on its own
                # re-probeable observation, so hand the caller the narrower
                # outcome that reruns the ordinary probe and re-presents the
                # identical plan.
                raise RecipeInstallPreflightExpired(
                    "install.plan_preflight_refresh_required"
                )
            reasons = list(
                dict.fromkeys(
                    _bounded_blocker_reason(reason.code, reason.detail)
                    for node in plan.nodes
                    for reason in node.blockers
                )
            )
            raise RecipeOperationConflict(
                "install plan is blocked: " + "; ".join(reasons[:3])
            )
        now = self._clock()
        with self._sessions() as session:
            existing_id = self._prepared_installation_id(session, plan)
        if existing_id is not None:
            return existing_id
        try:
            self._install_admission.refresh_install_receipts(
                plan, now=now, profile_application_id=profile_application_id
            )
        except InstallPreflightExpired as error:
            raise RecipeInstallPreflightExpired(str(error)) from error
        except (RuntimeError, ValueError) as error:
            raise RecipeOperationConflict(str(error)) from error
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
            except InstallPreflightExpired as error:
                # Nothing is persisted: the surrounding transaction rolls back.
                # Only the caller's own bounded preflight gate may act on this.
                raise RecipeInstallPreflightExpired(str(error)) from error
            except InstallAdmissionBusy:
                raise
            except (RuntimeError, ValueError) as error:
                raise RecipeOperationConflict(str(error)) from error
            installation = session.get(RecipeInstallation, installation_id)
            assert installation is not None
            try:
                stored_plan = parse_stored_installation_plan(installation.plan)
            except RecipeExecutionContractError as error:
                raise RecipeOperationConflict(
                    "compiled execution plan was not persisted"
                ) from error
            if not stored_plan.compiled_execution_plans:
                raise RecipeOperationConflict(
                    "compiled execution plan was not persisted"
                )
            return installation_id

    @staticmethod
    def _prepared_installation_id(session: Session, plan: InstallPlan) -> str | None:
        existing = session.scalar(
            select(RecipeInstallation)
            .where(
                RecipeInstallation.mapping_id == plan.mapping_id,
                RecipeInstallation.mapping_generation == plan.mapping_generation,
                RecipeInstallation.recipe_build_id == plan.recipe_build_id,
                RecipeInstallation.plan_digest == plan.plan_digest,
                RecipeInstallation.state.in_(
                    ("planned", "installing", "partial", "installed")
                ),
            )
            .order_by(RecipeInstallation.created_at.desc())
            .limit(1)
        )
        if existing is None:
            return None
        try:
            stored_plan = parse_stored_installation_plan(existing.plan)
        except RecipeExecutionContractError as error:
            raise RecipeOperationConflict(
                "stored installation plan is invalid"
            ) from error
        if not stored_plan.compiled_execution_plans:
            raise RecipeOperationConflict(
                "stored installation has no compiled execution plan"
            )
        return existing.id

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
                raise RecipeOperationConflict("recipe installation is unavailable")
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
                    Job.state.in_(("queued", "running", "waiting-for-operator")),
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == installation_id,
                )
                .limit(1)
            )
            if reconciliation is not None:
                raise RecipeOperationConflict(
                    "recipe installation is being reconciled"
                )
            if installation.state == "installed":
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
                raise RecipeOperationConflict(
                    "recipe installation is already complete without an operation"
                )
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
                    and active.payload.get("workload_intent_ordinal")
                    != workload_intent_ordinal
                ):
                    raise RecipeOperationConflict(
                        "prior installation intent requires exact observation before replacement"
                    )
                return self._view(active)
            if installation.state not in {"planned", "partial", "failed", "installing"}:
                raise RecipeOperationConflict("recipe installation is not launchable")
            try:
                stored_installation_plan = parse_stored_installation_plan(
                    installation.plan
                )
            except RecipeExecutionContractError as error:
                raise RecipeOperationConflict(
                    "compiled execution plan is unavailable"
                ) from error
            raw_plans = stored_installation_plan.compiled_execution_plans
            nodes = tuple(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id == installation_id)
                    .order_by(InstallationNode.rank, InstallationNode.node_id)
                )
            )
            if not nodes or any(node.node_id not in raw_plans for node in nodes):
                raise RecipeOperationConflict(
                    "compiled execution plan is missing for one or more nodes"
                )
            revision = _active_recipe_revision(session, installation.recipe_revision_id)
            if revision is None or revision.content_digest is None:
                raise RecipeOperationConflict("recipe revision is unavailable")
            installation.state = "installing"
            installation.updated_at = now
            for node in nodes:
                node.state = "planned"
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
                            "schema_version": 2,
                            "installation_id": installation_id,
                            "plan_digest": installation.plan_digest,
                            "rank": node.rank,
                            "role": node.role,
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

    def preview_image_distribution(
        self,
        recipe_build_id: str,
        mapping_id: str,
        *,
        mapping_generation: int,
    ) -> ImageDistributionPreview:
        if self._builds is None:
            raise RecipeOperationConflict("recipe build service is unavailable")
        try:
            plan = self._builds.plan_distribution(
                recipe_build_id, mapping_id, generation=mapping_generation
            )
        except (KeyError, RuntimeError, ValueError) as error:
            raise RecipeOperationConflict(str(error)) from error
        identity = {
            "schema_version": 1,
            "build_id": plan.build_id,
            "mapping_id": plan.mapping_id,
            "mapping_generation": plan.mapping_generation,
            "image_digest": plan.image_digest,
            "targets": [node_id for node_id, _payload in plan.targets],
        }
        return ImageDistributionPreview(
            recipe_build_id=plan.build_id,
            mapping_id=plan.mapping_id,
            mapping_generation=plan.mapping_generation,
            image_digest=plan.image_digest,
            node_ids=tuple(node_id for node_id, _payload in plan.targets),
            plan_digest=hashlib.sha256(canonical_message(identity)).hexdigest(),
        )

    def distribute_image(
        self,
        recipe_build_id: str,
        mapping_id: str,
        *,
        mapping_generation: int,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        if self._builds is None:
            raise RecipeOperationConflict("recipe build service is unavailable")
        try:
            plan = self._builds.plan_distribution(
                recipe_build_id, mapping_id, generation=mapping_generation
            )
        except (KeyError, RuntimeError, ValueError) as error:
            raise RecipeOperationConflict(str(error)) from error
        identity = {
            "schema_version": 1,
            "build_id": plan.build_id,
            "mapping_id": plan.mapping_id,
            "mapping_generation": plan.mapping_generation,
            "image_digest": plan.image_digest,
            "targets": [node_id for node_id, _payload in plan.targets],
        }
        actual_plan_digest = hashlib.sha256(canonical_message(identity)).hexdigest()
        if plan_digest != actual_plan_digest:
            raise RecipeOperationConflict(
                "submitted image distribution plan does not match preview"
            )
        existing = self._idempotent(request_id, "recipe.image.import.v1", plan_digest)
        if existing is not None:
            return existing
        if not plan.targets:
            raise RecipeOperationConflict(
                "exact built image is already present on every mapped GPU node"
            )
        return self._queue(
            kind="recipe.image.import.v1",
            owner_kind="image-distribution",
            owner_id=plan.build_id,
            plan_digest=plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=plan.targets,
            authority_digest=plan_digest,
            workload_intent_ordinal=workload_intent_ordinal,
        )

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
            or existing.payload.get("owner_kind") != "run"
        ):
            return None
        recorded_digest = existing.payload.get("plan_digest")
        if not isinstance(recorded_digest, str):
            return None
        if plan_digest is not None and recorded_digest != plan_digest:
            return None
        owner_id = existing.payload.get("owner_id")
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
                or existing.payload.get("owner_kind") != owner_kind
                or existing.payload.get("owner_id") != owner_id
            ):
                return None
            return self._view(existing)

    def run_status(self, run_id: str) -> RecipeRunStatus:
        now = _aware(self._clock())
        with self._sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise KeyError(run_id)
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            try:
                stored_plan = parse_stored_run_plan(run.plan)
            except RecipeExecutionContractError as error:
                raise RecipeOperationConflict("stored run plan is invalid") from error
            expected = [node.model_dump(mode="json") for node in stored_plan.nodes]
            exact_ranks = (
                isinstance(expected, list)
                and len(expected) == len(nodes)
                and all(isinstance(item, Mapping) for item in expected)
                and {
                    (item.get("node_id"), item.get("rank"), item.get("role"))
                    for item in expected
                }
                == {(node.node_id, node.rank, node.role) for node in nodes}
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
            )

    def install(
        self,
        plan: InstallPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        existing = self._idempotent(request_id, "recipe.install", plan_digest)
        if existing is not None:
            return existing
        if plan_digest != plan.plan_digest:
            raise RecipeOperationConflict(
                "submitted plan digest does not match preview"
            )
        now = self._clock()
        try:
            self._install_admission.refresh_install_receipts(plan, now=now)
        except (RuntimeError, ValueError) as error:
            raise RecipeOperationConflict(str(error)) from error
        with self._sessions.begin() as session:
            try:
                acquire_admission_keys(
                    session,
                    (
                        job_request_key(request_id),
                        *(node_admission_key(node.node_id) for node in plan.nodes),
                    ),
                )
            except AdmissionLockBusy as error:
                raise InstallAdmissionBusy("install.capacity_busy") from error
            replay = self._idempotent_in_session(
                session, request_id, "recipe.install", plan_digest
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
                raise RecipeOperationConflict(str(error)) from error
            installation = session.get(RecipeInstallation, installation_id)
            assert installation is not None
            installation.state = "installing"
            installation.updated_at = now
            compiled_plans = plan.compiled_plan_by_node
            if set(compiled_plans) != {node.node_id for node in plan.nodes}:
                raise RecipeOperationConflict(
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
                            "schema_version": 2,
                            "installation_id": installation_id,
                            "plan_digest": plan.plan_digest,
                            "rank": node.rank,
                            "role": node.role,
                            "expected_bytes": node.required_bytes,
                            "compiled_execution_plan": compiled_plans[node.node_id],
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
        if plan_digest != plan.plan_digest:
            raise RecipeOperationConflict(
                "submitted plan digest does not match preview"
            )
        existing = self._idempotent(request_id, "recipe.start", plan_digest)
        if existing is not None:
            return existing
        now = self._clock()
        with self._sessions.begin() as session:
            try:
                acquire_admission_keys(
                    session,
                    (
                        job_request_key(request_id),
                        *(node_admission_key(node.node_id) for node in plan.nodes),
                    ),
                )
            except AdmissionLockBusy as error:
                raise RunAdmissionBusy("run capacity writer is busy") from error
            replay = self._idempotent_in_session(
                session, request_id, "recipe.start", plan_digest
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
                raise RecipeOperationConflict(
                    "recipe node endpoint evidence is unavailable"
                )
            master = next((node for node in plan.nodes if node.endpoint_owner), None)
            if master is None:
                raise RecipeOperationConflict("recipe run has no endpoint owner")
            world_size = len(plan.nodes)
            master_address = master.fabric_address if world_size > 1 else None
            master_port = master.rendezvous_port if world_size > 1 else None
            if world_size > 1 and (master_address is None or master_port is None):
                raise RecipeOperationConflict(
                    "recipe direct-fabric rendezvous is unavailable"
                )
            active_uninstall = session.scalar(
                select(Job.id)
                .where(
                    Job.kind.in_(("recipe.uninstall", "recipe.reconcile")),
                    Job.state.in_({"queued", "running", "waiting-for-operator"}),
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == plan.installation_id,
                )
                .limit(1)
            )
            if active_uninstall is not None:
                raise RecipeOperationConflict("recipe installation is not runnable")
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
                raise RecipeOperationConflict(str(error)) from error
            run = session.get(RecipeRun, run_id)
            revision = _active_recipe_revision(session, plan.recipe_revision_id)
            installation = session.get(RecipeInstallation, plan.installation_id)
            assert run is not None and revision is not None and installation is not None
            try:
                stored_installation_plan = parse_stored_installation_plan(
                    installation.plan
                )
            except RecipeExecutionContractError as error:
                raise RecipeOperationConflict(
                    "compiled execution plan is unavailable for the installed recipe"
                ) from error
            compiled_plans = {
                node_id: value.model_dump(mode="json")
                for node_id, value in stored_installation_plan.compiled_execution_plans.items()
            }
            if not compiled_plans:
                raise RecipeOperationConflict(
                    "compiled execution plan is unavailable for the installed recipe"
                )
            if any(
                not isinstance(compiled_plans.get(node.node_id), Mapping)
                for node in plan.nodes
            ):
                raise RecipeOperationConflict(
                    "compiled execution plan is missing for one or more run ranks"
                )
            start_order = _topology_order(revision.document, "start_order")
            topology = recipe_topology(revision.document)
            distributed_readiness = _canonical_distributed_readiness(revision.document)
            two_phase_start = (
                world_size > 1
                and topology.get("mode") == "distributed"
                and distributed_readiness is not None
            )
            start_deadline = (
                _distributed_start_deadline(
                    revision.document,
                    now=now,
                    timeout_seconds=self._distributed_start_timeout_seconds,
                )
                if two_phase_start
                else None
            )
            if two_phase_start:
                advertised = {
                    node.node_id: set(node.capabilities or ())
                    for node in session.scalars(
                        select(AgentNode).where(
                            AgentNode.node_id.in_(
                                [planned.node_id for planned in plan.nodes]
                            )
                        )
                    )
                }
                if any(
                    not {
                        _DISTRIBUTED_START_CAPABILITY,
                        _EXACT_RUN_INSPECTION_CAPABILITY,
                    }
                    <= advertised.get(planned.node_id, set())
                    for planned in plan.nodes
                ):
                    raise RecipeOperationConflict(
                        "distributed start requires two-phase exact-observation agent support"
                    )
            run.state = "starting"
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
                        raise KeyError("recipe start endpoint address is unavailable")
                    payload = build_recipe_start_payload(
                        run_id=run_id,
                        installation_id=plan.installation_id,
                        recipe_revision_id=plan.recipe_revision_id,
                        recipe_content_sha256=recipe_digest,
                        mapping_id=run.mapping_id,
                        mapping_generation=run.mapping_generation,
                        run_generation=run.run_generation,
                        image_digest=installation.image_digest,
                        plan_digest=plan.plan_digest,
                        alias=plan.alias,
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
                        endpoint_address=endpoint_address,
                        compiled_endpoint_address=(
                            presences[node_id] if endpoint_owner else None
                        ),
                        world_size=world_size,
                        compiled_execution_plan=compiled_plans[node_id],
                        local_address=(node.fabric_address if world_size > 1 else None),
                        master_address=master_address,
                        master_port=master_port,
                        phase="rank-launch" if start_deadline is not None else None,
                        start_deadline=start_deadline,
                    )
                except (KeyError, RecipeStartPayloadError) as error:
                    raise RecipeOperationConflict(
                        "recipe start payload is invalid"
                    ) from error
                return node_id, payload

            start_payloads = tuple(start_payload(node) for node in plan.nodes)
            role_phases = _role_phases(start_order, start_payloads)
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
        if run is None or run.state != "running":
            raise RecipeOperationConflict("recipe run is not accepting jobs")
        node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == run_id,
                RunNode.node_id == node_id,
                RunNode.state == "running",
            )
        )
        if node is None:
            raise RecipeOperationConflict("recipe job target is not running")
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

    def activate_job_run(
        self,
        plan: RunPlan,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
    ) -> RecipeOperationView:
        """Reserve an installed artifact recipe without starting a service container."""
        if plan_digest != plan.plan_digest:
            raise RecipeOperationConflict(
                "submitted plan digest does not match preview"
            )
        existing = self._idempotent(request_id, "recipe.job.activate.v1", plan_digest)
        if existing is not None:
            return existing
        now = self._clock()
        with self._sessions.begin() as session:
            installation = session.get(RecipeInstallation, plan.installation_id)
            revision = (
                _active_recipe_revision(session, installation.recipe_revision_id)
                if installation is not None
                else None
            )
            interfaces = (
                revision.document.get("interfaces")
                if revision is not None and isinstance(revision.document, Mapping)
                else None
            )
            adapters = (
                [
                    item.get("adapter")
                    for item in interfaces
                    if isinstance(item, Mapping)
                ]
                if isinstance(interfaces, list)
                else []
            )
            artifact_adapters = {
                "audio-job",
                "video-job",
                "image-job",
                "mesh-job",
                "artifact-job",
            }
            if len(adapters) != 1 or adapters[0] not in artifact_adapters:
                raise RecipeOperationConflict("recipe is not an artifact job recipe")
            topology = (
                revision.document.get("topology") if revision is not None else None
            )
            if not isinstance(topology, Mapping) or topology.get("node_count") != 1:
                raise RecipeOperationConflict(
                    "artifact job recipes currently require a single-node topology"
                )
            try:
                run_id = self._run_admission.accept_run_in_session(
                    session, plan, actor=actor, now=now
                )
            except (RuntimeError, ValueError) as error:
                raise RecipeOperationConflict(str(error)) from error
            run = session.get(RecipeRun, run_id)
            assert run is not None and revision is not None
            run.state = "running"
            run.route_state = "withdrawn"
            try:
                updated_plan = run_plan_document(run.plan)
                updated_plan["execution_mode"] = "one-shot-jobs"
                run.plan = run_plan_document(updated_plan)
            except RecipeExecutionContractError as error:
                raise RecipeOperationConflict("stored run plan is invalid") from error
            run.updated_at = now
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            for node in nodes:
                node.state = "running"
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
                raise RecipeOperationConflict("artifact workload target disappeared")
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
            job = Job(
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
        self, kind: str, owner_id: str, workload_intent_ordinal: int | None = None
    ) -> IssuedWorkloadReconciliation | None:
        """Name exact issued work that needs fresh observation before resumption."""
        if workload_intent_ordinal is not None and (
            type(workload_intent_ordinal) is not int or workload_intent_ordinal < 1
        ):
            raise RecipeOperationConflict("workload intent ordinal is invalid")
        now = _aware(self._clock())
        with self._sessions() as session:
            scope = _workload_owner_scope(session, kind, owner_id)
            if workload_intent_ordinal is not None and not _intent_is_current(
                session, workload_intent_ordinal, scope
            ):
                raise RecipeOperationConflict("workload intent was superseded")
            pending: list[IssuedWorkloadReconciliation] = []
            for job in _active_owned_workload_jobs(
                session, kind, owner_id, include_waiting_cancellation=True
            ):
                ordinal = job.payload.get("workload_intent_ordinal")
                if tuple(sorted(job.targets)) != scope:
                    raise RecipeOperationConflict("workload owner scope changed")
                if type(ordinal) is not int or ordinal < 1:
                    raise RecipeOperationConflict(
                        "issued workload authority is invalid"
                    )
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
                if not children or any(
                    child.node_id not in scope
                    or child.workload_intent_ordinal != ordinal
                    for child in children
                ):
                    raise RecipeOperationConflict(
                        "issued workload children are invalid"
                    )
                attempts = tuple(
                    session.scalars(
                        select(AgentOperationAttempt).where(
                            AgentOperationAttempt.operation_id.in_(
                                tuple(child.id for child in children)
                            )
                        )
                    )
                )
                if not attempts:
                    raise RecipeOperationConflict(
                        "issued workload attempt evidence is missing"
                    )
                latest_lease = max(
                    _aware(attempt.lease_deadline) for attempt in attempts
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
                plan_digest = job.payload.get("plan_digest")
                if not isinstance(plan_digest, str):
                    raise RecipeOperationConflict("issued workload plan is invalid")
                pending.append(
                    IssuedWorkloadReconciliation(
                        job_id=job.id,
                        kind=kind,
                        owner_id=owner_id,
                        plan_digest=plan_digest,
                        payload_digests=tuple(
                            child.payload_digest for child in children
                        ),
                        failure_kind=FailureKind.UNCERTAIN_EFFECT,
                        observe_due_at=observe_due_at,
                        observation_deadline=observation_deadline,
                    )
                )
            if len(pending) > 1:
                raise RecipeOperationConflict(
                    "multiple issued workload effects need observation"
                )
            return pending[0] if pending else None

    def reconcile_superseded_unissued(
        self, kind: str, owner_id: str, workload_intent_ordinal: int
    ) -> bool:
        """Retire only exact older workload jobs with no issued agent attempt."""
        if type(workload_intent_ordinal) is not int or workload_intent_ordinal < 1:
            raise RecipeOperationConflict("workload intent ordinal is invalid")
        now = self._clock()
        retired = False
        with self._sessions.begin() as session:
            scope = _workload_owner_scope(session, kind, owner_id)
            nodes = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(scope))
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                )
            )
            if tuple(node.node_id for node in nodes) != scope or any(
                node.workload_intent_ordinal != workload_intent_ordinal
                or node.state != "active"
                or node.revoked_at is not None
                for node in nodes
            ):
                raise RecipeOperationConflict("workload intent was superseded")
            for job in _active_owned_workload_jobs(session, kind, owner_id, lock=True):
                previous_ordinal = job.payload.get("workload_intent_ordinal")
                if tuple(sorted(job.targets)) != scope:
                    raise RecipeOperationConflict("workload owner scope changed")
                if (
                    type(previous_ordinal) is not int
                    or previous_ordinal < 1
                    or previous_ordinal >= workload_intent_ordinal
                ):
                    continue
                children = _unissued_workload_children(session, job, lock=True)
                if children is None:
                    continue
                job.state = "cancelled"
                job.status_reason = "superseded before agent issuance"
                job.updated_at = now
                for child in children:
                    child.state = "cancelled"
                    child.status_reason = "superseded before agent issuance"
                    child.retry_due_at = None
                    child.updated_at = now
                retired = True
        return retired

    def preview_stop(self, run_id: str) -> StopPlan:
        with self._sessions() as session:
            return self._stop_plan_in_session(session, run_id, lock=False)

    def stop(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        existing = self._idempotent(
            request_id,
            "recipe.stop",
            plan_digest,
            owner_kind="run",
            owner_id=run_id,
        )
        if existing is not None and not (
            existing.state == "running" and self._one_shot_stop_is_pending(request_id)
        ):
            return existing
        logical = self._stop_logical_job_run(
            run_id,
            plan_digest=plan_digest,
            actor=actor,
            request_id=request_id,
            workload_intent_ordinal=workload_intent_ordinal,
        )
        if isinstance(logical, RecipeArtifactJobCancellationPending):
            raise logical
        if logical is not None:
            return logical
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
                    plan_digest,
                    owner_kind="run",
                    owner_id=run_id,
                )
                if existing is not None:
                    return existing
                admitted = self._stop_plan_in_session(session, run_id, lock=True)
                if not admitted.allowed or admitted.plan_digest != plan_digest:
                    raise RecipeOperationConflict("stop plan is stale or blocked")
                prepared = (
                    self._route_publications.prepare_withdrawal_in_session(
                        session, frozenset({run_id})
                    )
                    if self._route_publications is not None
                    else None
                )
                run = session.get(RecipeRun, run_id)
                assert run is not None
                revision = _active_recipe_revision(session, admitted.recipe_revision_id)
                if revision is None:
                    raise RecipeOperationConflict("recipe run topology is unavailable")
                stop_order = _topology_order(revision.document, "stop_order")
                run.state = "stopping"
                run.updated_at = now
                job = self._queue_in_session(
                    session,
                    kind="recipe.stop",
                    owner_kind="run",
                    owner_id=run_id,
                    plan_digest=admitted.plan_digest,
                    actor=actor,
                    request_id=request_id,
                    node_payloads=tuple(
                        (
                            node.node_id,
                            {
                                "schema_version": 1,
                                "run_id": run_id,
                                "plan_digest": admitted.authority_digest,
                                "cancel_pending_start": True,
                            },
                        )
                        for node in admitted.nodes
                    ),
                    phases=_role_phases(
                        stop_order,
                        tuple(
                            (
                                node.node_id,
                                {
                                    "schema_version": 1,
                                    "run_id": run_id,
                                    "plan_digest": admitted.authority_digest,
                                    "cancel_pending_start": True,
                                    "role": node.role,
                                },
                            )
                            for node in admitted.nodes
                        ),
                        include_role=False,
                    ),
                    authority_digest=admitted.authority_digest,
                    now=now,
                    workload_intent_ordinal=workload_intent_ordinal,
                )
                session.flush()
                if self._route_publications is not None:
                    self._route_publications.withdraw_run_in_session(
                        session, run_id, prepared=prepared
                    )
                else:
                    self._route_withdrawer(run_id)
                    run.route_state = "withdrawn"
                    run.route_error = None
                    run.updated_at = now
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
            raise RecipeOperationConflict(
                "request key was already used differently"
            ) from error
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def preview_uninstall(self, installation_id: str) -> UninstallPlan:
        with self._sessions() as session:
            return self._uninstall_plan_in_session(session, installation_id, lock=False)

    def preview_reconciliation_authority(
        self,
        installation_id: str,
        *,
        session: Session | None = None,
        allow_active_reconciliation: bool = False,
    ) -> InstallationReconciliationAuthority:
        """Bind an explicit cleanup review to successful install provenance.

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

    def reconciliation_operation_receipts(
        self,
        request_id: str,
        *,
        expected_authority: Mapping[str, object],
    ) -> tuple[RecipeReconcileResult, ...] | None:
        """Return canonical receipts only after proving full topology completion."""

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
                or canonical_message(
                    job.payload.get("reconciliation_authority", {})
                )
                != canonical_message(expected_authority)
            ):
                return None
            children = tuple(
                session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == job.id
                    )
                )
            )
            if not self._reconciliation_job_has_exact_receipts(
                session, job, children
            ):
                return None
            result = job.result
            evidence = (
                result.get("node_evidence") if isinstance(result, Mapping) else None
            )
            if not isinstance(evidence, Mapping):
                return None
            targets = expected_authority.get("targets")
            pending_ids = (
                sorted(
                    str(target.get("node_id"))
                    for target in targets
                    if isinstance(target, Mapping) and target.get("state") == "pending"
                )
                if isinstance(targets, list)
                else []
            )
            if not pending_ids or set(evidence) != set(pending_ids):
                return None
            receipts: list[RecipeReconcileResult] = []
            for node_id in pending_ids:
                raw_receipt = evidence.get(node_id)
                if not isinstance(raw_receipt, Mapping):
                    return None
                try:
                    receipts.append(
                        RecipeReconcileResult.model_validate_json(
                            canonical_message(raw_receipt)
                        )
                    )
                except (TypeError, ValueError):
                    return None
            return tuple(receipts)

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
                select(RecipeRun).where(
                    RecipeRun.installation_id == installation.id
                )
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
                        ("queued", "running", "waiting-for-operator")
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
                            ("queued", "running", "waiting-for-operator")
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
                select(AgentOperation).where(
                    AgentOperation.parent_job_id.in_(job_ids)
                ),
            ),
        )
        try:
            locked = lock_admission_rows(session, requests)
        except AdmissionLockBusy as error:
            raise InstallAdmissionBusy("reconcile.capacity_busy") from error
        locked_nodes = locked["reconcile-installation-nodes"]
        locked_mapping_nodes = locked["reconcile-mapping-nodes"]
        if (
            tuple(
                sorted((node.node_id, node.rank, node.role) for node in locked_nodes)
            )
            != tuple(sorted((node.node_id, node.rank, node.role) for node in node_rows))
            or tuple(
                sorted(
                    (node.node_id, node.rank, node.role)
                    for node in locked_mapping_nodes
                )
            )
            != tuple(
                sorted((node.node_id, node.rank, node.role) for node in mapping_nodes)
            )
        ):
            raise InstallAdmissionBusy("reconcile.membership_changed")

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
            raise KeyError(installation_id)

        if lock:
            self._lock_reconciliation_rows_in_session(session, installation)
            # The helper refreshed every locked row after acquiring the full
            # set in canonical table/primary-key order. Keep later reads free
            # of ad-hoc locks that could invert that order.
            lock = False
            allow_active_reconciliation = False

        def blocked(code: str, detail: str) -> NoReturn:
            raise RecipeReconciliationBlocked(code, detail)

        stored_plan = installation.plan
        if not isinstance(stored_plan, Mapping):
            blocked(
                "reconcile.installation_identity_unavailable",
                "The original installation identity is not a JSON object.",
            )

        revision_statement = select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.id == installation.recipe_revision_id,
            CatalogDocumentRevision.kind == "recipe",
        )
        if lock:
            revision_statement = revision_statement.with_for_update(
                of=CatalogDocumentRevision
            )
        revision = session.scalar(revision_statement)
        if (
            revision is None
            or revision.content_digest is None
            or not isinstance(revision.document, Mapping)
            or revision.content_digest != stored_plan.get("recipe_content_sha256")
        ):
            blocked(
                "reconcile.recipe_revision_unavailable",
                "The installation's exact accepted recipe revision is unavailable.",
            )

        # This is the immutable admitted document, read only as generic JSON.
        # A failed current launch schema check is the reason for this explicit
        # cleanup route and must not be converted into an executable fallback.
        try:
            stored_plan_digest = installation_plan_digest_from_stored_document(
                stored_plan
            )
            stored_plan_sha256 = hashlib.sha256(canonical_message(stored_plan)).hexdigest()
        except (KeyError, TypeError, ValueError) as error:
            blocked(
                "reconcile.installation_identity_unavailable",
                f"The original installation identity cannot be fingerprinted: {error}",
            )
        expected_top_level = {
            "mapping_id": installation.mapping_id,
            "mapping_generation": installation.mapping_generation,
            "recipe_build_id": installation.recipe_build_id,
            "image_digest": installation.image_digest,
            "recipe_revision_id": installation.recipe_revision_id,
            "recipe_content_sha256": revision.content_digest,
            "plan_digest": installation.plan_digest,
        }
        if (
            any(stored_plan.get(key) != value for key, value in expected_top_level.items())
            or stored_plan_digest != installation.plan_digest
        ):
            blocked(
                "reconcile.installation_identity_mismatch",
                "The relational installation identity differs from its original admitted plan.",
            )
        try:
            recipe_model_content_sha256, _ = _primary_model_identity(
                revision.document
            )
        except RecipeOperationConflict as error:
            blocked(
                "reconcile.recipe_revision_unavailable",
                f"The exact accepted recipe revision has no model identity: {error}",
            )
        if installation.model_content_sha256 not in {
            None,
            recipe_model_content_sha256,
        }:
            blocked(
                "reconcile.installation_identity_mismatch",
                "The relational model identity differs from the exact accepted recipe revision.",
            )
        if installation.state not in {"installed", "partial"}:
            blocked(
                "reconcile.installation_effect_unknown",
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
                "reconcile.rank_membership_changed",
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
        stored_nodes = stored_plan.get("nodes")
        stored_membership = (
            tuple(
                (item.get("node_id"), item.get("rank"), item.get("role"))
                for item in stored_nodes
            )
            if isinstance(stored_nodes, list)
            and all(isinstance(item, Mapping) for item in stored_nodes)
            else ()
        )
        if (
            mapping is None
            or mapping.recipe_revision_id != revision.id
            or mapping.generation != installation.mapping_generation
            or mapping.node_count != len(mapping_nodes)
            or not mapping_nodes
            or actual_membership != mapping_membership
            or actual_membership != stored_membership
            or tuple(node.rank for node in all_nodes) != tuple(range(len(all_nodes)))
            or mapping.endpoint_owner_node_id
            not in {node.node_id for node in mapping_nodes}
        ):
            blocked(
                "reconcile.rank_membership_changed",
                "The stored installation, relational membership, and saved mapping no longer identify the same exact ranks.",
            )

        active_runs_statement = select(RecipeRun).where(
            RecipeRun.installation_id == installation.id
        )
        if lock:
            active_runs_statement = active_runs_statement.with_for_update(
                of=RecipeRun
            )
        installation_runs = tuple(session.scalars(active_runs_statement))
        if any(
            run.state != "stopped" or run.route_state != "withdrawn"
            for run in installation_runs
        ):
            blocked(
                "reconcile.active_effect_unknown",
                "An installation run or route is active or has an unconfirmed effect.",
            )

        active_jobs_statement = select(Job).where(
            Job.state.in_(("queued", "running", "waiting-for-operator")),
            Job.payload["owner_kind"].as_string() == "installation",
            Job.payload["owner_id"].as_string() == installation.id,
            Job.kind.in_(
                ("recipe.install", "recipe.uninstall", "recipe.reconcile")
            ),
        )
        if lock:
            active_jobs_statement = active_jobs_statement.with_for_update(of=Job)
        active_jobs = tuple(session.scalars(active_jobs_statement))
        run_ids = {run.id for run in installation_runs}
        active_run_jobs = (
            tuple(
                session.scalars(
                    select(Job).where(
                        Job.state.in_(("queued", "running", "waiting-for-operator")),
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
                "reconcile.operation_active",
                "An install, start, stop, uninstall, or reconciliation operation is still active or uncertain.",
            )

        install_jobs = tuple(
            session.scalars(
                select(Job)
                .where(
                    Job.kind == "recipe.install",
                    Job.state == "succeeded",
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == installation.id,
                    Job.payload["plan_digest"].as_string() == installation.plan_digest,
                )
                .order_by(Job.created_at, Job.id)
                .limit(2)
            )
        )
        if len(install_jobs) != 1:
            blocked(
                "reconcile.install_provenance_unavailable",
                "Exactly one successful original install operation is required to identify the local specs.",
            )
        install_job = install_jobs[0]
        if (
            not isinstance(install_job.payload, Mapping)
            or install_job.payload.get("schema_version") != 1
            or install_job.payload.get("owner_kind") != "installation"
            or install_job.payload.get("owner_id") != installation.id
            or install_job.payload.get("plan_digest") != installation.plan_digest
            or install_job.targets != sorted(node.node_id for node in all_nodes)
            or install_job.authority_revision != revision.content_digest
            or hashlib.sha256(canonical_message(install_job.payload)).hexdigest()
            != install_job.payload_digest
        ):
            blocked(
                "reconcile.install_provenance_mismatch",
                "The successful original install job no longer matches the admitted installation identity.",
            )
        try:
            install_result = parse_recipe_lifecycle_result(
                "recipe.install", install_job.result
            )
        except (TypeError, ValueError) as error:
            blocked(
                "reconcile.install_provenance_unavailable",
                f"The original successful install receipt cannot be validated: {error}",
            )
        if (
            not isinstance(install_result, RecipeOperationResult)
            or install_result.failed_nodes
            or install_result.recovery_error is not None
            or set(install_result.successful_nodes)
            != {node.node_id for node in all_nodes}
            or set(install_result.node_evidence)
            != {node.node_id for node in all_nodes}
        ):
            blocked(
                "reconcile.install_provenance_unavailable",
                "The original install receipt does not prove success on every exact node.",
            )

        operations = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == install_job.id)
                .order_by(AgentOperation.node_id, AgentOperation.id)
            )
        )
        if (
            len(operations) != len(all_nodes)
            or {operation.node_id for operation in operations}
            != {node.node_id for node in all_nodes}
        ):
            blocked(
                "reconcile.install_provenance_unavailable",
                "The original install child operations do not match exact node membership.",
            )

        node_by_id = {node.node_id: node for node in all_nodes}
        stored_compiled = stored_plan.get("compiled_execution_plans")
        if not isinstance(stored_compiled, Mapping):
            blocked(
                "reconcile.installation_identity_unavailable",
                "The opaque admitted plan has no exact per-node specification map.",
            )
        agent_nodes_statement = (
            select(AgentNode)
            .where(AgentNode.node_id.in_(node_by_id))
            .order_by(AgentNode.node_id)
        )
        if lock:
            agent_nodes_statement = agent_nodes_statement.with_for_update(
                of=AgentNode
            )
        agent_nodes = tuple(session.scalars(agent_nodes_statement))
        agent_node_by_id = {node.node_id: node for node in agent_nodes}
        targets: list[InstallationReconciliationTarget] = []
        for operation in operations:
            node = node_by_id[operation.node_id]
            agent_node = agent_node_by_id.get(operation.node_id)
            if (
                operation.kind != "recipe.install"
                or operation.state != "succeeded"
                or operation.current_attempt < 1
                or operation.authority_revision != revision.content_digest
                or not isinstance(operation.payload, Mapping)
                or hashlib.sha256(canonical_message(operation.payload)).hexdigest()
                != operation.payload_digest
            ):
                blocked(
                    "reconcile.install_provenance_mismatch",
                    f"The original install operation for {node.node_id} is not an exact successful source.",
                )
            payload = operation.payload
            if (
                payload.get("schema_version") != 2
                or payload.get("installation_id") != installation.id
                or payload.get("plan_digest") != installation.plan_digest
                or payload.get("rank") != node.rank
                or payload.get("role") != node.role
                or payload.get("expected_bytes") != node.required_bytes
            ):
                blocked(
                    "reconcile.install_provenance_mismatch",
                    f"The original install payload for {node.node_id} differs from its exact installation row.",
                )
            compiled = payload.get("compiled_execution_plan")
            stored_node_compiled = stored_compiled.get(node.node_id)
            if (
                not isinstance(compiled, Mapping)
                or not isinstance(stored_node_compiled, Mapping)
                or canonical_message(compiled) != canonical_message(stored_node_compiled)
            ):
                blocked(
                    "reconcile.spec_identity_mismatch",
                    f"The original install payload and stored opaque specification differ for {node.node_id}.",
                )
            identity = compiled.get("identity")
            runtime = compiled.get("runtime")
            runtime_image = compiled.get("runtime_image")
            placement = runtime.get("placement") if isinstance(runtime, Mapping) else None
            if (
                not isinstance(identity, Mapping)
                or not isinstance(runtime, Mapping)
                or not isinstance(runtime_image, Mapping)
                or not isinstance(placement, Mapping)
                or identity.get("recipe_revision_sha256") != revision.content_digest
                or runtime.get("image_digest") != installation.image_digest
                or runtime_image.get("image_digest") != installation.image_digest
                or placement.get("rank") != node.rank
                or placement.get("role") != node.role
            ):
                blocked(
                    "reconcile.spec_identity_mismatch",
                    f"The original specification for {node.node_id} is not bound to this recipe, image, and rank.",
                )
            evidence = install_result.node_evidence.get(node.node_id)
            if not isinstance(evidence, Mapping):
                blocked(
                    "reconcile.install_provenance_unavailable",
                    f"The original successful receipt for {node.node_id} is unavailable.",
                )
            current_attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation.id,
                    AgentOperationAttempt.attempt == operation.current_attempt,
                )
            )
            if (
                current_attempt is None
                or current_attempt.state != "succeeded"
                or not isinstance(current_attempt.result, Mapping)
                or canonical_message(current_attempt.result) != canonical_message(evidence)
                or evidence.get("installed_bytes") != node.installed_bytes
            ):
                blocked(
                    "reconcile.install_provenance_unavailable",
                    f"The current successful install attempt does not match recorded bytes for {node.node_id}.",
                )
            if (
                agent_node is None
                or agent_node.state != "active"
                or agent_node.revoked_at is not None
            ):
                blocked(
                    "reconcile.agent_unavailable",
                    f"Node {node.node_id} is not an active authorized target.",
                )
            capabilities = set(agent_node.capabilities or [])
            required_reconciliation_capabilities = {
                "recipe.reconcile",
                RECIPE_RECONCILE_FEATURE_CAPABILITY,
                EXACT_LIFECYCLE_RESUME_CAPABILITY,
            }
            if not required_reconciliation_capabilities <= capabilities:
                blocked(
                    "reconcile.agent_upgrade_required",
                    f"Node {node.node_id} must advertise recipe.reconcile, recipe.reconcile.v1, and agent.lifecycle.resume.exact.v1 before safe reconciliation can be authorized.",
                )
            if not _lower_hex_digest(operation.payload_digest):
                blocked(
                    "reconcile.install_provenance_mismatch",
                    f"The original install payload digest for {node.node_id} is invalid.",
                )
            targets.append(
                self._reconciliation_target_in_session(
                    session,
                    installation=installation,
                    revision=revision,
                    node=node,
                    install_operation_id=operation.id,
                    install_operation_payload_sha256=operation.payload_digest,
                    compiled_spec_canonical_sha256=hashlib.sha256(
                        canonical_message(compiled)
                    ).hexdigest(),
                )
            )

        return InstallationReconciliationAuthority(
            installation_id=installation.id,
            original_plan_digest=installation.plan_digest,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            mapping_id=mapping.id,
            mapping_generation=mapping.generation,
            recipe_build_id=installation.recipe_build_id,
            image_digest=installation.image_digest,
            model_content_sha256=recipe_model_content_sha256,
            stored_plan_canonical_sha256=stored_plan_sha256,
            targets=tuple(targets),
        )

    @staticmethod
    def _reconciliation_target_in_session(
        session: Session,
        *,
        installation: RecipeInstallation,
        revision: CatalogDocumentRevision,
        node: InstallationNode,
        install_operation_id: str,
        install_operation_payload_sha256: str,
        compiled_spec_canonical_sha256: str,
    ) -> InstallationReconciliationTarget:
        """Carry forward only a prior receipt proven by its exact child op."""

        expected = {
            "installation_id": installation.id,
            "install_operation_id": install_operation_id,
            "install_operation_payload_sha256": install_operation_payload_sha256,
            "plan_digest": installation.plan_digest,
            "recipe_revision_id": revision.id,
            "recipe_content_sha256": revision.content_digest,
            "compiled_spec_canonical_sha256": compiled_spec_canonical_sha256,
        }
        reconciliation_jobs = tuple(
            session.scalars(
                select(Job)
                .where(
                    Job.kind == "recipe.reconcile",
                    Job.payload["owner_kind"].as_string() == "installation",
                    Job.payload["owner_id"].as_string() == installation.id,
                )
                .order_by(Job.created_at.desc(), Job.id.desc())
            )
        )
        prior_receipt: RecipeReconcileResult | None = None
        for job in reconciliation_jobs:
            authority = job.payload.get("reconciliation_authority")
            authority_targets = (
                authority.get("targets") if isinstance(authority, Mapping) else None
            )
            target_authority = next(
                (
                    target
                    for target in authority_targets
                    if isinstance(target, Mapping)
                    and target.get("node_id") == node.node_id
                ),
                None,
            ) if isinstance(authority_targets, list) else None
            if (
                not isinstance(authority, Mapping)
                or authority.get("schema_version") != 2
                or authority.get("installation_id") != installation.id
                or authority.get("original_plan_digest") != installation.plan_digest
                or authority.get("recipe_revision_id") != revision.id
                or authority.get("recipe_content_sha256") != revision.content_digest
                or target_authority is None
                or any(
                    target_authority.get(key) != value
                    for key, value in expected.items()
                    if key not in {"installation_id", "plan_digest", "recipe_revision_id", "recipe_content_sha256"}
                )
            ):
                continue
            result = job.result
            evidence_by_node = (
                result.get("node_evidence") if isinstance(result, Mapping) else None
            )
            evidence = (
                evidence_by_node.get(node.node_id)
                if isinstance(evidence_by_node, Mapping)
                else None
            )
            if not isinstance(evidence, Mapping):
                continue
            try:
                receipt = RecipeReconcileResult.model_validate_json(
                    canonical_message(evidence)
                )
            except (TypeError, ValueError):
                continue
            receipt_identity = {
                "node_id": receipt.node_id,
                "installation_id": receipt.installation_id,
                "install_operation_id": receipt.install_operation_id,
                "install_operation_payload_sha256": (
                    receipt.install_operation_payload_sha256
                ),
                "plan_digest": receipt.plan_digest,
                "recipe_revision_id": receipt.recipe_revision_id,
                "recipe_content_sha256": receipt.recipe_content_sha256,
                "compiled_spec_canonical_sha256": (
                    receipt.compiled_spec_canonical_sha256
                ),
            }
            if receipt_identity != {"node_id": node.node_id, **expected}:
                continue
            children = tuple(
                session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == job.id,
                        AgentOperation.node_id == node.node_id,
                    )
                )
            )
            if len(children) != 1:
                continue
            child = children[0]
            try:
                child_payload = RecipeReconcilePayload.model_validate_json(
                    canonical_message(child.payload)
                )
            except (TypeError, ValueError):
                continue
            attempt = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == child.id,
                    AgentOperationAttempt.attempt == child.current_attempt,
                )
            )
            if (
                child.kind != "recipe.reconcile"
                or child.state != "succeeded"
                or child.current_attempt < 1
                or hashlib.sha256(canonical_message(child.payload)).hexdigest()
                != child.payload_digest
                or child_payload.node_id != node.node_id
                or child_payload.installation_id != installation.id
                or child_payload.install_operation_id != install_operation_id
                or child_payload.install_operation_payload_sha256
                != install_operation_payload_sha256
                or child_payload.plan_digest != installation.plan_digest
                or child_payload.recipe_revision_id != revision.id
                or child_payload.recipe_content_sha256 != revision.content_digest
                or child_payload.compiled_spec_canonical_sha256
                != compiled_spec_canonical_sha256
                or attempt is None
                or attempt.state != "succeeded"
                or not isinstance(attempt.result, Mapping)
                or canonical_message(attempt.result) != canonical_message(evidence)
            ):
                continue
            prior_receipt = receipt
            break

        if node.state == "uninstalled":
            if (
                prior_receipt is None
                or prior_receipt.cleanup_receipt_sha256 != node.evidence_digest
            ):
                raise RecipeReconciliationBlocked(
                    "reconcile.prior_effect_unproven",
                    f"Node {node.node_id} is marked uninstalled without its exact durable reconciliation receipt.",
                )
            state = "reconciled"
            cleanup_receipt_sha256 = prior_receipt.cleanup_receipt_sha256
        elif prior_receipt is not None:
            raise RecipeReconciliationBlocked(
                "reconcile.prior_effect_mismatch",
                f"Node {node.node_id} has a cleanup receipt that conflicts with its current installation state.",
            )
        else:
            # `failed` or `installing` may be a stale SQL projection left by a
            # cancelled attempt. The exact successful source install still
            # authorizes a fresh identity-bound reconciliation; the agent
            # checks its current local effects and tombstone before changing
            # anything.
            state = "pending"
            cleanup_receipt_sha256 = None
        return InstallationReconciliationTarget(
            node_id=node.node_id,
            rank=node.rank,
            role=node.role,
            installed_bytes=node.installed_bytes,
            install_operation_id=install_operation_id,
            install_operation_payload_sha256=install_operation_payload_sha256,
            compiled_spec_canonical_sha256=compiled_spec_canonical_sha256,
            state=state,
            cleanup_receipt_sha256=cleanup_receipt_sha256,
        )

    def reconcile_installation(
        self,
        installation_id: str,
        *,
        expected_authority: Mapping[str, object],
        run_switch_plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        """Queue exact managed cleanup under a current Run/Switch review."""

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
                    )
                except AdmissionLockBusy as error:
                    raise InstallAdmissionBusy("reconcile.capacity_busy") from error
                authority = self._reconciliation_authority_in_session(
                    session, installation_id, lock=True
                )
                if canonical_message(authority.document()) != canonical_message(
                    expected_authority
                ):
                    raise RecipeOperationConflict(
                        "reconciliation authority changed after preview"
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
                    target
                    for target in authority.targets
                    if target.state == "pending"
                )
                if not pending_targets:
                    raise RecipeOperationConflict(
                        "reconciliation has no pending targets but is not complete"
                    )
                payloads = tuple(
                    (
                        target.node_id,
                        RecipeReconcilePayload(
                            schema_version=1,
                            node_id=target.node_id,
                            installation_id=authority.installation_id,
                            install_operation_id=target.install_operation_id,
                            install_operation_payload_sha256=(
                                target.install_operation_payload_sha256
                            ),
                            plan_digest=authority.original_plan_digest,
                            recipe_revision_id=authority.recipe_revision_id,
                            recipe_content_sha256=authority.recipe_content_sha256,
                            compiled_spec_canonical_sha256=(
                                target.compiled_spec_canonical_sha256
                            ),
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
            raise InstallAdmissionBusy("reconcile.capacity_busy") from error
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
            raise RecipeOperationConflict(
                "request key was already used differently"
            ) from error
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def uninstall(
        self,
        installation_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        existing = self._idempotent(
            request_id,
            "recipe.uninstall",
            plan_digest,
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
                    raise RecipeOperationConflict("recipe installation does not exist")
                existing = self._idempotent_in_session(
                    session,
                    request_id,
                    "recipe.uninstall",
                    plan_digest,
                    owner_kind="installation",
                    owner_id=installation_id,
                )
                if existing is not None:
                    return existing
                plan = self._uninstall_plan_in_session(
                    session, installation_id, lock=True
                )
                if not plan.allowed:
                    raise RecipeOperationConflict(
                        "uninstall plan is stale or blocked: "
                        + "; ".join(reason.code for reason in plan.blockers)
                    )
                if plan.disposition != "uninstall":
                    # A plan that never reached a node is abandoned by the
                    # cleanup phase; it must never queue agent removal work.
                    raise RecipeOperationConflict(
                        "installation was never installed; abandon it instead "
                        "of uninstalling it"
                    )
                if plan.plan_digest != plan_digest:
                    raise RecipeOperationConflict("uninstall plan is stale or blocked")
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
                                "schema_version": 1,
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
                        if node.state != "uninstalled"
                    ),
                    authority_digest=plan.installation_authority_digest,
                    now=now,
                    workload_intent_ordinal=workload_intent_ordinal,
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
            raise RecipeOperationConflict(
                "request key was already used differently"
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
                raise RecipeOperationConflict("recipe installation does not exist")
            if installation.state == "uninstalled":
                # A restarted cleanup phase replays the disposal.  The row is
                # already resolved, so the replay succeeds instead of failing
                # the operation after the effect has landed.
                return {
                    "installation_id": installation_id,
                    "disposition": "abandoned",
                }
            plan = self._uninstall_plan_in_session(session, installation_id, lock=True)
            if not plan.allowed or plan.disposition != "abandon":
                raise RecipeOperationConflict(
                    "installation is not a never-installed plan; it cannot be abandoned"
                )
            nodes = tuple(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id == installation_id)
                    .with_for_update(of=InstallationNode)
                )
            )
            for node in nodes:
                node.state = "uninstalled"
                node.updated_at = now
            installation.state = "uninstalled"
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
            if previous is None:
                raise RecipeOperationConflict("recipe operation is not retryable")
            previous_plan_digest = _required_string(previous.payload, "plan_digest")
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if existing is not None:
                if (
                    existing.kind != previous.kind
                    or _required_string(existing.payload, "plan_digest")
                    != previous_plan_digest
                ):
                    raise RecipeOperationConflict("request key was already used")
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
            elif previous.kind == "recipe.image.import.v1":
                job = self._retry_image_distribution_in_session(
                    session, previous, actor=actor, request_id=request_id, now=now
                )
            elif previous.kind == "recipe.install" and previous.state == "failed":
                job = self._retry_install_in_session(
                    session, previous, actor=actor, request_id=request_id, now=now
                )
            else:
                raise RecipeOperationConflict("recipe operation is not retryable")
        self._agent_jobs.notify_available()
        return self.get(job.id)

    def _retry_image_distribution_in_session(
        self,
        session: Session,
        previous: Job,
        *,
        actor: str,
        request_id: str,
        now: datetime,
    ) -> Job:
        if previous.state not in _RETRYABLE_IMAGE_DISTRIBUTION_STATES:
            raise RecipeOperationConflict("recipe image distribution is not retryable")
        plan_digest = _required_string(previous.payload, "plan_digest")
        owner_id = _required_string(previous.payload, "owner_id")
        active = any(
            job.id != previous.id
            and job.state in {"queued", "running"}
            and isinstance(job.payload, Mapping)
            and job.payload.get("owner_id") == owner_id
            and job.payload.get("plan_digest") == plan_digest
            for job in session.scalars(
                select(Job).where(Job.kind == "recipe.image.import.v1")
            )
        )
        if active:
            raise RecipeOperationConflict(
                "recipe image distribution already has an active retry"
            )
        children = tuple(
            session.scalars(
                select(AgentOperation)
                .where(AgentOperation.parent_job_id == previous.id)
                .order_by(AgentOperation.node_id)
            )
        )
        payload_identities = {
            (
                child.payload.get("mapping_id"),
                child.payload.get("mapping_generation"),
                child.payload.get("source_node_id"),
                child.payload.get("image_digest"),
                child.payload.get("oci_layout_sha256"),
                child.payload.get("image_bytes"),
            )
            for child in children
            if isinstance(child.payload, Mapping)
        }
        if (
            previous.payload.get("owner_kind") != "image-distribution"
            or not children
            or tuple(child.node_id for child in children)
            != tuple(sorted(previous.targets))
            or previous.authority_revision != plan_digest.removeprefix("sha256:")
            or len(payload_identities) != 1
            or any(
                child.kind != "recipe.image.import.v1"
                or child.state not in _RETRYABLE_IMAGE_OPERATION_STATES
                or child.authority_revision != plan_digest.removeprefix("sha256:")
                or child.payload_digest
                != hashlib.sha256(canonical_message(child.payload)).hexdigest()
                or not _valid_image_import_payload(child.payload, owner_id)
                for child in children
            )
        ):
            raise RecipeOperationConflict("stored recipe image distribution is invalid")
        return self._queue_in_session(
            session,
            kind="recipe.image.import.v1",
            owner_kind="image-distribution",
            owner_id=owner_id,
            plan_digest=plan_digest,
            actor=actor,
            request_id=request_id,
            node_payloads=tuple(
                (child.node_id, dict(child.payload)) for child in children
            ),
            authority_digest=plan_digest,
            now=now,
            workload_intent_ordinal=_bound_workload_intent(previous),
        )

    def _retry_install_in_session(
        self,
        session: Session,
        previous: Job,
        *,
        actor: str,
        request_id: str,
        now: datetime,
    ) -> Job:
        previous_plan_digest = _required_string(previous.payload, "plan_digest")
        owner_id = _required_string(previous.payload, "owner_id")
        installation = session.get(RecipeInstallation, owner_id, with_for_update=True)
        if installation is None or installation.state not in {"partial", "failed"}:
            raise RecipeOperationConflict("recipe installation is not retryable")
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
        try:
            stored_installation_plan = parse_stored_installation_plan(installation.plan)
        except RecipeExecutionContractError as error:
            raise RecipeOperationConflict(
                "stored compiled execution plan is missing for install retry"
            ) from error
        compiled_plans = stored_installation_plan.compiled_execution_plans
        if (
            not compiled_plans
            or not nodes
            or any(node.node_id not in compiled_plans for node in nodes)
        ):
            raise RecipeOperationConflict(
                "stored compiled execution plan is missing for install retry"
            )
        installation.state = "installing"
        installation.updated_at = now
        for node in nodes:
            node.state = "planned"
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
                        "schema_version": 2,
                        "installation_id": owner_id,
                        "plan_digest": previous_plan_digest,
                        "rank": node.rank,
                        "role": node.role,
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
        if previous.state not in {"failed", "waiting-for-operator", "expired"}:
            raise RecipeOperationConflict("recipe build is not retryable")
        if build_cancellation(previous) is not None:
            raise RecipeOperationConflict(
                "cancelled recipe build intent is not retryable"
            )
        if self._builds is None:
            raise RecipeOperationConflict("recipe build service is unavailable")
        owner_id = _required_string(previous.payload, "owner_id")
        build = session.get(RecipeBuild, owner_id, with_for_update=True)
        if build is None or build.state not in {"building", "failed"}:
            raise RecipeOperationConflict("recipe build is not retryable")
        active = any(
            job.id != previous.id
            and job.state in {"queued", "running"}
            and isinstance(job.payload, Mapping)
            and job.payload.get("owner_id") == owner_id
            for job in session.scalars(select(Job).where(Job.kind == "recipe.build.v1"))
        )
        if active:
            raise RecipeOperationConflict("recipe build already has an active retry")
        try:
            payload = build_plan_document(build.plan)
            parsed_payload = parse_stored_build_plan(payload)
            plan = RecipeBuildPlan(
                build_id=owner_id,
                recipe_revision_id=parsed_payload.recipe_revision_id,
                recipe_content_sha256=parsed_payload.recipe_content_sha256,
                builder_node_id=build.builder_node_id,
                source_bundle_sha256=parsed_payload.source_bundle_sha256,
                build_input_sha256=build.build_input_sha256,
                agent_payload=payload,
            )
        except (RecipeExecutionContractError, KeyError, TypeError) as error:
            detail = (
                error.detail if isinstance(error, RecipeExecutionContractError) else ""
            )
            raise RecipeOperationConflict(
                "stored recipe build plan is invalid" + detail
            ) from error
        if (
            payload.get("build_id") != owner_id
            or payload.get("build_input_sha256") != build.build_input_sha256
        ):
            raise RecipeOperationConflict("stored recipe build plan is invalid")
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
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id)
            if job is None or not job.kind.startswith("recipe."):
                raise KeyError(operation_id)
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
                raise RecipeOperationConflict("node is not part of operation group")
            operation.state = "succeeded" if succeeded else "failed"
            operation.updated_at = now
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
            if (
                not isinstance(job.result, Mapping)
                or job.result.get("cancel_requested") is not True
            ):
                raise RecipeOperationConflict("recipe cancellation was not requested")
            owner_id = _required_string(job.payload, "owner_id")
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
                    raise RecipeOperationConflict(
                        "installation cancellation scope changed"
                    )
                node.state = "failed"
                node.updated_at = now
                installation.state = "partial"
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
                    raise RecipeOperationConflict("run cancellation scope changed")
                # The agent sends cancelled only after its exact host STOP has
                # returned. Keep reservations for the current intent's stop.
                node.state = "stopped"
                node.updated_at = now
                run.state = "lost"
                run.route_state = "withdrawn"
                run.updated_at = now
            return
        if state == "waiting-for-operator" and isinstance(result, Mapping):
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
            and operation.state == "waiting-for-operator"
            and operation.retry_disposition == "retry"
            and operation.retry_disposition_attempt == operation.current_attempt
            and isinstance(result, Mapping)
        ):
            parsed = validate_result_for_operation(operation.kind, result, state=state)
            if isinstance(parsed, AgentFailureResult):
                # The queue owner has already admitted this exact retry using
                # its failure policy. Do not independently restate that policy
                # here or turn its scheduled cleanup into a terminal failure.
                return
        if state not in {"succeeded", "failed"} or not isinstance(result, Mapping):
            raise RecipeOperationConflict("recipe agent result is invalid")
        if state == "succeeded" and job.kind in {
            "recipe.stop",
            "recipe.uninstall",
            "recipe.reconcile",
        }:
            try:
                parsed_result = parse_recipe_operation_result(
                    ProtocolAgentOperation(job.kind), result
                )
            except (KeyError, ValueError) as error:
                raise RecipeOperationConflict(
                    "recipe operation result is invalid"
                ) from error
            raw_evidence = parsed_result.model_dump(mode="json")
        else:
            raw_evidence = result.get("evidence", result)
            if not isinstance(raw_evidence, Mapping):
                raise RecipeOperationConflict("recipe agent evidence is invalid")
            if raw_evidence is not result and "evidence_digest" in result:
                raw_evidence = {
                    **raw_evidence,
                    "evidence_digest": result["evidence_digest"],
                }
        self._project_node_result(
            session,
            job,
            operation,
            succeeded=state == "succeeded",
            evidence=raw_evidence,
            now=self._clock(),
        )

    @staticmethod
    def _reconciliation_job_has_exact_receipts(
        session: Session, job: Job, children: Sequence[AgentOperation]
    ) -> bool:
        """Prove a full topology receipt before releasing its reservation."""

        authority = job.payload.get("reconciliation_authority")
        targets_value = authority.get("targets") if isinstance(authority, Mapping) else None
        if (
            not isinstance(authority, Mapping)
            or authority.get("schema_version") != 2
            or authority.get("installation_id")
            != job.payload.get("owner_id")
            or not isinstance(targets_value, list)
            or not targets_value
            or not all(isinstance(target, Mapping) for target in targets_value)
        ):
            return False
        targets = {str(target["node_id"]): target for target in targets_value}
        if len(targets) != len(targets_value):
            return False
        pending = {
            node_id: target
            for node_id, target in targets.items()
            if target.get("state") == "pending"
        }
        if (
            not pending
            or job.targets != sorted(pending)
            or len(children) != len(pending)
            or {child.node_id for child in children} != set(pending)
            or any(child.state != "succeeded" for child in children)
        ):
            return False
        result = job.result
        evidence_by_node = (
            result.get("node_evidence") if isinstance(result, Mapping) else None
        )
        if (
            not isinstance(evidence_by_node, Mapping)
            or set(evidence_by_node) != set(pending)
        ):
            return False
        installation_id = str(authority["installation_id"])
        node_rows = tuple(
            session.scalars(
                select(InstallationNode).where(
                    InstallationNode.installation_id == installation_id
                )
            )
        )
        if {node.node_id for node in node_rows} != set(targets):
            return False
        node_by_id = {node.node_id: node for node in node_rows}
        for node_id, target in targets.items():
            node = node_by_id[node_id]
            if (
                target.get("rank") != node.rank
                or target.get("role") != node.role
                or target.get("installed_bytes") != node.installed_bytes
                or node.state != "uninstalled"
            ):
                return False
            if target.get("state") == "reconciled":
                receipt_sha256 = target.get("cleanup_receipt_sha256")
            elif target.get("state") == "pending":
                raw_evidence = evidence_by_node.get(node_id)
                if not isinstance(raw_evidence, Mapping):
                    return False
                try:
                    evidence = RecipeReconcileResult.model_validate_json(
                        canonical_message(raw_evidence)
                    )
                except (TypeError, ValueError):
                    return False
                expected = {
                    "node_id": node_id,
                    "installation_id": installation_id,
                    "install_operation_id": target.get("install_operation_id"),
                    "install_operation_payload_sha256": target.get(
                        "install_operation_payload_sha256"
                    ),
                    "plan_digest": authority.get("original_plan_digest"),
                    "recipe_revision_id": authority.get("recipe_revision_id"),
                    "recipe_content_sha256": authority.get(
                        "recipe_content_sha256"
                    ),
                    "compiled_spec_canonical_sha256": target.get(
                        "compiled_spec_canonical_sha256"
                    ),
                }
                observed = evidence.model_dump(mode="json")
                if any(observed.get(key) != value for key, value in expected.items()):
                    return False
                receipt_sha256 = evidence.cleanup_receipt_sha256
                child = next(
                    (item for item in children if item.node_id == node_id), None
                )
                if child is None or child.current_attempt < 1:
                    return False
                try:
                    expected_payload = RecipeReconcilePayload(
                        schema_version=1,
                        node_id=node_id,
                        installation_id=installation_id,
                        install_operation_id=str(target["install_operation_id"]),
                        install_operation_payload_sha256=str(
                            target["install_operation_payload_sha256"]
                        ),
                        plan_digest=str(authority["original_plan_digest"]),
                        recipe_revision_id=str(authority["recipe_revision_id"]),
                        recipe_content_sha256=str(
                            authority["recipe_content_sha256"]
                        ),
                        compiled_spec_canonical_sha256=str(
                            target["compiled_spec_canonical_sha256"]
                        ),
                    )
                except (KeyError, TypeError, ValueError):
                    return False
                if (
                    child.kind != "recipe.reconcile"
                    or not isinstance(child.payload, Mapping)
                    or canonical_message(child.payload)
                    != canonical_message(expected_payload.model_dump(mode="json"))
                    or hashlib.sha256(canonical_message(child.payload)).hexdigest()
                    != child.payload_digest
                ):
                    return False
                attempt = session.scalar(
                    select(AgentOperationAttempt).where(
                        AgentOperationAttempt.operation_id == child.id,
                        AgentOperationAttempt.attempt == child.current_attempt,
                    )
                )
                if (
                    attempt is None
                    or attempt.state != "succeeded"
                    or not isinstance(attempt.result, Mapping)
                    or canonical_message(attempt.result)
                    != canonical_message(evidence.model_dump(mode="json"))
                ):
                    return False
            else:
                return False
            if node.evidence_digest != receipt_sha256:
                return False
        return True

    def _project_node_result(
        self,
        session: Session,
        job: Job,
        operation: AgentOperation,
        *,
        succeeded: bool,
        evidence: Mapping[str, object],
        now: datetime,
    ) -> bool:
        cleanup_queued = False
        locked_job = session.get(Job, job.id, with_for_update=True)
        if locked_job is None:
            raise RecipeOperationConflict("recipe operation disappeared")
        job = locked_job
        # Lock acquisition can outlive a retained recovery deadline. Every
        # phase/terminal transition uses a fresh time sampled inside the lock.
        now = self._clock()
        operation.updated_at = now
        node_id = operation.node_id
        owner_id = _required_string(job.payload, "owner_id")
        if job.kind == "recipe.build.cleanup.v1":
            if succeeded:
                receipt = RecipeBuildCleanupEvidence.model_validate_json(
                    canonical_message(evidence)
                )
                expected = RecipeBuildCleanupRequest.model_validate_json(
                    canonical_message(operation.payload)
                )
                if (
                    receipt.build_id != owner_id
                    or receipt.build_id != expected.build_id
                    or receipt.operation_id != expected.operation_id
                ):
                    raise RecipeOperationConflict(
                        "recipe build cleanup evidence does not match its authority"
                    )
                original = session.get(
                    AgentOperation, receipt.operation_id, with_for_update=True
                )
                original_job = (
                    None
                    if original is None
                    else session.get(Job, original.parent_job_id, with_for_update=True)
                )
                build = session.get(RecipeBuild, owner_id, with_for_update=True)
                if (
                    original is None
                    or original.node_id != node_id
                    or original.kind != "recipe.build.v1"
                    or original_job is None
                    or original_job.payload.get("owner_id") != owner_id
                    or build is None
                ):
                    raise RecipeOperationConflict(
                        "recipe build cleanup authority changed"
                    )
                cancellation = build_cancellation(original_job)
                requested = RecipeOperationCancellationResult.model_validate_json(
                    canonical_message(job.payload["build_cancellation"])
                )
                if (
                    cancellation is None
                    or cancellation.cancel_request_id != requested.cancel_request_id
                    or cancellation.cancel_actor != requested.cancel_actor
                    or cancellation.cancel_requested_at != requested.cancel_requested_at
                    or cancellation.reason != requested.reason
                ):
                    raise RecipeOperationConflict(
                        "recipe build cleanup authority changed"
                    )
                original.state = "cancelled"
                original.updated_at = now
                original_job.state = "cancelled"
                original_job.result = cancellation.model_copy(
                    update={"cancelled": True}
                ).model_dump(mode="json", exclude_none=True)
                original_job.updated_at = now
                self._release_cancelled_build(session, owner_id, now)
        elif job.kind == "recipe.build.v1":
            build = session.get(RecipeBuild, owner_id, with_for_update=True)
            if build is None or build.builder_node_id != node_id:
                raise RecipeOperationConflict("recipe build authority is invalid")
            cancellation = build_cancellation(job)
            if cancellation is not None:
                # Completion can race cancellation. Retain the authenticated
                # attempt's outcome, but only its own cancellation governs it.
                # A later attempt may already own this build and its capacity.
                if cancellation.cancelled is True:
                    operation.state = "cancelled"
                    job.state = "cancelled"
                else:
                    job.state = "waiting-for-operator"
                return False
            force_rebuild = job.payload.get("force_rebuild") is True
            if succeeded:
                _record_build_evidence(
                    session,
                    build,
                    evidence,
                    now=now,
                    replace_existing=force_rebuild,
                )
            else:
                # A forced rebuild is an attempt to replace a still-valid
                # receipt.  Keep that receipt authoritative if the new
                # attempt fails; the availability operation itself reports
                # the failed attempt and remains retryable.
                build.state = "succeeded" if force_rebuild else "failed"
                build.error = str(evidence.get("reason", "agent build failed"))[:512]
                build.updated_at = now
        elif job.kind == "recipe.image.import.v1":
            _record_image_import_evidence(session, operation, evidence, succeeded, now)
        elif job.kind == "recipe.install":
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            assert node is not None
            node.state = "installed" if succeeded else "failed"
            if succeeded:
                installed_bytes = evidence.get("installed_bytes")
                if not isinstance(installed_bytes, int) or installed_bytes < 0:
                    raise RecipeOperationConflict("install evidence is invalid")
                node.installed_bytes = installed_bytes
            node.updated_at = now
        elif job.kind in {"recipe.start", "recipe.stop"}:
            node = session.scalar(
                select(RunNode).where(
                    RunNode.run_id == owner_id, RunNode.node_id == node_id
                )
            )
            assert node is not None
            start_phase = (
                operation.payload.get("phase") if job.kind == "recipe.start" else None
            )
            if start_phase == "rank-launch":
                if not succeeded:
                    node.state = "failed"
                elif node.state != "failed":
                    node.state = "starting"
                if succeeded:
                    node.evidence_digest = _validate_rank_launch_evidence(
                        session, owner_id, operation, evidence
                    )
                node.updated_at = now
            elif start_phase == "collective-readiness":
                if not succeeded:
                    node.state = "failed"
                if succeeded:
                    endpoint, digest = _validate_start_evidence(
                        session, owner_id, operation, evidence
                    )
                    recorded = _validated_result(job.kind, job.result) or {}
                    launches = recorded.get("launch_evidence")
                    expected_generation = operation.payload.get("run_generation")
                    for started_node in session.scalars(
                        select(RunNode).where(RunNode.run_id == owner_id)
                    ):
                        launch = (
                            launches.get(started_node.node_id)
                            if isinstance(launches, Mapping)
                            else None
                        )
                        if not isinstance(launch, Mapping) or (
                            expected_generation is not None
                            and launch.get("run_generation") != expected_generation
                        ):
                            raise RecipeOperationConflict(
                                "collective readiness preceded rank launch"
                            )
                        started_node.state = "running"
                        started_node.updated_at = now
                    try:
                        node.endpoint = run_endpoint_document({"url": endpoint})
                    except RecipeExecutionContractError as error:
                        raise RecipeOperationConflict(
                            "recipe start endpoint evidence is invalid"
                        ) from error
                    node.evidence_digest = digest
                node.updated_at = now
            else:
                node.state = (
                    "running"
                    if job.kind == "recipe.start" and succeeded
                    else "stopped"
                    if succeeded
                    else "failed"
                )
                if job.kind == "recipe.start" and succeeded:
                    endpoint, digest = _validate_start_evidence(
                        session, owner_id, operation, evidence
                    )
                    try:
                        node.endpoint = run_endpoint_document({"url": endpoint})
                    except RecipeExecutionContractError as error:
                        raise RecipeOperationConflict(
                            "recipe start endpoint evidence is invalid"
                        ) from error
                    node.evidence_digest = digest
                node.updated_at = now
            if (
                job.kind == "recipe.start"
                and succeeded
                and start_phase != "rank-launch"
            ):
                run = session.get(RecipeRun, owner_id)
                assert run is not None
                if _stored_run_plan(run.plan).get("observation_schema_version") == 2:
                    run.observation_deadline_at = now + timedelta(
                        seconds=_INITIAL_OBSERVATION_GRACE_SECONDS
                    )
                    for started_node in session.scalars(
                        select(RunNode).where(RunNode.run_id == owner_id)
                    ):
                        started_node.observed_run_generation = None
                        started_node.observation_receipt_sha256 = None
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
                raise RecipeOperationConflict("reconciliation scope changed")
            authority = job.payload.get("reconciliation_authority")
            authority_targets = (
                authority.get("targets") if isinstance(authority, Mapping) else None
            )
            target = next(
                (
                    item
                    for item in authority_targets
                    if isinstance(item, Mapping) and item.get("node_id") == node_id
                ),
                None,
            ) if isinstance(authority_targets, list) else None
            try:
                expected = RecipeReconcilePayload.model_validate_json(
                    canonical_message(operation.payload)
                )
            except (TypeError, ValueError) as error:
                raise RecipeOperationConflict(
                    "reconciliation operation payload is invalid"
                ) from error
            if (
                not isinstance(authority, Mapping)
                or authority.get("schema_version") != 2
                or authority.get("installation_id") != owner_id
                or authority.get("original_plan_digest") != expected.plan_digest
                or authority.get("recipe_revision_id") != expected.recipe_revision_id
                or authority.get("recipe_content_sha256")
                != expected.recipe_content_sha256
                or target is None
                or target.get("state") != "pending"
                or target.get("cleanup_receipt_sha256") is not None
                or target.get("rank") != node.rank
                or target.get("role") != node.role
                or target.get("installed_bytes") != node.installed_bytes
                or expected.node_id != node_id
                or expected.installation_id != owner_id
                or expected.install_operation_id
                != target.get("install_operation_id")
                or expected.install_operation_payload_sha256
                != target.get("install_operation_payload_sha256")
                or expected.compiled_spec_canonical_sha256
                != target.get("compiled_spec_canonical_sha256")
                or job.targets != sorted(
                    str(item.get("node_id"))
                    for item in authority_targets
                    if isinstance(item, Mapping) and item.get("state") == "pending"
                )
            ):
                raise RecipeOperationConflict(
                    "reconciliation operation differs from its reviewed authority"
                )
            if succeeded:
                try:
                    receipt = RecipeReconcileResult.model_validate_json(
                        canonical_message(evidence)
                    )
                except (TypeError, ValueError) as error:
                    raise RecipeOperationConflict(
                        "reconciliation receipt is invalid"
                    ) from error
                if (
                    receipt.node_id != expected.node_id
                    or receipt.installation_id != expected.installation_id
                    or receipt.install_operation_id != expected.install_operation_id
                    or receipt.install_operation_payload_sha256
                    != expected.install_operation_payload_sha256
                    or receipt.plan_digest != expected.plan_digest
                    or receipt.recipe_revision_id != expected.recipe_revision_id
                    or receipt.recipe_content_sha256
                    != expected.recipe_content_sha256
                    or receipt.compiled_spec_canonical_sha256
                    != expected.compiled_spec_canonical_sha256
                ):
                    raise RecipeOperationConflict(
                        "reconciliation receipt does not match its exact authority"
                    )
                node.state = "uninstalled"
                node.evidence_digest = receipt.cleanup_receipt_sha256
            else:
                node.state = "failed"
            node.updated_at = now
        elif job.kind == "recipe.uninstall":
            node = session.scalar(
                select(InstallationNode).where(
                    InstallationNode.installation_id == owner_id,
                    InstallationNode.node_id == node_id,
                )
            )
            assert node is not None
            node.state = "uninstalled" if succeeded else "failed"
            node.updated_at = now
        recorded_result = _validated_result(job.kind, job.result) or {}
        evidence_field = (
            "launch_evidence"
            if job.kind == "recipe.start"
            and (
                operation.payload.get("phase") == "rank-launch"
                or operation.payload.get("phase") is None
            )
            else "node_evidence"
        )
        raw_node_evidence = recorded_result.get(evidence_field, {})
        if not isinstance(raw_node_evidence, Mapping):
            raise RecipeOperationConflict("recipe operation evidence is invalid")
        node_evidence = {
            str(recorded_node): dict(recorded_evidence)
            for recorded_node, recorded_evidence in raw_node_evidence.items()
            if isinstance(recorded_node, str) and isinstance(recorded_evidence, Mapping)
        }
        if len(node_evidence) != len(raw_node_evidence):
            raise RecipeOperationConflict("recipe operation evidence is invalid")
        observed_evidence = json.loads(canonical_message(evidence))
        if node_id in node_evidence and node_evidence[node_id] != observed_evidence:
            raise RecipeOperationConflict("recipe node evidence changed")
        node_evidence[node_id] = observed_evidence
        job.result = _validated_result(
            job.kind,
            {**recorded_result, evidence_field: node_evidence},
        )
        children = tuple(
            session.scalars(
                select(AgentOperation).where(AgentOperation.parent_job_id == job.id)
            )
        )
        phases = _stored_phases(job.payload)
        recovery_error: DistributedLifecycleError | None = None
        if phases:
            try:
                _enforce_start_deadline(job.payload, now=now)
                enforce_recovery_deadline(job.payload, now=now)
            except DistributedLifecycleError as error:
                recovery_error = error
                for child in children:
                    if child.state not in _TERMINAL_JOB_STATES:
                        child.state = "failed"
                        child.updated_at = now
            phase_index = _current_phase_index(children, phases)
            if phase_index is not None:
                phase_operations = {
                    operation_id
                    for operation_id, _node_id, _payload in phases[phase_index]
                }
                phase_children = tuple(
                    child for child in children if child.id in phase_operations
                )
                if any(
                    child.state not in _TERMINAL_JOB_STATES for child in phase_children
                ):
                    job.state = "running"
                    job.updated_at = now
                    return cleanup_queued
                if (
                    all(child.state == "succeeded" for child in phase_children)
                    and phase_index + 1 < len(phases)
                    and recovery_error is None
                ):
                    for (
                        next_operation_id,
                        next_node_id,
                        next_payload,
                    ) in phases[phase_index + 1]:
                        self._agent_jobs.enqueue_in_session(
                            session,
                            job.id,
                            next_node_id,
                            job.kind,
                            job.authority_revision,
                            next_payload,
                            operation_id=next_operation_id,
                        )
                    job.state = "running"
                    job.updated_at = now
                    return True
        terminal = all(child.state in _TERMINAL_JOB_STATES for child in children)
        if terminal:
            successful = sorted(
                {child.node_id for child in children if child.state == "succeeded"}
            )
            failed = sorted(
                {child.node_id for child in children if child.state == "failed"}
            )
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
                    and self._reconciliation_job_has_exact_receipts(
                        session, job, children
                    )
                )
                if not failed and not reconciliation_complete:
                    reconciliation_error = (
                        "complete topology cleanup lacks exact durable receipts"
                    )
            if job.kind == "recipe.start" and recovery_error is None:
                try:
                    _enforce_start_deadline(job.payload, now=now)
                    enforce_recovery_deadline(job.payload, now=now)
                except DistributedLifecycleError as error:
                    recovery_error = error
            start_failed = bool(failed) or recovery_error is not None
            job_failed = (
                start_failed
                or reconciliation_error is not None
                or (job.kind == "recipe.reconcile" and not reconciliation_complete)
            )
            job.state = "failed" if job_failed else "succeeded"
            stored_result = job.result
            projected_result = (
                dict(stored_result) if isinstance(stored_result, Mapping) else {}
            )
            final_result = {
                "successful_nodes": successful,
                "failed_nodes": failed,
                "node_evidence": projected_result.get("node_evidence", {}),
            }
            if "launch_evidence" in projected_result:
                final_result["launch_evidence"] = projected_result["launch_evidence"]
            job.result = _validated_result(job.kind, final_result)
            if recovery_error is not None:
                job.result = _validated_result(
                    job.kind,
                    {
                        **(job.result or {}),
                        "recovery_error": str(recovery_error),
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
                installation.state = "partial" if failed else "installed"
                installation.updated_at = now
            elif job.kind == "recipe.start":
                run = session.get(RecipeRun, owner_id)
                assert run is not None
                if start_failed:
                    run.state = "stopping"
                    run.route_state = "withdrawn"
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
                    intent_current = _intent_is_current(
                        session, _bound_workload_intent(job), job.targets
                    )
                    if not intent_current:
                        run.state = "failed"
                        run.route_error = (
                            "start cleanup superseded by a newer workload intent"
                        )
                    if (
                        not session.scalar(
                            select(Job.id).where(Job.request_id == cleanup_request_id)
                        )
                        and intent_current
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
                        if revision is None:
                            raise RecipeOperationConflict(
                                "recipe run topology is unavailable"
                            )
                        self._queue_in_session(
                            session,
                            kind="recipe.stop",
                            owner_kind="run",
                            owner_id=owner_id,
                            plan_digest=run.plan_digest,
                            actor=job.actor,
                            request_id=cleanup_request_id,
                            node_payloads=tuple(
                                (
                                    run_node.node_id,
                                    {
                                        "schema_version": 1,
                                        "run_id": owner_id,
                                        "plan_digest": run.plan_digest,
                                    },
                                )
                                for run_node in stop_nodes
                            ),
                            phases=_role_phases(
                                _topology_order(revision.document, "stop_order"),
                                tuple(
                                    (
                                        run_node.node_id,
                                        {
                                            "schema_version": 1,
                                            "run_id": owner_id,
                                            "plan_digest": run.plan_digest,
                                            "role": run_node.role,
                                        },
                                    )
                                    for run_node in stop_nodes
                                ),
                                include_role=False,
                            ),
                            authority_digest=run.plan_digest,
                            now=now,
                            workload_intent_ordinal=_bound_workload_intent(job),
                        )
                        cleanup_queued = True
                else:
                    run.state = "running"
                    run.route_state = "pending"
                    run.route_error = None
                run.updated_at = now
            elif job.kind == "recipe.stop":
                run = session.get(RecipeRun, owner_id)
                assert run is not None
                recovery = None
                recovery_error: DistributedLifecycleError | None = None
                try:
                    recovery = recovery_start_plan(job.payload, now=now)
                except DistributedLifecycleError as error:
                    recovery_error = error
                if (
                    recovery is not None
                    and not failed
                    and _intent_is_current(
                        session, _bound_workload_intent(job), job.targets
                    )
                ):
                    phases, marker = recovery
                    installation = session.get(RecipeInstallation, run.installation_id)
                    revision = (
                        _active_recipe_revision(
                            session, installation.recipe_revision_id
                        )
                        if installation is not None
                        else None
                    )
                    if revision is None or revision.content_digest is None:
                        raise RecipeOperationConflict(
                            "distributed recovery authority is unavailable"
                        )
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
                        node_payloads=unique_payloads,
                        phases=phases,
                        authority_digest=revision.content_digest,
                        now=now,
                        workload_intent_ordinal=_bound_workload_intent(job),
                        job_context={
                            "recovery": marker,
                            "start_deadline": marker["deadline"],
                        },
                    )
                    run.state = "starting"
                    run.route_state = "withdrawn"
                    run.route_error = "distributed recovery restarting"
                    run.updated_at = now
                    cleanup_queued = True
                else:
                    run.state = "failed" if failed or recovery_error else "stopped"
                    run.stopped_at = now if not failed else None
                    run.route_state = "withdrawn"
                    if recovery_error is not None:
                        run.route_error = str(recovery_error)[:512]
                    run.updated_at = now
                    if not failed:
                        self._release(session, "run", owner_id, now)
            elif job.kind == "recipe.uninstall":
                installation = session.get(RecipeInstallation, owner_id)
                assert installation is not None
                installation.state = "failed" if failed else "uninstalled"
                installation.updated_at = now
                if not failed:
                    self._release(session, "installation", owner_id, now)
            elif job.kind == "recipe.reconcile":
                installation = session.get(RecipeInstallation, owner_id)
                assert installation is not None
                installation.state = (
                    "uninstalled" if reconciliation_complete else "partial"
                )
                installation.updated_at = now
                if reconciliation_complete:
                    self._release(session, "installation", owner_id, now)
        else:
            job.state = "running"
        job.updated_at = now
        return cleanup_queued

    def get(self, operation_id: str) -> RecipeOperationView:
        with self._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or not job.kind.startswith("recipe."):
                raise KeyError(operation_id)
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
                raise RecipeOperationConflict("recipe operation is not cancellable")
            bound_ordinal = job.payload.get("workload_intent_ordinal")
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
                    job.state == "waiting-for-operator"
                    and isinstance(job.result, Mapping)
                    and job.result.get("cancel_requested") is True
                )
            )
            if already_invalidated:
                if (
                    not isinstance(actor, str)
                    or not 1 <= len(actor) <= 256
                    or not isinstance(request_id, str)
                ):
                    raise RecipeOperationConflict(
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
                    raise RecipeOperationConflict(
                        "cancellation request key was already used differently"
                    )
                try:
                    if str(uuid.UUID(request_id)) != request_id:
                        raise ValueError("noncanonical cancellation request key")
                except ValueError as error:
                    raise RecipeOperationConflict(
                        "cancellation request identity is invalid"
                    ) from error
                logging.getLogger(__name__).info(
                    "ignored cancellation of superseded recipe operation %s", job.id
                )
                return self._view(job)
            if job.state == "cancelled":
                previous = _validated_result(job.kind, job.result) or {}
                if (
                    previous.get("cancel_request_id") == request_id
                    and previous.get("reason") == cancellation_reason
                    and previous.get("cancel_actor") == actor
                ):
                    return self._view(job)
                raise RecipeOperationConflict(
                    "cancellation request key was already used differently"
                )
            if job.state not in {"queued", "running"}:
                raise RecipeOperationConflict("recipe operation is not cancellable")
            previous = _validated_result(job.kind, job.result) or {}
            if previous.get("cancel_requested") is True:
                if (
                    previous.get("cancel_request_id") == request_id
                    and previous.get("reason") == cancellation_reason
                    and previous.get("cancel_actor") == actor
                ):
                    return self._view(job)
                raise RecipeOperationConflict(
                    "cancellation request key was already used differently"
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
                    child.state = "cancelled"
                    child.updated_at = now
            if any(
                child.state in {"running", "waiting-for-operator"} for child in children
            ):
                job.result = _validated_result(
                    job.kind,
                    {
                        **previous,
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
            job.state = "cancelled"
            job.status_reason = cancellation_reason
            job.result = _validated_result(
                job.kind,
                {
                    **(dict(job.result) if isinstance(job.result, Mapping) else {}),
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

    def reconcile_cancelled_builds(self) -> bool:
        """Cancel unneeded dependent builds and resume exact issued cleanup."""
        with self._sessions() as session:
            eligible = (
                select(Job)
                .where(
                    Job.kind == "recipe.build.v1",
                    Job.state.in_(("queued", "running", "waiting-for-operator")),
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
                        Job.state == "failed",
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
                _validated_result(job.kind, job.result)
                owner_id = _required_string(job.payload, "owner_id")
                ordinal = _bound_workload_intent(job)
                kind = (
                    "recipe.stop"
                    if job.kind in {"recipe.start", "recipe.stop"}
                    else "recipe.uninstall"
                )
                owner_kind = "run" if kind == "recipe.stop" else "installation"
                if job.payload.get("owner_kind") != owner_kind:
                    raise RecipeOperationConflict("retired owner binding is invalid")
                with self._sessions() as session:
                    existing = self._idempotent_in_session(
                        session,
                        request_id,
                        kind,
                        None,
                        owner_kind=owner_kind,
                        owner_id=owner_id,
                    )
                    current = _intent_is_current(session, ordinal, job.targets)
                    owner = session.get(
                        RecipeRun if kind == "recipe.stop" else RecipeInstallation,
                        owner_id,
                    )
                    completed = (
                        owner is not None
                        and owner.state
                        == ("stopped" if kind == "recipe.stop" else "uninstalled")
                        and session.scalar(
                            select(ResourceReservation.id)
                            .where(
                                ResourceReservation.owner_kind
                                == ("run" if kind == "recipe.stop" else "installation"),
                                ResourceReservation.owner_id == owner_id,
                                ResourceReservation.state == "active",
                            )
                            .limit(1)
                        )
                        is None
                    )
                if completed:
                    reason = "exact cleanup confirmed; capacity released"
                elif existing is not None:
                    if existing.state in {
                        "failed",
                        "cancelled",
                        "waiting-for-operator",
                    }:
                        reason = (
                            f"exact cleanup {existing.id} is {existing.state}: "
                            f"{existing.status_reason or 'inspect its retained failure evidence'}; "
                            "inspect/correct the blocker and submit a new exact stop or uninstall; capacity retained"
                        )
                    else:
                        reason = f"exact cleanup {existing.id} is {existing.state}; capacity retained until its receipt"
                elif not current:
                    reason = "newer workload intent owns cleanup; uncertain capacity remains reserved"
                else:
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
                    and stored.state == "failed"
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
                                **(stored.result or {}),
                                "recovery": "retry creates a new operation",
                            },
                        )
                    stored.updated_at = now
        return progressed

    def _cancel_build(
        self,
        job_id: str,
        *,
        actor: str,
        request_id: str,
        reason: str,
        only_if_unneeded: bool = False,
    ) -> bool:
        try:
            return self._cancel_current_build(
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
            raise RecipeOperationConflict(
                "build.consumer_busy: build ownership is changing; retry cancellation"
            ) from error

    def _cancel_current_build(
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
                hinted is None
                or hinted.kind != "recipe.build.v1"
                or len(hinted.targets) != 1
            ):
                raise RecipeOperationConflict("recipe build is not cancellable")
            node_id = hinted.targets[0]
        now = self._clock()
        with self._sessions.begin() as session:
            node = session.get(AgentNode, node_id, with_for_update={"nowait": True})
            job = session.get(Job, job_id, with_for_update={"nowait": True})
            if node is None or job is None or job.targets != [node_id]:
                raise RecipeOperationConflict("recipe build authority changed")
            if job.state == "cancelled":
                return False
            if job.state not in {"queued", "running", "waiting-for-operator"}:
                raise RecipeOperationConflict("recipe build is not cancellable")
            children = tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(AgentOperation.parent_job_id == job.id)
                    .order_by(AgentOperation.id)
                    .with_for_update(of=AgentOperation, nowait=True)
                )
            )
            if len(children) != 1 or children[0].node_id != node_id:
                raise RecipeOperationConflict(
                    "recipe build operation authority changed"
                )
            child = children[0]
            try:
                build = session.get(
                    RecipeBuild,
                    _required_string(job.payload, "owner_id"),
                    with_for_update={"nowait": True},
                )
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "55P03":
                    raise
                raise RecipeOperationConflict(
                    "build.consumer_busy: build ownership is changing; retry cancellation"
                ) from error
            if build is None or build.builder_node_id != node_id:
                raise RecipeOperationConflict("recipe build authority changed")
            if build_cancellation(job) is None:
                if only_if_unneeded and read_build_intent(job).kind == "independent":
                    return False
                try:
                    consumers = current_build_consumers(session, build)
                except BuildConsumerError as error:
                    raise RecipeOperationConflict(f"{error.code}: {error}") from error
                if consumers:
                    if only_if_unneeded:
                        return False
                    raise RecipeOperationConflict(
                        "build.shared_consumers: accepted preparation still needs this build; "
                        "cancel its parent intent first"
                    )
            cancellation = request_build_cancellation(
                job, actor=actor, request_id=request_id, reason=reason, now=_aware(now)
            )
            # As with a failed replacement, cancellation keeps the last verified
            # image. Explicit cache removal independently invalidates its bytes.
            if build.state == "building":
                build.state = (
                    "succeeded"
                    if job.payload.get("force_rebuild") is True
                    else "failed"
                )
            build.error = cancellation.reason
            build.updated_at = now
            if child.current_attempt == 0:
                child.state = "cancelled"
                child.updated_at = now
                job.state = "cancelled"
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
            if "recipe.build.cleanup.v1" not in (node.capabilities or []):
                return False
            payload = RecipeBuildCleanupRequest(
                schema_version=1, build_id=build.id, operation_id=child.id
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
                Job.state.in_(("queued", "running", "waiting-for-operator")),
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
    ) -> RecipeOperationView | RecipeArtifactJobCancellationPending | None:
        now = self._clock()
        with self._sessions() as session:
            existing_run = session.get(RecipeRun, run_id)
            if (
                existing_run is None
                or _stored_run_plan(existing_run.plan).get("execution_mode")
                != "one-shot-jobs"
            ):
                return None
        with self._sessions.begin() as session:
            pending_job = session.scalar(
                select(Job).where(Job.request_id == request_id).with_for_update(of=Job)
            )
            if pending_job is not None:
                if (
                    pending_job.kind != "recipe.stop"
                    or pending_job.state != "running"
                    or pending_job.payload.get("owner_id") != run_id
                    or pending_job.payload.get("plan_digest") != plan_digest
                    or pending_job.payload.get("execution_mode") != "one-shot-jobs"
                ):
                    raise RecipeOperationConflict(
                        "request key was already used differently"
                    )
                bound = _bound_workload_intent(pending_job)
                if (
                    workload_intent_ordinal is not None
                    and workload_intent_ordinal != bound
                ):
                    raise RecipeOperationConflict("workload intent was superseded")
                workload_intent_ordinal = bound
            run = session.get(RecipeRun, run_id, with_for_update=True)
            if (
                run is None
                or _stored_run_plan(run.plan).get("execution_mode") != "one-shot-jobs"
            ):
                raise RecipeOperationConflict(
                    "logical recipe run changed while stopping"
                )
            admitted = self._stop_plan_in_session(session, run_id, lock=True)
            if not admitted.allowed or admitted.plan_digest != plan_digest:
                raise RecipeOperationConflict("stop plan is stale or blocked")
            installation = session.get(RecipeInstallation, run.installation_id)
            revision = (
                _active_recipe_revision(session, installation.recipe_revision_id)
                if installation is not None
                else None
            )
            if revision is None or revision.content_digest is None:
                raise RecipeOperationConflict("recipe revision is unavailable")
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            targets = tuple(sorted(node.node_id for node in nodes))
            target_nodes = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(targets))
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                )
            )
            if tuple(node.node_id for node in target_nodes) != targets:
                raise RecipeOperationConflict("artifact workload target disappeared")
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
                raise RecipeOperationConflict("workload intent was superseded")
            payload = {
                "schema_version": 1,
                "owner_kind": "run",
                "owner_id": run_id,
                "plan_digest": plan_digest,
                "execution_mode": "one-shot-jobs",
                "workload_intent_ordinal": workload_intent_ordinal,
            }
            pending = self._one_shot_stop_prerequisite(session, run_id, now)
            if pending is not None:
                if pending_job is None:
                    session.add(
                        Job(
                            id=str(uuid.uuid4()),
                            request_id=request_id,
                            kind="recipe.stop",
                            state="running",
                            actor=actor,
                            authority_revision=revision.content_digest,
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
                node.state = "stopped"
                node.updated_at = now
            run.state = "stopped"
            run.route_state = "withdrawn"
            run.stopped_at = now
            run.updated_at = now
            self._release(session, "run", run_id, now)
            job = pending_job or Job(
                id=str(uuid.uuid4()),
                request_id=request_id,
                kind="recipe.stop",
                state="succeeded",
                actor=actor,
                authority_revision=revision.content_digest,
                targets=list(targets),
                payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
                payload=payload,
                result=_validated_result("recipe.stop", {"stopped": True}),
                created_at=now,
                updated_at=now,
            )
            if pending_job is not None:
                pending_job.state = "succeeded"
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
                and job.payload.get("execution_mode") == "one-shot-jobs"
            )

    @staticmethod
    def _one_shot_stop_prerequisite(
        session: Session, run_id: str, now: datetime
    ) -> RecipeArtifactJobCancellationPending | None:
        uncertain = tuple(
            session.scalars(
                select(ArtifactJob)
                .where(ArtifactJob.run_id == run_id, ArtifactJob.state == "failed")
                .order_by(ArtifactJob.created_at, ArtifactJob.id)
                .with_for_update(of=ArtifactJob)
            )
        )
        for artifact in uncertain:
            evidence = artifact.result_evidence
            if isinstance(evidence, Mapping) and (
                evidence.get("active_scope_may_remain") is True
                or evidence.get("failure_kind") == "agent-lease-expired"
            ):
                if artifact.operation_id is None:
                    raise RecipeOperationConflict(
                        "uncertain artifact job has no operation"
                    )
                return RecipeArtifactJobCancellationPending(
                    job_id=artifact.operation_id,
                    observe_due_at=now,
                    observation_deadline=now,
                )
        active = tuple(
            session.scalars(
                select(ArtifactJob)
                .where(
                    ArtifactJob.run_id == run_id,
                    ArtifactJob.state.in_(
                        {
                            "draft",
                            "ready",
                            "queued",
                            "running",
                            "cancelling",
                            "waiting-for-operator",
                        }
                    ),
                )
                .order_by(ArtifactJob.created_at, ArtifactJob.id)
                .with_for_update(of=ArtifactJob)
            )
        )
        pending: RecipeArtifactJobCancellationPending | None = None
        for artifact in active:
            if artifact.operation_id is None:
                if artifact.state not in {"draft", "ready"}:
                    raise RecipeOperationConflict(
                        "artifact job operation identity is missing"
                    )
                artifact.state = "cancelled"
                artifact.status_reason = "superseded by newer workload intent"
                artifact.completed_at = now
                artifact.updated_at = now
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
                or parent.payload.get("owner_id") != artifact.id
                or len(children) != 1
                or children[0].node_id not in parent.targets
            ):
                raise RecipeOperationConflict(
                    "artifact job cancellation authority changed"
                )
            if (
                parent.state == "cancelled"
                and children[0].state == "cancelled"
                and children[0].current_attempt == 0
            ):
                artifact.state = "cancelled"
                artifact.status_reason = "superseded before agent dispatch"
                artifact.completed_at = now
                artifact.updated_at = now
                continue
            deadline = superseded_cancellation_deadline(parent.result)
            if deadline is None:
                raise RecipeOperationConflict(
                    "artifact job has no cancellation authority"
                )
            artifact.state = (
                "cancelling"
                if artifact.state != "waiting-for-operator"
                else artifact.state
            )
            artifact.status_reason = "waiting for exact artifact cancellation receipt"
            artifact.updated_at = now
            if pending is None:
                pending = RecipeArtifactJobCancellationPending(
                    job_id=parent.id,
                    observe_due_at=now + timedelta(seconds=5),
                    observation_deadline=deadline,
                )
        return pending

    def _stop_plan_in_session(
        self, session: Session, run_id: str, *, lock: bool
    ) -> StopPlan:
        run_statement = select(RecipeRun).where(RecipeRun.id == run_id)
        if lock:
            run_statement = run_statement.with_for_update(of=RecipeRun)
        run = session.scalar(run_statement)
        if run is None:
            raise RecipeOperationConflict("recipe run does not exist")
        installation_statement = select(RecipeInstallation).where(
            RecipeInstallation.id == run.installation_id
        )
        if lock:
            installation_statement = installation_statement.with_for_update(
                of=RecipeInstallation
            )
        installation = session.scalar(installation_statement)
        if installation is None:
            raise RecipeOperationConflict("recipe installation does not exist")

        revision = _active_recipe_revision(
            session, installation.recipe_revision_id, for_update=lock
        )
        if revision is None:
            raise RecipeOperationConflict("recipe revision does not exist")

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
                ResourceReservation.state == "active",
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

        stored_run_plan = _stored_run_plan(run.plan)
        expected_nodes = stored_run_plan.get("nodes")
        expected_identity = (
            {
                (item.get("node_id"), item.get("rank"), item.get("role"))
                for item in expected_nodes
            }
            if isinstance(expected_nodes, list)
            and all(isinstance(item, Mapping) for item in expected_nodes)
            else set()
        )
        actual_identity = {(node.node_id, node.rank, node.role) for node in nodes}
        immutable_membership_exact = (
            len(all_nodes) <= _MAX_ACTION_NODES
            and isinstance(expected_nodes, list)
            and len(expected_nodes) == len(nodes)
            and len(expected_identity) == len(nodes)
            and expected_identity == actual_identity
            and stored_run_plan.get("installation_id") == run.installation_id
            and stored_run_plan.get("mapping_id") == run.mapping_id
            and stored_run_plan.get("mapping_generation") == run.mapping_generation
            and stored_run_plan.get("recipe_revision_id") == revision.id
            and stored_run_plan.get("plan_digest") == run.plan_digest
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
            recipe_revision_id=revision.id,
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
            immutable_membership_exact=immutable_membership_exact,
            reservation_membership_exact=reservation_membership_exact,
            reservation_facts=reservation_facts,
        )

    def _uninstall_plan_in_session(
        self, session: Session, installation_id: str, *, lock: bool
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
            raise RecipeOperationConflict("recipe installation does not exist")

        revision = _active_recipe_revision(
            session, installation.recipe_revision_id, for_update=lock
        )
        if (
            revision is None
            or revision.content_digest is None
            or not isinstance(revision.document, Mapping)
        ):
            raise RecipeOperationConflict("recipe revision authority is unavailable")
        stored_installation_plan = _stored_installation_plan(installation.plan)

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
                    RecipeRun.state != "stopped",
                )
            )
            or 0
        )
        active_statement = (
            select(RecipeRun)
            .where(
                RecipeRun.installation_id == installation_id,
                RecipeRun.state != "stopped",
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
                Job.state.in_({"queued", "running", "waiting-for-operator"}),
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
                    Job.state.in_({"queued", "running", "waiting-for-operator"}),
                    Job.payload["owner_id"].as_string() == installation_id,
                )
                .limit(1)
            )
            active_uninstalls = _active_owned_workload_jobs(
                session, "recipe.uninstall", installation_id
            )
            scope = tuple(sorted(node.node_id for node in nodes))
            if active_reconciliation is None and active_uninstalls and all(
                tuple(sorted(job.targets)) == scope
                and _unissued_workload_children(session, job) is not None
                for job in active_uninstalls
            ):
                active_operation = False

        expected_nodes = stored_installation_plan.get("nodes")
        expected_identity = (
            {
                (item.get("node_id"), item.get("rank"), item.get("role"))
                for item in expected_nodes
            }
            if isinstance(expected_nodes, list)
            and all(isinstance(item, Mapping) for item in expected_nodes)
            else set()
        )
        actual_identity = {(node.node_id, node.rank, node.role) for node in nodes}
        immutable_membership_exact = (
            len(all_nodes) <= _MAX_ACTION_NODES
            and isinstance(expected_nodes, list)
            and len(expected_nodes) == len(nodes)
            and len(expected_identity) == len(nodes)
            and expected_identity == actual_identity
            and stored_installation_plan.get("mapping_id") == installation.mapping_id
            and stored_installation_plan.get("mapping_generation")
            == installation.mapping_generation
            and stored_installation_plan.get("recipe_revision_id") == revision.id
            and stored_installation_plan.get("recipe_content_sha256")
            == revision.content_digest
            and stored_installation_plan.get("plan_digest") == installation.plan_digest
        )
        model_content_sha256, model_title = _primary_model_identity(revision.document)
        if installation.model_content_sha256 not in {None, model_content_sha256}:
            raise RecipeOperationConflict("installation model authority is invalid")
        dependent_recipe_ids_by_node = self._model_dependents_on_nodes(
            session,
            model_content_sha256,
            {node.node_id for node in nodes},
            exclude_installation_id=installation.id,
            lock=lock,
        )
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
                    installed_bytes=(
                        node.installed_bytes if node.state == "installed" else None
                    ),
                    evidence_digest=node.evidence_digest,
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
        exclude_installation_id: str | None,
        lock: bool,
    ) -> dict[str, tuple[str, ...]]:
        statement = select(RecipeInstallation).where(
            RecipeInstallation.state != "uninstalled"
        )
        if exclude_installation_id is not None:
            statement = statement.where(
                RecipeInstallation.id != exclude_installation_id
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
                InstallationNode.state != "uninstalled",
            )
        ):
            memberships.setdefault(installation_id, set()).add(node_id)
        for installation in candidates:
            member_nodes = memberships.get(installation.id)
            if not member_nodes:
                continue
            revision = _active_recipe_revision(session, installation.recipe_revision_id)
            if revision is None:
                raise RecipeOperationConflict(
                    "dependent recipe authority is unavailable"
                )
            primary_digest, _title = _primary_model_identity(revision.document)
            if installation.model_content_sha256 not in {None, primary_digest}:
                raise RecipeOperationConflict("dependent model authority is invalid")
            if any(
                digest == model_content_sha256
                for digest, _title in _recipe_model_identities(
                    session, revision.document
                )
            ):
                for node_id in member_nodes:
                    dependent_recipe_ids[node_id].add(revision.document_id)
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
    ) -> RecipeOperationView | None:
        with self._sessions() as session:
            return self._idempotent_in_session(
                session,
                request_id,
                kind,
                plan_digest,
                owner_kind=owner_kind,
                owner_id=owner_id,
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
    ) -> RecipeOperationView | None:
        existing = self._idempotent_job_in_session(
            session,
            request_id,
            kind,
            plan_digest,
            owner_kind=owner_kind,
            owner_id=owner_id,
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
    ) -> Job | None:
        existing = session.scalar(select(Job).where(Job.request_id == request_id))
        if existing is None:
            return None
        existing_digest = existing.payload.get("plan_digest")
        if (
            existing.kind != kind
            or (plan_digest is not None and existing_digest != plan_digest)
            or (
                owner_kind is not None
                and existing.payload.get("owner_kind") != owner_kind
            )
            or (owner_id is not None and existing.payload.get("owner_id") != owner_id)
        ):
            raise RecipeOperationConflict("request key was already used differently")
        return existing

    def _queue(
        self,
        *,
        kind: str,
        owner_kind: str,
        owner_id: str,
        plan_digest: str,
        actor: str,
        request_id: str,
        node_payloads: Sequence[tuple[str, Mapping[str, object]]],
        authority_digest: str,
        workload_intent_ordinal: int | None = None,
    ) -> RecipeOperationView:
        now = self._clock()
        with self._sessions.begin() as session:
            job = self._queue_in_session(
                session,
                kind=kind,
                owner_kind=owner_kind,
                owner_id=owner_id,
                plan_digest=plan_digest,
                actor=actor,
                request_id=request_id,
                node_payloads=node_payloads,
                authority_digest=authority_digest,
                now=now,
                workload_intent_ordinal=workload_intent_ordinal,
            )
        self._agent_jobs.notify_available()
        return self.get(job.id)

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
    ) -> Job:
        if not node_payloads:
            raise RecipeOperationConflict("operation group has no target nodes")
        try:
            payload_model = _RECIPE_WIRE_PAYLOAD_MODELS[kind]
        except KeyError:
            payload_model = None
        if payload_model is not None:
            try:
                for _node_id, payload in node_payloads:
                    payload_model.model_validate_json(canonical_message(payload))
            except Exception as error:
                raise RecipeOperationConflict(
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
            raise RecipeOperationConflict("operation phases are invalid")
        flattened = tuple(item for group in requested_phase_groups for item in group)
        if {node_id for node_id, _payload in flattened} != {
            node_id for node_id, _payload in node_payloads
        } or len({node_id for node_id, _payload in node_payloads}) != len(
            node_payloads
        ):
            raise RecipeOperationConflict("operation phases do not match target nodes")
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
            )
        except AdmissionLockBusy as error:
            if kind == "recipe.install":
                raise InstallAdmissionBusy("install.capacity_busy") from error
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
                    raise InstallAdmissionBusy("install.capacity_busy") from error
                raise RunAdmissionBusy("run capacity writer is busy") from error
            if tuple(node.node_id for node in target_nodes) != tuple(targets):
                raise RecipeOperationConflict("workload intent target disappeared")
            if workload_intent_ordinal is None:
                next_ordinal = (
                    max(node.workload_intent_ordinal for node in target_nodes) + 1
                )
                workload_intent_ordinal = next_ordinal
                for node in target_nodes:
                    node.workload_intent_ordinal = next_ordinal
                try:
                    AgentJobService.request_superseded_workload_cancellation_in_session(
                        session, targets, next_ordinal, now
                    )
                except AdmissionLockBusy as error:
                    if kind == "recipe.install":
                        raise InstallAdmissionBusy("install.capacity_busy") from error
                    raise RunAdmissionBusy("run capacity writer is busy") from error
            elif (
                type(workload_intent_ordinal) is not int
                or workload_intent_ordinal < 1
                or any(
                    node.workload_intent_ordinal != workload_intent_ordinal
                    for node in target_nodes
                )
            ):
                raise RecipeOperationConflict("workload intent was superseded")
        job_payload: dict[str, object] = {
            "schema_version": 1,
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "plan_digest": plan_digest,
        }
        if workload_intent_ordinal is not None:
            if type(workload_intent_ordinal) is not int or workload_intent_ordinal < 1:
                raise RecipeOperationConflict("workload intent ordinal is invalid")
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
                raise RecipeOperationConflict("operation context is invalid")
            job_payload.update(json.loads(canonical_message(job_context)))
        if kind in {"recipe.install", "recipe.start"}:
            try:
                for _node_id, payload in flattened:
                    compiled_plan = payload.get("compiled_execution_plan")
                    if not isinstance(compiled_plan, Mapping):
                        raise CompiledExecutionPlanError(
                            "compiled execution plan is missing"
                        )
                    validate_compiled_launch_payload(compiled_plan)
            except (CompiledExecutionPlanError, TypeError, ValueError) as error:
                raise RecipeOperationConflict(
                    f"compiled execution plan is invalid: {error}"
                ) from None
            if len(canonical_message(job_payload)) > MAX_COMPILED_EXECUTION_PLAN_BYTES:
                raise RecipeOperationConflict(
                    "recipe operation job payload is too large"
                )
        job = Job(
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
                raise InstallAdmissionBusy("install.capacity_busy") from error
            raise RunAdmissionBusy("run capacity writer is busy") from error
        except OperationalError as error:
            if not is_admission_contention(error):
                raise
            if kind == "recipe.install":
                raise InstallAdmissionBusy("install.capacity_busy") from error
            raise RunAdmissionBusy("run capacity writer is busy") from error
        return job

    def _view(self, job: Job, *, session: Session | None = None) -> RecipeOperationView:
        validate_recipe_lifecycle_terminal(job.kind, job.state, job.result)
        waiting_children = (
            tuple(
                session.scalars(
                    select(AgentOperation)
                    .where(
                        AgentOperation.parent_job_id == job.id,
                        AgentOperation.state == "waiting-for-operator",
                    )
                    .order_by(AgentOperation.node_id, AgentOperation.id)
                )
            )
            if session is not None
            and job.state in {"queued", "running", "waiting-for-operator"}
            else ()
        )
        retry_due_at = min(
            (
                _aware(child.retry_due_at)
                for child in waiting_children
                if child.retry_due_at is not None
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
            owner_id=_required_string(job.payload, "owner_id"),
            state=job.state,
            plan_digest=_required_string(job.payload, "plan_digest"),
            nodes=tuple(job.targets),
            result=_validated_result(job.kind, job.result),
            retry_due_at=retry_due_at,
            status_reason=child_reason or job.status_reason,
        )

    @staticmethod
    def _release(
        session: Session, owner_kind: str, owner_id: str, now: datetime
    ) -> None:
        release_owned_reservations_in_session(session, owner_kind, owner_id, now)


def _required_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise RecipeOperationConflict(f"operation {key} is invalid")
    return item


def _lower_hex_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _primary_model_identity(document: Mapping[str, object]) -> tuple[str, str]:
    selections = document.get("models")
    selection = (
        selections[0]
        if isinstance(selections, Sequence)
        and not isinstance(selections, (str, bytes))
        and selections
        else None
    )
    model = selection.get("model") if isinstance(selection, Mapping) else None
    if not isinstance(model, Mapping):
        raise RecipeOperationConflict("recipe model authority is unavailable")
    digest = model.get("content_sha256")
    publisher = model.get("publisher")
    slug = model.get("slug")
    if (
        not isinstance(digest, str)
        or not _lower_hex_digest(digest)
        or not isinstance(publisher, str)
        or not publisher
        or not isinstance(slug, str)
        or not slug
    ):
        raise RecipeOperationConflict("recipe model authority is invalid")
    return digest, f"{publisher}/{slug}"


def _recipe_model_identities(
    session: Session,
    document: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    try:
        recipe = RecipeDefinition.model_validate(document)
    except (TypeError, ValueError) as error:
        raise RecipeOperationConflict(
            "recipe model dependencies are invalid"
        ) from error
    if not recipe.models:
        raise RecipeOperationConflict("recipe model dependencies are invalid")
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
            raise RecipeOperationConflict("recipe model reference is unavailable")
        try:
            model = ModelDefinition.model_validate(revision.document)
        except (TypeError, ValueError) as error:
            raise RecipeOperationConflict(
                "recipe model reference is invalid"
            ) from error
        result.append(
            (reference.content_sha256, f"{reference.publisher}/{reference.slug}")
        )
        pending.extend(model.dependencies)
    if len({digest for digest, _title in result}) != len(result):
        raise RecipeOperationConflict("recipe model dependencies are duplicated")
    return tuple(result)


def _topology_order(document: Mapping[str, object], key: str) -> tuple[str, ...]:
    try:
        topology = recipe_topology(document)
    except Exception as error:
        raise RecipeOperationConflict("recipe topology is invalid") from error
    order = topology.get(key)
    if not isinstance(order, list) or not all(isinstance(role, str) for role in order):
        raise RecipeOperationConflict(f"recipe topology {key} is invalid")
    return tuple(order)


def _distributed_start_deadline(
    document: Mapping[str, object], *, now: datetime, timeout_seconds: int
) -> str:
    readiness = _canonical_distributed_readiness(document)
    if readiness is None:
        raise RecipeOperationConflict("distributed readiness policy is unavailable")
    # Persist the accepted loading/JIT budget. Exact rank-loss recovery derives
    # this same duration; neither lease renewal nor retry extends a deadline.
    return (_aware(now) + timedelta(seconds=timeout_seconds)).isoformat()


def _canonical_distributed_readiness(
    document: Mapping[str, object],
) -> dict[str, object] | None:
    runtime = document.get("runtime")
    lifecycle = runtime.get("lifecycle") if isinstance(runtime, Mapping) else None
    interfaces = document.get("interfaces")
    if not isinstance(lifecycle, Mapping) or not isinstance(interfaces, Sequence):
        raise RecipeOperationConflict("distributed readiness timeout is invalid")
    try:
        readiness = canonical_distributed_readiness(
            topology=recipe_topology(document),
            interfaces=interfaces,
            lifecycle=lifecycle,
        )
    except DistributedLifecycleError as error:
        raise RecipeOperationConflict(str(error)) from error
    return readiness


def _enforce_start_deadline(payload: Mapping[str, object], *, now: datetime) -> bool:
    value = payload.get("start_deadline")
    if value is None:
        return False
    if not isinstance(value, str):
        raise DistributedLifecycleError("distributed start deadline is invalid")
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError as error:
        raise DistributedLifecycleError(
            "distributed start deadline is invalid"
        ) from error
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise DistributedLifecycleError("distributed start deadline is invalid")
    if _aware(now) >= _aware(deadline):
        raise DistributedLifecycleError("distributed start deadline elapsed")
    return True


def _role_phases(
    order: Sequence[str],
    node_payloads: Sequence[tuple[str, Mapping[str, object]]],
    *,
    include_role: bool = True,
) -> tuple[tuple[tuple[str, Mapping[str, object]], ...], ...]:
    by_role: dict[str, list[tuple[str, Mapping[str, object]]]] = {}
    for node_id, payload in node_payloads:
        role = payload.get("role")
        if not isinstance(role, str):
            raise RecipeOperationConflict("operation role is invalid")
        projected = dict(payload)
        if not include_role:
            projected.pop("role")
        by_role.setdefault(role, []).append((node_id, projected))
    if set(by_role) != set(order) or len(set(order)) != len(order):
        raise RecipeOperationConflict("operation topology order is invalid")
    return tuple(
        tuple(sorted(by_role[role], key=lambda item: item[0])) for role in order
    )


def _stored_phases(
    payload: Mapping[str, object],
) -> tuple[tuple[tuple[str, str, Mapping[str, object]], ...], ...]:
    raw_phases = payload.get("phases")
    if raw_phases is None:
        return ()
    if not isinstance(raw_phases, list) or not raw_phases:
        raise RecipeOperationConflict("stored operation phases are invalid")
    phases: list[tuple[tuple[str, str, Mapping[str, object]], ...]] = []
    seen_operations: set[str] = set()
    for raw_phase in raw_phases:
        if not isinstance(raw_phase, list) or not raw_phase:
            raise RecipeOperationConflict("stored operation phases are invalid")
        group: list[tuple[str, str, Mapping[str, object]]] = []
        for raw_item in raw_phase:
            if not isinstance(raw_item, Mapping):
                raise RecipeOperationConflict("stored operation phases are invalid")
            operation_id = raw_item.get("operation_id")
            node_id = raw_item.get("node_id")
            item_payload = raw_item.get("payload")
            if not isinstance(node_id, str) or not isinstance(item_payload, Mapping):
                raise RecipeOperationConflict("stored operation phases are invalid")
            if not isinstance(operation_id, str) or operation_id in seen_operations:
                raise RecipeOperationConflict("stored operation phases are invalid")
            try:
                uuid.UUID(operation_id)
            except ValueError:
                raise RecipeOperationConflict("stored operation phases are invalid")
            seen_operations.add(operation_id)
            group.append((operation_id, node_id, dict(item_payload)))
        phases.append(tuple(group))
    return tuple(phases)


def _current_phase_index(
    children: Sequence[AgentOperation],
    phases: Sequence[Sequence[tuple[str, str, Mapping[str, object]]]],
) -> int | None:
    child_operations = {child.id for child in children}
    for index in range(len(phases) - 1, -1, -1):
        phase_operations = {
            operation_id for operation_id, _node_id, _payload in phases[index]
        }
        if phase_operations <= child_operations:
            return index
    return None


def _valid_image_import_payload(value: object, expected_build_id: str) -> bool:
    expected_fields = {
        "schema_version",
        "kind",
        "build_id",
        "mapping_id",
        "mapping_generation",
        "source_node_id",
        "image_digest",
        "oci_layout_sha256",
        "image_bytes",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        return False
    image_digest = value.get("image_digest")
    layout_digest = value.get("oci_layout_sha256")
    return (
        value.get("schema_version") == 1
        and value.get("kind") == "recipe.image.import.v1"
        and value.get("build_id") == expected_build_id
        and isinstance(value.get("mapping_id"), str)
        and isinstance(value.get("mapping_generation"), int)
        and value["mapping_generation"] >= 1
        and isinstance(value.get("source_node_id"), str)
        and isinstance(image_digest, str)
        and len(image_digest) == 71
        and image_digest.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in image_digest[7:])
        and isinstance(layout_digest, str)
        and len(layout_digest) == 64
        and all(character in "0123456789abcdef" for character in layout_digest)
        and isinstance(value.get("image_bytes"), int)
        and value["image_bytes"] > 0
    )


def _record_build_evidence(
    session: Session,
    build: RecipeBuild,
    evidence: Mapping[str, object],
    *,
    now: datetime,
    replace_existing: bool = False,
) -> None:
    # A retried build may already be present in this transaction's identity map
    # with the previous attempt's upload fields. Refresh under the row lock so
    # terminal evidence is compared with the upload transaction that just
    # completed, not with stale in-memory values.
    session.refresh(build, with_for_update=True)
    expected = {
        "build_input_sha256",
        "image_bytes",
        "image_digest",
        "oci_layout_sha256",
        "policy",
    }
    policy = evidence.get("policy")
    findings = policy.get("findings") if isinstance(policy, Mapping) else None
    image_digest = evidence.get("image_digest")
    layout_digest = evidence.get("oci_layout_sha256")
    image_bytes = evidence.get("image_bytes")
    try:
        stored_build_plan = parse_stored_build_plan(build.plan)
        parse_stored_build_policy(build.policy_report)
    except RecipeExecutionContractError as error:
        raise RecipeOperationConflict(
            "stored recipe build envelope is invalid" + error.detail
        ) from error
    if (
        set(evidence) != expected
        or evidence.get("build_input_sha256") != build.build_input_sha256
        or not isinstance(image_digest, str)
        or len(image_digest) != 71
        or not image_digest.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in image_digest[7:])
        or not isinstance(layout_digest, str)
        or len(layout_digest) != 64
        or any(character not in "0123456789abcdef" for character in layout_digest)
        or not isinstance(image_bytes, int)
        or isinstance(image_bytes, bool)
        or not 1 <= image_bytes <= 16 * 1024**4
        or not isinstance(policy, Mapping)
        or policy.get("passed") is not True
        or not isinstance(findings, (list, tuple))
        or bool(findings)
        or policy.get("dockerfile") != stored_build_plan.dockerfile
        or (not replace_existing and build.image_digest not in {None, image_digest})
        or (
            not replace_existing
            and build.oci_layout_sha256 not in {None, layout_digest}
        )
        or (not replace_existing and build.image_bytes not in {None, image_bytes})
    ):
        raise RecipeOperationConflict("recipe build evidence is invalid")
    build.state = "succeeded"
    build.image_digest = image_digest
    build.oci_layout_sha256 = layout_digest
    build.image_bytes = image_bytes
    build.error = None
    build.updated_at = now


def _record_image_import_evidence(
    session: Session,
    operation: AgentOperation,
    evidence: Mapping[str, object],
    succeeded: bool,
    now: datetime,
) -> None:
    if not succeeded:
        return
    expected = {"build_id", "image_bytes", "image_digest", "oci_layout_sha256"}
    build_id = operation.payload.get("build_id")
    build = session.get(RecipeBuild, build_id) if isinstance(build_id, str) else None
    if (
        set(evidence) != expected
        or build is None
        or build.state != "succeeded"
        or evidence.get("build_id") != build.id
        or evidence.get("image_digest") != build.image_digest
        or evidence.get("oci_layout_sha256") != build.oci_layout_sha256
        or evidence.get("image_bytes") != build.image_bytes
        or operation.payload.get("image_digest") != build.image_digest
        or operation.payload.get("oci_layout_sha256") != build.oci_layout_sha256
        or operation.payload.get("image_bytes") != build.image_bytes
    ):
        raise RecipeOperationConflict("recipe image import evidence is invalid")
    assert build.image_digest is not None and build.image_bytes is not None
    raw_digest = build.image_digest[7:]
    artifact = session.scalar(
        select(NodeArtifact).where(
            NodeArtifact.node_id == operation.node_id,
            NodeArtifact.digest == raw_digest,
        )
    )
    if artifact is None:
        session.add(
            NodeArtifact(
                node_id=operation.node_id,
                kind="image",
                digest=raw_digest,
                source=f"docker-archive:{build.oci_layout_sha256}",
                size_bytes=build.image_bytes,
                state="verified",
                ref_count=0,
                verified_at=now,
                updated_at=now,
            )
        )
    elif (
        artifact.kind != "image"
        or artifact.size_bytes != build.image_bytes
        or artifact.state != "verified"
    ):
        raise RecipeOperationConflict(
            "recipe image import conflicts with local artifact state"
        )


def _validate_rank_launch_evidence(
    session: Session,
    run_id: str,
    operation: AgentOperation,
    evidence: Mapping[str, object],
) -> str:
    run_generation = operation.payload.get("run_generation")
    if type(run_generation) is not int or run_generation < 1:
        raise RecipeOperationConflict("start run generation is invalid")
    expected_fields = {
        "phase",
        "run_id",
        "recipe_revision_id",
        "recipe_content_sha256",
        "image_digest",
        "artifact_set_digest",
        "model_identity",
        "rank",
        "role",
        "world_size",
        "local_address",
        "master_address",
        "master_port",
        "memory_reservation_bytes",
        "process_running",
        "fabric_projection_bound",
        "launched",
        "evidence_digest",
        "run_generation",
        "runtime_arguments_sha256",
    }
    if (
        set(evidence) != expected_fields
        or evidence.get("phase") != "rank-launch"
        or evidence.get("process_running") is not True
        or evidence.get("fabric_projection_bound") is not True
        or evidence.get("launched") is not True
    ):
        raise RecipeOperationConflict("rank launch evidence is invalid")
    run = session.get(RecipeRun, run_id)
    installation = (
        session.get(RecipeInstallation, run.installation_id)
        if run is not None
        else None
    )
    revision = (
        _active_recipe_revision(session, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if revision is None or installation is None:
        raise RecipeOperationConflict("start evidence authority is unavailable")
    selections = revision.document.get("models")
    selection = (
        selections[0]
        if isinstance(selections, Sequence)
        and not isinstance(selections, (str, bytes))
        and selections
        else None
    )
    model = selection.get("model") if isinstance(selection, Mapping) else None
    if not isinstance(model, Mapping):
        raise RecipeOperationConflict("start evidence authority is invalid")
    try:
        model_identity = format_model_identity(
            model["publisher"], model["slug"], model["content_sha256"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RecipeOperationConflict("start evidence authority is invalid") from error
    comparisons = {
        "phase": "rank-launch",
        "run_id": run_id,
        "recipe_revision_id": operation.payload.get("recipe_revision_id"),
        "recipe_content_sha256": operation.payload.get("recipe_content_sha256"),
        "image_digest": installation.image_digest,
        "model_identity": model_identity,
        "rank": operation.payload.get("rank"),
        "role": operation.payload.get("role"),
        "world_size": operation.payload.get("world_size"),
        "local_address": operation.payload.get("local_address"),
        "master_address": operation.payload.get("master_address"),
        "master_port": operation.payload.get("master_port"),
        "memory_reservation_bytes": operation.payload.get("reserved_memory_bytes"),
    }
    comparisons["run_generation"] = run_generation
    if any(evidence.get(key) != value for key, value in comparisons.items()):
        raise RecipeOperationConflict(
            "rank launch evidence does not match its fenced request"
        )
    artifact_set_digest = evidence.get("artifact_set_digest")
    if (
        not isinstance(artifact_set_digest, str)
        or len(artifact_set_digest) != 64
        or any(character not in "0123456789abcdef" for character in artifact_set_digest)
    ):
        raise RecipeOperationConflict("start artifact evidence is invalid")
    runtime_arguments_sha256 = evidence.get("runtime_arguments_sha256")
    if (
        not isinstance(runtime_arguments_sha256, str)
        or len(runtime_arguments_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in runtime_arguments_sha256
        )
    ):
        raise RecipeOperationConflict("start runtime identity evidence is invalid")
    digest = evidence.get("evidence_digest")
    identity = {
        key: value for key, value in evidence.items() if key != "evidence_digest"
    }
    observed_digest = hashlib.sha256(canonical_message(identity)).hexdigest()
    if not isinstance(digest, str) or digest != observed_digest:
        raise RecipeOperationConflict("start evidence digest is invalid")
    return digest


def _validate_start_evidence(
    session: Session,
    run_id: str,
    operation: AgentOperation,
    evidence: Mapping[str, object],
) -> tuple[str, str]:
    run_generation = operation.payload.get("run_generation")
    if type(run_generation) is not int or run_generation < 1:
        raise RecipeOperationConflict("start run generation is invalid")
    expected_fields = {
        "recipe_revision_id",
        "recipe_content_sha256",
        "image_digest",
        "artifact_set_digest",
        "model_identity",
        "rank",
        "world_size",
        "endpoint",
        "memory_reservation_bytes",
        "ready",
        "evidence_digest",
        "run_generation",
        "runtime_arguments_sha256",
        "local_address",
        "master_address",
        "master_port",
    }
    phase = operation.payload.get("phase")
    if phase == "collective-readiness":
        expected_fields |= {"phase", "run_id", "role"}
    if set(evidence) != expected_fields or evidence.get("ready") is not True:
        raise RecipeOperationConflict("start evidence is invalid")
    run = session.get(RecipeRun, run_id)
    installation = (
        session.get(RecipeInstallation, run.installation_id)
        if run is not None
        else None
    )
    revision = (
        _active_recipe_revision(session, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if revision is None or installation is None:
        raise RecipeOperationConflict("start evidence authority is unavailable")
    selections = revision.document.get("models")
    selection = (
        selections[0]
        if isinstance(selections, Sequence)
        and not isinstance(selections, (str, bytes))
        and selections
        else None
    )
    model = selection.get("model") if isinstance(selection, Mapping) else None
    if not isinstance(model, Mapping):
        raise RecipeOperationConflict("start evidence authority is invalid")
    image_digest = installation.image_digest
    try:
        model_identity = format_model_identity(
            model["publisher"], model["slug"], model["content_sha256"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RecipeOperationConflict("start evidence authority is invalid") from error
    endpoint_address = operation.payload.get("endpoint_address")
    if not isinstance(endpoint_address, (str, bytes, int)):
        raise RecipeOperationConflict("start evidence authority is invalid")
    port = operation.payload.get("port")
    try:
        parsed_address = ipaddress.ip_address(endpoint_address)
    except (TypeError, ValueError) as error:
        raise RecipeOperationConflict("start evidence authority is invalid") from error
    host = f"[{parsed_address}]" if parsed_address.version == 6 else str(parsed_address)
    expected_endpoint = f"http://{host}:{port}"
    comparisons = {
        "recipe_revision_id": operation.payload.get("recipe_revision_id"),
        "recipe_content_sha256": operation.payload.get("recipe_content_sha256"),
        "image_digest": image_digest,
        "model_identity": model_identity,
        "rank": operation.payload.get("rank"),
        "world_size": operation.payload.get("world_size"),
        "endpoint": expected_endpoint,
        "memory_reservation_bytes": operation.payload.get("reserved_memory_bytes"),
    }
    if phase == "collective-readiness":
        comparisons.update(
            {
                "phase": "collective-readiness",
                "run_id": run_id,
                "role": operation.payload.get("role"),
            }
        )
    comparisons["run_generation"] = run_generation
    comparisons["local_address"] = operation.payload.get("local_address")
    comparisons["master_address"] = operation.payload.get("master_address")
    comparisons["master_port"] = operation.payload.get("master_port")
    if any(evidence.get(key) != value for key, value in comparisons.items()):
        raise RecipeOperationConflict(
            "start evidence does not match its fenced request"
        )
    artifact_set_digest = evidence.get("artifact_set_digest")
    if (
        not isinstance(artifact_set_digest, str)
        or len(artifact_set_digest) != 64
        or any(character not in "0123456789abcdef" for character in artifact_set_digest)
    ):
        raise RecipeOperationConflict("start artifact evidence is invalid")
    runtime_arguments_sha256 = evidence.get("runtime_arguments_sha256")
    if (
        not isinstance(runtime_arguments_sha256, str)
        or len(runtime_arguments_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in runtime_arguments_sha256
        )
    ):
        raise RecipeOperationConflict("start runtime identity evidence is invalid")
    digest = evidence.get("evidence_digest")
    identity = {
        key: value for key, value in evidence.items() if key != "evidence_digest"
    }
    observed_digest = hashlib.sha256(canonical_message(identity)).hexdigest()
    if not isinstance(digest, str) or digest != observed_digest:
        raise RecipeOperationConflict("start evidence digest is invalid")
    return expected_endpoint, digest


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
    """Partition one exact-v2 partial or explicit-empty failure snapshot."""

    assigned = tuple(
        node
        for node in session.scalars(
            select(RunNode)
            .join(RecipeRun, RecipeRun.id == RunNode.run_id)
            .where(
                RunNode.node_id == node_id,
                RecipeRun.state == "running",
            )
            .order_by(RunNode.run_id)
            .with_for_update(of=RunNode)
        )
        if (
            (run := session.get(RecipeRun, node.run_id)) is not None
            and _stored_run_plan(run.plan).get("observation_schema_version") == 2
        )
    )
    if included_run_ids - {node.run_id for node in assigned}:
        raise ValueError("recipe run observation is not assigned")
    if not included_run_ids:
        for node in assigned:
            if _aware(node.updated_at) < observed_at:
                node.state = "failed"
                node.updated_at = observed_at
    return assigned


__all__ = [
    "RecipeOperationConflict",
    "RecipeOperationService",
    "RecipeOperationView",
    "RecipeRunRankStatus",
    "RecipeRunStatus",
    "prepare_exact_recipe_run_observation_nodes",
]
