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
from vonk_agent_protocol import (
    CertificateState,
    InstallationNodeState,
    InstallationState,
    InstallDegradedReason,
    NodeOfflineReason,
    ProjectionCode,
    ReservationState,
    RouteState,
    RunDegradedReason,
    RunState,
)
from vonk_agent_protocol.inventory import NetworkInterface
from vonk_forge_contracts import RecipeDefinition, read_recipe

from .auth import CursorError
from .cluster_mappings import mapping_option_choices
from .fleet_events import FleetEventDraft, FleetEventRepository
from .machine_states import (
    CertificateStateField,
    InstallationStateField,
    RouteStateField,
    RunStateField,
    read_state,
)
from .models import (
    AgentCertificate,
    AgentNode,
    AgentNodeProfile,
    AgentPresence,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    FleetProfileApplication,
    InstallationNode,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from .nas_route_notice import nas_route_notice
from .operation_blockers import PHASE_RETRY_CODE, read_blockers
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
# Database rows and decoded JSON carry these closed values as plain strings, so
# they are read back through the declared alias instead of an unchecked
# assignment into the typed projection model.
_AGENT_STATE_ADAPTER = TypeAdapter(AgentState)
_INSTALL_DEGRADED_REASON_ADAPTER = TypeAdapter(InstallDegradedReason)
_RUN_DEGRADED_REASON_ADAPTER = TypeAdapter(RunDegradedReason)
_CERTIFICATE_OFFLINE_REASONS: Mapping[CertificateState, NodeOfflineReason | None] = {
    CertificateState.VALID: None,
    CertificateState.MISSING: NodeOfflineReason.CERTIFICATE_MISSING,
    CertificateState.NOT_YET_VALID: NodeOfflineReason.CERTIFICATE_NOT_YET_VALID,
    CertificateState.EXPIRED: NodeOfflineReason.CERTIFICATE_EXPIRED,
    CertificateState.REVOKED: NodeOfflineReason.CERTIFICATE_REVOKED,
    CertificateState.INACTIVE: NodeOfflineReason.CERTIFICATE_INACTIVE,
}


_INSTALL_PARTIAL_MAX_NAMED = 16
_INSTALL_REASON_WORDS: dict[str, str] = {
    InstallDegradedReason.EXTERNAL_MEMBER: "a member is outside this fleet",
    InstallDegradedReason.MAPPING_INCOMPLETE: "the cluster mapping is incomplete",
    InstallDegradedReason.MISSING_RANKS: "ranks are missing",
    InstallDegradedReason.UNEXPECTED_RANKS: "ranks are unexpected",
    InstallDegradedReason.RANK_MEMBERSHIP_MISMATCH: "ranks are on the wrong Sparks",
    InstallDegradedReason.INSTALLATION_NOT_INSTALLED: "the installation is not installed",
    InstallDegradedReason.RANK_NOT_INSTALLED: "a rank is not installed",
    InstallDegradedReason.RANK_INCOMPLETE_BYTES: "a rank is short of its payload",
}


def _install_partial_warnings(
    installed: Sequence[RecipePresence],
) -> list[ProjectionReason]:
    """One warning per incomplete installation group on the Spark.

    Each names the installation, the rank this Spark holds and the reason, as
    typed evidence and in the text, so the owner sees which one to act on. A
    flood of leftovers is capped and summarized, keeping the warning list
    bounded.
    """

    incomplete = [value for value in installed if not value.complete]
    named = incomplete[:_INSTALL_PARTIAL_MAX_NAMED]
    warnings: list[ProjectionReason] = []
    for value in named:
        reason = value.degraded_reason
        if reason is None:
            reason = InstallDegradedReason.INSTALLATION_NOT_INSTALLED
        words = _INSTALL_REASON_WORDS[reason]
        ranks = (
            f" (rank {', '.join(str(rank) for rank in value.affected_ranks)})"
            if value.affected_ranks
            else ""
        )
        detail = (
            f"{value.title} installation {value.installation_id[:8]} "
            f"rank {value.rank} of {value.expected_rank_count} is incomplete: "
            f"{words}{ranks} "
            f"[{value.group_state}/{value.rank_state}]"
        )
        warnings.append(
            ProjectionReason(
                code=ProjectionCode.INSTALL_PARTIAL,
                detail=detail[:256],
                severity="warning",
                install_partial=InstallPartialEvidence(
                    installation_id=value.installation_id,
                    recipe_id=value.recipe_id,
                    recipe_revision_id=value.recipe_revision_id,
                    title=value.title,
                    rank=value.rank,
                    expected_rank_count=value.expected_rank_count,
                    present_ranks=value.present_ranks,
                    affected_ranks=value.affected_ranks,
                    reason=reason,
                    group_state=value.group_state,
                    rank_state=value.rank_state,
                    installed_bytes=value.installed_bytes,
                    required_bytes=value.required_bytes,
                ),
            )
        )
    if len(incomplete) > len(named):
        warnings.append(
            ProjectionReason(
                code=ProjectionCode.INSTALL_PARTIAL,
                detail=(
                    f"{len(incomplete) - len(named)} more recipe installation "
                    "groups are incomplete."
                ),
                severity="warning",
            )
        )
    return warnings


def _install_degraded_reason(
    value: str | None,
) -> InstallDegradedReason | None:
    """Read one shared group reason as an installation degraded reason."""

    if value is None:
        return None
    return _INSTALL_DEGRADED_REASON_ADAPTER.validate_python(str(value), strict=True)


def _run_degraded_reason(value: str | None) -> RunDegradedReason | None:
    """Read one shared group reason as a run degraded reason."""

    if value is None:
        return None
    return _RUN_DEGRADED_REASON_ADAPTER.validate_python(str(value), strict=True)


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


class InstallPartialEvidence(_StrictModel):
    """Why one recipe installation group on a Spark is not complete."""

    installation_id: Text128
    recipe_id: Text128
    recipe_revision_id: Text128
    title: Text200
    #: The rank this Spark holds in the installation.
    rank: Rank
    expected_rank_count: int = Field(ge=1, le=_MAX_FLEET_NODES)
    present_ranks: list[Rank] = Field(max_length=_MAX_FLEET_NODES)
    #: Ranks the reason names (not installed, or short of their payload);
    #: empty for a reason about the group as a whole.
    affected_ranks: list[Rank] = Field(max_length=_MAX_FLEET_NODES)
    reason: InstallDegradedReason
    group_state: InstallationStateField
    rank_state: InstallationStateField
    installed_bytes: int | None = Field(default=None, ge=0, le=_MAX_SIGNED_BIGINT)
    required_bytes: int | None = Field(default=None, ge=0, le=_MAX_SIGNED_BIGINT)


class ProjectionReason(_StrictModel):
    code: ProjectionCode
    detail: Text256
    severity: Literal["info", "warning", "error"]
    #: Typed evidence for ``install.partial``: which installation, which rank
    #: and which reason. Absent for every other code.
    install_partial: InstallPartialEvidence | None = None
    recommendation: Text256 | None = None


class NodeConnection(_StrictModel):
    agent_state: AgentState
    certificate_state: CertificateStateField
    online_state: Literal["online", "offline", "unregistered"]
    offline_reason: NodeOfflineReason | None
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
    network_interfaces: list[NetworkInterface] | None = Field(
        default=None, max_length=16
    )
    nas_route_interface: str | None = Field(default=None, max_length=15)


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
    group_state: InstallationStateField
    rank_state: InstallationStateField
    complete: bool
    degraded_reason: InstallDegradedReason | None = None
    affected_ranks: list[Rank] = Field(
        default_factory=list, max_length=_MAX_FLEET_NODES
    )
    installed_bytes: int | None = Field(default=None, ge=0, le=_MAX_SIGNED_BIGINT)
    required_bytes: int | None = Field(default=None, ge=0, le=_MAX_SIGNED_BIGINT)


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
    run_state: RunStateField
    route_state: RouteStateField
    rank_state: RunStateField
    rank_age_seconds: float = Field(ge=0, le=float(_MAX_SIGNED_BIGINT))
    rank_fresh: bool
    group_state: Literal["healthy", "degraded"]
    healthy: bool
    degraded_reason: RunDegradedReason | None = None
    # Why the route is not published, when the Controller withdrew it.
    route_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    # The recipe option choices this run was started with; empty when the
    # recipe declares none.
    option_choices: dict[Text64, Text64] = Field(default_factory=dict, max_length=16)
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
            stalls = self._stalled_loads(session, node_ids)
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
                    stalls=stalls.get(node_id, ()),
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
                RecipeInstallation.state != InstallationState.UNINSTALLED,
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
            return InstallDegradedReason.EXTERNAL_MEMBER
        expected_ranks = [value.rank for value in expected]
        actual_ranks = [value.rank for value in actual]
        if len(expected) != expected_count or expected_ranks != list(
            range(expected_count)
        ):
            return InstallDegradedReason.MAPPING_INCOMPLETE
        missing = set(expected_ranks) - set(actual_ranks)
        if missing:
            return InstallDegradedReason.MISSING_RANKS
        unexpected = set(actual_ranks) - set(expected_ranks)
        if unexpected or len(actual_ranks) != expected_count:
            return InstallDegradedReason.UNEXPECTED_RANKS
        expected_members = {
            (value.rank, value.node_id, value.role) for value in expected
        }
        actual_members = {(value.rank, value.node_id, value.role) for value in actual}
        if actual_members != expected_members:
            return InstallDegradedReason.RANK_MEMBERSHIP_MISMATCH
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
                        group_state=read_state(InstallationState, installation.state),
                        rank_state=read_state(InstallationState, node.state),
                        complete=reason is None,
                        degraded_reason=reason,
                        affected_ranks=affected,
                        installed_bytes=node.installed_bytes,
                        required_bytes=expectations.get(node.node_id),
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
            if run.state in {RunState.STOPPED, RunState.FAILED, RunState.LOST}:
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
                        run_state=read_state(RunState, run.state),
                        route_state=read_state(RouteState, run.route_state),
                        rank_state=read_state(RunState, node.state),
                        rank_age_seconds=rank_age,
                        rank_fresh=rank_fresh,
                        group_state="healthy" if reason is None else "degraded",
                        healthy=reason is None,
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
                RecipeRun.state.not_in(
                    {RunState.STOPPED, RunState.FAILED, RunState.LOST}
                ),
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
                ResourceReservation.state.in_(
                    (ReservationState.ACTIVE, ReservationState.PROMISED)
                ),
            )
            .group_by(ResourceReservation.node_id, ResourceReservation.kind)
            .order_by(ResourceReservation.node_id, ResourceReservation.kind)
        )
        values: dict[str, dict[str, tuple[int, int]]] = {}
        for node_id, kind, amount, count in rows:
            values.setdefault(node_id, {})[kind] = (int(amount or 0), int(count))
        return values

    @staticmethod
    def _stalled_loads(
        session: Session, node_ids: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        """What each Spark's live profile load keeps retrying, by the load's own words.

        The application mirrors the stall of its child (the same typed blocker
        the child shows), so this reads that one fact instead of deriving a
        second classification. An unreadable document yields no stall.
        """

        stalls: dict[str, list[str]] = {}
        known = frozenset(node_ids)
        for progress in session.scalars(
            select(FleetProfileApplication.progress)
            .where(FleetProfileApplication.state == "running")
            .order_by(FleetProfileApplication.id)
            .limit(_MAX_OPERATIONAL_GROUPS)
        ):
            raw = progress.get("blockers") if isinstance(progress, Mapping) else None
            for blocker in read_blockers(raw):
                if blocker.code != PHASE_RETRY_CODE:
                    continue
                for node_id in blocker.node_ids:
                    if node_id in known:
                        stalls.setdefault(node_id, []).append(blocker.detail)
        return {node_id: tuple(details) for node_id, details in stalls.items()}

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
        stalls: Sequence[str] = (),
    ) -> FleetNode:
        warnings: list[ProjectionReason] = []
        connection = self._connection(agent, certificate, current)
        inventory_state = self._inventory(inventory, current)
        telemetry_state = self._telemetry_state(telemetry, current)
        if connection.online_state != "online":
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.NODE_OFFLINE,
                    detail="The authenticated agent is not currently online.",
                    severity="warning",
                )
            )
        if inventory_state is None:
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.INVENTORY_MISSING,
                    detail="No admission inventory snapshot is available.",
                    severity="warning",
                )
            )
        elif inventory_state.freshness == "stale":
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.INVENTORY_STALE,
                    detail="Admission inventory is stale.",
                    severity="warning",
                )
            )
        route_notice = (
            None
            if inventory_state is None
            else nas_route_notice(
                inventory_state.network_interfaces, inventory_state.nas_route_interface
            )
        )
        if route_notice is not None:
            warnings.append(
                ProjectionReason(
                    code=route_notice.code,
                    detail=route_notice.detail,
                    severity="warning",
                    recommendation=route_notice.recommendation,
                )
            )
        if telemetry_state is None:
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.TELEMETRY_MISSING,
                    detail="No telemetry sample is available.",
                    severity="warning",
                )
            )
        elif telemetry_state.freshness == "delayed":
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.TELEMETRY_DELAYED,
                    detail="Telemetry delivery is delayed.",
                    severity="warning",
                )
            )
        elif telemetry_state.freshness == "stale":
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.TELEMETRY_STALE,
                    detail="Telemetry is stale.",
                    severity="warning",
                )
            )
        if telemetry_state is not None and telemetry_state.freshness != "stale":
            low_clock = sustained_low_cpu_clock(recent_telemetry)
            if low_clock is not None:
                warnings.append(
                    ProjectionReason(
                        code=ProjectionCode.CPU_LOW_CLOCK,
                        detail=_low_clock_detail(low_clock),
                        severity="warning",
                    )
                )
        warnings.extend(_install_partial_warnings(installed))
        for detail in stalls:
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.PROFILE_RETRYING,
                    detail=f"A profile load keeps retrying: {detail}"[:256],
                    severity="warning",
                )
            )
        if any(not value.healthy for value in loaded):
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.RUN_DEGRADED,
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
                offline_reason=NodeOfflineReason.UNREGISTERED,
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
            offline_reason: NodeOfflineReason | None = NodeOfflineReason.AGENT_REVOKED
        elif value.state != "active":
            offline_reason = NodeOfflineReason.AGENT_INACTIVE
        elif certificate_state != "valid":
            offline_reason = _CERTIFICATE_OFFLINE_REASONS[certificate_state]
        elif last_seen is None:
            offline_reason = NodeOfflineReason.NEVER_SEEN
        elif current - last_seen < timedelta(0):
            offline_reason = NodeOfflineReason.LAST_SEEN_IN_FUTURE
        elif current - last_seen > timedelta(seconds=self._agent_online_seconds):
            offline_reason = NodeOfflineReason.STALE
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
            return CertificateState.MISSING
        if (
            value.state == "revoked"
            or value.revoked_at is not None
            or value.ca_revoked_at is not None
        ):
            return CertificateState.REVOKED
        if value.state != "active":
            return CertificateState.INACTIVE
        if _utc(value.not_before) > current:
            return CertificateState.NOT_YET_VALID
        if _utc(value.not_after) <= current:
            return CertificateState.EXPIRED
        return CertificateState.VALID

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
            network_interfaces=(
                None
                if value.network_interfaces is None
                else [NetworkInterface(**item) for item in value.network_interfaces]
            ),
            nas_route_interface=value.nas_route_interface,
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
