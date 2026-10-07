"""Evidence based capacity planning for canonical recipe settings.

The public RecipeDefinition owns settings and topology.  This module consumes
that typed projection and never invents an engine memory formula.  Context and
concurrency terms are used only when the selected serving kind has the setting
and an explicit measured/declarative evidence term.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal, TypeGuard, get_args

from pydantic import ValidationError
from vonk_agent_protocol import (
    ResourceBlockerCode,
    ResourcePlanningCode,
    ResourceTerm,
    ResourceTermProblem,
    resource_term_code,
)
from vonk_agent_protocol.inventory import MemoryPool
from vonk_forge_contracts import ModelDefinition, RecipeDefinition
from vonk_forge_contracts.recipe import (
    RecipeDiskResources,
    RecipeMemoryResources,
    Scalar,
)

from .bounded_json import require_integer
from .resource_planning_contract import (
    ResourceRecipeProjection,
)
from .run_switch_contract import (
    EffectiveSettingsSelection,
    MemoryKind,
    RunSwitchChangeEffect,
)

EvidenceState = Literal["declared", "measured", "fresh", "stale", "unknown"]
Effect = Literal["reuse", "restart", "reprepare", "reinstall", "rebuild"]
_CHANGE_EFFECTS: frozenset[RunSwitchChangeEffect] = frozenset(
    get_args(RunSwitchChangeEffect)
)
# The memory the platform keeps free on every Spark beyond a workload's declared
# peak. A recipe's own ``reserve_bytes`` is informational and is never added.
PLATFORM_MEMORY_FLOOR_BYTES = 2_000_000_000

# Informational: a recipe's declared peak plus the platform floor exceeds a
# Spark's physical memory. Never a refusal; the declared envelope is an estimate.
ENVELOPE_EXCEEDS_CAPACITY = ResourcePlanningCode.ENVELOPE_EXCEEDS_CAPACITY.value

# Warning: admitted although the declared envelope does not fit, because no Vonk
# claim holds memory on the Spark. The run's real outcome is the evidence.
ENVELOPE_UNVERIFIED = ResourcePlanningCode.ENVELOPE_UNVERIFIED.value


@dataclass(frozen=True, slots=True)
class ResourceReason:
    code: str
    detail: str
    severity: Literal["blocker", "warning"] = "blocker"
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class InstallationDiskRequirement:
    required_bytes: int
    floor_bytes: int


def installation_disk_requirement(
    disk: RecipeDiskResources,
    *,
    required_download_bytes: int,
    minimum_floor_bytes: int,
) -> InstallationDiskRequirement:
    """One disk envelope for operator review and installation admission.

    The caller supplies exact missing payload bytes, or a full allocation when
    it cannot yet promise reuse. The reserve remains separate from consumed
    bytes so installation does not mistake headroom for downloaded data.
    """
    if required_download_bytes < 0 or minimum_floor_bytes < 0:
        raise ValueError("installation disk envelope contains negative bytes")
    return InstallationDiskRequirement(
        required_download_bytes + disk.working_bytes,
        max(minimum_floor_bytes, disk.safety_margin_bytes),
    )


@dataclass(frozen=True, slots=True)
class ParallelismSettings:
    world_size: int
    tensor: int
    pipeline: int
    data: int
    backend: str


@dataclass(frozen=True, slots=True)
class EffectiveResourceSettings:
    kind: Literal["generation", "embedding", "job"]
    context_tokens: int | None
    concurrency: int | None
    batch_tokens: int | None
    parallelism: ParallelismSettings
    knobs: Mapping[str, Scalar] = field(default_factory=dict)
    change_effects: Mapping[str, RunSwitchChangeEffect] = field(default_factory=dict)
    identity_digest: str = ""

    def identity(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "context_tokens": self.context_tokens,
            "concurrency": self.concurrency,
            "batch_tokens": self.batch_tokens,
            "parallelism": {
                "world_size": self.parallelism.world_size,
                "tensor": self.parallelism.tensor,
                "pipeline": self.parallelism.pipeline,
                "data": self.parallelism.data,
                "backend": self.parallelism.backend,
            },
            "knobs": _canonical(self.knobs),
        }


@dataclass(frozen=True, slots=True)
class SettingsResolution:
    settings: EffectiveResourceSettings | None
    reasons: tuple[ResourceReason, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.settings is not None and not any(
            reason.severity == "blocker" for reason in self.reasons
        )


@dataclass(frozen=True, slots=True)
class ResourceEvidence:
    weights_bytes: int | None
    runtime_overhead_bytes: int | None
    baseline_context_tokens: int | None = None
    baseline_concurrency: int | None = None
    baseline_batch_tokens: int | None = None
    context_bytes_per_token: int | None = None
    concurrency_bytes_per_request: int | None = None
    batch_bytes_per_token: int | None = None
    supported_context_tokens: tuple[int, int] | None = None
    supported_concurrency: tuple[int, int] | None = None
    supported_batch_tokens: tuple[int, int] | None = None
    evidence_state: EvidenceState = "unknown"
    evidence_digest: str | None = None
    declared_total_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ResourceDemand:
    weights_bytes: int | None
    runtime_overhead_bytes: int | None
    context_bytes: int | None
    concurrency_bytes: int | None
    batch_bytes: int | None
    total_bytes: int | None
    evidence_state: EvidenceState
    reasons: tuple[ResourceReason, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.total_bytes is not None and not any(
            reason.severity == "blocker" for reason in self.reasons
        )


@dataclass(frozen=True, slots=True)
class UnknownRunMemoryResidual:
    """Upper bound for an active run whose resident usage is not observed."""

    run_id: str
    run_generation: int
    reservation_kind: str
    maximum_bytes: int


@dataclass(frozen=True, slots=True)
class MemoryReservationTotals:
    """Hard peak commitments and bounds not resolved by physical observations."""

    committed_bytes_by_kind: Mapping[str, int]
    unmaterialized_bytes_by_kind: Mapping[str, int]
    unknown_run_residuals_by_kind: Mapping[
        str, tuple[UnknownRunMemoryResidual, ...]
    ] = field(default_factory=dict)
    #: Claims of runs whose Stop receipts the Controller confirmed after the
    #: inventory sample was taken. The sample still counts those runs' memory as
    #: used, so the freed bytes are credited back, never more than the sample
    #: reports as occupied.
    released_unobserved_bytes_by_kind: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryRequirement:
    """One recipe rank's demand and reserve, shared by review and admission."""

    kind: MemoryKind
    floor_bytes: int
    demand: ResourceDemand

    @property
    def reservation_kind(self) -> str:
        return memory_reservation_kind(self.kind)


