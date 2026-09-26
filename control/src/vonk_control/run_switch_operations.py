"""Controller-owned Run/Switch planning and durable phase orchestration.

This service is the boundary used by Library, Fleet profiles, the HTTP API,
and the CLI.  It intentionally composes the existing mapping, install, and
run services.  Artifact delivery remains behind ``RunSwitchArtifactInspector``
and ``RunSwitchPhaseExecutor`` so this module never downloads or publishes a
cache payload itself.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, TypeGuard, runtime_checkable

import httpx
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement
from vonk_agent_protocol import (
    DistributionAssignment,
    OperationProgress,
    canonical_message,
)

from .admission_locking import AdmissionLockBusy
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
from .bounded_json import require_integer, require_sequence
from .cluster_mappings import (
    ClusterMappingError,
    ClusterMappingPlan,
    ClusterMappingService,
    candidate_placements,
    validate_mapping_parameters,
)
from .disk_reservations import outstanding_disk_reservation_bytes
from .install_admission import InstallAdmissionBusy
from .inventory_repository import MAX_INVENTORY_FUTURE_SKEW, InventoryRepository
from .lifecycle_preflight import LifecyclePreflight, LifecyclePreflightCheckpoint
from .logging import log_event
from .memory_reservations import (
    MEMORY_RESERVATION_KINDS,
    memory_reservations,
    reviewed_run_memory_reservations,
)
from .model_cache import ModelCacheService
from .model_cache_contract import ModelCacheDownloadPreviewResponse
from .models import (
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
    RuntimeImageAuthorization,
)
from .operation_api import (
    OperationListPage,
    OperationQuery,
    _activity_keyset_filter,
)
from .operation_progress import observe_progress, project_progress
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
    parse_stored_build_plan,
    parse_stored_installation_plan,
    run_plan_document,
)
from .recipe_operations import (
    RecipeArtifactJobCancellationPending,
    RecipeInstallPreflightExpired,
    RecipeOperationConflict,
    RecipeOperationService,
    RecipeReconciliationBlocked,
)
from .recipe_runtime_specs import (
    RUNTIME_INTERFACE,
    RecipeRuntimeSpecError,
    recipe_topology,
    resolve_recipe_entities,
)
from .recovery_policy import (
    FailureKind,
    RecoveryDecision,
    RecoveryPolicy,
    classify,
    kind_for_agent_error,
)
from .resource_planning import (
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
    RunAdmissionBusy,
    run_port_blockers,
    run_port_demand,
)
from .run_switch_contract import (
    ArtifactStorageImpact,
    BuildCompatibilityEvidence,
    BuildSourceEvidence,
    CapabilityEvidence,
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
    RunSwitchCapabilityEvidenceState,
    RunSwitchChangeEffect,
    RunSwitchCleanupApplyRequest,
    RunSwitchCleanupPreviewRequest,
    RunSwitchContainerBuildResult,
    RunSwitchContainerBuildState,
    RunSwitchCoverage,
    RunSwitchMemberProgress,
    RunSwitchMemberState,
    RunSwitchOperation,
    RunSwitchOperationKind,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPhaseKind,
    RunSwitchPhaseResult,
    RunSwitchPlan,
    RunSwitchPreviewRequest,
    RunSwitchProgress,
    RunSwitchProgressState,
    RunSwitchReason,
    RunSwitchReasonScope,
    RunSwitchReasonSeverity,
    RunSwitchReconciliationAuthority,
    RunSwitchRetention,
    RunSwitchRuntimeImageReferenceIntent,
    RunSwitchStopApplyRequest,
    RunSwitchStopPreviewRequest,
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
)
from .runtime_image_preparation import (
    RuntimeImageReceipt as RuntimeImageReceiptDocument,
)


class PublishedImageReceiptLookup(Protocol):
    def __call__(
        self,
        registry_manifest_digest: str,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
    ) -> RuntimeImageReceiptDocument | None: ...


# Persisted progress and catalog documents arrive as decoded JSON, so the
# contract's closed value sets are read back through the declared alias instead
# of a hand-written membership test that could drift from it.
_CHANGE_EFFECTS_ADAPTER = TypeAdapter(dict[str, RunSwitchChangeEffect])
_CAPABILITY_EVIDENCE_ADAPTER = TypeAdapter(RunSwitchCapabilityEvidenceState)
_CONTAINER_BUILD_STATE_ADAPTER = TypeAdapter(RunSwitchContainerBuildState)
_BUILD_EVIDENCE_STATE_ADAPTER = TypeAdapter(RunSwitchBuildEvidenceState)
_OPERATION_KIND_ADAPTER = TypeAdapter(RunSwitchOperationKind)
_MEMBER_STATE_ADAPTER = TypeAdapter(RunSwitchMemberState)
_PROGRESS_STATE_ADAPTER = TypeAdapter(RunSwitchProgressState)
_SUBPHASE_ADAPTER = TypeAdapter(RunSwitchSubphase)
_REASON_SEVERITY_ADAPTER = TypeAdapter(RunSwitchReasonSeverity)


class RunSwitchOperationConflict(RuntimeError):
    """The selected outcome is stale, unsupported, or unsafe to execute."""


class _RunSwitchBuildParentChanged(RunSwitchOperationConflict):
    """This out-of-transaction executor no longer owns the build checkpoint."""


class RunSwitchIssuedWorkloadPending(RunSwitchOperationConflict):
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
        super().__init__(f"run-switch.{kind}-issued-pending: {owner_id} ({job_id})")
        self.kind = kind
        self.job_id = job_id
        self.observe_due_at = observe_due_at
        self.observation_deadline = observation_deadline


class RunSwitchPostStopEvidencePending(RunSwitchOperationConflict):
    """A released claim is not evidence that its physical bytes are free."""

    code = "run-switch.post-stop-inventory-pending"


class RunSwitchInstallPreflightExpired(RunSwitchOperationConflict):
    """A compile needs a fresh runtime preflight probe before it is accepted.

    The preflight window it was admitted on may have passed, or the host
    fingerprint or requirements moved while it compiled.  Nothing was accepted.
    ``_advance`` holds the runtime-plan checkpoint so the next tick re-enters
    ``LifecyclePreflight.ensure`` for a bounded refresh; any handler that does
    not know this subclass keeps failing the phase, which is the safe reading.
    """


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
        progress: Mapping[str, object],
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
        progress: Mapping[str, object],
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
        progress: Mapping[str, object],
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
                "run-switch.insufficient-memory", "run-switch.resource.insufficient"
            )
            or self.post_stop_memory_check is not None
        )

    @property
    def stop_before_transfer(self) -> bool:
        return self.requires_early_stop("run-switch.insufficient-disk")


_ACTIVE_RUN_STATES = frozenset({"planned", "starting", "running", "stopping"})
_STOPPABLE_RUN_STATES = _ACTIVE_RUN_STATES | {"lost"}
_TERMINAL_STATES = frozenset({"succeeded", "failed", "expired", "cancelled"})
_OPERATION_KINDS = frozenset(
    {"recipe.run-switch.v2", "recipe.stop.v2", "recipe.cleanup.v2"}
)
_MAX_RETRY_ATTEMPTS = 3
_MEMORY_CAPACITY_REFUSALS = frozenset(
    {
        "run-switch.resource.insufficient_capacity",
        "run-switch.resource.insufficient_capacity_after_stop",
    }
)
_MEMORY_STOP_CONDITIONAL_REFUSALS = _MEMORY_CAPACITY_REFUSALS | {
    "run-switch.resource.insufficient_reservation_budget",
    "run-switch.resource.resident_usage_unknown",
}
_INSTALL_PREFLIGHT_REFRESH_REASON = (
    "runtime preflight expired during install compilation"
)
_RUNTIME_IMAGE_OWNER_CHANGED = "run-switch.runtime-image-owner-changed"
_LOGGER = logging.getLogger("vonk-control-run-switch")
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


def _now(clock: Any) -> datetime:
    value = clock()
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("run/switch clock must be timezone-aware")
    return value.astimezone(UTC)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


_PLAN_VOLATILE_KEYS = frozenset(
    {"generated_at", "invocation", "plan_digest", "age_seconds", "verified_at"}
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


def _safe_mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _required_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _resource_reason(
    reason: object, *, node_ids: Sequence[str] = ()
) -> RunSwitchReason:
    node_id = getattr(reason, "node_id", None)
    return _as_reason(
        f"run-switch.{getattr(reason, 'code', 'resource.evidence_unknown')}",
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
        raise ValueError("effective settings are invalid")
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
        knobs=dict(resolved.knobs),
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


def _capability_evidence_state(
    value: object,
) -> RunSwitchCapabilityEvidenceState | None:
    """Read one capability evidence label, or ``None`` when it is not declared."""

    try:
        return _CAPABILITY_EVIDENCE_ADAPTER.validate_python(value, strict=True)
    except ValidationError:
        return None


def _capability_facts(
    document: Mapping[str, object] | None,
) -> list[CapabilityEvidence]:
    """Read only explicit capability metadata from one immutable document."""

    if document is None:
        return []
    raw = document.get("capabilities")
    facts: list[CapabilityEvidence] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        for value in raw:
            if isinstance(value, str) and value:
                facts.append(
                    CapabilityEvidence(
                        name=value,
                        declared=True,
                        evidence="unknown",
                        support="supported",
                    )
                )
        return facts
    if not isinstance(raw, Mapping):
        return facts
    raw_evidence = document.get("capability_evidence")
    evidence_by_name = raw_evidence if isinstance(raw_evidence, Mapping) else {}
    for name in sorted(str(key) for key in raw):
        value = raw.get(name)
        declared: bool | None
        evidence: RunSwitchCapabilityEvidenceState = "unknown"
        detail: str | None = None
        evidence_digest: str | None = None
        if isinstance(value, bool):
            declared = value
        elif isinstance(value, Mapping):
            candidate = value.get("declared", value.get("supported"))
            declared = candidate if isinstance(candidate, bool) else None
            candidate_evidence = _capability_evidence_state(value.get("evidence"))
            if candidate_evidence is not None:
                evidence = candidate_evidence
            if isinstance(value.get("detail"), str):
                detail = str(value["detail"])[:256]
            if isinstance(value.get("evidence_digest"), str):
                evidence_digest = str(value["evidence_digest"])
        else:
            declared = None
        evidence_value = evidence_by_name.get(name)
        if isinstance(evidence_value, Mapping):
            candidate_evidence = _capability_evidence_state(
                evidence_value.get("state", evidence_value.get("evidence"))
            )
            if candidate_evidence is not None:
                evidence = candidate_evidence
            candidate_digest = evidence_value.get(
                "digest", evidence_value.get("evidence_digest")
            )
            if isinstance(candidate_digest, str):
                evidence_digest = candidate_digest
        else:
            candidate_evidence = _capability_evidence_state(evidence_value)
            if candidate_evidence is not None:
                evidence = candidate_evidence
        support = (
            "unknown"
            if declared is None
            else "supported"
            if declared
            else "unsupported"
        )
        facts.append(
            CapabilityEvidence(
                name=name,
                declared=declared,
                evidence=evidence,
                support=support,
                evidence_digest=evidence_digest,
                detail=detail,
            )
        )
    return facts


def _recipe_capability_facts(
    document: Mapping[str, object] | None,
) -> list[CapabilityEvidence]:
    """Expose recipe interfaces as recipe-owned capability declarations."""

    if document is None:
        return []
    facts = _capability_facts(document)
    interfaces = document.get("interfaces")
    if not isinstance(interfaces, list):
        return facts
    names = {
        str(item.get("adapter"))
        for item in interfaces
        if isinstance(item, Mapping)
        and isinstance(item.get("adapter"), str)
        and item.get("adapter")
    }
    known = {fact.name for fact in facts}
    facts.extend(
        CapabilityEvidence(
            name=name,
            declared=True,
            evidence="not-tested",
            support="supported",
            detail="Declared by the immutable recipe interface; runtime acceptance is separate evidence.",
        )
        for name in sorted(names - known)
    )
    return facts


def _summary_capability_facts(summary: object) -> list[CapabilityEvidence]:
    """Adapt the shared model capability summary without importing its owner."""

    raw_facts = getattr(summary, "facts", None)
    if raw_facts is None and isinstance(summary, Mapping):
        raw_facts = summary.get("facts")
    if not isinstance(raw_facts, Sequence) or isinstance(raw_facts, (str, bytes)):
        return []
    facts: list[CapabilityEvidence] = []
    for raw in raw_facts:
        name = getattr(raw, "capability", None)
        support = getattr(raw, "support", None)
        evidence = getattr(raw, "evidence_status", None)
        digest = getattr(raw, "evidence_digest", None)
        if isinstance(raw, Mapping):
            name = raw.get("capability", raw.get("name"))
            support = raw.get("support", raw.get("status"))
            evidence = raw.get("evidence_status", raw.get("evidence"))
            digest = raw.get("evidence_digest")
        if not isinstance(name, str) or not name:
            continue
        if support not in {"supported", "unsupported", "unknown"}:
            support = "unknown"
        evidence_map: dict[str, RunSwitchCapabilityEvidenceState] = {
            "declared": "observed",
            "tested": "tested",
            "contradicted": "observed",
            "unknown": "unknown",
        }
        evidence = evidence_map.get(str(evidence), "unknown")
        facts.append(
            CapabilityEvidence(
                name=name,
                declared=(support != "unknown"),
                evidence=evidence,
                support=support,
                evidence_digest=digest if _is_hex_digest(digest) else None,
            )
        )
    return facts


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
            raise RuntimeError("model-cache manifest provider is unavailable")
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
        except Exception as error:
            raise RuntimeError(
                f"model-cache exact manifest is unavailable: {error}"
            ) from error
        artifact_set_sha256 = manifest.digest
        if preview.artifact_set_sha256 != artifact_set_sha256:
            raise RuntimeError(
                "model-cache download preview does not match its manifest"
            )
        if manifest.model_content_sha256 != model_content_sha256:
            raise RuntimeError(
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
                "run-switch.nas-download-blocked",
                detail,
                scope="artifact",
                node_ids=node_ids,
            )
            for detail in preview.blockers
        ]
        reused = 0
        missing_spark = 0
        reclaimable = 0
        reclaimable_digests: set[str] = set()
        for node_id in node_ids:
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
                    and row.state == "verified"
                    and row.size_bytes == size
                ):
                    reused += size
                else:
                    missing_spark += size
            if retention == "reclaim-unreferenced":
                for row in rows:
                    if (
                        row.digest in expected_by_digest
                        and row.state == "verified"
                        and row.ref_count == 0
                    ):
                        reclaimable += row.size_bytes
                        reclaimable_digests.add(row.digest)
        warnings: list[RunSwitchReason] = []
        if missing_nas_bytes:
            warnings.append(
                _as_reason(
                    "run-switch.nas-download-required",
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
        raise RunSwitchOperationConflict(
            "run-switch.container-build-evidence-invalid"
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
                raise RunSwitchOperationConflict("run-switch.preflight-recipe-changed")
            document = revision.document
        nodes = {node.node_id: False for node in plan.spark_group.nodes}
        if (
            phase.subphase == "container-build"
            and plan.build.builder_node_id
            or not progress.get("completed_phases")
            and plan.build.state in {"planned", "building"}
            and plan.build.builder_node_id
        ):
            nodes[plan.build.builder_node_id] = True
        previous = progress.get("preflight")
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
            previous=LifecyclePreflightCheckpoint.model_validate_json(
                canonical_message(previous)
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
                    raise RunSwitchOperationConflict(
                        "run-switch.stopped-run-identity-changed"
                    )
                members = set(
                    session.scalars(
                        select(RunNode.node_id).where(RunNode.run_id == run.id)
                    )
                )
                if members != set(stop.node_ids):
                    raise RunSwitchOperationConflict(
                        "run-switch.stopped-run-membership-changed"
                    )
                if run.state != "stopped" or run.stopped_at is None:
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
                        raise RunSwitchOperationConflict(
                            "run-switch.post-stop-memory-pool-changed"
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
                            f"latest {snapshot.observed_at.isoformat()})"
                        )

    def _execute_container_build(
        self,
        plan: RunSwitchPlan,
        *,
        phase_index: int,
        item_index: int,
        actor: str,
        request_key: str,
        progress: Mapping[str, object],
    ) -> PhaseExecution:
        """Start or replay the existing durable ``recipe.build.v1`` child."""

        build_id = plan.recipe_build_id or plan.build.build_id
        revision_id = plan.recipe_revision_id
        if build_id is None or revision_id is None:
            raise RunSwitchOperationConflict(
                "run-switch.container-build-identity-unavailable"
            )
        expected_build_id = _string_or_none(plan.build.build_id)
        expected_build_input = _string_or_none(plan.build.build_input_sha256)
        if expected_build_id != build_id or expected_build_input is None:
            raise RunSwitchOperationConflict("run-switch.container-build-plan-invalid")
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
            if build is None or build.recipe_revision_id != revision_id:
                raise RunSwitchOperationConflict(
                    "run-switch.container-build-receipt-unavailable"
                )
            # Reconnecting bypasses capacity admission, never identity. Both
            # a completed receipt and an active child must match the reviewed
            # executable inputs before either may be adopted.
            if expected_build_input != build.build_input_sha256:
                raise RunSwitchOperationConflict(
                    "run-switch.container-build-plan-invalid"
                )
            if build.state == "succeeded":
                return PhaseExecution(result=_container_build_result(build))
            if build.state not in {"planned", "building", "failed"}:
                raise RunSwitchOperationConflict(
                    "run-switch.container-build-state-invalid"
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
                raise RunSwitchOperationConflict(
                    "run-switch.container-build-plan-invalid"
                ) from error
        self._require_post_stop_inventory(plan)
        start_build = getattr(self._lifecycle, "build", None)
        if not callable(start_build):
            raise RunSwitchOperationConflict(
                "run-switch.container-build-executor-unavailable"
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
                raise RunSwitchOperationConflict(
                    "run-switch.container-build-plan-invalid"
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
                raise RunSwitchOperationConflict(
                    "run-switch.container-build-plan-invalid"
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
                raise RunSwitchOperationConflict(
                    f"run-switch.container-build-plan-invalid: {error}"
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
            raise RunSwitchOperationConflict(
                f"run-switch.container-build-start-unavailable: {error}"
            ) from error
        with self._sessions.begin() as session:
            admission_guard(session)
            persisted = session.get(RecipeBuild, build_id)
            if persisted is None:
                raise RunSwitchOperationConflict(
                    "run-switch.container-build-receipt-unavailable"
                )
            if (
                persisted.recipe_revision_id != revision_id
                or persisted.build_input_sha256 != build_input_sha256
            ):
                raise RunSwitchOperationConflict(
                    "run-switch.container-build-plan-invalid"
                )
            result = _container_build_result(persisted)
            if persisted.state == "succeeded":
                return PhaseExecution(result=result)
        return PhaseExecution(_started_operation_id(value), result)

    def _observe_older_issued(self, kind: str, owner_id: str, ordinal: int) -> None:
        pending = self._lifecycle.assess_superseded_issued(kind, owner_id, ordinal)
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
        progress: Mapping[str, object],
    ) -> PhaseExecution:
        if phase.kind in {"transfer", "verify", "cleanup"}:
            if self._artifact_executor is None:
                raise RunSwitchOperationConflict(
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
                raise RunSwitchOperationConflict(
                    f"run-switch.{phase.kind}-waiting-without-child"
                )
            if (
                execution.operation_id is None
                and execution.result is None
                and not execution.waiting
            ):
                raise RunSwitchOperationConflict(
                    f"run-switch.{phase.kind}-returned-no-evidence"
                )
            if execution.operation_id is None:
                _validate_artifact_execution(plan, phase, execution.result)
            return execution
        if phase.kind == "prepare" and phase.subphase == "runtime-image":
            if self._artifact_executor is None:
                raise RunSwitchOperationConflict(
                    "run-switch.runtime-image-executor-unavailable"
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
                raise RunSwitchOperationConflict(
                    "run-switch.runtime-image-waiting-without-child"
                )
            if execution.operation_id is None:
                _validate_artifact_execution(plan, phase, execution.result)
            return execution
        if phase.kind == "stop":
            if item_index >= len(plan.stops):
                return PhaseExecution()
            target = plan.stops[item_index]
            ordinal = _bound_workload_intent(progress)
            self._lifecycle.reconcile_superseded_unissued(
                "recipe.stop", target.run_id, ordinal
            )
            stop_digest = target.plan_digest
            if target.state in {"starting", "stopping"}:
                # A newer explicit Stop can cancel an older same-run command
                # under the current node ordinal.  Re-preview this exact run
                # because its prior Start/Stop may have changed state after
                # the high-level plan was reviewed.  The low-level Stop
                # authority validates current membership and reservations.
                with self._sessions() as session:
                    run = session.get(RecipeRun, target.run_id)
                    if run is None:
                        raise RunSwitchOperationConflict(
                            "run-switch.stop-target-disappeared"
                        )
                    if run.state == "stopped" and run.route_state == "withdrawn":
                        return PhaseExecution(result={"run_id": target.run_id})
                fresh = self._lifecycle.preview_stop(target.run_id)
                if not fresh.allowed:
                    raise RunSwitchOperationConflict(
                        "run-switch.stop-still-unresolved-after-cancellation"
                    )
                stop_digest = fresh.plan_digest
            child_key = str(uuid.uuid5(uuid.UUID(request_key), f"stop:{target.run_id}"))
            try:
                value = self._lifecycle.stop(
                    target.run_id,
                    plan_digest=stop_digest,
                    actor=actor,
                    request_id=child_key,
                    workload_intent_ordinal=ordinal,
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
            phase_results = progress.get("phase_results")
            if isinstance(phase_results, list):
                for result in reversed(phase_results):
                    if isinstance(result, Mapping):
                        mapping_id = (
                            _string_or_none(result.get("mapping_id")) or mapping_id
                        )
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
            profile_application_id = _string_or_none(
                progress.get("profile_application_id")
            )
            if profile_application_id is not None:
                with self._sessions() as session:
                    handed_off = prepared_profile_installation(
                        session,
                        profile_application_id,
                        _required_string(plan.recipe_revision_id),
                        tuple(node.node_id for node in plan.spark_group.nodes),
                        workload_intent_ordinal=_bound_workload_intent(progress),
                    )
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
                            "planned",
                            "installing",
                            "partial",
                            "installed",
                        }:
                            raise RunSwitchOperationConflict(
                                "run-switch.installation-handoff-unavailable"
                            )
                        stored = parse_stored_installation_plan(installation.plan)
                        if stored.plan_digest != install_plan_digest:
                            raise RunSwitchOperationConflict(
                                "run-switch.installation-identity-changed"
                            )
                        return self._prepared_installation_result(
                            installation_id,
                            mapping_id,
                            install_plan_digest,
                            {
                                node_id: compiled.model_dump(mode="json")
                                for node_id, compiled in stored.compiled_execution_plans.items()
                            },
                        )
            try:
                install_plan = self._lifecycle.preview_install(
                    mapping_id,
                    plan.recipe_build_id,
                    profile_application_id=_string_or_none(
                        progress.get("profile_application_id")
                    ),
                )
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise RunSwitchOperationConflict(
                    f"run-switch.install-plan-unavailable: {error}"
                ) from error
            prepare_installation = getattr(
                self._lifecycle, "prepare_installation", None
            )
            if not callable(prepare_installation):
                raise RunSwitchOperationConflict(
                    "run-switch.install-preparation-unavailable"
                )
            try:
                installation_id = prepare_installation(
                    install_plan,
                    actor=actor,
                    profile_application_id=_string_or_none(
                        progress.get("profile_application_id")
                    ),
                    workload_intent_ordinal=_bound_workload_intent(progress),
                )
            except RecipeInstallPreflightExpired as error:
                # Compiling the launch document above can outlast the runtime
                # preflight window this phase was admitted on.  Nothing else
                # about the install changed, so ask the caller to rerun the
                # ordinary probe rather than failing an identical plan.
                raise RunSwitchInstallPreflightExpired(
                    f"run-switch.install-preflight-expired: {error}"
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
                raise RunSwitchOperationConflict(
                    f"run-switch.install-preparation-failed: {error}"
                ) from error
            return self._prepared_installation_result(
                _required_string(installation_id),
                mapping_id,
                install_plan.plan_digest,
                install_plan.compiled_plan_by_node,
            )
        if phase.kind == "prepare" and phase.subphase == "runtime-install":
            ordinal = _bound_workload_intent(progress)
            installation_id = plan.installation_id
            phase_results = progress.get("phase_results")
            if installation_id is None and isinstance(phase_results, list):
                for result in reversed(phase_results):
                    if isinstance(result, Mapping):
                        installation_id = _string_or_none(result.get("installation_id"))
                        if installation_id is not None:
                            break
            if installation_id is None:
                raise RunSwitchOperationConflict(
                    "run-switch.installation-preparation-unavailable"
                )
            self._lifecycle.reconcile_superseded_unissued(
                "recipe.install", installation_id, ordinal
            )
            self._observe_older_issued("recipe.install", installation_id, ordinal)
            start_installation = getattr(self._lifecycle, "start_installation", None)
            if not callable(start_installation):
                raise RunSwitchOperationConflict(
                    "run-switch.install-executor-unavailable"
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
                raise RunSwitchOperationConflict(
                    f"run-switch.install-start-failed: {error}"
                ) from error
            return PhaseExecution(
                _started_operation_id(value),
                {"installation_id": installation_id},
            )
        if phase.kind == "prepare":
            raise RunSwitchOperationConflict("run-switch.prepare-subphase-unsupported")
        if phase.kind == "start":
            ordinal = _bound_workload_intent(progress)
            installation_id = plan.installation_id
            phase_results = progress.get("phase_results")
            if installation_id is None and isinstance(phase_results, list):
                for result in reversed(phase_results):
                    if isinstance(result, Mapping):
                        installation_id = _string_or_none(result.get("installation_id"))
                        if installation_id is not None:
                            break
            if installation_id is None or plan.alias is None:
                raise RunSwitchOperationConflict(
                    "run-switch.start_installation_unavailable"
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
            profile_application_id = _string_or_none(
                progress.get("profile_application_id")
            )
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
                raise RunSwitchOperationConflict(
                    "run-switch.uninstall_target_unavailable"
                )
            if plan.cleanup_mode == "reconcile":
                authority = plan.reconciliation_authority
                if authority is None:
                    raise RunSwitchOperationConflict(
                        "run-switch.reconciliation-authority-unavailable"
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
                except InstallAdmissionBusy:
                    raise
                except (
                    KeyError,
                    RecipeOperationConflict,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise RunSwitchOperationConflict(
                        f"run-switch.reconciliation-start-failed: {error}"
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
                    raise RunSwitchOperationConflict(
                        f"run-switch.uninstall-abandon-failed: {error}"
                    ) from error
                return PhaseExecution(
                    result={
                        **abandoned,
                        "reason": "installation-not-installed",
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
            except (
                KeyError,
                RecipeOperationConflict,
                RuntimeError,
                TypeError,
                ValueError,
            ) as error:
                raise RunSwitchOperationConflict(
                    f"run-switch.uninstall-start-failed: {error}"
                ) from error
            return PhaseExecution(value.id, {"installation_id": installation_id})
        if phase.kind == "final_verify":
            if plan.action == "cleanup":
                return self._verify_cleanup(plan, request_key=request_key)
            if plan.action == "install":
                return self._verify_installation(plan, progress)
            run_id = plan.run_id
            phase_results = progress.get("phase_results")
            if run_id is None and isinstance(phase_results, list):
                for result in reversed(phase_results):
                    if isinstance(result, Mapping):
                        run_id = _string_or_none(result.get("run_id"))
                        if run_id is not None:
                            break
            if run_id is None or self._lifecycle is None:
                raise RunSwitchOperationConflict(
                    "run-switch.final-verification-unavailable"
                )
            status = self._lifecycle.run_status(run_id)
            if plan.action == "stop":
                verified = (
                    status.state == "stopped"
                    and status.route_state == "withdrawn"
                    and all(rank.state == "stopped" for rank in status.ranks)
                )
                waiting = status.state in _ACTIVE_RUN_STATES or status.route_state in {
                    "pending",
                }
            else:
                verified = status.healthy and status.route_state == "published"
                waiting = status.state in _ACTIVE_RUN_STATES or status.route_state in {
                    "pending",
                }
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
                )
            raise RunSwitchOperationConflict("run-switch.final-verification-failed")
        return PhaseExecution()

    @staticmethod
    def _prepared_installation_result(
        installation_id: str,
        mapping_id: str,
        install_plan_digest: str,
        compiled: Mapping[str, Mapping[str, object]],
    ) -> PhaseExecution:
        first_compiled = next(iter(compiled.values()), {})
        identity = (
            first_compiled.get("identity")
            if isinstance(first_compiled, Mapping)
            else None
        )
        return PhaseExecution(
            result={
                "installation_id": installation_id,
                "mapping_id": mapping_id,
                "install_plan_digest": install_plan_digest,
                "model_artifact_set_sha256": (
                    identity.get("model_artifact_set_sha256")
                    if isinstance(identity, Mapping)
                    else None
                ),
                "model_artifact_set_bytes": (
                    identity.get("model_artifact_bytes")
                    if isinstance(identity, Mapping)
                    else None
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
            or installation.recipe_build_id != plan.recipe_build_id
            or installation.image_digest != plan.image_digest
            or (
                install_plan_digest is not None
                and installation.plan_digest != install_plan_digest
            )
        ):
            raise RunSwitchOperationConflict("run-switch.installation-identity-changed")
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
            raise RunSwitchOperationConflict(
                "run-switch.installation-membership-changed"
            )
        return installation, members

    def _verify_installation(
        self, plan: RunSwitchPlan, progress: Mapping[str, object]
    ) -> PhaseExecution:
        """Verify the bound installation, not merely its completed child job."""

        installation_id = plan.installation_id
        mapping_id = plan.mapping.mapping_id if plan.mapping is not None else None
        install_plan_digest = None
        phase_results = progress.get("phase_results")
        if isinstance(phase_results, list):
            for result in phase_results:
                if (
                    isinstance(result, Mapping)
                    and result.get("phase") == "prepare"
                    and result.get("subphase") == "runtime-plan"
                ):
                    installation_id = _string_or_none(result.get("installation_id"))
                    mapping_id = _string_or_none(result.get("mapping_id"))
                    install_plan_digest = _string_or_none(
                        result.get("install_plan_digest")
                    )
        if installation_id is None or mapping_id is None or plan.mapping is None:
            raise RunSwitchOperationConflict(
                "run-switch.installation-verification-unavailable"
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
            active_runs = sum(run.state in _ACTIVE_RUN_STATES for run in runs)
            unwithdrawn_routes = sum(run.route_state != "withdrawn" for run in runs)
            verified = (
                installation.state == "installed"
                and all(node.state == "installed" for node in members)
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
            if installation.state in {"planned", "installing"}:
                return PhaseExecution(result=evidence, waiting=True)
        raise RunSwitchOperationConflict("run-switch.installation-verification-failed")

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
            raise RunSwitchOperationConflict("run-switch.uninstall_target_unavailable")
        exact_reconciliation_receipts = True
        reconcile_request_id: str | None = None
        reconciliation_receipts: list[dict[str, object]] = []
        if plan.cleanup_mode == "reconcile":
            if self._lifecycle is None or plan.reconciliation_authority is None:
                raise RunSwitchOperationConflict(
                    "run-switch.reconciliation-authority-unavailable"
                )
            reconcile_request_id = str(uuid.uuid5(uuid.UUID(request_key), "reconcile"))
            verified_receipts = self._lifecycle.reconciliation_operation_receipts(
                reconcile_request_id,
                expected_authority=plan.reconciliation_authority.model_dump(
                    mode="json"
                ),
            )
            exact_reconciliation_receipts = verified_receipts is not None
            if verified_receipts is not None:
                reconciliation_receipts = [
                    receipt.model_dump(mode="json") for receipt in verified_receipts
                ]
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
                run.state != "stopped" or run.route_state != "withdrawn" for run in runs
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
                and installation.state == "uninstalled"
                and exact_members
                and all(node.state == "uninstalled" for node in members)
                and exact_reconciliation_receipts
            )
            if not exact_reconciliation_receipts:
                raise RunSwitchOperationConflict(
                    "run-switch.reconciliation-receipt-verification-failed"
                )
            if (
                installation is None
                or installation.state != "uninstalled"
                or not exact_members
                or any(
                    node.state != "uninstalled" or node.evidence_digest is None
                    for node in members
                )
            ):
                raise RunSwitchOperationConflict(
                    "run-switch.reconciliation-state-verification-failed"
                )
        else:
            removed = installation is None or installation.state == "uninstalled"
        evidence = {
            "installation_id": installation_id,
            "installation_state": (
                installation.state if installation is not None else None
            ),
            "removed": removed,
            "active_runs": active_runs,
            "cleanup_mode": plan.cleanup_mode,
            **(
                {
                    "reconciliation_request_id": reconcile_request_id,
                    "exact_reconciliation_receipts": exact_reconciliation_receipts,
                    "reconciliation_receipts": reconciliation_receipts,
                }
                if plan.cleanup_mode == "reconcile"
                else {}
            ),
        }
        if removed and not active_runs:
            return PhaseExecution(result={"final_verified": True, **evidence})
        return PhaseExecution(
            result={"final_verified": False, **evidence}, waiting=True
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
        raise KeyError(operation_id)


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
        model_capability_summary: Any | None = None,
        model_cache: ModelCacheService | None = None,
        build_archive_available: Callable[[str, int], bool] | None = None,
        published_image_receipt: PublishedImageReceiptLookup | None = None,
        inventory_max_age_seconds: int = 300,
        memory_floor_bytes: int = 0,
    ) -> None:
        if not 1 <= inventory_max_age_seconds <= 86_400:
            raise ValueError("run/switch inventory age is invalid")
        if memory_floor_bytes < 0:
            raise ValueError("run/switch memory floor is invalid")
        self._sessions = sessions
        self._lifecycle = lifecycle
        self._clock = clock
        self._mappings = mappings or ClusterMappingService(sessions)
        self._artifacts = artifacts or DatabaseRunSwitchArtifactInspector(model_cache)
        self._artifact_phase_executor = artifact_phase_executor
        self._model_capability_summary = model_capability_summary
        self._build_archive_available = build_archive_available
        self._published_image_receipt = published_image_receipt
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
                raise KeyError(recipe_revision_id)
            placements = candidate_placements(
                recipe_topology(revision.document), node_ids
            )
            model_digest = _primary_model_digest(revision.document)
            if model_digest is None:
                raise RecipeRuntimeSpecError("recipe has no exact primary model")
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
    ) -> RunSwitchPlan:
        run_id = request if isinstance(request, str) else request.run_id
        invocation = (
            InvocationMetadata() if isinstance(request, str) else request.invocation
        )
        now = _now(self._clock)
        with self._sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise KeyError(run_id)
            try:
                stored_run_plan = run_plan_document(run.plan)
            except RecipeExecutionContractError as error:
                raise RunSwitchOperationConflict(
                    "run-switch.run-plan-invalid"
                ) from error
            installation = session.get(RecipeInstallation, run.installation_id)
            revision = _active_recipe_revision(
                session, _string_or_none(stored_run_plan.get("recipe_revision_id"))
            )
            mapping = session.get(ClusterMapping, run.mapping_id)
            mapping_nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id == run.mapping_id)
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
            model_digest = (
                installation.model_content_sha256
                if installation is not None
                else _string_or_none(stored_run_plan.get("model_content_sha256"))
            )
            recipe_digest = revision.content_digest if revision is not None else None
            (
                _model_document,
                _model_documents,
                model_caps,
                recipe_caps,
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
                group,
                now=now,
                excluded_run_ids=(run.id,),
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
                    group,
                    require_available=False,
                    published_receipt_lookup=self._published_image_receipt,
                )
            )
            stop_digest = self._stop_digest(run.id)
            stops = (
                [
                    StopImpact(
                        run_id=run.id,
                        run_plan_digest=run.plan_digest,
                        alias=run.alias,
                        state=run.state,
                        node_ids=[node.node_id for node in mapping_nodes],
                        reserved_bytes=self._run_reserved_bytes(session, run.id),
                        plan_digest=stop_digest,
                    )
                ]
                if stop_digest is not None and run.state in _STOPPABLE_RUN_STATES
                else []
            )
            # Stopping a live run must remain possible when catalog/cache
            # evidence has aged or is unavailable. Capacity and artifact
            # findings remain diagnostics, but they do not block the stop.
            blockers: list[RunSwitchReason] = []
            warnings = [*fit_warnings, *inspection.warnings]
            if run.state not in _STOPPABLE_RUN_STATES:
                blockers.append(
                    _as_reason(
                        "run-switch.run-not-active",
                        "The selected run is no longer active and cannot be stopped.",
                        scope="operation",
                        node_ids=[node.node_id for node in mapping_nodes],
                    )
                )
            elif stop_digest is None:
                blockers.append(
                    _as_reason(
                        "run-switch.stop-plan-unavailable",
                        "The existing run cannot be represented by a safe stop plan.",
                        scope="operation",
                        node_ids=[node.node_id for node in mapping_nodes],
                    )
                )
            phases = self._phases(
                action="stop",
                group=group,
                installation_id=installation.id if installation is not None else None,
                installation_state=installation.state
                if installation is not None
                else None,
                stops=stops,
                inspection=inspection,
                runtime_storage=runtime_storage,
                retention="retain-cached",
                blockers=blockers,
                stop_before_transfer=False,
                stop_before_prepare=False,
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
                "action": "stop",
                "model_content_sha256": model_digest,
                "recipe_revision_id": revision.id if revision is not None else None,
                "recipe_content_sha256": recipe_digest,
                "alias": run.alias,
                "run_id": run.id,
                "spark_group": group,
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
                "model_capabilities": model_caps,
                "recipe_capabilities": recipe_caps,
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
        invocation = (
            InvocationMetadata() if isinstance(request, str) else request.invocation
        )
        now = _now(self._clock)
        with self._sessions() as session:
            installation = session.get(RecipeInstallation, installation_id)
            if installation is None:
                raise KeyError(installation_id)
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
                model_caps,
                recipe_caps,
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
                    published_receipt_lookup=self._published_image_receipt,
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
                            "run-switch.reconciliation-assessment-unavailable"
                            if cleanup_mode == "reconcile"
                            else "run-switch.uninstall-assessment-unavailable"
                        ),
                        "Cleanup cannot be assessed without the lifecycle service.",
                        scope="operation",
                        node_ids=node_ids,
                    )
                )
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
                            f"run-switch.{error.code}",
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
                            "run-switch.reconciliation-assessment-unavailable",
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
                                "run-switch.reconciliation-prerequisite",
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
                                "run-switch.reconciliation-receipts-retained",
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
                            "run-switch.uninstall-assessment-unavailable",
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
                            "run-switch.uninstall-issued-prerequisite"
                            if issued is not None
                            and blocker.code == "uninstall.operation_active"
                            else "run-switch.uninstall-blocked",
                            (
                                "The prior issued uninstall will be cancelled and "
                                "observed before this cleanup starts."
                                if issued is not None
                                and blocker.code == "uninstall.operation_active"
                                else f"{blocker.code}: {blocker.detail}"
                            ),
                            scope="operation",
                            node_ids=node_ids,
                            severity=(
                                "warning"
                                if issued is not None
                                and blocker.code == "uninstall.operation_active"
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
                                f"run-switch.{warning.code}",
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
                "model_capabilities": model_caps,
                "recipe_capabilities": recipe_caps,
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
        if request.request_key is not None:
            existing = self._existing_request_operation(
                request.request_key,
                kind="recipe.cleanup.v2",
                plan_digest=request.plan_digest,
            )
            if existing is not None:
                return existing
        preview = self.preview_cleanup(request, actor=actor)
        if (
            request.plan_digest is not None
            and preview.plan_digest != request.plan_digest
        ):
            raise RunSwitchOperationConflict(
                "run-switch.stale_plan: current evidence no longer matches preview"
            )
        if not preview.allowed:
            raise RunSwitchOperationConflict(
                "run-switch.plan_blocked: "
                + "; ".join(reason.code for reason in preview.blockers[:8])
            )
        return self._apply_plan(
            preview,
            request_key=request_key,
            actor=actor,
            kind="recipe.cleanup.v2",
            workload_intent_ordinal=workload_intent_ordinal,
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
        if request.request_key is not None:
            existing = self._existing_request_operation(
                request.request_key,
                kind="recipe.run-switch.v2",
                plan_digest=request.plan_digest,
            )
            if existing is not None:
                return existing
        plan = self.preview(
            request, actor=actor, profile_application_id=profile_application_id
        )
        if request.plan_digest is not None and plan.plan_digest != request.plan_digest:
            raise RunSwitchOperationConflict(
                "run-switch.stale_plan: current evidence no longer matches preview"
            )
        if not plan.allowed:
            raise RunSwitchOperationConflict(
                "run-switch.plan_blocked: "
                + "; ".join(reason.code for reason in plan.blockers[:8])
            )
        return self._apply_plan(
            plan,
            request_key=request_key,
            actor=actor,
            kind="recipe.run-switch.v2",
            workload_intent_ordinal=workload_intent_ordinal,
            profile_application_id=profile_application_id,
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
    ) -> RunSwitchOperation:
        request_key = request.request_key or str(uuid.uuid4())
        if request.request_key is not None:
            existing = self._existing_request_operation(
                request.request_key,
                kind="recipe.stop.v2",
                plan_digest=request.plan_digest,
            )
            if existing is not None:
                return existing
        preview = self.preview_stop(request, actor=actor)
        if (
            request.plan_digest is not None
            and preview.plan_digest != request.plan_digest
        ):
            raise RunSwitchOperationConflict(
                "run-switch.stale_plan: current evidence no longer matches preview"
            )
        if not preview.allowed:
            raise RunSwitchOperationConflict(
                "run-switch.plan_blocked: "
                + "; ".join(reason.code for reason in preview.blockers[:8])
            )
        return self._apply_plan(
            preview,
            request_key=request_key,
            actor=actor,
            kind="recipe.stop.v2",
            workload_intent_ordinal=workload_intent_ordinal,
        )

    def get(self, operation_id: str) -> RunSwitchOperation:
        with self._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.kind not in _OPERATION_KINDS:
                raise KeyError(operation_id)
            return self._operation_view(job)

    def cancel(
        self, operation_id: str, *, actor: str, request_key: str, reason: str
    ) -> RunSwitchOperation:
        """Stop at the next safe phase boundary, keeping shared immutable work."""
        cancellation = RunSwitchCancellation(
            request_key=request_key,
            actor=actor,
            reason=" ".join(reason.split()),
            requested_at=_now(self._clock),
        )
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or job.kind not in _OPERATION_KINDS:
                raise KeyError(operation_id)
            progress = _read_progress(job.result)
            previous = progress.get("cancellation")
            if previous:
                if not isinstance(previous, Mapping):
                    raise RunSwitchOperationConflict(
                        "run-switch cancellation evidence is invalid"
                    )
                if any(
                    previous.get(key) != getattr(cancellation, key)
                    for key in ("request_key", "actor", "reason")
                ):
                    raise RunSwitchOperationConflict(
                        "run-switch cancellation request was already used differently"
                    )
                return self._operation_view(job)
            if job.state not in {"queued", "running"}:
                raise RunSwitchOperationConflict(
                    "run-switch operation is not cancellable"
                )
            plan = _load_plan(job.payload["plan"])
            try:
                lock_run_switch_build_dependency(
                    session,
                    plan,
                    phase_index=require_integer(
                        progress.get("phase_index", 0), "phase index"
                    ),
                    allow_cancelling=True,
                )
            except BuildConsumerError as error:
                raise RunSwitchOperationConflict(f"{error.code}: {error}") from error
            phase = plan.phases[
                min(
                    require_integer(progress.get("phase_index", 0), "phase index"),
                    len(plan.phases) - 1,
                )
            ]
            if "start" in require_sequence(
                progress.get("completed_phases", []), "completed phases"
            ) or (job.state == "running" and phase.kind in {"start", "final_verify"}):
                raise RunSwitchOperationConflict(
                    "run-switch runtime is starting or active; use the explicit Stop operation"
                )
            progress["cancellation"] = cancellation.model_dump(mode="json")
            job.status_reason = (
                "Cancellation requested; finishing the current preparation safely."
            )
            if phase.subphase == "container-build" or (
                job.state == "queued" and not progress.get("child_operation_id")
            ):
                _complete_cancellation(job, progress, cancellation.requested_at)
            else:
                job.result = _persisted_result(progress)
                job.updated_at = cancellation.requested_at
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
            raise RunSwitchOperationConflict(
                "run-switch retry request key is invalid"
            ) from error
        with self._sessions.begin() as session:
            previous = session.get(Job, operation_id, with_for_update=True)
            if previous is None or previous.kind not in _OPERATION_KINDS:
                raise RunSwitchOperationConflict(
                    "run-switch operation is not retryable"
                )
            existing = session.scalar(select(Job).where(Job.request_id == request_key))
            if existing is not None:
                if existing.kind != previous.kind or existing.payload.get(
                    "plan_digest"
                ) != previous.payload.get("plan_digest"):
                    raise RunSwitchOperationConflict(
                        "run-switch request key was already used"
                    )
                return self._operation_view(existing)
            current_progress = _parse_persisted_result(previous.result)
            progress = (
                current_progress.model_dump(mode="json", exclude_unset=True)
                if current_progress is not None
                else {}
            )
            raw_retry = previous.payload.get("retry", {})
            retry = dict(raw_retry) if isinstance(raw_retry, Mapping) else {}
            operator_retries = retry.get("operator_retries")
            operator_retries = (
                operator_retries
                if type(operator_retries) is int and operator_retries >= 0
                else 0
            )
            if (
                current_progress is None
                or previous.state != "failed"
                or progress.get("retryable") is not True
                or operator_retries >= _MAX_RETRY_ATTEMPTS
            ):
                raise RunSwitchOperationConflict(
                    "run-switch operation is not retryable"
                )
            nodes = list(
                session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(previous.targets))
                    .order_by(AgentNode.node_id)
                    .with_for_update()
                )
            )
            if len(nodes) != len(previous.targets) or any(
                node.workload_intent_ordinal
                != previous.payload.get("workload_intent_ordinal")
                for node in nodes
            ):
                raise RunSwitchOperationConflict(
                    "run-switch.superseded: retry belongs to an obsolete workload intent"
                )
            retry_plan = _load_plan(previous.payload["plan"])
            prior_image_intent = current_progress.runtime_image_reference_intent
            if prior_image_intent is not None:
                try:
                    validated_intent = _run_switch_runtime_image_intent(
                        previous, retry_plan
                    )
                except ArtifactLifecycleError as error:
                    raise RunSwitchOperationConflict(
                        "run-switch retry runtime image reference is invalid"
                    ) from error
                if validated_intent != prior_image_intent:
                    raise RunSwitchOperationConflict(
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
                    raise RunSwitchOperationConflict(
                        f"{error.code}: {error.detail}"
                    ) from error
            try:
                lock_run_switch_build_dependency(
                    session,
                    retry_plan,
                    phase_index=current_progress.phase_index,
                )
            except BuildConsumerError as error:
                raise RunSwitchOperationConflict(f"{error.code}: {error}") from error
            ordinal = max(node.workload_intent_ordinal for node in nodes) + 1
            for node in nodes:
                node.workload_intent_ordinal = ordinal
            self.request_superseded_workload_cancellation_in_session(
                session, tuple(previous.targets), ordinal, now
            )
            retry_job_id = str(uuid.uuid4())
            if prior_image_intent is not None:
                rebound_intent = RunSwitchRuntimeImageReferenceIntent.model_validate(
                    {
                        **prior_image_intent.model_dump(mode="json"),
                        "operation_id": retry_job_id,
                        "request_key": request_key,
                        "actor": actor,
                        "workload_intent_ordinal": ordinal,
                    },
                    strict=True,
                )
                progress["runtime_image_reference_intent"] = rebound_intent.model_dump(
                    mode="json"
                )
            payload = dict(previous.payload)
            payload["workload_intent_ordinal"] = ordinal
            payload["progress"] = progress
            payload["retry_of"] = previous.id
            payload["retry"] = {
                "automatic_attempts": 1,
                "operator_retries": operator_retries + 1,
            }
            progress["child_operation_id"] = None
            progress["retryable"] = False
            progress.pop("failure_code", None)
            progress["workload_intent_ordinal"] = ordinal
            job = Job(
                id=retry_job_id,
                request_id=request_key,
                kind=previous.kind,
                state="queued",
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
            raise RunSwitchOperationConflict(
                "run-switch.artifact-inspector-does-not-support-model-cache"
            )
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
                    Job.state.in_(("queued", "running", "waiting-for-operator")),
                    or_(due_at.is_(None), due_at <= _now(self._clock).isoformat()),
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
            except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
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
                )
                continue
        return advanced

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
                raise KeyError(request.recipe_revision_id)
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
                        "run-switch.recipe_unresolved",
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
                        "run-switch.model_recipe_mismatch",
                        "The selected model variant is not the model pinned by this recipe revision.",
                        scope="model",
                    )
                )
            (
                _model_document,
                model_documents,
                model_caps,
                recipe_caps,
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
                        "run-switch.mapping_materialization_unavailable",
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
                and installation.state == "installed"
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
                            for name in RuntimeImageIdentity.model_fields
                        },
                    )
            if (
                installation is not None
                and _is_source_build(revision.document)
                and (
                    build is None
                    or not installation_matches_runtime_image(
                        installation,
                        build_id=build.id,
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
                    published_receipt_lookup=self._published_image_receipt,
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
                    raise RunSwitchOperationConflict(
                        "profile.runtime-image-changed: selected build differs from the accepted image; review and load the profile again"
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
                            "run-switch.installation_preparation_unavailable",
                            "No phase executor is configured to prepare this exact recipe on the selected group.",
                            scope="operation",
                            node_ids=node_ids,
                        )
                    )
            elif (
                request.action != "install"
                and installation.state == "installed"
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
                            "run-switch.run_admission_unavailable",
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
                                reason.code == "run.insufficient_memory"
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
                                "run-switch.run_admission_blocked",
                                "The existing run admission primitive rejected one or more selected ranks.",
                                scope="operation",
                                node_ids=node_ids,
                            )
                        )
            if not model_caps:
                warnings.append(
                    _as_reason(
                        "run-switch.model_capabilities_unknown",
                        "The exact model revision declares no typed capability facts.",
                        scope="model",
                        severity="warning",
                    )
                )
            if not recipe_caps:
                warnings.append(
                    _as_reason(
                        "run-switch.recipe_capabilities_unknown",
                        "The exact recipe revision declares no typed capability facts.",
                        scope="recipe",
                        severity="warning",
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
                        "run-switch.artifact-phase-executor-unavailable",
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
                "model_capabilities": model_caps,
                "recipe_capabilities": recipe_caps,
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
        include_capability_summary: bool = True,
    ) -> tuple[
        Mapping[str, object] | None,
        Mapping[tuple[str, str, str], Mapping[str, object]],
        list[CapabilityEvidence],
        list[CapabilityEvidence],
        list[RunSwitchReason],
    ]:
        blockers: list[RunSwitchReason] = []
        model_document: Mapping[str, object] | None = None
        model_documents: dict[tuple[str, str, str], Mapping[str, object]] = {}
        recipe_caps = _recipe_capability_facts(
            revision.document if revision is not None else None
        )
        model_caps: list[CapabilityEvidence] = []
        if revision is None:
            return None, {}, [], recipe_caps, blockers
        if (
            requested_recipe_digest is not None
            and revision.content_digest != requested_recipe_digest
        ):
            blockers.append(
                _as_reason(
                    "run-switch.recipe_digest_changed",
                    "The selected recipe revision digest changed before planning.",
                    scope="recipe",
                    stale=True,
                )
            )
        try:
            resolved = resolve_recipe_entities(session, revision.document)
            resolved_models = resolved.get("models")
            resolved_model_items = (
                tuple(resolved_models)
                if isinstance(resolved_models, Sequence)
                and not isinstance(resolved_models, (str, bytes))
                else ()
            )
            resolved_model = resolved_model_items[0] if resolved_model_items else None
            for resolved_item in resolved_model_items:
                candidate_item = getattr(resolved_item, "document", None)
                if not isinstance(candidate_item, Mapping):
                    continue
                publisher = getattr(resolved_item, "publisher", None)
                slug = getattr(resolved_item, "slug", None)
                content_digest = getattr(resolved_item, "content_digest", None)
                if (
                    isinstance(publisher, str)
                    and publisher
                    and isinstance(slug, str)
                    and slug
                    and isinstance(content_digest, str)
                    and content_digest
                ):
                    model_documents[(publisher, slug, content_digest)] = candidate_item
            candidate = getattr(resolved_model, "document", None)
            if isinstance(candidate, Mapping):
                model_document = candidate
            if (
                include_capability_summary
                and self._model_capability_summary is not None
            ):
                provider = self._model_capability_summary
                try:
                    summary = (
                        provider(session, model_digest)
                        if callable(provider)
                        else provider.get(session, model_digest)
                    )
                    model_caps = _summary_capability_facts(summary)
                except (KeyError, RuntimeError, TypeError, ValueError):
                    model_caps = []
            if (
                model_digest is None
                or getattr(resolved_model, "content_digest", None) != model_digest
            ):
                blockers.append(
                    _as_reason(
                        "run-switch.model_revision_unavailable",
                        "The exact model definition selected for this run is not resolved in local catalog authority.",
                        scope="model",
                    )
                )
        except (RecipeRuntimeSpecError, RuntimeError, TypeError, ValueError):
            blockers.append(
                _as_reason(
                    "run-switch.recipe_dependencies_unavailable",
                    "Exact model and runtime dependencies could not be resolved from immutable catalog authority.",
                    scope="recipe",
                )
            )
        return model_document, model_documents, model_caps, recipe_caps, blockers

    def _resolve_mapping(
        self,
        session: Session,
        revision: CatalogDocumentRevision,
        group: SparkGroup,
        *,
        actor: str,
    ) -> tuple[
        ClusterMapping | None,
        MappingSelection | None,
        list[RunSwitchReason],
    ]:
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
            return mapping, self._mapping_selection(mapping, nodes), []
        try:
            plan = self._mappings.preview(revision.id, desired_ids, {}, actor)
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
                        "run-switch.mapping_invalid",
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
                        "run-switch.mapping_group_mismatch",
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
                        ("installed", "installing", "partial")
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
            installed = {
                (node.node_id, node.rank, node.role)
                for node in session.scalars(
                    select(InstallationNode).where(
                        InstallationNode.installation_id == installation.id
                    )
                )
                if node.state == "installed"
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
        revision = session.get(CatalogDocumentRevision, revision_id)
        if revision is not None and not _is_source_build(revision.document):
            return None
        if expected_image is not None:
            # Accepted work remains bound to its approved receipt even when a
            # newer completed build becomes available while it is waiting.
            build = (
                session.get(RecipeBuild, expected_image.build_id)
                if expected_image.build_id is not None
                else None
            )
            if build is None or not self._build_is_available(build):
                return None
            authorized = (
                build.recipe_revision_id == revision_id
                or session.scalar(
                    select(RuntimeImageAuthorization.id)
                    .where(
                        RuntimeImageAuthorization.recipe_revision_id == revision_id,
                        RuntimeImageAuthorization.source == "controller-build",
                        RuntimeImageAuthorization.state == "authorized",
                        RuntimeImageAuthorization.build_id == build.id,
                        RuntimeImageAuthorization.oci_archive_sha256
                        == expected_image.oci_layout_sha256,
                    )
                    .limit(1)
                )
                is not None
            )
            if not authorized:
                raise RunSwitchOperationConflict(
                    "profile.runtime-image-changed: accepted build is not authorized for this recipe"
                )
            return build

        # A fresh review selects the current completed image, independently of
        # the immutable build that an older installation still references.
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

        # An editorial successor may reuse a build from another revision only
        # through its explicit authorization binding.
        authorized_build_ids = session.scalars(
            select(RuntimeImageAuthorization.build_id)
            .where(
                RuntimeImageAuthorization.recipe_revision_id == revision_id,
                RuntimeImageAuthorization.source == "controller-build",
                RuntimeImageAuthorization.state == "authorized",
                RuntimeImageAuthorization.build_id.is_not(None),
            )
            .order_by(RuntimeImageAuthorization.authorized_at.desc())
        )
        for build_id in authorized_build_ids:
            build = session.get(RecipeBuild, build_id)
            if build is not None and self._build_is_available(build):
                return build
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

        if not _is_source_build(revision.document):
            # Published images are selected by the canonical recipe and a
            # verified RuntimeImageReceipt.  Creating a synthetic RecipeBuild
            # would change the authority boundary and make a direct install
            # depend on source availability.
            return _BuildSelection(build=None, candidate=None)
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
                        "run-switch.container-build-unavailable",
                        "The existing recipe build primitive is unavailable; the Controller cannot prepare the exact OCI runtime image.",
                        scope="operation",
                        node_ids=[node.node_id for node in group.nodes],
                    ),
                ),
            )

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
        ordered_nodes = tuple(
            sorted(nodes, key=lambda node: (node.node_id in group_ids, node.node_id))
        )
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
                    "run-switch.container-build-unavailable",
                    detail,
                    scope="operation",
                    node_ids=[node.node_id for node in group.nodes],
                ),
            ),
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
            or "recipe.build.v1" not in node.capabilities
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
        published_receipt_lookup: PublishedImageReceiptLookup | None = None,
        expected_image: RuntimeImageIdentity | None = None,
    ) -> tuple[
        RunSwitchBuildEvidence,
        RuntimeImageStorageImpact,
        list[RunSwitchReason],
        list[RunSwitchReason],
    ]:
        blockers: list[RunSwitchReason] = []
        warnings: list[RunSwitchReason] = []
        authorization: RuntimeImageAuthorization | None = None
        document = revision.document if revision is not None else {}
        execution = document.get("execution") if isinstance(document, Mapping) else None
        source_build = _is_source_build(document)
        published_digest = _published_manifest_digest(document)
        direct_receipt = None
        direct_receipt_verified = False
        direct_receipt_missing = False
        direct_receipt_issue: str | None = None
        if not source_build and revision is not None and published_digest is not None:
            # Preview has not yet compiled mapping parameters into the
            # effective execution key. Reuse the same immutable published
            # image identity across parameter-only keys; compile/install
            # resolves and persists the exact effective key before planning.
            authorization = session.scalar(
                select(RuntimeImageAuthorization)
                .where(
                    RuntimeImageAuthorization.recipe_revision_id == revision.id,
                    RuntimeImageAuthorization.source == "published",
                    RuntimeImageAuthorization.state == "authorized",
                    RuntimeImageAuthorization.registry_manifest_digest
                    == published_digest,
                )
                .order_by(
                    RuntimeImageAuthorization.authorized_at.desc(),
                    RuntimeImageAuthorization.id.desc(),
                )
                .limit(1)
            )
            if authorization is not None:
                direct_receipt = {
                    "platform_manifest_digest": (
                        authorization.platform_manifest_digest
                    ),
                    "image_bytes": authorization.image_bytes,
                    "oci_archive_sha256": authorization.oci_archive_sha256,
                }
        raw_build = execution.get("build") if isinstance(execution, Mapping) else None
        raw_platform = None
        if isinstance(raw_build, Mapping):
            base_image = raw_build.get("base_image")
            if isinstance(base_image, Mapping):
                raw_platform = base_image.get("platform")
            if raw_platform is None:
                raw_platform = raw_build.get("platform")
        expected_architecture = (
            str(raw_platform)
            if isinstance(raw_platform, str) and raw_platform
            else "linux/arm64"
            if not source_build
            else "unknown"
        )
        if authorization is not None:
            if published_receipt_lookup is None:
                direct_receipt_missing = True
            else:
                try:
                    observed_receipt = published_receipt_lookup(
                        published_digest or "",
                        expected_architecture=expected_architecture,
                        expected_runtime_interface=RUNTIME_INTERFACE,
                    )
                except RuntimeImagePreparationError as error:
                    direct_receipt_issue = f"{error.code}: {error}"
                else:
                    if observed_receipt is None:
                        direct_receipt_missing = True
                    elif _published_receipt_matches_authorization(
                        observed_receipt,
                        authorization,
                        expected_architecture=expected_architecture,
                    ):
                        direct_receipt_verified = True
                    else:
                        direct_receipt_issue = "managed published receipt does not match its durable authorization"
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
        if not source_build:
            source_state = "available"
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
                candidate_plan = parse_stored_build_plan(candidate.plan)
            except RecipeExecutionContractError:
                candidate_plan_valid = False
                blockers.append(
                    _as_reason(
                        "run-switch.container-build-plan-invalid",
                        "The persisted source-build plan is invalid.",
                        scope="operation",
                    )
                )
            else:
                observed_architecture = candidate_plan.platform
            if candidate_plan_valid and observed_architecture is None:
                builder = session.get(AgentNode, candidate.builder_node_id)
                if builder is not None and isinstance(builder.architecture, str):
                    observed_architecture = _normalise_architecture(
                        builder.architecture
                    )
        elif direct_receipt is not None:
            # A runtime-image authorization is a verified platform image; the
            # runtime-image contract fixes its architecture to linux-arm64, so
            # the observation is that constant rather than a column the
            # authorization does not carry.
            observed_architecture = RUNTIME_IMAGE_ARCHITECTURE
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
            if source_build
            and source_identity is not None
            and source_identity.image_digest is not None
            else expected_image.image_digest
            if source_build and build is None and expected_image is not None
            else build.image_digest
            if build is not None
            else direct_receipt["platform_manifest_digest"]
            if direct_receipt is not None
            else expected_image.image_digest
            if not source_build and expected_image is not None
            else None
        )
        image_bytes = (
            source_identity.image_bytes
            if source_build
            and source_identity is not None
            and source_identity.image_bytes is not None
            else expected_image.image_bytes
            if source_build and build is None and expected_image is not None
            else build.image_bytes
            if build is not None
            else direct_receipt["image_bytes"]
            if direct_receipt is not None
            else expected_image.image_bytes
            if not source_build and expected_image is not None
            else None
        )
        oci_layout = (
            source_identity.oci_layout_sha256
            if source_build
            and source_identity is not None
            and source_identity.oci_layout_sha256 is not None
            else expected_image.oci_layout_sha256
            if source_build and build is None and expected_image is not None
            else build.oci_layout_sha256
            if build is not None
            else direct_receipt["oci_archive_sha256"]
            if direct_receipt is not None
            else expected_image.oci_layout_sha256
            if not source_build and expected_image is not None
            else None
        )
        runtime_reused = 0
        runtime_missing = 0
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
                    and artifact.state == "verified"
                    and artifact.size_bytes >= image_bytes
                ):
                    runtime_reused += image_bytes
                else:
                    runtime_missing += image_bytes
                if (
                    artifact is not None
                    and artifact.state == "verified"
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
            preparation_required=(
                not source_build
                and published_digest is not None
                and direct_receipt_issue is None
                and not direct_receipt_verified
            ),
            registry_manifest_digest=published_digest,
            image_digest=image_digest,
            oci_layout_sha256=oci_layout,
            image_bytes=image_bytes,
            required_bytes=(
                image_bytes * len(group.nodes) if image_bytes is not None else None
            ),
            reused_bytes=runtime_reused,
            copied_bytes=runtime_missing,
            missing_nas_bytes=(
                image_bytes
                if direct_receipt_missing or (source_build and build is None)
                else 0
                if direct_receipt_verified
                else None
            ),
            missing_spark_bytes=(runtime_missing if image_bytes is not None else None),
            missing_image_distribution_bytes=(
                runtime_missing if image_bytes is not None else None
            ),
            nas_coverage=(
                "partial"
                if direct_receipt_missing or (source_build and build is None)
                else "complete"
                if direct_receipt_verified
                or (
                    source_build
                    and build is not None
                    and image_bytes is not None
                    and oci_layout is not None
                )
                else "unknown"
            ),
            spark_coverage=runtime_coverage,
            reclaimable_bytes=runtime_reclaimable,
            reclaimable_digests=sorted(runtime_reclaimable_digests),
        )
        state = (
            "available"
            if direct_receipt_verified
            else "incompatible"
            if direct_receipt_issue is not None
            else "missing"
            if direct_receipt is not None
            else "missing"
            if candidate is None
            else str(candidate.state)
        )
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
        elif not source_build and direct_receipt is None:
            detail = "No verified published runtime image receipt is available for this recipe revision."
        elif direct_receipt_issue is not None:
            detail = direct_receipt_issue
        elif direct_receipt_missing:
            detail = "The authorized published runtime image archive is missing from Controller storage."
        if compatibility_state == "incompatible":
            state = "incompatible"
        if require_available:
            if source_build and build is None:
                pending = candidate is not None and candidate.state in {
                    "planned",
                    "building",
                }
                if (pending or defer_source_build) and source_state == "available":
                    warnings.append(
                        _as_reason(
                            "run-switch.container-build-required",
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
                            "run-switch.recipe-build-unavailable",
                            (
                                "No successful immutable runtime image build is available."
                                if candidate is None
                                else f"Runtime image preparation is {candidate.state}: {candidate.error or 'no completed image receipt is available.'}"
                            ),
                            scope="operation",
                            node_ids=[node.node_id for node in group.nodes],
                        )
                    )
            elif direct_receipt_issue is not None:
                blockers.append(
                    _as_reason(
                        "run-switch.runtime-image-authorization-mismatch",
                        direct_receipt_issue,
                        scope="artifact",
                        node_ids=[node.node_id for node in group.nodes],
                    )
                )
            elif direct_receipt_missing:
                warnings.append(
                    _as_reason(
                        "run-switch.runtime-image-preparation-required",
                        "The exact authorized published image archive is missing from Controller storage and will be restored before install admission.",
                        scope="operation",
                        severity="warning",
                        node_ids=[node.node_id for node in group.nodes],
                    )
                )
            elif not source_build and direct_receipt is None:
                warnings.append(
                    _as_reason(
                        "run-switch.runtime-image-preparation-required",
                        detail
                        or "The pinned published image will be pulled and verified before install admission.",
                        scope="operation",
                        severity="warning",
                        node_ids=[node.node_id for node in group.nodes],
                    )
                )
            if compatibility_state == "incompatible":
                blockers.append(
                    _as_reason(
                        "run-switch.recipe-build-incompatible",
                        compatibility.detail
                        or "Runtime image architecture is incompatible with the selected group.",
                        scope="operation",
                        node_ids=[node.node_id for node in group.nodes],
                    )
                )
            elif compatibility_state == "unknown":
                warnings.append(
                    _as_reason(
                        "run-switch.recipe-build-compatibility-unknown",
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
            # RolloutPreparation until a successful build or published receipt
            # exists.
            return None
        primary_model_digest = _primary_model_digest(revision.document)
        if primary_model_digest is None:
            return None
        model_digests = tuple(inspection.artifact_digests)
        model_expected = _per_target_bytes(inspection.required_bytes, len(group.nodes))
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
        target_count = len(group.nodes)
        model_missing = _per_target_bytes(inspection.missing_spark_bytes, target_count)
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
            target_ready = model_expected is not None and model_missing == 0
            model_targets.append(
                TargetAssetState(
                    node_id=node.node_id,
                    state="ready" if target_ready else "unknown",
                    expected_bytes=model_expected,
                    present_bytes=(
                        model_expected - (model_missing or 0)
                        if model_expected is not None and model_missing is not None
                        else 0
                    ),
                    missing_bytes=(
                        model_missing
                        if model_expected is not None and model_missing is not None
                        else None
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
                    reason.code == "run-switch.runtime-image-authorization-mismatch"
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
            source=(
                "controller-build"
                if build is not None or build_candidate is not None
                else "published"
            ),
            reason=(
                None
                if runtime_storage.nas_coverage == "complete"
                and runtime_storage.missing_nas_bytes in (None, 0)
                else next(
                    (
                        reason.detail[:256]
                        for reason in reasons
                        if reason.code
                        == "run-switch.runtime-image-authorization-mismatch"
                    ),
                    "The exact OCI archive is missing from Controller storage."
                    if runtime_storage.missing_nas_bytes not in (None, 0)
                    else "Controller storage coverage for the exact OCI image is not proven.",
                )
            ),
        )
        runtime_targets = [
            TargetAssetState(
                node_id=node.node_id,
                state="ready"
                if runtime_storage.missing_image_distribution_bytes == 0
                else "unknown",
                expected_bytes=image_bytes,
                present_bytes=(
                    image_bytes
                    if runtime_storage.missing_image_distribution_bytes == 0
                    else max(
                        0,
                        image_bytes
                        - _required_per_target_bytes(
                            runtime_storage.missing_image_distribution_bytes,
                            target_count,
                        ),
                    )
                ),
                missing_bytes=_per_target_bytes(
                    runtime_storage.missing_image_distribution_bytes,
                    target_count,
                ),
                verified_sha256=layout_digest
                if runtime_storage.missing_image_distribution_bytes == 0
                else None,
                verified_at=now
                if runtime_storage.missing_image_distribution_bytes == 0
                else None,
                imported_image_digest=(
                    image_digest
                    if runtime_storage.missing_image_distribution_bytes == 0
                    else None
                ),
                reason=(
                    None
                    if runtime_storage.missing_image_distribution_bytes == 0
                    else "The exact OCI image is not imported on this Spark."
                ),
            )
            for node in group.nodes
        ]
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
                    RecipeRun.state.in_(_STOPPABLE_RUN_STATES),
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
                    "run-switch.cross-group_conflict",
                    "An active distributed run crosses the selected complete Spark group; partial stop is unsafe.",
                    scope="conflict",
                    node_ids=tuple(sorted(overlap)),
                )
                conflicts.append(reason)
                blockers.append(reason)
                continue
            if action == "run":
                reason = _as_reason(
                    "run-switch.active-run-conflict",
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
                    "run-switch.stop_plan_unavailable",
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
            raise RunSwitchOperationConflict("run-switch.recipe_unresolved")
        _, model_documents, _, _, blockers = self._resolve_documents(
            session,
            revision,
            request.model_content_sha256,
            requested_recipe_digest=revision.content_digest,
            include_capability_summary=False,
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
        return RunSwitchAssessment.model_validate(
            {
                **{
                    name: getattr(reviewed, name)
                    for name in RunSwitchAssessment.model_fields
                },
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
            }
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
        model_documents: Mapping[tuple[str, str, str], Mapping[str, object]],
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
        model_documents: Mapping[tuple[str, str, str], Mapping[str, object]]
        | None = None,
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
        topology = revision.document.get("topology") if revision is not None else None
        roles = topology.get("roles") if isinstance(topology, Mapping) else None
        role_by_name = (
            {
                str(role.get("name")): role
                for role in roles
                if isinstance(role, Mapping) and isinstance(role.get("name"), str)
            }
            if isinstance(roles, list)
            else {}
        )
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
                    port_demand = run_port_demand(
                        revision.document,
                        node_count=len(group.nodes),
                        endpoint_owner=item.endpoint_owner,
                    )
                except (TypeError, ValueError) as error:
                    node_blockers.append(
                        _as_reason(
                            "run-switch.interface-invalid",
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
                        "run-switch.inventory-unknown",
                        "No authenticated Spark inventory is available.",
                        scope="freshness",
                        node_ids=(item.node_id,),
                    )
                )
            elif evidence.state == "stale":
                node_blockers.append(
                    _as_reason(
                        "run-switch.inventory-stale",
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
                        "run-switch.spark-unavailable",
                        "Selected Spark is not active in Controller authority.",
                        scope="node",
                        node_ids=(item.node_id,),
                    )
                )
            role = role_by_name.get(item.role)
            resources = role.get("resources") if isinstance(role, Mapping) else None
            memory = resources.get("memory") if isinstance(resources, Mapping) else None
            disk = resources.get("disk") if isinstance(resources, Mapping) else None
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
            if not isinstance(memory, Mapping) or not isinstance(disk, Mapping):
                node_blockers.append(
                    _as_reason(
                        "run-switch.resource-contract-invalid",
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
                                "run-switch.memory-envelope-invalid",
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
                                        raise ValueError(
                                            "active run memory claim has an invalid kind"
                                        )
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
                try:
                    image_size = (
                        image_bytes
                        if image_bytes is not None
                        else _required_int(disk.get("image_bytes"))
                    )
                    artifact_size = (
                        artifact_bytes
                        if artifact_bytes is not None
                        else _required_int(disk.get("artifact_bytes"))
                    )
                    if image_size is None or artifact_size is None:
                        raise ValueError("payload size is unavailable")
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
                    node_blockers.append(
                        _as_reason(
                            "run-switch.disk-envelope-invalid",
                            "The recipe disk envelope is incomplete.",
                            scope="recipe",
                            node_ids=(item.node_id,),
                        )
                    )
                else:
                    # Review promises a full allocation, including headroom.
                    # Installation may reduce it for exact target-local reuse;
                    # it must never grow a claim after operator acceptance.
                    required_disk = disk_need.required_bytes + disk_need.floor_bytes
                    disk_free = (
                        snapshot.disk_free_bytes if snapshot is not None else None
                    )
                    if snapshot is not None and disk_free is not None:
                        reserved_disk = outstanding_disk_reservation_bytes(
                            session,
                            item.node_id,
                            inventory_observed_at=snapshot.observed_at,
                            excluded_run_ids=excluded,
                            excluded_profile_application_ids=excluded_profile_application_ids,
                        )
                        disk_free_after = disk_free - reserved_disk - required_disk
                        if disk_free_after < 0:
                            node_blockers.append(
                                _as_reason(
                                    "run-switch.insufficient-disk",
                                    f"The operation needs {required_disk} bytes and would leave {disk_free_after} bytes.",
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
                    ResourceReservation.state == "active",
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
                        "run-switch.artifact-identity-unknown",
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
                        "run-switch.artifact-inspection-unavailable",
                        f"Artifact coverage could not be inspected: {error}",
                        scope="artifact",
                    ),
                ),
            )
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
                        "run-switch.nas-coverage-unknown",
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
                        "run-switch.artifact-manifest-unknown",
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
        installation_id: str | None,
        installation_state: str | None,
        stops: Sequence[StopImpact],
        inspection: ArtifactInspection,
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
        node_ids = [node.node_id for node in group.nodes]
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
                        detail="Stop the selected workload as one complete group.",
                    )
                )
            phases.append(
                RunSwitchPhase(
                    index=len(phases),
                    kind="final_verify",
                    state="planned" if not blockers else "blocked",
                    node_ids=node_ids,
                    detail="Verify that the selected workload is stopped and its route is withdrawn.",
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
        needs_prepare = installation_id is None or installation_state != "installed"
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
        if stops and stop_before_prepare and not stop_added:
            add(
                "stop",
                "Stop conflicting workloads before memory-consuming runtime preparation.",
            )
            stop_added = True
        if build_required and stop_before_prepare:
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

    def _stop_digest(self, run_id: str) -> str | None:
        if self._lifecycle is None:
            return None
        try:
            return self._lifecycle.preview_stop(run_id).plan_digest
        except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError):
            return None

    @staticmethod
    def _run_reserved_bytes(session: Session, run_id: str) -> int:
        return int(
            session.scalar(
                select(
                    func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)
                ).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                    ResourceReservation.state == "active",
                )
            )
            or 0
        )

    @staticmethod
    def _finalize_plan(data: Mapping[str, object]) -> RunSwitchPlan:
        plan = RunSwitchPlan.model_validate(dict(data))
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
    ) -> RunSwitchOperation:
        now = _now(self._clock)
        total_bytes, member_totals = _planned_transfer_bytes(plan)
        payload = {
            "schema_version": 2,
            "operation_kind": kind,
            "action": plan.action,
            "plan_digest": plan.plan_digest,
            "plan": plan.model_dump(mode="json"),
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
                ],
            },
            "retry": {"automatic_attempts": 1, "operator_retries": 0},
        }
        with self._sessions.begin() as session:
            nodes = list(
                session.scalars(
                    select(AgentNode)
                    .where(
                        AgentNode.node_id.in_(
                            [node.node_id for node in plan.spark_group.nodes]
                        )
                    )
                    .order_by(AgentNode.node_id)
                    .with_for_update()
                )
            )
            if len(nodes) != len(plan.spark_group.nodes):
                raise RunSwitchOperationConflict(
                    "run-switch target Spark scope changed"
                )
            existing = session.scalar(select(Job).where(Job.request_id == request_key))
            if existing is not None:
                if (
                    existing.kind != kind
                    or existing.payload.get("plan_digest") != plan.plan_digest
                ):
                    raise RunSwitchOperationConflict(
                        "run-switch.request_key_reused_differently"
                    )
                return self._operation_view(existing)
            _reserve_run_switch_assets(session, plan, now=now)
            try:
                lock_run_switch_build_dependency(session, plan)
            except BuildConsumerError as error:
                raise RunSwitchOperationConflict(f"{error.code}: {error}") from error
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
                raise RunSwitchOperationConflict(
                    "run-switch.superseded: Spark scope has a later workload intent"
                )
            payload["workload_intent_ordinal"] = workload_intent_ordinal
            payload["progress"]["workload_intent_ordinal"] = workload_intent_ordinal
            job = Job(
                id=str(uuid.uuid4()),
                request_id=request_key,
                kind=kind,
                state="queued",
                actor=actor,
                authority_revision=(plan.recipe_content_sha256 or plan.plan_digest),
                targets=[node.node_id for node in plan.spark_group.nodes],
                payload_digest=_digest(payload),
                payload=payload,
                result=_persisted_result(payload["progress"]),
                created_at=now,
                updated_at=now,
            )
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
        plan_digest: str | None,
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
            if existing.kind != kind or (
                plan_digest is not None
                and existing.payload.get("plan_digest") != plan_digest
            ):
                raise RunSwitchOperationConflict(
                    "run-switch.request_key_reused_differently"
                )
            return self._operation_view(existing)

    def _advance(self, operation_id: str) -> bool:
        now = _now(self._clock)
        with self._sessions() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None or job.kind not in _OPERATION_KINDS:
                return True
            if job.state not in {"queued", "running", "waiting-for-operator"}:
                return False
            payload = job.payload
            raw_plan = payload.get("plan")
            if not isinstance(raw_plan, Mapping):
                _reject_invalid_operation(
                    job, "run-switch persisted plan is invalid", now
                )
                session.commit()
                return True
            try:
                plan = _load_plan(raw_plan)
            except RunSwitchOperationConflict:
                _reject_invalid_operation(
                    job, "run-switch persisted plan is invalid", now
                )
                session.commit()
                return True
            try:
                progress = _read_progress(job.result)
            except RunSwitchOperationConflict:
                # The stored result is the evidence of what was issued; it is
                # retained untouched rather than replaced with a fabricated
                # empty progress document.
                _reject_invalid_operation(
                    job, "run-switch persisted progress is invalid", now
                )
                session.commit()
                return True
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
            if intent_status == "superseded":
                job.state = "cancelled"
                job.status_reason = (
                    "run-switch.superseded: the logical order was cancelled by "
                    "a later authorized Spark intent; issued effects still "
                    "require their own cancellation receipts"
                )
                progress["retryable"] = False
                job.result = _persisted_result(progress)
                job.updated_at = now
                session.commit()
                return True
            observation_due = progress.get("observation_due_at")
            if isinstance(observation_due, str) and now < _aware(
                datetime.fromisoformat(observation_due)
            ):
                return False
            raw_phase_index = progress.get("phase_index", 0)
            raw_item_index = progress.get("item_index", 0)
            child_id = progress.get("child_operation_id")
            if job.state == "waiting-for-operator":
                if not child_id:
                    return False
                # Only observe the already-issued child. No new effect is
                # authorized by reopening this parent's observation checkpoint.
                job.state = "running"
                session.commit()
            if progress.get("cancellation") and child_id is None:
                _complete_cancellation(job, progress, now)
                session.commit()
                return True
            expected_image = None
            profile_application_id = _string_or_none(
                progress.get("profile_application_id")
            )
            if (
                profile_application_id is not None
                and plan.recipe_revision_id is not None
            ):
                try:
                    expected_image = accepted_profile_runtime_image(
                        session,
                        profile_application_id,
                        plan.recipe_revision_id,
                        tuple(node.node_id for node in plan.spark_group.nodes),
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
                job.state = "failed"
                job.status_reason = "run-switch persisted progress is invalid"
                job.updated_at = now
                session.commit()
                return True
            # Declare the validated integers so the checkpoint closure below
            # carries their exact type rather than the raw JSON union.
            phase_index: int = raw_phase_index
            item_index: int = raw_item_index
            if phase_index >= len(plan.phases):
                job.state = "succeeded"
                job.status_reason = None
                progress = _complete_operation_progress(plan, progress)
                job.result = _persisted_result(progress)
                job.updated_at = now
                session.commit()
                return True
            checkpoint_actor = job.actor
            checkpoint_request_key = job.request_id

        checkpoint_plan = plan
        checkpoint_cancellation = progress.get("cancellation")
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
            retryable: bool = False,
            failure_code: str | None = None,
        ) -> None:
            self._fail(
                operation_id,
                reason,
                retryable=retryable,
                checkpoint=(phase_index, item_index, child_id),
                failure_code=failure_code,
                checkpoint_guard=checkpoint_job,
            )

        if child_id is not None:
            if not isinstance(child_id, str):
                fail("run-switch child operation identity is invalid")
                return True
            try:
                child = self._get_child_operation(child_id)
            except KeyError:
                fail("run-switch child operation disappeared")
                return True
            if child is None:
                fail("run-switch child operation disappeared")
                return True
            if child.state in {"queued", "running"}:
                child_progress = _child_progress_payload(child)
                with self._sessions.begin() as session:
                    job = checkpoint_job(session)
                    if job is None:
                        return False
                    original = _read_progress(job.result)
                    progress = dict(original)
                    if not _checkpoint_matches(
                        job, progress, phase_index, item_index, child_id
                    ):
                        return False
                    persisted_plan = _load_plan(job.payload["plan"])
                    persisted_phase_index = require_integer(
                        progress.get("phase_index", phase_index), "phase index"
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
                    progress["phase"] = persisted_phase.kind
                    progress["subphase"] = persisted_phase.subphase
                    retry_due_at = getattr(child, "retry_due_at", None)
                    if child.state == "queued" and isinstance(retry_due_at, datetime):
                        progress["observation_due_at"] = _aware(
                            retry_due_at
                        ).isoformat()
                    else:
                        progress.pop("observation_due_at", None)
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
                    job.state = "running"
                    job.status_reason = status_reason
                    job.result = _persisted_result(progress)
                    job.updated_at = now
                return True
            if child.state in _TERMINAL_STATES and child.state != "succeeded":
                with self._sessions.begin() as session:
                    job = checkpoint_job(session)
                    current = _read_progress(job.result) if job is not None else {}
                    if (
                        job is not None
                        and _checkpoint_matches(
                            job, current, phase_index, item_index, child_id
                        )
                        and current.get("cancellation")
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
                elif child.state == "waiting-for-operator":
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
                        job.state = "waiting-for-operator"
                        due = now + timedelta(seconds=60)
                        progress["observation_due_at"] = due.isoformat()
                        job.result = _persisted_result(progress)
                        job.status_reason = (
                            (
                                getattr(child, "status_reason", None)
                                or "Lifecycle effect is uncertain; exact child remains pending"
                            )[:400]
                            + f"; next exact observation at {due.isoformat()}"
                        )[:512]
                        job.updated_at = now
                    return True
                else:
                    reason = f"run-switch phase operation failed: {child.state if child else 'unknown'}"
                    evidence = _child_progress_payload(child)
                    detail = evidence.get("reason") or evidence.get("status_reason")
                    if isinstance(detail, str) and detail:
                        reason += ": " + detail[:384]
                    kind = _child_failure_kind(child)
                    # The agent authority owns exact transfer retry/observation
                    # and its durable attempt budget. A terminal child is not
                    # replayed by a second parent-level automatic retry loop.
                    self._fail(
                        operation_id,
                        reason,
                        retryable=classify(kind) is RecoveryDecision.RETRY,
                        checkpoint=(phase_index, item_index, child_id),
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
                progress["observation_due_at"] = None
                progress["observation_deadline_at"] = None
                job.status_reason = None
                phase_index = require_integer(
                    progress.get("phase_index", 0), "phase index"
                )
                item_index = (
                    require_integer(progress.get("item_index", 0), "item index") + 1
                )
                persisted_plan = _load_plan(job.payload["plan"])
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
                    results = list(
                        require_sequence(
                            progress.get("phase_results", []), "phase results"
                        )
                    )
                    results.extend(
                        _phase_result(receipt, phase=phase)
                        for receipt in child_receipts
                        if isinstance(receipt, Mapping)
                    )
                    progress["phase_results"] = results
                if phase.subphase == "container-build":
                    try:
                        receipt = _build_receipt_in_session(
                            session,
                            persisted_plan,
                            expected_image=expected_image,
                        )
                    except RunSwitchOperationConflict as error:
                        job.state = "failed"
                        job.status_reason = str(error)[:512]
                        progress["failed_phase"] = phase.kind
                        job.result = _persisted_result(progress)
                        job.updated_at = now
                        return True
                    results = list(
                        require_sequence(
                            progress.get("phase_results", []), "phase results"
                        )
                    )
                    if not any(
                        isinstance(item, Mapping)
                        and item.get("build_id") == receipt["build_id"]
                        and item.get("image_digest") == receipt["image_digest"]
                        for item in results
                    ):
                        results.append(_phase_result(receipt, phase=phase))
                        progress["phase_results"] = results
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
                        self._mark_failed(job, str(error), now=now, progress=progress)
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
                        self._mark_failed(job, str(error), now=now, progress=progress)
                        return True
                    progress["phase_results"] = [
                        *require_sequence(
                            progress.get("phase_results", []), "phase results"
                        ),
                        receipt,
                    ]
                item_total = len(persisted_plan.stops) if phase.kind == "stop" else 1
                progress["child_operation_id"] = None
                if item_index >= item_total:
                    completed = list(
                        require_sequence(
                            progress.get("completed_phases", []), "completed phases"
                        )
                    )
                    completed.append(phase.kind)
                    progress["completed_phases"] = completed
                    _complete_phase_progress(progress, persisted_plan, phase)
                    progress["phase_index"] = phase_index + 1
                    progress["item_index"] = 0
                    progress["phase"] = (
                        persisted_plan.phases[phase_index + 1].kind
                        if phase_index + 1 < len(persisted_plan.phases)
                        else "final_verify"
                    )
                    progress["subphase"] = (
                        persisted_plan.phases[phase_index + 1].subphase
                        if phase_index + 1 < len(persisted_plan.phases)
                        else None
                    )
                else:
                    progress["item_index"] = item_index
                job.state = "running"
                job.result = _persisted_result(progress)
                job.updated_at = now
                if progress.get("cancellation"):
                    _complete_cancellation(job, progress, now)
            return True
        with self._sessions.begin() as session:
            job = checkpoint_job(session)
            if job is None or job.state not in {"queued", "running"}:
                return False
            plan = _load_plan(job.payload["plan"])
            progress = _read_progress(job.result)
            if progress.get("cancellation"):
                _complete_cancellation(job, progress, now)
                return True
            job.state = "running"
            phase_index = require_integer(progress.get("phase_index", 0), "phase index")
            item_index = require_integer(progress.get("item_index", 0), "item index")
            if phase_index >= len(plan.phases):
                return True
            phase = plan.phases[phase_index]
            actor = job.actor
            request_key = job.request_id
        gate = getattr(self._phase_executor, "preflight", None)
        if isinstance(gate, _PhasePreflightGate):
            try:
                checkpoint, blocked = gate(
                    plan, phase, actor=actor, request_key=request_key, progress=progress
                )
            except (RuntimeError, ValueError, KeyError) as error:
                fail(str(error))
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
                    current["preflight"] = checkpoint.model_dump(mode="json")
                    current["observation_due_at"] = (
                        checkpoint.next_check_at.isoformat()
                        if checkpoint.next_check_at is not None
                        else None
                    )
                    if blocked:
                        self._mark_failed(job, blocked, now=now, progress=current)
                        return True
                    if checkpoint.pending_job_id:
                        current["operation"] = observe_progress(
                            _progress_mapping(current.get("operation")),
                            {
                                "phase": "runtime-preflight",
                                "completed_bytes": 0,
                                "total_bytes_known": False,
                            },
                            now,
                        )
                    job.result = _persisted_result(current)
                    job.updated_at = now
                if checkpoint.pending_job_id or checkpoint.next_check_at is not None:
                    return True
        if phase.state in {"skipped", "retained"}:
            execution = PhaseExecution()
        elif phase.state == "blocked":
            fail(f"run-switch phase blocked: {phase.kind}")
            return True
        elif self._phase_executor is None:
            fail(f"run-switch phase executor unavailable: {phase.kind}")
            return True
        else:
            try:
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
            except RunSwitchInstallPreflightExpired:
                return self._hold_for_preflight_refresh(
                    operation_id, phase_index, item_index
                )
            except (
                AdmissionLockBusy,
                InstallAdmissionBusy,
                RunAdmissionBusy,
                RecipeBuildAdmissionBusy,
            ) as busy:
                return self._hold_capacity_writer(
                    operation_id, phase_index, item_index, reason=busy.code
                )
            except RunSwitchPostStopEvidencePending as pending:
                return self._hold_capacity_writer(
                    operation_id,
                    phase_index,
                    item_index,
                    reason=pending.code,
                    detail=str(pending),
                )
            except RunSwitchIssuedWorkloadPending as pending:
                return self._hold_issued_observation(
                    operation_id,
                    phase_index=phase_index,
                    item_index=item_index,
                    pending=pending,
                )
            except RunSwitchOperationConflict as error:
                fail(str(error))
                return True
            except RuntimeImagePreparationError as error:
                if error.code == _RUNTIME_IMAGE_OWNER_CHANGED:
                    return False
                if error.retryable and error.code.startswith("artifact."):
                    return self._hold_capacity_writer(
                        operation_id,
                        phase_index,
                        item_index,
                        reason=error.code,
                        detail=error.detail,
                    )
                fail(
                    f"{type(error).__name__}: {error}",
                    failure_code=error.code,
                )
                return True
            except (
                OSError,
                httpx.HTTPError,
                RuntimeError,
                TypeError,
                ValueError,
                KeyError,
            ) as error:
                fail(
                    f"{type(error).__name__}: {error}",
                    retryable=_transient_distribution_exception(error),
                )
                return True
        if (
            execution.waiting
            and execution.operation_id is None
            and phase.kind != "final_verify"
        ):
            fail(f"run-switch.{phase.kind}-waiting-without-child")
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
                fail(str(error))
                return True
        with self._sessions.begin() as session:
            job = checkpoint_job(session)
            if job is None:
                return False
            progress = _read_progress(job.result)
            if not _checkpoint_matches(job, progress, phase_index, item_index, None):
                return False
            if progress.get("observation_due_at") is not None:
                progress["observation_due_at"] = None
                progress["observation_deadline_at"] = None
                job.status_reason = None
            if progress.get("retry_reason") in (
                AdmissionLockBusy.code,
                InstallAdmissionBusy.code,
                RunAdmissionBusy.code,
                RecipeBuildAdmissionBusy.code,
                RunSwitchPostStopEvidencePending.code,
            ):
                progress["retry_reason"] = None
            _merge_progress_evidence(
                progress,
                plan,
                phase,
                execution.result,
                now,
            )
            if execution.waiting:
                progress["phase"] = phase.kind
                progress["subphase"] = phase.subphase
                if phase.kind == "final_verify" and execution.operation_id is None:
                    started = progress.setdefault(
                        "final_verify_started_at", now.timestamp()
                    )
                    if (
                        isinstance(started, bool)
                        or not isinstance(started, (int, float))
                        or now.timestamp() < started
                    ):
                        self._mark_failed(
                            job,
                            "run-switch.final-verification-clock-invalid",
                            now=now,
                            progress=progress,
                        )
                        return True
                    due = now + timedelta(
                        seconds=60 if now.timestamp() - started >= 300 else 5
                    )
                    progress["observation_due_at"] = due.isoformat()
                    job.status_reason = (
                        "Waiting for exact run and route verification; "
                        f"next observation at {due.isoformat()}"
                    )
                    # Keep one current observation while awaiting route publication.
                    # Repeated polling must not grow durable phase receipts.
                    progress["final_observation"] = _phase_result(
                        execution.result or {}, phase=phase
                    )
                elif execution.result is not None:
                    results = list(
                        require_sequence(
                            progress.get("phase_results", []), "phase results"
                        )
                    )
                    results.append(_phase_result(execution.result, phase=phase))
                    progress["phase_results"] = results
            elif execution.operation_id is not None:
                progress["child_operation_id"] = execution.operation_id
                progress["phase"] = phase.kind
                progress["subphase"] = phase.subphase
                if phase.kind == "start":
                    child = session.get(Job, execution.operation_id)
                    raw_deadline = (
                        child.payload.get("start_deadline")
                        if child is not None
                        else None
                    )
                    if child is not None and isinstance(raw_deadline, str):
                        deadline = _aware(datetime.fromisoformat(raw_deadline))
                        progress["start_deadline"] = deadline.isoformat()
                        budget = int(
                            (deadline - _aware(child.created_at)).total_seconds()
                        )
                        if budget > 0:
                            progress["startup_budget_seconds"] = budget
                if execution.result is not None:
                    results = list(
                        require_sequence(
                            progress.get("phase_results", []), "phase results"
                        )
                    )
                    results.append(_phase_result(execution.result, phase=phase))
                    progress["phase_results"] = results
            else:
                if execution.result is not None:
                    results = list(
                        require_sequence(
                            progress.get("phase_results", []), "phase results"
                        )
                    )
                    results.append(_phase_result(execution.result, phase=phase))
                    progress["phase_results"] = results
                if phase.subphase == "container-build":
                    try:
                        receipt = _build_receipt_in_session(
                            session, plan, expected_image=expected_image
                        )
                    except RunSwitchOperationConflict as error:
                        job.state = "failed"
                        job.status_reason = str(error)[:512]
                        progress["failed_phase"] = phase.kind
                        job.result = _persisted_result(progress)
                        job.updated_at = now
                        return True
                    results = list(
                        require_sequence(
                            progress.get("phase_results", []), "phase results"
                        )
                    )
                    if not any(
                        isinstance(item, Mapping)
                        and item.get("build_id") == receipt["build_id"]
                        and item.get("image_digest") == receipt["image_digest"]
                        for item in results
                    ):
                        results.append(_phase_result(receipt, phase=phase))
                        progress["phase_results"] = results
                completed = list(
                    require_sequence(
                        progress.get("completed_phases", []), "completed phases"
                    )
                )
                completed.append(phase.kind)
                progress["completed_phases"] = completed
                _complete_phase_progress(progress, plan, phase)
                progress["phase_index"] = (
                    require_integer(progress.get("phase_index", 0), "phase index") + 1
                )
                progress["item_index"] = 0
                next_index = int(progress["phase_index"])
                progress["phase"] = (
                    plan.phases[next_index].kind
                    if next_index < len(plan.phases)
                    else "final_verify"
                )
                progress["subphase"] = (
                    plan.phases[next_index].subphase
                    if next_index < len(plan.phases)
                    else None
                )
            job.state = "running"
            job.result = _persisted_result(progress)
            job.updated_at = now
            if progress.get("cancellation") and not progress.get("child_operation_id"):
                _complete_cancellation(job, progress, now)
        return True

    @staticmethod
    def _scope_intent_status(session: Session, job: Job) -> str:
        """Distinguish malformed authority from a later authorized node head."""

        ordinal = job.payload.get("workload_intent_ordinal")
        if type(ordinal) is not int or ordinal < 1:
            return "invalid"
        nodes = session.scalars(
            select(AgentNode).where(AgentNode.node_id.in_(job.targets))
        )
        current = list(nodes)
        if len(current) != len(job.targets) or any(
            node.revoked_at is not None or node.state != "active" for node in current
        ):
            return "invalid"
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
            raw_deadline = progress.get("observation_deadline_at")
            if isinstance(raw_deadline, str):
                deadline = _aware(datetime.fromisoformat(raw_deadline))
            else:
                child = session.get(Job, child_id)
                child_deadline = (
                    child.payload.get("start_deadline") if child is not None else None
                )
                deadline = (
                    _aware(datetime.fromisoformat(child_deadline))
                    if isinstance(child_deadline, str)
                    else now + timedelta(seconds=120)
                )
            if now >= deadline:
                # Expiry establishes an overdue observation, not a stopped or
                # failed runtime. Preserve the exact child and reservations;
                # its owner alone can reconcile or retry the uncertain effect.
                progress["observation_deadline_at"] = deadline.isoformat()
                due = now + timedelta(seconds=60)
                progress["observation_due_at"] = due.isoformat()
                job.state = "waiting-for-operator"
                job.status_reason = (
                    "run-switch.start-observation-expired: exact effect remains "
                    f"unresolved; next observation at {due.isoformat()}"
                )
                job.result = _persisted_result(progress)
                job.updated_at = now
                return True
            progress["observation_deadline_at"] = deadline.isoformat()
            progress["observation_due_at"] = min(
                deadline, now + timedelta(seconds=5)
            ).isoformat()
            job.state = "running"
            job.status_reason = "Start result uncertain; observing the existing run."
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
            progress["observation_due_at"] = due.isoformat()
            progress["observation_deadline_at"] = deadline.isoformat()
            job.state = "running"
            job.status_reason = (
                f"Observing older {pending.kind} operation {pending.job_id}; "
                f"effect unresolved, next observation at {due.isoformat()}"
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
    ) -> bool:
        """Retry an unchanged capacity handoff at most once per five seconds.

        The admission transaction has rolled back and released its locks.
        The wait belongs to the existing operation, holds no worker slot, and
        expires at the next admission attempt; busy SQL is not a failed effect.
        """
        now = _now(self._clock)
        due = now + timedelta(seconds=5)
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return False
            progress = _read_progress(job.result)
            if not _checkpoint_matches(job, progress, phase_index, item_index, None):
                return False
            if progress.get("cancellation"):
                _complete_cancellation(job, progress, now)
            else:
                progress["retry_reason"] = reason
                progress["observation_due_at"] = due.isoformat()
                progress["observation_deadline_at"] = due.isoformat()
                job.state = "running"
                job.status_reason = (
                    detail or "Admission is waiting for the Controller capacity writer"
                ) + f"; admission will retry at {due.isoformat()}."
                job.result = _persisted_result(progress)
                job.updated_at = now
        return True

    def _hold_for_preflight_refresh(
        self,
        operation_id: str,
        phase_index: int,
        item_index: int,
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
            if progress.get("cancellation"):
                _complete_cancellation(job, progress, now)
                return True
            attempt = (
                require_integer(progress.get("retry_attempt"), "retry attempt")
                if progress.get("retry_reason") == _INSTALL_PREFLIGHT_REFRESH_REASON
                and progress.get("retry_attempt") is not None
                else 1
            )
            progress["retry_reason"] = _INSTALL_PREFLIGHT_REFRESH_REASON
            progress["operation_phase_index"] = phase_index
            progress["operation"] = observe_progress(
                _progress_mapping(progress.get("operation")),
                {
                    "phase": "install-preflight-refresh",
                    "completed_items": attempt,
                    "completed_bytes": 0,
                    "total_bytes_known": False,
                },
                now,
            )
            self._schedule_checkpoint_retry(
                job, progress, _INSTALL_PREFLIGHT_REFRESH_REASON, now
            )
        return True

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
        retryable: bool = False,
        failure_code: str | None = None,
        checkpoint: tuple[int, int, object] | None = None,
        child_evidence: object | None = None,
        checkpoint_guard: Callable[[Session], Job | None] | None = None,
    ) -> None:
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
                plan = _load_plan(job.payload["plan"])
                if checkpoint[0] < len(plan.phases):
                    _merge_progress_evidence(
                        progress, plan, plan.phases[checkpoint[0]], child_evidence, now
                    )
            if progress.get("cancellation"):
                if progress.get("child_operation_id") is None:
                    _complete_cancellation(job, progress, now)
                    return
            elif retryable and checkpoint is not None and checkpoint[2] is None:
                plan = _load_plan(job.payload["plan"])
                phase = plan.phases[checkpoint[0]]
                if phase.kind in {"prepare", "transfer", "verify"}:
                    # The phase executor reuses its deterministic request key
                    # and exact stored plan. Issued children keep their own
                    # retry owner; never replace one with a parent-level replay.
                    self._schedule_checkpoint_retry(job, progress, reason, now)
                    return
            self._mark_failed(
                job,
                reason,
                now=now,
                retryable=retryable,
                failure_code=failure_code,
                progress=progress,
            )

    @staticmethod
    def _schedule_checkpoint_retry(
        job: Job, progress: dict[str, Any], reason: str, now: datetime
    ) -> None:
        """Wait at the exact checkpoint without replacing accepted intent."""
        attempt = require_integer(progress.get("retry_attempt") or 1, "retry attempt")
        due = RecoveryPolicy().next_attempt(job.id, attempt, now, ongoing_intent=True)
        assert due is not None
        progress["retry_attempt"] = attempt + 1
        progress["retry_reason"] = reason[:512]
        progress["observation_due_at"] = due.isoformat()
        progress["retryable"] = False
        job.state = "running"
        job.status_reason = f"{reason[:400]}; next attempt at {due.isoformat()}"[:512]
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
        progress: dict[str, Any] | None = None,
    ) -> None:
        """Record failure using the caller's transaction and existing row lock."""

        job.state = "failed"
        job.status_reason = reason[:512]
        if progress is None:
            progress = _read_progress(job.result)
        progress["failed_phase"] = progress.get("phase")
        progress["retryable"] = retryable
        if failure_code is None:
            progress.pop("failure_code", None)
        else:
            progress["failure_code"] = failure_code
        job.result = _persisted_result(progress)
        job.updated_at = now

    @staticmethod
    def _operation_view(job: Job) -> RunSwitchOperation:
        progress = _read_progress(job.result)
        persisted_result = _parse_persisted_result(job.result)
        # Target membership belongs to the Job; receipts carry member progress.
        progress["node_ids"] = list(job.targets)
        plan = _load_plan(job.payload.get("plan"))
        current_phase = persisted_result.phase if persisted_result is not None else None
        completed = (
            persisted_result.completed_phases if persisted_result is not None else []
        )
        return RunSwitchOperation(
            operation_id=job.id,
            kind=_OPERATION_KIND_ADAPTER.validate_python(job.kind, strict=True),
            action=plan.action,
            state=job.state,
            plan_digest=plan.plan_digest,
            request_key=job.request_id,
            cleanup_mode=(plan.cleanup_mode if plan.action == "cleanup" else None),
            installation_id=(
                plan.installation_id if plan.action == "cleanup" else None
            ),
            node_ids=list(job.targets),
            current_phase=current_phase,
            completed_phases=completed,
            progress=_progress_view(
                plan,
                progress,
                job.state,
                job.status_reason,
            ),
            status_reason=job.status_reason,
            result=persisted_result,
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
            raise ValueError("operation provider page limit is invalid")
        state = getattr(query, "state", None)
        if state is not None and not isinstance(state, str):
            raise ValueError("operation provider state filter is invalid")
        node_id = getattr(query, "node_id", None)
        if node_id is not None and not isinstance(node_id, str):
            raise ValueError("operation provider node filter is invalid")
        request_id = getattr(query, "request_id", None)
        if request_id is not None and not isinstance(request_id, str):
            raise ValueError("operation provider request filter is invalid")
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
                raise ValueError("operation provider cursor is invalid")
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
                raise KeyError(operation_id)
            return self._item(job)

    def _item(self, job: Job) -> Mapping[str, object]:
        node_ids: list[str] = []
        try:
            if (
                not isinstance(job.targets, list)
                or not job.targets
                or any(
                    not isinstance(node_id, str)
                    or re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None
                    for node_id in job.targets
                )
            ):
                raise ValueError("stored Run/Switch targets are malformed")
            node_ids = list(job.targets)
            operation = self._service._operation_view(job)
        except (
            AttributeError,
            KeyError,
            RunSwitchOperationConflict,
            TypeError,
            ValueError,
            ValidationError,
        ):
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
                "failure": {
                    "error_code": "operation_history_unreadable",
                    "summary": "Stored Run/Switch history is malformed",
                    "retryable": False,
                },
            }
        endpoint_node = node_ids[0]
        raw_plan = job.payload.get("plan") if isinstance(job.payload, Mapping) else None
        if isinstance(raw_plan, Mapping):
            raw_group = raw_plan.get("spark_group")
            raw_nodes = (
                raw_group.get("nodes") if isinstance(raw_group, Mapping) else None
            )
            if isinstance(raw_nodes, list):
                endpoint_node = next(
                    (
                        str(node.get("node_id"))
                        for node in raw_nodes
                        if isinstance(node, Mapping)
                        and node.get("endpoint_owner") is True
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
                if operation.state in {"queued", "running"}
                and not (operation.result and operation.result.cancellation)
                and (
                    operation.state == "queued"
                    or operation.current_phase not in {"start", "final_verify"}
                )
                else []
            ),
            "result": _activity_result(operation),
            "detail": operation.status_reason,
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

    result = (
        operation.result.model_dump(mode="json") if operation.result is not None else {}
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

    node_ids = [node.node_id for node in plan.spark_group.nodes]
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
    model_each = _per_target_bytes(model_bytes, len(node_ids))
    image_each = _per_target_bytes(image_bytes, len(node_ids))
    each = (
        model_each + image_each
        if model_each is not None and image_each is not None
        else None
    )
    return total, {node_id: each for node_id in node_ids}


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
        raise RunSwitchOperationConflict(
            "profile.runtime-image-changed: "
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
        raise RunSwitchOperationConflict(
            "run-switch.container-build-receipt-unavailable"
        )
    build = session.get(RecipeBuild, build_id)
    if (
        build is None
        or build.recipe_revision_id != plan.recipe_revision_id
        or build.state != "succeeded"
        or build.build_input_sha256 != plan.build.build_input_sha256
        or not _is_oci_digest(build.image_digest)
        or not _is_hex_digest(build.oci_layout_sha256)
        or type(build.image_bytes) is not int
        or build.image_bytes < 1
    ):
        raise RunSwitchOperationConflict(
            "run-switch.container-build-receipt-unavailable"
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
        raise RunSwitchOperationConflict("run-switch workload intent is unbound")
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


def _parse_persisted_result(value: object) -> RunSwitchOperationResult | None:
    """Parse stored JSON strictly, including nested datetime and tuple fields."""

    if value is None:
        return None
    try:
        return RunSwitchOperationResult.model_validate_json(
            json.dumps(value), strict=True
        )
    except (TypeError, ValueError) as error:
        raise RunSwitchOperationConflict(
            "run-switch persisted result is invalid"
        ) from error


def _read_progress(value: object) -> dict[str, object]:
    result = _parse_persisted_result(value)
    return (
        result.model_dump(mode="json", exclude_unset=True) if result is not None else {}
    )


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

    try:
        normalized = dict(value)
        if phase is not None:
            if "phase" in normalized and normalized["phase"] != phase.kind:
                raise ValueError("phase receipt belongs to a different phase")
            normalized.setdefault("phase", phase.kind)
            subphase = getattr(phase, "subphase", None)
            if subphase is None and phase.kind in {"transfer", "verify"}:
                subphase = "target-copy"
            if "subphase" in normalized and normalized["subphase"] != subphase:
                raise ValueError("phase receipt belongs to a different subphase")
            normalized.setdefault("subphase", subphase)
        assignments = normalized.get("assignments")
        if isinstance(assignments, Mapping):
            normalized["assignments"] = {
                node_id: DistributionAssignment.parse(raw)
                if isinstance(raw, Mapping)
                else raw
                for node_id, raw in assignments.items()
            }
        result = _PHASE_RESULT_ADAPTER.validate_python(normalized, strict=True)
    except (TypeError, ValueError) as error:
        raise RunSwitchOperationConflict(
            "run-switch phase receipt is invalid"
        ) from error
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

    if phase.kind != "start" or getattr(child, "state", None) != "waiting-for-operator":
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
    if phase.kind != "start" or getattr(child, "state", None) != "waiting-for-operator":
        return False
    run_id = getattr(child, "owner_id", None)
    getter = getattr(lifecycle, "run_status", None)
    if not isinstance(run_id, str) or not callable(getter):
        return False
    try:
        status: Any = getter(run_id)
    except (KeyError, RuntimeError, TypeError, ValueError):
        return False
    return status.state in _ACTIVE_RUN_STATES and any(
        rank.fresh and rank.state in {"planned", "starting", "running"}
        for rank in status.ranks
    )


def _transient_distribution_exception(error: BaseException) -> bool:
    if isinstance(error, httpx.HTTPError):
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        if type(status) is int:
            return status == 429 or status >= 500
        return isinstance(error, (httpx.TimeoutException, httpx.ConnectError))
    if isinstance(error, OSError):
        return getattr(error, "errno", None) in {
            errno.ECONNRESET,
            errno.ECONNREFUSED,
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
            errno.ETIMEDOUT,
            errno.EPIPE,
        }
    return False


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
    job.state = "cancelled"
    cancellation = _progress_mapping(progress.get("cancellation"))
    if cancellation is None:
        raise RunSwitchOperationConflict("run-switch cancellation evidence is invalid")
    job.status_reason = _string_or_none(cancellation.get("reason"))
    progress["child_operation_id"] = None
    progress["retryable"] = False
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
        raise RunSwitchOperationConflict("run-switch.container-build-plan-invalid")
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
        job = session.scalar(
            select(Job)
            .where(Job.request_id == request_key)
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
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
        raise RunSwitchOperationConflict("run-switch.container-build-parent-invalid")
    if _load_plan(job.payload["plan"]) != plan:
        raise RunSwitchOperationConflict("run-switch.container-build-plan-invalid")
    current = _read_progress(job.result)
    if _bound_workload_intent(current) != ordinal:
        raise RunSwitchOperationConflict("run-switch.container-build-parent-invalid")
    if (
        not _checkpoint_matches(job, current, phase_index, item_index, child_id)
        or current.get("cancellation") != cancellation
        or RunSwitchOperationService._scope_intent_status(session, job) != "current"
    ):
        raise _RunSwitchBuildParentChanged("run-switch.container-build-parent-changed")
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
        progress["operation"] = OperationProgress.model_validate(canonical).model_dump(
            mode="json", exclude_none=True
        )
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
        current = OperationProgress.model_validate(values).model_dump(
            mode="json", exclude_none=True
        )
        if prior is not None and prior.get("phase") == phase.kind:
            current["completed_bytes"] = max(
                require_integer(prior.get("completed_bytes", 0), "completed bytes"),
                reported_completed,
            )
        progress["operation"] = observe_progress(prior, current, now)
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
    known_nodes = {node.node_id for node in plan.spark_group.nodes}
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
            target["state"] = member_state
        error = item.get("error")
        if isinstance(error, str):
            target["error"] = error[:256]
    progress["members"] = list(existing.values())


def _complete_phase_progress(
    progress: dict[str, object],
    plan: RunSwitchPlan,
    phase: RunSwitchPhase,
) -> None:
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
        item["state"] = "succeeded"
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
        item["state"] = "succeeded"
        item["error"] = None
    progress["members"] = list(entries.values())
    progress["phase"] = "final_verify"
    progress["subphase"] = None
    progress["retryable"] = False
    progress["failed_phase"] = None
    progress.pop("failure_code", None)
    progress["child_operation_id"] = None
    return progress


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
        node_ids = [node.node_id for node in plan.spark_group.nodes]
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
        # High-level operations always have a complete group.  Keep a valid
        # DTO if a corrupted historical row is inspected so the API can still
        # report the operation's durable failure.
        raise RunSwitchOperationConflict("run-switch operation has no target members")
    measurement = (
        OperationProgress.model_validate(raw["operation"])
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
        raise RunSwitchOperationConflict(
            f"run-switch.{phase.kind}-returned-invalid-evidence"
        )
    if phase.kind == "prepare" and phase.subphase == "runtime-image":
        try:
            receipt = RuntimeImageReceiptDocument.model_validate(
                result.get("runtime_image"), strict=True
            )
        except (TypeError, ValidationError) as error:
            raise RunSwitchOperationConflict(
                "run-switch.runtime-image-preparation-receipt-invalid"
            ) from error
        image_digest = receipt.image_digest
        layout_digest = receipt.oci_archive_sha256
        registry_digest = receipt.registry_manifest_digest
        if expected_image is not None:
            _require_profile_runtime_image(
                expected_image,
                {
                    "image_digest": image_digest,
                    "oci_layout_sha256": layout_digest,
                    "image_bytes": receipt.image_bytes,
                    "build_id": receipt.build_id,
                    "architecture": receipt.architecture,
                    "runtime_interface": receipt.runtime_interface,
                },
            )
        if (
            plan.image_digest is not None
            and image_digest != plan.image_digest
            and registry_digest != plan.image_digest
        ):
            raise RunSwitchOperationConflict(
                "run-switch.runtime-image-preparation-digest-mismatch"
            )
        expected_layout = plan.build.oci_layout_sha256
        if expected_layout is not None and layout_digest != expected_layout:
            raise RunSwitchOperationConflict(
                "run-switch.runtime-image-preparation-layout-mismatch"
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
            raise RunSwitchOperationConflict(
                "run-switch.model-download-artifact-set-mismatch"
            )
        if result.get("coverage") != "complete":
            raise RunSwitchOperationConflict(
                "run-switch.model-download-coverage-incomplete"
            )
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
            raise RunSwitchOperationConflict(
                "run-switch.model-download-byte-evidence-mismatch"
            )
        return
    if phase.kind == "verify":
        try:
            normalized = dict(result)
            normalized.setdefault("phase", "verify")
            normalized.setdefault("subphase", "target-copy")
            verification = RunSwitchVerifyResult.model_validate(normalized, strict=True)
        except (TypeError, ValidationError) as error:
            if (
                plan.recipe_build_id is not None
                and isinstance(result, Mapping)
                and result.get("verified_build_id") != plan.recipe_build_id
            ):
                raise RunSwitchOperationConflict(
                    "run-switch.runtime-build-verification-mismatch"
                ) from error
            raise RunSwitchOperationConflict(
                "run-switch.artifact-verification-result-invalid"
            ) from error
        result = verification.model_dump(mode="python")
        if result.get("verified") is not True:
            raise RunSwitchOperationConflict(
                "run-switch.artifact-digest-verification-failed"
            )
        expected = set(plan.storage.artifact_digests)
        if expected:
            raw_digests = result.get("verified_digests")
            if (
                not isinstance(raw_digests, list)
                or not all(isinstance(value, str) for value in raw_digests)
                or set(raw_digests) != expected
            ):
                raise RunSwitchOperationConflict(
                    "run-switch.artifact-digest-verification-mismatch"
                )
        if plan.image_digest is not None:
            verified_image = result.get("verified_image_digest")
            verified_registry = result.get("verified_registry_manifest_digest")
            if (
                verified_image != plan.image_digest
                and verified_registry != plan.image_digest
            ):
                raise RunSwitchOperationConflict(
                    "run-switch.runtime-image-verification-mismatch"
                )
            expected_layout = plan.build.oci_layout_sha256
            if (
                expected_layout is not None
                and result.get("verified_oci_layout_sha256") != expected_layout
            ):
                raise RunSwitchOperationConflict(
                    "run-switch.runtime-layout-verification-mismatch"
                )
        if verification.verified_build_id != plan.recipe_build_id:
            # A build performed by the same high-level operation has no OCI
            # output digest at preview time.  The distribution adapter must
            # bind its verification receipt to the exact durable build row;
            # published-image plans must carry an explicit null build ID.
            raise RunSwitchOperationConflict(
                "run-switch.runtime-build-verification-mismatch"
            )
    elif phase.kind == "cleanup":
        if result.get("scope") != "spark-local":
            raise RunSwitchOperationConflict("run-switch.cleanup-scope-invalid")
        if result.get("nas_evicted") is True:
            raise RunSwitchOperationConflict(
                "run-switch.cleanup-nas-eviction-forbidden"
            )
        reclaimed = result.get("reclaimed_bytes")
        if type(reclaimed) is not int or reclaimed < 0:
            raise RunSwitchOperationConflict(
                "run-switch.cleanup-reclaim-evidence-invalid"
            )
        protected_bytes = result.get("protected_referenced_bytes")
        if type(protected_bytes) is not int or protected_bytes < 0:
            raise RunSwitchOperationConflict(
                "run-switch.cleanup-reference-protection-evidence-invalid"
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
            raise RunSwitchOperationConflict(
                "run-switch.cleanup-reference-protection-evidence-invalid"
            )
        reclaimed_digests = set(raw_reclaimed)
        protected_digests = set(raw_protected)
        if reclaimed_digests & protected_digests:
            raise RunSwitchOperationConflict(
                "run-switch.cleanup-reference-protection-overlap"
            )
        allowed_reclaimable = set(plan.storage.reclaimable_digests)
        allowed_reclaimable.update(plan.runtime_storage.reclaimable_digests)
        if not reclaimed_digests <= allowed_reclaimable:
            raise RunSwitchOperationConflict(
                "run-switch.cleanup-reclaimed-digest-not-planned"
            )
        maximum_reclaimable = (
            plan.storage.reclaimable_bytes + plan.runtime_storage.reclaimable_bytes
        )
        if reclaimed > maximum_reclaimable:
            raise RunSwitchOperationConflict(
                "run-switch.cleanup-reclaimed-bytes-exceed-plan"
            )
    elif phase.kind == "transfer":
        copied = result.get("copied_bytes")
        if copied is not None and (type(copied) is not int or copied < 0):
            raise RunSwitchOperationConflict(
                "run-switch.transfer-byte-evidence-invalid"
            )


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


def _is_source_build(document: Mapping[str, object]) -> bool:
    execution = document.get("execution")
    return isinstance(execution, Mapping) and execution.get("mode") == "build"


def _published_manifest_digest(document: Mapping[str, object]) -> str | None:
    execution = document.get("execution")
    image = execution.get("image") if isinstance(execution, Mapping) else None
    digest = image.get("digest") if isinstance(image, Mapping) else None
    if isinstance(digest, str) and _is_oci_digest(digest):
        return digest
    if isinstance(digest, str) and _is_hex_digest(digest):
        return f"sha256:{digest}"
    return None


def _published_receipt_matches_authorization(
    receipt: RuntimeImageReceiptDocument,
    authorization: RuntimeImageAuthorization,
    *,
    expected_architecture: str,
) -> bool:
    """Match every durable published-image identity field to managed bytes."""

    return (
        receipt.source == "published"
        and receipt.distribution_content_sha256 == authorization.original_content_digest
        and receipt.registry_manifest_digest == authorization.registry_manifest_digest
        and receipt.platform_manifest_digest == authorization.platform_manifest_digest
        and receipt.image_digest == authorization.platform_manifest_digest
        and receipt.local_image_config_id == authorization.local_image_config_id
        and receipt.oci_archive_sha256 == authorization.oci_archive_sha256
        and receipt.image_bytes == authorization.image_bytes
        and receipt.build_id is None
        and receipt.build_input_sha256 is None
        and _normalise_architecture(receipt.architecture)
        == _normalise_architecture(expected_architecture)
        and receipt.runtime_interface == RUNTIME_INTERFACE
        and receipt.runtime_adapter is None
        and receipt.runtime_adapter_sha256 is None
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


RUNTIME_IMAGE_ARCHITECTURE = "linux/arm64"


def _normalise_architecture(value: str) -> str:
    return {
        "linux-arm64": "linux/arm64",
        "linux/aarch64": "linux/arm64",
        "aarch64": "linux/arm64",
        "arm64": "linux/arm64",
    }.get(value.lower(), value.lower())


def _per_target_bytes(value: int | None, target_count: int) -> int | None:
    if value is None or target_count < 1 or value % target_count != 0:
        return None
    return value // target_count


def _required_per_target_bytes(value: int | None, target_count: int) -> int:
    """Per-target bytes for arithmetic, failing closed when it is not exact."""

    per_target = _per_target_bytes(value, target_count)
    if per_target is None:
        raise RunSwitchOperationConflict("run-switch.per-target-byte-evidence-invalid")
    return per_target


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RunSwitchOperationConflict("run-switch persisted identity is invalid")
    return value


def _started_operation_id(value: object) -> str:
    """Read the identity of a started child operation without assuming its class."""

    operation_id = getattr(value, "id", None)
    if not isinstance(operation_id, str) or not operation_id:
        raise RunSwitchOperationConflict(
            "run-switch child operation identity is invalid"
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


def _load_plan(value: object) -> RunSwitchPlan:
    if not isinstance(value, Mapping):
        raise RunSwitchOperationConflict("run-switch persisted plan is invalid")
    # Job.payload is JSON, so strict validation must permit the RFC3339
    # timestamp representation when a worker restarts and reloads a plan.
    try:
        return RunSwitchPlan.model_validate_json(json.dumps(value), strict=True)
    except (TypeError, ValueError) as error:
        raise RunSwitchOperationConflict(
            "run-switch persisted plan is invalid"
        ) from error


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
        raise RunSwitchOperationConflict(f"{error.code}: {error.detail}") from error


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

    def owner_changed(detail: str) -> RuntimeImagePreparationError:
        return RuntimeImagePreparationError(_RUNTIME_IMAGE_OWNER_CHANGED, detail)

    def identity_invalid(detail: str) -> RuntimeImagePreparationError:
        return RuntimeImagePreparationError(
            "run-switch.runtime-image-reference-identity-mismatch", detail
        )

    try:
        parsed_receipt = RuntimeImageReceiptDocument.model_validate(
            receipt, strict=True
        )
    except (TypeError, ValueError) as error:
        raise identity_invalid("verified runtime image receipt is invalid") from error
    try:
        ordinal = _bound_workload_intent(progress)
    except RunSwitchOperationConflict as error:
        raise owner_changed("RunSwitch phase no longer has a workload claim") from error
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
        raise identity_invalid("runtime image callback is outside its phase")
    canonical_execution_keys = tuple(sorted(set(execution_keys)))
    if len(canonical_execution_keys) != len(execution_keys):
        raise identity_invalid("runtime image execution identities are not unique")

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
                raise RuntimeImagePreparationError(
                    "artifact.reference_busy",
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
            raise owner_changed("RunSwitch Spark scope or workload claim changed")

        try:
            require_reference_open(
                session,
                (ArtifactIdentity("runtime-image", parsed_receipt.oci_archive_sha256),),
                now=now,
            )
        except ArtifactLifecycleError as error:
            raise RuntimeImagePreparationError(
                error.code, error.detail, retryable=error.retryable
            ) from error

        try:
            job = session.scalar(
                select(Job)
                .where(Job.request_id == request_key)
                .with_for_update(nowait=True)
                .execution_options(populate_existing=True)
            )
        except DBAPIError as error:
            state = getattr(error.orig, "sqlstate", None) or getattr(
                error.orig, "pgcode", None
            )
            if state in {"55P03", "40P01", "40001"}:
                raise RuntimeImagePreparationError(
                    "artifact.reference_busy",
                    "RunSwitch operation ownership is changing; image publication will retry",
                    retryable=True,
                ) from error
            raise
        if (
            job is None
            or job.kind != "recipe.run-switch.v2"
            or job.actor != actor
            or job.state not in {"queued", "running"}
            or tuple(sorted(job.targets)) != target_nodes
            or job.payload.get("workload_intent_ordinal") != ordinal
            or job.payload.get("plan_digest") != plan.plan_digest
        ):
            raise owner_changed("RunSwitch operation no longer owns image publication")
        raw_plan = job.payload.get("plan")
        try:
            persisted_plan = _load_plan(raw_plan)
        except RunSwitchOperationConflict as error:
            raise identity_invalid("persisted RunSwitch plan is invalid") from error
        if persisted_plan != plan:
            raise identity_invalid("RunSwitch plan changed before image publication")

        try:
            current = _read_progress(job.result)
            current_ordinal = _bound_workload_intent(current)
        except RunSwitchOperationConflict as error:
            raise owner_changed(
                "RunSwitch progress no longer owns image publication"
            ) from error
        if (
            current_ordinal != ordinal
            or _string_or_none(current.get("profile_application_id"))
            != profile_application_id
            or current.get("cancellation") is not None
            or not _checkpoint_matches(job, current, phase.index, item_index, None)
            or RunSwitchOperationService._scope_intent_status(session, job) != "current"
        ):
            raise owner_changed("RunSwitch phase was cancelled or superseded")

        revision = session.get(CatalogDocumentRevision, recipe_revision_id)
        if (
            revision is None
            or revision.kind != "recipe"
            or revision.state != "active"
            or revision.content_digest != plan.recipe_content_sha256
            or parsed_receipt.distribution_publisher != revision.publisher
            or parsed_receipt.distribution_slug != revision.slug
            or parsed_receipt.distribution_content_sha256 != revision.content_digest
        ):
            raise identity_invalid(
                "runtime image no longer matches the approved recipe"
            )
        if plan.image_digest is not None and plan.image_digest not in {
            parsed_receipt.image_digest,
            parsed_receipt.registry_manifest_digest,
        }:
            raise identity_invalid(
                "runtime image differs from the approved image digest"
            )
        if (
            plan.runtime_storage.image_digest is not None
            and plan.runtime_storage.image_digest != parsed_receipt.image_digest
        ):
            raise identity_invalid(
                "runtime image differs from the approved platform digest"
            )
        if (
            plan.runtime_storage.registry_manifest_digest is not None
            and plan.runtime_storage.registry_manifest_digest
            != parsed_receipt.registry_manifest_digest
        ):
            raise identity_invalid(
                "runtime image differs from the approved registry digest"
            )
        expected_layout = (
            plan.runtime_storage.oci_layout_sha256 or plan.build.oci_layout_sha256
        )
        if (
            expected_layout is not None
            and expected_layout != parsed_receipt.oci_archive_sha256
        ):
            raise identity_invalid(
                "runtime image archive differs from the approved plan"
            )
        if (
            plan.runtime_storage.image_bytes is not None
            and plan.runtime_storage.image_bytes > 0
            and plan.runtime_storage.image_bytes != parsed_receipt.image_bytes
        ):
            raise identity_invalid("runtime image size differs from the approved plan")
        if (
            plan.build.image_digest is not None
            and plan.build.image_digest != parsed_receipt.image_digest
        ):
            raise identity_invalid(
                "runtime image differs from the approved build digest"
            )
        if (
            plan.build.oci_layout_sha256 is not None
            and plan.build.oci_layout_sha256 != parsed_receipt.oci_archive_sha256
        ):
            raise identity_invalid(
                "runtime image differs from the approved build archive"
            )
        if (
            plan.build.image_bytes is not None
            and plan.build.image_bytes > 0
            and plan.build.image_bytes != parsed_receipt.image_bytes
        ):
            raise identity_invalid("runtime image size differs from the approved build")

        expected_build_id = plan.recipe_build_id or plan.build.build_id
        if parsed_receipt.source == "controller-build":
            if (
                expected_build_id is None
                or parsed_receipt.build_id != expected_build_id
                or plan.recipe_revision_id is None
            ):
                raise identity_invalid("runtime image is not the approved build result")
            try:
                build = _build_receipt_in_session(session, plan)
            except RunSwitchOperationConflict as error:
                raise identity_invalid(
                    "approved recipe build is no longer available"
                ) from error
            if any(
                parsed_receipt_value != build_value
                for parsed_receipt_value, build_value in (
                    (parsed_receipt.build_id, build["build_id"]),
                    (parsed_receipt.build_input_sha256, build["build_input_sha256"]),
                    (parsed_receipt.image_digest, build["image_digest"]),
                    (parsed_receipt.oci_archive_sha256, build["oci_layout_sha256"]),
                    (parsed_receipt.image_bytes, build["image_bytes"]),
                    (parsed_receipt.build_input_sha256, plan.build.build_input_sha256),
                )
            ):
                raise identity_invalid(
                    "runtime image receipt differs from the approved build"
                )
        elif expected_build_id is not None:
            raise identity_invalid(
                "published runtime image conflicts with the selected build"
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
                        "build_id": parsed_receipt.build_id,
                        "architecture": parsed_receipt.architecture,
                        "runtime_interface": parsed_receipt.runtime_interface,
                    },
                )
            except (RunSwitchOperationConflict, ValueError) as error:
                raise identity_invalid(
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
            source=parsed_receipt.source,
            registry_manifest_digest=parsed_receipt.registry_manifest_digest,
            image_digest=parsed_receipt.image_digest,
            archive_sha256=parsed_receipt.oci_archive_sha256,
            image_bytes=parsed_receipt.image_bytes,
            build_id=parsed_receipt.build_id,
            build_input_sha256=parsed_receipt.build_input_sha256,
        )
        prior_intent = current.get("runtime_image_reference_intent")
        if prior_intent is not None:
            try:
                parsed_prior = RunSwitchRuntimeImageReferenceIntent.model_validate(
                    prior_intent, strict=True
                )
            except (TypeError, ValueError) as error:
                raise identity_invalid(
                    "stored RunSwitch image reference is invalid"
                ) from error
            if parsed_prior != intent:
                raise identity_invalid(
                    "RunSwitch image reference changed during publication"
                )
        else:
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

    job.state = "failed"
    job.status_reason = reason[:512]
    job.updated_at = now


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
