"""Fleet projection: service concerns."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    CertificateState,
    InstallationState,
    InstallDegradedReason,
    ReservationState,
    RunState,
)

from ..auth import CursorError
from ..fleet_event_contract import NodeProfilePayload
from ..fleet_events import FleetEventDraft, FleetEventRepository
from ..models import (
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
from ..observation_capture import begin_observation_capture
from ..operation_blockers import PHASE_RETRY_CODE, read_blockers
from ..recipe_update_notice import newest_active_revisions
from ..telemetry import CPU_LOW_CLOCK_MIN_SECONDS, TelemetryRepository
from .common import (
    _AUTHORITY_REVISION,
    _LOW_CLOCK_LOOKBACK_SLACK,
    _MEMBER_COORDINATES,
    FleetNodeIdentity,
    FleetSnapshot,
    InstallationPresenceRow,
    RunPresenceRow,
    _utc,
)
from .nodes import _connection, _inventory, _node, _telemetry_state
from .presence import _installed_presence, _loaded_presence


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
        return self.read_at(None)

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
                        payload=NodeProfilePayload(
                            node_id=node_id, display_name_changed=True
                        ),
                    ),
                )
            return FleetNodeIdentity(
                id=node_id,
                display_name=profile.display_name,
                hostname=profile.hostname,
                ip_address=(None if presence is None else presence.management_address),
            )

    def read_at(self, event_cursor: int | None) -> FleetSnapshot:
        # A supplied cursor is a replay boundary, not historical reconstruction.
        # Normal capture reads its committed boundary in the same SQL snapshot.
        if event_cursor is not None and (
            type(event_cursor) is not int
            or not 0 <= event_cursor <= 9_223_372_036_854_775_807
        ):
            raise CursorError("Fleet event cursor is invalid")
        current = _utc(self._clock())
        with self._sessions() as session:
            begin_observation_capture(session)
            if event_cursor is None:
                event_cursor = self._events.high_watermark_in_session(session)
            agents = self._registered_agents(session)
            node_ids = tuple(agents)
            profiles = self._node_profiles(session, node_ids)
            presences = self._node_presences(session, node_ids)
            certificates = self._current_certificates(session, node_ids, current)
            inventories = self._latest_inventory(session, node_ids)
            unreadable_telemetry: set[str] = set()
            telemetry = self._telemetry.latest_in_session(
                session, node_ids, unreadable_node_ids=unreadable_telemetry
            )
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
                    telemetry_unreadable=node_id in unreadable_telemetry,
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
            )
        )
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
            )
        )

    @staticmethod
    def _mapping_members(
        rows: Sequence[ClusterMappingNode],
    ) -> dict[str, tuple[ClusterMappingNode, ...]]:
        grouped: dict[str, list[ClusterMappingNode]] = {}
        for row in rows:
            grouped.setdefault(row.mapping_id, []).append(row)
        return {mapping_id: tuple(values) for mapping_id, values in grouped.items()}

    @staticmethod
    def _exact_group_reason(
        *,
        expected_count: int,
        expected: Sequence[ClusterMappingNode],
        actual: Sequence[InstallationNode | RunNode],
        fleet_node_ids: frozenset[str],
    ) -> str | None:
        # Invalid persisted coordinates are unknown evidence, not a proven
        # missing rank. The owning group catches validation and retains members.
        for value in (*expected, *actual):
            _MEMBER_COORDINATES.validate_python((value.rank, value.role), strict=True)
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
        ):
            raw = progress.get("blockers") if isinstance(progress, Mapping) else None
            for blocker in read_blockers(raw):
                if blocker.code != PHASE_RETRY_CODE:
                    continue
                for node_id in blocker.node_ids:
                    if node_id in known:
                        stalls.setdefault(node_id, []).append(blocker.detail)
        return {node_id: tuple(details) for node_id, details in stalls.items()}

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

    _installed_presence = _installed_presence
    _loaded_presence = _loaded_presence
    _node = _node
    _connection = _connection
    _inventory = _inventory
    _telemetry_state = _telemetry_state
