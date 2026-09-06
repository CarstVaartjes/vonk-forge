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
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .cluster_mappings import (
    ClusterMappingError,
    ClusterMappingPlan,
    ClusterMappingService,
)
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
    RuntimeImageReceipt,
)
from .preparation_contract import (
    ControllerAssetState,
    ModelArtifactPreparation,
    PreparationReason,
    RolloutPreparation,
    RuntimeImagePreparation,
    TargetAssetState,
)
from .recipe_operations import RecipeOperationConflict, RecipeOperationService
from .recipe_runtime_specs import RecipeRuntimeSpecError, resolve_recipe_entities
from .resource_planning import (
    CapacitySnapshot,
    PlannedStopRelease,
    ResourceDemand,
    ResourceEvidence,
    plan_capacity,
    resolve_effective_settings,
    resource_demand,
)
from .run_switch_contract import (
    ArtifactStorageImpact,
    BuildCompatibilityEvidence,
    BuildSourceEvidence,
    CapabilityEvidence,
    EffectiveParallelism,
    EffectiveSettingsSelection,
    FreshnessEvidence,
    InvocationMetadata,
    MappingSelection,
    RecipeBuildEvidence,
    ResourceDemandEvidence,
    RunSwitchApplyRequest,
    RunSwitchMemberProgress,
    RunSwitchOperation,
    RunSwitchPhase,
    RunSwitchPhaseKind,
    RunSwitchPlan,
    RunSwitchPreviewRequest,
    RunSwitchProgress,
    RunSwitchReason,
    RunSwitchStopApplyRequest,
    RunSwitchStopPreviewRequest,
    RuntimeImageStorageImpact,
    SparkFit,
    SparkFitNode,
    SparkGroup,
    SparkGroupNode,
    StopImpact,
)


class RunSwitchOperationConflict(RuntimeError):
    """The selected outcome is stale, unsupported, or unsafe to execute."""


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
    nas_coverage: str
    spark_coverage: str
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
    dependency_model_version_sha256: tuple[str, ...] = ()


class RunSwitchArtifactInspector(Protocol):
    """Read cache coverage without transferring or mutating any bytes."""

    def inspect(
        self,
        session: Session,
        *,
        model_version_sha256: str,
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


_ACTIVE_RUN_STATES = frozenset({"planned", "starting", "running", "stopping"})
_TERMINAL_STATES = frozenset({"succeeded", "failed", "expired", "cancelled"})
_OPERATION_KINDS = frozenset({"recipe.run-switch.v2", "recipe.stop.v2"})
_MAX_RETRY_ATTEMPTS = 3
_TERMINAL_RETRY_MARKERS = (
    "digest",
    "integrity",
    "auth",
    "credential",
    "permission",
    "denied",
    "revoked",
    "receipt",
)
_PHASES: tuple[RunSwitchPhaseKind, ...] = (
    "transfer",
    "verify",
    "prepare",
    "cleanup",
    "stop",
    "start",
    "final_verify",
)


def _now(clock: Any) -> datetime:
    value = clock()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
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
    scope: str,
    severity: str = "blocker",
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


def _manifest_value(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _manifest_artifact_size(manifest: object, digest: str) -> int:
    artifacts = _manifest_value(manifest, "artifacts")
    if isinstance(artifacts, Sequence) and not isinstance(
        artifacts, (str, bytes, bytearray)
    ):
        for item in artifacts:
            if _manifest_value(item, "sha256") != digest:
                continue
            size = _manifest_value(item, "expected_bytes")
            if size is None:
                size = _manifest_value(item, "download_bytes")
            if type(size) is int and size > 0:
                return size
    raise RuntimeError("model-cache artifact size is unavailable")


def _required_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _resource_reason(reason: object, *, node_ids: Sequence[str] = ()) -> RunSwitchReason:
    node_id = getattr(reason, "node_id", None)
    return _as_reason(
        f"run-switch.{getattr(reason, 'code', 'resource.evidence_unknown')}",
        str(getattr(reason, "detail", "Resource planning evidence is unavailable.")),
        scope="node" if isinstance(node_id, str) else "operation",
        severity=str(getattr(reason, "severity", "blocker")),
        node_ids=(node_id,) if isinstance(node_id, str) else node_ids,
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
        change_effects=dict(resolved.change_effects),
        identity_sha256=resolved.identity_digest,
    )


def _selected_model_bytes(
    recipe_document: Mapping[str, object],
    model_documents: Mapping[tuple[str, str, str], Mapping[str, object]] | None,
    role_name: str,
) -> int | None:
    if not model_documents:
        return None
    selections = recipe_document.get("models")
    if not isinstance(selections, Sequence) or isinstance(selections, (str, bytes)):
        return None
    total = 0
    selected_any = False
    for selection in selections:
        if not isinstance(selection, Mapping):
            return None
        model_ref = selection.get("model")
        if not isinstance(model_ref, Mapping):
            return None
        reference = tuple(model_ref.get(name) for name in ("publisher", "slug", "content_sha256"))
        if any(not isinstance(value, str) or not value for value in reference):
            return None
        model_document = model_documents.get(reference)
        if model_document is None:
            return None
        files = model_document.get("files")
        if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
            return None
        by_id = {
            str(file.get("id")): file
            for file in files
            if isinstance(file, Mapping) and isinstance(file.get("id"), str)
        }
        raw_files = selection.get("files")
        if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)):
            return None
        selected_ids: set[str] = set()
        for item in raw_files:
            if not isinstance(item, Mapping):
                return None
            roles = item.get("roles", ())
            if isinstance(roles, Sequence) and not isinstance(roles, (str, bytes)) and role_name in roles:
                file_id = item.get("file_id")
                if not isinstance(file_id, str) or file_id not in by_id:
                    return None
                selected_ids.add(file_id)
        for file_id in selected_ids:
            size = by_id[file_id].get("size_bytes")
            if type(size) is not int or size < 0:
                return None
            total += size
            selected_any = True
    return total if selected_any else None


def _resource_evidence(
    recipe_document: Mapping[str, object],
    memory: Mapping[str, object],
    role_name: str,
    model_documents: Mapping[tuple[str, str, str], Mapping[str, object]] | None,
    declared_total_bytes: int,
    settings: object | None,
) -> ResourceEvidence:
    del memory
    model_bytes = _selected_model_bytes(recipe_document, model_documents, role_name)
    return ResourceEvidence(
        weights_bytes=model_bytes,
        runtime_overhead_bytes=None,
        declared_total_bytes=declared_total_bytes if model_bytes is not None else None,
        baseline_context_tokens=getattr(settings, "context_tokens", None),
        baseline_concurrency=getattr(settings, "concurrency", None),
        baseline_batch_tokens=getattr(settings, "batch_tokens", None),
        evidence_state="declared" if model_bytes is not None else "unknown",
    )


def _resource_evidence_digest(revision_digest: str | None) -> str | None:
    return revision_digest if isinstance(revision_digest, str) and len(revision_digest) == 64 else None


def _planned_stop_releases(
    session: Session,
    node_id: str,
    reservation_kind: str | None,
    stops: Sequence[StopImpact],
) -> tuple[PlannedStopRelease, ...]:
    if reservation_kind is None:
        return ()
    releases: list[PlannedStopRelease] = []
    for stop in stops:
        if node_id not in stop.node_ids:
            continue
        amount = session.scalar(
            select(func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)).where(
                ResourceReservation.node_id == node_id,
                ResourceReservation.kind == reservation_kind,
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == stop.run_id,
                ResourceReservation.state == "active",
            )
        )
        releases.append(
            PlannedStopRelease(
                stop.run_id,
                node_id,
                reservation_kind,
                int(amount or 0),
                True,
                stop.plan_digest,
            )
        )
    return tuple(releases)


def _capability_facts(document: Mapping[str, object] | None) -> list[CapabilityEvidence]:
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
        evidence = "unknown"
        detail: str | None = None
        evidence_digest: str | None = None
        if isinstance(value, bool):
            declared = value
        elif isinstance(value, Mapping):
            candidate = value.get("declared", value.get("supported"))
            declared = candidate if isinstance(candidate, bool) else None
            candidate_evidence = value.get("evidence")
            if candidate_evidence in {"tested", "observed", "not-tested", "unknown"}:
                evidence = str(candidate_evidence)
            if isinstance(value.get("detail"), str):
                detail = str(value["detail"])[:256]
            if isinstance(value.get("evidence_digest"), str):
                evidence_digest = str(value["evidence_digest"])
        else:
            declared = None
        evidence_value = evidence_by_name.get(name)
        if isinstance(evidence_value, Mapping):
            candidate_evidence = evidence_value.get("state", evidence_value.get("evidence"))
            if candidate_evidence in {"tested", "observed", "not-tested", "unknown"}:
                evidence = str(candidate_evidence)
            candidate_digest = evidence_value.get("digest", evidence_value.get("evidence_digest"))
            if isinstance(candidate_digest, str):
                evidence_digest = candidate_digest
        elif evidence_value in {"tested", "observed", "not-tested", "unknown"}:
            evidence = str(evidence_value)
        support = "unknown" if declared is None else "supported" if declared else "unsupported"
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