def memory_reservation_kind(kind: str) -> str:
    return {
        "unified": "unified-memory",
        "host": "host-memory",
        "accelerator": "gpu-memory",
    }[kind]


def memory_reservation_kinds(kind: str, pool: MemoryPool) -> tuple[str, ...]:
    """Declared consumers of one physical pool, independent of owner names."""
    kinds = ("host-memory", "gpu-memory", "unified-memory")
    if kind not in kinds:
        raise ValueError("reservation is not memory")
    if pool == "shared":
        return kinds
    if kind == "unified-memory":
        # Unified demand is checked against both independent capacities below.
        return kinds
    return kind, "unified-memory"


def memory_requirement(
    recipe_document: Mapping[str, object],
    memory: RecipeMemoryResources,
    role_name: str,
    model_documents: Mapping[
        tuple[str, str, str], ModelDefinition | Mapping[str, object]
    ]
    | None,
    *,
    settings: object | None = None,
    platform_floor_bytes: int = 0,
) -> MemoryRequirement:
    """DGX Spark memory is unified; the declared peak plus the platform floor describe a role.

    The recipe's ``reserve_bytes`` is informational: it is not added to the peak.
    """
    if platform_floor_bytes < 0:
        raise ValueError("recipe memory envelope is invalid")
    selected = settings if settings is not None else recipe_document
    resolution = (
        SettingsResolution(selected)
        if isinstance(selected, EffectiveResourceSettings)
        else resolve_effective_settings(selected)
    )
    evidence = _resource_evidence(
        recipe_document,
        role_name,
        model_documents,
        memory.peak_bytes,
        resolution.settings,
    )
    demand = resource_demand(
        resolution.settings if resolution.settings is not None else selected,
        evidence,
    )
    return MemoryRequirement("unified", platform_floor_bytes, demand)


def memory_capacity_snapshot(
    node_id: str,
    kind: MemoryKind,
    *,
    host: tuple[int, int] | None,
    accelerator: tuple[int, int] | None,
    reservations: MemoryReservationTotals,
    memory_pool: MemoryPool | None,
    evidence_state: EvidenceState,
    evidence_digest: str | None = None,
    evidence_observed_at: datetime | None = None,
) -> CapacitySnapshot:
    def component(kind: MemoryKind, values: tuple[int, int] | None) -> CapacitySnapshot:
        reserved = (
            sum(
                reservations.committed_bytes_by_kind.get(item, 0)
                for item in memory_reservation_kinds(
                    memory_reservation_kind(kind), memory_pool
                )
            )
            if memory_pool is not None
            else None
        )
        unmaterialized = (
            sum(
                reservations.unmaterialized_bytes_by_kind.get(item, 0)
                for item in memory_reservation_kinds(
                    memory_reservation_kind(kind), memory_pool
                )
            )
            if memory_pool is not None
            else None
        )
        unknown_run_residuals = (
            tuple(
                residual
                for item in memory_reservation_kinds(
                    memory_reservation_kind(kind), memory_pool
                )
                for residual in reservations.unknown_run_residuals_by_kind.get(item, ())
            )
            if memory_pool is not None
            else ()
        )
        total, free = values if values is not None else (None, None)
        credit = (
            sum(
                reservations.released_unobserved_bytes_by_kind.get(item, 0)
                for item in memory_reservation_kinds(
                    memory_reservation_kind(kind), memory_pool
                )
            )
            if memory_pool is not None
            else 0
        )
        return CapacitySnapshot(
            node_id,
            kind,
            total,
            max(0, total - free - credit)
            if total is not None and free is not None
            else None,
            reserved,
            evidence_state if total is not None else "unknown",
            evidence_digest,
            unmaterialized_bytes=unmaterialized,
            unknown_run_residuals=unknown_run_residuals,
            evidence_observed_at=evidence_observed_at,
        )

    if memory_pool == "shared":
        values = (
            (min(host[0], accelerator[0]), min(host[1], accelerator[1]))
            if host is not None and accelerator is not None
            else None
        )
        return component(kind, values)
    if kind == "host":
        return component("host", host)
    if kind == "accelerator":
        return component("accelerator", accelerator)
    # A unified envelope on separate hardware must fit each pool. Adding their
    # independent reservations would invent consumption; checking only one pool
    # would miss a blocker, including a different limiting pool after a stop.
    components = (component("host", host), component("accelerator", accelerator))
    limiting = min(
        components,
        key=lambda item: (
            min(
                item.available_bytes - item.reserved_bytes,
                item.available_bytes
                - item.occupied_bytes
                - (item.unmaterialized_bytes or 0)
                - sum(item.maximum_bytes for item in item.unknown_run_residuals),
            )
            if item.available_bytes is not None
            and item.occupied_bytes is not None
            and item.reserved_bytes is not None
            else -1
        ),
    )
    residuals = tuple(
        dict.fromkeys(
            residual
            for component in components
            for residual in component.unknown_run_residuals
        )
    )
    return replace(
        limiting,
        memory_kind="unified",
        components=components,
        unknown_run_residuals=residuals,
    )


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    node_id: str
    memory_kind: str
    available_bytes: int | None
    occupied_bytes: int | None
    reserved_bytes: int | None
    evidence_state: EvidenceState = "unknown"
    evidence_digest: str | None = None
    components: tuple[CapacitySnapshot, ...] = ()
    unmaterialized_bytes: int | None = 0
    unknown_run_residuals: tuple[UnknownRunMemoryResidual, ...] = ()
    evidence_observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PlannedStopRelease:
    run_id: str
    node_id: str
    memory_kind: str
    release_bytes: int | None
    planned: bool
    plan_digest: str | None = None


