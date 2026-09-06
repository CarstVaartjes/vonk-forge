"""Evidence based memory planning for canonical recipe settings.

The public RecipeDefinition owns settings and topology.  This module consumes
that typed projection and never invents an engine memory formula.  Context and
concurrency terms are used only when the selected serving kind has the setting
and an explicit measured/declarative evidence term.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

EvidenceState = Literal["declared", "measured", "fresh", "stale", "unknown"]
Effect = Literal["reuse", "restart", "reprepare", "reinstall", "rebuild"]


@dataclass(frozen=True, slots=True)
class ResourceReason:
    code: str
    detail: str
    severity: Literal["blocker", "warning"] = "blocker"
    node_id: str | None = None


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
class CapacitySnapshot:
    node_id: str
    memory_kind: str
    available_bytes: int | None
    occupied_bytes: int | None
    reserved_bytes: int | None
    evidence_state: EvidenceState = "unknown"
    evidence_digest: str | None = None


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
            and not self.reasons
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


def resolve_effective_settings(value: Mapping[str, object] | object) -> SettingsResolution:
    """Resolve one canonical RecipeDefinition or its typed settings projection."""

    raw = _as_mapping(value)
    if raw is None:
        return SettingsResolution(None, (_reason("resource.settings_unknown", "Canonical effective settings are unavailable."),))
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
        reasons.append(_reason("resource.settings_kind_unknown", "Canonical settings kind is missing or unsupported."))
        kind = "job"

    def positive(name: str, optional: bool = True) -> int | None:
        value, _ = setting(name)
        if value is None and optional:
            return None
        if type(value) is not int or value < 1:
            reasons.append(_reason("resource.settings_type", f"Canonical {name} must be a positive integer."))
            return None
        return value

    context = positive("context_tokens", optional=kind != "generation")
    concurrency = positive("concurrency")
    batch = positive("max_batch_tokens")
    knobs: dict[str, object] = {}
    effects: dict[str, str] = {}
    raw_knobs = settings.get("knobs", {})
    if not isinstance(raw_knobs, Mapping):
        reasons.append(_reason("resource.knobs_invalid", "Canonical settings knobs are invalid."))
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
        reasons.append(_reason("resource.parallelism_unknown", "Canonical topology parallelism is unavailable."))
        parallel = {}
    if "parallelism" in settings:
        reasons.append(_reason("resource.parallelism_duplicate", "Parallelism is owned by topology and cannot be repeated in settings."))
    dimensions: dict[str, int | None] = {}
    for name in ("tensor", "pipeline", "data"):
        value = parallel.get(name)
        if type(value) is not int or value < 1:
            reasons.append(_reason("resource.parallelism_type", f"Canonical topology parallelism {name} is invalid."))
            dimensions[name] = None
        else:
            dimensions[name] = value
    node_count = topology_map.get("node_count") if topology_map else None
    if type(node_count) is not int or node_count < 1:
        reasons.append(_reason("resource.parallelism_type", "Canonical topology node_count is invalid."))
        node_count = None
    world_size = parallel.get("world_size")
    if type(world_size) is not int or world_size < 1:
        reasons.append(_reason("resource.parallelism_type", "Canonical topology parallelism world_size is invalid."))
        world_size = None
    if all(dimensions[name] is not None for name in ("tensor", "pipeline", "data")):
        product = dimensions["tensor"] * dimensions["pipeline"] * dimensions["data"]
        if world_size is not None and product != world_size:
            reasons.append(_reason("resource.parallelism_inconsistent", "Topology parallelism product does not equal declared world_size."))
        if node_count is not None and world_size is not None and world_size != node_count:
            reasons.append(_reason("resource.parallelism_inconsistent", "Declared parallelism world_size does not equal node_count."))
    backend = parallel.get("backend")
    if not isinstance(backend, str) or not backend:
        reasons.append(_reason("resource.parallelism_type", "Canonical topology parallelism backend is invalid."))
        backend = "unknown"
    if reasons:
        return SettingsResolution(None, tuple(reasons))
    identity = {
        "kind": kind,
        "context_tokens": context,
        "concurrency": concurrency,
        "max_batch_tokens": batch,
        "parallelism": {"world_size": world_size, "tensor": dimensions["tensor"], "pipeline": dimensions["pipeline"], "data": dimensions["data"], "backend": backend},
        "knobs": _canonical(knobs),
    }
    canonical_digest = raw.get("identity_sha256")
    digest = canonical_digest if _is_digest(canonical_digest) else _digest(identity)
    return SettingsResolution(
        EffectiveResourceSettings(
            kind, context, concurrency, batch,
            ParallelismSettings(world_size, dimensions["tensor"], dimensions["pipeline"], dimensions["data"], backend),
            knobs, effects, digest,
        ),
        (),
    )


def resource_demand(settings: EffectiveResourceSettings | object, evidence: ResourceEvidence, *, node_id: str | None = None) -> ResourceDemand:
    resolution = SettingsResolution(settings, ()) if isinstance(settings, EffectiveResourceSettings) else resolve_effective_settings(settings)
    reasons = list(resolution.reasons)
    if resolution.settings is None:
        return ResourceDemand(None, None, None, None, None, None, "unknown", tuple(reasons))
    selected = resolution.settings
    if evidence.evidence_state in {"unknown", "stale"}:
        reasons.append(_reason("resource.evidence_unknown", "Memory evidence is missing or stale for the selected settings.", node_id=node_id))
    if evidence.evidence_digest is not None and not _is_digest(evidence.evidence_digest):
        reasons.append(_reason("resource.evidence_invalid", "Memory evidence digest is invalid.", node_id=node_id))
    for name, item in (("weights_bytes", evidence.weights_bytes), ("runtime_overhead_bytes", evidence.runtime_overhead_bytes)):
        if (type(item) is not int or item < 0) and not (type(evidence.declared_total_bytes) is int and evidence.declared_total_bytes >= 0):
            reasons.append(_reason("resource.evidence_unknown", f"{name} is missing or invalid; capacity cannot be predicted.", node_id=node_id))
    context = _term("context", selected.context_tokens, evidence.baseline_context_tokens, evidence.context_bytes_per_token, evidence.supported_context_tokens, node_id, required=selected.context_tokens is not None)
    concurrency = _term("concurrency", selected.concurrency, evidence.baseline_concurrency, evidence.concurrency_bytes_per_request, evidence.supported_concurrency, node_id, required=selected.concurrency is not None)
    batch = _term("batch", selected.batch_tokens, evidence.baseline_batch_tokens, evidence.batch_bytes_per_token, evidence.supported_batch_tokens, node_id, required=selected.batch_tokens is not None)
    for term in (context, concurrency, batch):
        reasons.extend(term[1])
    total: int | None = None
    if not reasons and all(isinstance(term[0], int) for term in (context, concurrency, batch)):
        base = evidence.declared_total_bytes
        if base is None and type(evidence.weights_bytes) is int and type(evidence.runtime_overhead_bytes) is int:
            base = evidence.weights_bytes + evidence.runtime_overhead_bytes
        if base is not None:
            total = base + int(context[0]) + int(concurrency[0]) + int(batch[0])
    return ResourceDemand(
        evidence.weights_bytes if type(evidence.weights_bytes) is int else None,
        evidence.runtime_overhead_bytes if type(evidence.runtime_overhead_bytes) is int else None,
        context[0], concurrency[0], batch[0], total, evidence.evidence_state, tuple(reasons),
    )


def plan_capacity(requirements: Mapping[str, ResourceDemand], capacities: Sequence[CapacitySnapshot], planned_stops: Sequence[PlannedStopRelease] = (), *, memory_floor_bytes: int = 0) -> CapacityPlan:
    reasons: list[ResourceReason] = []
    by_node = {item.node_id: item for item in capacities}
    releases: dict[tuple[str, str], int] = {}
    for stop in planned_stops:
        if not stop.planned:
            continue
        if type(stop.release_bytes) is not int or stop.release_bytes < 0:
            reasons.append(_reason("resource.stop_release_unknown", "A planned stop has no valid capacity release evidence.", node_id=stop.node_id))
            continue
        releases[(stop.node_id, stop.memory_kind)] = releases.get((stop.node_id, stop.memory_kind), 0) + stop.release_bytes
    nodes: list[NodeCapacityPlan] = []
    for node_id, demand in requirements.items():
        node_reasons = list(demand.reasons)
        capacity = by_node.get(node_id)
        if capacity is None:
            node_reasons.append(_reason("resource.capacity_unknown", "Capacity evidence is unavailable for the selected rank.", node_id=node_id))
            nodes.append(NodeCapacityPlan(node_id, demand.total_bytes, None, None, None, False, False, tuple(node_reasons)))
            continue
        for name, value in (("available", capacity.available_bytes), ("occupied", capacity.occupied_bytes), ("reserved", capacity.reserved_bytes)):
            if type(value) is not int or value < 0:
                node_reasons.append(_reason("resource.capacity_unknown", f"Current {name} capacity evidence is missing or invalid.", node_id=node_id))
        if capacity.evidence_state in {"unknown", "stale"}:
            node_reasons.append(_reason("resource.capacity_unknown", "Current capacity evidence is missing or stale.", node_id=node_id))
        if demand.total_bytes is None or node_reasons:
            nodes.append(NodeCapacityPlan(node_id, demand.total_bytes, None, None, None, False, False, tuple(node_reasons)))
            continue
        current = capacity.available_bytes - capacity.occupied_bytes - capacity.reserved_bytes - demand.total_bytes
        release = releases.get((node_id, capacity.memory_kind), 0)
        if not release:
            release = max((value for (candidate, kind), value in releases.items() if candidate == node_id and _same_memory_kind(kind, capacity.memory_kind)), default=0)
        after_stop = current + release
        current_fit = current >= memory_floor_bytes
        after_fit = after_stop >= memory_floor_bytes
        selected = after_stop if release else current
        allowed = selected >= memory_floor_bytes
        if not allowed:
            node_reasons.append(_reason("resource.insufficient_capacity_after_stop" if release else "resource.insufficient_capacity", f"Selected settings leave {selected} bytes after planned stops.", node_id=node_id))
        nodes.append(NodeCapacityPlan(node_id, demand.total_bytes, current, after_stop, selected, not current_fit and after_fit and release > 0, allowed, tuple(node_reasons)))
    reasons.extend(reason for node in nodes for reason in node.reasons)
    return CapacityPlan(tuple(nodes), bool(nodes) and not reasons and all(node.allowed for node in nodes), any(node.stop_required for node in nodes), tuple(reasons))


def plan_resource_preflight(effective_context: Mapping[str, object] | object, evidence_by_node: Mapping[str, ResourceEvidence], capacities: Sequence[CapacitySnapshot], planned_stops: Sequence[PlannedStopRelease] = (), *, memory_floor_bytes: int = 0) -> ResourcePreflightPlan:
    resolution = resolve_effective_settings(effective_context)
    if resolution.settings is None:
        return ResourcePreflightPlan(None, {}, None, resolution.reasons)
    demands = {node_id: resource_demand(resolution.settings, evidence, node_id=node_id) for node_id, evidence in evidence_by_node.items()}
    capacity = plan_capacity(demands, capacities, planned_stops, memory_floor_bytes=memory_floor_bytes)
    return ResourcePreflightPlan(resolution.settings, demands, capacity, (*resolution.reasons, *capacity.reasons))


def classify_preparation_effects(previous: EffectiveResourceSettings | object | None, current: EffectiveResourceSettings | object, *, parameter_effects: Mapping[str, str] | None = None) -> PreparationDecision:
    current_resolution = current if isinstance(current, EffectiveResourceSettings) else resolve_effective_settings(current).settings
    if current_resolution is None:
        raise ValueError("effective settings are invalid")
    previous_resolution = previous if isinstance(previous, EffectiveResourceSettings) else resolve_effective_settings(previous).settings if previous is not None else None
    current_identity = current_resolution.identity()
    previous_identity = previous_resolution.identity() if previous_resolution is not None else None
    changed = {key for key in current_identity if previous_identity is None or current_identity[key] != previous_identity.get(key)}
    effects = dict(current_resolution.change_effects)
    effects.update(parameter_effects or {})
    if any(effect not in {"none", "restart", "reprepare", "reinstall", "rebuild"} for effect in effects.values()):
        raise ValueError("parameter change effects are invalid")
    active = {key: effect for key, effect in effects.items() if effect != "none"}
    rebuild = "rebuild" in active.values()
    reinstall = rebuild or "reinstall" in active.values() or "parallelism" in changed
    reprepare = reinstall or bool(changed & {"context_tokens", "concurrency", "batch_tokens", "knobs", "kind"}) or "reprepare" in active.values()
    restart = reprepare or bool(changed) or "restart" in active.values()
    effect: Effect = "rebuild" if rebuild else "reinstall" if reinstall else "reprepare" if reprepare else "restart" if restart else "reuse"
    return PreparationDecision(effect, tuple(sorted(changed | set(active))), bool(changed or active), restart, reprepare, reinstall, rebuild, current_resolution.identity_digest or _digest(current_identity))


def _term(name: str, value: int | None, baseline: int | None, coefficient: int | None, supported: tuple[int, int] | None, node_id: str | None, *, required: bool) -> tuple[int | None, tuple[ResourceReason, ...]]:
    if value is None:
        return (0, ()) if not required else (None, (_reason(f"resource.{name}_unknown", f"Effective {name} setting is unavailable; capacity cannot be predicted.", node_id=node_id),))
    if supported is not None and (len(supported) != 2 or value < supported[0] or value > supported[1]):
        return None, (_reason(f"resource.{name}_unsupported", f"Effective {name} setting is outside the declared supported range.", node_id=node_id),)
    if baseline is None:
        return None, (_reason(f"resource.{name}_evidence_unknown", f"No baseline evidence is declared for effective {name}.", node_id=node_id),)
    if value == baseline:
        return 0, ()
    if type(coefficient) is not int or coefficient < 0:
        return None, (_reason(f"resource.{name}_evidence_unknown", f"No evidence supports changing effective {name} from its measured baseline.", node_id=node_id),)
    return max(0, value - baseline) * coefficient, ()


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        return dumped if isinstance(dumped, Mapping) else None
    if all(hasattr(value, name) for name in ("context_tokens", "concurrency", "parallelism", "kind")):
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
            "topology": {"node_count": parallel.world_size, "parallelism": {"world_size": parallel.world_size, "tensor": parallel.tensor, "pipeline": parallel.pipeline, "data": parallel.data, "backend": parallel.backend}},
            "identity_sha256": value.identity_digest,
        }
    return None


def _effect(value: object) -> str | None:
    value = getattr(value, "value", value)
    return value if value in {"none", "restart", "reprepare", "reinstall", "rebuild"} else None


def _same_memory_kind(left: str, right: str) -> bool:
    return {left, right} <= {"unified", "unified-memory"} or {left, right} <= {"host", "host-memory"} or {left, right} <= {"accelerator", "gpu-memory"}


def _reason(code: str, detail: str, *, node_id: str | None = None) -> ResourceReason:
    return ResourceReason(code, detail, node_id=node_id)


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical(item) for item in value]
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(_canonical(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)
