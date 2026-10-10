"""Fleet projection: common concerns."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, TypeAdapter
from sqlalchemy import Row
from vonk_agent_protocol import (
    CertificateState,
    InstallDegradedReason,
    NodeOfflineReason,
    ProjectionCode,
    RunDegradedReason,
)
from vonk_agent_protocol.inventory import NetworkInterface
from vonk_agent_protocol.telemetry import GpuUnavailableReason
from vonk_forge_contracts import RecipeDefinition, document_sha256, read_recipe

from ..machine_states import (
    CertificateStateField,
    InstallationStateField,
    RouteStateField,
    RunStateField,
)
from ..metrics_contract import PrometheusAttention
from ..models import (
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_installation_plan,
)
from ..recipe_update_notice import RecipeUpdateNotice
from ..strict_json import StrictModel
from ..telemetry import TelemetrySampleView

"""Bounded typed projection of PostgreSQL-authoritative Fleet state."""


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
        if document_sha256(revision.document) != revision.content_digest:
            return None
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


_AUTHORITY_REVISION = "ffb039e4a059e137e67110b3f845ab477b048e97ae9383031feb9dcfcbcbba55"


Text32 = Annotated[str, StringConstraints(min_length=1, max_length=32)]


Text64 = Annotated[str, StringConstraints(min_length=1, max_length=64)]


Text128 = Annotated[str, StringConstraints(min_length=1, max_length=128)]


Text200 = Annotated[str, StringConstraints(min_length=1, max_length=200)]


Text256 = Annotated[str, StringConstraints(min_length=1, max_length=256)]


Rank = Annotated[int, Field(ge=0, le=_MAX_FLEET_NODES - 1)]


_MEMBER_COORDINATES = TypeAdapter(tuple[Rank, Text64])


AgentState = Literal["unregistered", "pending", "active", "retired", "revoked"]


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
    installed: Sequence[InstallationPresence],
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
        if (
            isinstance(value, UnavailableRecipePresence)
            or value.projection_issue is not None
        ):
            warnings.append(
                ProjectionReason(
                    code=ProjectionCode.INSTALL_PARTIAL,
                    detail=f"{value.title or value.installation_id}: {value.projection_issue}"[
                        :256
                    ],
                    severity="warning",
                )
            )
            continue
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


class InstallPartialEvidence(StrictModel):
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
    installed_bytes: Annotated[int, Field(ge=0, le=_MAX_SIGNED_BIGINT)] | None = None
    required_bytes: Annotated[int, Field(ge=0, le=_MAX_SIGNED_BIGINT)] | None = None


class ProjectionReason(StrictModel):
    code: ProjectionCode
    detail: Text256
    severity: Literal["info", "warning", "error"]
    #: Typed evidence for ``install.partial``: which installation, which rank
    #: and which reason. Absent for every other code.
    install_partial: InstallPartialEvidence | None = None
    recommendation: Text256 | None = None


class NodeConnection(StrictModel):
    agent_state: AgentState
    certificate_state: CertificateStateField
    online_state: Literal["online", "offline", "unregistered"]
    offline_reason: NodeOfflineReason | None
    last_seen_at: datetime | None
    last_seen_age_seconds: float | None = Field(ge=0, le=float(_MAX_SIGNED_BIGINT))


class InventoryState(StrictModel):
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
    fabric_bandwidth_mbps: Annotated[int, Field(ge=1, le=_MAX_SIGNED_BIGINT)] | None = (
        None
    )
    nvidia_driver_version: Text256
    container_runtime_version: Text256
    network_interfaces: list[NetworkInterface] | None = Field(
        default=None, max_length=16
    )
    nas_route_interface: str | None = Field(default=None, max_length=15)


class TelemetryPoint(StrictModel):
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
    gpu_unavailable_reason: GpuUnavailableReason | None = None
    gpu_temperature_c: int | None = Field(default=None, ge=0, le=150)
    cpu_frequency_avg_mhz: int | None = Field(default=None, ge=1, le=20_000)
    cpu_frequency_min_mhz: int | None = Field(default=None, ge=1, le=20_000)
    cpu_frequency_max_mhz: int | None = Field(default=None, ge=1, le=20_000)


class TelemetryState(StrictModel):
    age_seconds: float = Field(ge=0, le=float(_MAX_SIGNED_BIGINT))
    freshness: Literal["live", "delayed", "stale"]
    sample: TelemetryPoint


class RecipePresence(StrictModel):
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
    complete: bool | None
    projection_issue: str | None = Field(default=None, max_length=256)
    degraded_reason: InstallDegradedReason | None = None
    affected_ranks: list[Rank] = Field(
        default_factory=list, max_length=_MAX_FLEET_NODES
    )
    installed_bytes: Annotated[int, Field(ge=0, le=_MAX_SIGNED_BIGINT)] | None = None
    required_bytes: Annotated[int, Field(ge=0, le=_MAX_SIGNED_BIGINT)] | None = None


class RunPresence(StrictModel):
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
    group_state: Literal["healthy", "degraded", "unavailable"]
    healthy: bool | None
    projection_issue: str | None = Field(default=None, max_length=256)
    degraded_reason: RunDegradedReason | None = None
    # Why the route is not published, when the Controller withdrew it.
    route_reason: Annotated[str, StringConstraints(max_length=512)] | None = None
    # The recipe option choices this run was started with; empty when the
    # recipe declares none.
    option_choices: dict[Text64, Text64] = Field(default_factory=dict, max_length=16)
    # Set when a newer revision of this recipe exists; informational only.
    recipe_update: RecipeUpdateNotice | None = None


class UnavailableRecipePresence(StrictModel):
    """Known membership whose stored group evidence cannot be projected."""

    installation_id: Text128
    projection_issue: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    recipe_id: Text128 | None = None
    recipe_revision_id: Text128 | None = None
    title: Text200 | None = None
    topology_name: Text64 | None = None
    expected_rank_count: Annotated[int, Field(ge=1, le=_MAX_FLEET_NODES)] | None = None
    present_ranks: Annotated[list[Rank], Field(max_length=_MAX_FLEET_NODES)] | None = (
        None
    )
    member_node_ids: (
        Annotated[list[NodeId], Field(max_length=_MAX_FLEET_NODES)] | None
    ) = None
    rank: Rank | None = None
    role: Text64 | None = None
    group_state: InstallationStateField | None = None
    rank_state: InstallationStateField | None = None
    complete: None
    degraded_reason: None = None
    affected_ranks: None = None
    installed_bytes: Annotated[int, Field(ge=0, le=_MAX_SIGNED_BIGINT)] | None = None
    required_bytes: None = None


class UnavailableRunPresence(StrictModel):
    run_id: Text128
    projection_issue: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    installation_id: Text128 | None = None
    recipe_id: Text128 | None = None
    recipe_revision_id: Text128 | None = None
    title: Text200 | None = None
    alias: Text128 | None = None
    expected_rank_count: Annotated[int, Field(ge=1, le=_MAX_FLEET_NODES)] | None = None
    present_ranks: Annotated[list[Rank], Field(max_length=_MAX_FLEET_NODES)] | None = (
        None
    )
    member_node_ids: (
        Annotated[list[NodeId], Field(max_length=_MAX_FLEET_NODES)] | None
    ) = None
    rank: Rank | None = None
    role: Text64 | None = None
    run_state: RunStateField | None = None
    route_state: RouteStateField | None = None
    rank_state: RunStateField | None = None
    rank_age_seconds: None = None
    rank_fresh: None = None
    group_state: Literal["unavailable"] = "unavailable"
    healthy: None
    degraded_reason: None = None
    route_reason: None = None
    option_choices: None = None
    recipe_update: None = None


InstallationPresence = RecipePresence | UnavailableRecipePresence


LoadedPresence = RunPresence | UnavailableRunPresence


def _unavailable_presence[M: StrictModel](
    identity: M, observed: Sequence[tuple[str, object]]
) -> M:
    """Retain each independently validating field, never repair or invent it."""
    model = type(identity)
    document = identity.model_dump()
    for name, value in observed:
        try:
            model.model_validate({**document, name: value}, strict=True)
        except (TypeError, ValueError):
            continue
        document[name] = value
    return model.model_validate(document, strict=True)


class CapacityReservations(StrictModel):
    disk_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    unified_memory_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    host_memory_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    gpu_memory_bytes: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    port_count: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)


class FleetNode(StrictModel):
    id: NodeId
    display_name: Text200
    hostname: Annotated[str, StringConstraints(max_length=255)]
    ip_address: Annotated[str, StringConstraints(max_length=45)] | None = None
    lifecycle: Text64
    labels: dict[Text64, Text256] | None = Field(max_length=64)
    projection_issues: list[Text256] | None = Field(default=None, max_length=16)
    connection: NodeConnection
    inventory: InventoryState | None
    telemetry: TelemetryState | None
    installed: list[InstallationPresence]
    loaded: list[LoadedPresence]
    reservations: CapacityReservations
    warnings: list[ProjectionReason] = Field(max_length=128)


class FleetSnapshot(StrictModel):
    event_cursor: int = Field(ge=0, le=_MAX_SIGNED_BIGINT)
    generated_at: datetime
    authority_revision: AuthorityRevision
    nodes: list[FleetNode]
    attention: list[PrometheusAttention] | None = None
    attention_unavailable: bool | None = None


class FleetNodeIdentity(StrictModel):
    id: NodeId
    display_name: Text200
    hostname: Annotated[str, StringConstraints(max_length=255)]
    ip_address: Annotated[str, StringConstraints(max_length=45)] | None = None


def telemetry_point(value: TelemetrySampleView) -> TelemetryPoint:
    # mTLS owns node identity; the Controller owns receive time.
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
        gpu_unavailable_reason=value.gpu_unavailable_reason,
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
