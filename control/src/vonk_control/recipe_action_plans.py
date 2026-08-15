"""Pure, canonical impact plans for destructive recipe actions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from vonk_agent_protocol import canonical_message


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
    nodes: tuple[UninstallNodeImpact, ...]
    bytes_removed: int | None
    active_runs: tuple[UninstallActiveRun, ...]
    active_run_count: int
    active_runs_truncated: bool
    blockers: tuple[ActionReason, ...]
    warnings: tuple[ActionReason, ...]
    consequences: UninstallConsequences
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
    immutable_membership_exact: bool,
    reservation_membership_exact: bool,
    reservation_facts: Sequence[Mapping[str, object]],
) -> StopPlan:
    """Build one stop impact plan; human copy is excluded from its digest."""

    ordered_nodes = tuple(sorted(nodes, key=lambda item: (item.rank, item.node_id)))
    blockers: list[ActionReason] = []
    if run_state not in {"starting", "running", "failed", "lost"}:
        blockers.append(
            ActionReason(
                "stop.run_not_stoppable",
                f"Run state {run_state} cannot accept a stop operation.",
            )
        )
    if not immutable_membership_exact or not ordered_nodes:
        blockers.append(
            ActionReason(
                "stop.rank_membership_changed",
                "Persisted ranks no longer match the immutable accepted run plan.",
            )
        )
    if not reservation_membership_exact:
        blockers.append(
            ActionReason(
                "stop.reservation_membership_changed",
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
        total_active_memory_reservation_bytes=sum(
            node.active_memory_reservation_bytes for node in ordered_nodes
        ),
        blockers=tuple(blockers),
        warnings=(
            ActionReason(
                "stop.capacity_release_deferred",
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
) -> UninstallPlan:
    """Build one uninstall impact plan with fail-closed byte semantics."""

    ordered_nodes = tuple(sorted(nodes, key=lambda item: (item.rank, item.node_id)))
    ordered_runs = tuple(sorted(active_runs, key=lambda item: item.run_id))
    canonical_content = json.loads(canonical_message(recipe_content))
    bytes_known = (
        installation_state == "installed"
        and bool(ordered_nodes)
        and immutable_membership_exact
        and all(
            node.state == "installed" and node.installed_bytes is not None
            for node in ordered_nodes
        )
    )
    bytes_removed = (
        sum(node.installed_bytes or 0 for node in ordered_nodes)
        if bytes_known
        else None
    )
    blockers: list[ActionReason] = []
    if installation_state not in {"installed", "partial", "failed"}:
        blockers.append(
            ActionReason(
                "uninstall.installation_not_uninstallable",
                f"Installation state {installation_state} cannot be uninstalled.",
            )
        )
    if not immutable_membership_exact or not ordered_nodes:
        blockers.append(
            ActionReason(
                "uninstall.rank_membership_changed",
                "Persisted nodes no longer match the immutable installation plan.",
            )
        )
    if active_run_count:
        blockers.append(
            ActionReason(
                "uninstall.active_run",
                f"{active_run_count} active run(s) must be stopped explicitly first.",
            )
        )
    if active_runs_truncated:
        blockers.append(
            ActionReason(
                "uninstall.active_runs_truncated",
                "The bounded active-run list is incomplete; uninstall remains blocked.",
            )
        )
    if not bytes_known:
        blockers.append(
            ActionReason(
                "uninstall.bytes_unknown",
                "Exact removable bytes are unknown for failed or partial installation residue.",
            )
        )
    if active_operation:
        blockers.append(
            ActionReason(
                "uninstall.operation_active",
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
    }
    digest = hashlib.sha256(canonical_message(identity)).hexdigest()
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
        nodes=ordered_nodes,
        bytes_removed=bytes_removed,
        active_runs=ordered_runs,
        active_run_count=active_run_count,
        active_runs_truncated=active_runs_truncated,
        blockers=tuple(blockers),
        warnings=(),
        consequences=UninstallConsequences(),
        plan_digest=digest,
    )


__all__ = [
    "ActionReason",
    "StopNodeImpact",
    "StopPlan",
    "UninstallActiveRun",
    "UninstallConsequences",
    "UninstallNodeImpact",
    "UninstallPlan",
    "stop_plan",
    "uninstall_plan",
]
