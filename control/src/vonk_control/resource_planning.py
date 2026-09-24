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
from typing import Literal, Protocol, TypeGuard, get_args, runtime_checkable

from vonk_agent_protocol.inventory import MemoryPool

from .bounded_json import require_integer
from .run_switch_contract import MemoryKind, RunSwitchChangeEffect

EvidenceState = Literal["declared", "measured", "fresh", "stale", "unknown"]
Effect = Literal["reuse", "restart", "reprepare", "reinstall", "rebuild"]
_CHANGE_EFFECTS: frozenset[RunSwitchChangeEffect] = frozenset(
    get_args(RunSwitchChangeEffect)
)


@runtime_checkable
class _ParallelismProjection(Protocol):
    """The structural shape of one typed parallelism projection."""

    world_size: int
    tensor: int
    pipeline: int
    data: int
    backend: str


@runtime_checkable
class _EffectiveSettingsProjection(Protocol):
    """The structural shape of one typed effective-settings projection."""

    kind: Literal["generation", "embedding", "job"]
    context_tokens: int | None
    concurrency: int | None
    batch_tokens: int | None
    knobs: Mapping[str, object]
    change_effects: Mapping[str, str]
    identity_digest: str
    parallelism: _ParallelismProjection


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
    disk: Mapping[str, object],
    *,
    required_download_bytes: int,
    minimum_floor_bytes: int,
) -> InstallationDiskRequirement:
    """One disk envelope for operator review and installation admission.

    The caller supplies exact missing payload bytes, or a full allocation when
    it cannot yet promise reuse. The reserve remains separate from consumed
    bytes so installation does not mistake headroom for downloaded data.
    """
    staging, cache, rollback, safety = (
        require_integer(disk.get(name), name)
        for name in (
            "staging_bytes",
            "cache_bytes",
            "rollback_bytes",
            "safety_margin_bytes",
        )
    )
    if any(
        value < 0
        for value in (
            staging,
            cache,
            rollback,
            safety,
            required_download_bytes,
            minimum_floor_bytes,
        )
    ):
        raise ValueError("installation disk envelope contains negative bytes")
    return InstallationDiskRequirement(
        required_download_bytes + staging + cache + rollback,
        max(minimum_floor_bytes, safety),
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
    knobs: Mapping[str, object] = field(default_factory=dict)
    change_effects: Mapping[str, str] = field(default_factory=dict)
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
class MemoryReservationTotals:
    """Exact peak commitments and promises not yet reflected in physical free."""

    committed_bytes_by_kind: Mapping[str, int]
    unmaterialized_bytes_by_kind: Mapping[str, int]


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


def _is_memory_kind(value: object) -> TypeGuard[MemoryKind]:
    return isinstance(value, str) and value in get_args(MemoryKind)


def memory_requirement(
    recipe_document: Mapping[str, object],
    memory: Mapping[str, object],
    role_name: str,
    model_documents: Mapping[tuple[str, str, str], Mapping[str, object]] | None,
    *,
    settings: object | None = None,
    platform_floor_bytes: int = 0,
) -> MemoryRequirement:
    kind = memory.get("kind")
    if not _is_memory_kind(kind):
        raise ValueError("recipe memory kind is invalid")
    values = [
        require_integer(memory.get(name), name)
        for name in (
            "startup_peak_bytes",
            "steady_state_bytes",
            "runtime_growth_bytes",
            "system_reserve_bytes",
        )
    ]
    if min(*values, platform_floor_bytes) < 0:
        raise ValueError("recipe memory envelope is invalid")
    startup, steady, growth, reserve = values
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
        max(startup, steady + growth),
        resolution.settings,
    )
    demand = resource_demand(
        resolution.settings if resolution.settings is not None else selected,
        evidence,
    )
    return MemoryRequirement(kind, max(platform_floor_bytes, reserve), demand)


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
        total, free = values if values is not None else (None, None)
        return CapacitySnapshot(
            node_id,
            kind,
            total,
            total - free if total is not None and free is not None else None,
            reserved,
            evidence_state if total is not None else "unknown",
            evidence_digest,
            unmaterialized_bytes=unmaterialized,
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
                - (item.unmaterialized_bytes or 0),
            )
            if item.available_bytes is not None
            and item.occupied_bytes is not None
            and item.reserved_bytes is not None
            else -1
        ),
    )
    return replace(limiting, memory_kind="unified", components=components)


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


