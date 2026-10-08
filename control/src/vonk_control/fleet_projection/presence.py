"""Fleet projection: presence concerns."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    InstallDegradedReason,
    RouteState,
    RunDegradedReason,
    RunState,
)

from ..cluster_mappings import mapping_option_choices
from ..machine_states import read_state
from ..models import CatalogDocumentRevision, ClusterMappingNode
from ..recipe_update_notice import recipe_update_notice
from ..strict_json import warn_unreadable_once
from .common import (
    InstallationPresence,
    InstallationPresenceRow,
    LoadedPresence,
    RecipePresence,
    RunPresence,
    RunPresenceRow,
    UnavailableRecipePresence,
    UnavailableRunPresence,
    _canonical_recipe,
    _install_degraded_reason,
    _installation_payload_expectations,
    _run_degraded_reason,
    _unavailable_presence,
    _utc,
)

if TYPE_CHECKING:
    from .service import FleetProjection


def _installed_presence(
    self: FleetProjection,
    rows: Sequence[InstallationPresenceRow],
    mapping_rows: Sequence[ClusterMappingNode],
    fleet_node_ids: frozenset[str],
) -> dict[str, tuple[InstallationPresence, ...]]:
    mappings = self._mapping_members(mapping_rows)
    grouped: dict[str, list[InstallationPresenceRow]] = {}
    for row in rows:
        node = row[0]
        grouped.setdefault(node.installation_id, []).append(row)
    by_node: dict[str, list[InstallationPresence]] = {}
    for installation_id in sorted(grouped):
        projected_group: dict[str, list[InstallationPresence]] = {}
        try:
            group = sorted(grouped[installation_id], key=lambda value: value[0].rank)
            nodes = [value[0] for value in group]
            installation = group[0][1]
            mapping = group[0][2]
            revision = group[0][3]
            recipe = group[0][4]
            projection_issue = (
                "Stored recipe revision is unreadable; installation completeness is unknown."
                if _canonical_recipe(revision) is None
                else None
            )
            visible_nodes = [node for node in nodes if node.node_id in fleet_node_ids]
            reason = _install_degraded_reason(
                self._exact_group_reason(
                    expected_count=mapping.node_count,
                    expected=tuple(
                        sorted(
                            mappings.get(mapping.id, ()),
                            key=lambda member: member.rank,
                        )
                    ),
                    actual=nodes,
                    fleet_node_ids=fleet_node_ids,
                )
            )
            affected: list[int] = []
            expectations = _installation_payload_expectations(installation.plan)
            if reason is None and installation.state != InstallationState.INSTALLED:
                reason = InstallDegradedReason.INSTALLATION_NOT_INSTALLED
            if reason is None:
                affected = [
                    node.rank
                    for node in nodes
                    if node.state != InstallationNodeState.INSTALLED
                ]
                if affected:
                    reason = InstallDegradedReason.RANK_NOT_INSTALLED
            if reason is None:
                affected = [
                    node.rank
                    for node in nodes
                    if node.node_id in expectations
                    and node.installed_bytes < expectations[node.node_id]
                ]
                if affected:
                    reason = InstallDegradedReason.RANK_INCOMPLETE_BYTES
            present_ranks = [node.rank for node in visible_nodes]
            member_node_ids = sorted(node.node_id for node in visible_nodes)
            for node in visible_nodes:
                projected_group.setdefault(node.node_id, []).append(
                    RecipePresence(
                        installation_id=installation.id,
                        recipe_id=recipe.id,
                        recipe_revision_id=revision.id,
                        title=recipe.title,
                        topology_name=mapping.topology_name,
                        expected_rank_count=mapping.node_count,
                        present_ranks=present_ranks,
                        member_node_ids=member_node_ids,
                        rank=node.rank,
                        role=node.role,
                        group_state=read_state(InstallationState, installation.state),
                        rank_state=read_state(InstallationState, node.state),
                        complete=None
                        if projection_issue is not None
                        else reason is None,
                        projection_issue=projection_issue,
                        degraded_reason=reason,
                        affected_ranks=affected,
                        installed_bytes=node.installed_bytes,
                        required_bytes=expectations.get(node.node_id),
                    )
                )
        except (AttributeError, TypeError, ValueError):
            warn_unreadable_once("Fleet installation_id", installation_id)
            projected_group = {}
            for row in grouped[installation_id]:
                if row[0].node_id not in fleet_node_ids:
                    continue
                unavailable = _unavailable_presence(
                    UnavailableRecipePresence(
                        installation_id=installation_id,
                        complete=None,
                        projection_issue="Stored group evidence is unreadable; membership details and health are unknown.",
                    ),
                    (
                        ("recipe_id", row[4].id),
                        ("recipe_revision_id", row[3].id),
                        ("title", row[4].title),
                        ("topology_name", row[2].topology_name),
                        ("expected_rank_count", row[2].node_count),
                        ("rank", row[0].rank),
                        ("role", row[0].role),
                        ("group_state", row[1].state),
                        ("rank_state", row[0].state),
                        ("installed_bytes", row[0].installed_bytes),
                    ),
                )
                projected_group.setdefault(row[0].node_id, []).append(unavailable)
        for node_id, values in projected_group.items():
            by_node.setdefault(node_id, []).extend(values)
    return {
        node_id: tuple(
            sorted(
                values,
                key=lambda value: (
                    value.installation_id,
                    -1 if value.rank is None else value.rank,
                ),
            )
        )
        for node_id, values in by_node.items()
    }


def _loaded_presence(
    self: FleetProjection,
    rows: Sequence[RunPresenceRow],
    mapping_rows: Sequence[ClusterMappingNode],
    fleet_node_ids: frozenset[str],
    current: datetime,
    newest: Mapping[str, CatalogDocumentRevision] | None = None,
) -> dict[str, tuple[LoadedPresence, ...]]:
    mappings = self._mapping_members(mapping_rows)
    grouped: dict[str, list[RunPresenceRow]] = {}
    for row in rows:
        node = row[0]
        grouped.setdefault(node.run_id, []).append(row)
    by_node: dict[str, list[LoadedPresence]] = {}
    for run_id in sorted(grouped):
        projected_group: dict[str, list[LoadedPresence]] = {}
        try:
            group = sorted(grouped[run_id], key=lambda value: value[0].rank)
            nodes = [value[0] for value in group]
            run = group[0][1]
            if run.state in {RunState.STOPPED, RunState.FAILED, RunState.LOST}:
                continue
            mapping = group[0][2]
            revision = group[0][4]
            recipe = group[0][5]
            projection_issue = (
                "Stored recipe revision is unreadable; run health is unknown."
                if _canonical_recipe(revision) is None
                else None
            )
            visible_nodes = [node for node in nodes if node.node_id in fleet_node_ids]
            reason = _run_degraded_reason(
                self._exact_group_reason(
                    expected_count=mapping.node_count,
                    expected=tuple(
                        sorted(
                            mappings.get(mapping.id, ()),
                            key=lambda member: member.rank,
                        )
                    ),
                    actual=nodes,
                    fleet_node_ids=fleet_node_ids,
                )
            )
            freshness: dict[str, tuple[float, bool]] = {}
            for node in nodes:
                age_delta = current - _utc(node.updated_at)
                age = max(0.0, age_delta.total_seconds())
                freshness[node.id] = (
                    age,
                    timedelta(0)
                    <= age_delta
                    < timedelta(seconds=self._run_rank_fresh_seconds),
                )
            if reason is None and run.state != RunState.RUNNING:
                reason = RunDegradedReason.RUN_NOT_RUNNING
            if reason is None and any(node.state != RunState.RUNNING for node in nodes):
                reason = RunDegradedReason.RANK_NOT_RUNNING
            if reason is None and any(not freshness[node.id][1] for node in nodes):
                reason = RunDegradedReason.RANK_STALE
            if reason is None and run.route_state != RouteState.PUBLISHED:
                reason = RunDegradedReason.ROUTE_NOT_PUBLISHED
            present_ranks = [node.rank for node in visible_nodes]
            member_node_ids = sorted(node.node_id for node in visible_nodes)
            update = recipe_update_notice(
                recipe.title, revision, (newest or {}).get(recipe.id)
            )
            for node in visible_nodes:
                rank_age, rank_fresh = freshness[node.id]
                projected_group.setdefault(node.node_id, []).append(
                    RunPresence(
                        run_id=run.id,
                        installation_id=run.installation_id,
                        recipe_id=recipe.id,
                        recipe_revision_id=revision.id,
                        title=recipe.title,
                        alias=run.alias,
                        expected_rank_count=mapping.node_count,
                        present_ranks=present_ranks,
                        member_node_ids=member_node_ids,
                        rank=node.rank,
                        role=node.role,
                        run_state=read_state(RunState, run.state),
                        route_state=read_state(RouteState, run.route_state),
                        rank_state=read_state(RunState, node.state),
                        rank_age_seconds=rank_age,
                        rank_fresh=rank_fresh,
                        group_state=(
                            "unavailable"
                            if projection_issue is not None
                            else "healthy"
                            if reason is None
                            else "degraded"
                        ),
                        healthy=None
                        if projection_issue is not None
                        else reason is None,
                        projection_issue=projection_issue,
                        degraded_reason=reason,
                        route_reason=(
                            run.route_error
                            if run.route_state != RouteState.PUBLISHED
                            else None
                        ),
                        option_choices=mapping_option_choices(mapping.parameters),
                        recipe_update=update,
                    )
                )
        except (AttributeError, TypeError, ValueError):
            warn_unreadable_once("Fleet run_id", run_id)
            projected_group = {}
            for row in grouped[run_id]:
                if row[0].node_id not in fleet_node_ids:
                    continue
                unavailable = _unavailable_presence(
                    UnavailableRunPresence(
                        run_id=run_id,
                        healthy=None,
                        projection_issue="Stored group evidence is unreadable; membership details and health are unknown.",
                    ),
                    (
                        ("installation_id", row[1].installation_id),
                        ("recipe_id", row[5].id),
                        ("recipe_revision_id", row[4].id),
                        ("title", row[5].title),
                        ("alias", row[1].alias),
                        ("expected_rank_count", row[2].node_count),
                        ("rank", row[0].rank),
                        ("role", row[0].role),
                        ("run_state", row[1].state),
                        ("route_state", row[1].route_state),
                        ("rank_state", row[0].state),
                    ),
                )
                projected_group.setdefault(row[0].node_id, []).append(unavailable)
        for node_id, values in projected_group.items():
            by_node.setdefault(node_id, []).extend(values)
    return {
        node_id: tuple(
            sorted(
                values,
                key=lambda value: (
                    value.run_id,
                    -1 if value.rank is None else value.rank,
                ),
            )
        )
        for node_id, values in by_node.items()
    }
