"""Audit-event boundary used by API and persistent implementations."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from .models import AgentCertificate, AgentEnrollment, AgentNode, AuditEvent


@dataclass(frozen=True)
class AuditRecord:
    request_id: str
    actor: str
    action: str
    base_commit: str | None
    targets: tuple[str, ...]

    def __post_init__(self) -> None:
        """Keep audit projections free of bearer keys and private credentials."""
        values = (self.request_id, self.actor, self.action, self.base_commit, *self.targets)
        if any(_contains_secret_material(value) for value in values if value is not None):
            raise ValueError("audit record contains secret material")


def _contains_secret_material(value: str) -> bool:
    lowered = value.lower()
    return (
        "private key" in lowered
        or "begin " in lowered and " key" in lowered
        or "bearer " in lowered
        or lowered.startswith(("grant_", "token_", "v1."))
    )

@dataclass(frozen=True)
class IdentityHistoryRecord:
    node_id: str
    agent_state: str
    certificate_serial: str | None
    certificate_fingerprint: str | None
    certificate_generation: int | None
    enrolled_at: object | None
    revoked_at: object | None


def _identity_history_rows(sessions: sessionmaker[Session], limit: int) -> list[IdentityHistoryRecord]:
    from sqlalchemy import select

    with sessions() as session:
        nodes = session.scalars(select(AgentNode).order_by(AgentNode.node_id).limit(limit)).all()
        certificates = {row.node_id: row for row in session.scalars(select(AgentCertificate).order_by(AgentCertificate.generation.desc())).all()}
        enrollments = {row.node_id: row for row in session.scalars(select(AgentEnrollment).order_by(AgentEnrollment.created_at)).all()}
        return [
            IdentityHistoryRecord(
                node.node_id,
                node.state,
                certificates.get(node.node_id).serial if certificates.get(node.node_id) else None,
                certificates.get(node.node_id).fingerprint if certificates.get(node.node_id) else None,
                certificates.get(node.node_id).generation if certificates.get(node.node_id) else None,
                enrollments.get(node.node_id).created_at if enrollments.get(node.node_id) else None,
                node.revoked_at,
            )
            for node in nodes
        ]

class MemoryAuditStore:
    def __init__(self) -> None:
        self._events: list[AuditRecord] = []

    def append(self, event: AuditRecord) -> None:
        self._events.append(event)

    def for_request(self, request_id: str) -> AuditRecord:
        matches = [event for event in self._events if event.request_id == request_id]
        if len(matches) != 1:
            raise KeyError(request_id)
        return matches[0]

    def list(self, *, limit: int = 100) -> list[AuditRecord]:
        return list(reversed(self._events[-limit:]))
    def identity_history(self, *, limit: int = 100) -> list[IdentityHistoryRecord]:
        del limit
        return []


class SqlAuditStore:
    def __init__(self, sessions: sessionmaker[Session], clock) -> None:
        self._sessions = sessions
        self._clock = clock

    def append(self, event: AuditRecord) -> None:
        with self._sessions.begin() as session:
            session.add(AuditEvent(
                request_id=event.request_id,
                actor=event.actor,
                action=event.action,
                base_commit=event.base_commit,
                targets=list(event.targets),
                occurred_at=self._clock(),
            ))

    def list(self, *, limit: int = 100) -> list[AuditRecord]:
        from sqlalchemy import select

        with self._sessions() as session:
            rows = session.scalars(select(AuditEvent).order_by(AuditEvent.occurred_at.desc()).limit(limit))
            return [AuditRecord(row.request_id, row.actor, row.action, row.base_commit, tuple(row.targets)) for row in rows]
    def identity_history(self, *, limit: int = 100) -> list[IdentityHistoryRecord]:
        return _identity_history_rows(self._sessions, limit)
