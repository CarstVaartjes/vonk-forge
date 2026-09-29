"""Bounded typed projection of PostgreSQL-authoritative Fleet state."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
)
from sqlalchemy import Row, case, func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import RecipeDefinition, read_recipe

from .auth import CursorError
from .fleet_events import FleetEventDraft, FleetEventRepository
from .models import (
    AgentCertificate,
    AgentNode,
    AgentNodeProfile,
    AgentPresence,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_installation_plan,
)
from .recipe_update_notice import (
    RECIPE_UPDATE_AVAILABLE,
    RecipeUpdateNotice,
    newest_active_revisions,
    recipe_update_notice,
)
from .strict_json import StrictJSONModel
from .telemetry import (
    CPU_LOW_CLOCK_MIN_SECONDS,
    TelemetryRepository,
    TelemetrySampleView,
    sustained_low_cpu_clock,
)

_REVISION_PATTERN = r"^[0-9a-f]{64}$"
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


def _canonical_recipe(revision: CatalogDocumentRevision) -> RecipeDefinition | None:
    if (
        revision.kind != "recipe"
        or revision.schema_version != 2
        or revision.state != "active"
    ):
        return None
    try:
        return read_recipe(revision.document)
    except (TypeError, ValueError):
        return None


def _installation_payload_expectations(plan: object) -> dict[str, int]:
    """Per-node materialized payload expectation from the persisted plan.

    ``RecipeInstallation.plan`` is the durable admission record and already
    carries each node's admitted payload, so the presence byte check reads the
    expectation from there instead of duplicating it into a column.  A plan
    admitted before the expectation existed, or one that no longer reads as
    the current contract, yields no expectation: an absent expectation is not
    evidence that an installation is short, and this projection must not turn
    a display annotation into a fleet-wide read failure.
    """

    try:
        stored = parse_stored_installation_plan(plan)
    except RecipeExecutionContractError:
        return {}
    return {
        node.node_id: node.required_payload_bytes
        for node in stored.nodes
        if node.required_payload_bytes is not None
    }


NodeId = Annotated[str, StringConstraints(pattern=_NODE_PATTERN)]
UuidId = Annotated[str, StringConstraints(pattern=_UUID_PATTERN)]
BootId = Annotated[str, StringConstraints(pattern=_BOOT_UUID_PATTERN)]
AuthorityRevision = Annotated[str, StringConstraints(pattern=_REVISION_PATTERN)]
# vonkctl qualification compares this snapshot field across views.  The
# Controller keeps no revisioned authority document, so the value is the fixed
# digest of the empty document the retired authority head always named.
_AUTHORITY_REVISION = "ffb039e4a059e137e67110b3f845ab477b048e97ae9383031feb9dcfcbcbba55"
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

# Database rows and decoded JSON carry these closed values as plain strings, so
# they are read back through the declared alias instead of an unchecked
# assignment into the typed projection model.
_AGENT_STATE_ADAPTER = TypeAdapter(AgentState)
_INSTALL_DEGRADED_REASON_ADAPTER = TypeAdapter(InstallDegradedReason)
_RUN_DEGRADED_REASON_ADAPTER = TypeAdapter(RunDegradedReason)
_CERTIFICATE_OFFLINE_REASONS: Mapping[CertificateState, OfflineReason | None] = {
    "valid": None,
    "missing": "certificate-missing",
    "not-yet-valid": "certificate-not-yet-valid",
    "expired": "certificate-expired",
    "revoked": "certificate-revoked",
    "inactive": "certificate-inactive",
}


def _install_degraded_reason(
    value: str | None,
) -> InstallDegradedReason | None:
    """Read one shared group reason as an installation degraded reason."""

    if value is None:
        return None
    return _INSTALL_DEGRADED_REASON_ADAPTER.validate_python(value, strict=True)


def _run_degraded_reason(value: str | None) -> RunDegradedReason | None:
    """Read one shared group reason as a run degraded reason."""

    if value is None:
        return None
    return _RUN_DEGRADED_REASON_ADAPTER.validate_python(value, strict=True)


class _StrictModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


# Extra history beyond the one-minute rule, so a slightly late sample still
# leaves an unbroken run to measure.
_LOW_CLOCK_LOOKBACK_SLACK = 30


def _low_clock_detail(sample: TelemetrySampleView) -> str:
    average = sample.cpu_frequency_avg_mhz
    maximum = sample.cpu_frequency_max_mhz
    context = (
        f"{sample.gpu_temperature_c} °C"
        if sample.gpu_temperature_c is not None
        else f"{sample.gpu_utilization_percent:.0f}% GPU load"
    )
    return (
        f"CPU clock is low while hot or busy: {average} of {maximum} MHz at "
        f"{context}. Thermal or power throttling is possible."
    )


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
        "recipe.update_available",
        "cpu.low-clock",
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


class TelemetryPoint(_StrictModel):
    model_config = ConfigDict(regex_engine="python-re")

    id: UuidId
    node_id: NodeId
    boot_id: BootId
    observed_at: datetime
    received_at: datetime
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
    gpu_temperature_c: int | None = Field(default=None, ge=0, le=150)
    cpu_frequency_avg_mhz: int | None = Field(default=None, ge=1, le=20_000)
    cpu_frequency_min_mhz: int | None = Field(default=None, ge=1, le=20_000)
    cpu_frequency_max_mhz: int | None = Field(default=None, ge=1, le=20_000)


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
    # Set when a newer revision of this recipe exists; informational only.
    recipe_update: RecipeUpdateNotice | None = None


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
    ip_address: Annotated[str, StringConstraints(max_length=45)] | None = None
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
    event_cursor: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    generated_at: datetime
    authority_revision: AuthorityRevision
    nodes: list[FleetNode] = Field(max_length=_MAX_FLEET_NODES)


class FleetNodeIdentity(_StrictModel):
    id: NodeId
    display_name: Text200
    hostname: Annotated[str, StringConstraints(max_length=255)]
    ip_address: Annotated[str, StringConstraints(max_length=45)] | None = None


def telemetry_point(value: TelemetrySampleView) -> TelemetryPoint:
    # The mTLS identity is authoritative for node ownership.  The receive
    # timestamp is assigned by the Controller, so neither can be spoofed by a
    # producer embedded in the report.
    return TelemetryPoint(
        id=value.id,
        node_id=value.node_id,
        boot_id=str(value.boot_id),
        observed_at=value.observed_at,
        received_at=value.received_at,
        memory_total_bytes=value.memory_total_bytes,
        memory_available_bytes=value.memory_available_bytes,
        disk_total_bytes=value.disk_total_bytes,
        disk_free_bytes=value.disk_free_bytes,
        gpu_utilization_percent=value.gpu_utilization_percent,
        gpu_memory_total_bytes=value.gpu_memory_total_bytes,
        gpu_memory_free_bytes=value.gpu_memory_free_bytes,
        gpu_temperature_c=value.gpu_temperature_c,
        cpu_frequency_avg_mhz=value.cpu_frequency_avg_mhz,
        cpu_frequency_min_mhz=value.cpu_frequency_min_mhz,
        cpu_frequency_max_mhz=value.cpu_frequency_max_mhz,
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


type RunPresenceRow = Row[
    RunNode,
    RecipeRun,
    ClusterMapping,
    RecipeInstallation,
    CatalogDocumentRevision,
    CatalogDocument,
]

type InstallationPresenceRow = Row[
    InstallationNode,
    RecipeInstallation,
    ClusterMapping,
    CatalogDocumentRevision,
    CatalogDocument,
]


class FleetProjection:
    """Merge a fixed database query set into enrolled Fleet nodes."""

    def __init__(
        self,
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

    def update_display_name(self, node_id: str, display_name: str) -> FleetNodeIdentity:
        """Persist an operator alias without changing the node's technical identity."""

        with self._sessions.begin() as session:
            node = session.get(AgentNode, node_id)
            if node is None or node.state == "revoked" or node.revoked_at is not None:
                raise KeyError(node_id)
            profile = session.get(AgentNodeProfile, node_id)
            if profile is None:
                raise KeyError(node_id)
            presence = session.get(AgentPresence, node_id)
            if profile.display_name != display_name:
                profile.display_name = display_name
                self._events.append_in_session(
                    session,
                    FleetEventDraft(
                        event_type="node-profile",
                        node_id=node_id,
                        entity_kind="node-profile",
                        entity_id=node_id,
                        payload={
                            "node_id": node_id,
                            "display_name_changed": True,
                        },
                    ),
                )
            return FleetNodeIdentity(
                id=node_id,
                display_name=profile.display_name,
                hostname=profile.hostname,
                ip_address=(None if presence is None else presence.management_address),
            )

    def read_at(self, event_cursor: int) -> FleetSnapshot:
        if (
            type(event_cursor) is not int
            or not 0 <= event_cursor <= 9_223_372_036_854_775_807
        ):
            raise CursorError("Fleet event cursor is invalid")
        current = _utc(self._clock())
        with self._sessions.begin() as session:
            agents = self._registered_agents(session)
            node_ids = tuple(agents)
            profiles = self._node_profiles(session, node_ids)
            presences = self._node_presences(session, node_ids)
            certificates = self._current_certificates(session, node_ids, current)
            inventories = self._latest_inventory(session, node_ids)
            telemetry = self._telemetry.latest_in_session(session, node_ids)
            recent_telemetry = self._telemetry.recent_in_session(
                session,
                node_ids,
                current
                - timedelta(
                    seconds=CPU_LOW_CLOCK_MIN_SECONDS + _LOW_CLOCK_LOOKBACK_SLACK
                ),
            )
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
            newest = newest_active_revisions(session, {row[5].id for row in run_rows})
            loaded = self._loaded_presence(
                run_rows, mapping_nodes, frozenset(node_ids), current, newest
            )
            reservations = self._reservations(session, node_ids)
        return FleetSnapshot(
            event_cursor=event_cursor,
            generated_at=current,
            authority_revision=_AUTHORITY_REVISION,
            nodes=[
                self._node(
                    node_id,
                    profiles.get(node_id),
                    presences.get(node_id),
                    current=current,
                    agent=agents[node_id],
                    certificate=certificates.get(node_id),
                    inventory=inventories.get(node_id),
                    telemetry=telemetry.get(node_id),
                    recent_telemetry=recent_telemetry.get(node_id, ()),
                    installed=installed.get(node_id, ()),
                    loaded=loaded.get(node_id, ()),
                    reservations=reservations.get(node_id, {}),
                )
                for node_id in node_ids
            ],
        )

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
    def _node_presences(
        session: Session, node_ids: Sequence[str]
    ) -> dict[str, AgentPresence]:
        rows = session.scalars(
            select(AgentPresence)
            .where(AgentPresence.node_id.in_(node_ids))
            .order_by(AgentPresence.node_id)
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
    def _installation_rows(
        session: Session, node_ids: Sequence[str]
    ) -> tuple[InstallationPresenceRow, ...]:
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
                    CatalogDocumentRevision,
                    CatalogDocument,
                )
                .join(
                    RecipeInstallation,
                    RecipeInstallation.id == InstallationNode.installation_id,
                )
                .join(
                    ClusterMapping, ClusterMapping.id == RecipeInstallation.mapping_id
                )
                .join(
                    CatalogDocumentRevision,
                    CatalogDocumentRevision.id == RecipeInstallation.recipe_revision_id,
                )
                .join(
                    CatalogDocument,
                    CatalogDocument.id == CatalogDocumentRevision.document_id,
                )
                .where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.schema_version == 2,
                    CatalogDocumentRevision.state == "active",
                )
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
        rows: Sequence[InstallationPresenceRow],
        mapping_rows: Sequence[ClusterMappingNode],
        fleet_node_ids: frozenset[str],
    ) -> dict[str, tuple[RecipePresence, ...]]:
        mappings = self._mapping_members(mapping_rows)
        grouped: dict[str, list[InstallationPresenceRow]] = {}
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
            if _canonical_recipe(revision) is None:
                # An ineligible revision is simply not this projection's
                # business. A damaged active one cannot pass unnoticed: the ORM
                # refuses to commit a stored document that no longer hashes to
                # its recorded digest, so the read fails instead of returning a
                # node with nothing installed.
                continue
            visible_nodes = [node for node in nodes if node.node_id in fleet_node_ids]
            reason = _install_degraded_reason(
                self._exact_group_reason(
                    expected_count=mapping.node_count,
                    expected=mappings.get(mapping.id, ()),
                    actual=nodes,
                    fleet_node_ids=fleet_node_ids,
                )
            )
            if reason is None and installation.state != "installed":
                reason = "installation-not-installed"
            if reason is None and any(node.state != "installed" for node in nodes):
                reason = "rank-not-installed"
            if reason is None:
                expectations = _installation_payload_expectations(installation.plan)
                if any(
                    node.installed_bytes < expectations[node.node_id]
                    for node in nodes
                    if node.node_id in expectations
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
        rows: Sequence[RunPresenceRow],
        mapping_rows: Sequence[ClusterMappingNode],
        fleet_node_ids: frozenset[str],
        current: datetime,
        newest: Mapping[str, CatalogDocumentRevision] | None = None,
    ) -> dict[str, tuple[RunPresence, ...]]:
        mappings = self._mapping_members(mapping_rows)
        grouped: dict[str, list[RunPresenceRow]] = {}
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
            # As above: a damaged active revision fails the read at commit, so
            # this only ever skips a run that is genuinely ineligible.
            if _canonical_recipe(revision) is None:
                continue
            visible_nodes = [node for node in nodes if node.node_id in fleet_node_ids]
            reason = _run_degraded_reason(
                self._exact_group_reason(
                    expected_count=mapping.node_count,
                    expected=mappings.get(mapping.id, ()),
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
            update = recipe_update_notice(
                recipe.title, revision, (newest or {}).get(recipe.id)
            )
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
                        recipe_update=update,
                    )
                )
        return {
            node_id: tuple(sorted(values, key=lambda value: (value.run_id, value.rank)))
            for node_id, values in by_node.items()
        }

    @staticmethod
    def _run_rows(
        session: Session, node_ids: Sequence[str]
    ) -> tuple[RunPresenceRow, ...]:
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
                    CatalogDocumentRevision,
                    CatalogDocument,
                )
                .join(RecipeRun, RecipeRun.id == RunNode.run_id)
                .join(ClusterMapping, ClusterMapping.id == RecipeRun.mapping_id)
                .join(
                    RecipeInstallation,
                    RecipeInstallation.id == RecipeRun.installation_id,
                )
                .join(
                    CatalogDocumentRevision,
                    CatalogDocumentRevision.id == RecipeInstallation.recipe_revision_id,
                )
                .join(
                    CatalogDocument,
                    CatalogDocument.id == CatalogDocumentRevision.document_id,
                )
                .where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.schema_version == 2,
                    CatalogDocumentRevision.state == "active",
                )
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
                func.count(
                    func.distinct(
                        case(
                            (
                                ResourceReservation.kind == "port",
                                ResourceReservation.resource_key,
                            ),
                            else_=ResourceReservation.id,
                        )
                    )
                ),
            )
            .where(
                ResourceReservation.node_id.in_(node_ids),
                ResourceReservation.state.in_(("active", "promised")),
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
        presence: AgentPresence | None,
        *,
        current: datetime,
        agent: AgentNode,
        certificate: AgentCertificate | None,
        inventory: NodeInventorySnapshot | None,
        telemetry: TelemetrySampleView | None,
        recent_telemetry: Sequence[TelemetrySampleView],
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
        if telemetry_state is not None and telemetry_state.freshness != "stale":
            low_clock = sustained_low_cpu_clock(recent_telemetry)
            if low_clock is not None:
                warnings.append(
                    ProjectionReason(
                        code="cpu.low-clock",
                        detail=_low_clock_detail(low_clock),
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
        for value in loaded:
            if value.recipe_update is not None:
                warnings.append(
                    ProjectionReason(
                        code=RECIPE_UPDATE_AVAILABLE,
                        detail=value.recipe_update.detail,
                        severity="info",
                    )
                )
        labels = {} if profile is None else profile.labels
        if not isinstance(labels, Mapping):
            raise TypeError("Fleet node profile labels are invalid")
        return FleetNode(
            id=node_id,
            display_name=node_id if profile is None else profile.display_name,
            hostname="" if profile is None else profile.hostname,
            ip_address=(None if presence is None else presence.management_address),
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
            offline_reason: OfflineReason | None = "agent-revoked"
        elif value.state != "active":
            offline_reason = "agent-inactive"
        elif certificate_state != "valid":
            offline_reason = _CERTIFICATE_OFFLINE_REASONS[certificate_state]
        elif last_seen is None:
            offline_reason = "never-seen"
        elif current - last_seen < timedelta(0):
            offline_reason = "last-seen-in-future"
        elif current - last_seen > timedelta(seconds=self._agent_online_seconds):
            offline_reason = "stale"
        else:
            offline_reason = None
        return NodeConnection(
            agent_state=_AGENT_STATE_ADAPTER.validate_python(value.state, strict=True),
            certificate_state=certificate_state,
            online_state="online" if offline_reason is None else "offline",
            offline_reason=offline_reason,
            last_seen_at=last_seen,
            last_seen_age_seconds=age,
        )

    @staticmethod
    def _certificate_state(
        value: AgentCertificate | None, current: datetime
    ) -> CertificateState:
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
