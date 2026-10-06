"""Pure, canonical impact plans for destructive recipe actions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from vonk_agent_protocol import (
    StopPlanCode,
    UninstallPlanCode,
    canonical_message,
)

SharedCachePolicy = Literal["retain-shared-download-cache"]


@dataclass(frozen=True, slots=True)
class ActionReason:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class StopNodeImpact:
    node_id: str
    rank: int
    role: str
    state: str
    reserved_memory_bytes: int
    active_memory_reservation_bytes: int


@dataclass(frozen=True, slots=True)
class StopPlan:
    run_id: str
    installation_id: str
    recipe_revision_id: str
    alias: str
    run_state: str
    route_state: str
    route_generation: int | None
    route_digest: str | None
    authority_digest: str
    allowed: bool
    route_withdrawal: bool
    nodes: tuple[StopNodeImpact, ...]
    # Full original membership remains in ``nodes``. These ordered sets bind
    # the subset receiving Stop jobs and any ranks absent from the live fleet.
    target_node_ids: tuple[str, ...]
    missing_node_ids: tuple[str, ...]
    total_active_memory_reservation_bytes: int
    blockers: tuple[ActionReason, ...]
    warnings: tuple[ActionReason, ...]
    plan_digest: str


@dataclass(frozen=True, slots=True)
class UninstallNodeImpact:
    node_id: str
    rank: int
    role: str
    state: str
    # Bytes the membership row records on its Spark.  A ``planned`` row that
    # already recorded bytes contradicts its own plan, so it can never be
    # treated as a never-installed leftover.
    installed_bytes: int | None


@dataclass(frozen=True, slots=True)
class UninstallActiveRun:
    run_id: str
    alias: str
    state: str
    route_state: str


@dataclass(frozen=True, slots=True)
class UninstallConsequences:
    catalog_retained: bool = True
    automatic_stop: bool = False
    reinstall_required: bool = True


@dataclass(frozen=True, slots=True)
class UninstallModelImpact:
    model_content_sha256: str
    model_title: str
    effect: str
    dependent_recipe_ids: tuple[str, ...]
    cleanup_node_ids: tuple[str, ...]
    retained_node_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UninstallPlan:
    installation_id: str
    recipe_id: str
    recipe_revision_id: str
    recipe_content_sha256: str
    recipe_content: dict[str, object]
    installation_authority_digest: str
    original_plan_digest: str
    installation_state: str
    allowed: bool
    # A persistence-only plan that never reached a node is abandoned rather
    # than uninstalled: there are no installed bytes to remove.  ``blockers``
    # stays authoritative for both dispositions.
    disposition: Literal["uninstall", "abandon"]
    nodes: tuple[UninstallNodeImpact, ...]
    bytes_removed: int | None
    active_runs: tuple[UninstallActiveRun, ...]
    active_run_count: int
    active_runs_truncated: bool
    blockers: tuple[ActionReason, ...]
    warnings: tuple[ActionReason, ...]
    consequences: UninstallConsequences
    model_impact: UninstallModelImpact
    plan_digest: str


def stop_plan(
    *,
    run_id: str,
    installation_id: str,
    recipe_revision_id: str,
    alias: str,
    run_state: str,
    route_state: str,
    route_generation: int | None,
    route_digest: str | None,
    authority_digest: str,
    nodes: Sequence[StopNodeImpact],
    target_node_ids: Sequence[str] | None = None,
    missing_node_ids: Sequence[str] = (),
    immutable_membership_exact: bool,
    reservation_membership_exact: bool,
    reservation_facts: Sequence[Mapping[str, object]],
) -> StopPlan:
    """Build one stop impact plan; human copy is excluded from its digest."""

    ordered_nodes = tuple(sorted(nodes, key=lambda item: (item.rank, item.node_id)))
    full_node_ids = tuple(sorted(node.node_id for node in ordered_nodes))
    target_ids = tuple(
        sorted(target_node_ids if target_node_ids is not None else full_node_ids)
    )
    missing_ids = tuple(sorted(missing_node_ids))
    stop_scope_exact = (
        len(set(target_ids)) == len(target_ids)
        and len(set(missing_ids)) == len(missing_ids)
        and set(target_ids).isdisjoint(missing_ids)
        and set(target_ids) | set(missing_ids) == set(full_node_ids)
        and bool(target_ids)
    )
    blockers: list[ActionReason] = []
    if run_state not in {"starting", "running", "stopping", "failed", "lost"}:
        blockers.append(
            ActionReason(
                StopPlanCode.RUN_NOT_STOPPABLE,
                f"Run state {run_state} cannot accept a stop operation.",
            )
        )
    if not immutable_membership_exact or not ordered_nodes:
        blockers.append(
            ActionReason(
                StopPlanCode.RANK_MEMBERSHIP_CHANGED,
                "Persisted ranks no longer match the immutable accepted run plan.",
            )
        )
    if not stop_scope_exact:
        blockers.append(
            ActionReason(
                StopPlanCode.TARGET_SCOPE_CHANGED,
                "Stop targets and missing ranks do not partition the accepted group.",
            )
        )
    if not reservation_membership_exact:
        blockers.append(
            ActionReason(
                StopPlanCode.RESERVATION_MEMBERSHIP_CHANGED,
                "Active run reservations include a node outside the accepted rank group.",
            )
        )
    identity = {
        "schema_version": 1,
        "action": "recipe.stop",
        "owner": {"kind": "run", "id": run_id},
        "installation_id": installation_id,
        "recipe_revision_id": recipe_revision_id,
        "authority_digest": authority_digest,
        "run_state": run_state,
        # Route generation and digest are descriptive publication metadata.
        # They change for every route candidate, including one that withdraws
        # a different run. Keep the route state itself in the authority so a
        # real state change remains stale, while independent stop operations
        # can serialize without invalidating one another's previews.
        "route": {
            "state": route_state,
        },
        "nodes": [
            {
                "node_id": node.node_id,
                "rank": node.rank,
                "role": node.role,
                "state": node.state,
                "reserved_memory_bytes": node.reserved_memory_bytes,
                "active_memory_reservation_bytes": (
                    node.active_memory_reservation_bytes
                ),
            }
            for node in ordered_nodes
        ],
        "target_node_ids": list(target_ids),
        "missing_node_ids": list(missing_ids),
        "active_memory_reservations": list(reservation_facts),
        "immutable_membership_exact": immutable_membership_exact,
        "reservation_membership_exact": reservation_membership_exact,
    }
    digest = hashlib.sha256(canonical_message(identity)).hexdigest()
    return StopPlan(
        run_id=run_id,
        installation_id=installation_id,
        recipe_revision_id=recipe_revision_id,
        alias=alias,
        run_state=run_state,
        route_state=route_state,
        route_generation=route_generation,
        route_digest=route_digest,
        authority_digest=authority_digest,
        allowed=not blockers,
        route_withdrawal=True,
        nodes=ordered_nodes,
        target_node_ids=target_ids,
        missing_node_ids=missing_ids,
        total_active_memory_reservation_bytes=sum(
            node.active_memory_reservation_bytes for node in ordered_nodes
        ),
        blockers=tuple(blockers),
        warnings=(
            ActionReason(
                StopPlanCode.CAPACITY_RELEASE_DEFERRED,
                "Capacity remains reserved until every selected rank stops successfully.",
            ),
        ),
        plan_digest=digest,
    )


def uninstall_plan(
    *,
    installation_id: str,
    recipe_id: str,
    recipe_revision_id: str,
    recipe_content_sha256: str,
    recipe_content: Mapping[str, object],
    original_plan_digest: str,
    installation_state: str,
    nodes: Sequence[UninstallNodeImpact],
    immutable_membership_exact: bool,
    active_runs: Sequence[UninstallActiveRun],
    active_run_count: int,
    active_runs_truncated: bool,
    active_operation: bool,
    model_content_sha256: str,
    model_title: str,
    dependent_recipe_ids_by_node: Mapping[str, Sequence[str]],
) -> UninstallPlan:
    """Bind exact cleanup authority while reporting unknown reclaimable bytes."""

    ordered_nodes = tuple(sorted(nodes, key=lambda item: (item.rank, item.node_id)))
    ordered_runs = tuple(sorted(active_runs, key=lambda item: item.run_id))
    node_ids = {node.node_id for node in ordered_nodes}
    if set(dependent_recipe_ids_by_node) != node_ids:
        raise ValueError("model dependency evidence must cover every uninstall node")
    dependents_by_node = {
        node_id: tuple(sorted(set(dependent_recipe_ids_by_node[node_id])))
        for node_id in sorted(node_ids)
    }
    ordered_dependents = tuple(
        sorted(
            {
                recipe_id
                for recipe_ids in dependents_by_node.values()
                for recipe_id in recipe_ids
            }
        )
    )
    cleanup_node_ids = tuple(
        node_id for node_id, recipe_ids in dependents_by_node.items() if not recipe_ids
    )
    retained_node_ids = tuple(
        node_id for node_id, recipe_ids in dependents_by_node.items() if recipe_ids
    )
    model_effect = (
        "recipe-and-unused-model"
        if len(cleanup_node_ids) == len(ordered_nodes)
        else "recipe-and-partial-model-cleanup"
        if cleanup_node_ids
        else "recipe-only"
    )
    canonical_content = json.loads(canonical_message(recipe_content))
    # A plan that was persisted but never applied has nothing on any node to
    # remove.  It may be abandoned while the installation itself is still
    # ``planned`` (an install moves it on before anything reaches a Spark) and
    # every membership row it has proves that: each rank is ``planned`` and none
    # recorded installed bytes.  Whether the membership still matches the plan's
    # ranks does not matter here: a plan that never started has no effect to
    # misjudge, and a rank row that went missing must not keep the plan, its
    # disk claim and an "incomplete installation" warning for ever.  A rank that
    # shows any effect keeps the row on the ordinary uninstall path, where the
    # integrity check above reports it.
    never_installed = bool(
        installation_state == "planned"
        and ordered_nodes
        and all(
            node.state == "planned" and node.installed_bytes in (None, 0)
            for node in ordered_nodes
        )
    )
    disposition: Literal["uninstall", "abandon"] = (
        "abandon" if never_installed else "uninstall"
    )
    bytes_known = disposition == "abandon" or (
        installation_state == "installed"
        and bool(ordered_nodes)
        and immutable_membership_exact
        and all(
            node.state == "installed" and node.installed_bytes is not None
            for node in ordered_nodes
        )
    )
    bytes_removed = (
        0
        if disposition == "abandon"
        else sum(node.installed_bytes or 0 for node in ordered_nodes)
        if bytes_known
        else None
    )
    blockers: list[ActionReason] = []
    if disposition != "abandon" and installation_state not in {
        "installed",
        "partial",
        "failed",
    }:
        blockers.append(
            ActionReason(
                UninstallPlanCode.INSTALLATION_NOT_UNINSTALLABLE,
                f"Installation state {installation_state} cannot be uninstalled.",
            )
        )
    if (disposition != "abandon" and not immutable_membership_exact) or (
        not ordered_nodes
    ):
        blockers.append(
            ActionReason(
                UninstallPlanCode.RANK_MEMBERSHIP_CHANGED,
                "Persisted nodes no longer match the immutable installation plan.",
            )
        )
    if active_run_count:
        blockers.append(
            ActionReason(
                UninstallPlanCode.ACTIVE_RUN,
                f"{active_run_count} active run(s) must be stopped explicitly first.",
            )
        )
    if active_runs_truncated:
        blockers.append(
            ActionReason(
                UninstallPlanCode.ACTIVE_RUNS_TRUNCATED,
                "The bounded active-run list is incomplete; uninstall remains blocked.",
            )
        )
    if active_operation:
        blockers.append(
            ActionReason(
                UninstallPlanCode.OPERATION_ACTIVE,
                "This installation already has an active uninstall operation.",
            )
        )
    identity = {
        "schema_version": 1,
        "action": "recipe.uninstall",
        "owner": {"kind": "installation", "id": installation_id},
        "recipe": {
            "id": recipe_id,
            "revision_id": recipe_revision_id,
            "content_sha256": recipe_content_sha256,
            "content": canonical_content,
        },
        "installation_authority_digest": recipe_content_sha256,
        "original_plan_digest": original_plan_digest,
        "installation_state": installation_state,
        "disposition": disposition,
        "nodes": [
            {
                "node_id": node.node_id,
                "rank": node.rank,
                "role": node.role,
                "state": node.state,
                "installed_bytes": node.installed_bytes,
            }
            for node in ordered_nodes
        ],
        "immutable_membership_exact": immutable_membership_exact,
        "bytes_removed": bytes_removed,
        "active_runs": [
            {
                "run_id": run.run_id,
                "state": run.state,
                "route_state": run.route_state,
            }
            for run in ordered_runs
        ],
        "active_run_count": active_run_count,
        "active_runs_truncated": active_runs_truncated,
        "active_operation": active_operation,
        "model_impact": {
            "model_content_sha256": model_content_sha256,
            "effect": model_effect,
            "dependent_recipe_ids": list(ordered_dependents),
            "dependent_recipe_ids_by_node": dependents_by_node,
            "cleanup_node_ids": list(cleanup_node_ids),
            "retained_node_ids": list(retained_node_ids),
        },
    }
    digest = hashlib.sha256(canonical_message(identity)).hexdigest()
    warnings: list[ActionReason] = []
    if disposition == "abandon":
        warnings.append(
            ActionReason(
                UninstallPlanCode.ABANDON_NEVER_INSTALLED,
                "The persisted plan never reached a node; it is abandoned "
                "rather than uninstalled.",
            )
        )
    elif not bytes_known:
        warnings.append(
            ActionReason(
                UninstallPlanCode.BYTES_UNKNOWN,
                "Reclaimable bytes are unknown; cleanup remains scoped to this installation.",
            )
        )
    return UninstallPlan(
        installation_id=installation_id,
        recipe_id=recipe_id,
        recipe_revision_id=recipe_revision_id,
        recipe_content_sha256=recipe_content_sha256,
        recipe_content=canonical_content,
        installation_authority_digest=recipe_content_sha256,
        original_plan_digest=original_plan_digest,
        installation_state=installation_state,
        allowed=not blockers,
        disposition=disposition,
        nodes=ordered_nodes,
        bytes_removed=bytes_removed,
        active_runs=ordered_runs,
        active_run_count=active_run_count,
        active_runs_truncated=active_runs_truncated,
        blockers=tuple(blockers),
        warnings=tuple(warnings),
        consequences=UninstallConsequences(),
        model_impact=UninstallModelImpact(
            model_content_sha256=model_content_sha256,
            model_title=model_title,
            effect=model_effect,
            dependent_recipe_ids=ordered_dependents,
            cleanup_node_ids=cleanup_node_ids,
            retained_node_ids=retained_node_ids,
        ),
        plan_digest=digest,
    )


__all__ = [
    "ActionReason",
    "SharedCachePolicy",
    "StopNodeImpact",
    "StopPlan",
    "UninstallActiveRun",
    "UninstallConsequences",
    "UninstallModelImpact",
    "UninstallNodeImpact",
    "UninstallPlan",
    "stop_plan",
    "uninstall_plan",
]
