"""Bounded typed projection of repository-authoritative Fleet state."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .fleet_events import FleetEventRepository
from .models import (
    AgentNode,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    LocalRecipe,
    LocalRecipeRevision,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from .telemetry import (
    TelemetryDetailsInput,
    TelemetryRepository,
    TelemetrySampleView,
)

_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_MAX_FLEET_NODES = 500
_MAX_OPERATIONAL_GROUPS = 512
_MAX_GROUP_MEMBER_ROWS = 8_192


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectionReason(_StrictModel):
    code: str = Field(min_length=1, max_length=64)
    detail: str = Field(min_length=1, max_length=256)
    severity: Literal["info", "warning", "error"]


class NodeConnection(_StrictModel):
    agent_state: str = Field(min_length=1, max_length=24)
    online_state: Literal["online", "offline", "unregistered"]
    last_seen_at: datetime | None
    last_seen_age_seconds: float | None = Field(ge=0)


class InventoryState(_StrictModel):
    observed_at: datetime
    received_at: datetime
    age_seconds: float = Field(ge=0)
    freshness: Literal["fresh", "stale"]
    disk_total_bytes: int = Field(ge=0)
    disk_free_bytes: int = Field(ge=0)
    host_memory_total_bytes: int = Field(ge=0)
    host_memory_free_bytes: int = Field(ge=0)
    gpu_memory_total_bytes: int = Field(ge=0)
    gpu_memory_free_bytes: int = Field(ge=0)
    gpu_count: int = Field(ge=0)
    artifact_store_read_only: bool
    capabilities: list[str] = Field(max_length=64)
    fabric_address: str | None = Field(default=None, max_length=45)
    fabric_bandwidth_mbps: int | None = Field(default=None, ge=1)
    nvidia_driver_version: str = Field(min_length=1, max_length=256)
    container_runtime_version: str = Field(min_length=1, max_length=256)


class TelemetryDetails(_StrictModel):
    accelerator_name: str | None = Field(default=None, max_length=256)
    accelerator_performance_state: str | None = Field(default=None, max_length=32)


class TelemetryPoint(_StrictModel):
    id: str = Field(min_length=1, max_length=36)
    node_id: str = Field(pattern=_NODE_PATTERN)
    boot_id: str = Field(min_length=36, max_length=36)
    sequence: int = Field(ge=0, le=9_223_372_036_854_775_807)
    observed_at: datetime
    received_at: datetime
    cpu_utilization_percent: float | None = Field(default=None, ge=0, le=100)
    load_average_1m: float | None = Field(default=None, ge=0)
    memory_total_bytes: int | None = Field(default=None, ge=0)
    memory_available_bytes: int | None = Field(default=None, ge=0)
    disk_total_bytes: int | None = Field(default=None, ge=0)
    disk_free_bytes: int | None = Field(default=None, ge=0)
    gpu_utilization_percent: float | None = Field(default=None, ge=0, le=100)
    gpu_memory_total_bytes: int | None = Field(default=None, ge=0)
    gpu_memory_free_bytes: int | None = Field(default=None, ge=0)
    temperature_c: float | None = None
    power_watts: float | None = Field(default=None, ge=0)
    network_receive_bytes_per_second: float | None = Field(default=None, ge=0)
    network_transmit_bytes_per_second: float | None = Field(default=None, ge=0)
    gap_samples: int = Field(ge=0, le=9_223_372_036_854_775_807)
    details: TelemetryDetails


class TelemetryState(_StrictModel):
    age_seconds: float = Field(ge=0)
    freshness: Literal["live", "delayed", "stale"]
    sample: TelemetryPoint


class RecipePresence(_StrictModel):
    installation_id: str = Field(min_length=1, max_length=36)
    recipe_id: str = Field(min_length=1, max_length=36)
    recipe_revision_id: str = Field(min_length=1, max_length=36)
    title: str = Field(min_length=1, max_length=200)
    profile_name: str = Field(min_length=1, max_length=64)
    expected_rank_count: int = Field(ge=1, le=_MAX_FLEET_NODES)
    present_ranks: list[int] = Field(max_length=_MAX_FLEET_NODES)
    member_node_ids: list[str] = Field(max_length=_MAX_FLEET_NODES)
    rank: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=64)
    group_state: str = Field(min_length=1, max_length=24)
    rank_state: str = Field(min_length=1, max_length=24)
    complete: bool
    degraded_reason: str | None = Field(default=None, max_length=64)


class RunPresence(_StrictModel):
    run_id: str = Field(min_length=1, max_length=36)
    installation_id: str = Field(min_length=1, max_length=36)
    recipe_id: str = Field(min_length=1, max_length=36)
    recipe_revision_id: str = Field(min_length=1, max_length=36)
    title: str = Field(min_length=1, max_length=200)
    alias: str = Field(min_length=1, max_length=128)
    expected_rank_count: int = Field(ge=1, le=_MAX_FLEET_NODES)
    present_ranks: list[int] = Field(max_length=_MAX_FLEET_NODES)
    member_node_ids: list[str] = Field(max_length=_MAX_FLEET_NODES)
    rank: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=64)
    run_state: str = Field(min_length=1, max_length=24)
    route_state: str = Field(min_length=1, max_length=24)
    rank_state: str = Field(min_length=1, max_length=24)
    rank_age_seconds: float = Field(ge=0)
    rank_fresh: bool
    group_state: Literal["healthy", "degraded"]
    healthy: bool
    degraded_reason: str | None = Field(default=None, max_length=64)


class CapacityReservations(_StrictModel):
    disk_bytes: int = Field(ge=0)
    unified_memory_bytes: int = Field(ge=0)
    host_memory_bytes: int = Field(ge=0)
    gpu_memory_bytes: int = Field(ge=0)
    port_count: int = Field(ge=0)


class FleetNode(_StrictModel):
    id: str = Field(pattern=_NODE_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    hostname: str = Field(max_length=255)
    lifecycle: str = Field(min_length=1, max_length=64)
    labels: dict[str, str] = Field(max_length=64)
    connection: NodeConnection
    inventory: InventoryState | None
    telemetry: TelemetryState | None
    installed: list[RecipePresence] = Field(max_length=512)
    loaded: list[RunPresence] = Field(max_length=512)
    reservations: CapacityReservations
    warnings: list[ProjectionReason] = Field(max_length=128)


class FleetSnapshot(_StrictModel):
    schema_version: Literal[1] = 1
    event_cursor: int = Field(ge=0, le=9_223_372_036_854_775_807)
    generated_at: datetime
    repository_commit: str = Field(pattern=_COMMIT_PATTERN)
    nodes: list[FleetNode] = Field(max_length=_MAX_FLEET_NODES)


class TelemetryHistoryResponse(_StrictModel):
    schema_version: Literal[1] = 1
    node_id: str = Field(pattern=_NODE_PATTERN)
    start: datetime
    end: datetime
    maximum_points: int = Field(ge=1, le=1_500)
    points: list[TelemetryPoint] = Field(max_length=1_500)


def telemetry_point(value: TelemetrySampleView) -> TelemetryPoint:
    return TelemetryPoint(
        id=value.id,
        node_id=value.node_id,
        boot_id=str(value.boot_id),
        sequence=value.sequence,
        observed_at=value.observed_at,
        received_at=value.received_at,
        cpu_utilization_percent=value.cpu_utilization_percent,
        load_average_1m=value.load_average_1m,
        memory_total_bytes=value.memory_total_bytes,
        memory_available_bytes=value.memory_available_bytes,
        disk_total_bytes=value.disk_total_bytes,
        disk_free_bytes=value.disk_free_bytes,
        gpu_utilization_percent=value.gpu_utilization_percent,
        gpu_memory_total_bytes=value.gpu_memory_total_bytes,
        gpu_memory_free_bytes=value.gpu_memory_free_bytes,
        temperature_c=value.temperature_c,
        power_watts=value.power_watts,
        network_receive_bytes_per_second=value.network_receive_bytes_per_second,
        network_transmit_bytes_per_second=value.network_transmit_bytes_per_second,
        gap_samples=value.gap_samples,
        details=_telemetry_details(value.details),
    )


def _telemetry_details(value: TelemetryDetailsInput) -> TelemetryDetails:
    return TelemetryDetails(
        accelerator_name=value.accelerator_name,
        accelerator_performance_state=value.accelerator_performance_state,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class FleetProjection:
    """Merge a fixed database query set into repository-defined Fleet nodes."""

    def __init__(
        self,
        repository: object,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        events: FleetEventRepository | None = None,
        telemetry: TelemetryRepository | None = None,
        telemetry_live_seconds: float = 6,
        telemetry_delayed_seconds: float = 20,
        agent_online_seconds: float = 150,
        inventory_fresh_seconds: float = 300,
        run_rank_fresh_seconds: float = 300,
    ) -> None:
        windows = (
            telemetry_live_seconds,
            telemetry_delayed_seconds,
            agent_online_seconds,
            inventory_fresh_seconds,
            run_rank_fresh_seconds,
        )
        if any(value <= 0 for value in windows):
            raise ValueError("Fleet projection freshness windows must be positive")
        if telemetry_delayed_seconds < telemetry_live_seconds:
            raise ValueError("Fleet telemetry freshness windows are invalid")
        self._repository = repository
        self._sessions = sessions
        self._clock = clock
        self._events = events or FleetEventRepository(sessions, clock=clock)
        self._telemetry = telemetry or TelemetryRepository(sessions, clock=clock)
        self._telemetry_live_seconds = telemetry_live_seconds
        self._telemetry_delayed_seconds = telemetry_delayed_seconds
        self._agent_online_seconds = agent_online_seconds
        self._inventory_fresh_seconds = inventory_fresh_seconds
        self._run_rank_fresh_seconds = run_rank_fresh_seconds

    def read(self) -> FleetSnapshot:
        return self.read_at(self._events.high_watermark())

    def read_at(self, event_cursor: int) -> FleetSnapshot:
        if type(event_cursor) is not int or not 0 <= event_cursor <= 9_223_372_036_854_775_807:
            raise ValueError("Fleet event cursor is invalid")
        commit, fleet_nodes = self._repository_nodes()
        node_ids = tuple(sorted(fleet_nodes))
        current = _utc(self._clock())
        with self._sessions.begin() as session:
            agents = {
                row.node_id: row
                for row in session.scalars(
                    select(AgentNode)
                    .where(AgentNode.node_id.in_(node_ids))
                    .order_by(AgentNode.node_id)
                )
            }
            inventories = self._latest_inventory(session, node_ids)
            telemetry = self._telemetry.latest_in_session(session, node_ids)
            installation_rows = self._installation_rows(session, node_ids)
            run_rows = self._run_rows(session, node_ids)
            mapping_ids = {
                row[2].id for row in (*installation_rows, *run_rows)
            }
            mapping_nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id.in_(mapping_ids))
                    .order_by(ClusterMappingNode.mapping_id, ClusterMappingNode.rank)
                    .limit(_MAX_GROUP_MEMBER_ROWS)
                )
            )
            installed = self._installed_presence(installation_rows, mapping_nodes)
            loaded = self._loaded_presence(run_rows, mapping_nodes, current)
            reservations = self._reservations(session, node_ids)
        return FleetSnapshot(
            event_cursor=event_cursor,
            generated_at=current,
            repository_commit=commit,
            nodes=[
                self._node(
                    node_id,
                    fleet_nodes[node_id],
                    current=current,
                    agent=agents.get(node_id),
                    inventory=inventories.get(node_id),
                    telemetry=telemetry.get(node_id),
                    installed=installed.get(node_id, ()),
                    loaded=loaded.get(node_id, ()),
                    reservations=reservations.get(node_id, {}),
                )
                for node_id in node_ids
            ],
        )

    def telemetry_history(
        self,
        node_id: str,
        *,
        start: datetime,
        end: datetime,
        maximum_points: int,
    ) -> TelemetryHistoryResponse:
        if type(maximum_points) is not int or not 1 <= maximum_points <= 1_500:
            raise ValueError("telemetry history maximum points is invalid")
        if (
            start.tzinfo is None
            or start.utcoffset() is None
            or end.tzinfo is None
            or end.utcoffset() is None
        ):
            raise ValueError("telemetry history window must be timezone-aware")
        start_utc = start.astimezone(UTC)
        end_utc = end.astimezone(UTC)
        if start_utc >= end_utc:
            raise ValueError("telemetry history window is invalid")
        if end_utc - start_utc > timedelta(hours=24):
            raise ValueError("telemetry history raw window exceeds 24 hours")
        _, nodes = self._repository_nodes()
        if node_id not in nodes:
            raise KeyError(node_id)
        points = self._telemetry.history(
            node_id,
            start_utc,
            end_utc,
            maximum_points,
        )
        return TelemetryHistoryResponse(
            node_id=node_id,
            start=start_utc,
            end=end_utc,
            maximum_points=maximum_points,
            points=[telemetry_point(value) for value in points],
        )

    def _repository_nodes(self) -> tuple[str, dict[str, Mapping[str, object]]]:
        commit = self._repository.head()
        document = self._repository.read_document(commit, "inventory/fleet.toml")
        parsed = document.parsed
        if not isinstance(parsed, Mapping) or not isinstance(
            parsed.get("nodes"), Mapping
        ):
            raise TypeError("fleet document does not contain a node table")
        nodes = {
            node_id: value
            for node_id, value in parsed["nodes"].items()
            if isinstance(node_id, str) and isinstance(value, Mapping)
        }
        if len(nodes) > _MAX_FLEET_NODES:
            raise ValueError("Fleet document contains more than 500 nodes")
        return commit, nodes

    @staticmethod
    def _latest_inventory(
        session: Session, node_ids: Sequence[str]
    ) -> dict[str, NodeInventorySnapshot]:
        ranked = (
            select(
                NodeInventorySnapshot.id.label("id"),
                func.row_number()
                .over(
                    partition_by=NodeInventorySnapshot.node_id,
                    order_by=(
                        NodeInventorySnapshot.observed_at.desc(),
                        NodeInventorySnapshot.id.desc(),
                    ),
                )
                .label("position"),
            )
            .where(NodeInventorySnapshot.node_id.in_(node_ids))
            .subquery()
        )
        rows = session.scalars(
            select(NodeInventorySnapshot)
            .join(ranked, NodeInventorySnapshot.id == ranked.c.id)
            .where(ranked.c.position == 1)
            .order_by(NodeInventorySnapshot.node_id)
        )
        return {row.node_id: row for row in rows}

    @staticmethod
    def _installation_rows(session: Session, node_ids: Sequence[str]):
        selected = (
            select(RecipeInstallation.id)
            .join(
                InstallationNode,
                InstallationNode.installation_id == RecipeInstallation.id,
            )
            .where(
                InstallationNode.node_id.in_(node_ids),
                RecipeInstallation.state != "uninstalled",
            )
            .group_by(RecipeInstallation.id, RecipeInstallation.updated_at)
            .order_by(
                RecipeInstallation.updated_at.desc(), RecipeInstallation.id.desc()
            )
            .limit(_MAX_OPERATIONAL_GROUPS)
        )
        return tuple(
            session.execute(
                select(
                    InstallationNode,
                    RecipeInstallation,
                    ClusterMapping,
                    LocalRecipeRevision,
                    LocalRecipe,
                )
                .join(
                    RecipeInstallation,
                    RecipeInstallation.id == InstallationNode.installation_id,
                )
                .join(ClusterMapping, ClusterMapping.id == RecipeInstallation.mapping_id)
                .join(
                    LocalRecipeRevision,
                    LocalRecipeRevision.id == RecipeInstallation.recipe_revision_id,
                )
                .join(LocalRecipe, LocalRecipe.id == LocalRecipeRevision.recipe_id)
                .where(InstallationNode.installation_id.in_(selected))
                .order_by(InstallationNode.installation_id, InstallationNode.rank)
                .limit(_MAX_GROUP_MEMBER_ROWS)
            )
        )

    @staticmethod
    def _mapping_members(
        rows: Sequence[ClusterMappingNode],
    ) -> dict[str, tuple[ClusterMappingNode, ...]]:
        grouped: dict[str, list[ClusterMappingNode]] = {}
        for row in rows:
            grouped.setdefault(row.mapping_id, []).append(row)
        return {
            mapping_id: tuple(sorted(values, key=lambda value: value.rank))
            for mapping_id, values in grouped.items()
        }

    @staticmethod
    def _exact_group_reason(
        *,
        expected_count: int,
        expected: Sequence[ClusterMappingNode],
        actual: Sequence[InstallationNode | RunNode],
    ) -> str | None:
        expected_ranks = [value.rank for value in expected]
        actual_ranks = [value.rank for value in actual]
        if len(expected) != expected_count or expected_ranks != list(
            range(expected_count)
        ):
            return "mapping-incomplete"
        missing = set(expected_ranks) - set(actual_ranks)
        if missing:
            return "missing-ranks"
        unexpected = set(actual_ranks) - set(expected_ranks)
        if unexpected or len(actual_ranks) != expected_count:
            return "unexpected-ranks"
        expected_members = {
            (value.rank, value.node_id, value.role) for value in expected
        }
        actual_members = {(value.rank, value.node_id, value.role) for value in actual}
        if actual_members != expected_members:
            return "rank-membership-mismatch"
        return None

    def _installed_presence(
        self,
        rows: Sequence[object],
        mapping_rows: Sequence[ClusterMappingNode],
    ) -> dict[str, tuple[RecipePresence, ...]]:
        mappings = self._mapping_members(mapping_rows)
        grouped: dict[str, list[object]] = {}
        for row in rows:
            node = row[0]
            grouped.setdefault(node.installation_id, []).append(row)
        by_node: dict[str, list[RecipePresence]] = {}
        for installation_id in sorted(grouped):
            group = sorted(grouped[installation_id], key=lambda value: value[0].rank)
            nodes = [value[0] for value in group]
            installation = group[0][1]
            mapping = group[0][2]
            revision = group[0][3]
            recipe = group[0][4]
            reason = self._exact_group_reason(
                expected_count=mapping.node_count,
                expected=mappings.get(mapping.id, ()),
                actual=nodes,
            )
            if reason is None and installation.state != "installed":
                reason = "installation-not-installed"
            if reason is None and any(node.state != "installed" for node in nodes):
                reason = "rank-not-installed"
            if reason is None and any(
                node.installed_bytes < node.required_bytes for node in nodes
            ):
                reason = "rank-incomplete-bytes"
            present_ranks = [node.rank for node in nodes]
            member_node_ids = sorted(node.node_id for node in nodes)
            for node in nodes:
                by_node.setdefault(node.node_id, []).append(
                    RecipePresence(
                        installation_id=installation.id,
                        recipe_id=recipe.id,
                        recipe_revision_id=revision.id,
                        title=recipe.title,
                        profile_name=mapping.profile_name,
                        expected_rank_count=mapping.node_count,
                        present_ranks=present_ranks,
                        member_node_ids=member_node_ids,
                        rank=node.rank,
                        role=node.role,
                        group_state=installation.state,
                        rank_state=node.state,
                        complete=reason is None,
                        degraded_reason=reason,
                    )
                )
        return {
            node_id: tuple(
                sorted(values, key=lambda value: (value.installation_id, value.rank))
            )
            for node_id, values in by_node.items()
        }

    def _loaded_presence(
        self,
        rows: Sequence[object],
        mapping_rows: Sequence[ClusterMappingNode],
        current: datetime,
    ) -> dict[str, tuple[RunPresence, ...]]:
        mappings = self._mapping_members(mapping_rows)
        grouped: dict[str, list[object]] = {}
        for row in rows:
            node = row[0]
            grouped.setdefault(node.run_id, []).append(row)
        by_node: dict[str, list[RunPresence]] = {}
        for run_id in sorted(grouped):
            group = sorted(grouped[run_id], key=lambda value: value[0].rank)
            nodes = [value[0] for value in group]
            run = group[0][1]
            if run.state in {"stopped", "failed", "lost"}:
                continue
            mapping = group[0][2]
            revision = group[0][4]
            recipe = group[0][5]
            reason = self._exact_group_reason(
                expected_count=mapping.node_count,
                expected=mappings.get(mapping.id, ()),
                actual=nodes,
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
            if reason is None and run.state != "running":
                reason = "run-not-running"
            if reason is None and any(node.state != "running" for node in nodes):
                reason = "rank-not-running"
            if reason is None and any(not freshness[node.id][1] for node in nodes):
                reason = "rank-stale"
            if reason is None and run.route_state != "published":
                reason = "route-not-published"
            present_ranks = [node.rank for node in nodes]
            member_node_ids = sorted(node.node_id for node in nodes)
            for node in nodes:
                rank_age, rank_fresh = freshness[node.id]
                by_node.setdefault(node.node_id, []).append(
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
                        run_state=run.state,
                        route_state=run.route_state,
                        rank_state=node.state,
                        rank_age_seconds=rank_age,
                        rank_fresh=rank_fresh,
                        group_state="healthy" if reason is None else "degraded",
                        healthy=reason is None,
                        degraded_reason=reason,
                    )
                )
        return {
            node_id: tuple(sorted(values, key=lambda value: (value.run_id, value.rank)))
            for node_id, values in by_node.items()
        }

    @staticmethod
    def _run_rows(session: Session, node_ids: Sequence[str]):
        selected = (
            select(RecipeRun.id)
            .join(RunNode, RunNode.run_id == RecipeRun.id)
            .where(
                RunNode.node_id.in_(node_ids),
                RecipeRun.state.not_in({"stopped", "failed", "lost"}),
            )
            .group_by(RecipeRun.id, RecipeRun.updated_at)
            .order_by(RecipeRun.updated_at.desc(), RecipeRun.id.desc())
            .limit(_MAX_OPERATIONAL_GROUPS)
        )
        return tuple(
            session.execute(
                select(
                    RunNode,
                    RecipeRun,
                    ClusterMapping,
                    RecipeInstallation,
                    LocalRecipeRevision,
                    LocalRecipe,
                )
                .join(RecipeRun, RecipeRun.id == RunNode.run_id)
                .join(ClusterMapping, ClusterMapping.id == RecipeRun.mapping_id)
                .join(
                    RecipeInstallation,
                    RecipeInstallation.id == RecipeRun.installation_id,
                )
                .join(
                    LocalRecipeRevision,
                    LocalRecipeRevision.id == RecipeInstallation.recipe_revision_id,
                )
                .join(LocalRecipe, LocalRecipe.id == LocalRecipeRevision.recipe_id)
                .where(RunNode.run_id.in_(selected))
                .order_by(RunNode.run_id, RunNode.rank)
                .limit(_MAX_GROUP_MEMBER_ROWS)
            )
        )

    @staticmethod
    def _reservations(
        session: Session, node_ids: Sequence[str]
    ) -> dict[str, dict[str, tuple[int, int]]]:
        rows = session.execute(
            select(
                ResourceReservation.node_id,
                ResourceReservation.kind,
                func.sum(ResourceReservation.amount_bytes),
                func.count(ResourceReservation.id),
            )
            .where(
                ResourceReservation.node_id.in_(node_ids),
                ResourceReservation.state == "active",
            )
            .group_by(ResourceReservation.node_id, ResourceReservation.kind)
            .order_by(ResourceReservation.node_id, ResourceReservation.kind)
        )
        values: dict[str, dict[str, tuple[int, int]]] = {}
        for node_id, kind, amount, count in rows:
            values.setdefault(node_id, {})[kind] = (int(amount or 0), int(count))
        return values

    def _node(
        self,
        node_id: str,
        raw: Mapping[str, object],
        *,
        current: datetime,
        agent: AgentNode | None,
        inventory: NodeInventorySnapshot | None,
        telemetry: TelemetrySampleView | None,
        installed: Sequence[RecipePresence],
        loaded: Sequence[RunPresence],
        reservations: Mapping[str, tuple[int, int]],
    ) -> FleetNode:
        warnings: list[ProjectionReason] = []
        connection = self._connection(agent, current)
        inventory_state = self._inventory(inventory, current)
        telemetry_state = self._telemetry_state(telemetry, current)
        if connection.online_state != "online":
            warnings.append(
                ProjectionReason(
                    code="node.offline",
                    detail="The authenticated agent is not currently online.",
                    severity="warning",
                )
            )
        if inventory_state is None:
            warnings.append(
                ProjectionReason(
                    code="inventory.missing",
                    detail="No admission inventory snapshot is available.",
                    severity="warning",
                )
            )
        elif inventory_state.freshness == "stale":
            warnings.append(
                ProjectionReason(
                    code="inventory.stale",
                    detail="Admission inventory is stale.",
                    severity="warning",
                )
            )
        if telemetry_state is None:
            warnings.append(
                ProjectionReason(
                    code="telemetry.missing",
                    detail="No telemetry sample is available.",
                    severity="warning",
                )
            )
        elif telemetry_state.freshness == "delayed":
            warnings.append(
                ProjectionReason(
                    code="telemetry.delayed",
                    detail="Telemetry delivery is delayed.",
                    severity="warning",
                )
            )
        elif telemetry_state.freshness == "stale":
            warnings.append(
                ProjectionReason(
                    code="telemetry.stale",
                    detail="Telemetry is stale.",
                    severity="warning",
                )
            )
        if any(not value.complete for value in installed):
            warnings.append(
                ProjectionReason(
                    code="install.partial",
                    detail="A recipe installation group is incomplete.",
                    severity="warning",
                )
            )
        if any(not value.healthy for value in loaded):
            warnings.append(
                ProjectionReason(
                    code="run.degraded",
                    detail="A loaded recipe group is degraded.",
                    severity="warning",
                )
            )
        labels = raw.get("labels", {})
        if not isinstance(labels, Mapping):
            raise TypeError("fleet node labels are invalid")
        return FleetNode(
            id=node_id,
            display_name=str(raw.get("display_name", node_id)),
            hostname=str(raw.get("hostname", "")),
            lifecycle=str(raw.get("lifecycle", "unknown")),
            labels=dict(labels),
            connection=connection,
            inventory=inventory_state,
            telemetry=telemetry_state,
            installed=list(installed),
            loaded=list(loaded),
            reservations=CapacityReservations(
                disk_bytes=reservations.get("disk", (0, 0))[0],
                unified_memory_bytes=reservations.get("unified-memory", (0, 0))[0],
                host_memory_bytes=reservations.get("host-memory", (0, 0))[0],
                gpu_memory_bytes=reservations.get("gpu-memory", (0, 0))[0],
                port_count=reservations.get("port", (0, 0))[1],
            ),
            warnings=warnings,
        )

    def _connection(self, value: AgentNode | None, current: datetime) -> NodeConnection:
        if value is None:
            return NodeConnection(
                agent_state="unregistered",
                online_state="unregistered",
                last_seen_at=None,
                last_seen_age_seconds=None,
            )
        last_seen = None if value.last_seen_at is None else _utc(value.last_seen_at)
        age = None if last_seen is None else max(0.0, (current - last_seen).total_seconds())
        online = (
            value.state == "active"
            and value.revoked_at is None
            and last_seen is not None
            and timedelta(0) <= current - last_seen <= timedelta(seconds=self._agent_online_seconds)
        )
        return NodeConnection(
            agent_state=value.state,
            online_state="online" if online else "offline",
            last_seen_at=last_seen,
            last_seen_age_seconds=age,
        )

    def _inventory(
        self, value: NodeInventorySnapshot | None, current: datetime
    ) -> InventoryState | None:
        if value is None:
            return None
        observed = _utc(value.observed_at)
        age = max(0.0, (current - observed).total_seconds())
        return InventoryState(
            observed_at=observed,
            received_at=_utc(value.received_at),
            age_seconds=age,
            freshness="stale" if age > self._inventory_fresh_seconds else "fresh",
            disk_total_bytes=value.disk_total_bytes,
            disk_free_bytes=value.disk_free_bytes,
            host_memory_total_bytes=value.host_memory_total_bytes,
            host_memory_free_bytes=value.host_memory_free_bytes,
            gpu_memory_total_bytes=value.gpu_memory_total_bytes,
            gpu_memory_free_bytes=value.gpu_memory_free_bytes,
            gpu_count=value.gpu_count,
            artifact_store_read_only=value.artifact_store_read_only,
            capabilities=list(value.capabilities),
            fabric_address=value.fabric_address,
            fabric_bandwidth_mbps=value.fabric_bandwidth_mbps,
            nvidia_driver_version=value.nvidia_driver_version,
            container_runtime_version=value.container_runtime_version,
        )

    def _telemetry_state(
        self, value: TelemetrySampleView | None, current: datetime
    ) -> TelemetryState | None:
        if value is None:
            return None
        age = max(0.0, (current - _utc(value.observed_at)).total_seconds())
        if age <= self._telemetry_live_seconds:
            freshness = "live"
        elif age <= self._telemetry_delayed_seconds:
            freshness = "delayed"
        else:
            freshness = "stale"
        return TelemetryState(
            age_seconds=age,
            freshness=freshness,
            sample=telemetry_point(value),
        )