def resolve_effective_settings(
    value: Mapping[str, object] | object,
) -> SettingsResolution:
    """Resolve one canonical RecipeDefinition or its typed settings projection."""

    raw = _as_mapping(value)
    if raw is None:
        return SettingsResolution(
            None,
            (
                _reason(
                    "resource.settings_unknown",
                    "Canonical effective settings are unavailable.",
                ),
            ),
        )
    source = raw.get("settings", raw)
    settings = dict(source) if isinstance(source, Mapping) else {}
    reasons: list[ResourceReason] = []

    def setting(name: str) -> tuple[object, str | None]:
        value = settings.get(name)
        if isinstance(value, Mapping) and "value" in value:
            return value.get("value"), _effect(value.get("change_effect"))
        return value, None

    kind = settings.get("kind")
    if kind not in {"generation", "embedding", "job"}:
        reasons.append(
            _reason(
                "resource.settings_kind_unknown",
                "Canonical settings kind is missing or unsupported.",
            )
        )
        kind = "job"

    def positive(name: str, optional: bool = True) -> int | None:
        value, _ = setting(name)
        if value is None and optional:
            return None
        if type(value) is not int or value < 1:
            reasons.append(
                _reason(
                    "resource.settings_type",
                    f"Canonical {name} must be a positive integer.",
                )
            )
            return None
        return value

    context = positive("context_tokens", optional=kind != "generation")
    concurrency = positive("concurrency")
    batch = positive("max_batch_tokens")
    knobs: dict[str, object] = {}
    effects: dict[str, str] = {}
    raw_knobs = settings.get("knobs", {})
    if not isinstance(raw_knobs, Mapping):
        reasons.append(
            _reason("resource.knobs_invalid", "Canonical settings knobs are invalid.")
        )
        raw_knobs = {}
    for name, raw_value in raw_knobs.items():
        if isinstance(raw_value, Mapping) and "value" in raw_value:
            knobs[str(name)] = raw_value.get("value")
            effect = _effect(raw_value.get("change_effect"))
        else:
            knobs[str(name)] = raw_value
            effect = None
        if effect is not None:
            effects[str(name)] = effect
    for name in ("context_tokens", "concurrency", "max_batch_tokens"):
        _, effect = setting(name)
        if effect is not None:
            effects[name] = effect
    raw_effects = settings.get("change_effects")
    if isinstance(raw_effects, Mapping):
        for name, raw_value in raw_effects.items():
            effect = _effect(raw_value)
            if effect is not None:
                effects[str(name)] = effect

    topology = raw.get("topology")
    topology_map = topology if isinstance(topology, Mapping) else None
    parallel = topology_map.get("parallelism") if topology_map else None
    if not isinstance(parallel, Mapping):
        reasons.append(
            _reason(
                "resource.parallelism_unknown",
                "Canonical topology parallelism is unavailable.",
            )
        )
        parallel = {}
    if "parallelism" in settings:
        reasons.append(
            _reason(
                "resource.parallelism_duplicate",
                "Parallelism is owned by topology and cannot be repeated in settings.",
            )
        )
    dimensions: dict[str, int | None] = {}
    for name in ("tensor", "pipeline", "data"):
        value = parallel.get(name)
        if type(value) is not int or value < 1:
            reasons.append(
                _reason(
                    "resource.parallelism_type",
                    f"Canonical topology parallelism {name} is invalid.",
                )
            )
            dimensions[name] = None
        else:
            dimensions[name] = value
    node_count = topology_map.get("node_count") if topology_map else None
    if type(node_count) is not int or node_count < 1:
        reasons.append(
            _reason(
                "resource.parallelism_type", "Canonical topology node_count is invalid."
            )
        )
        node_count = None
    world_size = parallel.get("world_size")
    if type(world_size) is not int or world_size < 1:
        reasons.append(
            _reason(
                "resource.parallelism_type",
                "Canonical topology parallelism world_size is invalid.",
            )
        )
        world_size = None
    tensor, pipeline, data = (
        dimensions["tensor"],
        dimensions["pipeline"],
        dimensions["data"],
    )
    if tensor is not None and pipeline is not None and data is not None:
        product = tensor * pipeline * data
        if world_size is not None and product != world_size:
            reasons.append(
                _reason(
                    "resource.parallelism_inconsistent",
                    "Topology parallelism product does not equal declared world_size.",
                )
            )
        if (
            node_count is not None
            and world_size is not None
            and world_size != node_count
        ):
            reasons.append(
                _reason(
                    "resource.parallelism_inconsistent",
                    "Declared parallelism world_size does not equal node_count.",
                )
            )
    backend = parallel.get("backend")
    if not isinstance(backend, str) or not backend:
        reasons.append(
            _reason(
                "resource.parallelism_type",
                "Canonical topology parallelism backend is invalid.",
            )
        )
        backend = "unknown"
    if reasons:
        return SettingsResolution(None, tuple(reasons))
    identity = {
        "kind": kind,
        "context_tokens": context,
        "concurrency": concurrency,
        "max_batch_tokens": batch,
        "parallelism": {
            "world_size": world_size,
            "tensor": dimensions["tensor"],
            "pipeline": dimensions["pipeline"],
            "data": dimensions["data"],
            "backend": backend,
        },
        "knobs": _canonical(knobs),
    }
    canonical_digest = raw.get("identity_sha256")
    digest = canonical_digest if _is_digest(canonical_digest) else _digest(identity)
    if world_size is None or tensor is None or pipeline is None or data is None:
        # Unreachable: an invalid dimension records a blocker reason and returns
        # above, so the explicit narrowing never discards a valid resolution.
        return SettingsResolution(None, tuple(reasons))
    return SettingsResolution(
        EffectiveResourceSettings(
            kind,
            context,
            concurrency,
            batch,
            ParallelismSettings(world_size, tensor, pipeline, data, backend),
            knobs,
            effects,
            digest,
        ),
        (),
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
        publisher = model_ref.get("publisher")
        slug = model_ref.get("slug")
        content_sha256 = model_ref.get("content_sha256")
        if (
            not isinstance(publisher, str)
            or not publisher
            or not isinstance(slug, str)
            or not slug
            or not isinstance(content_sha256, str)
            or not content_sha256
        ):
            return None
        model_document = model_documents.get((publisher, slug, content_sha256))
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
            if (
                isinstance(roles, Sequence)
                and not isinstance(roles, (str, bytes))
                and role_name in roles
            ):
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
    role_name: str,
    model_documents: Mapping[tuple[str, str, str], Mapping[str, object]] | None,
    declared_total_bytes: int,
    settings: object | None,
) -> ResourceEvidence:
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
                "resource.evidence_invalid",
                "Declared recipe-role memory envelope is invalid.",
                node_id=node_id,
            )
        )
    if evidence.evidence_state in {"unknown", "stale"}:
        if declared_bound is None:
            reasons.append(
                _reason(
                    "resource.evidence_unknown",
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
                "resource.evidence_invalid",
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
                        "resource.evidence_unknown",
                        f"{name} is missing and no declared role bound is available.",
                        node_id=node_id,
                    )
                )
            else:
                uncertain.append(name)
        elif type(item) is not int or item < 0:
            reasons.append(
                _reason(
                    "resource.evidence_invalid",
                    f"{name} is invalid; resource evidence cannot be trusted.",
                    node_id=node_id,
                )
            )
    context = _term(
        "context",
        selected.context_tokens,
        evidence.baseline_context_tokens,
        evidence.context_bytes_per_token,
        evidence.supported_context_tokens,
        node_id,
        required=selected.context_tokens is not None,
    )
    concurrency = _term(
        "concurrency",
        selected.concurrency,
        evidence.baseline_concurrency,
        evidence.concurrency_bytes_per_request,
        evidence.supported_concurrency,
        node_id,
        required=selected.concurrency is not None,
    )
    batch = _term(
        "batch",
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
                "resource.estimate_uncertain",
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
                    "resource.stop_release_unknown",
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
                    "resource.capacity_unknown",
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
                )
            )
            continue
        occupied = capacity.occupied_bytes
        reserved = capacity.reserved_bytes
        unmaterialized = capacity.unmaterialized_bytes
        for name, value in (
            ("available", available),
            ("occupied", occupied),
            ("reserved", reserved),
            ("unmaterialized", unmaterialized),
        ):
            if type(value) is not int or value < 0:
                node_reasons.append(
                    _reason(
                        "resource.capacity_unknown",
                        f"Current {name} capacity evidence is missing or invalid.",
                        node_id=node_id,
                    )
                )
        if capacity.evidence_state in {"unknown", "stale"}:
            node_reasons.append(
                _reason(
                    "resource.capacity_unknown",
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
        # The exact peak commitment owns the hard admission budget. Only
        # promises that have not materialized reduce observed free bytes: a
        # live run's use is already part of ``occupied`` and must not be
        # charged against free a second time.
        current = available - occupied - unmaterialized - total_bytes
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
                    "resource.insufficient_reservation_budget",
                    f"Exact memory commitments plus demand and reserve exceed the physical pool by {-budget_after} bytes.",
                    node_id=node_id,
                )
            )
        if current < memory_floor_bytes and not release:
            node_reasons.append(
                _reason(
                    "resource.insufficient_capacity",
                    f"Selected demand leaves {current} bytes before the required {memory_floor_bytes}-byte reserve.",
                    node_id=node_id,
                )
            )
        elif release and after_stop < memory_floor_bytes:
            node_reasons.append(
                _reason(
                    "resource.insufficient_capacity_after_stop",
                    f"Selected demand leaves {selected} bytes after planned stops; {memory_floor_bytes} bytes must remain reserved.",
                    node_id=node_id,
                )
            )
        nodes.append(
            NodeCapacityPlan(
                node_id,
                demand.total_bytes,
                current,
                after_stop,
                selected,
                not current_fit and after_fit and release > 0,
                allowed,
                tuple(node_reasons),
                (capacity.memory_kind,) if not current_fit else (),
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
    parameter_effects: Mapping[str, str] | None = None,
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
    name: str,
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
                        f"resource.{name}_unknown",
                        f"Effective {name} setting is unavailable; capacity cannot be predicted.",
                        node_id=node_id,
                    ),
                ),
            )
        )
    if baseline is not None and (type(baseline) is not int or baseline < 0):
        return None, (
            _reason(
                f"resource.{name}_evidence_invalid",
                f"Measured baseline evidence for {name} is invalid.",
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
                f"resource.{name}_evidence_invalid",
                f"Declared supported range for {name} is invalid.",
                node_id=node_id,
            ),
        )
    if supported is not None and (value < supported[0] or value > supported[1]):
        return None, (
            _reason(
                f"resource.{name}_unsupported",
                f"Effective {name} setting is outside the declared supported range.",
                node_id=node_id,
            ),
        )
    if baseline is None:
        return None, (
            _reason(
                f"resource.{name}_evidence_unknown",
                f"No baseline evidence is declared for effective {name}.",
                node_id=node_id,
            ),
        )
    if value == baseline:
        return 0, ()
    if coefficient is None:
        return None, (
            _reason(
                f"resource.{name}_evidence_unknown",
                f"No evidence supports changing effective {name} from its measured baseline.",
                node_id=node_id,
            ),
        )
    if type(coefficient) is not int or coefficient < 0:
        return None, (
            _reason(
                f"resource.{name}_evidence_invalid",
                f"Measured coefficient for {name} is invalid.",
                node_id=node_id,
            ),
        )
    return max(0, value - baseline) * coefficient, ()


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        return dumped if isinstance(dumped, Mapping) else None
    if isinstance(value, _EffectiveSettingsProjection):
        parallel = value.parallelism
        return {
            "settings": {
                "kind": value.kind,
                "context_tokens": value.context_tokens,
                "concurrency": value.concurrency,
                "max_batch_tokens": value.batch_tokens,
                "knobs": dict(value.knobs),
                "change_effects": dict(value.change_effects),
            },
            "topology": {
                "node_count": parallel.world_size,
                "parallelism": {
                    "world_size": parallel.world_size,
                    "tensor": parallel.tensor,
                    "pipeline": parallel.pipeline,
                    "data": parallel.data,
                    "backend": parallel.backend,
                },
            },
            "identity_sha256": value.identity_digest,
        }
    return None


def _effect(value: object) -> RunSwitchChangeEffect | None:
    candidate = getattr(value, "value", value)
    return candidate if candidate in _CHANGE_EFFECTS else None


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
