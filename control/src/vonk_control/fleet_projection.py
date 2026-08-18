"""Bounded typed projection of PostgreSQL-authoritative Fleet state."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker

from .fleet_events import FleetEventRepository
from .models import (
    AgentCertificate,
    AgentNode,
    AgentNodeProfile,
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
    TelemetryResolution,
    TelemetryRollupPointView,
    TelemetrySampleView,
)

_COMMIT_PATTERN = r"^[0-9a-f]{40}$"
_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_BOOT_UUID_PATTERN = (
    r"^(?!00000000-0000-0000-0000-000000000000$)"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_MAX_FLEET_NODES = 500
_MAX_OPERATIONAL_GROUPS = 512
_MAX_GROUP_MEMBER_ROWS = 8_192
_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807
_MAX_SIGNED_INTEGER = 2_147_483_647
_MAX_TELEMETRY_BYTES = 16 * 1024**4
_MAX_TELEMETRY_RATE = 1_000_000_000_000_000.0

NodeId = Annotated[str, StringConstraints(pattern=_NODE_PATTERN)]
UuidId = Annotated[str, StringConstraints(pattern=_UUID_PATTERN)]
BootId = Annotated[str, StringConstraints(pattern=_BOOT_UUID_PATTERN)]
CommitId = Annotated[str, StringConstraints(pattern=_COMMIT_PATTERN)]
Text32 = Annotated[str, StringConstraints(min_length=1, max_length=32)]
Text64 = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Text128 = Annotated[str, StringConstraints(min_length=1, max_length=128)]
Text200 = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Text256 = Annotated[str, StringConstraints(min_length=1, max_length=256)]
Rank = Annotated[int, Field(ge=0, le=_MAX_FLEET_NODES - 1)]

AgentState = Literal["unregistered", "pending", "active", "retired", "revoked"]
CertificateState = Literal[
    "valid", "missing", "not-yet-valid", "expired", "revoked", "inactive"
]
OfflineReason = Literal[
    "unregistered",
    "agent-inactive",
    "agent-revoked",
    "never-seen",
    "last-seen-in-future",
    "stale",
    "certificate-missing",
    "certificate-not-yet-valid",
    "certificate-expired",
    "certificate-revoked",
    "certificate-inactive",
]
InstallationState = Literal[
    "planned", "installing", "installed", "partial", "failed", "uninstalled"
]
RunState = Literal[
    "planned", "starting", "running", "stopping", "stopped", "failed", "lost"
]
RouteState = Literal["withdrawn", "pending", "published", "failed"]
InstallDegradedReason = Literal[
    "external-member",
    "mapping-incomplete",
    "missing-ranks",
    "unexpected-ranks",
    "rank-membership-mismatch",
    "installation-not-installed",
    "rank-not-installed",
    "rank-incomplete-bytes",
]
RunDegradedReason = Literal[
    "external-member",
    "mapping-incomplete",
    "missing-ranks",
    "unexpected-ranks",
    "rank-membership-mismatch",
    "run-not-running",
    "rank-not-running",
    "rank-stale",
    "route-not-published",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ProjectionReason(_StrictModel):
    code: Literal[
        "node.offline",
        "inventory.missing",
        "inventory.stale",
        "telemetry.missing",
        "telemetry.delayed",
        "telemetry.stale",
        "install.partial",
        "run.degraded",
    ]
    detail: Text256
    severity: Literal["info", "warning", "error"]


class NodeConnection(_StrictModel):
    agent_state: AgentState
    certificate_state: CertificateState
    online_state: Literal["online", "offline", "unregistered"]
    offline_reason: OfflineReason | None
    last_seen_at: datetime | None
    last_seen_age_seconds: float | None = Field(ge=0, le=float(_MAX_SIGNED_BIGINT))


class InventoryState(_StrictModel):
    observed_at: datetime
    received_at: datetime
    age_seconds: float = Field(ge=0, le=float(_MAX_SIGNED_BIGINT))
    freshness: Literal["fresh", "stale"]
    disk_total_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    disk_free_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    host_memory_total_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    host_memory_free_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    gpu_memory_total_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    gpu_memory_free_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    gpu_count: int = Field(ge=0, le=_MAX_SIGNED_INTEGER)
    artifact_store_read_only: bool
    capabilities: list[Text64] = Field(max_length=64)
    fabric_address: str | None = Field(default=None, max_length=45)
    fabric_bandwidth_mbps: int | None = Field(default=None, ge=1, le=_MAX_SIGNED_BIGINT)
    nvidia_driver_version: Text256
    container_runtime_version: Text256


class TelemetryDetails(_StrictModel):
    accelerator_name: Text256 | None = None
    accelerator_performance_state: Text32 | None = None


class TelemetryPoint(_StrictModel):
    model_config = ConfigDict(regex_engine="python-re")

    id: UuidId
    node_id: NodeId
    boot_id: BootId
    sequence: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    observed_at: datetime
    received_at: datetime
    cpu_utilization_percent: float | None = Field(default=None, ge=0, le=100)
    load_average_1m: float | None = Field(default=None, ge=0, le=1_000_000)
    memory_total_bytes: int | None = Field(default=None, ge=0, le=_MAX_TELEMETRY_BYTES)
    memory_available_bytes: int | None = Field(
        default=None, ge=0, le=_MAX_TELEMETRY_BYTES
    )
    disk_total_bytes: int | None = Field(default=None, ge=0, le=_MAX_TELEMETRY_BYTES)
    disk_free_bytes: int | None = Field(default=None, ge=0, le=_MAX_TELEMETRY_BYTES)
    gpu_utilization_percent: float | None = Field(default=None, ge=0, le=100)
    gpu_memory_total_bytes: int | None = Field(
        default=None, ge=0, le=_MAX_TELEMETRY_BYTES
    )
    gpu_memory_free_bytes: int | None = Field(
        default=None, ge=0, le=_MAX_TELEMETRY_BYTES
    )
    temperature_c: float | None = Field(default=None, ge=-100, le=300)
    power_watts: float | None = Field(default=None, ge=0, le=100_000)
    network_receive_bytes_per_second: float | None = Field(
        default=None, ge=0, le=_MAX_TELEMETRY_RATE
    )
    network_transmit_bytes_per_second: float | None = Field(
        default=None, ge=0, le=_MAX_TELEMETRY_RATE
    )
    gap_samples: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    details: TelemetryDetails


class TelemetryMetricSummary(_StrictModel):
    count: int = Field(ge=1, le=_MAX_SIGNED_BIGINT)
    minimum: float
    mean: float
    maximum: float


class TelemetryRollupPoint(_StrictModel):
    node_id: NodeId
    resolution: Literal["minute", "fifteen-minute"]
    bucket_start: datetime
    bucket_end: datetime
    source_sample_count: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    gap_samples: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    metrics: dict[str, TelemetryMetricSummary] = Field(max_length=64)


class TelemetryState(_StrictModel):
    age_seconds: float = Field(ge=0, le=float(_MAX_SIGNED_BIGINT))
    freshness: Literal["live", "delayed", "stale"]
    sample: TelemetryPoint


class RecipePresence(_StrictModel):
    installation_id: Text128
    recipe_id: Text128
    recipe_revision_id: Text128
    title: Text200
    topology_name: Text64
    expected_rank_count: int = Field(ge=1, le=_MAX_FLEET_NODES)
    present_ranks: list[Rank] = Field(max_length=_MAX_FLEET_NODES)
    member_node_ids: list[NodeId] = Field(max_length=_MAX_FLEET_NODES)
    rank: Rank
    role: Text64
    group_state: InstallationState
    rank_state: InstallationState
    complete: bool
    degraded_reason: InstallDegradedReason | None = None


class RunPresence(_StrictModel):
    run_id: Text128
    installation_id: Text128
    recipe_id: Text128
    recipe_revision_id: Text128
    title: Text200
    alias: Text128
    expected_rank_count: int = Field(ge=1, le=_MAX_FLEET_NODES)
    present_ranks: list[Rank] = Field(max_length=_MAX_FLEET_NODES)
    member_node_ids: list[NodeId] = Field(max_length=_MAX_FLEET_NODES)
    rank: Rank
    role: Text64
    run_state: RunState
    route_state: RouteState
    rank_state: RunState
    rank_age_seconds: float = Field(ge=0, le=float(_MAX_SIGNED_BIGINT))
    rank_fresh: bool
    group_state: Literal["healthy", "degraded"]
    healthy: bool
    degraded_reason: RunDegradedReason | None = None


class CapacityReservations(_StrictModel):
    disk_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    unified_memory_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    host_memory_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    gpu_memory_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    port_count: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)


class FleetNode(_StrictModel):
    id: NodeId
    display_name: Text200
    hostname: Annotated[str, StringConstraints(max_length=255)]
    lifecycle: Text64
    labels: dict[Text64, Text256] = Field(max_length=64)
    connection: NodeConnection
    inventory: InventoryState | None
    telemetry: TelemetryState | None
    installed: list[RecipePresence] = Field(max_length=512)
    loaded: list[RunPresence] = Field(max_length=512)
    reservations: CapacityReservations
    warnings: list[ProjectionReason] = Field(max_length=128)


class FleetSnapshot(_StrictModel):
    schema_version: Literal[1] = 1
    event_cursor: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    generated_at: datetime
    repository_commit: CommitId
    nodes: list[FleetNode] = Field(max_length=_MAX_FLEET_NODES)


class TelemetryHistoryResponse(_StrictModel):
    schema_version: Literal[1] = 1
    node_id: NodeId
    start: datetime
    end: datetime
    resolution: TelemetryResolution
    maximum_points: int = Field(ge=1, le=1_500)
    points: list[TelemetryPoint | TelemetryRollupPoint] = Field(max_length=1_500)


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


def telemetry_rollup_point(value: TelemetryRollupPointView) -> TelemetryRollupPoint:
    return TelemetryRollupPoint(
        node_id=value.node_id,
        resolution=value.resolution,
        bucket_start=value.bucket_start,
        bucket_end=value.bucket_end,
        source_sample_count=value.source_sample_count,
        gap_samples=value.gap_samples,
        metrics={
            name: TelemetryMetricSummary(
                count=metric.count,
                minimum=metric.minimum,
                mean=metric.mean,
                maximum=metric.maximum,
            )
            for name, metric in value.metrics.items()
        },
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
    """Merge a fixed database query set into enrolled Fleet nodes."""

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
        if (
            type(event_cursor) is not int
            or not 0 <= event_cursor <= 9_223_372_036_854_775_807
        ):
            raise ValueError("Fleet event cursor is invalid")
        commit = self._repository.head()
        current = _utc(self._clock())
        with self._sessions.begin() as session:
            agents = self._registered_agents(session)
            node_ids = tuple(agents)
            profiles = self._node_profiles(session, node_ids)
            certificates = self._current_certificates(session, node_ids, current)
            inventories = self._latest_inventory(session, node_ids)
            telemetry = self._telemetry.latest_in_session(session, node_ids)
            installation_rows = self._installation_rows(session, node_ids)
            run_rows = self._run_rows(session, node_ids)
            mapping_ids = {row[2].id for row in (*installation_rows, *run_rows)}
            mapping_nodes = tuple(
                session.scalars(
                    select(ClusterMappingNode)
                    .where(ClusterMappingNode.mapping_id.in_(mapping_ids))
                    .order_by(ClusterMappingNode.mapping_id, ClusterMappingNode.rank)
                    .limit(_MAX_GROUP_MEMBER_ROWS)
                )
            )
            installed = self._installed_presence(
                installation_rows, mapping_nodes, frozenset(node_ids)
            )
            loaded = self._loaded_presence(
                run_rows, mapping_nodes, frozenset(node_ids), current
            )
            reservations = self._reservations(session, node_ids)
        return FleetSnapshot(
            event_cursor=event_cursor,
            generated_at=current,
            repository_commit=commit,
            nodes=[
                self._node(
                    node_id,
                    profiles.get(node_id),
                    current=current,
                    agent=agents[node_id],
                    certificate=certificates.get(node_id),
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
        resolution: TelemetryResolution,
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
        if node_id not in self._registered_node_ids(node_id):
            raise KeyError(node_id)
        points = self._telemetry.history(
            node_id,
            start_utc,
            end_utc,
            maximum_points,
            resolution=resolution,
        )
        return TelemetryHistoryResponse(
            node_id=node_id,
            start=start_utc,
            end=end_utc,
            resolution=resolution,
            maximum_points=maximum_points,
            points=[
                telemetry_point(value)
                if isinstance(value, TelemetrySampleView)
                else telemetry_rollup_point(value)
                for value in points
            ],
        )

    def _registered_node_ids(self, node_id: str | None = None) -> tuple[str, ...]:
        with self._sessions.begin() as session:
            statement = (
                select(AgentNode.node_id)
                .where(
                    AgentNode.state != "revoked",
                    AgentNode.revoked_at.is_(None),
                )
                .order_by(AgentNode.node_id)
                .limit(_MAX_FLEET_NODES + 1)
            )
            if node_id is not None:
                statement = statement.where(AgentNode.node_id == node_id)
            node_ids = tuple(session.scalars(statement))
        if len(node_ids) > _MAX_FLEET_NODES:
            raise ValueError("Fleet contains more than 500 registered nodes")
        return node_ids

    @staticmethod
    def _registered_agents(session: Session) -> dict[str, AgentNode]:
        rows = tuple(
            session.scalars(
                select(AgentNode)
                .where(
                    AgentNode.state != "revoked",
                    AgentNode.revoked_at.is_(None),
                )
                .order_by(AgentNode.node_id)
                .limit(_MAX_FLEET_NODES + 1)
            )
        )
        if len(rows) > _MAX_FLEET_NODES:
            raise ValueError("Fleet contains more than 500 registered nodes")
        return {row.node_id: row for row in rows}

    @staticmethod
    def _node_profiles(
        session: Session, node_ids: Sequence[str]
    ) -> dict[str, AgentNodeProfile]:
        rows = session.scalars(
            select(AgentNodeProfile)
            .where(AgentNodeProfile.node_id.in_(node_ids))
            .order_by(AgentNodeProfile.node_id)
        )
        return {row.node_id: row for row in rows}

    @staticmethod
    def _current_certificates(
        session: Session, node_ids: Sequence[str], current: datetime
    ) -> dict[str, AgentCertificate]:
        valid = (
            (AgentCertificate.state == "active")
            & (AgentCertificate.revoked_at.is_(None))
            & (AgentCertificate.ca_revoked_at.is_(None))
            & (AgentCertificate.not_before <= current)
            & (AgentCertificate.not_after > current)
        )
        ranked = (
            select(
                AgentCertificate.serial.label("serial"),
                func.row_number()
                .over(
                    partition_by=AgentCertificate.node_id,
                    order_by=(
                        case((valid, 0), else_=1),
                        AgentCertificate.generation.desc(),
                        AgentCertificate.not_after.desc(),
                        AgentCertificate.serial.desc(),
                    ),
                )
                .label("position"),
            )
            .where(AgentCertificate.node_id.in_(node_ids))
            .subquery()
        )
        rows = session.scalars(
            select(AgentCertificate)
            .join(ranked, AgentCertificate.serial == ranked.c.serial)
            .where(ranked.c.position == 1)
            .order_by(AgentCertificate.node_id)
        )
        return {row.node_id: row for row in rows}

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
                .join(
                    ClusterMapping, ClusterMapping.id == RecipeInstallation.mapping_id
                )
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
        fleet_node_ids: frozenset[str],
    ) -> str | None:
        if any(value.node_id not in fleet_node_ids for value in (*expected, *actual)):
            return "external-member"
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
        fleet_node_ids: frozenset[str],
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
            visible_nodes = [node for node in nodes if node.node_id in fleet_node_ids]
            reason = self._exact_group_reason(
                expected_count=mapping.node_count,
                expected=mappings.get(mapping.id, ()),
                actual=nodes,
                fleet_node_ids=fleet_node_ids,
            )
            if reason is None and installation.state != "installed":
                reason = "installation-not-installed"
            if reason is None and any(node.state != "installed" for node in nodes):
                reason = "rank-not-installed"
            if reason is None and any(
                node.installed_bytes < node.required_bytes for node in nodes
            ):
                reason = "rank-incomplete-bytes"
            present_ranks = [node.rank for node in visible_nodes]
            member_node_ids = sorted(node.node_id for node in visible_nodes)
            for node in visible_nodes:
                by_node.setdefault(node.node_id, []).append(
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
        fleet_node_ids: frozenset[str],
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
            visible_nodes = [node for node in nodes if node.node_id in fleet_node_ids]
            reason = self._exact_group_reason(
                expected_count=mapping.node_count,
                expected=mappings.get(mapping.id, ()),
                actual=nodes,
                fleet_node_ids=fleet_node_ids,
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
            present_ranks = [node.rank for node in visible_nodes]
            member_node_ids = sorted(node.node_id for node in visible_nodes)
            for node in visible_nodes:
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
        profile: AgentNodeProfile | None,
        *,
        current: datetime,
        agent: AgentNode,
        certificate: AgentCertificate | None,
        inventory: NodeInventorySnapshot | None,
        telemetry: TelemetrySampleView | None,
        installed: Sequence[RecipePresence],
        loaded: Sequence[RunPresence],
        reservations: Mapping[str, tuple[int, int]],
    ) -> FleetNode:
        warnings: list[ProjectionReason] = []
        connection = self._connection(agent, certificate, current)
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
        labels = {} if profile is None else profile.labels
        if not isinstance(labels, Mapping):
            raise TypeError("Fleet node profile labels are invalid")
        return FleetNode(
            id=node_id,
            display_name=node_id if profile is None else profile.display_name,
            hostname="" if profile is None else profile.hostname,
            lifecycle="managed" if profile is None else profile.lifecycle,
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

    def _connection(
        self,
        value: AgentNode | None,
        certificate: AgentCertificate | None,
        current: datetime,
    ) -> NodeConnection:
        certificate_state = self._certificate_state(certificate, current)
        if value is None:
            return NodeConnection(
                agent_state="unregistered",
                certificate_state=certificate_state,
                online_state="unregistered",
                offline_reason="unregistered",
                last_seen_at=None,
                last_seen_age_seconds=None,
            )
        last_seen = None if value.last_seen_at is None else _utc(value.last_seen_at)
        age = (
            None
            if last_seen is None
            else max(0.0, (current - last_seen).total_seconds())
        )
        if value.state == "revoked" or value.revoked_at is not None:
            offline_reason = "agent-revoked"
        elif value.state != "active":
            offline_reason = "agent-inactive"
        elif certificate_state != "valid":
            offline_reason = f"certificate-{certificate_state}"
        elif last_seen is None:
            offline_reason = "never-seen"
        elif current - last_seen < timedelta(0):
            offline_reason = "last-seen-in-future"
        elif current - last_seen > timedelta(seconds=self._agent_online_seconds):
            offline_reason = "stale"
        else:
            offline_reason = None
        return NodeConnection(
            agent_state=value.state,
            certificate_state=certificate_state,
            online_state="online" if offline_reason is None else "offline",
            offline_reason=offline_reason,
            last_seen_at=last_seen,
            last_seen_age_seconds=age,
        )

    @staticmethod
    def _certificate_state(value: AgentCertificate | None, current: datetime) -> str:
        if value is None:
            return "missing"
        if (
            value.state == "revoked"
            or value.revoked_at is not None
            or value.ca_revoked_at is not None
        ):
            return "revoked"
        if value.state != "active":
            return "inactive"
        if _utc(value.not_before) > current:
            return "not-yet-valid"
        if _utc(value.not_after) <= current:
            return "expired"
        return "valid"

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
