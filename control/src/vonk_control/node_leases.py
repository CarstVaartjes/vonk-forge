"""Atomic durable ownership for node mutations and route side effects."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import AgentNode, NodeMutationLease

_OWNER_KINDS = frozenset({"reconciliation"})


class NodeLeaseConflict(RuntimeError):
    """The requested nodes are already owned or the supplied fence is stale."""

    def __init__(self, node_ids: Sequence[str]) -> None:
        self.node_ids = tuple(sorted(set(node_ids)))
        super().__init__(
            "node mutation lease conflict: " + ", ".join(self.node_ids)
        )


@dataclass(frozen=True)
class NodeLeaseGrant:
    owner_kind: str
    owner_id: str
    fence: str
    node_ids: tuple[str, ...]
    state: str


class NodeLeaseService:
    """Acquire and release one fenced lease group inside a caller transaction."""

    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        self._clock = clock

    def acquire_in_session(
        self,
        session: Session,
        node_ids: Sequence[str],
        *,
        owner_kind: str,
        owner_id: str,
    ) -> NodeLeaseGrant:
        nodes = _nodes(node_ids)
        _owner(owner_kind, owner_id)
        existing = self._rows(session, nodes)
        grant = _matching_grant(existing, nodes, owner_kind, owner_id)
        if grant is not None:
            return grant
        if existing:
            raise NodeLeaseConflict(row.node_id for row in existing)

        known = tuple(
            session.scalars(
                select(AgentNode.node_id)
                .where(AgentNode.node_id.in_(nodes))
                .order_by(AgentNode.node_id)
            )
        )
        if known != nodes:
            missing = tuple(node_id for node_id in nodes if node_id not in known)
            raise KeyError(", ".join(missing))

        fence = str(uuid.uuid4())
        now = _aware(self._clock())
        values = [
            {
                "node_id": node_id,
                "owner_kind": owner_kind,
                "owner_id": owner_id,
                "fence": fence,
                "state": "held",
                "acquired_at": now,
                "updated_at": now,
            }
            for node_id in nodes
        ]
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(NodeMutationLease)
        elif dialect == "postgresql":
            statement = postgresql_insert(NodeMutationLease)
        else:
            raise RuntimeError(f"node mutation leases do not support {dialect}")
        inserted = tuple(
            session.scalars(
                statement.values(values)
                .on_conflict_do_nothing(index_elements=["node_id"])
                .returning(NodeMutationLease.node_id)
            )
        )
        if len(inserted) != len(nodes):
            if inserted:
                session.execute(
                    delete(NodeMutationLease).where(
                        NodeMutationLease.fence == fence,
                        NodeMutationLease.node_id.in_(inserted),
                    )
                )
            conflicts = tuple(
                session.scalars(
                    select(NodeMutationLease.node_id)
                    .where(NodeMutationLease.node_id.in_(nodes))
                    .order_by(NodeMutationLease.node_id)
                )
            )
            raise NodeLeaseConflict(conflicts or nodes)
        return NodeLeaseGrant(owner_kind, owner_id, fence, nodes, "held")

    def owned_grant_in_session(
        self,
        session: Session,
        node_ids: Sequence[str],
        *,
        owner_kind: str,
        owner_id: str,
    ) -> NodeLeaseGrant | None:
        """Recover a held or release-pending grant after process restart."""

        nodes = _nodes(node_ids)
        _owner(owner_kind, owner_id)
        rows = self._rows(session, nodes)
        if not rows:
            return None
        fences = {row.fence for row in rows}
        states = {row.state for row in rows}
        if (
            len(rows) != len(nodes)
            or tuple(row.node_id for row in rows) != nodes
            or len(fences) != 1
            or len(states) != 1
            or any(
                row.owner_kind != owner_kind or row.owner_id != owner_id
                for row in rows
            )
        ):
            raise NodeLeaseConflict(row.node_id for row in rows)
        return NodeLeaseGrant(
            owner_kind,
            owner_id,
            fences.pop(),
            nodes,
            states.pop(),
        )

    def mark_releasing_in_session(
        self, session: Session, grant: NodeLeaseGrant
    ) -> None:
        rows = self._grant_rows(session, grant)
        if any(row.state not in {"held", "releasing"} for row in rows):
            raise NodeLeaseConflict(grant.node_ids)
        now = _aware(self._clock())
        for row in rows:
            row.state = "releasing"
            row.updated_at = now

    def release_in_session(self, session: Session, grant: NodeLeaseGrant) -> None:
        rows = self._grant_rows(session, grant)
        if any(row.state != "releasing" for row in rows):
            raise NodeLeaseConflict(grant.node_ids)
        for row in rows:
            session.delete(row)

    @staticmethod
    def _rows(
        session: Session, node_ids: tuple[str, ...]
    ) -> tuple[NodeMutationLease, ...]:
        return tuple(
            session.scalars(
                select(NodeMutationLease)
                .where(NodeMutationLease.node_id.in_(node_ids))
                .order_by(NodeMutationLease.node_id)
                .with_for_update(of=NodeMutationLease)
            )
        )

    def _grant_rows(
        self, session: Session, grant: NodeLeaseGrant
    ) -> tuple[NodeMutationLease, ...]:
        _owner(grant.owner_kind, grant.owner_id)
        nodes = _nodes(grant.node_ids)
        _uuid(grant.fence, "node mutation lease fence")
        rows = self._rows(session, nodes)
        if (
            len(rows) != len(nodes)
            or any(
                row.owner_kind != grant.owner_kind
                or row.owner_id != grant.owner_id
                or row.fence != grant.fence
                for row in rows
            )
        ):
            raise NodeLeaseConflict(nodes)
        return rows


def _matching_grant(
    rows: tuple[NodeMutationLease, ...],
    nodes: tuple[str, ...],
    owner_kind: str,
    owner_id: str,
) -> NodeLeaseGrant | None:
    fences = {row.fence for row in rows}
    if (
        len(rows) == len(nodes)
        and tuple(row.node_id for row in rows) == nodes
        and len(fences) == 1
        and all(
            row.owner_kind == owner_kind
            and row.owner_id == owner_id
            and row.state == "held"
            for row in rows
        )
    ):
        return NodeLeaseGrant(owner_kind, owner_id, fences.pop(), nodes, "held")
    return None


def _nodes(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("node mutation lease targets are invalid")
    nodes = tuple(sorted(values))
    if not nodes or len(nodes) != len(set(nodes)) or any(not value for value in nodes):
        raise ValueError("node mutation lease targets are invalid")
    return nodes


def _owner(kind: str, owner_id: str) -> None:
    if kind not in _OWNER_KINDS:
        raise ValueError("node mutation lease owner kind is invalid")
    _uuid(owner_id, "node mutation lease owner")


def _uuid(value: str, name: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{name} is invalid") from error
    if str(parsed) != value:
        raise ValueError(f"{name} is invalid")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
