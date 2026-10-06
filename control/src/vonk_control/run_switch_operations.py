"""Controller-owned Run/Switch planning and durable phase orchestration.

This service is the boundary used by Library, Fleet profiles, the HTTP API,
and the CLI.  It intentionally composes the existing mapping, install, and
run services.  Artifact delivery remains behind ``RunSwitchArtifactInspector``
and ``RunSwitchPhaseExecutor`` so this module never downloads or publishes a
cache payload itself.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, TypeGuard, get_args, runtime_checkable

import httpx2
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    InstallationNodeState,
    InstallationState,
    InstallDegradedReason,
    InvalidRequestError,
    InvalidRequestReason,
    LifecycleState,
    ModelFileState,
    OperationProgress,
    ProfileReasonCode,
    ReservationState,
    ResourceBlockerCode,
    ResourcePlanningCode,
    RouteState,
    RunAdmissionCode,
    RunState,
    RunSwitchCode,
    SecurityRefusalError,
    SecurityRefusalReason,
    UninstallPlanCode,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
    run_switch_code,
)
from vonk_agent_protocol.compiled_execution_plan import CompiledExecutionPlan
from vonk_forge_contracts import ModelDefinition
from vonk_forge_contracts.recipe import Scalar

from . import job_states
from .admission_locking import AdmissionLockBusy, busy_detail, patient_admission
from .agent_jobs import AgentJobService
from .artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    require_reference_open,
)
from .artifact_reference_scan import (
    _run_switch_runtime_image_intent,
    require_model_sets_open,
)
from .attempt_residues import unowned_never_installed
from .bounded_json import require_integer, require_sequence
from .categorized_errors import (
    InvalidValue,
    MissingRecord,
)
from .categorized_faults import security_reason
from .cluster_mappings import (
    ClusterMappingError,
    ClusterMappingPlan,
    ClusterMappingService,
    candidate_placements,
    effective_option_choices,
    mapping_option_choices,
    validate_mapping_parameters,
)
from .content_identity import ImageContent, differing_image_fields, same_image
from .disk_reservations import (
    describe_disk_charges,
    models_stored_on_node,
    outstanding_disk_charges,
)
from .distribution_assignment import NodeDistributionAssignment
from .failure_classification import error_code, is_redownload, is_security_failure
from .install_admission import (
    AGENT_UPGRADE_REQUIRED_DETAIL,
    IMAGE_PULL_CAPABILITY,
    InstallAdmissionBusy,
    InstallPreflightExpired,
)
from .inventory_repository import MAX_INVENTORY_FUTURE_SKEW, InventoryRepository
from .job_documents import (
    RecipeStartParent,
    RecipeStopParent,
    RunSwitchCleanupIntent,
    RunSwitchJobPayload,
    RunSwitchProfileStopIntent,
    RunSwitchRunIntent,
    RunSwitchStopIntent,
)
from .lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from .lifecycle.run_switch import (
    KEEP as _KEEP_REASON,
)
from .lifecycle.run_switch import (
    LIVE_STATES as _LIVE_STATES,
)
from .lifecycle.run_switch import (
    RunSwitchAdapter,
    set_member_state,
)
from .lifecycle.types import Effect as _LifecycleEffect
from .lifecycle.types import State as _LifecycleState
from .lifecycle_preflight import LifecyclePreflight, LifecyclePreflightCheckpoint
from .logging import log_event, redact_text
from .memory_reservations import (
    MEMORY_RESERVATION_KINDS,
    memory_reservations,
    reviewed_run_memory_reservations,
)
from .model_cache import ModelCacheService
from .model_cache_contract import ModelCacheDownloadPreviewResponse
from .models import (
    ACTIVE_RUN_STATES,
    STOPPABLE_RUN_STATES,
    AgentNode,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    Job,
    NodeArtifact,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    RecipeSourceBundle,
    ResourceReservation,
    RunNode,
)
from .operation_api import (
    OperationListPage,
    OperationQuery,
    _activity_keyset_filter,
)
from .operation_blockers import (
    PHASE_RETRY_CODE,
    OperationBlocker,
    bound_blockers,
    make_blocker,
)
from .operation_progress import progress_document, project_progress, sample_progress
from .prebuilt_images import policy_prebuilt_reference
from .preparation_contract import (
    ControllerAssetState,
    ModelArtifactPreparation,
    PreparationReason,
    RolloutPreparation,
    RuntimeImageIdentity,
    RuntimeImagePreparation,
    TargetAssetState,
    controller_assets_ready,
)
from .profile_capacity import (
    ProfileHandoffInconsistent,
    accepted_profile_runtime_image,
    prepared_profile_installation,
    reservation_visible,
)
from .recipe_build_cancellation import (
    BuildConsumerError,
    lock_run_switch_build_dependency,
)
from .recipe_builds import RecipeBuildAdmissionBusy, RecipeBuildPlan
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    build_plan_document,
    installation_matches_runtime_image,
    installation_serves_authorised_ports,
    parse_stored_build_plan,
    parse_stored_installation_plan,
    parse_stored_run_plan,
)
from .recipe_operations import (
    RecipeArtifactJobCancellationPending,
    RecipeInstallPreflightExpired,
    RecipeOperationConflict,
    RecipeOperationService,
    RecipeReconciliationBlocked,
)
from .recipe_runtime_specs import (
    OPTION_CHOICES_KEY,
    RUNTIME_INTERFACE,
    RecipeRuntimeSpecError,
    recipe_topology,
    resolve_recipe_entities,
)
from .recovery_policy import (
    FailureKind,
    RecoveryDecision,
    classify,
    kind_for_agent_error,
)
from .resource_planning import (
    PLATFORM_MEMORY_FLOOR_BYTES,
    ResourceDemand,
    installation_disk_requirement,
    memory_capacity_snapshot,
    memory_requirement,
    memory_reservation_kinds,
    plan_capacity,
    resolve_effective_settings,
)
from .run_admission import (
    PORT_ADMISSION_CODES,
    RETRYABLE_PLAN_BLOCKERS,
    RUN_ADMISSION_WAIT_CODES,
    RunAdmissionBusy,
    allocate_service_port,
    run_port_blockers,
    run_port_demand,
)
from .run_switch_contract import (
    ArtifactStorageImpact,
    BuildCompatibilityEvidence,
    BuildSourceEvidence,
    ConditionalPostStopMemoryCheck,
    EffectiveParallelism,
    EffectiveSettingsSelection,
    FreshnessEvidence,
    InvocationMetadata,
    MappingSelection,
    MemoryUsageUncertainty,
    ResourceDemandEvidence,
    RunMemoryResidualRange,
    RunSwitchAction,
    RunSwitchApplyRequest,
    RunSwitchAssessment,
    RunSwitchBuildEvidence,
    RunSwitchBuildEvidenceState,
    RunSwitchCancellation,
    RunSwitchChangeEffect,
    RunSwitchCleanupApplyRequest,
    RunSwitchCleanupPreviewRequest,
    RunSwitchContainerBuildResult,
    RunSwitchContainerBuildState,
    RunSwitchCoverage,
    RunSwitchFinalVerifyResult,
    RunSwitchMemberProgress,
    RunSwitchMemberState,
    RunSwitchOperation,
    RunSwitchOperationKind,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPhaseKind,
    RunSwitchPhaseResult,
    RunSwitchPlacementAction,
    RunSwitchPlan,
    RunSwitchPreviewRequest,
    RunSwitchProfileStopScope,
    RunSwitchProgress,
    RunSwitchProgressState,
    RunSwitchReason,
    RunSwitchReasonScope,
    RunSwitchReasonSeverity,
    RunSwitchReconciliationAuthority,
    RunSwitchRetention,
    RunSwitchRuntimeImageReferenceIntent,
    RunSwitchRuntimeInstallResult,
    RunSwitchRuntimePlanResult,
    RunSwitchStartResult,
    RunSwitchStopApplyRequest,
    RunSwitchStopPreviewRequest,
    RunSwitchStopResult,
    RunSwitchSubphase,
    RunSwitchVerifyResult,
    RuntimeImageStorageImpact,
    SparkFit,
    SparkFitNode,
    SparkGroup,
    SparkGroupNode,
    StopImpact,
)
from .runtime_image_preparation import (
    RuntimeImagePreparationError,
    RuntimeImagePreparationRefused,
    RuntimeImagePreparationUnknown,
)
from .runtime_image_preparation import (
    RuntimeImageReceipt as RuntimeImageReceiptDocument,
)
from .stored_json import read_row_column
from .strict_json import (
    read_stored_document,
    read_stored_model,
    serialize_json_value,
    warn_unreadable_once,
)
from .unused_storage_collection import spark_eviction_capacity

# Persisted progress and catalog documents arrive as decoded JSON, so the
# contract's closed value sets are read back through the declared alias instead
# of a hand-written membership test that could drift from it.
_KNOBS_ADAPTER: TypeAdapter[dict[str, Scalar]] = TypeAdapter(dict[str, Scalar])
_CHANGE_EFFECTS_ADAPTER = TypeAdapter(dict[str, RunSwitchChangeEffect])
_CONTAINER_BUILD_STATE_ADAPTER = TypeAdapter(RunSwitchContainerBuildState)
_BUILD_EVIDENCE_STATE_ADAPTER = TypeAdapter(RunSwitchBuildEvidenceState)
_OPERATION_KIND_ADAPTER = TypeAdapter(RunSwitchOperationKind)
_MEMBER_STATE_ADAPTER = TypeAdapter(RunSwitchMemberState)
_OBSERVING = LifecycleState.OBSERVING.value
_PROGRESS_STATE_ADAPTER = TypeAdapter(RunSwitchProgressState)
_SUBPHASE_ADAPTER = TypeAdapter(RunSwitchSubphase)
_REASON_SEVERITY_ADAPTER = TypeAdapter(RunSwitchReasonSeverity)


class RunSwitchOperationConflict(RuntimeError):
    """The selected outcome is stale, unsupported, or unsafe to execute.

    ``definite`` marks an outcome its raiser *reports* rather than a failure to
    observe: the phase did its work and the answer is "this is how it ended" (the
    profile Stop below), which the parent reads as a typed result.  It ends the
    operation; every other conflict is an unknown that is observed again.
    """

    definite = False


class _RunSwitchDefiniteConflict(InvalidRequestError, RunSwitchOperationConflict):
    """A refusal of the accepted request itself, not a failure to observe.

    The accepted image identity changed, or the plan names a phase this executor
    cannot do: nothing observed again will change it, and the person (or the
    profile above) must review and apply again.  The allowlist keeps these in its
    ``input-validation`` family; ``test_run_switch_lifecycle`` ties the two together.
    """

    definite = True


class _RunSwitchIncompleteProfileGroupConflict(_RunSwitchDefiniteConflict):
    """Reachable ranks stopped, but the profile group remains incomplete."""

    code = RunSwitchCode.PROFILE_INCOMPLETE_MULTI_SPARK_MODEL


class _RunSwitchBuildParentChanged(UnknownOutcomeError, RunSwitchOperationConflict):
    """This out-of-transaction executor no longer owns the build checkpoint."""


class RunSwitchIssuedWorkloadPending(UnknownOutcomeError, RunSwitchOperationConflict):
    """An older issued lifecycle effect needs observation, never blind replay."""

    def __init__(
        self,
        *,
        kind: str,
        owner_id: str,
        job_id: str,
        observe_due_at: datetime,
        observation_deadline: datetime,
    ) -> None:
        code = run_switch_code(f"{kind}-issued-pending")
        super().__init__(f"{code}: {owner_id} ({job_id})")
        self.kind = kind
        self.job_id = job_id
        self.observe_due_at = observe_due_at
        self.observation_deadline = observation_deadline


class RunSwitchPostStopEvidencePending(UnknownOutcomeError, RunSwitchOperationConflict):
    """A released claim is not evidence that its physical bytes are free.

    ``collected_after`` is the instant evidence must be collected after (strictly):
    a retry before it cannot find any, so the retry clock never schedules one.
    """

    code = RunSwitchCode.POST_STOP_INVENTORY_PENDING

    def __init__(self, message: str, *, collected_after: datetime | None = None):
        super().__init__(message)
        self.collected_after = collected_after


class RunSwitchInstallPreflightExpired(UnknownOutcomeError, RunSwitchOperationConflict):
    """A compile needs a fresh runtime preflight probe before it is accepted.

    The preflight window it was admitted on may have passed, or the host
    fingerprint or requirements moved while it compiled.  Nothing was accepted.
    ``_advance`` holds the runtime-plan checkpoint so the next tick re-enters
    ``LifecyclePreflight.ensure`` for a bounded refresh; any handler that does
    not know this subclass keeps failing the phase, which is the safe reading.
    """


class RunSwitchRefused(SecurityRefusalError, RunSwitchOperationConflict):
    """A refusal at a security boundary: an artifact digest, a reclaim not planned
    or an eviction that would delete another person's bytes."""

    def __init__(
        self, *args: object, reason: SecurityRefusalReason | None = None
    ) -> None:
        super().__init__(
            *args,
            reason=reason
            if reason is not None
            else security_reason(args[0] if args else None),
        )


class RunSwitchRequestInvalid(InvalidRequestError, RunSwitchOperationConflict):
    """The selected outcome is stale, unsupported or not allowed for this request:
    refused at submit time or at the phase that owns the request."""


class RunSwitchRetryLater(UnknownOutcomeError, RunSwitchOperationConflict):
    """Evidence, capacity or an owner that is not settled yet: the phase observes
    it again on the next tick; nothing is refused and nothing waits for a person."""


class RunSwitchRuntimeSpecInvalid(InvalidRequestError, RecipeRuntimeSpecError):
    """A recipe whose canonical document cannot produce a runtime projection."""


class _RuntimeImageOwnerChanged(RuntimeImagePreparationUnknown):
    """The phase, claim or progress that owned an image publication moved on."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            _RUNTIME_IMAGE_OWNER_CHANGED,
            detail,
            reason=WaitReason.SCOPE_CHANGED,
        )


class _RuntimeImageIdentityMismatch(RuntimeImagePreparationRefused):
    """A published image or receipt that differs from the approved identity."""

    def __init__(self, detail: str) -> None:
        super().__init__(_RUNTIME_IMAGE_IDENTITY_MISMATCH, detail)


class _RuntimeImageIdentityUnknown(RuntimeImagePreparationUnknown):
    """An image reference whose stored identity cannot be settled here."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            _RUNTIME_IMAGE_IDENTITY_MISMATCH,
            detail,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )


def _active_recipe_revision(
    session: Session,
    revision_id: str | None,
) -> CatalogDocumentRevision | None:
    """Load only an active canonical Recipe revision for Run/Switch."""

    if not isinstance(revision_id, str) or not revision_id:
        return None
    return session.scalar(
        select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.id == revision_id,
            CatalogDocumentRevision.kind == "recipe",
            CatalogDocumentRevision.state == "active",
        )
    )


@dataclass(frozen=True, slots=True)
class ArtifactInspection:
    """Evidence returned by the cache boundary for one high-level plan."""

    required_bytes: int | None
    reused_bytes: int
    copied_bytes: int
    missing_nas_bytes: int | None
    missing_spark_bytes: int | None
    reclaimable_bytes: int
    nas_coverage: RunSwitchCoverage
    spark_coverage: RunSwitchCoverage
    artifact_digests: tuple[str, ...] = ()
    reclaimable_digests: tuple[str, ...] = ()
    freshness: tuple[FreshnessEvidence, ...] = ()
    blockers: tuple[RunSwitchReason, ...] = ()
    warnings: tuple[RunSwitchReason, ...] = ()
    # Node-keyed breakdown of ``missing_spark_bytes`` (never total / N).
    missing_spark_bytes_by_node: Mapping[str, int] | None = None
    # A cache provider may expose the authoritative full manifest identity.
    # The Controller must never infer it from whichever files happen to be
    # present on one target.
    artifact_set_sha256: str | None = None
    artifact_set_bytes: int | None = None
    dependency_model_content_sha256: tuple[str, ...] = ()


class RunSwitchArtifactInspector(Protocol):
    """Read cache coverage without transferring or mutating any bytes."""

    def inspect(
        self,
        session: Session,
        *,
        model_content_sha256: str,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        retention: str,
        now: datetime,
    ) -> ArtifactInspection: ...


@dataclass(frozen=True, slots=True)
class PhaseExecution:
    """Result of one phase invocation.

    A phase can complete in the Controller transaction (``operation_id`` is
    ``None``) or hand off to an existing durable low-level operation.  The
    high-level job remains the only operation exposed to clients.
    """

    operation_id: str | None = None
    result: Mapping[str, object] | None = None
    waiting: bool = False
    status_reason: str | None = None


class RunSwitchArtifactPhaseExecutor(Protocol):
    """Injected cache boundary for transfer, verify, and Spark-local cleanup.

    Implementations may return a synchronous evidence result or a durable
    child operation.  They own NAS/cache transport and destination digest
    verification; this Controller service never downloads payload bytes.
    Cleanup implementations must be Spark-local and must not evict NAS data.
    """

    def execute(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> PhaseExecution: ...

    def get(self, operation_id: str) -> Any: ...


class RunSwitchPhaseExecutor(Protocol):
    """Execute a planned phase by composing existing lifecycle primitives."""

    def execute(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> PhaseExecution: ...


@runtime_checkable
class _PhasePreflightGate(Protocol):
    """Optional executor capability consulted before a phase is executed."""

    def __call__(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> tuple[LifecyclePreflightCheckpoint | None, str | None]: ...


@dataclass(frozen=True, slots=True)
class _ConflictRun:
    run: RecipeRun
    nodes: tuple[RunNode, ...]
    reserved_bytes: int
    stop_plan_digest: str | None


@dataclass(frozen=True, slots=True)
class _BuildSelection:
    """The exact build receipt or pending Controller build selected for a plan."""

    build: RecipeBuild | None
    candidate: RecipeBuild | None
    builder_freshness: FreshnessEvidence | None = None
    blockers: tuple[RunSwitchReason, ...] = ()


@dataclass(frozen=True, slots=True)
class _ResourceFits:
    """One current/after-stop resource decision for review and admission."""

    freshness: list[FreshnessEvidence]
    current: SparkFit
    after_stop: SparkFit | None
    current_blockers: list[RunSwitchReason]
    blockers: list[RunSwitchReason]
    warnings: list[RunSwitchReason]
    post_stop_memory_check: ConditionalPostStopMemoryCheck | None

    def requires_early_stop(self, *reason_prefixes: str) -> bool:
        return (
            not self.current.allowed
            and self.after_stop is not None
            and self.after_stop.allowed
            and any(
                reason.code.startswith(reason_prefixes)
                for reason in self.current_blockers
            )
        )

    @property
    def stop_before_prepare(self) -> bool:
        return (
            self.requires_early_stop(
                RunSwitchCode.INSUFFICIENT_MEMORY,
                f"run-switch.{ResourceBlockerCode.INSUFFICIENT}",
            )
            or self.post_stop_memory_check is not None
        )

    @property
    def stop_before_transfer(self) -> bool:
        return self.requires_early_stop(RunSwitchCode.INSUFFICIENT_DISK)


_TERMINAL_STATES = frozenset(
    job_states.words(
        LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
    )
)
_OPERATION_KINDS = frozenset(
    {"recipe.run-switch.v2", "recipe.stop.v2", "recipe.cleanup.v2"}
)
_MEMORY_CAPACITY_REFUSALS = frozenset(
    {
        f"run-switch.{ResourceBlockerCode.INSUFFICIENT_CAPACITY}",
        f"run-switch.{ResourceBlockerCode.INSUFFICIENT_CAPACITY_AFTER_STOP}",
    }
)
_MEMORY_STOP_CONDITIONAL_REFUSALS = _MEMORY_CAPACITY_REFUSALS | {
    f"run-switch.{ResourceBlockerCode.INSUFFICIENT_RESERVATION_BUDGET}",
    f"run-switch.{ResourceBlockerCode.RESIDENT_USAGE_UNKNOWN}",
}
_INSTALL_PREFLIGHT_REFRESH_REASON = (
    "runtime preflight expired during install compilation"
)
_RUNTIME_IMAGE_OWNER_CHANGED = RunSwitchCode.RUNTIME_IMAGE_OWNER_CHANGED
_RUNTIME_IMAGE_IDENTITY_MISMATCH = (
    RunSwitchCode.RUNTIME_IMAGE_REFERENCE_IDENTITY_MISMATCH
)
_LOGGER = logging.getLogger("vonk-control-run-switch")
_FINAL_VERIFICATION_MAX_SECONDS = 900
_PHASES: tuple[RunSwitchPhaseKind, ...] = (
    "transfer",
    "verify",
    "prepare",
    "cleanup",
    "stop",
    "uninstall",
    "start",
    "final_verify",
)


def _refused_retries(progress: RunSwitchOperationResult) -> int:
    """How many times in a row admission was refused for capacity (0 if not)."""

    reason = progress.retry_reason
    attempt = progress.retry_attempt
    if (
        isinstance(reason, str)
        and reason.endswith(".capacity_busy")
        and type(attempt) is int
    ):
        return attempt
    return 0


def _now(clock: Any) -> datetime:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise InvalidValue("run/switch clock must be timezone-aware")
    return value.astimezone(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


_PLAN_VOLATILE_KEYS = frozenset(
    {
        "generated_at",
        "invocation",
        "plan_digest",
        "age_seconds",
        "verified_at",
        # Node-keyed breakdowns of byte totals that are already bound.
        "missing_spark_bytes_by_node",
        "missing_image_distribution_bytes_by_node",
    }
)


def _plan_identity(value: object) -> object:
    """Remove presentation and wall-clock fields before digesting a plan.

    The plan still binds observed freshness state, timestamps, and evidence
    digests.  Relative age and locally generated verification timestamps are
    omitted so a preview can be applied a moment later without changing its
    authority merely because the clock advanced.
    """

    if isinstance(value, Mapping):
        return {
            str(key): _plan_identity(item)
            for key, item in value.items()
            if str(key) not in _PLAN_VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_plan_identity(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_plan_identity(item) for item in value)
    return value


def _as_reason(
    code: str,
    detail: str,
    *,
    scope: RunSwitchReasonScope,
    severity: RunSwitchReasonSeverity = "blocker",
    node_ids: Sequence[str] = (),
    stale: bool = False,
) -> RunSwitchReason:
    return RunSwitchReason(
        code=code,
        detail=detail[:512],
        severity=severity,
        scope=scope,
        node_ids=list(node_ids),
        stale=stale,
    )


def _run_switch_payload(job: Job) -> RunSwitchJobPayload | None:
    payload = read_row_column(job, "payload")
    return payload if isinstance(payload, RunSwitchJobPayload) else None


def _stored_job_plan(job: Job) -> RunSwitchPlan | None:
    payload = _run_switch_payload(job)
    return payload.plan if payload is not None else None


def _start_parent(job: Job | None) -> RecipeStartParent | None:
    payload = read_row_column(job, "payload") if job is not None else None
    return payload if isinstance(payload, RecipeStartParent) else None


def _required_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _resource_reason(
    reason: object, *, node_ids: Sequence[str] = ()
) -> RunSwitchReason:
    node_id = getattr(reason, "node_id", None)
    return _as_reason(
        run_switch_code(getattr(reason, "code", ResourcePlanningCode.EVIDENCE_UNKNOWN)),
        str(getattr(reason, "detail", "Resource planning evidence is unavailable.")),
        scope="node" if isinstance(node_id, str) else "operation",
        severity=_REASON_SEVERITY_ADAPTER.validate_python(
            getattr(reason, "severity", "blocker"), strict=True
        ),
        node_ids=(node_id,) if isinstance(node_id, str) else node_ids,
    )


def _is_memory_reservation_kind(
    value: str,
) -> TypeGuard[Literal["host-memory", "gpu-memory", "unified-memory"]]:
    return value in MEMORY_RESERVATION_KINDS


def _conditional_post_stop_memory_check(
    session: Session,
    stops: Sequence[StopImpact],
    fit: SparkFit,
    blockers: Sequence[RunSwitchReason],
    insufficient_components: Mapping[str, frozenset[str]],
) -> ConditionalPostStopMemoryCheck | None:
    """Permit only a known-capacity memory shortfall covered by exact stop claims."""

    if (
        not stops
        or not blockers
        or any(
            reason.code not in _MEMORY_STOP_CONDITIONAL_REFUSALS for reason in blockers
        )
    ):
        return None
    blocked_nodes = {node_id for reason in blockers for node_id in reason.node_ids}
    nodes = {node.node_id: node for node in fit.nodes}
    if not blocked_nodes or not blocked_nodes <= nodes.keys():
        return None
    for node in fit.nodes:
        if (
            node.memory_required_bytes is None
            or node.memory_kind is None
            or node.memory_pool is None
            or node.memory_floor_bytes is None
            or node.memory_capacity_bytes is None
            or node.memory_available_bytes is None
            or node.resource_demand is None
            or node.memory_capacity_bytes
            < node.memory_required_bytes + node.memory_floor_bytes
        ):
            return None
    for node_id in blocked_nodes:
        node = nodes[node_id]
        components = insufficient_components.get(node_id, frozenset())
        if not components or node.memory_pool is None:
            return None
        for component in components:
            reservation_kind = {
                "host": "host-memory",
                "accelerator": "gpu-memory",
                "shared": "unified-memory",
            }.get(component)
            if reservation_kind is None:
                return None
            claim_kinds = memory_reservation_kinds(reservation_kind, node.memory_pool)
            if not any(
                node_id in stop.node_ids
                and any(
                    claim.kind in claim_kinds
                    for claim in reviewed_run_memory_reservations(
                        session, node_id, stop
                    )
                )
                for stop in stops
            ):
                return None
    return ConditionalPostStopMemoryCheck(
        stop_run_ids=sorted(stop.run_id for stop in stops)
    )


def _settings_view(settings: object) -> EffectiveSettingsSelection:
    resolution = resolve_effective_settings(settings)
    if resolution.settings is None:
        raise InvalidValue("effective settings are invalid")
    resolved = resolution.settings
    return EffectiveSettingsSelection(
        kind=resolved.kind,
        context_tokens=resolved.context_tokens,
        concurrency=resolved.concurrency,
        max_batch_tokens=resolved.batch_tokens,
        parallelism=EffectiveParallelism(
            world_size=resolved.parallelism.world_size,
            tensor=resolved.parallelism.tensor,
            pipeline=resolved.parallelism.pipeline,
            data=resolved.parallelism.data,
            backend=resolved.parallelism.backend,
        ),
        knobs=_KNOBS_ADAPTER.validate_python(dict(resolved.knobs), strict=True),
        change_effects=_CHANGE_EFFECTS_ADAPTER.validate_python(
            dict(resolved.change_effects), strict=True
        ),
        identity_sha256=resolved.identity_digest,
    )


def _resource_evidence_digest(revision_digest: str | None) -> str | None:
    return (
        revision_digest
        if isinstance(revision_digest, str) and len(revision_digest) == 64
        else None
    )


def _latest_inventory(
    session: Session,
    node_id: str,
    *,
    now: datetime,
    maximum_age_seconds: int,
) -> tuple[NodeInventorySnapshot | None, FreshnessEvidence]:
    snapshot = session.scalar(
        select(NodeInventorySnapshot)
        .where(NodeInventorySnapshot.node_id == node_id)
        .order_by(NodeInventorySnapshot.observed_at.desc())
        .limit(1)
    )
    if snapshot is None:
        return None, FreshnessEvidence(
            source=f"spark:{node_id}:inventory",
            state="unknown",
            maximum_age_seconds=maximum_age_seconds,
        )
    observed = snapshot.observed_at
    if observed.tzinfo is None or observed.utcoffset() is None:
        observed = observed.replace(tzinfo=UTC)
    age = max(0.0, (now - observed.astimezone(UTC)).total_seconds())
    fresh = age <= maximum_age_seconds
    return snapshot, FreshnessEvidence(
        source=f"spark:{node_id}:inventory",
        state="fresh" if fresh else "stale",
        observed_at=observed.astimezone(UTC),
        age_seconds=age,
        maximum_age_seconds=maximum_age_seconds,
        evidence_digest=(
            snapshot.evidence_digest
            if isinstance(snapshot.evidence_digest, str)
            and len(snapshot.evidence_digest) == 64
            else None
        ),
    )


def _inspection_unavailable(detail: str) -> ArtifactInspection:
    """What a coverage inspection that could not be made says: coverage unknown,
    with a blocker the plan observes again (never a refusal of the request)."""

    return ArtifactInspection(
        required_bytes=None,
        reused_bytes=0,
        copied_bytes=0,
        missing_nas_bytes=None,
        missing_spark_bytes=None,
        reclaimable_bytes=0,
        nas_coverage="unknown",
        spark_coverage="unknown",
        artifact_digests=(),
        reclaimable_digests=(),
        blockers=(
            _as_reason(
                RunSwitchCode.ARTIFACT_INSPECTION_UNAVAILABLE,
                f"Artifact coverage could not be inspected: {detail}",
                scope="artifact",
            ),
        ),
    )


class DatabaseRunSwitchArtifactInspector:
    """Conservative Spark-side coverage inspector.

    The Controller model-cache provider is the sole authority for NAS
    coverage.  A database row can describe target-local observations, but it
    cannot identify the complete immutable model set or its NAS download
    plan.  Construction without the provider therefore fails explicitly.
    """

    def __init__(self, model_cache: ModelCacheService | None = None) -> None:
        self._model_cache = model_cache

    def bind_model_cache(self, model_cache: ModelCacheService) -> None:
        """Attach the Controller model-cache authority after startup wiring."""

        self._model_cache = model_cache

    def inspect(
        self,
        session: Session,
        *,
        model_content_sha256: str,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        retention: str,
        now: datetime,
    ) -> ArtifactInspection:
        if self._model_cache is None:
            return _inspection_unavailable(
                "model-cache manifest provider is unavailable"
            )
        return self._inspect_model_cache(
            session,
            model_cache=self._model_cache,
            model_content_sha256=model_content_sha256,
            recipe_revision_id=recipe_revision_id,
            node_ids=node_ids,
            retention=retention,
            now=now,
        )

    def _inspect_model_cache(
        self,
        session: Session,
        *,
        model_cache: ModelCacheService,
        model_content_sha256: str,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        retention: str,
        now: datetime,
    ) -> ArtifactInspection:
        """Read exact model identity from the cache manifest provider.

        Resolution is metadata-only.  A partial NAS set is a planned download
        when the trusted catalog manifest resolves; only an unavailable or
        contradictory provider becomes a blocker.
        """

        try:
            manifest = model_cache.resolve_artifact_set(
                model_content_sha256=model_content_sha256,
                recipe_revision_id=recipe_revision_id,
            )
            preview_document = model_cache.download_preview(
                model_content_sha256=model_content_sha256,
                recipe_revision_id=recipe_revision_id,
            )
            preview = ModelCacheDownloadPreviewResponse.model_validate(
                {
                    key: value
                    for key, value in preview_document.items()
                    if not key.startswith("_")
                }
            )
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
            return _inspection_unavailable(
                f"model-cache exact manifest is unavailable: {error}"
            )
        artifact_set_sha256 = manifest.digest
        if preview.artifact_set_sha256 != artifact_set_sha256:
            return _inspection_unavailable(
                "model-cache download preview does not match its manifest"
            )
        if manifest.model_content_sha256 != model_content_sha256:
            return _inspection_unavailable(
                "model-cache manifest model identity does not match the request"
            )
        # The cache authority validates the manifest.  Consume its exact typed
        # fields, including empty support files and shared physical objects.
        expected_by_digest = {
            artifact.sha256: artifact.expected_bytes for artifact in manifest.artifacts
        }
        model_digests = tuple(expected_by_digest)
        artifact_bytes = manifest.expected_bytes
        # Transfer checkpoints reduce the bytes left to download, but do not
        # make an object verified or available for profile admission.
        missing_nas_bytes = (
            sum(expected_by_digest.values()) - preview.already_cached_bytes
        )
        blockers = [
            _as_reason(
                RunSwitchCode.NAS_DOWNLOAD_BLOCKED,
                detail,
                scope="artifact",
                node_ids=node_ids,
            )
            for detail in preview.blockers
        ]
        reused = 0
        missing_spark = 0
        missing_by_node: dict[str, int] = {}
        reclaimable = 0
        reclaimable_digests: set[str] = set()
        for node_id in node_ids:
            missing_by_node[node_id] = 0
            rows = tuple(
                session.scalars(
                    select(NodeArtifact).where(NodeArtifact.node_id == node_id)
                )
            )
            by_digest = {row.digest: row for row in rows}
            for digest, size in expected_by_digest.items():
                row = by_digest.get(digest)
                if (
                    row is not None
                    and row.state == ModelFileState.VERIFIED
                    and row.size_bytes == size
                ):
                    reused += size
                else:
                    missing_spark += size
                    missing_by_node[node_id] += size
            if retention == "reclaim-unreferenced":
                for row in rows:
                    if (
                        row.digest in expected_by_digest
                        and row.state == ModelFileState.VERIFIED
                        and row.ref_count == 0
                    ):
                        reclaimable += row.size_bytes
                        reclaimable_digests.add(row.digest)
        warnings: list[RunSwitchReason] = []
        if missing_nas_bytes:
            warnings.append(
                _as_reason(
                    RunSwitchCode.NAS_DOWNLOAD_REQUIRED,
                    "The exact model artifact set is resolved but missing from the NAS cache; the operation will download it before Spark transfer.",
                    scope="artifact",
                    severity="warning",
                    node_ids=node_ids,
                )
            )
        dependencies = tuple(
            sorted(set(manifest.model_content_digests) - {model_content_sha256})
        )
        return ArtifactInspection(
            required_bytes=artifact_bytes * len(node_ids),
            reused_bytes=reused,
            copied_bytes=missing_spark,
            missing_nas_bytes=missing_nas_bytes,
            missing_spark_bytes=missing_spark,
            missing_spark_bytes_by_node=missing_by_node,
            reclaimable_bytes=reclaimable,
            nas_coverage="complete" if missing_nas_bytes == 0 else "partial",
            spark_coverage="complete" if missing_spark == 0 else "partial",
            artifact_digests=tuple(model_digests),
            reclaimable_digests=tuple(sorted(reclaimable_digests)),
            freshness=(),
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            artifact_set_sha256=artifact_set_sha256,
            artifact_set_bytes=artifact_bytes,
            dependency_model_content_sha256=dependencies,
        )


def _refreshed_freshness(
    reviewed: Sequence[FreshnessEvidence], fresh: Sequence[FreshnessEvidence]
) -> list[FreshnessEvidence]:
    """The reviewed evidence, with each source the recheck read replaced by it."""

    current = {item.source: item for item in fresh}
    kept = [current.pop(item.source, item) for item in reviewed]
    return [*kept, *current.values()]


def _recipe_model_digests(revision: CatalogDocumentRevision | None) -> frozenset[str]:
    """The content digests of the models a recipe revision names."""

    document = revision.document if revision is not None else None
    models = document.get("models") if isinstance(document, Mapping) else None
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes)):
        return frozenset()
    found: set[str] = set()
    for item in models:
        model = item.get("model") if isinstance(item, Mapping) else None
        digest = model.get("content_sha256") if isinstance(model, Mapping) else None
        if isinstance(digest, str):
            found.add(digest)
    return frozenset(found)


def _container_build_result(build: RecipeBuild) -> dict[str, object]:
    """Project the authoritative build record into its phase receipt."""
    succeeded = build.state == "succeeded"
    try:
        receipt = RunSwitchContainerBuildResult(
            phase="prepare",
            subphase="container-build",
            build_id=build.id,
            build_input_sha256=build.build_input_sha256,
            state=_CONTAINER_BUILD_STATE_ADAPTER.validate_python(
                build.state, strict=True
            ),
            image_digest=build.image_digest if succeeded else None,
            oci_layout_sha256=build.oci_layout_sha256 if succeeded else None,
            image_bytes=build.image_bytes if succeeded else None,
        )
    except ValidationError as error:
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_EVIDENCE_INVALID,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        ) from error
    return json.loads(canonical_message(receipt))


class RecipeLifecyclePhaseExecutor:
    """Default executor for phases covered by existing recipe primitives."""

    def __init__(
        self,
        lifecycle: RecipeOperationService,
        sessions: sessionmaker[Session],
        mappings: ClusterMappingService,
        clock: Any,
        artifact_executor: RunSwitchArtifactPhaseExecutor | None = None,
        inventory_max_age_seconds: int = 300,
    ) -> None:
        self._lifecycle = lifecycle
        self._sessions = sessions
        self._mappings = mappings
        self._clock = clock
        self._artifact_executor = artifact_executor
        self._inventory_max_age = inventory_max_age_seconds
        self._preflight = None

    def preflight(self, plan, phase, *, actor, request_key, progress):
        if (
            phase.kind in {"stop", "cleanup", "final_verify", "verify"}
            or phase.state != "planned"
        ):
            return None, None
        with self._sessions() as session:
            revision = session.get(CatalogDocumentRevision, plan.recipe_revision_id)
            if (
                revision is None
                or revision.content_digest != plan.recipe_content_sha256
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.PREFLIGHT_RECIPE_CHANGED,
                    reason=WaitReason.SCOPE_CHANGED,
                )
            document = revision.document
        nodes = {node.node_id: False for node in plan.spark_group.nodes}
        if (
            phase.subphase == "container-build"
            and plan.build.builder_node_id
            or not progress.completed_phases
            and plan.build.state in {"planned", "building"}
            and plan.build.builder_node_id
        ):
            nodes[plan.build.builder_node_id] = True
        previous = progress.preflight
        if self._preflight is None:
            self._preflight = LifecyclePreflight(
                self._sessions,
                self._lifecycle._agent_jobs,
                self._clock,
                self._lifecycle._install_admission._disk_floor,
            )
        return self._preflight.ensure(
            document=document,
            nodes=nodes,
            phase_index=phase.index,
            request_key=request_key,
            actor=actor,
            previous=read_stored_model(
                LifecyclePreflightCheckpoint,
                canonical_message(previous),
                from_json=True,
            )
            if previous
            else None,
        )

    def _require_post_stop_inventory(self, plan: RunSwitchPlan) -> None:
        if not plan.stops:
            return
        now = _now(self._clock)
        expected_pools = {
            node.node_id: node.memory_pool for node in plan.fit_current.nodes
        }
        inventory = InventoryRepository(self._sessions, clock=lambda: now)
        with self._sessions() as session:
            for stop in plan.stops:
                run = session.get(RecipeRun, stop.run_id)
                if run is None or run.plan_digest != stop.run_plan_digest:
                    raise RunSwitchRetryLater(
                        RunSwitchCode.STOPPED_RUN_IDENTITY_CHANGED,
                        reason=WaitReason.SCOPE_CHANGED,
                    )
                members = set(
                    session.scalars(
                        select(RunNode.node_id).where(RunNode.run_id == run.id)
                    )
                )
                if members != set(stop.node_ids):
                    raise RunSwitchRetryLater(
                        RunSwitchCode.STOPPED_RUN_MEMBERSHIP_CHANGED,
                        reason=WaitReason.SCOPE_CHANGED,
                    )
                if run.state != RunState.STOPPED or run.stopped_at is None:
                    raise RunSwitchPostStopEvidencePending(
                        f"Stop receipt for {stop.run_id} is not complete"
                    )
                stopped_at = _aware(run.stopped_at)
                for node_id in sorted(members & expected_pools.keys()):
                    try:
                        snapshot = inventory.latest(
                            node_id,
                            now=now,
                            maximum_age=self._inventory_max_age,
                            _session=session,
                        )
                    except KeyError:
                        raise RunSwitchPostStopEvidencePending(
                            f"Spark {node_id} has no post-stop inventory"
                        ) from None
                    if snapshot.memory_pool != expected_pools.get(node_id):
                        raise RunSwitchRetryLater(
                            RunSwitchCode.POST_STOP_MEMORY_POOL_CHANGED,
                            reason=WaitReason.SCOPE_CHANGED,
                        )
                    # A sample received after the receipt can still have been
                    # collected before the stop. Preserve the producer clock's
                    # full admitted lead instead of treating receipt time as
                    # collection time or a released promise as physical memory.
                    if (
                        snapshot.stale
                        or _aware(snapshot.received_at) < stopped_at
                        or snapshot.observed_at
                        <= stopped_at + MAX_INVENTORY_FUTURE_SKEW
                    ):
                        raise RunSwitchPostStopEvidencePending(
                            f"Spark {node_id} needs inventory collected after stop {stop.run_id} "
                            f"({(stopped_at + MAX_INVENTORY_FUTURE_SKEW).isoformat()}; "
                            f"latest {snapshot.observed_at.isoformat()})",
                            collected_after=stopped_at + MAX_INVENTORY_FUTURE_SKEW,
                        )

    def _execute_container_build(
        self,
        plan: RunSwitchPlan,
        *,
        phase_index: int,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> PhaseExecution:
        """Start or replay the existing durable ``recipe.build.v1`` child."""

        build_id = plan.recipe_build_id or plan.build.build_id
        revision_id = plan.recipe_revision_id
        if build_id is None or revision_id is None:
            raise RunSwitchRetryLater(
                RunSwitchCode.CONTAINER_BUILD_IDENTITY_UNAVAILABLE,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        expected_build_id = _string_or_none(plan.build.build_id)
        expected_build_input = _string_or_none(plan.build.build_input_sha256)
        if expected_build_id != build_id or expected_build_input is None:
            raise RunSwitchRetryLater(
                RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID, reason=WaitReason.STALE_PLAN
            )
        ordinal = _bound_workload_intent(progress)

        def admission_guard(session: Session) -> None:
            _lock_current_build_parent(
                session,
                plan=plan,
                phase_index=phase_index,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                ordinal=ordinal,
            )

        with self._sessions.begin() as session:
            admission_guard(session)
            build = session.get(RecipeBuild, build_id)
            if build is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_RECEIPT_UNAVAILABLE,
                    reason=WaitReason.RECEIPT_MISSING,
                )
            # Reconnecting bypasses capacity admission, never identity. Both
            # a completed receipt and an active child must match the reviewed
            # executable inputs before either may be adopted.
            if expected_build_input != build.build_input_sha256:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                    reason=WaitReason.STALE_PLAN,
                )
            if build.state == "succeeded":
                return PhaseExecution(result=_container_build_result(build))
            if build.state not in {"planned", "building", "failed"}:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_STATE_INVALID,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            active = session.scalar(
                select(Job)
                .where(
                    Job.kind == "recipe.build.v1",
                    Job.state.in_(("queued", "running")),
                    Job.payload["owner_id"].as_string() == build.id,
                    Job.payload["plan_digest"].as_string() == build.build_input_sha256,
                )
                .order_by(Job.updated_at.desc(), Job.id)
                .limit(1)
            )
            if active is not None:
                return PhaseExecution(active.id, _container_build_result(build))
            builder_node_id = build.builder_node_id
            build_input_sha256 = build.build_input_sha256
            source_bundle_sha256 = build.source_bundle_sha256
            try:
                stored_plan = build_plan_document(build.plan)
            except RecipeExecutionContractError as error:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                    reason=WaitReason.STALE_PLAN,
                ) from error
        self._require_post_stop_inventory(plan)
        start_build = getattr(self._lifecycle, "build", None)
        if not callable(start_build):
            raise RunSwitchRetryLater(
                RunSwitchCode.CONTAINER_BUILD_EXECUTOR_UNAVAILABLE,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        # Preview already selected and persisted the exact executable build
        # plan in ``RecipeBuild.plan``.  Re-running preview here would admit
        # mutable builder evidence a second time and could derive a different
        # build id/input digest between preview and apply.  Consume the
        # durable producer record instead; the lifecycle build primitive still
        # validates the current builder runtime and resource admission before
        # it queues the child.
        with self._sessions() as session:
            revision = session.get(CatalogDocumentRevision, revision_id)
            try:
                parsed_plan = parse_stored_build_plan(stored_plan)
            except RecipeExecutionContractError as error:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                    reason=WaitReason.STALE_PLAN,
                ) from error
            if (
                parsed_plan.build_id != build_id
                or parsed_plan.recipe_revision_id != revision_id
                or parsed_plan.source_bundle_sha256 != source_bundle_sha256
                or parsed_plan.build_input_sha256 != build_input_sha256
                or revision is None
                or revision.kind != "recipe"
                or revision.state != "active"
                or parsed_plan.recipe_content_sha256 != revision.content_digest
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                    reason=WaitReason.STALE_PLAN,
                )
            try:
                build_plan = RecipeBuildPlan(
                    build_id=build_id,
                    recipe_revision_id=revision_id,
                    recipe_content_sha256=revision.content_digest,
                    builder_node_id=builder_node_id,
                    source_bundle_sha256=source_bundle_sha256,
                    build_input_sha256=build_input_sha256,
                    agent_payload=dict(stored_plan),
                )
            except (TypeError, ValueError) as error:
                raise RunSwitchRetryLater(
                    f"{RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID}: {error}",
                    reason=WaitReason.STALE_PLAN,
                ) from error
        child_key = str(uuid.uuid5(uuid.UUID(request_key), "container-build"))
        try:
            value = start_build(
                build_plan,
                build_input_sha256=build_input_sha256,
                actor=actor,
                request_id=child_key,
                admission_guard=admission_guard,
            )
        except (RecipeBuildAdmissionBusy, _RunSwitchBuildParentChanged):
            raise
        except (
            KeyError,
            RecipeOperationConflict,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            raise RunSwitchRetryLater(
                f"{RunSwitchCode.CONTAINER_BUILD_START_UNAVAILABLE}: {error}",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        with self._sessions.begin() as session:
            admission_guard(session)
            persisted = session.get(RecipeBuild, build_id)
            if persisted is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_RECEIPT_UNAVAILABLE,
                    reason=WaitReason.RECEIPT_MISSING,
                )
            if (
                persisted.recipe_revision_id != revision_id
                or persisted.build_input_sha256 != build_input_sha256
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                    reason=WaitReason.STALE_PLAN,
                )
            result = _container_build_result(persisted)
            if persisted.state == "succeeded":
                return PhaseExecution(result=result)
        return PhaseExecution(_started_operation_id(value), result)

    def _observe_older_issued(
        self,
        kind: str,
        owner_id: str,
        ordinal: int,
        *,
        profile_target_node_ids: Sequence[str] | None = None,
    ) -> None:
        pending = self._lifecycle.assess_superseded_issued(
            kind,
            owner_id,
            ordinal,
            profile_target_node_ids=profile_target_node_ids,
        )
        if pending is not None:
            raise RunSwitchIssuedWorkloadPending(
                kind=kind,
                owner_id=owner_id,
                job_id=pending.job_id,
                observe_due_at=pending.observe_due_at,
                observation_deadline=pending.observation_deadline,
            )

    def execute(
        self,
        plan: RunSwitchPlan,
        phase: RunSwitchPhase,
        *,
        item_index: int,
        actor: str,
        request_key: str,
        progress: RunSwitchOperationResult,
    ) -> PhaseExecution:
        if phase.kind in {"transfer", "verify", "cleanup"}:
            if self._artifact_executor is None:
                raise RunSwitchRetryLater(
                    f"run-switch.{phase.kind}-executor-unavailable"
                )
            execution = self._artifact_executor.execute(
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
            if execution.operation_id is None and execution.waiting:
                raise RunSwitchRetryLater(
                    f"run-switch.{phase.kind}-waiting-without-child"
                )
            if (
                execution.operation_id is None
                and execution.result is None
                and not execution.waiting
            ):
                raise RunSwitchRetryLater(
                    f"run-switch.{phase.kind}-returned-no-evidence"
                )
            if execution.operation_id is None:
                _validate_artifact_execution(plan, phase, execution.result)
            return execution
        if phase.kind == "prepare" and phase.subphase == "runtime-image":
            if self._artifact_executor is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.RUNTIME_IMAGE_EXECUTOR_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            execution = self._artifact_executor.execute(
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
            if (
                execution.operation_id is None
                and execution.waiting
                and phase.subphase != "runtime-image"
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.RUNTIME_IMAGE_WAITING_WITHOUT_CHILD,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            if execution.operation_id is None and not execution.waiting:
                _validate_artifact_execution(plan, phase, execution.result)
            return execution
        if phase.kind == "stop":
            if item_index >= len(plan.stops):
                return PhaseExecution()
            target = plan.stops[item_index]
            ordinal = _bound_workload_intent(progress)
            stop_digest = target.plan_digest
            profile_target_node_ids = (
                plan.profile_stop_scope.target_node_ids
                if plan.profile_stop_scope is not None
                else None
            )
            self._lifecycle.reconcile_superseded_unissued(
                "recipe.stop",
                target.run_id,
                ordinal,
                profile_target_node_ids=profile_target_node_ids,
            )
            if profile_target_node_ids is not None:
                self._observe_older_issued(
                    "recipe.stop",
                    target.run_id,
                    ordinal,
                    profile_target_node_ids=profile_target_node_ids,
                )
            if target.state in {RunState.STARTING, RunState.STOPPING}:
                # A newer explicit Stop can cancel an older same-run command
                # under the current node ordinal.  Re-preview this exact run
                # because its prior Start/Stop may have changed state after
                # the high-level plan was reviewed.  The low-level Stop
                # authority validates current membership and reservations.
                with self._sessions() as session:
                    run = session.get(RecipeRun, target.run_id)
                    if run is None:
                        raise RunSwitchRetryLater(RunSwitchCode.STOP_TARGET_DISAPPEARED)
                    if (
                        run.state == RunState.STOPPED
                        and run.route_state == RouteState.WITHDRAWN
                    ):
                        return PhaseExecution(result={"run_id": target.run_id})
                fresh = self._lifecycle.preview_stop(
                    target.run_id,
                    profile_target_node_ids=profile_target_node_ids,
                )
                if not fresh.allowed:
                    raise RunSwitchRetryLater(
                        RunSwitchCode.STOP_STILL_UNRESOLVED_AFTER_CANCELLATION
                    )
                stop_digest = fresh.plan_digest
            profile_application_id = _string_or_none(progress.profile_application_id)
            child_key = str(uuid.uuid5(uuid.UUID(request_key), f"stop:{target.run_id}"))
            if profile_application_id is not None:
                child_key = str(
                    uuid.uuid5(
                        uuid.UUID(child_key),
                        f"profile-stop:{profile_application_id}",
                    )
                )
            try:
                value = self._lifecycle.stop(
                    target.run_id,
                    plan_digest=stop_digest,
                    actor=actor,
                    request_id=child_key,
                    workload_intent_ordinal=ordinal,
                    profile_target_node_ids=profile_target_node_ids,
                    profile_application_id=profile_application_id,
                )
            except RecipeArtifactJobCancellationPending as pending:
                raise RunSwitchIssuedWorkloadPending(
                    kind="artifact-job-cancellation",
                    owner_id=target.run_id,
                    job_id=pending.job_id,
                    observe_due_at=pending.observe_due_at,
                    observation_deadline=pending.observation_deadline,
                ) from pending
            return PhaseExecution(value.id, {"run_id": target.run_id})
        if phase.kind == "prepare" and phase.subphase == "container-build":
            return self._execute_container_build(
                plan,
                phase_index=phase.index,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
        if phase.kind == "prepare" and phase.subphase == "runtime-plan":
            # Bind and persist the exact schema-2 launch plan at the
            # Controller boundary.  This phase deliberately does not enqueue
            # Spark work: target-copy must verify every model/image receipt
            # before the agent install child can start.
            mapping_id = plan.mapping.mapping_id if plan.mapping is not None else None
            phase_results = progress.phase_results
            if phase_results:
                for result in reversed(phase_results):
                    if isinstance(result, RunSwitchRuntimePlanResult):
                        mapping_id = result.mapping_id or mapping_id
            if (
                mapping_id is None
                and plan.mapping is not None
                and plan.mapping.action == "create"
            ):
                mapping_plan = ClusterMappingPlan(
                    recipe_revision_id=_required_string(plan.recipe_revision_id),
                    recipe_content_sha256=_required_string(plan.recipe_content_sha256),
                    topology_name=plan.mapping.topology_name,
                    generation=_required_int(plan.mapping.mapping_generation) or 1,
                    parameters=dict(plan.mapping.parameters),
                    nodes=tuple(_mapping_node(node) for node in plan.mapping.nodes),
                    placement_digest=plan.mapping.placement_digest,
                )
                mapping_id = self._mappings.materialize(
                    mapping_plan, actor=actor, now=_now(self._clock)
                )
            if mapping_id is None:
                return PhaseExecution(result={"prepared": True})
            profile_application_id = _string_or_none(progress.profile_application_id)
            if profile_application_id is not None:
                with self._sessions() as session:
                    try:
                        handed_off = prepared_profile_installation(
                            session,
                            profile_application_id,
                            _required_string(plan.recipe_revision_id),
                            tuple(node.node_id for node in plan.spark_group.nodes),
                            workload_intent_ordinal=_bound_workload_intent(progress),
                        )
                    except ProfileHandoffInconsistent as error:
                        raise RunSwitchRetryLater(
                            f"{RunSwitchCode.INSTALLATION_HANDOFF_INCONSISTENT}: {error}",
                            reason=WaitReason.SCOPE_CHANGED,
                        ) from error
                    if handed_off is not None:
                        installation_id, install_plan_digest = handed_off
                        installation, _ = self._bound_installation(
                            session,
                            plan,
                            installation_id,
                            mapping_id,
                            install_plan_digest,
                        )
                        if installation.state not in {
                            InstallationState.PLANNED,
                            InstallationState.INSTALLING,
                            InstallationState.PARTIAL,
                            InstallationState.INSTALLED,
                        }:
                            raise RunSwitchRetryLater(
                                RunSwitchCode.INSTALLATION_HANDOFF_UNAVAILABLE,
                                reason=WaitReason.OBSERVATION_UNAVAILABLE,
                            )
                        stored = parse_stored_installation_plan(installation.plan)
                        if stored.plan_digest != install_plan_digest:
                            raise RunSwitchRetryLater(
                                RunSwitchCode.INSTALLATION_IDENTITY_CHANGED,
                                reason=WaitReason.SCOPE_CHANGED,
                            )
                        return self._prepared_installation_result(
                            installation_id,
                            mapping_id,
                            install_plan_digest,
                            stored.compiled_execution_plans,
                        )
            try:
                install_plan = self._lifecycle.preview_install(
                    mapping_id,
                    plan.recipe_build_id,
                    profile_application_id=_string_or_none(
                        progress.profile_application_id
                    ),
                )
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise RunSwitchRetryLater(
                    f"{RunSwitchCode.INSTALL_PLAN_UNAVAILABLE}: {error}",
                    reason=WaitReason.STALE_PLAN,
                ) from error
            prepare_installation = getattr(
                self._lifecycle, "prepare_installation", None
            )
            if not callable(prepare_installation):
                raise RunSwitchRetryLater(
                    RunSwitchCode.INSTALL_PREPARATION_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            try:
                installation_id = prepare_installation(
                    install_plan,
                    actor=actor,
                    profile_application_id=_string_or_none(
                        progress.profile_application_id
                    ),
                    workload_intent_ordinal=_bound_workload_intent(progress),
                )
            except (RecipeInstallPreflightExpired, InstallPreflightExpired) as error:
                # Compiling the launch document above can outlast the runtime
                # preflight window this phase was admitted on.  Nothing else
                # about the install changed, so ask the caller to rerun the
                # ordinary probe rather than failing an identical plan.
                raise RunSwitchInstallPreflightExpired(
                    f"{RunSwitchCode.INSTALL_PREFLIGHT_EXPIRED}: {error}"
                ) from error
            except InstallAdmissionBusy:
                raise
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise RunSwitchRetryLater(
                    f"{RunSwitchCode.INSTALL_PREPARATION_FAILED}: {error}"
                ) from error
            prepared_id = _required_string(installation_id)
            with self._sessions() as session:
                installation = session.get(RecipeInstallation, prepared_id)
                if installation is None:
                    raise RunSwitchRetryLater(
                        RunSwitchCode.INSTALL_PREPARATION_UNAVAILABLE,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                try:
                    stored_plan = parse_stored_installation_plan(installation.plan)
                except RecipeExecutionContractError as error:
                    raise RunSwitchRetryLater(
                        RunSwitchCode.INSTALLATION_IDENTITY_UNAVAILABLE,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    ) from error
            return self._prepared_installation_result(
                prepared_id,
                mapping_id,
                stored_plan.plan_digest,
                stored_plan.compiled_execution_plans,
            )
        if phase.kind == "prepare" and phase.subphase == "runtime-install":
            ordinal = _bound_workload_intent(progress)
            installation_id = plan.installation_id
            phase_results = progress.phase_results
            if installation_id is None and isinstance(phase_results, list):
                for result in reversed(phase_results):
                    if isinstance(
                        result,
                        RunSwitchRuntimePlanResult | RunSwitchRuntimeInstallResult,
                    ):
                        installation_id = result.installation_id
                        break
            if installation_id is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.INSTALLATION_PREPARATION_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            self._lifecycle.reconcile_superseded_unissued(
                "recipe.install", installation_id, ordinal
            )
            self._observe_older_issued("recipe.install", installation_id, ordinal)
            start_installation = getattr(self._lifecycle, "start_installation", None)
            if not callable(start_installation):
                raise RunSwitchRetryLater(
                    RunSwitchCode.INSTALL_EXECUTOR_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            try:
                value = start_installation(
                    installation_id,
                    actor=actor,
                    request_id=str(
                        uuid.uuid5(uuid.UUID(request_key), "runtime-install")
                    ),
                    workload_intent_ordinal=ordinal,
                )
            except InstallAdmissionBusy:
                raise
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise RunSwitchRetryLater(
                    f"{RunSwitchCode.INSTALL_START_FAILED}: {error}"
                ) from error
            return PhaseExecution(
                _started_operation_id(value),
                {"installation_id": installation_id},
            )
        if phase.kind == "prepare":
            raise _RunSwitchDefiniteConflict(RunSwitchCode.PREPARE_SUBPHASE_UNSUPPORTED)
        if phase.kind == "start":
            ordinal = _bound_workload_intent(progress)
            installation_id = plan.installation_id
            phase_results = progress.phase_results
            if installation_id is None and isinstance(phase_results, list):
                for result in reversed(phase_results):
                    if isinstance(
                        result,
                        RunSwitchRuntimePlanResult | RunSwitchRuntimeInstallResult,
                    ):
                        installation_id = result.installation_id
                        break
            if installation_id is None or plan.alias is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.START_INSTALLATION_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            start_request_id = str(uuid.uuid5(uuid.UUID(request_key), "start"))
            # Adopt the start this phase already queued before re-previewing.
            # Run admission hashes inventory observation time and current
            # reservations, so a refreshed inventory re-derives a different
            # plan digest for the identical child and the idempotency check
            # would have rejected our own durable request key.
            adopted = self._lifecycle.adopt_start(
                installation_id, plan.alias, request_id=start_request_id
            )
            if adopted is not None:
                return PhaseExecution(adopted.id, {"run_id": adopted.owner_id})
            self._require_post_stop_inventory(plan)
            self._lifecycle.reconcile_superseded_unissued(
                "recipe.uninstall", installation_id, ordinal
            )
            self._observe_older_issued("recipe.uninstall", installation_id, ordinal)
            profile_application_id = _string_or_none(progress.profile_application_id)
            low_level = self._lifecycle.preview_run(
                installation_id,
                plan.alias,
                profile_application_id=profile_application_id,
            )
            value = self._lifecycle.start(
                low_level,
                plan_digest=low_level.plan_digest,
                actor=actor,
                request_id=start_request_id,
                workload_intent_ordinal=ordinal,
                profile_application_id=profile_application_id,
            )
            return PhaseExecution(value.id, {"run_id": value.owner_id})
        if phase.kind == "uninstall":
            ordinal = _bound_workload_intent(progress)
            installation_id = plan.installation_id
            if installation_id is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.UNINSTALL_TARGET_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            if (
                plan.cleanup_mode == "reconcile"
                and plan.cleanup_disposition != "abandon"
            ):
                authority = plan.reconciliation_authority
                if authority is None:
                    raise RunSwitchRetryLater(
                        RunSwitchCode.RECONCILIATION_AUTHORITY_UNAVAILABLE,
                        reason=WaitReason.OBSERVATION_UNAVAILABLE,
                    )
                reconcile_request_id = str(
                    uuid.uuid5(uuid.UUID(request_key), "reconcile")
                )
                adopted = self._lifecycle.adopt_owned_operation(
                    reconcile_request_id,
                    kind="recipe.reconcile",
                    owner_kind="installation",
                    owner_id=installation_id,
                )
                if adopted is not None:
                    return PhaseExecution(
                        adopted.id, {"installation_id": installation_id}
                    )
                self._lifecycle.reconcile_superseded_unissued(
                    "recipe.reconcile", installation_id, ordinal
                )
                self._observe_older_issued("recipe.reconcile", installation_id, ordinal)
                try:
                    value = self._lifecycle.reconcile_installation(
                        installation_id,
                        expected_authority=authority.model_dump(mode="json"),
                        run_switch_plan_digest=plan.plan_digest,
                        actor=actor,
                        request_id=reconcile_request_id,
                        workload_intent_ordinal=ordinal,
                    )
                except RunAdmissionBusy:
                    # A competing capacity writer is recoverable.  Preserve
                    # the exact phase checkpoint so the outer service parks
                    # this operation and retries after the writer releases
                    # its rows instead of turning it into a terminal
                    # reconciliation conflict.
                    raise
                except InstallAdmissionBusy:
                    raise
                except (
                    KeyError,
                    RecipeOperationConflict,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise RunSwitchRetryLater(
                        f"{RunSwitchCode.RECONCILIATION_START_FAILED}: {error}"
                    ) from error
                return PhaseExecution(value.id, {"installation_id": installation_id})
            if plan.cleanup_disposition == "abandon":
                # The installation's own assessment proved the plan never
                # reached a node, so no agent order is queued.  The lifecycle
                # re-checks that disposition under the row lock and records the
                # disposal on the cleanup operation's receipt.
                try:
                    abandoned = self._lifecycle.abandon_never_installed(installation_id)
                except (
                    KeyError,
                    RecipeOperationConflict,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise RunSwitchRetryLater(
                        f"{RunSwitchCode.UNINSTALL_ABANDON_FAILED}: {error}"
                    ) from error
                return PhaseExecution(
                    result={
                        **abandoned,
                        "reason": InstallDegradedReason.INSTALLATION_NOT_INSTALLED,
                    }
                )
            uninstall_request_id = str(uuid.uuid5(uuid.UUID(request_key), "uninstall"))
            # Reconnect to the removal this operation already queued before
            # asking for a fresh assessment, so a restart never creates a
            # second removal for the same installation.
            adopted = self._lifecycle.adopt_owned_operation(
                uninstall_request_id,
                kind="recipe.uninstall",
                owner_kind="installation",
                owner_id=installation_id,
            )
            if adopted is not None:
                return PhaseExecution(adopted.id, {"installation_id": installation_id})
            self._lifecycle.reconcile_superseded_unissued(
                "recipe.uninstall", installation_id, ordinal
            )
            self._observe_older_issued("recipe.uninstall", installation_id, ordinal)
            try:
                uninstall_plan = self._lifecycle.preview_uninstall(installation_id)
                value = self._lifecycle.uninstall(
                    installation_id,
                    plan_digest=uninstall_plan.plan_digest,
                    actor=actor,
                    request_id=uninstall_request_id,
                    workload_intent_ordinal=ordinal,
                )
            except RunAdmissionBusy:
                # Capacity contention is a retryable admission outcome.  Do
                # not wrap it as uninstall-start-failed; the parent service
                # will release its transaction and schedule the same exact
                # uninstall attempt again.
                raise
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise RunSwitchRetryLater(
                    f"{RunSwitchCode.UNINSTALL_START_FAILED}: {error}"
                ) from error
            return PhaseExecution(value.id, {"installation_id": installation_id})
        if phase.kind == "final_verify":
            if plan.action == "cleanup":
                return self._verify_cleanup(plan, request_key=request_key)
            if plan.action == "install":
                return self._verify_installation(plan, progress)
            run_id = plan.run_id
            phase_results = progress.phase_results
            if run_id is None and isinstance(phase_results, list):
                for result in reversed(phase_results):
                    if isinstance(result, RunSwitchStartResult | RunSwitchStopResult):
                        run_id = result.run_id
                        break
            if run_id is None or self._lifecycle is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.FINAL_VERIFICATION_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            status = self._lifecycle.run_status(run_id)
            if plan.action == "stop":
                scope = plan.profile_stop_scope
                if scope is not None:
                    reachable_stopped = (
                        status.state == RunState.LOST
                        and status.route_state == RouteState.WITHDRAWN
                        and all(
                            rank.state == RunState.STOPPED
                            for rank in status.ranks
                            if rank.node_id in scope.target_node_ids
                        )
                    )
                    if reachable_stopped:
                        missing_ranks = [
                            rank
                            for rank in status.ranks
                            if rank.node_id in scope.missing_node_ids
                        ]
                        raise _RunSwitchIncompleteProfileGroupConflict(
                            f"{RunSwitchCode.PROFILE_INCOMPLETE_MULTI_SPARK_MODEL}: "
                            f"{plan.alias or run_id} was removed from service after "
                            "stopping reachable ranks; missing Spark ranks may still "
                            "be running: "
                            + ", ".join(
                                f"rank {rank.rank} ({rank.node_id})"
                                for rank in missing_ranks
                            )
                        )
                verified = (
                    status.state == RunState.STOPPED
                    and status.route_state == RouteState.WITHDRAWN
                    and all(rank.state == RunState.STOPPED for rank in status.ranks)
                )
                waiting = (
                    status.state in STOPPABLE_RUN_STATES
                    or status.route_state
                    in {
                        RouteState.PENDING,
                    }
                )
                status_reason = (
                    f"{RunSwitchCode.STOP_VERIFICATION_PENDING}: run {run_id} is "
                    f"{status.state}, route is {status.route_state}"
                )
            else:
                verified = status.healthy and status.route_state == RouteState.PUBLISHED
                waiting = False
                status_reason = None
                route_error = (
                    redact_text(status.route_error)
                    if status.route_error is not None
                    else None
                )
                if not verified:
                    if status.state in {
                        RunState.FAILED,
                        RunState.LOST,
                        RunState.STOPPED,
                    }:
                        detail = route_error or "run owner reached a terminal state"
                        raise RunSwitchRetryLater(
                            f"{RunSwitchCode.RUN_OWNER_TERMINAL}: {status.state}; {detail}"
                        )
                    if status.route_state == RouteState.FAILED:
                        detail = route_error or "route owner reported terminal failure"
                        raise RunSwitchRetryLater(
                            f"{RunSwitchCode.ROUTE_OWNER_FAILED}: {detail}"
                        )
                    waiting = True
                    route_cause = f"route is {status.route_state}"
                    if status.route_state == RouteState.PENDING:
                        if status.route_next_attempt_at is not None:
                            route_cause = (
                                "route publication is pending; route owner next "
                                f"attempt {status.route_next_attempt_at.isoformat()}"
                            )
                        elif status.observation_deadline_at is not None:
                            route_cause = (
                                "route publication is pending; route owner has no "
                                "retry scheduled; initial observation deadline "
                                f"{status.observation_deadline_at.isoformat()}"
                            )
                        else:
                            route_cause = (
                                "route publication is pending; route owner next "
                                "attempt is not scheduled"
                            )
                    elif status.route_state == RouteState.WITHDRAWN:
                        route_cause = (
                            f"route is withdrawn; cause {route_error or 'unknown'}"
                        )
                    if status.recovery_owners:
                        owners = ", ".join(
                            f"{owner.kind} {owner.operation_id} ({owner.state})"
                            for owner in status.recovery_owners
                        )
                        status_reason = (
                            f"{RunSwitchCode.DISTRIBUTED_RECOVERY_ACTIVE}: {owners}; run "
                            f"{run_id} generation {status.run_generation}; {route_cause}"
                        )
                    elif status.route_recovery_pending:
                        status_reason = (
                            f"{RunSwitchCode.ROUTE_HEALTH_RECOVERY_ACTIVE}: route owner is "
                            f"reconciling run {run_id} generation {status.run_generation}; "
                            f"{route_cause}"
                        )
                    elif status.state in {RunState.STARTING, RunState.STOPPING}:
                        status_reason = (
                            f"{RunSwitchCode.RUN_OWNER_ACTIVE}: run {run_id} generation "
                            f"{status.run_generation} is {status.state}; {route_cause}"
                        )
                    elif status.route_state == RouteState.PENDING:
                        status_reason = (
                            f"{RunSwitchCode.ROUTE_PUBLICATION_PENDING}: run {run_id} "
                            f"generation {status.run_generation}; {route_cause}"
                        )
                    elif status.route_state == RouteState.WITHDRAWN:
                        cause = route_error or "no terminal route-owner cause recorded"
                        status_reason = (
                            f"{RunSwitchCode.ROUTE_WITHDRAWN_OWNER_UNKNOWN}: run {run_id} "
                            f"generation {status.run_generation} remains {status.state}; "
                            f"route cause {cause}; waiting for exact reconciliation"
                        )
                    else:
                        status_reason = (
                            f"{RunSwitchCode.FINAL_OWNER_STATE_UNKNOWN}: run {run_id} "
                            f"generation {status.run_generation} is {status.state}; "
                            f"route is {status.route_state}; waiting for exact reconciliation"
                        )
            evidence = {
                "run_id": run_id,
                "state": status.state,
                "route_state": status.route_state,
                "healthy": status.healthy,
                "ranks": [
                    {
                        "node_id": rank.node_id,
                        "rank": rank.rank,
                        "role": rank.role,
                        "state": rank.state,
                        "fresh": rank.fresh,
                    }
                    for rank in status.ranks
                ],
            }
            if verified:
                return PhaseExecution(result={"final_verified": True, **evidence})
            if waiting:
                return PhaseExecution(
                    result={"final_verified": False, **evidence},
                    waiting=True,
                    status_reason=status_reason,
                )
            raise RunSwitchRetryLater(RunSwitchCode.FINAL_VERIFICATION_FAILED)
        return PhaseExecution()

    @staticmethod
    def _prepared_installation_result(
        installation_id: str,
        mapping_id: str,
        install_plan_digest: str,
        compiled: Mapping[str, CompiledExecutionPlan],
    ) -> PhaseExecution:
        first_compiled = next(iter(compiled.values()), None)
        identity = first_compiled.identity if first_compiled is not None else None
        return PhaseExecution(
            result={
                "installation_id": installation_id,
                "mapping_id": mapping_id,
                "install_plan_digest": install_plan_digest,
                "model_artifact_set_sha256": (
                    identity.model_artifact_set_sha256 if identity is not None else None
                ),
                "model_artifact_set_bytes": (
                    identity.model_artifact_bytes if identity is not None else None
                ),
                "compiled_plan_persisted": True,
            }
        )

    @staticmethod
    def _bound_installation(
        session: Session,
        plan: RunSwitchPlan,
        installation_id: str,
        mapping_id: str,
        install_plan_digest: str | None,
    ) -> tuple[RecipeInstallation, tuple[InstallationNode, ...]]:
        installation = session.get(RecipeInstallation, installation_id)
        if (
            installation is None
            or plan.mapping is None
            or installation.recipe_revision_id != plan.recipe_revision_id
            or installation.model_content_sha256 != plan.model_content_sha256
            or installation.mapping_id != mapping_id
            or installation.mapping_generation != plan.mapping.mapping_generation
            or installation.image_digest != plan.image_digest
            or (
                install_plan_digest is not None
                and installation.plan_digest != install_plan_digest
            )
        ):
            raise RunSwitchRetryLater(
                RunSwitchCode.INSTALLATION_IDENTITY_CHANGED,
                reason=WaitReason.SCOPE_CHANGED,
            )
        members = tuple(
            session.scalars(
                select(InstallationNode)
                .where(InstallationNode.installation_id == installation_id)
                .order_by(InstallationNode.rank, InstallationNode.node_id)
            )
        )
        expected = {
            (node.node_id, node.rank, node.role) for node in plan.spark_group.nodes
        }
        if {(node.node_id, node.rank, node.role) for node in members} != expected:
            raise RunSwitchRetryLater(
                RunSwitchCode.INSTALLATION_MEMBERSHIP_CHANGED,
                reason=WaitReason.SCOPE_CHANGED,
            )
        return installation, members

    def _verify_installation(
        self, plan: RunSwitchPlan, progress: RunSwitchOperationResult
    ) -> PhaseExecution:
        """Verify the bound installation, not merely its completed child job."""

        installation_id = plan.installation_id
        mapping_id = plan.mapping.mapping_id if plan.mapping is not None else None
        install_plan_digest = None
        phase_results = progress.phase_results
        if phase_results:
            for result in phase_results:
                if isinstance(result, RunSwitchRuntimePlanResult):
                    installation_id = result.installation_id
                    mapping_id = result.mapping_id
                    install_plan_digest = result.install_plan_digest
        if installation_id is None or mapping_id is None or plan.mapping is None:
            raise RunSwitchRetryLater(
                RunSwitchCode.INSTALLATION_VERIFICATION_UNAVAILABLE,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        with self._sessions() as session:
            installation, members = self._bound_installation(
                session, plan, installation_id, mapping_id, install_plan_digest
            )
            runs = tuple(
                session.scalars(
                    select(RecipeRun).where(
                        RecipeRun.installation_id == installation_id
                    )
                )
            )
            active_runs = sum(run.state in STOPPABLE_RUN_STATES for run in runs)
            unwithdrawn_routes = sum(
                run.route_state != RouteState.WITHDRAWN for run in runs
            )
            verified = (
                installation.state == InstallationState.INSTALLED
                and all(
                    node.state == InstallationNodeState.INSTALLED for node in members
                )
                and not active_runs
                and not unwithdrawn_routes
            )
            evidence = {
                "final_verified": verified,
                "installation_id": installation_id,
                "installation_state": installation.state,
                "active_runs": active_runs,
                "unwithdrawn_routes": unwithdrawn_routes,
                "ranks": [
                    {
                        "node_id": node.node_id,
                        "rank": node.rank,
                        "role": node.role,
                        "state": node.state,
                    }
                    for node in members
                ],
            }
            if verified:
                return PhaseExecution(result=evidence)
            if installation.state in {
                InstallationState.PLANNED,
                InstallationState.INSTALLING,
            }:
                return PhaseExecution(result=evidence, waiting=True)
        raise RunSwitchRetryLater(RunSwitchCode.INSTALLATION_VERIFICATION_FAILED)

    def _verify_cleanup(
        self, plan: RunSwitchPlan, *, request_key: str
    ) -> PhaseExecution:
        """Observe whether the scoped removal has actually taken effect.

        The installation row and its active runs are the authority, so the
        result is derived from durable state rather than from the removal
        child's own report.
        """

        installation_id = plan.installation_id
        if installation_id is None:
            raise RunSwitchRetryLater(
                RunSwitchCode.UNINSTALL_TARGET_UNAVAILABLE,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        reconciliation_complete = True
        reconcile_request_id: str | None = (
            str(uuid.uuid5(uuid.UUID(request_key), "reconcile"))
            if plan.cleanup_mode == "reconcile"
            else None
        )
        if plan.cleanup_mode == "reconcile" and plan.cleanup_disposition != "abandon":
            if self._lifecycle is None or plan.reconciliation_authority is None:
                raise RunSwitchRetryLater(
                    RunSwitchCode.RECONCILIATION_AUTHORITY_UNAVAILABLE,
                    reason=WaitReason.OBSERVATION_UNAVAILABLE,
                )
            assert reconcile_request_id is not None
            reconciliation_complete = self._lifecycle.reconciliation_complete(
                reconcile_request_id,
                expected_authority=plan.reconciliation_authority.model_dump(
                    mode="json"
                ),
            )
        with self._sessions() as session:
            installation = session.get(RecipeInstallation, installation_id)
            members = tuple(
                session.scalars(
                    select(InstallationNode)
                    .where(InstallationNode.installation_id == installation_id)
                    .order_by(InstallationNode.rank, InstallationNode.node_id)
                )
            )
            runs = tuple(
                session.scalars(
                    select(RecipeRun).where(
                        RecipeRun.installation_id == installation_id
                    )
                )
            )
            active_runs = sum(
                run.state != RunState.STOPPED or run.route_state != RouteState.WITHDRAWN
                for run in runs
            )
        if plan.cleanup_mode == "reconcile":
            expected_members = {
                (node.node_id, node.rank, node.role) for node in plan.spark_group.nodes
            }
            exact_members = {
                (node.node_id, node.rank, node.role) for node in members
            } == expected_members
            removed = (
                installation is not None
                and installation.state == InstallationState.UNINSTALLED
                and exact_members
                and all(
                    node.state == InstallationNodeState.UNINSTALLED for node in members
                )
                and reconciliation_complete
            )
            if not reconciliation_complete:
                raise RunSwitchRetryLater(
                    RunSwitchCode.RECONCILIATION_VERIFICATION_FAILED
                )
            if (
                installation is None
                or installation.state != InstallationState.UNINSTALLED
                or not exact_members
                or any(
                    node.state != InstallationNodeState.UNINSTALLED for node in members
                )
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.RECONCILIATION_STATE_VERIFICATION_FAILED
                )
        else:
            removed = (
                installation is None
                or installation.state == InstallationState.UNINSTALLED
            )
        evidence = {
            "installation_id": installation_id,
            "installation_state": (
                installation.state if installation is not None else None
            ),
            "removed": removed,
            "active_runs": active_runs,
            "cleanup_mode": plan.cleanup_mode,
            **(
                {"reconciliation_request_id": reconcile_request_id}
                if plan.cleanup_mode == "reconcile"
                else {}
            ),
        }
        if removed and not active_runs:
            return PhaseExecution(result={"final_verified": True, **evidence})
        return PhaseExecution(
            result={"final_verified": False, **evidence}, waiting=True
        )

    def abandon(
        self, session: Session, operation_id: str, now: datetime, *, reason: str
    ) -> bool:
        """Close a parked idempotent artifact child; lifecycle children never are."""

        abandon = getattr(self._artifact_executor, "abandon", None)
        return bool(
            callable(abandon) and abandon(session, operation_id, now, reason=reason)
        )

    def get(self, operation_id: str) -> Any:
        """Resolve an artifact child first, then an existing recipe child."""

        if self._artifact_executor is not None:
            getter = getattr(self._artifact_executor, "get", None)
            if not callable(getter):
                getter = None
            try:
                child = getter(operation_id) if getter is not None else None
            except KeyError:
                child = None
            if child is not None:
                return child
        if self._lifecycle is not None:
            return self._lifecycle.get(operation_id)
        raise MissingRecord(operation_id)


#: Decisions and writes of every operation go through the lifecycle core; the
#: reads (a child's orders, a stop) use an adapter bound to the caller's session.
_ADAPTER = RunSwitchAdapter()


class RunSwitchOperationService:
    """Preview and advance one durable, digest-bound Run/Switch outcome."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        lifecycle: RecipeOperationService | None = None,
        clock: Any,
        mappings: ClusterMappingService | None = None,
        artifacts: RunSwitchArtifactInspector | None = None,
        artifact_phase_executor: RunSwitchArtifactPhaseExecutor | None = None,
        phase_executor: RunSwitchPhaseExecutor | None = None,
        model_cache: ModelCacheService | None = None,
        build_archive_available: Callable[[str, int], bool] | None = None,
        inventory_max_age_seconds: int = 300,
        memory_floor_bytes: int = PLATFORM_MEMORY_FLOOR_BYTES,
    ) -> None:
        if not 1 <= inventory_max_age_seconds <= 86_400:
            raise InvalidValue("run/switch inventory age is invalid")
        if memory_floor_bytes < 0:
            raise InvalidValue("run/switch memory floor is invalid")
        self._sessions = sessions
        self._lifecycle = lifecycle
        self._clock = clock
        self._mappings = mappings or ClusterMappingService(sessions)
        self._artifacts = artifacts or DatabaseRunSwitchArtifactInspector(model_cache)
        self._artifact_phase_executor = artifact_phase_executor
        self._build_archive_available = build_archive_available
        self._custom_phase_executor = phase_executor is not None
        self._phase_executor = phase_executor or (
            RecipeLifecyclePhaseExecutor(
                lifecycle,
                sessions,
                self._mappings,
                clock,
                artifact_executor=artifact_phase_executor,
                inventory_max_age_seconds=inventory_max_age_seconds,
            )
            if lifecycle is not None
            else None
        )
        self._inventory_max_age = inventory_max_age_seconds
        self._memory_floor = memory_floor_bytes
        self._tick_cursor: str | None = None

    def preview(
        self,
        request: RunSwitchPreviewRequest,
        *,
        actor: str,
        profile_application_id: str | None = None,
    ) -> RunSwitchPlan:
        return self._preview_run(
            request,
            actor=actor,
            profile_application_id=profile_application_id,
            excluded_profile_application_ids=(profile_application_id,)
            if profile_application_id is not None
            else (),
        )

    def preview_run(
        self,
        request: RunSwitchPreviewRequest,
        *,
        actor: str,
    ) -> RunSwitchPlan:
        return self._preview_run(request, actor=actor)

    def inspect_request(
        self,
        request: RunSwitchPreviewRequest,
        *,
        actor: str,
        defer_source_build: bool = False,
        expected_runtime_image: RuntimeImageIdentity | None = None,
        excluded_profile_application_ids: tuple[str, ...] = (),
    ) -> RunSwitchPlan:
        """Read admission without authoring preparation work.

        An existing authorized cache-recovery operation may defer a missing
        source build until its worker runs. Other blockers remain unchanged.
        This flag grants no execution authority and never creates a build.
        """
        return self._preview_run(
            request,
            actor=actor,
            create_build=False,
            defer_source_build=defer_source_build,
            reviewed_runtime_image=expected_runtime_image,
            excluded_profile_application_ids=excluded_profile_application_ids,
        )

    def inspect_candidate(
        self, recipe_revision_id: str, node_ids: tuple[str, ...], *, actor: str
    ) -> RunSwitchPlan:
        """Inspect one library placement without creating a planned build."""
        with self._sessions() as session:
            revision = _active_recipe_revision(session, recipe_revision_id)
            if revision is None:
                raise MissingRecord(recipe_revision_id)
            placements = candidate_placements(
                recipe_topology(revision.document), node_ids
            )
            model_digest = _primary_model_digest(revision.document)
            if model_digest is None:
                raise RunSwitchRuntimeSpecInvalid(
                    "recipe has no exact primary model",
                    reason=InvalidRequestReason.NOT_FOUND,
                )
            request = RunSwitchPreviewRequest(
                recipe_revision_id=recipe_revision_id,
                model_content_sha256=model_digest,
                spark_group=SparkGroup(
                    nodes=[
                        SparkGroupNode(
                            node_id=node.node_id,
                            rank=node.rank,
                            role=node.role,
                            endpoint_owner=node.endpoint_owner,
                        )
                        for node in placements
                    ]
                ),
                alias=revision.slug,
            )
        return self.inspect_request(request, actor=actor)

    def preview_stop(
        self,
        request: RunSwitchStopPreviewRequest | str,
        *,
        actor: str,
        profile_stop_scope: RunSwitchProfileStopScope | None = None,
    ) -> RunSwitchPlan:
        run_id = request if isinstance(request, str) else request.run_id
        invocation = (
            InvocationMetadata() if isinstance(request, str) else request.invocation
        )
        now = _now(self._clock)
        with self._sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise MissingRecord(run_id)
            installation = session.get(RecipeInstallation, run.installation_id)
            # Stopping a live run never waits on its stored plan: a plan that
            # cannot be read is rebuilt from the installation (the evidence of
            # which recipe and model the run serves), else it is retired.
            run_plan = read_or_rebuild(
                kind="run-switch.run-plan",
                subject=run.id,
                read=lambda: parse_stored_run_plan(run.plan),
            )
            recipe_revision_id = (
                installation.recipe_revision_id
                if isinstance(run_plan, Residue) and installation is not None
                else run_plan.recipe_revision_id
                if not isinstance(run_plan, Residue)
                else None
            )
            revision = _active_recipe_revision(session, recipe_revision_id)
            mapping = session.get(ClusterMapping, run.mapping_id)
            mapping_nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == run.mapping_id)
                    .order_by(ClusterMappingNode.rank)
                )
            )
            original_group = SparkGroup(
                nodes=[
                    SparkGroupNode(
                        node_id=node.node_id,
                        rank=node.rank,
                        role=node.role,
                        endpoint_owner=node.endpoint_owner,
                    )
                    for node in mapping_nodes
                ]
            )
            target_node_ids = tuple(node.node_id for node in original_group.nodes)
            if profile_stop_scope is not None:
                if original_group != profile_stop_scope.original_group:
                    raise RunSwitchRequestInvalid(
                        RunSwitchCode.PROFILE_STOP_SCOPE_CHANGED,
                        reason=InvalidRequestReason.CONFLICT,
                    )
                target_node_ids = tuple(profile_stop_scope.target_node_ids)
            model_digest = (
                installation.model_content_sha256 if installation is not None else None
            )
            recipe_digest = revision.content_digest if revision is not None else None
            (
                _model_document,
                _model_documents,
                _document_blockers,
            ) = self._resolve_documents(
                session,
                revision,
                model_digest,
                requested_recipe_digest=recipe_digest,
            )
            (
                freshness,
                fit_current,
                _fit_blockers,
                fit_warnings,
                _memory_shortfalls,
            ) = self._fit(
                session,
                revision,
                original_group,
                now=now,
                excluded_run_ids=(run.id,),
            )
            inspection = self._inspect_artifacts(
                session,
                model_digest,
                revision.id if revision is not None else None,
                original_group,
                retention="retain-cached",
                now=now,
            )
            build = (
                session.get(RecipeBuild, installation.recipe_build_id)
                if installation is not None and installation.recipe_build_id is not None
                else None
            )
            build_candidate = build or (
                self._latest_build(session, revision.id)
                if revision is not None
                else None
            )
            build_evidence, runtime_storage, _build_blockers, _build_warnings = (
                self._build_evidence(
                    session,
                    revision,
                    build,
                    build_candidate,
                    original_group,
                    require_available=False,
                )
            )
            stop_digest = self._stop_digest(
                run.id,
                target_node_ids=(
                    profile_stop_scope.target_node_ids
                    if profile_stop_scope is not None
                    else None
                ),
            )
            if profile_stop_scope is not None and stop_digest is not None:
                lifecycle = self._lifecycle
                # An unbound lifecycle owner cannot cross-check the stop set:
                # that is unknown, and the stop (idempotent, scoped by the
                # profile's own reviewed group) goes ahead on the digest.
                lifecycle_stop = (
                    lifecycle.preview_stop(
                        run.id,
                        profile_target_node_ids=profile_stop_scope.target_node_ids,
                    )
                    if lifecycle is not None
                    else None
                )
                if lifecycle_stop is not None and (
                    lifecycle_stop.target_node_ids
                    != tuple(profile_stop_scope.target_node_ids)
                    or lifecycle_stop.missing_node_ids
                    != tuple(profile_stop_scope.missing_node_ids)
                    or {
                        (node.node_id, node.rank, node.role)
                        for node in lifecycle_stop.nodes
                    }
                    != {
                        (node.node_id, node.rank, node.role)
                        for node in profile_stop_scope.original_group.nodes
                    }
                    or not lifecycle_stop.allowed
                ):
                    stop_digest = None
            stops = (
                [
                    StopImpact(
                        run_id=run.id,
                        run_plan_digest=run.plan_digest,
                        alias=run.alias,
                        state=run.state,
                        node_ids=list(target_node_ids),
                        reserved_bytes=self._run_reserved_bytes(
                            session,
                            run.id,
                            node_ids=target_node_ids,
                        ),
                        plan_digest=stop_digest,
                    )
                ]
                if stop_digest is not None and run.state in STOPPABLE_RUN_STATES
                else []
            )
            # Stopping a live run must remain possible when catalog/cache
            # evidence has aged or is unavailable. Capacity and artifact
            # findings remain diagnostics, but they do not block the stop.
            blockers: list[RunSwitchReason] = []
            warnings = [*fit_warnings, *inspection.warnings]
            if profile_stop_scope is not None:
                missing_ranks = [
                    node
                    for node in profile_stop_scope.original_group.nodes
                    if node.node_id in profile_stop_scope.missing_node_ids
                ]
                warnings.append(
                    _as_reason(
                        RunSwitchCode.PROFILE_INCOMPLETE_MULTI_SPARK_MODEL,
                        "This profile Stop will withdraw the model route and stop "
                        "only reachable ranks. Missing Spark ranks may still be running: "
                        + ", ".join(
                            f"rank {node.rank} ({node.node_id})"
                            for node in missing_ranks
                        ),
                        scope="group",
                        node_ids=profile_stop_scope.missing_node_ids,
                    )
                )
            if run.state not in STOPPABLE_RUN_STATES:
                blockers.append(
                    _as_reason(
                        RunSwitchCode.RUN_NOT_ACTIVE,
                        "The selected run is no longer active and cannot be stopped.",
                        scope="operation",
                        node_ids=[node.node_id for node in mapping_nodes],
                    )
                )
            elif stop_digest is None:
                blockers.append(
                    _as_reason(
                        RunSwitchCode.STOP_PLAN_UNAVAILABLE,
                        "The existing run cannot be represented by a safe stop plan.",
                        scope="operation",
                        node_ids=[node.node_id for node in mapping_nodes],
                    )
                )
            phases = self._phases(
                action="stop",
                group=original_group,
                phase_node_ids=target_node_ids,
                installation_id=installation.id if installation is not None else None,
                installation_state=installation.state
                if installation is not None
                else None,
                stops=stops,
                inspection=inspection,
                partial_stop=profile_stop_scope is not None,
                runtime_storage=runtime_storage,
                retention="retain-cached",
                blockers=blockers,
                stop_before_transfer=False,
                stop_before_prepare=False,
            )
            storage = self._storage(inspection, retention="retain-cached")
            preparation = (
                None
                if profile_stop_scope is not None
                else self._preparation(
                    revision=revision,
                    group=original_group,
                    inspection=inspection,
                    build=build,
                    build_candidate=build_candidate,
                    runtime_storage=runtime_storage,
                    now=now,
                    reasons=[*blockers, *warnings],
                )
            )
            plan_data: dict[str, object] = {
                "schema_version": 2,
                "generated_at": now,
                "action": "stop",
                "model_content_sha256": model_digest,
                "recipe_revision_id": revision.id if revision is not None else None,
                "recipe_content_sha256": recipe_digest,
                "alias": run.alias,
                "run_id": run.id,
                "spark_group": original_group,
                "profile_stop_scope": profile_stop_scope,
                "mapping": self._mapping_selection(mapping, mapping_nodes),
                "installation_id": installation.id
                if installation is not None
                else None,
                "installation_state": installation.state
                if installation is not None
                else None,
                "recipe_build_id": (
                    installation.recipe_build_id if installation is not None else None
                ),
                "image_digest": (
                    installation.image_digest if installation is not None else None
                ),
                "start_plan_digest": None,
                "freshness": freshness,
                "fit_current": fit_current,
                "fit_after_stop": None,
                "fit": fit_current,
                "storage": storage,
                "runtime_storage": runtime_storage,
                "build": build_evidence,
                "preparation": preparation,
                "conflicts": [],
                "stops": stops,
                "reclaimed_bytes": 0,
                "phases": phases,
                "allowed": not blockers,
                "blockers": blockers,
                "warnings": warnings,
                "invocation": invocation,
                "plan_digest": "0" * 64,
                "stop_before_prepare": False,
            }
            return self._finalize_plan(plan_data)

    def preview_profile_stop(
        self,
        run_id: str,
        profile_stop_scope: RunSwitchProfileStopScope,
        *,
        actor: str,
    ) -> RunSwitchPlan:
        """Preview a partial Stop authorized only by a FleetProfile review."""

        return self.preview_stop(
            run_id,
            actor=actor,
            profile_stop_scope=profile_stop_scope,
        )

    def preview_cleanup(
        self,
        request: RunSwitchCleanupPreviewRequest | str,
        *,
        actor: str,
    ) -> RunSwitchPlan:
        """Plan one scoped removal without consulting launch readiness.

        The installation's own uninstall assessment is the only authority for
        whether the removal may proceed.  Capacity, freshness, catalog and
        cache findings stay diagnostics, because being unable to start work must
        never prevent removing work.
        """

        installation_id = (
            request if isinstance(request, str) else request.installation_id
        )
        cleanup_mode: Literal["uninstall", "reconcile"] = (
            "uninstall" if isinstance(request, str) else request.cleanup_mode
        )
        invocation = InvocationMetadata()
        now = _now(self._clock)
        with self._sessions() as session:
            installation = session.get(RecipeInstallation, installation_id)
            if installation is None:
                raise MissingRecord(installation_id)
            revision = (
                session.get(CatalogDocumentRevision, installation.recipe_revision_id)
                if cleanup_mode == "reconcile"
                else _active_recipe_revision(session, installation.recipe_revision_id)
            )
            if revision is not None and revision.kind != "recipe":
                revision = None
            mapping = session.get(ClusterMapping, installation.mapping_id)
            mapping_nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == installation.mapping_id)
                    .order_by(ClusterMappingNode.rank)
                )
            )
            group = SparkGroup(
                nodes=[
                    SparkGroupNode(
                        node_id=node.node_id,
                        rank=node.rank,
                        role=node.role,
                        endpoint_owner=node.endpoint_owner,
                    )
                    for node in mapping_nodes
                ]
            )
            model_digest = installation.model_content_sha256
            recipe_digest = revision.content_digest if revision is not None else None
            (
                _model_document,
                _model_documents,
                document_warnings,
            ) = self._resolve_documents(
                session,
                revision,
                model_digest,
                requested_recipe_digest=recipe_digest,
            )
            (
                freshness,
                fit_current,
                _fit_blockers,
                fit_warnings,
                _memory_shortfalls,
            ) = self._fit(
                session,
                revision,
                group,
                now=now,
                # The installation's own runs are being removed, so they are not
                # capacity this plan has to fit alongside.
                excluded_run_ids=tuple(
                    session.scalars(
                        select(RecipeRun.id).where(
                            RecipeRun.installation_id == installation_id
                        )
                    )
                ),
            )
            inspection = self._inspect_artifacts(
                session,
                model_digest,
                revision.id if revision is not None else None,
                group,
                retention="retain-cached",
                now=now,
            )
            build = (
                session.get(RecipeBuild, installation.recipe_build_id)
                if installation.recipe_build_id is not None
                else None
            )
            build_candidate = build or (
                self._latest_build(session, revision.id)
                if revision is not None
                else None
            )
            build_evidence, runtime_storage, _build_blockers, _build_warnings = (
                self._build_evidence(
                    session,
                    revision,
                    build,
                    build_candidate,
                    group,
                    require_available=False,
                )
            )
            node_ids = [node.node_id for node in group.nodes]
            blockers: list[RunSwitchReason] = []
            warnings = [*document_warnings, *fit_warnings, *inspection.warnings]
            cleanup_disposition: Literal["uninstall", "abandon"] = "uninstall"
            reconciliation_authority: RunSwitchReconciliationAuthority | None = None
            if self._lifecycle is None:
                blockers.append(
                    _as_reason(
                        (
                            RunSwitchCode.RECONCILIATION_ASSESSMENT_UNAVAILABLE
                            if cleanup_mode == "reconcile"
                            else RunSwitchCode.UNINSTALL_ASSESSMENT_UNAVAILABLE
                        ),
                        "Cleanup cannot be assessed without the lifecycle service.",
                        scope="operation",
                        node_ids=node_ids,
                    )
                )
            elif cleanup_mode == "reconcile" and self._never_installed(installation_id):
                # Reconciling a plan that never reached a node discards the
                # record: there is no effect on a Spark to reconcile.
                cleanup_disposition = "abandon"
            elif cleanup_mode == "reconcile":
                try:
                    authority = self._lifecycle.preview_reconciliation_authority(
                        installation_id,
                        session=session,
                        allow_active_reconciliation=True,
                    )
                    reconciliation_authority = (
                        RunSwitchReconciliationAuthority.model_validate(
                            authority.document()
                        )
                    )
                except RecipeReconciliationBlocked as error:
                    blockers.append(
                        _as_reason(
                            run_switch_code(error.code),
                            error.detail,
                            scope="operation",
                            node_ids=node_ids,
                        )
                    )
                except (
                    KeyError,
                    RecipeOperationConflict,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    blockers.append(
                        _as_reason(
                            RunSwitchCode.RECONCILIATION_ASSESSMENT_UNAVAILABLE,
                            f"The installation cannot be represented by an exact reconciliation authority: {error}",
                            scope="operation",
                            node_ids=node_ids,
                        )
                    )
                else:
                    if (
                        self._lifecycle.assess_superseded_unissued(
                            "recipe.reconcile", installation_id
                        )
                        or self._lifecycle.assess_superseded_issued(
                            "recipe.reconcile", installation_id
                        )
                        is not None
                    ):
                        warnings.append(
                            _as_reason(
                                RunSwitchCode.RECONCILIATION_PREREQUISITE,
                                "The prior exact reconciliation attempt will be retired or observed before new cleanup is queued.",
                                scope="operation",
                                node_ids=node_ids,
                                severity="warning",
                            )
                        )
                    completed_nodes = [
                        target.node_id
                        for target in reconciliation_authority.targets
                        if target.state == "reconciled"
                    ]
                    if completed_nodes:
                        warnings.append(
                            _as_reason(
                                RunSwitchCode.RECONCILIATION_RECEIPTS_RETAINED,
                                "Previously verified node cleanup receipts will be reused.",
                                scope="node",
                                node_ids=completed_nodes,
                                severity="warning",
                            )
                        )
            else:
                try:
                    assessment = self._lifecycle.preview_uninstall(installation_id)
                except (
                    KeyError,
                    RecipeOperationConflict,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ):
                    assessment = None
                if assessment is None:
                    blockers.append(
                        _as_reason(
                            RunSwitchCode.UNINSTALL_ASSESSMENT_UNAVAILABLE,
                            "The installation cannot be represented by a safe uninstall plan.",
                            scope="operation",
                            node_ids=node_ids,
                        )
                    )
                elif not assessment.allowed:
                    issued = self._lifecycle.assess_superseded_issued(
                        "recipe.uninstall", installation_id
                    )
                    for blocker in assessment.blockers:
                        reason = _as_reason(
                            RunSwitchCode.UNINSTALL_ISSUED_PREREQUISITE
                            if issued is not None
                            and blocker.code == UninstallPlanCode.OPERATION_ACTIVE
                            else RunSwitchCode.UNINSTALL_BLOCKED,
                            (
                                "The prior issued uninstall will be cancelled and "
                                "observed before this cleanup starts."
                                if issued is not None
                                and blocker.code == UninstallPlanCode.OPERATION_ACTIVE
                                else f"{blocker.code}: {blocker.detail}"
                            ),
                            scope="operation",
                            node_ids=node_ids,
                            severity=(
                                "warning"
                                if issued is not None
                                and blocker.code == UninstallPlanCode.OPERATION_ACTIVE
                                else "blocker"
                            ),
                        )
                        if reason.severity == "warning":
                            warnings.append(reason)
                        else:
                            blockers.append(reason)
                else:
                    # The installation's own assessment decides whether this is
                    # a removal or an abandonment; the phase executor reads the
                    # same disposition rather than re-deriving it.
                    cleanup_disposition = assessment.disposition
                    for warning in assessment.warnings:
                        warnings.append(
                            _as_reason(
                                run_switch_code(warning.code),
                                warning.detail,
                                scope="operation",
                                node_ids=node_ids,
                                severity="warning",
                            )
                        )
            phases = self._phases(
                action="cleanup",
                group=group,
                installation_id=installation.id,
                installation_state=installation.state,
                stops=[],
                inspection=inspection,
                runtime_storage=runtime_storage,
                retention="retain-cached",
                blockers=blockers,
                stop_before_transfer=False,
                stop_before_prepare=False,
                cleanup_disposition=cleanup_disposition,
                cleanup_mode=cleanup_mode,
            )
            storage = self._storage(inspection, retention="retain-cached")
            preparation = self._preparation(
                revision=revision,
                group=group,
                inspection=inspection,
                build=build,
                build_candidate=build_candidate,
                runtime_storage=runtime_storage,
                now=now,
                reasons=[*blockers, *warnings],
            )
            plan_data: dict[str, object] = {
                "schema_version": 2,
                "generated_at": now,
                "action": "cleanup",
                "model_content_sha256": model_digest,
                "recipe_revision_id": revision.id if revision is not None else None,
                "recipe_content_sha256": recipe_digest,
                "alias": None,
                "run_id": None,
                "spark_group": group,
                "mapping": self._mapping_selection(mapping, mapping_nodes),
                "installation_id": installation.id,
                "installation_state": installation.state,
                "cleanup_disposition": cleanup_disposition,
                "cleanup_mode": cleanup_mode,
                "reconciliation_authority": reconciliation_authority,
                "recipe_build_id": installation.recipe_build_id,
                "image_digest": installation.image_digest,
                "start_plan_digest": None,
                "freshness": freshness,
                "fit_current": fit_current,
                "fit_after_stop": None,
                "fit": fit_current,
                "storage": storage,
                "runtime_storage": runtime_storage,
                "build": build_evidence,
                "preparation": preparation,
                "conflicts": [],
                "stops": [],
                "reclaimed_bytes": 0,
                "phases": phases,
                "allowed": not blockers,
                "blockers": blockers,
                "warnings": warnings,
                "invocation": invocation,
                "plan_digest": "0" * 64,
                "stop_before_prepare": False,
            }
            return self._finalize_plan(plan_data)

    def apply_cleanup(
        self,
        request: RunSwitchCleanupApplyRequest,
        *,
        actor: str,
        workload_intent_ordinal: int | None = None,
    ) -> RunSwitchOperation:
        request_key = request.request_key or str(uuid.uuid4())
        intent = {
            "type": "cleanup",
            **request.model_dump(
                mode="json", exclude={"request_key"}, exclude_none=True
            ),
        }
        if request.request_key is not None:
            existing = self._existing_request_operation(
                request.request_key,
                kind="recipe.cleanup.v2",
                intent=intent,
            )
            if existing is not None:
                return existing
        preview = self.preview_cleanup(request, actor=actor)
        _require_reviewed_plan(request.plan_digest, preview)
        if not preview.allowed and not _plan_blockers_are_waitable(preview):
            raise RunSwitchRequestInvalid(
                f"{RunSwitchCode.PLAN_BLOCKED}: "
                + "; ".join(reason.code for reason in preview.blockers[:8])
            )
        return self._apply_plan(
            preview,
            request_key=request_key,
            actor=actor,
            kind="recipe.cleanup.v2",
            workload_intent_ordinal=workload_intent_ordinal,
            intent=intent,
        )

    def apply(
        self,
        request: RunSwitchApplyRequest,
        *,
        actor: str,
        workload_intent_ordinal: int | None = None,
        profile_application_id: str | None = None,
    ) -> RunSwitchOperation:
        request_key = request.request_key or str(uuid.uuid4())
        intent = {
            "type": "run",
            "request": request.model_dump(
                mode="json", exclude={"request_key"}, exclude_none=True
            ),
        }
        if request.request_key is not None:
            existing = self._existing_request_operation(
                request.request_key,
                kind="recipe.run-switch.v2",
                intent=intent,
            )
            if existing is not None:
                return existing
        plan = self.preview(
            request, actor=actor, profile_application_id=profile_application_id
        )
        _require_reviewed_plan(request.plan_digest, plan)
        if not plan.allowed and not _plan_blockers_are_waitable(plan):
            raise RunSwitchRequestInvalid(
                f"{RunSwitchCode.PLAN_BLOCKED}: "
                + "; ".join(reason.code for reason in plan.blockers[:8])
            )
        return self._apply_plan(
            plan,
            request_key=request_key,
            actor=actor,
            kind="recipe.run-switch.v2",
            workload_intent_ordinal=workload_intent_ordinal,
            profile_application_id=profile_application_id,
            intent=intent,
        )

    def apply_run(
        self,
        request: RunSwitchApplyRequest,
        *,
        actor: str,
    ) -> RunSwitchOperation:
        return self.apply(request, actor=actor)

    def apply_stop(
        self,
        request: RunSwitchStopApplyRequest,
        *,
        actor: str,
        workload_intent_ordinal: int | None = None,
        profile_application_id: str | None = None,
    ) -> RunSwitchOperation:
        request_key = request.request_key or str(uuid.uuid4())
        intent = {
            "type": "stop",
            **request.model_dump(
                mode="json", exclude={"request_key"}, exclude_none=True
            ),
        }
        if request.request_key is not None:
            existing = self._existing_request_operation(
                request.request_key,
                kind="recipe.stop.v2",
                intent=intent,
            )
            if existing is not None:
                return existing
        preview = self.preview_stop(request, actor=actor)
        _require_reviewed_plan(request.plan_digest, preview)
        if not preview.allowed and not _plan_blockers_are_waitable(preview):
            raise RunSwitchRequestInvalid(
                f"{RunSwitchCode.PLAN_BLOCKED}: "
                + "; ".join(reason.code for reason in preview.blockers[:8])
            )
        return self._apply_plan(
            preview,
            request_key=request_key,
            actor=actor,
            kind="recipe.stop.v2",
            workload_intent_ordinal=workload_intent_ordinal,
            profile_application_id=profile_application_id,
            intent=intent,
        )

    def apply_profile_stop(
        self,
        run_id: str,
        profile_stop_scope: RunSwitchProfileStopScope,
        *,
        plan_digest: str,
        request_key: str,
        actor: str,
        workload_intent_ordinal: int,
        profile_application_id: str,
    ) -> RunSwitchOperation:
        """Apply only a FleetProfile-reviewed reachable-rank Stop scope."""

        intent = {
            "type": "profile-stop",
            "run_id": run_id,
            "profile_stop_scope": profile_stop_scope.model_dump(mode="json"),
        }
        existing = self._existing_request_operation(
            request_key,
            kind="recipe.stop.v2",
            intent=intent,
        )
        if existing is not None:
            return existing
        plan = self.preview_stop(
            run_id,
            actor=actor,
            profile_stop_scope=profile_stop_scope,
        )
        if not plan.allowed and not _plan_blockers_are_waitable(plan):
            raise RunSwitchRequestInvalid(
                f"{RunSwitchCode.PLAN_BLOCKED}: "
                + "; ".join(reason.code for reason in plan.blockers[:8])
            )
        return self._apply_plan(
            plan,
            request_key=request_key,
            actor=actor,
            kind="recipe.stop.v2",
            workload_intent_ordinal=workload_intent_ordinal,
            profile_application_id=profile_application_id,
            intent=intent,
        )

    def get(self, operation_id: str) -> RunSwitchOperation:
        with self._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.kind not in _OPERATION_KINDS:
                raise MissingRecord(operation_id)
            return self._operation_view(job)

    def cancel(
        self, operation_id: str, *, actor: str, request_key: str, reason: str
    ) -> RunSwitchOperation:
        """Stop at the next safe phase boundary, keeping shared immutable work."""
        stop_run_id: str | None = None
        profile_application_id: str | None = None
        cancellation = RunSwitchCancellation(
            request_key=request_key,
            actor=actor,
            reason=" ".join(reason.split()),
            requested_at=_now(self._clock),
        )
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or job.kind not in _OPERATION_KINDS:
                raise MissingRecord(operation_id)
            progress = _read_progress(job.result)
            previous = progress.cancellation
            if previous:
                if (previous.request_key, previous.actor, previous.reason) != (
                    cancellation.request_key,
                    cancellation.actor,
                    cancellation.reason,
                ):
                    raise RunSwitchRequestInvalid(
                        "run-switch cancellation request was already used differently",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                return self._operation_view(job)
            if job.state not in _LIVE_STATES:
                raise RunSwitchRequestInvalid("run-switch operation is not cancellable")
            # A cancel always completes: a plan that cannot be read is unknown,
            # so the cancel treats the operation as possibly started and Stops
            # through the child's run, without the plan's build-dependency lock.
            plan = _stored_job_plan(job)
            profile_application_id = _string_or_none(progress.profile_application_id)
            phase = None
            if plan is not None:
                try:
                    lock_run_switch_build_dependency(
                        session,
                        plan,
                        phase_index=require_integer(
                            progress.phase_index, "phase index"
                        ),
                        allow_cancelling=True,
                    )
                except BuildConsumerError as error:
                    raise RunSwitchRequestInvalid(f"{error.code}: {error}") from error
                phase = plan.phases[
                    min(
                        require_integer(progress.phase_index, "phase index"),
                        len(plan.phases) - 1,
                    )
                ]
            if "start" in progress.completed_phases or (
                job.state
                in job_states.words(LifecycleState.RUNNING, LifecycleState.OBSERVING)
                and (phase is None or phase.kind in {"start", "final_verify"})
            ):
                child_id = _string_or_none(progress.child_operation_id)
                child = session.get(Job, child_id) if child_id is not None else None
                child_parent = (
                    read_row_column(child, "payload") if child is not None else None
                )
                owner_id = (
                    child_parent.owner_id
                    if isinstance(child_parent, RecipeStartParent | RecipeStopParent)
                    and child_parent.owner_kind == "run"
                    else None
                )
                stop_run_id = (
                    plan.run_id if plan is not None else None
                ) or _string_or_none(owner_id)
                # The operation is marked cancelled only after its Stop is
                # durably accepted below; a failed Stop leaves it cancellable.
                # An intent with no run identity to Stop is a bookkeeping gap,
                # not a refusal: the cancel is recorded and driven like any
                # other, so it observes the child and completes (rule 4).
            if stop_run_id is None:
                progress.cancellation = cancellation
                progress.retry_attempt = None
                if job.state in job_states.words(
                    LifecycleState.OBSERVING, LifecycleState.NEEDS_OPERATOR
                ):
                    progress.observation_due_at = cancellation.requested_at
                job.status_reason = (
                    "Cancellation requested; finishing the current preparation safely."
                )
                if phase is not None and phase.subphase == "container-build":
                    _complete_cancellation(job, progress, cancellation.requested_at)
                else:
                    # Nothing issued ends at once; an issued child is stopped and
                    # observed up to the core's budget, then the cancel ends with
                    # its effect recorded unknown.
                    self._cancel_with_session(
                        session, job, progress, cancellation.requested_at
                    )
                    job.result = _persisted_result(progress)
                    job.updated_at = cancellation.requested_at
        if stop_run_id is not None:
            stop = self.apply_stop(
                RunSwitchStopApplyRequest(
                    run_id=stop_run_id, request_key=cancellation.request_key
                ),
                actor=actor,
                profile_application_id=profile_application_id,
            )
            with self._sessions.begin() as session:
                job = session.get(Job, operation_id, with_for_update=True)
                if job is not None and job.state in _LIVE_STATES:
                    progress = _read_progress(job.result)
                    progress.cancellation = cancellation
                    _ADAPTER.cancelled(
                        job,
                        progress,
                        cancellation.requested_at,
                        reason=(
                            "Cancellation translated into Run/Switch Stop "
                            f"operation {stop.operation_id}"
                        ),
                    )
                    job.result = _persisted_result(progress)
                    job.updated_at = cancellation.requested_at
            return stop
        return self.get(operation_id)

    def retry(
        self,
        operation_id: str,
        *,
        actor: str,
        request_key: str,
    ) -> RunSwitchOperation:
        """Queue one bounded retry from the persisted Run/Switch plan."""

        try:
            uuid.UUID(request_key)
        except (TypeError, ValueError, AttributeError) as error:
            raise RunSwitchRequestInvalid(
                "run-switch retry request key is invalid"
            ) from error
        with self._sessions.begin() as session:
            previous = session.get(Job, operation_id, with_for_update=True)
            if previous is None or previous.kind not in _OPERATION_KINDS:
                raise RunSwitchRequestInvalid("run-switch operation is not retryable")
            existing = session.scalar(select(Job).where(Job.request_id == request_key))
            if existing is not None:
                if existing.kind != previous.kind:
                    raise RunSwitchRequestInvalid(
                        "run-switch request key was already used",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                return self._operation_view(existing)
            current_progress = _parse_persisted_result(previous.result)
            progress = (
                current_progress.model_copy(deep=True)
                if current_progress is not None
                else RunSwitchOperationResult()
            )
            if (
                current_progress is None
                or previous.state != "failed"
                or progress.retryable is not True
            ):
                raise RunSwitchRequestInvalid("run-switch operation is not retryable")
            nodes = list(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(previous.targets))
                    .order_by(AgentNode.node_id)
                    .with_for_update()
                )
            )
            previous_parent = _run_switch_payload(previous)
            if (
                previous_parent is None
                or len(nodes) != len(previous.targets)
                or any(
                    node.workload_intent_ordinal
                    != previous_parent.workload_intent_ordinal
                    for node in nodes
                )
            ):
                raise RunSwitchRequestInvalid(
                    f"{RunSwitchCode.SUPERSEDED}: retry belongs to an obsolete workload intent",
                    reason=InvalidRequestReason.SUPERSEDED,
                )
            retry_plan = _stored_job_plan(previous)
            if retry_plan is None:
                # Nothing to retry from: the request-led way forward is a new
                # request, exactly as for an operation without a readable result.
                raise RunSwitchRequestInvalid("run-switch operation is not retryable")
            prior_image_intent = current_progress.runtime_image_reference_intent
            if prior_image_intent is not None:
                try:
                    validated_intent = _run_switch_runtime_image_intent(
                        previous, retry_plan
                    )
                except ArtifactLifecycleError as error:
                    raise RunSwitchRequestInvalid(
                        "run-switch retry runtime image reference is invalid"
                    ) from error
                if validated_intent != prior_image_intent:
                    raise RunSwitchRequestInvalid(
                        "run-switch retry runtime image reference is invalid"
                    )
            now = _now(self._clock)
            _reserve_run_switch_assets(session, retry_plan, now=now)
            if prior_image_intent is not None:
                try:
                    require_reference_open(
                        session,
                        (
                            ArtifactIdentity(
                                "runtime-image", prior_image_intent.archive_sha256
                            ),
                        ),
                        now=now,
                    )
                except ArtifactLifecycleError as error:
                    raise RunSwitchRequestInvalid(
                        f"{error.code}: {error.detail}"
                    ) from error
            try:
                lock_run_switch_build_dependency(
                    session,
                    retry_plan,
                    phase_index=current_progress.phase_index,
                )
            except BuildConsumerError as error:
                raise RunSwitchRequestInvalid(f"{error.code}: {error}") from error
            ordinal = max(node.workload_intent_ordinal for node in nodes) + 1
            for node in nodes:
                node.workload_intent_ordinal = ordinal
            self.request_superseded_workload_cancellation_in_session(
                session, tuple(previous.targets), ordinal, now
            )
            retry_job_id = str(uuid.uuid4())
            if prior_image_intent is not None:
                rebound_intent = read_stored_model(
                    RunSwitchRuntimeImageReferenceIntent,
                    {
                        **prior_image_intent.model_dump(mode="json"),
                        "operation_id": retry_job_id,
                        "request_key": request_key,
                        "actor": actor,
                        "workload_intent_ordinal": ordinal,
                    },
                    strict=True,
                )
                progress.runtime_image_reference_intent = rebound_intent
            progress.child_operation_id = None
            progress.retryable = False
            progress.failure_code = None
            progress.workload_intent_ordinal = ordinal
            payload = serialize_json_value(
                previous_parent.model_copy(
                    update={
                        "workload_intent_ordinal": ordinal,
                        "progress": progress,
                        "retry_of": previous.id,
                    }
                )
            )
            job = _ADAPTER.new_operation(
                allowed=True,
                id=retry_job_id,
                request_id=request_key,
                kind=previous.kind,
                actor=actor,
                authority_revision=previous.authority_revision,
                targets=list(previous.targets),
                payload_digest=_digest(payload),
                payload=payload,
                result=_persisted_result(progress),
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            return self._operation_view(job)

    def activity_provider(self) -> RunSwitchOperationProvider:
        """Return the Run/Switch family adapter for global Activity.

        ``operation_api`` owns the shared provider dataclass.  Keeping the
        adapter's data projection here lets the global registry bind it by
        duck type while that API evolves (and keeps Activity from reading the
        high-level job payload directly).
        """

        return RunSwitchOperationProvider(self)

    def bind_model_cache(self, model_cache: ModelCacheService) -> None:
        """Bind the authoritative NAS cache after production composition."""

        binder = getattr(self._artifacts, "bind_model_cache", None)
        if not callable(binder):
            # An inspector without a model-cache binding reports its artifact
            # evidence as unavailable, which admission already reconciles; the
            # composition continues instead of failing the Controller start.
            retire_as_unknown(
                "run-switch.model-cache-binding",
                type(self._artifacts).__name__,
                BookkeepingReason.EVIDENCE_UNAVAILABLE,
                "artifact inspector does not support model-cache binding",
            )
            return
        binder(model_cache)

    def tick(self) -> bool:
        """Give every due independent operation a bounded chance to advance."""

        # Canonical JSON emits UTC as Z; the clock's isoformat uses +00:00.
        # Compare the same spelling so an exactly due operation is eligible.
        due_at = func.replace(
            Job.result["observation_due_at"].as_string(), "Z", "+00:00"
        )
        with self._sessions() as session:
            active = (
                select(Job.id)
                .where(
                    Job.kind.in_(_OPERATION_KINDS),
                    or_(
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.OBSERVING,
                            )
                        ),
                        # Legacy: a wait with a clock is observed, one without is
                        # healed (re-evaluated) by the first advance, never left.
                        Job.state.in_(job_states.words(LifecycleState.NEEDS_OPERATOR)),
                    ),
                    or_(
                        due_at.is_(None),
                        due_at <= _now(self._clock).isoformat(),
                        # A cancel in flight is looked at on every tick: it ends as
                        # soon as its child does (its stop attempts are spaced by
                        # the core, not by this clock).
                        Job.result["cancellation"].as_string().is_not(None),
                    ),
                )
                .order_by(Job.id)
                .limit(16)
            )
            if self._tick_cursor is None:
                job_ids = tuple(session.scalars(active))
            else:
                following = tuple(
                    session.scalars(active.where(Job.id > self._tick_cursor))
                )
                job_ids = following + tuple(
                    session.scalars(
                        active.where(Job.id <= self._tick_cursor).limit(
                            16 - len(following)
                        )
                    )
                )
        if job_ids:
            self._tick_cursor = str(job_ids[-1])
        advanced = False
        for job_id in job_ids:
            try:
                advanced = self._advance(str(job_id)) or advanced
                self._record_wait(str(job_id))
            except (
                UnknownOutcomeError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                KeyError,
            ) as error:
                # One persisted operation must never deny unrelated operations
                # their turn.  A malformed contract is rejected and retained by
                # ``_advance`` itself; this contains an unexpected per-job
                # failure, reports it, and lets the rest of the batch advance.
                log_event(
                    _LOGGER,
                    "run_switch.job_advance_failed",
                    service="control-worker",
                    job_id=str(job_id),
                    error=type(error).__name__,
                    message=redact_text(error),
                    traceback=redact_text(traceback.format_exc()),
                )
                self._hold_after_advance_failure(str(job_id), error)
                continue
        return advanced

    def _hold_after_advance_failure(self, operation_id: str, error: Exception) -> None:
        """Show an unexpected advance failure on the operation and back off.

        The operation keeps its checkpoint and is tried again, but it is never
        a silent endless retry: its wait names the failure and the next try.
        """

        now = _now(self._clock)
        try:
            with self._sessions.begin() as session:
                job = session.get(Job, operation_id, with_for_update=True)
                if job is None or job.state not in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.OBSERVING,
                ):
                    return
                progress = _read_progress(job.result)
                code = error_code(error) or RunSwitchCode.ADVANCE_FAILED
                attempt = (
                    require_integer(progress.retry_attempt, "retry attempt")
                    if progress.retry_reason == code
                    and progress.retry_attempt is not None
                    else 1
                )
                _ADAPTER.retry(
                    job,
                    progress,
                    code,
                    now,
                    reset_on_change=True,
                    describe=lambda due: (
                        f"{code}: {type(error).__name__}: {redact_text(error)}"[:400]
                        + f"; retry {attempt} at {due.isoformat()}"
                    ),
                )
                job.result = _persisted_result(progress)
                job.updated_at = now
            self._record_wait(operation_id)
        except (OSError, RuntimeError, TypeError, ValueError, KeyError):
            return  # the log above still names the failure

    def _record_wait(self, operation_id: str) -> None:
        """Store what a waiting or retrying operation waits for; log changes.

        A retry is a wait, so the reason is kept next to the progress and shown
        wherever the operation is shown. The list is a current snapshot: it is
        replaced at each check and empty once the operation runs on or settles.
        """

        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or job.kind not in _OPERATION_KINDS:
                return
            if _progress_damaged(job.result):
                return
            progress = _read_progress(job.result)
            wanted = bound_blockers(_wait_blockers(job, progress))
            stored = wanted
            if stored == (progress.blockers or []):
                return
            before = {(item.code, tuple(item.node_ids)) for item in progress.blockers}
            if stored:
                progress.blockers = stored
            else:
                progress.blockers = []
            job.result = _persisted_result(progress)
            after = {(item.code, tuple(item.node_ids)) for item in wanted}
            if wanted and before != after:
                _LOGGER.info(
                    "run/switch %s is waiting: %s",
                    operation_id,
                    "; ".join(f"{item.code}: {item.detail}" for item in wanted[:4]),
                )

    def _preview_run(
        self,
        request: RunSwitchPreviewRequest,
        *,
        actor: str,
        create_build: bool = True,
        reviewed_runtime_image: RuntimeImageIdentity | None = None,
        defer_source_build: bool = False,
        excluded_profile_application_ids: tuple[str, ...] = (),
        profile_application_id: str | None = None,
    ) -> RunSwitchPlan:
        now = _now(self._clock)
        group = request.spark_group
        node_ids = tuple(node.node_id for node in group.nodes)
        with self._sessions() as session:
            revision = _active_recipe_revision(session, request.recipe_revision_id)
            if revision is None:
                raise MissingRecord(request.recipe_revision_id)
            expected_image = (
                accepted_profile_runtime_image(
                    session, profile_application_id, revision.id, node_ids
                )
                if profile_application_id is not None
                else reviewed_runtime_image
            )
            blockers: list[RunSwitchReason] = []
            warnings: list[RunSwitchReason] = []
            if revision.state != "active" or revision.content_digest is None:
                blockers.append(
                    _as_reason(
                        RunSwitchCode.RECIPE_UNRESOLVED,
                        "The selected recipe revision is not an immutable resolved revision.",
                        scope="recipe",
                    )
                )
            selections = revision.document.get("models")
            model_selection = (
                selections[0]
                if isinstance(selections, Sequence)
                and not isinstance(selections, (str, bytes))
                and selections
                else None
            )
            model_ref = (
                model_selection.get("model")
                if isinstance(model_selection, Mapping)
                else None
            )
            recipe_model_digest = (
                model_ref.get("content_sha256")
                if isinstance(model_ref, Mapping)
                else None
            )
            if recipe_model_digest != request.model_content_sha256:
                blockers.append(
                    _as_reason(
                        RunSwitchCode.MODEL_RECIPE_MISMATCH,
                        "The selected model variant is not the model pinned by this recipe revision.",
                        scope="model",
                    )
                )
            (
                _model_document,
                model_documents,
                document_blockers,
            ) = self._resolve_documents(
                session,
                revision,
                request.model_content_sha256,
                requested_recipe_digest=revision.content_digest,
            )
            blockers.extend(document_blockers)
            settings_resolution = resolve_effective_settings(revision.document)
            effective_settings = settings_resolution.settings
            effective_settings_view = None
            if effective_settings is None:
                blockers.extend(
                    _resource_reason(reason, node_ids=node_ids)
                    for reason in settings_resolution.reasons
                )
            else:
                effective_settings_view = _settings_view(effective_settings)
            mapping, mapping_selection, mapping_blockers = self._resolve_mapping(
                session,
                revision,
                group,
                actor=actor,
                option_choices=request.option_choices,
            )
            blockers.extend(mapping_blockers)
            placement_blockers = tuple(blockers)
            conflicts, stops, conflict_blockers = self._conflicts(
                session,
                node_ids,
                action=request.action,
            )
            blockers.extend(conflict_blockers)
            inspection = self._inspect_artifacts(
                session,
                request.model_content_sha256,
                revision.id,
                group,
                retention=request.retention,
                now=now,
            )
            blockers.extend(inspection.blockers)
            warnings.extend(inspection.warnings)
            if (
                mapping_selection is not None
                and mapping_selection.action == "create"
                and self._phase_executor is None
            ):
                blockers.append(
                    _as_reason(
                        RunSwitchCode.MAPPING_MATERIALIZATION_UNAVAILABLE,
                        "No phase executor is configured to materialize a new exact Spark mapping.",
                        scope="mapping",
                        node_ids=node_ids,
                    )
                )
            installation = self._matching_installation(
                session,
                revision.id,
                request.model_content_sha256,
                mapping,
                group,
            )
            build = self._matching_build(
                session, revision.id, expected_image=expected_image
            )
            build_candidate = build or (
                session.get(RecipeBuild, expected_image.build_id)
                if expected_image is not None and expected_image.build_id is not None
                else self._latest_build(session, revision.id)
            )
            build_selection = self._select_build(
                session,
                revision,
                build,
                build_candidate,
                group,
                now=now,
                create_build=create_build,
            )
            build = build_selection.build
            build_candidate = build_selection.candidate
            restoring_installation = (
                expected_image is not None
                and installation is not None
                and installation.state == InstallationState.INSTALLED
                and build is None
                and build_candidate is not None
                and build_candidate.state in {"planned", "building"}
                and installation.recipe_build_id
                == build_candidate.id
                == expected_image.build_id
                and installation.image_digest == expected_image.image_digest
            )
            if restoring_installation:
                assert installation is not None and expected_image is not None
                installed_plan = parse_stored_installation_plan(installation.plan)
                for compiled in installed_plan.compiled_execution_plans.values():
                    _require_profile_runtime_image(
                        expected_image,
                        {
                            name: getattr(compiled.runtime_image, name)
                            for name in (
                                "image_digest",
                                "oci_layout_sha256",
                                "image_bytes",
                            )
                        },
                    )
            if (
                installation is not None
                and (
                    build is None
                    or not installation_matches_runtime_image(
                        installation,
                        image_digest=build.image_digest,
                        oci_layout_sha256=build.oci_layout_sha256,
                        image_bytes=build.image_bytes,
                    )
                )
                and not restoring_installation
            ):
                # An installed source build is reusable only while its exact
                # Controller build remains selected, or its recreation is
                # bound to the accepted image in every installed rank. Other
                # replacements need a fresh compiled plan and install receipt.
                installation = None
            blockers.extend(build_selection.blockers)
            build_evidence, runtime_storage, build_blockers, build_warnings = (
                self._build_evidence(
                    session,
                    revision,
                    build,
                    build_candidate,
                    group,
                    defer_source_build=defer_source_build,
                    expected_image=expected_image,
                )
            )
            blockers.extend(build_blockers)
            warnings.extend(build_warnings)
            resource_fits = self._resource_fits(
                session,
                revision,
                request,
                now=now,
                stops=stops,
                placement_blockers=placement_blockers,
                effective_settings=effective_settings,
                model_documents=model_documents,
                image_bytes=(
                    runtime_storage.image_bytes
                    if runtime_storage.image_bytes is not None
                    else expected_image.image_bytes
                    if expected_image is not None
                    else None
                ),
                artifact_bytes=inspection.artifact_set_bytes,
                excluded_profile_application_ids=excluded_profile_application_ids,
            )
            freshness = resource_fits.freshness
            fit_current = resource_fits.current
            fit_after_stop = resource_fits.after_stop
            blockers.extend(resource_fits.blockers)
            warnings.extend(resource_fits.warnings)
            if build_selection.builder_freshness is not None:
                freshness.append(build_selection.builder_freshness)
            start_plan_digest: str | None = None
            recipe_build_id = (
                build.id
                if build is not None
                else build_candidate.id
                if build_candidate is not None
                and (
                    build_candidate.state in {"planned", "building"}
                    or expected_image is not None
                )
                else None
            )
            image_digest = (
                build.image_digest
                if build is not None
                else expected_image.image_digest
                if expected_image is not None
                else runtime_storage.image_digest
            )
            if expected_image is not None and profile_application_id is not None:
                if recipe_build_id != expected_image.build_id:
                    raise RunSwitchRequestInvalid(
                        f"{ProfileReasonCode.RUNTIME_IMAGE_CHANGED}: selected build differs from the accepted image; review and load the profile again",
                        reason=InvalidRequestReason.CONFLICT,
                    )
                if runtime_storage.image_digest is not None:
                    _require_profile_runtime_image(
                        expected_image,
                        {
                            "image_digest": runtime_storage.image_digest,
                            "oci_layout_sha256": runtime_storage.oci_layout_sha256,
                            "image_bytes": runtime_storage.image_bytes,
                            "build_id": recipe_build_id,
                            "architecture": "linux-arm64",
                            "runtime_interface": RUNTIME_INTERFACE,
                        },
                    )
            if installation is None:
                if self._phase_executor is None:
                    blockers.append(
                        _as_reason(
                            RunSwitchCode.INSTALLATION_PREPARATION_UNAVAILABLE,
                            "No phase executor is configured to prepare this exact recipe on the selected group.",
                            scope="operation",
                            node_ids=node_ids,
                        )
                    )
            elif (
                request.action != "install"
                and installation.state == InstallationState.INSTALLED
                and self._lifecycle is not None
            ):
                # The runs this plan stops release their reservations in the
                # Stop phase, so admission must not count that capacity against
                # the replacement's own preview.  Counting it refused the plan
                # for the workload it was replacing, and since the only release
                # path is a successful stop, nothing could break the tie.
                planned_stop_ids = frozenset(stop.run_id for stop in stops)
                try:
                    low_level_plan = self._lifecycle.preview_run(
                        installation.id,
                        request.alias,
                        excluded_profile_application_ids=excluded_profile_application_ids,
                        released_run_ids=planned_stop_ids,
                    )
                except (
                    KeyError,
                    RecipeOperationConflict,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    blockers.append(
                        _as_reason(
                            RunSwitchCode.RUN_ADMISSION_UNAVAILABLE,
                            f"The exact run admission plan could not be produced: {error}",
                            scope="operation",
                            node_ids=node_ids,
                        )
                    )
                else:
                    start_plan_digest = low_level_plan.plan_digest
                    remaining_admission_blockers = False
                    for item in low_level_plan.nodes:
                        for reason in item.blockers:
                            if reason.code in PORT_ADMISSION_CODES:
                                # The shared fit checks these for fresh and existing
                                # installs and excludes only exact reviewed stops.
                                continue
                            if (
                                reason.code == RunAdmissionCode.INSUFFICIENT_MEMORY
                                and resource_fits.post_stop_memory_check is not None
                            ):
                                # The reviewed exact stops require a fresh
                                # post-stop fit; this pre-stop result cannot
                                # establish the later physical capacity.
                                continue
                            remaining_admission_blockers = True
                            blockers.append(
                                _as_reason(
                                    reason.code,
                                    reason.detail,
                                    scope="node",
                                    node_ids=(item.node_id,),
                                )
                            )
                        for reason in item.warnings:
                            warnings.append(
                                _as_reason(
                                    reason.code,
                                    reason.detail,
                                    scope="node",
                                    severity="warning",
                                    node_ids=(item.node_id,),
                                )
                            )
                    if remaining_admission_blockers:
                        blockers.append(
                            _as_reason(
                                RunSwitchCode.RUN_ADMISSION_BLOCKED,
                                "The existing run admission primitive rejected one or more selected ranks.",
                                scope="operation",
                                node_ids=node_ids,
                            )
                        )
            stop_before_prepare = resource_fits.stop_before_prepare
            stop_before_transfer = resource_fits.stop_before_transfer
            phases = self._phases(
                action=request.action,
                group=group,
                installation_id=installation.id if installation is not None else None,
                installation_state=installation.state
                if installation is not None
                else None,
                stops=stops,
                inspection=inspection,
                runtime_storage=runtime_storage,
                retention=request.retention,
                blockers=blockers,
                stop_before_transfer=stop_before_transfer,
                stop_before_prepare=stop_before_prepare,
                build_required=(
                    build is None
                    and build_candidate is not None
                    and build_candidate.state in {"planned", "building"}
                ),
                build_on_target=(
                    build is None
                    and build_candidate is not None
                    and build_candidate.state in {"planned", "building"}
                    and build_candidate.builder_node_id in node_ids
                    # A prebuilt image is pulled by the Controller; the
                    # nominal builder Spark does no work and needs no memory.
                    and policy_prebuilt_reference(build_candidate.policy_report) is None
                ),
            )
            if (
                not self._custom_phase_executor
                and self._artifact_phase_executor is None
                and any(
                    phase.kind in {"transfer", "verify", "cleanup"}
                    or (phase.kind == "prepare" and phase.subphase == "runtime-image")
                    for phase in phases
                )
            ):
                blockers.append(
                    _as_reason(
                        RunSwitchCode.ARTIFACT_PHASE_EXECUTOR_UNAVAILABLE,
                        "Artifact transfer, verification, Spark-local cleanup, or Controller image preparation requires an injected cache boundary.",
                        scope="artifact",
                        node_ids=node_ids,
                    )
                )
                phases = self._phases(
                    action=request.action,
                    group=group,
                    installation_id=installation.id
                    if installation is not None
                    else None,
                    installation_state=installation.state
                    if installation is not None
                    else None,
                    stops=stops,
                    inspection=inspection,
                    runtime_storage=runtime_storage,
                    retention=request.retention,
                    blockers=blockers,
                    stop_before_transfer=stop_before_transfer,
                    stop_before_prepare=stop_before_prepare,
                    build_required=(
                        build is None
                        and build_candidate is not None
                        and build_candidate.state in {"planned", "building"}
                    ),
                    build_on_target=(
                        build is None
                        and build_candidate is not None
                        and build_candidate.state in {"planned", "building"}
                        and build_candidate.builder_node_id in node_ids
                        and policy_prebuilt_reference(build_candidate.policy_report)
                        is None
                    ),
                )
            preparation = self._preparation(
                revision=revision,
                group=group,
                inspection=inspection,
                build=build,
                build_candidate=build_candidate,
                runtime_storage=runtime_storage,
                now=now,
                reasons=[*blockers, *warnings],
            )
            storage = self._storage(inspection, retention=request.retention)
            plan_data: dict[str, object] = {
                "schema_version": 2,
                "generated_at": now,
                "action": request.action,
                "model_content_sha256": request.model_content_sha256,
                "recipe_revision_id": revision.id,
                "recipe_content_sha256": revision.content_digest,
                "alias": request.alias,
                "run_id": None,
                "spark_group": group,
                "mapping": mapping_selection,
                "installation_id": installation.id
                if installation is not None
                else None,
                "installation_state": installation.state
                if installation is not None
                else None,
                "recipe_build_id": recipe_build_id,
                "image_digest": image_digest,
                "start_plan_digest": start_plan_digest,
                "freshness": freshness,
                "fit_current": fit_current,
                "fit_after_stop": fit_after_stop,
                "post_stop_memory_check": resource_fits.post_stop_memory_check,
                "fit": fit_current,
                "effective_settings": effective_settings_view,
                "storage": storage,
                "runtime_storage": runtime_storage,
                "build": build_evidence,
                "preparation": preparation,
                "conflicts": conflicts,
                "stops": stops,
                "reclaimed_bytes": (
                    inspection.reclaimable_bytes + runtime_storage.reclaimable_bytes
                    if request.retention == "reclaim-unreferenced"
                    else 0
                ),
                "phases": phases,
                "allowed": not blockers,
                "blockers": blockers,
                "warnings": warnings,
                "invocation": request.invocation,
                "plan_digest": "0" * 64,
                "stop_before_prepare": stop_before_prepare,
                "stop_before_transfer": stop_before_transfer,
            }
            return self._finalize_plan(plan_data)

    def _resolve_documents(
        self,
        session: Session,
        revision: CatalogDocumentRevision | None,
        model_digest: str | None,
        *,
        requested_recipe_digest: str | None,
    ) -> tuple[
        ModelDefinition | None,
        Mapping[tuple[str, str, str], ModelDefinition],
        list[RunSwitchReason],
    ]:
        blockers: list[RunSwitchReason] = []
        model_document: ModelDefinition | None = None
        model_documents: dict[tuple[str, str, str], ModelDefinition] = {}
        if revision is None:
            return None, {}, blockers
        if (
            requested_recipe_digest is not None
            and revision.content_digest != requested_recipe_digest
        ):
            blockers.append(
                _as_reason(
                    RunSwitchCode.RECIPE_DIGEST_CHANGED,
                    "The selected recipe revision digest changed before planning.",
                    scope="recipe",
                    stale=True,
                )
            )
        try:
            resolved = resolve_recipe_entities(session, revision.document)
            resolved_model_items = resolved.model_revisions
            resolved_model = resolved_model_items[0] if resolved_model_items else None
            resolved_models = resolved.models
            for resolved_item in resolved_model_items:
                candidate_item = resolved_models.get(resolved_item.content_digest)
                if candidate_item is not None:
                    model_documents[
                        (
                            resolved_item.publisher,
                            resolved_item.slug,
                            resolved_item.content_digest,
                        )
                    ] = candidate_item
            model_document = (
                resolved_models.get(resolved_model.content_digest)
                if resolved_model is not None
                else None
            )
            if (
                model_digest is None
                or resolved_model is None
                or resolved_model.content_digest != model_digest
            ):
                blockers.append(
                    _as_reason(
                        RunSwitchCode.MODEL_REVISION_UNAVAILABLE,
                        "The exact model definition selected for this run is not resolved in local catalog authority.",
                        scope="model",
                    )
                )
        except (RecipeRuntimeSpecError, RuntimeError, TypeError, ValueError):
            blockers.append(
                _as_reason(
                    RunSwitchCode.RECIPE_DEPENDENCIES_UNAVAILABLE,
                    "Exact model and runtime dependencies could not be resolved from immutable catalog authority.",
                    scope="recipe",
                )
            )
        return model_document, model_documents, blockers

    def _resolve_mapping(
        self,
        session: Session,
        revision: CatalogDocumentRevision,
        group: SparkGroup,
        *,
        actor: str,
        option_choices: Mapping[str, str],
    ) -> tuple[
        ClusterMapping | None,
        MappingSelection | None,
        list[RunSwitchReason],
    ]:
        try:
            wanted_choices = effective_option_choices(revision.document, option_choices)
        except ClusterMappingError as error:
            return (
                None,
                None,
                [
                    _as_reason(
                        RunSwitchCode.OPTION_INVALID,
                        str(error),
                        scope="mapping",
                    )
                ],
            )
        desired = tuple(
            (node.node_id, node.rank, node.role, node.endpoint_owner)
            for node in group.nodes
        )
        desired_ids = tuple(node.node_id for node in group.nodes)
        mappings = tuple(
            session.scalars(
                select(ClusterMapping)
                .where(
                    ClusterMapping.recipe_revision_id == revision.id,
                    ClusterMapping.state == "ready",
                )
                .order_by(ClusterMapping.generation.desc(), ClusterMapping.id)
            )
        )
        for mapping in mappings:
            nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == mapping.id)
                    .order_by(ClusterMappingNode.rank)
                )
            )
            actual = tuple(
                (node.node_id, node.rank, node.role, node.endpoint_owner)
                for node in nodes
            )
            if actual != desired:
                continue
            # A different choice of recipe options is a different mapping (and
            # so a new installation and run) over the same cached model and image.
            if mapping_option_choices(mapping.parameters) != wanted_choices:
                continue
            return mapping, self._mapping_selection(mapping, nodes), []
        try:
            plan = self._mappings.preview(
                revision.id,
                desired_ids,
                {OPTION_CHOICES_KEY: wanted_choices} if wanted_choices else {},
                actor,
            )
        except (
            ClusterMappingError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as error:
            return (
                None,
                None,
                [
                    _as_reason(
                        RunSwitchCode.MAPPING_INVALID,
                        f"The selected Spark group cannot satisfy the exact recipe topology: {error}",
                        scope="mapping",
                        node_ids=desired_ids,
                    )
                ],
            )
        planned = tuple(
            (node.node_id, node.rank, node.role, node.endpoint_owner)
            for node in plan.nodes
        )
        if planned != desired:
            return (
                None,
                None,
                [
                    _as_reason(
                        RunSwitchCode.MAPPING_GROUP_MISMATCH,
                        "The selected Spark ranks and roles do not form the complete topology required by the recipe.",
                        scope="group",
                        node_ids=desired_ids,
                    )
                ],
            )
        return (
            None,
            MappingSelection(
                mapping_id=None,
                mapping_generation=plan.generation,
                topology_name=plan.topology_name,
                parameters=dict(plan.parameters),
                option_choices=mapping_option_choices(plan.parameters),
                placement_digest=plan.placement_digest,
                action="create",
                nodes=[
                    SparkGroupNode(
                        node_id=node.node_id,
                        rank=node.rank,
                        role=node.role,
                        endpoint_owner=node.endpoint_owner,
                    )
                    for node in plan.nodes
                ],
            ),
            [],
        )

    def _matching_installation(
        self,
        session: Session,
        revision_id: str,
        model_digest: str,
        mapping: ClusterMapping | None,
        group: SparkGroup,
    ) -> RecipeInstallation | None:
        if mapping is None:
            return None
        candidates = tuple(
            session.scalars(
                select(RecipeInstallation)
                .where(
                    RecipeInstallation.recipe_revision_id == revision_id,
                    RecipeInstallation.mapping_id == mapping.id,
                    RecipeInstallation.mapping_generation == mapping.generation,
                    RecipeInstallation.model_content_sha256 == model_digest,
                    RecipeInstallation.state.in_(
                        (
                            InstallationState.INSTALLED,
                            InstallationState.INSTALLING,
                            InstallationState.PARTIAL,
                        )
                    ),
                )
                .order_by(
                    RecipeInstallation.state.desc(),
                    RecipeInstallation.updated_at.desc(),
                )
            )
        )
        desired = {(node.node_id, node.rank, node.role) for node in group.nodes}
        for installation in candidates:
            if not installation_serves_authorised_ports(installation):
                continue
            installed = {
                (node.node_id, node.rank, node.role)
                for node in session.scalars(
                    select(InstallationNode).where(
                        InstallationNode.installation_id == installation.id
                    )
                )
                if node.state == InstallationNodeState.INSTALLED
            }
            if installed == desired:
                return installation
        return None

    def _build_is_available(self, candidate: RecipeBuild) -> bool:
        """Whether the recorded receipt still proves a present prepared image."""

        if (
            candidate.state != "succeeded"
            or candidate.image_digest is None
            or candidate.oci_layout_sha256 is None
            or type(candidate.image_bytes) is not int
        ):
            return False
        return self._build_archive_available is None or self._build_archive_available(
            candidate.oci_layout_sha256, candidate.image_bytes
        )

    def _matching_build(
        self,
        session: Session,
        revision_id: str,
        *,
        expected_image: RuntimeImageIdentity | None,
    ) -> RecipeBuild | None:
        if expected_image is not None:
            # Accepted work remains bound to its approved receipt even when a
            # newer completed build becomes available while it is waiting. The
            # image is identified by its content: the receipt names the exact
            # build, and its digest is compared to the compiled image later.
            build = (
                session.get(RecipeBuild, expected_image.build_id)
                if expected_image.build_id is not None
                else None
            )
            if build is None or not self._build_is_available(build):
                return None
            return build

        # A fresh review selects the current completed image of this revision.
        # A successor with the same executable inputs finds its predecessor's
        # build by content in ``_select_build``.
        candidates = session.scalars(
            select(RecipeBuild)
            .where(
                RecipeBuild.recipe_revision_id == revision_id,
                RecipeBuild.state == "succeeded",
                RecipeBuild.image_digest.is_not(None),
                RecipeBuild.image_bytes.is_not(None),
            )
            .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id.desc())
        )
        for candidate in candidates:
            if self._build_is_available(candidate):
                return candidate
        return None

    @staticmethod
    def _latest_build(session: Session, revision_id: str) -> RecipeBuild | None:
        return session.scalar(
            select(RecipeBuild)
            .where(RecipeBuild.recipe_revision_id == revision_id)
            .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id)
            .limit(1)
        )

    def _select_build(
        self,
        session: Session,
        revision: CatalogDocumentRevision,
        build: RecipeBuild | None,
        candidate: RecipeBuild | None,
        group: SparkGroup,
        *,
        now: datetime,
        create_build: bool = True,
    ) -> _BuildSelection:
        """Resolve an immutable build or create a pending Controller build.

        A successful receipt is reusable without re-admitting its builder.  A
        pending receipt is reusable only while its builder still has fresh
        typed build evidence.  Otherwise the Controller chooses the first
        compatible builder in deterministic order, preferring a node outside
        the inference group, and delegates planning to the existing recipe
        build primitive.  This method only creates the durable *planned*
        receipt; bytes are produced by ``recipe.build.v1`` during apply.
        """

        if build is not None:
            return _BuildSelection(build=build, candidate=build)

        group_ids = {node.node_id for node in group.nodes}
        if candidate is not None and candidate.state in {"planned", "building"}:
            builder = session.get(AgentNode, candidate.builder_node_id)
            freshness, admissible = self._builder_admission(session, builder, now=now)
            if admissible:
                return _BuildSelection(
                    build=None,
                    candidate=candidate,
                    builder_freshness=freshness,
                )

        # The same executable inputs mean the same image, whichever revision
        # first built it: reuse that build by content, creating nothing.
        reusable_build_id = getattr(self._lifecycle, "reusable_build_id", None)
        if callable(reusable_build_id):
            found_id = reusable_build_id(revision.id)
            found = session.get(RecipeBuild, found_id) if found_id else None
            if found is not None and self._build_is_available(found):
                return _BuildSelection(build=found, candidate=found)

        preview_build = getattr(self._lifecycle, "preview_build", None)
        if not create_build:
            # The same missing-build evidence below explains the blocker.
            # Inspection is not permission to persist preparation intent.
            return _BuildSelection(build=None, candidate=candidate)
        if not callable(preview_build):
            return _BuildSelection(
                build=None,
                candidate=None,
                blockers=(
                    _as_reason(
                        RunSwitchCode.CONTAINER_BUILD_UNAVAILABLE,
                        "The existing recipe build primitive is unavailable; the Controller cannot prepare the exact OCI runtime image.",
                        scope="operation",
                        node_ids=[node.node_id for node in group.nodes],
                    ),
                ),
            )

        ordered_nodes = self._builder_nodes(session, group_ids)
        errors: list[str] = []
        saw_builder = False
        for node in ordered_nodes:
            freshness, admissible = self._builder_admission(session, node, now=now)
            if not admissible:
                continue
            saw_builder = True
            try:
                proposed = preview_build(revision.id, node.node_id)
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                errors.append(f"{node.node_id}: {error}")
                continue
            proposed_id = getattr(proposed, "build_id", None)
            if not isinstance(proposed_id, str):
                errors.append(
                    f"{node.node_id}: build preview returned no build identity"
                )
                continue
            selected = session.get(RecipeBuild, proposed_id)
            if selected is not None:
                # ``preview_build`` persists through the lifecycle service's
                # own short transaction.  This Session may already hold the
                # succeeded row that was reset after its archive disappeared;
                # refresh it so the first preview sees the durable planned row.
                session.refresh(selected)
            if selected is None:
                errors.append(f"{node.node_id}: build preview receipt is unavailable")
                continue
            if selected.recipe_revision_id == revision.id:
                return _BuildSelection(
                    build=None,
                    candidate=selected,
                    builder_freshness=freshness,
                )
            # A succeeded receipt may be recorded under an earlier editorial
            # revision of the same recipe while its executable input identity
            # is identical (including the resolved runtime adapter).  That is
            # the exact prepared image; require the recorded input identity and
            # present bytes rather than the revision id, and never accept a
            # mismatched identity.
            if getattr(
                proposed, "build_input_sha256", None
            ) != selected.build_input_sha256 or not self._build_is_available(selected):
                errors.append(f"{node.node_id}: build preview receipt is unavailable")
                continue
            return _BuildSelection(build=selected, candidate=selected)

        if saw_builder:
            detail = "No compatible Controller builder could prepare the exact recipe source and runtime image."
            if errors:
                detail += " " + errors[-1]
        else:
            detail = "No active linux-arm64 worker has fresh recipe.build.v1 admission evidence."
        return _BuildSelection(
            build=None,
            candidate=None,
            blockers=(
                _as_reason(
                    RunSwitchCode.CONTAINER_BUILD_UNAVAILABLE,
                    detail,
                    scope="operation",
                    node_ids=[node.node_id for node in group.nodes],
                ),
            ),
        )

    @staticmethod
    def _builder_nodes(session: Session, group_ids: set[str]) -> tuple[AgentNode, ...]:
        nodes = tuple(
            session.scalars(
                select(AgentNode)
                .where(
                    AgentNode.state == "active",
                    AgentNode.revoked_at.is_(None),
                    AgentNode.architecture == "linux-arm64",
                )
                .order_by(AgentNode.node_id)
            )
        )
        # A builder may be a member of the selected group, but a separate
        # active worker is preferred so build memory cannot contend with the
        # inference admission.  Both choices remain deterministic.
        return tuple(
            sorted(nodes, key=lambda node: (node.node_id in group_ids, node.node_id))
        )

    def _builder_admission(
        self,
        session: Session,
        node: AgentNode | None,
        *,
        now: datetime,
    ) -> tuple[FreshnessEvidence | None, bool]:
        """Return fresh builder evidence and whether it can run recipe.build.v1."""

        if node is None:
            return None, False
        freshness = _latest_inventory(
            session,
            node.node_id,
            now=now,
            maximum_age_seconds=self._inventory_max_age,
        )[1]
        if (
            node.state != "active"
            or node.revoked_at is not None
            or node.architecture != "linux-arm64"
            or not _is_hex_digest(node.binary_digest)
            or freshness.state != "fresh"
        ):
            return freshness, False
        snapshot = session.scalar(
            select(NodeInventorySnapshot)
            .where(NodeInventorySnapshot.node_id == node.node_id)
            .order_by(NodeInventorySnapshot.observed_at.desc())
            .limit(1)
        )
        if snapshot is None or "recipe.build.v1" not in snapshot.capabilities:
            return freshness, False
        return freshness, True

    @staticmethod
    def _build_evidence(
        session: Session,
        revision: CatalogDocumentRevision | None,
        build: RecipeBuild | None,
        candidate: RecipeBuild | None,
        group: SparkGroup,
        *,
        require_available: bool = True,
        defer_source_build: bool = False,
        expected_image: RuntimeImageIdentity | None = None,
    ) -> tuple[
        RunSwitchBuildEvidence,
        RuntimeImageStorageImpact,
        list[RunSwitchReason],
        list[RunSwitchReason],
    ]:
        blockers: list[RunSwitchReason] = []
        warnings: list[RunSwitchReason] = []
        document = revision.document if revision is not None else {}
        execution = document.get("execution") if isinstance(document, Mapping) else None
        raw_build = execution.get("build") if isinstance(execution, Mapping) else None
        # Every recipe image is built for the DGX Spark platform.
        expected_architecture = "linux/arm64"
        source_digest = (
            candidate.source_bundle_sha256
            if candidate is not None
            else (
                raw_build.get("context", {}).get("sha256")
                if isinstance(raw_build, Mapping)
                and isinstance(raw_build.get("context"), Mapping)
                else None
            )
        )
        source_row = (
            session.get(RecipeSourceBundle, source_digest)
            if isinstance(source_digest, str)
            else None
        )
        source_state = "available" if source_row is not None else "missing"
        source = BuildSourceEvidence(
            state=source_state,
            source_bundle_sha256=(
                source_digest if _is_hex_digest(source_digest) else None
            ),
            detail=(
                None
                if source_row is not None
                else "The verified source bundle is not present in Controller storage."
            ),
        )
        observed_architecture: str | None = None
        candidate_plan_valid = True
        if candidate is not None:
            try:
                parse_stored_build_plan(candidate.plan)
            except RecipeExecutionContractError:
                candidate_plan_valid = False
                blockers.append(
                    _as_reason(
                        RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID,
                        "The persisted source-build plan is invalid.",
                        scope="operation",
                    )
                )
            else:
                observed_architecture = "linux/arm64"
            if candidate_plan_valid and observed_architecture is None:
                builder = session.get(AgentNode, candidate.builder_node_id)
                if builder is not None and isinstance(builder.architecture, str):
                    observed_architecture = _normalise_architecture(
                        builder.architecture
                    )
        node_architectures = tuple(
            _normalise_architecture(node.architecture)
            for node in session.scalars(
                select(AgentNode).where(
                    AgentNode.node_id.in_([item.node_id for item in group.nodes])
                )
            )
            if isinstance(node.architecture, str)
        )
        compatibility_state = "unknown"
        if expected_architecture != "unknown" and observed_architecture is not None:
            compatibility_state = (
                "compatible"
                if (
                    _normalise_architecture(expected_architecture)
                    == _normalise_architecture(observed_architecture)
                    and len(node_architectures) == len(group.nodes)
                    and all(
                        architecture == _normalise_architecture(expected_architecture)
                        for architecture in node_architectures
                    )
                )
                else "incompatible"
            )
        compatibility = BuildCompatibilityEvidence(
            expected_architecture=expected_architecture,
            observed_architecture=observed_architecture,
            state=compatibility_state,
            evidence_digest=(
                _digest(
                    {
                        "expected": expected_architecture,
                        "observed": observed_architecture,
                        "nodes": node_architectures,
                    }
                )
                if observed_architecture is not None
                else None
            ),
            detail=(
                None
                if compatibility_state != "incompatible"
                else "The built image or selected Spark group does not match the recipe platform."
            ),
        )
        # The accepted image is an execution constraint, never proof of bytes.
        # Preserve a newly observed output so the caller can reject identity drift.
        source_identity = build if build is not None else candidate
        image_digest = (
            source_identity.image_digest
            if source_identity is not None and source_identity.image_digest is not None
            else expected_image.image_digest
            if build is None and expected_image is not None
            else build.image_digest
            if build is not None
            else None
        )
        image_bytes = (
            source_identity.image_bytes
            if source_identity is not None and source_identity.image_bytes is not None
            else expected_image.image_bytes
            if build is None and expected_image is not None
            else build.image_bytes
            if build is not None
            else None
        )
        oci_layout = (
            source_identity.oci_layout_sha256
            if source_identity is not None
            and source_identity.oci_layout_sha256 is not None
            else expected_image.oci_layout_sha256
            if build is None and expected_image is not None
            else build.oci_layout_sha256
            if build is not None
            else None
        )
        runtime_reused = 0
        runtime_missing = 0
        runtime_missing_by_node: dict[str, int] = {}
        runtime_reclaimable = 0
        runtime_reclaimable_digests: set[str] = set()
        runtime_raw_digest = (
            image_digest.removeprefix("sha256:")
            if isinstance(image_digest, str)
            else None
        )
        if image_bytes is not None and runtime_raw_digest is not None:
            for node in group.nodes:
                artifact = session.scalar(
                    select(NodeArtifact).where(
                        NodeArtifact.node_id == node.node_id,
                        NodeArtifact.digest == runtime_raw_digest,
                    )
                )
                if (
                    artifact is not None
                    and artifact.kind == "image"
                    and artifact.state == ModelFileState.VERIFIED
                    and artifact.size_bytes >= image_bytes
                ):
                    runtime_reused += image_bytes
                else:
                    runtime_missing += image_bytes
                    runtime_missing_by_node[node.node_id] = image_bytes
                runtime_missing_by_node.setdefault(node.node_id, 0)
                if (
                    artifact is not None
                    and artifact.state == ModelFileState.VERIFIED
                    and artifact.ref_count == 0
                ):
                    runtime_reclaimable += artifact.size_bytes
                    runtime_reclaimable_digests.add(artifact.digest)
        runtime_coverage = (
            "complete"
            if image_bytes is not None and runtime_missing == 0
            else "partial"
            if image_bytes is not None
            else "unknown"
        )
        runtime = RuntimeImageStorageImpact(
            build_id=(
                build.id
                if build is not None
                else candidate.id
                if candidate is not None
                else None
            ),
            preparation_required=False,
            image_digest=image_digest,
            oci_layout_sha256=oci_layout,
            image_bytes=image_bytes,
            required_bytes=(
                image_bytes * len(group.nodes) if image_bytes is not None else None
            ),
            reused_bytes=runtime_reused,
            copied_bytes=runtime_missing,
            missing_nas_bytes=image_bytes if build is None else None,
            missing_spark_bytes=(runtime_missing if image_bytes is not None else None),
            missing_image_distribution_bytes=(
                runtime_missing if image_bytes is not None else None
            ),
            missing_image_distribution_bytes_by_node=(
                runtime_missing_by_node if image_bytes is not None else None
            ),
            nas_coverage=(
                "partial"
                if build is None
                else "complete"
                if image_bytes is not None and oci_layout is not None
                else "unknown"
            ),
            spark_coverage=runtime_coverage,
            reclaimable_bytes=runtime_reclaimable,
            reclaimable_digests=sorted(runtime_reclaimable_digests),
        )
        state = "missing" if candidate is None else str(candidate.state)
        detail: str | None = None
        if build is not None:
            state = "available"
            if not isinstance(oci_layout, str) or not _is_hex_digest(oci_layout):
                state = "incompatible"
                detail = "The successful build has no verified OCI layout identity."
        elif candidate is not None:
            if candidate.state == "succeeded":
                state = "missing"
                detail = (
                    "The recorded build has no currently usable Controller archive."
                )
            else:
                detail = candidate.error
        if compatibility_state == "incompatible":
            state = "incompatible"
        if require_available:
            if build is None:
                pending = candidate is not None and candidate.state in {
                    "planned",
                    "building",
                }
                if (pending or defer_source_build) and source_state == "available":
                    warnings.append(
                        _as_reason(
                            RunSwitchCode.CONTAINER_BUILD_REQUIRED,
                            (
                                "The accepted runtime image needs cache repair before target transfer."
                                if defer_source_build and not pending
                                else "The exact OCI runtime image is pinned to a durable Controller build and will be prepared before target transfer."
                            ),
                            scope="operation",
                            severity="warning",
                            node_ids=[node.node_id for node in group.nodes],
                        )
                    )
                else:
                    blockers.append(
                        _as_reason(
                            RunSwitchCode.RECIPE_BUILD_UNAVAILABLE,
                            (
                                "No successful immutable runtime image build is available."
                                if candidate is None
                                else f"Runtime image preparation is {candidate.state}: {candidate.error or 'no completed image receipt is available.'}"
                            ),
                            scope="operation",
                            node_ids=[node.node_id for node in group.nodes],
                        )
                    )
            if compatibility_state == "incompatible":
                blockers.append(
                    _as_reason(
                        RunSwitchCode.RECIPE_BUILD_INCOMPATIBLE,
                        compatibility.detail
                        or "Runtime image architecture is incompatible with the selected group.",
                        scope="operation",
                        node_ids=[node.node_id for node in group.nodes],
                    )
                )
            elif compatibility_state == "unknown":
                warnings.append(
                    _as_reason(
                        RunSwitchCode.RECIPE_BUILD_COMPATIBILITY_UNKNOWN,
                        "The immutable build receipt does not include enough architecture evidence to prove compatibility.",
                        scope="operation",
                        severity="warning",
                        node_ids=[node.node_id for node in group.nodes],
                    )
                )
        return (
            RunSwitchBuildEvidence(
                state=_BUILD_EVIDENCE_STATE_ADAPTER.validate_python(state, strict=True),
                build_id=build.id
                if build is not None
                else candidate.id
                if candidate is not None
                else None,
                build_input_sha256=(
                    build.build_input_sha256
                    if build is not None
                    else candidate.build_input_sha256
                    if candidate is not None
                    else None
                ),
                builder_node_id=(
                    build.builder_node_id
                    if build is not None
                    else candidate.builder_node_id
                    if candidate is not None
                    else None
                ),
                image_digest=image_digest,
                image_bytes=image_bytes,
                oci_layout_sha256=oci_layout,
                source=source,
                compatibility=compatibility,
                runtime=runtime,
                detail=detail,
            ),
            runtime,
            blockers,
            warnings,
        )

    @staticmethod
    def _preparation(
        *,
        revision: CatalogDocumentRevision | None,
        group: SparkGroup,
        inspection: ArtifactInspection,
        build: RecipeBuild | None,
        build_candidate: RecipeBuild | None,
        runtime_storage: RuntimeImageStorageImpact,
        now: datetime,
        reasons: Sequence[RunSwitchReason],
    ) -> RolloutPreparation | None:
        """Normalize model and runtime asset readiness for shared callers."""

        if revision is None or (build is None and runtime_storage.image_digest is None):
            # There is no honest immutable runtime image identity to place in
            # RolloutPreparation until a successful build receipt
            # exists.
            return None
        primary_model_digest = _primary_model_digest(revision.document)
        if primary_model_digest is None:
            return None
        model_digests = tuple(inspection.artifact_digests)
        model_expected = inspection.artifact_set_bytes
        if model_expected is None or model_expected < 1:
            # The shared preparation contract requires the exact model set
            # size.  Keep this unknown rather than manufacturing a byte count.
            return None
        artifact_set_digest = inspection.artifact_set_sha256
        if not _is_hex_digest(artifact_set_digest):
            return None
        artifact_set_bytes = inspection.artifact_set_bytes or model_expected
        if artifact_set_bytes != model_expected:
            return None
        model_missing_by_node = inspection.missing_spark_bytes_by_node
        model_controller_expected = model_expected
        model_controller_verified = (
            model_controller_expected
            if inspection.nas_coverage == "complete"
            and inspection.missing_nas_bytes in (None, 0)
            else 0
        )
        model_controller_missing = (
            None
            if model_controller_expected is None
            else model_controller_expected - model_controller_verified
        )
        model_controller_ready = (
            model_controller_expected is not None
            and model_controller_missing == 0
            and inspection.nas_coverage == "complete"
        )
        model_controller = ControllerAssetState(
            state="ready" if model_controller_ready else "unknown",
            expected_bytes=model_controller_expected,
            verified_bytes=model_controller_verified,
            missing_bytes=model_controller_missing,
            verified_sha256=artifact_set_digest if model_controller_ready else None,
            verified_at=now if model_controller_ready else None,
            source="nas-cache" if inspection.nas_coverage != "unknown" else "unknown",
            reason=(
                None
                if model_controller_ready
                else "NAS coverage for the exact model artifact set is not proven."
            ),
        )
        model_targets: list[TargetAssetState] = []
        for node in group.nodes:
            model_missing = _node_missing_bytes(
                model_missing_by_node,
                node.node_id,
                inspection.missing_spark_bytes,
                len(group.nodes),
            )
            target_ready = model_expected is not None and model_missing == 0
            model_targets.append(
                TargetAssetState(
                    node_id=node.node_id,
                    state="ready" if target_ready else "unknown",
                    expected_bytes=model_expected,
                    present_bytes=(
                        max(0, model_expected - model_missing)
                        if model_missing is not None
                        else 0
                    ),
                    # Unknown per-node evidence means "treat as missing"; the
                    # distribution step then copies or verifies it.
                    missing_bytes=(
                        model_missing if model_missing is not None else model_expected
                    ),
                    verified_sha256=artifact_set_digest if target_ready else None,
                    verified_at=now if target_ready else None,
                    reason=(
                        None
                        if target_ready
                        else "The exact model artifact set is not verified on this Spark."
                    ),
                )
            )
        model_completeness = (
            "unknown"
            if not model_digests
            else "complete"
            if model_controller_ready
            and all(target.state == "ready" for target in model_targets)
            else "incomplete"
        )
        model = ModelArtifactPreparation(
            artifact_set_sha256=artifact_set_digest,
            model_content_sha256=primary_model_digest,
            recipe_revision_sha256=revision.content_digest,
            artifact_count=max(1, len(model_digests)),
            artifact_set_bytes=artifact_set_bytes,
            dependency_model_content_sha256=sorted(
                set(inspection.dependency_model_content_sha256)
            ),
            completeness=model_completeness,
            controller=model_controller,
            targets=model_targets,
        )
        image_digest = (
            build.image_digest if build is not None else runtime_storage.image_digest
        )
        image_bytes = (
            build.image_bytes if build is not None else runtime_storage.image_bytes
        )
        layout_digest = (
            build.oci_layout_sha256
            if build is not None
            else runtime_storage.oci_layout_sha256
        )
        if (
            not _is_oci_digest(image_digest)
            or image_bytes is None
            or not _is_hex_digest(layout_digest)
        ):
            return None
        runtime_controller = ControllerAssetState(
            state=(
                "ready"
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else "failed"
                if any(
                    reason.code == RunSwitchCode.RUNTIME_IMAGE_AUTHORIZATION_MISMATCH
                    for reason in reasons
                )
                else "missing"
                if runtime_storage.missing_nas_bytes not in (None, 0)
                else "unknown"
            ),
            expected_bytes=image_bytes,
            verified_bytes=(
                image_bytes
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else 0
            ),
            missing_bytes=(
                0
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else image_bytes
            ),
            verified_sha256=(
                layout_digest
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else None
            ),
            verified_at=(
                now
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else None
            ),
            source="controller-build",
            reason=(
                None
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else next(
                    (
                        reason.detail[:256]
                        for reason in reasons
                        if reason.code
                        == RunSwitchCode.RUNTIME_IMAGE_AUTHORIZATION_MISMATCH
                    ),
                    "The exact OCI archive is missing from Controller storage."
                    if runtime_storage.missing_nas_bytes not in (None, 0)
                    else "Controller storage coverage for the exact OCI image is not proven.",
                )
            ),
        )
        runtime_targets = []
        for node in group.nodes:
            image_missing = _node_missing_bytes(
                runtime_storage.missing_image_distribution_bytes_by_node,
                node.node_id,
                runtime_storage.missing_image_distribution_bytes,
                len(group.nodes),
            )
            image_ready = image_missing == 0
            runtime_targets.append(
                TargetAssetState(
                    node_id=node.node_id,
                    state="ready" if image_ready else "unknown",
                    expected_bytes=image_bytes,
                    present_bytes=(
                        max(0, image_bytes - image_missing)
                        if image_missing is not None
                        else 0
                    ),
                    missing_bytes=(
                        image_missing if image_missing is not None else image_bytes
                    ),
                    verified_sha256=layout_digest if image_ready else None,
                    verified_at=now if image_ready else None,
                    imported_image_digest=image_digest if image_ready else None,
                    reason=(
                        None
                        if image_ready
                        else "The exact OCI image is not imported on this Spark."
                    ),
                )
            )
        runtime = RuntimeImagePreparation(
            image_digest=image_digest,
            oci_layout_sha256=layout_digest,
            image_bytes=image_bytes,
            architecture="linux-arm64",
            runtime_interface=RUNTIME_INTERFACE,
            build_id=build.id
            if build is not None
            else build_candidate.id
            if build_candidate is not None
            else None,
            controller=runtime_controller,
            targets=runtime_targets,
        )
        prep_reasons = [
            PreparationReason(
                code=reason.code,
                detail=reason.detail,
                severity=reason.severity,
                node_ids=reason.node_ids,
            )
            for reason in reasons
            if reason.severity in {"blocker", "warning", "info"}
        ]
        controller_ready = controller_assets_ready(model, runtime)
        targets_ready = all(
            target.state == "ready"
            for asset in (model, runtime)
            for target in asset.targets
        )
        ready = (
            controller_ready
            and targets_ready
            and not any(reason.severity == "blocker" for reason in prep_reasons)
        )
        return RolloutPreparation(
            model=model,
            runtime_image=runtime,
            exceptions=[],
            target_node_ids=sorted(node.node_id for node in group.nodes),
            controller_ready=controller_ready,
            targets_ready=targets_ready,
            ready=ready,
            reasons=prep_reasons,
        )

    def _conflicts(
        self,
        session: Session,
        node_ids: tuple[str, ...],
        *,
        action: str,
    ) -> tuple[list[RunSwitchReason], list[StopImpact], list[RunSwitchReason]]:
        conflicts: list[RunSwitchReason] = []
        stops: list[StopImpact] = []
        blockers: list[RunSwitchReason] = []
        rows = tuple(
            session.scalars(
                select(RecipeRun)
                .where(
                    RecipeRun.id.in_(
                        select(RunNode.run_id).where(RunNode.node_id.in_(node_ids))
                    ),
                    RecipeRun.state.in_(STOPPABLE_RUN_STATES),
                )
                .order_by(RecipeRun.created_at, RecipeRun.id)
            )
        )
        wanted = set(node_ids)
        for run in rows:
            run_nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run.id)
                    .order_by(RunNode.rank)
                )
            )
            run_ids = {node.node_id for node in run_nodes}
            overlap = wanted & run_ids
            if not overlap:
                continue
            if run_ids - wanted:
                reason = _as_reason(
                    RunSwitchCode.CROSS_GROUP_CONFLICT,
                    "An active distributed run crosses the selected complete Spark group; partial stop is unsafe.",
                    scope="conflict",
                    node_ids=tuple(sorted(overlap)),
                )
                conflicts.append(reason)
                blockers.append(reason)
                continue
            if action == "run":
                reason = _as_reason(
                    RunSwitchCode.ACTIVE_RUN_CONFLICT,
                    "The selected Spark group already has an active workload and must be stopped before starting this outcome.",
                    scope="conflict",
                    severity="warning",
                    node_ids=tuple(sorted(run_ids)),
                )
                conflicts.append(reason)
            try:
                stop_plan = (
                    self._lifecycle.preview_stop(run.id) if self._lifecycle else None
                )
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                stop_plan = None
            if stop_plan is None or not stop_plan.allowed:
                reason = _as_reason(
                    RunSwitchCode.STOP_PLAN_UNAVAILABLE,
                    "The existing workload cannot be represented by a safe stop plan.",
                    scope="conflict",
                    node_ids=tuple(sorted(run_ids)),
                )
                blockers.append(reason)
                continue
            stops.append(
                StopImpact(
                    run_id=run.id,
                    run_plan_digest=run.plan_digest,
                    alias=run.alias,
                    state=run.state,
                    node_ids=sorted(run_ids),
                    reserved_bytes=self._run_reserved_bytes(session, run.id),
                    plan_digest=stop_plan.plan_digest,
                )
            )
        return conflicts, stops, blockers

    @staticmethod
    def _placement_fit(fit: SparkFit, blockers: Sequence[RunSwitchReason]) -> SparkFit:
        all_blockers = [*blockers, *fit.blockers]
        return fit.model_copy(
            update={"allowed": not all_blockers, "blockers": all_blockers}
        )

    def recheck_resources_in_session(
        self,
        session: Session,
        request: RunSwitchPreviewRequest,
        reviewed: RunSwitchAssessment,
        *,
        excluded_profile_application_ids: tuple[str, ...] = (),
    ) -> RunSwitchAssessment:
        """Refresh SQL-owned resource facts without reopening artifact admission.

        The caller fences inventory/reservation writers and validates the exact
        reviewed stop set before this call. Cache, build and capability-provider
        work remains outside this transaction. Preparation evidence here stays
        the reviewed snapshot; this method makes no new availability claim.
        """
        revision = _active_recipe_revision(session, request.recipe_revision_id)
        if revision is None:
            raise RunSwitchRequestInvalid(RunSwitchCode.RECIPE_UNRESOLVED)
        _, model_documents, blockers = self._resolve_documents(
            session,
            revision,
            request.model_content_sha256,
            requested_recipe_digest=revision.content_digest,
        )
        resolution = resolve_effective_settings(revision.document)
        blockers.extend(
            _resource_reason(
                reason,
                node_ids=tuple(node.node_id for node in request.spark_group.nodes),
            )
            for reason in resolution.reasons
        )
        resources = self._resource_fits(
            session,
            revision,
            request,
            now=_now(self._clock),
            stops=reviewed.stops,
            placement_blockers=blockers,
            effective_settings=resolution.settings,
            model_documents=model_documents,
            image_bytes=reviewed.preparation.runtime_image.image_bytes
            if reviewed.preparation is not None
            else None,
            artifact_bytes=reviewed.preparation.model.artifact_set_bytes
            if reviewed.preparation is not None
            else None,
            excluded_profile_application_ids=excluded_profile_application_ids,
        )
        blockers.extend(resources.blockers)
        return read_stored_model(
            RunSwitchAssessment,
            {
                **{
                    name: getattr(reviewed, name)
                    for name in RunSwitchAssessment.model_fields
                },
                # The fit above read the Spark inventory as it is now, and its
                # memory evidence names that sample. Carry the same samples, not
                # the reviewed ones: an inventory refreshed since the review
                # (a parked admission is retried minutes later) would otherwise
                # fail the assessment's own sample binding on every retry.
                "freshness": _refreshed_freshness(
                    reviewed.freshness, resources.freshness
                ),
                "allowed": not blockers,
                "blockers": blockers,
                "fit_current": resources.current,
                "fit_after_stop": resources.after_stop,
                "post_stop_memory_check": resources.post_stop_memory_check,
                "effective_settings": _settings_view(resolution.settings)
                if resolution.settings is not None
                else None,
                "stop_before_prepare": resources.stop_before_prepare,
                "stop_before_transfer": resources.stop_before_transfer,
            },
        )

    def _resource_fits(
        self,
        session: Session,
        revision: CatalogDocumentRevision,
        request: RunSwitchPreviewRequest,
        *,
        now: datetime,
        stops: Sequence[StopImpact],
        placement_blockers: Sequence[RunSwitchReason],
        effective_settings: object | None,
        model_documents: Mapping[tuple[str, str, str], ModelDefinition],
        image_bytes: int | None = None,
        artifact_bytes: int | None = None,
        excluded_profile_application_ids: tuple[str, ...] = (),
    ) -> _ResourceFits:
        (
            freshness,
            current,
            current_blockers,
            current_warnings,
            current_memory_shortfalls,
        ) = self._fit(
            session,
            revision,
            request.spark_group,
            now=now,
            excluded_run_ids=(),
            effective_settings=effective_settings,
            model_documents=model_documents,
            serving=request.action != "install",
            revision_digest=revision.content_digest,
            image_bytes=image_bytes,
            artifact_bytes=artifact_bytes,
            excluded_profile_application_ids=excluded_profile_application_ids,
        )
        current = self._placement_fit(current, placement_blockers)
        after_stop = None
        blockers = current_blockers
        conditional = None
        if stops:
            (
                _,
                after_stop,
                after_stop_blockers,
                after_stop_warnings,
                after_stop_memory_shortfalls,
            ) = self._fit(
                session,
                revision,
                request.spark_group,
                now=now,
                excluded_run_ids=tuple(stop.run_id for stop in stops),
                effective_settings=effective_settings,
                model_documents=model_documents,
                serving=request.action != "install",
                revision_digest=revision.content_digest,
                image_bytes=image_bytes,
                artifact_bytes=artifact_bytes,
                excluded_profile_application_ids=excluded_profile_application_ids,
            )
            after_stop = self._placement_fit(after_stop, placement_blockers)
            warnings = list(current_warnings)
            for warning in after_stop_warnings:
                if warning not in warnings:
                    warnings.append(warning)

            current_memory_blockers = tuple(
                reason
                for reason in current_blockers
                if reason.code in _MEMORY_STOP_CONDITIONAL_REFUSALS
            )
            if current_memory_blockers:
                # A post-stop fit is still based on the pre-stop inventory.
                # When current memory fit depends on an exact stop, publish
                # only the stop-bound condition and let the existing fresh
                # post-stop inventory gate decide whether dispatch can start.
                post_stop_blockers_are_memory_only = all(
                    reason.code in _MEMORY_CAPACITY_REFUSALS
                    for reason in after_stop.blockers
                )
                memory_shortfalls = {
                    node_id: current_memory_shortfalls.get(node_id, frozenset())
                    | after_stop_memory_shortfalls.get(node_id, frozenset())
                    for node_id in set(current_memory_shortfalls)
                    | set(after_stop_memory_shortfalls)
                }
                if post_stop_blockers_are_memory_only:
                    conditional = _conditional_post_stop_memory_check(
                        session,
                        stops,
                        current,
                        (
                            *current_memory_blockers,
                            *(
                                reason
                                for reason in after_stop.blockers
                                if reason.code in _MEMORY_CAPACITY_REFUSALS
                            ),
                        ),
                        memory_shortfalls,
                    )
                blockers = list(current_memory_blockers)
                for reason in after_stop.blockers:
                    if reason not in blockers:
                        blockers.append(reason)
                after_stop = None
            else:
                conditional = _conditional_post_stop_memory_check(
                    session,
                    stops,
                    after_stop,
                    after_stop.blockers,
                    after_stop_memory_shortfalls,
                )
                blockers = after_stop_blockers
            if conditional is not None:
                after_stop = None
                blockers = []
        else:
            warnings = current_warnings
        return _ResourceFits(
            freshness,
            current,
            after_stop,
            current_blockers,
            blockers,
            warnings,
            conditional,
        )

    def _fit(
        self,
        session: Session,
        revision: CatalogDocumentRevision | None,
        group: SparkGroup,
        *,
        now: datetime,
        excluded_run_ids: Sequence[str],
        effective_settings: object | None = None,
        model_documents: Mapping[tuple[str, str, str], ModelDefinition] | None = None,
        revision_digest: str | None = None,
        serving: bool = True,
        image_bytes: int | None = None,
        artifact_bytes: int | None = None,
        excluded_profile_application_ids: tuple[str, ...] = (),
    ) -> tuple[
        list[FreshnessEvidence],
        SparkFit,
        list[RunSwitchReason],
        list[RunSwitchReason],
        dict[str, frozenset[str]],
    ]:
        freshness: list[FreshnessEvidence] = []
        nodes: list[SparkFitNode] = []
        blockers: list[RunSwitchReason] = []
        warnings: list[RunSwitchReason] = []
        insufficient_components_by_node: dict[str, frozenset[str]] = {}
        adoptable = (
            unowned_never_installed(
                session,
                recipe_revision_id=revision.id,
                node_ids=frozenset(node.node_id for node in group.nodes),
            )
            if revision is not None
            else ()
        )
        role_by_name = (
            {role.name: role for role in recipe_topology(revision.document).roles}
            if revision is not None
            else {}
        )
        recipe_models = _recipe_model_digests(revision)
        excluded = set(excluded_run_ids)
        for item in group.nodes:
            snapshot, evidence = _latest_inventory(
                session,
                item.node_id,
                now=now,
                maximum_age_seconds=self._inventory_max_age,
            )
            freshness.append(evidence)
            node_blockers: list[RunSwitchReason] = []
            node_warnings: list[RunSwitchReason] = []
            ports_required: list[int] = []
            if serving and revision is not None:
                try:
                    port_demand = allocate_service_port(
                        session,
                        item.node_id,
                        run_port_demand(
                            revision.document,
                            node_count=len(group.nodes),
                            endpoint_owner=item.endpoint_owner,
                        ),
                        excluded_run_ids=excluded_run_ids,
                        excluded_profile_application_ids=excluded_profile_application_ids,
                    )
                except (TypeError, ValueError) as error:
                    node_blockers.append(
                        _as_reason(
                            RunSwitchCode.INTERFACE_INVALID,
                            str(error),
                            scope="recipe",
                            node_ids=(item.node_id,),
                        )
                    )
                else:
                    ports_required = list(port_demand.required_ports)
                    node_blockers.extend(
                        _as_reason(
                            reason.code,
                            reason.detail,
                            scope="node",
                            node_ids=(item.node_id,),
                        )
                        for reason in run_port_blockers(
                            session,
                            item.node_id,
                            port_demand,
                            excluded_run_ids=excluded_run_ids,
                            excluded_profile_application_ids=excluded_profile_application_ids,
                        )
                    )
            if snapshot is None:
                node_blockers.append(
                    _as_reason(
                        RunSwitchCode.INVENTORY_UNKNOWN,
                        "No authenticated Spark inventory is available.",
                        scope="freshness",
                        node_ids=(item.node_id,),
                    )
                )
            elif evidence.state == "stale":
                node_blockers.append(
                    _as_reason(
                        RunSwitchCode.INVENTORY_STALE,
                        "Spark inventory is older than the Run/Switch freshness policy.",
                        scope="freshness",
                        node_ids=(item.node_id,),
                        stale=True,
                    )
                )
            agent = session.get(AgentNode, item.node_id)
            if agent is None or agent.state != "active" or agent.revoked_at is not None:
                node_blockers.append(
                    _as_reason(
                        RunSwitchCode.SPARK_UNAVAILABLE,
                        "Selected Spark is not active in Controller authority.",
                        scope="node",
                        node_ids=(item.node_id,),
                    )
                )
            if (
                serving
                and snapshot is not None
                and IMAGE_PULL_CAPABILITY not in snapshot.capabilities
            ):
                node_blockers.append(
                    _as_reason(
                        RunSwitchCode.AGENT_UPGRADE_REQUIRED,
                        AGENT_UPGRADE_REQUIRED_DETAIL,
                        scope="node",
                        node_ids=(item.node_id,),
                    )
                )
            role = role_by_name.get(item.role)
            memory = None if role is None else role.resources.memory
            disk = None if role is None else role.resources.disk
            required_memory: int | None = None
            memory_kind = None
            memory_floor = None
            memory_capacity: int | None = None
            memory_available: int | None = None
            memory_free_after: int | None = None
            memory_usage_uncertainty: MemoryUsageUncertainty | None = None
            required_disk: int | None = None
            disk_free: int | None = None
            disk_free_after: int | None = None
            demand: ResourceDemand | None = None
            if memory is None or disk is None:
                node_blockers.append(
                    _as_reason(
                        RunSwitchCode.RESOURCE_CONTRACT_INVALID,
                        "The selected recipe role does not contain an exact disk and memory envelope.",
                        scope="recipe",
                        node_ids=(item.node_id,),
                    )
                )
            else:
                if not serving:
                    required_memory = 0
                else:
                    try:
                        memory_need = memory_requirement(
                            revision.document if revision is not None else {},
                            memory,
                            item.role,
                            model_documents,
                            settings=effective_settings,
                            platform_floor_bytes=self._memory_floor,
                        )
                    except (TypeError, ValueError) as error:
                        node_blockers.append(
                            _as_reason(
                                RunSwitchCode.MEMORY_ENVELOPE_INVALID,
                                str(error),
                                scope="recipe",
                                node_ids=(item.node_id,),
                            )
                        )
                    else:
                        demand = memory_need.demand
                        memory_kind = memory_need.kind
                        memory_floor = memory_need.floor_bytes
                        required_memory = demand.total_bytes
                        reservations = memory_reservations(
                            session,
                            item.node_id,
                            memory_pool=snapshot.memory_pool if snapshot else None,
                            excluded_profile_application_ids=excluded_profile_application_ids,
                            excluded_run_ids=excluded_run_ids,
                            observed_at=snapshot.observed_at if snapshot else None,
                        )
                        capacity = memory_capacity_snapshot(
                            item.node_id,
                            memory_need.kind,
                            host=(
                                snapshot.host_memory_total_bytes,
                                snapshot.host_memory_free_bytes,
                            )
                            if snapshot
                            else None,
                            accelerator=(
                                snapshot.gpu_memory_total_bytes,
                                snapshot.gpu_memory_free_bytes,
                            )
                            if snapshot
                            else None,
                            reservations=reservations,
                            memory_pool=snapshot.memory_pool if snapshot else None,
                            evidence_state="fresh"
                            if evidence.state == "fresh"
                            else "unknown",
                            evidence_digest=snapshot.evidence_digest
                            if snapshot
                            else None,
                            evidence_observed_at=snapshot.observed_at
                            if snapshot
                            else None,
                        )
                        capacity_totals = [
                            part.available_bytes for part in capacity.components
                        ]
                        known_capacity_totals = [
                            value for value in capacity_totals if type(value) is int
                        ]
                        memory_capacity = (
                            min(known_capacity_totals)
                            if capacity_totals
                            and len(known_capacity_totals) == len(capacity_totals)
                            else capacity.available_bytes
                        )
                        if (
                            capacity.available_bytes is not None
                            and capacity.occupied_bytes is not None
                        ):
                            memory_available = (
                                capacity.available_bytes - capacity.occupied_bytes
                            )
                        fit_capacity = plan_capacity(
                            {item.node_id: demand},
                            [capacity],
                            memory_floor_bytes=memory_need.floor_bytes,
                        )
                        fit_node = fit_capacity.nodes[0]
                        memory_free_after = fit_node.selected_free_after_bytes
                        if fit_node.unknown_run_residuals:
                            if (
                                snapshot is not None
                                and evidence.observed_at is not None
                                and evidence.evidence_digest is not None
                            ):
                                residual_ranges = []
                                for residual in sorted(
                                    fit_node.unknown_run_residuals,
                                    key=lambda value: (
                                        value.run_id,
                                        value.run_generation,
                                        value.reservation_kind,
                                    ),
                                ):
                                    if not _is_memory_reservation_kind(
                                        residual.reservation_kind
                                    ):
                                        # A claim of a kind this contract cannot
                                        # name is left out of the typed range and
                                        # recorded; the fit above already counted
                                        # it, so the plan stays conservative.
                                        retire_as_unknown(
                                            "run-switch.memory-claim",
                                            residual.run_id,
                                            BookkeepingReason.PERSISTED_STATE_DAMAGED,
                                            "an active run memory claim has an "
                                            "invalid kind",
                                        )
                                        continue
                                    residual_ranges.append(
                                        RunMemoryResidualRange(
                                            run_id=residual.run_id,
                                            run_generation=residual.run_generation,
                                            reservation_kind=residual.reservation_kind,
                                            maximum_bytes=residual.maximum_bytes,
                                        )
                                    )
                                memory_usage_uncertainty = MemoryUsageUncertainty(
                                    source="aggregate_inventory_without_run_usage",
                                    inventory_observed_at=evidence.observed_at,
                                    inventory_evidence_digest=evidence.evidence_digest,
                                    residual_ranges=residual_ranges,
                                )
                            # An exact numeric headroom would imply measured
                            # per-run usage. The typed range and fit decision
                            # carry the conservative bound instead.
                            memory_free_after = None
                        if fit_node.insufficient_components:
                            insufficient_components_by_node[item.node_id] = frozenset(
                                "shared"
                                if snapshot is not None
                                and snapshot.memory_pool == "shared"
                                else component
                                for component in fit_node.insufficient_components
                            )
                        for reason in fit_node.reasons:
                            projected = _resource_reason(
                                reason, node_ids=(item.node_id,)
                            )
                            if projected.severity == "warning":
                                node_warnings.append(projected)
                            else:
                                node_blockers.append(projected)
                disk_need = None
                try:
                    image_size = (
                        image_bytes if image_bytes is not None else disk.image_bytes
                    )
                    artifact_size = (
                        artifact_bytes
                        if artifact_bytes is not None
                        else disk.artifact_bytes
                    )
                    # An unstated payload size leaves the envelope incomplete.
                    if image_size is not None and artifact_size is not None:
                        if recipe_models and recipe_models <= models_stored_on_node(
                            session, item.node_id
                        ):
                            # The Spark's shared store already holds every model of
                            # the recipe (an installation of it exists there), so
                            # the install links those files and writes none.
                            artifact_size = 0
                        disk_need = installation_disk_requirement(
                            disk,
                            required_download_bytes=image_size + artifact_size,
                            minimum_floor_bytes=(
                                self._lifecycle._install_admission._disk_floor
                                if self._lifecycle is not None
                                else 0
                            ),
                        )
                except (TypeError, ValueError):
                    disk_need = None
                if disk_need is None:
                    node_blockers.append(
                        _as_reason(
                            RunSwitchCode.DISK_ENVELOPE_INVALID,
                            "The recipe disk envelope is incomplete.",
                            scope="recipe",
                            node_ids=(item.node_id,),
                        )
                    )
                else:
                    # Review promises a full allocation, including headroom,
                    # less the model files the Spark's store already holds.
                    # Installation may reduce it for exact target-local reuse;
                    # it must never grow a claim after operator acceptance.
                    required_disk = disk_need.required_bytes + disk_need.floor_bytes
                    disk_free = (
                        snapshot.disk_free_bytes if snapshot is not None else None
                    )
                    if snapshot is not None and disk_free is not None:
                        charges = outstanding_disk_charges(
                            session,
                            item.node_id,
                            inventory_observed_at=snapshot.observed_at,
                            excluded_run_ids=excluded,
                            excluded_profile_application_ids=excluded_profile_application_ids,
                            # This attempt adopts the unowned plan a failed
                            # attempt of the same recipe left on these Sparks,
                            # so its claim is not capacity to wait for.
                            excluded_installation_ids=adoptable,
                        )
                        reserved_disk = sum(charge.amount_bytes for charge in charges)
                        disk_free_after = disk_free - reserved_disk - required_disk
                        if disk_free_after < 0:
                            holders = describe_disk_charges(session, charges)
                            # Cleanup is space-driven: unused installations the
                            # collector may remove cover a shortfall, so the
                            # review plans that eviction (the load waits for it)
                            # and refuses only for what nothing can free.
                            capacity = spark_eviction_capacity(
                                session, item.node_id, now
                            )
                            evictable, kept_sentence = (
                                capacity.freeable,
                                capacity.kept,
                            )
                            if evictable >= -disk_free_after:
                                node_warnings.append(
                                    _as_reason(
                                        RunSwitchCode.DISK_EVICTION_PLANNED,
                                        f"The operation needs {required_disk} bytes and "
                                        f"{-disk_free_after} more must be freed on "
                                        "this Spark: "
                                        + capacity.plan(-disk_free_after)
                                        + ", least recently used first. Models stay "
                                        "on the NAS, so a later load reinstalls.",
                                        scope="node",
                                        node_ids=(item.node_id,),
                                        severity="warning",
                                    )
                                )
                            else:
                                node_blockers.append(
                                    _as_reason(
                                        RunSwitchCode.INSUFFICIENT_DISK,
                                        f"The operation needs {required_disk} bytes and would leave {disk_free_after} bytes."
                                        + (f" Disk is {holders}." if holders else "")
                                        + f" Only {evictable} bytes of unused installations can be removed. "
                                        + kept_sentence,
                                        scope="node",
                                        node_ids=(item.node_id,),
                                    )
                                )
            nodes.append(
                SparkFitNode(
                    node_id=item.node_id,
                    rank=item.rank,
                    role=item.role,
                    allowed=not node_blockers,
                    ports_required=ports_required,
                    disk_required_bytes=required_disk,
                    disk_free_bytes=disk_free,
                    disk_free_after_bytes=disk_free_after,
                    memory_required_bytes=required_memory,
                    memory_kind=memory_kind,
                    memory_pool=snapshot.memory_pool if snapshot else None,
                    memory_floor_bytes=memory_floor,
                    memory_capacity_bytes=memory_capacity,
                    memory_available_bytes=memory_available,
                    memory_free_after_bytes=memory_free_after,
                    memory_usage_uncertainty=memory_usage_uncertainty,
                    resource_demand=(
                        ResourceDemandEvidence(
                            weights_bytes=demand.weights_bytes,
                            runtime_overhead_bytes=demand.runtime_overhead_bytes,
                            context_bytes=demand.context_bytes,
                            concurrency_bytes=demand.concurrency_bytes,
                            batch_bytes=demand.batch_bytes,
                            total_bytes=demand.total_bytes,
                            evidence_state=demand.evidence_state,
                            evidence_digest=_resource_evidence_digest(
                                revision_digest,
                            ),
                        )
                        if demand is not None
                        else None
                    ),
                    blockers=node_blockers,
                    warnings=node_warnings,
                )
            )
            blockers.extend(node_blockers)
            warnings.extend(node_warnings)
        return (
            freshness,
            SparkFit(
                allowed=not blockers,
                nodes=nodes,
                blockers=blockers,
                warnings=warnings,
            ),
            blockers,
            warnings,
            insufficient_components_by_node,
        )

    def _active_reservation_bytes(
        self,
        session: Session,
        node_id: str,
        kind: str | None,
        excluded_run_ids: set[str],
        *,
        excluded_profile_application_ids: tuple[str, ...] = (),
    ) -> int:
        if kind is None:
            return 0
        reservations = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.node_id == node_id,
                    ResourceReservation.kind == kind,
                    ResourceReservation.state == ReservationState.ACTIVE,
                    reservation_visible(excluded_profile_application_ids),
                )
            )
        )
        total = 0
        for reservation in reservations:
            if (
                reservation.owner_kind == "run"
                and reservation.owner_id in excluded_run_ids
            ):
                continue
            total += reservation.amount_bytes
        return total

    def _inspect_artifacts(
        self,
        session: Session,
        model_digest: str | None,
        revision_id: str | None,
        group: SparkGroup,
        *,
        retention: str,
        now: datetime,
    ) -> ArtifactInspection:
        if model_digest is None or revision_id is None:
            return ArtifactInspection(
                required_bytes=None,
                reused_bytes=0,
                copied_bytes=0,
                missing_nas_bytes=None,
                missing_spark_bytes=None,
                reclaimable_bytes=0,
                nas_coverage="unknown",
                spark_coverage="unknown",
                artifact_digests=(),
                reclaimable_digests=(),
                blockers=(
                    _as_reason(
                        RunSwitchCode.ARTIFACT_IDENTITY_UNKNOWN,
                        "The exact model artifact identity is unavailable.",
                        scope="artifact",
                    ),
                ),
            )
        try:
            inspection = self._artifacts.inspect(
                session,
                model_content_sha256=model_digest,
                recipe_revision_id=revision_id,
                node_ids=tuple(node.node_id for node in group.nodes),
                retention=retention,
                now=now,
            )
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
            return _inspection_unavailable(str(error))
        if (
            inspection.missing_spark_bytes not in (None, 0)
            and inspection.nas_coverage == "unknown"
        ):
            inspection = ArtifactInspection(
                required_bytes=inspection.required_bytes,
                reused_bytes=inspection.reused_bytes,
                copied_bytes=inspection.copied_bytes,
                missing_nas_bytes=inspection.missing_nas_bytes,
                missing_spark_bytes=inspection.missing_spark_bytes,
                missing_spark_bytes_by_node=inspection.missing_spark_bytes_by_node,
                reclaimable_bytes=inspection.reclaimable_bytes,
                nas_coverage=inspection.nas_coverage,
                spark_coverage=inspection.spark_coverage,
                artifact_digests=inspection.artifact_digests,
                reclaimable_digests=inspection.reclaimable_digests,
                artifact_set_sha256=inspection.artifact_set_sha256,
                artifact_set_bytes=inspection.artifact_set_bytes,
                dependency_model_content_sha256=inspection.dependency_model_content_sha256,
                freshness=inspection.freshness,
                blockers=(
                    *inspection.blockers,
                    _as_reason(
                        RunSwitchCode.NAS_COVERAGE_UNKNOWN,
                        "Spark copies are missing but NAS coverage is unknown; the Controller cannot promise a reusable source.",
                        scope="artifact",
                    ),
                ),
                warnings=inspection.warnings,
            )
        if (
            inspection.required_bytes is None
            or inspection.required_bytes < 1
            or not inspection.artifact_digests
            or not _is_hex_digest(inspection.artifact_set_sha256)
            or any(not _is_hex_digest(value) for value in inspection.artifact_digests)
        ):
            inspection = replace(
                inspection,
                blockers=(
                    *inspection.blockers,
                    _as_reason(
                        RunSwitchCode.ARTIFACT_MANIFEST_UNKNOWN,
                        "The authoritative complete model artifact set and byte manifest are unavailable.",
                        scope="artifact",
                    ),
                ),
            )
        return inspection

    @staticmethod
    def _storage(
        inspection: ArtifactInspection, *, retention: RunSwitchRetention
    ) -> ArtifactStorageImpact:
        return ArtifactStorageImpact(
            artifact_set_sha256=inspection.artifact_set_sha256,
            artifact_set_bytes=inspection.artifact_set_bytes,
            required_bytes=inspection.required_bytes,
            reused_bytes=inspection.reused_bytes,
            copied_bytes=inspection.copied_bytes,
            missing_nas_bytes=inspection.missing_nas_bytes,
            missing_spark_bytes=inspection.missing_spark_bytes,
            missing_spark_bytes_by_node=(
                dict(inspection.missing_spark_bytes_by_node)
                if inspection.missing_spark_bytes_by_node is not None
                else None
            ),
            reclaimable_bytes=inspection.reclaimable_bytes,
            reclaimed_bytes=(
                inspection.reclaimable_bytes
                if retention == "reclaim-unreferenced"
                else 0
            ),
            nas_coverage=inspection.nas_coverage,
            spark_coverage=inspection.spark_coverage,
            retention=retention,
            artifact_digests=list(inspection.artifact_digests),
            reclaimable_digests=list(inspection.reclaimable_digests),
        )

    def _phases(
        self,
        *,
        action: RunSwitchAction,
        group: SparkGroup,
        phase_node_ids: Sequence[str] | None = None,
        installation_id: str | None,
        installation_state: str | None,
        stops: Sequence[StopImpact],
        inspection: ArtifactInspection,
        partial_stop: bool = False,
        runtime_storage: RuntimeImageStorageImpact | None,
        retention: RunSwitchRetention,
        blockers: Sequence[RunSwitchReason],
        stop_before_transfer: bool,
        stop_before_prepare: bool,
        build_required: bool = False,
        build_on_target: bool = False,
        cleanup_disposition: Literal["uninstall", "abandon"] = "uninstall",
        cleanup_mode: Literal["uninstall", "reconcile"] = "uninstall",
    ) -> list[RunSwitchPhase]:
        node_ids = list(
            phase_node_ids
            if phase_node_ids is not None
            else (node.node_id for node in group.nodes)
        )
        phases: list[RunSwitchPhase] = []
        if action == "cleanup":
            # Disposal is authorized by the installation's own uninstall
            # assessment, so the phase sequence never consults launch
            # readiness: being unable to start work must not prevent removing
            # work.  A plan that never reached a node has nothing to remove,
            # so its first phase abandons the record instead of uninstalling it.
            phases.append(
                RunSwitchPhase(
                    index=0,
                    kind="uninstall",
                    state="planned" if not blockers else "blocked",
                    node_ids=node_ids,
                    detail=(
                        "Abandon the persisted plan that never reached a node; "
                        "there are no installed bytes to remove."
                        if cleanup_disposition == "abandon"
                        else "Reconcile the exact stored installation effects from "
                        "successful install provenance while preserving shared caches."
                        if cleanup_mode == "reconcile"
                        else "Remove the installation that is no longer desired, "
                        "scoped to its authorized membership."
                    ),
                )
            )
            phases.append(
                RunSwitchPhase(
                    index=len(phases),
                    kind="final_verify",
                    state="planned" if not blockers else "blocked",
                    node_ids=node_ids,
                    detail=(
                        "Verify exact reconciliation receipts and retained reservations "
                        "for every rank before final release."
                        if cleanup_mode == "reconcile"
                        else "Verify the installation and its runtime are removed."
                    ),
                )
            )
            return phases
        if action == "stop":
            if stops:
                phases.append(
                    RunSwitchPhase(
                        index=0,
                        kind="stop",
                        state="planned" if not blockers else "blocked",
                        node_ids=node_ids,
                        detail=(
                            "Stop the selected workload on the reachable Sparks; "
                            "the profile will report the missing ranks separately."
                            if partial_stop
                            else "Stop the selected workload as one complete group."
                        ),
                    )
                )
            phases.append(
                RunSwitchPhase(
                    index=len(phases),
                    kind="final_verify",
                    state="planned" if not blockers else "blocked",
                    node_ids=node_ids,
                    detail=(
                        "Verify that reachable ranks are stopped and the model route "
                        "is withdrawn."
                        if partial_stop
                        else "Verify that the selected workload is stopped and its route is withdrawn."
                    ),
                )
            )
            return phases

        needs_model_download = inspection.missing_nas_bytes not in (None, 0)
        needs_target_copy = (
            installation_id is None
            or inspection.missing_spark_bytes not in (None, 0)
            or (
                runtime_storage is not None
                and runtime_storage.missing_image_distribution_bytes not in (None, 0)
            )
        )
        # Image preparation is a Controller-side phase.  It must complete
        # before install admission compiles and persists the schema-2 agent
        # payload, even when the selected Spark already has a copy.  A pending
        # source build has no image identity at preview time, so it is also an
        # explicit preparation input for the same durable phase sequence.
        needs_runtime_image_prepare = (
            build_required
            or (runtime_storage is not None and runtime_storage.preparation_required)
            or (
                runtime_storage is not None
                and runtime_storage.missing_nas_bytes not in (None, 0)
            )
            or (
                runtime_storage is not None
                and runtime_storage.missing_image_distribution_bytes not in (None, 0)
            )
            or (
                runtime_storage is not None
                and runtime_storage.image_digest is not None
                and runtime_storage.oci_layout_sha256 is None
            )
        )
        needs_prepare = (
            installation_id is None or installation_state != InstallationState.INSTALLED
        )
        needs_cleanup = retention == "reclaim-unreferenced" and (
            inspection.reclaimable_bytes > 0
            or (runtime_storage is not None and runtime_storage.reclaimable_bytes > 0)
        )
        stop_added = False

        def add(
            kind: RunSwitchPhaseKind,
            detail: str,
            *,
            subphase: RunSwitchSubphase | None = None,
        ) -> None:
            phases.append(
                RunSwitchPhase(
                    index=len(phases),
                    kind=kind,
                    subphase=subphase,
                    state="blocked"
                    if blockers and kind in {"prepare", "start", "final_verify"}
                    else "planned",
                    node_ids=node_ids,
                    detail=detail,
                )
            )

        # Only a Spark build inside the group needs the memory the old
        # workload holds. Controller image preparation, transfer and install
        # run beside it, so without such a build the old workload keeps
        # serving until the one stop right before start (whose post-stop
        # memory check still guards the start).
        stop_for_build = stop_before_prepare and build_required and build_on_target
        if build_required and not build_on_target:
            # Controller build output is an input to transfer.  Keep it ahead
            # of target disk cleanup/stop because this builder is outside the
            # selected inference group.
            add(
                "prepare",
                "Build the exact linux-arm64 runtime container in Controller storage before target transfer.",
                subphase="container-build",
            )
        if stops and stop_before_transfer:
            add("stop", "Stop conflicting workloads before disk capacity is consumed.")
            stop_added = True
        if build_required and build_on_target and not stop_before_prepare:
            add(
                "prepare",
                "Build the exact linux-arm64 runtime container in Controller storage before target transfer.",
                subphase="container-build",
            )
        if needs_model_download:
            add(
                "transfer",
                "Download the exact model artifact set into Controller/NAS cache before Spark distribution.",
                subphase="model-download",
            )
        if stops and stop_for_build and not stop_added:
            add(
                "stop",
                "Stop conflicting workloads before the Spark build needs their memory.",
            )
            stop_added = True
        if stop_for_build:
            # A build outside the group was already placed first above.
            # This remains a ``prepare`` phase for the shared lifecycle
            # vocabulary; the subphase makes Controller OCI preparation
            # explicit and gives clients a stable progress label.
            add(
                "prepare",
                "Build the exact linux-arm64 runtime container in Controller storage before target transfer.",
                subphase="container-build",
            )
        if needs_runtime_image_prepare:
            add(
                "prepare",
                "Prepare and verify the exact Controller OCI runtime image before install admission.",
                subphase="runtime-image",
            )
        if needs_prepare:
            add(
                "prepare",
                "Compile and persist the exact schema-2 launch plan before target transfer.",
                subphase="runtime-plan",
            )
        if needs_target_copy:
            add(
                "transfer",
                "Copy missing model artifacts and runtime images after the verified schema-2 payload is persisted.",
                subphase="target-copy",
            )
            add(
                "verify",
                "Verify every transferred artifact against its immutable model set and OCI identity.",
                subphase="target-copy",
            )
        if needs_prepare:
            add(
                "prepare",
                "Start the prepared installation after target copy and verification complete.",
                subphase="runtime-install",
            )
        if needs_cleanup:
            add(
                "cleanup",
                "Reclaim only unreferenced Spark-local artifacts under the selected retention policy.",
            )
        if stops and not stop_added:
            add(
                "stop", "Stop conflicting workloads as one complete group before start."
            )
        if action in {"run", "switch"}:
            add("start", "Start the selected exact model and recipe outcome.")
        add(
            "final_verify",
            "Verify the intended final observed state for every affected rank.",
        )
        return phases

    def _mapping_selection(
        self,
        mapping: ClusterMapping | None,
        nodes: Sequence[ClusterMappingNode],
    ) -> MappingSelection | None:
        if mapping is None:
            return None
        return MappingSelection(
            mapping_id=mapping.id,
            mapping_generation=mapping.generation,
            topology_name=mapping.topology_name,
            parameters=validate_mapping_parameters(mapping.parameters),
            option_choices=mapping_option_choices(mapping.parameters),
            placement_digest=mapping.placement_digest,
            action="reuse",
            nodes=[
                SparkGroupNode(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    endpoint_owner=node.endpoint_owner,
                )
                for node in nodes
            ],
        )

    def _stop_digest(
        self,
        run_id: str,
        *,
        target_node_ids: Sequence[str] | None = None,
    ) -> str | None:
        if self._lifecycle is None:
            return None
        try:
            plan = self._lifecycle.preview_stop(
                run_id, profile_target_node_ids=target_node_ids
            )
            return plan.plan_digest if plan.allowed else None
        except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError):
            return None

    @staticmethod
    def _run_reserved_bytes(
        session: Session, run_id: str, *, node_ids: Sequence[str] | None = None
    ) -> int:
        statement = select(
            func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)
        ).where(
            ResourceReservation.owner_kind == "run",
            ResourceReservation.owner_id == run_id,
            ResourceReservation.state == ReservationState.ACTIVE,
        )
        if node_ids is not None:
            statement = statement.where(ResourceReservation.node_id.in_(node_ids))
        return int(session.scalar(statement) or 0)

    @staticmethod
    def _finalize_plan(data: Mapping[str, object]) -> RunSwitchPlan:
        plan = read_stored_model(RunSwitchPlan, dict(data))
        identity = _plan_identity(plan.model_dump(mode="json"))
        return plan.model_copy(update={"plan_digest": _digest(identity)})

    def _apply_plan(
        self,
        plan: RunSwitchPlan,
        *,
        request_key: str,
        actor: str,
        kind: str,
        workload_intent_ordinal: int | None,
        profile_application_id: str | None = None,
        intent: Mapping[str, object] | None = None,
    ) -> RunSwitchOperation:
        now = _now(self._clock)
        target_node_ids = _plan_target_node_ids(plan)
        total_bytes, member_totals = _planned_transfer_bytes(plan)
        payload = {
            "schema_version": 2,
            "operation_kind": kind,
            "action": plan.action,
            "plan_digest": plan.plan_digest,
            "plan": plan.model_dump(mode="json"),
            **({"intent": dict(intent)} if intent is not None else {}),
            "progress": {
                **(
                    {"profile_application_id": profile_application_id}
                    if profile_application_id is not None
                    else {}
                ),
                "phase_index": 0,
                "item_index": 0,
                "phase": (plan.phases[0].kind if plan.phases else "final_verify"),
                "subphase": (plan.phases[0].subphase if plan.phases else None),
                "completed_phases": [],
                "child_operation_id": None,
                "phase_results": [],
                "completed_bytes": 0,
                "total_bytes": total_bytes,
                "total_bytes_known": total_bytes is not None,
                "members": [
                    {
                        "node_id": node.node_id,
                        "phase": plan.phases[0].kind if plan.phases else None,
                        "state": "pending",
                        "completed_bytes": 0,
                        "total_bytes": member_totals.get(node.node_id),
                        "error": None,
                    }
                    for node in plan.spark_group.nodes
                    if node.node_id in target_node_ids
                ],
            },
        }
        with self._sessions.begin() as session:
            nodes = list(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(target_node_ids))
                    .order_by(AgentNode.node_id)
                    .with_for_update()
                )
            )
            if len(nodes) != len(target_node_ids) or (
                plan.profile_stop_scope is not None
                and any(
                    node.revoked_at is not None or node.state != "active"
                    for node in nodes
                )
            ):
                raise RunSwitchRequestInvalid(
                    "run-switch target Spark scope changed",
                    reason=InvalidRequestReason.CONFLICT,
                )
            existing = session.scalar(select(Job).where(Job.request_id == request_key))
            if existing is not None:
                if existing.kind != kind or not _same_intent(existing, intent):
                    raise RunSwitchRequestInvalid(
                        RunSwitchCode.REQUEST_KEY_REUSED_DIFFERENTLY,
                        reason=InvalidRequestReason.CONFLICT,
                    )
                return self._operation_view(existing)
            _reserve_run_switch_assets(session, plan, now=now)
            try:
                lock_run_switch_build_dependency(session, plan)
            except BuildConsumerError as error:
                raise RunSwitchRequestInvalid(f"{error.code}: {error}") from error
            if workload_intent_ordinal is None:
                workload_intent_ordinal = (
                    max((node.workload_intent_ordinal for node in nodes), default=0) + 1
                )
                for node in nodes:
                    node.workload_intent_ordinal = workload_intent_ordinal
                self.request_superseded_workload_cancellation_in_session(
                    session,
                    tuple(node.node_id for node in nodes),
                    workload_intent_ordinal,
                    now,
                )
            elif (
                type(workload_intent_ordinal) is not int
                or workload_intent_ordinal < 1
                or any(
                    node.workload_intent_ordinal != workload_intent_ordinal
                    for node in nodes
                )
            ):
                raise RunSwitchRequestInvalid(
                    f"{RunSwitchCode.SUPERSEDED}: Spark scope has a later workload intent",
                    reason=InvalidRequestReason.SUPERSEDED,
                )
            payload["workload_intent_ordinal"] = workload_intent_ordinal
            payload["progress"]["workload_intent_ordinal"] = workload_intent_ordinal
            job = _ADAPTER.new_operation(
                allowed=plan.allowed,
                id=str(uuid.uuid4()),
                request_id=request_key,
                kind=kind,
                actor=actor,
                authority_revision=(plan.recipe_content_sha256 or plan.plan_digest),
                targets=list(target_node_ids),
                payload_digest=_digest(payload),
                payload=payload,
                result=_persisted_result(_read_progress(payload["progress"])),
                created_at=now,
                updated_at=now,
            )
            if not plan.allowed:
                progress = _read_progress(payload["progress"])
                blocked = "; ".join(reason.code for reason in plan.blockers[:8])
                _ADAPTER.retry(
                    job,
                    progress,
                    blocked,
                    now,
                    visible=_OBSERVING,
                    record_reason=False,
                    describe=lambda due: (
                        f"{blocked}; next re-plan at {due.isoformat()}"
                    ),
                )
                job.result = _persisted_result(progress)
            session.add(job)
            session.flush()
            return self._operation_view(job)

    def request_superseded_workload_cancellation_in_session(
        self,
        session: Session,
        targets: Sequence[str],
        ordinal: int,
        now: datetime,
    ) -> None:
        """Cancel exact older agent orders in the same ordinal-admission transaction."""

        AgentJobService.request_superseded_workload_cancellation_in_session(
            session, targets, ordinal, now
        )

    def _existing_request_operation(
        self,
        request_key: str,
        *,
        kind: str,
        intent: Mapping[str, object],
    ) -> RunSwitchOperation | None:
        """Replay a durable operation before re-planning mutable evidence.

        A client may retry after losing the initial response.  Looking up the
        request key first keeps that retry idempotent even if inventory or
        workload state has changed since the original preview.
        """

        with self._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_key))
            if existing is None:
                return None
            if existing.kind != kind or not _same_intent(existing, intent):
                raise RunSwitchRequestInvalid(
                    RunSwitchCode.REQUEST_KEY_REUSED_DIFFERENTLY,
                    reason=InvalidRequestReason.CONFLICT,
                )
            return self._operation_view(existing)

    def _refresh_blocked_plan(self, operation_id: str, now: datetime) -> bool:
        """Re-plan accepted intent when its current admission evidence was blocked."""
        with self._sessions() as session:
            row = session.get(Job, operation_id)
            if row is None or row.kind not in _OPERATION_KINDS:
                return False
            plan = _stored_job_plan(row)
            if _progress_damaged(row.result):
                # Unreadable evidence of what was issued is never re-planned
                # from the accepted payload: a Start may already exist.
                return False
            persisted_progress = _read_progress(row.result)
            if (
                plan is not None
                and plan.allowed
                and not persisted_progress.force_replan
            ):
                return False
            # Cancellation and newer intent are settled before any re-plan.
            if persisted_progress.cancellation or (
                self._scope_intent_status(session, row) != "current"
            ):
                return False
            due = persisted_progress.observation_due_at
            if due is not None and now < _aware(due):
                return False
            parent = _run_switch_payload(row)
            intent = parent.intent if parent is not None else None
            if intent is None:
                return False
            actor = row.actor
            profile_application_id = _string_or_none(
                persisted_progress.profile_application_id
            )

        try:
            if isinstance(intent, RunSwitchRunIntent):
                refreshed = self.preview(
                    intent.request,
                    actor=actor,
                    profile_application_id=profile_application_id,
                )
            elif isinstance(intent, RunSwitchStopIntent):
                refreshed = self.preview_stop(intent, actor=actor)
            elif isinstance(intent, RunSwitchCleanupIntent):
                refreshed = self.preview_cleanup(intent, actor=actor)
            elif isinstance(intent, RunSwitchProfileStopIntent):
                refreshed = self.preview_stop(
                    intent.run_id,
                    actor=actor,
                    profile_stop_scope=intent.profile_stop_scope,
                )
            else:
                # An intent this plan cannot be refreshed from is read as no
                # refreshed plan; the waiting operation backs off below.
                refreshed = None
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            if is_security_failure(error_code(error)):
                with self._sessions.begin() as session:
                    current = session.get(Job, operation_id, with_for_update=True)
                    if current is not None:
                        self._mark_failed(current, str(error), now=now)
                return True
            refreshed = None

        with self._sessions.begin() as session:
            current = session.get(Job, operation_id, with_for_update=True)
            if current is None or current.state not in job_states.words(
                LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.OBSERVING
            ):
                return True
            current_plan = _stored_job_plan(current)
            if _progress_damaged(current.result):
                return False
            progress = _read_progress(current.result)
            if (
                current_plan is not None
                and current_plan.allowed
                and not progress.force_replan
            ):
                return False
            if progress.cancellation or (
                self._scope_intent_status(session, current) != "current"
            ):
                return False
            # Target membership is fenced by the accepted workload ordinal on
            # exactly these Sparks. A refreshed plan naming other Sparks is not
            # this intent; wait (with backoff) until the plan matches again or a
            # newer request supersedes it.
            targets_changed = (refreshed is not None and refreshed.allowed) and sorted(
                _plan_target_node_ids(refreshed)
            ) != sorted(current.targets)
            if refreshed is not None and refreshed.allowed and not targets_changed:
                current_parent = _run_switch_payload(current)
                if current_parent is None:
                    return False
                current.payload = serialize_json_value(
                    current_parent.model_copy(
                        update={"plan": refreshed, "plan_digest": refreshed.plan_digest}
                    )
                )
                current.authority_revision = (
                    refreshed.recipe_content_sha256 or refreshed.plan_digest
                )
                total_bytes, _ = _planned_transfer_bytes(refreshed)
                # Replanned phases are idempotent, and even an unchanged shape
                # does not prove the old phase remains valid under new authority.
                # The new plan starts with no results from the old one and a new
                # child identity generation, so no installation, run, or adopted
                # Start of the old plan can be attributed to the new plan.
                phase_index = 0
                progress.retry_attempt = None
                progress = progress.model_copy(
                    update={
                        "phase_index": phase_index,
                        "item_index": 0,
                        "completed_phases": [],
                        "phase_results": [],
                        "child_operation_id": None,
                        "phase_retry_generation": require_integer(
                            progress.phase_retry_generation or 0,
                            "phase retry generation",
                        )
                        + 1,
                        "phase": refreshed.phases[phase_index].kind
                        if refreshed.phases
                        else "final_verify",
                        "subphase": refreshed.phases[phase_index].subphase
                        if refreshed.phases
                        else None,
                        "total_bytes": total_bytes,
                        "total_bytes_known": total_bytes is not None,
                        "observation_due_at": None,
                        "force_replan": False,
                    }
                )
                # The last refusal stays on record: if a fresh plan meets the
                # same refusal again, ``_fail`` retries in place and counts it.
                progress.failed_phase = None
                _ADAPTER.project(
                    current, progress, now, state=_LifecycleState.QUEUED, reason=None
                )
            else:
                reasons = (
                    RunSwitchCode.PLAN_TARGETS_CHANGED
                    if targets_changed
                    else "; ".join(reason.code for reason in refreshed.blockers[:8])
                    if refreshed is not None
                    else RunSwitchCode.PLAN_REFRESH_UNAVAILABLE
                )
                _ADAPTER.retry(
                    current,
                    progress,
                    reasons,
                    now,
                    visible=_OBSERVING,
                    record_reason=False,
                    describe=lambda due: (
                        f"{reasons}; next re-plan at {due.isoformat()}"
                    ),
                )
            current.result = _persisted_result(progress)
            current_parent = _run_switch_payload(current)
            if current_parent is None:
                return False
            current.payload = serialize_json_value(
                current_parent.model_copy(update={"progress": progress})
            )
            current.payload_digest = _digest(current.payload)
            current.updated_at = now
        return True

    def _advance(self, operation_id: str) -> bool:
        now = _now(self._clock)
        if self._refresh_blocked_plan(operation_id, now):
            return True
        with self._sessions() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or job.kind not in _OPERATION_KINDS:
                return True
            if job.state not in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.OBSERVING,
                LifecycleState.NEEDS_OPERATOR,
            ):
                return False
            plan = _stored_job_plan(job)
            if plan is None:
                _reject_invalid_operation(
                    job, "run-switch persisted plan is invalid", now
                )
                session.commit()
                return True
            if _progress_damaged(job.result):
                # The stored result is the evidence of what was issued; it is
                # retained untouched rather than replaced with a fabricated
                # empty progress document. The operation is retired as unknown.
                _reject_invalid_operation(
                    job, "run-switch persisted progress is invalid", now
                )
                session.commit()
                return True
            progress = _read_progress(job.result)
            intent_status = self._scope_intent_status(session, job)
            if intent_status == "invalid":
                self._mark_failed(
                    job,
                    "run-switch workload authority or Spark scope is invalid",
                    now=now,
                    progress=progress,
                )
                session.commit()
                return True
            if intent_status == "missing-target":
                self._mark_failed(
                    job,
                    "run-switch target node no longer exists; accepted intent is superseded",
                    now=now,
                    progress=progress,
                )
                session.commit()
                return True
            if intent_status == "waiting":
                pending_due = progress.observation_due_at
                if (
                    job.state in job_states.words(LifecycleState.OBSERVING)
                    and pending_due is not None
                    and now < _aware(pending_due)
                ):
                    return False
                _ADAPTER.retry(
                    job,
                    progress,
                    RunSwitchCode.TARGET_NOT_ACTIVE,
                    now,
                    visible=_OBSERVING,
                    record_reason=False,
                    describe=lambda due: (
                        "Waiting for a target Spark to return to active state; "
                        f"next check at {due.isoformat()}"
                    ),
                )
                job.result = _persisted_result(progress)
                job.updated_at = now
                session.commit()
                return True
            if intent_status == "superseded":
                _ADAPTER.cancelled(
                    job,
                    progress,
                    now,
                    reason=(
                        f"{RunSwitchCode.SUPERSEDED}: the logical order was cancelled by "
                        "a later authorized Spark intent; issued effects still "
                        "require their own cancellation receipts"
                    ),
                    effect=_LifecycleEffect.UNKNOWN,
                )
                job.result = _persisted_result(progress)
                job.updated_at = now
                session.commit()
                return True
            if progress.observation_due_at is None and (
                job.state in job_states.words(LifecycleState.NEEDS_OPERATOR)
                or (
                    job.state in job_states.words(LifecycleState.OBSERVING)
                    and not progress.child_operation_id
                    and progress.phase != "final_verify"
                )
            ):
                # A wait nothing will ever end: a legacy ``waiting-for-operator``
                # (no Run/Switch action exists) or a ``waiting`` with no clock.
                # Rules 1 and 3: it is retried, never left for a person.
                _ADAPTER.heal(job, progress, now)
                job.result = _persisted_result(progress)
                session.commit()
                return True
            observation_due = progress.observation_due_at
            if (
                observation_due is not None
                and now < _aware(observation_due)
                # A cancel in flight looks at its child on every pass, so it ends
                # as soon as the child does; only the stop attempts are spaced
                # (by the core), never the observation.
                and not progress.cancellation
            ):
                return False
            raw_phase_index = progress.phase_index
            raw_item_index = progress.item_index
            child_id = progress.child_operation_id
            if job.state in job_states.words(
                LifecycleState.OBSERVING, LifecycleState.NEEDS_OPERATOR
            ):
                if not child_id:
                    # Final verification observes an existing run and route;
                    # reopening this checkpoint cannot issue a new workload.
                    # A due automatic wait (an inactive target Spark that has
                    # returned, a background preparation, a backoff) resumes
                    # the exact checkpoint: no child is outstanding, and the
                    # phase key is unchanged, so re-entry is idempotent.
                    # Newer intent was checked above, and the persisted start
                    # deadline remains immutable.
                    if progress.phase != "final_verify" and not (
                        job.state in job_states.words(LifecycleState.OBSERVING)
                        and observation_due is not None
                    ):
                        return False
                    _ADAPTER.project(
                        job,
                        None,
                        now,
                        state=_LifecycleState.RUNNING,
                        reason=_KEEP_REASON
                        if progress.phase == "final_verify"
                        else None,
                    )
                    session.commit()
                else:
                    # Only observe the already-issued child. No new effect is
                    # authorized by reopening this parent's observation checkpoint.
                    _ADAPTER.project(job, None, now, state=_LifecycleState.RUNNING)
                    session.commit()
            if progress.cancellation and child_id is None:
                _complete_cancellation(job, progress, now)
                session.commit()
                return True
            expected_image = None
            profile_application_id = _string_or_none(progress.profile_application_id)
            if (
                profile_application_id is not None
                and plan.recipe_revision_id is not None
                and plan.action != "stop"
            ):
                try:
                    expected_image = accepted_profile_runtime_image(
                        session,
                        profile_application_id,
                        plan.recipe_revision_id,
                        _plan_target_node_ids(plan),
                    )
                except ValueError as error:
                    self._mark_failed(job, str(error), now=now, progress=progress)
                    session.commit()
                    return True
            if (
                type(raw_phase_index) is not int
                or type(raw_item_index) is not int
                or raw_phase_index < 0
                or raw_item_index < 0
            ):
                _ADAPTER.reject(job, "run-switch persisted progress is invalid", now)
                session.commit()
                return True
            # Declare the validated integers so the checkpoint closure below
            # carries their exact type rather than the raw JSON union.
            phase_index: int = raw_phase_index
            item_index: int = raw_item_index
            if phase_index >= len(plan.phases):
                progress = _complete_operation_progress(plan, progress)
                _ADAPTER.succeed(job, progress, now)
                job.result = _persisted_result(progress)
                job.updated_at = now
                session.commit()
                return True
            checkpoint_actor = job.actor
            checkpoint_request_key = job.request_id

        checkpoint_plan = plan
        checkpoint_cancellation = progress.cancellation
        checkpoint_phase = plan.phases[phase_index]
        is_build_checkpoint = (
            checkpoint_phase.kind,
            checkpoint_phase.subphase,
            checkpoint_phase.state,
        ) == ("prepare", "container-build", "planned")
        checkpoint_ordinal = (
            _bound_workload_intent(progress) if is_build_checkpoint else 0
        )

        def checkpoint_job(session: Session) -> Job | None:
            if not is_build_checkpoint:
                return session.get(Job, operation_id, with_for_update=True)
            try:
                return _lock_current_build_parent(
                    session,
                    plan=checkpoint_plan,
                    phase_index=phase_index,
                    item_index=item_index,
                    actor=checkpoint_actor,
                    request_key=checkpoint_request_key,
                    ordinal=checkpoint_ordinal,
                    child_id=child_id,
                    cancellation=checkpoint_cancellation,
                )
            except (_RunSwitchBuildParentChanged, RecipeBuildAdmissionBusy):
                return None

        def fail(
            reason: str,
            *,
            failure_code: str | None = None,
            replan: bool = False,
            clear_child: bool = False,
            definite: bool = False,
        ) -> None:
            self._fail(
                operation_id,
                reason,
                definite=definite,
                replan=replan,
                checkpoint=(phase_index, item_index, child_id),
                failure_code=failure_code,
                checkpoint_guard=checkpoint_job,
                clear_child=clear_child,
            )

        if child_id is not None:
            if not isinstance(child_id, str):
                # Bookkeeping: the child is looked up again by the phase's
                # deterministic identity, which reconciles what it issued.
                fail("run-switch child operation identity is invalid", clear_child=True)
                return True
            try:
                child = self._get_child_operation(child_id)
            except KeyError:
                fail("run-switch child operation disappeared", clear_child=True)
                return True
            if child is None:
                fail("run-switch child operation disappeared", clear_child=True)
                return True
            if child.state in {"queued", "running"}:
                child_progress = _child_progress_payload(child)
                with self._sessions.begin() as session:
                    job = checkpoint_job(session)
                    if job is None:
                        return False
                    original = _read_progress(job.result)
                    progress = original.model_copy(deep=True)
                    if not _checkpoint_matches(
                        job, progress, phase_index, item_index, child_id
                    ):
                        return False
                    persisted_plan = _stored_job_plan(job)
                    if persisted_plan is None:
                        _reject_invalid_operation(
                            job, "run-switch persisted plan is invalid", now
                        )
                        return True
                    persisted_phase_index = require_integer(
                        progress.phase_index, "phase index"
                    )
                    persisted_phase = (
                        persisted_plan.phases[persisted_phase_index]
                        if persisted_phase_index < len(persisted_plan.phases)
                        else persisted_plan.phases[-1]
                    )
                    _merge_progress_evidence(
                        progress,
                        persisted_plan,
                        persisted_phase,
                        child_progress,
                        now,
                    )
                    progress.phase = persisted_phase.kind
                    progress.subphase = persisted_phase.subphase
                    if progress.cancellation:
                        # A cancel is driven, not waited for: the child is stopped
                        # when it can be and observed at the core's bounded rate,
                        # and the cancel ends within the stop budget either way.
                        if self._cancel_with_session(
                            session, job, progress, now, tick=True
                        ):
                            job.result = _persisted_result(progress)
                            job.updated_at = now
                            return True
                        return False
                    retry_due_at = getattr(child, "retry_due_at", None)
                    if child.state == "queued" and isinstance(retry_due_at, datetime):
                        progress.observation_due_at = _aware(retry_due_at)
                    else:
                        progress.observation_due_at = None
                    child_reason = getattr(child, "status_reason", None)
                    status_reason = (
                        child_reason[:512] if isinstance(child_reason, str) else None
                    )
                    if (
                        job.state == "running"
                        and job.status_reason == status_reason
                        and _without_observation_time(progress)
                        == _without_observation_time(original)
                    ):
                        return False
                    _ADAPTER.project(
                        job,
                        progress,
                        now,
                        state=_LifecycleState.RUNNING,
                        reason=status_reason,
                    )
                    job.result = _persisted_result(progress)
                    job.updated_at = now
                return True
            if child.state in _TERMINAL_STATES and child.state != "succeeded":
                with self._sessions.begin() as session:
                    job = checkpoint_job(session)
                    current = (
                        _read_progress(job.result)
                        if job is not None
                        else RunSwitchOperationResult()
                    )
                    if (
                        job is not None
                        and _checkpoint_matches(
                            job, current, phase_index, item_index, child_id
                        )
                        and current.cancellation
                    ):
                        _complete_cancellation(job, current, now)
                        return True
            if (
                child.state in job_states.words(LifecycleState.NEEDS_OPERATOR)
                and checkpoint_cancellation
            ):
                # A cancelled order needs no operator for an idempotent
                # transfer: nothing is running, so close its parked operations
                # (copied bytes stay on the Spark) and finish the cancellation
                # instead of waiting for an owner that has nothing to resume.
                with self._sessions.begin() as session:
                    job = checkpoint_job(session)
                    if job is None:
                        return False
                    current = _read_progress(job.result)
                    if (
                        _checkpoint_matches(
                            job, current, phase_index, item_index, child_id
                        )
                        and current.cancellation
                        and self._abandon_idempotent_child(session, child_id, now)
                    ):
                        _complete_cancellation(job, current, now)
                        return True
            if child.state not in _TERMINAL_STATES or child.state != "succeeded":
                established = _established_start_effect(
                    self._lifecycle, plan.phases[phase_index], child
                )
                if established is not None:
                    # The effect is established under current authority, so the
                    # ordinary success path records the checkpoint the missing
                    # acknowledgement would have produced.
                    child = _EstablishedEffect(child, established)
                elif _start_still_progressing(
                    self._lifecycle, plan.phases[phase_index], child
                ):
                    return self._hold_start_observation(
                        operation_id,
                        child_id=child_id,
                        phase_index=phase_index,
                        item_index=item_index,
                    )
                elif child.state in job_states.words(LifecycleState.NEEDS_OPERATOR):
                    # The lifecycle child owns the uncertain effect. Keep its
                    # exact identity and capacity reservation instead of
                    # turning an agent restart into a terminal parent failure.
                    with self._sessions.begin() as session:
                        job = checkpoint_job(session)
                        if job is None:
                            return False
                        progress = _read_progress(job.result)
                        if not _checkpoint_matches(
                            job, progress, phase_index, item_index, child_id
                        ):
                            return False
                        child_reason = (
                            getattr(child, "status_reason", None)
                            or "Lifecycle effect is uncertain; exact child remains pending"
                        )[:400]
                        _ADAPTER.retry(
                            job,
                            progress,
                            child_reason,
                            now,
                            visible=_OBSERVING,
                            record_reason=False,
                            describe=lambda due: (
                                f"{child_reason}; next exact observation at "
                                f"{due.isoformat()}"
                            ),
                        )
                        job.result = _persisted_result(progress)
                        job.updated_at = now
                    return True
                else:
                    reason = f"run-switch phase operation failed: {child.state if child else 'unknown'}"
                    evidence = _child_progress_payload(child)
                    detail = evidence.reason or evidence.status_reason
                    if isinstance(detail, str) and detail:
                        reason += ": " + detail[:384]
                    kind = _child_failure_kind(child)
                    if classify(kind) is RecoveryDecision.RETRY:
                        with self._sessions.begin() as session:
                            job = checkpoint_job(session)
                            if job is None:
                                return False
                            current = _read_progress(job.result)
                            if not _checkpoint_matches(
                                job, current, phase_index, item_index, child_id
                            ):
                                return False
                            current.child_operation_id = None
                            current.phase_retry_generation = (
                                require_integer(
                                    current.phase_retry_generation or 0,
                                    "phase retry generation",
                                )
                                + 1
                            )
                            self._schedule_checkpoint_retry(job, current, reason, now)
                        return True
                    # An uncertain effect stays attached to its exact child;
                    # only a terminal temporary dependency is safe to resubmit.
                    self._fail(
                        operation_id,
                        reason,
                        checkpoint=(phase_index, item_index, child_id),
                        failure_code=_child_failure_code(evidence),
                        child_evidence=evidence,
                        checkpoint_guard=checkpoint_job,
                    )
                    return True
            with self._sessions.begin() as session:
                job = checkpoint_job(session)
                if job is None:
                    return False
                progress = _read_progress(job.result)
                if not _checkpoint_matches(
                    job, progress, phase_index, item_index, child_id
                ):
                    return False
                progress.observation_due_at = None
                progress.observation_deadline_at = None
                job.status_reason = None
                phase_index = require_integer(progress.phase_index, "phase index")
                item_index = require_integer(progress.item_index, "item index") + 1
                persisted_plan = _stored_job_plan(job)
                if persisted_plan is None:
                    _reject_invalid_operation(
                        job, "run-switch persisted plan is invalid", now
                    )
                    return True
                phase = persisted_plan.phases[phase_index]
                _merge_progress_evidence(
                    progress,
                    persisted_plan,
                    phase,
                    _child_progress_payload(child),
                    now,
                )
                # Preserve terminal child receipts for the following verify
                # phase. Byte/member projection alone cannot prove every
                # model object and the imported OCI identity reached the
                # target; the receipts are the durable handoff across a
                # restart.
                child_result = _progress_mapping(getattr(child, "result", None))
                child_receipts = (
                    child_result.get("evidence") if child_result is not None else None
                )
                if isinstance(child_receipts, list):
                    results = list(progress.phase_results)
                    try:
                        results.extend(
                            _phase_result(receipt, phase=phase)
                            for receipt in child_receipts
                            if isinstance(receipt, Mapping)
                        )
                    except RunSwitchOperationConflict as error:
                        # A receipt that does not validate is an unknown, not a
                        # failure: the idempotent child is issued again under a
                        # new identity and its fresh receipt is validated.
                        self._settle_invalid_receipt(job, error, now, reissue=True)
                        return True
                    progress.phase_results = results
                if phase.subphase == "container-build":
                    try:
                        receipt = _build_receipt_in_session(
                            session,
                            persisted_plan,
                            expected_image=expected_image,
                        )
                    except RunSwitchOperationConflict as error:
                        # The build's receipt is read from the Controller's own
                        # records, which may not be complete yet: observe it
                        # again without issuing the build a second time.
                        self._settle_invalid_receipt(job, error, now, reissue=False)
                        return True
                    results = list(progress.phase_results)
                    if not any(same_image(item, receipt) for item in results):
                        results.append(_phase_result(receipt, phase=phase))
                        progress.phase_results = results
                if phase.kind in {"transfer", "verify", "cleanup"} or (
                    phase.kind == "prepare" and phase.subphase == "runtime-image"
                ):
                    try:
                        _validate_artifact_execution(
                            persisted_plan,
                            phase,
                            getattr(child, "result", None),
                            expected_image=expected_image,
                        )
                    except RunSwitchOperationConflict as error:
                        # A receipt that does not validate is an unknown, not a
                        # failure: the idempotent child is issued again under a
                        # new identity and its fresh receipt is validated.
                        self._settle_invalid_receipt(job, error, now, reissue=True)
                        return True
                if (
                    child_receipts is None
                    and child_result is not None
                    and (
                        phase.kind in {"transfer", "verify", "cleanup"}
                        or phase.subphase == "runtime-image"
                    )
                ):
                    try:
                        receipt = _phase_result(child_result, phase=phase)
                    except RunSwitchOperationConflict as error:
                        # A receipt that does not validate is an unknown, not a
                        # failure: the idempotent child is issued again under a
                        # new identity and its fresh receipt is validated.
                        self._settle_invalid_receipt(job, error, now, reissue=True)
                        return True
                    progress.phase_results = [
                        *progress.phase_results,
                        receipt,
                    ]
                item_total = len(persisted_plan.stops) if phase.kind == "stop" else 1
                progress.child_operation_id = None
                if item_index >= item_total:
                    completed = list(progress.completed_phases)
                    completed.append(phase.kind)
                    progress.completed_phases = completed
                    _complete_phase_progress(progress, persisted_plan, phase)
                    progress.phase_index = phase_index + 1
                    progress.item_index = 0
                    progress.phase = (
                        persisted_plan.phases[phase_index + 1].kind
                        if phase_index + 1 < len(persisted_plan.phases)
                        else "final_verify"
                    )
                    progress.subphase = (
                        persisted_plan.phases[phase_index + 1].subphase
                        if phase_index + 1 < len(persisted_plan.phases)
                        else None
                    )
                else:
                    progress.item_index = item_index
                _ADAPTER.project(job, progress, now, state=_LifecycleState.RUNNING)
                job.result = _persisted_result(progress)
                job.updated_at = now
                if progress.cancellation:
                    _complete_cancellation(job, progress, now)
            return True
        expiry_event: dict[str, object] | None = None
        with self._sessions.begin() as session:
            job = checkpoint_job(session)
            if job is None or job.state not in {"queued", "running"}:
                return False
            plan = _stored_job_plan(job)
            progress = _read_progress(job.result)
            if progress.cancellation:
                _complete_cancellation(job, progress, now)
                return True
            if plan is None:
                _reject_invalid_operation(
                    job, "run-switch persisted plan is invalid", now
                )
                return True
            _ADAPTER.project(job, None, now, state=_LifecycleState.RUNNING)
            phase_index = require_integer(progress.phase_index, "phase index")
            item_index = require_integer(progress.item_index, "item index")
            if phase_index >= len(plan.phases):
                return True
            phase = plan.phases[phase_index]
            actor = job.actor
            retry_generation = require_integer(
                progress.phase_retry_generation or 0,
                "phase retry generation",
            )
            request_key = (
                _phase_request_key(
                    job.request_id, phase_index, item_index, retry_generation
                )
                if retry_generation
                else job.request_id
            )
        gate = getattr(self._phase_executor, "preflight", None)
        if isinstance(gate, _PhasePreflightGate):
            try:
                checkpoint, blocked = gate(
                    plan, phase, actor=actor, request_key=request_key, progress=progress
                )
            except (RuntimeError, ValueError, KeyError) as error:
                fail(
                    str(error),
                    failure_code=error_code(error),
                    replan=True,
                    definite=getattr(error, "definite", False),
                )
                return True
            if checkpoint is not None:
                with self._sessions.begin() as session:
                    job = checkpoint_job(session)
                    if job is None:
                        return False
                    current = _read_progress(job.result)
                    if not _checkpoint_matches(
                        job, current, phase_index, item_index, None
                    ):
                        return False
                    current.preflight = checkpoint
                    current.observation_due_at = (
                        checkpoint.next_check_at
                        if checkpoint.next_check_at is not None
                        else None
                    )
                    if blocked:
                        # Runtime evidence that says "not now" (a full disk, a
                        # missing prerequisite) is observed again at the core's
                        # backoff, with a re-plan before any Start could have
                        # launched: the request stays accepted and recovers when
                        # the cause clears, instead of ending the load.
                        current.force_replan = (
                            "start" not in current.completed_phases
                            and current.phase != "start"
                        )
                        self._schedule_checkpoint_retry(job, current, blocked, now)
                        return True
                    if checkpoint.pending_job_id:
                        current.operation = _observe_progress(
                            current.operation,
                            OperationProgress.model_validate(
                                {
                                    "phase": "runtime-preflight",
                                    "completed_bytes": 0,
                                    "total_bytes_known": False,
                                }
                            ),
                            now,
                        )
                    job.result = _persisted_result(current)
                    job.updated_at = now
                if checkpoint.pending_job_id or checkpoint.next_check_at is not None:
                    return True
        if phase.state in {"skipped", "retained"}:
            execution = PhaseExecution()
        elif phase.state == "blocked":
            fail(f"run-switch phase blocked: {phase.kind}", replan=True)
            return True
        elif self._phase_executor is None:
            fail(f"run-switch phase executor unavailable: {phase.kind}")
            return True
        else:
            try:
                with patient_admission(_refused_retries(progress)):
                    execution = self._phase_executor.execute(
                        plan,
                        phase,
                        item_index=item_index,
                        actor=actor,
                        request_key=request_key,
                        progress=progress,
                    )
            except _RunSwitchBuildParentChanged:
                return False
            except RunSwitchInstallPreflightExpired as expired:
                return self._hold_for_preflight_refresh(
                    operation_id, phase_index, item_index, cause=str(expired)
                )
            except (
                AdmissionLockBusy,
                InstallAdmissionBusy,
                RunAdmissionBusy,
                RecipeBuildAdmissionBusy,
            ) as busy:
                return self._hold_capacity_writer(
                    operation_id,
                    phase_index,
                    item_index,
                    reason=busy.code,
                    detail=(
                        busy.detail
                        if isinstance(busy, RunAdmissionBusy) and busy.detail
                        else busy_detail(busy)
                    ),
                )
            except RunSwitchPostStopEvidencePending as pending:
                return self._hold_capacity_writer(
                    operation_id,
                    phase_index,
                    item_index,
                    reason=pending.code,
                    detail=str(pending),
                    # Evidence collected at the threshold itself is not "after" it:
                    # the retry waits one second past it, never lands on it.
                    not_before=(
                        None
                        if pending.collected_after is None
                        else _aware(pending.collected_after) + timedelta(seconds=1)
                    ),
                )
            except RunSwitchIssuedWorkloadPending as pending:
                return self._hold_issued_observation(
                    operation_id,
                    phase_index=phase_index,
                    item_index=item_index,
                    pending=pending,
                )
            except RunSwitchOperationConflict as error:
                code = error_code(error)
                # Bookkeeping becomes unknown, then reconcile: a conflict that is
                # not a security refusal is observed again by re-entering the
                # same idempotent phase.  That includes final verification: a
                # check that cannot be observed (wrong image, missing member) is
                # not proof that the workload failed, so it is looked at again,
                # visibly (the retry names the cause and attempt), until it
                # holds, a newer intent supersedes it, or the load is cancelled.
                # A re-plan is only for planning drift before a Start could have
                # launched, never for a final verification of a live workload.
                fail(
                    str(error),
                    failure_code=code,
                    replan=phase.kind != "final_verify",
                    definite=error.definite,
                )
                return True
            except UnknownOutcomeError as error:
                # An unknown outcome is observed again, never ended: it is not
                # a definite failure even where the preparation owner left its
                # ``retryable`` flag unset, and the core backs the retry off.
                code = getattr(error, "code", None)
                if isinstance(code, str) and (
                    code == _RUNTIME_IMAGE_OWNER_CHANGED or code.startswith("artifact.")
                ):
                    return self._hold_capacity_writer(
                        operation_id,
                        phase_index,
                        item_index,
                        reason=code,
                        detail=str(getattr(error, "detail", None) or error),
                    )
                fail(
                    f"{type(error).__name__}: {error}",
                    failure_code=_failure_code_of(error),
                )
                return True
            except RuntimeImagePreparationError as error:
                # A publication that no longer finds its owner is re-checked,
                # visibly: a cancelled or superseded owner settles on the next
                # tick, a current one prepares again (reusing the archive).
                if error.code == _RUNTIME_IMAGE_OWNER_CHANGED or (
                    error.retryable and error.code.startswith("artifact.")
                ):
                    return self._hold_capacity_writer(
                        operation_id,
                        phase_index,
                        item_index,
                        reason=error.code,
                        detail=error.detail,
                    )
                # Bytes that failed their digest are refused and fetched again
                # by the ordinary preparation retry, with exponential backoff.
                # The preparation owner types its own verdict: an invalid archive
                # or identity is not retried, a transient failure and a digest
                # that is fetched again are.
                fail(
                    f"{type(error).__name__}: {error}",
                    failure_code=error.code,
                    definite=not (error.retryable or is_redownload(error.code)),
                )
                return True
            except (
                OSError,
                httpx2.HTTPError,
                RuntimeError,
                TypeError,
                ValueError,
                KeyError,
            ) as error:
                detail = f"{type(error).__name__}: {error}"
                fail(
                    detail,
                    failure_code=_failure_code_of(error),
                    replan=isinstance(error, (RuntimeError, ValueError)),
                )
                return True
        if (
            execution.waiting
            and execution.operation_id is None
            and phase.kind != "final_verify"
            and not (phase.kind == "prepare" and phase.subphase == "runtime-image")
        ):
            fail(run_switch_code(f"{phase.kind}-waiting-without-child"), replan=True)
            return True
        if (
            execution.operation_id is None
            and execution.result is not None
            and (
                phase.kind in {"transfer", "verify", "cleanup"}
                or (phase.kind == "prepare" and phase.subphase == "runtime-image")
            )
        ):
            try:
                _validate_artifact_execution(
                    plan, phase, execution.result, expected_image=expected_image
                )
            except RunSwitchOperationConflict as error:
                # Completed work whose receipt does not validate is not proof
                # that the work failed: its effect is unknown.  Entering the
                # same idempotent phase again observes it (a transfer or verify
                # re-reads the bytes; a cleanup re-reads the reclaim evidence),
                # and a re-plan drops stale evidence.  Never terminal.
                fail(
                    str(error),
                    failure_code=error_code(error),
                    replan=True,
                    definite=error.definite,
                )
                return True
        with self._sessions.begin() as session:
            job = checkpoint_job(session)
            if job is None:
                return False
            progress = _read_progress(job.result)
            if not _checkpoint_matches(job, progress, phase_index, item_index, None):
                return False
            previous_status_reason = job.status_reason
            if progress.observation_due_at is not None:
                progress.observation_due_at = None
                progress.observation_deadline_at = None
                job.status_reason = None
            retry_reason = progress.retry_reason
            if retry_reason in (
                AdmissionLockBusy.code,
                InstallAdmissionBusy.code,
                RunAdmissionBusy.code,
                *RETRYABLE_PLAN_BLOCKERS,
                *RUN_ADMISSION_WAIT_CODES,
                RecipeBuildAdmissionBusy.code,
                RunSwitchPostStopEvidencePending.code,
                _RUNTIME_IMAGE_OWNER_CHANGED,
            ) or (
                isinstance(retry_reason, str) and retry_reason.startswith("artifact.")
            ):
                progress.retry_reason = None
                progress.retry_attempt = None
            deadline_expired = False
            _merge_progress_evidence(
                progress,
                plan,
                phase,
                execution.result,
                now,
            )
            if execution.waiting:
                progress.phase = phase.kind
                progress.subphase = phase.subphase
                if phase.kind == "final_verify" and execution.operation_id is None:
                    if progress.final_verify_started_at is None:
                        progress.final_verify_started_at = now.timestamp()
                    started = progress.final_verify_started_at
                    if (
                        isinstance(started, bool)
                        or not isinstance(started, (int, float))
                        or now.timestamp() < started
                    ):
                        self._mark_failed(
                            job,
                            RunSwitchCode.FINAL_VERIFICATION_CLOCK_INVALID,
                            now=now,
                            progress=progress,
                        )
                        return True
                    raw_start_deadline = progress.start_deadline
                    start_deadline = (
                        _aware(raw_start_deadline)
                        if raw_start_deadline is not None
                        else None
                    )
                    # The start's budget begins when it is first dispatched, so
                    # the Controller moves its deadline past the queue wait; the
                    # start job's own payload is the one source of the accepted
                    # deadline.
                    verify_run_id = (execution.result or {}).get("run_id")
                    if isinstance(verify_run_id, str):
                        issued = session.scalar(
                            select(Job)
                            .where(
                                Job.kind == "recipe.start",
                                Job.payload["owner_id"].as_string() == verify_run_id,
                            )
                            .order_by(Job.created_at.desc())
                            .limit(1)
                        )
                        issued_parent = _start_parent(issued)
                        issued_deadline = (
                            issued_parent.start_deadline
                            if issued_parent is not None
                            else None
                        )
                        if issued_deadline is not None:
                            anchored = _aware(issued_deadline)
                            if start_deadline is None or anchored > start_deadline:
                                start_deadline = anchored
                                progress.start_deadline = anchored
                    deadline_expired = (
                        plan.action in {"run", "switch"}
                        and start_deadline is not None
                        and now >= start_deadline
                    )
                    if (
                        deadline_expired
                        and now.timestamp() - started >= _FINAL_VERIFICATION_MAX_SECONDS
                    ):
                        phase_evidence = _phase_result(
                            execution.result or {}, phase=phase
                        )
                        run_id = (
                            phase_evidence.run_id
                            if isinstance(phase_evidence, RunSwitchFinalVerifyResult)
                            else None
                        )
                        run = (
                            session.get(RecipeRun, run_id)
                            if isinstance(run_id, str)
                            else None
                        )
                        if run is not None and run.state == RunState.RUNNING:
                            run.route_state = RouteState.WITHDRAWN
                            run.route_error = (
                                "final verification exceeded its 15 minute bound; "
                                "exact workload recovery is inspecting this run"
                            )
                            run.route_next_attempt_at = now
                            failed_node = session.scalar(
                                select(RunNode)
                                .where(RunNode.run_id == run.id)
                                .order_by(RunNode.rank)
                                .limit(1)
                            )
                            if failed_node is not None:
                                # This marks the accepted run degraded, not absent.
                                # The recovery coordinator must obtain fresh exact
                                # signed evidence and reconcile Stop before Start.
                                failed_node.state = RunState.FAILED
                                failed_node.updated_at = now
                            run.updated_at = now
                        reason = (
                            f"{RunSwitchCode.FINAL_VERIFICATION_TIMEOUT}: exact run and "
                            "route evidence did not arrive within 15 minutes; the "
                            "route is withdrawn and exact workload recovery has "
                            "been queued"
                        )
                        self._mark_failed(
                            job,
                            reason,
                            now=now,
                            failure_code=RunSwitchCode.FINAL_VERIFICATION_TIMEOUT,
                            progress=progress,
                        )
                        return True
                    due = now + timedelta(
                        seconds=(
                            60
                            if deadline_expired or now.timestamp() - started >= 300
                            else 5
                        )
                    )
                    progress.observation_due_at = due
                    if deadline_expired and start_deadline is not None:
                        owner_reason = execution.status_reason or (
                            "run and route owners have not produced exact final evidence"
                        )
                        job.status_reason = (
                            f"{RunSwitchCode.FINAL_VERIFICATION_EXPIRED}: accepted start "
                            f"deadline {start_deadline.isoformat()} passed; "
                            f"{owner_reason[:220]}; exact reconciliation retains the "
                            f"run and reservations; next observation at {due.isoformat()}"
                        )[:512]
                        _ADAPTER.project(
                            job,
                            progress,
                            now,
                            state=_LifecycleState.OBSERVING,
                            due=due,
                            visible=_OBSERVING,
                        )
                        if not (
                            isinstance(previous_status_reason, str)
                            and previous_status_reason.startswith(
                                f"{RunSwitchCode.FINAL_VERIFICATION_EXPIRED}:"
                            )
                        ):
                            phase_evidence = _phase_result(
                                execution.result or {}, phase=phase
                            )
                            expiry_event = {
                                "operation_id": job.id,
                                "run_id": phase_evidence.run_id
                                if isinstance(
                                    phase_evidence, RunSwitchFinalVerifyResult
                                )
                                else None,
                                "run_state": phase_evidence.state
                                if isinstance(
                                    phase_evidence, RunSwitchFinalVerifyResult
                                )
                                else None,
                                "route_state": phase_evidence.route_state
                                if isinstance(
                                    phase_evidence, RunSwitchFinalVerifyResult
                                )
                                else None,
                                "accepted_start_deadline": start_deadline.isoformat(),
                                "status_reason": job.status_reason,
                            }
                    else:
                        job.status_reason = execution.status_reason or (
                            "Waiting for exact run and route verification; "
                            f"next observation at {due.isoformat()}"
                        )
                    # Keep one current observation while awaiting route publication.
                    # Repeated polling must not grow durable phase receipts.
                    progress.final_observation = _phase_result(
                        execution.result or {}, phase=phase
                    )
                elif execution.result is not None:
                    results = list(progress.phase_results)
                    results.append(_phase_result(execution.result, phase=phase))
                    progress.phase_results = results
                elif phase.kind == "prepare" and phase.subphase == "runtime-image":
                    due = now + timedelta(seconds=5)
                    progress.observation_due_at = due
                    job.status_reason = execution.status_reason or (
                        "Runtime image preparation is running in the background; "
                        f"next check at {due.isoformat()}"
                    )
                    _ADAPTER.project(
                        job,
                        progress,
                        now,
                        state=_LifecycleState.OBSERVING,
                        due=due,
                        visible=_OBSERVING,
                    )
            elif execution.operation_id is not None:
                progress.child_operation_id = execution.operation_id
                progress.phase = phase.kind
                progress.subphase = phase.subphase
                if phase.kind == "start":
                    child = session.get(Job, execution.operation_id)
                    child_parent = _start_parent(child)
                    raw_deadline = (
                        child_parent.start_deadline
                        if child_parent is not None
                        else None
                    )
                    if child is not None and raw_deadline is not None:
                        deadline = _aware(raw_deadline)
                        progress.start_deadline = deadline
                        budget = int(
                            (deadline - _aware(child.created_at)).total_seconds()
                        )
                        if budget > 0:
                            progress.startup_budget_seconds = budget
                if execution.result is not None:
                    results = list(progress.phase_results)
                    results.append(_phase_result(execution.result, phase=phase))
                    progress.phase_results = results
            else:
                if execution.result is not None:
                    results = list(progress.phase_results)
                    results.append(_phase_result(execution.result, phase=phase))
                    progress.phase_results = results
                if phase.subphase == "container-build":
                    try:
                        receipt = _build_receipt_in_session(
                            session, plan, expected_image=expected_image
                        )
                    except RunSwitchOperationConflict as error:
                        # The build's receipt is read from the Controller's own
                        # records, which may not be complete yet: observe it
                        # again without issuing the build a second time.
                        self._settle_invalid_receipt(job, error, now, reissue=False)
                        return True
                    results = list(progress.phase_results)
                    if not any(same_image(item, receipt) for item in results):
                        results.append(_phase_result(receipt, phase=phase))
                        progress.phase_results = results
                completed = list(progress.completed_phases)
                completed.append(phase.kind)
                progress.completed_phases = completed
                _complete_phase_progress(progress, plan, phase)
                progress.phase_index = (
                    require_integer(progress.phase_index, "phase index") + 1
                )
                progress.item_index = 0
                next_index = int(progress.phase_index)
                progress.phase = (
                    plan.phases[next_index].kind
                    if next_index < len(plan.phases)
                    else "final_verify"
                )
                progress.subphase = (
                    plan.phases[next_index].subphase
                    if next_index < len(plan.phases)
                    else None
                )
            if not deadline_expired and not (
                execution.waiting
                and phase.kind == "prepare"
                and phase.subphase == "runtime-image"
            ):
                _ADAPTER.project(job, progress, now, state=_LifecycleState.RUNNING)
            job.result = _persisted_result(progress)
            job.updated_at = now
            if progress.cancellation and not progress.child_operation_id:
                _complete_cancellation(job, progress, now)
        if expiry_event is not None:
            log_event(
                _LOGGER,
                "run_switch.final_verification_expired",
                service="control-worker",
                **expiry_event,
            )
        return True

    def _never_installed(self, installation_id: str) -> bool:
        """Whether the installation's own assessment proves no node effect."""

        if self._lifecycle is None:
            return False
        try:
            assessment = self._lifecycle.preview_uninstall(installation_id)
        except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError):
            return False
        return assessment.allowed and assessment.disposition == "abandon"

    @staticmethod
    def _scope_intent_status(session: Session, job: Job) -> str:
        """Distinguish malformed authority from a later authorized node head."""

        parent = _run_switch_payload(job)
        ordinal = parent.workload_intent_ordinal if parent is not None else None
        if type(ordinal) is not int or ordinal < 1:
            return "invalid"
        nodes = session.scalars(
            select(AgentNode).where(AgentNode.node_id.in_(job.targets))
        )
        current = list(nodes)
        if any(node.revoked_at is not None for node in current):
            return "invalid"
        if len(current) != len(job.targets):
            return "missing-target"
        if any(node.state != "active" for node in current):
            return "waiting"
        if any(node.workload_intent_ordinal != ordinal for node in current):
            return "superseded"
        return "current"

    def _hold_start_observation(
        self,
        operation_id: str,
        *,
        child_id: str,
        phase_index: int,
        item_index: int,
    ) -> bool:
        """Observe a progressing uncertain start until its immutable deadline."""

        now = _now(self._clock)
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return False
            progress = _read_progress(job.result)
            if not _checkpoint_matches(
                job, progress, phase_index, item_index, child_id
            ):
                return False
            raw_deadline = progress.observation_deadline_at
            if raw_deadline is not None:
                deadline = _aware(raw_deadline)
            else:
                child = session.get(Job, child_id)
                child_parent = _start_parent(child)
                child_deadline = (
                    child_parent.start_deadline if child_parent is not None else None
                )
                deadline = (
                    _aware(child_deadline)
                    if child_deadline is not None
                    else now + timedelta(seconds=120)
                )
            if now >= deadline:
                # Expiry establishes an overdue observation, not a stopped or
                # failed runtime. Preserve the exact child and reservations;
                # its owner alone can reconcile or retry the uncertain effect.
                progress.observation_deadline_at = deadline
                _ADAPTER.retry(
                    job,
                    progress,
                    RunSwitchCode.START_OBSERVATION_EXPIRED,
                    now,
                    visible=_OBSERVING,
                    record_reason=False,
                    describe=lambda due: (
                        f"{RunSwitchCode.START_OBSERVATION_EXPIRED}: exact effect remains "
                        f"unresolved; next observation at {due.isoformat()}"
                    ),
                )
                job.result = _persisted_result(progress)
                job.updated_at = now
                return True
            progress.observation_deadline_at = deadline
            _ADAPTER.project(
                job,
                progress,
                now,
                state=_LifecycleState.OBSERVING,
                due=min(deadline, now + timedelta(seconds=5)),
                visible="running",
                reason="Start result uncertain; observing the existing run.",
            )
            job.result = _persisted_result(progress)
            job.updated_at = now
        return True

    def _hold_issued_observation(
        self,
        operation_id: str,
        *,
        phase_index: int,
        item_index: int,
        pending: RunSwitchIssuedWorkloadPending,
    ) -> bool:
        """Bound polling of an issued older effect without authorizing replay."""

        now = _now(self._clock)
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return False
            progress = _read_progress(job.result)
            if not _checkpoint_matches(job, progress, phase_index, item_index, None):
                return False
            deadline = _aware(pending.observation_deadline)
            # The lifecycle admission path already re-observes this exact
            # dependency before issuing anything. A missing cancellation receipt
            # cannot turn that safe observation into permanently parked intent.
            due = (
                now + timedelta(seconds=60)
                if now >= deadline
                else min(
                    deadline,
                    max(now + timedelta(seconds=5), _aware(pending.observe_due_at)),
                )
            )
            progress.observation_deadline_at = deadline
            _ADAPTER.project(
                job,
                progress,
                now,
                state=_LifecycleState.OBSERVING,
                due=due,
                visible="running",
                reason=(
                    f"Observing older {pending.kind} operation {pending.job_id}; "
                    f"effect unresolved, next observation at {due.isoformat()}"
                ),
            )
            job.result = _persisted_result(progress)
            job.updated_at = now
        return True

    def _hold_capacity_writer(
        self,
        operation_id: str,
        phase_index: int,
        item_index: int,
        *,
        reason: str,
        detail: str | None = None,
        not_before: datetime | None = None,
    ) -> bool:
        """Retry an unchanged capacity handoff with bounded exponential backoff.

        The admission transaction has rolled back and released its locks.
        The wait belongs to the existing operation, holds no worker slot, and
        expires at the next admission attempt; busy SQL is not a failed effect.
        """
        now = _now(self._clock)
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return False
            progress = _read_progress(job.result)
            if not _checkpoint_matches(job, progress, phase_index, item_index, None):
                return False
            if progress.cancellation:
                _complete_cancellation(job, progress, now)
            else:
                attempt = (
                    require_integer(progress.retry_attempt, "retry attempt")
                    if progress.retry_reason == reason
                    and progress.retry_attempt is not None
                    else 1
                )
                _ADAPTER.retry(
                    job,
                    progress,
                    reason,
                    now,
                    reset_on_change=True,
                    retry_after=not_before,
                    describe=lambda due: (
                        (
                            detail
                            or "Admission is waiting for the Controller capacity writer"
                        )
                        + (
                            f"; admission retry {attempt} in "
                            f"{max(int((due - now).total_seconds()), 1)}s "
                            f"at {due.isoformat()}."
                        )
                    ),
                )
                progress.observation_deadline_at = progress.observation_due_at
                job.result = _persisted_result(progress)
                job.updated_at = now
        return True

    def _hold_for_preflight_refresh(
        self,
        operation_id: str,
        phase_index: int,
        item_index: int,
        *,
        cause: str | None = None,
    ) -> bool:
        """Keep the runtime-plan checkpoint so the existing gate reprobes.

        Acceptance rolled its transaction back, so no installation, reservation
        or agent child exists.  Leaving ``phase_index`` and ``item_index``
        untouched sends the next tick back through ``LifecyclePreflight.ensure``.
        Back off even when the gate's cached receipt is still fresh while
        admission selects a different, expired database receipt. The existing
        typed retry fields retain the compilation attempt, cause and next time;
        temporary freshness races cannot permanently abandon accepted intent.

        A hold never advances a cancelled operation into a subsequent
        acceptance.  Cancellation racing a successful acceptance is unchanged.
        """

        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return False
            progress = _read_progress(job.result)
            if not _checkpoint_matches(job, progress, phase_index, item_index, None):
                return False
            # Compilation may have taken minutes; persist the actual hold time.
            now = _now(self._clock)
            if progress.cancellation:
                _complete_cancellation(job, progress, now)
                return True
            attempt = (
                require_integer(progress.retry_attempt, "retry attempt")
                if progress.retry_reason == _INSTALL_PREFLIGHT_REFRESH_REASON
                and progress.retry_attempt is not None
                else 1
            )
            progress.retry_reason = _INSTALL_PREFLIGHT_REFRESH_REASON
            progress.operation_phase_index = phase_index
            progress.operation = _observe_progress(
                progress.operation,
                OperationProgress.model_validate(
                    {
                        "phase": "install-preflight-refresh",
                        "completed_items": attempt,
                        "completed_bytes": 0,
                        "total_bytes_known": False,
                    }
                ),
                now,
            )
            self._schedule_checkpoint_retry(
                job, progress, _INSTALL_PREFLIGHT_REFRESH_REASON, now
            )
            if cause:
                # Say which runtime evidence was refused, not only that the
                # probe runs again: a repeating cause is the stall to look at.
                job.status_reason = (
                    f"{job.status_reason}; attempt {attempt}: {cause}"
                )[:512]
            _LOGGER.warning(
                "run/switch %s: install admission refused its runtime preflight "
                "evidence (attempt %d: %s); probing the Sparks again",
                operation_id,
                attempt,
                cause or "expired",
            )
        return True

    def _stop_child(self, session: Session, job: Job, now: datetime) -> bool:
        """The adapter's idempotent stop: whether nothing of the child still runs.

        A child that already ended, or a parked idempotent one that can be closed
        (its copied bytes stay on the Spark), is stopped.  A running child is
        observed, not aborted: its own lifecycle owns it, and the core's stop budget
        ends the cancel either way.
        """

        result = job.result
        child_id = (
            result.get("child_operation_id") if isinstance(result, Mapping) else None
        )
        if not isinstance(child_id, str) or not child_id:
            return True
        try:
            child = self._get_child_operation(child_id)
        except KeyError:
            return True  # nothing is left to stop
        if child is None or child.state in _TERMINAL_STATES:
            return True
        if child.state in job_states.words(LifecycleState.NEEDS_OPERATOR):
            return self._abandon_idempotent_child(session, child_id, now)
        return False

    def _cancel_with_session(
        self,
        session: Session,
        job: Job,
        progress: RunSwitchOperationResult,
        now: datetime,
        *,
        tick: bool = False,
    ) -> bool:
        """Drive a recorded cancel through the core (rule 4); it always completes.

        Returns whether the cancel moved (a tick that is not due changes nothing).
        """

        adapter = RunSwitchAdapter(session, clock=lambda: now, stopper=self._stop_child)
        if tick:
            return adapter.tick_cancel(job, progress, now)
        adapter.request_cancel(job, progress, now)
        return True

    def _abandon_idempotent_child(
        self, session: Session, operation_id: str, now: datetime
    ) -> bool:
        """Ask the phase executor to close a parked idempotent child, if it can."""

        abandon = getattr(self._phase_executor, "abandon", None)
        if not callable(abandon):
            return False
        return bool(
            abandon(
                session,
                operation_id,
                now,
                reason="Cancelled with its Run/Switch order; copied bytes remain "
                "on the Spark for reuse",
            )
        )

    def _get_child_operation(self, operation_id: str) -> Any:
        getter = getattr(self._phase_executor, "get", None)
        if callable(getter):
            try:
                child = getter(operation_id)
            except KeyError:
                child = None
            if child is not None:
                return child
        if self._lifecycle is not None:
            return self._lifecycle.get(operation_id)
        return None

    def _fail(
        self,
        operation_id: str,
        reason: str,
        *,
        replan: bool = False,
        failure_code: str | None = None,
        checkpoint: tuple[int, int, object] | None = None,
        child_evidence: object | None = None,
        checkpoint_guard: Callable[[Session], Job | None] | None = None,
        clear_child: bool = False,
        definite: bool = False,
    ) -> None:
        """A phase could not be settled.

        Only a real security refusal (``is_security_failure``) is a definite end.
        Everything else (a receipt that does not validate, a verification that
        cannot be observed, a missing child, a wiring gap) is an unknown: the same
        idempotent phase is entered again at the core's bounded backoff, so the
        failure never ends a load whose bytes and workload are fine.  The caller
        no longer chooses; the classifier does (``failure_classification``).
        ``definite`` is only for an owner that typed its own verdict (the image
        preparation's non-retryable errors: an invalid archive or identity).
        """

        with self._sessions.begin() as session:
            job = (
                checkpoint_guard(session)
                if checkpoint_guard is not None
                else session.get(Job, operation_id, with_for_update=True)
            )
            if job is None or job.state not in {"queued", "running"}:
                return
            progress = _read_progress(job.result)
            if checkpoint is not None and not _checkpoint_matches(
                job, progress, *checkpoint
            ):
                return
            now = _now(self._clock)
            if child_evidence is not None and checkpoint is not None:
                plan = _stored_job_plan(job)
                if plan is not None and checkpoint[0] < len(plan.phases):
                    _merge_progress_evidence(
                        progress, plan, plan.phases[checkpoint[0]], child_evidence, now
                    )
            if progress.cancellation:
                if progress.child_operation_id is None:
                    _complete_cancellation(job, progress, now)
                else:
                    # A failure under a cancel is not the end of it: the cancel is
                    # driven (the child stopped, observed up to the budget).
                    self._cancel_with_session(session, job, progress, now, tick=True)
                    job.result = _persisted_result(progress)
                    job.updated_at = now
                return
            elif (
                not definite
                and not is_security_failure(failure_code)
                and checkpoint is not None
                and (checkpoint[2] is None or clear_child)
            ):
                # Re-enter the current phase with the same accepted intent and
                # deterministic child identity. The executor reconciles any
                # issued effect before it retries; the parent never replaces a
                # live child or changes the exact workload plan.
                # A re-plan restarts from the first phase, so it is only used
                # before a Start could have launched this intent's workload.
                # A cause that a fresh plan did not remove is not planning
                # drift: the same refusal after a re-plan retries in place, so
                # the attempts accumulate and surface as a named stall instead
                # of replanning (and forgetting its attempts) forever.
                progress.force_replan = (
                    replan
                    and progress.retry_reason != reason[:512]
                    and "start" not in progress.completed_phases
                    and progress.phase != "start"
                )
                if clear_child:
                    progress.child_operation_id = None
                self._schedule_checkpoint_retry(job, progress, reason, now)
                return
            self._mark_failed(
                job,
                reason,
                now=now,
                failure_code=failure_code,
                progress=progress,
            )

    @staticmethod
    def _settle_invalid_receipt(
        job: Job, error: RunSwitchOperationConflict, now: datetime, *, reissue: bool
    ) -> None:
        """A receipt did not validate inside the checkpoint transaction (rule 5).

        Its effect is unknown, so the phase is entered again at the core's backoff:
        an idempotent child is issued again under a new identity (``reissue``), a
        receipt read from the Controller's own records is simply observed again.
        Only a reviewed destructive-effect or digest guard ends the operation.
        The progress is re-read, so nothing half-merged from the invalid receipt is
        kept.
        """

        code = error_code(error)
        progress = _read_progress(job.result)
        if error.definite or is_security_failure(code):
            RunSwitchOperationService._mark_failed(
                job, str(error), now=now, progress=progress, failure_code=code
            )
            return
        if reissue:
            progress.child_operation_id = None
            progress.phase_retry_generation = (
                require_integer(
                    progress.phase_retry_generation or 0,
                    "phase retry generation",
                )
                + 1
            )
        RunSwitchOperationService._schedule_checkpoint_retry(
            job, progress, str(error), now
        )

    @staticmethod
    def _schedule_checkpoint_retry(
        job: Job, progress: RunSwitchOperationResult, reason: str, now: datetime
    ) -> None:
        """Wait at the exact checkpoint without replacing accepted intent."""
        _ADAPTER.retry(job, progress, reason, now)
        job.result = _persisted_result(progress)
        job.updated_at = now

    @staticmethod
    def _mark_failed(
        job: Job,
        reason: str,
        *,
        now: datetime,
        retryable: bool = False,
        failure_code: str | None = None,
        progress: RunSwitchOperationResult | None = None,
    ) -> None:
        """Record failure using the caller's transaction and existing row lock."""

        if progress is None:
            progress = _read_progress(job.result)
        _ADAPTER.fail(
            job,
            progress,
            reason,
            now,
            failure_code=failure_code,
            retryable=retryable,
        )
        job.result = _persisted_result(progress)
        job.updated_at = now

    @staticmethod
    def _operation_view(job: Job) -> RunSwitchOperation:
        progress = _read_progress(job.result)
        persisted_result = _parse_persisted_result(job.result)
        # Target membership belongs to the Job; receipts carry member progress.
        plan = _stored_job_plan(job)
        # A plan that cannot be read still leaves the identity the operation was
        # accepted under in the payload: the view is rebuilt from that.
        action, plan_digest, cleanup_mode, installation_id = _view_identity(job, plan)
        current_phase = persisted_result.phase if persisted_result is not None else None
        completed = (
            persisted_result.completed_phases if persisted_result is not None else []
        )
        projected_state = _progress_operation_state(job.state)
        if (
            projected_state == LifecycleState.NEEDS_OPERATOR
            and persisted_result is not None
            and persisted_result.observation_due_at is not None
        ):
            # Existing accepted rows parked by the prior automatic-observation
            # state are still auto-observed; present their actual behavior.
            projected_state = LifecycleState.OBSERVING
        blockers = (
            list(persisted_result.blockers)
            if persisted_result is not None
            and job.state
            in job_states.words(
                LifecycleState.QUEUED,
                LifecycleState.RUNNING,
                LifecycleState.OBSERVING,
                LifecycleState.NEEDS_OPERATOR,
            )
            else []
        )
        return RunSwitchOperation(
            operation_id=job.id,
            kind=_OPERATION_KIND_ADAPTER.validate_python(job.kind, strict=True),
            action=action,
            state=projected_state,
            plan_digest=plan_digest,
            request_key=job.request_id,
            cleanup_mode=cleanup_mode,
            installation_id=installation_id,
            node_ids=list(job.targets),
            current_phase=current_phase,
            completed_phases=completed,
            progress=_progress_view(
                plan,
                progress,
                projected_state,
                job.status_reason,
                node_ids=job.targets,
            ),
            status_reason=job.status_reason
            if plan is not None or job.status_reason is not None
            else "The stored plan cannot be read; the operation is shown from its "
            "recorded identity.",
            result=persisted_result,
            blockers=blockers,
            next_attempt_at=(
                persisted_result.observation_due_at
                if blockers and persisted_result is not None
                else None
            ),
        )


class RunSwitchOperationProvider:
    """Project high-level jobs into the global Activity provider contract."""

    family = "run-switch"

    def __init__(self, service: RunSwitchOperationService) -> None:
        self._service = service

    @property
    def represented_job_kinds(self) -> frozenset[str]:
        return _OPERATION_KINDS

    def list_operations(self, query: OperationQuery) -> OperationListPage:
        limit = getattr(query, "limit", None)
        if type(limit) is not int or not 1 <= limit <= 101:
            raise InvalidValue(
                "operation provider page limit is invalid",
                reason=InvalidRequestReason.OUT_OF_RANGE,
            )
        state = getattr(query, "state", None)
        if state is not None and not isinstance(state, str):
            raise InvalidValue("operation provider state filter is invalid")
        node_id = getattr(query, "node_id", None)
        if node_id is not None and not isinstance(node_id, str):
            raise InvalidValue("operation provider node filter is invalid")
        request_id = getattr(query, "request_id", None)
        if request_id is not None and not isinstance(request_id, str):
            raise InvalidValue("operation provider request filter is invalid")
        after = getattr(query, "after", None)
        with self._service._sessions() as session:
            base_filters: list[ColumnElement[bool]] = [Job.kind.in_(_OPERATION_KINDS)]
            if state is not None:
                base_filters.append(Job.state == state)
            if request_id is not None:
                base_filters.append(Job.request_id == request_id)
            if node_id is not None:
                base_filters.append(cast(Job.targets, String).contains(f'"{node_id}"'))
            if after is not None and (
                not isinstance(after, tuple)
                or len(after) != 2
                or not isinstance(after[0], datetime)
                or not isinstance(after[1], str)
            ):
                raise InvalidValue("operation provider cursor is invalid")
            filters = list(base_filters)
            boundary = _activity_keyset_filter(
                Job.created_at,
                Job.id,
                "",
                None if after is None else (_aware(after[0]), after[1]),
            )
            if boundary is not None:
                filters.append(boundary)
            total = int(
                session.scalar(
                    select(func.count()).select_from(Job).where(*base_filters)
                )
                or 0
            )
            jobs = list(
                session.scalars(
                    select(Job)
                    .where(*filters)
                    .order_by(Job.created_at.desc(), Job.id.desc())
                    .limit(limit)
                )
            )
        items = tuple(self._item(job) for job in jobs[:limit])
        return OperationListPage(items, None, total)

    def get_operation(self, operation_id: str) -> Mapping[str, object]:
        with self._service._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.kind not in _OPERATION_KINDS:
                raise MissingRecord(operation_id)
            return self._item(job)

    def _item(self, job: Job) -> Mapping[str, object]:
        node_ids: list[str] = []
        # Malformed stored targets read as an unreadable row (shown below), the
        # same as a plan that does not parse.
        readable = not (
            not isinstance(job.targets, list)
            or not job.targets
            or any(
                not isinstance(node_id, str)
                or re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None
                for node_id in job.targets
            )
        )
        operation: RunSwitchOperation | None = None
        if readable:
            node_ids = list(job.targets)
            try:
                operation = self._service._operation_view(job)
            except (
                AttributeError,
                KeyError,
                RunSwitchOperationConflict,
                TypeError,
                ValueError,
                ValidationError,
            ):
                operation = None
        if operation is None:
            warn_unreadable_once("run-switch job", job.id)
            return {
                "id": job.id,
                "job_id": job.id,
                "parent_id": None,
                "owner": {"kind": "job", "id": job.id, "request_id": job.request_id},
                # Job.targets is the durable owner of target membership. Keep
                # a valid scope visible when plan parsing fails so global
                # node-filtered Activity pages retain this unreadable row.
                "node_ids": node_ids,
                "kind": "run-switch-unreadable",
                "state": "unavailable",
                "attempt": max(0, int(getattr(job, "current_attempt", 0) or 0)),
                "progress": None,
                "created_at": _aware(job.created_at).isoformat(),
                "updated_at": _aware(job.updated_at).isoformat(),
                "supported_actions": [],
                "status_reason": "Stored Run/Switch history is unreadable.",
            }
        endpoint_node = node_ids[0]
        plan = _stored_job_plan(job)
        if plan is not None:
            endpoint_node = next(
                (
                    node.node_id
                    for node in plan.spark_group.nodes
                    if node.endpoint_owner
                ),
                endpoint_node,
            )
        return {
            "id": operation.operation_id,
            "job_id": operation.operation_id,
            "parent_id": None,
            "owner": {
                "kind": "job",
                "id": job.id,
                "request_id": job.request_id,
            },
            # The singular field is retained for older Activity readers; the
            # complete group is authoritative in node_ids and progress.members.
            "node_id": endpoint_node,
            "node_ids": node_ids,
            "kind": operation.kind,
            "state": operation.state,
            "attempt": max(1, int(getattr(job, "current_attempt", 0) or 0)),
            "progress": _activity_progress(operation),
            "created_at": _aware(job.created_at).isoformat(),
            "updated_at": _aware(job.updated_at).isoformat(),
            "supported_actions": (
                ["retry"]
                if operation.state == "failed"
                and operation.result
                and operation.result.retryable
                else ["cancel"]
                if operation.state
                in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.OBSERVING,
                )
                and not (operation.result and operation.result.cancellation)
                and (
                    operation.state == "queued"
                    or operation.current_phase not in {"start", "final_verify"}
                )
                else []
            ),
            "result": _activity_result(operation),
            "detail": operation.status_reason,
            "blockers": [item.model_dump(mode="json") for item in operation.blockers],
            "next_attempt_at": (
                operation.next_attempt_at.isoformat()
                if operation.next_attempt_at is not None
                else None
            ),
        }


def _activity_progress(operation: RunSwitchOperation) -> dict[str, object]:
    """Normalize the family DTO into the generic Activity progress vocabulary."""

    raw = operation.progress.model_dump(mode="json")
    phase = raw.get("phase")
    generic_phase = phase if isinstance(phase, str) and phase else "unknown"
    members: list[dict[str, object]] = []
    for raw_member in raw.get("members", []):
        if not isinstance(raw_member, Mapping):
            continue
        node_id = raw_member.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            continue
        member_phase = raw_member.get("phase")
        member: dict[str, object] = {
            "member_id": node_id,
            "phase": (
                member_phase
                if isinstance(member_phase, str) and member_phase
                else generic_phase
            ),
            "completed_bytes": int(raw_member.get("completed_bytes", 0) or 0),
            "state": str(raw_member.get("state", "unknown")),
        }
        if raw_member.get("total_bytes") is not None:
            member["total_bytes"] = int(raw_member["total_bytes"])
        members.append(member)
    progress: dict[str, object] = {
        "phase": generic_phase,
        "completed_bytes": int(raw.get("completed_bytes", 0) or 0),
        "total_bytes_known": bool(raw.get("total_bytes_known", False)),
        "members": members,
        "checkpoint": {
            "key": "run-switch-phase",
            "sequence": int(raw.get("phase_index", 0) or 0),
            "digest": operation.plan_digest,
        },
    }
    if raw.get("total_bytes") is not None:
        progress["total_bytes"] = int(raw["total_bytes"])
    measured = raw.get("operation")
    if isinstance(measured, Mapping):
        # Unknown measured totals must clear planned totals as well as their
        # known flag; mixing the two makes the public progress invalid.
        progress.update(measured)
    return progress


def _activity_result(operation: RunSwitchOperation) -> dict[str, object] | None:
    """Keep family result data and add bounded generic failure evidence."""

    # Blockers travel as their own Activity field; the failure evidence is
    # bounded by key count, so they are not repeated inside the result.
    result = (
        operation.result.model_dump(mode="json", exclude={"blockers"})
        if operation.result is not None
        else {}
    )
    if operation.state == "failed":
        retryable = bool(result.get("retryable") is True)
        result.update(
            {
                "error_code": "run_switch_failed",
                "summary": operation.status_reason or "Run/Switch operation failed",
                "detail": operation.status_reason,
                "retryable": retryable,
                "uncertain": False,
            }
        )
    return result or None


def _planned_transfer_bytes(
    plan: RunSwitchPlan,
) -> tuple[int | None, dict[str, int | None]]:
    """Return the exact transfer envelope represented by the persisted plan.

    The model and OCI image are independent preparation inputs.  A missing
    byte count on either input keeps the aggregate unknown; silently treating
    an unknown cache manifest as zero would make the progress contract lie.
    """

    node_ids = list(_plan_target_node_ids(plan))
    if plan.action == "stop":
        return 0, {node_id: 0 for node_id in node_ids}
    model_download_bytes = plan.storage.missing_nas_bytes
    model_bytes = plan.storage.missing_spark_bytes
    image_bytes = plan.runtime_storage.missing_image_distribution_bytes
    target_bytes = (
        model_bytes + image_bytes
        if model_bytes is not None and image_bytes is not None
        else None
    )
    total = (
        model_download_bytes + target_bytes
        if model_download_bytes is not None and target_bytes is not None
        else None
    )
    per_node: dict[str, int | None] = {}
    for node_id in node_ids:
        model_each = _node_missing_bytes(
            plan.storage.missing_spark_bytes_by_node,
            node_id,
            model_bytes,
            len(node_ids),
        )
        image_each = _node_missing_bytes(
            plan.runtime_storage.missing_image_distribution_bytes_by_node,
            node_id,
            image_bytes,
            len(node_ids),
        )
        per_node[node_id] = (
            model_each + image_each
            if model_each is not None and image_each is not None
            else None
        )
    return total, per_node


def _plan_target_node_ids(plan: RunSwitchPlan) -> tuple[str, ...]:
    """Return physical targets while retaining the full reviewed topology."""

    if plan.profile_stop_scope is not None:
        return tuple(plan.profile_stop_scope.target_node_ids)
    return tuple(node.node_id for node in plan.spark_group.nodes)


def _planned_transfer_parts(
    plan: RunSwitchPlan,
) -> tuple[int | None, int | None, int | None]:
    """Return Controller model download, target copy, and aggregate bytes."""

    model_download = plan.storage.missing_nas_bytes
    model_copy = plan.storage.missing_spark_bytes
    image_copy = plan.runtime_storage.missing_image_distribution_bytes
    target_copy = (
        model_copy + image_copy
        if model_copy is not None and image_copy is not None
        else None
    )
    aggregate = (
        model_download + target_copy
        if model_download is not None and target_copy is not None
        else None
    )
    return model_download, target_copy, aggregate


def effective_build_receipt(
    plan: RunSwitchPlan,
    progress: Mapping[str, object],
) -> Mapping[str, object] | None:
    """Resolve the exact OCI receipt available to later transfer phases.

    A pending container build cannot put its output digest in the immutable
    preview digest.  Once the durable build child succeeds, ``_advance``
    records the receipt in ``phase_results``.  Distribution executors should
    use this helper so they bind the target assignment to that receipt while
    preserving the original plan digest.
    """

    progress_mapping = _progress_mapping(progress)
    if progress_mapping is None:
        return None
    progress = progress_mapping
    expected_build_id = plan.recipe_build_id
    expected_input = plan.build.build_input_sha256
    candidates: list[Mapping[str, object]] = []
    if plan.image_digest is not None:
        candidates.append(
            {
                "build_id": expected_build_id,
                "build_input_sha256": expected_input,
                "image_digest": plan.image_digest,
                "oci_layout_sha256": plan.build.oci_layout_sha256,
                "image_bytes": plan.build.image_bytes,
            }
        )
    raw_results = progress.get("phase_results")
    if isinstance(raw_results, Sequence) and not isinstance(
        raw_results, (str, bytes, bytearray)
    ):
        candidates.extend(
            result for result in reversed(raw_results) if isinstance(result, Mapping)
        )
    for result in candidates:
        if (
            expected_build_id is not None
            and result.get("build_id") != expected_build_id
        ):
            continue
        if expected_input is not None and result.get("build_input_sha256") not in {
            None,
            expected_input,
        }:
            continue
        image_digest = result.get("image_digest")
        layout_digest = result.get("oci_layout_sha256")
        image_bytes = result.get("image_bytes")
        if (
            _is_oci_digest(image_digest)
            and _is_hex_digest(layout_digest)
            and type(image_bytes) is int
            and image_bytes > 0
        ):
            return {
                "build_id": expected_build_id,
                "build_input_sha256": expected_input,
                "image_digest": image_digest,
                "oci_layout_sha256": layout_digest,
                "image_bytes": image_bytes,
            }
    return None


def _require_profile_runtime_image(
    expected: RuntimeImageIdentity, observed: Mapping[str, object]
) -> None:
    """Compare observed identity fields with the accepted image, naming drift."""
    changes = [
        name for name, value in observed.items() if getattr(expected, name) != value
    ]
    if changes:
        raise _RunSwitchDefiniteConflict(
            f"{ProfileReasonCode.RUNTIME_IMAGE_CHANGED}: "
            + ", ".join(changes)
            + " differs from the accepted image; review and load the profile again"
        )


def _build_receipt_in_session(
    session: Session,
    plan: RunSwitchPlan,
    *,
    expected_image: RuntimeImageIdentity | None = None,
) -> dict[str, object]:
    """Read and validate the successful build receipt for a pending plan."""

    build_id = plan.recipe_build_id or plan.build.build_id
    if build_id is None:
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_RECEIPT_UNAVAILABLE,
            reason=WaitReason.RECEIPT_MISSING,
        )
    build = session.get(RecipeBuild, build_id)
    if (
        build is None
        or build.state != "succeeded"
        or build.build_input_sha256 != plan.build.build_input_sha256
        or not _is_oci_digest(build.image_digest)
        or not _is_hex_digest(build.oci_layout_sha256)
        or type(build.image_bytes) is not int
        or build.image_bytes < 1
    ):
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_RECEIPT_UNAVAILABLE,
            reason=WaitReason.RECEIPT_MISSING,
        )
    if expected_image is not None:
        _require_profile_runtime_image(
            expected_image,
            {
                "build_id": build.id,
                "image_digest": build.image_digest,
                "oci_layout_sha256": build.oci_layout_sha256,
                "image_bytes": build.image_bytes,
            },
        )
    return {
        "build_id": build.id,
        "build_input_sha256": build.build_input_sha256,
        "image_digest": build.image_digest,
        "oci_layout_sha256": build.oci_layout_sha256,
        "image_bytes": build.image_bytes,
        "state": "succeeded",
    }


def _bound_workload_intent(progress: Mapping[str, object]) -> int:
    ordinal = progress.get("workload_intent_ordinal")
    if type(ordinal) is not int or ordinal < 1:
        raise RunSwitchRetryLater("run-switch workload intent is unbound")
    return ordinal


def _progress_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _progress_state(value: object) -> RunSwitchMemberState | None:
    try:
        return _MEMBER_STATE_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        return None


def _progress_operation_state(value: object) -> RunSwitchProgressState:
    try:
        return _PROGRESS_STATE_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        return "unknown"


def _progress_subphase(value: object) -> RunSwitchSubphase | None:
    try:
        return _SUBPHASE_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        return None


def _observe_progress(previous: object, current: object, now: datetime) -> Any:
    """Sample the typed operation meter from the documents the run keeps."""

    sampled = sample_progress(
        read_stored_model(OperationProgress, previous) if previous else None,
        read_stored_model(OperationProgress, current),
        now,
    )
    return progress_document(sampled)


def _progress_phase(value: object) -> RunSwitchPhaseKind | None:
    return value if value in _PHASES else None


def _progress_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json")
        except (TypeError, ValueError):
            return None
        return dumped if isinstance(dumped, Mapping) else None
    return None


def _stored_result(value: object) -> RunSwitchOperationResult | Residue | None:
    """Parse stored JSON strictly, including nested datetime and tuple fields.

    A damaged stored result is a :class:`Residue` (typed unknown), never an
    exception: a reader shows what it can, and the advancing tick retires the
    one operation (``_reject_invalid_operation``) while retaining the bytes.
    """

    if value is None:
        return None
    loaded = read_or_rebuild(
        kind="run-switch.result",
        subject="stored-result",
        read=lambda: read_stored_document(
            lambda document: RunSwitchOperationResult.model_validate_json(
                json.dumps(document), strict=True
            ),
            value,
        ),
    )
    return loaded


def _parse_persisted_result(value: object) -> RunSwitchOperationResult | None:
    result = _stored_result(value)
    return None if isinstance(result, Residue) else result


def _progress_damaged(value: object) -> bool:
    """Whether the stored result exists but cannot be read (evidence unknown)."""

    return isinstance(_stored_result(value), Residue)


def _read_progress(value: object) -> dict[str, object]:
    result = _parse_persisted_result(value)
    return (
        result.model_dump(mode="json", exclude_unset=True) if result is not None else {}
    )


_WAIT_CODE = re.compile(
    r"^(run-switch\.[a-z0-9-]+|[a-z][a-z0-9_]*\.[a-z0-9_.-]+)(?=[:;,]|$)"
)


_WAIT_PHRASES = (
    ("Waiting for a target Spark", RunSwitchCode.TARGET_NOT_ACTIVE),
    ("Runtime image preparation", RunSwitchCode.RUNTIME_IMAGE_PREPARING),
    ("Waiting for exact run and route", RunSwitchCode.FINAL_VERIFICATION),
    ("Lifecycle effect is uncertain", RunSwitchCode.EFFECT_UNCERTAIN),
)


def _wait_code(reason: str, fallback: str) -> str:
    text = reason.strip()
    match = _WAIT_CODE.match(text)
    if match:
        return match.group(1)
    return next(
        (code for phrase, code in _WAIT_PHRASES if text.startswith(phrase)), fallback
    )


def _wait_blockers(job: Job, progress: Mapping[str, object]) -> list[OperationBlocker]:
    """The reasons a queued, waiting or retrying operation is not moving."""

    reason = job.status_reason or ""
    nodes = job.targets if isinstance(job.targets, list) else []
    if job.state in job_states.words(
        LifecycleState.OBSERVING, LifecycleState.NEEDS_OPERATOR
    ):
        return [
            make_blocker(
                _wait_code(reason, RunSwitchCode.WAITING),
                reason or "waiting for the next check",
                node_ids=nodes,
            )
        ]
    retry_reason = progress.get("retry_reason")
    if (
        job.state == "running"
        and isinstance(retry_reason, str)
        and retry_reason
        and progress.get("observation_due_at") is not None
    ):
        # A phase that will be tried again is waiting, not failed.  The retry
        # names its own cause: the underlying typed code first, then the
        # detail the phase reported (never just a generic busy marker).
        cause = reason.split("; admission retry", 1)[0].strip()
        return [
            make_blocker(
                PHASE_RETRY_CODE,
                cause
                if cause.startswith(retry_reason)
                else f"{retry_reason}: {cause}"
                if cause
                else retry_reason,
                node_ids=nodes,
            )
        ]
    if job.state == "running" and reason.startswith("Start result uncertain"):
        return [
            make_blocker(
                RunSwitchCode.START_OBSERVATION,
                reason,
                severity="info",
                node_ids=nodes,
            )
        ]
    return []


def _persisted_result(value: Mapping[str, object]) -> dict[str, object]:
    """Validate the same canonical JSON contract before storing a result."""

    return _read_progress(value)


_PHASE_RESULT_ADAPTER = TypeAdapter(RunSwitchPhaseResult)


def _phase_result(
    value: Mapping[str, object],
    *,
    phase: RunSwitchPhase | None = None,
) -> dict[str, object]:
    """Validate one phase receipt before it enters durable progress."""

    result: RunSwitchPhaseResult | None = None
    failure: Exception | None = None
    try:
        normalized = dict(value)
        belongs = True
        if phase is not None:
            belongs = "phase" not in normalized or normalized["phase"] == phase.kind
            normalized.setdefault("phase", phase.kind)
            subphase = getattr(phase, "subphase", None)
            if subphase is None and phase.kind in {"transfer", "verify"}:
                subphase = "target-copy"
            # A receipt of another phase or subphase is not this phase's.
            belongs = belongs and (
                "subphase" not in normalized or normalized["subphase"] == subphase
            )
            normalized.setdefault("subphase", subphase)
        assignments = normalized.get("assignments")
        if isinstance(assignments, Mapping):
            normalized["assignments"] = {
                node_id: NodeDistributionAssignment.parse(raw)
                if isinstance(raw, Mapping)
                else raw
                for node_id, raw in assignments.items()
            }
        if belongs:
            result = _PHASE_RESULT_ADAPTER.validate_python(normalized, strict=True)
    except (TypeError, ValueError) as error:
        failure = error
    if result is None:
        raise RunSwitchRetryLater(
            "run-switch phase receipt is invalid",
            reason=WaitReason.RECEIPT_MISSING,
        ) from failure
    return result.model_dump(mode="json", exclude_unset=True)


def _child_progress_payload(child: object) -> Mapping[str, object]:
    """Project durable child result/progress without exposing the child ID."""

    payload: dict[str, object] = {}
    child_progress = _progress_mapping(getattr(child, "progress", None))
    if child_progress is not None:
        payload.update(child_progress)
    child_result = _progress_mapping(getattr(child, "result", None))
    if child_result is not None:
        nested = _progress_mapping(child_result.get("progress"))
        if nested is not None:
            payload.update(nested)
        payload.update(child_result)
    child_state = getattr(child, "state", None)
    if isinstance(child_state, str):
        payload["child_state"] = child_state
    child_reason = getattr(child, "status_reason", None)
    if isinstance(child_reason, str) and child_reason:
        payload["status_reason"] = child_reason[:512]
    return payload


def _child_failure_code(evidence: Mapping[str, object]) -> str | None:
    """Carry the child's typed error code; a security code anywhere wins."""

    codes: list[str] = []
    code = evidence.get("error_code")
    if isinstance(code, str) and code:
        codes.append(code)
    for field in ("node_evidence", "launch_evidence"):
        members = evidence.get(field)
        if isinstance(members, Mapping):
            codes.extend(
                item["error_code"]
                for item in members.values()
                if isinstance(item, Mapping)
                and isinstance(item.get("error_code"), str)
                and item["error_code"]
            )
    return next(
        (value for value in codes if is_security_failure(value)),
        codes[0] if codes else None,
    )


def _child_failure_kind(child: object) -> FailureKind:
    """Read typed child evidence; an unknown mixed result stays blocked."""

    payload = _child_progress_payload(child)
    if (
        "failure_kind" in payload
        or "error_code" in payload
        or payload.get("uncertain") is True
    ):
        return kind_for_agent_error(payload)
    kinds: list[FailureKind] = []
    for field in ("node_evidence", "launch_evidence"):
        evidence = payload.get(field)
        if isinstance(evidence, Mapping):
            kinds.extend(
                kind_for_agent_error(item)
                for item in evidence.values()
                if isinstance(item, Mapping)
            )
    if kinds and all(kind is FailureKind.TEMPORARY_DEPENDENCY for kind in kinds):
        return FailureKind.TEMPORARY_DEPENDENCY
    if kinds and all(kind is FailureKind.UNCERTAIN_EFFECT for kind in kinds):
        return FailureKind.UNCERTAIN_EFFECT
    return FailureKind.INVALID_CONTRACT


def _without_observation_time(value: Mapping[str, object]) -> dict[str, object]:
    """Ignore only the clock/rate projection of the typed operation meter."""

    result = dict(value)
    operation = result.get("operation")
    if isinstance(operation, Mapping):
        result["operation"] = {
            key: item
            for key, item in operation.items()
            if key
            not in {
                "observed_at",
                "last_progress_at",
                "elapsed_seconds",
                "bytes_per_second",
                "smoothed_bytes_per_second",
                "eta_seconds",
            }
        }
    return result


class _EstablishedEffect:
    """Present a parked child whose effect the lifecycle owner has observed.

    The ordinary success path records exactly the checkpoint the missing
    acknowledgement would have produced, so the run keeps one identity and the
    final verification phase re-confirms it under current authority.
    """

    def __init__(self, child: object, run_id: str) -> None:
        self._child = child
        self.state = "succeeded"
        self.owner_id = run_id

    def __getattr__(self, name: str) -> object:
        return getattr(self._child, name)


def _established_start_effect(
    lifecycle: object,
    phase: RunSwitchPhase,
    child: object,
) -> str | None:
    """Return the run id when a parked start already produced its effect.

    A start whose result never reached the Controller can still have brought
    the run up: the acknowledgement is missing, not the effect.  The lifecycle
    owner observes the exact run, and only an established effect is accepted;
    anything else keeps its real failure, so a retry policy never conceals a
    permanently bad model or runtime.
    """

    if phase.kind != "start" or getattr(child, "state", None) not in job_states.words(
        LifecycleState.NEEDS_OPERATOR
    ):
        return None
    run_id = getattr(child, "owner_id", None)
    if not isinstance(run_id, str) or not run_id:
        return None
    getter = getattr(lifecycle, "run_status", None)
    if not callable(getter):
        return None
    try:
        status: Any = getter(run_id)
    except (KeyError, RuntimeError, TypeError, ValueError):
        return None
    if status.healthy:
        return run_id
    return None


def _start_still_progressing(
    lifecycle: object,
    phase: RunSwitchPhase,
    child: object,
) -> bool:
    if phase.kind != "start" or getattr(child, "state", None) not in job_states.words(
        LifecycleState.NEEDS_OPERATOR
    ):
        return False
    run_id = getattr(child, "owner_id", None)
    getter = getattr(lifecycle, "run_status", None)
    if not isinstance(run_id, str) or not callable(getter):
        return False
    try:
        status: Any = getter(run_id)
    except (KeyError, RuntimeError, TypeError, ValueError):
        return False
    return status.state in ACTIVE_RUN_STATES and any(
        rank.fresh
        and rank.state in {RunState.PLANNED, RunState.STARTING, RunState.RUNNING}
        for rank in status.ranks
    )


def _failure_code_of(error: BaseException) -> str | None:
    """The code ``fail`` classifies: an authentication refusal from a dependency
    (HTTP 401/403) is a real security boundary and the only terminal case; every
    other error is an unknown that is observed again."""

    if isinstance(error, httpx2.HTTPError):
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status in {401, 403}:
            return str(status)
    return error_code(error)


def _same_intent(existing: Job, intent: Mapping[str, object] | None) -> bool:
    """A reused request key must name the same accepted intent, not only kind."""

    stored = (
        existing.payload.get("intent")
        if isinstance(existing.payload, Mapping)
        else None
    )
    if stored is None or intent is None:
        return True
    return canonical_message(stored) == canonical_message(dict(intent))


def _require_reviewed_plan(reviewed_digest: str | None, plan: RunSwitchPlan) -> None:
    if reviewed_digest is not None and reviewed_digest != plan.plan_digest:
        raise RunSwitchRequestInvalid(
            f"{RunSwitchCode.STALE_PLAN}: effects changed; review the current plan",
            reason=InvalidRequestReason.SUPERSEDED,
        )


def _plan_blockers_are_waitable(plan: RunSwitchPlan) -> bool:
    return bool(plan.blockers) and not any(
        is_security_failure(reason.code) for reason in plan.blockers
    )


def _phase_request_key(
    operation_id: str, phase_index: int, item_index: int, attempt: int
) -> str:
    """Keep one idempotency key per exact phase attempt."""
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"vonk:run-switch-phase:{operation_id}:{phase_index}:{item_index}:{attempt}",
        )
    )


def _lock_phase_owner(
    session: Session, request_key: str, phase_index: int, item_index: int
) -> Job | None:
    """Lock the RunSwitch whose phase ``request_key`` names, or return None.

    A phase runs under its operation's own request key or, after a retry or
    re-plan, under the current generation's ``_phase_request_key``. Both name
    the same owner; a key from an earlier generation names none. Only the
    matched row is locked (``NOWAIT``; the caller maps a busy lock and checks
    the owner's state and checkpoint itself).
    """

    def locked(condition: Any) -> Job | None:
        return session.scalar(
            select(Job)
            .where(condition)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )

    def current_key(job: Job) -> bool:
        generation = (job.result or {}).get("phase_retry_generation")
        return (
            type(generation) is int
            and generation > 0
            and request_key
            == _phase_request_key(job.request_id, phase_index, item_index, generation)
        )

    job = locked(Job.request_id == request_key)
    if job is not None:
        return job
    # A derived key cannot be inverted; only an active operation can own it.
    owner = next(
        (
            job
            for job in session.scalars(
                select(Job).where(
                    Job.kind == "recipe.run-switch.v2",
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.OBSERVING,
                        )
                    ),
                )
            )
            if current_key(job)
        ),
        None,
    )
    job = locked(Job.id == owner.id) if owner is not None else None
    return job if job is not None and current_key(job) else None


def _progress_member_entries(value: object) -> list[Mapping[str, object]]:
    if isinstance(value, Mapping):
        entries: list[Mapping[str, object]] = []
        for node_id, raw in value.items():
            item = _progress_mapping(raw)
            if item is None:
                continue
            if "node_id" not in item:
                item = {**item, "node_id": node_id}
            entries.append(item)
        return entries
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for raw in value if (item := _progress_mapping(raw)) is not None]
    return []


def _complete_cancellation(
    job: Job, progress: dict[str, object], now: datetime
) -> None:
    """End a cancelled operation: the child's phase ended (or none was issued).

    A cancellation record that cannot be read does not refuse the cancel: the
    operation still ends ``cancelled`` (rule 4), just without the requester's text.
    """

    _ADAPTER.cancelled(job, progress, now, reason=None)
    job.result = _persisted_result(progress)
    job.updated_at = now


def _lock_current_build_parent(
    session: Session,
    *,
    plan: RunSwitchPlan,
    phase_index: int,
    item_index: int,
    actor: str,
    request_key: str,
    ordinal: int,
    child_id: object = None,
    cancellation: object = None,
) -> Job:
    """Fence build admission/observations with the same current parent intent.

    Lock scope nodes (including an external builder) in stable order, then the
    parent, before the lifecycle service locks the build. All acquisition is
    nonblocking and the caller retains these locks through its SQL commit.
    """
    if not 0 <= phase_index < len(plan.phases) or (
        plan.phases[phase_index].kind,
        plan.phases[phase_index].subphase,
        plan.phases[phase_index].state,
    ) != ("prepare", "container-build", "planned"):
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID, reason=WaitReason.STALE_PLAN
        )
    targets = {node.node_id for node in plan.spark_group.nodes}
    locked_nodes = targets | (
        {plan.build.builder_node_id} if plan.build.builder_node_id else set()
    )
    try:
        nodes = tuple(
            session.scalars(
                select(AgentNode)
                .where(AgentNode.node_id.in_(locked_nodes))
                .order_by(AgentNode.node_id)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        )
        job = _lock_phase_owner(session, request_key, phase_index, item_index)
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != "55P03":
            raise
        raise RecipeBuildAdmissionBusy() from error
    if (
        job is None
        or job.kind != "recipe.run-switch.v2"
        or job.actor != actor
        or set(job.targets) != targets
        or len(nodes) != len(locked_nodes)
        or job.payload.get("workload_intent_ordinal") != ordinal
    ):
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_PARENT_INVALID,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if _load_plan(job.payload["plan"]) != plan:
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_PLAN_INVALID, reason=WaitReason.STALE_PLAN
        )
    current = _read_progress(job.result)
    if _bound_workload_intent(current) != ordinal:
        raise RunSwitchRetryLater(
            RunSwitchCode.CONTAINER_BUILD_PARENT_INVALID,
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    if (
        not _checkpoint_matches(job, current, phase_index, item_index, child_id)
        or current.get("cancellation") != cancellation
        or RunSwitchOperationService._scope_intent_status(session, job) != "current"
    ):
        raise _RunSwitchBuildParentChanged(RunSwitchCode.CONTAINER_BUILD_PARENT_CHANGED)
    return job


def _checkpoint_matches(
    job: Job,
    progress: Mapping[str, object],
    phase_index: int,
    item_index: int,
    child_id: object,
) -> bool:
    """Accept an out-of-transaction observation only for its original checkpoint."""
    return (
        job.state in {"queued", "running"}
        and progress.get("phase_index", 0) == phase_index
        and progress.get("item_index", 0) == item_index
        and progress.get("child_operation_id") == child_id
    )


def _merge_progress_evidence(
    progress: dict[str, object],
    plan: RunSwitchPlan,
    phase: RunSwitchPhase,
    evidence: object,
    now: datetime | None = None,
) -> None:
    """Persist bounded child byte/member evidence for restart-safe polling."""

    payload = _progress_mapping(evidence)
    if payload is None:
        return
    nested = _progress_mapping(payload.get("progress"))
    if nested is not None:
        _merge_progress_evidence(progress, plan, phase, nested, now)

    canonical = _progress_mapping(payload.get("operation"))
    if canonical is not None:
        progress["operation"] = read_stored_model(
            OperationProgress, canonical
        ).model_dump(mode="json", exclude_none=True)
        progress["operation_phase_index"] = phase.index

    current_completed = _progress_int(progress.get("completed_bytes")) or 0
    reported_completed = next(
        (
            _progress_int(payload.get(key))
            for key in (
                "completed_bytes",
                "copied_bytes",
                "downloaded_bytes",
            )
            if _progress_int(payload.get(key)) is not None
        ),
        None,
    )
    if reported_completed is not None:
        model_download, _target_copy, _aggregate = _planned_transfer_parts(plan)
        offset = (
            model_download
            if phase.subphase == "target-copy" and model_download is not None
            else 0
        )
        progress["completed_bytes"] = max(
            current_completed, offset + reported_completed
        )
    if (
        now is not None
        and reported_completed is not None
        and nested is None
        and canonical is None
    ):
        prior = _progress_mapping(progress.get("operation"))
        values = {
            key: value
            for key, value in payload.items()
            if key in OperationProgress.model_fields
            and key not in {"members", "checkpoint"}
        }
        values["phase"] = phase.kind
        values["completed_bytes"] = reported_completed
        if "total_bytes" in values:
            values["total_bytes_known"] = values["total_bytes"] is not None
        else:
            values["total_bytes_known"] = False
        # Child receipts may report phase-local bytes. Keep the nested measurement
        # phase-local too: an ETA for future build/start work would be invented.
        current = read_stored_model(OperationProgress, values).model_dump(
            mode="json", exclude_none=True
        )
        if prior is not None and prior.get("phase") == phase.kind:
            current["completed_bytes"] = max(
                require_integer(prior.get("completed_bytes", 0), "completed bytes"),
                reported_completed,
            )
        progress["operation"] = _observe_progress(prior, current, now)
        progress["operation_phase_index"] = phase.index
    reported_total = next(
        (
            _progress_int(payload.get(key))
            for key in ("total_bytes",)
            if _progress_int(payload.get(key)) is not None
        ),
        None,
    )
    if reported_total is not None and progress.get("total_bytes") is None:
        model_download, _target_copy, _aggregate = _planned_transfer_parts(plan)
        if phase.subphase == "target-copy" and model_download is not None:
            progress["total_bytes"] = model_download + reported_total
            progress["total_bytes_known"] = True

    member_values = payload.get("members", payload.get("member_progress"))
    entries = _progress_member_entries(member_values)
    if not entries:
        return
    known_nodes = set(_plan_target_node_ids(plan))
    existing = {
        str(item.get("node_id")): dict(item)
        for item in _progress_member_entries(progress.get("members"))
        if isinstance(item.get("node_id"), str) and item.get("node_id") in known_nodes
    }
    for item in entries:
        node_id = item.get("node_id")
        if not isinstance(node_id, str) or node_id not in known_nodes:
            continue
        target = existing.setdefault(node_id, {"node_id": node_id})
        completed = _progress_int(item.get("completed_bytes"))
        if completed is not None:
            target["completed_bytes"] = max(
                _progress_int(target.get("completed_bytes")) or 0,
                completed,
            )
        total = _progress_int(item.get("total_bytes"))
        if total is not None:
            target["total_bytes"] = total
        member_phase = _progress_phase(item.get("phase"))
        if member_phase is not None:
            target["phase"] = member_phase
        member_state = _progress_state(item.get("state"))
        if member_state is not None:
            set_member_state(target, member_state)
        error = item.get("error")
        if isinstance(error, str):
            target["error"] = error[:256]
    progress["members"] = list(existing.values())


def _complete_phase_progress(
    progress: dict[str, object],
    plan: RunSwitchPlan,
    phase: RunSwitchPhase,
) -> None:
    # Progress resets the retry backoff of the accepted intent.
    progress.pop("retry_attempt", None)
    if phase.kind != "transfer":
        return
    model_download, target_copy, aggregate = _planned_transfer_parts(plan)
    if phase.subphase == "model-download":
        if model_download is not None:
            progress["completed_bytes"] = max(
                _progress_int(progress.get("completed_bytes")) or 0,
                model_download,
            )
        return
    if phase.subphase != "target-copy":
        return
    _total, member_totals = _planned_transfer_bytes(plan)
    if aggregate is not None:
        progress["completed_bytes"] = aggregate
        progress["total_bytes"] = aggregate
        progress["total_bytes_known"] = True
    elif target_copy is not None:
        progress["completed_bytes"] = max(
            _progress_int(progress.get("completed_bytes")) or 0,
            target_copy + (model_download or 0),
        )
    entries = {
        str(item.get("node_id")): dict(item)
        for item in _progress_member_entries(progress.get("members"))
        if isinstance(item.get("node_id"), str)
    }
    for node_id, member_total in member_totals.items():
        item = entries.setdefault(node_id, {"node_id": node_id})
        if member_total is not None:
            item["total_bytes"] = member_total
            item["completed_bytes"] = member_total
        item["phase"] = phase.kind
        set_member_state(item, "succeeded")
        item["error"] = None
    progress["members"] = list(entries.values())


def _complete_operation_progress(
    plan: RunSwitchPlan,
    progress: dict[str, object],
) -> dict[str, object]:
    total, member_totals = _planned_transfer_bytes(plan)
    if total is not None:
        progress["completed_bytes"] = total
        progress["total_bytes"] = total
        progress["total_bytes_known"] = True
    entries = {
        str(item.get("node_id")): dict(item)
        for item in _progress_member_entries(progress.get("members"))
        if isinstance(item.get("node_id"), str)
    }
    for node_id, member_total in member_totals.items():
        item = entries.setdefault(node_id, {"node_id": node_id})
        if member_total is not None:
            item["total_bytes"] = member_total
            item["completed_bytes"] = member_total
        item["phase"] = "final_verify"
        set_member_state(item, "succeeded")
        item["error"] = None
    progress["members"] = list(entries.values())
    progress["phase"] = "final_verify"
    progress["subphase"] = None
    progress["retryable"] = False
    progress["failed_phase"] = None
    progress.pop("failure_code", None)
    progress["child_operation_id"] = None
    return progress


#: The one placeholder member of a damaged operation that records no targets.
_UNKNOWN_MEMBER_NODE_ID = "spk_" + "0" * 32


def _progress_view(
    plan: RunSwitchPlan | None,
    raw: Mapping[str, object],
    operation_state: str,
    status_reason: str | None,
) -> RunSwitchProgress:
    """Build the stable operation progress DTO from durable JSON state."""

    if plan is None:
        node_ids = [
            str(value)
            for value in require_sequence(raw.get("node_ids", []), "node ids")
            if isinstance(value, str)
        ]
        phase_count = 1
        phase_index = 0
        phase = None
        subphase: RunSwitchSubphase | None = None
        total = _progress_int(raw.get("total_bytes"))
        member_totals = {node_id: None for node_id in node_ids}
    else:
        node_ids = list(_plan_target_node_ids(plan))
        phase_count = max(1, len(plan.phases))
        raw_index = raw.get("phase_index")
        phase_index = raw_index if type(raw_index) is int and raw_index >= 0 else 0
        phase_index = min(phase_index, 31)
        phase = _progress_phase(raw.get("phase"))
        if phase is None and phase_index < len(plan.phases):
            phase = plan.phases[phase_index].kind
        subphase = _progress_subphase(raw.get("subphase"))
        if subphase is None:
            subphase = (
                plan.phases[phase_index].subphase
                if phase_index < len(plan.phases)
                else None
            )
        total, member_totals = _planned_transfer_bytes(plan)
        if total is None:
            candidate_total = _progress_int(raw.get("total_bytes"))
            total = candidate_total

    state = _progress_operation_state(operation_state)
    if state == "succeeded":
        phase = "final_verify"
        subphase = None
        phase_index = min(max(phase_index, phase_count - 1), 31)
    completed = _progress_int(raw.get("completed_bytes")) or 0
    if state == "succeeded" and total is not None:
        completed = total
    elif plan is not None and total is not None:
        # NAS download and Spark distribution are distinct transfer checkpoints.
        # Only their persisted byte counters prove how much actually completed.
        completed = min(completed, total)
    if not node_ids:
        # Rebuild the group from the member receipts the operation recorded.
        recorded = (
            item.get("node_id")
            for item in _progress_member_entries(
                raw.get("members", raw.get("member_progress"))
            )
        )
        node_ids = [
            value
            for value in recorded
            if isinstance(value, str) and re.fullmatch(r"spk_[0-9a-f]{32}", value)
        ][:32]
    raw_members = {
        str(item.get("node_id")): item
        for item in _progress_member_entries(
            raw.get("members", raw.get("member_progress"))
        )
        if isinstance(item.get("node_id"), str) and item.get("node_id") in set(node_ids)
    }
    current_nodes: set[str] = set()
    if plan is not None and phase_index < len(plan.phases):
        current_nodes = set(plan.phases[phase_index].node_ids)
    completed_phases = raw.get("completed_phases")
    completed_set = (
        {value for value in completed_phases if value in _PHASES}
        if isinstance(completed_phases, Sequence)
        and not isinstance(completed_phases, (str, bytes, bytearray))
        else set()
    )
    members: list[RunSwitchMemberProgress] = []
    for node_id in node_ids:
        item = raw_members.get(node_id, {})
        member_total = _progress_int(item.get("total_bytes"))
        if member_total is None:
            member_total = member_totals.get(node_id)
        member_completed = _progress_int(item.get("completed_bytes")) or 0
        member_completed = (
            min(member_completed, member_total)
            if member_total is not None
            else member_completed
        )
        member_state = _progress_state(item.get("state"))
        if member_state is None:
            if state == "succeeded":
                member_state = "succeeded"
            elif state == "failed" and node_id in current_nodes:
                member_state = "failed"
            elif state == "running" and node_id in current_nodes:
                member_state = "running"
            elif state == "queued" or state == "running" and completed_set:
                member_state = "pending"
            else:
                member_state = "unknown"
        member_phase = _progress_phase(item.get("phase")) or phase
        raw_error = item.get("error")
        error = raw_error if isinstance(raw_error, str) else None
        if state == "failed" and node_id in current_nodes and error is None:
            error = status_reason[:256] if isinstance(status_reason, str) else None
        members.append(
            RunSwitchMemberProgress(
                node_id=node_id,
                phase=member_phase,
                state=member_state,
                completed_bytes=member_completed,
                total_bytes=member_total,
                error=error,
            )
        )
    if not members:
        # High-level operations always have a complete group.  A damaged
        # historical row without any recorded target is unknown, not an error:
        # keep a valid DTO (one unknown member) so the API still reports the
        # operation's durable state.
        retire_as_unknown(
            "run-switch.progress",
            "no-target-members",
            BookkeepingReason.ROW_INCOMPLETE,
            "operation has no recorded target members",
        )
        members.append(
            RunSwitchMemberProgress(
                node_id=_UNKNOWN_MEMBER_NODE_ID,
                phase=phase,
                state="unknown",
                completed_bytes=0,
                total_bytes=None,
                error="no target members are recorded for this operation",
            )
        )
    measurement = (
        read_stored_model(OperationProgress, raw["operation"])
        if raw.get("operation")
        else None
    )
    if measurement is not None:
        pending_preflight = _progress_mapping(raw.get("preflight"))
        measuring_preflight = bool(
            pending_preflight and pending_preflight.get("pending_job_id")
        )
        if (
            raw.get("operation_phase_index") != phase_index and not measuring_preflight
        ) or state not in {"queued", "running"}:
            measurement = measurement.model_copy(
                update={
                    "phase": phase or "unknown",
                    "bytes_per_second": None,
                    "smoothed_bytes_per_second": None,
                    "eta_seconds": None,
                    "activity": None,
                }
            )
        else:
            measurement = project_progress(measurement)
    raw_start_deadline = raw.get("start_deadline")
    return RunSwitchProgress(
        operation=measurement,
        startup_budget_seconds=_progress_int(raw.get("startup_budget_seconds")),
        start_deadline=(
            _aware(datetime.fromisoformat(raw_start_deadline))
            if isinstance(raw_start_deadline, str)
            else None
        ),
        phase_index=phase_index,
        phase_count=phase_count,
        phase=phase,
        state=state,
        completed_bytes=completed,
        total_bytes=total,
        total_bytes_known=total is not None,
        subphase=subphase,
        members=members,
    )


def _validate_artifact_execution(
    plan: RunSwitchPlan,
    phase: RunSwitchPhase,
    result: object,
    *,
    expected_image: RuntimeImageIdentity | None = None,
) -> None:
    """Enforce evidence required from an injected artifact phase adapter."""

    if not isinstance(result, Mapping):
        raise RunSwitchRetryLater(f"run-switch.{phase.kind}-returned-invalid-evidence")
    if phase.kind == "prepare" and phase.subphase == "runtime-image":
        try:
            receipt = read_stored_model(
                RuntimeImageReceiptDocument, result.get("runtime_image"), strict=True
            )
        except (TypeError, ValidationError) as error:
            raise RunSwitchRetryLater(
                RunSwitchCode.RUNTIME_IMAGE_PREPARATION_RECEIPT_INVALID,
                reason=WaitReason.RECEIPT_MISSING,
            ) from error
        image_digest = receipt.image_digest
        layout_digest = receipt.oci_archive_sha256
        if expected_image is not None:
            _require_profile_runtime_image(
                expected_image,
                {
                    "image_digest": image_digest,
                    "oci_layout_sha256": layout_digest,
                    "image_bytes": receipt.image_bytes,
                    "architecture": receipt.architecture,
                    "runtime_interface": receipt.runtime_interface,
                },
            )
        differing = differing_image_fields(
            ImageContent(
                image_digest=plan.image_digest,
                archive_sha256=plan.build.oci_layout_sha256,
            ),
            receipt,
        )
        if "image_digest" in differing:
            raise RunSwitchRefused(
                SecurityRefusalReason.RUN_SWITCH_RUNTIME_IMAGE_PREPARATION_DIGEST_MISMATCH.value
            )
        if "archive_sha256" in differing:
            raise RunSwitchRetryLater(
                RunSwitchCode.RUNTIME_IMAGE_PREPARATION_LAYOUT_MISMATCH,
                reason=WaitReason.SCOPE_CHANGED,
            )
        return
    if phase.kind == "transfer" and phase.subphase == "model-download":
        preparation = plan.preparation
        expected_set = (
            preparation.model.artifact_set_sha256
            if preparation is not None
            else plan.storage.artifact_set_sha256
        )
        if result.get("artifact_set_sha256") != expected_set:
            raise RunSwitchRetryLater(
                RunSwitchCode.MODEL_DOWNLOAD_ARTIFACT_SET_MISMATCH,
                reason=WaitReason.SCOPE_CHANGED,
            )
        if result.get("coverage") != "complete":
            raise RunSwitchRetryLater(RunSwitchCode.MODEL_DOWNLOAD_COVERAGE_INCOMPLETE)
        expected_bytes = (
            preparation.model.artifact_set_bytes
            if preparation is not None
            else plan.storage.artifact_set_bytes
        )
        completed = result.get("downloaded_bytes")
        total = result.get("total_bytes")
        planned_transfer = plan.storage.missing_nas_bytes
        if planned_transfer is None:
            # An unknown plan total must remain unknown all the way through a
            # terminal child result; a full manifest size is not a valid
            # substitute for a missing transfer estimate.
            valid_bytes = completed is None and total is None
        else:
            valid_bytes = (
                type(expected_bytes) is int
                and type(completed) is int
                and type(total) is int
                and completed >= 0
                and total >= 0
                and completed == total == planned_transfer
                and total <= expected_bytes
            )
        if not valid_bytes:
            raise RunSwitchRetryLater(
                RunSwitchCode.MODEL_DOWNLOAD_BYTE_EVIDENCE_MISMATCH,
                reason=WaitReason.SCOPE_CHANGED,
            )
        return
    if phase.kind == "verify":
        try:
            normalized = dict(result)
            normalized.setdefault("phase", "verify")
            normalized.setdefault("subphase", "target-copy")
            verification = read_stored_model(
                RunSwitchVerifyResult, normalized, strict=True
            )
        except (TypeError, ValidationError) as error:
            if (
                plan.recipe_build_id is not None
                and isinstance(result, Mapping)
                and result.get("verified_build_id") != plan.recipe_build_id
            ):
                raise RunSwitchRetryLater(
                    RunSwitchCode.RUNTIME_BUILD_VERIFICATION_MISMATCH,
                    reason=WaitReason.SCOPE_CHANGED,
                ) from error
            raise RunSwitchRetryLater(
                RunSwitchCode.ARTIFACT_VERIFICATION_RESULT_INVALID,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            ) from error
        result = verification.model_dump(mode="python")
        if result.get("verified") is not True:
            raise RunSwitchRefused(
                SecurityRefusalReason.RUN_SWITCH_ARTIFACT_DIGEST_VERIFICATION_FAILED.value
            )
        if verification.verified_build_id != plan.recipe_build_id:
            # A build performed by the same high-level operation has no OCI
            # output digest at preview time.  The distribution adapter must
            # bind its verification receipt to the exact durable build row.
            raise RunSwitchRetryLater(
                RunSwitchCode.RUNTIME_BUILD_VERIFICATION_MISMATCH,
                reason=WaitReason.SCOPE_CHANGED,
            )
    elif phase.kind == "cleanup":
        if result.get("scope") != "spark-local":
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_SCOPE_INVALID, reason=WaitReason.SCOPE_CHANGED
            )
        if result.get("nas_evicted") is True:
            raise RunSwitchRefused(
                SecurityRefusalReason.RUN_SWITCH_CLEANUP_NAS_EVICTION_FORBIDDEN.value
            )
        reclaimed = result.get("reclaimed_bytes")
        if type(reclaimed) is not int or reclaimed < 0:
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_RECLAIM_EVIDENCE_INVALID,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        protected_bytes = result.get("protected_referenced_bytes")
        if type(protected_bytes) is not int or protected_bytes < 0:
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_REFERENCE_PROTECTION_EVIDENCE_INVALID,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        raw_reclaimed = result.get("reclaimed_digests")
        raw_protected = result.get("protected_digests")
        if (
            not isinstance(raw_reclaimed, list)
            or not isinstance(raw_protected, list)
            or not all(
                _is_hex_digest(value) for value in [*raw_reclaimed, *raw_protected]
            )
            or len(set(raw_reclaimed)) != len(raw_reclaimed)
            or len(set(raw_protected)) != len(raw_protected)
        ):
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_REFERENCE_PROTECTION_EVIDENCE_INVALID,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        reclaimed_digests = set(raw_reclaimed)
        protected_digests = set(raw_protected)
        if reclaimed_digests & protected_digests:
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_REFERENCE_PROTECTION_OVERLAP
            )
        allowed_reclaimable = set(plan.storage.reclaimable_digests)
        allowed_reclaimable.update(plan.runtime_storage.reclaimable_digests)
        if not reclaimed_digests <= allowed_reclaimable:
            raise RunSwitchRefused(
                SecurityRefusalReason.RUN_SWITCH_CLEANUP_RECLAIMED_DIGEST_NOT_PLANNED.value
            )
        maximum_reclaimable = (
            plan.storage.reclaimable_bytes + plan.runtime_storage.reclaimable_bytes
        )
        if reclaimed > maximum_reclaimable:
            raise RunSwitchRetryLater(
                RunSwitchCode.CLEANUP_RECLAIMED_BYTES_EXCEED_PLAN,
                reason=WaitReason.STALE_PLAN,
            )
    elif phase.kind == "transfer":
        copied = result.get("copied_bytes")
        if copied is not None and (type(copied) is not int or copied < 0):
            raise RunSwitchRetryLater(
                RunSwitchCode.TRANSFER_BYTE_EVIDENCE_INVALID,
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )


def _stated_bytes(value: object) -> int | None:
    """A recorded size, or ``None`` when the plan does not state one."""

    return value if type(value) is int and value > 0 else None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _is_hex_digest(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_oci_digest(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and _is_hex_digest(value[7:])
    )


def _primary_model_digest(document: object) -> str | None:
    if not isinstance(document, Mapping):
        return None
    selections = document.get("models")
    selection = (
        selections[0]
        if isinstance(selections, Sequence)
        and not isinstance(selections, (str, bytes))
        and selections
        else None
    )
    model = selection.get("model") if isinstance(selection, Mapping) else None
    value = model.get("content_sha256") if isinstance(model, Mapping) else None
    return value if _is_hex_digest(value) else None


def _normalise_architecture(value: str) -> str:
    return {
        "linux-arm64": "linux/arm64",
        "linux/aarch64": "linux/arm64",
        "aarch64": "linux/arm64",
        "arm64": "linux/arm64",
    }.get(value.lower(), value.lower())


def _node_missing_bytes(
    by_node: Mapping[str, int] | None,
    node_id: str,
    total: int | None,
    target_count: int,
) -> int | None:
    """Bytes missing on ONE node, from per-node evidence only.

    ``total`` is a sum over targets and is never divided: presence is uneven
    in general (one Spark may already hold the model or image).  Without
    per-node evidence only a zero total (all present) or a single target
    (the total is that node's) is exact; otherwise return None, which callers
    treat as "missing here" so distribution copies or verifies it.
    """

    if by_node is not None and node_id in by_node:
        return by_node[node_id]
    if total == 0:
        return 0
    if total is not None and target_count == 1:
        return total
    return None


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RunSwitchRetryLater(
            "run-switch persisted identity is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return value


def _started_operation_id(value: object) -> str:
    """Read the identity of a started child operation without assuming its class."""

    operation_id = getattr(value, "id", None)
    if not isinstance(operation_id, str) or not operation_id:
        raise RunSwitchRetryLater(
            "run-switch child operation identity is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return operation_id


def _mapping_node(node: SparkGroupNode) -> Any:
    from .cluster_mappings import ClusterMappingPlacement

    return ClusterMappingPlacement(
        node_id=node.node_id,
        rank=node.rank,
        role=node.role,
        endpoint_owner=node.endpoint_owner,
    )


_DIGEST = re.compile(r"[0-9a-f]{64}")


def _view_identity(
    job: Job, plan: RunSwitchPlan | None
) -> tuple[RunSwitchAction, str, Literal["uninstall", "reconcile"] | None, str | None]:
    """Action, digest and cleanup identity of an operation, from its plan or,
    when the plan is unreadable, from the identity recorded beside it."""

    if plan is not None:
        cleanup = plan.action == "cleanup"
        return (
            plan.action,
            plan.plan_digest,
            plan.cleanup_mode if cleanup else None,
            plan.installation_id if cleanup else None,
        )
    digest = job.payload.get("plan_digest")
    recorded_action = job.payload.get("action")
    # A cleanup carries an identity (mode, installation) only its plan held.
    action: RunSwitchAction = "stop"
    for candidate in get_args(RunSwitchPlacementAction):
        if recorded_action == candidate:
            action = candidate
    return (
        action,
        digest if isinstance(digest, str) and _DIGEST.fullmatch(digest) else "0" * 64,
        None,
        None,
    )


def _load_plan(value: object) -> RunSwitchPlan | None:
    """The stored plan, or ``None`` once it cannot be read (retired as unknown).

    A damaged stored plan is never a reason to refuse: each caller either
    rebuilds from the accepted intent (re-plan), retires the one operation
    (``_reject_invalid_operation``), or carries on without the plan.
    """

    if not isinstance(value, Mapping):
        retire_as_unknown(
            "run-switch.plan",
            "stored-plan",
            BookkeepingReason.PERSISTED_STATE_DAMAGED,
            "stored plan is not a document",
        )
        return None
    # Job.payload is JSON, so strict validation must permit the RFC3339
    # timestamp representation when a worker restarts and reloads a plan.
    loaded = read_or_rebuild(
        kind="run-switch.plan",
        subject="stored-plan",
        read=lambda: read_stored_document(
            lambda document: RunSwitchPlan.model_validate_json(
                json.dumps(document), strict=True
            ),
            value,
        ),
    )
    return None if isinstance(loaded, Residue) else loaded


def _reserve_run_switch_assets(
    session: Session, plan: RunSwitchPlan, *, now: datetime
) -> None:
    """Keep exact accepted plan identities open through the Job commit."""

    model_set = plan.storage.artifact_set_sha256
    image_archive = plan.runtime_storage.oci_layout_sha256
    try:
        if model_set is not None:
            require_model_sets_open(
                session,
                (model_set,),
                now=now,
                object_digests=plan.storage.artifact_digests,
            )
        if image_archive is not None:
            require_reference_open(
                session,
                (ArtifactIdentity("runtime-image", image_archive),),
                now=now,
            )
    except ArtifactLifecycleError as error:
        raise RunSwitchRequestInvalid(f"{error.code}: {error.detail}") from error


def _persist_run_switch_runtime_image_reference(
    sessions: sessionmaker[Session],
    plan: RunSwitchPlan,
    phase: RunSwitchPhase,
    *,
    item_index: int,
    actor: str,
    request_key: str,
    progress: Mapping[str, object],
    execution_keys: Sequence[str],
    receipt: RuntimeImageReceiptDocument,
    clock: Any,
) -> None:
    """Commit this current phase's exact image reference before publication.

    The caller holds the nonblocking lock for ``receipt.oci_archive_sha256``.
    This transaction rechecks the durable RunSwitch checkpoint and current
    workload claim, then commits the reference intent while that lock is
    still held. It never waits for image work or storage from inside SQL.
    """

    try:
        parsed_receipt = read_stored_model(
            RuntimeImageReceiptDocument, receipt, strict=True
        )
    except (TypeError, ValueError) as error:
        raise _RuntimeImageIdentityUnknown(
            "verified runtime image receipt is invalid"
        ) from error
    try:
        ordinal = _bound_workload_intent(progress)
    except RunSwitchOperationConflict as error:
        raise _RuntimeImageOwnerChanged(
            "RunSwitch phase no longer has a workload claim"
        ) from error
    profile_application_id = _string_or_none(progress.get("profile_application_id"))
    target_nodes = tuple(sorted(node.node_id for node in plan.spark_group.nodes))
    recipe_revision_id = plan.recipe_revision_id
    if (
        phase.kind != "prepare"
        or phase.subphase != "runtime-image"
        or item_index != 0
        or phase.index >= len(plan.phases)
        or plan.phases[phase.index] != phase
        or not target_nodes
        or not execution_keys
        or recipe_revision_id is None
    ):
        raise _RuntimeImageIdentityUnknown(
            "runtime image callback is outside its phase"
        )
    canonical_execution_keys = tuple(sorted(set(execution_keys)))
    if len(canonical_execution_keys) != len(execution_keys):
        raise _RuntimeImageIdentityUnknown(
            "runtime image execution identities are not unique"
        )

    now = _now(clock)
    with sessions.begin() as session:
        try:
            nodes = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(target_nodes))
                    .order_by(AgentNode.node_id)
                    .with_for_update(nowait=True)
                    .execution_options(populate_existing=True)
                )
            )
        except DBAPIError as error:
            state = getattr(error.orig, "sqlstate", None) or getattr(
                error.orig, "pgcode", None
            )
            if state in {"55P03", "40P01", "40001"}:
                raise RuntimeImagePreparationUnknown(
                    ArtifactLifecycleCode.REFERENCE_BUSY,
                    "RunSwitch target ownership is changing; image publication will retry",
                    retryable=True,
                ) from error
            raise
        if len(nodes) != len(target_nodes) or any(
            node.state != "active"
            or node.revoked_at is not None
            or node.workload_intent_ordinal != ordinal
            for node in nodes
        ):
            raise _RuntimeImageOwnerChanged(
                "RunSwitch Spark scope or workload claim changed"
            )

        try:
            require_reference_open(
                session,
                (ArtifactIdentity("runtime-image", parsed_receipt.oci_archive_sha256),),
                now=now,
            )
        except ArtifactLifecycleError as error:
            raise RuntimeImagePreparationUnknown(
                error.code, error.detail, retryable=error.retryable
            ) from error

        try:
            job = _lock_phase_owner(session, request_key, phase.index, item_index)
        except DBAPIError as error:
            state = getattr(error.orig, "sqlstate", None) or getattr(
                error.orig, "pgcode", None
            )
            if state in {"55P03", "40P01", "40001"}:
                raise RuntimeImagePreparationUnknown(
                    ArtifactLifecycleCode.REFERENCE_BUSY,
                    "RunSwitch operation ownership is changing; image publication will retry",
                    retryable=True,
                ) from error
            raise
        if (
            job is None
            or job.kind != "recipe.run-switch.v2"
            or job.actor != actor
            # Background preparation publishes while its operation waits on
            # it; the exact checkpoint below still binds ownership.
            or job.state
            not in job_states.words(
                LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.OBSERVING
            )
            or tuple(sorted(job.targets)) != target_nodes
            or job.payload.get("workload_intent_ordinal") != ordinal
            or job.payload.get("plan_digest") != plan.plan_digest
        ):
            raise _RuntimeImageOwnerChanged(
                "RunSwitch operation no longer owns image publication"
            )
        raw_plan = job.payload.get("plan")
        persisted_plan = _load_plan(raw_plan)
        if persisted_plan is None:
            raise _RuntimeImageIdentityUnknown("persisted RunSwitch plan is invalid")
        if persisted_plan != plan:
            raise _RuntimeImageIdentityUnknown(
                "RunSwitch plan changed before image publication"
            )

        if _progress_damaged(job.result):
            raise _RuntimeImageOwnerChanged(
                "RunSwitch progress no longer owns image publication"
            )
        try:
            current = _read_progress(job.result)
            current_ordinal = _bound_workload_intent(current)
        except RunSwitchOperationConflict as error:
            raise _RuntimeImageOwnerChanged(
                "RunSwitch progress no longer owns image publication"
            ) from error
        if (
            current_ordinal != ordinal
            or _string_or_none(current.get("profile_application_id"))
            != profile_application_id
            or current.get("cancellation") is not None
            # The operation waits on this very background preparation, so the
            # same phase/item checkpoint also owns publication while waiting.
            or job.state
            not in job_states.words(
                LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.OBSERVING
            )
            or current.get("phase_index", 0) != phase.index
            or current.get("item_index", 0) != item_index
            or current.get("child_operation_id") is not None
            or RunSwitchOperationService._scope_intent_status(session, job) != "current"
        ):
            raise _RuntimeImageOwnerChanged(
                "RunSwitch phase was cancelled or superseded"
            )

        # An image is its content: the publisher, slug, revision and build a
        # stored receipt first recorded are provenance of whoever asked first,
        # not a property of this plan, so they are not compared.
        expected_layout = (
            plan.runtime_storage.oci_layout_sha256 or plan.build.oci_layout_sha256
        )
        for label, approved in (
            ("image", ImageContent(image_digest=plan.image_digest)),
            (
                "platform",
                ImageContent(
                    image_digest=plan.runtime_storage.image_digest,
                    archive_sha256=expected_layout,
                    image_bytes=_stated_bytes(plan.runtime_storage.image_bytes),
                ),
            ),
            (
                "build",
                ImageContent(
                    image_digest=plan.build.image_digest,
                    archive_sha256=plan.build.oci_layout_sha256,
                    image_bytes=_stated_bytes(plan.build.image_bytes),
                ),
            ),
        ):
            differing = differing_image_fields(approved, parsed_receipt)
            if differing:
                raise _RuntimeImageIdentityMismatch(
                    f"runtime image differs from the approved {label}: "
                    + ", ".join(differing)
                )

        if (
            plan.recipe_build_id or plan.build.build_id
        ) is None or plan.recipe_revision_id is None:
            raise _RuntimeImageIdentityMismatch(
                "runtime image is not the approved build result"
            )
        try:
            build = _build_receipt_in_session(session, plan)
        except RunSwitchOperationConflict as error:
            raise _RuntimeImageIdentityUnknown(
                "approved recipe build is no longer available"
            ) from error
        differing = differing_image_fields(build, parsed_receipt)
        if differing:
            raise _RuntimeImageIdentityMismatch(
                "runtime image receipt differs from the approved build: "
                + ", ".join(differing)
            )

        if profile_application_id is not None:
            try:
                expected_image = accepted_profile_runtime_image(
                    session,
                    profile_application_id,
                    recipe_revision_id,
                    target_nodes,
                )
                _require_profile_runtime_image(
                    expected_image,
                    {
                        "image_digest": parsed_receipt.image_digest,
                        "oci_layout_sha256": parsed_receipt.oci_archive_sha256,
                        "image_bytes": parsed_receipt.image_bytes,
                        "architecture": parsed_receipt.architecture,
                        "runtime_interface": parsed_receipt.runtime_interface,
                    },
                )
            except (RunSwitchOperationConflict, ValueError) as error:
                raise _RuntimeImageIdentityMismatch(
                    "runtime image differs from the accepted Fleet profile"
                ) from error

        intent = RunSwitchRuntimeImageReferenceIntent(
            owner_kind="run-switch-job",
            operation_id=job.id,
            request_key=job.request_id,
            actor=job.actor,
            plan_digest=plan.plan_digest,
            phase_index=phase.index,
            item_index=item_index,
            workload_intent_ordinal=ordinal,
            recipe_revision_id=recipe_revision_id,
            profile_application_id=profile_application_id,
            execution_keys=list(canonical_execution_keys),
            image_digest=parsed_receipt.image_digest,
            archive_sha256=parsed_receipt.oci_archive_sha256,
            image_bytes=parsed_receipt.image_bytes,
            build_id=parsed_receipt.build_id,
            build_input_sha256=parsed_receipt.build_input_sha256,
        )
        prior_intent = current.get("runtime_image_reference_intent")
        if prior_intent is not None:
            try:
                parsed_prior = read_stored_model(
                    RunSwitchRuntimeImageReferenceIntent, prior_intent, strict=True
                )
            except (TypeError, ValueError) as error:
                raise _RuntimeImageIdentityUnknown(
                    "stored RunSwitch image reference is invalid"
                ) from error
            if parsed_prior == intent:
                return
            # A re-plan leaves the earlier plan's reference behind; this plan's
            # phase now owns the image. Within one plan it never changes.
            if parsed_prior.plan_digest == intent.plan_digest:
                raise _RuntimeImageIdentityUnknown(
                    "RunSwitch image reference changed during publication"
                )
        current["runtime_image_reference_intent"] = intent.model_dump(mode="json")
        job.result = _persisted_result(current)
        job.updated_at = now


def _reject_invalid_operation(job: Job, reason: str, now: datetime) -> None:
    """Reject one malformed persisted operation while retaining its evidence.

    The invalid contract is neither repaired nor replaced: the job keeps its
    stored plan and result bytes, gains a precise operator-visible reason, and
    stops being advanced.  Its already-issued effects still require their own
    cancellation receipts, so this never claims they were stopped.
    """

    _ADAPTER.reject(job, reason, now)


__all__ = [
    "ArtifactInspection",
    "DatabaseRunSwitchArtifactInspector",
    "PhaseExecution",
    "RecipeLifecyclePhaseExecutor",
    "RunSwitchArtifactInspector",
    "RunSwitchArtifactPhaseExecutor",
    "RunSwitchOperationConflict",
    "RunSwitchOperationProvider",
    "RunSwitchOperationService",
    "RunSwitchPhaseExecutor",
    "effective_build_receipt",
]
