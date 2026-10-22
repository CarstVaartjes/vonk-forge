"""Append-only authenticated GPU node inventory evidence."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

from pydantic import field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.inventory import (
    InventoryRequest,
)
from vonk_agent_protocol.wire_model import strict_json_datetime

from .installed_bytes import reconcile_missing_installations, refresh_installed_bytes
from .lifecycle.evidence import Residue
from .logging import log_event
from .models import AgentNode, NodeInventorySnapshot

_LOG = logging.getLogger(__name__)

# Agent inventory may lead the Controller clock by this much. Consumers that
# order it against Controller-owned events must retain the same uncertainty.
MAX_INVENTORY_FUTURE_SKEW = timedelta(seconds=30)


class NodeInventoryReport(InventoryRequest):
    """Authenticated node binding around the canonical agent report."""

    node_id: str


class InventorySnapshotView(NodeInventoryReport):
    id: str
    received_at: datetime
    evidence_digest: str
    stale: bool

    _parse_received_at = field_validator("received_at", mode="before")(
        strict_json_datetime
    )


def _validated_inventory(
    value: NodeInventoryReport | NodeInventorySnapshot, *, observed_at: datetime
) -> InventoryRequest:
    document = {
        name: getattr(value, name)
        for name in InventoryRequest.model_fields
        if name
        not in {
            "schema_version",
            "observed_at",
        }
    }
    if any(isinstance(item, Residue) for item in document.values()):
        raise ValueError("persisted inventory item is unreadable")
    document.update(schema_version=1, observed_at=observed_at.isoformat())
    validated = InventoryRequest.model_validate_json(canonical_message(document))
    for warning in validated.evidence_warnings:
        log_event(
            _LOG,
            "agent.evidence_dropped",
            service="control-api",
            code=warning.value,
            endpoint="inventory",
            node_id=value.node_id,
        )
    return validated


class InventoryRepository:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions, self._clock = sessions, clock

    def record(self, value: NodeInventoryReport) -> NodeInventorySnapshot:
        validated = _validated_inventory(value, observed_at=value.observed_at)
        validated = validated.model_copy(
            update={"capabilities": sorted(validated.capabilities)}
        )
        document = validated.model_dump(mode="json", exclude={"schema_version"})
        document["node_id"] = value.node_id
        document["capabilities"] = sorted(validated.capabilities)
        digest = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        row = NodeInventorySnapshot(
            node_id=value.node_id,
            received_at=self._clock(),
            evidence_digest=digest,
            **{
                name: getattr(validated, name)
                for name in InventoryRequest.model_fields
                if name != "schema_version"
            },
        )
        with self._sessions.begin() as session:
            session.execute(
                select(AgentNode.node_id)
                .where(AgentNode.node_id == value.node_id)
                .with_for_update(of=AgentNode)
            ).one()
            existing = session.scalar(
                select(NodeInventorySnapshot).where(
                    NodeInventorySnapshot.node_id == value.node_id,
                    NodeInventorySnapshot.observed_at == value.observed_at,
                )
            )
            if existing is not None:
                if existing.evidence_digest != digest:
                    raise ValueError(
                        "inventory identity already binds different evidence"
                    )
                return existing
            latest = session.scalar(
                select(NodeInventorySnapshot.observed_at)
                .where(NodeInventorySnapshot.node_id == value.node_id)
                .order_by(NodeInventorySnapshot.observed_at.desc())
                .limit(1)
            )
            retained_report = _validated_inventory(row, observed_at=value.observed_at)
            if latest is None or value.observed_at >= (
                latest if latest.tzinfo is not None else latest.replace(tzinfo=UTC)
            ):
                reconcile_missing_installations(
                    session,
                    value.node_id,
                    retained_report.installations,
                    observed_at=value.observed_at,
                    before=value.observed_at - MAX_INVENTORY_FUTURE_SKEW,
                )
                if isinstance(row.installation_bytes, list):
                    refresh_installed_bytes(
                        session, value.node_id, row.installation_bytes
                    )
            session.add(row)
        return row

    def latest(
        self,
        node_id: str,
        *,
        now: datetime,
        maximum_age: int,
        _session: Session | None = None,
    ) -> InventorySnapshotView:
        with (
            nullcontext(_session) if _session is not None else self._sessions()
        ) as session:
            row = session.scalar(
                select(NodeInventorySnapshot)
                .where(NodeInventorySnapshot.node_id == node_id)
                .order_by(NodeInventorySnapshot.observed_at.desc())
                .limit(1)
            )
            if row is None:
                raise KeyError(node_id)
            observed = (
                row.observed_at
                if row.observed_at.tzinfo
                else row.observed_at.replace(tzinfo=UTC)
            )
            # Validate the persisted JSON semantics using the producer's current
            # contract. A typed annotation or SQL string alone is not evidence.
            validated = _validated_inventory(row, observed_at=observed)
            return InventorySnapshotView.model_validate_json(
                canonical_message(
                    {
                        **validated.model_dump(mode="json"),
                        "id": row.id,
                        "node_id": row.node_id,
                        "received_at": row.received_at,
                        "evidence_digest": row.evidence_digest,
                        "stale": (now - observed).total_seconds() > maximum_age,
                    }
                )
            )

    def snapshot_count(self, node_id: str) -> int:
        with self._sessions() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(NodeInventorySnapshot)
                    .where(NodeInventorySnapshot.node_id == node_id)
                )
                or 0
            )