def _recipe_capability_facts(document: Mapping[str, object] | None) -> list[CapabilityEvidence]:
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
        evidence_map = {
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

    def __init__(self, model_cache: object | None = None) -> None:
        self._model_cache = model_cache

    def bind_model_cache(self, model_cache: object) -> None:
        """Attach the Controller model-cache authority after startup wiring."""

        self._model_cache = model_cache

    def inspect(
        self,
        session: Session,
        *,
        model_version_sha256: str,
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
            model_version_sha256=model_version_sha256,
            recipe_revision_id=recipe_revision_id,
            node_ids=node_ids,
            retention=retention,
            now=now,
        )

    def _inspect_model_cache(
        self,
        session: Session,
        *,
        model_cache: object,
        model_version_sha256: str,
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

        resolve = getattr(model_cache, "resolve_artifact_set", None)
        preview = getattr(model_cache, "download_preview", None)
        if not callable(resolve) or not callable(preview):
            raise TypeError("model-cache manifest provider is unavailable")
        try:
            manifest = resolve(
                model_version_sha256=model_version_sha256,
                recipe_revision_id=recipe_revision_id,
            )
            preview_value = preview(
                model_version_sha256=model_version_sha256,
                recipe_revision_id=recipe_revision_id,
            )
        except Exception as error:
            raise RuntimeError(f"model-cache exact manifest is unavailable: {error}") from error
        artifact_set_sha256 = _manifest_value(manifest, "digest")
        if artifact_set_sha256 is None:
            artifact_set_sha256 = _manifest_value(preview_value, "artifact_set_sha256")
        artifacts = _manifest_value(manifest, "artifacts")
        if not _is_hex_digest(artifact_set_sha256) or not isinstance(artifacts, Sequence):
            raise RuntimeError("model-cache exact manifest identity is invalid")
        model_digests: list[str] = []
        artifact_bytes = 0
        seen: set[str] = set()
        for item in artifacts:
            digest = _manifest_value(item, "sha256")
            size = _manifest_value(item, "expected_bytes")
            if size is None:
                size = _manifest_value(item, "download_bytes")
            if not _is_hex_digest(digest) or type(size) is not int or size <= 0:
                raise RuntimeError("model-cache artifact identity is invalid")
            if digest not in seen:
                model_digests.append(digest)
                artifact_bytes += size
                seen.add(digest)
        if not model_digests or artifact_bytes < 1:
            raise RuntimeError("model-cache exact manifest has no artifacts")
        manifest_model_digest = _manifest_value(manifest, "model_version_sha256")
        if manifest_model_digest not in (None, model_version_sha256):
            raise RuntimeError("model-cache manifest model identity does not match the request")
        manifest_bytes = _manifest_value(manifest, "expected_bytes")
        if manifest_bytes is not None and manifest_bytes != artifact_bytes:
            raise RuntimeError("model-cache manifest byte total does not match its artifacts")
        missing_nas_bytes = _manifest_value(preview_value, "new_bytes")
        if type(missing_nas_bytes) is not int or missing_nas_bytes < 0:
            raise RuntimeError("model-cache download preview has no bounded byte total")
        blockers: list[RunSwitchReason] = []
        raw_preview_blockers = _manifest_value(preview_value, "blockers")
        if isinstance(raw_preview_blockers, Sequence) and not isinstance(
            raw_preview_blockers, (str, bytes, bytearray)
        ):
            for raw in raw_preview_blockers:
                detail = str(raw).strip()
                if detail:
                    blockers.append(
                        _as_reason(
                            "run-switch.nas-download-blocked",
                            detail,
                            scope="artifact",
                            node_ids=node_ids,
                        )
                    )
        expected_by_digest = {
            digest: _manifest_artifact_size(manifest, digest) for digest in model_digests
        }
        reused = 0
        missing_spark = 0
        reclaimable = 0
        reclaimable_digests: set[str] = set()
        for node_id in node_ids:
            rows = tuple(
                session.scalars(select(NodeArtifact).where(NodeArtifact.node_id == node_id))
            )
            by_digest = {row.digest: row for row in rows}
            for digest, size in expected_by_digest.items():
                row = by_digest.get(digest)
                if row is not None and row.state == "verified" and row.size_bytes == size:
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
        dependency_versions = _manifest_value(manifest, "model_versions")
        raw_dependencies = (
            dependency_versions
            if isinstance(dependency_versions, Sequence)
            and not isinstance(dependency_versions, (str, bytes, bytearray))
            else ()
        )
        dependencies = tuple(
            sorted(
                value
                for value in raw_dependencies
                if isinstance(value, str)
                and value != model_version_sha256
                and _is_hex_digest(value)
            )
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
            dependency_model_version_sha256=dependencies,
        )


class RecipeLifecyclePhaseExecutor:
    """Default executor for phases covered by existing recipe primitives."""

    def __init__(
        self,
        lifecycle: RecipeOperationService,
        sessions: sessionmaker[Session],
        mappings: ClusterMappingService,
        clock: Any,
        artifact_executor: RunSwitchArtifactPhaseExecutor | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._sessions = sessions
        self._mappings = mappings
        self._clock = clock
        self._artifact_executor = artifact_executor

    def _execute_container_build(
        self,
        plan: RunSwitchPlan,
        *,
        actor: str,
        request_key: str,
    ) -> PhaseExecution:
        """Start or replay the existing durable ``recipe.build.v1`` child."""

        build_id = plan.recipe_build_id or plan.build.build_id
        revision_id = plan.recipe_revision_id
        if build_id is None or revision_id is None:
            raise RunSwitchOperationConflict(
                "run-switch.container-build-identity-unavailable"
            )
        with self._sessions() as session:
            build = session.get(RecipeBuild, build_id)
            if build is None or build.recipe_revision_id != revision_id:
                raise RunSwitchOperationConflict(
                    "run-switch.container-build-receipt-unavailable"
                )
            if build.state == "succeeded":
                if (
                    not isinstance(build.image_digest, str)
                    or not _is_oci_digest(build.image_digest)
                    or not _is_hex_digest(build.oci_layout_sha256)
                    or type(build.image_bytes) is not int
                    or build.image_bytes < 1
                ):
                    raise RunSwitchOperationConflict(
                        "run-switch.container-build-evidence-invalid"
                    )
                return PhaseExecution(
                    result={
                        "build_id": build.id,
                        "build_input_sha256": build.build_input_sha256,
                        "image_digest": build.image_digest,
                        "oci_layout_sha256": build.oci_layout_sha256,
                        "image_bytes": build.image_bytes,
                        "state": "succeeded",
                    }
                )
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
                    Job.payload["plan_digest"].as_string()
                    == build.build_input_sha256,
                )
                .order_by(Job.updated_at.desc(), Job.id)
                .limit(1)
            )
            if active is not None:
                return PhaseExecution(
                    active.id,
                    {
                        "build_id": build.id,
                        "build_input_sha256": build.build_input_sha256,
                        "state": build.state,
                    },
                )
            builder_node_id = build.builder_node_id
            build_input_sha256 = build.build_input_sha256
        preview_build = getattr(self._lifecycle, "preview_build", None)
        start_build = getattr(self._lifecycle, "build", None)
        if not callable(preview_build) or not callable(start_build):
            raise RunSwitchOperationConflict(
                "run-switch.container-build-executor-unavailable"
            )
        try:
            build_plan = preview_build(revision_id, builder_node_id)
        except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError) as error:
            raise RunSwitchOperationConflict(
                f"run-switch.container-build-plan-unavailable: {error}"
            ) from error
        if (
            getattr(build_plan, "build_id", None) != build_id
            or getattr(build_plan, "build_input_sha256", None) != build_input_sha256
        ):
            raise RunSwitchOperationConflict(
                "run-switch.container-build-plan-changed"
            )
        child_key = str(uuid.uuid5(uuid.UUID(request_key), "container-build"))
        try:
            value = start_build(
                build_plan,
                build_input_sha256=build_input_sha256,
                actor=actor,
                request_id=child_key,
            )
        except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError) as error:
            raise RunSwitchOperationConflict(
                f"run-switch.container-build-start-unavailable: {error}"
            ) from error
        result = {
            "build_id": build_id,
            "build_input_sha256": build_input_sha256,
            "state": getattr(value, "state", "unknown"),
        }
        if getattr(value, "state", None) == "succeeded":
            with self._sessions() as session:
                completed = session.get(RecipeBuild, build_id)
                if completed is None:
                    raise RunSwitchOperationConflict(
                        "run-switch.container-build-receipt-unavailable"
                    )
                result.update(
                    {
                        "image_digest": completed.image_digest,
                        "oci_layout_sha256": completed.oci_layout_sha256,
                        "image_bytes": completed.image_bytes,
                        "state": completed.state,
                    }
                )
            return PhaseExecution(result=result)
        return PhaseExecution(value.id, result)

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
            child_key = str(uuid.uuid5(uuid.UUID(request_key), f"stop:{target.run_id}"))
            value = self._lifecycle.stop(
                target.run_id,
                plan_digest=target.plan_digest,
                actor=actor,
                request_id=child_key,
            )
            return PhaseExecution(value.id, {"run_id": target.run_id})
        if phase.kind == "prepare" and phase.subphase == "container-build":
            return self._execute_container_build(
                plan,
                actor=actor,
                request_key=request_key,
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
                        mapping_id = _string_or_none(result.get("mapping_id")) or mapping_id
            if mapping_id is None and plan.mapping is not None and plan.mapping.action == "create":
                mapping_plan = ClusterMappingPlan(
                    recipe_revision_id=_required_string(plan.recipe_revision_id),
                    recipe_content_sha256=_required_string(plan.recipe_content_sha256),
                    topology_name=plan.mapping.topology_name,
                    generation=_required_int(plan.mapping.mapping_generation) or 1,
                    parameters=dict(plan.mapping.parameters),
                    nodes=tuple(
                        _mapping_node(node) for node in plan.mapping.nodes
                    ),
                    placement_digest=plan.mapping.placement_digest,
                )
                mapping_id = self._mappings.materialize(
                    mapping_plan, actor=actor, now=_now(self._clock)
                )
            if mapping_id is None:
                return PhaseExecution(result={"prepared": True})
            try:
                install_plan = self._lifecycle.preview_install(
                    mapping_id, plan.recipe_build_id
                )
            except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError) as error:
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
                )
            except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError) as error:
                raise RunSwitchOperationConflict(
                    f"run-switch.install-preparation-failed: {error}"
                ) from error
            compiled = install_plan.compiled_plan_by_node
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
                    "install_plan_digest": install_plan.plan_digest,
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
        if phase.kind == "prepare" and phase.subphase == "runtime-install":
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
            start_installation = getattr(self._lifecycle, "start_installation", None)
            if not callable(start_installation):
                raise RunSwitchOperationConflict(
                    "run-switch.install-executor-unavailable"
                )
            try:
                value = start_installation(
                    installation_id,
                    actor=actor,
                    request_id=str(uuid.uuid5(uuid.UUID(request_key), "runtime-install")),
                )
            except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError) as error:
                raise RunSwitchOperationConflict(
                    f"run-switch.install-start-failed: {error}"
                ) from error
            return PhaseExecution(
                value.id,
                {"installation_id": installation_id},
            )
        if phase.kind == "prepare":
            raise RunSwitchOperationConflict(
                "run-switch.prepare-subphase-unsupported"
            )
        if phase.kind == "start":
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
            low_level = self._lifecycle.preview_run(installation_id, plan.alias)
            value = self._lifecycle.start(
                low_level,
                plan_digest=low_level.plan_digest,
                actor=actor,
                request_id=str(uuid.uuid5(uuid.UUID(request_key), "start")),
            )
            return PhaseExecution(value.id, {"run_id": value.owner_id})
        if phase.kind == "final_verify":
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
            raise RunSwitchOperationConflict(
                "run-switch.final-verification-failed"
            )
        return PhaseExecution()

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
        model_cache: object | None = None,
        inventory_max_age_seconds: int = 300,
        memory_floor_bytes: int = 4_000_000_000,
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
        self._custom_phase_executor = phase_executor is not None
        self._phase_executor = phase_executor or (
            RecipeLifecyclePhaseExecutor(
                lifecycle,
                sessions,
                self._mappings,
                clock,
                artifact_executor=artifact_phase_executor,
            )
            if lifecycle is not None
            else None
        )
        self._inventory_max_age = inventory_max_age_seconds
        self._memory_floor = memory_floor_bytes

    def preview(
        self,
        request: RunSwitchPreviewRequest,
        *,
        actor: str,
    ) -> RunSwitchPlan:
        return self._preview_run(request, actor=actor)

    def preview_run(
        self,
        request: RunSwitchPreviewRequest,
        *,
        actor: str,
    ) -> RunSwitchPlan:
        return self._preview_run(request, actor=actor)

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
            installation = session.get(RecipeInstallation, run.installation_id)
            revision = _active_recipe_revision(
                session, run.plan.get("recipe_revision_id")
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
                installation.model_version_sha256
                if installation is not None
                else _string_or_none(run.plan.get("model_version_sha256"))
            )
            recipe_digest = revision.content_digest if revision is not None else None
            _model_document, _model_documents, model_caps, recipe_caps, _document_blockers = self._resolve_documents(
                session,
                revision,
                model_digest,
                requested_recipe_digest=recipe_digest,
            )
            freshness, fit_current, _fit_blockers, fit_warnings = self._fit(
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
                )
            )
            stop_digest = self._stop_digest(run.id)
            stops = (
                [
                    StopImpact(
                        run_id=run.id,
                        alias=run.alias,
                        state=run.state,
                        node_ids=[node.node_id for node in mapping_nodes],
                        reserved_bytes=self._run_reserved_bytes(session, run.id),
                        plan_digest=stop_digest,
                    )
                ]
                if stop_digest is not None and run.state in _ACTIVE_RUN_STATES
                else []
            )
            # Stopping a live run must remain possible when catalog/cache
            # evidence has aged or is unavailable. Capacity and artifact
            # findings remain diagnostics, but they do not block the stop.
            blockers: list[RunSwitchReason] = []
            warnings = [*fit_warnings, *inspection.warnings]
            if run.state not in _ACTIVE_RUN_STATES:
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
                installation_state=installation.state if installation is not None else None,
                starts=False,
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
                "model_version_sha256": model_digest,
                "recipe_revision_id": revision.id if revision is not None else None,
                "recipe_content_sha256": recipe_digest,
                "alias": run.alias,
                "run_id": run.id,
                "spark_group": group,
                "mapping": self._mapping_selection(mapping, mapping_nodes),
                "installation_id": installation.id if installation is not None else None,
                "installation_state": installation.state if installation is not None else None,
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

    def apply(
        self,
        request: RunSwitchApplyRequest,
        *,
        actor: str,
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
        plan = self.preview(request, actor=actor)
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
        if request.plan_digest is not None and preview.plan_digest != request.plan_digest:
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
        )

    def get(self, operation_id: str) -> RunSwitchOperation:
        with self._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.kind not in _OPERATION_KINDS:
                raise KeyError(operation_id)
            return self._operation_view(job)

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
            raise RunSwitchOperationConflict("run-switch retry request key is invalid") from error
        with self._sessions.begin() as session:
            previous = session.get(Job, operation_id, with_for_update=True)
            if previous is None or previous.kind not in _OPERATION_KINDS:
                raise RunSwitchOperationConflict("run-switch operation is not retryable")
            existing = session.scalar(select(Job).where(Job.request_id == request_key))
            if existing is not None:
                if (
                    existing.kind != previous.kind
                    or existing.payload.get("plan_digest")
                    != previous.payload.get("plan_digest")
                ):
                    raise RunSwitchOperationConflict("run-switch request key was already used")
                return self._operation_view(existing)
            progress = dict(previous.result) if isinstance(previous.result, Mapping) else {}
            raw_retry = previous.payload.get("retry", {})
            retry = dict(raw_retry) if isinstance(raw_retry, Mapping) else {}
            operator_retries = retry.get("operator_retries")
            operator_retries = (
                operator_retries
                if type(operator_retries) is int and operator_retries >= 0
                else 0
            )
            if (
                previous.state != "failed"
                or progress.get("retryable") is not True
                or operator_retries >= _MAX_RETRY_ATTEMPTS
            ):
                raise RunSwitchOperationConflict("run-switch operation is not retryable")
            payload = dict(previous.payload)
            payload["progress"] = progress
            payload["retry_of"] = previous.id
            payload["retry"] = {
                "automatic_attempts": 1,
                "operator_retries": operator_retries + 1,
            }
            progress["child_operation_id"] = None
            progress["retryable"] = False
            now = _now(self._clock)
            job = Job(
                id=str(uuid.uuid4()),
                request_id=request_key,
                kind=previous.kind,
                state="queued",
                actor=actor,
                authority_revision=previous.authority_revision,
                targets=list(previous.targets),
                payload_digest=_digest(payload),
                payload=payload,
                result=progress,
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

    def bind_model_cache(self, model_cache: object) -> None:
        """Bind the authoritative NAS cache after production composition."""

        binder = getattr(self._artifacts, "bind_model_cache", None)
        if not callable(binder):
            raise RunSwitchOperationConflict(
                "run-switch.artifact-inspector-does-not-support-model-cache"
            )
        binder(model_cache)

    def tick(self) -> bool:
        """Advance at most one high-level phase, safely after a restart."""

        with self._sessions() as session:
            job_id = session.scalar(
                select(Job.id)
                .where(Job.kind.in_(_OPERATION_KINDS), Job.state.in_(("queued", "running")))
                .order_by(Job.created_at, Job.id)
                .limit(1)
            )
        if job_id is None:
            return False
        return self._advance(str(job_id))

    def _preview_run(
        self,
        request: RunSwitchPreviewRequest,
        *,
        actor: str,
    ) -> RunSwitchPlan:
        now = _now(self._clock)
        group = request.spark_group
        node_ids = tuple(node.node_id for node in group.nodes)
        with self._sessions() as session:
            revision = _active_recipe_revision(session, request.recipe_revision_id)
            if revision is None:
                raise KeyError(request.recipe_revision_id)
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
                model_ref.get("content_sha256") if isinstance(model_ref, Mapping) else None
            )
            if recipe_model_digest != request.model_version_sha256:
                blockers.append(
                    _as_reason(
                        "run-switch.model_recipe_mismatch",
                        "The selected model variant is not the model pinned by this recipe revision.",
                        scope="model",
                    )
                )
            _model_document, model_documents, model_caps, recipe_caps, document_blockers = self._resolve_documents(
                session,
                revision,
                request.model_version_sha256,
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
            conflicts, stops, conflict_blockers = self._conflicts(
                session,
                node_ids,
                action=request.action,
            )
            blockers.extend(conflict_blockers)
            stopped_run_ids = tuple(stop.run_id for stop in stops)
            freshness, fit_current, current_fit_blockers, current_fit_warnings = self._fit(
                session,
                revision,
                group,
                now=now,
                excluded_run_ids=(),
                effective_settings=effective_settings,
                model_documents=model_documents,
                revision_digest=revision.content_digest if revision is not None else None,
            )
            fit_after_stop = None
            after_fit_blockers: list[RunSwitchReason] = []
            after_fit_warnings: list[RunSwitchReason] = []
            if stops:
                _, fit_after_stop, after_fit_blockers, after_fit_warnings = self._fit(
                    session,
                    revision,
                    group,
                    now=now,
                    excluded_run_ids=stopped_run_ids,
                    effective_settings=effective_settings,
                    model_documents=model_documents,
                    revision_digest=revision.content_digest if revision is not None else None,
                    planned_stops=stops,
                )
                # A current capacity failure caused only by the workload that
                # will be stopped is an ordering decision, not a blocker.  A
                # failure that remains after stop is a real blocker.
                blockers.extend(after_fit_blockers)
                warnings.extend(after_fit_warnings)
            else:
                blockers.extend(current_fit_blockers)
                warnings.extend(current_fit_warnings)
            inspection = self._inspect_artifacts(
                session,
                request.model_version_sha256,
                revision.id,
                group,
                retention=request.retention,
                now=now,
            )
            blockers.extend(inspection.blockers)
            warnings.extend(inspection.warnings)
            if mapping_selection is not None and mapping_selection.action == "create" and self._phase_executor is None:
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
                request.model_version_sha256,
                mapping,
                group,
            )
            build = self._matching_build(session, revision.id, installation)
            build_candidate = build or self._latest_build(session, revision.id)
            build_selection = self._select_build(
                session,
                revision,
                build,
                build_candidate,
                group,
                now=now,
            )
            build = build_selection.build
            build_candidate = build_selection.candidate
            if build_selection.builder_freshness is not None:
                freshness.append(build_selection.builder_freshness)
            blockers.extend(build_selection.blockers)
            build_evidence, runtime_storage, build_blockers, build_warnings = (
                self._build_evidence(
                    session,
                    revision,
                    build,
                    build_candidate,
                    group,
                )
            )
            blockers.extend(build_blockers)
            warnings.extend(build_warnings)
            start_plan_digest: str | None = None
            recipe_build_id = (
                build.id
                if build is not None
                else build_candidate.id
                if build_candidate is not None
                and build_candidate.state in {"planned", "building"}
                else None
            )
            image_digest = (
                build.image_digest
                if build is not None
                else runtime_storage.image_digest
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
            elif installation.state == "installed" and self._lifecycle is not None:
                try:
                    low_level_plan = self._lifecycle.preview_run(
                        installation.id, request.alias
                    )
                except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError) as error:
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
                    for item in low_level_plan.nodes:
                        for reason in item.blockers:
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
                    if not low_level_plan.allowed:
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
            stop_before_prepare = bool(
                stops
                and not fit_current.allowed
                and fit_after_stop is not None
                and fit_after_stop.allowed
                and any(
                    reason.code.startswith("run-switch.insufficient-memory")
                    or reason.code.startswith("run-switch.resource.insufficient")
                    for reason in current_fit_blockers
                )
            )
            stop_before_transfer = bool(
                stops
                and not fit_current.allowed
                and fit_after_stop is not None
                and fit_after_stop.allowed
                and any(
                    reason.code.startswith("run-switch.insufficient-disk")
                    for reason in current_fit_blockers
                )
            )
            phases = self._phases(
                action=request.action,
                group=group,
                installation_id=installation.id if installation is not None else None,
                installation_state=installation.state if installation is not None else None,
                starts=True,
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
                    or (
                        phase.kind == "prepare"
                        and phase.subphase == "runtime-image"
                    )
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
                    installation_id=installation.id if installation is not None else None,
                    installation_state=installation.state if installation is not None else None,
                    starts=True,
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
                "model_version_sha256": request.model_version_sha256,
                "recipe_revision_id": revision.id,
                "recipe_content_sha256": revision.content_digest,
                "alias": request.alias,
                "run_id": None,
                "spark_group": group,
                "mapping": mapping_selection,
                "installation_id": installation.id if installation is not None else None,
                "installation_state": installation.state if installation is not None else None,
                "recipe_build_id": recipe_build_id,
                "image_digest": image_digest,
                "start_plan_digest": start_plan_digest,
                "model_capabilities": model_caps,
                "recipe_capabilities": recipe_caps,
                "freshness": freshness,
                "fit_current": fit_current,
                "fit_after_stop": fit_after_stop,
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
        Mapping[str, object] | None,
        Mapping[tuple[str, str, str], Mapping[str, object]],
        list[CapabilityEvidence],
        list[CapabilityEvidence],
        list[RunSwitchReason],
    ]:
        blockers: list[RunSwitchReason] = []
        model_document: Mapping[str, object] | None = None
        model_documents: dict[tuple[str, str, str], Mapping[str, object]] = {}
        recipe_caps = _recipe_capability_facts(revision.document if revision is not None else None)
        model_caps: list[CapabilityEvidence] = []
        if revision is None:
            return None, {}, [], recipe_caps, blockers
        if requested_recipe_digest is not None and revision.content_digest != requested_recipe_digest:
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
                reference = (
                    getattr(resolved_item, "publisher", None),
                    getattr(resolved_item, "slug", None),
                    getattr(resolved_item, "content_digest", None),
                )
                if all(isinstance(value, str) and value for value in reference):
                    model_documents[reference] = candidate_item
            candidate = getattr(resolved_model, "document", None)
            if isinstance(candidate, Mapping):
                model_document = candidate
            if self._model_capability_summary is not None:
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
            if model_digest is None or getattr(resolved_model, "content_digest", None) != model_digest:
                blockers.append(
                    _as_reason(
                        "run-switch.model_revision_unavailable",
                        "The exact model version selected for this run is not resolved in local catalog authority.",
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
        except (ClusterMappingError, KeyError, RuntimeError, TypeError, ValueError) as error:
            return None, None, [
                _as_reason(
                    "run-switch.mapping_invalid",
                    f"The selected Spark group cannot satisfy the exact recipe topology: {error}",
                    scope="mapping",
                    node_ids=desired_ids,
                )
            ]
        planned = tuple(
            (node.node_id, node.rank, node.role, node.endpoint_owner)
            for node in plan.nodes
        )
        if planned != desired:
            return None, None, [
                _as_reason(
                    "run-switch.mapping_group_mismatch",
                    "The selected Spark ranks and roles do not form the complete topology required by the recipe.",
                    scope="group",
                    node_ids=desired_ids,
                )
            ]
        return None, MappingSelection(
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
        ), []

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
                    RecipeInstallation.model_version_sha256 == model_digest,
                    RecipeInstallation.state.in_(("installed", "installing", "partial")),
                )
                .order_by(RecipeInstallation.state.desc(), RecipeInstallation.updated_at.desc())
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

    @staticmethod
    def _matching_build(
        session: Session,
        revision_id: str,
        installation: RecipeInstallation | None,
    ) -> RecipeBuild | None:
        revision = session.get(CatalogDocumentRevision, revision_id)
        if revision is not None and not _is_source_build(revision.document):
            return None
        if installation is not None:
            build = (
                session.get(RecipeBuild, installation.recipe_build_id)
                if installation.recipe_build_id is not None
                else None
            )
            if (
                build is not None
                and build.recipe_revision_id == revision_id
                and build.state == "succeeded"
                and build.image_digest is not None
                and build.image_bytes is not None
            ):
                return build
        return session.scalar(
            select(RecipeBuild)
            .where(
                RecipeBuild.recipe_revision_id == revision_id,
                RecipeBuild.state == "succeeded",
                RecipeBuild.image_digest.is_not(None),
                RecipeBuild.image_bytes.is_not(None),
            )
            .order_by(RecipeBuild.updated_at.desc(), RecipeBuild.id)
            .limit(1)
        )

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
            freshness, admissible = self._builder_admission(
                session, builder, now=now
            )
            if admissible:
                return _BuildSelection(
                    build=None,
                    candidate=candidate,
                    builder_freshness=freshness,
                )

        preview_build = getattr(self._lifecycle, "preview_build", None)
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
            freshness, admissible = self._builder_admission(
                session, node, now=now
            )
            if not admissible:
                continue
            saw_builder = True
            try:
                proposed = preview_build(revision.id, node.node_id)
            except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError) as error:
                errors.append(f"{node.node_id}: {error}")
                continue
            proposed_id = getattr(proposed, "build_id", None)
            if not isinstance(proposed_id, str):
                errors.append(f"{node.node_id}: build preview returned no build identity")
                continue
            selected = session.get(RecipeBuild, proposed_id)
            if selected is None or selected.recipe_revision_id != revision.id:
                errors.append(f"{node.node_id}: build preview receipt is unavailable")
                continue
            return _BuildSelection(
                build=None,
                candidate=selected,
                builder_freshness=freshness,
            )

        if saw_builder:
            detail = (
                "No compatible Controller builder could prepare the exact recipe source and runtime image."
            )
            if errors:
                detail += " " + errors[-1]
        else:
            detail = (
                "No active linux-arm64 worker has fresh recipe.build.v1 admission evidence."
            )
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
    ) -> tuple[
        RecipeBuildEvidence,
        RuntimeImageStorageImpact,
        list[RunSwitchReason],
        list[RunSwitchReason],
    ]:
        blockers: list[RunSwitchReason] = []
        warnings: list[RunSwitchReason] = []
        document = revision.document if revision is not None else {}
        execution = document.get("execution") if isinstance(document, Mapping) else None
        source_build = _is_source_build(document)
        published_digest = _published_manifest_digest(document)
        direct_receipt = None
        if not source_build and revision is not None and published_digest is not None:
            # Preview has not yet compiled mapping parameters into the
            # effective execution key. Reuse the same immutable published
            # image identity across parameter-only keys; compile/install
            # resolves and persists the exact effective key before planning.
            direct_receipt = session.scalar(
                select(RuntimeImageReceipt)
                .where(
                    RuntimeImageReceipt.recipe_revision_id == revision.id,
                    RuntimeImageReceipt.source == "published",
                    RuntimeImageReceipt.original_content_digest == revision.content_digest,
                    RuntimeImageReceipt.registry_manifest_digest == published_digest,
                    RuntimeImageReceipt.architecture == "linux-arm64",
                    RuntimeImageReceipt.runtime_interface == "vonk.runtime.v1",
                    RuntimeImageReceipt.state == "verified",
                )
                .order_by(RuntimeImageReceipt.verified_at.desc(), RuntimeImageReceipt.id.desc())
                .limit(1)
            )
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
                source_digest
                if _is_hex_digest(source_digest)
                else None
            ),
            detail=(
                None
                if source_row is not None
                else "The verified source bundle is not present in Controller storage."
            ),
        )
        observed_architecture: str | None = None
        if candidate is not None:
            candidate_plan = candidate.plan if isinstance(candidate.plan, Mapping) else {}
            raw_observed = candidate_plan.get("platform")
            if isinstance(raw_observed, str) and raw_observed:
                observed_architecture = raw_observed
            if observed_architecture is None:
                builder = session.get(AgentNode, candidate.builder_node_id)
                if builder is not None and isinstance(builder.architecture, str):
                    observed_architecture = _normalise_architecture(builder.architecture)
        elif direct_receipt is not None:
            observed_architecture = _normalise_architecture(direct_receipt.architecture)
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
        image_digest = (
            build.image_digest
            if build is not None
            else direct_receipt.platform_manifest_digest
            if direct_receipt is not None
            else published_digest
        )
        image_bytes = (
            build.image_bytes
            if build is not None
            else direct_receipt.image_bytes
            if direct_receipt is not None
            else None
        )
        oci_layout = (
            build.oci_layout_sha256
            if build is not None
            else direct_receipt.oci_archive_sha256
            if direct_receipt is not None
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
                if artifact is not None and artifact.state == "verified" and artifact.ref_count == 0:
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
            image_digest=image_digest,
            oci_layout_sha256=oci_layout,
            image_bytes=image_bytes,
            required_bytes=(
                image_bytes * len(group.nodes)
                if image_bytes is not None
                else None
            ),
            reused_bytes=runtime_reused,
            copied_bytes=runtime_missing,
            missing_nas_bytes=None,
            missing_spark_bytes=(runtime_missing if image_bytes is not None else None),
            missing_image_distribution_bytes=(
                runtime_missing if image_bytes is not None else None
            ),
            nas_coverage=("complete" if image_bytes is not None and oci_layout else "unknown"),
            spark_coverage=runtime_coverage,
            reclaimable_bytes=runtime_reclaimable,
            reclaimable_digests=sorted(runtime_reclaimable_digests),
        )
        state = (
            "available"
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
            detail = candidate.error
        elif not source_build and direct_receipt is None:
            detail = "No verified published runtime image receipt is available for this recipe revision."
        if compatibility_state == "incompatible":
            state = "incompatible"
        if require_available:
            if source_build and build is None:
                pending = candidate is not None and candidate.state in {
                    "planned",
                    "building",
                }
                if pending and source_state == "available":
                    warnings.append(
                        _as_reason(
                            "run-switch.container-build-required",
                            "The exact OCI runtime image is pinned to a durable Controller build and will be prepared before target transfer.",
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
            elif not source_build and direct_receipt is None:
                warnings.append(
                    _as_reason(
                        "run-switch.runtime-image-preparation-required",
                        detail or "The pinned published image will be pulled and verified before install admission.",
                        scope="operation",
                        severity="warning",
                        node_ids=[node.node_id for node in group.nodes],
                    )
                )
            if compatibility_state == "incompatible":
                blockers.append(
                    _as_reason(
                        "run-switch.recipe-build-incompatible",
                        compatibility.detail or "Runtime image architecture is incompatible with the selected group.",
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
            RecipeBuildEvidence(
                state=state,
                build_id=build.id if build is not None else candidate.id if candidate is not None else None,
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

        if revision is None or (
            build is None and runtime_storage.image_digest is None
        ):
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
            if model_controller_ready and all(target.state == "ready" for target in model_targets)
            else "incomplete"
        )
        model = ModelArtifactPreparation(
            artifact_set_sha256=artifact_set_digest,
            model_version_sha256=primary_model_digest,
            recipe_revision_sha256=revision.content_digest,
            artifact_count=max(1, len(model_digests)),
            artifact_set_bytes=artifact_set_bytes,
            dependency_model_version_sha256=sorted(
                set(inspection.dependency_model_version_sha256)
            ),
            completeness=model_completeness,
            controller=model_controller,
            targets=model_targets,
        )
        image_digest = build.image_digest if build is not None else runtime_storage.image_digest
        image_bytes = build.image_bytes if build is not None else runtime_storage.image_bytes
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
            state="ready",
            expected_bytes=image_bytes,
            verified_bytes=image_bytes,
            missing_bytes=0,
            verified_sha256=layout_digest,
            verified_at=now,
            source=("controller-build" if build is not None else "published"),
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
                        - _per_target_bytes(
                            runtime_storage.missing_image_distribution_bytes,
                            target_count,
                        )
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
            runtime_interface=_runtime_interface(revision.document),
            build_id=build.id if build is not None else None,
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
        controller_ready = (
            model.completeness == "complete"
            and model.controller.state == "ready"
            and runtime.controller.state == "ready"
        )
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
                .join(RunNode, RunNode.run_id == RecipeRun.id)
                .where(
                    RunNode.node_id.in_(node_ids),
                    RecipeRun.state.in_(_ACTIVE_RUN_STATES),
                )
                .distinct()
                .order_by(RecipeRun.created_at, RecipeRun.id)
            )
        )
        wanted = set(node_ids)
        for run in rows:
            run_nodes = tuple(
                session.scalars(
                    select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
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
                stop_plan = self._lifecycle.preview_stop(run.id) if self._lifecycle else None
            except (KeyError, RecipeOperationConflict, RuntimeError, TypeError, ValueError):
                stop_plan = None
            if stop_plan is None:
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
                    alias=run.alias,
                    state=run.state,
                    node_ids=sorted(run_ids),
                    reserved_bytes=self._run_reserved_bytes(session, run.id),
                    plan_digest=stop_plan.plan_digest,
                )
            )
        return conflicts, stops, blockers

    def _fit(
        self,
        session: Session,
        revision: CatalogDocumentRevision | None,
        group: SparkGroup,
        *,
        now: datetime,
        excluded_run_ids: Sequence[str],
        effective_settings: object | None = None,
        model_documents: Mapping[tuple[str, str, str], Mapping[str, object]] | None = None,
        revision_digest: str | None = None,
        planned_stops: Sequence[StopImpact] = (),
    ) -> tuple[list[FreshnessEvidence], SparkFit, list[RunSwitchReason], list[RunSwitchReason]]:
        freshness: list[FreshnessEvidence] = []
        nodes: list[SparkFitNode] = []
        blockers: list[RunSwitchReason] = []
        warnings: list[RunSwitchReason] = []
        topology = revision.document.get("topology") if revision is not None else None
        roles = topology.get("roles") if isinstance(topology, Mapping) else None
        role_by_name = {
            str(role.get("name")): role
            for role in roles
            if isinstance(role, Mapping) and isinstance(role.get("name"), str)
        } if isinstance(roles, list) else {}
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
            memory_available: int | None = None
            memory_free_after: int | None = None
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
                startup = _required_int(memory.get("startup_peak_bytes"))
                steady = _required_int(memory.get("steady_state_bytes"))
                growth = _required_int(memory.get("runtime_growth_bytes"))
                reserve = _required_int(memory.get("system_reserve_bytes"))
                if startup is None or steady is None or growth is None or reserve is None:
                    node_blockers.append(
                        _as_reason(
                            "run-switch.memory-envelope-invalid",
                            "The recipe memory envelope is incomplete.",
                            scope="recipe",
                            node_ids=(item.node_id,),
                        )
                    )
                else:
                    required_memory = max(startup, steady + growth)
                    memory_kind = memory.get("kind")
                    if snapshot is not None:
                        memory_available = (
                            min(snapshot.host_memory_free_bytes, snapshot.gpu_memory_free_bytes)
                            if memory_kind == "unified"
                            else snapshot.host_memory_free_bytes
                            if memory_kind == "host"
                            else snapshot.gpu_memory_free_bytes
                            if memory_kind == "accelerator"
                            else None
                        )
                    if memory_available is None:
                        node_blockers.append(
                            _as_reason(
                                "run-switch.memory-inventory-unknown",
                                "The selected memory kind has no current Spark inventory.",
                                scope="freshness",
                                node_ids=(item.node_id,),
                            )
                        )
                    else:
                        reservation_kind = {
                            "unified": "unified-memory",
                            "host": "host-memory",
                            "accelerator": "gpu-memory",
                        }.get(str(memory_kind))
                        reserved = self._active_reservation_bytes(
                            session,
                            item.node_id,
                            reservation_kind,
                            set(),
                        )
                        evidence = _resource_evidence(
                            revision.document if revision is not None else {},
                            memory,
                            item.role,
                            model_documents,
                            required_memory,
                            effective_settings,
                        )
                        if effective_settings is not None:
                            demand = resource_demand(
                                effective_settings,
                                evidence,
                                node_id=item.node_id,
                            )
                            node_blockers.extend(
                                _resource_reason(reason, node_ids=(item.node_id,))
                                for reason in demand.reasons
                            )
                        else:
                            demand = ResourceDemand(
                                None, None, None, None, None, None, "unknown", ()
                            )
                        releases = _planned_stop_releases(
                            session,
                            item.node_id,
                            reservation_kind,
                            planned_stops,
                        )
                        total_memory = (
                            snapshot.host_memory_total_bytes
                            if memory_kind == "host"
                            else snapshot.gpu_memory_total_bytes
                            if memory_kind == "accelerator"
                            else min(
                                snapshot.host_memory_total_bytes,
                                snapshot.gpu_memory_total_bytes,
                            )
                        ) if snapshot is not None else None
                        occupied_memory = (
                            snapshot.host_memory_total_bytes - snapshot.host_memory_free_bytes
                            if memory_kind == "host"
                            else snapshot.gpu_memory_total_bytes - snapshot.gpu_memory_free_bytes
                            if memory_kind == "accelerator"
                            else max(
                                snapshot.host_memory_total_bytes - snapshot.host_memory_free_bytes,
                                snapshot.gpu_memory_total_bytes - snapshot.gpu_memory_free_bytes,
                            )
                        ) if snapshot is not None else None
                        if demand is not None:
                            fit_capacity = plan_capacity(
                                {item.node_id: demand},
                                [CapacitySnapshot(
                                    item.node_id,
                                    str(memory_kind),
                                    total_memory,
                                    occupied_memory,
                                    reserved,
                                    "fresh" if snapshot is not None and evidence.evidence_state != "unknown" else "unknown",
                                    snapshot.evidence_digest if snapshot is not None else None,
                                )],
                                releases,
                                memory_floor_bytes=max(self._memory_floor, reserve),
                            )
                            fit_node = fit_capacity.nodes[0]
                            memory_free_after = fit_node.selected_free_after_bytes
                            node_blockers.extend(
                                _resource_reason(reason, node_ids=(item.node_id,))
                                for reason in fit_node.reasons
                                if reason.code.startswith("resource.insufficient")
                            )
                disk_parts = [
                    _required_int(disk.get(name))
                    for name in (
                        "image_bytes",
                        "artifact_bytes",
                        "staging_bytes",
                        "cache_bytes",
                        "safety_margin_bytes",
                    )
                ]
                if any(value is None for value in disk_parts):
                    node_blockers.append(
                        _as_reason(
                            "run-switch.disk-envelope-invalid",
                            "The recipe disk envelope is incomplete.",
                            scope="recipe",
                            node_ids=(item.node_id,),
                        )
                    )
                else:
                    required_disk = sum(value for value in disk_parts if value is not None)
                    disk_free = snapshot.disk_free_bytes if snapshot is not None else None
                    if disk_free is not None:
                        reserved_disk = self._active_reservation_bytes(
                            session, item.node_id, "disk", excluded
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
                    disk_required_bytes=required_disk,
                    disk_free_bytes=disk_free,
                    disk_free_after_bytes=disk_free_after,
                    memory_required_bytes=required_memory,
                    memory_available_bytes=memory_available,
                    memory_free_after_bytes=memory_free_after,
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
        return freshness, SparkFit(
            allowed=not blockers,
            nodes=nodes,
            blockers=blockers,
            warnings=warnings,
        ), blockers, warnings

    def _active_reservation_bytes(
        self,
        session: Session,
        node_id: str,
        kind: str | None,
        excluded_run_ids: set[str],
    ) -> int:
        if kind is None:
            return 0
        reservations = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.node_id == node_id,
                    ResourceReservation.kind == kind,
                    ResourceReservation.state == "active",
                )
            )
        )
        total = 0
        for reservation in reservations:
            if reservation.owner_kind == "run" and reservation.owner_id in excluded_run_ids:
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
                model_version_sha256=model_digest,
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
        if inspection.missing_spark_bytes not in (None, 0) and inspection.nas_coverage == "unknown":
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
                dependency_model_version_sha256=inspection.dependency_model_version_sha256,
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
    def _storage(inspection: ArtifactInspection, *, retention: str) -> ArtifactStorageImpact:
        return ArtifactStorageImpact(
            required_bytes=inspection.required_bytes,
            reused_bytes=inspection.reused_bytes,
            copied_bytes=inspection.copied_bytes,
            missing_nas_bytes=inspection.missing_nas_bytes,
            missing_spark_bytes=inspection.missing_spark_bytes,
            reclaimable_bytes=inspection.reclaimable_bytes,
            reclaimed_bytes=(inspection.reclaimable_bytes if retention == "reclaim-unreferenced" else 0),
            nas_coverage=inspection.nas_coverage,
            spark_coverage=inspection.spark_coverage,
            retention=retention,
            artifact_digests=list(inspection.artifact_digests),
            reclaimable_digests=list(inspection.reclaimable_digests),
        )

    def _phases(
        self,
        *,
        action: str,
        group: SparkGroup,
        installation_id: str | None,
        installation_state: str | None,
        starts: bool,
        stops: Sequence[StopImpact],
        inspection: ArtifactInspection,
        runtime_storage: RuntimeImageStorageImpact | None,
        retention: str,
        blockers: Sequence[RunSwitchReason],
        stop_before_transfer: bool,
        stop_before_prepare: bool,
        build_required: bool = False,
        build_on_target: bool = False,
    ) -> list[RunSwitchPhase]:
        node_ids = [node.node_id for node in group.nodes]
        phases: list[RunSwitchPhase] = []
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
            build_required
            or installation_id is None
            or installation_state != "installed"
        )
        needs_cleanup = retention == "reclaim-unreferenced" and (
            inspection.reclaimable_bytes > 0
            or (
                runtime_storage is not None and runtime_storage.reclaimable_bytes > 0
            )
        )
        stop_added = False

        def add(
            kind: RunSwitchPhaseKind,
            detail: str,
            *,
            subphase: str | None = None,
        ) -> None:
            phases.append(
                RunSwitchPhase(
                    index=len(phases),
                    kind=kind,
                    subphase=subphase,
                    state="blocked" if blockers and kind in {"prepare", "start", "final_verify"} else "planned",
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
            add("stop", "Stop conflicting workloads before memory-consuming runtime preparation.")
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
            add("cleanup", "Reclaim only unreferenced Spark-local artifacts under the selected retention policy.")
        if stops and not stop_added:
            add("stop", "Stop conflicting workloads as one complete group before start.")
        if starts:
            add("start", "Start the selected exact model and recipe outcome.")
            add("final_verify", "Verify the intended final observed state for every affected rank.")
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
            parameters=dict(mapping.parameters),
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
                select(func.coalesce(func.sum(ResourceReservation.amount_bytes), 0)).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                    ResourceReservation.state == "active",
                )
            )
            or 0
        )

    @staticmethod
    def _finalize_plan(data: Mapping[str, object]) -> RunSwitchPlan:
        plan = RunSwitchPlan(**data)
        identity = _plan_identity(plan.model_dump(mode="json"))
        return plan.model_copy(update={"plan_digest": _digest(identity)})

    def _apply_plan(
        self,
        plan: RunSwitchPlan,
        *,
        request_key: str,
        actor: str,
        kind: str,
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
                "phase_index": 0,
                "item_index": 0,
                "phase": (
                    plan.phases[0].kind if plan.phases else "final_verify"
                ),
                "subphase": (
                    plan.phases[0].subphase if plan.phases else None
                ),
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
                result=dict(payload["progress"]),
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            return self._operation_view(job)

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
            job = session.get(Job, operation_id)
            if job is None or job.kind not in _OPERATION_KINDS:
                return True
            if job.state not in {"queued", "running"}:
                return False
            payload = job.payload
            raw_plan = payload.get("plan")
            if not isinstance(raw_plan, Mapping):
                job.state = "failed"
                job.status_reason = "run-switch persisted plan is invalid"
                job.updated_at = now
                session.commit()
                return True
            plan = _load_plan(raw_plan)
            progress = dict(job.result) if isinstance(job.result, Mapping) else {}
            phase_index = progress.get("phase_index", 0)
            item_index = progress.get("item_index", 0)
            child_id = progress.get("child_operation_id")
            if type(phase_index) is not int or type(item_index) is not int or phase_index < 0 or item_index < 0:
                job.state = "failed"
                job.status_reason = "run-switch persisted progress is invalid"
                job.updated_at = now
                session.commit()
                return True
            if phase_index >= len(plan.phases):
                job.state = "succeeded"
                progress = _complete_operation_progress(plan, progress)
                job.result = progress
                job.updated_at = now
                session.commit()
                return True
        if child_id is not None:
            if not isinstance(child_id, str):
                self._fail(operation_id, "run-switch child operation identity is invalid")
                return True
            try:
                child = self._get_child_operation(child_id)
            except KeyError:
                self._fail(operation_id, "run-switch child operation disappeared")
                return True
            if child is None:
                self._fail(operation_id, "run-switch child operation disappeared")
                return True
            if child.state in {"queued", "running"}:
                child_progress = _child_progress_payload(child)
                with self._sessions.begin() as session:
                    job = session.get(Job, operation_id, with_for_update=True)
                    if job is None:
                        return False
                    progress = dict(job.result) if isinstance(job.result, Mapping) else {}
                    persisted_plan = _load_plan(job.payload["plan"])
                    persisted_phase_index = int(progress.get("phase_index", phase_index))
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
                    )
                    progress["phase"] = persisted_phase.kind
                    progress["subphase"] = persisted_phase.subphase
                    job.state = "running"
                    job.result = progress
                    job.updated_at = now
                return True
            if child.state not in _TERMINAL_STATES or child.state != "succeeded":
                reason = f"run-switch phase operation failed: {child.state if child else 'unknown'}"
                if _transient_distribution_failure(child) and self._queue_transient_retry(
                    operation_id, child, phase_index=phase_index
                ):
                    return True
                self._fail(
                    operation_id,
                    reason,
                    retryable=_transient_distribution_failure(child),
                )
                return True
            with self._sessions.begin() as session:
                job = session.get(Job, operation_id, with_for_update=True)
                if job is None:
                    return False
                progress = dict(job.result) if isinstance(job.result, Mapping) else {}
                phase_index = int(progress.get("phase_index", 0))
                item_index = int(progress.get("item_index", 0)) + 1
                persisted_plan = _load_plan(job.payload["plan"])
                phase = persisted_plan.phases[phase_index]
                _merge_progress_evidence(
                    progress,
                    persisted_plan,
                    phase,
                    _child_progress_payload(child),
                )
                # Preserve terminal child receipts for the following verify
                # phase. Byte/member projection alone cannot prove every
                # model object and the imported OCI identity reached the
                # target; the receipts are the durable handoff across a
                # restart.
                child_result = _progress_mapping(getattr(child, "result", None))
                child_receipts = (
                    child_result.get("evidence")
                    if child_result is not None
                    else None
                )
                if isinstance(child_receipts, list):
                    results = list(progress.get("phase_results", []))
                    results.extend(
                        dict(receipt)
                        for receipt in child_receipts
                        if isinstance(receipt, Mapping)
                    )
                    progress["phase_results"] = results
                if phase.subphase == "container-build":
                    try:
                        receipt = _build_receipt_in_session(
                            session,
                            persisted_plan,
                        )
                    except RunSwitchOperationConflict as error:
                        job.state = "failed"
                        job.status_reason = str(error)[:512]
                        progress["failed_phase"] = phase.kind
                        job.result = progress
                        job.updated_at = now
                        return True
                    results = list(progress.get("phase_results", []))
                    if not any(
                        isinstance(item, Mapping)
                        and item.get("build_id") == receipt["build_id"]
                        and item.get("image_digest") == receipt["image_digest"]
                        for item in results
                    ):
                        results.append(receipt)
                        progress["phase_results"] = results
                if phase.kind in {"transfer", "verify", "cleanup"} or (
                    phase.kind == "prepare" and phase.subphase == "runtime-image"
                ):
                    try:
                        _validate_artifact_execution(
                            persisted_plan,
                            phase,
                            getattr(child, "result", None),
                        )
                    except RunSwitchOperationConflict as error:
                        self._fail(operation_id, str(error))
                        return True
                item_total = len(persisted_plan.stops) if phase.kind == "stop" else 1
                progress["child_operation_id"] = None
                if item_index >= item_total:
                    completed = list(progress.get("completed_phases", []))
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
                job.result = progress
                job.updated_at = now
            return True
        with self._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None:
                return False
            plan = _load_plan(job.payload["plan"])
            progress = dict(job.result) if isinstance(job.result, Mapping) else {}
            phase_index = int(progress.get("phase_index", 0))
            item_index = int(progress.get("item_index", 0))
            if phase_index >= len(plan.phases):
                return True
            phase = plan.phases[phase_index]
            actor = job.actor
            request_key = job.request_id
        if phase.state in {"skipped", "retained"}:
            execution = PhaseExecution()
        elif phase.state == "blocked":
            self._fail(operation_id, f"run-switch phase blocked: {phase.kind}")
            return True
        elif self._phase_executor is None:
            self._fail(operation_id, f"run-switch phase executor unavailable: {phase.kind}")
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
            except RunSwitchOperationConflict as error:
                self._fail(operation_id, str(error))
                return True
            except (OSError, httpx.HTTPError, RuntimeError, TypeError, ValueError, KeyError) as error:
                self._fail(
                    operation_id,
                    f"{type(error).__name__}: {error}",
                    retryable=_transient_distribution_exception(error),
                )
                return True
        if execution.waiting and execution.operation_id is None:
            self._fail(operation_id, f"run-switch.{phase.kind}-waiting-without-child")
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
                _validate_artifact_execution(plan, phase, execution.result)
            except RunSwitchOperationConflict as error:
                self._fail(operation_id, str(error))
                return True
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return False
            progress = dict(job.result) if isinstance(job.result, Mapping) else {}
            _merge_progress_evidence(
                progress,
                plan,
                phase,
                execution.result,
            )
            if execution.waiting:
                progress["phase"] = phase.kind
                progress["subphase"] = phase.subphase
                if execution.result is not None:
                    results = list(progress.get("phase_results", []))
                    results.append(dict(execution.result))
                    progress["phase_results"] = results
            elif execution.operation_id is not None:
                progress["child_operation_id"] = execution.operation_id
                progress["phase"] = phase.kind
                progress["subphase"] = phase.subphase
                if execution.result is not None:
                    results = list(progress.get("phase_results", []))
                    results.append(dict(execution.result))
                    progress["phase_results"] = results
            else:
                if execution.result is not None:
                    results = list(progress.get("phase_results", []))
                    results.append(dict(execution.result))
                    progress["phase_results"] = results
                if phase.subphase == "container-build":
                    try:
                        receipt = _build_receipt_in_session(session, plan)
                    except RunSwitchOperationConflict as error:
                        job.state = "failed"
                        job.status_reason = str(error)[:512]
                        progress["failed_phase"] = phase.kind
                        job.result = progress
                        job.updated_at = now
                        return True
                    results = list(progress.get("phase_results", []))
                    if not any(
                        isinstance(item, Mapping)
                        and item.get("build_id") == receipt["build_id"]
                        and item.get("image_digest") == receipt["image_digest"]
                        for item in results
                    ):
                        results.append(receipt)
                        progress["phase_results"] = results
                completed = list(progress.get("completed_phases", []))
                completed.append(phase.kind)
                progress["completed_phases"] = completed
                _complete_phase_progress(progress, plan, phase)
                progress["phase_index"] = int(progress.get("phase_index", 0)) + 1
                progress["item_index"] = 0
                next_index = int(progress["phase_index"])
                progress["phase"] = (
                    plan.phases[next_index].kind if next_index < len(plan.phases) else "final_verify"
                )
                progress["subphase"] = (
                    plan.phases[next_index].subphase
                    if next_index < len(plan.phases)
                    else None
                )
            job.state = "running"
            job.result = progress
            job.updated_at = now
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

    def _queue_transient_retry(
        self,
        operation_id: str,
        child: object,
        *,
        phase_index: int,
    ) -> bool:
        """Requeue one parent attempt while preserving child progress receipts."""

        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return False
            attempt = max(1, int(job.current_attempt or 0))
            progress = dict(job.result) if isinstance(job.result, Mapping) else {}
            plan = _load_plan(job.payload["plan"])
            raw_retry = job.payload.get("retry", {})
            retry = dict(raw_retry) if isinstance(raw_retry, Mapping) else {}
            automatic_attempts = retry.get("automatic_attempts")
            automatic_attempts = (
                automatic_attempts
                if type(automatic_attempts) is int and automatic_attempts >= 1
                else attempt
            )
            if phase_index >= len(plan.phases) or automatic_attempts >= _MAX_RETRY_ATTEMPTS:
                return False
            phase = plan.phases[phase_index]
            _merge_progress_evidence(progress, plan, phase, _child_progress_payload(child))
            progress["child_operation_id"] = None
            progress["retryable"] = True
            progress["retry_attempt"] = attempt + 1
            progress["retry_reason"] = "transient distribution failure"
            retry["automatic_attempts"] = automatic_attempts + 1
            job.payload = dict(job.payload) | {"retry": retry}
            job.current_attempt = attempt + 1
            job.state = "queued"
            job.status_reason = None
            job.result = progress
            job.updated_at = _now(self._clock)
            return True

    def _fail(self, operation_id: str, reason: str, *, retryable: bool = False) -> None:
        with self._sessions.begin() as session:
            job = session.get(Job, operation_id, with_for_update=True)
            if job is None:
                return
            job.state = "failed"
            job.status_reason = reason[:512]
            progress = dict(job.result) if isinstance(job.result, Mapping) else {}
            progress["failed_phase"] = progress.get("phase")
            progress["retryable"] = retryable
            job.result = progress
            job.updated_at = _now(self._clock)

    @staticmethod
    def _operation_view(job: Job) -> RunSwitchOperation:
        progress = dict(job.result) if isinstance(job.result, Mapping) else {}
        # The durable progress payload contains member counters, while the
        # Job row owns the authoritative target membership.  Keep the
        # projection valid even when a plan is unavailable during restart or
        # recovery and the raw payload has no copied node_ids field.
        progress["node_ids"] = [
            str(node_id) for node_id in job.targets if isinstance(node_id, str)
        ]
        raw_plan = job.payload.get("plan")
        plan = _load_plan(raw_plan) if isinstance(raw_plan, Mapping) else None
        action = plan.action if plan is not None else str(job.payload.get("action", "run"))
        current = progress.get("phase")
        current_phase = current if current in _PHASES else None
        completed = [value for value in progress.get("completed_phases", []) if value in _PHASES]
        return RunSwitchOperation(
            operation_id=job.id,
            kind=job.kind,
            action=action,
            state=job.state,
            plan_digest=str(job.payload.get("plan_digest", "0" * 64)),
            request_key=job.request_id,
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
            result=dict(job.result) if isinstance(job.result, Mapping) else None,
        )