@dataclass(frozen=True, slots=True)
class NodeCapacityPlan:
    node_id: str
    required_bytes: int | None
    current_free_after_bytes: int | None
    after_stop_free_after_bytes: int | None
    selected_free_after_bytes: int | None
    stop_required: bool
    allowed: bool
    reasons: tuple[ResourceReason, ...] = ()
    insufficient_components: tuple[str, ...] = ()
    unknown_run_residuals: tuple[UnknownRunMemoryResidual, ...] = ()


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    nodes: tuple[NodeCapacityPlan, ...]
    allowed: bool
    stop_before_prepare: bool
    reasons: tuple[ResourceReason, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourcePreflightPlan:
    settings: EffectiveResourceSettings | None
    demands: Mapping[str, ResourceDemand]
    capacity: CapacityPlan | None
    reasons: tuple[ResourceReason, ...] = ()

    @property
    def allowed(self) -> bool:
        return (
            self.settings is not None
            and self.capacity is not None
            and self.capacity.allowed
            and not any(reason.severity == "blocker" for reason in self.reasons)
        )


@dataclass(frozen=True, slots=True)
class PreparationDecision:
    effect: Effect
    settings_changed: tuple[str, ...]
    compatibility_changed: bool
    requires_restart: bool
    requires_reprepare: bool
    requires_reinstall: bool
    requires_rebuild: bool
    settings_digest: str


def resolve_effective_settings(value: object) -> SettingsResolution:
    """Read canonical settings once, then plan from typed owned fields."""
    if isinstance(value, EffectiveResourceSettings):
        return SettingsResolution(value)
    if isinstance(value, EffectiveSettingsSelection):
        parallel = value.parallelism
        return SettingsResolution(
            EffectiveResourceSettings(
                value.kind,
                value.context_tokens,
                value.concurrency,
                value.max_batch_tokens,
                ParallelismSettings(
                    parallel.world_size,
                    parallel.tensor,
                    parallel.pipeline,
                    parallel.data,
                    parallel.backend,
                ),
                value.knobs,
                value.change_effects,
                value.identity_sha256,
            )
        )
    if isinstance(value, RecipeDefinition):
        raw = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        raw = value
    else:
        return SettingsResolution(
            None,
            (
                _reason(
                    ResourcePlanningCode.SETTINGS_UNKNOWN,
                    "Canonical effective settings are unavailable.",
                ),
            ),
        )
    try:
        recipe = ResourceRecipeProjection.model_validate_json(
            json.dumps(raw, allow_nan=False), strict=True
        )
    except ValidationError as error:
        reasons = []
        for detail in error.errors():
            location = detail["loc"]
            if "parallelism" in location and "settings" in location:
                code = ResourcePlanningCode.PARALLELISM_DUPLICATE
            elif "topology" in location:
                code = (
                    ResourcePlanningCode.PARALLELISM_UNKNOWN
                    if detail["type"] == "missing"
                    else ResourcePlanningCode.PARALLELISM_TYPE
                )
            elif "knobs" in location:
                code = ResourcePlanningCode.KNOBS_INVALID
            elif detail["type"] in {"union_tag_invalid", "union_tag_not_found"}:
                code = ResourcePlanningCode.SETTINGS_KIND_UNKNOWN
            else:
                code = ResourcePlanningCode.SETTINGS_TYPE
            reasons.append(
                _reason(
                    code,
                    "Canonical resource settings are invalid: "
                    + ".".join(str(part) for part in location),
                )
            )
        return SettingsResolution(None, tuple(dict.fromkeys(reasons)))
    except (TypeError, ValueError):
        return SettingsResolution(
            None,
            (
                _reason(
                    ResourcePlanningCode.SETTINGS_UNKNOWN,
                    "Canonical effective settings cannot be read.",
                ),
            ),
        )
    settings = recipe.settings
    parallel = recipe.topology.parallelism
    if (
        parallel.tensor * parallel.pipeline * parallel.data
        != recipe.topology.node_count
    ):
        return SettingsResolution(
            None,
            (
                _reason(
                    ResourcePlanningCode.PARALLELISM_INCONSISTENT,
                    "Topology parallelism product does not equal node_count.",
                ),
            ),
        )
    context = settings.context_tokens.value if settings.kind == "generation" else None
    concurrency = (
        settings.concurrency.value if settings.concurrency is not None else None
    )
    batch = (
        settings.max_batch_tokens.value
        if settings.kind != "job" and settings.max_batch_tokens is not None
        else None
    )
    knobs = {name: setting.value for name, setting in settings.knobs.items()}
    effects: dict[str, RunSwitchChangeEffect] = {
        name: setting.change_effect for name, setting in settings.knobs.items()
    }
    if settings.kind == "generation":
        effects["context_tokens"] = settings.context_tokens.change_effect
    if settings.concurrency is not None:
        effects["concurrency"] = settings.concurrency.change_effect
    if settings.kind != "job" and settings.max_batch_tokens is not None:
        effects["max_batch_tokens"] = settings.max_batch_tokens.change_effect
    identity = {
        "kind": settings.kind,
        "context_tokens": context,
        "concurrency": concurrency,
        "max_batch_tokens": batch,
        "parallelism": {
            "world_size": recipe.topology.node_count,
            "tensor": parallel.tensor,
            "pipeline": parallel.pipeline,
            "data": parallel.data,
            "backend": parallel.backend,
        },
        "knobs": _canonical(knobs),
    }
    return SettingsResolution(
        EffectiveResourceSettings(
            settings.kind,
            context,
            concurrency,
            batch,
            ParallelismSettings(
                recipe.topology.node_count,
                parallel.tensor,
                parallel.pipeline,
                parallel.data,
                parallel.backend,
            ),
            knobs,
            effects,
            _digest(identity),
        )
    )


def _selected_model_bytes(
    recipe_document: Mapping[str, object],
    model_documents: Mapping[
        tuple[str, str, str], ModelDefinition | Mapping[str, object]
    ]
    | None,
    role_name: str,
) -> int | None:
    if not model_documents:
        return None
    try:
        recipe = ResourceRecipeProjection.model_validate_json(
            json.dumps(recipe_document, allow_nan=False), strict=True
        )
        total = 0
        selected_any = False
        for selection in recipe.models:
            reference = selection.model
            raw_model = model_documents.get(
                (reference.publisher, reference.slug, reference.content_sha256)
            )
            if raw_model is None:
                return None
            model = (
                raw_model
                if isinstance(raw_model, ModelDefinition)
                else ModelDefinition.model_validate_json(
                    json.dumps(raw_model, allow_nan=False), strict=True
                )
            )
            by_id = {file.id: file for file in model.files}
            selected_ids = {
                item.file_id for item in selection.files if role_name in item.roles
            }
            for file_id in selected_ids:
                file = by_id.get(file_id)
                if file is None:
                    return None
                total += file.size_bytes
                selected_any = True
        return total if selected_any else None
    except (TypeError, ValueError):
        return None


def _resource_evidence(
    recipe_document: Mapping[str, object],
    role_name: str,
    model_documents: Mapping[
        tuple[str, str, str], ModelDefinition | Mapping[str, object]
    ]
    | None,
    declared_total_bytes: int,
    settings: EffectiveResourceSettings | None,
) -> ResourceEvidence:
    model_bytes = _selected_model_bytes(recipe_document, model_documents, role_name)
    return ResourceEvidence(
        weights_bytes=model_bytes,
        runtime_overhead_bytes=None,
        declared_total_bytes=declared_total_bytes if model_bytes is not None else None,
        baseline_context_tokens=settings.context_tokens
        if settings is not None
        else None,
        baseline_concurrency=settings.concurrency if settings is not None else None,
        baseline_batch_tokens=settings.batch_tokens if settings is not None else None,
        evidence_state="declared" if model_bytes is not None else "unknown",
    )


def resource_demand(
    settings: EffectiveResourceSettings | object,
    evidence: ResourceEvidence,
    *,
    node_id: str | None = None,
) -> ResourceDemand:
    resolution = (
        SettingsResolution(settings, ())
        if isinstance(settings, EffectiveResourceSettings)
        else resolve_effective_settings(settings)
    )
    reasons = list(resolution.reasons)
    if resolution.settings is None:
        return ResourceDemand(
            None, None, None, None, None, None, "unknown", tuple(reasons)
        )
    selected = resolution.settings
    declared_bound = (
        evidence.declared_total_bytes
        if type(evidence.declared_total_bytes) is int
        and evidence.declared_total_bytes >= 0
        else None
    )
    uncertain: list[str] = []
    if evidence.declared_total_bytes is not None and declared_bound is None:
        reasons.append(
            _reason(
                ResourcePlanningCode.EVIDENCE_INVALID,
                "Declared recipe-role memory envelope is invalid.",
                node_id=node_id,
            )
        )
    if evidence.evidence_state in {"unknown", "stale"}:
        if declared_bound is None:
            reasons.append(
                _reason(
                    ResourcePlanningCode.EVIDENCE_UNKNOWN,
                    "Memory evidence is missing or stale and no declared role bound is available.",
                    node_id=node_id,
                )
            )
        else:
            uncertain.append(f"{evidence.evidence_state} evidence")
    if evidence.evidence_digest is not None and not _is_digest(
        evidence.evidence_digest
    ):
        reasons.append(
            _reason(
                ResourcePlanningCode.EVIDENCE_INVALID,
                "Memory evidence digest is invalid.",
                node_id=node_id,
            )
        )
    for name, item in (
        ("weights_bytes", evidence.weights_bytes),
        ("runtime_overhead_bytes", evidence.runtime_overhead_bytes),
    ):
        if item is None:
            if declared_bound is None:
                reasons.append(
                    _reason(
                        ResourcePlanningCode.EVIDENCE_UNKNOWN,
                        f"{name} is missing and no declared role bound is available.",
                        node_id=node_id,
                    )
                )
            else:
                uncertain.append(name)
        elif type(item) is not int or item < 0:
            reasons.append(
                _reason(
                    ResourcePlanningCode.EVIDENCE_INVALID,
                    f"{name} is invalid; resource evidence cannot be trusted.",
                    node_id=node_id,
                )
            )
    context = _term(
        ResourceTerm.CONTEXT,
        selected.context_tokens,
        evidence.baseline_context_tokens,
        evidence.context_bytes_per_token,
        evidence.supported_context_tokens,
        node_id,
        required=selected.context_tokens is not None,
    )
    concurrency = _term(
        ResourceTerm.CONCURRENCY,
        selected.concurrency,
        evidence.baseline_concurrency,
        evidence.concurrency_bytes_per_request,
        evidence.supported_concurrency,
        node_id,
        required=selected.concurrency is not None,
    )
    batch = _term(
        ResourceTerm.BATCH,
        selected.batch_tokens,
        evidence.baseline_batch_tokens,
        evidence.batch_bytes_per_token,
        evidence.supported_batch_tokens,
        node_id,
        required=selected.batch_tokens is not None,
    )
    terms: list[tuple[int | None, tuple[ResourceReason, ...]]] = []
    for name, term in (
        ("context", context),
        ("concurrency", concurrency),
        ("batch", batch),
    ):
        if term[0] is None and declared_bound is not None:
            if term[1] and all(
                reason.code.endswith(("_unknown", "_unsupported")) for reason in term[1]
            ):
                uncertain.append(f"{name} estimate")
                terms.append((0, ()))
            else:
                terms.append(term)
                reasons.extend(term[1])
        else:
            terms.append(term)
            reasons.extend(term[1])
    total: int | None = None
    if not reasons and all(isinstance(term[0], int) for term in terms):
        base = declared_bound
        if (
            base is None
            and type(evidence.weights_bytes) is int
            and type(evidence.runtime_overhead_bytes) is int
        ):
            base = evidence.weights_bytes + evidence.runtime_overhead_bytes
        if base is not None:
            total = (
                base
                + require_integer(terms[0][0], "context")
                + require_integer(terms[1][0], "concurrency")
                + require_integer(terms[2][0], "batch")
            )
    if uncertain and declared_bound is not None and total is not None:
        reasons.append(
            _reason(
                ResourcePlanningCode.ESTIMATE_UNCERTAIN,
                f"Forecast {total} bytes from the declared recipe-role memory envelope ({declared_bound} bytes); {', '.join(dict.fromkeys(uncertain))} is unavailable, so actual demand may exceed this bound.",
                severity="warning",
                node_id=node_id,
            )
        )
    return ResourceDemand(
        evidence.weights_bytes if type(evidence.weights_bytes) is int else None,
        evidence.runtime_overhead_bytes
        if type(evidence.runtime_overhead_bytes) is int
        else None,
        context[0],
        concurrency[0],
        batch[0],
        total,
        evidence.evidence_state,
        tuple(reasons),
    )


def _minimum_known(values: Sequence[int | None]) -> int | None:
    return (
        None if None in values else min(value for value in values if value is not None)
    )


def _resident_usage_uncertainty_detail(
    capacity: CapacitySnapshot,
    residuals: Sequence[UnknownRunMemoryResidual],
    *,
    admitted: bool,
) -> str:
    observed_at = (
        capacity.evidence_observed_at.isoformat()
        if capacity.evidence_observed_at is not None
        else "timestamp unavailable"
    )
    digest = capacity.evidence_digest or "digest unavailable"
    maximum = sum(item.maximum_bytes for item in residuals)
    if admitted:
        return (
            f"Aggregate inventory {observed_at} ({digest}) reports no per-run "
            f"resident usage for {len(residuals)} exact active claim(s); their "
            f"remaining commitment is in the range 0..{maximum} bytes. Admission "
            "applies the full upper bound."
        )
    return (
        f"Capacity is unverified by aggregate inventory {observed_at} ({digest}): "
        f"{len(residuals)} exact active run claim(s) may retain 0..{maximum} "
        "bytes, and the safe upper bound does not fit. Reconcile the exact run "
        "claims and retry against fresh inventory."
    )


def plan_capacity(
    requirements: Mapping[str, ResourceDemand],
    capacities: Sequence[CapacitySnapshot],
    planned_stops: Sequence[PlannedStopRelease] = (),
    *,
    memory_floor_bytes: int = 0,
) -> CapacityPlan:
    reasons: list[ResourceReason] = []
    by_node = {item.node_id: item for item in capacities}
    releases: dict[tuple[str, str], int] = {}
    for stop in planned_stops:
        if not stop.planned:
            continue
        if type(stop.release_bytes) is not int or stop.release_bytes < 0:
            reasons.append(
                _reason(
                    ResourcePlanningCode.STOP_RELEASE_UNKNOWN,
                    "A planned stop has no valid capacity release evidence.",
                    node_id=stop.node_id,
                )
            )
            continue
        releases[(stop.node_id, stop.memory_kind)] = (
            releases.get((stop.node_id, stop.memory_kind), 0) + stop.release_bytes
        )
    nodes: list[NodeCapacityPlan] = []
    for node_id, demand in requirements.items():
        node_reasons = list(demand.reasons)
        capacity = by_node.get(node_id)
        if capacity is None:
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.CAPACITY_UNKNOWN,
                    "Capacity evidence is unavailable for the selected rank.",
                    node_id=node_id,
                )
            )
            nodes.append(
                NodeCapacityPlan(
                    node_id,
                    demand.total_bytes,
                    None,
                    None,
                    None,
                    False,
                    False,
                    tuple(node_reasons),
                )
            )
            continue
        available = capacity.available_bytes
        if capacity.components:
            parts = [
                plan_capacity(
                    {node_id: demand},
                    [part],
                    planned_stops,
                    memory_floor_bytes=memory_floor_bytes,
                ).nodes[0]
                for part in capacity.components
            ]
            nodes.append(
                NodeCapacityPlan(
                    node_id,
                    demand.total_bytes,
                    _minimum_known([part.current_free_after_bytes for part in parts]),
                    _minimum_known(
                        [part.after_stop_free_after_bytes for part in parts]
                    ),
                    _minimum_known([part.selected_free_after_bytes for part in parts]),
                    any(part.stop_required for part in parts),
                    all(part.allowed for part in parts),
                    tuple(
                        dict.fromkeys(
                            reason for part in parts for reason in part.reasons
                        )
                    ),
                    tuple(
                        dict.fromkeys(
                            component
                            for part in parts
                            for component in part.insufficient_components
                        )
                    ),
                    tuple(
                        dict.fromkeys(
                            residual
                            for part in parts
                            for residual in part.unknown_run_residuals
                        )
                    ),
                )
            )
            continue
        occupied = capacity.occupied_bytes
        reserved = capacity.reserved_bytes
        unmaterialized = capacity.unmaterialized_bytes
        unknown_residuals = capacity.unknown_run_residuals
        unknown_upper_bytes = sum(
            residual.maximum_bytes for residual in unknown_residuals
        )
        for name, value in (
            ("available", available),
            ("occupied", occupied),
            ("reserved", reserved),
            ("unmaterialized", unmaterialized),
        ):
            if type(value) is not int or value < 0:
                node_reasons.append(
                    _reason(
                        ResourceBlockerCode.CAPACITY_UNKNOWN,
                        f"Current {name} capacity evidence is missing or invalid.",
                        node_id=node_id,
                    )
                )
        if capacity.evidence_state in {"unknown", "stale"}:
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.CAPACITY_UNKNOWN,
                    "Current capacity evidence is missing or stale.",
                    node_id=node_id,
                )
            )
        total_bytes = demand.total_bytes
        if (
            not isinstance(available, int)
            or not isinstance(occupied, int)
            or not isinstance(reserved, int)
            or not isinstance(unmaterialized, int)
            or total_bytes is None
            or any(reason.severity == "blocker" for reason in node_reasons)
        ):
            nodes.append(
                NodeCapacityPlan(
                    node_id,
                    demand.total_bytes,
                    None,
                    None,
                    None,
                    False,
                    False,
                    tuple(node_reasons),
                )
            )
            continue
        # Aggregate inventory has no exact resident usage for a retained run.
        # Its remaining reservation is therefore a range from zero to the full
        # peak. Admission uses the safe lower-capacity bound; it never treats a
        # starting/running state as measured bytes or as a release receipt.
        current_without_unknown = available - occupied - unmaterialized - total_bytes
        current = current_without_unknown - unknown_upper_bytes
        budget_after = available - reserved - total_bytes - memory_floor_bytes
        release = releases.get((node_id, capacity.memory_kind), 0)
        if not release:
            release = max(
                (
                    value
                    for (candidate, kind), value in releases.items()
                    if candidate == node_id
                    and _same_memory_kind(kind, capacity.memory_kind)
                ),
                default=0,
            )
        idle = reserved == 0 and unmaterialized == 0 and not unknown_residuals
        declared_shortfall = (
            available - total_bytes - memory_floor_bytes < 0
            or current < memory_floor_bytes
            or budget_after < 0
        )
        if (
            idle
            and not release
            and declared_shortfall
            and available - occupied >= memory_floor_bytes
        ):
            # No Vonk claim holds memory on this Spark, so the only thing the
            # declared envelope can displace is nothing of ours. The envelope is
            # an estimate and hardware is the truth: admit the attempt, typed as
            # an unverified fit, and let the run's real outcome be the evidence.
            node_reasons.append(
                _reason(
                    ENVELOPE_UNVERIFIED,
                    f"The recipe's declared memory envelope ({total_bytes} bytes peak plus the "
                    f"{memory_floor_bytes}-byte platform floor) does not fit this Spark's "
                    f"{available - occupied} bytes of free memory, but no Vonk workload or claim "
                    "holds memory here. The declared envelope is an estimate, so the attempt is "
                    "admitted as an unverified fit; the run's own outcome is the evidence.",
                    severity="warning",
                    node_id=node_id,
                )
            )
            if available - total_bytes - memory_floor_bytes < 0:
                node_reasons.append(
                    _reason(
                        ENVELOPE_EXCEEDS_CAPACITY,
                        f"The declared envelope ({total_bytes + memory_floor_bytes} bytes) "
                        f"exceeds this Spark's {available}-byte memory capacity by "
                        f"{total_bytes + memory_floor_bytes - available} bytes. Informational: "
                        "the declared envelope is an estimate.",
                        severity="warning",
                        node_id=node_id,
                    )
                )
            nodes.append(
                NodeCapacityPlan(
                    node_id,
                    demand.total_bytes,
                    current,
                    current,
                    current,
                    False,
                    not any(reason.severity == "blocker" for reason in node_reasons),
                    tuple(node_reasons),
                )
            )
            continue
        after_stop = current + release
        budget_after_stop = budget_after + release
        current_fit = current >= memory_floor_bytes and budget_after >= 0
        after_fit = after_stop >= memory_floor_bytes and budget_after_stop >= 0
        selected = after_stop if release else current
        allowed = (after_fit if release else current_fit) and not any(
            reason.severity == "blocker" for reason in node_reasons
        )
        if budget_after < 0 and (not release or budget_after_stop < 0):
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.INSUFFICIENT_RESERVATION_BUDGET,
                    f"Exact memory commitments plus demand and reserve exceed the physical pool by {-budget_after} bytes.",
                    node_id=node_id,
                )
            )
        if (
            current_without_unknown >= memory_floor_bytes
            and current < memory_floor_bytes
            and not release
        ):
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.RESIDENT_USAGE_UNKNOWN,
                    _resident_usage_uncertainty_detail(
                        capacity, unknown_residuals, admitted=False
                    ),
                    node_id=node_id,
                )
            )
        elif current_without_unknown < memory_floor_bytes and not release:
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.INSUFFICIENT_CAPACITY,
                    f"Observed free capacity less definite claims and selected demand leaves "
                    f"{current_without_unknown} bytes before the required "
                    f"{memory_floor_bytes}-byte reserve, even if retained runs use zero bytes."
                    + (
                        " No Vonk claim holds memory on this Spark, so the shortfall is "
                        "memory used outside Vonk's workloads (the operating system "
                        "or other processes); stopping workloads cannot free it."
                        if reserved == 0
                        and unmaterialized == 0
                        and not unknown_residuals
                        else ""
                    ),
                    node_id=node_id,
                )
            )
        elif release and after_stop < memory_floor_bytes:
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.INSUFFICIENT_CAPACITY_AFTER_STOP,
                    f"Selected demand leaves {selected} bytes after planned stops; {memory_floor_bytes} bytes must remain reserved.",
                    node_id=node_id,
                )
            )
        elif (
            not release
            and current >= memory_floor_bytes
            and budget_after >= 0
            and unknown_residuals
        ):
            node_reasons.append(
                _reason(
                    ResourceBlockerCode.RESIDENT_USAGE_UNKNOWN,
                    _resident_usage_uncertainty_detail(
                        capacity, unknown_residuals, admitted=True
                    ),
                    severity="warning",
                    node_id=node_id,
                )
            )
        nodes.append(
            NodeCapacityPlan(
                node_id,
                demand.total_bytes,
                None if unknown_residuals else current,
                None if unknown_residuals else after_stop,
                None if unknown_residuals else selected,
                not current_fit and after_fit and release > 0,
                allowed,
                tuple(node_reasons),
                (capacity.memory_kind,) if not current_fit else (),
                unknown_residuals,
            )
        )
    reasons.extend(reason for node in nodes for reason in node.reasons)
    return CapacityPlan(
        tuple(nodes),
        bool(nodes)
        and all(node.allowed for node in nodes)
        and not any(reason.severity == "blocker" for reason in reasons),
        any(node.stop_required for node in nodes),
        tuple(reasons),
    )


