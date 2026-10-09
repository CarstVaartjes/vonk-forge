"""Resource planning: memory."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from vonk_agent_protocol.inventory import MemoryPool
from vonk_forge_contracts import ModelDefinition
from vonk_forge_contracts.recipe import (
    RecipeMemoryResources,
)

from ..run_switch_contract import (
    MemoryKind,
)
from .demand import _resource_evidence, resource_demand
from .memory_kinds import memory_reservation_kind, memory_reservation_kinds
from .settings import resolve_effective_settings
from .types import (
    CapacitySnapshot,
    EffectiveResourceSettings,
    EvidenceState,
    MemoryRequirement,
    MemoryReservationTotals,
    SettingsResolution,
)


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