@dataclass(frozen=True, slots=True)
class ActivityOperationListPage:
    """Fallback page value used before the global Activity seam is imported."""

    items: tuple[Mapping[str, object], ...]
    next_cursor: str | None
    total: int


class RunSwitchOperationProvider:
    """Project high-level jobs into the global Activity provider contract."""

    family = "run-switch"

    def __init__(self, service: RunSwitchOperationService) -> None:
        self._service = service

    def list_operations(self, query: object) -> object:
        limit = getattr(query, "limit", None)
        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError("operation provider page limit is invalid")
        state = getattr(query, "state", None)
        if state is not None and not isinstance(state, str):
            raise ValueError("operation provider state filter is invalid")
        node_id = getattr(query, "node_id", None)
        if node_id is not None and not isinstance(node_id, str):
            raise ValueError("operation provider node filter is invalid")
        after = getattr(query, "after", None)
        with self._service._sessions() as session:
            statement = select(Job).where(Job.kind.in_(_OPERATION_KINDS))
            if state is not None:
                statement = statement.where(Job.state == state)
            jobs = list(
                session.scalars(
                    statement.order_by(Job.created_at.desc(), Job.id.desc())
                )
            )
        if node_id is not None:
            jobs = [job for job in jobs if node_id in job.targets]
        total = len(jobs)
        if after is not None:
            if (
                not isinstance(after, tuple)
                or len(after) != 2
                or not isinstance(after[0], datetime)
                or not isinstance(after[1], str)
            ):
                raise ValueError("operation provider cursor is invalid")
            boundary = (_aware(after[0]), after[1])
            jobs = [
                job
                for job in jobs
                if (_aware(job.created_at), str(job.id)) < boundary
            ]
        items = tuple(self._item(job) for job in jobs[:limit])
        return _activity_page(items, None, total)

    def get_operation(self, operation_id: str) -> Mapping[str, object]:
        with self._service._sessions() as session:
            job = session.get(Job, operation_id)
            if job is None or job.kind not in _OPERATION_KINDS:
                raise KeyError(operation_id)
            return self._item(job)

    def _item(self, job: Job) -> Mapping[str, object]:
        operation = self._service._operation_view(job)
        node_ids = [str(node_id) for node_id in job.targets]
        endpoint_node = node_ids[0]
        raw_plan = job.payload.get("plan") if isinstance(job.payload, Mapping) else None
        if isinstance(raw_plan, Mapping):
            raw_group = raw_plan.get("spark_group")
            raw_nodes = raw_group.get("nodes") if isinstance(raw_group, Mapping) else None
            if isinstance(raw_nodes, list):
                endpoint_node = next(
                    (
                        str(node.get("node_id"))
                        for node in raw_nodes
                        if isinstance(node, Mapping) and node.get("endpoint_owner") is True
                    ),
                    endpoint_node,
                )
        return {
            "id": operation.operation_id,
            "job_id": operation.operation_id,
            "parent_id": None,
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
                and isinstance(operation.result, Mapping)
                and operation.result.get("retryable") is True
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
    return progress


def _activity_result(operation: RunSwitchOperation) -> dict[str, object] | None:
    """Keep family result data and add bounded generic failure evidence."""

    result = dict(operation.result) if operation.result is not None else {}
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


def _activity_page(
    items: tuple[Mapping[str, object], ...],
    next_cursor: str | None,
    total: int,
) -> object:
    """Construct whichever shared page DTO is available in the host branch."""

    try:
        from .operation_api import OperationListPage
    except ImportError:
        return ActivityOperationListPage(items, next_cursor, total)
    return OperationListPage(items, next_cursor, total)


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
            result
            for result in reversed(raw_results)
            if isinstance(result, Mapping)
        )
    for result in candidates:
        if expected_build_id is not None and result.get("build_id") != expected_build_id:
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


def _build_receipt_in_session(
    session: Session,
    plan: RunSwitchPlan,
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
    return {
        "build_id": build.id,
        "build_input_sha256": build.build_input_sha256,
        "image_digest": build.image_digest,
        "oci_layout_sha256": build.oci_layout_sha256,
        "image_bytes": build.image_bytes,
        "state": "succeeded",
    }


def _progress_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _progress_state(value: object) -> str | None:
    return value if value in {"pending", "running", "succeeded", "failed", "unknown"} else None


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


def _transient_distribution_failure(child: object) -> bool:
    """Retry transport uncertainty while keeping receipt failures terminal."""

    payload = _child_progress_payload(child)
    if getattr(child, "state", None) in {"waiting-for-operator", "uncertain"}:
        return True
    raw_uncertain = payload.get("uncertain")
    text = " ".join(
        str(payload.get(key, ""))
        for key in ("error_code", "code", "reason", "detail", "summary", "error")
    ).casefold()
    if any(marker in text for marker in _TERMINAL_RETRY_MARKERS):
        return False
    if raw_uncertain is True:
        return True
    return any(
        marker in text
        for marker in (
            "http",
            "timeout",
            "timed out",
            "connection",
            "network",
            "transport",
            "copy",
            "unavailable",
            "temporary",
            "oserror",
        )
    )


def _transient_distribution_exception(error: BaseException) -> bool:
    text = str(error).casefold()
    if any(
        marker in text
        for marker in _TERMINAL_RETRY_MARKERS
    ):
        return False
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


def _merge_progress_evidence(
    progress: dict[str, object],
    plan: RunSwitchPlan,
    phase: RunSwitchPhase,
    evidence: object,
) -> None:
    """Persist bounded child byte/member evidence for restart-safe polling."""

    payload = _progress_mapping(evidence)
    if payload is None:
        return
    nested = _progress_mapping(payload.get("progress"))
    if nested is not None:
        _merge_progress_evidence(progress, plan, phase, nested)

    current_completed = _progress_int(progress.get("completed_bytes")) or 0
    reported_completed = next(
        (
            _progress_int(payload.get(key))
            for key in (
                "completed_bytes",
                "bytes_done",
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
        progress["completed_bytes"] = max(current_completed, offset + reported_completed)
    reported_total = next(
        (
            _progress_int(payload.get(key))
            for key in ("total_bytes", "bytes_total")
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
        if isinstance(item.get("node_id"), str)
        and item.get("node_id") in known_nodes
    }
    for item in entries:
        node_id = item.get("node_id")
        if not isinstance(node_id, str) or node_id not in known_nodes:
            continue
        target = existing.setdefault(node_id, {"node_id": node_id})
        completed = _progress_int(item.get("completed_bytes"))
        if completed is None:
            completed = _progress_int(item.get("bytes_done"))
        if completed is not None:
            target["completed_bytes"] = max(
                _progress_int(target.get("completed_bytes")) or 0,
                completed,
            )
        total = _progress_int(item.get("total_bytes"))
        if total is None:
            total = _progress_int(item.get("bytes_total"))
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
    return progress


def _progress_view(
    plan: RunSwitchPlan | None,
    raw: Mapping[str, object],
    operation_state: str,
    status_reason: str | None,
) -> RunSwitchProgress:
    """Build the stable operation progress DTO from durable JSON state."""

    if plan is None:
        node_ids = [str(value) for value in raw.get("node_ids", []) if isinstance(value, str)]
        phase_count = 1
        phase_index = 0
        phase = None
        subphase = None
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
        subphase = raw.get("subphase")
        if subphase not in {
            "container-build",
            "model-download",
            "runtime-image",
            "runtime-plan",
            "target-copy",
            "runtime-install",
        }:
            subphase = (
                plan.phases[phase_index].subphase
                if phase_index < len(plan.phases)
                else None
            )
        total, member_totals = _planned_transfer_bytes(plan)
        if total is None:
            candidate_total = _progress_int(raw.get("total_bytes"))
            total = candidate_total

    state = operation_state if operation_state in {"queued", "running", "succeeded", "failed"} else "unknown"
    if state == "succeeded":
        phase = "final_verify"
        subphase = None
        phase_index = min(max(phase_index, phase_count - 1), 31)
    completed = _progress_int(raw.get("completed_bytes")) or 0
    if state == "succeeded" and total is not None:
        completed = total
    elif plan is not None and total is not None:
        completed_phases = raw.get("completed_phases")
        if isinstance(completed_phases, Sequence) and "transfer" in completed_phases:
            completed = total
        completed = min(completed, total)
    raw_members = {
        str(item.get("node_id")): item
        for item in _progress_member_entries(raw.get("members", raw.get("member_progress")))
        if isinstance(item.get("node_id"), str)
        and item.get("node_id") in set(node_ids)
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
        member_completed = min(member_completed, member_total) if member_total is not None else member_completed
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
        error = item.get("error") if isinstance(item.get("error"), str) else None
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
    return RunSwitchProgress(
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
) -> None:
    """Enforce evidence required from an injected artifact phase adapter."""

    if not isinstance(result, Mapping):
        raise RunSwitchOperationConflict(
            f"run-switch.{phase.kind}-returned-invalid-evidence"
        )
    if phase.kind == "prepare" and phase.subphase == "runtime-image":
        raw_receipt = result.get("runtime_image")
        receipt = raw_receipt if isinstance(raw_receipt, Mapping) else result
        image_digest = receipt.get("image_digest")
        layout_digest = receipt.get("oci_layout_sha256", receipt.get("oci_archive_sha256"))
        image_bytes = receipt.get("image_bytes")
        if (
            not _is_oci_digest(image_digest)
            or not _is_hex_digest(layout_digest)
            or type(image_bytes) is not int
            or image_bytes < 1
        ):
            raise RunSwitchOperationConflict(
                "run-switch.runtime-image-preparation-evidence-invalid"
            )
        registry_digest = receipt.get("registry_manifest_digest")
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
            else None
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
            else None
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
            if expected_layout is not None and result.get("verified_oci_layout_sha256") != expected_layout:
                raise RunSwitchOperationConflict(
                    "run-switch.runtime-layout-verification-mismatch"
                )
        elif plan.recipe_build_id is not None:
            # A build performed by the same high-level operation has no OCI
            # output digest at preview time.  The distribution adapter must
            # bind its verification receipt to the exact durable build row.
            if result.get("verified_build_id") != plan.recipe_build_id:
                raise RunSwitchOperationConflict(
                    "run-switch.runtime-build-verification-mismatch"
                )
    elif phase.kind == "cleanup":
        if result.get("scope") != "spark-local":
            raise RunSwitchOperationConflict(
                "run-switch.cleanup-scope-invalid"
            )
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
            or not all(_is_hex_digest(value) for value in [*raw_reclaimed, *raw_protected])
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
        maximum_reclaimable = plan.storage.reclaimable_bytes + plan.runtime_storage.reclaimable_bytes
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


def _is_hex_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_oci_digest(value: object) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and _is_hex_digest(value[7:])


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


def _per_target_bytes(value: int | None, target_count: int) -> int | None:
    if value is None or target_count < 1 or value % target_count != 0:
        return None
    return value // target_count


def _runtime_interface(document: Mapping[str, object]) -> str:
    interfaces = document.get("interfaces")
    if isinstance(interfaces, list):
        for item in interfaces:
            if isinstance(item, Mapping) and isinstance(item.get("adapter"), str):
                return item["adapter"]
    return "unknown"


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RunSwitchOperationConflict("run-switch persisted identity is invalid")
    return value


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
    return RunSwitchPlan.model_validate(value, strict=False)


__all__ = [
    "ActivityOperationListPage",
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