def plan_resource_preflight(
    effective_context: Mapping[str, object] | object,
    evidence_by_node: Mapping[str, ResourceEvidence],
    capacities: Sequence[CapacitySnapshot],
    planned_stops: Sequence[PlannedStopRelease] = (),
    *,
    memory_floor_bytes: int = 0,
) -> ResourcePreflightPlan:
    resolution = resolve_effective_settings(effective_context)
    if resolution.settings is None:
        return ResourcePreflightPlan(None, {}, None, resolution.reasons)
    demands = {
        node_id: resource_demand(resolution.settings, evidence, node_id=node_id)
        for node_id, evidence in evidence_by_node.items()
    }
    capacity = plan_capacity(
        demands, capacities, planned_stops, memory_floor_bytes=memory_floor_bytes
    )
    return ResourcePreflightPlan(
        resolution.settings, demands, capacity, (*resolution.reasons, *capacity.reasons)
    )


def classify_preparation_effects(
    previous: EffectiveResourceSettings | object | None,
    current: EffectiveResourceSettings | object,
    *,
    parameter_effects: Mapping[str, RunSwitchChangeEffect] | None = None,
) -> PreparationDecision:
    current_resolution = (
        current
        if isinstance(current, EffectiveResourceSettings)
        else resolve_effective_settings(current).settings
    )
    if current_resolution is None:
        raise ValueError("effective settings are invalid")
    previous_resolution = (
        previous
        if isinstance(previous, EffectiveResourceSettings)
        else resolve_effective_settings(previous).settings
        if previous is not None
        else None
    )
    current_identity = current_resolution.identity()
    previous_identity = (
        previous_resolution.identity() if previous_resolution is not None else None
    )
    changed = {
        key
        for key in current_identity
        if previous_identity is None
        or current_identity[key] != previous_identity.get(key)
    }
    effects = dict(current_resolution.change_effects)
    effects.update(parameter_effects or {})
    if any(effect not in _CHANGE_EFFECTS for effect in effects.values()):
        raise ValueError("parameter change effects are invalid")
    active = {key: effect for key, effect in effects.items() if effect != "none"}
    rebuild = "rebuild" in active.values()
    reinstall = rebuild or "reinstall" in active.values() or "parallelism" in changed
    reprepare = (
        reinstall
        or bool(
            changed & {"context_tokens", "concurrency", "batch_tokens", "knobs", "kind"}
        )
        or "reprepare" in active.values()
    )
    restart = reprepare or bool(changed) or "restart" in active.values()
    effect: Effect = (
        "rebuild"
        if rebuild
        else "reinstall"
        if reinstall
        else "reprepare"
        if reprepare
        else "restart"
        if restart
        else "reuse"
    )
    return PreparationDecision(
        effect,
        tuple(sorted(changed | set(active))),
        bool(changed or active),
        restart,
        reprepare,
        reinstall,
        rebuild,
        current_resolution.identity_digest or _digest(current_identity),
    )


def _term(
    name: ResourceTerm,
    value: int | None,
    baseline: int | None,
    coefficient: int | None,
    supported: tuple[int, int] | None,
    node_id: str | None,
    *,
    required: bool,
) -> tuple[int | None, tuple[ResourceReason, ...]]:
    if value is None:
        return (
            (0, ())
            if not required
            else (
                None,
                (
                    _reason(
                        resource_term_code(name, ResourceTermProblem.UNKNOWN),
                        f"Effective {name.value} setting is unavailable; capacity cannot be predicted.",
                        node_id=node_id,
                    ),
                ),
            )
        )
    if baseline is not None and (type(baseline) is not int or baseline < 0):
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.EVIDENCE_INVALID),
                f"Measured baseline evidence for {name.value} is invalid.",
                node_id=node_id,
            ),
        )
    if supported is not None and (
        len(supported) != 2
        or any(type(item) is not int or item < 0 for item in supported)
        or supported[0] > supported[1]
    ):
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.EVIDENCE_INVALID),
                f"Declared supported range for {name.value} is invalid.",
                node_id=node_id,
            ),
        )
    if supported is not None and (value < supported[0] or value > supported[1]):
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.UNSUPPORTED),
                f"Effective {name.value} setting is outside the declared supported range.",
                node_id=node_id,
            ),
        )
    if baseline is None:
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.EVIDENCE_UNKNOWN),
                f"No baseline evidence is declared for effective {name.value}.",
                node_id=node_id,
            ),
        )
    if value == baseline:
        return 0, ()
    if coefficient is None:
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.EVIDENCE_UNKNOWN),
                f"No evidence supports changing effective {name.value} from its measured baseline.",
                node_id=node_id,
            ),
        )
    if type(coefficient) is not int or coefficient < 0:
        return None, (
            _reason(
                resource_term_code(name, ResourceTermProblem.EVIDENCE_INVALID),
                f"Measured coefficient for {name.value} is invalid.",
                node_id=node_id,
            ),
        )
    return max(0, value - baseline) * coefficient, ()


def _same_memory_kind(left: str, right: str) -> bool:
    return (
        {left, right} <= {"unified", "unified-memory"}
        or {left, right} <= {"host", "host-memory"}
        or {left, right} <= {"accelerator", "gpu-memory"}
    )


def _reason(
    code: str,
    detail: str,
    *,
    severity: Literal["blocker", "warning"] = "blocker",
    node_id: str | None = None,
) -> ResourceReason:
    return ResourceReason(code, detail, severity=severity, node_id=node_id)


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical(item) for item in value]
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _is_digest(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )
